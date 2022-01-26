FROM nginx:1.20.2 as builder
LABEL maintainer="NG6"

# RUN sed -i 's|security.debian.org/debian-security|mirrors.ustc.edu.cn/debian-security|g' /etc/apt/sources.list
# RUN sed -i 's/deb.debian.org/mirrors.ustc.edu.cn/g' /etc/apt/sources.list

# For latest build deps, see https://github.com/nginxinc/docker-nginx/blob/master/stable/debian/Dockerfile
RUN apt-get update \
    && apt-get install -y \
    build-essential ca-certificates zlib1g-dev libpcre3 libpcre3-dev uuid-dev tar unzip libssl-dev wget curl git cmake


ENV NGINX_VERSION=1.20.2 \
    PAGESPEED_VERSION=1.13.35.2-stable \
    PAGESPEED_ARCH=1.13.35.2-x64 \
    LUAJIT=2.1-20220111 \
    LUA=0.10.20 \
    NDK=0.3.1 \
    LRC=0.1.22 \
    LRL=0.11 \
    LUAJIT_LIB=/usr/local/luajit/lib \
    LUAJIT_INC=/usr/local/luajit/include/luajit-2.1

# Download sources
RUN mkdir -p /usr/src && \
    curl http://nginx.org/download/nginx-${NGINX_VERSION}.tar.gz | tar -xz -C /usr/src/

# Reuse same cli arguments as the nginx:alpine image used to build
RUN cd /usr/src && \
    git clone https://github.com/google/ngx_brotli.git && \
    git clone https://github.com/nginx-modules/ngx_cache_purge.git && \
    curl -L https://github.com/openresty/lua-nginx-module/archive/refs/tags/v${LUA}.tar.gz | tar -xz && \
    curl -L https://github.com/vision5/ngx_devel_kit/archive/refs/tags/v${NDK}.tar.gz | tar -xz && \
    curl -L https://github.com/openresty/luajit2/archive/refs/tags/v${LUAJIT}.tar.gz | tar -xz && \
    wget https://github.com/openresty/lua-resty-core/archive/refs/tags/v${LRC}.tar.gz && tar -xf v${LRC}.tar.gz && \
    wget https://github.com/openresty/lua-resty-lrucache/archive/refs/tags/v${LRL}.tar.gz && tar -xf v${LRL}.tar.gz && \
    curl -L https://github.com/apache/incubator-pagespeed-ngx/archive/v${PAGESPEED_VERSION}.tar.gz | tar -xz && \
    curl -L https://dl.google.com/dl/page-speed/psol/${PAGESPEED_ARCH}.tar.gz | tar -xz -C /usr/src/incubator-pagespeed-ngx-${PAGESPEED_VERSION}

RUN cd /usr/src/ngx_brotli && git submodule update --init && \
    cd /usr/src/luajit2-${LUAJIT} && \
    make && make install PREFIX=/usr/local/luajit && \
    cd /usr/src/lua-resty-core-${LRC} && \
    make install PREFIX=/etc/nginx && \
    cd /usr/src/lua-resty-lrucache-${LRL} && \
    make install PREFIX=/etc/nginx


RUN cd /usr/src && curl -L https://storage.googleapis.com/downloads.webmproject.org/releases/webp/libwebp-1.2.2-linux-x86-64.tar.gz | tar -xz && \
    chmod a+x /usr/src/libwebp-1.2.2-linux-x86-64/bin/cwebp


# Compile nginx && modules
RUN CONFARGS=$(nginx -V 2>&1 | sed -n -e 's/^.*arguments: //p') \
    cd /usr/src/nginx-$NGINX_VERSION && \
    ./configure --with-compat $CONFARGS \
    --with-ld-opt="-Wl,-rpath,/usr/local/luajit/lib" \
    --add-dynamic-module=/usr/src/ngx_devel_kit-${NDK} \
    --add-dynamic-module=/usr/src/lua-nginx-module-${LUA} \
    --add-dynamic-module=/usr/src/ngx_cache_purge \
    --add-dynamic-module=/usr/src/ngx_brotli \
    --add-dynamic-module=/usr/src/incubator-pagespeed-ngx-${PAGESPEED_VERSION} && \
    make && make install


FROM nginx:1.20.2
# Extract the dynamic modules from the builder image
COPY --from=builder /usr/local/luajit /usr/local/luajit
COPY --from=builder /usr/local/nginx/modules/ /usr/local/nginx/modules/
COPY --from=builder /etc/nginx /etc/nginx
COPY --from=builder /usr/src/libwebp-1.2.2-linux-x86-64/bin/cwebp /usr/bin/cwebp

ENV LUAJIT_LIB=/usr/local/luajit/lib \
    LUAJIT_INC=/usr/local/luajit/include/luajit-2.1