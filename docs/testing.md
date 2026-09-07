# TCP Brutal 模块测试与发布边界

测试代码随源码提交，生产配置和本机原始运行记录不提交。所有示例均使用通用域名和隔离回环端口。

## 自动构建检查

默认生产镜像构建依赖 Dockerfile 的 `test` 阶段，检查：

- 与同版本官方 nginx 的动态模块加载兼容性。
- 整数 Mbps 和 gain 的上下界、小数/旧语法/非法单位拒绝、多 SNI、重复参数及跨 server 速率/gain 冲突。
- 目标/非目标/无 SNI、ClientHello 分片、IPv4/IPv6、worker 和 fd 复用。
- 原算法读取失败、已有 Brutal、切换失败、版本和参数异常、规则冲突、回滚失败。
- 错误发生后仍正常代理，以及 upstream socket 不被模块修改。
- reload 更新参数及删除指令后的新连接行为。
- warn 级别下的 access log 状态快照、失败恢复结果和每 worker 一次的启动版本日志。
- 多 worker 的 access log 启用 1 秒缓冲刷新，按条件等待并按 SNI 比对，避免固定延时和日志顺序假设。
- `sni_failure.py` 在临时目录编译强制 SNI 查询失败的测试模块，确认 `failed/read-sni`、`sni=-`、server 级限频及正常代理且不操作 socket；不修改生产源码或产物。

这些测试使用真实 Nginx 解析和代理，探针仅模拟 socket 系统调用以复现故障。fake 模式中的原拥塞算法固定模拟为 `cubic`，不依赖构建主机内核或 QEMU 的 socket 返回值。无需构建主机支持 Brutal v2，也不会改变其默认算法或路由。

## 真实内核集成

在已加载 TCP Brutal v2 的 Linux 宿主机上：

```sh
docker build --target test -t ngx-brutal:test .
docker run --rm --network none --entrypoint python3 ngx-brutal:test \
    /usr/src/ngx_stream_brutal_module/tests/integration.py \
    --nginx /usr/sbin/nginx \
    --module /etc/nginx/modules/ngx_stream_brutal_module.so \
    --probe /tmp/sockopt_probe.so --mode real
```

此时探针转发真实内核调用，验证算法和参数读回。没有设置 privileged、NET_ADMIN、主机网络或生产配置挂载。

## 下载吞吐与 reload

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

吞吐脚本 `--rate` 仍以 MB/s 计量，但必须能精确换算成整数 Mbps（例如默认 1 MB/s → 配置 `download_mbps=8`）。默认目标 1 MB/s，1/4/16 条连接，每组预热 10 秒、测量 60 秒、重复 3 次。各重复试验使用不同 SNI/group；单次试验内所有连接共享同一目标。每条连接不保证均分，验收指标是客户端合计接收速度位于目标 ±10%。

同样的 nginx/module/probe 参数运行 `tests/lifecycle.py`，验证同一下载连接跨 reload：1 MB/s → 2 MB/s → 删除指令后仍保持已有 group，同时新连接不再启用。

## 长时间验证

```sh
docker run --rm --network none \
    --mount type=bind,src=/tmp/ngx-brutal-results,dst=/results \
    --entrypoint python3 ngx-brutal:test \
    /usr/src/ngx_stream_brutal_module/tests/soak.py \
    --nginx /usr/sbin/nginx \
    --module /etc/nginx/modules/ngx_stream_brutal_module.so \
    --probe /tmp/sockopt_probe.so --output /results/soak.json
```

默认持续 86400 秒，反复建立关闭连接，保留长连接，周期 reload 并采样 worker RSS/FD。可用 `--seconds 60 --reload-every 15` 做短时诊断。检查最终记录的资源趋势和宿主机内核日志；短测通过不等于长测通过。

## 当前验证摘要

已在 Nginx 1.30.2、Linux arm64、TCP Brutal 2.0.0 下完成自动构建、故障注入、真实 socket、跨 worker/IPv4/IPv6、旧连接 reload、合计吞吐和短时连接反复建立关闭测试。

在目标 1 MB/s 的回环测试中，1/4/16 连接各三次实测合计均约 1.000～1.009 MB/s；没有随连接数成倍增长。旧连接跨 reload 的速度约为 1/2/2 MB/s。60 秒短测完成超过 2,300 次连接和三次 reload，未观察到 FD 持续增长。

尚未完成 24 小时验收、具体 VLESS + Reality 客户端公网对比、公网丢包场景和所有发布架构的真实内核验证。CI 多架构构建成功只证明对应的编译和模拟接口测试通过。上线前核对实际客户端、既有 multiplex/Brutal 规则与宿主机环境。

## 0.2.0 验证补充（2026-09-07）

在 Linux arm64 / TCP Brutal 2.0.0 上完成：

- Nginx 1.30.2 与 1.30.4 下的整数 Mbps/可选 gain、多 SNI、配置冲突、15 种故障注入及真实 socket 测试。
- Nginx 1.30.4 完整 Dockerfile 构建，以及最终生产镜像的版本和模块加载检查；本地产物 `ngx-brutal:review-0.2.0`，未部署或发布。
- warn 级别 access log 正确记录两个 SNI 的 300/200 Mbps（37,500,000/25,000,000 bytes/s）、gain 15/80 和不同 group；每个 worker 的启动版本 notice 恰好一条。
- 故障路径的状态、恢复结果及共享 group 修改提示；reload 更新速率/gain 和移除指令。
- 旧连接跨 reload：实测约 1.000003 / 2.000023 / 2.000006 MB/s，连接保持。
- 短测（预热 3 秒、测量 10 秒）中，1/4 连接为 1.001890 / 0.999122 MB/s；16 连接为 0.838792 MB/s，未达到 ±10% 阈值。
- 对 16 连接按原标准预热 10 秒、测量 60 秒、重复三次，结果为 1.007995 / 1.007996 / 1.007996 MB/s，全部通过；没有为通过测试修改内核调度或验收阈值。短测不能替代标准时长验收。

这些是隔离回环验证，不包含 300 Mbps 公网实测、24 小时长测或其他架构验收。

## 共享 group 的真实内核回归

构建当前 test 镜像后，在隔离容器中运行（无需 NET_ADMIN）：

```sh
mkdir -p /tmp/ngx-brutal-results
docker run --rm --network none \
    --mount type=bind,src=/tmp/ngx-brutal-results,dst=/results \
    --entrypoint python3 ngx-brutal:test \
    /usr/src/ngx_stream_brutal_module/tests/groups.py \
    --nginx /usr/sbin/nginx \
    --module /etc/nginx/modules/ngx_stream_brutal_module.so \
    --probe /tmp/sockopt_probe.so --output /results/groups.json
```

测试覆盖：

- 两个 SNI 各有 IPv4/IPv6 下载连接，同时共享各自的 8/16 Mbps；修改 A 为 16 Mbps、gain=15，不改变 B 的吞吐；移除 A 后旧下载继续，新 A 连接不再设置参数。
- 保留旧 worker 中分片 ClientHello，等待新 worker 接管，再完成旧握手，验证退出中的旧 worker 不操作该客户端 socket，group 保持 16 Mbps，access log 记录 `skipped-worker-exiting`。修复前曾确定性重现 `16 → 8 → 16 Mbps`；此检查仍不代表原子更新。
- `real-verify-fail` 探针实际执行内核参数写入，仅让后续参数读回失败；确认 socket 算法恢复成功，同时另一 group 成员仍读到新速率/gain，日志标记 `may-have-changed`。

辅助 socket 和 worker 在该测试中使用相同 UID，确保加入同一应用 group。仅使用随机测试 SNI 和容器回环，不改规则或系统默认算法。此测试依赖真实 v2 内核，不放入跨架构 fake 构建验收。

本次运行默认每阶段预热 10 秒、测量 30 秒：A/B 合计速度分别约为 `1.002/2.001`、`1.999/1.999`、`2.001/1.999 MB/s`，全部通过 ±10% 检查；reload 重叠和真实回滚副作用检查通过。

## 可选 netem 诊断镜像

```sh
docker build --target test -t ngx-brutal:test .
docker build -f ngx_stream_brutal_module/tests/Dockerfile.netem \
    --build-arg TEST_IMAGE=ngx-brutal:test -t ngx-brutal:netem .
docker run --rm --network none --cap-add NET_ADMIN \
    --mount type=bind,src=/tmp/ngx-brutal-results,dst=/results \
    --entrypoint python3 ngx-brutal:netem \
    /usr/src/ngx_stream_brutal_module/tests/netem.py \
    --nginx /usr/sbin/nginx \
    --module /etc/nginx/modules/ngx_stream_brutal_module.so \
    --probe /tmp/sockopt_probe.so --output /results/netem.json
```

仅额外测试镜像安装 iproute2。脚本拒绝存在外部网卡或既有 loopback 自定义 qdisc 的环境；允许内核自动创建但未启用的 sit0。绝不可使用 host 网络运行。

默认四组：300 Mbps、模拟 RTT 150ms、下载容量 500 Mbps、4 连接，gain 15/20 × 下载丢包 0/0.1%；每组预热 10 秒、测量 20 秒。仅按 Nginx 前端端口施加双向各半 RTT 的延迟，下载方向施加丢包和容量限制，后端不匹配这些过滤规则。退出时移除容器自身的测试 qdisc。

JSON 记录客户端有效吞吐、客户端 TCP RTT 快照和 qdisc 统计。不设“必须跑满”的成功阈值；这是诊断工具。Loopback 的 MTU/offload、netem 模型和短采样均与公网不同，不能用单次结果判定 gain 优劣、链路容量或完整丢包适应能力，也不替代 veth/真实客户端测试、重传/CPU/延迟分位数和 24 小时验收。


本次 netem 诊断结果（Nginx 1.30.4、Linux arm64、TCP Brutal 2.0.0）：

| 下载丢包配置 | gain | 客户端合计有效吞吐 Mbps |
| --- | --- | --- |
| 0% | 15 | 299.991 |
| 0% | 20 | 300.017 |
| 0.1% | 15 | 300.829 |
| 0.1% | 20 | 300.541 |

最后一组因原临时命令会话输出未保留而单独补跑；其客户端 RTT 快照为 150.022～150.068ms，下载 netem 队列记录 23 次丢弃。此统计为整个运行期间的 qdisc skb 计数，不能直接当作线上 TCP 分段丢包率或测量窗口内的重传率。前三组保留终端汇总，最后一组完整 JSON 保存于本机结果目录。四组均接近 300 Mbps，但单次短测不足以认定 gain 15 或 20 更优，保持默认 gain=20。


## 退出 worker 跳过初始化的回归

初始化前检查 `ngx_exiting`，已进入优雅退出的 worker 记录 `skipped-worker-exiting`，沿用当前 socket 算法正常代理。此检查不引入共享内存或锁，也不撤销已经开始的初始化，不承诺严格原子切换。

已在 Nginx 1.30.4 / arm64 完成完整构建、fake 和真实 socket 回归：改速率、相同配置 reload、移除指令三种场景中，旧 worker 的延迟 ClientHello 均完整代理，探针确认该客户端没有任何 socket 读写调用。真实 group 在新连接写入、旧握手完成、再次新建连接三个时刻均保持 2,000,000 bytes/s（16 Mbps）。已启用的旧下载连接跨改速率/删除指令，实测约 1.000/2.000/2.000 MB/s，连接保持。
