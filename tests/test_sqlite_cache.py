# -*- coding: utf-8 -*-
"""sqlite_cache migration tests."""
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common import sqlite_cache as sc  # noqa: E402


def test_migrate_legacy_json_backs_up_corrupt_file(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(sc, "get_settings_dir", lambda: tmp)
        legacy = os.path.join(tmp, sc.LEGACY_JSON_NAME)
        with open(legacy, "w", encoding="utf-8") as f:
            f.write("{broken")

        conn = sqlite3.connect(":memory:")
        sc._ensure_schema(conn)

        assert sc._migrate_legacy_json_if_present(conn) is False
        assert not os.path.exists(legacy)
        backups = [n for n in os.listdir(tmp)
                   if n.startswith(sc.LEGACY_JSON_NAME + ".corrupt-")]
        assert len(backups) == 1


def test_migrate_legacy_json_imports_rows_and_renames_backup(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr(sc, "get_settings_dir", lambda: tmp)
        legacy = os.path.join(tmp, sc.LEGACY_JSON_NAME)
        with open(legacy, "w", encoding="utf-8") as f:
            f.write('{"D123": {"2026-05-24": [1, 2, 3]}}')

        conn = sqlite3.connect(":memory:")
        sc._ensure_schema(conn)

        assert sc._migrate_legacy_json_if_present(conn) is True
        rows = conn.execute(
            "SELECT doc_no, date_iso, payload FROM clinic_counts"
        ).fetchall()
        assert rows == [("D123", "2026-05-24", "[1, 2, 3]")]
        assert not os.path.exists(legacy)
        assert os.path.exists(legacy + ".migrated.bak")


def test_save_selected_doctor_empty_result_removes_stale_rows(monkeypatch):
    conn = sqlite3.connect(":memory:", isolation_level=None)
    sc._ensure_schema(conn)
    conn.execute(
        "INSERT INTO clinic_counts(doc_no, date_iso, payload, updated_at) "
        "VALUES (?, ?, ?, ?)",
        ("D123", "2026-05-24", "[1]", 1.0),
    )
    conn.execute(
        "INSERT INTO clinic_counts(doc_no, date_iso, payload, updated_at) "
        "VALUES (?, ?, ?, ?)",
        ("D456", "2026-05-24", "[2]", 1.0),
    )
    monkeypatch.setattr(sc, "_initialized", True)
    monkeypatch.setattr(sc, "_get_conn", lambda: conn)

    sc.save_clinic_counts({"D123": {}}, only_doctor_no="D123")

    assert conn.execute(
        "SELECT doc_no FROM clinic_counts ORDER BY doc_no"
    ).fetchall() == [("D456",)]


def test_save_selected_doctor_error_preserves_stale_rows(monkeypatch):
    conn = sqlite3.connect(":memory:", isolation_level=None)
    sc._ensure_schema(conn)
    conn.execute(
        "INSERT INTO clinic_counts(doc_no, date_iso, payload, updated_at) "
        "VALUES (?, ?, ?, ?)",
        ("D123", "2026-05-24", "[1]", 1.0),
    )
    monkeypatch.setattr(sc, "_initialized", True)
    monkeypatch.setattr(sc, "_get_conn", lambda: conn)

    sc.save_clinic_counts(
        {"D123": {"error": "network unavailable"}},
        only_doctor_no="D123",
    )

    assert conn.execute(
        "SELECT doc_no FROM clinic_counts"
    ).fetchall() == [("D123",)]


def test_initialize_failure_closes_partial_connection_and_allows_retry(monkeypatch):
    closed = []
    monkeypatch.setattr(sc, "_initialized", False)
    monkeypatch.setattr(sc, "_get_conn", lambda: object())
    monkeypatch.setattr(
        sc,
        "_ensure_schema",
        lambda _conn: (_ for _ in ()).throw(RuntimeError("disk unavailable")),
    )
    monkeypatch.setattr(sc, "_close_cached_conn", lambda: closed.append(True))

    assert sc._ensure_initialized() is False
    assert closed == [True]
    assert sc._initialized is False


def test_load_returns_empty_without_query_when_initialize_fails(monkeypatch):
    monkeypatch.setattr(sc, "_ensure_initialized", lambda: False)
    monkeypatch.setattr(
        sc,
        "_get_conn",
        lambda: (_ for _ in ()).throw(
            AssertionError("must not query a partially initialized database")),
    )

    assert sc.load_clinic_counts() == {}


# === [stability r4] 執行期(非啟動時)損壞復原 ===

def test_is_corruption_error_classifies_lock_vs_malformed():
    # 暫時鎖競爭 → 不是損壞，不應觸發重建
    assert sc._is_corruption_error(
        sqlite3.OperationalError("database is locked")) is False
    assert sc._is_corruption_error(
        sqlite3.OperationalError("database table is busy")) is False
    # 真正的損壞 / 磁碟錯誤 → 視為損壞，應觸發重建
    assert sc._is_corruption_error(
        sqlite3.DatabaseError("database disk image is malformed")) is True
    assert sc._is_corruption_error(
        sqlite3.OperationalError("disk I/O error")) is True
    assert sc._is_corruption_error(
        sqlite3.DatabaseError("file is not a database")) is True
    # 非 sqlite 例外 → 不重建
    assert sc._is_corruption_error(ValueError("nope")) is False


def test_save_clinic_counts_recovers_from_runtime_corruption(monkeypatch):
    """啟動後才損壞：save 的 conn.execute 拋 DatabaseError → 清 _initialized
    並關連線，讓下一次呼叫重走隔離+重建路徑（不再永久壞死至重啟）。"""
    closed = []

    class _CorruptConn:
        def execute(self, *_a, **_k):
            raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(sc, "_initialized", True)
    monkeypatch.setattr(sc, "_get_conn", lambda: _CorruptConn())
    monkeypatch.setattr(sc, "_close_cached_conn", lambda: closed.append(True))

    # 不應拋例外（錯誤被吞），但偵測到損壞後重設狀態
    sc.save_clinic_counts({"D123": {"2026-05-24": [1, 2, 3]}})

    assert closed == [True]
    assert sc._initialized is False


def test_load_clinic_counts_recovers_from_runtime_corruption(monkeypatch):
    closed = []

    class _CorruptConn:
        def execute(self, *_a, **_k):
            raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(sc, "_initialized", True)
    monkeypatch.setattr(sc, "_get_conn", lambda: _CorruptConn())
    monkeypatch.setattr(sc, "_close_cached_conn", lambda: closed.append(True))

    assert sc.load_clinic_counts() == {}
    assert closed == [True]
    assert sc._initialized is False


def test_transient_lock_does_not_reset_initialized(monkeypatch):
    """暫時鎖競爭不應拆連線重建（否則一次偶發鎖等待就浪費重連）。"""
    closed = []

    class _LockedConn:
        def execute(self, *_a, **_k):
            raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sc, "_initialized", True)
    monkeypatch.setattr(sc, "_get_conn", lambda: _LockedConn())
    monkeypatch.setattr(sc, "_close_cached_conn", lambda: closed.append(True))

    sc.save_clinic_counts({"D123": {"2026-05-24": [1]}})

    assert closed == []
    assert sc._initialized is True
