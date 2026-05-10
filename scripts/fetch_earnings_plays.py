"""Earnings Play Screener — 赌财报模块 v2

For each AI-sector stock with earnings in the next 7 days, computes:

  1. Post-earnings historical reactions — last 5 quarters, exact next-day %
  2. Direction bias — LONG / SHORT based on win rate + current technicals
  3. Multi-factor conviction score (0–100)
  4. Exact entry strategy per direction

Scoring (0–100):
  historical_reaction (0–35): consistency + avg magnitude of past post-earnings moves
  technical_setup     (0–25): pre-earnings drift quality
  analyst_conviction  (0–20): consensus rating + price-target upside
  sector_momentum     (0–10): leading AI sector alignment
  eps_surprise_trend  (0–10): EPS beat consistency (supplementary)

Output: data/earnings_plays.json
"""
from __future__ import annotations

import json
import time
import warnings
from datetime import date, timedelta
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from _common import PROJECT_ROOT, load_env_file

warnings.filterwarnings("ignore")

OUTPUT_JSON     = PROJECT_ROOT / "data" / "earnings_plays.json"
CANDIDATES_JSON = PROJECT_ROOT / "data" / "candidates.json"

EARNINGS_WINDOW_DAYS = 7   # scan earnings within next 7 calendar days
MIN_SCORE            = 40  # minimum total score to include
EARNINGS_MARKETS     = {"US", "HK"}
HISTORY_QUARTERS     = 5   # how many past earnings reactions to show


# ── Data loaders ──────────────────────────────────────────────────────────────

def _load_candidates() -> dict[str, dict]:
    if not CANDIDATES_JSON.exists():
        return {}
    data = json.loads(CANDIDATES_JSON.read_text())
    return {c["symbol"]: c for c in data.get("candidates", [])}


def _load_leading_sectors() -> set[str]:
    today = str(date.today())
    leading: set[str] = set()
    for mkt in ("us", "ah"):
        path = PROJECT_ROOT / "reports" / "daily" / f"{today}-{mkt}-rotation.json"
        if path.exists():
            try:
                for s in json.loads(path.read_text()).get("leading_sectors_today", []):
                    leading.add(s.get("sector", ""))
            except Exception:
                pass
    return leading


# ── Historical post-earnings reaction ─────────────────────────────────────────

def _get_post_earnings_reactions(ticker_obj, n: int = HISTORY_QUARTERS) -> list[dict]:
    """Return list of last N post-earnings next-day price reactions.

    For each past earnings date we calculate:
      pct = (close_day_after_earnings - close_day_before_earnings) / close_day_before_earnings

    Returns list of dicts sorted newest-first:
      {"date": "2024-11-20", "pct": 0.092, "direction": "▲", "eps_surprise_pct": 0.12}
    """
    import pandas as pd

    reactions: list[dict] = []
    try:
        # earnings_dates: DataFrame indexed by date, columns include EPS/Surprise data
        ed = ticker_obj.earnings_dates
        if ed is None or ed.empty:
            return []

        # Filter to past dates only (upcoming earnings are excluded)
        today = date.today()
        past = ed[ed.index.map(lambda x: x.date() < today)].sort_index(ascending=False)

        if past.empty:
            return []

        # Fetch 2 years of daily price history (covers all recent earnings)
        hist = ticker_obj.history(period="2y", auto_adjust=True)
        if hist.empty:
            return []
        hist.index = hist.index.map(lambda x: x.date())  # normalise to date objects

        price_dates = sorted(hist.index)

        for earnings_ts in past.index[:n]:
            earnings_d = earnings_ts.date()
            try:
                # Find the trading day immediately BEFORE the earnings date
                before_days = [d for d in price_dates if d < earnings_d]
                if not before_days:
                    continue
                day_before = before_days[-1]

                # Find the trading day ON or immediately AFTER (handles AMC vs BMO)
                # We use the day AFTER so we capture the full gap + first-day reaction
                after_days = [d for d in price_dates if d > earnings_d]
                if not after_days:
                    # Earnings on last day in our data — use same day close
                    after_day = earnings_d if earnings_d in price_dates else None
                else:
                    after_day = after_days[0]

                if after_day is None:
                    continue

                close_before = float(hist.loc[day_before, "Close"])
                close_after  = float(hist.loc[after_day,  "Close"])

                if close_before <= 0:
                    continue

                pct = (close_after - close_before) / close_before

                # EPS surprise if available
                row = past.loc[earnings_ts]
                eps_surprise = None
                if "Surprise(%)" in row:
                    val = row["Surprise(%)"]
                    if val is not None and str(val) not in ("", "nan", "NaN"):
                        eps_surprise = round(float(val) / 100, 4)

                reactions.append({
                    "date":            str(earnings_d),
                    "pct":             round(pct, 4),
                    "direction":       "▲" if pct >= 0 else "▼",
                    "eps_surprise_pct": eps_surprise,
                })
            except Exception:
                continue

    except Exception:
        pass

    return reactions[:n]


# ── Direction analysis ─────────────────────────────────────────────────────────

def _analyze_direction(reactions: list[dict], candidate: dict | None) -> dict:
    """Determine recommended side (LONG/SHORT) and conviction level.

    Logic:
    - Win-rate LONG  = fraction of past reactions that were positive
    - Win-rate SHORT = 1 - win_rate_long
    - Avg move on up-days / down-days
    - Combine with current technical direction (ret_5d) to resolve ties
    """
    if not reactions:
        ret_5d = float(candidate.get("ret_5d", 0) if candidate else 0)
        side = "LONG" if ret_5d >= 0 else "SHORT"
        return {
            "side": side, "win_rate": None,
            "avg_up": None, "avg_down": None,
            "conviction": "低（无历史数据）",
        }

    ups   = [r["pct"] for r in reactions if r["pct"] >= 0]
    downs = [r["pct"] for r in reactions if r["pct"] < 0]
    win_rate = len(ups) / len(reactions)

    avg_up   = round(sum(ups)   / len(ups),   4) if ups   else None
    avg_down = round(sum(downs) / len(downs), 4) if downs else None

    ret_5d = float(candidate.get("ret_5d", 0) if candidate else 0)

    # Side determination: majority direction + technicals must agree
    if win_rate >= 0.6:
        if ret_5d >= -0.05:   # technicals not severely against
            side = "LONG"
        else:
            side = "LONG"     # historical edge overrides mild weakness
    elif win_rate <= 0.4:
        side = "SHORT"
    else:
        # 50/50 historically — follow technicals
        side = "LONG" if ret_5d >= 0 else "SHORT"

    n = len(reactions)
    if (side == "LONG"  and win_rate >= 0.8) or (side == "SHORT" and win_rate <= 0.2):
        conviction = f"极高（{n}次{'上涨' if side == 'LONG' else '下跌'}{int(win_rate*n if side=='LONG' else (1-win_rate)*n)}/{n}）"
    elif win_rate >= 0.6 or win_rate <= 0.4:
        conviction = f"中高（胜率{win_rate:.0%}）"
    else:
        conviction = f"中等（历史分歧）"

    return {
        "side":       side,
        "win_rate":   round(win_rate, 4),
        "avg_up":     avg_up,
        "avg_down":   avg_down,
        "conviction": conviction,
    }


# ── Scoring functions ──────────────────────────────────────────────────────────

def _score_historical_reaction(reactions: list[dict], side: str) -> tuple[float, str]:
    """Score based on actual post-earnings price history (primary signal)."""
    if not reactions:
        return 5.0, "无历史财报价格数据"

    n = len(reactions)
    ups   = [r["pct"] for r in reactions if r["pct"] >= 0]
    downs = [r["pct"] for r in reactions if r["pct"] < 0]
    win_rate = (len(ups) if side == "LONG" else len(downs)) / n

    # Magnitude: avg abs move (bigger moves = higher payout potential)
    all_abs = [abs(r["pct"]) for r in reactions]
    avg_abs = sum(all_abs) / len(all_abs)

    # Win-rate score (max 20 pts)
    win_score = win_rate * 20.0

    # Magnitude score (max 15 pts): avg >10% move = full 15 pts
    mag_score = min(15.0, avg_abs * 150)

    total = round(min(35.0, win_score + mag_score), 1)

    # Human-readable history summary
    history_str = "  ".join(
        f"{r['direction']}{abs(r['pct']):.1%}"
        for r in reactions
    )
    note = f"近{n}次财报次日: {history_str}  |  {'多' if side=='LONG' else '空'}胜率{win_rate:.0%} 平均振幅{avg_abs:.1%}"
    return total, note


def _score_technical(candidate: dict | None) -> tuple[float, str]:
    if not candidate:
        return 5.0, "候选池外"

    ret_5d  = float(candidate.get("ret_5d",  0))
    ret_20d = float(candidate.get("ret_20d", 0))
    atr_pct = float(candidate.get("atr_pct", 0))
    notes   = []
    score   = 0.0

    if 0.02 <= ret_5d <= 0.15:
        score += 12.0; notes.append(f"5日漂移{ret_5d:+.1%}【理想】")
    elif 0 < ret_5d < 0.02:
        score += 6.0;  notes.append("轻微正漂移")
    elif ret_5d > 0.20:
        score += 3.0;  notes.append(f"过热{ret_5d:+.1%}【卖消息风险】")
    else:
        score += 0.0;  notes.append(f"弱势{ret_5d:+.1%}")

    if ret_20d > 0.05:
        score += 8.0; notes.append("20日上行趋势")
    elif ret_20d > 0:
        score += 4.0

    if atr_pct > 0.04:
        score += 5.0; notes.append("高日内波动")

    return round(min(25.0, score), 1), "，".join(notes)


def _score_analyst(info: dict) -> tuple[float, str]:
    try:
        rec  = float(info.get("recommendationMean") or 3.0)
        tgt  = float(info.get("targetMeanPrice")    or 0)
        cur  = float(info.get("currentPrice") or info.get("regularMarketPrice") or 0)
        rating_score = max(0.0, (4.0 - rec) / 3.0 * 15.0)
        upside_score = 0.0
        upside_str   = ""
        if cur > 0 and tgt > 0:
            upside = (tgt - cur) / cur
            upside_score = min(5.0, upside * 20)
            upside_str   = f"，目标价{upside:+.1%}"
        return round(min(20.0, rating_score + upside_score), 1), f"分析师评级{rec:.1f}/5{upside_str}"
    except Exception:
        return 5.0, "分析师数据不可用"


def _score_eps_trend(ticker_obj) -> tuple[float, str]:
    """EPS beat consistency (supplementary to historical price reactions)."""
    try:
        hist = ticker_obj.earnings_history
        if hist is None or hist.empty:
            return 0.0, "无EPS历史"
        h = hist.tail(4)
        if "EPS Estimate" not in h.columns:
            return 0.0, "EPS字段缺失"
        beats, total = 0, 0
        avg_beat_pct = 0.0
        for _, r in h.iterrows():
            est    = float(r.get("EPS Estimate", 0) or 0)
            actual = float(r.get("Reported EPS", 0) or 0)
            if est != 0:
                total += 1
                bp = (actual - est) / abs(est)
                avg_beat_pct += bp
                if bp > 0:
                    beats += 1
        if total == 0:
            return 0.0, "无EPS数据"
        avg_beat_pct /= total
        score = min(10.0, max(0.0, (beats / total) * 10 + avg_beat_pct * 30))
        return round(score, 1), f"EPS近{total}季{beats}季超预期 均值{avg_beat_pct:+.1%}"
    except Exception:
        return 0.0, "EPS数据获取失败"


# ── Timing + entry ─────────────────────────────────────────────────────────────

def _timing_and_entry(side: str, win_rate: float | None, ret_5d: float, current: float, atr_pct: float) -> dict:
    """Return timing recommendation and entry price range."""
    wr = win_rate or 0.5
    atr = atr_pct or 0.03

    if side == "LONG":
        if ret_5d > 0.05 and wr >= 0.6:
            timing = "盘前买入"
            note   = "强漂移+高胜率，财报前吃漂移"
            entry_low  = round(current * 0.997, 2)
            entry_high = round(current * 1.005, 2)
            stop_loss  = round(current * (1 - atr * 1.5), 2)
            target_1   = round(current * (1 + atr * 2.0), 2)
            target_2   = round(current * (1 + atr * 3.5), 2)
        elif wr >= 0.6:
            timing = "收盘前买入"
            note   = "财报当日收盘前最后30分钟建仓"
            entry_low  = round(current * 0.995, 2)
            entry_high = round(current * 1.003, 2)
            stop_loss  = round(current * (1 - atr * 1.5), 2)
            target_1   = round(current * (1 + atr * 2.0), 2)
            target_2   = round(current * (1 + atr * 3.5), 2)
        else:
            timing = "盘后买入（等确认）"
            note   = "等财报发布后gap up确认再追，避免赌方向"
            gap_entry  = round(current * 1.03, 2)
            entry_low  = gap_entry
            entry_high = round(current * 1.06, 2)
            stop_loss  = round(current * 0.97, 2)
            target_1   = round(current * (1 + atr * 3.0), 2)
            target_2   = round(current * (1 + atr * 5.0), 2)
    else:  # SHORT
        if wr <= 0.4 and ret_5d < 0.05:
            timing = "收盘前做空"
            note   = "财报当日尾盘建空，等财报后gap down"
        else:
            timing = "盘后做空（等确认）"
            note   = "等财报发布后gap down确认再做空"
        entry_low  = round(current * 0.995, 2)
        entry_high = round(current * 1.003, 2)
        stop_loss  = round(current * (1 + atr * 1.5), 2)
        target_1   = round(current * (1 - atr * 2.0), 2)
        target_2   = round(current * (1 - atr * 3.5), 2)

    return {
        "timing":     timing,
        "timing_note": note,
        "entry_low":  entry_low,
        "entry_high": entry_high,
        "stop_loss":  stop_loss,
        "target_1":   target_1,
        "target_2":   target_2,
        "rr":         round(abs(target_1 - current) / abs(current - stop_loss), 2) if current != stop_loss else 0.0,
    }


# ── Earnings date lookup ───────────────────────────────────────────────────────

def _next_earnings_date(ticker_obj) -> str | None:
    try:
        cal = ticker_obj.calendar
        if not isinstance(cal, dict):
            return None
        for d in sorted(cal.get("Earnings Date", [])):
            try:
                d_date = d.date() if hasattr(d, "date") else date.fromisoformat(str(d)[:10])
                if d_date >= date.today():
                    return str(d_date)
            except Exception:
                pass
        return None
    except Exception:
        return None


# ── Main screener ──────────────────────────────────────────────────────────────

def screen_earnings_plays(universe_csv_path: Path) -> list[dict]:
    import pandas as pd
    import yfinance as yf

    universe     = pd.read_csv(universe_csv_path)
    candidates   = _load_candidates()
    leading_secs = _load_leading_sectors()
    today        = date.today()
    window_end   = today + timedelta(days=EARNINGS_WINDOW_DAYS)

    scan_df = universe[universe["market"].isin(EARNINGS_MARKETS)].copy()
    print(f"[earnings] Scanning {len(scan_df)} tickers (US+HK) for earnings next {EARNINGS_WINDOW_DAYS}d ...", flush=True)

    plays: list[dict] = []
    checked = found = 0

    for _, urow in scan_df.iterrows():
        yf_sym  = str(urow["yf_symbol"])
        our_sym = str(urow["symbol"])
        market  = str(urow["market"])

        try:
            t = yf.Ticker(yf_sym)
            earnings_str = _next_earnings_date(t)
            checked += 1

            if not earnings_str:
                continue
            earnings_dt = date.fromisoformat(earnings_str)
            if not (today <= earnings_dt <= window_end):
                continue
            found += 1

            info      = t.info or {}
            candidate = candidates.get(our_sym) or candidates.get(yf_sym)

            # ── Core signals ──────────────────────────────────────────────────
            reactions = _get_post_earnings_reactions(t, n=HISTORY_QUARTERS)
            direction = _analyze_direction(reactions, candidate)
            side      = direction["side"]

            atr_pct   = float(candidate.get("atr_pct", 0.03) if candidate else 0.03)
            ret_5d    = float(candidate.get("ret_5d",  0)    if candidate else 0)
            cur_price = float(
                candidate.get("current_price") if candidate
                else info.get("currentPrice") or info.get("regularMarketPrice") or 0
            )

            # ── Scores ────────────────────────────────────────────────────────
            react_score, react_note = _score_historical_reaction(reactions, side)
            tech_score,  tech_note  = _score_technical(candidate)
            analyst_score, analyst_note = _score_analyst(info)
            eps_score,   eps_note   = _score_eps_trend(t)
            sector = str(urow.get("sector_tags", "")).split(",")[0].strip()
            sector_score = 10.0 if sector in leading_secs else 3.0

            total = round(react_score + tech_score + analyst_score + eps_score + sector_score, 1)
            if total < MIN_SCORE:
                continue

            # ── Entry strategy ────────────────────────────────────────────────
            entry = _timing_and_entry(side, direction["win_rate"], ret_5d, cur_price, atr_pct)

            rec_mean   = float(info.get("recommendationMean") or 3.0)
            tgt_price  = float(info.get("targetMeanPrice") or 0)
            tgt_upside = round((tgt_price - cur_price) / cur_price, 4) if cur_price > 0 and tgt_price > 0 else 0.0

            play = {
                "symbol":       our_sym,
                "yf_symbol":    yf_sym,
                "company_name": str(urow.get("name", yf_sym)),
                "market":       market,
                "sector":       sector,
                "market_cap":   float(urow.get("market_cap") or 0),

                # Earnings timing
                "earnings_date":    earnings_str,
                "days_to_earnings": (earnings_dt - today).days,

                # Price
                "current_price": round(cur_price, 4),
                "atr_pct":       round(atr_pct, 4),
                "ret_5d":        round(ret_5d, 4),

                # Direction
                "side":       side,                         # "LONG" or "SHORT"
                "win_rate":   direction["win_rate"],        # historical win rate for that side
                "conviction": direction["conviction"],
                "avg_up":     direction["avg_up"],          # avg next-day % on up earnings
                "avg_down":   direction["avg_down"],        # avg next-day % on down earnings

                # Past 5 reactions (newest first)
                "historical_reactions": reactions,

                # Entry plan
                "timing":      entry["timing"],
                "timing_note": entry["timing_note"],
                "entry_low":   entry["entry_low"],
                "entry_high":  entry["entry_high"],
                "stop_loss":   entry["stop_loss"],
                "target_1":    entry["target_1"],
                "target_2":    entry["target_2"],
                "rr":          entry["rr"],

                # Analyst
                "analyst_target": round(tgt_price, 2),
                "target_upside":  tgt_upside,
                "rec_mean":       round(rec_mean, 1),

                # Scores
                "total_score": total,
                "score_breakdown": {
                    "historical_reaction": react_score,
                    "technical_setup":     tech_score,
                    "analyst_conviction":  analyst_score,
                    "eps_surprise_trend":  eps_score,
                    "sector_momentum":     sector_score,
                },
                "notes": {
                    "reaction":  react_note,
                    "technical": tech_note,
                    "analyst":   analyst_note,
                    "eps":       eps_note,
                },
                "is_hot_sector": sector in leading_secs,
            }
            plays.append(play)
            print(
                f"  ✓ {our_sym} — 财报{earnings_str} [{side}] "
                f"胜率{direction['win_rate']:.0%} if direction['win_rate'] else '' "
                f"评分{total} {entry['timing']}",
                flush=True,
            )

        except Exception:
            pass

        if checked % 20 == 0:
            time.sleep(0.4)

    plays.sort(key=lambda x: x["total_score"], reverse=True)
    print(f"[earnings] Checked {checked} | found upcoming earnings {found} | above threshold {len(plays)}", flush=True)
    return plays


def main() -> None:
    import datetime
    load_env_file()
    plays = screen_earnings_plays(PROJECT_ROOT / "data" / "universe_full.csv")
    payload = {
        "generated_at":  datetime.datetime.utcnow().isoformat() + "Z",
        "date":          str(date.today()),
        "window_days":   EARNINGS_WINDOW_DAYS,
        "earnings_plays": plays,
    }
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"Saved {len(plays)} earnings plays → {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
