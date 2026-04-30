#!/usr/bin/env bash
# Idempotent setup script for a fresh DigitalOcean Ubuntu 24.04 droplet.
#
# Usage on the droplet (as root):
#   bash setup_droplet.sh
#
# Prereqs: a fresh droplet with SSH access. Cheapest tier ($6/mo basic)
# is plenty for this workload.
#
# This script:
#   1. Installs Docker.
#   2. Clones the screener repo into /opt/options-screener.
#   3. Writes /opt/options-screener/.env from values you provide via env vars
#      (or you can edit the file after).
#   4. Builds the Docker image.
#   5. Installs an hourly cron job (M-F, 14-21 UTC) that runs one scan.
#
# After running, edit /opt/options-screener/.env if you didn't pre-set env vars.

set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Bisma06817/options-screener.git}"
INSTALL_DIR="${INSTALL_DIR:-/opt/options-screener}"
IMAGE_TAG="options-screener:latest"

echo "== Installing Docker =="
if ! command -v docker >/dev/null; then
  apt-get update
  apt-get install -y ca-certificates curl gnupg git
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
    | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin
  systemctl enable --now docker
fi

echo "== Cloning repo =="
if [ ! -d "$INSTALL_DIR/.git" ]; then
  git clone "$REPO_URL" "$INSTALL_DIR"
else
  git -C "$INSTALL_DIR" pull --ff-only
fi

cd "$INSTALL_DIR"

echo "== Writing .env (skipping if it already exists) =="
if [ ! -f .env ]; then
  cp .env.example .env
  for k in TASTYTRADE_CLIENT_SECRET TASTYTRADE_REFRESH_TOKEN TASTYTRADE_ACCOUNT_ID \
           ANTHROPIC_API_KEY GOOGLE_SERVICE_ACCOUNT_JSON SPREADSHEET_ID; do
    if [ -n "${!k:-}" ]; then
      # escape special chars in value
      v=$(printf '%s' "${!k}" | sed -e 's/[\/&]/\\&/g')
      sed -i "s|^${k}=.*|${k}=${v}|" .env
    fi
  done
  chmod 600 .env
  echo "Wrote $INSTALL_DIR/.env  (mode 600)."
  echo "If any values are still blank, edit it now:  nano $INSTALL_DIR/.env"
fi

echo "== Building Docker image =="
docker build -t "$IMAGE_TAG" .

echo "== Installing hourly cron =="
CRON_LINE="0 14-21 * * 1-5 root cd $INSTALL_DIR && docker run --rm --env-file .env $IMAGE_TAG >> /var/log/options-screener.log 2>&1"
CRON_FILE="/etc/cron.d/options-screener"
echo "$CRON_LINE" > "$CRON_FILE"
chmod 644 "$CRON_FILE"

echo
echo "== Done =="
echo "Logs:        tail -f /var/log/options-screener.log"
echo "Run now:     cd $INSTALL_DIR && docker run --rm --env FORCE_RUN=1 --env-file .env $IMAGE_TAG"
echo "Edit .env:   nano $INSTALL_DIR/.env"
echo "Tweak cron:  nano /etc/cron.d/options-screener   (script no-ops outside the configured scan window)"
