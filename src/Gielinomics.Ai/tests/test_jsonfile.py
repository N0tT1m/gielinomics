"""Atomic JSON writes.

The guarantee under test is not "the data gets written" -- that was never in
doubt -- but that a reader never sees a partial file. Both stores that use this
read an unparseable file as an empty one, so a half-written save is not a lost
update, it is a silently wiped store.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reldo.jsonfile import write_json


def test_it_writes_what_it_was_given(tmp_path):
    path = tmp_path / "accounts.json"
    write_json(path, {"1": "Zezima"})
    assert json.loads(path.read_text(encoding="utf-8")) == {"1": "Zezima"}


def test_it_creates_the_directory(tmp_path):
    path = tmp_path / "data" / "nested" / "progress.json"
    write_json(path, {})
    assert path.exists()


def test_indent_is_honoured(tmp_path):
    """accounts.json is meant to be readable by a human editing it by hand."""
    path = tmp_path / "accounts.json"
    write_json(path, {"1": "Zezima"}, indent=2)
    assert "\n  " in path.read_text(encoding="utf-8")


def test_the_target_still_holds_the_old_file_until_the_rename(tmp_path, monkeypatch):
    """The whole point: the target is never opened for truncation.

    `write_text` truncates and then writes, so there is a window in which the
    file on disk is neither the old contents nor the new. Writing beside it and
    renaming over removes the window rather than narrowing it.
    """
    path = tmp_path / "accounts.json"
    path.write_text('{"1": "Zezima"}', encoding="utf-8")

    during: list[str] = []
    real_replace = os.replace

    def spy(src, dst):
        during.append(Path(dst).read_text(encoding="utf-8"))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    write_json(path, {"2": "Lynx Titan"})

    assert during == ['{"1": "Zezima"}'], "the target was modified before the rename"
    assert json.loads(path.read_text(encoding="utf-8")) == {"2": "Lynx Titan"}


def test_a_failed_write_leaves_the_previous_file_intact(tmp_path, monkeypatch):
    path = tmp_path / "accounts.json"
    path.write_text('{"1": "Zezima"}', encoding="utf-8")

    def boom(src, dst):
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        write_json(path, {"2": "Lynx Titan"})

    assert json.loads(path.read_text(encoding="utf-8")) == {"1": "Zezima"}


def test_a_failed_write_leaves_no_debris(tmp_path, monkeypatch):
    """A stray .tmp beside a file that is still good reads like the corruption
    that did not happen."""
    path = tmp_path / "accounts.json"
    path.write_text("{}", encoding="utf-8")

    def boom(src, dst):
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        write_json(path, {"2": "Lynx Titan"})

    assert [p.name for p in tmp_path.iterdir()] == ["accounts.json"]


def test_the_temporary_file_is_beside_the_target(tmp_path, monkeypatch):
    """os.replace is only atomic within one filesystem, and the system temp
    directory is routinely a different one."""
    path = tmp_path / "data" / "accounts.json"
    seen: list[Path] = []

    real_replace = os.replace

    def spy(src, dst):
        seen.append(Path(src))
        return real_replace(src, dst)

    monkeypatch.setattr(os, "replace", spy)
    write_json(path, {})

    assert seen and seen[0].parent == path.parent
