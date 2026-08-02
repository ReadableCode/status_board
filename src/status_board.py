# %%
# Imports #

import argparse
import platform
import re
import shlex
import subprocess
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from itertools import groupby

from config import grandparent_dir, parent_dir
from readable_utils.host_stats_tools import (  # noqa: F401 - meter pieces re-exported for callers/tests
    METER_EMPTY,
    METER_FILLED,
    METER_WIDTH,
    ramp_style,
    stats_renderable,
)
from readable_utils.host_tools import get_uppercase_hostname
from rich.console import Console
from rich.style import Style
from rich.text import Text
from utils.statusboard_tools import build_ssh_argv, fetch_panel, load_panels

# %%
# Variables #

REPO_ROOT = parent_dir
CREDENTIALS_ROOT = grandparent_dir

# Panel `browser:` values -> platform-specific launch names. Anything not in
# this table is passed through as the app/binary name verbatim.
BROWSER_APPS = {
    "edge": {"Darwin": "Microsoft Edge", "Windows": "msedge", "Linux": "microsoft-edge"},
    "chrome": {"Darwin": "Google Chrome", "Windows": "chrome", "Linux": "google-chrome"},
    "firefox": {"Darwin": "Firefox", "Windows": "firefox", "Linux": "firefox"},
    "safari": {"Darwin": "Safari"},
}


# %%
# Browser launching #


def browser_open_argv(browser, url, system=None):
    """
    The argv that opens url in the named browser on this platform, or None
    when no browser is named (caller falls back to the OS default handler).
    """
    if not browser:
        return None
    system = system or platform.system()
    app = BROWSER_APPS.get(browser.lower(), {}).get(system, browser)
    if system == "Darwin":
        return ["open", "-a", app, url]
    if system == "Windows":
        # `start` resolves app-execution aliases like msedge/chrome
        return ["cmd", "/c", "start", "", app, url]
    return [app, url]


def open_link(url, browser=None):
    """Open url in the panel's configured browser, or the OS default when none is set."""
    argv = browser_open_argv(browser, url)
    if argv is None:
        webbrowser.open(url)
        return
    subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# %%
# Rendering #
# (the host-stats meters - ramp_style, stats_renderable, METER_* - live in
# readable_utils.host_stats_tools, shared with herdstone)


def legend_text():
    """One-line key for the PR badge symbols, shown at the bottom of the board."""
    text = Text()
    for index, (badge, style, label) in enumerate([
        ("✏", "bold red", "unsubmitted draft"),
        ("●", "bold cyan", "needs your review"),
        ("✋", "bold yellow", "waiting on author"),
        ("💬", "dim", "you commented"),
        ("⬆", "bold magenta", "your PR"),
        ("◌", "dim", "draft, parked"),
    ]):
        if index:
            text.append("   ", style="dim")
        text.append(badge, style=style)
        text.append(f" {label}", style="dim")
    return text


def mark_log_links(text, log_link, panel_name):
    """
    Make the job tokens in an ssh_command panel's rendered output clickable:
    every first-capture-group match of log_link's pattern gets underlined and
    wired to app.view_log, which pushes the log-follow pane for that job.
    """
    pattern = re.compile(log_link["pattern"], re.MULTILINE)
    for match in pattern.finditer(text.plain):
        start, end = match.span(1)
        job = match.group(1)
        text.stylize(
            Style(underline=True, meta={"@click": f"app.view_log({panel_name!r}, {job!r})"}),
            start,
            end,
        )
    return text


def result_renderable(result, browser=None, tui=False, log_link=None, panel_name=None):
    """
    Turn a PanelResult into a rich renderable (used by both the TUI and --once).

    Link rows render differently per mode: --once emits plain OSC 8 hyperlinks
    (the terminal handles clicks), but inside the TUI Textual captures the
    mouse, so rows carry an @click action meta that routes through
    app.action_open_link - which is also what honors the panel's browser.
    """
    if not result.ok:
        return Text(result.body, style="red")
    if result.kind == "ansi":
        if not result.body:
            return Text("(no output)", style="dim")
        text = Text.from_ansi(result.body)
        if tui and log_link:
            mark_log_links(text, log_link, panel_name)
        return text
    if not result.body:
        return Text("nothing awaiting review 🎉", style="green")
    text = Text()
    for index, row in enumerate(result.body):
        if index:
            text.append("\n")
        if row.get("badge"):
            text.append(f"{row['badge']} ", style=row.get("badge_style", ""))
        dim = bool(row.get("dim"))
        if tui:
            style = Style(
                bold=not dim, dim=dim, underline=True,
                meta={"@click": f"app.open_link({row['url']!r}, {browser!r})"},
            )
        else:
            style = Style(bold=not dim, dim=dim, link=row["url"])
        text.append(row["text"], style=style)
        if row.get("meta"):
            text.append(f"\n    {row['meta']}", style="dim")
    return text


# %%
# TUI #


def panel_under_mouse(app, panel_class):
    """The panel widget the mouse is currently inside, or None."""
    try:
        position = app.mouse_position
    except AttributeError:  # no mouse events yet / headless
        return None
    for widget in app.query(panel_class):
        if widget.region.contains(position.x, position.y):
            return widget
    return None


def focused_panel(app, panel_class):
    """The panel widget owning focus (walking up from the focused node), or None."""
    node = app.focused
    while node is not None and not isinstance(node, panel_class):
        node = node.parent
    return node


def build_log_tail_screen():
    """
    Construct the log-follow Screen class lazily (same reason as build_app:
    --once and the unit tests never import textual).
    """
    from textual.app import ComposeResult
    from textual.screen import Screen
    from textual.widgets import Footer, Header, RichLog

    class LogTailScreen(Screen):
        """
        Full-screen live follow of one remote log, pushed when a log-linked
        row on the board is clicked. Streams the panel's log_link command
        (a tail -F) over the same ssh chain the panel itself uses; escape/q
        pops back to the board and kills the ssh.
        """

        BINDINGS = [("escape", "app.pop_screen", "back to board"), ("q", "app.pop_screen", "back to board")]

        def __init__(self, title, argv):
            super().__init__()
            self.tail_title = title
            self.argv = argv
            self.process = None

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            log = RichLog(highlight=False, markup=False, wrap=True, auto_scroll=True)
            log.border_title = self.tail_title
            yield log
            yield Footer()

        def on_mount(self):
            self.sub_title = self.tail_title
            self.run_worker(self._stream, thread=True)

        def _stream(self):
            log = self.query_one(RichLog)

            def write(line):
                self.app.call_from_thread(log.write, line)

            try:
                self.process = subprocess.Popen(
                    self.argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    text=True,
                )
                for line in self.process.stdout:
                    write(Text.from_ansi(line.rstrip("\n")))
                code = self.process.wait()
                if code != 0:
                    write(Text(f"[stream exited {code}]", style="red"))
            except FileNotFoundError:
                write(Text("ssh not found on PATH", style="red"))
            except RuntimeError:
                pass  # screen was popped mid-write; the process is being torn down anyway

        def on_unmount(self):
            if self.process and self.process.poll() is None:
                self.process.terminate()

    return LogTailScreen


def build_panel_widget(local_hostname):
    """
    Construct the Panel widget class lazily (same reason as build_app:
    --once and the unit tests never import textual).
    """
    from textual.app import ComposeResult
    from textual.containers import Horizontal, Vertical
    from textual.widgets import Label, ProgressBar, Static

    class Panel(Vertical):
        """
        One board panel: bordered output area that refetches itself on its own
        interval (panels poll independently), with a real-time bar at the
        bottom filling toward the next poll - indeterminate while a fetch is
        actually in flight. host_stats panels get a stats strip pinned BELOW
        the (possibly clipped) output scroll region so the meters never
        scroll out of view; a stats-only panel renders the meters as its
        body, which reads identically since there is no output above them.

        Panels are focusable (tab / shift+tab cycle them, clicking one
        focuses it) so the app's single-pane refresh binding has a target
        when the mouse isn't over any panel.
        """

        can_focus = True

        def __init__(self, panel):
            super().__init__()
            self.panel = panel
            self.border_title = panel["name"]
            self.deadline = None  # monotonic time of the next scheduled poll; None = fetching
            self.stats_only = bool(panel.get("host_stats")) and not panel.get("command")

        def compose(self) -> ComposeResult:
            yield Static("loading…", markup=False, classes="panel-output")
            if self.panel.get("host_stats") and not self.stats_only:
                yield Static("", markup=False, classes="panel-stats")
            with Horizontal(classes="panel-footer"):
                yield ProgressBar(total=None, show_eta=False, show_percentage=False)
                yield Label("refreshing…", classes="panel-countdown")

        def on_mount(self):
            self.refresh_panel()
            self.set_interval(self.panel["interval"], self.refresh_panel)
            self.set_interval(1.0, self._tick)

        def refresh_panel(self):
            self.deadline = None
            self._fired = time.monotonic()
            bar = self.query_one(ProgressBar)
            bar.total = None  # indeterminate pulse while the worker runs
            self.query_one(".panel-countdown", Label).update("refreshing…")
            self.run_worker(self._fetch, thread=True, group=self.panel["name"], exclusive=True)

        def _fetch(self):
            result = fetch_panel(self.panel, CREDENTIALS_ROOT, local_hostname)
            self.app.call_from_thread(self._show, result)

        def _show(self, result):
            self.set_class(not result.ok, "error")
            state = result.summary or ("ok" if result.ok else "error")
            self.border_subtitle = f"{state} · {time.strftime('%H:%M:%S')}"
            if self.stats_only and result.ok:
                renderable = stats_renderable(result.stats)
            else:
                renderable = result_renderable(
                    result,
                    browser=self.panel.get("browser"),
                    tui=True,
                    log_link=self.panel.get("log_link"),
                    panel_name=self.panel["name"],
                )
            self.query_one(".panel-output", Static).update(renderable)
            for stats_widget in self.query(".panel-stats"):
                stats_widget.update(stats_renderable(result.stats) if result.ok else Text())
            # the poll timer fires one interval after the previous FIRE, not
            # after completion - anchor the countdown to fetch start so the
            # bar reaches full just as the timer actually fires
            self.deadline = self._fired + self.panel["interval"]
            bar = self.query_one(ProgressBar)
            bar.total = self.panel["interval"]
            bar.progress = 0

        def _tick(self):
            if self.deadline is None:
                return  # fetch in flight - bar is pulsing
            remaining = max(0, self.deadline - time.monotonic())
            interval = self.panel["interval"]
            self.query_one(ProgressBar).progress = interval - remaining
            minutes, seconds = divmod(int(remaining), 60)
            self.query_one(".panel-countdown", Label).update(f"next in {minutes}m{seconds:02d}s")

    return Panel


def build_app(panels, local_hostname):
    """
    Construct the Textual app class lazily so --once (and the unit tests)
    never need textual imported at module import time.
    """
    from readable_utils.design_tokens import terminal_navy_textual_theme
    from textual.app import App, ComposeResult
    from textual.containers import Vertical, VerticalScroll
    from textual.widgets import Footer, Header, Static

    panels_by_name = {panel["name"]: panel for panel in panels}
    LogTailScreen = build_log_tail_screen()
    Panel = build_panel_widget(local_hostname)

    class StatusBoardApp(App):
        TITLE = "status board"

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            # the shared readablecode "terminal navy" palette (same theme
            # herdstone's TUI registers), so the two boards read as one family
            self.register_theme(terminal_navy_textual_theme())
            self.theme = "terminal-navy"
        BINDINGS = [
            ("q", "quit", "quit"),
            ("r", "refresh_all", "refresh all"),
            ("u", "refresh_pane", "refresh pane"),
        ]
        CSS = """
        .context-group {
            border: double $secondary;
            border-title-color: $secondary;
            border-title-style: bold;
            border-title-align: left;
            height: auto;
            margin: 0 1 1 1;
            padding: 1 1 0 1;
        }
        Panel {
            border: round $primary 40%;
            border-title-color: $accent;
            height: auto;
            max-height: 30;
            margin: 0 0 1 0;
            padding: 0 1;
        }
        Panel:hover { border: round $primary; }
        Panel:focus { border: round $accent; }
        Panel.error { border: round red; }
        .panel-output { height: auto; max-height: 26; overflow-y: auto; }
        .panel-stats { height: 1; margin-top: 1; }
        .panel-footer { height: 1; margin-top: 1; }
        .panel-footer ProgressBar { width: 1fr; }
        .panel-footer Bar { width: 1fr; }
        .panel-countdown { color: $text-muted; margin-left: 2; }
        .legend { height: 1; padding: 0 2; }
        LogTailScreen RichLog {
            border: round $primary;
            border-title-color: $accent;
            padding: 0 1;
        }
        """

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            with VerticalScroll():
                # one bordered box per context, in config-discovery order -
                # panels from the same statusboard config are contiguous, so
                # a plain groupby keeps each credentials repo's panels
                # together under its own labeled rectangle
                for context, group in groupby(panels, key=lambda p: p["_context"]):
                    with Vertical(classes="context-group") as box:
                        box.border_title = f" {context.replace('_', ' ')} "
                        for panel in group:
                            yield Panel(panel)
            yield Static(legend_text(), classes="legend")
            yield Footer()

        def action_refresh_all(self):
            for widget in self.query(Panel):
                widget.refresh_panel()

        def action_refresh_pane(self):
            """
            Refresh ONE panel, the way `r` refreshes them all: the panel
            under the mouse wins, else the focused panel (tab cycles focus
            through panels, and clicking a panel focuses it).
            """
            target = panel_under_mouse(self, Panel) or focused_panel(self, Panel)
            if target is not None:
                target.refresh_panel()

        def action_open_link(self, url, browser=None):
            open_link(url, browser)

        def action_view_log(self, panel_name, job):
            panel = panels_by_name[panel_name]
            command = panel["log_link"]["command"].format(job=shlex.quote(job))
            argv = build_ssh_argv(panel, CREDENTIALS_ROOT, local_hostname, command=command)
            self.push_screen(LogTailScreen(f"{panel_name} · {job}", argv))

    return StatusBoardApp


# %%
# Main #


def run_once(panels, local_hostname):
    """
    Fetch every panel concurrently and print a static board in config order
    (sanity check / headless use) - the herdstone fan-out idea, so the render
    takes as long as the slowest panel instead of the sum of them.
    """
    console = Console()
    with ThreadPoolExecutor(max_workers=min(8, len(panels))) as pool:
        results = list(pool.map(lambda p: fetch_panel(p, CREDENTIALS_ROOT, local_hostname), panels))
    last_context = None
    for panel, result in zip(panels, results):
        if panel["_context"] != last_context:
            last_context = panel["_context"]
            console.rule(f"[bold]══ {last_context.replace('_', ' ')} ══[/bold]", style="cyan", characters="═")
        state = result.summary or ("ok" if result.ok else "error")
        console.rule(f"[bold]{panel['name']}[/bold] · {state}", style="green" if result.ok else "red")
        if result.body or not (result.ok and panel.get("host_stats")):
            console.print(result_renderable(result))
        if result.ok and panel.get("host_stats"):
            console.print(stats_renderable(result.stats))
        console.print()
    console.print(legend_text())
    return 0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Long-lived TUI status board; panels come from <context>_statusboard.yaml "
        "configs discovered in sibling *_credentials repos (plus an optional "
        "statusboard.yaml in this repo)."
    )
    parser.add_argument("--once", action="store_true", help="fetch every panel once, print, and exit (no TUI)")
    parser.add_argument(
        "--add",
        action="store_true",
        help="interactive wizard: add a new panel to one of the statusboard configs",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="load only this statusboard config file, skipping discovery (for testing)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.add:
        from utils.wizard_tools import run_wizard
        return run_wizard(CREDENTIALS_ROOT, REPO_ROOT)
    panels, config_paths = load_panels(CREDENTIALS_ROOT, REPO_ROOT, config_path=args.config)
    if not panels:
        print(
            "No statusboard panels found - add a <context>_statusboard.yaml to a sibling "
            "*_credentials repo, or run with --add to create one (see README.md)"
        )
        return 1
    local_hostname = get_uppercase_hostname() or ""
    print(f"configs: {', '.join(config_paths)}")
    if args.once:
        return run_once(panels, local_hostname)
    app_class = build_app(panels, local_hostname)
    app_class().run()
    return 0


if __name__ == "__main__":
    sys.exit(main())


# %%
