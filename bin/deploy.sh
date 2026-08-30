#!/usr/bin/env bash
set -euo pipefail

readonly DEPLOY_PATH="/var/www/vchat"
readonly BRANCH="master"
readonly SERVICES=(vchat-backend.service vchat-celery.service vchat-embedder.service)

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

sudo systemctl daemon-reload
sudo systemctl enable "${SERVICES[@]}"
sudo systemctl restart "${SERVICES[@]}"

for attempt in {1..30}; do
  if curl --fail --silent --show-error http://127.0.0.1:9080/health/live >/dev/null; then
    echo "vchat deployment completed at $(git rev-parse --short HEAD)"
    exit 0
  fi
  sleep 2
done
sudo systemctl --no-pager --full status vchat-backend.service >&2 || true
echo "vchat did not become live within 60 seconds" >&2
exit 1
