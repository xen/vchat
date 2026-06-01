#!/bin/bash

# Linux system dependencies setup
# This script installs all required system packages for vchat

set -e
export DEBIAN_FRONTEND=noninteractive

# Check if running as root
if [ "$EUID" -ne 0 ]; then
  echo "Error: This script must be run as root"
  echo "Usage: sudo ./bin/linux-setup.sh"
  exit 1
fi

echo "Installing system dependencies for vchat..."

echo "Refreshing PostgreSQL APT repository key..."
rm -f /etc/apt/sources.list.d/pgvector.list
install -d -m 0755 /etc/apt/keyrings
curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc | gpg --dearmor --batch --yes -o /etc/apt/keyrings/postgresql.gpg
chmod 0644 /etc/apt/keyrings/postgresql.gpg

echo "Configuring PostgreSQL APT repository..."
. /etc/os-release
echo "deb [signed-by=/etc/apt/keyrings/postgresql.gpg] http://apt.postgresql.org/pub/repos/apt ${VERSION_CODENAME}-pgdg main" > /etc/apt/sources.list.d/pgdg.list

apt-get update

echo "Installing system dependencies for vchat..."
apt-get install -y \
  build-essential \
  libldap-dev \
  libsasl2-dev \
  libpq-dev \
  libssl-dev \
  python3-dev \
  python3-pip \
  git \
  postgresql \
  postgresql-contrib \
  redis-server \
  nginx \
  nodejs \
  npm \
  ca-certificates \
  gnupg \
  curl \
  wget \
  postgresql-18-pgvector

echo "Installing Python package manager uv..."
if ! command -v uv >/dev/null 2>&1; then
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh
fi
uv --version

echo "✓ All system dependencies installed successfully!"
