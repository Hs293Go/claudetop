"""The non-interactive command line."""

from __future__ import annotations

import json
import subprocess
import sys
from importlib.metadata import version

import pytest

from claudetop import cli, core


def test_version_flag(capsys):
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"claudetop {version('claudetop')}"


def test_report_lists_orphans_separately(fake, capsys):
    assert cli.main(["--root", str(fake.root), "--report", "--no-cache"]) == 0
    out = capsys.readouterr().out
    assert "ORPHANED  1 sessions" in out
    assert "sessions whose project dir is gone" not in out


def test_json_output(fake, capsys):
    assert cli.main(["--root", str(fake.root), "--json", "--no-cache"]) == 0
    data = json.loads(capsys.readouterr().out)
    by = {s["sid"]: s for s in data["sessions"]}
    assert by[fake.S2]["live"]
    assert by[fake.S2]["context_window"] == core.LARGE_CONTEXT_WINDOW
    assert by[fake.S1]["title"] == "My title"
    assert data["orphaned_sessions"] == [fake.S4]


def test_trash_listing_and_restore(fake, capsys):
    target = fake.work / "x"
    target.write_text("x")
    core.move_to_trash(fake.root, [target], "manual")
    assert cli.main(["--root", str(fake.root), "--trash"]) == 0
    assert "manual" in capsys.readouterr().out
    assert cli.main(["--root", str(fake.root), "--restore", "latest"]) == 0
    assert target.read_text() == "x"


def test_non_interactive_modes_do_not_import_textual():
    code = "import sys, claudetop.cli; assert 'textual' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)
