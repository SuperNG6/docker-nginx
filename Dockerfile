# 默认值只在 build-arg 未提供时生效。如果提供了空值，则变量为空。
ARG NGINX_VERSION=1.26.1

# Builder Stage: 用于编译模块
FROM nginx:${NGINX_VERSION} AS builder

LABEL maintainer="NG6"

# 设置 DEBIAN_FRONTEND 以避免 apt 在构建过程中出现交互式提示
ENV DEBIAN_FRONTEND=noninteractive

# 安装编译所需的依赖
# For latest build deps, see https://github.com/nginxinc/docker-nginx/blob/master/stable/debian/Dockerfile
RUN apt-get update -qq \
    && apt-get install -qq --no-install-recommends -y \
    build-essential \
    ca-certificates \
    libpcre3-dev \
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
    && wget -O - http://nginx.org/download/nginx-${NGINX_VERSION}.tar.gz | tar -xz -C /usr/src/nginx --strip-components=1
RUN git clone --depth 1 https://github.com/arut/nginx-dav-ext-module.git /usr/src/nginx-dav-ext-module
RUN git clone --depth 1 https://github.com/openresty/headers-more-nginx-module.git /usr/src/headers-more-nginx-module
RUN git clone --depth 1 https://github.com/google/ngx_brotli.git /usr/src/ngx_brotli \
    && cd /usr/src/ngx_brotli && git submodule update --init

# 编译 Nginx 和动态模块
RUN cd /usr/src/nginx && \
    ./configure --with-compat \
    --add-dynamic-module=/usr/src/ngx_brotli \
    --add-dynamic-module=/usr/src/headers-more-nginx-module \
    --add-dynamic-module=/usr/src/nginx-dav-ext-module && \
    make modules

# Final Stage: 基于官方镜像，仅拷贝编译好的模块
FROM nginx:${NGINX_VERSION}

# 从 builder 阶段拷贝编译好的动态模块
COPY --from=builder /usr/src/nginx/objs/*.so /etc/nginx/modules/

# 你可以在这里添加加载模块的配置，或者通过挂载配置文件的方式来加载
# RUN echo "load_module modules/ngx_http_brotli_filter_module.so;" >> /etc/nginx/nginx.conf
