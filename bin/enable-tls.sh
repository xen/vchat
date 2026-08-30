#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run bin/enable-tls.sh as root." >&2
  exit 1
fi

install -d -o deploy -g deploy -m 0755 /var/www/vchat/acme
certbot certonly --webroot --webroot-path /var/www/vchat/acme \
  --non-interactive --agree-tos --email m@dzen.dev -d vchat.dzen.dev
install -m 0644 /var/www/vchat/deploy/nginx/vchat.conf /etc/nginx/sites-available/vchat
nginx -t
systemctl reload nginx
