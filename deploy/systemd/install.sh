#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
SYSTEMD_DIR="/etc/systemd/system"
SERVICE_TEMPLATE="$SCRIPT_DIR/ai-rotator@.service.template"
SERVICE_TARGET="$SYSTEMD_DIR/ai-rotator@.service"
TIMERS=(
  "ai-rotator-morning.timer"
  "ai-rotator-ah-open.timer"
  "ai-rotator-midday.timer"
  "ai-rotator-evening.timer"
)

if [[ "${1:-install}" == "uninstall" ]]; then
  sudo systemctl disable --now "${TIMERS[@]}" || true
  sudo rm -f "$SERVICE_TARGET"
  for timer in "${TIMERS[@]}"; do
    sudo rm -f "$SYSTEMD_DIR/$timer"
  done
  sudo systemctl daemon-reload
  echo "Removed ai-rotator systemd timers and service."
  exit 0
fi

tmp_service="$(mktemp)"
sed "s#__REPO_DIR__#$REPO_DIR#g" "$SERVICE_TEMPLATE" > "$tmp_service"

sudo install -m 0644 "$tmp_service" "$SERVICE_TARGET"
rm -f "$tmp_service"

for timer in "${TIMERS[@]}"; do
  sudo install -m 0644 "$SCRIPT_DIR/$timer" "$SYSTEMD_DIR/$timer"
done

sudo systemctl daemon-reload
sudo systemctl enable --now "${TIMERS[@]}"

echo "Installed ai-rotator systemd service + timers."
echo "Check status with:"
echo "  systemctl list-timers 'ai-rotator-*'"
echo "Manual run example:"
echo "  systemctl start ai-rotator@morning.service"
