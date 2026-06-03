# -*- coding: utf-8 -*-
"""Updater path validation and batch rollback tests."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import updater  # noqa: E402
from cmuh_common.atomic_io import atomic_write_text as real_atomic_write_text  # noqa: E402


class _FakeResponse:
    def __init__(self, *, text="", json_data=None):
        self.text = text
        self.encoding = None
        self._json_data = json_data

    def raise_for_status(self):
        return None

    def json(self):
        return self._json_data


@pytest.fixture(autouse=True)
def _isolate_updater_state(tmp_path, monkeypatch):
    """每個測試獨立的 commit SHA 快取檔 + 乾淨的記憶體狀態。

    fix② 新增了 commit SHA 磁碟快取；測試時導向 tmp，避免污染使用者 settings。
    """
    monkeypatch.setattr(updater, "_commit_sha_cache", "")
    updater._sha_mismatch_until.clear()
    cache_file = tmp_path / "last_commit_sha.txt"
    monkeypatch.setattr(updater, "_commit_sha_cache_path", lambda: str(cache_file))
    yield
    updater._sha_mismatch_until.clear()


def test_fetch_manifest_uses_unique_cache_buster(monkeypatch):
    urls = []
    commit_sha = "a" * 40
    responses = iter([
        _FakeResponse(json_data={"object": {"sha": commit_sha}}),
        _FakeResponse(json_data={"files": []}),
    ])
    monkeypatch.setattr(updater.time, "time_ns", lambda: 123456789)
    monkeypatch.setattr(
        updater.requests,
        "get",
        lambda url, timeout, **_kwargs: (urls.append(url) or next(responses)),
    )

    assert updater._fetch_manifest() == {
        "files": [],
        "_remote_commit_sha": commit_sha,
    }
    assert urls == [
        f"{updater.API_REF_URL}?t=123456789",
        (
            "https://raw.githubusercontent.com/"
            f"{updater.GITHUB_OWNER}/{updater.GITHUB_REPO}/{commit_sha}"
            f"/manifest.json?v={commit_sha}&t=123456789"
        ),
    ]


def test_download_one_uses_expected_sha_as_cache_key(tmp_path, monkeypatch):
    content = "print('new')\n"
    expected_sha = updater._sha256_text(content)
    urls = []
    monkeypatch.setattr(
        updater.requests,
        "get",
        lambda url, timeout: (urls.append(url) or _FakeResponse(text=content)),
    )
    entry = {
        "key": "sample",
        "remote_path": "src/sample.py",
        "local_filename": "src/sample.py",
        "version": "2099.01.01.1",
        "sha256": expected_sha,
    }

    result = updater._download_one(entry, str(tmp_path))

    assert result == ("sample", "src/sample.py", "2099.01.01.1", content)
    assert urls == [
        f"{updater.RAW_BASE}/src/sample.py?v={expected_sha}"
    ]


def test_download_one_uses_manifest_commit_sha(tmp_path, monkeypatch):
    content = "print('pinned')\n"
    expected_sha = updater._sha256_text(content)
    commit_sha = "b" * 40
    urls = []
    monkeypatch.setattr(
        updater.requests,
        "get",
        lambda url, timeout: (urls.append(url) or _FakeResponse(text=content)),
    )
    entry = {
        "key": "sample",
        "remote_path": "src/sample.py",
        "local_filename": "src/sample.py",
        "version": "2099.01.01.1",
        "sha256": expected_sha,
        "_remote_commit_sha": commit_sha,
    }

    assert updater._download_one(entry, str(tmp_path)) is not None
    assert urls == [
        (
            "https://raw.githubusercontent.com/"
            f"{updater.GITHUB_OWNER}/{updater.GITHUB_REPO}/{commit_sha}"
            f"/src/sample.py?v={expected_sha}"
        )
    ]


# ---- fix②：commit SHA 快取，403 限流時沿用上次成功的 commit ----

def test_resolve_commit_sha_reuses_cache_when_api_fails(monkeypatch):
    good_sha = "c" * 40
    # 第一次：API 成功 → 回 SHA 並寫入快取（記憶體 + 磁碟）
    monkeypatch.setattr(
        updater.requests, "get",
        lambda url, timeout, **_k: _FakeResponse(json_data={"object": {"sha": good_sha}}),
    )
    assert updater._resolve_commit_sha(5) == good_sha
    assert updater._load_cached_commit_sha() == good_sha

    # 第二次：API 403 / 連線中斷 → 沿用快取，不退回 branch（不回 ""）
    def boom(url, timeout, **_k):
        raise RuntimeError("403 rate limit exceeded")

    monkeypatch.setattr(updater.requests, "get", boom)
    assert updater._resolve_commit_sha(5) == good_sha


def test_resolve_commit_sha_returns_empty_without_cache(monkeypatch):
    # 無快取 + API 失敗 → 回 ''（呼叫端最後才退回 branch 路徑）
    monkeypatch.setattr(
        updater.requests, "get",
        lambda url, timeout, **_k: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    assert updater._resolve_commit_sha(5) == ""


def test_fetch_manifest_pins_to_cached_sha_on_api_failure(monkeypatch):
    cached_sha = "d" * 40
    monkeypatch.setattr(updater, "_commit_sha_cache", cached_sha)
    monkeypatch.setattr(updater.time, "time_ns", lambda: 42)
    urls = []

    def fake_get(url, timeout, **_k):
        urls.append(url)
        if url.startswith(updater.API_REF_URL):
            raise RuntimeError("403 rate limit exceeded")
        return _FakeResponse(json_data={"files": []})

    monkeypatch.setattr(updater.requests, "get", fake_get)

    manifest = updater._fetch_manifest()

    # API 失敗仍把 manifest 釘在 cached commit（不是 branch /main/ 舊版路徑）
    assert manifest["_remote_commit_sha"] == cached_sha
    assert urls[-1] == (
        "https://raw.githubusercontent.com/"
        f"{updater.GITHUB_OWNER}/{updater.GITHUB_REPO}/{cached_sha}"
        f"/manifest.json?v={cached_sha}&t=42"
    )


# ---- fix①：單檔重試，不要一次就鎖 1 小時 ----

def test_download_verified_retries_then_succeeds_with_cache_buster(monkeypatch):
    content = "ok\n"
    sha = updater._sha256_text(content)
    seq = iter(["WRONG", content])  # 第一次 SHA 不符，第二次正確
    calls = []

    def fake_get(url, timeout):
        calls.append(url)
        return _FakeResponse(text=next(seq))

    monkeypatch.setattr(updater.requests, "get", fake_get)
    monkeypatch.setattr(updater.time, "sleep", lambda *_a: None)
    monkeypatch.setattr(updater.time, "time_ns", lambda: 999)

    base = f"{updater.RAW_BASE}/x.py?v={sha}"
    assert updater._download_verified("x", base, sha) == content
    # 第一次乾淨網址（共用 CDN 快取）；重試才加 nanotime 防快取
    assert calls == [base, f"{base}&t=999"]


def test_download_verified_returns_none_after_attempts(monkeypatch):
    monkeypatch.setattr(
        updater.requests, "get",
        lambda url, timeout: _FakeResponse(text="bad"),
    )
    monkeypatch.setattr(updater.time, "sleep", lambda *_a: None)

    base = f"{updater.RAW_BASE}/x.py?v=deadbeef"
    assert updater._download_verified("x", base, "f" * 64) is None


def test_download_one_uses_short_backoff_after_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(
        updater.requests, "get",
        lambda url, timeout: _FakeResponse(text="bad"),
    )
    monkeypatch.setattr(updater.time, "sleep", lambda *_a: None)
    entry = {
        "key": "z",
        "remote_path": "src/z.py",
        "local_filename": "src/z.py",
        "version": "2099.01.01.1",
        "sha256": "a" * 64,
    }

    with pytest.raises(ValueError, match="分鐘"):
        updater._download_one(entry, str(tmp_path))

    # backoff 鎖較短時間（10 分鐘），絕不是舊版的 1 小時
    until = updater._sha_mismatch_until.get("z", 0.0)
    remaining = until - updater.time.time()
    assert 0 < remaining <= updater._DOWNLOAD_FAIL_BACKOFF_SEC + 1
    assert updater._DOWNLOAD_FAIL_BACKOFF_SEC < 3600


def test_check_and_update_rejects_stale_manifest_before_download(monkeypatch):
    monkeypatch.setattr(updater, "is_frozen", lambda: False)
    monkeypatch.setattr(
        updater,
        "_fetch_manifest",
        lambda: {"app_version": "2000.01.01.1", "files": [{"key": "old"}]},
    )
    monkeypatch.setattr(
        updater,
        "_download_one",
        lambda *_args: pytest.fail("stale manifest must not download files"),
    )

    result = updater.check_and_update(write_files=True)

    assert result.checked is True
    assert result.has_update is False
    assert result.updated_files == []


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
