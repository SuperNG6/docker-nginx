# Nginx Stream TCP Brutal v2

在服务端按 TLS SNI 选择公网客户端 TCP 连接，为 Nginx → 客户端的下载方向启用 TCP Brutal v2。客户端上传和其他 SNI 的拥塞算法不由模块修改。

## 配置

在 main 作用域加载模块，在已有 SNI 分流的 Stream `server` 中添加目标域名：

```nginx
load_module /etc/nginx/modules/ngx_stream_brutal_module.so;
error_log /var/log/nginx/error.log notice;

events {}

stream {
    error_log /var/log/nginx/error.log warn;
    log_format brutal '$remote_addr sni=$ssl_preread_server_name '
        'brutal=$brutal_status rate_Bps=$brutal_rate gain=$brutal_gain '
        'group=$brutal_group error=$brutal_error recovery=$brutal_recovery '
        'group_state=$brutal_group_state sent=$bytes_sent session=$session_time';
    access_log /var/log/nginx/stream-access.log brutal;

    map $ssl_preread_server_name $backend {
        vless.example.com 127.0.0.1:10001;
        video.example.com 127.0.0.1:10002;
        # 在实际部署中保留你原有的其他映射。
        default             127.0.0.1:1;
    }

    server {
        listen 443 reuseport;
        listen [::]:443 reuseport;
        ssl_preread on;

        brutal_sni vless.example.com download_mbps=300;
        brutal_sni video.example.com download_mbps=200 gain=15;

        proxy_pass $backend;
    }
}
```

这是完整语法示例，不应覆盖现有配置。模块不会自动加载或自动启用。如果 Stream 本身也是 `.so`，先加载 Stream，再加载本模块。

语法为 `brutal_sni <SNI> download_mbps=<整数> [gain=<整数>];`，两个参数的顺序不限。

`download_mbps=300` 表示 **300 Mbps**，内部转换为 37,500,000 bytes/s。只接受十进制整数 `1～1000000`，不接受小数（包括 `300.0`）、正负号、指数或单位后缀。旧 `download=` 已移除，原 `download=50` 若要保留相同带宽应改为 `download_mbps=400`。

每个 `server` 可配置多个不同 SNI，每个 SNI 分别共享自己的目标速度；不是每条连接各享有该速度。同一 server 内重复 SNI 会报错；跨 server 的同一 SNI 必须使用相同的速率和 gain。SNI 大小写不敏感、精确匹配，不接受通配符、正则或尾部点。无 SNI、其他 SNI 不修改算法。只支持 IPv4/IPv6 TCP + `ssl_preread` 透传，不支持 `listen ssl`、UDP 或 Unix socket。

`gain` 可选，默认 **20**，允许整数 **5～80**，单位为十分之一（`15` = 1.5 倍、`20` = 2.0 倍 CWND 增益），不直接改变配置带宽。稳定的非零 group ID 由模块按 SNI 生成。相同 socket UID、netns、SNI 的连接共享 group，包括跨 worker、IPv4/IPv6、不同监听端口和同 UID/netns 的另一实例。

按客户端实际可承受带宽设置；Brutal 无法突破线路容量，丢包补偿、重传、协议开销和突发可能使网卡流量超过配置目标，这不是严格出口限速。

## 构建

从项目根目录执行：

```sh
docker build --build-arg NGINX_VERSION=1.30.4 -t ngx-brutal:local .
```

最终镜像基于同版本官方 nginx，保留现有第三方模块。构建自动验证 `.so` 加载，并在单独测试阶段运行配置和故障注入回归；Python、编译器和测试探针不进入最终镜像。

独立编译模块也可使用：

```sh
./configure --with-compat --with-stream --with-stream_ssl_preread_module \
    --add-dynamic-module=/path/to/ngx_stream_brutal_module
make modules
```

必须与运行中的 Nginx 精确版本、构建条件和架构兼容。当前验证版本为 1.30.2 和 1.30.4；`--with-compat` 不表示可以跨版本加载。修改 `.so` 后用新镜像重建容器，不依赖原进程 reload 替换已加载的动态模块。

宿主机需安装 TCP Brutal v2；Docker 共享宿主机内核。模块不调用 brutalctl、不改默认算法或路由，也不需要为本功能默认增加 privileged/NET_ADMIN。`nginx -t` 不会试图切换测试 socket；内核缺失时运行连接会记录告警并继续代理。

## 测试

构建带测试工具的独立镜像：

```sh
docker build --target test -t ngx-brutal:test .
```

在支持 Brutal v2 的宿主机运行真实 socket 集成测试：

```sh
docker run --rm --network none --entrypoint python3 ngx-brutal:test \
    /usr/src/ngx_stream_brutal_module/tests/integration.py \
    --nginx /usr/sbin/nginx \
    --module /etc/nginx/modules/ngx_stream_brutal_module.so \
    --probe /tmp/sockopt_probe.so --mode real
```

真实多连接下载测试（1/4/16 连接，每组预热 10 秒、测量 60 秒、重复 3 次）：

```sh
mkdir -p /tmp/ngx-brutal-results
docker run --rm --network none \
    --mount type=bind,src=/tmp/ngx-brutal-results,dst=/results \
    --entrypoint python3 ngx-brutal:test \
    /usr/src/ngx_stream_brutal_module/tests/throughput.py \
    --nginx /usr/sbin/nginx \
    --module /etc/nginx/modules/ngx_stream_brutal_module.so \
    --probe /tmp/sockopt_probe.so --output /results/throughput.json
```

全部监听使用独立回环临时端口；这些命令不挂载生产配置，不使用主机网络，也不设置系统规则。探针的 fake 模式模拟 socket 接口以注入故障，real 模式转发真实内核调用并记录结果；探针不属于生产模块。

保持旧下载连接跨 reload 的验证，可用同样参数运行 `tests/lifecycle.py`。长时间连接反复建立关闭、保活、reload 和资源采样使用 `tests/soak.py`，默认运行 86400 秒，需要增加 `--output /results/soak.json` 并挂载结果目录。短时诊断可额外指定 `--seconds 60 --reload-every 15`，不能作为 24 小时验收。长测结束后仍需检查记录中的资源趋势与宿主机内核日志。

## 故障与日志

正常运行 Stream 使用 `warn`，通过 access log 查看每连接结果，无需打开 debug。Access log 在会话结束时写入，长连接尚未结束时不会立即出现；可暂时打开 Stream debug 查看初始化成功事件（运行 nginx 需要 `--with-debug`）。HTTP 块内的日志设置不控制 Stream。

| 变量 | 含义 |
| --- | --- |
| `$brutal_status` | `not-enabled` 未由模块初始化；`enabled` 初始化验证成功；`external` 原算法已是 Brutal、未接管；`failed` 初始化失败；`skipped-worker-exiting` worker 已进入优雅退出，跳过初始化并正常代理 |
| `$brutal_enabled` | 本模块初始化验证成功为 `1`，其余为 `0`；不代表当前 socket 一定未使用 Brutal |
| `$brutal_rate` / `$brutal_gain` / `$brutal_group` | 成功验证时的 bytes/s、gain、group 快照；未成功为 `-` |
| `$brutal_version` | 已验证支持的内核 API 编码，如 `131072`（2.0.0）；未知或不支持为 `-` |
| `$brutal_original_cc` | 初始化时读到的原算法；未知为 `-` |
| `$brutal_error` | 首个失败步骤；无错误为 `none`；外部 Brutal 为 `already-brutal` |
| `$brutal_recovery` | `none`、`not-modified`、`external-brutal-untouched`、`original-restored`、`restore-failed` |
| `$brutal_group_state` | `untouched` 未成功写入 group 参数；`applied` 写入且验证成功；`may-have-changed` 写入成功但验证失败 |

这些变量保存**连接初始化结果**，不在日志阶段查询内核，也不表示 reload 后共享 group 的当前参数。错误日志按 worker/SNI/失败类别限频，access log 保留每连接结果。`not-enabled` 也覆盖未到达 content handler 的连接；内存分配失败会在 error log 中记录 alert。

模块版本为 `0.2.0`。每个 worker 启动时（包括 reload 新 worker）向 **main error_log** 写一次 `notice`：

```text
brutal: module=0.2.0 built=<编译日期和时间> brutal_api_major=2 rate_unit=Mbps default_gain=20
```

主配置需允许 `notice` 才能看到，Stream 可以继续保持 `warn`。`nginx -t` 不启动 worker，不输出该启动记录。构建时间为编译环境时间，不承诺时区；不引入 commit 固定或额外构建参数。

成功的 debug 日志包含 SNI、客户端 fd、原算法、字节/秒速率、group ID 和 v2 版本。Nginx 日志本身带 PID 和连接号。故障日志包含以下步骤：

| step | 含义 |
| --- | --- |
| `read-sni` | SNI 变量读取失败，记录 `failed/read-sni`；`sni=-`，按 server 独立限频，不操作 socket |
| `read-original` | 不能读取原算法，未尝试修改 |
| `already-brutal` | 原连接已由外部配置启用 Brutal，本模块不接管 |
| `switch` | 切换算法失败，如内核模块不可用或权限不足 |
| `version` | 不是支持的 v2 接口，尝试回滚 |
| `read-initial` / `rule-conflict` | 初始参数不可读或已属于其他规则 group，尝试回滚 |
| `set-params` / `verify` | 参数设置或读回失败，尝试回滚 |
| `rollback` | 恢复算法失败，以 alert 记录，仍继续代理 |

`original-restored` 表示已读回确认原算法名称；不表示恢复此前全部内核内部状态，也不撤销已写入的共享 group 参数；此时日志明确标记 `group_state=may-have-changed`。相同故障每 worker/SNI 配置/类别每 10 秒最多一条常规日志，后续及 worker 退出时汇总抑制数量，debug 可见逐次故障。

若原连接就已经是 Brutal，或者切换时加入 destination rule group，本模块不会改其 group 参数。部署前检查已有规则，避免混用覆盖目标连接的 brutalctl 规则；不要为测试清空系统规则。已有 sing-box multiplex Brutal 控制的是它自己的 socket，不等于 Nginx 公网 socket；是否保留需结合实际客户端协商验证。

## reload 与回退

- 改速率或 gain 并 reload：新 worker 首条成功初始化的连接更新共享 group，旧成员随后使用新参数。新旧 worker 交接期不保证参数原子切换。worker 已进入优雅退出状态时，尚未开始的 Brutal 初始化会跳过并记录 `skipped-worker-exiting`，连接沿用原算法正常代理；相同配置 reload 和删除指令也适用。已启用 Brutal 的连接不受此检查影响。检查前已开始初始化或尚未观察到退出状态的旧 worker 仍可能写入，因此这不是跨 worker 的原子更新保证。
- 删除 `brutal_sni` 并 reload：新连接不启用，旧连接保持到自然关闭。
- 改 SNI：新旧 SNI 的 group 在过渡期可能同时存在，合计不受单一旧速率约束。
- 如需立即结束旧连接，重启会中断它们；先保留原镜像与原配置。
- 不自动修改 Compose。部署新镜像需要让现有 nginx 服务引用该镜像，并挂载匹配的配置。

## 实现边界与发布状态

模块包装已配置的 content handler，不依赖 ssl_preread 私有 TLS context。配置检查使用 `ssl_preread` 指令自身的 flag 元数据，并检查 TCP 监听及 handler 存在；Nginx 公共接口不能证明该函数一定来自 `proxy_pass`。支持与验收的生产用法限定为 `proxy_pass`，不混用其他 content handler 模块。

通过构建和短时集成测试不等于已经完成长期稳定性验收。24 小时压力/长连接测试、真实 VLESS + Reality 下载对比和生产发布属于独立验收项，见 [规格](../docs/ngx-stream-brutal-spec.md) 与 [测试记录](../docs/testing.md)。
