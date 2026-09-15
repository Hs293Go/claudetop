"""Scanning, parsing, and the destructive actions, against a fake ~/.claude."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime

import pytest

from claudetop import core


def sessions(root) -> dict[str, core.Session]:
    return {s.sid: s for s in core.scan(root, use_cache=False).sessions}


# ── parsing ──


def test_usage_counted_once_per_message(fake):
    s1 = sessions(fake.root)[fake.S1]
    assert s1.tokens == 10 + 7 + 100 + 50
    assert s1.models == {"claude-opus-5": 1}


def test_title_precedence(fake):
    by = sessions(fake.root)
    assert by[fake.S1].title == "My title"
    assert by[fake.S3].title == "three"


def test_context_window_follows_peak(fake):
    by = sessions(fake.root)
    assert by[fake.S1].context_window() == core.DEFAULT_CONTEXT_WINDOW
    assert by[fake.S2].context_window() == core.LARGE_CONTEXT_WINDOW
    assert by[fake.S2].context_pct() < 100
    assert by[fake.S2].context_window(500_000) == 500_000


def test_tildify_contracts_only_paths_under_home(monkeypatch):
    monkeypatch.setenv("HOME", "/home/alex")
    assert core.tildify("/home/alex") == "~"
    assert core.tildify("/home/alex/src/app") == "~/src/app"
    assert core.tildify("/home/alexander/src") == "/home/alexander/src"
    assert core.tildify("/srv/app") == "/srv/app"


# ── scanning ──


def test_strays_skip_symlinks_recent_data_and_non_session_names(fake):
    strays = {path for _d, path, _s in core.scan(fake.root, use_cache=False).strays}
    assert fake.root / "file-history" / fake.STRAY_OLD in strays
    assert fake.root / "debug" / f"{fake.STRAY_OLD}.txt" in strays
    assert fake.root / "file-history" / fake.STRAY_NEW not in strays
    assert fake.root / "debug" / "latest" not in strays


def test_memory_only_project_cwd_is_guessed(fake):
    idx = core.scan(fake.root, use_cache=False)
    encoded = core.encode_project_path(str(fake.new))
    project = next(p for p in idx.projects if p.encoded.name == encoded)
    assert project.cwd is None
    assert project.cwd_guess == str(fake.new)
    assert project.exists


def test_cache_is_versioned_and_drops_outgrown_entries(fake):
    core.scan(fake.root)
    cache = fake.root / core.CACHE_NAME
    data = json.loads(cache.read_text())
    assert data["version"] == core.CACHE_VERSION
    count = len(data["entries"])

    with (fake.pdir / f"{fake.S3}.jsonl").open("a") as fh:
        fh.write('{"type":"system"}\n')
    core.scan(fake.root)
    assert len(json.loads(cache.read_text())["entries"]) == count

    transcript = fake.pdir / f"{fake.S1}.jsonl"
    st = transcript.stat()
    key = core.Cache.key(transcript, st.st_size, st.st_mtime)
    stale = {"version": core.CACHE_VERSION - 1, "entries": {key: {"title": "stale"}}}
    cache.write_text(json.dumps(stale))
    assert {s.sid: s for s in core.scan(fake.root).sessions}[
        fake.S1
    ].title == "My title"


def test_live_sessions_ignore_reused_pids(fake):
    assert core.live_sessions(fake.root) == {fake.S2: os.getpid()}
    assert sessions(fake.root)[fake.S2].live


def test_orphans_are_opt_in(fake):
    idx = core.scan(fake.root, use_cache=False)
    orphan = fake.gone / f"{fake.S4}.jsonl"
    assert orphan not in {path for _k, path, _s in core.reclaimable(idx)}
    included = core.reclaimable(idx, include_orphans=True)
    assert orphan in {path for _k, path, _s in included}
    assert [s.sid for s in core.orphaned_sessions(idx)] == [fake.S4]


# ── rehome ──


def test_rehome_refuses_running_session(fake):
    ok, msg = core.rehome(sessions(fake.root)[fake.S2], str(fake.new), fake.root)
    assert not ok
    assert "running" in msg


def test_rehome_remaps_cwd_and_preserves_other_bytes(fake):
    original = (fake.pdir / f"{fake.S1}.jsonl").read_bytes()
    ok, msg = core.rehome(sessions(fake.root)[fake.S1], str(fake.new), fake.root)
    assert ok, msg

    dest_dir = fake.root / "projects" / core.encode_project_path(str(fake.new))
    lines = (dest_dir / f"{fake.S1}.jsonl").read_bytes().splitlines(keepends=True)
    cwds = [json.loads(line).get("cwd") for line in lines if b"\xff" not in line]
    assert [c for c in cwds if c] == [str(fake.new)] * 3 + [
        str(fake.new / "sub"),
        "/elsewhere",
    ]
    assert all(b'", "' not in line and b'": ' not in line for line in lines)
    untouched = [
        line for line in original.splitlines(keepends=True) if b'"cwd"' not in line
    ]
    assert all(line in lines for line in untouched)  # incl. the invalid UTF-8 one

    assert (dest_dir / fake.S1 / "subagents" / "agent-a.jsonl").exists()
    assert not (fake.pdir / f"{fake.S1}.jsonl").exists()
    assert not (fake.pdir / fake.S1).exists()
    assert not list(dest_dir.glob("*.rehome-tmp"))


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_rehome_rolls_back_when_original_cannot_be_removed(fake):
    s3 = sessions(fake.root)[fake.S3]
    target = fake.work / "third"
    target.mkdir()
    fake.pdir.chmod(0o555)
    try:
        ok, msg = core.rehome(s3, str(target), fake.root)
    finally:
        fake.pdir.chmod(0o755)
    assert not ok, msg
    assert (fake.pdir / f"{fake.S3}.jsonl").exists()
    assert not (fake.root / "projects" / core.encode_project_path(str(target))).exists()


# ── trash ──


class FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 1, 1, 12, 0, 0)


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_trash_batches_are_unique_and_report_failures(fake, monkeypatch):
    monkeypatch.setattr(core, "datetime", FrozenDatetime)
    a, b = fake.work / "a", fake.work / "b"
    a.write_text("aaaa")
    b.write_text("bb")
    locked = fake.work / "locked"
    locked.mkdir()
    (locked / "c").write_text("c")
    locked.chmod(0o555)
    try:
        n1, bytes1, fail1, batch1 = core.move_to_trash(fake.root, [a], "one")
        n2, bytes2, fail2, batch2 = core.move_to_trash(
            fake.root, [b, locked / "c"], "two"
        )
    finally:
        locked.chmod(0o755)

    assert (batch1.name, batch2.name) == ("20260101-120000", "20260101-120000-2")
    assert (n1, bytes1, fail1) == (1, 4, [])
    assert (n2, bytes2) == (1, 2)
    assert len(fail2) == 1
    assert "locked/c" in fail2[0]
    manifest = json.loads((batch2 / "manifest.json").read_text())
    assert manifest["complete"]
    assert len(manifest["items"]) == 1
    assert len(manifest["failed"]) == 1


def test_interrupted_batch_is_listed_and_restorable(fake, monkeypatch):
    first, second = fake.work / "c1", fake.work / "c2"
    first.write_text("1")
    second.write_text("2")
    real_move, calls = shutil.move, []

    def flaky(src, dst):
        calls.append(src)
        if len(calls) == 2:
            raise KeyboardInterrupt
        return real_move(src, dst)

    monkeypatch.setattr(core.shutil, "move", flaky)
    with pytest.raises(KeyboardInterrupt):
        core.move_to_trash(fake.root, [first, second], "interrupted")
    monkeypatch.undo()

    [(name, data, _size)] = core.list_trash(fake.root)
    assert data["complete"] is False
    assert core.restore(fake.root, name) == (1, [])
    assert first.exists()
    assert second.exists()


def test_partial_restore_can_be_retried(fake):
    a, b = fake.work / "a", fake.work / "b"
    a.write_text("a")
    b.write_text("bb")
    *_, batch = core.move_to_trash(fake.root, [a, b], "two")

    b.write_text("blocker")
    restored, problems = core.restore(fake.root, batch.name)
    assert restored == 1
    assert len(problems) == 1
    assert batch.is_dir()

    b.unlink()
    assert core.restore(fake.root, batch.name) == (1, [])
    assert not batch.exists()
    assert b.read_text() == "bb"


def test_hard_delete_reports_freed_bytes(fake):
    target = fake.work / "d"
    target.mkdir()
    (target / "f").write_text("12345")
    assert core.hard_delete([target, fake.work / "missing"]) == (1, 5, [])
