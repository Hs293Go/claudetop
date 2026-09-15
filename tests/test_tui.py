"""Drive the Textual app headlessly."""

from __future__ import annotations

import asyncio

from claudetop import core
from claudetop.tui import ClaudeTop, ConfirmScreen, DetailScreen, VimScroll


async def settle(app, pilot) -> None:
    # Actions hop between worker threads and the event loop (io worker, then a
    # rescan worker), so wait out the whole chain.
    for _ in range(4):
        await app.workers.wait_for_complete()
        await pilot.pause()


def test_loads_filters_and_opens_detail(fake):
    async def scenario():
        app = ClaudeTop(fake.root, use_cache=False)
        async with app.run_test(size=(160, 40)) as pilot:
            await settle(app, pilot)
            assert app.sessions_table.row_count == 4

            # Digits typed into the filter must not switch tabs.
            await pilot.press("slash", *"1111", "enter")
            assert app.active_tab == "sessions"
            assert [s.sid for s in app.session_rows] == [fake.S1]

            await pilot.press("enter")
            assert isinstance(app.screen, DetailScreen)
            await pilot.press("escape")
            assert not isinstance(app.screen, DetailScreen)

            await pilot.press("s")
            assert app.sort_key == "size"

    asyncio.run(scenario())


def test_vim_navigation(fake):
    async def scenario():
        app = ClaudeTop(fake.root, use_cache=False)
        async with app.run_test(size=(160, 40)) as pilot:
            await settle(app, pilot)
            table = app.sessions_table
            last = table.row_count - 1

            await pilot.press("G")
            assert table.cursor_row == last
            await pilot.press("k")
            assert table.cursor_row == last - 1
            await pilot.press("g", "g")
            assert table.cursor_row == 0
            await pilot.press("j")
            assert table.cursor_row == 1
            await pilot.press("ctrl+d")
            assert table.cursor_row == last  # half a page is more than 4 rows
            await pilot.press("ctrl+u")
            assert table.cursor_row == 0

            # A g followed by a non-chord key does its normal thing and resets.
            await pilot.press("g", "j", "escape", "g", "t")
            assert table.cursor_row == 1
            assert app.active_tab == "projects"
            await pilot.press("g", "T", "g", "T")
            assert app.active_tab == "prune"
            await pilot.press("g", "t")
            assert app.active_tab == "sessions"

            await pilot.press("3")
            assert app.focused is app.traffic_scroll
            await pilot.press("1", "enter")
            assert isinstance(app.screen, DetailScreen)
            assert isinstance(app.focused, VimScroll)
            await pilot.press("g", "t", "q")
            assert not isinstance(app.screen, DetailScreen)
            assert app.active_tab == "sessions"

    asyncio.run(scenario())


def test_tab_switches_tabs_and_only_slash_enters_filter(fake):
    async def scenario():
        app = ClaudeTop(fake.root, use_cache=False)
        async with app.run_test(size=(160, 40)) as pilot:
            await settle(app, pilot)
            assert app.focused is app.sessions_table

            await pilot.press("tab")
            assert app.active_tab == "projects"
            assert app.focused is app.projects_table
            await pilot.press("tab", "tab")
            assert app.active_tab == "prune"
            assert app.focused is app.prune_table
            await pilot.press("tab")
            assert app.active_tab == "sessions"
            await pilot.press("shift+tab")
            assert app.active_tab == "prune"
            await pilot.press("shift+tab", "shift+tab", "shift+tab")
            assert app.active_tab == "sessions"
            assert app.focused is app.sessions_table

            await pilot.click("#filter")
            assert app.focused is app.sessions_table

            await pilot.press("slash", *"1111")
            assert app.focused is app.filter_input
            # Tab leaves the filter for the next tab, keeping what was typed.
            await pilot.press("tab")
            assert app.active_tab == "projects"
            assert app.focused is app.projects_table
            assert not app.filter_input.can_focus
            await pilot.press("shift+tab")
            assert app.focused is app.sessions_table
            assert [s.sid for s in app.session_rows] == [fake.S1]

            # Inside a dialog, tab doesn't switch the tabs behind it.
            await pilot.press("d")
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("tab")
            assert app.active_tab == "sessions"
            await pilot.press("escape")
            assert not isinstance(app.screen, ConfirmScreen)

    asyncio.run(scenario())
    assert (fake.pdir / f"{fake.S1}.jsonl").exists()


def test_prune_and_delete_go_to_trash(fake):
    async def scenario():
        app = ClaudeTop(fake.root, use_cache=False)
        async with app.run_test(size=(160, 40)) as pilot:
            await settle(app, pilot)

            await pilot.press("4")
            assert app.active_tab == "prune"
            assert len(app.prune_rows) == 2
            await pilot.press("O")
            assert len(app.prune_rows) == 3
            await pilot.press("O", "x")
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press(*"prune", "enter")
            await settle(app, pilot)
            assert not (fake.root / "file-history" / fake.STRAY_OLD).exists()
            assert app.prune_rows == []

            await pilot.press("1", "slash", *"1111", "enter", "d")
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press(*"yes", "enter")
            await settle(app, pilot)
            assert not (fake.pdir / f"{fake.S1}.jsonl").exists()
            assert fake.S1 not in {s.sid for s in app.idx.sessions}

    asyncio.run(scenario())
    reasons = [data["reason"] for _name, data, _size in core.list_trash(fake.root)]
    assert reasons == ["prune", f"delete session {fake.S1}"]


def test_running_session_and_cancel_leave_files_alone(fake):
    async def scenario():
        app = ClaudeTop(fake.root, use_cache=False)
        async with app.run_test(size=(160, 40)) as pilot:
            await settle(app, pilot)

            await pilot.press("slash", *"2222", "enter", "d")
            assert not isinstance(app.screen, ConfirmScreen)

            await pilot.press("escape", "slash", *"3333", "enter", "d")
            assert isinstance(app.screen, ConfirmScreen)
            await pilot.press("escape")
            await settle(app, pilot)
            assert not isinstance(app.screen, ConfirmScreen)

    asyncio.run(scenario())
    assert (fake.pdir / f"{fake.S2}.jsonl").exists()
    assert (fake.pdir / f"{fake.S3}.jsonl").exists()
    assert core.list_trash(fake.root) == []
