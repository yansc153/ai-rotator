from __future__ import annotations

import akshare as ak
import pandas as pd


def get_cn_stock_data(symbol: str, start_date: str, end_date: str) -> str:
    raw = symbol.split(".")[0]
    frame = ak.stock_zh_a_hist(symbol=raw, period="daily", start_date=start_date.replace("-", ""), end_date=end_date.replace("-", ""), adjust="qfq")
    return frame.tail(30).to_string(index=False) if isinstance(frame, pd.DataFrame) and not frame.empty else ""
