# Nginx Stream TCP Brutal v2

在服务端按 TLS SNI 选择公网客户端 TCP 连接，为 Nginx → 客户端的下载方向启用 TCP Brutal v2。客户端上传和其他 SNI 的拥塞算法不由模块修改。

## 配置

在 main 作用域加载模块，在已有 SNI 分流的 Stream `server` 中增加一行：

```nginx
load_module /etc/nginx/modules/ngx_stream_brutal_module.so;

events {}

stream {
    error_log /var/log/nginx/error.log warn;

    map $ssl_preread_server_name $backend {
        vless.example.com 127.0.0.1:10001;
        # 在实际部署中保留你原有的其他映射。
        default             127.0.0.1:1;
    }

    server {
        listen 443 reuseport;
        listen [::]:443 reuseport;
        ssl_preread on;

        brutal_sni vless.example.com download=50;

        proxy_pass $backend;
    }
}
```

这是完整语法示例，不应覆盖现有配置。模块不会自动加载或自动启用。如果 Stream 本身也是 `.so`，先加载 Stream，再加载本模块。

`download=50` 表示 **50 MB/s**，即 400 Mbps；仅填写数字，固定十进制 MB/s，支持最多六位小数。它是所有命中该 SNI 的连接共享的目标速度，并不是每连接各 50 MB/s。按客户端实际可承受带宽设置；Brutal 无法突破线路容量，重传和协议开销可能使网卡流量超过该数字。

每个 `server` 最多一条 `brutal_sni`。SNI 大小写不敏感、精确匹配，不接受通配符、正则或尾部点。无 SNI、其他 SNI 不修改算法。只支持 IPv4/IPv6 TCP + `ssl_preread` 透传，不支持 `listen ssl`、UDP 或 Unix socket。

`gain=20` 和稳定的非零 group ID 在内部处理，无需填写。相同 socket UID、netns、SNI 的连接共享 group，包括跨 worker、IPv4/IPv6 和不同监听端口。第一版针对单个 Nginx 实例；另一实例在同 UID/netns 使用相同 SNI 会共享速度。

## 构建

从项目根目录执行：

```sh
docker build --build-arg NGINX_VERSION=1.30.2 -t ngx-brutal:local .
```

最终镜像基于同版本官方 nginx，保留现有第三方模块。构建自动验证 `.so` 加载，并在单独测试阶段运行配置和故障注入回归；Python、编译器和测试探针不进入最终镜像。

独立编译模块也可使用：

```sh
./configure --with-compat --with-stream --with-stream_ssl_preread_module \
    --add-dynamic-module=/path/to/ngx_stream_brutal_module
make modules
```

必须与运行中的 Nginx 精确版本、构建条件和架构兼容。当前验证版本为 1.30.2；`--with-compat` 不表示可以跨版本加载。修改 `.so` 后用新镜像重建容器，不依赖原进程 reload 替换已加载的动态模块。

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

正常运行使用 warn 日志。排障可暂时在 `stream` 或其 `server` 中设为 debug；需要带 `--with-debug` 的运行 nginx。HTTP 块内的 `error_log` 不会配置 Stream 日志。

成功的 debug 日志包含 SNI、客户端 fd、原算法、字节/秒速率、group ID 和 v2 版本。Nginx 日志本身带 PID 和连接号。故障日志包含以下步骤：

| step | 含义 |
| --- | --- |
| `read-original` | 不能读取原算法，未尝试修改 |
| `already-brutal` | 原连接已由外部配置启用 Brutal，本模块不接管 |
| `switch` | 切换算法失败，如内核模块不可用或权限不足 |
| `version` | 不是支持的 v2 接口，尝试回滚 |
| `read-initial` / `rule-conflict` | 初始参数不可读或已属于其他规则 group，尝试回滚 |
| `set-params` / `verify` | 参数设置或读回失败，尝试回滚 |
| `rollback` | 恢复算法失败，以 alert 记录，仍继续代理 |

`original-restored` 表示已读回确认原算法名称；不表示恢复此前全部内核内部状态。相同故障每 worker/配置/类别每 10 秒最多一条常规日志，后续及 worker 退出时汇总抑制数量，debug 可见逐次故障。

若原连接就已经是 Brutal，或者切换时加入 destination rule group，本模块不会改其 group 参数。部署前检查已有规则，避免混用覆盖目标连接的 brutalctl 规则；不要为测试清空系统规则。已有 sing-box multiplex Brutal 控制的是它自己的 socket，不等于 Nginx 公网 socket；是否保留需结合实际客户端协商验证。

## reload 与回退

- 改速率并 reload：新 worker 首条成功初始化的连接更新共享 group，旧成员随后使用新参数。新旧 worker 交接期不保证参数原子切换。
- 删除 `brutal_sni` 并 reload：新连接不启用，旧连接保持到自然关闭。
- 改 SNI：新旧 SNI 的 group 在过渡期可能同时存在，合计不受单一旧速率约束。
- 如需立即结束旧连接，重启会中断它们；先保留原镜像与原配置。
- 不自动修改 Compose。部署新镜像需要让现有 nginx 服务引用该镜像，并挂载匹配的配置。

## 实现边界与发布状态

模块包装已配置的 content handler，不依赖 ssl_preread 私有 TLS context。配置检查使用 `ssl_preread` 指令自身的 flag 元数据，并检查 TCP 监听及 handler 存在；Nginx 公共接口不能证明该函数一定来自 `proxy_pass`。支持与验收的生产用法限定为 `proxy_pass`，不混用其他 content handler 模块。

通过构建和短时集成测试不等于已经完成长期稳定性验收。24 小时压力/长连接测试、真实 VLESS + Reality 下载对比和生产发布属于独立验收项，见 [规格](../docs/ngx-stream-brutal-spec.md) 与 [测试记录](../docs/testing.md)。
