# %%
# Imports #

import json
import os
import re
import shlex
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import requests
import yaml
from readable_utils.host_stats_tools import (  # noqa: F401 - STATS_MARKER re-exported for callers
    HOST_STATS_COMMAND,
    STATS_MARKER,
    parse_host_stats,
    split_host_stats,
)
from readable_utils.inventory_tools import (
    credentials_context,
    find_credentials_dirs,
    find_host_record,
    find_inventory_paths,
    overlay_context,
)
from readable_utils.ssh_tools import (  # noqa: F401 - options/destination re-exported for callers
    SSH_BASE_OPTIONS,
)
from readable_utils.ssh_tools import build_ssh_argv as build_host_ssh_argv
from readable_utils.ssh_tools import (  # noqa: F401 - options/destination re-exported for callers
    ssh_destination,
)

# %%
# Variables #

PANEL_TYPES = ("ssh_command", "github_prs", "bitbucket_prs", "http_checks", "claude_usage")

# Per-type defaults: refresh interval (seconds) and, where relevant, timeouts
DEFAULT_INTERVALS = {
    "ssh_command": 300, "github_prs": 180, "bitbucket_prs": 180, "http_checks": 120, "claude_usage": 300,
}
DEFAULT_SSH_TIMEOUT = 60
DEFAULT_HTTP_TIMEOUT = 30
# http_checks: per-site curl --max-time (seconds), overridable per panel
DEFAULT_PROBE_TIMEOUT = 10
# http_checks probe output markers. curl reads a -w format that STARTS with
# "@" from a file, so neither marker may begin with one (the @@STATS@@ style
# of the host-stats line is not reusable here).
SITE_MARKER = "==SITE=="
HTTP_CHECK_MARKER = "==CHECK=="
DEFAULT_GITHUB_API = "https://api.github.com"
# claude_usage: the stdlib-only script piped to `python3 -` on the panel's host
CLAUDE_USAGE_PROBE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "claude_usage_probe.py")
BITBUCKET_API = "https://api.bitbucket.org/2.0"

# The @@STATS@@ probe, its parser, and the ssh plumbing (base options, jump
# hops, identity files, the run-locally short-circuit) live in readable_utils
# so herdstone and this board can't drift apart - this module only keeps the
# panel-level wiring.
_split_host_stats = split_host_stats
_parse_host_stats = parse_host_stats


# %%
# Results #


class PanelResult:
    """
    Outcome of one panel fetch.

    kind is "ansi" (body: raw terminal text to render as-is), "links"
    (body: list of {"text", "url", "meta"} rows the TUI turns into clickable
    lines), "sites" (link rows with a "tail" status column, laid out in as
    many columns as the panel width fits) or "claude_usage" (body: the
    probe's report dict). A failed
    fetch has ok=False and the error text in body. stats is the parsed
    host-stats dict when the panel collects them (None otherwise, or when the
    remote failed to emit/parse them). alert marks a fetch that WORKED but
    found something wrong (a site down) - the panel renders its rows normally
    and is flagged like an error, instead of collapsing to an error message.
    """

    def __init__(self, ok, kind, body, summary="", stats=None, alert=False):
        self.ok = ok
        self.kind = kind
        self.body = body
        self.summary = summary
        self.stats = stats
        self.alert = alert
        self.fetched_at = time.time()

    @classmethod
    def error(cls, message):
        return cls(False, "ansi", str(message), "error")


# %%
# Config discovery #


def discover_statusboard_configs(credentials_root, repo_root=None):
    """
    Locate every statusboard config to load: an optional ``statusboard.yaml``
    in this repo's root (tracked in the public repo, so secrets-free panels
    only) plus, for each sibling ``*_credentials`` repo, an optional
    ``<context>_statusboard.yaml`` - the same overlay pattern the dotfiles
    deploy tooling uses for manifests. Returns a list of
    (config_path, base_dir) pairs, overlays sorted for determinism.
    """
    configs = []
    if repo_root:
        main_config = os.path.join(repo_root, "statusboard.yaml")
        if os.path.exists(main_config):
            configs.append((main_config, repo_root))
    for credentials_dir in find_credentials_dirs(credentials_root):
        overlay = os.path.join(credentials_dir, f"{credentials_context(credentials_dir)}_statusboard.yaml")
        if os.path.exists(overlay):
            configs.append((overlay, credentials_dir))
    return configs


def load_panels(credentials_root, repo_root=None, config_path=None):
    """
    Load every discovered statusboard config, returning (panels, config_paths).
    Each panel is stamped with ``_base_dir`` (its config's repo root, which
    env_file paths resolve against and whose host inventory is searched first)
    and ``_config`` (for error messages). Panel names must be unique across
    ALL loaded configs.

    Passing config_path (the --config test escape hatch) loads only that file,
    base_dir its containing directory, skipping discovery.
    """
    if config_path:
        located = [(config_path, os.path.dirname(os.path.abspath(config_path)))]
    else:
        located = discover_statusboard_configs(credentials_root, repo_root)
    panels = []
    seen: dict = {}
    for path, base_dir in located:
        for panel in _parse_config_file(path):
            if panel["name"] in seen:
                raise ValueError(
                    f"Duplicate statusboard panel name '{panel['name']}' in {path} "
                    f"(already defined in {seen[panel['name']]})"
                )
            seen[panel["name"]] = path
            panel["_base_dir"] = base_dir
            panel["_config"] = path
            # context token for visual grouping: the credentials repo the
            # panel travels with ("acme", "personal", ...), or this repo's
            # own directory name for the root config
            panel["_context"] = overlay_context(base_dir)
            panels.append(panel)
    return panels, [path for path, _ in located]


def _parse_config_file(config_path):
    """Parse one statusboard config and validate the panel schema, returning a list of panel dicts."""
    with open(config_path, "r", encoding="utf-8") as file_handle:
        panels = yaml.safe_load(file_handle) or []
    if not isinstance(panels, list):
        raise ValueError(f"Statusboard config {config_path} must be a YAML list of panels")
    for panel in panels:
        _validate_panel(panel, config_path)
        panel.setdefault("interval", DEFAULT_INTERVALS[panel["type"]])
    return panels


def _validate_panel(panel, config_path):
    if not isinstance(panel, dict) or "name" not in panel or "type" not in panel:
        raise ValueError(f"Statusboard panel must be a mapping with 'name' and 'type' keys ({config_path}): {panel}")
    if panel["type"] not in PANEL_TYPES:
        raise ValueError(
            f"Statusboard panel '{panel['name']}' in {config_path} has unknown type '{panel['type']}' "
            f"(expected one of {', '.join(PANEL_TYPES)})"
        )
    required = {
        "ssh_command": ("host", "command"),
        "github_prs": ("token_env",),
        "bitbucket_prs": ("workspace", "username_env", "app_password_env"),
        "http_checks": ("sites",),
        "claude_usage": (),
    }[panel["type"]]
    missing = [key for key in required if not panel.get(key)]
    if panel.get("host_stats"):
        if panel["type"] != "ssh_command":
            raise ValueError(
                f"Statusboard panel '{panel['name']}' in {config_path}: "
                f"host_stats is only supported on ssh_command panels"
            )
        # a stats-only panel is valid: host_stats stands in for the command
        missing = [key for key in missing if key != "command"]
    if missing:
        raise ValueError(
            f"Statusboard panel '{panel['name']}' in {config_path} "
            f"(type {panel['type']}) is missing required keys: {', '.join(missing)}"
        )
    if panel.get("log_link"):
        _validate_log_link(panel, config_path)
    if panel["type"] == "http_checks":
        _validate_sites(panel, config_path)
    if panel["type"] == "claude_usage" and panel.get("jump") and not panel.get("host"):
        raise ValueError(f"Statusboard panel '{panel['name']}' in {config_path}: jump needs a host to hop to")


def _validate_sites(panel, config_path):
    """
    ``sites`` is a non-empty list of ``{url, name?, expect?, insecure?}``
    mappings: ``expect`` is the status code (or list of codes) that counts as
    up (default: any 2xx/3xx), ``insecure`` skips certificate verification
    for self-signed internal hosts. An optional ``host``/``jump`` pair moves
    the probe to that machine (through the usual ssh chain) - a jump without
    a host has nothing to hop to.
    """
    prefix = f"Statusboard panel '{panel['name']}' in {config_path}"
    sites = panel["sites"]
    if not isinstance(sites, list) or not sites:
        raise ValueError(f"{prefix}: sites must be a non-empty list of {{url, name, expect, insecure}} mappings")
    for site in sites:
        if not isinstance(site, dict) or not site.get("url"):
            raise ValueError(f"{prefix}: every site needs a url: {site}")
        if not re.match(r"https?://", str(site["url"])):
            raise ValueError(f"{prefix}: site url must start with http:// or https://: {site['url']}")
        expect = site.get("expect")
        if expect is not None:
            codes = expect if isinstance(expect, list) else [expect]
            if not codes or not all(isinstance(code, int) and not isinstance(code, bool) for code in codes):
                raise ValueError(f"{prefix}: site expect must be a status code or list of codes: {site['url']}")
    if panel.get("jump") and not panel.get("host"):
        raise ValueError(f"{prefix}: jump needs a host to hop to")


def _validate_log_link(panel, config_path):
    """
    ``log_link`` makes an ssh_command panel's output rows clickable: ``pattern``
    is a regex whose first capture group pulls a job token out of each output
    line, and ``command`` is the remote command (with ``{job}`` substituted)
    the TUI streams in a follow pane when the row is clicked.
    """
    prefix = f"Statusboard panel '{panel['name']}' in {config_path}"
    if panel["type"] != "ssh_command":
        raise ValueError(f"{prefix}: log_link is only supported on ssh_command panels")
    log_link = panel["log_link"]
    if not isinstance(log_link, dict) or not log_link.get("pattern") or not log_link.get("command"):
        raise ValueError(f"{prefix}: log_link must be a mapping with 'pattern' and 'command' keys")
    try:
        compiled = re.compile(log_link["pattern"])
    except re.error as error:
        raise ValueError(f"{prefix}: log_link pattern does not compile: {error}")
    if compiled.groups < 1:
        raise ValueError(f"{prefix}: log_link pattern needs a capture group (the job token)")
    if "{job}" not in log_link["command"]:
        raise ValueError(f"{prefix}: log_link command must contain a {{job}} placeholder")


# %%
# Host inventory #


def find_host(token, base_dir, credentials_root):
    """
    Resolve a panel's host token (inventory ``name`` or one of its
    ``aliases``, case-insensitive) to its full inventory record. The config's
    own credentials repo inventory is searched first, then every other sibling
    inventory, so a panel travels with the repo that knows its hosts but can
    still reference machines declared elsewhere.
    """
    inventory_paths = []
    for filename in (f"{credentials_context(base_dir)}_hosts.json", "hosts.json"):
        path = os.path.join(base_dir, filename)
        if os.path.exists(path):
            inventory_paths.append(path)
            break
    inventory_paths += [path for path in find_inventory_paths(credentials_root) if path not in inventory_paths]
    return find_host_record(token, inventory_paths)


def panel_command(panel):
    """
    The remote command an ssh_command panel actually runs: its own command,
    the host-stats one-liner, or both - stats separated from the command's
    output by a blank line so the board reads as output-then-footer.
    """
    if not panel.get("host_stats"):
        return panel["command"]
    if not panel.get("command"):
        return HOST_STATS_COMMAND
    return f"{panel['command']}; echo; {HOST_STATS_COMMAND}"


def build_ssh_argv(panel, credentials_root, local_hostname="", command=None):
    """
    Build the full argv for an ssh_command panel, resolving ``host`` and the
    optional ``jump`` hop from the host inventories. ``command`` overrides
    the panel's own command (same host/hop chain) - used by the log-follow
    pane to stream a tail over the connection the panel already defines.

    The chain semantics (jump-hop injection, skip-the-hop-when-this-machine-
    IS-the-jump, identity_file/port support, and running the command locally
    when the target IS this machine) live in readable_utils.ssh_tools, shared
    with herdstone.
    """
    target = find_host(panel["host"], panel["_base_dir"], credentials_root)
    jump = find_host(panel["jump"], panel["_base_dir"], credentials_root) if panel.get("jump") else None
    return build_host_ssh_argv(
        target, command or panel_command(panel), jump=jump, local_hostname=local_hostname
    )


# %%
# Secrets #


def resolve_secret(panel, key):
    """
    Look up the env var named by panel[key]: the real environment wins, then
    the panel's optional ``env_file`` (path relative to the panel's config
    repo - so tokens live in the gitignored/private env files, never in the
    statusboard configs themselves).
    """
    var_name = panel[key]
    value = os.environ.get(var_name)
    if value:
        return value
    env_file = panel.get("env_file")
    if env_file:
        env_path = os.path.join(panel["_base_dir"], os.path.expanduser(env_file))
        if not os.path.exists(env_path):
            raise ValueError(f"'{panel['name']}': env_file {env_path} does not exist")
        value = _parse_env_file(env_path).get(var_name)
        if value:
            return value
    raise ValueError(
        f"'{panel['name']}': env var {var_name} is not set"
        + (f" and not found in {panel['env_file']}" if panel.get("env_file") else " (no env_file configured)")
    )


def _parse_env_file(path):
    """KEY=value lines (optional ``export``, quotes stripped); comments and non-kv lines ignored."""
    values = {}
    with open(path, "r", encoding="utf-8") as file_handle:
        for line in file_handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.replace("export ", "", 1).strip()
            value = value.strip().strip("'\"")
            if key:
                values[key] = value
    return values


# %%
# Fetchers #


def fetch_ssh_command(panel, credentials_root, local_hostname=""):
    """Run the panel's command over ssh (through the jump hop if configured) and capture its output."""
    argv = build_ssh_argv(panel, credentials_root, local_hostname)
    timeout = panel.get("timeout", DEFAULT_SSH_TIMEOUT)
    try:
        completed = subprocess.run(
            # Explicit utf-8, not text=True: the remote output is utf-8 (the
            # board glyphs), and on Windows text mode decodes with the ANSI
            # codepage - the decode error kills subprocess's reader thread and
            # stdout comes back None even on a successful run.
            argv,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return PanelResult.error(f"ssh timed out after {timeout}s: {' '.join(argv[:-1])}")
    except FileNotFoundError:
        return PanelResult.error("ssh not found on PATH")
    if completed.returncode != 0 and not completed.stdout.strip():
        detail = completed.stderr.strip() or f"ssh exited {completed.returncode}"
        return PanelResult.error(detail)
    output = completed.stdout.rstrip()
    stats = None
    if panel.get("host_stats"):
        output, stats = _split_host_stats(output)
    return PanelResult(True, "ansi", output, f"exit {completed.returncode}", stats=stats)


def curl_argv(site, max_time, devnull=os.devnull):
    """
    The curl argv that probes one site: a plain GET with the body discarded,
    no redirect following (a 3xx to a login page means the site IS up), a
    hard per-request deadline, and one machine-readable trailer line
    (``==CHECK== <http_code> <time_total>``) the parser reads back. Errors
    stay on stderr as curl's own ``curl: (N) reason`` line, which the caller
    merges into the same stream. ``devnull`` is the probing host's null
    device (``/dev/null`` when the probe runs over ssh on a POSIX host).
    """
    argv = ["curl", "-sS", "-o", devnull, "--max-time", str(max_time)]
    if site.get("insecure"):
        argv.append("-k")
    argv += ["-w", f"{HTTP_CHECK_MARKER} %{{http_code}} %{{time_total}}\\n", str(site["url"])]
    return argv


def http_checks_command(panel):
    """
    One POSIX sh command line that probes every site of an http_checks panel
    from a remote host: each site's curl, prefixed with a ``==SITE== <index>``
    line so the output splits back into per-site blocks. Same curl flags as
    the local probe, so the two vantage points are parsed and rendered
    identically. Requires curl and a POSIX shell on the host (Linux/macOS).
    """
    max_time = panel.get("max_time", DEFAULT_PROBE_TIMEOUT)
    parts = []
    for index, site in enumerate(panel["sites"]):
        argv = curl_argv(site, max_time, devnull="/dev/null")
        parts.append(f"echo '{SITE_MARKER} {index}'; {shlex.join(argv)} 2>&1")
    return "; ".join(parts)


def split_site_blocks(output):
    """Remote probe output -> {site_index: that site's curl output}, keyed by the ==SITE== lines."""
    blocks: dict = {}
    current = None
    for line in output.splitlines():
        if line.startswith(SITE_MARKER):
            try:
                current = int(line.split()[1])
            except (IndexError, ValueError):
                current = None
                continue
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    return {index: "\n".join(lines) for index, lines in blocks.items()}


def parse_http_check(output):
    """
    One site's merged curl output -> ``{"code", "seconds", "error"}``.
    ``code`` is the HTTP status (0 when no response was received - curl still
    prints its -w trailer on failure), ``error`` curl's own reason line minus
    the ``curl:`` prefix, or a generic one when curl printed nothing at all.
    """
    code, seconds, error = None, None, None
    for line in output.splitlines():
        if line.startswith(HTTP_CHECK_MARKER):
            fields = line.split()
            try:
                code, seconds = int(fields[1]), float(fields[2])
            except (IndexError, ValueError):
                continue
        elif line.startswith("curl:"):
            error = line[len("curl:"):].strip()
    if code is None and error is None:
        error = "no response from curl"
    return {"code": code or 0, "seconds": seconds, "error": error}


def site_is_up(site, check):
    """A site is up when it answered with an expected code (any 2xx/3xx unless ``expect`` narrows it)."""
    code = check["code"]
    if not code:
        return False
    expect = site.get("expect")
    if expect is None:
        return 200 <= code < 400
    return code in (expect if isinstance(expect, list) else [expect])


def site_label(site):
    """The row text for a site: its name, else the url's host (and port)."""
    if site.get("name"):
        return str(site["name"])
    url = str(site["url"])
    return re.sub(r"^https?://", "", url).split("/", 1)[0] or url


def http_check_rows(sites, checks):
    """
    Turn per-site probe results into link rows (click opens the site) and a
    summary. Returns (rows, summary, down_count). The ``tail`` is the bare
    status; the renderer pads it into alignment per grid column.
    """
    rows, down = [], 0
    for site, check in zip(sites, checks):
        if site_is_up(site, check):
            badge, badge_style, tail_style = "✓", "bold green", "dim"
            status = f"{check['code']} · {check['seconds'] * 1000:.0f}ms"
        else:
            down += 1
            badge, badge_style, tail_style = "✗", "bold red", "red"
            reason = check["error"] or f"unexpected status {check['code']}"
            status = f"DOWN · {check['code']} · {reason}" if check["code"] else f"DOWN · {reason}"
        rows.append({
            "badge": badge,
            "badge_style": badge_style,
            "text": site_label(site),
            "url": str(site["url"]),
            "tail": status,
            "tail_style": tail_style,
            "dim": False,
        })
    up = len(sites) - down
    summary = f"all {up} up" if not down else f"{up} up · {down} DOWN"
    return rows, summary, down


def _run_local_http_checks(sites, max_time):
    """Probe every site from this machine, concurrently - one curl process per site, no shell."""
    def probe(site):
        argv = curl_argv(site, max_time)
        try:
            completed = subprocess.run(
                argv, capture_output=True, encoding="utf-8", errors="replace",
                timeout=max_time + 5, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return f"curl: (28) no answer within {max_time}s"
        return f"{completed.stdout}\n{completed.stderr}"

    with ThreadPoolExecutor(max_workers=min(8, len(sites))) as pool:
        return dict(enumerate(pool.map(probe, sites)))


def fetch_http_checks(panel, credentials_root, local_hostname=""):
    """
    Probe each site with curl and report up/down per site. With ``host`` set
    the probes run on that machine over the same ssh chain an ssh_command
    panel uses (jump hop included) - the way to watch sites that are only
    reachable from inside a network, from wherever the board happens to run.
    When the chain resolves the host to THIS machine (the board is running
    on the vantage host) or no host is set, curl runs locally per site - no
    shell involved, so a Windows board works too.
    """
    sites = panel["sites"]
    max_time = panel.get("max_time", DEFAULT_PROBE_TIMEOUT)
    argv = None
    if panel.get("host"):
        argv = build_ssh_argv(panel, credentials_root, local_hostname, command=http_checks_command(panel))
    if argv is None or argv[0] != "ssh":
        try:
            outputs = _run_local_http_checks(sites, max_time)
        except FileNotFoundError:
            return PanelResult.error("curl not found on PATH")
    else:
        timeout = panel.get("timeout", DEFAULT_SSH_TIMEOUT)
        try:
            completed = subprocess.run(
                argv, capture_output=True, encoding="utf-8", errors="replace",
                timeout=timeout, stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return PanelResult.error(f"ssh timed out after {timeout}s: {' '.join(argv[:-1])}")
        except FileNotFoundError:
            return PanelResult.error("ssh not found on PATH")
        # the script's exit status is the LAST curl's, so a down site makes
        # ssh exit non-zero with the probe output intact - only an empty
        # stdout means the chain itself failed
        if not completed.stdout.strip():
            return PanelResult.error(completed.stderr.strip() or f"ssh exited {completed.returncode}")
        outputs = split_site_blocks(completed.stdout)
    checks = [parse_http_check(outputs.get(index, "")) for index in range(len(sites))]
    rows, summary, down = http_check_rows(sites, checks)
    if panel.get("host"):
        summary += f" · from {panel['host']}"
    return PanelResult(True, "sites", rows, summary, alert=down > 0)


def run_claude_usage_probe(panel, credentials_root, local_hostname=""):
    """
    Pipe the probe script to python3 on the panel's host over the usual ssh
    chain (jump hop included) and parse the one JSON line it prints. With no
    host, or when the chain resolves the host to THIS machine, the board's
    own interpreter runs the probe - no shell, so a Windows board works too.
    Returns (report, error), error None on success.
    """
    argv = None
    if panel.get("host"):
        argv = build_ssh_argv(panel, credentials_root, local_hostname, command="python3 -")
    if argv is None or argv[0] != "ssh":
        argv = [sys.executable, "-"]
    with open(CLAUDE_USAGE_PROBE, "r", encoding="utf-8") as file_handle:
        script = file_handle.read()
    timeout = panel.get("timeout", DEFAULT_SSH_TIMEOUT)
    try:
        completed = subprocess.run(
            argv, input=script, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return None, f"claude usage probe timed out after {timeout}s"
    except FileNotFoundError:
        return None, f"{argv[0]} not found on PATH"
    lines = completed.stdout.strip().splitlines()
    try:
        return json.loads(lines[-1]), None
    except (IndexError, ValueError):
        return None, completed.stderr.strip() or completed.stdout.strip() or f"probe exited {completed.returncode}"


def claude_usage_limits(usage):
    """
    The plan limits in a usage-endpoint response, as rows of
    ``{"label", "percent", "resets_at", "severity"}`` (resets_at an aware
    datetime or None). The response's ``limits`` array is authoritative - the
    same rows the desktop app's Usage page draws: session, weekly_all and one
    weekly_scoped row per model or surface with its own cap. The top-level
    five_hour / seven_day dicts carry the same numbers and are only read when
    a response has no limits array. Every other top-level key (the codenamed
    ones) is a feature flag and ignored.
    """
    rows = []
    for limit in usage.get("limits") or []:
        kind = limit.get("kind") or limit.get("group") or "limit"
        scope = limit.get("scope") or {}
        if kind == "session":
            label = "session"
        elif kind == "weekly_all":
            label = "weekly"
        elif kind == "weekly_scoped":
            label = f"weekly {(scope.get('model') or {}).get('display_name') or scope.get('surface') or 'scoped'}"
        else:
            label = kind.replace("_", " ")
        rows.append({
            "label": label,
            "percent": float(limit.get("percent") or 0),
            "resets_at": _parse_reset(limit.get("resets_at")),
            "severity": limit.get("severity"),
        })
    if rows:
        return rows
    for key, label in (("five_hour", "session"), ("seven_day", "weekly")):
        window = usage.get(key)
        if isinstance(window, dict) and window.get("utilization") is not None:
            rows.append({
                "label": label,
                "percent": float(window["utilization"]),
                "resets_at": _parse_reset(window.get("resets_at")),
                "severity": None,
            })
    return rows


def claude_spend(usage):
    """
    The usage-credits row (``{"label", "percent", "used", "limit"}``, money
    already formatted) when the account has credits switched on, else None.
    UNVERIFIED beyond a disabled personal account: the amounts are read as
    minor units scaled by their exponent, the way the disabled block reports
    its zero.
    """
    spend = usage.get("spend") or {}
    if not spend.get("enabled"):
        return None

    def money(amount):
        if not isinstance(amount, dict) or amount.get("amount_minor") is None:
            return None
        value = amount["amount_minor"] / 10 ** int(amount.get("exponent") or 0)
        return f"{value:,.2f} {amount.get('currency') or ''}".strip()

    return {
        "label": "credits",
        "percent": float(spend.get("percent") or 0),
        "used": money(spend.get("used")),
        "limit": money(spend.get("limit")),
    }


def _parse_reset(value):
    """resets_at arrives as ISO-8601 (maybe with a trailing Z) or epoch seconds; None when absent."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def compact_count(value):
    """Token counts for the board: 874, 16.5k, 841k, 7.2M, 365M, 1.3B."""
    for size, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if value >= size:
            scaled = value / size
            return f"{scaled:.1f}{suffix}" if scaled < 100 else f"{scaled:.0f}{suffix}"
    return str(int(value))


def claude_plan_label(report):
    """claude.ai plan for the panel: the subscription, plus the rate-limit tier when it says more (max 20x)."""
    plan = report.get("subscription") or "claude.ai"
    tier = (report.get("tier") or "").replace("default_claude_", "")
    if tier and tier != plan and tier.startswith(plan):
        plan = tier.replace("_", " ")
    return plan


def fetch_claude_usage(panel, credentials_root, local_hostname=""):
    """
    Claude usage for whatever connection the host's Claude Code is configured
    for (see claude_usage_probe.py): plan limits with what is left and when
    each resets on a claude.ai subscription, or this host's token tally on
    Bedrock, which has no allowance. The probe only reads - it never
    refreshes a token - so a stale login shows as an error until claude next
    runs there.
    """
    report, error = run_claude_usage_probe(panel, credentials_root, local_hostname)
    if error:
        return PanelResult.error(error)
    if report.get("error"):
        where = f" on {report['host']}" if report.get("host") else ""
        plan = claude_plan_label(report) if report.get("mode") == "subscription" else report.get("mode")
        return PanelResult.error(f"{plan}{where}: {report['error']}")
    if report["mode"] == "bedrock":
        today = report["tokens"]["today"]
        summary = f"bedrock · {compact_count(today['output'])} out today"
        return PanelResult(True, "claude_usage", report, summary)
    limits = claude_usage_limits(report.get("usage") or {})
    summary = claude_plan_label(report)
    if limits:
        top = max(limits, key=lambda row: row["percent"])
        summary += f" · {top['label']} {top['percent']:.0f}% used"
    return PanelResult(True, "claude_usage", report, summary, alert=any(row["percent"] >= 100 for row in limits))


def fetch_github_prs(panel):
    """
    Every open PR the token's account is waiting on or waited for, across all
    repos the token can see (search-wide - no per-repo config): PRs whose
    review is requested, PRs already reviewed (badged "waiting on author" when
    the account's latest review requested changes and nothing was pushed
    since), and the account's own open PRs with their aggregate review status.
    """
    token = resolve_secret(panel, "token_env")
    api = panel.get("api_url", DEFAULT_GITHUB_API).rstrip("/")
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    login, login_error = _github_login(api, headers)
    if login_error:
        return PanelResult.error(login_error)

    # A fine-grained token only surfaces repos it was granted, so a panel's
    # scope is primarily the TOKEN's repo selection; the optional ``search``
    # qualifiers refine on top (e.g. -repo:owner/name to keep two panels of
    # the same account from overlapping when one token sees everything).
    def search(qualifiers):
        query = f"is:open is:pr archived:false {qualifiers} {panel.get('search', '')}".strip()
        response = requests.get(
            f"{api}/search/issues",
            params={"q": query, "sort": "updated", "per_page": 50},
            headers=headers,
            timeout=DEFAULT_HTTP_TIMEOUT,
        )
        if response.status_code != 200:
            raise ValueError(f"GitHub search returned {response.status_code}: {response.text[:200]}")
        return response.json().get("items", [])

    requested = search(f"review-requested:{login}")
    reviewed = search(f"reviewed-by:{login} -author:{login}")
    mine = search(f"author:{login}")
    review_states, review_commits = {}, {}
    for item in {i["html_url"]: i for i in requested + reviewed + mine}.values():
        states, commits = _pr_review_states(api, headers, item)
        review_states[item["html_url"]] = states
        review_commits[item["html_url"]] = commits
    # Two signals unstick a changes-requested PR, probed with one PR-detail
    # call each (only for that subset, to keep the extra API calls rare):
    # appearing in requested_reviewers again means the author explicitly
    # re-requested me (submitting a review clears me from that list, and a
    # team request matches the search but not the list); a head sha that
    # moved past the sha my review was submitted against means the author
    # pushed since - GitHub keeps my blocking review active across pushes,
    # so without this the PR would sit at "you requested changes" forever.
    rerequested, updated_since = set(), set()
    requested_urls = {i["html_url"] for i in requested}
    for item in {i["html_url"]: i for i in requested + reviewed}.values():
        url = item["html_url"]
        if review_states.get(url, {}).get(login) != "CHANGES_REQUESTED":
            continue
        repo = item["repository_url"].split("/repos/", 1)[-1]
        response = requests.get(
            f"{api}/repos/{repo}/pulls/{item['number']}", headers=headers, timeout=DEFAULT_HTTP_TIMEOUT
        )
        if response.status_code != 200:
            continue
        detail = response.json()
        reviewers = [user.get("login") for user in detail.get("requested_reviewers", [])]
        head_sha = (detail.get("head") or {}).get("sha")
        reviewed_sha = review_commits.get(url, {}).get(login)
        if url in requested_urls and login in reviewers:
            rerequested.add(url)
        elif head_sha and reviewed_sha and head_sha != reviewed_sha:
            updated_since.add(url)
    rows, summary = classify_github_prs(
        login, requested, reviewed, mine, review_states, rerequested, updated_since
    )

    # SAML orgs silently drop their repos from search results (a plain 200
    # with fewer items, no marker header) until the token is SSO-authorized -
    # probe each org the account belongs to, or an unauthorized token looks
    # like an empty review queue forever.
    blocked = _github_sso_blocked_orgs(api, headers)
    if blocked:
        return PanelResult.error(
            f"search results exclude SAML org(s) {', '.join(blocked)} - authorize the PAT "
            f"(github.com -> Settings -> Developer settings -> your token -> Configure SSO); "
            f"{len(rows)} PRs visible without them ({login})"
        )
    return PanelResult(True, "links", rows, f"{summary} ({login})")


def _github_login(api, headers):
    """
    The account behind the token, asking the token itself - no login in any
    panel config. REST /user stays the primary probe (its status code is the
    only thing that tells a dead token apart from a dead endpoint), with
    GraphQL's ``viewer`` as fallback: a separate backend that kept serving the
    login through a /user 503 while search and the PR endpoints - everything
    else this panel needs - stayed healthy. Returns (login, error), error None
    on success.
    """
    user_response = requests.get(f"{api}/user", headers=headers, timeout=DEFAULT_HTTP_TIMEOUT)
    if user_response.status_code == 200:
        return user_response.json()["login"], None
    code = user_response.status_code
    # A revoked or expired token fails every endpoint alike, so don't bother
    # GraphQL - and keep saying "token" only for the codes that mean it.
    if code in (401, 403):
        return None, f"GitHub /user returned {code} (bad/expired token?)"
    # GitHub Enterprise splits the two APIs as /api/v3 and /api/graphql;
    # github.com hangs GraphQL off the same api.github.com host.
    graphql_url = f"{api[: -len('/api/v3')]}/api/graphql" if api.endswith("/api/v3") else f"{api}/graphql"
    viewer_response = requests.post(
        graphql_url, json={"query": "{viewer{login}}"}, headers=headers, timeout=DEFAULT_HTTP_TIMEOUT
    )
    if viewer_response.status_code == 200:
        viewer = ((viewer_response.json().get("data") or {}).get("viewer") or {}).get("login")
        if viewer:
            return viewer, None
    hint = " (GitHub-side, not your token)" if code >= 500 else ""
    return None, (
        f"GitHub /user returned {code}{hint}; GraphQL viewer returned {viewer_response.status_code}"
    )


def _pr_review_states(api, headers, item):
    """
    Latest submitted review state per reviewer login for one search item
    (chronological walk, so later reviews overwrite earlier ones; a DISMISSED
    review resets that reviewer to no active state). Returns
    (states, commit_ids): commit_ids maps each active reviewer to the head
    sha their latest review was submitted against, so callers can tell
    whether the branch has moved since.
    """
    repo = item["repository_url"].split("/repos/", 1)[-1]
    response = requests.get(
        f"{api}/repos/{repo}/pulls/{item['number']}/reviews",
        params={"per_page": 100},
        headers=headers,
        timeout=DEFAULT_HTTP_TIMEOUT,
    )
    if response.status_code != 200:
        return {}, {}
    states: dict = {}
    commits: dict = {}
    for review in response.json():
        user = (review.get("user") or {}).get("login")
        state = review.get("state")
        # PENDING = the caller's own unsubmitted draft (the API never shows
        # anyone else's) - kept so the board can flag it: the author cannot
        # see a draft, so it reads as "no review yet" to everyone else.
        if not user or state not in ("APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED", "PENDING"):
            continue
        states[user] = None if state == "DISMISSED" else state
        commits[user] = review.get("commit_id")
    active = {user: state for user, state in states.items() if state}
    return active, {user: commits.get(user) for user in active}


def classify_github_prs(login, requested, reviewed, mine, review_states, rerequested=frozenset(),
                        updated_since=frozenset()):
    """
    Turn the three searches into ordered, badged link rows. Returns
    (rows, summary). ``rerequested`` is the set of PR urls where I appear in
    requested_reviewers DESPITE having a changes-requested review - only an
    explicit re-request by the author puts me back there, so those PRs return
    to "needs my review". ``updated_since`` is the set of PR urls whose head
    moved past the sha my changes-requested review was submitted against -
    the author pushed something (maybe an unrelated merge, but a second look
    beats never seeing it again), so those also return to "needs my review";
    an explicit re-request wins when both signals are present. Buckets, in
    display order:

    - my UNSUBMITTED draft review (state PENDING): flagged loudest - a draft
      is invisible to the author, so until I hit "Submit review" nobody knows
      I responded and everyone is waiting on everyone;
    - "changes requested" by me, not re-requested (a team-level request
      matches the search without putting me back personally) and nothing
      pushed since: waiting on the AUTHOR, not on me;
    - review requested and I have not blocked it - including re-requests
      and new pushes after my changes-requested review, marked so I know
      it's round two;
    - reviewed with only a comment and no pending request: soft state, shown
      so it isn't forgotten;
    - my own open PRs, with the aggregate verdict of everyone else's reviews;
    - DRAFT PRs - anyone's, mine included - greyed at the very bottom:
      parked on purpose, nothing for anyone to approve yet.

    Approved-by-me PRs with no new request are dropped - nothing is waited on.
    """
    drafts, need, waiting, commented, own, parked = [], [], [], [], [], []
    seen = set()
    for item in requested + reviewed:
        if item["html_url"] in seen:
            continue
        seen.add(item["html_url"])
        if item.get("draft"):
            parked.append(_github_row(item, "◌", "dim", "draft · parked, nothing to approve", dim=True))
            continue
        my_state = review_states.get(item["html_url"], {}).get(login)
        is_requested = any(i["html_url"] == item["html_url"] for i in requested)
        if my_state == "PENDING":
            drafts.append(_github_row(item, "✏", "bold red", "UNSUBMITTED draft review - the author can't see it"))
        elif my_state == "CHANGES_REQUESTED" and item["html_url"] in rerequested:
            need.append(_github_row(item, "●", "bold cyan", "re-requested after your changes"))
        elif my_state == "CHANGES_REQUESTED" and item["html_url"] in updated_since:
            need.append(_github_row(item, "●", "bold cyan", "updated since your changes - re-review?"))
        elif my_state == "CHANGES_REQUESTED":
            waiting.append(_github_row(item, "✋", "bold yellow", "you requested changes"))
        elif is_requested:
            need.append(_github_row(item, "●", "bold cyan", None))
        elif my_state == "COMMENTED":
            commented.append(_github_row(item, "💬", "dim", "you commented"))
    for item in mine:
        if item.get("draft"):
            parked.append(_github_row(item, "◌", "dim", "your draft · parked, nothing to approve", dim=True))
            continue
        others = {u: s for u, s in review_states.get(item["html_url"], {}).items() if u != login}
        own.append(_github_row(item, "⬆", "bold magenta", f"your PR · {_own_pr_verdict(others)}"))
    rows = drafts + need + waiting + commented + own + parked
    summary = f"{len(need)} to review · {len(waiting)} on author · {len(own)} yours"
    if drafts:
        summary = f"{len(drafts)} UNSUBMITTED · {summary}"
    if parked:
        summary += f" · {len(parked)} parked"
    return rows, summary


def _own_pr_verdict(others):
    """Aggregate verdict of everyone else's reviews on one of my own PRs."""
    if "CHANGES_REQUESTED" in others.values():
        return "✗ changes requested"
    if "APPROVED" in others.values():
        return "✓ approved"
    return "⧗ awaiting review"


def _github_row(item, badge, badge_style, note, dim=False):
    repo = item["repository_url"].split("/repos/", 1)[-1]
    meta = f"by {item['user']['login']} · updated {_age(item['updated_at'])} ago"
    if note:
        meta += f" · {note}"
    return {
        "badge": badge,
        "badge_style": badge_style,
        "text": f"{repo}#{item['number']}  {item['title']}",
        "url": item["html_url"],
        "meta": meta,
        "dim": dim,
    }


def _github_sso_blocked_orgs(api, headers):
    """
    Org logins whose SAML SSO blocks this token. GitHub returns the account's
    org memberships regardless, but a direct org-resource request 403s with an
    ``X-GitHub-SSO: required`` header until the token is authorized - the only
    reliable signal, since filtered search responses carry no marker.
    """
    orgs_response = requests.get(f"{api}/user/orgs", headers=headers, timeout=DEFAULT_HTTP_TIMEOUT)
    if orgs_response.status_code != 200:
        return []
    blocked = []
    for org in orgs_response.json():
        probe = requests.get(
            f"{api}/orgs/{org['login']}/repos", params={"per_page": 1}, headers=headers,
            timeout=DEFAULT_HTTP_TIMEOUT,
        )
        if probe.status_code == 403 and probe.headers.get("X-GitHub-SSO", "").startswith("required"):
            blocked.append(org["login"])
    return blocked


def fetch_bitbucket_prs(panel):
    """
    Open PRs that involve the app-password's account - either as a reviewer or
    as the AUTHOR (the github_prs panel shows both, so this one must too) -
    across the panel's ``repos`` list (or every repo in ``workspace`` when
    omitted - slower, one API call per repo page).

    Involvement is decided CLIENT-SIDE from the plain ``state="OPEN"`` listing
    rather than a ``q=author.uuid/reviewers.uuid`` filter: Bitbucket's PR search
    index lags and mishandles those fields (an ``author.uuid`` match flips
    between 1 and 0 across seconds, and OR-ing it with ``reviewers.uuid`` drops
    both halves to 0), so a server-side filter silently hides your own PRs. The
    unfiltered listing is consistent. ``participants``/``reviewers`` are not in
    the list endpoint's default serialization, so they are requested
    explicitly - ``reviewers`` to tell whether I'm a requested reviewer,
    ``participants`` for who has already voted.
    """
    auth = (resolve_secret(panel, "username_env"), resolve_secret(panel, "app_password_env"))
    user_response = requests.get(f"{BITBUCKET_API}/user", auth=auth, timeout=DEFAULT_HTTP_TIMEOUT)
    if user_response.status_code != 200:
        return PanelResult.error(f"Bitbucket /user returned {user_response.status_code} (bad app password?)")
    uuid = user_response.json()["uuid"]
    workspace = panel["workspace"]
    repos = panel.get("repos") or _bitbucket_workspace_repos(workspace, auth)
    found = []
    for repo in repos:
        url = f"{BITBUCKET_API}/repositories/{workspace}/{repo}/pullrequests"
        params = {
            "state": "OPEN",
            "pagelen": 50,
            "fields": "+values.participants,+values.reviewers,+values.draft",
        }
        response = requests.get(url, params=params, auth=auth, timeout=DEFAULT_HTTP_TIMEOUT)
        if response.status_code != 200:
            return PanelResult.error(f"Bitbucket {workspace}/{repo} returned {response.status_code}")
        for pr in response.json().get("values", []):
            if _bitbucket_involves(uuid, pr):
                found.append((repo, pr))
    rows, summary = classify_bitbucket_prs(uuid, found)
    return PanelResult(True, "links", rows, f"{summary} ({workspace})")


def _bitbucket_involves(uuid, pr):
    """True when I authored the PR or am one of its requested reviewers."""
    if (pr.get("author") or {}).get("uuid") == uuid:
        return True
    return any(reviewer.get("uuid") == uuid for reviewer in pr.get("reviewers") or [])


def classify_bitbucket_prs(uuid, found):
    """
    Turn (repo_slug, pr) pairs into ordered, badged link rows - the Bitbucket
    counterpart of classify_github_prs, using the same badges so both panels
    read the same way. Buckets, in display order:

    - review requested and I have not voted: needs my review;
    - I requested changes: waiting on the AUTHOR, not on me;
    - my own open PRs, with the aggregate verdict of everyone else's votes;
    - draft PRs - anyone's, mine included - greyed at the bottom.

    PRs I already approved are dropped (nothing is waited on). Bitbucket has
    no "commented" participant state, so there is no equivalent of the GitHub
    💬 bucket, and no unsubmitted-draft-review state to flag.
    """
    need, waiting, own, parked = [], [], [], []
    for repo, pr in found:
        author_uuid = (pr.get("author") or {}).get("uuid")
        participants = pr.get("participants") or []
        mine = author_uuid == uuid
        if pr.get("draft"):
            note = "your draft · parked, nothing to approve" if mine else "draft · parked, nothing to approve"
            parked.append(_bitbucket_row(repo, pr, "◌", "dim", note, dim=True))
            continue
        if mine:
            others = [p for p in participants if (p.get("user") or {}).get("uuid") != uuid]
            own.append(_bitbucket_row(repo, pr, "⬆", "bold magenta", f"your PR · {_bitbucket_verdict(others)}"))
            continue
        my_state = next(
            (p.get("state") for p in participants if (p.get("user") or {}).get("uuid") == uuid), None
        )
        if my_state == "approved":
            continue
        if my_state == "changes_requested":
            waiting.append(_bitbucket_row(repo, pr, "✋", "bold yellow", "you requested changes"))
        else:
            need.append(_bitbucket_row(repo, pr, "●", "bold cyan", None))
    rows = need + waiting + own + parked
    summary = f"{len(need)} to review · {len(waiting)} on author · {len(own)} yours"
    if parked:
        summary += f" · {len(parked)} parked"
    return rows, summary


def _bitbucket_verdict(others):
    """Aggregate verdict of everyone else's votes on one of my own PRs."""
    states = [participant.get("state") for participant in others]
    if "changes_requested" in states:
        return "✗ changes requested"
    if "approved" in states:
        return "✓ approved"
    return "⧗ awaiting review"


def _bitbucket_row(repo, pr, badge, badge_style, note, dim=False):
    meta = f"by {pr['author']['display_name']} · updated {_age(pr['updated_on'])} ago"
    if note:
        meta += f" · {note}"
    return {
        "badge": badge,
        "badge_style": badge_style,
        "text": f"{repo}#{pr['id']}  {pr['title']}",
        "url": pr["links"]["html"]["href"],
        "meta": meta,
        "dim": dim,
    }


def _bitbucket_workspace_repos(workspace, auth):
    """Every repo slug in the workspace (paged)."""
    repos, url = [], f"{BITBUCKET_API}/repositories/{workspace}?pagelen=100&fields=next,values.slug"
    while url:
        response = requests.get(url, auth=auth, timeout=DEFAULT_HTTP_TIMEOUT)
        response.raise_for_status()
        payload = response.json()
        repos += [value["slug"] for value in payload.get("values", [])]
        url = payload.get("next")
    return repos


def fetch_panel(panel, credentials_root, local_hostname=""):
    """Dispatch one panel fetch; never raises - errors come back as PanelResult.error."""
    try:
        if panel["type"] == "ssh_command":
            return fetch_ssh_command(panel, credentials_root, local_hostname)
        if panel["type"] == "github_prs":
            return fetch_github_prs(panel)
        if panel["type"] == "http_checks":
            return fetch_http_checks(panel, credentials_root, local_hostname)
        if panel["type"] == "claude_usage":
            return fetch_claude_usage(panel, credentials_root, local_hostname)
        return fetch_bitbucket_prs(panel)
    except Exception as error:  # noqa: BLE001 - a panel must never take the board down
        return PanelResult.error(f"{type(error).__name__}: {error}")


def _age(iso_timestamp):
    """ISO timestamp -> compact age string ("5m", "3h", "2d")."""
    clean = iso_timestamp.replace("Z", "+00:00")
    try:
        seconds = int(time.time() - datetime.fromisoformat(clean).timestamp())
    except ValueError:
        return "?"
    if seconds < 3600:
        return f"{max(seconds, 0) // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


# %%
