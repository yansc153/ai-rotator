from __future__ import annotations

from typing import Any

import pandas as pd


MIN_HISTORY_BARS = 14
FULL_SIGNAL_BARS = 60


def _last_float(series: pd.Series, default: float = 0.0) -> float:
    try:
        value = series.iloc[-1]
        if pd.isna(value):
            return default
        return float(value)
    except (IndexError, TypeError, ValueError):
        return default


def _rolling_avedev(series: pd.Series, window: int) -> pd.Series:
    mean = series.rolling(window).mean()
    return (series - mean).abs().rolling(window).mean()


def _cci(typical: pd.Series, ma_window: int, avedev_window: int) -> pd.Series:
    ma = typical.rolling(ma_window).mean()
    avedev = _rolling_avedev(typical, avedev_window)
    denom = 0.015 * avedev
    return (typical - ma) / denom.replace(0, float("nan"))


def _filtered_recent(signal: pd.Series, window: int) -> pd.Series:
    filtered: list[bool] = []
    cooldown = 0
    for raw in signal.fillna(False).astype(bool).tolist():
        if raw and cooldown == 0:
            filtered.append(True)
            cooldown = window
        else:
            filtered.append(False)
            cooldown = max(0, cooldown - 1)
    return pd.Series(filtered, index=signal.index)


def evaluate_three_locks(frame: pd.DataFrame) -> dict[str, Any]:
    """Return an explainable daily-bar technical confirmation signal.

    This mirrors the useful parts of the pasted "三把锁" formula but keeps the
    product contract simple: a status, a score, support/pressure, and a reason.
    It is a confirmation layer, not an independent trading system.
    """
    if frame.empty or len(frame) < MIN_HISTORY_BARS:
        return {
            "status": "insufficient_history",
            "score": 0.0,
            "k_state": "neutral",
            "above_ma5": False,
            "above_ma10": False,
            "bullish_trigger": False,
            "pressure_level": None,
            "support_level": None,
            "breakout_pressure": False,
            "breakdown_support": False,
            "reason": f"日线历史不足{MIN_HISTORY_BARS}根",
        }

    df = frame.sort_values("date").copy()
    close = pd.to_numeric(df["close"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    open_ = pd.to_numeric(df["open"], errors="coerce")

    ma5 = close.rolling(5).mean()
    ma10 = close.rolling(10).mean()
    typical = (high + low + close) / 3

    abc12 = (2 * close + high + low) / 4
    low_5 = low.rolling(5).min()
    high_4 = high.rolling(4).max()
    special_base = ((abc12 - low_5) / (high_4 - low_5).replace(0, float("nan"))) * 100
    c1 = special_base.ewm(span=4, adjust=False).mean()
    c2 = (0.667 * c1.shift(1) + 0.333 * c1).ewm(span=2, adjust=False).mean()
    k_red = c1 >= c2
    k_blue = c2 > c1

    cci1 = _cci(typical, min(6, len(df)), min(5, len(df)))
    cci4_window = min(30, len(df))
    cci4 = _cci(typical, cci4_window, cci4_window)
    diff4 = (cci4.shift(1) - cci4).abs()

    pillar_up = cci1 > diff4
    pillar_down = cci1 < -diff4
    formed2 = (pillar_down.shift(1) == True) & (close > low.shift(1))
    bullish_signal = _filtered_recent(formed2, 5) & (close > ma10)

    aaa = (3 * close + high + low + open_) / 6
    weighted_line = (
        8 * aaa
        + 7 * aaa.shift(1)
        + 6 * aaa.shift(2)
        + 5 * aaa.shift(3)
        + 4 * aaa.shift(4)
        + 3 * aaa.shift(5)
        + 2 * aaa.shift(6)
        + aaa.shift(8)
    ) / 36
    short_support_line = (
        weighted_line.rolling(2).min()
        + weighted_line.rolling(4).min()
        + weighted_line.rolling(6).min()
    ) / 3

    last_close = _last_float(close)
    last_ma5 = _last_float(ma5)
    last_ma10 = _last_float(ma10)
    above_ma5 = bool(last_close > last_ma5 > 0)
    above_ma10 = bool(last_close > last_ma10 > 0)
    k_state = "red" if bool(k_red.iloc[-1]) else ("blue" if bool(k_blue.iloc[-1]) else "neutral")

    pressure_candidates = high.shift(1).where(pillar_up)
    support_candidates = low.shift(1).where(pillar_down)
    pressure_level = _last_float(pressure_candidates.dropna(), default=0.0) if pressure_candidates.notna().any() else None
    support_level = _last_float(support_candidates.dropna(), default=0.0) if support_candidates.notna().any() else None
    if support_level is None:
        support_candidate = _last_float(short_support_line, default=0.0)
        support_level = support_candidate if support_candidate > 0 else None

    breakout_pressure = bool(pressure_level is not None and last_close > pressure_level)
    trend_break = bool(last_ma10 > 0 and last_close < last_ma10 * 0.95)
    breakdown_support = bool((support_level is not None and last_close < support_level) or trend_break)
    bullish_recent = bool(bullish_signal.tail(5).any())

    score = 0.0
    short_history = len(df) < FULL_SIGNAL_BARS
    if k_state == "red":
        score += 25.0
    if above_ma5 and above_ma10:
        score += 25.0
    elif above_ma5 or above_ma10:
        score += 12.0
    if bullish_recent:
        score += 30.0
    if breakout_pressure:
        score += 10.0
    if support_level is not None and not breakdown_support:
        score += 10.0
    if breakdown_support or trend_break:
        score = min(score, 15.0)
    elif short_history:
        score = min(score, 70.0)

    if breakdown_support or trend_break or k_state == "blue":
        status = "invalid"
    elif score >= 80.0:
        status = "triple_lock"
    elif score >= 50.0:
        status = "double_lock"
    elif score >= 25.0:
        status = "single_lock"
    else:
        status = "invalid"

    reasons: list[str] = []
    if k_state == "red":
        reasons.append("红K")
    elif k_state == "blue":
        reasons.append("蓝K")
    if above_ma5:
        reasons.append("站上操盘线")
    if above_ma10:
        reasons.append("站上黄金线")
    if bullish_recent:
        reasons.append("看涨触发")
    if breakout_pressure:
        reasons.append("突破压力")
    elif pressure_level is not None:
        reasons.append("未突破压力")
    if breakdown_support:
        reasons.append("跌破支撑")
    elif trend_break:
        reasons.append("跌破黄金线")
    if short_history and status != "insufficient_history":
        reasons.append("短史确认")

    return {
        "status": status,
        "score": round(score, 4),
        "k_state": k_state,
        "above_ma5": above_ma5,
        "above_ma10": above_ma10,
        "bullish_trigger": bullish_recent,
        "pressure_level": round(pressure_level, 4) if pressure_level is not None else None,
        "support_level": round(support_level, 4) if support_level is not None else None,
        "breakout_pressure": breakout_pressure,
        "breakdown_support": breakdown_support,
        "reason": " + ".join(reasons) if reasons else "结构未确认",
    }
