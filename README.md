# Docker Nginx

基于官方 Nginx Debian 镜像，增加可选动态模块。保留官方入口、默认配置和运行方式，按需在自己的 `nginx.conf` 中加载模块。

| 模块 | 用途 |
| --- | --- |
| ngx_brotli | Brotli 动态与静态压缩 |
| headers-more | 设置、清除 HTTP 请求及响应头 |
| ngx_cache_purge | 清理代理缓存 |
| nginx-dav-ext | WebDAV 扩展 |
| ngx_stream_brutal | 按 TLS SNI 为指定客户端连接启用 TCP Brutal v2 下载控制 |

当前仓库构建基线见 [ReleaseTag](ReleaseTag)。PageSpeed 已不包含在当前镜像中。

## 获取镜像

GitHub Actions 构建成功后发布到 Docker Hub 和 GHCR：

```sh
docker pull superng6/nginx:stable-1.30.2
# 或
docker pull ghcr.io/superng6/nginx:stable-1.30.2
```

版本号按实际发布替换。`latest` 跟随最新成功发布；相同 Nginx 版本的模块更新会重建 `stable-<版本>` 标签。需要可回退的精确部署时，记录并使用镜像 digest。

通用 Compose 示例：

```yaml
services:
  nginx:
    image: superng6/nginx:stable-1.30.2
    restart: unless-stopped
    ports:
      - "443:443"
    volumes:
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - ./conf.d:/etc/nginx/conf.d:ro
      - ./html:/usr/share/nginx/html:ro
      - ./logs:/var/log/nginx
```

后端可通过同一 Docker 网络的服务名连接。示例不包含任何个人域名、内网地址或凭据，不应直接覆盖已有部署配置。

## 按 SNI 启用 TCP Brutal v2

适合由 Nginx Stream 接收公网 TCP，再按 SNI 转发到 sing-box 等后端的服务端。模块只设置 **Nginx → 客户端** 的公网发送 socket；客户端上传、其他 SNI 及后端连接的算法不由模块修改。

宿主机需要已经安装并加载 TCP Brutal v2。仅安装 Nginx `.so` 不能代替内核模块。无需为了此功能将 Brutal 设置为系统默认算法，也无需默认给容器 privileged/NET_ADMIN。

在 main 作用域加载模块，并在现有 Stream `server` 内添加 `brutal_sni`：

```nginx
load_module /etc/nginx/modules/ngx_stream_brutal_module.so;

error_log /var/log/nginx/error.log warn;
events {}

stream {
    map $ssl_preread_server_name $backend {
        vless.example.com proxy_backend:10001;
        default           web_backend:8443;
    }

    server {
        listen 443;
        listen [::]:443;
        ssl_preread on;

        brutal_sni vless.example.com download=50;

        proxy_pass $backend;
    }
}
```

替换示例 SNI 和后端服务名，并保留自己的其他映射。若 Stream 本身为动态模块，应先加载 Stream，再加载 Brutal 模块。

- **只填写数字**：`download=50` 表示 50 MB/s，即 400 Mbps；支持 `download=12.5`，不要附加单位。
- **共享速度**：同一 SNI 的连接共享目标速度，支持跨 worker 和 IPv4/IPv6，不是每条连接各 50 MB/s。
- **精确匹配**：域名大小写不敏感；每个 `server` 最多一条，不支持通配符、正则或尾部点。
- **默认关闭**：不加载模块或不配置指令，就不启用。
- **故障降级**：启用失败记录原因，已切换算法时尝试恢复原算法，继续正常代理；恢复失败会记录 alert。
- **上传保持原状**：上传拥塞控制由客户端发送端决定，服务端模块无法替客户端设置。

速度应按实际可承受带宽设置。Brutal 不保证比 BBR 更快，也不保证突破线路容量；其丢包补偿及协议开销可能使网卡总流量超过配置目标。SNI 只是选择条件，不是客户端认证。

修改速率并 reload 后，新连接成功设置参数会更新共享 group。删除指令并 reload 后新连接不启用，已有连接保留至自然结束。替换模块二进制应重建容器。操作前先保留旧镜像和配置。

```sh
docker compose exec nginx nginx -t
docker compose exec nginx nginx -s reload
```

完整参数、日志、兼容性、已有 brutalctl 规则的冲突处理见 [模块使用说明](ngx_stream_brutal_module/README.md)，实现边界见 [规格文档](docs/ngx-stream-brutal-spec.md)。

## 其他动态模块

在 main 作用域按需加载：

```nginx
load_module /etc/nginx/modules/ngx_http_brotli_static_module.so;
load_module /etc/nginx/modules/ngx_http_brotli_filter_module.so;
load_module /etc/nginx/modules/ngx_http_headers_more_filter_module.so;
load_module /etc/nginx/modules/ngx_http_cache_purge_module.so;
load_module /etc/nginx/modules/ngx_http_dav_ext_module.so;
```

例如在 `http` 中启用 Brotli：

```nginx
brotli on;
brotli_comp_level 5;
brotli_static on;
brotli_types text/plain text/css application/javascript application/json image/svg+xml;
```

## 本地构建生产镜像

```sh
./build.sh
# 或指定本地标签
NGINX_IMAGE=nginx-custom:local ./build.sh
```

脚本读取 `ReleaseTag`，默认生成 `superng6/nginx:stable-<版本>`。也可以直接构建：

```sh
docker build --target production --build-arg NGINX_VERSION=1.30.2 \
    -t nginx-custom:local .
```

Dockerfile 使用 `builder → test → production` 多阶段构建。默认最终目标是生产镜像，构建必须先通过模块加载、配置和故障注入测试。最终镜像仅增加 `.so`，不包含源码、Python、编译器、测试探针、测试配置或本机运行记录；不自动加载模块或改动用户配置。

构建保留模块 debug 日志能力，正常配置使用 warn 级别；这不代表默认开启调试日志。

## 测试与发布

测试源码应该随项目提交，便于升级 Nginx 后复现验证；它们不会进入生产镜像。真实内核/吞吐/长测是单独运行项，详见 [测试说明](docs/testing.md)。

本机已验证 Nginx 1.30.2 / Linux arm64 / TCP Brutal 2.0.0 的隔离连接与共享速率。**24 小时长期稳定性、公网丢包和具体代理客户端的端到端表现尚未完成验收**，不能仅因镜像构建成功就认为这些检查已通过。

仓库不提交个人 Nginx/sing-box 配置、域名、容器地址、凭据、本机原始测试记录或本地 tcp-brutal 源码副本；文档使用通用示例。

GitHub Actions 每周检查 Nginx stable 版本，也支持手动运行 **Build Docker Image**。相同版本更新模块时使用 `force_build=true`，避免因为已有镜像标签而跳过构建。构建矩阵沿用项目配置，测试阶段随各架构构建执行，通过后才合并并发布生产镜像。

如果该 Nginx 版本已有 GitHub Release，强制重建会创建 `v<版本>-r<提交短哈希>` 的源码发布，保留原标签，确保新模块的源码对应实际构建提交。Docker 镜像仍更新上述 stable/latest 标签。
