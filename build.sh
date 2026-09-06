#!/bin/bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
NGINX_VERSION="$(head -n 1 ReleaseTag | xargs)"
NGINX_IMAGE="${NGINX_IMAGE:-superng6/nginx:stable-${NGINX_VERSION}}"

docker build \
  --target production \
  --build-arg "NGINX_VERSION=${NGINX_VERSION}" \
  --tag "${NGINX_IMAGE}" \
  --force-rm \
  .
