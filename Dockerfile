ARG NGINX_VERSION=defaultValue
FROM nginx:${NGINX_VERSION} AS builder
LABEL maintainer="NG6"

# For latest build deps, see https://github.com/nginxinc/docker-nginx/blob/master/stable/debian/Dockerfile
RUN apt-get update \
    && apt-get install -y \
    build-essential ca-certificates libpcre3 libpcre3-dev zlib1g zlib1g-dev openssl libssl-dev wget git gcc make libbrotli-dev libxml2-dev libxslt1-dev libexpat1-dev
# Download sources
RUN curl http://nginx.org/download/nginx-${NGINX_VERSION}.tar.gz | tar -xz -C /usr/src/
RUN git clone https://github.com/arut/nginx-dav-ext-module.git /usr/src/nginx-dav-ext-module
RUN git clone https://github.com/openresty/headers-more-nginx-module.git /usr/src/headers-more-nginx-module


# Reuse same cli arguments as the nginx:alpine image used to build
RUN cd /usr/src && \
    git clone https://github.com/google/ngx_brotli.git && \
    cd /usr/src/ngx_brotli && git submodule update --init

# Compile nginx && modules
RUN CONFARGS=$(nginx -V 2>&1 | sed -n -e 's/^.*arguments: //p') \
    cd /usr/src/nginx-$NGINX_VERSION && \
    ./configure --with-compat $CONFARGS \
    --add-dynamic-module=/usr/src/ngx_brotli \
    --add-dynamic-module=/usr/src/headers-more-nginx-module \
    --add-dynamic-module=/usr/src/nginx-dav-ext-module && \
    make && make install


FROM nginx:${NGINX_VERSION}
# Extract the dynamic modules from the builder image
COPY --from=builder /usr/local/nginx/modules/ /etc/nginx/modules/
