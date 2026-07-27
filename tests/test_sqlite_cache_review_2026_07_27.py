# -*- coding: utf-8 -*-
"""[2026-07-27 未審檔案 review] sqlite_cache.py

`_ensure_initialized` 的 except 是整個模組【唯一會毀掉 30 天門診人數歷史】的路徑，
但它原本吃下所有 sqlite3.DatabaseError —— 包含 'database is locked'。
模組裡明明已經有 `_is_corruption_error` 專門分流(stability r4 為執行期路徑加的)，
偏偏這條會刪檔的沒用上。
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import sqlite_cache  # noqa: E402


@pytest.fixture()
def db(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite_cache, "get_settings_dir", lambda: str(tmp_path))
    sqlite_cache._close_cached_conn()
    sqlite_cache._initialized = False
    yield tmp_path
    sqlite_cache._close_cached_conn()
    sqlite_cache._initialized = False


def _seed(db_dir):
    assert sqlite_cache._ensure_initialized()
    sqlite_cache.save_clinic_counts({"D1": {"2026-07-27": [{"n": 3}]}})
    sqlite_cache._close_cached_conn()
    sqlite_cache._initialized = False


def _files(db_dir):
    return sorted(p.name for p in db_dir.iterdir())


def test_transient_lock_must_not_quarantine_the_database(db, caplog, monkeypatch):
    """★核心★ 暫時鎖競爭不可讓 30 天歷史被搬走。

    真實觸發情境:程式自我重啟時舊 process 還握著連線、設定目錄在網路碟/OneDrive
    卡住 → CREATE TABLE 等超過 timeout=10s → OperationalError('database is locked')。
    """
    _seed(db)
    before = _files(db)
    _real_schema = sqlite_cache._ensure_schema

    def _boom(conn):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sqlite_cache, "_ensure_schema", _boom)
    assert sqlite_cache._ensure_initialized() is False, "暫時性失敗 → 本次停用快取"
    assert _files(db) == before, f"資料檔不可被動到:{before} → {_files(db)}"
    assert not any(".corrupt-" in n for n in _files(db))

    # 下一次啟動(鎖已放掉)要能自然恢復,而且資料還在
    # (只還原 _ensure_schema — monkeypatch.undo() 會連 fixture 的
    #  get_settings_dir 一起還原,那會讓測試改讀真正的設定目錄)
    monkeypatch.setattr(sqlite_cache, "_ensure_schema", _real_schema)
    sqlite_cache._initialized = False
    data = sqlite_cache.load_clinic_counts()
    assert data["D1"]["2026-07-27"] == [{"n": 3}], "歷史快取必須完好"


def test_real_corruption_still_quarantines_and_rebuilds(db, caplog):
    """不可矯枉過正:真的損壞仍要隔離重建,否則快取永久壞死(stability 原案)。"""
    _seed(db)
    p = os.path.join(str(db), sqlite_cache.DB_FILE_NAME)
    with open(p, "wb") as f:
        f.write(b"this is definitely not a sqlite database" * 40)
    assert sqlite_cache._ensure_initialized() is True
    assert any(".corrupt-" in n for n in _files(db)), "損壞檔要被隔離"
    assert sqlite_cache.load_clinic_counts() == {}, "重建後是空的(可重新累積)"


def test_quarantine_message_states_only_what_is_known(db, caplog, monkeypatch):
    """★訊息只能陳述程式確知的事★ 舊檔搬不走(Windows 上被占用的檔不能改名)時，
    DB 其實原封不動,不可謊報「歷史快取丟失」。"""
    _seed(db)

    calls = {"n": 0}
    real = sqlite_cache._ensure_schema

    def _fail_first(conn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return real(conn)

    monkeypatch.setattr(sqlite_cache, "_ensure_schema", _fail_first)
    monkeypatch.setattr(sqlite_cache.os, "replace",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("in use")))
    monkeypatch.setattr(sqlite_cache.os, "remove",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("in use")))
    with caplog.at_level("WARNING"):
        assert sqlite_cache._ensure_initialized() is True
    msgs = [r.getMessage() for r in caplog.records]
    assert not any("歷史快取丟失" in m for m in msgs), \
        "一個檔都沒搬走就不可宣稱歷史丟失"
    assert any("資料未動" in m for m in msgs)


def _corrupt_then_recover(monkeypatch):
    """讓第一次 _ensure_schema 丟出『損壞』、第二次(重建後)正常。"""
    calls = {"n": 0}
    real = sqlite_cache._ensure_schema

    def _fail_first(conn):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.DatabaseError("database disk image is malformed")
        return real(conn)

    monkeypatch.setattr(sqlite_cache, "_ensure_schema", _fail_first)


def test_delete_fallback_is_not_reported_as_recoverable_quarantine(db, caplog,
                                                                   monkeypatch):
    """★外審 P3★ 改名失敗、退而直接刪除 → 救援副本【不存在】,
    訊息不可講成「已隔離為 .corrupt-…」讓人事後去撈檔。"""
    _seed(db)
    _corrupt_then_recover(monkeypatch)
    monkeypatch.setattr(sqlite_cache.os, "replace",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("in use")))
    with caplog.at_level("WARNING"):
        assert sqlite_cache._ensure_initialized() is True
    msgs = [r.getMessage() for r in caplog.records]
    assert any("直接刪除" in m and "沒有" in m and "救援副本" in m for m in msgs), msgs
    assert not any("救援副本仍在磁碟上" in m for m in msgs)


def test_sidecar_only_move_does_not_claim_history_lost(db, caplog, monkeypatch):
    """★外審 P3★ 只有 -wal/-shm 搬走、主 DB 沒動 → 歷史其實還在,
    不可宣稱「歷史快取丟失」。"""
    _seed(db)
    base = os.path.join(str(db), sqlite_cache.DB_FILE_NAME)
    # sidecar 必須在 _close_cached_conn 之後才存在 —— 乾淨關閉連線會 checkpoint
    # 並刪掉 -wal/-shm,所以在關閉之後才建，才能模擬「隔離當下 sidecar 還在」。
    _real_close = sqlite_cache._close_cached_conn

    def _close_then_leave_sidecars():
        _real_close()
        for suffix in ("-wal", "-shm"):
            with open(base + suffix, "wb") as f:
                f.write(b"x")

    monkeypatch.setattr(sqlite_cache, "_close_cached_conn",
                        _close_then_leave_sidecars)
    _corrupt_then_recover(monkeypatch)
    real_replace = sqlite_cache.os.replace

    def _replace(src, dst, *a, **k):
        if os.path.basename(str(src)) == sqlite_cache.DB_FILE_NAME:
            raise OSError("main db in use")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(sqlite_cache.os, "replace", _replace)
    monkeypatch.setattr(sqlite_cache.os, "remove",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("in use")))
    with caplog.at_level("WARNING"):
        assert sqlite_cache._ensure_initialized() is True
    msgs = [r.getMessage() for r in caplog.records]
    assert not any("歷史快取丟失" in m for m in msgs), msgs
    assert any("資料未動" in m for m in msgs)
    # 但也不可假裝什麼都沒發生 —— sidecar 的去向要記下來
    assert any("-wal" in m for m in msgs), msgs


def test_error_result_does_not_wipe_cached_doctor(db):
    """既有性質守門:查詢失敗(doc_data 帶 'error')不可覆蓋既有快取。"""
    _seed(db)
    sqlite_cache._initialized = False
    sqlite_cache.save_clinic_counts({"D1": {"error": "timeout"}})
    assert sqlite_cache.load_clinic_counts()["D1"]["2026-07-27"] == [{"n": 3}]
