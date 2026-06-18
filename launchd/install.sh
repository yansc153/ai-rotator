#!/usr/bin/env bash
# launchd/install.sh — Install AI-Rotator launchd agents
#
# Usage:
#   ./launchd/install.sh          # install all 5 agents
#   ./launchd/install.sh uninstall # remove all 5 agents
#
# Schedule (all local timezone, every day):
#   06:24 — morning   全量OHLCV刷新 + 盘前早报 (到 07:00 左右发出)
#   08:45 — ah_open   A股开盘精选盯盘清单 (到 09:00-09:10 发出, 09:30 开盘前)
#   12:30 — midday    A/H 午盘可执行信号
#   14:30 — tail_close 尾盘确认
#   20:30 — evening   AI短线精灵美股盯盘清单

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
ACTION="${1:-install}"

PLISTS=(
  "com.airotator.morning"
  "com.airotator.ahopen"
  "com.airotator.midday"
  "com.airotator.tailclose"
  "com.airotator.evening"
)

if [ "$ACTION" = "uninstall" ]; then
  echo "Uninstalling AI-Rotator launchd agents..."
  for label in "${PLISTS[@]}"; do
    plist="$LAUNCH_AGENTS_DIR/$label.plist"
    if [ -f "$plist" ]; then
      launchctl unload "$plist" 2>/dev/null || true
      rm -f "$plist"
      echo "  ✓ Removed $label"
    else
      echo "  - $label not installed"
    fi
  done
  echo "Done."
  exit 0
fi

echo "Installing AI-Rotator launchd agents..."
mkdir -p "$LAUNCH_AGENTS_DIR"
mkdir -p "$REPO_DIR/logs"

for label in "${PLISTS[@]}"; do
  src="$SCRIPT_DIR/$label.plist"
  dst="$LAUNCH_AGENTS_DIR/$label.plist"

  # Verify source plist exists
  if [ ! -f "$src" ]; then
    echo "  [ERROR] $src not found — skipping"
    continue
  fi

  # Unload existing if already installed
  launchctl unload "$dst" 2>/dev/null || true

  # Copy and load
  cp "$src" "$dst"
  launchctl load "$dst"
  echo "  ✓ Installed and loaded: $label"
done

echo ""
echo "Schedule:"
echo "  06:24 daily → morning  (全量OHLCV刷新 → 盘前早报 ~07:00)"
echo "  08:45 daily → ah_open  (A股开盘精选盯盘清单 ~09:00-09:10)"
echo "  12:30 daily → midday   (A/H 午盘可执行信号)"
echo "  14:30 daily → tail_close (尾盘确认)"
echo "  20:30 daily → evening  (AI短线精灵美股盯盘清单)"
echo ""
echo "Logs: $REPO_DIR/logs/launchd-{morning,ahopen,midday,tailclose,evening}.log"
echo ""
echo "To check status:"
echo "  launchctl list | grep airotator"
echo ""
echo "To run manually right now:"
echo "  bash $REPO_DIR/scripts/local_pipeline.sh morning"
echo ""
echo "⚠️  Make sure your .env has DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID set."
