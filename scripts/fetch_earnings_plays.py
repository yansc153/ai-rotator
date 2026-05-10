"""Earnings Play Screener — 赌财报模块 v3

Correct architecture:
  Step 1  Pull full earnings calendar from NASDAQ API for next 7 days
          → ~250 stocks/day, 5 calls total for the week
  Step 2  Cross-reference with our universe_full.csv
          → typically 30–50 matches from our AI-sector universe
  Step 3  For each match, fetch detailed data via yfinance:
            - Last 5 post-earnings next-day price reactions (actual %)
            - Direction bias (LONG / SHORT) based on historical win rate
            - Pre-earnings technical setup from candidates.json
            - Analyst consensus + price target
  Step 4  Score (0–100) and rank
  Step 5  Output top plays to data/earnings_plays.json

Scoring (0–100):
  historical_reaction (0–35): consistency + magnitude of past post-earnings moves
  technical_setup     (0–25): pre-earnings drift quality (momentum, not overbought)
  analyst_conviction  (0–20): consensus rating + price-target upside
  eps_surprise_trend  (0–10): EPS beat consistency last 4 quarters
  sector_momentum     (0–10): stock is in a leading AI sector today
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
UNIVERSE_CSV    = PROJECT_ROOT / "data" / "universe_full.csv"

EARNINGS_WINDOW_DAYS = 7    # calendar days to look ahead
MIN_SCORE            = 35   # minimum total score to include
HISTORY_QUARTERS     = 5    # post-earnings reactions to show


# ── Step 1: Pull NASDAQ earnings calendar ─────────────────────────────────────

def _fetch_nasdaq_day(date_str: str) -> list[dict]:
    """Fetch all stocks reporting on a single date from NASDAQ calendar API."""
    import requests, certifi
    url = f"https://api.nasdaq.com/api/calendar/earnings?date={date_str}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    try:
        r = requests.get(url, headers=headers, verify=certifi.where(), timeout=15)
        rows = (r.json().get("data") or {}).get("rows") or []
        result = []
        for row in rows:
            sym = str(row.get("symbol", "")).strip()
            if not sym:
                continue
            result.append({
                "symbol":            sym,
                "nasdaq_name":       str(row.get("name", "")),
                "earnings_date":     date_str,
                "release_time":      row.get("time", "time-not-supplied"),  # pre-market / after-hours / not-supplied
                "eps_forecast":      row.get("epsForecast", ""),
                "eps_last_year":     row.get("lastYearEPS", ""),
                "market_cap_str":    row.get("marketCap", ""),
                "fiscal_quarter":    row.get("fiscalQuarterEnding", ""),
            })
        return result
    except Exception as exc:
        print(f"  [calendar] NASDAQ fetch failed for {date_str}: {exc}", flush=True)
        return []


def fetch_earnings_calendar(start: date, days: int = EARNINGS_WINDOW_DAYS) -> list[dict]:
    """Fetch earnings calendar for the next `days` calendar days."""
    all_rows: list[dict] = []
    for i in range(days):
        d = start + timedelta(days=i)
        if d.weekday() >= 5:   # skip weekends
            continue
        rows = _fetch_nasdaq_day(str(d))
        print(f"  [calendar] {d}: {len(rows)} stocks reporting", flush=True)
        all_rows.extend(rows)
        time.sleep(0.3)    # gentle throttle
    return all_rows


# ── Step 2: Cross-reference with universe ─────────────────────────────────────

def cross_reference_universe(calendar: list[dict]) -> list[dict]:
    """Filter calendar to stocks in our AI-sector universe. Returns enriched dicts."""
    import pandas as pd
    universe = pd.read_csv(UNIVERSE_CSV)

    # Build fast lookup: yf_symbol → row, symbol → row
    sym_to_row: dict[str, dict] = {}
    for _, row in universe.iterrows():
        d = row.to_dict()
        sym_to_row[str(row["yf_symbol"])] = d
        sym_to_row[str(row["symbol"])]    = d

    matches: list[dict] = []
    seen: set[str] = set()
    for entry in calendar:
        sym = entry["symbol"]
        if sym in seen:
            continue
        u = sym_to_row.get(sym)
        if u is None:
            continue
        seen.add(sym)
        sector_raw = str(u.get("sector_tags", "") or "")
        sector     = sector_raw.split(",")[0].strip().split(";")[0].strip()
        matches.append({
            **entry,
            "our_symbol":   str(u.get("symbol", sym)),
            "yf_symbol":    str(u.get("yf_symbol", sym)),
            "company_name": str(u.get("name", entry["nasdaq_name"])),
            "market":       str(u.get("market", "US")),
            "sector":       sector,
            "sector_tags":  sector_raw,
            "market_cap":   float(u.get("market_cap") or 0),
        })
    return matches


# ── Step 3a: Historical post-earnings reactions ────────────────────────────────

def get_post_earnings_reactions(ticker_obj, n: int = HISTORY_QUARTERS) -> list[dict]:
    """Return last N actual post-earnings next-day % reactions.

    Method: use price history around each past earnings date.
      pct = (close_1st_trading_day_after_earnings - close_last_day_before_earnings)
            / close_last_day_before_earnings

    Handles BMO (same-day gap at open) and AMC (next-day gap).
    """
    import pandas as pd

    reactions: list[dict] = []
    try:
        ed = ticker_obj.earnings_dates
        if ed is None or ed.empty:
            return []

        today = date.today()
        past  = ed[ed.index.map(lambda x: x.date() < today)].sort_index(ascending=False)
        if past.empty:
            return []

        hist = ticker_obj.history(period="2y", auto_adjust=True)
        if hist.empty:
            return []
        hist.index = hist.index.map(lambda x: x.date())
        price_dates = sorted(hist.index)

        for earnings_ts in past.index[:n]:
            earnings_d = earnings_ts.date()
            try:
                befores = [d for d in price_dates if d < earnings_d]
                afters  = [d for d in price_dates if d > earnings_d]
                if not befores or not afters:
                    continue

                day_before = befores[-1]
                day_after  = afters[0]

                close_before = float(hist.loc[day_before, "Close"])
                close_after  = float(hist.loc[day_after,  "Close"])
                if close_before <= 0:
                    continue

                pct = (close_after - close_before) / close_before

                # EPS surprise
                row = past.loc[earnings_ts]
                surp = None
                for col in ("Surprise(%)", "Reported EPS", "EPS Estimate"):
                    pass   # just access row below
                try:
                    surp_val = row.get("Surprise(%)") if hasattr(row, "get") else row["Surprise(%)"] if "Surprise(%)" in row.index else None
                    surp = round(float(surp_val) / 100, 4) if surp_val is not None and str(surp_val) not in ("nan", "NaN", "") else None
                except Exception:
                    surp = None

                reactions.append({
                    "date":       str(earnings_d),
                    "pct":        round(pct, 4),
                    "direction":  "▲" if pct >= 0 else "▼",
                    "eps_beat":   surp,   # None if unknown
                })
            except Exception:
                continue
    except Exception:
        pass
    return reactions[:n]


# ── Step 3b: Direction + conviction ───────────────────────────────────────────

def analyze_direction(reactions: list[dict], candidate: dict | None, ret_5d: float) -> dict:
    """Determine LONG vs SHORT and conviction from historical post-earnings moves."""
    if not reactions:
        side = "LONG" if ret_5d >= 0 else "SHORT"
        return {"side": side, "win_rate": None, "avg_move": None,
                "conviction": "低（无历史数据）", "history_label": "无历史"}

    ups   = [r["pct"] for r in reactions if r["pct"] >= 0]
    downs = [r["pct"] for r in reactions if r["pct"] < 0]
    n     = len(reactions)
    win_rate_long = len(ups) / n

    avg_abs = sum(abs(r["pct"]) for r in reactions) / n
    avg_up   = sum(ups)   / len(ups)   if ups   else None
    avg_down = sum(downs) / len(downs) if downs else None

    # Determine side: historical bias + technical confirmation
    if win_rate_long >= 0.6:
        side = "LONG"
        wr   = win_rate_long
    elif win_rate_long <= 0.4:
        side = "SHORT"
        wr   = 1 - win_rate_long
    else:
        # 50-50 history → follow 5-day technical direction
        side = "LONG" if ret_5d >= 0 else "SHORT"
        wr   = win_rate_long if side == "LONG" else 1 - win_rate_long

    # Conviction label
    wr_side = win_rate_long if side == "LONG" else 1 - win_rate_long
    if wr_side >= 0.8:
        conv = f"极高 {int(wr_side * n)}/{n}次{'涨' if side == 'LONG' else '跌'}"
    elif wr_side >= 0.6:
        conv = f"中高 {int(wr_side * n)}/{n}次{'涨' if side == 'LONG' else '跌'}"
    else:
        conv = f"分歧 历史各半"

    history_label = "  ".join(f"{r['direction']}{r['pct']:+.1%}" for r in reactions)

    return {
        "side":        side,
        "win_rate":    round(wr_side, 4),
        "avg_move":    round(avg_abs, 4),
        "avg_up":      round(avg_up,   4) if avg_up   is not None else None,
        "avg_down":    round(avg_down, 4) if avg_down is not None else None,
        "conviction":  conv,
        "history_label": history_label,   # "▲+9.2%  ▼-3.1%  ▲+14.5%  ▲+6.8%  ▲+11.3%"
    }


# ── Step 3c: Scoring sub-functions ────────────────────────────────────────────

def score_historical(reactions: list[dict], side: str) -> tuple[float, str]:
    if not reactions:
        return 5.0, "无历史价格数据"
    n = len(reactions)
    wins = [r for r in reactions if (r["pct"] >= 0) == (side == "LONG")]
    win_rate = len(wins) / n
    avg_abs  = sum(abs(r["pct"]) for r in reactions) / n
    win_score = win_rate * 20.0
    mag_score = min(15.0, avg_abs * 150)   # 10% avg move → 15pts
    return round(min(35.0, win_score + mag_score), 1), f"胜率{win_rate:.0%} 平均振幅{avg_abs:.1%}"


def score_technical(candidate: dict | None) -> tuple[float, str]:
    if not candidate:
        return 5.0, "候选池外"
    ret_5d  = float(candidate.get("ret_5d",  0))
    ret_20d = float(candidate.get("ret_20d", 0))
    atr_pct = float(candidate.get("atr_pct", 0))
    score, parts = 0.0, []

    if 0.02 <= ret_5d <= 0.15:
        score += 12.0; parts.append(f"5日{ret_5d:+.1%}漂移理想")
    elif 0 < ret_5d < 0.02:
        score += 6.0;  parts.append("轻微正漂移")
    elif ret_5d > 0.20:
        score += 2.0;  parts.append(f"过热{ret_5d:+.1%}注意卖消息")
    else:
        parts.append(f"弱势{ret_5d:+.1%}")

    if ret_20d > 0.05:
        score += 8.0; parts.append("20日上行")
    elif ret_20d > 0:
        score += 4.0

    if atr_pct > 0.04:
        score += 5.0; parts.append("高波动")

    return round(min(25.0, score), 1), "，".join(parts) or "无明显信号"


def score_analyst(info: dict) -> tuple[float, str]:
    try:
        rec = float(info.get("recommendationMean") or 3.0)
        tgt = float(info.get("targetMeanPrice")    or 0)
        cur = float(info.get("currentPrice") or info.get("regularMarketPrice") or 0)
        rs  = max(0.0, (4.0 - rec) / 3.0 * 15.0)
        us  = min(5.0, (tgt - cur) / cur * 20) if cur > 0 and tgt > 0 else 0.0
        label = f"分析师评级{rec:.1f}/5"
        if cur > 0 and tgt > 0:
            label += f"  目标价{tgt:.2f}({(tgt-cur)/cur:+.1%})"
        return round(min(20.0, rs + us), 1), label
    except Exception:
        return 5.0, "分析师数据不可用"


def score_eps_trend(ticker_obj) -> tuple[float, str]:
    try:
        h = ticker_obj.earnings_history
        if h is None or h.empty or "EPS Estimate" not in h.columns:
            return 0.0, "无EPS历史"
        h = h.tail(4)
        beats, total, avg_bp = 0, 0, 0.0
        for _, r in h.iterrows():
            est    = float(r.get("EPS Estimate", 0) or 0)
            actual = float(r.get("Reported EPS",  0) or 0)
            if est != 0:
                total += 1; bp = (actual - est) / abs(est); avg_bp += bp
                if bp > 0: beats += 1
        if not total:
            return 0.0, "无EPS数据"
        avg_bp /= total
        return round(min(10.0, max(0.0, (beats/total) * 10 + avg_bp * 30)), 1), \
               f"EPS近{total}季{beats}季超预期 均值{avg_bp:+.1%}"
    except Exception:
        return 0.0, "EPS获取失败"


# ── Step 3d: Entry strategy per direction + release timing ────────────────────

def build_entry_strategy(
    side: str, win_rate: float | None, ret_5d: float,
    cur: float, atr_pct: float, release_time: str,
) -> dict:
    """Compute timing + entry price + SL + targets based on side and release time.

    release_time values from NASDAQ:
      "time-pre-market"   → BMO: stock gaps at TODAY's open (buy night before or pre-market)
      "time-after-hours"  → AMC: stock gaps at NEXT DAY's open (buy before today's close)
      "time-not-supplied" → unknown
    """
    wr  = win_rate or 0.5
    atr = atr_pct or 0.03
    is_bmo = release_time == "time-pre-market"
    is_amc = release_time == "time-after-hours"

    if side == "LONG":
        # Determine WHEN to buy
        if is_bmo:
            # Earnings released pre-market — stock gaps at open
            # Best entry: buy the night BEFORE earnings (or pre-market if strong)
            if wr >= 0.6 and ret_5d > 0.02:
                timing      = "盘前买入（财报日前一晚建仓）"
                timing_note = "BMO财报，提前一天建仓吃gap up"
            elif wr >= 0.6:
                timing      = "财报日盘前买入"
                timing_note = "BMO财报，盘前开盘前市价买入"
            else:
                timing      = "观望，等开盘gap确认"
                timing_note = "胜率不高，等开盘方向确认再追"
        elif is_amc:
            # Earnings released after close — stock gaps at next day open
            # Best entry: buy before today's close (last 30-60 min)
            if wr >= 0.6 and ret_5d > 0:
                timing      = "收盘前30分钟建仓（赌AMC财报）"
                timing_note = "AMC财报，收盘前吃隔夜gap预期"
            elif wr >= 0.6:
                timing      = "收盘前观望，尾盘决策"
                timing_note = "AMC财报，技术偏弱需谨慎"
            else:
                timing      = "财报后次日盘前等gap确认"
                timing_note = "胜率不高，等次日开盘gap up确认"
        else:
            timing      = "收盘前建仓（发布时间未知）"
            timing_note = "发布时间不详，收盘前少量建仓"

        el = round(cur * 0.997, 2);  eh = round(cur * 1.005, 2)
        sl = round(cur * (1 - atr * 1.5), 2)
        t1 = round(cur * (1 + atr * 2.0), 2)
        t2 = round(cur * (1 + atr * 3.5), 2)

    else:  # SHORT
        if is_bmo:
            timing      = "财报日开盘确认gap down后做空"
            timing_note = "BMO财报，等开盘确认下跌方向"
        elif is_amc:
            timing      = "财报当日收盘前做空"
            timing_note = "AMC财报，尾盘建空等次日gap down"
        else:
            timing      = "财报后确认方向再做空"
            timing_note = "发布时间不详，确认后做空"

        el = round(cur * 0.995, 2);  eh = round(cur * 1.003, 2)
        sl = round(cur * (1 + atr * 1.5), 2)
        t1 = round(cur * (1 - atr * 2.0), 2)
        t2 = round(cur * (1 - atr * 3.5), 2)

    rr = round(abs(t1 - cur) / abs(cur - sl), 2) if abs(cur - sl) > 0 else 0.0
    return {
        "timing":      timing,
        "timing_note": timing_note,
        "entry_low":   el,
        "entry_high":  eh,
        "stop_loss":   sl,
        "target_1":    t1,
        "target_2":    t2,
        "rr":          rr,
    }


# ── Step 4: Main screener ──────────────────────────────────────────────────────

def screen_earnings_plays() -> list[dict]:
    import yfinance as yf

    today = date.today()

    # Step 1: pull full week calendar
    print(f"[earnings] Fetching NASDAQ earnings calendar ({today} → {today + timedelta(days=EARNINGS_WINDOW_DAYS)}) ...", flush=True)
    calendar = fetch_earnings_calendar(today, EARNINGS_WINDOW_DAYS)
    print(f"[earnings] Calendar total: {len(calendar)} stocks next week", flush=True)

    # Step 2: cross-reference with universe
    matches = cross_reference_universe(calendar)
    print(f"[earnings] Universe matches: {len(matches)} stocks from our AI universe\n", flush=True)

    # Load supporting data
    candidates:     dict[str, dict] = {}
    leading_secs:   set[str]        = set()
    if CANDIDATES_JSON.exists():
        data = json.loads(CANDIDATES_JSON.read_text())
        for c in data.get("candidates", []):
            candidates[c["symbol"]] = c
            candidates[c.get("yf_symbol", "")] = c

    today_str = str(today)
    for mkt in ("us", "ah"):
        p = PROJECT_ROOT / "reports" / "daily" / f"{today_str}-{mkt}-rotation.json"
        if p.exists():
            try:
                for s in json.loads(p.read_text()).get("leading_sectors_today", []):
                    leading_secs.add(s.get("sector", ""))
            except Exception:
                pass

    plays: list[dict] = []

    # Step 3: detailed analysis for each matched stock
    for entry in matches:
        yf_sym  = entry["yf_symbol"]
        our_sym = entry["our_symbol"]
        print(f"  Analysing {our_sym} ({entry['company_name'][:25]}) — {entry['earnings_date']} {entry['release_time']}", flush=True)

        try:
            t = yf.Ticker(yf_sym)
            info      = t.info or {}
            candidate = candidates.get(our_sym) or candidates.get(yf_sym)

            cur_price = float(
                (candidate or {}).get("current_price")
                or info.get("currentPrice") or info.get("regularMarketPrice") or 0
            )
            atr_pct = float((candidate or {}).get("atr_pct", 0.03))
            ret_5d  = float((candidate or {}).get("ret_5d",  0.0))

            # Historical reactions
            reactions = get_post_earnings_reactions(t, HISTORY_QUARTERS)
            direction = analyze_direction(reactions, candidate, ret_5d)
            side      = direction["side"]

            # Scores
            react_score,  react_note  = score_historical(reactions, side)
            tech_score,   tech_note   = score_technical(candidate)
            analyst_score, analyst_note = score_analyst(info)
            eps_score,    eps_note    = score_eps_trend(t)
            sect_score = 10.0 if entry["sector"] in leading_secs else 3.0
            total = round(react_score + tech_score + analyst_score + eps_score + sect_score, 1)

            if total < MIN_SCORE:
                print(f"    → score {total} < {MIN_SCORE}, skipped", flush=True)
                continue

            # Entry strategy
            entry_plan = build_entry_strategy(
                side, direction["win_rate"], ret_5d,
                cur_price, atr_pct, entry["release_time"],
            )

            tgt_price  = float(info.get("targetMeanPrice") or 0)
            tgt_upside = round((tgt_price - cur_price) / cur_price, 4) if cur_price > 0 and tgt_price > 0 else 0.0

            plays.append({
                # Identity
                "symbol":       our_sym,
                "yf_symbol":    yf_sym,
                "company_name": entry["company_name"],
                "market":       entry["market"],
                "sector":       entry["sector"],
                "market_cap":   entry["market_cap"],

                # Earnings event
                "earnings_date":    entry["earnings_date"],
                "days_to_earnings": (date.fromisoformat(entry["earnings_date"]) - today).days,
                "release_time":     entry["release_time"],    # pre-market / after-hours
                "eps_forecast":     entry["eps_forecast"],
                "eps_last_year":    entry["eps_last_year"],
                "fiscal_quarter":   entry["fiscal_quarter"],

                # Price
                "current_price": round(cur_price, 4),
                "atr_pct":       round(atr_pct, 4),
                "ret_5d":        round(ret_5d, 4),

                # Direction
                "side":            direction["side"],         # LONG / SHORT
                "win_rate":        direction["win_rate"],
                "avg_move":        direction["avg_move"],
                "conviction":      direction["conviction"],
                "history_label":   direction["history_label"],  # "▲+9.2%  ▼-3.1%  ..."
                "historical_reactions": reactions,

                # Entry plan
                **entry_plan,

                # Analyst
                "analyst_target": round(tgt_price, 2),
                "target_upside":  tgt_upside,
                "rec_mean":       round(float(info.get("recommendationMean") or 3.0), 1),

                # Scores
                "total_score": total,
                "score_breakdown": {
                    "historical_reaction": react_score,
                    "technical_setup":     tech_score,
                    "analyst_conviction":  analyst_score,
                    "eps_surprise_trend":  eps_score,
                    "sector_momentum":     sect_score,
                },
                "notes": {
                    "reaction":  react_note,
                    "technical": tech_note,
                    "analyst":   analyst_note,
                    "eps":       eps_note,
                },
                "is_hot_sector": entry["sector"] in leading_secs,
            })
            side_emoji = "🟢多" if side == "LONG" else "🔴空"
            print(
                f"    → {side_emoji} 胜率{direction['win_rate']:.0%} "
                f"评分{total}  {entry_plan['timing']}",
                flush=True,
            )

        except Exception as exc:
            print(f"    → failed: {exc}", flush=True)
            continue

        time.sleep(0.3)

    plays.sort(key=lambda x: x["total_score"], reverse=True)
    print(f"\n[earnings] Done: {len(plays)} plays above threshold (min {MIN_SCORE})", flush=True)
    return plays


def main() -> None:
    import datetime
    load_env_file()
    plays = screen_earnings_plays()
    payload = {
        "generated_at":   datetime.datetime.utcnow().isoformat() + "Z",
        "date":           str(date.today()),
        "window_days":    EARNINGS_WINDOW_DAYS,
        "earnings_plays": plays,
    }
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"Saved {len(plays)} earnings plays → {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
