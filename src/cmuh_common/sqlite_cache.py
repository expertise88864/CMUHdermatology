# -*- coding: utf-8 -*-
"""[O22] 用 SQLite 取代 cache_clinic_counts.json 的大型 JSON 寫入。

優勢：
  - 增量寫入：只更新變動的醫師×日期 row，不必每次 74KB 全檔重寫
  - 原子性：SQLite 每次 commit 是原子的，不會半寫入損毀
  - 自動升級：開檔時若舊 .json 存在，一次性 import 後刪除

API（與原 _save_cache/load_cached_data 介面相容）：
  - load_clinic_counts() -> dict[doc_no, dict[date, list]]
  - save_clinic_counts(all_doctors_data, *, only_doctor_no=None)
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

from cmuh_common.atomic_io import safe_load_json
from cmuh_common.paths import get_settings_dir

DB_FILE_NAME = "clinic_counts.sqlite"
LEGACY_JSON_NAME = "cache_clinic_counts.json"

_db_lock = threading.RLock()
_initialized = False
# 【效能 2026-05-21】單例連線。原本每次 load/save/vacuum 都 sqlite3.connect()
# + 兩個 PRAGMA = ~15-25ms 開銷。改成 module 級單例（check_same_thread=False
# + _db_lock 序列化）後，每次呼叫省 ~15-25ms，高頻寫入時段累計可省 100ms+。
# WAL header 寫一次就持久（journal_mode 是 db file metadata），不必每次 set。
_conn_cached: Optional[sqlite3.Connection] = None


def _db_path() -> str:
    return os.path.join(get_settings_dir(), DB_FILE_NAME)


def _legacy_json_path() -> str:
    return os.path.join(get_settings_dir(), LEGACY_JSON_NAME)


def _get_conn() -> sqlite3.Connection:
    """取得共享連線（thread-safe — 呼叫端必須在 _db_lock 內）。

    PRAGMA WAL/synchronous 只在連線建立時 set 一次。SQLite 將 journal_mode 寫進
    db file header，所以即使 process 重啟，WAL 模式仍然啟用。"""
    global _conn_cached
    if _conn_cached is None:
        _conn_cached = sqlite3.connect(
            _db_path(), timeout=10.0, isolation_level=None,
            check_same_thread=False,
        )
        try:
            _conn_cached.execute("PRAGMA journal_mode=WAL;")
            _conn_cached.execute("PRAGMA synchronous=NORMAL;")
        except sqlite3.Error:
            logging.debug("SQLite PRAGMA 設定失敗", exc_info=True)
    return _conn_cached


def _close_cached_conn() -> None:
    """關閉快取連線（atexit / 測試用）。"""
    global _conn_cached
    if _conn_cached is not None:
        try:
            _conn_cached.close()
        except sqlite3.Error:
            pass
        _conn_cached = None


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
    raw = safe_load_json(legacy, default=None)
    if raw is None:
        logging.warning("[O22] 舊 JSON 損壞或讀取失敗，已由 safe_load_json 備份或略過")
        return False

    if not isinstance(raw, dict):
        ts = time.strftime("%Y%m%d_%H%M%S")
        try:
            os.replace(legacy, f"{legacy}.invalid-{ts}")
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


def _ensure_initialized() -> bool:
    global _initialized
    if _initialized:
        return True
    with _db_lock:
        if _initialized:
            return True
        try:
            conn = _get_conn()
            _ensure_schema(conn)
            _migrate_legacy_json_if_present(conn)
            _initialized = True
            return True
        except sqlite3.DatabaseError as e:
            # [stability] DB 檔損壞(斷電/磁碟壞軌/被外部程式截斷)時，原本每次啟動都
            # 在此 except 失敗 → clinic-counts 快取永久壞掉、永不恢復。改為：隔離損壞
            # 檔(連同 -wal/-shm sidecar)成 .corrupt-<ts>，重建一個空 DB，讓快取自我
            # 修復(僅丟失歷史樣本，會重新累積)。
            logging.error("[O22] SQLite 疑似損壞(%s)，隔離舊檔並重建空 DB", e,
                          exc_info=True)
            _close_cached_conn()
            try:
                ts = time.strftime("%Y%m%d_%H%M%S")
                base = _db_path()
                for suffix in ("", "-wal", "-shm"):
                    p = base + suffix
                    if os.path.exists(p):
                        try:
                            os.replace(p, f"{p}.corrupt-{ts}")
                        except OSError:
                            try:
                                os.remove(p)
                            except OSError:
                                logging.debug("[O22] 移除損壞檔失敗 %s", p,
                                              exc_info=True)
                conn = _get_conn()
                _ensure_schema(conn)
                _initialized = True
                logging.warning(
                    "[O22] 已重建空 clinic_counts DB（歷史快取丟失，將重新累積）")
                return True
            except Exception:
                logging.error("[O22] SQLite 損壞後重建失敗", exc_info=True)
                _close_cached_conn()
                return False
        except Exception:
            logging.error("[O22] SQLite 初始化失敗", exc_info=True)
            _close_cached_conn()
            return False


def _is_corruption_error(exc: BaseException) -> bool:
    """[stability r4] 判斷例外是否為『DB 檔損壞』(需隔離重建)，而非暫時性鎖競爭。

    sqlite3.OperationalError 是 DatabaseError 子類；'database is locked'/'busy' 屬
    暫時鎖競爭，不應觸發重建(否則一次偶發鎖等待就拆連線、浪費)。其餘 DatabaseError
    (malformed / not a database / disk I/O error 等)視為損壞。"""
    if not isinstance(exc, sqlite3.DatabaseError):
        return False
    if isinstance(exc, sqlite3.OperationalError):
        msg = str(exc).lower()
        if "lock" in msg or "busy" in msg:
            return False
    return True


def _reset_for_corruption(where: str, exc: BaseException) -> None:
    """[stability r4] 執行期(非啟動時)偵測到 DB 損壞 → 關閉連線並清 _initialized，
    讓下一次呼叫重走 _ensure_initialized 的隔離+重建路徑。

    原本 _initialized 一旦 True 永久 latch，啟動後才損壞(磁碟壞軌/被外部截斷/WAL 損壞)
    時 save/load 的 except 只記 log、不復原 → 快取永久壞死直到 process 重啟。此函式把
    『執行期損壞』導回既有的『啟動期損壞』復原機制。"""
    global _initialized
    with _db_lock:
        logging.error("[O22] %s 偵測到 SQLite 疑似損壞(%s) → 關閉連線，下次呼叫將隔離重建",
                      where, exc)
        _close_cached_conn()
        _initialized = False


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


def load_clinic_counts(*, since_date: Optional[str] = None) -> dict:
    """載入 clinic counts。回傳 {doc_no: {date_iso: payload, ...}}。

    Args:
        since_date: ISO date string (YYYY-MM-DD)；只載 date_iso >= since_date 的 row。
                    None = 載全部。冷啟動建議傳今天的日期，省 100-300ms。
    """
    out: dict[str, dict[str, Any]] = {}
    if not _ensure_initialized():
        return out
    try:
        with _db_lock:
            conn = _get_conn()
            if since_date:
                cur = conn.execute(
                    "SELECT doc_no, date_iso, payload FROM clinic_counts WHERE date_iso >= ?",
                    (since_date,))
            else:
                cur = conn.execute("SELECT doc_no, date_iso, payload FROM clinic_counts")
            for doc_no, date_iso, payload_str in cur.fetchall():
                try:
                    payload = json.loads(payload_str)
                except Exception:
                    continue
                out.setdefault(doc_no, {})[date_iso] = payload
    except Exception as e:
        logging.error("[O22] load_clinic_counts 失敗", exc_info=True)
        if _is_corruption_error(e):
            _reset_for_corruption("load_clinic_counts", e)
    return out


def save_clinic_counts(all_doctors_data: dict,
                       *, only_doctor_no: Optional[str] = None) -> None:
    """儲存 clinic counts。

    Args:
        all_doctors_data: {doc_no: {date_or_str: appointments}}
        only_doctor_no: 若指定，僅更新該醫師的所有日期 row（其他醫師原 row 保留）。
                        明確傳入空 dict 代表查詢成功但無門診，會清掉該醫師舊 row。
    """
    if not _ensure_initialized():
        return
    if not isinstance(all_doctors_data, dict):
        return
    now = time.time()
    rows = []
    selected_doctor_no = (
        str(only_doctor_no) if only_doctor_no is not None else None
    )
    selected_doctor_has_valid_data = False
    for doc_no, doc_data in all_doctors_data.items():
        normalized_doc_no = str(doc_no)
        if selected_doctor_no is not None and normalized_doc_no != selected_doctor_no:
            continue
        if not isinstance(doc_data, dict) or "error" in doc_data:
            continue
        if selected_doctor_no is not None:
            selected_doctor_has_valid_data = True
        for k, payload in doc_data.items():
            date_iso = _normalize_date_key(k)
            if date_iso is None:
                continue
            try:
                payload_str = json.dumps(payload, ensure_ascii=False, default=_json_default)
            except Exception:
                logging.debug("[O22] 跳過無法序列化的 payload", exc_info=True)
                continue
            rows.append((normalized_doc_no, str(date_iso), payload_str, now))

    should_clear_selected_doctor = (
        selected_doctor_no is not None and selected_doctor_has_valid_data
    )
    if not rows and not should_clear_selected_doctor:
        return
    try:
        with _db_lock:
            conn = _get_conn()
            conn.execute("BEGIN")
            try:
                if should_clear_selected_doctor:
                    conn.execute(
                        "DELETE FROM clinic_counts WHERE doc_no = ?",
                        (selected_doctor_no,),
                    )
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
    except Exception as e:
        logging.error("[O22] save_clinic_counts 失敗", exc_info=True)
        if _is_corruption_error(e):
            _reset_for_corruption("save_clinic_counts", e)


def vacuum_old_entries(*, older_than_days: int = 30) -> int:
    """清掉超過 N 天的 row（看更老的 date_iso）。回傳刪除筆數。"""
    if not _ensure_initialized():
        return 0
    cutoff = (datetime.now().date() - _date_offset(older_than_days)).isoformat()
    try:
        with _db_lock:
            conn = _get_conn()
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
