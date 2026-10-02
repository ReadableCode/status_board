# %%
# Imports #

import asyncio

from utils.statusboard_tools import PanelResult

# %%
# Helpers #


def make_panels():
    return [
        {
            "name": name, "type": "ssh_command", "host": "h", "command": "c", "interval": 300,
            "_context": "test", "_base_dir": "/nowhere", "_config": "test.yaml",
        }
        for name in ("one", "two")
    ]


def fake_fetch_factory(calls):
    def fake_fetch(panel, credentials_root, local_hostname=""):
        calls.append(panel["name"])
        return PanelResult(True, "ansi", f"output of {panel['name']}")

    return fake_fetch


# %%
# Single-pane vs all-pane refresh #


def test_refresh_pane_refreshes_only_the_focused_panel(monkeypatch):
    from src import status_board

    calls = []
    monkeypatch.setattr(status_board, "fetch_panel", fake_fetch_factory(calls))
    app_class = status_board.build_app(make_panels(), "TESTHOST")

    async def scenario():
        app = app_class()
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            baseline = list(calls)

            app.query("Panel").first().focus()
            await pilot.press("u")
            await app.workers.wait_for_complete()
            await pilot.pause()
            after_single = list(calls)

            await pilot.press("r")
            await app.workers.wait_for_complete()
            await pilot.pause()
            return baseline, after_single, list(calls)

    baseline, after_single, after_all = asyncio.run(scenario())
    assert sorted(baseline) == ["one", "two"]                      # both panels fetch on mount
    assert after_single[len(baseline):] == ["one"]                 # `u` refetches ONLY the focused panel
    assert sorted(after_all[len(after_single):]) == ["one", "two"]  # `r` still refetches every panel


def test_refresh_pane_without_focus_or_hover_is_a_noop(monkeypatch):
    from src import status_board

    calls = []
    monkeypatch.setattr(status_board, "fetch_panel", fake_fetch_factory(calls))
    app_class = status_board.build_app(make_panels(), "TESTHOST")

    async def scenario():
        app = app_class()
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            baseline = list(calls)
            app.set_focus(None)
            await pilot.press("u")
            await app.workers.wait_for_complete()
            await pilot.pause()
            return baseline, list(calls)

    baseline, after = asyncio.run(scenario())
    assert after == baseline  # nothing targeted, nothing refetched, no crash


# %%

# %%
# Alert state #


def test_alert_result_flags_panel_red_but_keeps_rows(monkeypatch):
    from src import status_board

    rows = [{"badge": "✗", "badge_style": "bold red", "text": "intranet", "url": "https://x/",
             "tail": "DOWN · (7) Failed to connect", "tail_style": "red"}]

    def fake_fetch(panel, credentials_root, local_hostname=""):
        return PanelResult(True, "sites", rows, "0 up · 1 DOWN", alert=True)

    monkeypatch.setattr(status_board, "fetch_panel", fake_fetch)
    panel = dict(make_panels()[0], name="sites", type="http_checks", sites=[{"url": "https://x/"}])
    app_class = status_board.build_app([panel], "TESTHOST")

    async def scenario():
        app = app_class()
        async with app.run_test() as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            widget = app.query("Panel").first()
            output = widget.query_one(".panel-output")
            return widget.has_class("error"), widget.border_subtitle, panel_text(output)

    is_error, subtitle, body = asyncio.run(scenario())
    assert is_error                              # a down site flags the panel like a fetch error
    assert subtitle.startswith("0 up · 1 DOWN")
    assert "intranet" in body and "DOWN" in body  # ...while the rows still render


# %%


# %%
# Site grid #


def panel_text(widget):
    """The text a panel's output Static currently shows, at its laid-out width."""
    return "\n".join(widget.render_line(y).text.rstrip() for y in range(widget.size.height))


def test_site_grid_reflows_when_the_terminal_resizes(monkeypatch):
    from src import status_board

    rows = [{"badge": "✓", "badge_style": "bold green", "text": f"site {index}", "url": "https://x/",
             "tail": "200 · 14ms", "tail_style": "dim"} for index in range(6)]

    def fake_fetch(panel, credentials_root, local_hostname=""):
        return PanelResult(True, "sites", rows, "all 6 up")

    monkeypatch.setattr(status_board, "fetch_panel", fake_fetch)
    panel = dict(make_panels()[0], name="sites", type="http_checks", sites=[{"url": "https://x/"}])
    app_class = status_board.build_app([panel], "TESTHOST")

    async def scenario():
        app = app_class()
        async with app.run_test(size=(160, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            output = app.query("Panel").first().query_one(".panel-output")
            wide = panel_text(output)
            await pilot.resize_terminal(50, 40)
            await pilot.pause()
            return wide, panel_text(output), output.size.width

    wide, narrow, narrow_width = asyncio.run(scenario())
    assert len(wide.splitlines()) == 1        # all six side by side
    assert len(narrow.splitlines()) == 6      # one per line once squeezed
    assert all(len(line) <= narrow_width for line in narrow.splitlines())


# %%
