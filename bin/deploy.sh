#!/bin/bash
set -euo pipefail

DEPLOY_PATH="/var/www/vchat"
PORT="9001"
SYSTEMD_DIR="$HOME/.config/systemd/user"
NGINX_SITE="/etc/nginx/sites-available/vchat"
NGINX_SITE_LINK="/etc/nginx/sites-enabled/vchat"
LOCAL_CONFIG_FILE="$DEPLOY_PATH/local.yaml"

if [ ! -d "$DEPLOY_PATH" ]; then
  echo "Deployment path $DEPLOY_PATH does not exist."
  exit 1
fi

cd "$DEPLOY_PATH"

if [ ! -f "$LOCAL_CONFIG_FILE" ]; then
  cat <<EOF
Missing local.yaml.
Create $LOCAL_CONFIG_FILE manually (see entry.py requirements) before running deploy.sh.
SECRET_KEY and other secrets must be stored there.
EOF
  exit 1
fi

echo "Building frontend (make frontend)..."
make frontend

echo "Deploying backend (make deploy)..."
make deploy

mkdir -p "$DEPLOY_PATH/data" "$DEPLOY_PATH/media" "$DEPLOY_PATH/static"

APP_PYTHON="$DEPLOY_PATH/venv/bin/python"
PUBLIC_URL="$("$APP_PYTHON" - <<'PY'
from vchat.settings import config

print(config.get("public_url", "").rstrip("/"))
PY
)"

if [ -z "$PUBLIC_URL" ]; then
  echo "public_url must be set in configuration."
  exit 1
fi

DEPLOY_HOST="$(PUBLIC_URL="$PUBLIC_URL" python3 - <<'PY'
from urllib.parse import urlparse
import os

public_url = os.environ["PUBLIC_URL"]
host = urlparse(public_url).hostname
print(host or "")
PY
)"

if [ -z "$DEPLOY_HOST" ]; then
  echo "Failed to derive deployment host from public_url=$PUBLIC_URL"
  exit 1
fi

mkdir -p "$SYSTEMD_DIR"

cat > "$SYSTEMD_DIR/vchat-backend.service" <<EOF
[Unit]
Description=vchat Backend Service
After=network.target

[Service]
Type=simple
WorkingDirectory=$DEPLOY_PATH
ExecStart=$DEPLOY_PATH/venv/bin/gunicorn vchat.app:create_app -k aiohttp.worker.GunicornWebWorker --bind 0.0.0.0:$PORT
Restart=always
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

cat > "$SYSTEMD_DIR/vchat-celery.service" <<EOF
[Unit]
Description=vchat Celery Worker
After=network.target

[Service]
WorkingDirectory=$DEPLOY_PATH
ExecStart=$DEPLOY_PATH/venv/bin/celery -A jobs.celery worker --beat --loglevel=INFO -Q celery,crawler
Restart=always
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=default.target
EOF

cat > "$SYSTEMD_DIR/vchat-embedder.service" <<EOF
[Unit]
Description=vchat Embedder Worker
After=network.target

[Service]
WorkingDirectory=$DEPLOY_PATH
ExecStart=$DEPLOY_PATH/venv/bin/celery -A jobs.celery worker --loglevel=INFO -Q embeddings --pool=prefork --autoscale=1,1
Environment=TOKENIZERS_PARALLELISM=false
Environment=OMP_NUM_THREADS=1
Environment=MKL_NUM_THREADS=1
MemoryMax=4G
Restart=always

[Install]
WantedBy=default.target
EOF

cat <<EOF | sudo tee "$NGINX_SITE" >/dev/null
server {
    listen 80;
    server_name $DEPLOY_HOST;
    return 301 https://$DEPLOY_HOST\$request_uri;
}

server {
    listen 443 ssl http2;
    server_name $DEPLOY_HOST;

    ssl_certificate /etc/letsencrypt/live/$DEPLOY_HOST/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/$DEPLOY_HOST/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    location / {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 600s;
        proxy_connect_timeout 600s;
        send_timeout 600s;
    }

    location /ws/ {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Upgrade \$http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_read_timeout 600s;
        proxy_connect_timeout 600s;
        send_timeout 600s;
    }

    location /static/ {
        alias $DEPLOY_PATH/static/;
    }

    location /media/ {
        alias $DEPLOY_PATH/media/;
    }

    location /data/ {
        alias $DEPLOY_PATH/data/;
    }

}
EOF

echo "Reloading systemd user units..."
systemctl --user daemon-reload
systemctl --user enable  "vchat-backend.service"
systemctl --user restart "vchat-backend.service"
systemctl --user enable  "vchat-celery.service"
systemctl --user restart "vchat-celery.service"
systemctl --user enable  "vchat-embedder.service"
systemctl --user restart "vchat-embedder.service"

if [ ! -L "$NGINX_SITE_LINK" ]; then
  echo "Enabling nginx site for vchat"
  sudo ln -s "$NGINX_SITE" "$NGINX_SITE_LINK"
fi

sudo systemctl reload nginx

echo "To check services jourals, use:"
echo "journalctl --user -u vchat-backend.service -f"
echo "journalctl --user -u vchat-celery.service -f"
echo "journalctl --user -u vchat-embedder.service -f"

echo "Deployment of vchat completed."
echo "Application is available at $PUBLIC_URL/"
