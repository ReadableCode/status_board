# %%
# Imports #

# Claude usage probe for the claude_usage panel. Runs ON the host whose Claude
# Code it reports: the board pipes this file to `python3 -` over the panel's
# ssh chain (or runs it locally when the host is this machine) and reads back
# one JSON line. Standard library only, so the host needs python3 and nothing
# else installed.
#
# It reports whichever connection the host's Claude Code is configured for,
# read from ~/.claude/settings.json - the file the auth-mode switches
# re-symlink - so the panel follows a switch on its next poll:
#
# - subscription (a claude.ai login: Pro/Max/Team/Enterprise): the usage
#   endpoint behind Claude Code's /usage screen, read with the stored OAuth
#   access token. Account metadata only, no tokens spent. READ-ONLY: the token
#   is never refreshed or written back. Refresh tokens rotate, so a second
#   refresher racing claude itself (or a cron poller on the same host) can
#   strand the stored pair; claude refreshes it on next use.
# - bedrock: AWS Bedrock is pay per token with no allowance and nothing that
#   resets, and the Bedrock IAM role need not grant CloudWatch, so usage is
#   tallied from Claude Code's own transcripts on the host.

import glob
import json
import os
import platform
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

# %%
# Variables #

CLAUDE_DIR = os.path.expanduser("~/.claude")
SETTINGS_FILE = os.path.join(CLAUDE_DIR, "settings.json")
CREDENTIALS_FILE = os.path.join(CLAUDE_DIR, ".credentials.json")
TRANSCRIPTS_GLOB = os.path.join(CLAUDE_DIR, "projects", "**", "*.jsonl")
KEYCHAIN_SERVICE = "Claude Code-credentials"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OAUTH_BETA_HEADER = "oauth-2025-04-20"
HTTP_TIMEOUT = 20
TRUTHY = ("1", "true", "yes", "on")
WEEK_SECONDS = 7 * 86400


# %%
# Configured connection #


def configured_env(settings_path=SETTINGS_FILE):
    """The env block of the user's Claude Code settings ({} when absent)."""
    if not os.path.exists(settings_path):
        return {}
    with open(settings_path, encoding="utf-8") as file_handle:
        settings = json.load(file_handle)
    return {key: str(value) for key, value in (settings.get("env") or {}).items()}


def connection_mode(env):
    """bedrock when settings switch it on, else the claude.ai subscription login."""
    if env.get("CLAUDE_CODE_USE_BEDROCK", "").strip().lower() in TRUTHY:
        return "bedrock"
    return "subscription"


# %%
# Subscription #


def load_oauth():
    """The claudeAiOauth record: the credentials file on Linux/Windows, the login keychain on macOS."""
    if os.path.exists(CREDENTIALS_FILE):
        with open(CREDENTIALS_FILE, encoding="utf-8") as file_handle:
            return json.load(file_handle).get("claudeAiOauth") or {}
    if platform.system() == "Darwin":
        result = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout).get("claudeAiOauth") or {}
    return {}


def expiry_seconds(oauth):
    """expiresAt is epoch milliseconds; tolerate seconds. None when the login carries no expiry."""
    raw = oauth.get("expiresAt")
    if not raw:
        return None
    raw = float(raw)
    return raw / 1000.0 if raw > 1e12 else raw


def subscription_report(now):
    oauth = load_oauth()
    report = {
        "mode": "subscription",
        "subscription": oauth.get("subscriptionType"),
        "tier": oauth.get("rateLimitTier"),
    }
    if not oauth.get("accessToken"):
        report["error"] = "no claude.ai login on this host - run `claude` and log in"
        return report
    expires = expiry_seconds(oauth)
    if expires is not None and expires <= now:
        report["error"] = (
            f"access token expired {datetime.fromtimestamp(expires).strftime('%a %m-%d %H:%M')}"
            " - claude refreshes it on next use here"
        )
        return report
    request = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {oauth['accessToken']}",
            "anthropic-beta": OAUTH_BETA_HEADER,
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            report["usage"] = json.load(response)
    except urllib.error.HTTPError as error:
        hint = " (token rejected - claude refreshes it on next use here)" if error.code in (401, 403) else ""
        report["error"] = f"usage endpoint returned HTTP {error.code}{hint}"
    except (urllib.error.URLError, TimeoutError) as error:
        report["error"] = f"usage endpoint unreachable: {getattr(error, 'reason', error)}"
    return report


# %%
# Bedrock #


def empty_tally():
    return {"turns": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0}


def tally_transcripts(paths, now, day_start):
    """
    Sum token usage per window (today since local midnight, rolling 7 days)
    across Claude Code transcripts. Claude Code writes one line per content
    block, each repeating the message's usage, so a message counts once by its
    message id. Files untouched for a week are skipped unread.
    """
    windows = {"today": empty_tally(), "week": empty_tally()}
    seen = set()
    for path in paths:
        try:
            if os.path.getmtime(path) < now - WEEK_SECONDS:
                continue
            with open(path, encoding="utf-8", errors="replace") as file_handle:
                lines = [line for line in file_handle if '"usage"' in line]
        except OSError:
            continue
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            message = entry.get("message") or {}
            usage = message.get("usage")
            if entry.get("type") != "assistant" or not isinstance(usage, dict):
                continue
            key = message.get("id") or entry.get("requestId") or entry.get("uuid")
            if key in seen:
                continue
            seen.add(key)
            try:
                stamp = datetime.fromisoformat(entry["timestamp"].replace("Z", "+00:00")).timestamp()
            except (KeyError, AttributeError, ValueError):
                continue
            for name, start in (("today", day_start), ("week", now - WEEK_SECONDS)):
                if stamp < start:
                    continue
                tally = windows[name]
                tally["turns"] += 1
                tally["input"] += int(usage.get("input_tokens") or 0)
                tally["output"] += int(usage.get("output_tokens") or 0)
                tally["cache_read"] += int(usage.get("cache_read_input_tokens") or 0)
                tally["cache_write"] += int(usage.get("cache_creation_input_tokens") or 0)
    return windows


def bedrock_report(env, now):
    day_start = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return {
        "mode": "bedrock",
        "model": env.get("ANTHROPIC_MODEL"),
        "region": env.get("AWS_REGION"),
        "profile": env.get("AWS_PROFILE"),
        "tokens": tally_transcripts(glob.glob(TRANSCRIPTS_GLOB, recursive=True), now, day_start),
    }


# %%
# Main #


def main():
    now = time.time()
    try:
        env = configured_env()
        report = bedrock_report(env, now) if connection_mode(env) == "bedrock" else subscription_report(now)
    except Exception as error:  # noqa: BLE001 - always answer with one JSON line
        report = {"mode": "unknown", "error": f"{type(error).__name__}: {error}"}
    report["host"] = platform.node()
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())


# %%
