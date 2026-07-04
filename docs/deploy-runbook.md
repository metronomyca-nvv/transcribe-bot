# Transcribe Bot Deploy Runbook

## Repo and runtime

- Repo path on VPS: `/root/transcribe-bot`
- Git remote: `git@github-transcribe-bot:metronomyca-nvv/transcribe-bot.git`
- Main service container: `transcribe-bot`
- Runtime state directory: `data/`
- Temporary files directory: `tmp/`

## Update credentials

Telegram bot token lives in `.env`:

```env
TELEGRAM_BOT_TOKEN=...
```

After changing `.env`, restart the service:

```bash
cd /root/transcribe-bot
docker compose up -d
```

Check logs after restart:

```bash
docker logs --tail 100 transcribe-bot
```

Expected healthy startup lines include:

- `Bot started`
- `Start polling`
- `Run polling for bot ...`

## Pull latest branch changes

```bash
cd /root/transcribe-bot
git status -sb
git pull --ff-only
```

If working on a specific branch:

```bash
cd /root/transcribe-bot
git switch codex/yandex-messenger-runtime
git pull --ff-only
```

## Push changes from VPS

```bash
cd /root/transcribe-bot
git status -sb
git add <files>
git commit -m "<message>"
git push
```

## Restart and verify

```bash
cd /root/transcribe-bot
docker compose up -d
docker ps --filter name=transcribe-bot
docker logs --tail 100 transcribe-bot
```

## Current repo hygiene rules

- `data/` is runtime state and should stay out of git.
- `.env` must stay out of git.
- Token rotation or production config changes should be followed by a restart and log check.

## Recovery checks

Quick checks when something looks wrong:

```bash
cd /root/transcribe-bot
git status -sb
docker ps --filter name=transcribe-bot
docker logs --tail 100 transcribe-bot
```

If the bot fails after a config change, verify `.env` first, then confirm the active branch and last commit:

```bash
cd /root/transcribe-bot
git branch --show-current
git log --oneline -1
grep '^TELEGRAM_BOT_TOKEN=' .env
```
