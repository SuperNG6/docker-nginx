# Docker Nginx

基于官方 Nginx Debian 镜像，编译并添加常用第三方动态模块，适用于网站托管、反向代理、缓存和 WebDAV 等场景。保留官方镜像的入口和默认配置，模块按需加载。

## 内置模块

| 模块 | 用途 |
| --- | --- |
| `ngx_brotli` | Brotli 动态压缩与预压缩静态文件支持 |
| `headers-more-nginx-module` | 设置、修改和清除 HTTP 请求头、响应头 |
| `ngx_cache_purge` | 清理 Nginx 缓存 |
| `nginx-dav-ext-module` | 扩展 WebDAV 功能 |
| `ngx_stream_brutal_module` | 按 TLS SNI 为 Stream 客户端连接启用 TCP Brutal v2 |

模块文件位于 `/etc/nginx/modules/`，需要在自己的配置中显式加载。当前镜像不包含 PageSpeed。

## 获取镜像

```sh
docker pull superng6/nginx:latest
# 或使用 GHCR
docker pull ghcr.io/superng6/nginx:latest
```

`latest` 跟随最新成功发布的镜像，也可以使用 `stable-<Nginx版本>` 标签，例如 `superng6/nginx:stable-1.30.2`。版本标签以实际发布为准。

## 加载模块

将需要的 `load_module` 放在 `nginx.conf` 的 main 作用域，也就是 `events`、`http`、`stream` 块之外。不使用的模块可以不加载。

```nginx
load_module /etc/nginx/modules/ngx_http_brotli_static_module.so;
load_module /etc/nginx/modules/ngx_http_brotli_filter_module.so;
load_module /etc/nginx/modules/ngx_http_headers_more_filter_module.so;
load_module /etc/nginx/modules/ngx_http_cache_purge_module.so;
load_module /etc/nginx/modules/ngx_http_dav_ext_module.so;
```

官方默认配置中的 `/etc/nginx/conf.d/*.conf` 在 `http` 块内引入，因此不要把 `load_module` 写在这些文件里。

### Brotli 配置

加载两个 Brotli 模块后，在 `http` 块中添加：

```nginx
brotli on;
brotli_comp_level 5;
brotli_static on;
brotli_types
    application/atom+xml
    application/javascript
    application/json
    application/rss+xml
    application/vnd.ms-fontobject
    application/xhtml+xml
    application/xml
    font/otf
    font/ttf
    image/svg+xml
    image/x-icon
    text/css
    text/plain
    text/javascript;
```

`brotli on` 启用动态压缩；`brotli_static on` 允许在客户端支持时使用预先生成的 `.br` 文件。

### TCP Brutal v2 模块

这是可选的 Stream 模块，适用于需要按 TLS SNI 控制服务端下载发送速率的 TCP 透传场景。使用时还需要宿主机加载 TCP Brutal v2 内核模块。

完整的模块加载、`brutal_sni`、日志和 reload 说明见 [TCP Brutal 模块文档](ngx_stream_brutal_module/README.md)。普通网站、反向代理、Brotli 和 WebDAV 用法无需配置此模块。

## Docker Compose

准备好自己的 `nginx.conf`、站点配置和目录后，可参考：

```yaml
services:
  nginx:
    image: superng6/nginx:latest
    container_name: docker_nginx
    restart: unless-stopped
    ports:
      - "80:80"
      - "443:443"
      # 使用 HTTP/3 时，同时映射 UDP 端口：
      # - "443:443/udp"
    volumes:
      - /usr/share/zoneinfo/Asia/Shanghai:/etc/localtime:ro
      - ./nginx.conf:/etc/nginx/nginx.conf:ro
      - ./conf.d:/etc/nginx/conf.d:ro
      - ./website:/var/www/html
      - ./ssl:/etc/nginx/ssl:ro
      - ./logs:/var/log/nginx
```

站点配置中的 `root` 应与挂载目录对应，例如 `/var/www/html`。WebDAV 如需写入文件，目录还需授予 Nginx worker 相应权限。证书和 HTTPS/HTTP3 监听需要在自己的 Nginx 配置中设置。

启动服务：

```sh
docker compose up -d
```

修改配置后，先检查再重载：

```sh
docker compose exec nginx nginx -t
docker compose exec nginx nginx -s reload
```

更新镜像：

```sh
docker compose pull
docker compose up -d
```

## 本地构建

使用项目构建脚本：

```sh
./build.sh
# 自定义生成的镜像标签
NGINX_IMAGE=nginx-custom:local ./build.sh
```

脚本从 [ReleaseTag](ReleaseTag) 读取 Nginx 版本。也可以直接指定版本构建：

```sh
docker build --build-arg NGINX_VERSION=1.30.4 -t nginx-custom:local .
```

直接构建时，未传入 `NGINX_VERSION` 则使用 [Dockerfile](Dockerfile) 中的默认值。

构建使用 `builder → test → production` 三个阶段：编译动态模块、执行模块加载与回归检查，再将 `.so` 复制到同版本官方运行镜像。编译器、测试源码和测试工具不进入生产镜像。测试方法见 [测试说明](docs/testing.md)。

## 自动更新

GitHub Actions 每周检查 Nginx stable 版本，构建并发布到 Docker Hub 和 GHCR，也支持手动运行 **Build Docker Image**。

同一 Nginx 版本需要重新构建时，可设置 `force_build=true`。发布会更新 `latest` 和对应的 `stable-<版本>` 标签。

## 历史更新



- **2026-09-07**：更新至 Nginx 1.30.4，添加ngx_stream_brutal_module模块。
- **2026-06-06**：更新至 Nginx 1.30.2，重新添加缓存清理模块。
- **2025-03-19**：更新至 Nginx 1.26.3，添加 WebDAV 扩展和 headers-more 模块。
- **2024-07-30**：Nginx 1.26.1 版本调整为仅添加 Brotli，移除当时的缓存清理模块；模块目录变更为 `/etc/nginx/modules/`。
- **2022-08-01**：从 Nginx 1.22.0 版本起移除 PageSpeed；需要旧配置时可查阅仓库历史版本。
