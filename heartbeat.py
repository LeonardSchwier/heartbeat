#!/usr/bin/env python3
"""
heartbeat.py - checks the outages nobody notices.

Not an uptime monitor. Uptime Kuma already does that, better, with a UI.
This checks the failure modes that stay green in every dashboard:
  - HTTP endpoints returning non-200 (the boring baseline)
  - LUKS-encrypted volumes silently unmounted or locked
  - MX DNS records drifting from what you expect (if this breaks, your
    whole alerting path dies *silently* - every monitor keeps reporting
    green because none of them checks whether mail can still reach you)
  - Borg backup archives going stale

Sends one summary email per run, HTML + plain text, emoji status in the
subject line so you can triage from a phone's notification preview.

Configuration is split in two, both external to this script:
  - Credentials (SMTP, Borg passphrase): environment variables, or an
    INI file as a fallback. See README.md / heartbeat.conf.example.
  - Checks (endpoints, drives, MX records, backup repo): a YAML file.
    See checks.example.yml.

    HEARTBEAT_CHECKS=/etc/heartbeat/checks.yml   (default)
    HEARTBEAT_CONF=/etc/heartbeat/heartbeat.conf (fallback for credentials)
    HEARTBEAT_LOG=/var/log/heartbeat.log         (default)
"""

import argparse
import configparser
import json
import logging
import os
import smtplib
import socket
import subprocess
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import dns.resolver
import requests
import urllib3
import yaml

__version__ = "0.1.0"

DEFAULT_CHECKS_FILE = os.environ.get("HEARTBEAT_CHECKS", "/etc/heartbeat/checks.yml")
DEFAULT_CREDENTIALS_FILE = os.environ.get("HEARTBEAT_CONF", "/etc/heartbeat/heartbeat.conf")
DEFAULT_LOG_FILE = os.environ.get("HEARTBEAT_LOG", "/var/log/heartbeat.log")

# ─────────────────────────────────────────────
# CREDENTIALS - environment variables first, INI file as fallback.
# Never hardcoded, never checked in. See heartbeat.conf.example.
# ─────────────────────────────────────────────

ENV_PREFIX = "HEARTBEAT_"


def _env(*names):
    for name in names:
        value = os.environ.get(ENV_PREFIX + name)
        if value:
            return value
    return None


def load_credentials(path: str) -> dict:
    """Loads SMTP + Borg credentials. Env vars win; falls back to an INI file.

    Env vars: HEARTBEAT_SMTP_HOST, HEARTBEAT_SMTP_PORT, HEARTBEAT_SMTP_USER,
    HEARTBEAT_SMTP_PASSWORD, HEARTBEAT_SMTP_FROM, HEARTBEAT_SMTP_TO
    (comma-separated), HEARTBEAT_BORG_PASSPHRASE.
    """
    host = _env("SMTP_HOST")
    if host:
        to_raw = _env("SMTP_TO") or ""
        creds = {
            "smtp_host": host,
            "smtp_port": int(_env("SMTP_PORT") or 587),
            "smtp_user": _env("SMTP_USER") or "",
            "smtp_password": _env("SMTP_PASSWORD") or "",
            "smtp_from": _env("SMTP_FROM") or "",
            "smtp_to": [a.strip() for a in to_raw.split(",") if a.strip()],
            "borg_passphrase": _env("BORG_PASSPHRASE"),
        }
        missing = [k for k in ("smtp_user", "smtp_password", "smtp_from") if not creds[k]]
        if not creds["smtp_to"]:
            missing.append("smtp_to")
        if missing:
            print(f"ERROR: missing required env vars for: {', '.join(missing)}", file=sys.stderr)
            sys.exit(1)
        return creds

    cfg = configparser.RawConfigParser()
    if not cfg.read(path):
        print(
            f"ERROR: no credentials found.\n"
            f"  Set HEARTBEAT_SMTP_HOST / _USER / _PASSWORD / _FROM / _TO env vars, or\n"
            f"  create {path} (see heartbeat.conf.example).",
            file=sys.stderr,
        )
        sys.exit(1)
    try:
        smtp = cfg["smtp"]
        creds = {
            "smtp_host": smtp["host"],
            "smtp_port": int(smtp.get("port", 587)),
            "smtp_user": smtp["user"],
            "smtp_password": smtp["password"],
            "smtp_from": smtp["from"],
            "smtp_to": [a.strip() for a in smtp["to"].split(",") if a.strip()],
            "borg_passphrase": None,
        }
        if "borg" in cfg:
            creds["borg_passphrase"] = cfg["borg"].get("passphrase", None)
        return creds
    except KeyError as e:
        print(f"ERROR: missing key {e} in credentials file {path}", file=sys.stderr)
        sys.exit(1)


# ─────────────────────────────────────────────
# CHECKS CONFIG - external YAML file. See checks.example.yml.
# ─────────────────────────────────────────────

def load_checks(path: str) -> dict:
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except FileNotFoundError:
        print(
            f"ERROR: checks file not found: {path}\n"
            f"  Copy checks.example.yml to {path} and adjust it, or set HEARTBEAT_CHECKS.",
            file=sys.stderr,
        )
        sys.exit(1)
    except yaml.YAMLError as e:
        print(f"ERROR: could not parse checks file {path}: {e}", file=sys.stderr)
        sys.exit(1)

    data.setdefault("http_checks", [])
    data.setdefault("drive_checks", [])
    data.setdefault("mx_checks", [])
    data.setdefault("borg_check", {"enabled": False})
    return data


# ─────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────

def setup_logging(log_file: str) -> logging.Logger:
    handlers = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(log_file))
    except OSError as e:
        print(f"WARNING: cannot write log file {log_file} ({e}); logging to stdout only", file=sys.stderr)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=handlers,
        force=True,
    )
    return logging.getLogger("heartbeat")


log = logging.getLogger("heartbeat")

# ─────────────────────────────────────────────
# CHECK FUNCTIONS
# ─────────────────────────────────────────────

def check_http(entry: dict, default_timeout: int = 10) -> dict:
    name = entry["name"]
    url = entry["url"]
    timeout = entry.get("timeout", default_timeout)
    verify_ssl = entry.get("verify_ssl", True)
    result = {"name": name, "url": url, "type": "http"}

    if not verify_ssl:
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    try:
        r = requests.get(url, timeout=timeout, allow_redirects=True, verify=verify_ssl)
        ok = r.status_code == 200
        result.update({"ok": ok, "status_code": r.status_code, "detail": f"HTTP {r.status_code}"})
    except requests.exceptions.Timeout:
        result.update({"ok": False, "status_code": None, "detail": "Timeout"})
    except requests.exceptions.ConnectionError as e:
        result.update({"ok": False, "status_code": None, "detail": f"Connection error: {e}"})
    except Exception as e:
        result.update({"ok": False, "status_code": None, "detail": str(e)})
    log.info("HTTP  %-30s %s  %s", name, "✅" if result["ok"] else "❌", result["detail"])
    return result


def check_drive(entry: dict) -> dict:
    name = entry["name"]
    mount_point = entry["mount_point"]
    luks_device = entry.get("luks_device")
    result = {"name": name, "mount_point": mount_point, "type": "drive", "luks_device": luks_device}
    mounted = False
    try:
        with open("/proc/mounts") as f:
            for line in f:
                if mount_point in line:
                    mounted = True
                    break
    except Exception as e:
        result.update({"ok": False, "detail": f"Cannot read /proc/mounts: {e}"})
        return result

    luks_open = None
    if luks_device:
        luks_open = os.path.exists(f"/dev/mapper/{luks_device}")

    ok = mounted and (luks_open if luks_device else True)
    parts = ["mounted ✅" if mounted else "NOT mounted ❌"]
    if luks_device is not None:
        parts.append("LUKS open ✅" if luks_open else "LUKS closed ❌")

    result.update({"ok": ok, "mounted": mounted, "luks_open": luks_open, "detail": ", ".join(parts)})
    log.info("DRIVE %-30s %s  %s", name, "✅" if ok else "❌", result["detail"])
    return result


def check_mx(entry: dict) -> dict:
    domain = entry["domain"]
    expected = {m.lower().rstrip(".") for m in entry["expected_mx"]}
    result = {"name": domain, "domain": domain, "type": "mx", "expected": sorted(expected)}
    try:
        answers = dns.resolver.resolve(domain, "MX")
        actual = {str(r.exchange).lower().rstrip(".") for r in answers}
        missing = expected - actual
        extra = actual - expected
        ok = len(missing) == 0
        detail = f"found: {sorted(actual)}"
        if missing:
            detail += f" | missing: {sorted(missing)}"
        if extra:
            detail += f" | unexpected: {sorted(extra)}"
        result.update({"ok": ok, "actual": sorted(actual), "missing": sorted(missing),
                        "extra": sorted(extra), "detail": detail})
    except dns.resolver.NXDOMAIN:
        result.update({"ok": False, "actual": [], "missing": sorted(expected), "extra": [],
                        "detail": "Domain not found (NXDOMAIN)"})
    except Exception as e:
        result.update({"ok": False, "actual": [], "missing": [], "extra": [], "detail": str(e)})
    log.info("MX    %-30s %s  %s", domain, "✅" if result["ok"] else "❌", result["detail"])
    return result


def _humanize_age(hours: float) -> str:
    if hours < 1:
        return f"{int(hours * 60)}m"
    if hours < 48:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def _borg_env(cfg: dict, borg_passphrase: str | None) -> dict | None:
    env = os.environ.copy()

    if borg_passphrase:
        env["BORG_PASSPHRASE"] = borg_passphrase
    else:
        passphrase_file = cfg.get("passphrase_file", "")
        try:
            with open(passphrase_file) as f:
                env["BORG_PASSPHRASE"] = f.read().strip()
        except Exception as e:
            log.error("BORG  Cannot read passphrase (set HEARTBEAT_BORG_PASSPHRASE, the "
                      "[borg] section in heartbeat.conf, or passphrase_file): %s", e)
            return None

    agent_sock = cfg.get("ssh_agent_sock") or env.get("SSH_AUTH_SOCK", "")
    if agent_sock and os.path.exists(agent_sock):
        env["SSH_AUTH_SOCK"] = agent_sock

    ssh_key = cfg.get("ssh_key", "")
    if ssh_key:
        env["BORG_RSH"] = (
            f"ssh -i {ssh_key} "
            f"-o StrictHostKeyChecking=accept-new "
            f"-o BatchMode=yes"
        )
    return env


def check_borg(cfg: dict, borg_passphrase: str | None) -> dict:
    """
    Checks the Borg repo by fetching the last archive only (borg list --last 1).
    Repo integrity (borg check) is deliberately skipped - on a multi-TB repo over
    SSH it can take hours. Schedule that separately as its own weekly cron job.
    Flags as failed if no archive exists or the newest is older than stale_after_hours.
    """
    name = cfg["name"]
    result = {"name": name, "type": "borg"}

    env = _borg_env(cfg, borg_passphrase)
    if env is None:
        result.update({"ok": False, "detail": "Cannot read passphrase (see log)",
                        "last_archive": None, "age_hours": None})
        return result

    repo = cfg["repo"]
    binary = cfg.get("borg_binary", "borg")
    timeout_list = cfg.get("borg_timeout_list", 60)

    try:
        proc = subprocess.run(
            [binary, "list", "--json", "--last", "1", repo],
            env=env, capture_output=True, text=True, timeout=timeout_list,
        )
        if proc.returncode != 0:
            last_line = (proc.stderr.strip().splitlines() or ["unknown error"])[-1]
            result.update({"ok": False, "detail": f"borg list failed: {last_line}",
                            "last_archive": None, "age_hours": None})
            log.error("BORG  %-30s ❌  %s", name, result["detail"])
            return result

        data = json.loads(proc.stdout)
        archives = data.get("archives", [])

        if not archives:
            result.update({"ok": False, "detail": "no archives found in repo",
                            "last_archive": None, "age_hours": None})
            log.warning("BORG  %-30s ❌  %s", name, result["detail"])
            return result

        archive = archives[-1]
        archive_name = archive.get("name", "unknown")
        ts_str = archive.get("time", "")
        archive_time = datetime.fromisoformat(ts_str).astimezone(timezone.utc)
        now_utc = datetime.now(timezone.utc)
        age_hours = (now_utc - archive_time).total_seconds() / 3600
        stale_limit = cfg.get("stale_after_hours", 24)
        fresh = age_hours <= stale_limit

        age_str = _humanize_age(age_hours)
        last_ts = archive_time.strftime("%Y-%m-%d %H:%M UTC")
        detail = f"last: {archive_name} · {last_ts} ({age_str} ago)"
        if not fresh:
            detail += f" ⚠️ STALE (>{stale_limit}h)"

        result.update({
            "ok": fresh,
            "last_archive": archive_name,
            "last_time": last_ts,
            "age_hours": round(age_hours, 1),
            "fresh": fresh,
            "detail": detail,
        })

    except subprocess.TimeoutExpired:
        result.update({"ok": False, "detail": f"borg list timed out (>{timeout_list}s)",
                        "last_archive": None, "age_hours": None})
        log.error("BORG  %-30s ❌  %s", name, result["detail"])
    except FileNotFoundError:
        result.update({"ok": False, "detail": f"borg binary not found: {binary}",
                        "last_archive": None, "age_hours": None})
        log.error("BORG  %-30s ❌  %s", name, result["detail"])
    except json.JSONDecodeError as e:
        result.update({"ok": False, "detail": f"failed to parse borg output: {e}",
                        "last_archive": None, "age_hours": None})
        log.error("BORG  %-30s ❌  %s", name, result["detail"])
    except Exception as e:
        result.update({"ok": False, "detail": str(e), "last_archive": None, "age_hours": None})
        log.error("BORG  %-30s ❌  %s", name, result["detail"])

    log.info("BORG  %-30s %s  %s", name, "✅" if result["ok"] else "❌", result["detail"])
    return result


# ─────────────────────────────────────────────
# EMAIL BUILDER
# ─────────────────────────────────────────────

def _row(label: str, detail: str, ok: bool) -> str:
    icon = "✅" if ok else "❌"
    bg = "#f0fdf4" if ok else "#fef2f2"
    col = "#166534" if ok else "#991b1b"
    return (
        f'<tr style="background:{bg}">'
        f'<td style="padding:8px 12px;font-size:18px">{icon}</td>'
        f'<td style="padding:8px 12px;font-weight:600;color:{col};white-space:nowrap">{label}</td>'
        f'<td style="padding:8px 12px;color:#374151;font-family:monospace;font-size:13px">{detail}</td>'
        f'</tr>'
    )


def _borg_row(r: dict) -> str:
    """Renders the Borg row with an extra sub-row showing the last archive timestamp."""
    icon = "✅" if r["ok"] else "❌"
    bg = "#f0fdf4" if r["ok"] else "#fef2f2"
    col = "#166534" if r["ok"] else "#991b1b"

    main_row = (
        f'<tr style="background:{bg}">'
        f'<td style="padding:8px 12px;font-size:18px">{icon}</td>'
        f'<td style="padding:8px 12px;font-weight:600;color:{col};white-space:nowrap">{r["name"]}</td>'
        f'<td style="padding:8px 12px;color:#374151;font-family:monospace;font-size:13px">{r["detail"]}</td>'
        f'</tr>'
    )

    sub_row = ""
    if r.get("last_archive") and r.get("last_time"):
        sub_row = (
            f'<tr style="background:#f8fafc">'
            f'<td style="padding:2px 12px 8px"></td>'
            f'<td style="padding:2px 12px 8px;font-family:monospace;font-size:12px;color:#6b7280">'
            f'📦 {r["last_archive"]}</td>'
            f'<td style="padding:2px 12px 8px;font-family:monospace;font-size:12px;color:#6b7280">'
            f'{r["last_time"]} &nbsp;·&nbsp; {_humanize_age(r["age_hours"])} ago</td>'
            f'</tr>'
        )

    return main_row + sub_row


def _section(title: str, icon: str, rows_html: str) -> str:
    if not rows_html:
        return ""
    return (
        f'<tr><td colspan="3" style="padding:16px 12px 6px;font-size:12px;'
        f'letter-spacing:.1em;text-transform:uppercase;color:#6b7280;'
        f'font-weight:700;border-top:1px solid #e5e7eb">{icon} {title}</td></tr>'
        f'{rows_html}'
    )


def build_email(host: str, http_results, drive_results, mx_results, borg_results) -> tuple[str, str, str]:
    all_results = http_results + drive_results + mx_results + borg_results
    all_ok = all(r["ok"] for r in all_results)
    fail_count = sum(1 for r in all_results if not r["ok"])

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    status_emoji = "✅" if all_ok else f"❌ {fail_count} issue{'s' if fail_count != 1 else ''}"
    subject = f"{status_emoji} | Heartbeat - {host} | {now_str}"

    # Plain text
    lines = [f"Heartbeat - {host}", f"Run at: {now_str}", ""]
    for section_title, results in [
        ("── HTTP ──────────────────────────", http_results),
        ("── DRIVES ────────────────────────", drive_results),
        ("── MX RECORDS ───────────────────", mx_results),
        ("── BORG BACKUPS ─────────────────", borg_results),
    ]:
        if not results:
            continue
        lines.append(section_title)
        for r in results:
            lines.append(f"{'✅' if r['ok'] else '❌'}  {r['name']}: {r['detail']}")
            if r.get("type") == "borg" and r.get("last_archive"):
                lines.append(f"   📦 {r['last_archive']}  ·  {r['last_time']}  ({_humanize_age(r['age_hours'])} ago)")
        lines.append("")
    lines.append("ALL SYSTEMS OPERATIONAL" if all_ok else f"⚠️  {fail_count} CHECK(S) FAILED")
    plain = "\n".join(lines)

    # HTML
    banner_bg = "#166534" if all_ok else "#991b1b"
    banner_txt = ("✅ ALL SYSTEMS OPERATIONAL" if all_ok
                  else f"❌ {fail_count} CHECK{'S' if fail_count != 1 else ''} FAILED")

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
</head>
<body style="margin:0;padding:24px;background:#f9fafb;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif">
  <table width="100%" cellpadding="0" cellspacing="0" style="max-width:680px;margin:0 auto">
    <tr><td style="background:{banner_bg};color:#fff;padding:20px 24px;border-radius:10px 10px 0 0;
        font-size:20px;font-weight:700;letter-spacing:.02em">{banner_txt}</td></tr>
    <tr><td style="background:#fff;border:1px solid #e5e7eb;border-top:none;border-radius:0 0 10px 10px;padding:8px 0 16px">
      <table width="100%" cellpadding="0" cellspacing="0">
        {_section("HTTP Endpoints", "🌐", "".join(_row(r["name"], r["detail"], r["ok"]) for r in http_results))}
        {_section("Drive Mounts & LUKS", "💾", "".join(_row(r["name"], r["detail"], r["ok"]) for r in drive_results))}
        {_section("MX DNS Records", "📬", "".join(_row(r["name"], r["detail"], r["ok"]) for r in mx_results))}
        {_section("Borg Backups", "🗄️", "".join(_borg_row(r) for r in borg_results))}
      </table>
    </td></tr>
    <tr><td style="padding:12px 0;font-size:12px;color:#9ca3af;text-align:center">
      {host} · {now_str} · heartbeat.py
    </td></tr>
  </table>
</body></html>"""

    return subject, plain, html


# ─────────────────────────────────────────────
# SMTP SENDER
# ─────────────────────────────────────────────

def send_email(creds: dict, subject: str, plain: str, html: str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = creds["smtp_from"]
    msg["To"] = ", ".join(creds["smtp_to"])
    msg.attach(MIMEText(plain, "plain"))
    msg.attach(MIMEText(html, "html"))

    log.info("Sending email: %s", subject)
    with smtplib.SMTP(creds["smtp_host"], creds["smtp_port"]) as s:
        s.ehlo()
        s.starttls()
        s.login(creds["smtp_user"], creds["smtp_password"])
        s.sendmail(creds["smtp_from"], creds["smtp_to"], msg.as_string())
    log.info("Email sent to %s", creds["smtp_to"])


# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="heartbeat.py",
        description="Checks the outages nobody notices: HTTP endpoints, LUKS drives, MX DNS, Borg backups.",
    )
    parser.add_argument("--checks", default=DEFAULT_CHECKS_FILE,
                         help=f"path to checks YAML file (default: {DEFAULT_CHECKS_FILE})")
    parser.add_argument("--conf", default=DEFAULT_CREDENTIALS_FILE,
                         help=f"path to credentials INI file, used only if HEARTBEAT_SMTP_* "
                              f"env vars are unset (default: {DEFAULT_CREDENTIALS_FILE})")
    parser.add_argument("--log-file", default=DEFAULT_LOG_FILE,
                         help=f"path to log file (default: {DEFAULT_LOG_FILE})")
    parser.add_argument("--dry-run", action="store_true",
                         help="run all checks and print the report, but don't send email")
    parser.add_argument("--version", action="version", version=f"heartbeat.py {__version__}")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)

    global log
    log = setup_logging(args.log_file)
    log.info("=== Heartbeat starting (v%s) ===", __version__)

    checks = load_checks(args.checks)
    hostname = checks.get("hostname_label") or socket.gethostname()
    request_timeout = checks.get("request_timeout", 10)

    http_results = [check_http(e, request_timeout) for e in checks["http_checks"]]
    drive_results = [check_drive(e) for e in checks["drive_checks"]]
    mx_results = [check_mx(e) for e in checks["mx_checks"]]

    borg_cfg = checks.get("borg_check", {})
    borg_results = []
    if borg_cfg.get("enabled"):
        creds_for_borg = load_credentials(args.conf) if not args.dry_run else {"borg_passphrase": None}
        borg_results = [check_borg(borg_cfg, creds_for_borg.get("borg_passphrase"))]

    subject, plain, html = build_email(hostname, http_results, drive_results, mx_results, borg_results)

    if args.dry_run:
        print(plain)
        with open("heartbeat_preview.html", "w") as f:
            f.write(html)
        log.info("Dry run: wrote heartbeat_preview.html, email not sent")
    else:
        creds = load_credentials(args.conf)
        try:
            send_email(creds, subject, plain, html)
        except Exception as e:
            log.error("Failed to send email: %s", e)
            sys.exit(1)

    all_ok = all(r["ok"] for r in http_results + drive_results + mx_results + borg_results)
    log.info("=== Done. Status: %s ===", "OK" if all_ok else "DEGRADED")
    sys.exit(0 if all_ok else 2)


if __name__ == "__main__":
    main()
