# AI-Rotator VPS Deploy

## Target shape

- Linux VPS host owns the schedule through `systemd timers`
- Runtime executes inside Docker through `docker compose run`
- Runtime state persists on the host under `data/`, `storage/`, `reports/`, `logs/`
- LLM enrichment uses API providers, not a local CLI session

## 1. Pull and build

```bash
git pull
docker compose build ai-rotator
```

## 2. Prepare environment

Copy `.env.example` to `.env`, then set:

- `DISCORD_BOT_TOKEN`
- `DISCORD_CHANNEL_ID`
- one of:
  - `OPENAI_API_KEY`
  - `ANTHROPIC_API_KEY`
  - `DEEPSEEK_API_KEY`
  - `GLM_API_KEY`

Optional overrides:

- `AI_ROTATOR_LLM_PROFILE=openai|default|quality|budget`
- `AI_ROTATOR_LLM_PROVIDER=...`
- `AI_ROTATOR_LLM_MODEL=...`
- `AI_ROTATOR_DB_PATH=storage/ai_rotator.db`

## 3. Set the VPS timezone

`systemd` timers follow the host timezone. For the current session schedule, set:

```bash
sudo timedatectl set-timezone Asia/Shanghai
timedatectl
```

## 4. Verify one manual run in Docker

```bash
docker compose run --rm ai-rotator morning
docker compose run --rm ai-rotator ah_open
docker compose run --rm ai-rotator midday
docker compose run --rm ai-rotator evening
```

## 5. Install timers

```bash
sudo bash deploy/systemd/install.sh
systemctl list-timers 'ai-rotator-*'
```

## 6. Useful commands

```bash
systemctl start ai-rotator@morning.service
systemctl status ai-rotator@midday.service
journalctl -u ai-rotator@evening.service -n 200 --no-pager
docker compose run --rm ai-rotator morning
sudo bash deploy/systemd/install.sh uninstall
```

## Session mapping

- `morning` → 06:24 Mon-Fri
- `ah_open` → 08:45 Mon-Fri
- `midday` → 12:30 Mon-Fri
- `evening` → 20:30 Mon-Fri
