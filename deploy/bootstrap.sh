#!/usr/bin/env bash
# Install the Hyperliquid USDC bot on a fresh Oracle Cloud (or any systemd) VM.
#
# Idempotent: safe to re-run to ship updates.
#
# Usage, from the repo root on the VM:
#   sudo SLACK_WEBHOOK_URL='https://hooks.slack.com/services/...' ./deploy/bootstrap.sh
#
# Omit SLACK_WEBHOOK_URL on a re-run to keep the value already installed.

set -euo pipefail

APP_DIR=/opt/hl-usdc-bot
ENV_FILE=/etc/hl-usdc-bot.env
SERVICE_USER=hlbot
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [[ $EUID -ne 0 ]]; then
  echo "Run with sudo." >&2
  exit 1
fi

echo "==> Installing system packages"
if command -v dnf >/dev/null 2>&1; then
  dnf install -y python3 python3-pip rsync
elif command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq
  apt-get install -y python3 python3-venv python3-pip rsync
else
  echo "Unsupported distro: need dnf (Oracle Linux) or apt-get (Ubuntu)." >&2
  exit 1
fi

echo "==> Creating service user"
id -u "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"

echo "==> Syncing application code to $APP_DIR"
mkdir -p "$APP_DIR"
rsync -a --delete \
  --exclude '.venv' --exclude '__pycache__' --exclude '.git' \
  --exclude 'tests' --exclude '.env' --exclude 'state.json' \
  "$REPO_DIR/hl_usdc_bot" "$APP_DIR/"

echo "==> Building the virtualenv"
if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$REPO_DIR/deploy/requirements-vm.txt"

echo "==> Writing $ENV_FILE"
if [[ -n "${SLACK_WEBHOOK_URL:-}" ]]; then
  # Written fresh so the webhook never lands in shell history or the repo.
  cat > "$ENV_FILE" <<ENVEOF
SLACK_WEBHOOK_URL=${SLACK_WEBHOOK_URL}
STATE_FILE=/var/lib/hl-usdc-bot/state.json
HEARTBEAT_HOURS=6
ESCALATED_INTERVAL_HOURS=1
ESCALATE_AT=0.79
DISPLAY_TIMEZONE=Asia/Manila
TOKEN_INDEX=0
ENVEOF
elif [[ ! -f "$ENV_FILE" ]]; then
  echo "SLACK_WEBHOOK_URL not set and $ENV_FILE does not exist." >&2
  exit 1
else
  echo "    keeping the existing $ENV_FILE"
fi
chown root:"$SERVICE_USER" "$ENV_FILE"
chmod 640 "$ENV_FILE"

chown -R "$SERVICE_USER":"$SERVICE_USER" "$APP_DIR"

echo "==> Installing the systemd units"
install -m 644 "$REPO_DIR/deploy/hl-usdc-bot.service" /etc/systemd/system/
install -m 644 "$REPO_DIR/deploy/hl-usdc-bot.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now hl-usdc-bot.timer

echo
echo "==> Done. Running one tick now to prove it works:"
systemctl start hl-usdc-bot.service || true
journalctl -u hl-usdc-bot.service -n 20 --no-pager

echo
echo "Next scheduled run:"
systemctl list-timers hl-usdc-bot.timer --no-pager
