#!/usr/bin/env bash
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run bin/linux-setup.sh as root." >&2
  exit 1
fi
export DEBIAN_FRONTEND=noninteractive
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc |
  gpg --dearmor --batch --yes -o /etc/apt/keyrings/postgresql.gpg
chmod 0644 /etc/apt/keyrings/postgresql.gpg
. /etc/os-release
echo "deb [signed-by=/etc/apt/keyrings/postgresql.gpg] https://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" > /etc/apt/sources.list.d/pgdg.list
apt-get update
apt-get install -y --no-install-recommends build-essential ca-certificates certbot curl docker.io git gnupg libldap2-dev libpq-dev libsasl2-dev libssl-dev libyaml-dev nginx nodejs npm postgresql-client-18 python3-dev redis-server
if ! command -v uv >/dev/null; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
fi
usermod -aG docker deploy
systemctl enable --now docker redis-server nginx
