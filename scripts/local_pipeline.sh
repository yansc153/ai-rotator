#!/usr/bin/env bash
# local_pipeline.sh — AI-Rotator local pipeline runner
#
# Usage:
#   ./scripts/local_pipeline.sh morning    # 07:00 — full pipeline + 盘前早报
#   ./scripts/local_pipeline.sh midday     # 12:30 — re-screen + 盘中播报
#   ./scripts/local_pipeline.sh evening    # 16:30 — full pipeline + 收盘晚报
#
# This script is designed to run locally on macOS where 'claude' CLI is
# installed and authenticated. launchd plists in launchd/ invoke it
# automatically on a weekday schedule.
#
# Unlike GitHub Actions (UTC-only), this script runs in the local
# Asia/Shanghai timezone — no date-offset bugs.

set -euo pipefail

SESSION="${1:-morning}"
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPTS="$REPO_DIR/scripts"
LOG_DIR="$REPO_DIR/logs"
LOG_FILE="$LOG_DIR/pipeline-$(date +%Y-%m-%d)-$SESSION.log"

mkdir -p "$LOG_DIR" "$REPO_DIR/data" "$REPO_DIR/storage" "$REPO_DIR/reports/daily"

# Load .env secrets (DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID, etc.)
# launchd jobs don't inherit shell environment, so we source manually.
if [ -f "$REPO_DIR/.env" ]; then
  # Export only lines that look like KEY=VALUE (skip comments and blanks)
  set -a
  # shellcheck disable=SC1090
  source <(grep -E '^[A-Z_]+=.+' "$REPO_DIR/.env") 2>/dev/null || true
  set +a
fi

# Redirect stdout+stderr to log file AND terminal
exec > >(tee -a "$LOG_FILE") 2>&1

echo "=========================================="
echo "AI-Rotator pipeline  session=$SESSION"
echo "Start: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "=========================================="

# Activate virtualenv if present (common local dev setup)
if [ -f "$REPO_DIR/.venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$REPO_DIR/.venv/bin/activate"
  echo "[INFO] virtualenv activated: $REPO_DIR/.venv"
elif [ -f "$HOME/.venv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$HOME/.venv/bin/activate"
  echo "[INFO] virtualenv activated: $HOME/.venv"
fi

# Ensure dependencies are importable
python3 -c "import yfinance, akshare, pandas" 2>/dev/null || {
  echo "[INFO] Installing dependencies..."
  pip install -q yfinance akshare pandas pyyaml certifi requests
}

cd "$REPO_DIR"

run_step() {
  local name="$1"
  local cmd="$2"
  echo ""
  echo "── $name ─────────────────────────────────"
  if eval "$cmd"; then
    echo "✓ $name done"
  else
    echo "⚠ $name failed (continuing)"
  fi
}

case "$SESSION" in
  morning)
    # Full pipeline: fresh data → screen → earnings → Discord 盘前早报
    # US market closed yesterday → yesterday's close is the latest data
    run_step "Fetch daily OHLCV (all 3357 stocks)"  "cd $SCRIPTS && python fetch_all_daily.py"
    run_step "Score and screen candidates"            "cd $SCRIPTS && python screen_candidates.py"
    run_step "Screen earnings plays (next 7 days)"   "cd $SCRIPTS && python fetch_earnings_plays.py"
    run_step "Send 盘前早报 to Discord"              "cd $SCRIPTS && python send_discord_brief.py --session morning"
    ;;

  midday)
    # Light update: re-score with existing price cache + update intraday overlay
    # A/HK markets are mid-session; full fetch would be slow, just re-screen
    run_step "Re-score candidates (existing cache)"  "cd $SCRIPTS && python screen_candidates.py"
    run_step "Send 盘中播报 to Discord"              "cd $SCRIPTS && python send_discord_brief.py --session midday"
    ;;

  evening)
    # Full pipeline: pick up today's AH closing prices + US pre-market
    run_step "Fetch daily OHLCV (refresh for AH close)"  "cd $SCRIPTS && python fetch_all_daily.py"
    run_step "Score and screen candidates"                "cd $SCRIPTS && python screen_candidates.py"
    run_step "Send 收盘晚报 to Discord"                   "cd $SCRIPTS && python send_discord_brief.py --session evening"
    ;;

  *)
    echo "[ERROR] Unknown session: $SESSION  (use: morning | midday | evening)"
    exit 1
    ;;
esac

echo ""
echo "=========================================="
echo "Done: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "=========================================="
