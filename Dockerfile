# 默认值只在 build-arg 未提供时生效。如果提供了空值，则变量为空。
ARG NGINX_VERSION=1.30.4

# Builder Stage: 用于编译模块
FROM nginx:${NGINX_VERSION} AS builder
ARG NGINX_VERSION

LABEL maintainer="NG6"

# 设置 DEBIAN_FRONTEND 以避免 apt 在构建过程中出现交互式提示
ENV DEBIAN_FRONTEND=noninteractive

# 安装编译所需的依赖
# For latest build deps, see https://github.com/nginxinc/docker-nginx/blob/master/stable/debian/Dockerfile
RUN apt-get update -qq \
    && apt-get install -qq --no-install-recommends -y \
    build-essential \
    ca-certificates \
    libpcre2-dev \
    zlib1g-dev \
    libssl-dev \
    wget \
    git \
    make \
    libbrotli-dev \
    libxml2-dev \
    libxslt1-dev \
    libexpat1-dev \
    && rm -rf /var/lib/apt/lists/*

# 下载 Nginx 源码和第三方模块源码
RUN mkdir -p /usr/src/nginx \
    && wget -O /tmp/nginx.tar.gz https://nginx.org/download/nginx-${NGINX_VERSION}.tar.gz \
    && tar -xzf /tmp/nginx.tar.gz -C /usr/src/nginx --strip-components=1 \
    && rm /tmp/nginx.tar.gz
RUN git clone --depth 1 https://github.com/arut/nginx-dav-ext-module.git /usr/src/nginx-dav-ext-module
RUN git clone --depth 1 https://github.com/openresty/headers-more-nginx-module.git /usr/src/headers-more-nginx-module
RUN git clone --depth 1 --branch 3.0.2 https://github.com/nginx-modules/ngx_cache_purge.git /usr/src/ngx_cache_purge
RUN git clone --depth 1 https://github.com/google/ngx_brotli.git /usr/src/ngx_brotli \
    && cd /usr/src/ngx_brotli && git submodule update --init

COPY ngx_stream_brutal_module/config ngx_stream_brutal_module/ngx_stream_brutal_module.c /usr/src/ngx_stream_brutal_module/

# 编译 Nginx 和动态模块
RUN cd /usr/src/nginx && \
    ./configure --with-compat \
    --with-stream \
    --with-stream_ssl_preread_module \
    --with-debug \
    --add-dynamic-module=/usr/src/ngx_stream_brutal_module \
    --add-dynamic-module=/usr/src/ngx_brotli \
    --add-dynamic-module=/usr/src/headers-more-nginx-module \
    --add-dynamic-module=/usr/src/ngx_cache_purge \
    --add-dynamic-module=/usr/src/nginx-dav-ext-module && \
    make modules

# 测试镜像：使用同一官方 nginx 二进制；测试依赖不进入最终镜像。
FROM builder AS test
RUN apt-get update -qq \
    && apt-get install -qq --no-install-recommends -y python3 \
    && rm -rf /var/lib/apt/lists/*
COPY ngx_stream_brutal_module/tests/ /usr/src/ngx_stream_brutal_module/tests/
RUN cp /usr/src/nginx/objs/*.so /etc/nginx/modules/ \
    && nginx -t -c /usr/src/ngx_stream_brutal_module/tests/smoke.conf \
    && cc -shared -fPIC -O2 -Wall -Wextra -Werror \
    -o /tmp/sockopt_probe.so /usr/src/ngx_stream_brutal_module/tests/sockopt_probe.c -ldl \
    && python3 /usr/src/ngx_stream_brutal_module/tests/integration.py \
    --nginx /usr/sbin/nginx \
    --module /etc/nginx/modules/ngx_stream_brutal_module.so \
    --probe /tmp/sockopt_probe.so \
    && python3 /usr/src/ngx_stream_brutal_module/tests/sni_failure.py \
    --nginx /usr/sbin/nginx --probe /tmp/sockopt_probe.so \
    --source /usr/src/ngx_stream_brutal_module/ngx_stream_brutal_module.c \
    --nginx-source /usr/src/nginx

# 生产镜像：保留官方入口和默认配置，仅添加通过测试的动态模块。
FROM nginx:${NGINX_VERSION} AS production
ARG NGINX_VERSION
LABEL org.opencontainers.image.title="Nginx with optional dynamic modules" \
      org.opencontainers.image.source="https://github.com/SuperNG6/docker-nginx" \
      org.opencontainers.image.version="${NGINX_VERSION}"
COPY --from=test /usr/src/nginx/objs/*.so /etc/nginx/modules/
