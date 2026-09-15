"""Command line entry point: the TUI, or a report, JSON dump, or trash operation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from importlib.metadata import version
from pathlib import Path

from claudetop.core import (
    APP,
    DEFAULT_CONTEXT_WINDOW,
    LARGE_CONTEXT_WINDOW,
    Index,
    claude_root,
    ellipsize,
    human_bytes,
    human_tokens,
    list_trash,
    orphaned_sessions,
    reclaimable,
    restore,
    scan,
)

# ──────────────────────────────────────────────────────── report mode ──


def print_report(idx: Index, window: int | None) -> None:
    print(f"\n{APP} — {idx.root}")
    print(
        f"{len(idx.sessions)} sessions across {len(idx.projects)} projects, "
        f"{human_bytes(idx.total_size)} on disk "
        f"(scanned in {idx.scan_seconds:.1f}s)\n"
    )

    print("LARGEST SESSIONS")
    for s in sorted(idx.sessions, key=lambda x: -x.total_size)[:10]:
        print(
            f"  {human_bytes(s.total_size):>9}  {human_tokens(s.tokens):>7} tok  "
            f"{s.context_pct(window):>4.0f}% ctx  {ellipsize(s.project_name, 24):<24} "
            f"{ellipsize(s.title or s.sid, 40)}"
        )

    print("\nBUSIEST PROJECTS")
    for p in sorted(idx.projects, key=lambda x: -x.size)[:10]:
        flag = "" if p.exists else "  [dir missing]"
        print(
            f"  {human_bytes(p.size):>9}  {len(p.sessions):>3} sessions  "
            f"{ellipsize(p.label, 46):<46}{flag}"
        )

    reclaim = reclaimable(idx)
    total = sum(sz for _, _, sz in reclaim)
    print(f"\nRECLAIMABLE  {human_bytes(total)}")
    buckets: dict[str, tuple[int, int]] = defaultdict(lambda: (0, 0))
    for kind, _p, sz in reclaim:
        c, s = buckets[kind]
        buckets[kind] = (c + 1, s + sz)
    for kind, (count, sz) in sorted(buckets.items(), key=lambda kv: -kv[1][1]):
        print(f"  {human_bytes(sz):>9}  {count:>4} items  {kind}")

    orphans = orphaned_sessions(idx)
    if orphans:
        size = sum(s.total_size for s in orphans)
        print(
            f"\nORPHANED  {len(orphans)} sessions, {human_bytes(size)} "
            "— project dir is gone (not counted above)"
        )
        for s in sorted(orphans, key=lambda x: -x.total_size)[:10]:
            print(
                f"  {human_bytes(s.total_size):>9}  "
                f"{ellipsize(s.cwd or s.project_dir.name, 46):<46} "
                f"{ellipsize(s.title or s.sid, 30)}"
            )
        print("  rehome them with m, or press O on the Prune tab to include them")
    print()


# ─────────────────────────────────────────────────────────────── entry ──


def main(argv: list[str] | None = None) -> int:
    try:
        return _run(argv)
    except BrokenPipeError:
        # `claudetop --report | head` closes the pipe early; exit quietly.
        try:
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        except OSError:
            pass
        return 0
    except KeyboardInterrupt:
        return 130


def _run(argv: list[str] | None) -> int:
    ap = argparse.ArgumentParser(
        prog=APP,
        description="Inspect and clean up Claude Code sessions on disk.",
    )
    ap.add_argument(
        "--version", action="version", version=f"%(prog)s {version('claudetop')}"
    )
    ap.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Claude config dir (default: $CLAUDE_CONFIG_DIR or ~/.claude)",
    )
    ap.add_argument(
        "--report", action="store_true", help="print a summary and exit, no TUI"
    )
    ap.add_argument(
        "--json", action="store_true", help="dump the scan as JSON and exit"
    )
    ap.add_argument(
        "--context-window",
        type=int,
        default=None,
        help=(
            f"tokens, for the CTX gauge (default: {DEFAULT_CONTEXT_WINDOW:,} per "
            f"session, or {LARGE_CONTEXT_WINDOW:,} once any turn went past that)"
        ),
    )
    ap.add_argument(
        "--hard",
        action="store_true",
        help="delete permanently instead of moving to the trash dir",
    )
    ap.add_argument(
        "--no-cache",
        action="store_true",
        help="re-parse every transcript instead of using the cache",
    )
    ap.add_argument(
        "--trash",
        action="store_true",
        help="list what is sitting in the trash directory",
    )
    ap.add_argument(
        "--restore", metavar="BATCH", help="restore a trash batch by name, or 'latest'"
    )
    args = ap.parse_args(argv)

    root = args.root.expanduser() if args.root else claude_root()
    if not root.is_dir():
        print(f"{APP}: no Claude directory at {root}", file=sys.stderr)
        return 1

    if args.trash:
        batches = list_trash(root)
        if not batches:
            print(f"{APP}: trash is empty")
            return 0
        for name, data, size in batches:
            flag = "  (interrupted)" if data.get("complete") is False else ""
            print(
                f"  {name}  {human_bytes(size):>9}  "
                f"{len(data.get('items', [])):>3} paths  {data.get('reason', '')}{flag}"
            )
        print(f"\nrestore with: {APP} --restore <batch>   (or 'latest')")
        return 0

    if args.restore:
        batches = list_trash(root)
        if not batches:
            print(f"{APP}: trash is empty", file=sys.stderr)
            return 1
        stamp = batches[-1][0] if args.restore == "latest" else args.restore
        n, problems = restore(root, stamp)
        print(f"{APP}: restored {n} paths from {stamp}")
        for p in problems:
            print(f"  ! {p}", file=sys.stderr)
        return 0 if not problems else 1

    if not (args.report or args.json):
        # Imported here so --report and --json don't pay Textual's startup cost.
        from claudetop.tui import run as run_tui

        run_tui(root, args.context_window, hard=args.hard, use_cache=not args.no_cache)
        return 0

    idx = scan(root, use_cache=not args.no_cache)

    if args.json:
        payload = {
            "root": str(root),
            "total_size": idx.total_size,
            "sessions": [
                {
                    "sid": s.sid,
                    "cwd": s.cwd,
                    "title": s.title,
                    "size": s.size,
                    "extras_size": s.extras_size,
                    "events": s.events,
                    "tokens": s.tokens,
                    "tok_in": s.tok_in,
                    "tok_out": s.tok_out,
                    "tok_cache_read": s.tok_cache_read,
                    "tok_cache_write": s.tok_cache_write,
                    "context_end": s.context_end,
                    "context_peak": s.context_peak,
                    "context_window": s.context_window(args.context_window),
                    "last_ts": s.last_ts,
                    "models": s.models,
                    "live": s.live,
                }
                for s in idx.sessions
            ],
            "reclaimable": [
                {"kind": k, "path": str(p), "size": sz} for k, p, sz in reclaimable(idx)
            ],
            "orphaned_sessions": [s.sid for s in orphaned_sessions(idx)],
        }
        json.dump(payload, sys.stdout, indent=2)
        print()
        return 0

    print_report(idx, args.context_window)
    return 0
