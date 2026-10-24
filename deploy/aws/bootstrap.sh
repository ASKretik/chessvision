#!/usr/bin/env bash
# One-time setup of a fresh Ubuntu 24.04 EC2 instance for the ChessVision API.
# Runs as root, normally from EC2 "User data" (see deploy/aws/README.md):
#
#   DUCKDNS_DOMAIN=yourname DUCKDNS_TOKEN=... LLM_API_KEY=... bash bootstrap.sh
#
# Safe to run again: every step checks whether it is already done.
# Progress is logged to /var/log/chessvision-bootstrap.log.
set -euo pipefail
exec > >(tee -a /var/log/chessvision-bootstrap.log) 2>&1

REPO=https://github.com/ASKretik/chessvision.git
DIR=/opt/chessvision

: "${DUCKDNS_DOMAIN:?set DUCKDNS_DOMAIN}"
: "${DUCKDNS_TOKEN:?set DUCKDNS_TOKEN}"
DUCKDNS_DOMAIN=${DUCKDNS_DOMAIN%.duckdns.org}   # accept "name" or "name.duckdns.org"

echo "== swap"
# 2 GB of RAM is enough to run the API but not to build its image
# (npm + pip + the 93 MB model download) next to it.
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 2G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi

echo "== docker"
apt-get update -q
DEBIAN_FRONTEND=noninteractive apt-get install -y -q docker.io docker-compose-v2 git curl
systemctl enable --now docker

echo "== code"
[ -d "$DIR/.git" ] || git clone -q "$REPO" "$DIR"

echo "== secrets"
umask 077
cat > "$DIR/deploy/aws/.env" <<EOF
SITE_ADDRESS=${DUCKDNS_DOMAIN}.duckdns.org
DUCKDNS_DOMAIN=${DUCKDNS_DOMAIN}
DUCKDNS_TOKEN=${DUCKDNS_TOKEN}
LLM_API_KEY=${LLM_API_KEY:-}
EOF

echo "== auto-update from GitHub every 5 minutes"
cat > /etc/systemd/system/chessvision-update.service <<EOF
[Unit]
Description=Pull ChessVision from GitHub and redeploy if the API changed
After=docker.service network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=$DIR/deploy/aws/update.sh
EOF
cat > /etc/systemd/system/chessvision-update.timer <<'EOF'
[Unit]
Description=Check GitHub for ChessVision updates

[Timer]
OnBootSec=1min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target
EOF
systemctl daemon-reload
systemctl enable --now chessvision-update.timer

echo "== first deploy (the image build takes 10-15 minutes on a t3.small)"
bash "$DIR/deploy/aws/update.sh" --force

echo "== done: https://${DUCKDNS_DOMAIN}.duckdns.org/health"
