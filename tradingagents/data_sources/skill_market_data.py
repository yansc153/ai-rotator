"""Thin adapters for the user-selected a-stock/global-stock data skills.

The skills are published as SKILL.md code blocks, not importable packages, so we
keep only the live data calls this pipeline needs.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

import requests

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def yahoo_chart(symbol: str, *, interval: str = "15m", range_: str = "5d", timeout: int = 15) -> list[dict[str, Any]]:
    """Yahoo chart API from global-stock-data; works for US and HK intraday."""
    url = f"https://query2.finance.yahoo.com/v8/finance/chart/{symbol}"
    resp = requests.get(url, params={"interval": interval, "range": range_}, headers={"User-Agent": UA}, timeout=timeout)
    resp.raise_for_status()
    chart = resp.json().get("chart", {}).get("result", [{}])[0]
    timestamps = chart.get("timestamp", [])
    quote = chart.get("indicators", {}).get("quote", [{}])[0]
    rows: list[dict[str, Any]] = []
    for i, ts in enumerate(timestamps):
        try:
            open_ = quote["open"][i]
            high = quote["high"][i]
            low = quote["low"][i]
            close = quote["close"][i]
            volume = quote["volume"][i]
        except (IndexError, KeyError):
            continue
        if close is None:
            continue
        dt_format = "%Y-%m-%d %H:%M" if "m" in interval or "h" in interval else "%Y-%m-%d"
        rows.append(
            {
                "datetime": datetime.fromtimestamp(ts).strftime(dt_format),
                "open": float(open_ or close),
                "high": float(high or close),
                "low": float(low or close),
                "close": float(close),
                "volume": int(volume or 0),
            }
        )
    return rows


def mootdx_cn_bars(code: str, *, category: int = 9, offset: int = 120) -> list[dict[str, Any]]:
    """A-stock-data mootdx bars: category 9 is 15m."""
    from mootdx.quotes import Quotes

    client = Quotes.factory(market="std")
    df = client.bars(symbol=code, category=category, offset=offset)
    if df is None or df.empty:
        return []
    rows: list[dict[str, Any]] = []
    for _, row in df.iterrows():
        dt = row.get("datetime") or row.get("date") or row.get("time")
        rows.append(
            {
                "datetime": str(dt),
                "open": float(row.get("open", row.get("close", 0)) or 0),
                "high": float(row.get("high", row.get("close", 0)) or 0),
                "low": float(row.get("low", row.get("close", 0)) or 0),
                "close": float(row.get("close", 0) or 0),
                "volume": float(row.get("vol", row.get("volume", 0)) or 0),
            }
        )
    return [row for row in rows if row["close"] > 0]

