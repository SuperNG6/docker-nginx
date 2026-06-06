#!/bin/bash
set -euo pipefail

NGINX_VERSION="$(head -n 1 ReleaseTag | xargs)"

docker build \
  --build-arg "NGINX_VERSION=${NGINX_VERSION}" \
  --tag superng6/nginx:dev \
  --force-rm \
  .
