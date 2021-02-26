FROM nginx:1.18.0-alpine as builder
LABEL maintainer="NG6"

ARG NGINX_VERSION=1.18.0

# For latest build deps, see https://github.com/nginxinc/docker-nginx/blob/master/mainline/alpine/Dockerfile
RUN apk add --no-cache --virtual .build-deps \
    gcc \
    libc-dev \
    make \
    openssl-dev \
    pcre-dev \
    zlib-dev \
    linux-headers \
    libxslt-dev \
    gd-dev \
    geoip-dev \
    perl-dev \
    libedit-dev \
    mercurial \
    bash \
    alpine-sdk \
    findutils \
    git \
    curl

# Download sources
RUN mkdir -p /usr/src && \
    curl http://nginx.org/download/nginx-${NGINX_VERSION}.tar.gz | tar -xz -C /usr/src/

# Reuse same cli arguments as the nginx:alpine image used to build
RUN cd /usr/src && \
    git clone https://github.com/google/ngx_brotli.git && \
    git clone https://github.com/nginx-modules/ngx_cache_purge.git && \
    cd /usr/src/ngx_brotli && git submodule update --init

# Compile nginx && modules
RUN CONFARGS=$(nginx -V 2>&1 | sed -n -e 's/^.*arguments: //p') \
    cd /usr/src/nginx-$NGINX_VERSION && \
    ./configure --with-compat $CONFARGS \
    --add-dynamic-module=/usr/src/ngx_cache_purge \
    --add-dynamic-module=/usr/src/ngx_brotli && \
    make && make install

FROM nginx:1.18.0-alpine 
# Extract the dynamic modules from the builder image
COPY --from=builder /usr/local/nginx/modules/ngx_http_brotli_filter_module.so /usr/local/nginx/modules/ngx_http_brotli_filter_module.so
COPY --from=builder /usr/local/nginx/modules/ngx_http_brotli_static_module.so /usr/local/nginx/modules/ngx_http_brotli_static_module.so
COPY --from=builder /usr/local/nginx/modules/ngx_http_cache_purge_module.so /usr/local/nginx/modules/ngx_http_cache_purge_module.so

RUN apk add --no-cache shadow \
&&  usermod -u 532 xfs \
&&  groupmod -g 523 xfs \
&&  adduser -D -H -s /bin/bash -G www-data www-data \
&&  usermod -u 33 www-data \
&&  groupmod -g 33 www-data
