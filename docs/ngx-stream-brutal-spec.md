# Nginx Stream TCP Brutal v2 模块：产品需求与技术规格

- 文档版本：1.3
- 日期：2026-09-06
- 状态：MVP 已实现并进入验证；实际通过项与未完成项见 [测试记录](testing.md)
- 适用场景：单个服务端 Nginx，通过 TLS SNI 分流到网站和多个 sing-box
- 模块产物：`ngx_stream_brutal_module.so`

## 1. 产品目标

在服务端 Nginx 根据 TLS SNI 分流 TCP 流量时，只对指定 SNI 对应的公网客户端连接启用 TCP Brutal v2，改善该 sing-box 代理的客户端下载表现。

用户只配置目标 SNI 和下载目标速度。客户端上传保持客户端原有拥塞控制；其他 SNI、网站、sing-box 的连接不由本模块修改。客户端公网 IP 变化不需要调整配置。

“加速”指在适合 Brutal 的线路上改善吞吐，并不保证所有网络都快于 BBR/CUBIC。目标速度应按客户端实际可承受的下载带宽设置，不能据此突破线路容量。

## 2. 架构与选择依据

```text
客户端 ←── 公网 TCP ──→ Nginx ←── 内部 TCP ──→ sing-box
                         │
                         └─ 读取 SNI，仅为命中连接的公网发送端启用 Brutal
```

Nginx 已经用 SNI 选择后端，因此复用这个条件，不额外识别容器名称、容器 IP 或客户端 IP。模块只操作 Nginx 接受的客户端 socket；对内部连接启用 Brutal不能控制公网下载发送端。

客户端下载是 Nginx 向客户端发送数据；客户端上传的发送端位于客户端。本模块不修改客户端，也不实现上传控制。

### 上传方向的能力边界

仅修改服务端 Nginx 模块，无法给远端客户端的发送 socket 设置 Brutal；这是控制位置的限制，不是增加一个上传参数或投入更多开发即可解决。

上传 Brutal 本身可以实现，但需要客户端发送端具备并启用对应能力。服务端接收限速也可以开发，不过那只是限速/背压，不是上传 Brutal。对于本项目“优先改善下载、上传保持原算法”的目标，两者均不进入 MVP。

## 3. MVP 范围

### 必须实现

1. 可单独编译和加载的 Nginx Stream 动态模块，无需修改 Nginx 源码。
2. 每个 Stream `server` 最多配置一个精确 SNI 和一个下载目标速度。
3. 命中后只修改客户端 TCP socket；未命中不调用改变其拥塞控制的接口。
4. 命中同一 SNI 的多连接共享一个内核 group，总目标速度不乘以连接数。
5. 支持多个 Nginx worker、IPv4 和 IPv6，不依赖客户端 IP。
6. 运行时启用失败继续代理，并在已经切换算法时尝试回滚。
7. 明确的配置检查、错误日志、调试验证方法、构建与回退说明。

### 不在第一版实现

- 上传 Brutal、上传限速、客户端改造或带宽协商。
- 多租户、按用户认证分组、同一 `server` 多条匹配规则。
- 通配符、正则、容器发现、IP 匹配、动态 API、管理页面。
- 自动测速、自适应带宽、用户态 group 调度、定时遍历存量连接。
- UDP/QUIC、TLS 终止场景、对 ECH 内层 SNI 的识别。
- 全局默认算法、路由、qdisc、sysctl 或 brutalctl 规则的自动修改。

## 4. 用户配置契约

```nginx
load_module modules/ngx_stream_brutal_module.so;

stream {
    upstream singbox_target {
        server 127.0.0.1:10001; # 示例：替换为真实后端
    }

    upstream default_backend {
        server 127.0.0.1:10443; # 示例：替换为真实后端
    }

    map $ssl_preread_server_name $backend {
        vless.example.com singbox_target;
        default           default_backend;
    }

    server {
        listen 443;
        ssl_preread on;

        brutal_sni vless.example.com download=50;

        proxy_pass $backend;
    }
}
```

示例中的域名、端口和速度均非部署默认值。若 Stream 本身为动态模块，必须先加载 Stream 模块，再加载本模块。

| 项目 | 规定 |
| --- | --- |
| 指令 | `brutal_sni <sni> download=<number>;` |
| 作用域 | 仅 `stream` 内的 `server`，不提供上层继承 |
| 默认状态 | 不配置即不启用，无隐式 SNI 或默认下载速度 |
| SNI | ASCII 域名或 IDNA A-label；转小写后精确匹配，不做 DNS 查询 |
| 尾部点 | 配置域名不接受尾部点；收到带尾部点的 SNI 不匹配 |
| 单位 | 参数只填写数字，固定按 MB/s 解释，不填写单位后缀；1 MB = 1,000,000 字节 |
| 数值 | 正十进制整数或最多六位小数；不接受指数、负数、NaN、零 |
| 范围 | 0.0625～125000 MB/s，对应当前 v2 内核允许范围；不是推荐带宽 |
| 关闭 | 删除该行并 reload；只对新连接停止启用 |

`download=50` 表示 50 MB/s，对应 50,000,000 字节/秒，即 400 Mbps。使用定点整数解析，检查乘法和累加溢出，不通过浮点数转换。

重复指令、未知参数、非法域名、带单位后缀、越界值应使 `nginx -t` 失败。支持的生产用法限定为 `ssl_preread on` 和 `proxy_pass`；不支持的监听类型和 TLS 终止配置明确拒绝。实现使用指令自身 flag 元数据检查 ssl_preread，并检查 content handler 存在。Nginx 公共接口不提供该 handler 的来源身份；为避免复制 proxy 模块私有结构，第一版不在配置期证明它一定来自 proxy_pass，不支持混用其他 content handler。这是配置验证边界，非额外代理功能。

不同 `server` 可以各自使用本指令；同一 Nginx 配置内相同规范化 SNI 必须配置相同速度，否则配置检查失败。

## 5. 源码依据与版本基线

实现依据为上游 TCP Brutal 源码，提交号为：

```text
377d2a0e9324ef585ff90ea91779baf276cf6a50
```

该提交的 `brutal.h` 标注版本 2.0.0。部署时须核对宿主机实际加载版本；参考源码版本不能证明运行中的模块版本。

| 源码位置 | 已确认行为 | 对模块设计的影响 |
| --- | --- | --- |
| `brutal.h` | 参数为 packed 20 字节，rate 单位 bytes/s，gain 默认 20 | 使用明确 ABI 和编译期长度检查 |
| `brutal_sockopt.c: brutal_group_get` | 按 ID、`sk_uid`、netns 查找 group | 不按 worker PID 分组；需测实际 socket UID |
| `brutal_sockopt.c: brutal_set_params` | 设置成员参数更新整个 group；锁定规则返回 EPERM | 固定配置一致性，定义 reload 行为 |
| `brutal_sockopt.c: brutal_get_params` | 可读回生效参数和 group ID | 不仅依赖 setsockopt 成功或日志 |
| `brutal_cc.c: brutal_init/brutal_release` | 初始化匹配规则；释放时离组并卸载 socket 接口 | 检查规则冲突，连接释放由内核处理 |
| `brutal_cc.c: brutal_effective_rate` | 对丢包补偿发送速率 | 配置值不是严格网卡流量上限 |
| `brutal_rules.c: brutal_apply_rule` | 启用算法时可能加入目的地址规则的 group | 不能认为手动启用一定避开已有规则 |

上游固定版本参考：[参数定义](https://github.com/HyNetworks/tcp-brutal/blob/377d2a0e9324ef585ff90ea91779baf276cf6a50/brutal.h)、[socket 与 group 实现](https://github.com/HyNetworks/tcp-brutal/blob/377d2a0e9324ef585ff90ea91779baf276cf6a50/brutal_sockopt.c)、[拥塞控制实现](https://github.com/HyNetworks/tcp-brutal/blob/377d2a0e9324ef585ff90ea91779baf276cf6a50/brutal_cc.c)、[规则实现](https://github.com/HyNetworks/tcp-brutal/blob/377d2a0e9324ef585ff90ea91779baf276cf6a50/brutal_rules.c)。

## 6. Nginx 集成方案

在模块 postconfiguration 中，只对显式启用本指令的 `server` 保存并包装原 content handler。包装函数读取已经完成 preread 的 `$ssl_preread_server_name`，完成一次 socket 初始化后调用保存的原 handler。

选择依据：`proxy_pass` 设置 Stream core 的 content handler，content 阶段位于 preread 之后。简单注册“位于 ssl_preread 后面”的 preread handler 并不可靠：阶段内执行顺序和 `NGX_OK` 跳转可能使其被跳过。

实现约束：

- 只使用客户端 `s->connection->fd`；不修改监听 fd 或 `s->upstream` 的连接。
- 使用 Nginx 变量接口获取 SNI，不直接依赖 ssl_preread 私有 context 布局。
- 不提前读取、缓存尚未解析的 SNI，不自行读取或消费 ClientHello。
- 未命中、SNI 不可用直接调用原 handler；原 Nginx 对畸形握手、超时的处理保持有效。
- 保存原 handler 时检查空指针和重复包装，防止递归；确保正常、跳过和失败路径均只调用原 handler 一次。
- 所有每连接状态使用连接内存池，按连接释放；不保存无生命周期保证的全局 session 指针。
- 参数设置只在连接进入代理前发生一次，不增加逐包过滤器、线程或外部命令。
- 对选定 Nginx 版本检查 handler 生命周期和其他已安装 Stream 模块的兼容性；此方案需经过实际集成测试才能视为成立。

Nginx 参考：[proxy_pass 设置 handler](https://github.com/nginx/nginx/blob/master/src/stream/ngx_stream_proxy_module.c)、[阶段构建](https://github.com/nginx/nginx/blob/master/src/stream/ngx_stream.c)、[阶段执行](https://github.com/nginx/nginx/blob/master/src/stream/ngx_stream_core_module.c)。这些链接是可变分支，发布前必须记录实际构建版本。

## 7. 内部参数与共享组

- `cwnd_gain` 固定为 20，不暴露配置。
- `group_id` 为规范化 SNI 的稳定非零 64 位标识。
- 确定算法为 FNV-1a 64：输入 ASCII 字节串 `ngx_stream_brutal:v1:` 后接小写 SNI，按标准 64 位无符号回绕；结果为 0 时改为 1。
- 不把带宽、PID、时间、客户端 IP、地址族或监听端口加入 ID。
- 配置阶段检测本配置内不同 SNI 的 ID 碰撞并拒绝加载。
- 同一 socket UID/netns 下，相同 SNI 在多个监听端口间也共享速度，属于明确设计行为。
- 不管理用户态共享内存；连接关闭和内核 group 生命周期交给 Brutal。

第一版支持单个 Nginx 实例。相同 UID/netns 下的另一实例如果也使用同一 SNI，会共享 group；与第三方应用人为使用相同 ID 也可能冲突。哈希不是安全隔离或跨应用唯一性保证，第一版不提供多实例命名空间配置。

内核参数使用固定宽度原生整数与 packed 布局，不使用网络字节序。编译期断言总长 20 字节和字段偏移；不直接包含内核私有头文件作为用户态依赖。

## 8. 连接初始化与故障恢复

按顺序执行：

1. 确认 TCP、命中 SNI，读取并保存原 `TCP_CONGESTION`。
2. 原算法读取失败：记录告警，跳过启用，正常代理。
3. 原算法已经是 `brutal`：视为外部配置介入，记录告警并保持原状；不接管其 group，不宣称本模块启用成功。
4. 将算法切换为 `brutal`；失败则记录 errno 并正常代理。
5. 读取 `TCP_BRUTAL_VERSION=23302`，检查返回长度为 4，第一版接受 major=2；v1、未知主版本或读取失败进入回滚。
6. 读取 20 字节当前参数；如果切换刚完成就已有非零 group，视为 destination rule 冲突，进入回滚，不修改该 group。
7. 通过 `TCP_BRUTAL_PARAMS=23301` 写入 rate、gain、group；失败进入回滚。
8. 读回算法、参数和返回长度，核对生效值；失败或不符进入回滚。
9. 成功则记录 debug 信息并进入原代理逻辑。

回滚：恢复保存的原拥塞算法，并读回确认。回滚成功以 warn 记录原始失败；回滚失败以 alert 记录实际结果；两种情况均继续原代理逻辑，不因本模块错误主动断开连接。

恢复算法名称不代表恢复旧算法此前的全部内部状态；设置 group 成功后发生读回错误，回滚该 socket 也不会撤销已对其他 group 成员生效的参数。不得记录虚假的“全部恢复成功”。

缺少内核模块是运行时降级，不让全部网站因此无法启动；非法配置是配置期错误。`nginx -t` 只证明配置与模块加载可用，不能证明真实 socket 可启用 Brutal。

## 9. reload、关闭与重启

| 操作 | 行为 |
| --- | --- |
| 相同配置 reload | 新旧连接继续使用相同 group，前提是 socket UID/netns 一致 |
| 修改速度后 reload | 新 worker 首个成功设置参数的连接更新 group；已有成员在后续内核发送处理中采用新值 |
| 新旧 worker 交接 | 极短交接期若仍有旧配置连接执行初始化，可能再次写入旧值；不承诺原子切换，验收以旧 worker 停止接收后的稳定状态为准 |
| 删除指令后 reload | 新连接不由模块启用，已有连接保持到自然结束 |
| 修改 SNI 后 reload | 新 SNI 使用另一个 group；旧连接可能继续发送，过渡期不承诺两个 group 合计受原速率约束 |
| 重启 Nginx | 现有连接中断，随后按新配置重新建立 |

不增加用于即时更新存量连接的常驻控制 socket 或定时器。用户需要立即停止所有既有 Brutal 连接时，应明确知晓重启会中断连接。

## 10. 日志与可观测性

- 启用成功：debug，包含 SNI、连接标识、客户端 fd、worker PID、原算法、rate 字节/秒、group、内核版本。
- 启用失败：warn，包含步骤、errno、SNI、连接标识和恢复结果。
- 回滚失败：alert，与普通启用失败分开。
- 未命中：不逐连接输出常规日志。
- 重复故障：每 worker、每配置、每失败类别最多每 10 秒一条常规告警；下一条报告期间抑制数量，退出时尽力汇总。debug 可保留完整明细。
- 不记录 ClientHello 原文、Reality 凭据或代理内容。

验证工具须区分客户端 fd 与 upstream fd，读回算法和 v2 参数。`ss` 可辅助观察算法，但不能仅凭它的单连接 pacing_rate 判断 group 总速率：内核可能为每个成员保留完整 group 的瞬时发送能力。

## 11. 构建、部署与维护

计划目录：

```text
docs/ngx-stream-brutal-spec.md
ngx_stream_brutal_module/
    config
    ngx_stream_brutal_module.c
    README.md
    tests/
```

模块源码保持小型 C 实现，文档给出一行配置、单位说明、排障、构建和回退方法。

构建要求：

1. builder 内正确声明 `ARG NGINX_VERSION`，源码与最终镜像使用同一精确版本。
2. 核对目标 `nginx -V`，以兼容条件构建 Stream、ssl_preread 和本动态模块；`--with-compat` 不是跨版本保证。
3. 运行镜像复制 `.so`，执行模块加载和示例配置的 `nginx -t`。
4. Compose 最终引用实际自定义镜像；仅构建 Dockerfile 不会使当前官方镜像获得模块。
5. 保留上一个通过测试的镜像，升级 Nginx 后重新构建和验证，不自动把未经测试版本推入生产。

部署前只读核对：宿主机内核及 Brutal 版本、默认算法、已有 destination rule/锁定路由、Nginx socket UID/netns。冲突应给出具体修正建议，不自动清空规则或更改系统默认算法。当前 host 网络部署尤其需要检查与宿主机现有规则的关系。

Brutal 的注册代码标记为非受限算法；不默认要求 privileged 容器或授予 NET_ADMIN。实际权限、宿主机安全策略和接口可用性以运行验证为准，失败按降级策略处理。

## 12. 验收标准

所有运行测试先在隔离监听端口与测试后端完成；现网验证使用实际客户端及 VLESS + Reality 路径。

| 编号 | 测试 | 通过条件 |
| --- | --- | --- |
| A1 | 构建与加载 | 精确版本镜像成功加载 `.so`，合法配置通过 nginx -t |
| A2 | 配置边界 | 数值边界、小数、溢出、带单位后缀、重复配置、SNI 冲突均符合契约 |
| A3 | 目标 SNI | 公网客户端 fd 读回 brutal 和准确的 20 字节参数；后端转发成功 |
| A4 | 非目标、无 SNI | 算法与无模块基线一致；模块没有修改算法的调用 |
| A5 | 分片 ClientHello | 分多次发送仍正确匹配，代理无新增读取错误或挂起 |
| A6 | 上游隔离 | 同一会话 upstream fd 的拥塞算法不由模块改变 |
| A7 | 网络变化 | 不同客户端 IP、IPv4/IPv6 建立目标连接，无需修改配置 |
| A8 | 多 worker/group | 实际覆盖至少两个 worker；相同 socket UID/netns 和 SNI 的 group 一致 |
| A9 | 并发总速率 | 1、4、16 条连接测试，合计目标不随连接数成倍增长 |
| A10 | 故障注入 | 原算法读取失败、切换失败、v1/未知版本、参数失败、读回失败、回滚失败均走规定路径；仍调用正常代理 |
| A11 | 外部规则 | 原本 brutal 或切换时加入规则 group 时不接管、不写入其参数，有明确日志 |
| A12 | reload | 同速率共享、改速率收敛、删除指令及旧连接保留符合第 9 节 |
| A13 | 上传与其他业务 | 客户端算法未改变，上传正常；其他 SNI 及网站可用且算法保持基线 |
| A14 | 稳定性 | 至少 24 小时长连接、连接反复建立关闭及 reload 混合测试，无崩溃、内核告警或持续 FD/内存增长 |

总速率测试口径：使用供给充足、接收端和链路能力高于目标、无主动丢包的受控 TCP 测试环境；预热 10 秒，测量至少 60 秒客户端实际接收字节数。1/4/16 连接分别重复 3 次，平均合计吞吐应处于目标 ±10%。若未达标，先明确链路、窗口或 CPU 瓶颈，不以日志正确代替性能验收。

VLESS/Reality 端到端测试另外记录应用吞吐，允许协议开销导致其低于公网 TCP 数据速率；丢包场景记录吞吐、重传和延迟，不用网卡瞬时峰值作为 group 失败的唯一依据。真实线路做同条件开关模块对比，不把“必然比 BBR 快”设为可承诺的验收项。

## 13. 实施阶段与交付门槛

1. **实现与配置验证**：完成源码、构建脚本接入、示例、解析和 socket 故障路径测试。
2. **真实内核集成**：完成客户端 socket、规则冲突、跨 worker/group、reload 验证，并保存测试证据。
3. **稳定性与发布**：完成 24 小时测试及实际 sing-box 下载对比，提供固定版本镜像和回退步骤。

只通过编译不能标记完成；没有真实 v2 内核测试条件时，必须标记运行验收未完成。Dockerfile 和模块实现已纳入本项目；真实业务验证及生产部署状态以测试记录为准，不因生成镜像自动视为已上线。

## 14. 已知边界

- SNI 是选择条件，不是客户端认证。使用相同 SNI 的未认证连接也可能进入 group。
- 算法配置隔离不等于物理带宽隔离，共享公网出口仍可能相互竞争。
- Brutal 仅控制 Nginx 到客户端这一段，不能修复后端源站、服务端 CPU 或其他路径瓶颈。
- fail-open 是继续正常代理并尽力恢复；内核故障或回滚失败时无法保证算法已经恢复。
- 内核模块的长期稳定性同样影响服务；本 Nginx 模块无法消除内核实现自身的缺陷。

第一版的使用体验固定为：**指定一个 SNI 和下载速度数字（固定单位 MB/s），其他参数由模块处理；上传保持原有行为。**
