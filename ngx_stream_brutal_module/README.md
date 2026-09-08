# Nginx Stream TCP Brutal v2

按 TLS SNI 为指定域名的 **Nginx → 客户端下载连接**启用 TCP Brutal v2。同一域名的连接共享配置带宽，客户端上传和其他域名的拥塞算法不受影响。

## 使用要求

- 宿主机已安装 TCP Brutal v2。Docker 共享宿主机内核，无需为此额外开启 `privileged` 或 `NET_ADMIN`。
- 使用 Nginx Stream 的 `ssl_preread` + `proxy_pass` 进行 TCP 透传，支持 IPv4/IPv6，不支持 `listen ssl`、UDP 或 Unix socket。
- 避免使用覆盖目标连接的 `brutalctl` 规则；已由外部启用 Brutal 的连接不会被本模块接管。

## 构建与部署

从项目根目录构建镜像：

```sh
docker build --build-arg NGINX_VERSION=1.30.4 -t ngx-brutal:local .
```

镜像基于同版本官方 nginx，保留本项目的其他第三方模块。将现有 Nginx 服务的镜像改为 `ngx-brutal:local`，挂载配置并重建容器。模块需要在配置中手动加载和启用，见下方示例。

如需独立编译，在 Nginx 源码目录执行：

```sh
./configure --with-compat --with-stream --with-stream_ssl_preread_module \
    --add-dynamic-module=/path/to/ngx_stream_brutal_module
make modules
```

模块必须与运行中的 Nginx 版本、构建条件和架构兼容，`--with-compat` 不支持跨版本加载。更新模块文件后应重启 Nginx；使用 Docker 时重建容器。

## 配置

以下示例将两个域名分流到各自后端，并分别设置下载带宽。请替换域名和后端地址，将相关配置合并到现有配置中。

```nginx
load_module /etc/nginx/modules/ngx_stream_brutal_module.so;

events {}

stream {
    error_log /var/log/nginx/error.log warn;
    log_format brutal '$remote_addr sni=$ssl_preread_server_name '
        'brutal=$brutal_status rate_Bps=$brutal_rate '
        'error=$brutal_error sent=$bytes_sent session=$session_time';
    access_log /var/log/nginx/stream-access.log brutal;

    map $ssl_preread_server_name $backend {
        vless.example.com 127.0.0.1:10001;
        video.example.com 127.0.0.1:10002;
        # 保留现有配置中的其他域名映射和默认后端。
        default           127.0.0.1:1;
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

`load_module` 放在 main 作用域。如果 Stream 也是动态模块，需先加载 Stream，再加载本模块。

### 参数

```nginx
brutal_sni <SNI> download_mbps=<整数> [gain=<整数>];
```

| 参数 | 说明 |
| --- | --- |
| `SNI` | 域名，大小写不敏感、精确匹配，不支持通配符、正则或尾部点 |
| `download_mbps` | 下载目标带宽，单位 **Mbps**，必填，范围 `1～1000000`，仅接受十进制整数 |
| `gain` | CWND 增益，选填，整数 `5～80`，默认 `20`；`15` 表示 1.5 倍，`20` 表示 2.0 倍，不直接改变目标带宽 |

例如，`download_mbps=300` 表示所有匹配连接共享 **300 Mbps**（37,500,000 bytes/s）。两个参数的顺序不限。

每个 `server` 可配置多个不同域名；同一 `server` 内不能重复配置同一域名，跨 `server` 的同一域名必须使用相同带宽和 gain。相同 socket UID 和网络命名空间下，同一域名的连接跨 worker、监听端口及 Nginx 实例共享带宽。无 SNI 或未匹配的连接保持原算法。

请按客户端实际可承受带宽设置。Brutal 无法突破线路容量，丢包补偿、重传和协议开销可能使实际网卡流量超过目标值，不能用作严格出口限速。

## 检查是否生效

修改配置后先运行 `nginx -t`，检查通过后再 reload。该命令只检查配置，不验证内核是否支持 Brutal；实际连接的启用结果请查看 Stream 日志。

示例配置会在**连接结束时**写入 access log，长连接需要等待断开后查看：

| `$brutal_status` | 含义 |
| --- | --- |
| `enabled` | 本模块已成功启用并验证 Brutal |
| `not-enabled` | 未由本模块启用，例如未匹配域名或连接提前结束 |
| `external` | 连接原本已使用 Brutal，本模块未接管 |
| `failed` | 初始化失败，结合 `$brutal_error` 和 error log 排查 |
| `skipped-worker-exiting` | reload 期间旧 worker 正在退出，跳过启用并正常代理 |

`$brutal_rate` 为初始化成功时的带宽，单位 bytes/s，未成功时为 `-`。日志记录的是连接初始化结果，不代表 reload 后的当前带宽。

启用失败时模块仍会继续代理，并在需要时尝试恢复原算法。优先检查宿主机是否安装 Brutal v2、是否存在冲突规则，以及 error log 中的具体错误。Stream 的日志级别需在 `stream` 中设置，HTTP 日志设置对它无效。

## 修改与停用

- **修改带宽或 gain**：修改配置并 reload，新 worker 首条成功初始化的连接会更新共享带宽组，已有成员随后使用新参数。
- **停用某个域名**：删除对应 `brutal_sni` 并 reload，新连接不再启用，已有连接保持到自然关闭。
- **更换域名**：过渡期新旧域名分别共享各自带宽，合计可能超过原域名的目标值。
- **立即停用所有已有连接**：重启 Nginx，会中断当前连接。

开发与测试细节见 [模块规格](../docs/ngx-stream-brutal-spec.md) 和 [测试记录](../docs/testing.md)。
