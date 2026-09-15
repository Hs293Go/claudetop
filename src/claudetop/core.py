"""
Everything claudetop knows about ~/.claude that doesn't involve a terminal.

Scanning reads every transcript (cached on size and mtime), rolls sessions up
per project, and finds stray and legacy data.

Actions:
  delete    removes a session AND every file keyed to its id across ~/.claude
  rehome    moves a session to a different project, rewriting cwd in the JSONL
  prune     clears out dead weight, with auto-memory protected by default

Destructive actions move files into a trash directory with a manifest so they
can be restored; hard_delete is the only thing that unlinks.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

APP = "claudetop"
CACHE_NAME = ".claudetop-cache.json"
TRASH_NAME = ".claudetop-trash"

# Directories under ~/.claude that hold per-session data keyed by session id.
# Sourced from the "application data" table in the Claude Code docs.
SESSION_KEYED_DIRS = [
    "file-history",
    "image-cache",
    "uploads",
    "tasks",
    "debug",
    "session-env",
    "todos",  # legacy, still present on older installs
]

# Directories that current Claude Code versions no longer write to.
LEGACY_DIRS = ["todos", "statsig", "logs"]

# Names inside projects/<project>/ that are NOT sessions.
NON_SESSION_ENTRIES = {"memory"}

DEFAULT_CONTEXT_WINDOW = 200_000
LARGE_CONTEXT_WINDOW = 1_000_000

# Bump whenever parse_transcript's output changes so stale cache entries are dropped.
CACHE_VERSION = 2

# Session-keyed data touched this recently may belong to a session that has
# started but not written its transcript yet; never offer it for pruning.
RECENT_SECONDS = 600

SID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
TITLE_HINT = re.compile(r'"type":\s*"(?:custom-title|ai-title|summary)"')
USER_HINT = re.compile(r'"type":\s*"user"')


# ─────────────────────────────────────────────────────────────── model ──


@dataclass
class Session:
    sid: str
    project_dir: Path  # ~/.claude/projects/<encoded>
    transcript: Path
    size: int  # transcript bytes only
    mtime: float
    cwd: str | None = None
    git_branch: str | None = None
    events: int = 0
    first_ts: str | None = None
    last_ts: str | None = None
    title: str = ""
    models: dict[str, int] = field(default_factory=dict)
    tok_in: int = 0
    tok_out: int = 0
    tok_cache_read: int = 0
    tok_cache_write: int = 0
    context_end: int = 0  # context occupied at the last assistant turn
    context_peak: int = 0  # largest context seen on any turn
    daily: dict[str, int] = field(default_factory=dict)  # date -> total tokens
    extras: list[Path] = field(default_factory=list)  # companion paths
    extras_size: int = 0
    unreadable: bool = False
    live_pid: int | None = None  # set while a Claude Code process has it open

    @property
    def live(self) -> bool:
        return self.live_pid is not None

    @property
    def total_size(self) -> int:
        return self.size + self.extras_size

    @property
    def tokens(self) -> int:
        return self.tok_in + self.tok_out + self.tok_cache_read + self.tok_cache_write

    @property
    def project_name(self) -> str:
        if self.cwd:
            parts = Path(self.cwd).parts
            return "/".join(parts[-2:]) if len(parts) > 1 else self.cwd
        return self.project_dir.name

    def context_window(self, override: int | None = None) -> int:
        if override:
            return override
        # Transcripts don't record the window size, but any turn past the
        # standard window proves the session was running with the large one.
        if self.context_peak > DEFAULT_CONTEXT_WINDOW:
            return LARGE_CONTEXT_WINDOW
        return DEFAULT_CONTEXT_WINDOW

    def context_pct(self, override: int | None = None) -> float:
        return self.context_end / self.context_window(override) * 100.0


@dataclass
class Project:
    encoded: Path
    cwd: str | None
    sessions: list[Session] = field(default_factory=list)
    memory_size: int = 0
    dead_weight: list[Path] = field(default_factory=list)  # superseded/orphaned
    dead_weight_size: int = 0
    cwd_guess: str | None = None  # decoded from the dir name when no transcript says

    @property
    def exists(self) -> bool:
        if self.cwd:
            return Path(self.cwd).is_dir()
        return bool(self.cwd_guess) and Path(self.cwd_guess).is_dir()

    @property
    def size(self) -> int:
        return (
            sum(s.total_size for s in self.sessions)
            + self.memory_size
            + self.dead_weight_size
        )

    @property
    def label(self) -> str:
        if self.cwd:
            return tildify(self.cwd)
        if self.cwd_guess:
            return f"{tildify(self.cwd_guess)} (guessed)"
        return f"?{self.encoded.name}"

    @property
    def last_mtime(self) -> float:
        return max((s.mtime for s in self.sessions), default=0.0)


@dataclass
class Index:
    root: Path
    projects: list[Project] = field(default_factory=list)
    sessions: list[Session] = field(default_factory=list)
    strays: list[tuple[str, Path, int]] = field(default_factory=list)
    legacy: list[tuple[str, Path, int]] = field(default_factory=list)
    live: dict[str, int] = field(default_factory=dict)  # session id -> pid
    scanned_at: float = 0.0
    scan_seconds: float = 0.0

    @property
    def total_size(self) -> int:
        return sum(p.size for p in self.projects)


# ──────────────────────────────────────────────────────────── scanning ──


def claude_root() -> Path:
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".claude"


def encode_project_path(cwd: str) -> str:
    """Mirror Claude Code's project-directory encoding: non-alphanumerics -> '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", cwd)


def dir_size(path: Path) -> int:
    total = 0
    try:
        for dirpath, _dirnames, filenames in os.walk(path, onerror=lambda _err: None):
            for name in filenames:
                try:
                    total += os.lstat(os.path.join(dirpath, name)).st_size
                except OSError:
                    pass
    except OSError:
        pass
    return total


def path_size(path: Path) -> int:
    try:
        st = path.lstat()
    except OSError:
        return 0
    return dir_size(path) if path.is_dir() else st.st_size


def _first_text(obj) -> str:
    """Pull a short human-readable string out of a transcript message payload."""
    if isinstance(obj, str):
        return obj
    if isinstance(obj, list):
        for block in obj:
            if isinstance(block, dict) and block.get("type") == "text":
                return str(block.get("text", ""))
            if isinstance(block, str):
                return block
    if isinstance(obj, dict):
        return _first_text(obj.get("content", ""))
    return ""


def parse_transcript(path: Path, size: int) -> dict:
    """
    Read a session JSONL.

    Fully parses only the lines that matter (first, last, title records, the
    opening user prompt, and anything carrying a usage block); everything else
    is counted, not decoded. The fields we need only ever live on those lines,
    and the ones we skip are the big spilled tool-result records. Measured ~2x
    faster than json.loads on every line for a transcript with that shape
    (225 MB in 0.15s), and about even when nearly every line carries usage.
    """
    out = {
        "events": 0,
        "cwd": None,
        "git_branch": None,
        "first_ts": None,
        "last_ts": None,
        "title": "",
        "models": {},
        "tok_in": 0,
        "tok_out": 0,
        "tok_cache_read": 0,
        "tok_cache_write": 0,
        "context_end": 0,
        "context_peak": 0,
        "daily": {},
        "unreadable": False,
    }
    if size == 0:
        return out

    state = _ParseState()
    last_raw, last_decoded = None, False
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                if not raw.strip():
                    continue
                out["events"] += 1
                last_raw, last_decoded = raw, False
                if (
                    out["events"] == 1
                    or '"usage"' in raw
                    or TITLE_HINT.search(raw)
                    or (not state.prompt and USER_HINT.search(raw))
                ):
                    last_decoded = True
                    rec = _loads(raw)
                    if rec is not None:
                        _absorb(out, rec, state, out["events"])
    except OSError:
        out["unreadable"] = True
        _finish(out, state)
        return out

    if last_raw is not None and not last_decoded:
        rec = _loads(last_raw)
        if rec is not None:
            _absorb(out, rec, state, out["events"])
    _finish(out, state)
    return out


class _ParseState:
    """Scratch data gathered while reading a transcript, folded in at the end."""

    def __init__(self):
        # Message id -> (model, input, output, cache read, cache write, day).
        self.turns: dict[str, tuple] = {}
        self.custom_title = ""
        self.ai_title = ""
        self.summary = ""
        self.prompt = ""


def _finish(out: dict, state: _ParseState) -> None:
    for model, i, o, cr, cw, day in state.turns.values():
        if model:
            out["models"][model] = out["models"].get(model, 0) + 1
        out["tok_in"] += i
        out["tok_out"] += o
        out["tok_cache_read"] += cr
        out["tok_cache_write"] += cw
        if day:
            out["daily"][day] = out["daily"].get(day, 0) + i + o + cr + cw
    # A title the user set wins over the generated one, which wins over the
    # legacy summary record, which wins over the opening prompt.
    title = state.custom_title or state.ai_title or state.summary or state.prompt
    out["title"] = title.strip().replace("\n", " ")[:120]


def _loads(raw: str):
    try:
        rec = json.loads(raw)
        return rec if isinstance(rec, dict) else None
    except (json.JSONDecodeError, ValueError):
        return None


def _absorb(out: dict, rec: dict, state: _ParseState, line_no: int) -> None:
    if not out["cwd"] and isinstance(rec.get("cwd"), str):
        out["cwd"] = rec["cwd"]
    if not out["git_branch"] and isinstance(rec.get("gitBranch"), str):
        out["git_branch"] = rec["gitBranch"]

    ts = rec.get("timestamp")
    if isinstance(ts, str):
        if not out["first_ts"]:
            out["first_ts"] = ts
        out["last_ts"] = ts

    # Titles can be regenerated or renamed mid-session, so the last one counts.
    kind = rec.get("type")
    if kind == "custom-title" and rec.get("customTitle"):
        state.custom_title = str(rec["customTitle"])
    elif kind == "ai-title" and rec.get("aiTitle"):
        state.ai_title = str(rec["aiTitle"])
    elif kind == "summary" and rec.get("summary"):
        state.summary = str(rec["summary"])
    elif kind == "user" and not state.prompt and not rec.get("isMeta"):
        text = _first_text(rec.get("message", {})).strip()
        if text and not text.startswith("<"):
            state.prompt = text

    msg = rec.get("message")
    if not isinstance(msg, dict):
        return
    usage = msg.get("usage")
    if not isinstance(usage, dict):
        return

    i = int(usage.get("input_tokens") or 0)
    o = int(usage.get("output_tokens") or 0)
    cr = int(usage.get("cache_read_input_tokens") or 0)
    cw = int(usage.get("cache_creation_input_tokens") or 0)

    # Context occupied on this turn = everything the model was shown.
    # The last such value is the high-water mark for the session, unless a
    # /compact reset it, in which case it honestly reflects the post-compact load.
    ctx = i + cr + cw
    out["context_end"] = ctx
    out["context_peak"] = max(out["context_peak"], ctx)

    # Claude Code writes one record per content block, each repeating the
    # message's usage. Keep only the last copy: output_tokens can grow.
    model = msg.get("model")
    key = msg.get("id") or rec.get("requestId") or f"line-{line_no}"
    day = ts[:10] if isinstance(ts, str) and len(ts) >= 10 else None
    state.turns[str(key)] = (
        model if isinstance(model, str) else None,
        i,
        o,
        cr,
        cw,
        day,
    )


class Cache:
    """
    Keyed on (path, size, mtime) so unchanged transcripts are parsed once.

    Only entries looked up or added during a scan are written back, so records
    for deleted transcripts and outgrown sizes don't pile up. A cache written
    under a different CACHE_VERSION is ignored wholesale.
    """

    def __init__(self, path: Path, enabled: bool = True):
        self.path = path
        self.enabled = enabled
        self.data: dict[str, dict] = {}
        self.used: dict[str, dict] = {}
        if enabled and path.exists():
            try:
                raw = json.loads(path.read_text())
            except (OSError, ValueError):
                raw = None
            if (
                isinstance(raw, dict)
                and raw.get("version") == CACHE_VERSION
                and isinstance(raw.get("entries"), dict)
            ):
                self.data = raw["entries"]

    @staticmethod
    def key(p: Path, size: int, mtime: float) -> str:
        return f"{p}|{size}|{mtime}"

    def get(self, p: Path, size: int, mtime: float) -> dict | None:
        if not self.enabled:
            return None
        k = self.key(p, size, mtime)
        hit = self.data.get(k)
        if hit is not None:
            self.used[k] = hit
        return hit

    def put(self, p: Path, size: int, mtime: float, value: dict) -> None:
        if self.enabled:
            self.used[self.key(p, size, mtime)] = value

    def save(self) -> None:
        if not self.enabled or self.used.keys() == self.data.keys():
            return
        try:
            _write_json_atomic(
                self.path, {"version": CACHE_VERSION, "entries": self.used}, indent=None
            )
        except OSError:
            pass


def _write_json_atomic(path: Path, data, indent: int | None = 2) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=indent))
    tmp.replace(path)


SUPERSEDED = re.compile(r"^(?P<sid>.+?)\.jsonl\.superseded-.+$")
ORPHANED = re.compile(r"^(?P<sid>.+?)\.orphaned-.+\.jsonl$")


def guess_project_cwd(encoded: str, budget: int = 500) -> str | None:
    """
    Best-effort reversal of encode_project_path by walking the filesystem.

    The encoding folds '/', '.', '_' and '-' together, so it can't be decoded
    directly. Instead, descend from / into any directory whose encoded name is a
    prefix of what remains. Returns None when no existing directory matches.
    """
    remaining = [budget]

    def walk(base: str, rest: str) -> str | None:
        if not rest:
            return base
        if remaining[0] <= 0:
            return None
        remaining[0] -= 1
        try:
            with os.scandir(base) as it:
                names = sorted(e.name for e in it if e.is_dir())
        except OSError:
            return None
        for name in names:
            enc = encode_project_path(name)
            if rest == enc or rest.startswith(enc + "-"):
                found = walk(os.path.join(base, name), rest[len(enc) + 1 :])
                if found:
                    return found
        return None

    if not encoded.startswith("-"):
        return None
    return walk("/", encoded[1:])


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        # os.kill(pid, 0) terminates the process on Windows; assume alive.
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _proc_start(pid: int) -> str | None:
    """Kernel start time of a process in clock ticks since boot (Linux only)."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # comm (field 2) may contain spaces and parens; field 3 onward follows the last ')'.
    fields = stat.rsplit(")", 1)[-1].split()
    return fields[19] if len(fields) > 19 else None


def live_sessions(root: Path) -> dict[str, int]:
    """
    Session id -> pid for every Claude Code session that is currently running.

    Claude Code registers each running session in sessions/<pid>.json. Entries
    whose process is gone are ignored, and so are entries whose pid has been
    reused, detected by comparing the recorded process start time.
    """
    live: dict[str, int] = {}
    reg = root / "sessions"
    if not reg.is_dir():
        return live
    for f in reg.glob("*.json"):
        try:
            data = json.loads(f.read_text())
            pid = int(data["pid"])
            sid = str(data["sessionId"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not _pid_alive(pid):
            continue
        recorded, actual = data.get("procStart"), _proc_start(pid)
        if recorded and actual and str(recorded) != actual:
            continue
        live[sid] = pid
    return live


def scan(root: Path, use_cache: bool = True, progress=None) -> Index:
    started = time.time()
    idx = Index(root=root)
    idx.live = live_sessions(root)
    cache = Cache(root / CACHE_NAME, enabled=use_cache)
    projects_dir = root / "projects"

    known_sids: set = set()

    if projects_dir.is_dir():
        entries = sorted(p for p in projects_dir.iterdir() if p.is_dir())
        for n, pdir in enumerate(entries):
            if progress:
                progress(n, len(entries), pdir.name)
            proj = Project(encoded=pdir, cwd=None)

            for item in sorted(pdir.iterdir()):
                name = item.name
                if item.is_dir():
                    if name in NON_SESSION_ENTRIES:
                        proj.memory_size += dir_size(item)
                    continue
                m = SUPERSEDED.match(name) or ORPHANED.match(name)
                if m:
                    proj.dead_weight.append(item)
                    proj.dead_weight_size += path_size(item)
                    continue
                if not name.endswith(".jsonl"):
                    continue

                sid = name[: -len(".jsonl")]
                try:
                    st = item.stat()
                except OSError:
                    continue

                parsed = cache.get(item, st.st_size, st.st_mtime)
                if parsed is None:
                    parsed = parse_transcript(item, st.st_size)
                    cache.put(item, st.st_size, st.st_mtime, parsed)

                sess = Session(
                    sid=sid,
                    project_dir=pdir,
                    transcript=item,
                    size=st.st_size,
                    mtime=st.st_mtime,
                    cwd=parsed.get("cwd"),
                    git_branch=parsed.get("git_branch"),
                    events=parsed.get("events", 0),
                    first_ts=parsed.get("first_ts"),
                    last_ts=parsed.get("last_ts"),
                    title=parsed.get("title", ""),
                    models=parsed.get("models", {}),
                    tok_in=parsed.get("tok_in", 0),
                    tok_out=parsed.get("tok_out", 0),
                    tok_cache_read=parsed.get("tok_cache_read", 0),
                    tok_cache_write=parsed.get("tok_cache_write", 0),
                    context_end=parsed.get("context_end", 0),
                    context_peak=parsed.get("context_peak", 0),
                    daily=parsed.get("daily", {}),
                    unreadable=parsed.get("unreadable", False),
                    live_pid=idx.live.get(sid),
                )
                attach_extras(root, sess)
                proj.sessions.append(sess)
                idx.sessions.append(sess)
                known_sids.add(sid)

            # The transcript's own cwd field is authoritative; the directory
            # name is a lossy encoding we can't reliably reverse.
            for s in proj.sessions:
                if s.cwd:
                    proj.cwd = s.cwd
                    break
            for s in proj.sessions:
                if not s.cwd:
                    s.cwd = proj.cwd
            if proj.cwd is None:
                proj.cwd_guess = guess_project_cwd(pdir.name)
            idx.projects.append(proj)

    # Session-keyed data left behind after its transcript went away.
    # Legacy dirs are skipped here: they're reported whole, further down.
    now = time.time()
    for dname in SESSION_KEYED_DIRS:
        d = root / dname
        if dname in LEGACY_DIRS or not d.is_dir():
            continue
        for item in sorted(d.iterdir()):
            stem = item.name.split(".")[0]
            # Only entries named for a session are keyed data (debug/latest is a
            # symlink, not a session), and a live or just-started session may
            # own data before its transcript exists.
            if not SID_RE.match(stem) or item.is_symlink():
                continue
            if stem in known_sids or stem in idx.live:
                continue
            try:
                if now - item.lstat().st_mtime < RECENT_SECONDS:
                    continue
            except OSError:
                continue
            idx.strays.append((dname, item, path_size(item)))

    for dname in LEGACY_DIRS:
        d = root / dname
        if d.is_dir():
            sz = dir_size(d)
            if sz or any(d.iterdir()):
                idx.legacy.append((dname, d, sz))

    cache.save()
    idx.scanned_at = time.time()
    idx.scan_seconds = idx.scanned_at - started
    return idx


def attach_extras(root: Path, sess: Session) -> None:
    """Find every path outside the transcript that belongs to this session."""
    extras: list[Path] = []

    companion = sess.project_dir / sess.sid  # subagents/, tool-results/
    if companion.is_dir():
        extras.append(companion)

    for name in sorted(os.listdir(sess.project_dir)):
        if name.startswith(sess.sid) and name != f"{sess.sid}.jsonl":
            p = sess.project_dir / name
            if p.is_file():
                extras.append(p)

    for dname in SESSION_KEYED_DIRS:
        d = root / dname
        if not d.is_dir():
            continue
        try:
            for name in os.listdir(d):
                if name == sess.sid or name.startswith(sess.sid + "."):
                    extras.append(d / name)
        except OSError:
            pass

    sess.extras = extras
    sess.extras_size = sum(path_size(p) for p in extras)


# ────────────────────────────────────────────────────────────── actions ──


def trash_dir(root: Path) -> Path:
    return root / TRASH_NAME


def move_to_trash(
    root: Path, paths: Iterable[Path], reason: str
) -> tuple[int, int, list[str], Path]:
    """
    Relocate paths under a fresh trash batch and record where they came from.

    The manifest lists the whole plan before anything moves and is rewritten
    with what actually moved afterwards, so an interrupted run still leaves a
    batch that --trash lists and --restore can undo.

    Returns (paths moved, bytes moved, failure messages, batch dir).
    """
    tdir = trash_dir(root)
    tdir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest_root, n = tdir / stamp, 1
    while True:
        try:
            dest_root.mkdir()
            break
        except FileExistsError:
            n += 1
            dest_root = tdir / f"{stamp}-{n}"

    # Resolve every target up front. A path inside another listed path travels
    # with its parent, so it isn't planned on its own.
    candidates = list(dict.fromkeys(p for p in paths if p.exists() or p.is_symlink()))
    listed = set(candidates)
    plan: list[tuple[Path, Path, int]] = []
    targets: set[Path] = set()
    for p in candidates:
        if any(parent in listed for parent in p.parents):
            continue
        try:
            rel = p.relative_to(root)
        except ValueError:
            rel = Path(p.name)
        target = base = dest_root / rel
        k = 1
        while target in targets:
            k += 1
            target = base.with_name(f"{base.name}.{k}")
        targets.add(target)
        plan.append((p, target, path_size(p)))

    manifest = dest_root / "manifest.json"
    header = {"reason": reason, "when": dest_root.name}
    try:
        _write_json_atomic(
            manifest,
            {
                **header,
                "complete": False,
                "items": [{"from": str(s), "to": str(t)} for s, t, _ in plan],
            },
        )
    except OSError as exc:
        shutil.rmtree(dest_root, ignore_errors=True)
        return 0, 0, [f"could not write trash manifest: {exc}"], dest_root

    items: list[dict] = []
    failures: list[str] = []
    moved_bytes = 0
    for src, target, size in plan:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(target))
        except (OSError, shutil.Error) as exc:
            failures.append(f"{src}: {exc}")
            continue
        items.append({"from": str(src), "to": str(target)})
        moved_bytes += size

    leftovers = any(
        (f.is_file() or f.is_symlink()) and f != manifest for f in dest_root.rglob("*")
    )
    if not items and not leftovers:
        shutil.rmtree(dest_root, ignore_errors=True)
    else:
        record = {**header, "complete": True, "items": items}
        if failures:
            record["failed"] = failures
        try:
            _write_json_atomic(manifest, record)
        except OSError as exc:
            # The planned manifest stays in place, and restore skips entries
            # that never moved.
            failures.append(f"could not finalise trash manifest: {exc}")
    return len(items), moved_bytes, failures, dest_root


def list_trash(root: Path) -> list[tuple[str, dict, int]]:
    """Every trash batch, newest last, with its manifest and on-disk size."""
    tdir = trash_dir(root)
    if not tdir.is_dir():
        return []
    out = []
    for batch in sorted(tdir.iterdir()):
        mf = batch / "manifest.json"
        if not mf.is_file():
            continue
        try:
            data = json.loads(mf.read_text())
        except (OSError, ValueError):
            continue
        out.append((batch.name, data, dir_size(batch)))
    return out


def restore(root: Path, stamp: str) -> tuple[int, list[str]]:
    """
    Put a trash batch back where it came from. Skips anything already there.

    The manifest is rewritten to list only what is still in the trash, so a
    partial restore can simply be retried; the batch is removed once empty.
    """
    batch = trash_dir(root) / stamp
    mf = batch / "manifest.json"
    if not mf.is_file():
        return 0, [f"no such trash batch: {stamp}"]
    try:
        data = json.loads(mf.read_text())
    except (OSError, ValueError) as exc:
        return 0, [f"unreadable manifest for {stamp}: {exc}"]

    def present(p: Path) -> bool:
        return p.exists() or p.is_symlink()

    restored, problems, remaining = 0, [], []
    for item in data.get("items", []):
        src, dst = Path(item["to"]), Path(item["from"])
        if not present(src):
            # Absent from the trash but back in place means an earlier restore
            # got it, or an interrupted batch never moved it.
            if not present(dst):
                problems.append(f"missing from trash: {src.name}")
            continue
        if present(dst):
            problems.append(f"already present, left in trash: {dst}")
            remaining.append(item)
            continue
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            restored += 1
        except (OSError, shutil.Error) as exc:
            problems.append(f"{dst}: {exc}")
            remaining.append(item)

    if remaining:
        data["items"] = remaining
        try:
            _write_json_atomic(mf, data)
        except OSError as exc:
            problems.append(f"could not update manifest: {exc}")
    else:
        shutil.rmtree(batch, ignore_errors=True)
    return restored, problems


def hard_delete(paths: Iterable[Path]) -> tuple[int, int, list[str]]:
    """Unlink paths for good. Returns (paths removed, bytes freed, failure messages)."""
    removed, freed, failures = 0, 0, []
    for p in paths:
        if not (p.exists() or p.is_symlink()):
            continue
        size = path_size(p)
        try:
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p)
            else:
                p.unlink()
        except OSError as exc:
            failures.append(f"{p}: {exc}")
            continue
        removed += 1
        freed += size
    return removed, freed, failures


def session_paths(sess: Session) -> list[Path]:
    return [sess.transcript, *sess.extras]


def rehome(sess: Session, new_cwd: str, root: Path) -> tuple[bool, str]:
    """
    Move a session to a different project.

    Two halves: relocate the transcript (plus its companion directory) into the
    destination project folder, and rewrite cwd so `claude --resume` reopens it
    in the right place. A cwd under the old project root keeps its subpath, so a
    session that cd'd into a subdirectory stays accurate; a cwd outside the old
    root is left alone. The rewrite goes to a temp file, every move is a rename
    within ~/.claude, and a failure part way rolls back to the original layout.
    """
    new_cwd = os.path.abspath(os.path.expanduser(new_cwd))
    if not Path(new_cwd).is_dir():
        return False, f"not a directory: {new_cwd}"
    old_cwd = os.path.abspath(sess.cwd) if sess.cwd else None
    if old_cwd == new_cwd:
        return False, "session is already homed there"
    pid = live_sessions(root).get(sess.sid)
    if pid is not None:
        return False, f"session is running (pid {pid}); exit it before rehoming"

    dest_dir = root / "projects" / encode_project_path(new_cwd)
    if dest_dir == sess.project_dir:
        return False, f"{new_cwd} maps to the project folder the session is already in"
    dest = dest_dir / sess.transcript.name
    companion = sess.project_dir / sess.sid  # subagents/, tool-results/
    dest_companion = dest_dir / sess.sid
    if dest.exists() or dest_companion.exists():
        return False, f"session {sess.sid[:8]} already exists in {dest_dir.name}"

    def remap(cwd: str) -> str:
        if old_cwd is None:
            return cwd
        if cwd == old_cwd:
            return new_cwd
        base = old_cwd.rstrip("/")
        if cwd.startswith(base + "/"):
            return new_cwd.rstrip("/") + cwd[len(base) :]
        return cwd

    created_dir = not dest_dir.exists()
    tmp = dest_dir / f"{sess.sid}.jsonl.rehome-tmp"

    def abort(msg: str) -> tuple[bool, str]:
        tmp.unlink(missing_ok=True)
        if created_dir:
            try:
                dest_dir.rmdir()
            except OSError:
                pass
        return False, msg

    rewritten = 0
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        # surrogateescape round-trips bytes that aren't valid UTF-8, and
        # newline="" leaves line endings as they were.
        with (
            sess.transcript.open(
                "r", encoding="utf-8", errors="surrogateescape", newline=""
            ) as src,
            tmp.open(
                "w", encoding="utf-8", errors="surrogateescape", newline=""
            ) as out,
        ):
            for raw in src:
                rec = _loads(raw) if '"cwd"' in raw else None
                cwd = rec.get("cwd") if rec is not None else None
                if rec is not None and isinstance(cwd, str) and remap(cwd) != cwd:
                    rec["cwd"] = remap(cwd)
                    # Compact separators match how Claude Code writes records.
                    out.write(
                        json.dumps(rec, ensure_ascii=False, separators=(",", ":"))
                        + "\n"
                    )
                    rewritten += 1
                else:
                    out.write(raw)
    except OSError as exc:
        return abort(f"rewrite failed: {exc}")

    moved_companion = False
    if companion.is_dir():
        try:
            os.rename(companion, dest_companion)
            moved_companion = True
        except OSError as exc:
            return abort(f"could not move companion dir {companion.name}/: {exc}")

    def undo_companion() -> str:
        if not moved_companion:
            return ""
        try:
            os.rename(dest_companion, companion)
        except OSError as exc:
            return f"; companion dir left at {dest_companion} ({exc})"
        return ""

    try:
        os.rename(tmp, dest)
    except OSError as exc:
        note = undo_companion()
        return abort(f"move failed: {exc}{note}")

    try:
        sess.transcript.unlink()
    except OSError as exc:
        try:
            dest.unlink()
        except OSError:
            return False, (
                f"could not remove the original transcript ({exc}) or roll back; "
                f"session {sess.sid[:8]} is now in both {sess.project_dir.name} "
                f"and {dest_dir.name}"
            )
        note = undo_companion()
        return abort(f"could not remove the original transcript: {exc}{note}")

    return True, f"rehomed to {new_cwd} ({rewritten} records rewritten)"


# ───────────────────────────────────────────────────────────── format ──


def tildify(path: str | Path) -> str:
    """Replace a leading home directory in path with ~."""
    text, home = str(path), str(Path.home())
    if home not in ("", "/") and (text == home or text.startswith(home + os.sep)):
        return "~" + text[len(home) :]
    return text


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def human_tokens(n: int) -> str:
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def human_age(ts: float) -> str:
    if not ts:
        return "—"
    delta = time.time() - ts
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    if delta < 86400 * 30:
        return f"{int(delta // 86400)}d ago"
    return datetime.fromtimestamp(ts).strftime("%b %d")


def short_model(m: str) -> str:
    m = re.sub(r"^claude-", "", m)
    m = re.sub(r"-\d{8}$", "", m)
    return m


def ellipsize(s: str, width: int) -> str:
    if width <= 0:
        return ""
    return s if len(s) <= width else s[: width - 1] + "…"


def bar(pct: float, width: int) -> str:
    pct = max(0.0, min(100.0, pct))
    filled = round(pct / 100 * width)
    return "█" * filled + "·" * (width - filled)


def orphaned_sessions(idx: Index) -> list[Session]:
    """Non-empty sessions whose project directory no longer exists."""
    return [
        s
        for p in idx.projects
        if not p.exists
        for s in p.sessions
        if s.size and not s.live
    ]


def reclaimable(
    idx: Index, include_orphans: bool = False, keep_memory: bool = True
) -> list[tuple[str, Path, int]]:
    """
    Everything a prune would remove.

    Sessions whose project dir is gone are left out unless include_orphans is
    set: a renamed repo or an unmounted drive looks exactly like a deleted
    project, and rehoming is usually the better fix. Running sessions and empty
    transcripts touched in the last RECENT_SECONDS are never included.
    """
    now = time.time()
    out: list[tuple[str, Path, int]] = []
    for p in idx.projects:
        for dw in p.dead_weight:
            out.append(("superseded/orphaned transcripts", dw, path_size(dw)))
        for s in p.sessions:
            if s.size == 0 and not s.live and now - s.mtime >= RECENT_SECONDS:
                out.append(("empty transcripts", s.transcript, 0))
        if include_orphans and not p.exists and p.sessions:
            for s in p.sessions:
                if s.size == 0 or s.live:
                    continue
                for path in session_paths(s):
                    out.append(
                        ("sessions whose project dir is gone", path, path_size(path))
                    )
            if not keep_memory and p.memory_size:
                out.append(
                    (
                        "auto-memory of missing projects",
                        p.encoded / "memory",
                        p.memory_size,
                    )
                )
    for dname, path, sz in idx.strays:
        out.append((f"stray {dname}/ data with no transcript", path, sz))
    for dname, path, sz in idx.legacy:
        out.append((f"legacy {dname}/ directory", path, sz))

    seen = set()
    deduped = []
    for kind, path, sz in out:
        if path in seen:
            continue
        seen.add(path)
        deduped.append((kind, path, sz))
    return deduped
