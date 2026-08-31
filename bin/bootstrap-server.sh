#!/usr/bin/env bash
set -euo pipefail

readonly DEPLOY_PATH="/var/www/vchat"
readonly REPOSITORY="git@github.com:xen/vchat.git"

if [[ $EUID -ne 0 ]]; then
  echo "Run bin/bootstrap-server.sh as root." >&2
  exit 1
fi

"$(dirname "$0")/linux-setup.sh"
install -d -o deploy -g deploy -m 0755 /var/www
if [[ ! -d "$DEPLOY_PATH/.git" ]]; then
  sudo -u deploy git clone "$REPOSITORY" "$DEPLOY_PATH"
fi
if [[ -d "$DEPLOY_PATH/postgres" ]]; then
  find "$DEPLOY_PATH" -mindepth 1 -maxdepth 1 ! -name postgres -exec chown -R deploy:deploy {} +
else
  chown -R deploy:deploy "$DEPLOY_PATH"
fi

password_file="$DEPLOY_PATH/.postgres-password"
if [[ ! -s "$password_file" ]]; then
  umask 077
  openssl rand -hex 32 > "$password_file"
  chown deploy:deploy "$password_file"
fi
database_password="$(<"$password_file")"
postgres_env="$DEPLOY_PATH/postgres.env"
if [[ ! -f "$postgres_env" ]]; then
  umask 077
  cat > "$postgres_env" <<EOF
POSTGRES_USER=vchat
POSTGRES_PASSWORD=${database_password}
POSTGRES_DB=vchat
EOF
  chown deploy:deploy "$postgres_env"
fi
install -m 0644 "$DEPLOY_PATH/deploy/systemd/vchat-postgres.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now vchat-postgres.service
for attempt in {1..30}; do
  if sudo -u deploy docker exec vchat-postgres pg_isready -U vchat -d vchat >/dev/null; then break; fi
  sleep 2
done
sudo -u deploy docker exec vchat-postgres psql -U vchat -d vchat -v ON_ERROR_STOP=1 -c 'CREATE EXTENSION IF NOT EXISTS vector'
sudo -u deploy docker exec vchat-postgres psql -U vchat -d vchat -v ON_ERROR_STOP=1 -c 'CREATE EXTENSION IF NOT EXISTS pg_search'

local_config="$DEPLOY_PATH/local.yaml"
if [[ ! -f "$local_config" ]]; then
  secret_key="$(openssl rand -base64 48 | tr -d '\n')"
  cookie_key="$(openssl rand -base64 32 | tr -d '\n')"
  umask 077
  cat > "$local_config" <<EOF
mode: production
secret_key: "${secret_key}"
cookie_key: "${cookie_key}"
public_url: "https://vchat.dzen.dev"
allowed_origins:
  - "https://vchat.dzen.dev"
cookie_domain: "vchat.dzen.dev"
cookie_secure: true
enable_https_middleware: true
database_uri: "postgresql+asyncpg://vchat:${database_password}@127.0.0.1:5432/vchat"
redis_uri: "redis://127.0.0.1:6379/30"
celery_redis_uri: "redis://127.0.0.1:6379/"
celery_broker_db: 31
celery_backend_db: 32
celery_worker_concurrency: 2
embedding_service_url: "http://127.0.0.1:8091"
request_embedding_concurrency: 1
log_format: "json"
EOF
  chown deploy:deploy "$local_config"
  chmod 600 "$local_config"
fi

install -m 0644 "$DEPLOY_PATH/deploy/systemd/vchat-backend.service" /etc/systemd/system/
install -m 0644 "$DEPLOY_PATH/deploy/systemd/vchat-celery.service" /etc/systemd/system/
install -m 0644 "$DEPLOY_PATH/deploy/nginx/vchat-http.conf" /etc/nginx/sites-available/vchat
ln -sfn /etc/nginx/sites-available/vchat /etc/nginx/sites-enabled/vchat
rm -f /etc/nginx/sites-enabled/default
nginx -t
systemctl reload nginx
echo "Bootstrap complete. Run: sudo -u deploy $DEPLOY_PATH/bin/deploy.sh"
