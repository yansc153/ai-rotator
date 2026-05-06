"""Parse AI_全市场股票清单_真实数据.md → data/universe_full.csv

Output columns:
    symbol       e.g. "300308.SZ", "0700.HK", "NVDA"
    yf_symbol    yfinance-compatible ticker (HK uses 4-digit: "0700.HK")
    akshare_symbol  akshare-compatible code (HK 5-digit no-dot: "00700")
    name         Chinese company name
    market       CN | HK | US
    sector_tags  comma-separated AI concept tags from the source file
    market_cap   float (億RMB for CN, 億HKD for HK, $B USD for US)
    pe           float or NaN
    float_pct    float percentage
    tags         raw tag string e.g. "【亏损】【低市值】"
    is_loss      1 if 【亏损】, 0 otherwise
    is_low_cap   1 if 【低市值】, 0 otherwise
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

DEFAULT_SOURCE = Path.home() / "Documents" / "AI_全市场股票清单_真实数据.md"
OUTPUT = ROOT / "data" / "universe_full.csv"


def _safe_float(s: str) -> float:
    try:
        return float(s.strip().replace(",", ""))
    except (ValueError, AttributeError):
        return float("nan")


def _parse_table_row(line: str) -> list[str] | None:
    """Split a markdown table row into cells, return None if it's a separator row."""
    line = line.strip()
    if not line.startswith("|"):
        return None
    if re.match(r"^\|[-| ]+\|$", line):
        return None  # separator row
    cells = [c.strip() for c in line.split("|")[1:-1]]
    return cells if len(cells) >= 4 else None


def _hk_to_yf(raw: str) -> str:
    """Convert HK code to yfinance 4-digit format.

    "01949.HK" → "1949.HK"
    "00700.HK" → "0700.HK"   (700 zero-padded to 4 digits)
    "09988.HK" → "9988.HK"
    """
    code = raw.split(".")[0]
    return f"{int(code):04d}.HK"


def _hk_to_akshare(raw: str) -> str:
    """Convert HK code to akshare 5-digit no-dot format.

    "00700.HK" → "00700"
    "0020.HK"  → "00020"
    """
    code = raw.split(".")[0]
    return code.zfill(5)


def parse(source: Path = DEFAULT_SOURCE) -> pd.DataFrame:
    text = source.read_text(encoding="utf-8")
    lines = text.splitlines()

    rows: list[dict] = []
    market: str | None = None

    for line in lines:
        # Detect section headers
        if "## 一、A股" in line:
            market = "CN"
            continue
        elif "## 二、港股" in line:
            market = "HK"
            continue
        elif "## 三、美股" in line:
            market = "US"
            continue

        if market is None:
            continue

        cells = _parse_table_row(line)
        if cells is None:
            continue

        # Skip header rows (first cell contains non-ticker text)
        if cells[0] in ("Ticker", "ticker", "代码", "symbol") or not cells[0]:
            continue

        symbol = cells[0].strip()

        if market == "CN":
            # | Ticker | 名称 | 总市值(亿RMB) | 流通市值(亿RMB) | 流通% | PE动态 | AI概念板块 | 标签 |
            if len(cells) < 7:
                continue
            name = cells[1]
            market_cap = _safe_float(cells[2])
            float_pct = _safe_float(cells[4])
            pe = _safe_float(cells[5])
            sector_tags = cells[6].strip().strip(";")
            tags = cells[7] if len(cells) > 7 else ""
            akshare_sym = symbol.split(".")[0]
            yf_sym = symbol  # CN not fetched via yfinance normally
            rows.append({
                "symbol": symbol,
                "yf_symbol": yf_sym,
                "akshare_symbol": akshare_sym,
                "name": name,
                "market": "CN",
                "sector_tags": sector_tags,
                "market_cap": market_cap,
                "pe": pe,
                "float_pct": float_pct,
                "tags": tags,
                "is_loss": int("亏损" in tags),
                "is_low_cap": int("低市值" in tags),
            })

        elif market == "HK":
            # | Ticker | 名称 | 总市值(亿HKD) | 流通市值(亿HKD) | 流通% | PE | AI概念板块 | 标签 |
            if len(cells) < 6:
                continue
            name = cells[1]
            market_cap = _safe_float(cells[2])
            float_pct = _safe_float(cells[4])
            pe = _safe_float(cells[5])
            sector_tags = cells[6].strip().strip(";") if len(cells) > 6 else ""
            tags = cells[7] if len(cells) > 7 else ""
            yf_sym = _hk_to_yf(symbol)
            akshare_sym = _hk_to_akshare(symbol)
            rows.append({
                "symbol": symbol,
                "yf_symbol": yf_sym,
                "akshare_symbol": akshare_sym,
                "name": name,
                "market": "HK",
                "sector_tags": sector_tags,
                "market_cap": market_cap,
                "pe": pe,
                "float_pct": float_pct,
                "tags": tags,
                "is_loss": int("亏损" in tags),
                "is_low_cap": int("低市值" in tags),
            })

        elif market == "US":
            # | Ticker | 名称 | 总市值($B) | 流通% | PE TTM | PE Fwd | AI赛道 | 标签 |
            if len(cells) < 5:
                continue
            name = cells[1]
            market_cap = _safe_float(cells[2])
            float_pct = _safe_float(cells[3])
            pe = _safe_float(cells[4])
            sector_tags = cells[6].strip() if len(cells) > 6 else ""
            tags = cells[7] if len(cells) > 7 else ""
            rows.append({
                "symbol": symbol,
                "yf_symbol": symbol,
                "akshare_symbol": symbol,
                "name": name,
                "market": "US",
                "sector_tags": sector_tags,
                "market_cap": market_cap,
                "pe": pe,
                "float_pct": float_pct,
                "tags": tags,
                "is_loss": int("亏损" in tags),
                "is_low_cap": int("低市值" in tags),
            })

    df = pd.DataFrame(rows)
    print(f"Parsed: CN={len(df[df.market=='CN'])}  HK={len(df[df.market=='HK'])}  US={len(df[df.market=='US'])}  total={len(df)}")
    return df


def main(source: Path = DEFAULT_SOURCE) -> None:
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    df = parse(source)
    df.to_csv(OUTPUT, index=False)
    print(f"Saved → {OUTPUT}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default=str(DEFAULT_SOURCE))
    args = ap.parse_args()
    main(Path(args.source))
