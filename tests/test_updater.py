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

    # 兩階段寫入：Phase 1 全部寫到 .upd.tmp（成功），Phase 2 逐檔 os.replace。
    # 模擬「b.py 的 os.replace 失敗」→ 應回滾已 replace 的 a.py（從 .bak 還原），
    # 整批視為失敗。這正是測新版「先全寫 tmp、再全 replace」的回滾路徑。
    real_replace = os.replace

    def fail_replace_b(src, dst):
        if str(dst).endswith("b.py"):
            raise OSError("simulated replace failure")
        return real_replace(src, dst)

    monkeypatch.setattr(updater.os, "replace", fail_replace_b)

    result = updater.check_and_update(write_files=True)

    assert first.read_text(encoding="utf-8") == "old-a"  # a.py 已從 .bak 回滾
    assert not (tmp_path / "b.py").exists()
    assert result.updated_files == []
    assert result.has_update is False
    assert any("[b] 寫入失敗" in error for error in result.errors)
    assert precompiled == []
    # 失敗後不留下 .upd.tmp 暫存殘檔
    assert not list(tmp_path.glob("*.upd.tmp"))


def test_check_and_update_rejects_duplicate_manifest_targets(tmp_path, monkeypatch):
    manifest = {
        "app_version": "2099.01.01.1",
        "files": [
            {"key": "a", "local_filename": "same.py"},
            {"key": "b", "local_filename": "same.py"},
        ],
    }
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

    result = updater.check_and_update(write_files=True)

    # 重複目標在「寫入前」的清單驗證就被擋下 → 一個檔都不該被建立
    assert not (tmp_path / "same.py").exists()
    assert result.updated_files == []
    assert any("更新清單重複目標" in error for error in result.errors)
