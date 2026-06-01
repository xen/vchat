#!/bin/bash

# Linux system dependencies setup
# This script installs all required system packages for vchat

set -e

# Check if running as root
if [ "$EUID" -ne 0 ]; then
  echo "Error: This script must be run as root"
  echo "Usage: sudo ./bin/linux-setup.sh"
  exit 1
fi

echo "Installing system dependencies for vchat..."

apt-get update

apt-get install -y \
  build-essential \
  libldap-dev \
  libpq-dev \
  libssl-dev \
  python3-dev \
  git \
  postgresql \
  postgresql-contrib \
  redis-server \
  nginx \
  nodejs \
  npm \
  curl \
  wget

echo "Installing Python package manager uv..."
pip3 install uv

echo "Installing pgvector..."
sh -c 'echo "deb [trusted=yes] https://apt.pgvector.org jammy main" > /etc/apt/sources.list.d/pgvector.list'
apt-get update
apt-get install -y pgvector

echo "✓ All system dependencies installed successfully!"
