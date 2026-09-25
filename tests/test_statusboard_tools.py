# %%
# Imports #

import json
import os
import re
import subprocess

import pytest
import yaml
from utils import statusboard_tools

# %%
# Helpers #


def write_yaml(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as file_handle:
        yaml.safe_dump(payload, file_handle)
    return path


def make_credentials_repo(root, context, panels=None, hosts=None):
    """Create a fake <context>_credentials repo with optional statusboard config and inventory."""
    repo = os.path.join(str(root), f"{context}_credentials")
    os.makedirs(repo, exist_ok=True)
    if panels is not None:
        write_yaml(os.path.join(repo, f"{context}_statusboard.yaml"), panels)
    if hosts is not None:
        with open(os.path.join(repo, f"{context}_hosts.json"), "w", encoding="utf-8") as file_handle:
            json.dump({"hosts": hosts}, file_handle)
    return repo


SSH_PANEL = {"name": "vm_jobs", "type": "ssh_command", "host": "sshvm", "jump": "LAPTOP-1", "command": "bash x.sh"}
ACME_HOSTS = [
    {"name": "LAPTOP-1", "hostname": "10.0.0.10", "user": "jdoe", "port": 2222, "aliases": ["jump1"]},
    {"name": "vm-01", "hostname": "10.0.0.20", "user": "svc_acme", "aliases": ["sshvm"]},
]


# %%
# Discovery / loading #


def test_discover_finds_overlays_and_repo_root_config(tmp_path):
    make_credentials_repo(tmp_path, "acme", panels=[])
    make_credentials_repo(tmp_path, "empty")  # no config -> contributes nothing
    repo_root = os.path.join(str(tmp_path), "dotfiles")
    write_yaml(os.path.join(repo_root, "statusboard.yaml"), [])
    configs = statusboard_tools.discover_statusboard_configs(str(tmp_path), repo_root)
    assert [os.path.basename(path) for path, _ in configs] == ["statusboard.yaml", "acme_statusboard.yaml"]


def test_load_panels_stamps_base_dir_context_and_defaults_interval(tmp_path):
    repo = make_credentials_repo(tmp_path, "acme", panels=[SSH_PANEL])
    panels, config_paths = statusboard_tools.load_panels(str(tmp_path))
    assert len(panels) == 1 and len(config_paths) == 1
    assert panels[0]["_base_dir"] == repo
    assert panels[0]["_context"] == "acme"
    assert panels[0]["interval"] == statusboard_tools.DEFAULT_INTERVALS["ssh_command"]


def test_load_panels_rejects_duplicate_names(tmp_path):
    github_panel = {"name": "vm_jobs", "type": "github_prs", "token_env": "T"}
    make_credentials_repo(tmp_path, "aaa", panels=[SSH_PANEL])
    make_credentials_repo(tmp_path, "bbb", panels=[github_panel])
    with pytest.raises(ValueError, match="Duplicate statusboard panel name 'vm_jobs'"):
        statusboard_tools.load_panels(str(tmp_path))


@pytest.mark.parametrize(
    "panel, match",
    [
        ({"name": "x", "type": "nope"}, "unknown type"),
        ({"name": "x", "type": "ssh_command", "host": "h"}, "missing required keys: command"),
        ({"name": "x", "type": "github_prs"}, "missing required keys: token_env"),
        ({"name": "x", "type": "bitbucket_prs", "workspace": "w"}, "missing required keys"),
        ({"type": "ssh_command"}, "'name' and 'type'"),
    ],
)
def test_panel_validation(tmp_path, panel, match):
    make_credentials_repo(tmp_path, "acme", panels=[panel])
    with pytest.raises(ValueError, match=match):
        statusboard_tools.load_panels(str(tmp_path))


GOOD_LOG_LINK = {"pattern": r"^\S+ +(\S+)", "command": "tail -F ~/logs/{job}.log"}


@pytest.mark.parametrize(
    "log_link, match",
    [
        ("nope", "mapping with 'pattern' and 'command'"),
        ({"pattern": r"(\S+)"}, "mapping with 'pattern' and 'command'"),
        ({"pattern": "(", "command": "tail {job}"}, "does not compile"),
        ({"pattern": r"\S+", "command": "tail {job}"}, "needs a capture group"),
        ({"pattern": r"(\S+)", "command": "tail x.log"}, "must contain a {job} placeholder"),
    ],
)
def test_log_link_validation(tmp_path, log_link, match):
    make_credentials_repo(tmp_path, "acme", panels=[dict(SSH_PANEL, log_link=log_link)])
    with pytest.raises(ValueError, match=re.escape(match)):
        statusboard_tools.load_panels(str(tmp_path))


def test_log_link_only_on_ssh_command(tmp_path):
    panel = {"name": "x", "type": "github_prs", "token_env": "T", "log_link": GOOD_LOG_LINK}
    make_credentials_repo(tmp_path, "acme", panels=[panel])
    with pytest.raises(ValueError, match="only supported on ssh_command"):
        statusboard_tools.load_panels(str(tmp_path))


def test_host_stats_only_on_ssh_command(tmp_path):
    panel = {"name": "x", "type": "github_prs", "token_env": "T", "host_stats": True}
    make_credentials_repo(tmp_path, "acme", panels=[panel])
    with pytest.raises(ValueError, match="host_stats is only supported on ssh_command"):
        statusboard_tools.load_panels(str(tmp_path))


def test_host_stats_makes_command_optional(tmp_path):
    stats_only = {"name": "vm_stats", "type": "ssh_command", "host": "sshvm", "host_stats": True}
    make_credentials_repo(tmp_path, "acme", panels=[stats_only])
    panels, _ = statusboard_tools.load_panels(str(tmp_path))
    assert panels[0]["host_stats"] is True
    # without host_stats a missing command is still an error (covered in test_panel_validation)


def test_panel_command_composition():
    plain = {"command": "bash x.sh"}
    stats_only = {"host_stats": True}
    both = {"command": "bash x.sh", "host_stats": True}
    assert statusboard_tools.panel_command(plain) == "bash x.sh"
    assert statusboard_tools.panel_command(stats_only) == statusboard_tools.HOST_STATS_COMMAND
    assert statusboard_tools.panel_command(both) == (
        f"bash x.sh; echo; {statusboard_tools.HOST_STATS_COMMAND}"
    )


def test_build_ssh_argv_appends_host_stats(tmp_path):
    repo = make_credentials_repo(tmp_path, "acme", hosts=ACME_HOSTS)
    panel = dict(SSH_PANEL, host_stats=True, _base_dir=repo)
    argv = statusboard_tools.build_ssh_argv(panel, str(tmp_path), local_hostname="OTHERBOX")
    assert argv[-1] == f"bash x.sh; echo; {statusboard_tools.HOST_STATS_COMMAND}"
    # an explicit command override (the log-follow pane) never picks up the stats line
    argv = statusboard_tools.build_ssh_argv(panel, str(tmp_path), local_hostname="OTHERBOX", command="tail -F x.log")
    assert argv[-1] == "tail -F x.log"


STATS_LINE = (
    "@@STATS@@ load=0.19,0.15,0.22 cpu=4 mem=2018/15992 "
    "disk=/:50331648/62914560|/Volumes/My Passport:1048576/2097152"
)
PARSED_STATS = {
    "disks": [
        {"label": "/", "used_kb": 50331648, "total_kb": 62914560},
        {"label": "/Volumes/My Passport", "used_kb": 1048576, "total_kb": 2097152},
    ],
    "load": (0.19, 0.15, 0.22),
    "cpus": 4,
    "mem_used_mb": 2018,
    "mem_total_mb": 15992,
}


def make_fake_run(stdout, returncode=0):
    class FakeCompleted:
        pass

    FakeCompleted.returncode = returncode
    FakeCompleted.stdout = stdout
    FakeCompleted.stderr = ""
    return lambda *args, **kwargs: FakeCompleted()


def test_fetch_ssh_command_splits_stats_marker_into_stats(tmp_path, monkeypatch):
    repo = make_credentials_repo(tmp_path, "acme", hosts=ACME_HOSTS)
    panel = dict(SSH_PANEL, host_stats=True, _base_dir=repo)
    monkeypatch.setattr(
        statusboard_tools.subprocess, "run", make_fake_run(f"job rows\n21 ok - 2 failed\n{STATS_LINE}\n")
    )
    result = statusboard_tools.fetch_ssh_command(panel, str(tmp_path))
    assert result.body == "job rows\n21 ok - 2 failed"
    assert result.summary == "exit 0"
    assert result.stats == PARSED_STATS


def test_fetch_ssh_command_stats_only_panel(tmp_path, monkeypatch):
    repo = make_credentials_repo(tmp_path, "acme", hosts=ACME_HOSTS)
    panel = {"name": "vm_stats", "type": "ssh_command", "host": "sshvm", "host_stats": True, "_base_dir": repo}
    monkeypatch.setattr(statusboard_tools.subprocess, "run", make_fake_run(f"{STATS_LINE}\n"))
    result = statusboard_tools.fetch_ssh_command(panel, str(tmp_path))
    assert result.body == ""
    assert result.stats == PARSED_STATS


def test_fetch_ssh_command_missing_stats_line_leaves_output_untouched(tmp_path, monkeypatch):
    repo = make_credentials_repo(tmp_path, "acme", hosts=ACME_HOSTS)
    panel = dict(SSH_PANEL, host_stats=True, _base_dir=repo)
    monkeypatch.setattr(statusboard_tools.subprocess, "run", make_fake_run("job rows\nlast line\n"))
    result = statusboard_tools.fetch_ssh_command(panel, str(tmp_path))
    assert result.body == "job rows\nlast line"
    assert result.stats is None


def test_parse_host_stats_garbled_line_returns_none():
    assert statusboard_tools._parse_host_stats("@@STATS@@ load=1,2,3 cpu=4 mem=1/2 disk=oops") is None
    assert statusboard_tools._parse_host_stats("@@STATS@@ load=1,2 cpu=4 mem=1/2 disk=/:1/2") is None
    assert statusboard_tools._parse_host_stats("@@STATS@@") is None


def test_parse_host_stats_no_drives_is_an_empty_list():
    stats = statusboard_tools._parse_host_stats("@@STATS@@ load=1,2,3 cpu=4 mem=1/2 disk=")
    assert stats["disks"] == [] and stats["cpus"] == 4


def test_log_link_valid_loads(tmp_path):
    make_credentials_repo(tmp_path, "acme", panels=[dict(SSH_PANEL, log_link=GOOD_LOG_LINK)])
    panels, _ = statusboard_tools.load_panels(str(tmp_path))
    assert panels[0]["log_link"] == GOOD_LOG_LINK


def test_load_panels_single_config_escape_hatch(tmp_path):
    config_path = write_yaml(os.path.join(str(tmp_path), "solo.yaml"), [SSH_PANEL])
    panels, config_paths = statusboard_tools.load_panels("/nonexistent", config_path=config_path)
    assert config_paths == [config_path]
    assert panels[0]["_base_dir"] == str(tmp_path)


# %%
# Host resolution / ssh argv #


def test_find_host_matches_name_and_alias_case_insensitive(tmp_path):
    repo = make_credentials_repo(tmp_path, "acme", hosts=ACME_HOSTS)
    assert statusboard_tools.find_host("SSHVM", repo, str(tmp_path))["hostname"] == "10.0.0.20"
    assert statusboard_tools.find_host("laptop-1", repo, str(tmp_path))["port"] == 2222


def test_find_host_searches_other_inventories(tmp_path):
    make_credentials_repo(tmp_path, "aaa", hosts=ACME_HOSTS)
    other = make_credentials_repo(tmp_path, "bbb", hosts=[])
    assert statusboard_tools.find_host("sshvm", other, str(tmp_path))["user"] == "svc_acme"


def test_find_host_unknown_raises(tmp_path):
    repo = make_credentials_repo(tmp_path, "acme", hosts=ACME_HOSTS)
    with pytest.raises(ValueError, match="Host 'ghost' not found"):
        statusboard_tools.find_host("ghost", repo, str(tmp_path))


def test_build_ssh_argv_with_jump_and_ports(tmp_path):
    repo = make_credentials_repo(tmp_path, "acme", hosts=ACME_HOSTS)
    panel = dict(SSH_PANEL, _base_dir=repo)
    argv = statusboard_tools.build_ssh_argv(panel, str(tmp_path), local_hostname="OTHERBOX")
    assert argv[0] == "ssh"
    assert argv[argv.index("-J") + 1] == "jdoe@10.0.0.10:2222"
    assert argv[-2:] == ["svc_acme@10.0.0.20", "bash x.sh"]


def test_build_ssh_argv_skips_jump_when_running_on_jump_host(tmp_path):
    repo = make_credentials_repo(tmp_path, "acme", hosts=ACME_HOSTS)
    panel = dict(SSH_PANEL, _base_dir=repo)
    argv = statusboard_tools.build_ssh_argv(panel, str(tmp_path), local_hostname="LAPTOP-1.local")
    assert "-J" not in argv


def test_build_ssh_argv_command_override(tmp_path):
    repo = make_credentials_repo(tmp_path, "acme", hosts=ACME_HOSTS)
    panel = dict(SSH_PANEL, _base_dir=repo)
    argv = statusboard_tools.build_ssh_argv(
        panel, str(tmp_path), local_hostname="OTHERBOX", command="tail -F ~/logs/git_pull.log"
    )
    # same chain as the panel's own fetch, only the remote command differs
    assert argv[argv.index("-J") + 1] == "jdoe@10.0.0.10:2222"
    assert argv[-2:] == ["svc_acme@10.0.0.20", "tail -F ~/logs/git_pull.log"]


def test_build_ssh_argv_target_port(tmp_path):
    hosts = [{"name": "boxy", "hostname": "10.0.0.5", "user": "me", "port": 2200}]
    repo = make_credentials_repo(tmp_path, "acme", hosts=hosts)
    panel = {"name": "p", "type": "ssh_command", "host": "boxy", "command": "uptime", "_base_dir": repo}
    argv = statusboard_tools.build_ssh_argv(panel, str(tmp_path))
    assert argv[argv.index("-p") + 1] == "2200"
    assert "-J" not in argv


def test_build_ssh_argv_identity_file(tmp_path):
    hosts = [{"name": "boxy", "hostname": "10.0.0.5", "user": "me", "identity_file": "~/.ssh/id_boxy"}]
    repo = make_credentials_repo(tmp_path, "acme", hosts=hosts)
    panel = {"name": "p", "type": "ssh_command", "host": "boxy", "command": "uptime", "_base_dir": repo}
    argv = statusboard_tools.build_ssh_argv(panel, str(tmp_path), local_hostname="OTHERBOX")
    assert argv[argv.index("-i") + 1] == os.path.expanduser("~/.ssh/id_boxy")


def test_build_ssh_argv_runs_locally_when_target_is_this_machine(tmp_path):
    # inventory-name match against local_hostname -> plain shell argv, no ssh
    repo = make_credentials_repo(tmp_path, "acme", hosts=ACME_HOSTS)
    panel = {"name": "p", "type": "ssh_command", "host": "sshvm", "command": "uptime", "_base_dir": repo}
    argv = statusboard_tools.build_ssh_argv(panel, str(tmp_path), local_hostname="vm-01.internal")
    assert argv[0] != "ssh"
    assert argv[-1] == "uptime"


# %%
# Secrets #


def test_resolve_secret_environment_wins(tmp_path, monkeypatch):
    env_file = os.path.join(str(tmp_path), "x.env")
    with open(env_file, "w", encoding="utf-8") as file_handle:
        file_handle.write("MY_TOKEN=from_file\n")
    panel = {"name": "p", "token_env": "MY_TOKEN", "env_file": "x.env", "_base_dir": str(tmp_path)}
    monkeypatch.setenv("MY_TOKEN", "from_env")
    assert statusboard_tools.resolve_secret(panel, "token_env") == "from_env"
    monkeypatch.delenv("MY_TOKEN")
    assert statusboard_tools.resolve_secret(panel, "token_env") == "from_file"


def test_resolve_secret_missing_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("MY_TOKEN", raising=False)
    panel = {"name": "p", "token_env": "MY_TOKEN", "_base_dir": str(tmp_path)}
    with pytest.raises(ValueError, match="MY_TOKEN is not set"):
        statusboard_tools.resolve_secret(panel, "token_env")


# %%
# Fetch dispatch #


def test_fetch_panel_never_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("NOPE_TOKEN", raising=False)
    panel = {"name": "p", "type": "github_prs", "token_env": "NOPE_TOKEN", "_base_dir": str(tmp_path)}
    result = statusboard_tools.fetch_panel(panel, str(tmp_path))
    assert result.ok is False
    assert "NOPE_TOKEN" in result.body


# %%
# Browser launch argv #


def test_browser_open_argv():
    from src.status_board import browser_open_argv

    url = "https://github.com/x/y/pull/1"
    assert browser_open_argv(None, url) is None
    assert browser_open_argv("edge", url, system="Darwin") == ["open", "-a", "Microsoft Edge", url]
    assert browser_open_argv("Edge", url, system="Windows") == ["cmd", "/c", "start", "", "msedge", url]
    assert browser_open_argv("chrome", url, system="Linux") == ["google-chrome", url]
    # unknown names pass through as the app/binary name
    assert browser_open_argv("Brave Browser", url, system="Darwin") == ["open", "-a", "Brave Browser", url]


# %%
# Host-stats meters #


def test_ramp_style_scales_green_to_red():
    from src.status_board import ramp_style

    def rgb(fraction):
        color = ramp_style(fraction)
        return int(color[1:3], 16), int(color[3:5], 16), int(color[5:7], 16)

    empty_r, empty_g, _ = rgb(0.0)
    full_r, full_g, _ = rgb(1.0)
    assert empty_g > empty_r        # empty end is green
    assert full_r > full_g          # full end is red
    assert rgb(-1) == rgb(0.0) and rgb(2) == rgb(1.0)  # clamped


def test_stats_renderable_meters_and_values():
    from src.status_board import (
        METER_EMPTY,
        METER_FILLED,
        METER_WIDTH,
        stats_renderable,
    )

    text = stats_renderable(PARSED_STATS)
    plain = text.plain
    # one line per drive (labels padded so the bars align), then cpu + mem on one line
    lines = plain.splitlines()
    assert len(lines) == 3
    longest = "disk /Volumes/My Passport"
    assert lines[0].startswith("disk /".ljust(len(longest)) + " ▕") and lines[1].startswith(longest + " ▕")
    assert lines[2].count("▕") == 2
    # four labeled meters, each a full-width bracketed bar
    assert plain.count("▕") == 4 and plain.count("▏") == 4
    for segment in plain.split("▕")[1:]:
        bar = segment.split("▏")[0]
        assert len(bar) == METER_WIDTH
        assert set(bar) <= {METER_FILLED, METER_EMPTY}
    # readouts: disks 80% and 50%, cpu load/cores 4%, mem 13%
    assert " 80% 48G of 60G" in plain
    assert " 50% 1G of 2G" in plain
    assert "load 0.19 0.15 0.22 · 4 cores" in plain
    assert " 13% 2.0G of 15.6G" in plain
    # disk bar is mostly full, cpu bar nearly empty
    disk_bar = plain.split("▕")[1].split("▏")[0]
    cpu_bar = plain.split("▕")[3].split("▏")[0]
    assert disk_bar.count(METER_FILLED) == round(0.8 * METER_WIDTH)
    assert cpu_bar.count(METER_FILLED) <= 1


def test_stats_renderable_handles_missing_stats():
    from src.status_board import stats_renderable

    assert "unavailable" in stats_renderable(None).plain


# %%
# Log-link row marking #


def test_mark_log_links_marks_job_tokens():
    from rich.text import Text
    from src.status_board import mark_log_links

    board = (
        "cron job status  (2026-07-22)\n"
        "● FAIL  git_pull      5m ago\n"
        "● ok    hme_ingest    2h ago\n"
        "3 ok · 1 failed\n"
    )
    text = mark_log_links(Text(board), {"pattern": r"^● \S+ +(\S+)", "command": "tail -F {job}"}, "vm_jobs")
    clicks = [
        (text.plain[span.start:span.end], span.style.meta["@click"])
        for span in text.spans
        if getattr(span.style, "meta", {}).get("@click")
    ]
    assert clicks == [
        ("git_pull", "app.view_log('vm_jobs', 'git_pull')"),
        ("hme_ingest", "app.view_log('vm_jobs', 'hme_ingest')"),
    ]
    # header and summary lines stay plain
    assert all(job in ("git_pull", "hme_ingest") for job, _ in clicks)


# %%

# %%
# GitHub PR classification #


def gh_item(repo, number, author="alice"):
    return {
        "html_url": f"https://github.com/{repo}/pull/{number}",
        "repository_url": f"https://api.github.com/repos/{repo}",
        "number": number,
        "title": f"PR {number}",
        "user": {"login": author},
        "updated_at": "2026-07-15T12:00:00Z",
    }


def test_classify_github_prs_buckets_and_order():
    fresh = gh_item("acme/app", 1)                # requested, no review from me -> needs review
    blocked = gh_item("acme/app", 2)              # requested again, but my latest review = CR -> on author
    reviewed_cr = gh_item("acme/app", 3)          # not requested anymore, my review = CR -> on author
    soft = gh_item("acme/app", 4)                 # only commented -> soft bucket
    approved = gh_item("acme/app", 5)             # I approved, nothing pending -> dropped
    my_open = gh_item("acme/app", 6, author="me")
    states = {
        blocked["html_url"]: {"me": "CHANGES_REQUESTED"},
        reviewed_cr["html_url"]: {"me": "CHANGES_REQUESTED"},
        soft["html_url"]: {"me": "COMMENTED"},
        approved["html_url"]: {"me": "APPROVED"},
        my_open["html_url"]: {"bob": "APPROVED"},
    }
    rows, summary = statusboard_tools.classify_github_prs(
        "me", [fresh, blocked], [reviewed_cr, soft, approved], [my_open], states
    )
    assert [(r["badge"], r["url"]) for r in rows] == [
        ("●", fresh["html_url"]),
        ("✋", blocked["html_url"]),
        ("✋", reviewed_cr["html_url"]),
        ("💬", soft["html_url"]),
        ("⬆", my_open["html_url"]),
    ]
    assert summary == "1 to review · 2 on author · 1 yours"
    assert "your PR · ✓ approved" in rows[-1]["meta"]


def test_classify_github_prs_own_pr_verdicts():
    pr = gh_item("acme/app", 7, author="me")
    for others, verdict in [
        ({"bob": "CHANGES_REQUESTED", "eve": "APPROVED"}, "✗ changes requested"),
        ({"bob": "APPROVED"}, "✓ approved"),
        ({}, "⧗ awaiting review"),
        ({"me": "COMMENTED"}, "⧗ awaiting review"),  # my own comments don't count
    ]:
        rows, _ = statusboard_tools.classify_github_prs("me", [], [], [pr], {pr["html_url"]: others})
        assert verdict in rows[0]["meta"]


def test_classify_github_prs_dedups_requested_and_reviewed():
    pr = gh_item("acme/app", 8)
    rows, summary = statusboard_tools.classify_github_prs("me", [pr], [pr], [], {})
    assert len(rows) == 1
    assert summary == "1 to review · 0 on author · 0 yours"


# %%

def test_classify_github_prs_flags_unsubmitted_draft_first():
    draft = gh_item("acme/app", 9)
    fresh = gh_item("acme/app", 10)
    states = {draft["html_url"]: {"me": "PENDING"}}
    rows, summary = statusboard_tools.classify_github_prs("me", [fresh, draft], [], [], states)
    assert [(r["badge"], r["url"]) for r in rows] == [("✏", draft["html_url"]), ("●", fresh["html_url"])]
    assert summary.startswith("1 UNSUBMITTED · 1 to review")
    assert "author can't see it" in rows[0]["meta"]


def test_classify_github_prs_parks_all_drafts_last():
    my_draft = dict(gh_item("acme/app", 11, author="me"), draft=True)
    their_draft = dict(gh_item("acme/app", 12), draft=True)  # even with my review requested
    fresh = gh_item("acme/app", 13)
    rows, summary = statusboard_tools.classify_github_prs("me", [their_draft, fresh], [], [my_draft], {})
    assert [(r["badge"], r["url"]) for r in rows] == [
        ("●", fresh["html_url"]),
        ("◌", their_draft["html_url"]),
        ("◌", my_draft["html_url"]),
    ]
    assert all(r["dim"] for r in rows if r["badge"] == "◌")
    assert summary == "1 to review · 0 on author · 0 yours · 2 parked"


def test_classify_github_prs_rerequest_returns_to_needs_review():
    pr = gh_item("acme/app", 14)
    states = {pr["html_url"]: {"me": "CHANGES_REQUESTED"}}
    # not personally re-requested (e.g. team request) -> still waiting on author
    rows, _ = statusboard_tools.classify_github_prs("me", [pr], [], [], states)
    assert rows[0]["badge"] == "✋"
    # author explicitly re-requested me -> back to needs review, round two
    rows, summary = statusboard_tools.classify_github_prs("me", [pr], [], [], states, {pr["html_url"]})
    assert rows[0]["badge"] == "●"
    assert "re-requested after your changes" in rows[0]["meta"]
    assert summary == "1 to review · 0 on author · 0 yours"


def test_classify_github_prs_pushed_since_returns_to_needs_review():
    pr = gh_item("acme/app", 15)
    states = {pr["html_url"]: {"me": "CHANGES_REQUESTED"}}
    # head moved past the sha my review was submitted against -> back to needs review
    rows, summary = statusboard_tools.classify_github_prs(
        "me", [], [pr], [], states, updated_since={pr["html_url"]}
    )
    assert rows[0]["badge"] == "●"
    assert "updated since your changes" in rows[0]["meta"]
    assert summary == "1 to review · 0 on author · 0 yours"
    # an explicit re-request wins over the pushed-since signal
    rows, _ = statusboard_tools.classify_github_prs(
        "me", [pr], [], [], states, {pr["html_url"]}, {pr["html_url"]}
    )
    assert "re-requested after your changes" in rows[0]["meta"]


def test_pr_review_states_tracks_commit_ids(monkeypatch):
    reviews = [
        {"user": {"login": "me"}, "state": "CHANGES_REQUESTED", "commit_id": "aaa111"},
        {"user": {"login": "bob"}, "state": "APPROVED", "commit_id": "bbb222"},
        {"user": {"login": "eve"}, "state": "COMMENTED", "commit_id": "ccc333"},
        {"user": {"login": "eve"}, "state": "DISMISSED", "commit_id": "ddd444"},
    ]

    class FakeResponse:
        status_code = 200

        def json(self):
            return reviews

    monkeypatch.setattr(statusboard_tools.requests, "get", lambda *a, **k: FakeResponse())
    states, commits = statusboard_tools._pr_review_states("https://api.github.com", {}, gh_item("acme/app", 16))
    assert states == {"me": "CHANGES_REQUESTED", "bob": "APPROVED"}
    assert commits == {"me": "aaa111", "bob": "bbb222"}


class FakeGithubResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.payload = payload
        self.headers = {}

    def json(self):
        return self.payload


def test_github_login_falls_back_to_graphql_viewer_on_user_5xx(monkeypatch):
    """/user 503 with search healthy: take the login from GraphQL, keep the panel alive."""
    posted = []

    def fake_post(url, **kwargs):
        posted.append((url, kwargs.get("json")))
        return FakeGithubResponse(200, {"data": {"viewer": {"login": "me"}}})

    monkeypatch.setattr(statusboard_tools.requests, "get", lambda *a, **k: FakeGithubResponse(503))
    monkeypatch.setattr(statusboard_tools.requests, "post", fake_post)
    login, error = statusboard_tools._github_login("https://api.github.com", {})
    assert (login, error) == ("me", None)
    assert posted == [("https://api.github.com/graphql", {"query": "{viewer{login}}"})]
    # Enterprise splits the APIs: /api/v3 for REST, /api/graphql for GraphQL
    statusboard_tools._github_login("https://ghe.example.com/api/v3", {})
    assert posted[-1][0] == "https://ghe.example.com/api/graphql"


def test_github_login_error_blames_the_token_only_on_401_403(monkeypatch):
    """A 5xx is GitHub's, not the PAT's - and a dead token skips the GraphQL hop."""
    posts = []

    def responses(user_code, viewer=FakeGithubResponse(503)):
        monkeypatch.setattr(statusboard_tools.requests, "get", lambda *a, **k: FakeGithubResponse(user_code))
        monkeypatch.setattr(statusboard_tools.requests, "post", lambda *a, **k: posts.append(a) or viewer)
        login, error = statusboard_tools._github_login("https://api.github.com", {})
        assert login is None
        return error

    assert responses(503) == "GitHub /user returned 503 (GitHub-side, not your token)" \
        "; GraphQL viewer returned 503"
    assert responses(404) == "GitHub /user returned 404; GraphQL viewer returned 503"
    # GraphQL answering 200 with no viewer (schema/permission oddity) is still a failure
    assert responses(503, FakeGithubResponse(200, {"data": {"viewer": None}})) == (
        "GitHub /user returned 503 (GitHub-side, not your token); GraphQL viewer returned 200"
    )
    before = len(posts)
    assert responses(401) == "GitHub /user returned 401 (bad/expired token?)"
    assert responses(403) == "GitHub /user returned 403 (bad/expired token?)"
    assert len(posts) == before  # no GraphQL attempt for a token GitHub already rejected


def test_fetch_github_prs_renders_through_a_user_outage(monkeypatch):
    """End to end: /user 503, GraphQL login, searches healthy -> real rows, no error panel."""
    mine = gh_item("acme/app", 21, author="me")

    def fake_get(url, **kwargs):
        if url.endswith("/user"):
            return FakeGithubResponse(503)
        if url.endswith("/search/issues"):
            author_query = "author:me" in kwargs["params"]["q"]
            return FakeGithubResponse(200, {"items": [mine] if author_query else []})
        if url.endswith("/reviews"):
            return FakeGithubResponse(200, [{"user": {"login": "bob"}, "state": "APPROVED", "commit_id": "a1"}])
        if url.endswith("/user/orgs"):
            return FakeGithubResponse(200, [])
        raise AssertionError(f"unexpected GET {url}")

    monkeypatch.setattr(statusboard_tools.requests, "get", fake_get)
    monkeypatch.setattr(
        statusboard_tools.requests, "post",
        lambda *a, **k: FakeGithubResponse(200, {"data": {"viewer": {"login": "me"}}}),
    )
    monkeypatch.setattr(statusboard_tools, "resolve_secret", lambda panel, key: "x")
    result = statusboard_tools.fetch_github_prs({"name": "gh", "type": "github_prs", "token_env": "T"})
    assert result.ok
    assert [row["url"] for row in result.body] == [mine["html_url"]]
    assert result.summary.endswith("(me)")


# %%
# Bitbucket PR classification #


def bb_pr(number, author_uuid="{alice}", participants=(), **extra):
    return dict(
        {
            "id": number,
            "title": f"PR {number}",
            "author": {"uuid": author_uuid, "display_name": "Alice"},
            "updated_on": "2026-07-15T12:00:00+00:00",
            "links": {"html": {"href": f"https://bitbucket.org/ws/repo/pull-requests/{number}"}},
            "participants": list(participants),
        },
        **extra,
    )


def test_classify_bitbucket_prs_buckets_and_order():
    me = "{me}"
    fresh = bb_pr(1)                                                              # reviewer, no vote -> needs review
    blocked = bb_pr(2, participants=[{"user": {"uuid": me}, "state": "changes_requested"}])
    approved = bb_pr(3, participants=[{"user": {"uuid": me}, "state": "approved"}])  # dropped
    my_open = bb_pr(4, author_uuid=me, participants=[{"user": {"uuid": "{bob}"}, "state": "approved"}])
    rows, summary = statusboard_tools.classify_bitbucket_prs(
        me, [("repo", fresh), ("repo", blocked), ("repo", approved), ("repo", my_open)]
    )
    assert [(r["badge"], r["url"]) for r in rows] == [
        ("●", fresh["links"]["html"]["href"]),
        ("✋", blocked["links"]["html"]["href"]),
        ("⬆", my_open["links"]["html"]["href"]),
    ]
    assert summary == "1 to review · 1 on author · 1 yours"
    assert "your PR · ✓ approved" in rows[-1]["meta"]
    assert rows[0]["text"] == "repo#1  PR 1"


def test_classify_bitbucket_prs_own_pr_verdicts():
    me = "{me}"
    for participants, verdict in [
        ([{"user": {"uuid": "{bob}"}, "state": "changes_requested"},
          {"user": {"uuid": "{eve}"}, "state": "approved"}], "✗ changes requested"),
        ([{"user": {"uuid": "{bob}"}, "state": "approved"}], "✓ approved"),
        ([], "⧗ awaiting review"),
        ([{"user": {"uuid": me}, "state": "approved"}], "⧗ awaiting review"),  # my own vote doesn't count
    ]:
        rows, _ = statusboard_tools.classify_bitbucket_prs(
            me, [("repo", bb_pr(5, author_uuid=me, participants=participants))]
        )
        assert verdict in rows[0]["meta"]


def test_classify_bitbucket_prs_parks_all_drafts_last():
    me = "{me}"
    my_draft = bb_pr(6, author_uuid=me, draft=True)
    their_draft = bb_pr(7, draft=True)
    fresh = bb_pr(8)
    rows, summary = statusboard_tools.classify_bitbucket_prs(
        me, [("repo", my_draft), ("repo", their_draft), ("repo", fresh)]
    )
    assert [(r["badge"], r["url"]) for r in rows] == [
        ("●", fresh["links"]["html"]["href"]),
        ("◌", my_draft["links"]["html"]["href"]),
        ("◌", their_draft["links"]["html"]["href"]),
    ]
    assert all(r["dim"] for r in rows if r["badge"] == "◌")
    assert summary == "1 to review · 0 on author · 0 yours · 2 parked"


def test_bitbucket_involves_author_or_reviewer():
    me = "{me}"
    assert statusboard_tools._bitbucket_involves(me, bb_pr(1, author_uuid=me))
    assert statusboard_tools._bitbucket_involves(
        me, bb_pr(2, author_uuid="{alice}", reviewers=[{"uuid": me}])
    )
    # neither author nor reviewer -> not mine to show
    assert not statusboard_tools._bitbucket_involves(
        me, bb_pr(3, author_uuid="{alice}", reviewers=[{"uuid": "{bob}"}])
    )


def test_fetch_bitbucket_prs_filters_client_side_no_uuid_query(monkeypatch):
    """The flaky server-side uuid filter is gone: list state=OPEN, filter locally."""
    calls = []
    me = "{me}"
    listing = [
        bb_pr(9, author_uuid=me),                                   # mine
        bb_pr(10, author_uuid="{alice}", reviewers=[{"uuid": me}]),  # I'm a reviewer, no vote yet
        bb_pr(11, author_uuid="{alice}", reviewers=[{"uuid": "{bob}"}]),  # not involved -> excluded
    ]

    class FakeResponse:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def fake_get(url, **kwargs):
        calls.append((url, kwargs.get("params") or {}))
        if url.endswith("/user"):
            return FakeResponse({"uuid": me})
        return FakeResponse({"values": listing})

    monkeypatch.setattr(statusboard_tools.requests, "get", fake_get)
    monkeypatch.setattr(statusboard_tools, "resolve_secret", lambda panel, key: "x")
    result = statusboard_tools.fetch_bitbucket_prs(
        {"name": "bb", "type": "bitbucket_prs", "workspace": "ws", "repos": ["repo"],
         "username_env": "U", "app_password_env": "P"}
    )
    list_params = calls[-1][1]
    assert "q" not in list_params                      # no unreliable uuid search
    assert list_params["state"] == "OPEN"
    assert "reviewers" in list_params["fields"] and "participants" in list_params["fields"]
    assert [row["badge"] for row in result.body] == ["●", "⬆"]  # PR 11 excluded
    assert result.summary == "1 to review · 0 on author · 1 yours (ws)"


# %%

# %%
# http_checks #


SITES = [
    {"name": "intranet", "url": "https://intranet.acme.internal/", "insecure": True},
    {"url": "http://10.0.0.20:8000/api/health"},
    {"name": "sso portal", "url": "https://portal.acme.internal/", "expect": [200, 302, 401]},
]
SITES_PANEL = {"name": "acme_sites", "type": "http_checks", "sites": SITES}


@pytest.mark.parametrize(
    "panel, match",
    [
        ({"name": "x", "type": "http_checks"}, "missing required keys: sites"),
        ({"name": "x", "type": "http_checks", "sites": "nope"}, "non-empty list"),
        ({"name": "x", "type": "http_checks", "sites": []}, "missing required keys: sites"),
        ({"name": "x", "type": "http_checks", "sites": [{"name": "no url"}]}, "every site needs a url"),
        ({"name": "x", "type": "http_checks", "sites": [{"url": "ftp://x"}]}, "must start with http"),
        ({"name": "x", "type": "http_checks", "sites": [{"url": "http://x", "expect": "200"}]}, "status code"),
        ({"name": "x", "type": "http_checks", "sites": [{"url": "http://x", "expect": [200, True]}]}, "status code"),
        ({"name": "x", "type": "http_checks", "sites": [{"url": "http://x"}], "jump": "j"}, "jump needs a host"),
        ({"name": "x", "type": "http_checks", "sites": [{"url": "http://x"}], "host_stats": True}, "only supported"),
    ],
)
def test_http_checks_validation(tmp_path, panel, match):
    make_credentials_repo(tmp_path, "acme", panels=[panel])
    with pytest.raises(ValueError, match=match):
        statusboard_tools.load_panels(str(tmp_path))


def test_http_checks_loads_with_defaults(tmp_path):
    make_credentials_repo(tmp_path, "acme", panels=[SITES_PANEL])
    panels, _ = statusboard_tools.load_panels(str(tmp_path))
    assert panels[0]["interval"] == statusboard_tools.DEFAULT_INTERVALS["http_checks"]
    assert panels[0]["sites"][2]["expect"] == [200, 302, 401]


def test_curl_argv_flags_and_marker():
    argv = statusboard_tools.curl_argv(SITES[0], 10, devnull="/dev/null")
    assert argv[:6] == ["curl", "-sS", "-o", "/dev/null", "--max-time", "10"]
    assert "-k" in argv and "-L" not in argv
    assert argv[-1] == SITES[0]["url"]
    fmt = argv[argv.index("-w") + 1]
    assert fmt.startswith(statusboard_tools.HTTP_CHECK_MARKER)  # a leading "@" would make curl read a file
    assert not fmt.startswith("@")
    assert fmt.endswith("%{http_code} %{time_total}\\n")
    assert "-k" not in statusboard_tools.curl_argv(SITES[1], 10)
    assert statusboard_tools.curl_argv(SITES[1], 10)[3] == os.devnull


def test_http_checks_command_one_block_per_site():
    command = statusboard_tools.http_checks_command(dict(SITES_PANEL, max_time=7))
    blocks = command.split("; echo ")
    assert command.startswith(f"echo '{statusboard_tools.SITE_MARKER} 0'; curl ")
    assert len(blocks) == len(SITES)
    assert command.count("2>&1") == len(SITES)
    assert "--max-time 7" in command
    assert "-o /dev/null" in command
    # the -w format is single-quoted so the shell leaves %{} and \n alone
    assert f"-w '{statusboard_tools.HTTP_CHECK_MARKER} %{{http_code}} %{{time_total}}\\n'" in command


REMOTE_OUTPUT = (
    "==SITE== 0\n"
    "==CHECK== 200 0.014022\n"
    "==SITE== 1\n"
    "curl: (7) Failed to connect to 10.0.0.20 port 8000 after 3 ms: Couldn't connect to server\n"
    "==CHECK== 000 0.003100\n"
    "==SITE== 2\n"
    "==CHECK== 302 0.247034\n"
)


def test_split_site_blocks_and_parse():
    blocks = statusboard_tools.split_site_blocks(REMOTE_OUTPUT)
    assert sorted(blocks) == [0, 1, 2]
    assert statusboard_tools.parse_http_check(blocks[0]) == {"code": 200, "seconds": 0.014022, "error": None}
    down = statusboard_tools.parse_http_check(blocks[1])
    assert down["code"] == 0
    assert down["error"].startswith("(7) Failed to connect")
    assert statusboard_tools.parse_http_check("") == {"code": 0, "seconds": None, "error": "no response from curl"}
    # curl's multi-line cert advice: the reason line wins, the trailer still parses
    cert_block = (
        "curl: (60) SSL certificate problem: unable to get local issuer certificate\n"
        "More details here: https://curl.se/docs/sslcerts.html\n\n"
        "curl failed to verify the legitimacy of the server and therefore could not\n"
        "==CHECK== 000 0.033715\n"
    )
    parsed = statusboard_tools.parse_http_check(cert_block)
    assert parsed["code"] == 0 and parsed["error"].startswith("(60) SSL certificate problem")


def test_site_is_up_default_and_expect():
    up = statusboard_tools.site_is_up
    assert up({}, {"code": 200}) and up({}, {"code": 302})
    assert not up({}, {"code": 401}) and not up({}, {"code": 502}) and not up({}, {"code": 0})
    assert up({"expect": 401}, {"code": 401}) and not up({"expect": 401}, {"code": 200})
    assert up({"expect": [200, 401]}, {"code": 401})
    assert not up({"expect": [200]}, {"code": 0})


def test_site_label_defaults_to_host():
    assert statusboard_tools.site_label(SITES[0]) == "intranet"
    assert statusboard_tools.site_label(SITES[1]) == "10.0.0.20:8000"


def test_http_check_rows_alignment_and_summary():
    checks = [
        {"code": 200, "seconds": 0.014, "error": None},
        {"code": 0, "seconds": 0.003, "error": "(7) Failed to connect"},
        {"code": 401, "seconds": 0.2, "error": None},
    ]
    rows, summary, down = statusboard_tools.http_check_rows(SITES, checks)
    assert summary == "2 up · 1 DOWN" and down == 1
    assert [row["badge"] for row in rows] == ["✓", "✗", "✓"]
    assert rows[0]["text"] == "intranet" and rows[0]["url"] == SITES[0]["url"]
    assert rows[0]["tail"].endswith("200 · 14ms")
    assert rows[1]["tail"].endswith("DOWN · (7) Failed to connect") and rows[1]["tail_style"] == "red"
    # status column aligned: label + tail padding is constant across rows
    widths = {len(row["text"]) + len(row["tail"]) - len(row["tail"].lstrip()) for row in rows}
    assert len(widths) == 1
    rows, summary, down = statusboard_tools.http_check_rows(SITES[:1], checks[:1])
    assert summary == "all 1 up" and down == 0
    # a wrong status code is down with the code shown
    rows, _, _ = statusboard_tools.http_check_rows(SITES[1:2], [{"code": 502, "seconds": 0.1, "error": None}])
    assert rows[0]["tail"].endswith("DOWN · 502 · unexpected status 502")


def test_fetch_http_checks_remote_runs_one_ssh_and_flags_alert(tmp_path, monkeypatch):
    repo = make_credentials_repo(tmp_path, "acme", hosts=ACME_HOSTS)
    panel = dict(SITES_PANEL, host="sshvm", jump="LAPTOP-1", _base_dir=repo)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return make_fake_run(REMOTE_OUTPUT, returncode=7)()  # last curl's exit status leaks through ssh

    monkeypatch.setattr(statusboard_tools.subprocess, "run", fake_run)
    result = statusboard_tools.fetch_http_checks(panel, str(tmp_path), local_hostname="OTHERBOX")
    assert len(calls) == 1 and calls[0][0] == "ssh"
    assert calls[0][calls[0].index("-J") + 1] == "jdoe@10.0.0.10:2222"
    assert calls[0][-1] == statusboard_tools.http_checks_command(panel)
    assert result.ok and result.alert
    assert result.kind == "links"
    assert result.summary == "2 up · 1 DOWN · from sshvm"
    assert [row["badge"] for row in result.body] == ["✓", "✗", "✓"]


def test_fetch_http_checks_remote_chain_failure_is_an_error(tmp_path, monkeypatch):
    repo = make_credentials_repo(tmp_path, "acme", hosts=ACME_HOSTS)
    panel = dict(SITES_PANEL, host="sshvm", _base_dir=repo)

    def fake_run(argv, **kwargs):
        completed = make_fake_run("", returncode=255)()
        completed.stderr = "ssh: connect to host 10.0.0.20 port 22: No route to host"
        return completed

    monkeypatch.setattr(statusboard_tools.subprocess, "run", fake_run)
    result = statusboard_tools.fetch_http_checks(panel, str(tmp_path), local_hostname="OTHERBOX")
    assert not result.ok and "No route to host" in result.body


def test_fetch_http_checks_local_runs_curl_per_site(tmp_path, monkeypatch):
    panel = dict(SITES_PANEL, _base_dir=str(tmp_path))
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        completed = make_fake_run("==CHECK== 200 0.020000\n")()
        completed.stderr = ""
        return completed

    monkeypatch.setattr(statusboard_tools.subprocess, "run", fake_run)
    result = statusboard_tools.fetch_http_checks(panel, str(tmp_path))
    assert len(calls) == len(SITES) and all(argv[0] == "curl" for argv in calls)
    assert result.ok and not result.alert
    assert result.summary == "all 3 up"


def test_fetch_http_checks_host_is_this_machine_probes_locally(tmp_path, monkeypatch):
    # host resolves to the board's own machine -> no ssh, no shell: per-site curl argv
    repo = make_credentials_repo(tmp_path, "acme", hosts=ACME_HOSTS)
    panel = dict(SITES_PANEL, host="sshvm", jump="LAPTOP-1", _base_dir=repo)
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        completed = make_fake_run("==CHECK== 302 0.100000\n")()
        completed.stderr = ""
        return completed

    monkeypatch.setattr(statusboard_tools.subprocess, "run", fake_run)
    result = statusboard_tools.fetch_http_checks(panel, str(tmp_path), local_hostname="vm-01.internal")
    assert len(calls) == len(SITES) and all(argv[0] == "curl" for argv in calls)
    assert result.summary == "all 3 up · from sshvm"


def test_fetch_http_checks_local_timeout_is_down_not_error(tmp_path, monkeypatch):
    panel = dict(SITES_PANEL, sites=SITES[:1], max_time=3, _base_dir=str(tmp_path))

    def fake_run(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))

    monkeypatch.setattr(statusboard_tools.subprocess, "run", fake_run)
    result = statusboard_tools.fetch_http_checks(panel, str(tmp_path))
    assert result.ok and result.alert
    assert result.body[0]["tail"].endswith("DOWN · (28) no answer within 3s")


def test_fetch_http_checks_missing_curl_is_an_error(tmp_path, monkeypatch):
    panel = dict(SITES_PANEL, _base_dir=str(tmp_path))

    def fake_run(argv, **kwargs):
        raise FileNotFoundError("curl")

    monkeypatch.setattr(statusboard_tools.subprocess, "run", fake_run)
    result = statusboard_tools.fetch_panel(panel, str(tmp_path))
    assert not result.ok and "curl not found" in result.body


def test_result_renderable_tail_on_same_line():
    from src.status_board import result_renderable

    rows = [{"badge": "✓", "badge_style": "bold green", "text": "intranet", "url": "https://x/",
             "tail": "200 · 14ms", "tail_style": "dim"}]
    plain = result_renderable(statusboard_tools.PanelResult(True, "links", rows, "all 1 up")).plain
    assert plain == "✓ intranet  200 · 14ms"
    plain = result_renderable(statusboard_tools.PanelResult(True, "links", rows), tui=True).plain
    assert plain == "✓ intranet  200 · 14ms"


# %%
