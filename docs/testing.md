# TCP Brutal 模块测试与发布边界

测试代码随源码提交，生产配置和本机原始运行记录不提交。所有示例均使用通用域名和隔离回环端口。

## 自动构建检查

默认生产镜像构建依赖 Dockerfile 的 `test` 阶段，检查：

- 与同版本官方 nginx 的动态模块加载兼容性。
- 参数数字、小数、上下界、非法单位、域名及重复配置。
- 目标/非目标/无 SNI、ClientHello 分片、IPv4/IPv6、worker 和 fd 复用。
- 原算法读取失败、已有 Brutal、切换失败、版本和参数异常、规则冲突、回滚失败。
- 错误发生后仍正常代理，以及 upstream socket 不被模块修改。
- reload 更新参数及删除指令后的新连接行为。

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

默认目标 1 MB/s，1/4/16 条连接，每组预热 10 秒、测量 60 秒、重复 3 次。各重复试验使用不同 SNI/group；单次试验内所有连接共享同一目标。每条连接不保证均分，验收指标是客户端合计接收速度位于目标 ±10%。

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

尚未完成 24 小时验收、具体 VLESS + Reality 客户端公网对比、丢包场景和所有发布架构的真实内核验证。CI 多架构构建成功只证明对应的编译和模拟接口测试通过。上线前核对实际客户端、既有 multiplex/Brutal 规则与宿主机环境。
