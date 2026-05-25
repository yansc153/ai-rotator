#!/usr/bin/env bash
# local_pipeline.sh — AI-Rotator pipeline runner
#
# Usage:
#   ./scripts/local_pipeline.sh morning    # 07:00 — full pipeline + 盘前早报推送
#   ./scripts/local_pipeline.sh midday     # 12:30 — re-screen + 盘中播报
#   ./scripts/local_pipeline.sh evening    # 20:30 — US prep watchlist + Discord push
#
# This script is the local runtime entrypoint for launchd-based scheduling.
# LLM enrichment is expected to use the locally logged-in Codex CLI session.

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
python3 -c "import yfinance, akshare, pandas, yaml" 2>/dev/null || {
  echo "[INFO] Installing dependencies..."
  pip3 install -q yfinance akshare pandas pyyaml certifi requests
}

cd "$REPO_DIR"

# run_critical_step: exits the pipeline with code 1 on failure.
# Use for steps where sending a brief without fresh data would be misleading
# (fetch_all_daily, screen_candidates). A failed fetch or empty candidate pool
# means the brief would show stale/empty picks — better to abort and alert.
run_critical_step() {
  local name="$1"
  local cmd="$2"
  echo ""
  echo "── $name ─────────────────────────────────"
  if eval "$cmd"; then
    echo "✓ $name done"
  else
    echo "✗ $name FAILED — aborting pipeline (exit 1)"
    exit 1
  fi
}

# run_step: non-critical steps — log failure and continue.
# Use for enrichment steps (earnings, intraday) where partial data is still
# better than no brief at all.
run_step() {
  local name="$1"
  local cmd="$2"
  echo ""
  echo "── $name ─────────────────────────────────"
  if eval "$cmd"; then
    echo "✓ $name done"
  else
    echo "⚠ $name failed (non-critical — continuing)"
  fi
}

case "$SESSION" in
  morning)
    # Morning session now sends a lightweight pre-open overview after the
    # upstream refresh/build steps succeed, so the day starts with a real brief.
    # fetch_all_daily and screen_candidates are critical: abort on failure.
    run_critical_step "Fetch daily OHLCV (all 3357 stocks)"  "cd $SCRIPTS && python3 fetch_all_daily.py"
    run_critical_step "Score and screen candidates"           "cd $SCRIPTS && python3 screen_candidates.py"
    run_step "Screen earnings plays (next 7 days)"            "cd $SCRIPTS && python3 fetch_earnings_plays.py"
    # Intraday bars: best-effort (yfinance/akshare may be blocked in CN).
    # Morning intraday_weight=0 so missing data has zero scoring impact,
    # but running it here seeds the CSV cache for midday/evening use.
    run_step "Fetch 1h intraday bars (seed cache)"           "cd $SCRIPTS && python3 fetch_intraday.py --session morning"
    run_critical_step "Build US rotation report"             "cd $SCRIPTS && python3 run_daily_rotation.py --market US"
    run_critical_step "Build AH rotation report"             "cd $SCRIPTS && python3 run_daily_rotation.py --market AH"
    run_critical_step "Send 盘前早报 to Discord"             "cd $SCRIPTS && python3 send_discord_brief.py --session morning"
    ;;

  midday)
    # Midday now requires a fresh market snapshot before any trading signal.
    # This is the hard freshness gate. Intraday overlay remains best-effort only.
    run_critical_step "Fetch daily OHLCV (midday refresh)"    "cd $SCRIPTS && python3 fetch_all_daily.py"
    run_critical_step "Re-score candidates (fresh cache)"     "cd $SCRIPTS && python3 screen_candidates.py"
    run_step "Fetch 1h intraday bars (midday refresh)"       "cd $SCRIPTS && python3 fetch_intraday.py --session midday"
    run_critical_step "Build US rotation report"             "cd $SCRIPTS && python3 run_daily_rotation.py --market US"
    run_critical_step "Build AH rotation report"             "cd $SCRIPTS && python3 run_daily_rotation.py --market AH"
    run_critical_step "Send 盘中播报 to Discord"             "cd $SCRIPTS && python3 send_discord_brief.py --session midday"
    ;;

  evening)
    # US prep watchlist window. Internal session name stays 'evening'
    # but the product meaning is shortline-genie watchlist, not an auto-buy signal.
    # Fresh market snapshot is the hard gate; intraday overlay is best-effort.
    run_critical_step "Fetch daily OHLCV (premarket refresh)"     "cd $SCRIPTS && python3 fetch_all_daily.py"
    run_critical_step "Score and screen candidates"                "cd $SCRIPTS && python3 screen_candidates.py"
    # Intraday bars for US evening scoring.
    run_step "Fetch 1h intraday bars (evening refresh)"      "cd $SCRIPTS && python3 fetch_intraday.py --session evening"
    run_critical_step "Build US rotation report"             "cd $SCRIPTS && python3 run_daily_rotation.py --market US"
    run_critical_step "Build AH rotation report"             "cd $SCRIPTS && python3 run_daily_rotation.py --market AH"
    run_critical_step "Send AI短线精灵盯盘清单 to Discord"   "cd $SCRIPTS && python3 send_discord_brief.py --session evening"
    ;;

  ah_open)
    # A/HK opening watchlist — fires at 08:45 CST, before A-share open (09:30).
    # Re-uses the morning's already-fetched OHLCV cache (no new fetch needed).
    # Only re-screens candidates and sends the A股开盘精选 watchlist.
    # screen_candidates is critical — if the cache is gone, abort cleanly.
    run_critical_step "Re-score A/HK candidates (pre-open)"   "cd $SCRIPTS && python3 screen_candidates.py"
    run_critical_step "Build AH rotation report (pre-open)"   "cd $SCRIPTS && python3 run_daily_rotation.py --market AH"
    run_critical_step "Send A股开盘精选 to Discord"            "cd $SCRIPTS && python3 send_discord_brief.py --session ah_open"
    ;;

  *)
    echo "[ERROR] Unknown session: $SESSION  (use: morning | midday | ah_open | evening)"
    exit 1
    ;;
esac

echo ""
echo "=========================================="
echo "Done: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "=========================================="
