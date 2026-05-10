"""Earnings Play Screener — 赌财报模块 v4

Financial modelling draws from three finance-skills patterns:
  • earnings-preview   → consensus EPS table, beat/miss history, surprise%
  • estimate-analysis  → eps_trend revision (7d/30d direction), revision breadth
  • options-payoff     → ATM straddle implied move (THE core pricing signal)

Correct architecture:
  Step 1  Pull full earnings calendar from NASDAQ API for next 7 days
          → ~250 stocks/day, 5 calls total for the week
  Step 2  Cross-reference with our universe_full.csv
          → typically 30–50 matches from our AI-sector universe
  Step 3  For each match, full financial model via yfinance:
            a) ATM straddle implied move  (market's priced gap magnitude)
            b) Historical post-earnings next-day reactions (5 quarters)
            c) Directional edge = hist_avg - implied  (positive = edge exists)
            d) EPS revision direction (30-day estimate revision)
            e) Beat rate + avg surprise % (4-quarter quality)
            f) Pre-earnings technical setup (SEPA Stage 2 check)
            g) Analyst consensus + price-target upside
  Step 4  Score (0–100) with options-based edge weighting, rank
  Step 5  Output top plays to data/earnings_plays.json

Scoring (0–100):
  implied_edge      (0–30): ATM-straddle implied vs actual historical move
                            positive edge → market underpricing history
  eps_revision      (0–20): 30-day EPS estimate revision + 4-quarter beat rate
  technical_setup   (0–20): pre-earnings drift quality + SEPA Stage 2 filter
  analyst_signal    (0–15): consensus rating + price-target upside
  sector_momentum   (0–10): stock is in a leading AI sector today
  liquidity_gate    (0–5):  ADTV filter (penalise illiquid small-caps)
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
                "release_time":      row.get("time", "time-not-supplied"),
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
        if d.weekday() >= 5:
            continue
        rows = _fetch_nasdaq_day(str(d))
        print(f"  [calendar] {d}: {len(rows)} stocks reporting", flush=True)
        all_rows.extend(rows)
        time.sleep(0.3)
    return all_rows


# ── Step 2: Cross-reference with universe ─────────────────────────────────────

def cross_reference_universe(calendar: list[dict]) -> list[dict]:
    """Filter calendar to stocks in our AI-sector universe."""
    import pandas as pd
    universe = pd.read_csv(UNIVERSE_CSV)

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

    pct = (close_1st_trading_day_after_earnings - close_last_day_before_earnings)
          / close_last_day_before_earnings
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

                # EPS surprise from earnings_dates row
                row = past.loc[earnings_ts]
                surp = None
                try:
                    surp_val = row.get("Surprise(%)") if hasattr(row, "get") else (
                        row["Surprise(%)"] if "Surprise(%)" in row.index else None
                    )
                    surp = round(float(surp_val) / 100, 4) if surp_val is not None and str(surp_val) not in ("nan", "NaN", "") else None
                except Exception:
                    surp = None

                reactions.append({
                    "date":       str(earnings_d),
                    "pct":        round(pct, 4),
                    "direction":  "▲" if pct >= 0 else "▼",
                    "eps_beat":   surp,
                })
            except Exception:
                continue
    except Exception:
        pass
    return reactions[:n]


# ── Step 3b: ATM straddle implied move ────────────────────────────────────────
# finance-skills: options-payoff pattern
# ATM straddle price / spot = market's priced earnings gap magnitude

def get_implied_move(ticker_obj, earnings_date_str: str, spot: float) -> dict:
    """Compute ATM straddle implied move from nearest post-earnings options expiry.

    Implied move = (ATM call last price + ATM put last price) / spot price
    This is the market consensus for the magnitude of the earnings gap.
    Compare to historical avg: if hist_avg > implied → directional edge exists.

    Returns:
        implied_move  float   e.g. 0.185 means market prices ±18.5% gap
        available     bool    False when options data absent (no-liquid small-caps)
    """
    result: dict = {"available": False, "implied_move": None,
                    "expiry": None, "atm_strike": None,
                    "atm_call": None, "atm_put": None, "iv_atm": None}
    if spot <= 0:
        return result
    try:
        expiries = ticker_obj.options
        if not expiries:
            return result

        earnings_d = date.fromisoformat(earnings_date_str)
        # First expiry on or after earnings date
        post_expiries = [e for e in expiries if date.fromisoformat(e) >= earnings_d]
        if not post_expiries:
            return result

        expiry = post_expiries[0]
        oc     = ticker_obj.option_chain(expiry)
        calls  = oc.calls
        puts   = oc.puts

        if calls.empty or puts.empty:
            return result

        # ATM strike: closest to current spot in available strikes
        strikes = calls["strike"].values
        atm_idx    = abs(strikes - spot).argmin()
        atm_strike = float(strikes[atm_idx])

        call_row = calls[calls["strike"] == atm_strike]
        put_row  = puts[puts["strike"]  == atm_strike]
        if call_row.empty or put_row.empty:
            return result

        call_row = call_row.iloc[0]
        put_row  = put_row.iloc[0]

        # Prefer lastPrice; fall back to mid of bid/ask
        def _price(row) -> float:
            lp = float(row.get("lastPrice", 0) or 0)
            if lp > 0:
                return lp
            bid = float(row.get("bid", 0) or 0)
            ask = float(row.get("ask", 0) or 0)
            return (bid + ask) / 2 if ask > 0 else 0.0

        call_price = _price(call_row)
        put_price  = _price(put_row)
        straddle   = call_price + put_price

        if straddle <= 0:
            return result

        iv_atm = float(call_row.get("impliedVolatility", 0) or 0)

        result.update({
            "available":    True,
            "implied_move": round(straddle / spot, 4),
            "expiry":       expiry,
            "atm_strike":   atm_strike,
            "atm_call":     round(call_price, 4),
            "atm_put":      round(put_price,  4),
            "iv_atm":       round(iv_atm, 4),
        })
    except Exception:
        pass
    return result


# ── Step 3c: EPS revision direction ───────────────────────────────────────────
# finance-skills: estimate-analysis pattern
# eps_trend table rows = current / 7daysAgo / 30daysAgo / 60daysAgo / 90daysAgo

def get_eps_revisions(ticker_obj) -> dict:
    """Fetch EPS revision direction (estimate-analysis pattern) + beat rate.

    eps_trend: DataFrame indexed by period string, columns = time windows.
    We focus on '0q' (current quarter) current vs 30daysAgo to flag
    analyst revision momentum — the most predictive pre-earnings signal.

    Returns:
        direction      str    '上调' / '下调' / '平稳' / None
        revision_pct   float  e.g. +0.042 = estimates up 4.2% in 30 days
        beat_rate      float  fraction of last N quarters that beat consensus
        avg_surprise_pct float mean Surprise(%) over recent quarters
    """
    result: dict = {
        "direction": None, "revision_pct": None,
        "current_est": None, "est_30d_ago": None,
        "beat_rate": None, "avg_surprise_pct": None,
        "available": False,
    }

    # 1) EPS revision from eps_trend table
    try:
        et = ticker_obj.eps_trend
        if et is not None and not et.empty:
            # columns may be period strings: '0q', '+1q', '0y', '+1y'
            # rows may be: 'current', '7daysAgo', '30daysAgo', ...
            col = "0q"
            if col not in et.columns:
                col = et.columns[0]

            def _safe_float(df, row_key, col_key):
                try:
                    return float(df.loc[row_key, col_key])
                except Exception:
                    return None

            current = _safe_float(et, "current",    col)
            d30     = _safe_float(et, "30daysAgo",  col)
            if current is not None and d30 is not None and d30 != 0:
                rev_pct = (current - d30) / abs(d30)
                direction = "上调" if rev_pct > 0.005 else ("下调" if rev_pct < -0.005 else "平稳")
                result.update({
                    "current_est": round(current, 4),
                    "est_30d_ago": round(d30, 4),
                    "revision_pct": round(rev_pct, 4),
                    "direction": direction,
                })
    except Exception:
        pass

    # 2) Beat rate + avg surprise from earnings_history
    try:
        eh = ticker_obj.earnings_history
        if eh is not None and not eh.empty and "Surprise(%)" in eh.columns:
            surprises = eh["Surprise(%)"].dropna()
            if len(surprises) > 0:
                beat_rate = float((surprises > 0).mean())
                avg_surp  = float(surprises.mean()) / 100   # pct → decimal
                result.update({
                    "beat_rate":        round(beat_rate, 4),
                    "avg_surprise_pct": round(avg_surp, 4),
                    "available": True,
                })
    except Exception:
        pass

    return result


# ── Step 3d: Direction + conviction (uses implied move) ───────────────────────

def analyze_direction(
    reactions: list[dict],
    candidate: dict | None,
    ret_5d: float,
    eps_rev: dict,
) -> dict:
    """Determine LONG vs SHORT and conviction.

    Priority:
      1. If clear EPS revision signal + historical agreement → follow both
      2. If only historical win rate ≥ 60% → follow history
      3. Else → follow 5-day pre-earnings drift
    """
    if not reactions:
        # No history: use eps revision + technicals
        if eps_rev.get("direction") == "上调":
            side = "LONG"
        elif eps_rev.get("direction") == "下调":
            side = "SHORT"
        else:
            side = "LONG" if ret_5d >= 0 else "SHORT"
        return {"side": side, "win_rate": None, "avg_move": None,
                "conviction": "低（无历史数据）", "history_label": "无历史"}

    ups   = [r["pct"] for r in reactions if r["pct"] >= 0]
    downs = [r["pct"] for r in reactions if r["pct"] < 0]
    n     = len(reactions)
    win_rate_long = len(ups) / n

    avg_abs  = sum(abs(r["pct"]) for r in reactions) / n
    avg_up   = sum(ups)   / len(ups)   if ups   else None
    avg_down = sum(downs) / len(downs) if downs else None

    # Revision alignment bonus
    rev_dir = eps_rev.get("direction")
    if win_rate_long >= 0.6:
        side = "LONG"
        # Downgrade to SHORT if EPS strongly revised down
        if rev_dir == "下调" and (eps_rev.get("revision_pct") or 0) < -0.05:
            side = "SHORT"
    elif win_rate_long <= 0.4:
        side = "SHORT"
        if rev_dir == "上调" and (eps_rev.get("revision_pct") or 0) > 0.05:
            side = "LONG"
    else:
        # Toss-up: EPS revision > 5d drift
        if rev_dir == "上调":
            side = "LONG"
        elif rev_dir == "下调":
            side = "SHORT"
        else:
            side = "LONG" if ret_5d >= 0 else "SHORT"

    wr_side = win_rate_long if side == "LONG" else 1 - win_rate_long
    if wr_side >= 0.8:
        conv = f"极高 {int(wr_side * n)}/{n}次{'涨' if side == 'LONG' else '跌'}"
    elif wr_side >= 0.6:
        conv = f"中高 {int(wr_side * n)}/{n}次{'涨' if side == 'LONG' else '跌'}"
    else:
        conv = "分歧 历史各半"

    history_label = "  ".join(f"{r['direction']}{r['pct']:+.1%}" for r in reactions)

    return {
        "side":          side,
        "win_rate":      round(wr_side, 4),
        "avg_move":      round(avg_abs, 4),
        "avg_up":        round(avg_up,   4) if avg_up   is not None else None,
        "avg_down":      round(avg_down, 4) if avg_down is not None else None,
        "conviction":    conv,
        "history_label": history_label,
    }


# ── Step 3e: Scoring functions ────────────────────────────────────────────────

def score_implied_edge(
    implied: dict,
    reactions: list[dict],
    side: str,
) -> tuple[float, str]:
    """Score directional edge = historical actual move vs options-implied move.

    finance-skills / options-payoff pattern:
      edge = hist_avg_abs - implied_move
      positive edge → market underpricing history → high conviction directional bet
      negative edge → options expensive → low directional value

    Max 30 pts:
      historical win rate : 0-15 pts
      magnitude edge vs implied : 0-15 pts
    """
    if not reactions:
        return 5.0, "无历史价格数据"

    n        = len(reactions)
    wins     = [r for r in reactions if (r["pct"] >= 0) == (side == "LONG")]
    win_rate = len(wins) / n
    avg_abs  = sum(abs(r["pct"]) for r in reactions) / n

    wr_score = min(15.0, win_rate * 20.0)   # 75% win rate → 15pts

    # Magnitude edge: compare to implied
    impl     = implied.get("implied_move")
    if impl and impl > 0:
        edge = avg_abs - impl
        if edge > 0.10:
            mag_score = 15.0; edge_str = f"历史均±{avg_abs:.1%} 隐含±{impl:.1%} 超额+{edge:.1%}✓"
        elif edge > 0.04:
            mag_score = 11.0; edge_str = f"历史均±{avg_abs:.1%} 隐含±{impl:.1%} 有效超额+{edge:.1%}"
        elif edge > -0.02:
            mag_score = 7.0;  edge_str = f"历史均±{avg_abs:.1%} 隐含±{impl:.1%} 基本公平"
        else:
            mag_score = 3.0;  edge_str = f"历史均±{avg_abs:.1%} 隐含±{impl:.1%} 期权偏贵{edge:.1%}"
    else:
        # No options data: score purely on magnitude
        mag_score = min(12.0, avg_abs * 120)
        edge_str  = f"历史均±{avg_abs:.1%} (期权数据不可用)"

    total = round(wr_score + mag_score, 1)
    return min(30.0, total), f"胜率{win_rate:.0%}({n}次)  {edge_str}"


def score_eps_revisions(eps_rev: dict) -> tuple[float, str]:
    """Score EPS revision direction + beat rate.

    finance-skills / estimate-analysis pattern:
      eps_trend 30-day revision: direction signal
      earnings_history Surprise(%): beat rate quality

    Max 20 pts:
      revision direction : 0-10 pts
      beat rate + avg surprise : 0-10 pts
    """
    rev_pct   = eps_rev.get("revision_pct") or 0
    direction = eps_rev.get("direction")
    beat_rate = eps_rev.get("beat_rate") or 0.5
    avg_surp  = eps_rev.get("avg_surprise_pct") or 0

    if direction == "上调":
        rev_score = min(10.0, 5.0 + rev_pct * 100)
        rev_label = f"↑EPS上调{rev_pct:+.1%}(30日)"
    elif direction == "下调":
        rev_score = max(1.0, 5.0 + rev_pct * 100)
        rev_label = f"↓EPS下调{rev_pct:+.1%}(30日)"
    elif direction == "平稳":
        rev_score = 4.0
        rev_label = "→EPS平稳(30日)"
    else:
        rev_score = 3.0
        rev_label = "EPS修正数据不可用"

    beat_score = min(10.0, beat_rate * 7.0 + max(0.0, avg_surp) * 30)
    beat_label = f"过去{int(round(beat_rate * 4))+1}季胜率{beat_rate:.0%}" if eps_rev.get("available") else ""

    total = round(rev_score + beat_score, 1)
    return min(20.0, total), f"{rev_label}  {beat_label}".strip()


def score_technical(candidate: dict | None) -> tuple[float, str]:
    """Score pre-earnings technical setup.

    finance-skills / sepa-strategy pattern:
      SEPA Stage 2: price trend direction check
      Ideal pre-earnings drift: 2-15% positive (not overbought, not downtrending)

    Max 20 pts.
    """
    if not candidate:
        return 5.0, "候选池外"
    ret_5d  = float(candidate.get("ret_5d",  0))
    ret_20d = float(candidate.get("ret_20d", 0))
    atr_pct = float(candidate.get("atr_pct", 0))
    score, parts = 0.0, []

    # Pre-earnings drift quality (2-15% 5d run is ideal)
    if 0.02 <= ret_5d <= 0.15:
        score += 10.0; parts.append(f"5日{ret_5d:+.1%}漂移理想")
    elif 0 < ret_5d < 0.02:
        score += 5.0;  parts.append("轻微正漂移")
    elif ret_5d > 0.20:
        score += 2.0;  parts.append(f"⚠过热{ret_5d:+.1%}可能卖消息")
    else:
        score += 0.0;  parts.append(f"弱势{ret_5d:+.1%}")

    # SEPA Stage 2 proxy: 20-day trend
    if ret_20d > 0.08:
        score += 7.0; parts.append("20日强势上行")
    elif ret_20d > 0.02:
        score += 4.0; parts.append("20日温和上行")
    elif ret_20d > 0:
        score += 2.0

    # Volatility (high vol = bigger gap potential)
    if atr_pct > 0.05:
        score += 3.0; parts.append("高波动")
    elif atr_pct > 0.03:
        score += 1.0

    return round(min(20.0, score), 1), "，".join(parts) or "无明显信号"


def score_analyst(info: dict) -> tuple[float, str]:
    """Analyst consensus + price-target upside. Max 15 pts."""
    try:
        rec = float(info.get("recommendationMean") or 3.0)
        tgt = float(info.get("targetMeanPrice")    or 0)
        cur = float(info.get("currentPrice") or info.get("regularMarketPrice") or 0)
        rs  = max(0.0, (4.0 - rec) / 3.0 * 10.0)   # rating 1→10pts, 4→0pts
        us  = min(5.0, (tgt - cur) / cur * 15) if cur > 0 and tgt > 0 else 0.0
        label = f"分析师评级{rec:.1f}/5"
        if cur > 0 and tgt > 0:
            label += f"  目标价{tgt:.2f}({(tgt-cur)/cur:+.1%})"
        return round(min(15.0, rs + us), 1), label
    except Exception:
        return 4.0, "分析师数据不可用"


def score_liquidity(info: dict, candidate: dict | None) -> tuple[float, str]:
    """Penalise illiquid stocks. Max 5 pts.

    finance-skills / stock-liquidity pattern:
      ADTV < $5M → too thin for institutional gap play → score 0
      ADTV > $50M → no constraint → score 5
    """
    try:
        avg_vol = float(info.get("averageVolume", 0) or 0)
        price   = float(info.get("currentPrice") or info.get("regularMarketPrice") or 0)
        if avg_vol <= 0 and candidate:
            avg_vol = float(candidate.get("volume", 0) or 0)
            price   = float(candidate.get("current_price", price) or price)
        adtv = avg_vol * price / 1e6   # $M
        if adtv >= 50:
            return 5.0, f"ADTV ${adtv:.0f}M 流动性充裕"
        elif adtv >= 10:
            return 3.0, f"ADTV ${adtv:.0f}M 中等流动性"
        elif adtv >= 3:
            return 1.0, f"ADTV ${adtv:.0f}M 流动性偏薄"
        else:
            return 0.0, f"ADTV ${adtv:.1f}M ⚠极薄"
    except Exception:
        return 2.0, "流动性数据不可用"


# ── Step 3f: Entry strategy ───────────────────────────────────────────────────

def build_entry_strategy(
    side: str, win_rate: float | None, ret_5d: float,
    cur: float, atr_pct: float, release_time: str,
) -> dict:
    """Compute timing + entry + SL + targets based on side and release time.

    release_time values:
      "time-pre-market"  → BMO: gaps at TODAY's open (build night before or pre-mkt)
      "time-after-hours" → AMC: gaps at NEXT DAY's open (build before today's close)
      "time-not-supplied"→ unknown
    """
    wr  = win_rate or 0.5
    atr = atr_pct or 0.03
    is_bmo = release_time == "time-pre-market"
    is_amc = release_time == "time-after-hours"

    if side == "LONG":
        if is_bmo:
            if wr >= 0.6 and ret_5d > 0.02:
                timing = "盘前买入（财报日前一晚建仓）"
            elif wr >= 0.6:
                timing = "财报日盘前买入"
            else:
                timing = "观望，等开盘gap确认"
        elif is_amc:
            if wr >= 0.6 and ret_5d > 0:
                timing = "收盘前30分钟建仓（赌AMC财报）"
            elif wr >= 0.6:
                timing = "收盘前观望，尾盘决策"
            else:
                timing = "财报后次日盘前等gap确认"
        else:
            timing = "收盘前建仓（发布时间未知）"

        el = round(cur * 0.997, 2); eh = round(cur * 1.005, 2)
        sl = round(cur * (1 - atr * 1.5), 2)
        t1 = round(cur * (1 + atr * 2.0), 2)
        t2 = round(cur * (1 + atr * 3.5), 2)

    else:  # SHORT
        if is_bmo:
            timing = "财报日开盘确认gap down后做空"
        elif is_amc:
            timing = "财报当日收盘前做空"
        else:
            timing = "财报后确认方向再做空"

        el = round(cur * 0.995, 2); eh = round(cur * 1.003, 2)
        sl = round(cur * (1 + atr * 1.5), 2)
        t1 = round(cur * (1 - atr * 2.0), 2)
        t2 = round(cur * (1 - atr * 3.5), 2)

    rr = round(abs(t1 - cur) / abs(cur - sl), 2) if abs(cur - sl) > 0 else 0.0
    return {
        "timing":     timing,
        "entry_low":  el,
        "entry_high": eh,
        "stop_loss":  sl,
        "target_1":   t1,
        "target_2":   t2,
        "rr":         rr,
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
    candidates: dict[str, dict] = {}
    leading_secs: set[str] = set()
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

    # yfinance rate-limit recovery after fetch_all_daily.py's massive batch
    print("[earnings] Waiting 45s for yfinance rate-limit recovery ...", flush=True)
    time.sleep(45)

    # Step 3: full financial model for each matched stock
    for entry in matches:
        yf_sym  = entry["yf_symbol"]
        our_sym = entry["our_symbol"]

        # Throttle BEFORE each call so even failed calls are spaced
        time.sleep(1.2)
        print(f"  Analysing {our_sym} ({entry['company_name'][:25]}) — {entry['earnings_date']} {entry['release_time']}", flush=True)

        # Retry with exponential backoff on rate-limit errors
        info: dict = {}
        t = None
        for attempt in range(3):
            try:
                t    = yf.Ticker(yf_sym)
                info = t.info or {}
                break
            except Exception as exc:
                if "Too Many Requests" in str(exc) or "Rate limit" in str(exc).lower():
                    wait = 15 * (2 ** attempt)
                    print(f"    [rate limit] sleeping {wait}s (attempt {attempt+1}/3)", flush=True)
                    time.sleep(wait)
                else:
                    print(f"    → info fetch failed: {exc}", flush=True)
                    break
        if t is None:
            continue

        try:
            candidate = candidates.get(our_sym) or candidates.get(yf_sym)
            cur_price = float(
                (candidate or {}).get("current_price")
                or info.get("currentPrice") or info.get("regularMarketPrice") or 0
            )
            atr_pct = float((candidate or {}).get("atr_pct", 0.03))
            ret_5d  = float((candidate or {}).get("ret_5d",  0.0))

            # 3a) Historical post-earnings reactions
            reactions = get_post_earnings_reactions(t, HISTORY_QUARTERS)

            # 3b) ATM straddle implied move  ← finance-skills: options-payoff
            implied = get_implied_move(t, entry["earnings_date"], cur_price)

            # 3c) EPS revision direction    ← finance-skills: estimate-analysis
            eps_rev = get_eps_revisions(t)

            # 3d) Direction + conviction
            direction = analyze_direction(reactions, candidate, ret_5d, eps_rev)
            side = direction["side"]

            # 3e) Scoring
            react_score,  react_note  = score_implied_edge(implied, reactions, side)
            eps_score,    eps_note    = score_eps_revisions(eps_rev)
            tech_score,   tech_note   = score_technical(candidate)
            analyst_score, analyst_note = score_analyst(info)
            liq_score,    liq_note    = score_liquidity(info, candidate)
            sect_score = 10.0 if entry["sector"] in leading_secs else 3.0

            total = round(react_score + eps_score + tech_score + analyst_score + liq_score + sect_score, 1)

            if total < MIN_SCORE:
                print(f"    → score {total} < {MIN_SCORE}, skipped", flush=True)
                continue

            # 3f) Entry strategy
            entry_plan = build_entry_strategy(
                side, direction["win_rate"], ret_5d,
                cur_price, atr_pct, entry["release_time"],
            )

            tgt_price  = float(info.get("targetMeanPrice") or 0)
            tgt_upside = round((tgt_price - cur_price) / cur_price, 4) if cur_price > 0 and tgt_price > 0 else 0.0

            # Implied edge label for Discord display
            impl_val = implied.get("implied_move")
            avg_abs  = direction.get("avg_move")
            if impl_val and avg_abs:
                edge = avg_abs - impl_val
                implied_label = f"期权隐含±{impl_val:.1%}  实际均±{avg_abs:.1%}  超额{'+' if edge>=0 else ''}{edge:.1%}"
            elif impl_val:
                implied_label = f"期权隐含±{impl_val:.1%}"
            elif avg_abs:
                implied_label = f"实际均±{avg_abs:.1%}（无期权数据）"
            else:
                implied_label = "无期权/历史数据"

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
                "release_time":     entry["release_time"],
                "eps_forecast":     entry["eps_forecast"],
                "eps_last_year":    entry["eps_last_year"],
                "fiscal_quarter":   entry["fiscal_quarter"],

                # Price
                "current_price": round(cur_price, 4),
                "atr_pct":       round(atr_pct, 4),
                "ret_5d":        round(ret_5d, 4),

                # Direction
                "side":            direction["side"],
                "win_rate":        direction["win_rate"],
                "avg_move":        direction["avg_move"],
                "conviction":      direction["conviction"],
                "history_label":   direction["history_label"],
                "historical_reactions": reactions,

                # Implied move (options-payoff model)
                "implied_move":   implied.get("implied_move"),
                "implied_expiry": implied.get("expiry"),
                "implied_edge":   round((direction.get("avg_move") or 0) - (implied.get("implied_move") or 0), 4),
                "iv_atm":         implied.get("iv_atm"),
                "implied_label":  implied_label,

                # EPS revision (estimate-analysis model)
                "eps_revision_direction": eps_rev.get("direction"),
                "eps_revision_pct":       eps_rev.get("revision_pct"),
                "beat_rate":              eps_rev.get("beat_rate"),
                "avg_surprise_pct":       eps_rev.get("avg_surprise_pct"),

                # Entry plan
                **entry_plan,

                # Analyst
                "analyst_target": round(tgt_price, 2),
                "target_upside":  tgt_upside,
                "rec_mean":       round(float(info.get("recommendationMean") or 3.0), 1),

                # Scores
                "total_score": total,
                "score_breakdown": {
                    "implied_edge":    react_score,
                    "eps_revision":    eps_score,
                    "technical_setup": tech_score,
                    "analyst_signal":  analyst_score,
                    "liquidity_gate":  liq_score,
                    "sector_momentum": sect_score,
                },
                "notes": {
                    "reaction":  react_note,
                    "eps":       eps_note,
                    "technical": tech_note,
                    "analyst":   analyst_note,
                    "liquidity": liq_note,
                },
                "is_hot_sector": entry["sector"] in leading_secs,
            })
            side_emoji = "🟢多" if side == "LONG" else "🔴空"
            print(
                f"    → {side_emoji} 胜率{direction['win_rate']:.0%} "
                f"评分{total}  {entry_plan['timing']}  {implied_label}",
                flush=True,
            )

        except Exception as exc:
            print(f"    → failed: {exc}", flush=True)
            continue

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
