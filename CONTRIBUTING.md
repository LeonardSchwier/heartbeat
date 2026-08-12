# Contributing

Thanks for considering a contribution. heartbeat is deliberately small - the
goal is to keep it that way while making it more correct and more useful.

## Development setup

```bash
git clone https://github.com/LeonardSchwier/heartbeat.git
cd heartbeat
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

## Running tests and lint

```bash
pytest -v
ruff check .
```

Both run in CI on every PR; please make sure they pass locally first.

## Trying it out locally

```bash
cp checks.example.yml checks.yml     # edit with your own (or fake) checks
export HEARTBEAT_SMTP_HOST=smtp.example.com
export HEARTBEAT_SMTP_USER=you@example.com
export HEARTBEAT_SMTP_PASSWORD=...
export HEARTBEAT_SMTP_FROM=you@example.com
export HEARTBEAT_SMTP_TO=you@example.com
python3 heartbeat.py --checks checks.yml --dry-run
```

`--dry-run` runs every check, prints the plain-text report, writes
`heartbeat_preview.html`, and never sends mail or touches credentials it
doesn't need - useful for iterating without an SMTP account at hand.

## What's in scope

- New check types that follow the existing pattern: read config, return a
  `dict` with `ok`/`name`/`detail`, log one line, don't raise.
- Bug fixes, clearer error messages, docs improvements.
- Packaging / deployment ergonomics (systemd units, container image, etc.)
  as long as they stay optional and don't add a hard dependency for people
  running this from cron.

## What's likely out of scope

- A web UI or persistent daemon. If you want that, Uptime Kuma already
  does it well - heartbeat's whole reason to exist is to stay a script
  you can read in five minutes.
- Notification channels beyond email, unless they're genuinely trivial to
  keep optional (e.g. behind a config flag with no new hard dependency).

If you're unsure whether something fits, open an issue to discuss before
sending a large PR.

## Pull requests

- Keep changes focused; unrelated cleanup makes review harder.
- Add or update tests for behavior changes.
- Update `checks.example.yml` / `heartbeat.conf.example` / README if you
  change configuration shape.
