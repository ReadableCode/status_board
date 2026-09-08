# %%
# Imports #

import os
import re

import yaml
from readable_utils.inventory_tools import credentials_context, find_credentials_dirs
from utils.statusboard_tools import (
    DEFAULT_INTERVALS,
    DEFAULT_PROBE_TIMEOUT,
    DEFAULT_SSH_TIMEOUT,
    PANEL_TYPES,
    _validate_panel,
    find_host,
    load_panels,
)

# %%
# Variables #

# Authoring order for wizard-written panels, so the generated YAML reads like
# the hand-written examples (name/type first, connection, then behavior).
FIELD_ORDER = (
    "name", "type", "host", "jump", "command", "host_stats", "log_link", "sites", "max_time",
    "token_env", "search", "workspace", "repos", "username_env", "app_password_env",
    "env_file", "browser", "interval", "timeout", "note",
)


# %%
# Pure helpers (unit-tested; no prompting) #


def wizard_targets(credentials_root, repo_root=None):
    """
    Every config file the wizard can write a panel into: the repo-root
    ``statusboard.yaml`` (tracked in the public repo - secrets-free panels
    only) plus ``<context>_statusboard.yaml`` in each sibling
    ``*_credentials`` repo, whether or not the file exists yet (the wizard
    creates missing ones). Returns dicts with path, base_dir, context, exists.
    """
    targets = []
    if repo_root:
        path = os.path.join(repo_root, "statusboard.yaml")
        targets.append({
            "path": path,
            "base_dir": repo_root,
            "context": os.path.basename(os.path.normpath(repo_root)),
            "exists": os.path.exists(path),
            "public": True,
        })
    for credentials_dir in find_credentials_dirs(credentials_root):
        context = credentials_context(credentials_dir)
        path = os.path.join(credentials_dir, f"{context}_statusboard.yaml")
        targets.append({
            "path": path,
            "base_dir": credentials_dir,
            "context": context,
            "exists": os.path.exists(path),
            "public": False,
        })
    return targets


def existing_panel_names(credentials_root, repo_root=None):
    """
    Panel names already taken across every discovered config (names must be
    unique board-wide). Configs that fail to parse contribute nothing - the
    wizard should still be usable to author panels next to a broken file.
    """
    try:
        panels, _ = load_panels(credentials_root, repo_root)
    except ValueError:
        return set()
    return {panel["name"] for panel in panels}


def format_panel_yaml(panel):
    """One panel as a YAML list-item block, keys in FIELD_ORDER authoring order."""
    ordered = {key: panel[key] for key in FIELD_ORDER if key in panel}
    ordered.update({key: value for key, value in panel.items() if key not in ordered and not key.startswith("_")})
    return yaml.safe_dump([ordered], sort_keys=False, allow_unicode=True, default_flow_style=False)


def append_panel(config_path, panel):
    """
    Append one panel to a statusboard config, creating the file (with a short
    header comment) when it doesn't exist yet. Appending - rather than
    re-dumping the whole document - preserves the hand-written formatting and
    comments of existing configs. Returns the YAML block that was written.
    """
    block = format_panel_yaml(panel)
    if os.path.exists(config_path):
        with open(config_path, "r", encoding="utf-8") as file_handle:
            existing = file_handle.read()
        prefix = "" if not existing or existing.endswith("\n") else "\n"
        with open(config_path, "a", encoding="utf-8") as file_handle:
            file_handle.write(f"{prefix}{block}")
    else:
        header = "# Status board panels - see the status_board repo README for the schema.\n"
        with open(config_path, "w", encoding="utf-8") as file_handle:
            file_handle.write(f"{header}{block}")
    return block


# %%
# Interactive wizard #


def _ask_optional(prompt_text, console, default=""):
    """Prompt for an optional field; empty answer means 'omit the key'."""
    from rich.prompt import Prompt

    value = Prompt.ask(prompt_text, default=default, console=console)
    return value.strip() if value else ""


def _prompt_host(label, base_dir, credentials_root, console, optional=False):
    """
    Prompt for a host token and check it resolves against the inventories,
    warning (but not blocking) when it doesn't - the inventory entry might be
    written right after the panel.
    """
    from rich.prompt import Prompt

    while True:
        token = Prompt.ask(label, default="" if optional else ..., console=console)
        token = (token or "").strip()
        if not token:
            if optional:
                return ""
            continue
        try:
            find_host(token, base_dir, credentials_root)
            return token
        except ValueError as error:
            console.print(f"[yellow]warning:[/yellow] {error}")
            from rich.prompt import Confirm

            if Confirm.ask("keep it anyway (add the inventory entry later)?", default=False, console=console):
                return token


def _prompt_ssh_fields(panel, target, credentials_root, console):
    from rich.prompt import Confirm, IntPrompt, Prompt

    panel["host"] = _prompt_host("host (inventory name or alias)", target["base_dir"], credentials_root, console)
    jump = _prompt_host(
        "jump hop (inventory name or alias, empty for none)",
        target["base_dir"], credentials_root, console, optional=True,
    )
    if jump:
        panel["jump"] = jump
    if Confirm.ask("append host_stats meters (disk/cpu/mem, Linux hosts only)?", default=False, console=console):
        panel["host_stats"] = True
    command = _ask_optional(
        "remote command" + (" (empty for a stats-only panel)" if panel.get("host_stats") else ""), console
    )
    if not command and not panel.get("host_stats"):
        while not command:
            command = Prompt.ask("remote command (required without host_stats)", console=console).strip()
    if command:
        panel["command"] = command
    timeout = IntPrompt.ask("ssh timeout (seconds)", default=DEFAULT_SSH_TIMEOUT, console=console)
    if timeout != DEFAULT_SSH_TIMEOUT:
        panel["timeout"] = timeout
    if panel.get("command") and Confirm.ask(
        "add a log_link (click an output row to follow its log)?", default=False, console=console
    ):
        panel["log_link"] = _prompt_log_link(console)


def _prompt_log_link(console):
    from rich.prompt import Prompt

    while True:
        pattern = Prompt.ask("log_link pattern (regex; first capture group = the job token)", console=console)
        try:
            if re.compile(pattern).groups < 1:
                console.print("[red]pattern needs a capture group[/red]")
                continue
        except re.error as error:
            console.print(f"[red]pattern does not compile: {error}[/red]")
            continue
        break
    while True:
        command = Prompt.ask("log_link command (remote; use {job} for the captured token)", console=console)
        if "{job}" in command:
            return {"pattern": pattern, "command": command}
        console.print("[red]command must contain a {job} placeholder[/red]")


def _prompt_site(console, first):
    """One http_checks site: url (required), optional display name, cert skip and expected codes."""
    from rich.prompt import Confirm, Prompt

    while True:
        url = Prompt.ask("site url" + ("" if first else " (empty to stop adding sites)"), default="", console=console)
        url = (url or "").strip()
        if not url and not first:
            return None
        if re.match(r"https?://", url):
            break
        console.print("[red]url must start with http:// or https://[/red]")
    site = {"url": url}
    name = _ask_optional("display name (empty to show the host)", console)
    if name:
        site["name"] = name
    if Confirm.ask("skip certificate verification (self-signed internal host)?", default=False, console=console):
        site["insecure"] = True
    while True:
        expect = _ask_optional("status codes that count as up, comma-separated (empty = any 2xx/3xx)", console)
        if not expect:
            break
        codes = [code.strip() for code in expect.split(",") if code.strip()]
        if all(code.isdigit() for code in codes):
            site["expect"] = [int(code) for code in codes] if len(codes) > 1 else int(codes[0])
            break
        console.print("[red]codes must be integers[/red]")
    return site


def _prompt_http_checks_fields(panel, target, credentials_root, console):
    from rich.prompt import IntPrompt

    panel["sites"] = []
    while True:
        site = _prompt_site(console, first=not panel["sites"])
        if site is None:
            break
        panel["sites"].append(site)
    host = _prompt_host(
        "probe from host (inventory name or alias; empty to probe from the machine running the board)",
        target["base_dir"], credentials_root, console, optional=True,
    )
    if host:
        panel["host"] = host
        jump = _prompt_host(
            "jump hop (inventory name or alias, empty for none)",
            target["base_dir"], credentials_root, console, optional=True,
        )
        if jump:
            panel["jump"] = jump
        timeout = IntPrompt.ask("ssh timeout (seconds)", default=DEFAULT_SSH_TIMEOUT, console=console)
        if timeout != DEFAULT_SSH_TIMEOUT:
            panel["timeout"] = timeout
    max_time = IntPrompt.ask("per-site curl deadline (seconds)", default=DEFAULT_PROBE_TIMEOUT, console=console)
    if max_time != DEFAULT_PROBE_TIMEOUT:
        panel["max_time"] = max_time


def _prompt_env_file(panel, target, console):
    env_file = _ask_optional(
        f"env_file with the token(s), relative to {target['base_dir']} (empty to use real env vars only)", console
    )
    if env_file:
        panel["env_file"] = env_file
        if not os.path.exists(os.path.join(target["base_dir"], os.path.expanduser(env_file))):
            console.print("[yellow]warning:[/yellow] that env_file doesn't exist yet - create it before launching")


def _prompt_github_fields(panel, target, console):
    from rich.prompt import Prompt

    panel["token_env"] = Prompt.ask("token_env (env var name holding the read-only PAT)", console=console).strip()
    _prompt_env_file(panel, target, console)
    search = _ask_optional("extra search qualifiers (e.g. -repo:owner/name; empty for none)", console)
    if search:
        panel["search"] = search


def _prompt_bitbucket_fields(panel, target, console):
    from rich.prompt import Prompt

    panel["workspace"] = Prompt.ask("workspace", console=console).strip()
    repos = _ask_optional("repos, comma-separated (empty scans the whole workspace - slower)", console)
    if repos:
        panel["repos"] = [repo.strip() for repo in repos.split(",") if repo.strip()]
    panel["username_env"] = Prompt.ask("username_env (env var name)", console=console).strip()
    panel["app_password_env"] = Prompt.ask("app_password_env (env var name)", console=console).strip()
    _prompt_env_file(panel, target, console)


def run_wizard(credentials_root, repo_root=None):
    """
    Interactive add-a-panel wizard (``status_board.py --add``): pick which
    config the panel lives in, answer the fields for its type, preview the
    YAML, confirm, and the panel is appended - the board picks it up on next
    launch. Returns a process exit code.
    """
    from rich.console import Console
    from rich.prompt import Confirm, IntPrompt, Prompt
    from rich.syntax import Syntax

    console = Console()
    targets = wizard_targets(credentials_root, repo_root)
    if not targets:
        console.print("[red]No candidate config locations found[/red] - is this repo cloned next to its siblings?")
        return 1

    console.print("\n[bold]Add a status board panel[/bold]\n\nWhere should it live?")
    for index, target in enumerate(targets, 1):
        marker = "" if target["exists"] else " [dim](new file)[/dim]"
        warning = "  [yellow]tracked in the public repo - secrets-free panels only[/yellow]" if target["public"] else ""
        console.print(f"  {index}. {target['path']}{marker}{warning}")
    choice = IntPrompt.ask("config", choices=[str(i) for i in range(1, len(targets) + 1)], console=console)
    target = targets[choice - 1]

    panel_type = Prompt.ask("panel type", choices=list(PANEL_TYPES), console=console)
    taken = existing_panel_names(credentials_root, repo_root)
    while True:
        name = Prompt.ask("panel name (unique across all configs)", console=console).strip()
        if name and name not in taken:
            break
        console.print(f"[red]'{name}' is empty or already taken[/red]")

    panel = {"name": name, "type": panel_type}
    {
        "ssh_command": lambda: _prompt_ssh_fields(panel, target, credentials_root, console),
        "github_prs": lambda: _prompt_github_fields(panel, target, console),
        "bitbucket_prs": lambda: _prompt_bitbucket_fields(panel, target, console),
        "http_checks": lambda: _prompt_http_checks_fields(panel, target, credentials_root, console),
    }[panel_type]()

    if panel_type != "ssh_command":
        browser = _ask_optional("browser for clicked links (edge/chrome/firefox/safari; empty for OS default)", console)
        if browser:
            panel["browser"] = browser
    interval = IntPrompt.ask(
        "refresh interval (seconds)", default=DEFAULT_INTERVALS[panel_type], console=console
    )
    if interval != DEFAULT_INTERVALS[panel_type]:
        panel["interval"] = interval
    note = _ask_optional("note (free text shown nowhere yet, kept for your own records; empty for none)", console)
    if note:
        panel["note"] = note

    try:
        _validate_panel(panel, target["path"])
    except ValueError as error:
        console.print(f"[red]{error}[/red]")
        return 1

    block = format_panel_yaml(panel)
    console.print(f"\nThis will be appended to [bold]{target['path']}[/bold]:\n")
    console.print(Syntax(block, "yaml"))
    if not Confirm.ask("write it?", default=True, console=console):
        console.print("nothing written")
        return 1
    append_panel(target["path"], panel)
    console.print("\n[green]written[/green] - the board loads it on next launch")
    if any(key.endswith("_env") for key in panel):
        console.print(
            "[dim]remember: the env var(s) named above hold the actual secrets - put them in the "
            "config repo's env file or the environment, never in the yaml.[/dim]"
        )
    return 0


# %%
