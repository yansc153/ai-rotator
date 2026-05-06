from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from .common import load_research_json


def _market_scope(market: str) -> set[str]:
    market = market.upper()
    if market == "AH":
        return {"CN", "HK"}
    if market == "US":
        return {"US"}
    return {"CN", "HK", "US"}


def _leaders_from_pool(day_active: list[dict[str, Any]], allowed: set[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in day_active:
        if row["market"] in allowed:
            grouped[(row["market"], row["sector"])].append(row)

    leaders = []
    for (market, sector), rows in grouped.items():
        score = sum(item["ret_5d"] * 0.6 + item["ret_20d"] * 0.4 + item["atr_pct"] for item in rows) / len(rows)
        leaders.append({
            "market": market,
            "sector": sector,
            "score": round(score, 6),
            "narrative": f"{sector} shows the strongest composite momentum in {market}.",
        })
    leaders.sort(key=lambda row: row["score"], reverse=True)
    return leaders[:3], list(reversed(leaders[-3:]))


def _load_cross_market_signals(allowed: set[str]) -> list[dict[str, Any]]:
    rows = load_research_json("r1_lead_lag.json") or []
    signals = []
    for row in rows:
        peer_market = row.get("peer_market")
        if peer_market not in allowed:
            continue
        signals.append({
            "sector": row.get("peer_name", row.get("peer_symbol")),
            "market": "CROSS",
            "score": row.get("corr", 0.0),
            "best_lag": row.get("best_lag"),
            "correlation": row.get("corr"),
            "verified_event": bool(row.get("stable")),
            "narrative": f"{row.get('us_symbol')} -> {row.get('peer_symbol')} lag {row.get('best_lag')} corr {row.get('corr'):.3f}",
        })
    return signals[:8]


def _load_transmission_events() -> list[dict[str, Any]]:
    rows = load_research_json("r3_event_study.json") or []
    return [
        {
            "rule_id": row["rule"],
            "event_label": row["event_label"],
            "aligned_trade_date": row["aligned_trade_date"],
            "verified": row["verified"],
            "half_life_days": row["half_life_days"],
            "peak_car": row["peak_car"],
        }
        for row in rows
    ]


def _build_prompt(
    leaders: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> str:
    sector_lines = "\n".join(
        f"- {r['sector']} ({r['market']}): momentum_score={r['score']:.4f}"
        for r in leaders
    ) or "无领涨板块数据"
    signal_lines = "\n".join(f"- {s['narrative']}" for s in signals[:3]) or "无跨市场信号数据"
    # Short-term candidates (non-ambush), top 8
    short_cands = [c for c in candidates if c.get("pool") != "ambush"][:8]
    # Ambush/swing candidates explicitly added so LLM generates theses for them too
    ambush_cands = [c for c in candidates if c.get("pool") == "ambush"][:5]
    all_cands = short_cands + ambush_cands

    def _fmt(c: dict[str, Any]) -> str:
        tag = "【中线左侧】" if c.get("pool") == "ambush" else "【短线】"
        return (
            f"- {tag}{c['symbol']} {c['company_name']} [{c['market']}·{c['sector']}] "
            f"5日涨跌={c['ret_5d']:.1%} ATR={c['atr_pct']:.1%} "
            f"评分={c.get('rotation_score', c['priority_score']):.1f}"
        )

    cand_lines = "\n".join(_fmt(c) for c in all_cands)

    # Pre-populate JSON keys so LLM only fills values, not keys
    sector_template = "\n".join(f'    "{r["sector"]}": "___"' for r in leaders)
    stock_template = "\n".join(f'    "{c["symbol"]}": "___"' for c in all_cands)

    return f"""你是AI赛道股票轮动分析师，每日为量化系统生成中文叙述。根据以下量化数据生成分析。

## 今日领涨板块（动量评分降序）
{sector_lines}

## 跨市场传导信号（历史研究数据）
{signal_lines}

## 今日候选标的（【短线】=日内动量，【中线左侧】=左侧布局）
{cand_lines}

将下面JSON中所有"___"替换为对应的中文分析，保持JSON结构不变，无任何额外文字：
{{
  "sector_narratives": {{
{sector_template}
  }},
  "cross_signal_narrative": "___",
  "stock_theses": {{
{stock_template}
  }}
}}"""


def _parse_llm_json(raw: str) -> dict[str, Any]:
    raw = raw.strip()
    if "```" in raw:
        for part in raw.split("```"):
            part = part.strip().lstrip("json").strip()
            try:
                return json.loads(part)
            except json.JSONDecodeError:
                continue
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def _generate_llm_narratives(
    leaders: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Call local Claude CLI (claude -p) to generate Chinese-language narratives. Returns {} on failure."""
    import shutil
    import subprocess

    prompt = _build_prompt(leaders, signals, candidates)

    if shutil.which("claude") is None:
        print("[WARN] claude CLI not found — skipping LLM narratives")
        return {}

    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=90,
        )
        if result.returncode != 0:
            print(f"[WARN] claude CLI exited {result.returncode}: {result.stderr[:200]}")
            return {}
        parsed = _parse_llm_json(result.stdout)
        print("[INFO] LLM narratives generated via claude CLI")
        return parsed
    except subprocess.TimeoutExpired:
        print("[WARN] claude CLI timed out — skipping LLM narratives")
        return {}
    except Exception as exc:
        print(f"[WARN] LLM narrative generation failed: {exc}")
        return {}


def _apply_narratives(
    leaders: list[dict[str, Any]],
    signals: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    narratives: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    sector_narr: dict[str, str] = narratives.get("sector_narratives", {})
    cross_narr: str = narratives.get("cross_signal_narrative", "")
    stock_theses: dict[str, str] = narratives.get("stock_theses", {})

    enriched_leaders = [
        {**row, "narrative": sector_narr.get(row["sector"], row["narrative"])}
        for row in leaders
    ]
    enriched_signals = list(signals)
    if cross_narr and enriched_signals:
        enriched_signals[0] = {**enriched_signals[0], "narrative": cross_narr}

    enriched_candidates = [
        {**c, "llm_thesis": stock_theses.get(c["symbol"], "") or c.get("llm_thesis", "")}
        for c in candidates
    ]
    return enriched_leaders, enriched_signals, enriched_candidates


def create_sector_rotation_agent(llm: Any = None):
    del llm

    def node(state: dict[str, Any]) -> dict[str, Any]:
        market = state.get("market", "ALL")
        allowed = _market_scope(market)
        pools = state.get("universe_pools", {})
        day_active = pools.get("day_active", [])
        ambush = pools.get("ambush", [])
        watch = pools.get("watch", [])

        leaders, fading = _leaders_from_pool(day_active, allowed)
        leader_sectors = {row["sector"] for row in leaders}

        candidate_set = [
            {
                **row,
                "rotation_score": round(
                    row["priority_score"] + (25 if row["sector"] in leader_sectors else 0), 6
                ),
            }
            for row in (day_active + ambush + watch)
            if row["market"] in allowed
        ]
        candidate_set.sort(key=lambda row: row["rotation_score"], reverse=True)

        signals = _load_cross_market_signals(allowed)

        # LLM narrative enrichment — enhances narratives, degrades gracefully if unavailable
        narratives = _generate_llm_narratives(leaders, signals, candidate_set)
        if narratives:
            leaders, signals, candidate_set = _apply_narratives(leaders, signals, candidate_set, narratives)

        return {
            "leading_sectors_today": leaders,
            "fading_sectors_today": fading,
            "cross_market_signals": signals,
            "transmission_events": _load_transmission_events(),
            "candidate_set": candidate_set,
        }

    return node
