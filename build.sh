#!/bin/bash

docker build \
  --tag superng6/nginx:dev \
  --force-rm \
    .
