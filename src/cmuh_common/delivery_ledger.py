# -*- coding: utf-8 -*-
"""寄送帳本（跨 poll／跨重啟的送達狀態）。

【為什麼需要它：2026-08-07 外審】
在此之前，「這封信到底寄出去了沒」只有兩種記法，而且都是**布林**：
  * 會診：`consult_notified.json` 記已通知的病歷號集合。
  * 止掛：`alert_email_sent.json` 記 notify_key → 日期。
兩者都只在「寄成功之後」才寫，於是遇到 `DeliveryOutcomeUnknown`（DATA 已提交、
等最終 250 逾時 —— 信很可能已送達）就只剩兩個都不好的選擇：

  當成失敗 → 下一輪重寄 → 醫師收到重複的臨床通知（止掛曾經如此）。
  當成成功 → 萬一真的沒送到，那一則永遠不補（目前的權宜之計）。

而且**重啟後記憶體去重全部消失**，同一批會診會被再判為 new。

本模組提供第三種狀態：把每一次寄送記成一筆有生命週期的紀錄，UNKNOWN 就誠實地
記成 UNKNOWN，之後用 Message-ID 回查寄件備份把它收斂成 CONFIRMED 或 FAILED。

【與既有 business key 的關係】
本帳本**不取代** consult_notified / alert_email_sent，而是它們的依據：
  business_key  會診＝roster signature；止掛＝notify_key；系統信＝event key。
呼叫端問的是「這個 business_key 有沒有一筆還沒被否證的送達」——
`has_live_delivery()` 回 True 就不要再寄。

【為什麼是每位收件人一個狀態】
smtplib 部分收件人被拒時是**正常返回**的。舊做法整輪記一個布林 → 被拒的那位
永遠不補寄。這裡每位收件人各自有 CONFIRMED / TRANSIENT_REFUSED /
PERMANENT_REFUSED / UNKNOWN，`recipients_needing_retry()` 只挑出該補的人。

【儲存層：SQLite WAL —— 2026-08-13 批次AD-2，使用者定案】
第一版是 JSON + sidecar 檔案鎖。外審(2026-08-12)指出三條 P1 是同一個病灶:
  * 跨 process 鎖 fail-open → 兩支程式同時 reconcile 同一筆,last-writer-wins;
  * `begin()` 寫回失敗仍回 delivery_id → 「send 前一定先留下 SUBMITTING」
    其實只保證了【記憶體】;
  * `has_live_delivery()` 與 `begin()` 是兩個操作 → 接成閘門就是 TOCTOU。
JSON + sidecar lock 已經在承擔資料庫的工作量。改用 SQLite(標準庫):
  * WAL 模式 + `BEGIN IMMEDIATE` 交易 → 跨 process 的互斥由資料庫保證,
    沒有「取不到鎖就照寫」這條路;
  * `synchronous=FULL` → **`begin()` 回傳 = 已 fsync 落地**(斷電也在);
  * `begin_if_no_live()` 在同一筆交易裡「查 live + 插入」→ 原子 claim
    (給日後的寄送閘門用;現在還沒有呼叫端接它)。
既有的 `delivery_ledger.json` 在每次啟動時以 INSERT OR IGNORE 併入(舊版
程式在更新空窗期還會寫 JSON;它更新自己那幾筆的終局狀態如果沒趕上匯入,
會停在 SUBMITTING,由既有的 Message-ID 回查收斂 —— 不會憑空多寄)。

【可用性立場(2026-08-05 事故定案,本批不變)】
帳本壞掉【不可以】停掉臨床通知:`begin()` 失敗會拋 `LedgerUnavailable`,
兩個呼叫端都接住、記 log、照樣寄 —— 代價是那一封沒有帳(誠實的缺口),
而不是假裝有帳。等閘門正式接上時,那個呼叫端必須自己重新寫下取捨。
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
import uuid
from typing import Optional

from cmuh_common.atomic_io import safe_load_json_ex
from cmuh_common.paths import get_settings_dir

LEDGER_FILENAME = "delivery_ledger.json"        # 舊格式(只讀,匯入用)
DB_FILENAME = "delivery_ledger.sqlite3"
_SCHEMA_VERSION = 1

# ── 整筆寄送的狀態 ──────────────────────────────────────────────────────────
PREPARED = "prepared"        # 舊格式遺留（新版 begin 直接落地成 SUBMITTING）
SUBMITTING = "submitting"    # 正在送（跨重啟看到它 = 上次送到一半就死了）
CONFIRMED = "confirmed"      # 全部收件人都確定送達
PARTIAL = "partial"          # 有人送達、有人沒有
UNKNOWN = "unknown"          # 結果不明（逾時但伺服器可能已收下）
FAILED = "failed"            # 確定沒送出

# ── 單一收件人的狀態 ────────────────────────────────────────────────────────
R_CONFIRMED = "confirmed"
R_TRANSIENT = "transient_refused"    # 4xx：暫時性，只重寄這位
R_PERMANENT = "permanent_refused"    # 5xx：位址錯/不存在，重寄無用，要告警
R_UNKNOWN = "unknown"

# 「還沒被否證」= 不可以再寄一次的狀態。UNKNOWN 也算——重寄的風險大於漏寄，
# 要等 Message-ID 回查把它否證成 FAILED 之後才可以重送。
LIVE_STATES = (PREPARED, SUBMITTING, CONFIRMED, PARTIAL, UNKNOWN)


class LedgerUnavailable(RuntimeError):
    """這一刻讀不到/寫不進帳本,無法回答「寄過了沒有」或無法落地。

    ★存在的理由★ 唯一比「答錯」更糟的是「猜一個答案卻讓人以為是查到的」。
    擋下來會停掉臨床通知(2026-08-05 就是這樣停了一個下午),放行會重複寄 ——
    這個取捨必須由接上閘門的那個呼叫端明寫,不可以藏在資料層的 except 裡。
    """

RETAIN_DAYS = 45              # 舊紀錄保留天數（需大於任何 business 的前瞻視窗）
_DAY_SEC = 86400.0
_BUSY_TIMEOUT_MS = 5000       # 交易互斥的等待上限;超過 → LedgerUnavailable


def _now() -> float:
    return time.time()


def new_delivery_id() -> str:
    return uuid.uuid4().hex


def classify_refusal(code) -> str:
    """SMTP 拒收碼 → 單一收件人狀態。4xx 暫時性、5xx 永久性。

    看不懂的碼一律當【暫時性】：重寄一位收件人的代價，遠小於把一則臨床通知
    永久丟掉。純函式。
    """
    try:
        n = int(code)
    except (TypeError, ValueError):
        return R_TRANSIENT
    if 500 <= n < 600:
        return R_PERMANENT
    return R_TRANSIENT


def summarize(recipient_states: dict) -> str:
    """每位收件人的狀態 → 整筆的狀態。純函式（狀態機的核心，好測）。

    規則（保守優先）：
      有人 UNKNOWN            → UNKNOWN（還不知道，不可以宣稱成功或失敗）
      全部 CONFIRMED          → CONFIRMED
      全部被拒（無人送達）    → FAILED
      有送達也有被拒          → PARTIAL
      空的                    → FAILED（沒有收件人＝什麼都沒送出）
    """
    states = list(recipient_states.values())
    if not states:
        return FAILED
    if any(s == R_UNKNOWN for s in states):
        return UNKNOWN
    delivered = [s for s in states if s == R_CONFIRMED]
    if len(delivered) == len(states):
        return CONFIRMED
    if not delivered:
        return FAILED
    return PARTIAL


def recipients_needing_retry(recipient_states: dict) -> list:
    """該補寄給誰。

    只有【暫時性被拒】要補。已送達的不可再寄（會重複轟炸）；永久性被拒重寄
    也不會變好（要改設定，另外告警）；UNKNOWN 要先驗證，不可盲目重寄。純函式。
    """
    return sorted(addr for addr, st in recipient_states.items()
                  if st == R_TRANSIENT)


def permanently_refused(recipient_states: dict) -> list:
    """位址設定有問題、需要人工修正的收件人。純函式。"""
    return sorted(addr for addr, st in recipient_states.items()
                  if st == R_PERMANENT)


def _as_epoch(value) -> float:
    """把（舊格式）帳本裡的時間欄位轉成【可比較的】秒數。看不懂 → 0.0(很舊)。

    ★[外審第 2 輪 #2/#5] 排序不可以直接吃原始值;NaN/Infinity 也是壞值★
    舊 JSON 裡只要有一筆的時間是字串/NaN,整份清單就列不出來或安靜卡死。
    SQLite 這一版由我們寫入的一定是 REAL;這個函式守在【匯入舊 JSON】的
    邊界上,把壞值在進門時就轉成 0.0(很舊),而不是讓它躺在庫裡。

    ★方向:看不懂 = 很舊★ 當成「很新」的話,壞掉的那一筆會被年齡門檻永遠濾掉
    —— 又是把一個看得見的壞變成安靜的壞。
    """
    try:
        f = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def _as_attempts(value) -> int:
    """(舊格式)attempts → 有界的 int。看不懂/超出範圍 → 0。

    ★[外審 AD-2 第 1 輪 P2]★ `int()` 對超過 SQLite 64-bit 範圍的值不會拋,
    binding 時才炸(OverflowError)—— 那不是 sqlite3.Error,會炸在交易裡。
    邊界防線與 `_as_epoch` 同一個立場:壞值在進門時就轉乾淨。
    """
    try:
        n = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return n if 0 <= n < 2**31 else 0


def _clean_recipients(value) -> dict:
    """收件人狀態欄位 → 乾淨的 {str: str}。匯入舊資料/讀回時的邊界防線。"""
    if not isinstance(value, dict):
        return {}
    return {str(k): str(v) for k, v in value.items()}


_COLUMNS = ("delivery_id", "business_key", "parent_id", "category", "subject",
            "message_id", "attachment_hash", "state", "recipients",
            "created_at", "updated_at", "attempts", "note")


class DeliveryLedger:
    """寄送帳本(SQLite WAL)。執行緒安全;每次變更=一筆已 fsync 的交易。

    ★契約★
      * `begin()` 回傳 delivery_id = 那一筆【已經在磁碟上】(synchronous=FULL);
        落不了地就拋 `LedgerUnavailable`,絕不回一個沒有帳的 id。
      * 讀查詢(`has_live_delivery` / `unresolved` / …)讀不到就拋,
        不把「讀不到」講成「沒有」;`state_of` 例外(回空字串=不知道,呼叫端
        以三態處理)。
      * 跨 process 一致性由 SQLite 交易保證 —— 沒有「取不到鎖就照寫」。
    """

    def __init__(self, path: "Optional[str]" = None, *,
                 retain_days: int = RETAIN_DAYS):
        import os
        if path and str(path).lower().endswith(".json"):
            # 相容舊呼叫形狀:給的是舊 JSON 路徑 → DB 放旁邊、JSON 當匯入來源
            self._legacy_json = str(path)
            self.path = str(path)[: -len(".json")] + ".sqlite3"
        else:
            base = path or os.path.join(get_settings_dir(), DB_FILENAME)
            self.path = str(base)
            self._legacy_json = os.path.join(
                os.path.dirname(self.path) or ".", LEDGER_FILENAME)
        self._retain_days = retain_days
        self._lock = threading.RLock()
        self._conn: "Optional[sqlite3.Connection]" = None
        try:
            self._connect_locked()
        except Exception:
            # ★開不起來不可以擋住程式啟動★ 之後每一次操作都會 lazy 重連 ——
            #   防毒短暫鎖住檔案不該把帳本永久打死(抑制要有出口)。
            logging.error("[delivery] 帳本資料庫開不起來(稍後每次操作會重試):%s",
                          self.path, exc_info=True)
        self._wire_lifecycle()

    # ── 連線與 schema ──────────────────────────────────────────────────────
    def _connect_locked(self) -> sqlite3.Connection:
        import os
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=_BUSY_TIMEOUT_MS / 1000.0,
                               check_same_thread=False, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            # ★FULL,不是 WAL 預設建議的 NORMAL★:NORMAL 在斷電時可能丟掉
            #   最近幾筆 commit —— 而「begin() 回傳=已落地」正是 P1-03 要的
            #   契約。寄信頻率是每小時個位數,fsync 的成本可以忽略。
            conn.execute("PRAGMA synchronous=FULL")
            conn.execute("PRAGMA busy_timeout=%d" % _BUSY_TIMEOUT_MS)
            conn.execute(
                "CREATE TABLE IF NOT EXISTS deliveries ("
                " delivery_id TEXT PRIMARY KEY,"
                " business_key TEXT NOT NULL DEFAULT '',"
                " parent_id TEXT NOT NULL DEFAULT '',"
                " category TEXT NOT NULL DEFAULT '',"
                " subject TEXT NOT NULL DEFAULT '',"
                " message_id TEXT NOT NULL DEFAULT '',"
                " attachment_hash TEXT NOT NULL DEFAULT '',"
                " state TEXT NOT NULL,"
                " recipients TEXT NOT NULL DEFAULT '{}',"
                " created_at REAL NOT NULL,"
                " updated_at REAL NOT NULL,"
                " attempts INTEGER NOT NULL DEFAULT 0,"
                " note TEXT NOT NULL DEFAULT '')")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_bk"
                         " ON deliveries(business_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_state"
                         " ON deliveries(state)")
            # ★schema version 是人維護的帳,不是推導出來的★(外審 2026-08-12)
            conn.execute("CREATE TABLE IF NOT EXISTS meta ("
                         " key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES"
                         " ('schema_version', ?)", (str(_SCHEMA_VERSION),))
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise
        self._conn = conn
        try:
            self._import_legacy_locked(conn)
        except Exception:
            logging.warning("[delivery] 舊 JSON 帳本匯入失敗(下次啟動會再試;"
                            "SQLite 本身可用)", exc_info=True)
        return conn

    def _ensure_conn_locked(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        try:
            return self._connect_locked()
        except Exception as e:
            raise LedgerUnavailable(
                "這一刻開不了寄送帳本資料庫:%s" % self.path) from e

    def _import_legacy_locked(self, conn: sqlite3.Connection) -> None:
        """把舊 JSON 的紀錄併進來(INSERT OR IGNORE,永不覆蓋 SQLite 既有列)。

        ★為什麼每次啟動都跑、而且不刪 JSON★ 更新是逐台逐程式的:空窗期裡
        「還在跑舊版的那一支」會繼續寫 JSON。每次啟動重新掃一遍,舊版新增的
        紀錄就會被撿進來。舊版對【自己那幾筆】的終局更新若沒趕上匯入,
        那幾筆在 SQLite 裡停在 SUBMITTING → 走既有的 Message-ID 回查收斂,
        方向安全(不會憑空多寄)。等全部機器都在新版上跑一陣子,JSON 自然
        不再變動,匯入變成 no-op。
        """
        import os
        if not os.path.isfile(self._legacy_json):
            return
        data, status = safe_load_json_ex(self._legacy_json, {},
                                         backup_on_corrupt=False)
        if status != "ok" or not isinstance(data, dict):
            if status not in ("ok", "missing"):
                logging.warning("[delivery] 舊 JSON 帳本讀不到/損壞(status=%s)"
                                " → 這一次不匯入", status)
            return
        rows = []
        for did, rec in data.items():
            if not isinstance(rec, dict):
                continue
            rows.append((
                str(did),
                str(rec.get("business_key") or ""),
                str(rec.get("parent_id") or ""),
                str(rec.get("category") or ""),
                str(rec.get("subject") or "")[:200],
                str(rec.get("message_id") or ""),
                str(rec.get("attachment_hash") or ""),
                str(rec.get("state") or UNKNOWN),
                json.dumps(_clean_recipients(rec.get("recipients")),
                           ensure_ascii=False),
                _as_epoch(rec.get("created_at")),
                _as_epoch(rec.get("updated_at")),
                _as_attempts(rec.get("attempts")),
                str(rec.get("note") or "")[:300],
            ))
        if not rows:
            return
        with self._txn(conn):
            cur = conn.executemany(
                "INSERT OR IGNORE INTO deliveries (%s) VALUES (%s)"
                % (",".join(_COLUMNS), ",".join("?" * len(_COLUMNS))), rows)
            if cur.rowcount:
                logging.info("[delivery] 從舊 JSON 帳本匯入 %d 筆", cur.rowcount)

    from contextlib import contextmanager as _ctx

    @_ctx
    def _txn(self, conn: sqlite3.Connection):
        """一筆交易。任何 SQLite 失敗 → rollback + `LedgerUnavailable`。

        ★BEGIN IMMEDIATE★:一開始就拿寫鎖,兩個 process 的「讀-改-寫」
        整段互斥 —— 這正是舊 sidecar lock fail-open 修不掉的那件事。
        等不到鎖(busy_timeout 用完)也是 LedgerUnavailable:
        ★沒有「取不到鎖就照寫」這條路★(外審 2026-08-12 P1-02)。
        """
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as e:
            raise LedgerUnavailable("寄送帳本交易開不起來(鎖競爭或資料庫"
                                    "不可用)") from e
        try:
            yield conn
        except BaseException as e:
            # ★[外審 AD-2 第 1 輪 P2] 任何例外都要 ROLLBACK,不只 SQL 的★
            #   binding 溢位(OverflowError)、回呼裡的 bug…… 只回捲 SQL 例外
            #   的話,交易會開著不放:本 process 之後全是 "cannot start a
            #   transaction within a transaction",別的 process 等到
            #   busy_timeout 為止 —— ★卡住的持有者把整本帳拖下水★。
            #   只把 SQLite 的錯誤翻譯成 LedgerUnavailable,其餘原樣重拋
            #   (那是程式錯誤,不可以偽裝成「帳本暫時不可用」)。
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            if isinstance(e, LedgerUnavailable):
                raise
            if isinstance(e, sqlite3.Error):
                raise LedgerUnavailable("寄送帳本寫入失敗") from e
            raise
        else:
            try:
                conn.execute("COMMIT")
            except sqlite3.Error as e:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise LedgerUnavailable("寄送帳本 COMMIT 失敗(這筆沒有落地)"
                                        ) from e

    def _wire_lifecycle(self) -> None:
        """開機收斂 + 結束時收尾,接在建構子裡(呼叫端遲早會漏掉一個)。"""
        try:
            import atexit  # noqa: PLC0415
            atexit.register(self._close_quietly)
        except Exception:
            logging.debug("[delivery] 註冊結束收尾失敗", exc_info=True)
        try:
            self.converge_stale_prepared()
        except Exception:
            logging.debug("[delivery] 開機收斂陳舊 PREPARED 失敗", exc_info=True)

    def _close_quietly(self) -> None:
        try:
            with self._lock:
                if self._conn is not None:
                    self._conn.close()
                    self._conn = None
        except Exception:
            logging.debug("[delivery] 關閉帳本資料庫失敗", exc_info=True)

    def flush(self) -> None:
        """相容 API。SQLite 每筆變更都已 fsync —— 這裡只做 WAL checkpoint,
        任何失敗都不可以影響關機(舊版靠它把記憶體殘帳寫回;現在沒有殘帳)。"""
        try:
            with self._lock:
                if self._conn is not None:
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            logging.debug("[delivery] WAL checkpoint 失敗(無礙)", exc_info=True)

    # ── row ↔ dict ─────────────────────────────────────────────────────────
    @staticmethod
    def _to_rec(row) -> dict:
        rec = dict(zip(_COLUMNS, row, strict=True))
        try:
            rec["recipients"] = _clean_recipients(json.loads(
                rec.get("recipients") or "{}"))
        except (TypeError, ValueError):
            rec["recipients"] = {}
        return rec

    def _get_row_locked(self, conn, delivery_id: str):
        cur = conn.execute(
            "SELECT %s FROM deliveries WHERE delivery_id=?" % ",".join(_COLUMNS),
            (str(delivery_id or ""),))
        row = cur.fetchone()
        return self._to_rec(row) if row else None

    # ── 生命週期 ───────────────────────────────────────────────────────────
    def begin(self, *, business_key: str, category: str, recipients: list,
              subject: str = "", message_id: str = "",
              attachment_hash: str = "", parent_id: str = "") -> str:
        """登記一次即將寄出的信。回傳 delivery_id。

        ★必須在真正送出【之前】呼叫★ —— 這樣即使送出當下斷電，重啟後看到的是
        一筆 SUBMITTING，而不是「什麼都沒發生」。

        ★[外審 2026-08-12 P1-03] 回傳 = 已落地★
        這個方法回來的那一刻,那一筆已經 COMMIT + fsync(synchronous=FULL)。
        落不了地就拋 `LedgerUnavailable` —— 絕不回一個只存在於記憶體的 id
        (那正是「send 前一定先留下 SUBMITTING」只保證到記憶體的舊病灶)。
        兩個呼叫端(會診/止掛)都接住這個例外、記 log、照樣寄:
        帳本壞掉不停臨床通知是 2026-08-05 事故後的定案;等寄送閘門正式接上,
        那個呼叫端必須重新明寫這個取捨。
        """
        did = new_delivery_id()
        rec = self._new_rec(did, business_key, category, recipients, subject,
                            message_id, attachment_hash, parent_id)
        with self._lock:
            conn = self._ensure_conn_locked()
            with self._txn(conn):
                self._insert_locked(conn, rec)
                self._prune_locked(conn)
        return did

    def begin_if_no_live(self, *, business_key: str, category: str,
                         recipients: list, subject: str = "",
                         message_id: str = "", attachment_hash: str = "",
                         parent_id: str = "") -> str:
        """★原子版的「沒有 live 才登記」★(外審 2026-08-12 P1-08)。

        `has_live_delivery()` + `begin()` 是兩個操作 —— 接成閘門就是 TOCTOU:
        兩個 process 同時查到「沒有」,然後各寄各的。這裡把「查 + 插」放在
        同一筆 BEGIN IMMEDIATE 交易裡,資料庫保證互斥。

        回傳 delivery_id;已有 live 紀錄時回空字串(= 不要寄)。
        目前還沒有呼叫端(閘門未接);接閘門的人用這個,不要用兩段式。
        """
        did = new_delivery_id()
        rec = self._new_rec(did, business_key, category, recipients, subject,
                            message_id, attachment_hash, parent_id)
        with self._lock:
            conn = self._ensure_conn_locked()
            with self._txn(conn):
                cur = conn.execute(
                    "SELECT 1 FROM deliveries WHERE business_key=? AND state IN"
                    " (%s) LIMIT 1" % ",".join("?" * len(LIVE_STATES)),
                    (str(business_key), *LIVE_STATES))
                if cur.fetchone():
                    return ""
                self._insert_locked(conn, rec)
                self._prune_locked(conn)
        return did

    @staticmethod
    def _new_rec(did, business_key, category, recipients, subject,
                 message_id, attachment_hash, parent_id) -> dict:
        return {
            "delivery_id": did,
            "business_key": str(business_key),
            # ★補寄與初次的關聯★(外審第 10 輪第 5 回):補寄是自己一筆,
            #   但「這位收件人收到了沒」的權威答案在【初次】那一筆上。
            "parent_id": str(parent_id or ""),
            "category": str(category),
            "subject": str(subject)[:200],
            "message_id": str(message_id),
            "attachment_hash": str(attachment_hash),
            # ★登記的當下就是 SUBMITTING★(外審第 10 輪第 3 回 P2-4)
            #   「已建檔但還沒送」在寫回可能失敗的世界裡是個假狀態 ——
            #   它只能靠 Message-ID 回查收斂,永遠不會被自動判死。
            "state": SUBMITTING,
            "recipients": {str(r).strip().lower(): R_UNKNOWN
                           for r in recipients if str(r).strip()},
            "created_at": _now(),
            "updated_at": _now(),
            "attempts": 0,
            "note": "",
        }

    def _insert_locked(self, conn, rec: dict) -> None:
        row = tuple(
            json.dumps(rec[c], ensure_ascii=False) if c == "recipients"
            else rec[c] for c in _COLUMNS)
        conn.execute("INSERT INTO deliveries (%s) VALUES (%s)"
                     % (",".join(_COLUMNS), ",".join("?" * len(_COLUMNS))), row)

    def mark_submitting(self, delivery_id: str) -> None:
        with self._lock:
            conn = self._ensure_conn_locked()
            with self._txn(conn):
                conn.execute(
                    "UPDATE deliveries SET state=?, attempts=attempts+1,"
                    " updated_at=? WHERE delivery_id=?",
                    (SUBMITTING, _now(), str(delivery_id or "")))

    def _mutate_recipients_locked(self, delivery_id: str, fn, note: str = ""):
        """讀一筆 → 讓 `fn(states)` 改收件人狀態 → summarize → 寫回。

        整段在同一筆 IMMEDIATE 交易裡:兩個 process 對同一筆的讀-改-寫
        不再可能交錯(舊版的 last-writer-wins 正是從這裡來的)。
        回傳 (新整筆狀態, fn 的回傳值);查無此筆 → (FAILED, None)。
        """
        with self._lock:
            conn = self._ensure_conn_locked()
            with self._txn(conn):
                rec = self._get_row_locked(conn, delivery_id)
                if rec is None:
                    return FAILED, None
                states = dict(rec.get("recipients") or {})
                out = fn(states)
                new_state = summarize(states)
                conn.execute(
                    "UPDATE deliveries SET recipients=?, state=?, note=?,"
                    " updated_at=? WHERE delivery_id=?",
                    (json.dumps(states, ensure_ascii=False), new_state,
                     (str(note)[:300] if note else rec.get("note") or ""),
                     _now(), rec["delivery_id"]))
                return new_state, out

    def settle(self, delivery_id: str, *, refused: "Optional[dict]" = None,
               unknown: bool = False, failed: bool = False,
               note: str = "") -> str:
        """寫入結果並算出整筆狀態。回傳新的整筆狀態。

        refused: smtplib 回傳的 {收件人: (碼, 訊息)}；空 dict = 全部送達。
        unknown: 逾時等「可能已送達」→ 所有還沒有結論的收件人記為 UNKNOWN。
        failed : 確定沒送出（例如連線階段就失敗）。

        ★settle 失敗(LedgerUnavailable)是安全方向★:那一筆停在 SUBMITTING,
        會被 `stuck_submitting()` 撿去 Message-ID 回查 —— 系統自有出口,
        呼叫端記 log 即可,不需要(也不應該)因此重寄。
        """
        def _apply(states: dict):
            if failed:
                for addr in states:
                    states[addr] = R_PERMANENT if states[addr] == R_PERMANENT \
                        else R_TRANSIENT
            elif unknown:
                pass                          # 沒有結論的維持 R_UNKNOWN,等回查
            else:
                bad = {}
                for addr, info in (refused or {}).items():
                    code = info[0] if isinstance(info, (tuple, list)) and info \
                        else info
                    bad[str(addr).strip().lower()] = classify_refusal(code)
                for addr in states:
                    states[addr] = bad.get(addr, R_CONFIRMED)
            return None

        state, _ = self._mutate_recipients_locked(delivery_id, _apply,
                                                  note=note)
        if state != FAILED or not failed:
            pass
        # `failed=True` 的整筆狀態以 summarize 為準之外,舊版強制 FAILED:
        # 全部收件人此時必為 TRANSIENT/PERMANENT → summarize 本來就回 FAILED,
        # 行為一致,不再另寫特例。
        return state

    def resolve_unknown(self, delivery_id: str, *, delivered: bool,
                        note: str = "") -> str:
        """Message-ID 回查的結果：找到＝送達、確定不存在＝失敗（可重寄）。"""
        def _apply(states: dict):
            for addr, st in states.items():
                if st == R_UNKNOWN:
                    states[addr] = R_CONFIRMED if delivered else R_TRANSIENT
            return None

        state, _ = self._mutate_recipients_locked(
            delivery_id, _apply,
            note=(note or ("寄件備份查到" if delivered else "寄件備份查無")))
        return state

    def confirm_recipients(self, delivery_id: str, addrs: list) -> list:
        """把這幾位收件人在【這一筆】上的狀態改成已送達。回傳真的改到的。

        ★正規化方式必須與 `begin()` 一致★(外審第 10 輪第 6 回):
        帳上的 key 是 `strip().lower()` —— 大小寫對不上就回寫不到,
        初次紀錄永遠掛著暫時被拒,最後被誤報成漏收。
        """
        want = {str(a).strip().lower() for a in (addrs or [])}

        def _apply(states: dict):
            done = sorted(a for a in states if a in want
                          and states[a] != R_CONFIRMED)
            for a in done:
                states[a] = R_CONFIRMED
            return done

        _state, done = self._mutate_recipients_locked(delivery_id, _apply)
        return done or []

    def abandon_recipient_retry(self, delivery_id: str, note: str = "") -> list:
        """放棄補寄:把仍是【暫時性被拒】的收件人改記成永久被拒。回傳那些人。

        補寄佇列在記憶體、重啟就忘;帳本是落地的 —— 真正的收尾要嘛補寄成功,
        要嘛在帳上明確結案並告警(外審第 10 輪第 4 回 P1-1)。
        """
        def _apply(states: dict):
            gone = sorted(a for a, st in states.items() if st == R_TRANSIENT)
            for a in gone:
                states[a] = R_PERMANENT
            return gone

        _state, gone = self._mutate_recipients_locked(
            delivery_id, _apply, note=(str(note) or "補寄已放棄"))
        return gone or []

    def claim_reconcile_pass(self, *, now: float, every_sec: float) -> bool:
        """跨 process 宣告「這一輪回查由我跑」。搶到才回 True。

        ★讀-比-寫在同一筆交易裡★(外審 2026-08-12 P1-02):舊版的時間戳
        sidecar 檔靠那把 fail-open 的鎖,兩個 process 同時 fail-open 就會
        同時「搶到」→ 同時 reconcile 同一筆 UNKNOWN → last-writer-wins。
        現在 BEGIN IMMEDIATE 保證同一時刻只有一個 process 走得進來。

        ★資料庫壞掉時回 True(照跑)★:回 False 會在資料庫永久壞掉時變成
        【永遠不回查】,而且一個字都不會說。寧可兩支程式都多跑一輪
        (代價=一次 IMAP 查詢),也不要安靜地停掉整個收斂機制 ——
        與舊版「讀不到當成沒跑過」同一個方向。
        """
        try:
            with self._lock:
                conn = self._ensure_conn_locked()
                with self._txn(conn):
                    cur = conn.execute(
                        "SELECT value FROM meta WHERE key='reconcile_last'")
                    row = cur.fetchone()
                    try:
                        prev = float(row[0]) if row else 0.0
                    except (TypeError, ValueError):
                        prev = 0.0
                    if not math.isfinite(prev):
                        prev = 0.0
                    if now - prev < float(every_sec):
                        return False
                    conn.execute(
                        "INSERT INTO meta(key, value) VALUES"
                        " ('reconcile_last', ?) ON CONFLICT(key)"
                        " DO UPDATE SET value=excluded.value",
                        ("%.3f" % now,))
                    return True
        except LedgerUnavailable:
            logging.warning("[delivery] 回查節流讀不到/寫不進 → 這一輪照跑"
                            "(寧可多跑一輪,不要安靜停掉收斂)", exc_info=True)
            return True

    # ── 查詢 ───────────────────────────────────────────────────────────────
    def get(self, delivery_id: str) -> dict:
        try:
            with self._lock:
                conn = self._ensure_conn_locked()
                rec = self._get_row_locked(conn, delivery_id)
        except (LedgerUnavailable, sqlite3.Error):
            return {}
        return rec or {}

    def _select(self, where: str, params: tuple) -> list:
        """SELECT 一批 → [dict],舊的排前面。讀不到 → LedgerUnavailable。"""
        try:
            with self._lock:
                conn = self._ensure_conn_locked()
                cur = conn.execute(
                    "SELECT %s FROM deliveries WHERE %s ORDER BY created_at"
                    % (",".join(_COLUMNS), where), params)
                return [self._to_rec(r) for r in cur.fetchall()]
        except sqlite3.Error as e:
            raise LedgerUnavailable("這一刻讀不到寄送帳本") from e

    def has_live_delivery(self, business_key: str) -> bool:
        """這個 business_key 還有沒有「未被否證」的寄送。

        True → 不要再寄（已送達、或結果不明還沒查清楚）。
        ★讀不到就拋,不回答★:回 True 是沒有出口的 fail-closed(2026-08-05
        事故的形狀),回 False 是把「不知道」講成「沒有」——
        接成閘門的人必須自己寫下要怎麼辦。
        ★接閘門請用 `begin_if_no_live()`★:查與寄之間隔著時間就是 TOCTOU。
        """
        try:
            with self._lock:
                conn = self._ensure_conn_locked()
                cur = conn.execute(
                    "SELECT 1 FROM deliveries WHERE business_key=? AND state IN"
                    " (%s) LIMIT 1" % ",".join("?" * len(LIVE_STATES)),
                    (str(business_key), *LIVE_STATES))
                return cur.fetchone() is not None
        except sqlite3.Error as e:
            raise LedgerUnavailable(
                "這一刻讀不到寄送帳本 → 無法判斷是否已經寄過;"
                "呼叫端必須自己決定要擋還是要放(不可以把讀不到當成沒有)") from e

    def state_of(self, delivery_id: str) -> str:
        """某一筆的目前狀態。**查不到／讀不到一律回空字串**。

        空字串 = 不知道,不是「沒送到」:呼叫端(`decide_pending`)靠這個
        區分三態,讀不到就繼續等,不可以拿它當「可以重寄」的證據。
        """
        did = str(delivery_id or "")
        if not did:
            return ""
        try:
            with self._lock:
                conn = self._ensure_conn_locked()
                cur = conn.execute(
                    "SELECT state FROM deliveries WHERE delivery_id=?", (did,))
                row = cur.fetchone()
        except (LedgerUnavailable, sqlite3.Error):
            logging.debug("[delivery] 這一刻讀不到帳本 → 無法回答 %s 的狀態", did)
            return ""
        return str(row[0]) if row else ""

    def unresolved(self) -> list:
        """所有還是 UNKNOWN 的紀錄（給回查流程用），舊的排前面。
        讀不到就拋 —— 空清單會被誤解成「沒有待回查的」。"""
        return self._select("state=?", (UNKNOWN,))

    def stuck_submitting(self, older_than_sec: float = 600.0) -> list:
        """卡在 SUBMITTING 超過一段時間的紀錄 —— 多半是上次送到一半就被砍。
        它們與 UNKNOWN 同樣需要回查，不可以直接當失敗重寄。"""
        return self._select("state=? AND updated_at<?",
                            (SUBMITTING, _now() - older_than_sec))

    def stale_prepared(self, older_than_sec: float = 900.0) -> list:
        """一直停在 PREPARED 的紀錄(只可能來自舊 JSON 匯入)。"""
        return self._select("state=? AND updated_at<?",
                            (PREPARED, _now() - older_than_sec))

    def converge_stale_prepared(self, older_than_sec: float = 900.0) -> int:
        """把舊格式留下的陳舊 PREPARED 收斂成 UNKNOWN。回傳收斂了幾筆。

        ★刻意不是 FAILED★(外審第 10 輪第 3 回 P2-4):舊版的狀態轉移寫回
        是 fail-open 的 —— 磁碟上的 PREPARED 可能是一封【已送達】的信。
        收斂成 UNKNOWN 走 Message-ID 回查,沒有任何一句話是編出來的。
        """
        with self._lock:
            conn = self._ensure_conn_locked()
            with self._txn(conn):
                cur = conn.execute(
                    "UPDATE deliveries SET state=?, updated_at=?, note=?"
                    " WHERE state=? AND updated_at<?",
                    (UNKNOWN, _now(),
                     "舊格式的陳舊 PREPARED:無法確定是否寄出"
                     "(狀態轉移的寫回是 fail-open) → 待 Message-ID 回查",
                     PREPARED, _now() - older_than_sec))
                n = cur.rowcount
        if n:
            logging.warning("[delivery] 收斂 %d 筆舊格式陳舊 PREPARED → UNKNOWN"
                            "(待回查,不可當成沒寄出)", n)
        return n

    def needs_recipient_retry(self) -> list:
        """(delivery_id, [該補寄的收件人]) —— 只含暫時性被拒者。

        ★補寄產生的紀錄不列★(有 `parent_id` 的):同一位收件人的權威狀態在
        【初次】那一筆上,兩邊都列會重複結案、重複告警。
        讀不到就拋 —— 空清單會被讀成「沒有人在等補寄」。
        """
        rows = self._select("parent_id=''", ())
        out = []
        for rec in rows:
            todo = recipients_needing_retry(rec.get("recipients") or {})
            if todo:
                out.append((rec["delivery_id"], todo))
        return sorted(out)

    # ── 維護 ───────────────────────────────────────────────────────────────
    def _prune_locked(self, conn) -> None:
        """剪掉太舊的【已收斂】紀錄。UNKNOWN / SUBMITTING / PREPARED 一律保留
        —— 還沒查清楚的東西不可以因為過期就被當成沒發生過。(在呼叫端的
        交易裡執行)"""
        cutoff = _now() - self._retain_days * _DAY_SEC
        cur = conn.execute(
            "DELETE FROM deliveries WHERE state NOT IN (?,?,?)"
            " AND updated_at<?", (UNKNOWN, SUBMITTING, PREPARED, cutoff))
        if cur.rowcount:
            logging.info("[delivery] 剪掉 %d 筆逾 %d 天的已收斂紀錄",
                         cur.rowcount, self._retain_days)
