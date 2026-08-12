# Security Policy

## Reporting a vulnerability

Please report security issues privately via
[GitHub Security Advisories](https://github.com/LeonardSchwier/heartbeat/security/advisories/new)
rather than opening a public issue. If that's not workable for you, open a
regular issue asking for a private channel and avoid including exploit
details in it.

Please include:

- The version / commit affected
- Steps to reproduce
- What impact you think it has

I'll do my best to respond within a few days.

## Scope notes

heartbeat.py runs unattended, typically as root or with access to SSH keys,
LUKS device state, and SMTP/Borg credentials. Things that matter here in
particular:

- **Credentials**: read from environment variables or a local file you
  control (`heartbeat.conf`); never logged, never sent anywhere but your
  configured SMTP server.
- **TLS verification**: on by default for every HTTP check; only disabled
  per-check when you explicitly set `verify_ssl: false` in `checks.yml`,
  intended for internal endpoints with self-signed certs you already trust.
- **Subprocess calls**: `borg` is invoked with a fixed, non-shell argument
  list (no shell interpolation of config values).

If you find a case where any of that doesn't hold, that's a security bug,
please report it as above.
