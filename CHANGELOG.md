# Changelog

All notable changes to this project are documented in this file.
Format loosely follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.1.0] - 2026-08-12

Initial public release.

### Added
- HTTP endpoint checks (status code, timeout, TLS verification toggle per check)
- LUKS-encrypted drive mount checks
- MX DNS record checks
- Borg backup freshness check (last-archive age, no full `borg check`)
- HTML + plain-text summary email per run
- Configuration split into credentials (env vars or INI file) and checks (YAML)
- `--dry-run` mode for testing without sending mail
- Test suite and CI (GitHub Actions: ruff + pytest across Python 3.10–3.13)
