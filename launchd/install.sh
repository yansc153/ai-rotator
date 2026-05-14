#!/usr/bin/env bash
# launchd/install.sh — Install AI-Rotator launchd agents
#
# Usage:
#   ./launchd/install.sh          # install all 3 agents
#   ./launchd/install.sh uninstall # remove all 3 agents
#
# Schedule (all CST / local timezone, Mon–Fri):
#   07:00 — morning   full pipeline (fetch + screen + earnings + 盘前早报)
#   12:30 — midday    re-screen + 盘中播报
#   16:30 — evening   full pipeline (fetch + screen + 收盘晚报)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
ACTION="${1:-install}"

PLISTS=(
  "com.airotator.morning"
  "com.airotator.midday"
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
echo "  07:00 CST Mon–Fri → morning  (fetch + screen + earnings + 盘前早报)"
echo "  12:30 CST Mon–Fri → midday   (re-screen + 盘中播报)"
echo "  16:30 CST Mon–Fri → evening  (fetch + screen + 收盘晚报)"
echo ""
echo "Logs: $REPO_DIR/logs/launchd-{morning,midday,evening}.log"
echo ""
echo "To check status:"
echo "  launchctl list | grep airotator"
echo ""
echo "To run manually right now:"
echo "  bash $REPO_DIR/scripts/local_pipeline.sh morning"
echo ""
echo "⚠️  Make sure your .env has DISCORD_BOT_TOKEN and DISCORD_CHANNEL_ID set."
