from __future__ import annotations

from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus
from urllib.request import Request, urlopen


_CST = timezone(timedelta(hours=8))

AI_CORE_KEYWORDS = (
    "AI芯片",
    "人工智能",
    "AI应用",
    "AI Agent",
    "Agent",
    "AIGC",
    "大模型",
    "算力",
    "GPU",
    "CPO",
    "光模块",
    "光通信",
    "服务器",
    "数据中心",
    "液冷",
    "存储",
    "机器人",
    "智能驾驶",
    "云计算",
)

WEAK_OR_ADJACENT_KEYWORDS = (
    "MLCC",
    "被动元件",
    "电容",
    "电子元件",
    "PCB",
    "电源",
    "智能交通",
    "轨交",
)

PSEUDO_AI_KEYWORDS = ("传媒", "游戏", "营销", "教育")


def today_cst() -> str:
    return datetime.now(_CST).strftime("%Y-%m-%d")


def ashare_board(symbol: str, market: str | None = None) -> str | None:
    if (market or "").upper() != "CN":
        return None
    digits = "".join(ch for ch in str(symbol) if ch.isdigit())
    if digits.startswith(("688", "689")):
        return "科创板"
    if digits.startswith(("300", "301")):
        return "创业板"
    if digits.startswith(("600", "601", "603", "605")):
        return "沪主板"
    if digits.startswith(("000", "001", "002", "003")):
        return "深主板"
    return "A股"


def market_board_label(symbol: str, market: str) -> str:
    market = market.upper()
    if market == "CN":
        return f"A股·{ashare_board(symbol, market) or 'A股'}"
    if market == "HK":
        return "港股"
    if market == "US":
        return "美股"
    return market


def market_cap_cny_billion(market: str, market_cap: Any) -> float | None:
    """Convert local market cap units into 亿人民币.

    universe_full.csv stores CN as 亿 RMB, HK as 亿 HKD, and US as USD billions.
    """
    try:
        value = float(market_cap)
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    market = market.upper()
    if market == "CN":
        return value
    if market == "HK":
        return value * 0.92
    if market == "US":
        return value * 72.0
    return None


def market_cap_gate(market: str, market_cap: Any, *, floor_cny_billion: float = 200.0) -> dict[str, Any]:
    cap_cny = market_cap_cny_billion(market, market_cap)
    ok = cap_cny is not None and cap_cny >= floor_cny_billion
    return {
        "market_cap_cny_billion": round(cap_cny, 4) if cap_cny is not None else None,
        "market_cap_ok": ok,
        "market_cap_floor_cny_billion": floor_cny_billion,
    }


def verify_company_concept(item: dict[str, Any], *, evidence_date: str | None = None) -> dict[str, Any]:
    """Deterministic concept verification record for final-candidate gating.

    This is intentionally not a scoring model. Local sector tags are treated as
    hints and converted into auditable fields; a web/cache verifier can replace
    the source fields later without changing downstream gates.
    """
    evidence_date = evidence_date or today_cst()
    text = " ".join(
        str(item.get(key, "") or "")
        for key in ("company_name", "sector", "sector_tags", "chain_group")
    )
    strong_hit = next((kw for kw in AI_CORE_KEYWORDS if kw.lower() in text.lower()), "")
    weak_hit = next((kw for kw in WEAK_OR_ADJACENT_KEYWORDS if kw.lower() in text.lower()), "")
    pseudo_hit = next((kw for kw in PSEUDO_AI_KEYWORDS if kw.lower() in text.lower()), "")

    if strong_hit and not pseudo_hit:
        status = "verified"
        ai_relationship = "核心/直接 AI"
        ai_relevance = "core_ai"
        confidence = 0.78
        verified = True
        concept = strong_hit
    elif weak_hit:
        status = "weak_ai"
        ai_relationship = "弱相关/上游边缘"
        ai_relevance = "adjacent_or_weak"
        confidence = 0.52
        verified = False
        concept = weak_hit
    elif pseudo_hit:
        status = "pseudo_ai"
        ai_relationship = "伪 AI/题材相关"
        ai_relevance = "pseudo_ai"
        confidence = 0.35
        verified = False
        concept = pseudo_hit
    else:
        status = "unverified"
        ai_relationship = "未核验到明确 AI 主业"
        ai_relevance = "unknown"
        confidence = 0.25
        verified = False
        concept = str(item.get("sector") or item.get("chain_group") or "未核验")

    if "风华高科" in text or "MLCC" in text.upper():
        status = "weak_ai"
        ai_relationship = "MLCC/电子元件，非核心 AI"
        ai_relevance = "adjacent_or_weak"
        confidence = min(confidence, 0.5)
        verified = False
        concept = "MLCC/被动元件"

    return {
        "company_concept": concept,
        "concept_verified": verified,
        "concept_status": status,
        "concept_source": "local_universe_tags",
        "concept_source_url": None,
        "concept_evidence_date": evidence_date,
        "concept_confidence": round(confidence, 2),
        "ai_relationship": ai_relationship,
        "ai_relevance": ai_relevance,
    }


def _concept_cache_key(item: dict[str, Any]) -> str:
    return f"{item.get('market','')}:{item.get('symbol','')}"


def _load_cache(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _save_cache(path: Path, cache: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, ensure_ascii=False, indent=2, sort_keys=True))


def verify_company_concept_cached(
    item: dict[str, Any],
    *,
    cache_path: Path,
    evidence_date: str | None = None,
    timeout: float = 3.0,
) -> dict[str, Any]:
    """Best-effort online concept verification for final candidates only."""
    evidence_date = evidence_date or today_cst()
    key = _concept_cache_key(item)
    cache = _load_cache(cache_path)
    cached = cache.get(key)
    if isinstance(cached, dict) and cached.get("concept_evidence_date") == evidence_date:
        return cached

    local = verify_company_concept(item, evidence_date=evidence_date)
    name = str(item.get("company_name") or item.get("symbol") or "").strip()
    if not name:
        return local

    query = quote_plus(f"{name} 主营业务 AI 概念")
    url = f"https://duckduckgo.com/html/?q={query}"
    try:
        request = Request(url, headers={"User-Agent": "ai-rotator-concept-verifier/1.0"})
        with urlopen(request, timeout=timeout) as response:
            text = response.read(120000).decode("utf-8", errors="ignore")
    except Exception:
        cache[key] = {**local, "concept_source": "local_universe_tags", "concept_source_url": None}
        _save_cache(cache_path, cache)
        return cache[key]

    probe = {
        **item,
        "sector_tags": " ".join([str(item.get("sector_tags", "")), text]),
    }
    verified = verify_company_concept(probe, evidence_date=evidence_date)
    if verified["concept_status"] == "unverified" and local["concept_status"] != "unverified":
        verified = local
    verified = {
        **verified,
        "concept_source": "duckduckgo_html",
        "concept_source_url": url,
        "concept_evidence_date": evidence_date,
    }
    cache[key] = verified
    _save_cache(cache_path, cache)
    return verified
