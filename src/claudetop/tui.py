"""
Textual front end for claudetop.

Tabs:
  Sessions  every transcript, with size, tokens, and end-of-session context load
  Projects  rolled up per working directory, flags dirs that no longer exist
  Traffic   token throughput per day, split by cache behaviour and by model
  Prune     reclaimable junk: superseded transcripts, stray session data, orphans

Destructive actions ask for a typed confirmation and, unless --hard is set, move
files into the trash directory instead of unlinking them.
"""

from __future__ import annotations

import os
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import ClassVar, cast

from rich.bar import Bar
from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.coordinate import Coordinate
from textual.screen import ModalScreen, Screen
from textual.suggester import Suggester
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.data_table import ColumnKey
from textual.worker import get_current_worker

from claudetop.core import (
    APP,
    TRASH_NAME,
    Index,
    Project,
    Session,
    bar,
    ellipsize,
    hard_delete,
    human_age,
    human_bytes,
    human_tokens,
    live_sessions,
    move_to_trash,
    path_size,
    reclaimable,
    rehome,
    scan,
    session_paths,
    short_model,
    tildify,
)

ACCENT = "color(173)"
TABS = ["sessions", "projects", "traffic", "prune"]

# (column key, label, width). The keys that appear in SORTS are sortable.
SESSION_COLUMNS = [
    ("last", "LAST", 9),
    ("size", "SIZE", 9),
    ("tokens", "TOKENS", 9),
    ("context", "CTX", 14),
    ("project", "PROJECT", 22),
    ("title", "TITLE", None),
]
SORTS: dict[str, Callable[[Session, int | None], float]] = {
    "last": lambda s, _w: -s.mtime,
    "size": lambda s, _w: -s.total_size,
    "tokens": lambda s, _w: -s.tokens,
    "context": lambda s, w: -s.context_pct(w),
}

# App actions that only apply on one tab; the footer hides them elsewhere.
TAB_ACTIONS = {
    "sort": "sessions",
    "delete": "sessions",
    "rehome": "sessions",
    "prune": "prune",
    "toggle_orphans": "prune",
    "toggle_memory": "prune",
}


# ───────────────────────────────────────────────────────────── helpers ──


def right(text: str, style: str = "") -> Text:
    return Text(text, style=style, justify="right")


def relative(path: Path, root: Path) -> Path:
    try:
        return path.relative_to(root)
    except ValueError:
        return path


def selected[T](table: DataTable, rows: list[T]) -> T | None:
    """The item under a table's cursor, given the rows in display order."""
    if rows and 0 <= table.cursor_row < len(rows):
        return rows[table.cursor_row]
    return None


def repopulate(table: DataTable, rows: list[tuple[str, tuple]]) -> None:
    """Replace a table's rows, keeping the cursor on the same row key if it survives."""
    current = None
    if table.row_count:
        cell = table.coordinate_to_cell_key(Coordinate(table.cursor_row, 0))
        current = cell.row_key.value
    table.clear()
    for key, cells in rows:
        table.add_row(*cells, key=key)
    keys = [key for key, _cells in rows]
    if current in keys:
        table.move_cursor(row=keys.index(current), animate=False)


def daily_tokens(idx: Index, days: int = 60) -> list[tuple[str, int]]:
    """Tokens per calendar day, newest first, with quiet days filled in as zero."""
    totals: dict[str, int] = defaultdict(int)
    for s in idx.sessions:
        for day, tok in s.daily.items():
            totals[day] += tok
    if not totals:
        return []
    start = datetime.strptime(min(totals), "%Y-%m-%d")
    cur = datetime.strptime(max(totals), "%Y-%m-%d")
    out = []
    while cur >= start and len(out) < days:
        key = cur.strftime("%Y-%m-%d")
        out.append((key, totals.get(key, 0)))
        cur -= timedelta(days=1)
    return out


def heading(text: str) -> Text:
    return Text(text, style="bold dim")


def traffic_view(idx: Index) -> RenderableType:
    days = daily_tokens(idx)
    if not days:
        return Text("no usage data found in transcripts", style="dim")

    peak = max(tok for _day, tok in days) or 1
    chart = Table.grid(expand=True, padding=(0, 1))
    chart.add_column(width=10, style="dim")
    chart.add_column(width=8, justify="right")
    chart.add_column(ratio=1)
    for day, tok in days:
        label = datetime.strptime(day, "%Y-%m-%d").strftime("%a %b %d")
        chart.add_row(label, human_tokens(tok), Bar(peak, 0, tok, color=ACCENT))

    parts = {
        "fresh input": sum(s.tok_in for s in idx.sessions),
        "output": sum(s.tok_out for s in idx.sessions),
        "cache read": sum(s.tok_cache_read for s in idx.sessions),
        "cache write": sum(s.tok_cache_write for s in idx.sessions),
    }
    total = sum(parts.values()) or 1
    composition = Table.grid(expand=True, padding=(0, 1))
    composition.add_column(width=12)
    composition.add_column(width=8, justify="right")
    composition.add_column(ratio=1)
    composition.add_column(width=6, justify="right")
    for name, val in parts.items():
        composition.add_row(
            name,
            human_tokens(val),
            Bar(total, 0, val, color="grey50"),
            f"{val / total:.1%}",
        )

    turns: dict[str, int] = defaultdict(int)
    for s in idx.sessions:
        for model, count in s.models.items():
            turns[model] += count
    models = Table.grid(padding=(0, 1))
    models.add_column(width=28)
    models.add_column(justify="right")
    for model, count in sorted(turns.items(), key=lambda kv: -kv[1]):
        models.add_row(short_model(model), f"{count:,} turns")

    return Group(
        heading("TOKENS PER DAY (input + output + cache)"),
        chart,
        Text(),
        heading("COMPOSITION"),
        composition,
        Text(),
        heading("MODELS"),
        models,
    )


class DirectorySuggester(Suggester):
    """Completes the last path component against directories that exist."""

    def __init__(self) -> None:
        super().__init__(use_cache=False, case_sensitive=True)

    async def get_suggestion(self, value: str) -> str | None:
        parent, prefix = os.path.split(os.path.expanduser(value))
        if not parent or not prefix:
            return None
        try:
            with os.scandir(parent) as entries:
                names = sorted(
                    e.name
                    for e in entries
                    if e.is_dir()
                    and e.name.startswith(prefix)
                    and e.name != prefix
                    and (prefix.startswith(".") or not e.name.startswith("."))
                )
        except OSError:
            return None
        return value + names[0][len(prefix) :] if names else None


# ───────────────────────────────────────────────────────────── widgets ──


# Vim keys shared by every scrollable widget, on top of each one's own j/k/G.
VIM_BINDINGS: list[BindingType] = [
    Binding("h", "scroll_left", show=False),
    Binding("l", "scroll_right", show=False),
    Binding("ctrl+f", "page_down", show=False),
    Binding("ctrl+b", "page_up", show=False),
    Binding("ctrl+d", "half_page(1)", show=False),
    Binding("ctrl+u", "half_page(-1)", show=False),
]


def vim_chord(widget: KeyTable | VimScroll, event: events.Key) -> None:
    """
    Handle the g-chords: gg (top), gt and gT (next and previous tab).

    Bindings match one key at a time, so chords live in on_key. A focused
    widget sees each key before bindings do, and stopping the event keeps the
    bindings from also firing.
    """
    pending, widget.pending_g = widget.pending_g, False
    if not pending:
        if event.key == "g":
            widget.pending_g = True
            event.stop()
        return
    app = widget.app
    if event.key == "g":
        widget.vim_top()
    elif event.key in ("t", "T") and isinstance(app, ClaudeTop):
        # Tabs belong to the main screen; ignore gt/gT over the detail view.
        if len(app.screen_stack) == 1:
            app.action_cycle_tab(1 if event.key == "t" else -1)
    else:
        return  # not a chord, so let the key do what it normally does
    event.stop()


class KeyTable(DataTable):
    """A row-cursor table with vim-style movement."""

    pending_g = False
    BINDINGS: ClassVar[list[BindingType]] = [
        *VIM_BINDINGS,
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("G", "scroll_bottom", show=False),
    ]

    def __init__(self, *, id: str) -> None:
        super().__init__(id=id, cursor_type="row")

    def on_key(self, event: events.Key) -> None:
        vim_chord(self, event)

    def on_blur(self) -> None:
        self.pending_g = False

    def vim_top(self) -> None:
        self.action_scroll_top()

    def action_half_page(self, direction: int) -> None:
        header = self.header_height if self.show_header else 0
        step = max(1, (self.scrollable_content_region.height - header) // 2)
        row = self.cursor_row + direction * step
        self.move_cursor(row=max(0, min(self.row_count - 1, row)))


class VimScroll(VerticalScroll):
    """A focusable scrolling view with vim-style movement."""

    can_focus = True
    pending_g = False
    BINDINGS: ClassVar[list[BindingType]] = [
        *VIM_BINDINGS,
        Binding("j", "scroll_down", show=False),
        Binding("k", "scroll_up", show=False),
        Binding("G", "scroll_end", show=False),
    ]

    def on_key(self, event: events.Key) -> None:
        vim_chord(self, event)

    def on_blur(self) -> None:
        self.pending_g = False

    def vim_top(self) -> None:
        self.scroll_home(animate=False)

    def action_half_page(self, direction: int) -> None:
        step = max(1, self.scrollable_content_region.height // 2)
        self.scroll_relative(y=direction * step, animate=False)


class ConfirmScreen(ModalScreen[bool]):
    """Ask for a typed word before doing something destructive."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, question: str, word: str, danger: bool = False) -> None:
        super().__init__()
        self.question = question
        self.word = word
        self.danger = danger

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.question, classes="danger" if self.danger else "")
            yield Input(placeholder=f"type '{self.word}' to confirm, esc to cancel")

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() == self.word)

    def action_cancel(self) -> None:
        self.dismiss(False)


class PromptScreen(ModalScreen[str | None]):
    """Ask for a line of text. Dismisses with None on escape."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self, question: str, value: str = "", suggester: Suggester | None = None
    ) -> None:
        super().__init__()
        self.question = question
        self.value = value
        self.suggester = suggester

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.question)
            yield Input(
                self.value,
                placeholder="→ accepts a completion, esc cancels",
                suggester=self.suggester,
            )

    @on(Input.Submitted)
    def submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value.strip() or None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class DetailScreen(Screen[None]):
    """Everything known about one session, and the files deleting it would remove."""

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape,q", "app.pop_screen", "Back"),
        Binding("d", "delete", "Delete"),
        Binding("m", "rehome", "Rehome"),
    ]

    def __init__(self, session: Session, context_override: int | None) -> None:
        super().__init__()
        self.session = session
        self.context_override = context_override
        # Measured once here rather than on every repaint.
        self.files = [(p, path_size(p)) for p in session_paths(session)]

    @property
    def main_app(self) -> ClaudeTop:
        return cast("ClaudeTop", self.app)

    def compose(self) -> ComposeResult:
        yield Header()
        with VimScroll(id="detail"):
            yield Static(self.describe())
        yield Footer()

    def describe(self) -> RenderableType:
        s = self.session
        pct = s.context_pct(self.context_override)
        window = s.context_window(self.context_override)
        status = (
            Text(f"RUNNING (pid {s.live_pid})", style="bold green")
            if s.live
            else Text("not running")
        )
        fields: list[tuple[str, str | Text]] = [
            ("session id", s.sid),
            ("status", status),
            (
                "project",
                tildify(s.cwd) if s.cwd else f"unknown (dir {s.project_dir.name})",
            ),
            ("git branch", s.git_branch or "—"),
            ("last active", f"{human_age(s.mtime)}  ({s.last_ts or '—'})"),
            ("events", f"{s.events:,} records"),
            ("transcript", f"{human_bytes(s.size)}  {s.transcript.name}"),
            ("associated", f"{human_bytes(s.extras_size)} in {len(s.extras)} paths"),
            ("total on disk", human_bytes(s.total_size)),
            ("", ""),
            ("fresh input", human_tokens(s.tok_in)),
            ("output", human_tokens(s.tok_out)),
            ("cache read", human_tokens(s.tok_cache_read)),
            ("cache write", human_tokens(s.tok_cache_write)),
            (
                "context at end",
                f"{bar(pct, 20)} {pct:.0f}%  ({s.context_end:,} of {window:,})",
            ),
            ("models", ", ".join(short_model(m) for m in s.models) or "—"),
        ]
        grid = Table.grid(padding=(0, 2))
        grid.add_column(width=16, style="dim")
        grid.add_column()
        for label, value in fields:
            grid.add_row(label, value)

        files = Table.grid(padding=(0, 2))
        files.add_column(width=9, justify="right", style="dim")
        files.add_column(style="dim")
        root = self.main_app.claude_root
        for path, size in self.files:
            files.add_row(human_bytes(size), str(relative(path, root)))

        files_heading = (
            Text(
                "FILES  (session is running: delete and rehome are disabled)",
                style="bold red",
            )
            if s.live
            else heading("FILES THAT WOULD BE REMOVED")
        )
        return Group(
            Text(s.title or "(untitled session)", style=f"bold {ACCENT}"),
            Text(),
            grid,
            Text(),
            files_heading,
            files,
        )

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action in ("delete", "rehome") and self.session.live:
            return None  # shown greyed out
        return True

    def action_delete(self) -> None:
        self.main_app.delete_session(self.session)

    def action_rehome(self) -> None:
        self.main_app.rehome_session(self.session)


# ───────────────────────────────────────────────────────────────── app ──


class ClaudeTop(App[None]):
    TITLE = APP
    CSS = """
    KeyTable { height: 1fr; }
    #filter { margin: 0 1; }
    #prune-summary { height: auto; padding: 0 1 1 1; }
    #traffic-scroll, #detail { padding: 1 2; }
    ConfirmScreen, PromptScreen { align: center middle; }
    .dialog {
        width: 90%;
        max-width: 110;
        height: auto;
        padding: 1 2;
        border: thick $accent;
        background: $surface;
    }
    .dialog Label { width: 100%; margin-bottom: 1; }
    .danger { color: $error; text-style: bold; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        # Priority, so tab switches tabs even from inside the filter instead of
        # cycling focus; check_action hands it back to dialogs.
        Binding("tab", "cycle_tab(1)", "Next tab", priority=True),
        Binding("shift+tab", "cycle_tab(-1)", show=False, priority=True),
        Binding("q", "quit", "Quit"),
        Binding("r", "rescan", "Rescan"),
        Binding("slash", "focus_filter", "Filter"),
        Binding("s", "sort", "Sort"),
        Binding("d", "delete", "Delete"),
        Binding("m", "rehome", "Rehome"),
        Binding("x", "prune", "Prune all"),
        Binding("O", "toggle_orphans", "Orphans"),
        Binding("M", "toggle_memory", "Memory"),
        Binding("escape", "clear_filter", show=False),
        *(
            Binding(str(n), f"show_tab('{tab}')", show=False)
            for n, tab in enumerate(TABS, 1)
        ),
    ]

    def __init__(
        self,
        root: Path,
        context_override: int | None = None,
        hard: bool = False,
        use_cache: bool = True,
    ) -> None:
        super().__init__()
        self.claude_root = root
        self.context_override = context_override
        self.hard = hard
        self.use_cache = use_cache
        self.idx = Index(root=root)
        self.active_tab = "sessions"
        self.filter_text = ""
        self.sort_key = "last"
        self.include_orphans = False
        self.keep_memory = True
        self.session_rows: list[Session] = []
        self.project_rows: list[Project] = []
        self.prune_rows: list[tuple[str, Path, int]] = []

    def compose(self) -> ComposeResult:
        self.tabbed = TabbedContent(initial="sessions")
        self.filter_input = Input(
            placeholder="press / to filter by title, project or session id", id="filter"
        )
        # Only / enters the filter, so focus otherwise stays in the tables.
        self.filter_input.can_focus = False
        self.sessions_table = KeyTable(id="sessions-table")
        self.projects_table = KeyTable(id="projects-table")
        self.prune_table = KeyTable(id="prune-table")
        self.traffic_body = Static(id="traffic-body")
        self.traffic_scroll = VimScroll(id="traffic-scroll")
        self.prune_summary = Static(id="prune-summary")

        yield Header()
        with self.tabbed:
            with TabPane("Sessions", id="sessions"):
                yield self.filter_input
                yield self.sessions_table
            with TabPane("Projects", id="projects"):
                yield self.projects_table
            with TabPane("Traffic", id="traffic"), self.traffic_scroll:
                yield self.traffic_body
            with TabPane("Prune", id="prune"):
                yield self.prune_summary
                yield self.prune_table
        yield Footer()

    def on_mount(self) -> None:
        for key, label, width in SESSION_COLUMNS:
            self.sessions_table.add_column(label, key=key, width=width)
        self.update_sort_labels()
        for label, width in (
            ("LAST", 9),
            ("SIZE", 9),
            ("SESS", 5),
            ("MEMORY", 8),
            ("PROJECT", None),
        ):
            self.projects_table.add_column(label, width=width)
        for label, width in (("SIZE", 9), ("KIND", 34), ("PATH", None)):
            self.prune_table.add_column(label, width=width)
        self.sessions_table.focus()
        self.start_scan()

    # ── loading ──

    @property
    def tables(self) -> tuple[DataTable, ...]:
        return (self.sessions_table, self.projects_table, self.prune_table)

    def start_scan(self, status: str | None = None) -> None:
        for table in self.tables:
            table.loading = True
        self.scan_worker(status)

    @work(thread=True, exclusive=True, group="scan")
    def scan_worker(self, status: str | None) -> None:
        idx = scan(self.claude_root, use_cache=self.use_cache)
        if not get_current_worker().is_cancelled:
            self.call_from_thread(self.load_index, idx, status)

    def load_index(self, idx: Index, status: str | None) -> None:
        self.idx = idx
        hard = " · HARD DELETE" if self.hard else ""
        self.sub_title = (
            f"{tildify(idx.root)} · {len(idx.sessions)} sessions · "
            f"{human_bytes(idx.total_size)}{hard}"
        )
        self.refresh_sessions()
        self.refresh_projects()
        self.refresh_prune()
        self.traffic_body.update(traffic_view(idx))
        for table in self.tables:
            table.loading = False
        # The loading overlay pushes focus onto the tab strip, where j/k do
        # nothing; hand it back unless the user is typing or on another screen.
        if len(self.screen_stack) == 1 and not isinstance(self.focused, Input):
            self.focus_active_tab()
        if status:
            self.notify(status)

    # ── tables ──

    def refresh_sessions(self) -> None:
        needle = self.filter_text.lower()
        rows = [
            s
            for s in self.idx.sessions
            if not needle
            or needle in s.title.lower()
            or needle in (s.cwd or "").lower()
            or needle in s.sid
        ]
        rows.sort(key=lambda s: SORTS[self.sort_key](s, self.context_override))
        self.session_rows = rows
        repopulate(
            self.sessions_table,
            [(str(s.transcript), self.session_cells(s)) for s in rows],
        )

    def session_cells(self, s: Session) -> tuple:
        pct = s.context_pct(self.context_override)
        return (
            Text("● live", style="green") if s.live else Text(human_age(s.mtime)),
            right(human_bytes(s.total_size)),
            right(human_tokens(s.tokens)),
            Text(f"{bar(pct, 8)} {pct:>3.0f}%", style="red" if pct >= 85 else ""),
            ellipsize(s.project_name, 22),
            s.title or s.sid[:8],
        )

    def update_sort_labels(self) -> None:
        for key, label, _width in SESSION_COLUMNS:
            marker = " ▼" if key == self.sort_key else ""
            self.sessions_table.columns[ColumnKey(key)].label = Text(label + marker)
        self.sessions_table.refresh()

    def refresh_projects(self) -> None:
        self.project_rows = sorted(self.idx.projects, key=lambda p: -p.size)
        rows = []
        for p in self.project_rows:
            label = Text(p.label)
            if not p.exists:
                label.append("  ⚠ dir missing", style="red")
            memory = human_bytes(p.memory_size) if p.memory_size else "—"
            cells = (
                human_age(p.last_mtime),
                right(human_bytes(p.size)),
                right(str(len(p.sessions))),
                right(memory),
                label,
            )
            rows.append((str(p.encoded), cells))
        repopulate(self.projects_table, rows)

    def refresh_prune(self) -> None:
        self.prune_rows = reclaimable(
            self.idx, include_orphans=self.include_orphans, keep_memory=self.keep_memory
        )
        total = sum(size for _kind, _path, size in self.prune_rows)
        if self.include_orphans:
            memory = (
                "auto-memory protected (M)"
                if self.keep_memory
                else "auto-memory WILL be removed (M)"
            )
            note = f"sessions whose project dir is gone INCLUDED · {memory}"
        else:
            note = (
                "sessions whose project dir is gone excluded "
                "(O includes them; m on the Sessions tab rehomes one instead)"
            )
        verb = (
            "PERMANENTLY delete everything listed"
            if self.hard
            else f"move everything listed to {TRASH_NAME}/"
        )
        risky = self.hard or self.include_orphans or not self.keep_memory
        self.prune_summary.update(
            Text.assemble(
                (
                    f"RECLAIMABLE — {human_bytes(total)} across "
                    f"{len(self.prune_rows)} items\n",
                    f"bold {ACCENT}",
                ),
                (f"x: {verb} · {note}", "red" if risky else "dim"),
            )
        )
        repopulate(
            self.prune_table,
            [
                (
                    str(path),
                    (
                        right(human_bytes(size)),
                        ellipsize(kind, 34),
                        str(relative(path, self.idx.root)),
                    ),
                )
                for kind, path, size in self.prune_rows
            ],
        )

    # ── navigation ──

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "cycle_tab":
            return len(self.screen_stack) == 1
        tab = TAB_ACTIONS.get(action)
        return tab is None or tab == self.active_tab

    @on(TabbedContent.TabActivated)
    def tab_activated(self, event: TabbedContent.TabActivated) -> None:
        self.active_tab = event.pane.id or ""
        self.refresh_bindings()
        self.focus_active_tab()

    def focus_active_tab(self) -> None:
        focus = {
            "sessions": self.sessions_table,
            "projects": self.projects_table,
            "traffic": self.traffic_scroll,
            "prune": self.prune_table,
        }.get(self.active_tab)
        if focus is not None:
            focus.focus()

    def action_show_tab(self, tab: str) -> None:
        self.tabbed.active = tab

    def action_cycle_tab(self, step: int) -> None:
        self.tabbed.active = TABS[(TABS.index(self.active_tab) + step) % len(TABS)]

    def action_rescan(self) -> None:
        self.start_scan("rescanned")

    def action_sort(self) -> None:
        order = list(SORTS)
        self.sort_key = order[(order.index(self.sort_key) + 1) % len(order)]
        self.update_sort_labels()
        self.refresh_sessions()

    @on(DataTable.HeaderSelected, "#sessions-table")
    def header_selected(self, event: DataTable.HeaderSelected) -> None:
        if event.column_key.value in SORTS:
            self.sort_key = event.column_key.value
            self.update_sort_labels()
            self.refresh_sessions()

    def action_focus_filter(self) -> None:
        self.tabbed.active = "sessions"
        self.filter_input.can_focus = True
        self.filter_input.focus()

    @on(Input.Blurred, "#filter")
    def filter_left(self) -> None:
        self.filter_input.can_focus = False

    def action_clear_filter(self) -> None:
        if self.filter_input.value:
            self.filter_input.value = ""
        if self.active_tab == "sessions":
            self.sessions_table.focus()

    @on(Input.Changed, "#filter")
    def filter_changed(self, event: Input.Changed) -> None:
        self.filter_text = event.value.strip()
        self.refresh_sessions()

    @on(Input.Submitted, "#filter")
    def filter_submitted(self) -> None:
        self.sessions_table.focus()

    @on(DataTable.RowSelected, "#sessions-table")
    def open_session(self) -> None:
        session = selected(self.sessions_table, self.session_rows)
        if session is not None:
            self.push_screen(DetailScreen(session, self.context_override))

    @on(DataTable.RowSelected, "#projects-table")
    def open_project(self) -> None:
        project = selected(self.projects_table, self.project_rows)
        if project is None or not project.sessions:
            return
        self.tabbed.active = "sessions"
        self.filter_input.value = project.cwd or ""

    # ── actions ──

    def refuse_if_live(self, s: Session, doing: str) -> bool:
        # Check now rather than trusting the index, which may be minutes old.
        pid = live_sessions(self.claude_root).get(s.sid)
        if pid is None:
            return False
        self.notify(
            f"session is running (pid {pid}); exit it before {doing}",
            severity="warning",
        )
        return True

    def action_delete(self) -> None:
        session = selected(self.sessions_table, self.session_rows)
        if session is not None:
            self.delete_session(session)

    def delete_session(self, s: Session) -> None:
        if self.refuse_if_live(s, "deleting"):
            return
        paths = session_paths(s)
        size = sum(path_size(p) for p in paths)
        verb = "PERMANENTLY delete" if self.hard else "Move to trash:"
        question = (
            f"{verb} {len(paths)} paths ({human_bytes(size)}) for {s.title or s.sid}?"
        )

        def confirmed(ok: bool | None) -> None:
            if ok:
                self.remove_paths(paths, f"delete session {s.sid}", "paths")

        self.push_screen(ConfirmScreen(question, "yes", danger=self.hard), confirmed)

    def action_rehome(self) -> None:
        session = selected(self.sessions_table, self.session_rows)
        if session is not None:
            self.rehome_session(session)

    def rehome_session(self, s: Session) -> None:
        if self.refuse_if_live(s, "rehoming"):
            return

        def chosen(target: str | None) -> None:
            if target:
                self.rehome_worker(s, target)

        self.push_screen(
            PromptScreen(
                f"Rehome “{s.title or s.sid}” to directory:",
                value=s.cwd or "",
                suggester=DirectorySuggester(),
            ),
            chosen,
        )

    @work(thread=True, group="io")
    def rehome_worker(self, s: Session, target: str) -> None:
        ok, msg = rehome(s, target, self.claude_root)
        if ok:
            self.call_from_thread(self.after_change, msg)
        else:
            self.call_from_thread(self.notify, msg, severity="error", timeout=10)

    def action_prune(self) -> None:
        # Sessions may have started since the scan; drop anything they own.
        live = live_sessions(self.claude_root)
        rows = [r for r in self.prune_rows if not any(sid in str(r[1]) for sid in live)]
        skipped = len(self.prune_rows) - len(rows)
        if not rows:
            self.notify("nothing to prune")
            return
        total = sum(size for _kind, _path, size in rows)
        verb = "PERMANENTLY delete" if self.hard else "Move to trash:"
        question = f"{verb} {len(rows)} items ({human_bytes(total)})?"
        suffix = f" · skipped {skipped} owned by running sessions" if skipped else ""

        def confirmed(ok: bool | None) -> None:
            if ok:
                paths = [path for _kind, path, _size in rows]
                self.remove_paths(paths, "prune", "items", suffix)

        danger = self.hard or self.include_orphans
        self.push_screen(ConfirmScreen(question, "prune", danger=danger), confirmed)

    def action_toggle_orphans(self) -> None:
        self.include_orphans = not self.include_orphans
        self.refresh_prune()

    def action_toggle_memory(self) -> None:
        self.keep_memory = not self.keep_memory
        self.refresh_prune()

    @work(thread=True, group="io")
    def remove_paths(
        self, paths: list[Path], reason: str, noun: str, suffix: str = ""
    ) -> None:
        if self.hard:
            n, freed, failures = hard_delete(paths)
            msg = f"permanently deleted {n} {noun} ({human_bytes(freed)})"
        else:
            n, freed, failures, dest = move_to_trash(self.claude_root, paths, reason)
            msg = (
                f"moved {n} {noun} ({human_bytes(freed)}) to "
                f"{relative(dest, self.claude_root)} — restorable"
                if n
                else "nothing was moved to the trash"
            )
        if failures:
            self.call_from_thread(
                self.notify,
                f"{len(failures)} failed, first: {failures[0]}",
                severity="error",
                timeout=15,
            )
        self.call_from_thread(self.after_change, msg + suffix)

    def after_change(self, status: str) -> None:
        if isinstance(self.screen, DetailScreen):
            self.pop_screen()
        self.start_scan(status)


def run(root: Path, context_override: int | None, hard: bool, use_cache: bool) -> None:
    ClaudeTop(root, context_override, hard=hard, use_cache=use_cache).run()
