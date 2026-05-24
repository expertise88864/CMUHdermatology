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
