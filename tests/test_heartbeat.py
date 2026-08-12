import json
from unittest.mock import MagicMock, mock_open, patch

import pytest

import heartbeat

# ── _humanize_age ────────────────────────────────────────────────

@pytest.mark.parametrize("hours,expected", [
    (0.5, "30m"),
    (5.25, "5.2h"),
    (72, "3.0d"),
])
def test_humanize_age(hours, expected):
    assert heartbeat._humanize_age(hours) == expected


# ── check_http ───────────────────────────────────────────────────

def test_check_http_ok():
    entry = {"name": "Example", "url": "https://example.com"}
    resp = MagicMock(status_code=200)
    with patch.object(heartbeat.requests, "get", return_value=resp) as get:
        result = heartbeat.check_http(entry)
    get.assert_called_once()
    assert result["ok"] is True
    assert result["status_code"] == 200


def test_check_http_non_200():
    entry = {"name": "Example", "url": "https://example.com"}
    resp = MagicMock(status_code=503)
    with patch.object(heartbeat.requests, "get", return_value=resp):
        result = heartbeat.check_http(entry)
    assert result["ok"] is False
    assert result["status_code"] == 503


def test_check_http_timeout():
    entry = {"name": "Example", "url": "https://example.com"}
    with patch.object(heartbeat.requests, "get", side_effect=heartbeat.requests.exceptions.Timeout):
        result = heartbeat.check_http(entry)
    assert result["ok"] is False
    assert result["detail"] == "Timeout"


def test_check_http_default_verify_true():
    entry = {"name": "Example", "url": "https://example.com"}
    resp = MagicMock(status_code=200)
    with patch.object(heartbeat.requests, "get", return_value=resp) as get:
        heartbeat.check_http(entry)
    assert get.call_args.kwargs["verify"] is True


def test_check_http_verify_ssl_false():
    entry = {"name": "Example", "url": "https://example.com", "verify_ssl": False}
    resp = MagicMock(status_code=200)
    with patch.object(heartbeat.requests, "get", return_value=resp) as get:
        heartbeat.check_http(entry)
    assert get.call_args.kwargs["verify"] is False


# ── check_drive ──────────────────────────────────────────────────

def test_check_drive_mounted_and_unlocked(tmp_path):
    entry = {"name": "Data", "mount_point": "/mnt/data", "luks_device": "data-crypt"}
    fake_mounts = "/dev/mapper/data-crypt /mnt/data ext4 rw 0 0\n"
    with patch("builtins.open", mock_open(read_data=fake_mounts)), \
         patch.object(heartbeat.os.path, "exists", return_value=True):
        result = heartbeat.check_drive(entry)
    assert result["ok"] is True
    assert result["mounted"] is True
    assert result["luks_open"] is True


def test_check_drive_not_mounted():
    entry = {"name": "Data", "mount_point": "/mnt/data", "luks_device": "data-crypt"}
    with patch("builtins.open", mock_open(read_data="")):
        result = heartbeat.check_drive(entry)
    assert result["ok"] is False
    assert result["mounted"] is False


def test_check_drive_mounted_but_luks_closed():
    entry = {"name": "Data", "mount_point": "/mnt/data", "luks_device": "data-crypt"}
    fake_mounts = "/dev/mapper/data-crypt /mnt/data ext4 rw 0 0\n"
    with patch("builtins.open", mock_open(read_data=fake_mounts)), \
         patch.object(heartbeat.os.path, "exists", return_value=False):
        result = heartbeat.check_drive(entry)
    assert result["ok"] is False
    assert result["luks_open"] is False


# ── check_mx ─────────────────────────────────────────────────────

def _mx_record(host):
    r = MagicMock()
    r.exchange = host
    return r


def test_check_mx_matches_expected():
    entry = {"domain": "example.com", "expected_mx": ["mx1.mail-provider.example."]}
    with patch.object(heartbeat.dns.resolver, "resolve",
                       return_value=[_mx_record("mx1.mail-provider.example.")]):
        result = heartbeat.check_mx(entry)
    assert result["ok"] is True
    assert result["missing"] == []


def test_check_mx_missing_record():
    entry = {"domain": "example.com", "expected_mx": ["mx1.mail-provider.example."]}
    with patch.object(heartbeat.dns.resolver, "resolve",
                       return_value=[_mx_record("unexpected-mx.example.")]):
        result = heartbeat.check_mx(entry)
    assert result["ok"] is False
    assert "mx1.mail-provider.example" in result["missing"]


def test_check_mx_nxdomain():
    entry = {"domain": "does-not-exist.example", "expected_mx": ["mx1.mail-provider.example."]}
    with patch.object(heartbeat.dns.resolver, "resolve",
                       side_effect=heartbeat.dns.resolver.NXDOMAIN):
        result = heartbeat.check_mx(entry)
    assert result["ok"] is False
    assert result["detail"] == "Domain not found (NXDOMAIN)"


# ── check_borg ───────────────────────────────────────────────────

def _borg_cfg(**overrides):
    cfg = {
        "name": "Offsite Backup",
        "repo": "ssh://user@backup-host.example:23/./backups/repo-name",
        "stale_after_hours": 24,
        "borg_timeout_list": 60,
        "borg_binary": "borg",
    }
    cfg.update(overrides)
    return cfg


def test_check_borg_fresh_archive():
    now = heartbeat.datetime.now(heartbeat.timezone.utc)
    stdout = json.dumps({"archives": [{"name": "2026-08-12", "time": now.isoformat()}]})
    proc = MagicMock(returncode=0, stdout=stdout, stderr="")
    with patch.object(heartbeat.subprocess, "run", return_value=proc):
        result = heartbeat.check_borg(_borg_cfg(), borg_passphrase="secret")
    assert result["ok"] is True
    assert result["last_archive"] == "2026-08-12"


def test_check_borg_stale_archive():
    from datetime import timedelta
    old = heartbeat.datetime.now(heartbeat.timezone.utc) - timedelta(hours=48)
    stdout = json.dumps({"archives": [{"name": "old-one", "time": old.isoformat()}]})
    proc = MagicMock(returncode=0, stdout=stdout, stderr="")
    with patch.object(heartbeat.subprocess, "run", return_value=proc):
        result = heartbeat.check_borg(_borg_cfg(stale_after_hours=24), borg_passphrase="secret")
    assert result["ok"] is False
    assert "STALE" in result["detail"]


def test_check_borg_no_passphrase():
    cfg = _borg_cfg(passphrase_file="/nonexistent/path")
    result = heartbeat.check_borg(cfg, borg_passphrase=None)
    assert result["ok"] is False
    assert "passphrase" in result["detail"].lower()


def test_check_borg_command_fails():
    proc = MagicMock(returncode=2, stdout="", stderr="connection refused")
    with patch.object(heartbeat.subprocess, "run", return_value=proc):
        result = heartbeat.check_borg(_borg_cfg(), borg_passphrase="secret")
    assert result["ok"] is False
    assert "borg list failed" in result["detail"]


# ── build_email ──────────────────────────────────────────────────

def test_build_email_all_ok():
    results = [{"name": "Example", "ok": True, "detail": "HTTP 200", "type": "http"}]
    subject, plain, html = heartbeat.build_email("test-host", results, [], [], [])
    assert "ALL SYSTEMS OPERATIONAL" in plain
    assert "✅" in subject
    assert "<html>" in html


def test_build_email_with_failure():
    results = [{"name": "Example", "ok": False, "detail": "Timeout", "type": "http"}]
    subject, plain, html = heartbeat.build_email("test-host", results, [], [], [])
    assert "1 CHECK(S) FAILED" in plain
    assert "❌" in subject


# ── load_checks / load_credentials ──────────────────────────────

def test_load_checks_missing_file_exits(tmp_path):
    with pytest.raises(SystemExit):
        heartbeat.load_checks(str(tmp_path / "does-not-exist.yml"))


def test_load_checks_fills_defaults(tmp_path):
    checks_file = tmp_path / "checks.yml"
    checks_file.write_text("http_checks:\n  - name: Example\n    url: https://example.com\n")
    checks = heartbeat.load_checks(str(checks_file))
    assert checks["drive_checks"] == []
    assert checks["mx_checks"] == []
    assert checks["borg_check"] == {"enabled": False}


def test_load_credentials_from_env(monkeypatch):
    monkeypatch.setenv("HEARTBEAT_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("HEARTBEAT_SMTP_USER", "user")
    monkeypatch.setenv("HEARTBEAT_SMTP_PASSWORD", "pw")
    monkeypatch.setenv("HEARTBEAT_SMTP_FROM", "from@example.com")
    monkeypatch.setenv("HEARTBEAT_SMTP_TO", "a@example.com, b@example.com")
    creds = heartbeat.load_credentials("/nonexistent/path")
    assert creds["smtp_host"] == "smtp.example.com"
    assert creds["smtp_to"] == ["a@example.com", "b@example.com"]


def test_load_credentials_missing_everything_exits(monkeypatch, tmp_path):
    for var in ("HEARTBEAT_SMTP_HOST", "HEARTBEAT_SMTP_USER", "HEARTBEAT_SMTP_PASSWORD",
                "HEARTBEAT_SMTP_FROM", "HEARTBEAT_SMTP_TO"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(SystemExit):
        heartbeat.load_credentials(str(tmp_path / "does-not-exist.conf"))
