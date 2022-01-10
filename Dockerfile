FROM nginx:1.20.2 as builder
LABEL maintainer="NG6"

ARG NGINX_VERSION=1.20.2
ARG PAGESPEED_VERSION=1.13.35.2-stable
ARG PAGESPEED_ARCH=1.13.35.2-x64

# For latest build deps, see https://github.com/nginxinc/docker-nginx/blob/master/stable/debian/Dockerfile
RUN apt-get update \
    && apt-get install -y \
    build-essential ca-certificates zlib1g-dev libpcre3 libpcre3-dev uuid-dev tar unzip libssl-dev wget curl git cmake

# Download sources
RUN mkdir -p /usr/src && \
    curl http://nginx.org/download/nginx-${NGINX_VERSION}.tar.gz | tar -xz -C /usr/src/

# Reuse same cli arguments as the nginx:alpine image used to build
RUN cd /usr/src && \
    git clone https://github.com/google/ngx_brotli.git && \
    git clone https://github.com/nginx-modules/ngx_cache_purge.git && \
    curl -L https://github.com/apache/incubator-pagespeed-ngx/archive/v${PAGESPEED_VERSION}.tar.gz | tar -xz && \
    curl -L https://dl.google.com/dl/page-speed/psol/${PAGESPEED_ARCH}.tar.gz | tar -xz -C /usr/src/incubator-pagespeed-ngx-${PAGESPEED_VERSION} && \
    cd /usr/src/ngx_brotli && git submodule update --init

# Compile nginx && modules
RUN CONFARGS=$(nginx -V 2>&1 | sed -n -e 's/^.*arguments: //p') \
    cd /usr/src/nginx-$NGINX_VERSION && \
    ./configure --with-compat $CONFARGS \
    --add-dynamic-module=/usr/src/ngx_cache_purge \
    --add-dynamic-module=/usr/src/ngx_brotli \
    --add-dynamic-module=/usr/src/incubator-pagespeed-ngx-${PAGESPEED_VERSION} && \
    make && make install


FROM nginx:1.20.2
# Extract the dynamic modules from the builder image
COPY --from=builder /usr/local/nginx/modules/ /usr/local/nginx/modules/
