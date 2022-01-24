FROM openresty/openresty:1.19.9.1-buster as builder
LABEL maintainer="NG6"

ARG NGINX_VERSION=1.19.9.1


# For latest build deps, see https://github.com/nginxinc/docker-nginx/blob/master/stable/debian/Dockerfile
RUN apt-get update \
    && apt-get install -y \
    build-essential ca-certificates gettext-base gnupg2 lsb-base lsb-release software-properties-common zlib1g-dev libpcre3 libpcre3-dev uuid-dev tar unzip libssl-dev wget curl git cmake

# Download sources
RUN mkdir -p /usr/src && \
    curl -L https://openresty.org/download/openresty-${NGINX_VERSION}.tar.gz | tar -xz -C /usr/src/

# Reuse same cli arguments as the nginx:alpine image used to build
RUN cd /usr/src && \
    git clone https://github.com/google/ngx_brotli.git && \
    git clone https://github.com/nginx-modules/ngx_cache_purge.git && \
    cd /usr/src/ngx_brotli && git submodule update --init

# Compile nginx && modules
RUN CONFARGS=$(nginx -V 2>&1 | sed -n -e 's/^.*arguments: //p') \
    cd /usr/src/openresty-$NGINX_VERSION && \
    ./configure --with-compat $CONFARGS \
    --add-dynamic-module=/usr/src/ngx_cache_purge \
    --add-dynamic-module=/usr/src/ngx_brotli && \
    make && make install


FROM openresty/openresty:1.19.9.1-buster
RUN apt-get update && \
    apt-get install -y webp && \
    echo "**** cleanup ****" && \
    apt-get clean && \
    mkdir -p /usr/local/nginx/modules
    rm -rf \
        /tmp/* \
        /var/lib/apt/lists/* \
        /var/tmp/*    
# Extract the dynamic modules from the builder image
COPY --from=builder /usr/local/openresty/nginx/modules/  /usr/local/nginx/modules/
RUN chown -R www:www /usr/local/nginx/modules
