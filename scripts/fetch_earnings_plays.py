"""Earnings Play Screener — 赌财报模块

Scans the AI-sector universe for stocks with earnings announcements next 7 days,
scores them across 5 dimensions, and returns the top plays ranked by "跑断线" probability.

Scoring model (0–100):
  surprise_history  (0–30): avg EPS beat % over last 4 quarters + consistency bonus
  technical_setup   (0–25): pre-earnings drift quality (momentum but not overbought)
  analyst_conviction(0–20): consensus rating + price-target upside
  vol_move_size     (0–15): expected move based on historical post-earnings ATR amplification
  sector_momentum   (0–10): is the stock in a leading AI sector?

Timing recommendation:
  "盘前买入"  — stock has been drifting up into earnings, buy pre-market on earnings day
  "盘后买入"  — wait for the earnings reaction, then buy the confirmed gap up or gap and go
  "收盘前买入" — buy before close on earnings day (gamma / momentum play)
  "观望"     — setup not clean enough

Output: list of dicts, saved to data/earnings_plays.json
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

OUTPUT_JSON = PROJECT_ROOT / "data" / "earnings_plays.json"
CANDIDATES_JSON = PROJECT_ROOT / "data" / "candidates.json"

# Look-ahead window: earnings within next N days
EARNINGS_WINDOW_DAYS = 7

# Minimum score threshold to include in output
MIN_SCORE = 40

# Markets to scan (US has the richest yfinance earnings data)
EARNINGS_MARKETS = {"US", "HK"}


def _load_candidates_by_market() -> dict[str, dict]:
    """Load candidates.json → {symbol: candidate_dict} for quick lookup."""
    if not CANDIDATES_JSON.exists():
        return {}
    data = json.loads(CANDIDATES_JSON.read_text())
    return {c["symbol"]: c for c in data.get("candidates", [])}


def _load_leading_sectors() -> set[str]:
    """Read today's rotation reports and extract leading sectors."""
    today = str(date.today())
    leading: set[str] = set()
    for market_code in ("us", "ah"):
        path = PROJECT_ROOT / "reports" / "daily" / f"{today}-{market_code}-rotation.json"
        if path.exists():
            try:
                d = json.loads(path.read_text())
                for s in d.get("leading_sectors_today", []):
                    leading.add(s.get("sector", ""))
            except Exception:
                pass
    return leading


def _score_surprise_history(ticker_obj) -> tuple[float, str]:
    """Score based on last 4 quarters EPS surprise history. Returns (score, note)."""
    try:
        hist = ticker_obj.earnings_history
        if hist is None or hist.empty:
            return 0.0, "无财报历史"
        # Keep last 4 quarters
        hist = hist.tail(4)
        if "EPS Estimate" not in hist.columns or "Reported EPS" not in hist.columns:
            return 0.0, "财报字段缺失"

        surprises = []
        for _, row in hist.iterrows():
            est = float(row.get("EPS Estimate", 0) or 0)
            actual = float(row.get("Reported EPS", 0) or 0)
            if est != 0:
                surprises.append((actual - est) / abs(est))

        if not surprises:
            return 0.0, "无EPS数据"

        avg_beat = sum(surprises) / len(surprises)
        beat_quarters = sum(1 for s in surprises if s > 0)
        consistency = beat_quarters / len(surprises)  # fraction of quarters beat

        # Score: avg beat contribution (max 20 pts) + consistency bonus (max 10 pts)
        beat_score = min(20.0, max(-20.0, avg_beat * 100))  # 10% beat → 10pts
        consistency_score = consistency * 10.0
        total = max(0.0, beat_score + consistency_score)
        note = f"近{len(surprises)}季平均超预期{avg_beat:+.1%}，{beat_quarters}/{len(surprises)}季超预期"
        return round(min(30.0, total), 1), note
    except Exception as exc:
        return 0.0, f"数据获取失败: {exc}"


def _score_technical_setup(candidate: dict | None, symbol: str) -> tuple[float, str]:
    """Score pre-earnings technical setup. Ideal: positive momentum, not overbought."""
    if not candidate:
        return 5.0, "候选池外，无技术数据"

    ret_5d = float(candidate.get("ret_5d", 0))
    ret_20d = float(candidate.get("ret_20d", 0))
    atr_pct = float(candidate.get("atr_pct", 0))
    current = float(candidate.get("current_price", 0))
    high_20d = float(candidate.get("high_20d", current) or current)

    score = 0.0
    notes = []

    # Pre-earnings drift: ideally +2% to +15% in last 5 days (sign of institutional accumulation)
    if 0.02 <= ret_5d <= 0.15:
        score += 12.0
        notes.append(f"5日漂移+{ret_5d:.1%}理想")
    elif 0 < ret_5d < 0.02:
        score += 6.0
        notes.append("轻微上漂")
    elif ret_5d > 0.20:
        score += 2.0   # overbought pre-earnings — risk of sell the news
        notes.append(f"过热(+{ret_5d:.1%})风险卖消息")
    else:
        score += 0.0
        notes.append("弱势/下跌")

    # 20d trend: uptrend adds confidence
    if ret_20d > 0.05:
        score += 8.0
        notes.append("20日上行趋势")
    elif ret_20d > 0:
        score += 4.0

    # ATR: higher ATR = more likely to make a big move post-earnings
    if atr_pct > 0.04:
        score += 5.0
        notes.append("高波动")

    return round(min(25.0, score), 1), "，".join(notes)


def _score_analyst_conviction(info: dict) -> tuple[float, str]:
    """Score based on analyst consensus rating and price-target upside."""
    try:
        # recommendationMean: 1=Strong Buy, 2=Buy, 3=Hold, 4=Sell, 5=Strong Sell
        rec_mean = float(info.get("recommendationMean") or 3.0)
        target = float(info.get("targetMeanPrice") or 0)
        current = float(info.get("currentPrice") or info.get("regularMarketPrice") or 0)

        # Rating score: 1→20pts, 2→15pts, 3→5pts, 4+→0pts
        rating_score = max(0.0, (4.0 - rec_mean) / 3.0 * 15.0)

        # Upside score: price target vs current
        if current > 0 and target > 0:
            upside = (target - current) / current
            upside_score = min(5.0, upside * 20)  # 25% upside → 5pts
        else:
            upside_score = 0.0

        total = round(min(20.0, rating_score + upside_score), 1)
        label = f"评级均值{rec_mean:.1f}"
        if target > 0 and current > 0:
            label += f"，目标价上行{(target-current)/current:+.1%}"
        return total, label
    except Exception:
        return 5.0, "分析师数据不可用"


def _score_vol_move_size(ticker_obj, atr_pct: float) -> tuple[float, str]:
    """Estimate post-earnings move size based on historical post-earnings ATR amplification."""
    try:
        # yfinance earnings_history gives dates — we can estimate post-earnings ATR expansion
        # Proxy: use implied expected move from options if available, else use ATR * amplification
        # For now: use historical earnings moves if we can compute from price history
        # Simple proxy: stocks with ATR > 5% tend to move > 8% on earnings → high probability
        if atr_pct > 0.07:
            return 15.0, "历史高波动→财报大幅波动概率高"
        elif atr_pct > 0.04:
            return 10.0, "中等波动→财报可能有显著波动"
        elif atr_pct > 0.02:
            return 5.0, "低波动→财报波动可能有限"
        else:
            return 2.0, "波动极低"
    except Exception:
        return 5.0, "波动估算失败"


def _recommend_timing(ret_5d: float, rec_mean: float, surprise_score: float) -> str:
    """Determine optimal entry timing for the earnings play."""
    # Strong pre-earnings drift + strong analyst conviction + good surprise history
    if ret_5d > 0.05 and rec_mean <= 2.0 and surprise_score >= 20:
        return "盘前买入"   # Pre-market on earnings day — riding the drift
    # Positive but not overextended, strong fundamentals
    elif 0 < ret_5d <= 0.15 and surprise_score >= 15:
        return "盘前买入"
    # Good surprise history but technical is flat or just turned positive
    elif surprise_score >= 20:
        return "盘后买入"   # Wait for the reaction, buy confirmed move
    # Weaker setup
    elif surprise_score >= 10 or ret_5d > 0.03:
        return "收盘前买入"  # Last-hour momentum play
    else:
        return "观望"


def _get_earnings_date(ticker_obj) -> str | None:
    """Get next earnings date for a ticker. Returns ISO date string or None."""
    try:
        cal = ticker_obj.calendar
        if cal is None:
            return None
        # calendar is a dict with 'Earnings Date' key (list of Timestamps)
        if isinstance(cal, dict):
            earnings_dates = cal.get("Earnings Date", [])
            if not earnings_dates:
                return None
            # Take the soonest upcoming date
            today = date.today()
            for d in sorted(earnings_dates):
                try:
                    d_date = d.date() if hasattr(d, "date") else date.fromisoformat(str(d)[:10])
                    if d_date >= today:
                        return str(d_date)
                except Exception:
                    pass
        return None
    except Exception:
        return None


def screen_earnings_plays(universe_csv_path: Path) -> list[dict]:
    """Main screening function. Returns sorted list of earnings plays."""
    import pandas as pd
    import yfinance as yf

    universe = pd.read_csv(universe_csv_path)
    candidates_by_sym = _load_candidates_by_market()
    leading_sectors = _load_leading_sectors()

    today = date.today()
    window_end = today + timedelta(days=EARNINGS_WINDOW_DAYS)

    # Filter universe to AI-relevant markets with earnings data support
    scan_df = universe[universe["market"].isin(EARNINGS_MARKETS)].copy()
    print(f"[earnings] Scanning {len(scan_df)} {list(EARNINGS_MARKETS)} tickers for earnings next {EARNINGS_WINDOW_DAYS}d ...", flush=True)

    plays: list[dict] = []
    checked = 0
    found = 0

    for _, row in scan_df.iterrows():
        symbol = str(row["yf_symbol"])
        our_sym = str(row["symbol"])
        market = str(row["market"])

        try:
            t = yf.Ticker(symbol)
            earnings_date_str = _get_earnings_date(t)
            checked += 1

            if not earnings_date_str:
                continue

            earnings_date = date.fromisoformat(earnings_date_str)
            if not (today <= earnings_date <= window_end):
                continue

            found += 1
            # Fetch info once (covers price, analyst consensus, forward estimates)
            info = t.info or {}
            candidate = candidates_by_sym.get(our_sym) or candidates_by_sym.get(symbol)

            # 1. Surprise history
            surprise_score, surprise_note = _score_surprise_history(t)
            # 2. Technical setup
            atr_pct = float(candidate.get("atr_pct", 0) if candidate else 0.03)
            ret_5d = float(candidate.get("ret_5d", 0) if candidate else 0)
            tech_score, tech_note = _score_technical_setup(candidate, our_sym)
            # 3. Analyst conviction
            rec_mean = float(info.get("recommendationMean") or 3.0)
            analyst_score, analyst_note = _score_analyst_conviction(info)
            # 4. Volatility / move size
            vol_score, vol_note = _score_vol_move_size(t, atr_pct)
            # 5. Sector momentum
            sector = str(row.get("sector_tags", "")).split(",")[0].strip()
            sector_score = 10.0 if sector in leading_sectors else 3.0

            total_score = round(surprise_score + tech_score + analyst_score + vol_score + sector_score, 1)

            if total_score < MIN_SCORE:
                continue

            timing = _recommend_timing(ret_5d, rec_mean, surprise_score)

            current_price = float(
                candidate.get("current_price") if candidate
                else info.get("currentPrice") or info.get("regularMarketPrice") or 0
            )
            target_mean = float(info.get("targetMeanPrice") or 0)

            plays.append({
                "symbol": our_sym,
                "yf_symbol": symbol,
                "company_name": str(row.get("name", symbol)),
                "market": market,
                "sector": sector,
                "earnings_date": earnings_date_str,
                "days_to_earnings": (earnings_date - today).days,
                "current_price": round(current_price, 4),
                "atr_pct": round(atr_pct, 4),
                "ret_5d": round(ret_5d, 4),
                "market_cap": float(row.get("market_cap") or 0),
                "timing": timing,
                "total_score": total_score,
                "score_breakdown": {
                    "surprise_history": surprise_score,
                    "technical_setup": tech_score,
                    "analyst_conviction": analyst_score,
                    "vol_move_size": vol_score,
                    "sector_momentum": sector_score,
                },
                "notes": {
                    "surprise": surprise_note,
                    "technical": tech_note,
                    "analyst": analyst_note,
                    "volatility": vol_note,
                },
                "analyst_target": round(target_mean, 2),
                "target_upside": round((target_mean - current_price) / current_price, 4) if current_price > 0 and target_mean > 0 else 0.0,
                "is_hot_sector": sector in leading_sectors,
            })

            print(f"  ✓ {our_sym} {row.get('name','')} — 财报{earnings_date_str} 评分{total_score} {timing}", flush=True)

        except Exception as exc:
            pass  # silent: most tickers won't have earnings data

        # Throttle: don't hammer yfinance
        if checked % 20 == 0:
            time.sleep(0.5)

    plays.sort(key=lambda x: x["total_score"], reverse=True)
    print(f"[earnings] Scanned {checked} tickers, {found} with upcoming earnings, {len(plays)} above threshold", flush=True)
    return plays


def main() -> None:
    load_env_file()
    universe_csv = PROJECT_ROOT / "data" / "universe_full.csv"
    plays = screen_earnings_plays(universe_csv)

    payload = {
        "generated_at": __import__("datetime").datetime.utcnow().isoformat() + "Z",
        "date": str(date.today()),
        "window_days": EARNINGS_WINDOW_DAYS,
        "earnings_plays": plays,
    }
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"Saved {len(plays)} earnings plays → {OUTPUT_JSON}")


if __name__ == "__main__":
    main()
