from __future__ import annotations

import yfinance as yf


def get_us_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    frame = yf.download(symbol, start=start_date, end=end_date, interval="1d", auto_adjust=False, progress=False)
    if frame.empty:
        return ""
    return frame.tail(30).to_string()
