# -*- coding: utf-8 -*-
"""Updater path validation and batch rollback tests."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import updater  # noqa: E402
from cmuh_common.atomic_io import atomic_write_text as real_atomic_write_text  # noqa: E402


def test_resolve_target_path_rejects_parent_escape(tmp_path):
    with pytest.raises(ValueError, match="超出程式目錄"):
        updater._resolve_target_path(str(tmp_path), "../outside.py")


@pytest.mark.parametrize("filename", ["", ".", os.path.abspath("absolute.py")])
def test_resolve_target_path_rejects_non_file_targets(tmp_path, filename):
    with pytest.raises(ValueError):
        updater._resolve_target_path(str(tmp_path), filename)


def test_rollback_written_files_restores_existing_and_removes_new(tmp_path):
    existing = tmp_path / "existing.py"
    created = tmp_path / "created.py"
    existing.write_text("old", encoding="utf-8")

    assert real_atomic_write_text(str(existing), "new") is True
    assert real_atomic_write_text(str(created), "created") is True

    errors = updater._rollback_written_files([
        updater._WrittenFile(str(existing), True),
        updater._WrittenFile(str(created), False),
    ])

    assert errors == []
    assert existing.read_text(encoding="utf-8") == "old"
    assert not created.exists()


def test_check_and_update_rolls_back_batch_when_later_write_fails(
    tmp_path, monkeypatch
):
    first = tmp_path / "a.py"
    first.write_text("old-a", encoding="utf-8")
    manifest = {
        "app_version": "2099.01.01.1",
        "files": [
            {"key": "a", "local_filename": "a.py"},
            {"key": "b", "local_filename": "b.py"},
        ],
    }
    precompiled = []

    monkeypatch.setattr(updater, "_fetch_manifest", lambda: manifest)
    monkeypatch.setattr(updater, "get_app_dir", lambda: str(tmp_path))
    monkeypatch.setattr(updater, "is_frozen", lambda: False)
    monkeypatch.setattr(
        updater,
        "_download_one",
        lambda entry, _app_dir: (
            entry["key"],
            entry["local_filename"],
            "2099.01.01.1",
            f"new-{entry['key']}",
        ),
    )
    monkeypatch.setattr(updater, "_precompile_files", precompiled.extend)

    def fail_second_write(path, content):
        if path.endswith("b.py"):
            return False
        return real_atomic_write_text(path, content)

    monkeypatch.setattr(updater, "atomic_write_text", fail_second_write)

    result = updater.check_and_update(write_files=True)

    assert first.read_text(encoding="utf-8") == "old-a"
    assert not (tmp_path / "b.py").exists()
    assert result.updated_files == []
    assert result.has_update is False
    assert any("[b] 寫入失敗" in error for error in result.errors)
    assert precompiled == []


def test_check_and_update_rejects_duplicate_manifest_targets(tmp_path, monkeypatch):
    manifest = {
        "app_version": "2099.01.01.1",
        "files": [
            {"key": "a", "local_filename": "same.py"},
            {"key": "b", "local_filename": "same.py"},
        ],
    }
    writes = []

    monkeypatch.setattr(updater, "_fetch_manifest", lambda: manifest)
    monkeypatch.setattr(updater, "get_app_dir", lambda: str(tmp_path))
    monkeypatch.setattr(updater, "is_frozen", lambda: False)
    monkeypatch.setattr(
        updater,
        "_download_one",
        lambda entry, _app_dir: (
            entry["key"],
            entry["local_filename"],
            "2099.01.01.1",
            entry["key"],
        ),
    )
    monkeypatch.setattr(
        updater,
        "atomic_write_text",
        lambda path, content: writes.append((path, content)) or True,
    )

    result = updater.check_and_update(write_files=True)

    assert writes == []
    assert result.updated_files == []
    assert any("更新清單重複目標" in error for error in result.errors)
