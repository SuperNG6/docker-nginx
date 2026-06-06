#!/bin/sh
set -e

rotate_nginx_logs() {
    /usr/local/bin/rotate-nginx-logs || true
}

rotate_nginx_logs

(
    while :; do
        sleep "${NGINX_LOGROTATE_INTERVAL:-3600}"
        rotate_nginx_logs
    done
) &
