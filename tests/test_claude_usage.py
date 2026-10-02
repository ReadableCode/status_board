# %%
# Imports #

import json
import sys
from datetime import datetime, timedelta, timezone

import pytest
from utils import claude_usage_probe, statusboard_tools

# %%
# Fixtures #

NOW = datetime(2026, 10, 2, 13, 0, tzinfo=timezone.utc)

# Trimmed from a real usage-endpoint response (a max account,
# 2026-10-02): the limits array plus the legacy dicts it duplicates.
USAGE = {
    "five_hour": {"utilization": 2.0, "resets_at": "2026-10-02T16:59:59.672510+00:00"},
    "seven_day": {"utilization": 45.0, "resets_at": "2026-10-03T08:59:59.672532+00:00"},
    "iguana_necktie": {"utilization": 1.06, "resets_at": "2026-11-05T07:59:00+00:00"},
    "limits": [
        {"kind": "session", "group": "session", "percent": 2, "severity": "normal",
         "resets_at": "2026-10-02T16:59:59.672510+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_all", "group": "weekly", "percent": 45, "severity": "normal",
         "resets_at": "2026-10-03T08:59:59.672532+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 77, "severity": "warning",
         "resets_at": "2026-10-03T08:59:59.672777+00:00",
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}, "is_active": True},
    ],
    "spend": {"used": {"amount_minor": 0, "currency": "USD", "exponent": 2}, "limit": None,
              "percent": 0, "enabled": False},
}

SUBSCRIPTION_REPORT = {
    "mode": "subscription", "subscription": "max", "tier": "default_claude_max_20x",
    "usage": USAGE, "host": "homebox",
}

BEDROCK_REPORT = {
    "mode": "bedrock", "model": "us.anthropic.claude-opus-5-5", "region": "us-east-1",
    "profile": "bedrock-profile", "host": "laptop",
    "tokens": {
        "today": {"turns": 36, "input": 74, "output": 16531, "cache_read": 7213334, "cache_write": 841007},
        "week": {"turns": 1473, "input": 2988, "output": 906946, "cache_read": 365286363, "cache_write": 17060883},
    },
}

HOSTS = [{"name": "Box", "hostname": "10.0.0.5", "user": "jdoe", "os": "linux", "aliases": ["sshbox"]}]


def make_repo(tmp_path):
    repo = tmp_path / "acme_credentials"
    repo.mkdir()
    (repo / "acme_hosts.json").write_text(json.dumps({"hosts": HOSTS}))
    return str(repo)


def fake_completed(stdout, stderr="", returncode=0):
    class Completed:
        pass

    completed = Completed()
    completed.stdout, completed.stderr, completed.returncode = stdout, stderr, returncode
    return completed


def transcript_line(message_id, timestamp, output, kind="assistant"):
    return json.dumps({
        "type": kind, "timestamp": timestamp, "requestId": f"r-{message_id}",
        "message": {"id": message_id, "model": "claude-opus-5-5", "usage": {
            "input_tokens": 1, "output_tokens": output,
            "cache_read_input_tokens": 100, "cache_creation_input_tokens": 10,
        }},
    })


# %%
# Probe: configured connection #


def test_connection_mode_reads_the_bedrock_switch():
    assert claude_usage_probe.connection_mode({"CLAUDE_CODE_USE_BEDROCK": "1"}) == "bedrock"
    assert claude_usage_probe.connection_mode({"CLAUDE_CODE_USE_BEDROCK": "true"}) == "bedrock"
    # the enterprise settings variant sets it to "0" rather than dropping it
    assert claude_usage_probe.connection_mode({"CLAUDE_CODE_USE_BEDROCK": "0"}) == "subscription"
    assert claude_usage_probe.connection_mode({}) == "subscription"


def test_configured_env_reads_the_settings_env_block(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"env": {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "us-east-1"}}))
    expected = {"CLAUDE_CODE_USE_BEDROCK": "1", "AWS_REGION": "us-east-1"}
    assert claude_usage_probe.configured_env(str(settings)) == expected
    assert claude_usage_probe.configured_env(str(tmp_path / "missing.json")) == {}


# %%
# Probe: subscription #


def test_subscription_report_never_calls_out_with_an_expired_token(monkeypatch):
    expired = (NOW - timedelta(hours=1)).timestamp() * 1000
    monkeypatch.setattr(claude_usage_probe, "load_oauth", lambda: {
        "accessToken": "t", "expiresAt": expired, "subscriptionType": "max",
    })

    def no_network(*args, **kwargs):
        raise AssertionError("must not call the usage endpoint")

    monkeypatch.setattr(claude_usage_probe.urllib.request, "urlopen", no_network)
    report = claude_usage_probe.subscription_report(NOW.timestamp())
    assert report["subscription"] == "max"
    assert report["error"].startswith("access token expired")


def test_subscription_report_without_a_login(monkeypatch):
    monkeypatch.setattr(claude_usage_probe, "load_oauth", lambda: {"subscriptionType": "enterprise"})
    report = claude_usage_probe.subscription_report(NOW.timestamp())
    assert report["subscription"] == "enterprise"
    assert "no claude.ai login" in report["error"]


def test_subscription_report_returns_the_usage_response(monkeypatch):
    monkeypatch.setattr(claude_usage_probe, "load_oauth", lambda: {"accessToken": "t", "subscriptionType": "max"})
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(USAGE).encode()

    def fake_urlopen(request, timeout):
        seen["auth"] = request.get_header("Authorization")
        seen["beta"] = request.get_header("Anthropic-beta")
        return Response()

    monkeypatch.setattr(claude_usage_probe.urllib.request, "urlopen", fake_urlopen)
    report = claude_usage_probe.subscription_report(NOW.timestamp())
    assert report["usage"] == USAGE and "error" not in report
    assert seen == {"auth": "Bearer t", "beta": claude_usage_probe.OAUTH_BETA_HEADER}


# %%
# Probe: bedrock tally #


def test_tally_counts_each_message_once_per_window(tmp_path):
    day_start = datetime(2026, 10, 2, tzinfo=timezone.utc).timestamp()
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("\n".join([
        # one message, written once per content block - counts once
        transcript_line("m1", "2026-10-02T10:00:00Z", 50),
        transcript_line("m1", "2026-10-02T10:00:00Z", 50),
        # earlier this week: in the week window only
        transcript_line("m2", "2026-09-29T10:00:00Z", 7),
        # older than a week: in neither
        transcript_line("m3", "2026-09-20T10:00:00Z", 1000),
        # not an assistant turn
        transcript_line("m4", "2026-10-02T10:00:00Z", 9, kind="user"),
        "not json at all",
    ]))
    windows = claude_usage_probe.tally_transcripts([str(transcript)], NOW.timestamp(), day_start)
    assert windows["today"] == {"turns": 1, "input": 1, "output": 50, "cache_read": 100, "cache_write": 10}
    assert windows["week"] == {"turns": 2, "input": 2, "output": 57, "cache_read": 200, "cache_write": 20}


def test_bedrock_report_carries_the_configured_model(monkeypatch):
    monkeypatch.setattr(claude_usage_probe.glob, "glob", lambda *a, **k: [])
    report = claude_usage_probe.bedrock_report(
        {"ANTHROPIC_MODEL": "us.anthropic.claude-opus-5-5", "AWS_REGION": "us-east-1", "AWS_PROFILE": "p"},
        NOW.timestamp(),
    )
    assert (report["mode"], report["model"], report["region"]) == ("bedrock", "us.anthropic.claude-opus-5-5",
                                                                   "us-east-1")
    assert report["tokens"]["week"]["turns"] == 0


# %%
# Board: parsing #


def test_claude_usage_limits_reads_the_limits_array():
    rows = statusboard_tools.claude_usage_limits(USAGE)
    assert [(row["label"], row["percent"]) for row in rows] == [
        ("session", 2.0), ("weekly", 45.0), ("weekly Fable", 77.0),
    ]
    assert rows[0]["resets_at"] == datetime(2026, 10, 2, 16, 59, 59, 672510, tzinfo=timezone.utc)


def test_claude_usage_limits_falls_back_to_the_legacy_windows():
    legacy = {key: USAGE[key] for key in ("five_hour", "seven_day", "iguana_necktie")}
    rows = statusboard_tools.claude_usage_limits(legacy)
    assert [(row["label"], row["percent"]) for row in rows] == [("session", 2.0), ("weekly", 45.0)]


def test_claude_spend_only_when_enabled():
    assert statusboard_tools.claude_spend(USAGE) is None
    enabled = {"spend": {"enabled": True, "percent": 12,
                         "used": {"amount_minor": 1200, "currency": "USD", "exponent": 2},
                         "limit": {"amount_minor": 10000, "currency": "USD", "exponent": 2}}}
    assert statusboard_tools.claude_spend(enabled) == {
        "label": "credits", "percent": 12.0, "used": "12.00 USD", "limit": "100.00 USD",
    }


def test_compact_count_and_plan_label():
    assert [statusboard_tools.compact_count(v) for v in (74, 16531, 841007, 7213334, 365286363)] == [
        "74", "16.5k", "841k", "7.2M", "365M",
    ]
    assert statusboard_tools.claude_plan_label(SUBSCRIPTION_REPORT) == "max 20x"
    assert statusboard_tools.claude_plan_label({"subscription": "enterprise", "tier": "default_claude_zero"}) == (
        "enterprise"
    )


# %%
# Board: fetch #


def test_fetch_pipes_the_probe_over_ssh_and_summarizes(tmp_path, monkeypatch):
    panel = {"name": "claude", "type": "claude_usage", "host": "sshbox", "_base_dir": make_repo(tmp_path)}
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return fake_completed(json.dumps(SUBSCRIPTION_REPORT) + "\n")

    monkeypatch.setattr(statusboard_tools.subprocess, "run", fake_run)
    result = statusboard_tools.fetch_panel(panel, str(tmp_path), local_hostname="OTHERBOX")
    argv, kwargs = calls[0]
    assert argv[0] == "ssh" and argv[-2:] == ["jdoe@10.0.0.5", "python3 -"]
    with open(statusboard_tools.CLAUDE_USAGE_PROBE, encoding="utf-8") as file_handle:
        assert kwargs["input"] == file_handle.read()
    assert result.ok and result.kind == "claude_usage" and not result.alert
    assert result.summary == "max 20x · weekly Fable 77% used"


def test_fetch_without_a_host_runs_the_probe_with_this_interpreter(tmp_path, monkeypatch):
    panel = {"name": "claude", "type": "claude_usage", "_base_dir": str(tmp_path)}
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return fake_completed(json.dumps(BEDROCK_REPORT))

    monkeypatch.setattr(statusboard_tools.subprocess, "run", fake_run)
    result = statusboard_tools.fetch_panel(panel, str(tmp_path))
    assert calls == [[sys.executable, "-"]]
    assert result.ok and result.summary == "bedrock · 16.5k out today"


def test_fetch_flags_an_exhausted_limit(tmp_path, monkeypatch):
    usage = dict(USAGE, limits=[dict(USAGE["limits"][0], percent=100)])
    report = dict(SUBSCRIPTION_REPORT, usage=usage)
    monkeypatch.setattr(statusboard_tools.subprocess, "run", lambda argv, **kw: fake_completed(json.dumps(report)))
    result = statusboard_tools.fetch_panel({"name": "c", "type": "claude_usage", "_base_dir": str(tmp_path)},
                                           str(tmp_path))
    assert result.ok and result.alert


def test_fetch_surfaces_the_probe_error_with_plan_and_host(tmp_path, monkeypatch):
    report = {"mode": "subscription", "subscription": "enterprise", "tier": "default_claude_zero",
              "host": "laptop", "error": "no claude.ai login on this host - run `claude` and log in"}
    monkeypatch.setattr(statusboard_tools.subprocess, "run", lambda argv, **kw: fake_completed(json.dumps(report)))
    result = statusboard_tools.fetch_panel({"name": "c", "type": "claude_usage", "_base_dir": str(tmp_path)},
                                           str(tmp_path))
    assert not result.ok
    assert result.body == "enterprise on laptop: no claude.ai login on this host - run `claude` and log in"


def test_fetch_chain_failure_is_an_error(tmp_path, monkeypatch):
    panel = {"name": "claude", "type": "claude_usage", "host": "sshbox", "_base_dir": make_repo(tmp_path)}
    monkeypatch.setattr(
        statusboard_tools.subprocess, "run",
        lambda argv, **kw: fake_completed("", stderr="ssh: connect to host 10.0.0.5 port 22: No route to host",
                                          returncode=255),
    )
    result = statusboard_tools.fetch_panel(panel, str(tmp_path), local_hostname="OTHERBOX")
    assert not result.ok and "No route to host" in result.body


def test_claude_usage_jump_needs_a_host(tmp_path):
    config = tmp_path / "acme_credentials"
    config.mkdir()
    path = config / "acme_statusboard.yaml"
    path.write_text("- name: c\n  type: claude_usage\n  jump: sshbox\n")
    with pytest.raises(ValueError, match="jump needs a host"):
        statusboard_tools.load_panels(str(tmp_path), config_path=str(path))


# %%
# Board: rendering #


def test_render_subscription_meters_with_left_and_reset():
    from src.status_board import claude_usage_renderable

    lines = claude_usage_renderable(SUBSCRIPTION_REPORT, now=NOW).plain.splitlines()
    assert lines[0] == "claude.ai max 20x · on homebox"
    assert lines[1].startswith("session      ▕")
    assert "  2% used · 98% left · resets " in lines[1] and "(in 3h 59m)" in lines[1]
    assert lines[3].startswith("weekly Fable ▕") and "77% used · 23% left" in lines[3]
    assert "(in 19h 59m)" in lines[3]


def test_render_bedrock_token_tally():
    from src.status_board import claude_usage_renderable

    lines = claude_usage_renderable(BEDROCK_REPORT).plain.splitlines()
    assert lines[0] == "bedrock · us.anthropic.claude-opus-5-5 · us-east-1 · on laptop"
    assert lines[1] == "pay per token: no allowance, nothing resets"
    assert lines[2] == "today      36 turns · 16.5k out · 74 in · 7.2M cache read · 841k cache write"
    assert lines[3] == "7 days   1473 turns · 907k out · 3.0k in · 365M cache read · 17.1M cache write"


def test_reset_text_names_the_day_beyond_today():
    from src.status_board import reset_text

    assert reset_text(None) == "no reset scheduled"
    assert reset_text(NOW - timedelta(minutes=1), now=NOW) == "resetting now"
    assert reset_text(NOW + timedelta(days=3, hours=2), now=NOW).endswith("(in 3d 2h)")
    later = reset_text(NOW + timedelta(days=3, hours=2), now=NOW)
    local = (NOW + timedelta(days=3, hours=2)).astimezone()
    assert later.startswith(f"resets {local.strftime('%a %H:%M')}")


# %%
