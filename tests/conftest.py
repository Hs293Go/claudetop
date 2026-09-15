"""A throwaway ~/.claude holding one of everything claudetop cares about."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

import pytest

from claudetop import core


@dataclass
class Fake:
    root: Path
    work: Path
    old: Path  # project dir of S1-S3
    new: Path  # has only auto-memory under ~/.claude
    pdir: Path  # ~/.claude/projects/<old>
    gone: Path  # ~/.claude/projects/<dir that no longer exists>

    # Titled, usage duplicated across records, companion dir, cd into a subdir.
    S1 = "11111111-1111-1111-1111-111111111111"
    # Past the standard context window, and registered as running.
    S2 = "22222222-2222-2222-2222-222222222222"
    # Named by its opening prompt; its registry entry has a reused pid.
    S3 = "33333333-3333-3333-3333-333333333333"
    # Its project directory is gone.
    S4 = "44444444-4444-4444-4444-444444444444"
    STRAY_OLD = "55555555-5555-5555-5555-555555555555"
    STRAY_NEW = "66666666-6666-6666-6666-666666666666"


def rec(**fields) -> str:
    return json.dumps(fields, separators=(",", ":"))


def usage(i: int, o: int, cr: int, cw: int) -> dict:
    return {
        "input_tokens": i,
        "output_tokens": o,
        "cache_read_input_tokens": cr,
        "cache_creation_input_tokens": cw,
    }


def age(path: Path, seconds: float = 3600) -> None:
    past = time.time() - seconds
    os.utime(path, (past, past), follow_symlinks=False)


@pytest.fixture
def fake(tmp_path: Path) -> Fake:
    base = tmp_path.resolve()
    root, work = base / "claude", base / "work"
    old, new = work / "old_proj", work / "new-proj"
    (old / "sub").mkdir(parents=True)
    (new / "sub").mkdir(parents=True)
    pdir = root / "projects" / core.encode_project_path(str(old))
    pdir.mkdir(parents=True)

    s1 = [
        rec(type="mode", mode="normal", sessionId=Fake.S1),
        rec(
            type="user",
            cwd=str(old),
            timestamp="2026-09-01T10:00:00Z",
            message={"role": "user", "content": "hello world prompt"},
        ),
        rec(
            type="assistant",
            cwd=str(old),
            timestamp="2026-09-01T10:00:01Z",
            message={
                "id": "m1",
                "model": "claude-opus-5",
                "usage": usage(10, 5, 100, 50),
            },
        ),
        rec(
            type="assistant",
            cwd=str(old),
            timestamp="2026-09-01T10:00:01Z",
            message={
                "id": "m1",
                "model": "claude-opus-5",
                "usage": usage(10, 7, 100, 50),
            },
        ),
        rec(type="ai-title", aiTitle="Generated title", sessionId=Fake.S1),
        rec(type="custom-title", customTitle="My title", sessionId=Fake.S1),
        rec(
            type="user", cwd=str(old / "sub"), message={"role": "user", "content": "x"}
        ),
        rec(type="user", cwd="/elsewhere", message={"role": "user", "content": "y"}),
    ]
    (pdir / f"{Fake.S1}.jsonl").write_bytes(
        ("\n".join(s1) + "\n").encode() + b'{"type":"note","text":"bad \xff byte"}\n'
    )
    (pdir / Fake.S1 / "subagents").mkdir(parents=True)
    (pdir / Fake.S1 / "subagents" / "agent-a.jsonl").write_text("{}\n")

    (pdir / f"{Fake.S2}.jsonl").write_text(
        rec(
            type="assistant",
            cwd=str(old),
            timestamp="2026-09-02T10:00:00Z",
            message={
                "id": "m9",
                "model": "claude-opus-5",
                "usage": usage(1, 1, 300_000, 0),
            },
        )
        + "\n"
    )
    (pdir / f"{Fake.S3}.jsonl").write_text(
        rec(type="user", cwd=str(old), message={"role": "user", "content": "three"})
        + "\n"
    )

    registry = root / "sessions"
    registry.mkdir()
    me = os.getpid()
    (registry / f"{me}.json").write_text(
        json.dumps({"pid": me, "sessionId": Fake.S2, "procStart": core._proc_start(me)})
    )
    (registry / "1.json").write_text(
        json.dumps({"pid": me, "sessionId": Fake.S3, "procStart": "1"})
    )

    for sid in (Fake.S1, Fake.STRAY_OLD, Fake.STRAY_NEW):
        (root / "file-history" / sid).mkdir(parents=True)
        (root / "file-history" / sid / "f").write_text("data")
    age(root / "file-history" / Fake.STRAY_OLD)
    (root / "debug").mkdir()
    (root / "debug" / f"{Fake.STRAY_OLD}.txt").write_text("log")
    age(root / "debug" / f"{Fake.STRAY_OLD}.txt")
    (root / "debug" / "latest").symlink_to(root / "debug" / f"{Fake.STRAY_OLD}.txt")
    age(root / "debug" / "latest")

    memory = root / "projects" / core.encode_project_path(str(new)) / "memory"
    memory.mkdir(parents=True)
    (memory / "notes.md").write_text("remember")

    gone = root / "projects" / core.encode_project_path(str(work / "gone"))
    gone.mkdir()
    (gone / f"{Fake.S4}.jsonl").write_text(
        rec(
            type="user",
            cwd=str(work / "gone"),
            message={"role": "user", "content": "orphan"},
        )
        + "\n"
    )

    return Fake(root=root, work=work, old=old, new=new, pdir=pdir, gone=gone)
