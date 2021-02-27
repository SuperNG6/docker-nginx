FROM nginx:1.18.0 as builder
LABEL maintainer="NG6"

ARG NGINX_VERSION=1.18.0
ARG LUAJIT_LIB=/usr/local/luajit/lib
ARG LUAJIT_INC=/usr/local/luajit/include/luajit-2.0
# For latest build deps, see https://github.com/nginxinc/docker-nginx/blob/master/stable/debian/Dockerfile
RUN apt-get update \
    && apt-get install -y \
    build-essential ca-certificates zlib1g-dev libpcre3 libpcre3-dev tar unzip libssl-dev wget curl git cmake

# Download sources
RUN mkdir -p /usr/src && \
    curl http://nginx.org/download/nginx-${NGINX_VERSION}.tar.gz | tar -xz -C /usr/src/

# Reuse same cli arguments as the nginx:alpine image used to build
RUN cd /usr/src && \
    git clone https://github.com/google/ngx_brotli.git && \
    git clone https://github.com/nginx-modules/ngx_cache_purge.git && \
    wget https://github.com/openresty/lua-nginx-module/archive/refs/tags/v0.10.20.zip && \
    wget https://github.com/vision5/ngx_devel_kit/archive/refs/tags/v0.3.1.zip && \
    wget https://luajit.org/download/LuaJIT-2.0.5.zip && \
    unzip v0.10.20.zip && \
    unzip v0.3.1.zip && \
    unzip LuaJIT-2.0.5.zip && \
    cd LuaJIT-2.0.5 && \
    make && \
    make install PREFIX=/usr/local/luajit && \
    touch /etc/ld.so.conf.d/luajit.conf && \
    echo "/usr/local/luajit/lib" > /etc/ld.so.conf.d/luajit.conf && \
    ldconfig && \
    cd /usr/src/ngx_brotli && git submodule update --init

# Compile nginx && modules
RUN CONFARGS=$(nginx -V 2>&1 | sed -n -e 's/^.*arguments: //p') \
    cd /usr/src/nginx-$NGINX_VERSION && \
    ./configure --with-compat $CONFARGS \
    --with-ld-opt=-Wl,-rpath,/usr/local/luajit/lib --add-dynamic-module=/usr/src/lua-nginx-module-0.10.20 --add-dynamic-module=/usr/src/ngx_devel_kit-0.3.1 \
    --add-dynamic-module=/usr/src/ngx_cache_purge \
    --add-dynamic-module=/usr/src/ngx_brotli && \
    make && make install

FROM nginx:1.18.0
ENV LUAJIT_LIB=/usr/local/luajit/lib
ENV LUAJIT_INC=/usr/local/luajit/include/luajit-2.0
# Extract the dynamic modules from the builder image
COPY --from=builder /usr/local/nginx/modules/ /usr/local/nginx/modules/
COPY --from=builder /usr/local/luajit /usr/local/luajit
COPY --from=builder /etc/ld.so.conf.d/luajit.conf /etc/ld.so.conf.d/luajit.conf