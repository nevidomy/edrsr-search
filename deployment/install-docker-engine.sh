#!/usr/bin/env bash
# Install Docker Engine + Compose plugin in this WSL distro.
# Requires sudo. Do not install Docker Desktop in parallel for this project.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Re-run with sudo: sudo $0" >&2
  exit 1
fi

apt-get update
apt-get install -y ca-certificates curl
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
usermod -aG docker "${SUDO_USER:-$USER}"

if [[ "$(cat /proc/sys/vm/max_map_count)" -lt 262144 ]]; then
  echo "vm.max_map_count=262144" > /etc/sysctl.d/99-court-search-opensearch.conf
  sysctl -w vm.max_map_count=262144
fi

echo "Docker Engine installed. Log out of WSL or run: newgrp docker"
docker --version
docker compose version
