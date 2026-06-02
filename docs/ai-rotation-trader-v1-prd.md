# AI Rotation Trader v1 PRD

## Goal

Build a Discord-first AI sector rotation trading radar for short-term users who want a small, high-volatility watchlist without manually understanding the full US/HK/CN AI universe.

The product is not a standalone indicator picker. It remains:

1. Candidate pool from AI sector rotation.
2. Three locks as technical confirmation, weighting, downgrade, and explanation.
3. Execution filter as the final guardrail.
4. Discord brief as the user-facing decision map.
5. Signal ledger as the proof layer from push price to later prices.

## User Outcome

Every pushed ticker should answer five questions:

1. Why did this ticker appear?
2. Can I act now?
3. If not now, what am I waiting for?
4. When is the idea invalid?
5. Is this a long opportunity or a failed-overheat short opportunity?

## Trading Playbooks

The system uses rules-driven admission, not fixed counts. Some days may show 3 names, some days may show 10 or more, depending on quality.

- `premarket_open_sell`: 主多 / 开盘强承接. Three locks are valid, the AI sector is active, and the name has enough strength to justify premarket/open focus.
- `intraday_dip_reversal`: 主多 / 回踩承接. This is still a long setup, but it is not a chase setup; three locks are valid, support exists, and the trade waits for a pullback near support/VWAP/opening range.
- `overheat_failure_short`: 反手空 / 过热失败. Mostly empty by design; only appears when a hot name begins to fail structurally.
- `radar_watch`: 高波动雷达. The structure is interesting but not a clean action signal because it is too volatile, too far from support, or lacks trigger clarity.
- `danger_pool`: 禁区池. Looks tempting but fails execution/liquidity/freshness/risk constraints.

Ranking favors highest probability and clearest execution first, not the largest story or widest potential move.

## Signal Ledger

Every real Discord push writes visible trade/radar signals to `signal_ledger`.

Primary performance metric:

- Return is calculated directly from pushed price to current price.
- For long signals this is also the trade return.
- For short signals the raw price return is still shown, while directional trade return is inverted internally.

Stored fields include session, playbook, side, push price, score, three-lock status/score, support, pressure, and the original signal payload.

## Review Blocks

Daily brief:

- Shows a short near-3-day review.
- Includes signal count, priced count, win rate, average raw return, best performers, and weakest names.

Weekly brief:

- Appears inside the existing Friday evening push.
- Does not add a new timer or extra Discord push.
- Uses the same ledger and price-cache outcome math, so the weekly chart/table can be built later from the database.

## VPS Readiness

The deployment path remains Docker + systemd timers:

- `morning`: Mon-Fri 06:24
- `ah_open`: Mon-Fri 08:45
- `midday`: Mon-Fri 12:30
- `evening`: Mon-Fri 20:30

Timers must keep `Persistent=true` so missed runs after VPS sleep/reboot are caught up by systemd. The service should depend on Docker and network readiness.

## Non-goals

- Do not remove three locks.
- Do not require DeepSeek or any LLM for core scoring or execution.
- Do not add fixed quotas such as exactly three main names.
- Do not commit Discord bot tokens or API keys to GitHub.
- Do not add new Discord push times for weekly review in v1.
