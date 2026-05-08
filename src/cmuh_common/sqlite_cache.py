# -*- coding: utf-8 -*-
"""[O22] 用 SQLite 取代 cache_clinic_counts.json 的大型 JSON 寫入。

優勢：
  - 增量寫入：只更新變動的醫師×日期 row，不必每次 74KB 全檔重寫
  - 原子性：SQLite 每次 commit 是原子的，不會半寫入損毀
  - 自動升級：開檔時若舊 .json 存在，一次性 import 後刪除

API（與原 _save_cache/load_cached_data 介面相容）：
  - load_clinic_counts() -> dict[doc_no, dict[date, list]]
  - save_clinic_counts(all_doctors_data, *, only_changed_doctors=None)
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from datetime import date, datetime
from typing import Any, Optional

from cmuh_common.paths import get_settings_dir

DB_FILE_NAME = "clinic_counts.sqlite"
LEGACY_JSON_NAME = "cache_clinic_counts.json"

_db_lock = threading.RLock()
_initialized = False


def _db_path() -> str:
    return os.path.join(get_settings_dir(), DB_FILE_NAME)


def _legacy_json_path() -> str:
    return os.path.join(get_settings_dir(), LEGACY_JSON_NAME)


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=10.0, isolation_level=None)
    # WAL：多 reader 同時不互鎖；commit 寫入快
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")  # 比 FULL 快、仍 crash-safe
    except sqlite3.Error:
        logging.debug("SQLite PRAGMA 設定失敗", exc_info=True)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clinic_counts (
            doc_no    TEXT NOT NULL,
            date_iso  TEXT NOT NULL,
            payload   TEXT NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (doc_no, date_iso)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_doc ON clinic_counts(doc_no)
    """)


def _migrate_legacy_json_if_present(conn: sqlite3.Connection) -> bool:
    """若舊 cache_clinic_counts.json 存在，匯入後刪除。回傳是否有匯入。"""
    legacy = _legacy_json_path()
    if not os.path.isfile(legacy):
        return False
    try:
        with open(legacy, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        logging.warning("[O22] 舊 JSON 損壞，刪除")
        try:
            os.remove(legacy)
        except OSError:
            pass
        return False

    if not isinstance(raw, dict):
        try:
            os.remove(legacy)
        except OSError:
            pass
        return False

    now = time.time()
    rows = []
    for doc_no, doc_data in raw.items():
        if not isinstance(doc_data, dict) or "error" in doc_data:
            continue
        for date_key, payload in doc_data.items():
            try:
                rows.append((str(doc_no), str(date_key),
                             json.dumps(payload, ensure_ascii=False, default=_json_default), now))
            except Exception:
                logging.debug("[O22] 跳過異常 row", exc_info=True)
                continue

    if rows:
        with conn:
            conn.executemany(
                "INSERT OR REPLACE INTO clinic_counts(doc_no, date_iso, payload, updated_at) VALUES (?, ?, ?, ?)",
                rows,
            )
        logging.info("[O22] 已從 cache_clinic_counts.json 匯入 %d 筆 → SQLite", len(rows))

    # 把舊 JSON 改名做備份（保留 7 天，cache_cleanup 之後會自動清掉）
    try:
        os.replace(legacy, legacy + ".migrated.bak")
    except OSError:
        try:
            os.remove(legacy)
        except OSError:
            pass
    return True


def _json_default(o: Any):
    if isinstance(o, (date, datetime)):
        return o.isoformat()
    raise TypeError(f"Type {type(o)} not JSON serializable")


def _ensure_initialized() -> None:
    global _initialized
    if _initialized:
        return
    with _db_lock:
        if _initialized:
            return
        try:
            with _connect() as conn:
                _ensure_schema(conn)
                _migrate_legacy_json_if_present(conn)
            _initialized = True
        except Exception:
            logging.error("[O22] SQLite 初始化失敗", exc_info=True)


def _normalize_date_key(k) -> Optional[str]:
    """將 dict key 轉為 ISO 日期字串。"""
    if isinstance(k, (date, datetime)):
        return k.isoformat() if isinstance(k, date) and not isinstance(k, datetime) \
            else k.date().isoformat()
    if isinstance(k, str):
        # 嘗試解析（容忍非標準鍵）
        try:
            date.fromisoformat(k)
            return k
        except ValueError:
            return k  # 非日期 key 也存著
    return None


def load_clinic_counts() -> dict:
    """載入所有 clinic counts。回傳 {doc_no: {date_iso: payload, ...}}。"""
    _ensure_initialized()
    out: dict[str, dict[str, Any]] = {}
    try:
        with _db_lock, _connect() as conn:
            cur = conn.execute("SELECT doc_no, date_iso, payload FROM clinic_counts")
            for doc_no, date_iso, payload_str in cur.fetchall():
                try:
                    payload = json.loads(payload_str)
                except Exception:
                    continue
                out.setdefault(doc_no, {})[date_iso] = payload
    except Exception:
        logging.error("[O22] load_clinic_counts 失敗", exc_info=True)
    return out


def save_clinic_counts(all_doctors_data: dict,
                       *, only_doctor_no: Optional[str] = None) -> None:
    """儲存 clinic counts。

    Args:
        all_doctors_data: {doc_no: {date_or_str: appointments}}
        only_doctor_no: 若指定，僅更新該醫師的所有日期 row（其他醫師原 row 保留）
    """
    _ensure_initialized()
    if not isinstance(all_doctors_data, dict):
        return
    now = time.time()
    rows = []
    for doc_no, doc_data in all_doctors_data.items():
        if only_doctor_no and doc_no != only_doctor_no:
            continue
        if not isinstance(doc_data, dict) or "error" in doc_data:
            continue
        for k, payload in doc_data.items():
            date_iso = _normalize_date_key(k)
            if date_iso is None:
                continue
            try:
                payload_str = json.dumps(payload, ensure_ascii=False, default=_json_default)
            except Exception:
                logging.debug("[O22] 跳過無法序列化的 payload", exc_info=True)
                continue
            rows.append((str(doc_no), str(date_iso), payload_str, now))

    if not rows:
        return
    try:
        with _db_lock, _connect() as conn:
            conn.execute("BEGIN")
            try:
                if only_doctor_no:
                    conn.execute("DELETE FROM clinic_counts WHERE doc_no = ?", (only_doctor_no,))
                # 全量寫入時不 DELETE 全部（保留歷史 row 以避免 race condition），用 UPSERT
                conn.executemany(
                    "INSERT OR REPLACE INTO clinic_counts(doc_no, date_iso, payload, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    rows,
                )
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
    except Exception:
        logging.error("[O22] save_clinic_counts 失敗", exc_info=True)


def vacuum_old_entries(*, older_than_days: int = 30) -> int:
    """清掉超過 N 天的 row（看更老的 date_iso）。回傳刪除筆數。"""
    _ensure_initialized()
    cutoff = (datetime.now().date() - _date_offset(older_than_days)).isoformat()
    try:
        with _db_lock, _connect() as conn:
            cur = conn.execute("DELETE FROM clinic_counts WHERE date_iso < ?", (cutoff,))
            return cur.rowcount or 0
    except Exception:
        logging.error("[O22] vacuum_old_entries 失敗", exc_info=True)
        return 0


def _date_offset(days: int):
    from datetime import timedelta
    return timedelta(days=days)


def get_size_bytes() -> int:
    p = _db_path()
    return os.path.getsize(p) if os.path.isfile(p) else 0
