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
