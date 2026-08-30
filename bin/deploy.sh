#!/usr/bin/env bash
set -euo pipefail

readonly DEPLOY_PATH="/var/www/vchat"
readonly BRANCH="master"
readonly SERVICES=(vchat-backend.service vchat-celery.service vchat-embedder.service)
readonly SYSTEMD_UNITS=(
  vchat-backend.service
  vchat-celery.service
  vchat-embedder.service
  vchat-postgres.service
)

if [[ "$(id -un)" != "deploy" ]]; then
  echo "bin/deploy.sh must run as deploy" >&2
  exit 1
fi
cd "$DEPLOY_PATH"
if [[ ! -f local.yaml ]]; then
  echo "Missing $DEPLOY_PATH/local.yaml; run bin/bootstrap-server.sh as root first." >&2
  exit 1
fi

git fetch --prune origin "$BRANCH"
git reset --hard "origin/$BRANCH"
install -d -m 0755 .cache/huggingface .cache/sentence-transformers .cache/prometheus
make frontend
make deploy

for unit in "${SYSTEMD_UNITS[@]}"; do
  sudo install -m 0644 "deploy/systemd/$unit" "/etc/systemd/system/$unit"
done
sudo install -m 0644 deploy/nginx/vchat.conf /etc/nginx/sites-available/vchat
sudo ln -sfn /etc/nginx/sites-available/vchat /etc/nginx/sites-enabled/vchat
sudo nginx -t
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICES[@]}"
sudo systemctl restart "${SERVICES[@]}"
sudo systemctl reload nginx

for attempt in {1..60}; do
  if curl --fail --silent --show-error http://127.0.0.1:9080/health/ready >/dev/null; then
    echo "vchat deployment completed and is ready at $(git rev-parse --short HEAD)"
    exit 0
  fi
  sleep 2
done
sudo systemctl --no-pager --full status "${SERVICES[@]}" >&2 || true
echo "vchat did not become ready within 120 seconds" >&2
exit 1
