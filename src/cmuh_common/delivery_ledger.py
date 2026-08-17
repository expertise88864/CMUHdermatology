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
# v2(2026-08-13 批次AD-3):新增 body_text —— UNKNOWN 查無後要拿什麼補寄。
# ★使用者定案:只落地文字★ 附件是 PHI 截圖,依既有隱私定案不落地。
# v3(2026-08-13 批次AE-1):新增 kind —— 區分「回查驅動的自動補寄」與
# 佇列補寄/初次寄送;自動補寄的次數上限要能跨重啟算得出來。
# v4(2026-08-17 批次AE-4):新增 superseded_by —— ★「補寄鏈已由較新紀錄
# 接手」必須是顯式狀態★:舊版只把 body 清掉,而 `body_text == ""` 同時
# 代表【已送達】【已放棄】【已被接手】三件完全不同的事 —— 結案路徑看到
# 「還有暫時被拒的人 + 沒有 payload」就誤報「始終沒收到,請人工轉寄」,
# 而那封信其實剛剛已經由較新的紀錄送到了(誘導人工重寄=重複通知)。
_SCHEMA_VERSION = 4
_BODY_TEXT_MAX = 100_000        # 信的文字內容上限(夠放最長的會診清單)
# ★body 有自己的保留期,與紀錄本身(45 天)分開★(外審 AD-3 第 1 輪 P1-3)
#   內文可能含臨床資訊 —— 只在「還可能需要補寄」的窗口裡保留。
#   ★[外審 2026-08-13 P1-01] 不可以在 FAILED/PARTIAL 的當下就清★
#   「原信 FAILED、body 已刪、補寄還沒 durable 建立」之間 crash,那封信
#   就永久消失 —— 所以 body 保留到【補寄鏈關閉】:全數送達(CONFIRMED)、
#   明確放棄(abandon,有告警)、或本上限(3 天,隱私天花板;補寄的臨床
#   價值早就衰減完了)。清除不依賴「之後還有信要寄」:啟動時也掃
#   (見 _connect_locked),回查每輪也掃(scrub_stale_bodies)。
BODY_RETAIN_SEC = 3 * 86400.0
# ★補寄鏈的界線★(外審 2026-08-13 P1-01/02):「這筆還欠一次補寄」必須由
#   資料庫自己表達(resends_owed),不靠 call-stack 的順序 —— 只要還欠著,
#   durable payload(body_text)就必須還在。
KIND_AUTO_RESEND = "auto_resend"    # 回查驅動的自動補寄子紀錄(kind 欄)
KIND_QUEUE_RETRY = "queue_retry"    # 退避佇列的補寄子紀錄(批次AE-3:佇列
#   也走 claim,與自動補寄共用同一套 recipient 仲裁;不吃 auto 額度 ——
#   佇列自己有退避上限與用盡告警)
# ★額度數的是「真正跨過 SMTP 邊界」的嘗試,不是 claim 次數★
#   (外審 R3 P1-01):claim COMMIT 之後、send 之前 crash 的子紀錄
#   attempts=0 —— 連續兩次這種 crash 就把臨床通知 abandon,等於
#   「durable at-most-N-claims」而不是 durable work。邊界=呼叫端在
#   send 之前 mark_submitting(attempts+1,已 fsync)。
RESEND_MAX_AUTO = 2                 # 實際進入 SMTP 的自動補寄上限(出口:
#   上限一到就明確放棄+告警;沒有上限的話,收不了信的信箱會被每輪追打)
RESEND_MAX_CLAIMS = 6               # auto claim 總數的硬背擋:機器反覆在
#   claim 與 send 之間中斷(每輪要 15+ 分鐘的收斂才會再 claim)也不可以
#   無限開子紀錄 —— 到頂就放棄+告警(那台機器本身壞了,補寄修不了它)

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
            "created_at", "updated_at", "attempts", "note", "body_text",
            "kind", "superseded_by")


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
            # ★前向版本守衛★(外審 2026-08-13 P2-02):資料庫比本程式【新】
            #   → 拒開,而且★不可以把 meta 降版★ —— rollback 到舊程式後把
            #   v4 資料庫改寫成「schema=3」,之後的新程式會以為不用遷移,
            #   forward-incompatible 的內容就被當成已相容。守衛要在任何
            #   CREATE/ALTER 之前:對太新的資料庫連 schema 都不該碰。
            conn.execute("CREATE TABLE IF NOT EXISTS meta ("
                         " key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            row = conn.execute(
                "SELECT value FROM meta WHERE key='schema_version'").fetchone()
            try:
                existing_ver = int(row[0]) if row else 0
            except (TypeError, ValueError):
                existing_ver = 0
            if existing_ver > _SCHEMA_VERSION:
                raise RuntimeError(
                    "寄送帳本資料庫的 schema(v%d)比本程式支援的(v%d)新 ——"
                    " 這台在跑舊版程式;拒絕開啟(降版改寫會讓新程式誤以為"
                    "不用遷移)" % (existing_ver, _SCHEMA_VERSION))
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
                " note TEXT NOT NULL DEFAULT '',"
                " body_text TEXT NOT NULL DEFAULT '',"
                " kind TEXT NOT NULL DEFAULT '',"
                " superseded_by TEXT NOT NULL DEFAULT '')")
            # ── v1→v2:補 body_text;v2→v3:補 kind;v3→v4:補
            #   superseded_by(批次AE-4)──
            #   CREATE IF NOT EXISTS 對【既有的】舊版資料庫不會補欄位,
            #   要自己 ALTER;新建的資料庫上面就有了。
            cols = {r[1] for r in
                    conn.execute("PRAGMA table_info(deliveries)").fetchall()}
            if "body_text" not in cols:
                conn.execute("ALTER TABLE deliveries ADD COLUMN"
                             " body_text TEXT NOT NULL DEFAULT ''")
            if "kind" not in cols:
                conn.execute("ALTER TABLE deliveries ADD COLUMN"
                             " kind TEXT NOT NULL DEFAULT ''")
            if "superseded_by" not in cols:
                conn.execute("ALTER TABLE deliveries ADD COLUMN"
                             " superseded_by TEXT NOT NULL DEFAULT ''")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_bk"
                         " ON deliveries(business_key)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_deliveries_state"
                         " ON deliveries(state)")
            # ★schema version 是人維護的帳,不是推導出來的★(外審 2026-08-12)
            #   升級是冪等的(上面的 ALTER 保證欄位在)→ 同版/舊版標到目前
            #   版本;比目前新的在上面已經拒開了,走不到這裡。
            conn.execute("INSERT INTO meta(key, value) VALUES"
                         " ('schema_version', ?) ON CONFLICT(key)"
                         " DO UPDATE SET value=excluded.value",
                         (str(_SCHEMA_VERSION),))
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise
        self._conn = conn
        try:
            with self._txn(conn):
                self._scrub_stale_bodies_locked(conn)
        except Exception:
            logging.warning("[delivery] 啟動時清逾期內文失敗(下次再試)",
                            exc_info=True)
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
                "",                     # 舊格式沒有 body_text(補寄自然沒得補)
                "",                     # 舊格式沒有 kind
                "",                     # 舊格式沒有 superseded_by
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
              attachment_hash: str = "", parent_id: str = "",
              body_text: str = "") -> str:
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
                            message_id, attachment_hash, parent_id, body_text)
        with self._lock:
            conn = self._ensure_conn_locked()
            with self._txn(conn):
                self._insert_locked(conn, rec)
                self._prune_locked(conn)
                self._scrub_stale_bodies_locked(conn)
        return did

    def begin_if_no_live(self, *, business_key: str, category: str,
                         recipients: list, subject: str = "",
                         message_id: str = "", attachment_hash: str = "",
                         parent_id: str = "", body_text: str = "") -> str:
        """★原子版的「沒有 live 才登記」★(外審 2026-08-12 P1-08)。

        `has_live_delivery()` + `begin()` 是兩個操作 —— 接成閘門就是 TOCTOU:
        兩個 process 同時查到「沒有」,然後各寄各的。這裡把「查 + 插」放在
        同一筆 BEGIN IMMEDIATE 交易裡,資料庫保證互斥。

        回傳 delivery_id;已有 live 紀錄時回空字串(= 不要寄)。
        目前還沒有呼叫端(閘門未接);接閘門的人用這個,不要用兩段式。
        """
        did = new_delivery_id()
        rec = self._new_rec(did, business_key, category, recipients, subject,
                            message_id, attachment_hash, parent_id, body_text)
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
                 message_id, attachment_hash, parent_id,
                 body_text: str = "", kind: str = "") -> dict:
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
            # ★只落地文字★(批次AD-3,使用者定案 2026-08-13):附件是 PHI
            #   截圖,不落地 —— Sent 查無後的自動補寄只有文字,並註明
            #   請至 HIS 查看。
            # ★子紀錄(有 parent_id)一律不落地 body★(外審 AE-1 第 1 輪
            #   P2-4):payload 的權威在親紀錄上,補寄從不讀子紀錄的 body ——
            #   佇列補寄傳進來的內文只是親紀錄的重複 PHI 副本,卻會依
            #   保留規則活到 3 天 scrub。在資料層強制,所有建立者一起管住。
            "body_text": ("" if parent_id
                          else str(body_text or "")[:_BODY_TEXT_MAX]),
            # ""=初次寄送或佇列補寄;KIND_AUTO_RESEND=回查驅動的自動補寄
            #   (次數上限只數這一種 —— 佇列自己有退避與用盡告警)。
            "kind": str(kind or ""),
            # 非空 = 這條補寄鏈已由那一筆較新的紀錄接手(顯式狀態,
            #   不再用「body 是空的」去暗示三件不同的事)。
            "superseded_by": "",
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

    def _mutate_recipients_locked(self, delivery_id: str, fn, note: str = "",
                                  clear_body: bool = False):
        """讀一筆 → 讓 `fn(states)` 改收件人狀態 → summarize → 寫回。

        整段在同一筆 IMMEDIATE 交易裡:兩個 process 對同一筆的讀-改-寫
        不再可能交錯(舊版的 last-writer-wins 正是從這裡來的)。
        回傳 (新整筆狀態, fn 的回傳值);查無此筆 → (FAILED, None)。
        """
        with self._lock:
            conn = self._ensure_conn_locked()
            with self._txn(conn):
                return self._mutate_states_in_txn(conn, delivery_id, fn,
                                                  note=note,
                                                  clear_body=clear_body)

    def _mutate_states_in_txn(self, conn, delivery_id: str, fn,
                              note: str = "", clear_body: bool = False):
        """`_mutate_recipients_locked` 的內層(★在呼叫端的交易裡執行★)。

        抽出來是為了讓「一次 RCPT 的結果」可以把【子紀錄拒收 + 親紀錄
        分類升級 + 嘗試邊界】寫在★同一筆交易★裡(外審 AE-5 第 1 輪 P2):
        拆成三筆的話,子已 permanent、親還是 transient 的中間狀態會被
        別的路徑讀到,做出前後不一致的決定。
        """
        rec = self._get_row_locked(conn, delivery_id)
        if rec is None:
            return FAILED, None
        states = dict(rec.get("recipients") or {})
        out = fn(states)
        new_state = summarize(states)
        # ★body 保留到補寄鏈關閉★(外審 2026-08-13 P1-01,取代
        #   AD-3 的「終局即清」):FAILED/PARTIAL 還欠補寄 ——
        #   在「補寄 durable 建立」之前把 payload 刪掉,中間 crash
        #   那封信就永久消失。只有【全數送達】(CONFIRMED)或
        #   【明確放棄】(clear_body=True,呼叫端已告警)才清;
        #   隱私天花板是 3 天的 scrub(獨立於狀態)。
        keep_body = ("" if (clear_body or new_state == CONFIRMED)
                     else rec.get("body_text") or "")
        conn.execute(
            "UPDATE deliveries SET recipients=?, state=?, note=?,"
            " updated_at=?, body_text=? WHERE delivery_id=?",
            (json.dumps(states, ensure_ascii=False), new_state,
             (str(note)[:300] if note else rec.get("note") or ""),
             _now(), keep_body, rec["delivery_id"]))
        return new_state, out

    def record_rcpt_outcome(self, child_id: str, parent_id: str,
                            refused: dict) -> bool:
        """★一次 RCPT 的結果,一筆交易★(外審 AE-5 第 1 輪 P2)。→ 成功嗎。

        同一筆 BEGIN IMMEDIATE 裡做完三件事:
          (1) 子紀錄記下逐位拒收(它剛建立,收件人都還是 UNKNOWN);
          (2) 親紀錄★分類★升級 —— 永久被拒(550)不論原本是 UNKNOWN 或
              TRANSIENT 都升(單調,絕不推翻 CONFIRMED);暫時性只補
              還沒有結論的(不動既有的 TRANSIENT/PERMANENT);
          (3) 子紀錄 attempts+1 = 真正跨過 SMTP protocol boundary。
        拆成三筆的話會出現「子已 permanent、親還是 transient」的中間狀態:
        claim 交易看子紀錄而拒絕補寄,佇列的終局判斷只看親紀錄而繼續退避,
        最後用「暫時性拒收用盡」的語氣對一個【確定不存在】的信箱告警。
        任何一步失敗 → 整筆回捲 → 呼叫端(補寄路徑)在 DATA 之前中止。
        """
        cid = str(child_id or "")
        if not cid:
            return False
        bad = {}
        for addr, info in (refused or {}).items():
            code = info[0] if isinstance(info, (tuple, list)) and info else info
            bad[str(addr).strip().lower()] = classify_refusal(code)

        def _child(states: dict):
            for addr, st in bad.items():
                if states.get(addr) == R_UNKNOWN:
                    states[addr] = st
            return None

        def _parent(states: dict):
            for addr, st in bad.items():
                cur = states.get(addr)
                if cur is None or cur == R_CONFIRMED:
                    continue            # 沒這個人 / 已送達(更強的結論)
                if st == R_PERMANENT:
                    states[addr] = R_PERMANENT      # 單調升級
                elif cur == R_UNKNOWN:
                    states[addr] = st               # 只補沒有結論的
            return None

        try:
            with self._lock:
                conn = self._ensure_conn_locked()
                with self._txn(conn):
                    if bad:
                        self._mutate_states_in_txn(conn, cid, _child)
                        if str(parent_id or ""):
                            self._mutate_states_in_txn(conn, str(parent_id),
                                                       _parent)
                    conn.execute(
                        "UPDATE deliveries SET state=?, attempts=attempts+1,"
                        " updated_at=? WHERE delivery_id=?",
                        (SUBMITTING, _now(), cid))
            return True
        except Exception:
            logging.warning("[delivery] 逐位 RCPT 結果落不了地(整筆回捲)",
                            exc_info=True)
            return False

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

    def record_refusals(self, delivery_id: str, refused: dict) -> list:
        """把【已知的 SMTP 逐位拒收】落地。→ 真的改到的人。

        ★存在的理由(外審 AE-3 第 2 輪 P1)★ `settle` 寫不進帳的那一刻,
        「B 被 4xx 拒收」只活在記憶體的退避佇列裡,帳上 B 還是 UNKNOWN。
        而 Message-ID 回查是【整封】粒度:它在寄件備份查到這封信(因為
        A 確實收到了)就把【所有】UNKNOWN 收件人一起判成已送達 ——
        B 那筆明確的拒收被永久覆蓋,補寄路徑之後看到 CONFIRMED 就結案,
        變成沉默的漏寄。所以已知的拒收要盡快落地,搶在粗粒度回查之前。

        ★只有仍 R_UNKNOWN 的會被改★(外審 AE-3 第 3 輪 P1):
        * CONFIRMED:那是「後來真的送達了」的權威結論(補寄成功會這樣
          寫),推翻它會讓已經收到的人再收一封;
        * ★PERMANENT:那是【已經結束】的狀態★ —— 5xx 位址錯誤、或補寄
          上限用盡後的明確放棄(`abandon_recipient_retry`)。被延遲的
          佇列項手上還握著一張舊的 421,重新把它打開就是繞過那些上限
          再寄一次(抑制的出口被自己的復原機制拆掉);
        * TRANSIENT:已經是「待補寄」了,不需要也不應該再翻動時間戳。
        這個原語只做一件事:把【還沒有結論】的收件人補上已知的拒收。
        """
        bad = {}
        for addr, info in (refused or {}).items():
            code = info[0] if isinstance(info, (tuple, list)) and info else info
            bad[str(addr).strip().lower()] = classify_refusal(code)
        if not bad:
            return []

        def _apply(states: dict):
            done = []
            for addr, st in bad.items():
                if states.get(addr) == R_UNKNOWN:
                    states[addr] = st
                    done.append(addr)
            return sorted(done)

        _state, done = self._mutate_recipients_locked(delivery_id, _apply)
        return done or []

    def mark_permanently_refused(self, delivery_id: str, addrs: list) -> list:
        """把這幾位在【這一筆】上升級成永久被拒。→ 真的改到的人。

        ★[外審 2026-08-17 P2-02] 結論要單調地往上傳★:補寄時對方回 550
        (查無此人),那是比「暫時被拒」更強的結論 —— 不往上傳的話,親紀錄
        永遠停在 TRANSIENT,那個不存在的信箱會一路吃完佇列退避、durable
        補寄額度,最後的告警還用「暫時性拒收」的語氣描述。
        ★絕不推翻 CONFIRMED★:那是「真的送達了」,比任何拒收都強。
        """
        want = {str(a).strip().lower() for a in (addrs or [])}
        if not want:
            return []

        def _apply(states: dict):
            done = sorted(a for a in states
                          if a in want and states[a] not in (R_CONFIRMED,
                                                             R_PERMANENT))
            for a in done:
                states[a] = R_PERMANENT
            return done

        _state, done = self._mutate_recipients_locked(delivery_id, _apply)
        return done or []

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
        ★放棄=補寄鏈明確關閉★ → body 一併清掉(它只為補寄而存在;
        呼叫端在這之後要告警,見 `_resend_owed_one` /
        `_close_out_stale_recipient_retries`)。
        """
        def _apply(states: dict):
            gone = sorted(a for a, st in states.items() if st == R_TRANSIENT)
            for a in gone:
                states[a] = R_PERMANENT
            return gone

        _state, gone = self._mutate_recipients_locked(
            delivery_id, _apply, note=(str(note) or "補寄已放棄"),
            clear_body=True)
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

    def claim_resend_child(self, parent_id: str, *, business_key: str,
                           category: str, recipients: list,
                           subject: str = "", message_id: str = "",
                           kind: str = KIND_AUTO_RESEND) -> str:
        """★「查 + 驗 + 登記補寄」在同一筆交易裡★(AD-3 P1-2/R3 P1-01/02)

        這裡是所有補寄 sender(回查的自動補寄【與】退避佇列)共用的
        recipient 仲裁 —— 外面的預檢只是省工,正確性在交易內。
        回傳新 delivery_id;不該補時回 ""(呼叫端用 `get()` 讀子紀錄的
        recipients 當實際寄送對象 —— 交易可能把名單縮小)。判準:

        * 有【結果未定】的子紀錄(SUBMITTING/PREPARED/UNKNOWN,任何
          kind)→ 不補:那封可能已送達,同一時刻只有一個 sender。
        * ★目標只留「此刻仍暫時性被拒、且沒有任何子紀錄已送達過」的人★
          (外審 R3 P1-02):佇列的子紀錄送達了、還沒回寫親紀錄的瞬間,
          另一個 executor 讀親紀錄仍看到 TRANSIENT —— 已送達集合要在
          【同一筆交易裡】從子紀錄算出來,不能只信親紀錄。名單空 → ""。
        * auto 額度(外審 R3 P1-01):已【實際進入 SMTP】(attempts>0,
          呼叫端在 send 前 mark_submitting)的 auto 子紀錄 ≥
          RESEND_MAX_AUTO → 不補;auto claim 總數 ≥ RESEND_MAX_CLAIMS
          → 不補(硬背擋)。★claim 本身不扣額度★ —— claim 後、send 前
          crash 的子紀錄(attempts=0)只佔 claim 背擋,不佔嘗試額度。
          佇列(kind=KIND_QUEUE_RETRY)不計入兩者。

        ★補寄紀錄自己不落地 body★:payload 的權威在【親紀錄】上,
        鏈沒關閉之前它都在(P1-01 的保留規則)。落不了地就拋
        LedgerUnavailable(呼叫端不寄:沒有帳的補寄會破壞有界性)。
        """
        pid = str(parent_id or "")
        if not pid:
            return ""
        want = [str(a).strip().lower() for a in (recipients or [])
                if str(a).strip()]
        if not want:
            return ""
        did = new_delivery_id()
        with self._lock:
            conn = self._ensure_conn_locked()
            with self._txn(conn):
                rows = conn.execute(
                    "SELECT state, kind, attempts, recipients FROM deliveries"
                    " WHERE parent_id=?", (pid,)).fetchall()
                if any(str(st) in (SUBMITTING, PREPARED, UNKNOWN)
                       for st, _k, _a, _r in rows):
                    return ""
                delivered_by_children: set = set()
                # ★永久被拒也是結論★(外審 2026-08-17 P2-02):補寄時收到
                #   550 的那位不該再寄 —— 就算親紀錄上還來不及升級。
                concluded_by_children: set = set()
                for _st, _k, _a, raw in rows:
                    try:
                        child_states = _clean_recipients(json.loads(raw or "{}"))
                    except (TypeError, ValueError):
                        child_states = {}
                    delivered_by_children.update(
                        a for a, st in child_states.items()
                        if st == R_CONFIRMED)
                    concluded_by_children.update(
                        a for a, st in child_states.items()
                        if st == R_PERMANENT)
                prow = conn.execute(
                    "SELECT recipients, business_key, created_at,"
                    " superseded_by FROM deliveries WHERE delivery_id=?",
                    (pid,)).fetchone()
                if prow is None:
                    return ""
                # ★已被接手的鏈不得再產生任何工作★(外審第五輪 P1-03):
                #   `superseded_by` 宣稱的是 durable 的 ownership transfer,
                #   那就必須是【資料層】的 fence —— 只擋掃描端的話,還活在
                #   記憶體裡的舊佇列項照樣能從已交棒的親紀錄開新子紀錄,
                #   與接手者的補寄同時寄給同一個人。
                if str(prow[3] or ""):
                    return ""
                try:
                    parent_states = _clean_recipients(
                        json.loads((prow[0] if prow else "") or "{}"))
                except (TypeError, ValueError):
                    parent_states = {}
                # ★同 business_key 底下任何一筆的已送達也要看★(外審 AE-3
                #   第 1 輪 F2):工作層重跑的較新 sibling(可能 bodyless
                #   PARTIAL)把 A 送到了,舊親紀錄還掛著 TRANSIENT ——
                #   只看自己的子紀錄,A 會再收一封。
                pbk = str(prow[1]) if prow else ""
                if pbk:
                    for (raw,) in conn.execute(
                            "SELECT recipients FROM deliveries WHERE"
                            " business_key=? AND delivery_id!=?", (pbk, pid)):
                        try:
                            states = _clean_recipients(
                                json.loads(raw or "{}"))
                        except (TypeError, ValueError):
                            states = {}
                        delivered_by_children.update(
                            a for a, st in states.items()
                            if st == R_CONFIRMED)
                        concluded_by_children.update(
                            a for a, st in states.items()
                            if st == R_PERMANENT)
                    # ★同一事件已經有【較新的存活初次寄送】就不要補★
                    #   (外審 2026-08-17 P1-02 的最低限度):工作層新一輪
                    #   剛 begin 的那一筆此刻全是 UNKNOWN —— 只看「已送達」
                    #   擋不住它,兩封會同時跨過 SMTP 邊界 = 重複的臨床通知。
                    #   ★這個檢查必須在【同一筆交易】裡★:掃描端的
                    #   `newer_sibling_takeover` 與這裡之間有 TOCTOU 窗口。
                    #   (完整的 event-level 寄送閘門仍是待定案的政策題;
                    #    這裡先關掉補寄側可以自己關的那一半。)
                    #   ★只擋【還在飛】的那種★(SUBMITTING/PREPARED/
                    #   UNKNOWN):它可能正要送給同一批人。已收斂的較新
                    #   紀錄(CONFIRMED/PARTIAL/FAILED)不擋 —— 它送到的人
                    #   已經在上面的已送達集合裡排除,沒送到的人本來就該由
                    #   這條鏈補(或由 takeover 收掉整條鏈)。
                    _pat = _as_epoch(prow[2]) if prow else 0.0
                    _inflight = (SUBMITTING, PREPARED, UNKNOWN)
                    live_newer = conn.execute(
                        "SELECT 1 FROM deliveries WHERE business_key=?"
                        " AND parent_id='' AND delivery_id!=? AND created_at>?"
                        " AND state IN (?,?,?) LIMIT 1",
                        (pbk, pid, _pat, *_inflight)).fetchone()
                    if live_newer:
                        return ""
                targets = [a for a in want
                           if parent_states.get(a) == R_TRANSIENT
                           and a not in delivered_by_children
                           and a not in concluded_by_children]
                if not targets:
                    return ""
                if str(kind) == KIND_AUTO_RESEND:
                    autos = [(st, a) for st, k, a, _r in rows
                             if str(k) == KIND_AUTO_RESEND]
                    started = sum(1 for _st, a in autos
                                  if _as_attempts(a) > 0)
                    if started >= RESEND_MAX_AUTO:
                        return ""
                    if len(autos) >= RESEND_MAX_CLAIMS:
                        return ""
                rec = self._new_rec(did, business_key, category, targets,
                                    subject, message_id, "", pid, "",
                                    kind=str(kind))
                self._insert_locked(conn, rec)
        return did

    def resend_children(self, parent_id: str) -> list:
        """這筆原信的所有補寄子紀錄(含佇列補寄)。讀不到 → LedgerUnavailable
        (空清單會被讀成「沒補過」→ 重複寄)。"""
        return self._select("parent_id=?", (str(parent_id or ""),))

    def resends_owed(self, *, min_age_sec: float) -> list:
        """★「還欠一次補寄」由資料庫自己回答★(外審 2026-08-13 P1-01/02)

        = 親紀錄(parent_id='')+ payload 還在(body_text≠'')+ 已被否證
        (FAILED)或部分未達(PARTIAL)+ 距上次變動超過 min_age_sec。
        呼叫端(`Reconciler._resend_owed_one`)再做 in-flight/上限/較新
        同 key 的判斷。年齡門檻讓記憶體退避佇列先跑完它的快路徑。
        讀不到 → LedgerUnavailable(空清單=「沒有人欠補寄」,不可假裝)。
        """
        return self._select(
            "parent_id='' AND superseded_by='' AND body_text!=''"
            " AND state IN (?,?) AND updated_at<?",
            (FAILED, PARTIAL, _now() - float(min_age_sec)))

    def supersede(self, delivery_id: str, *, by: str, note: str = "") -> bool:
        """這條補寄鏈由 `by` 那一筆接手 → 記下顯式狀態並清 payload。→ 有改到嗎。

        ★[外審 2026-08-17 P2-01]★ 舊版接手時只 `clear_body()`,而
        `body_text == ""` 同時代表【已送達】【已放棄】【已被接手】三件事:
        結案路徑看到「還有暫時被拒的人 + 沒有 payload」就判定「durable 補寄
        已無能為力」→ 明確結案 + 寄出「這幾位始終沒收到,請人工確認/轉寄」
        —— 但那封信剛剛已經由較新的紀錄送到了,告警反而誘導人工重寄。
        收件人狀態刻意【不動】(沒有證據說這一筆送到了);排除靠
        `superseded_by`,見 `needs_recipient_retry`。

        ★有 in-flight 子紀錄時不准交棒★(外審 AE-5 第 1 輪 P1):
        claim 交易已經擋掉「先交棒、後 claim」,但反過來的交錯 ——
        舊佇列先 claim 出 SUBMITTING 子紀錄、另一個 process 才 supersede
        —— 舊鏈仍會把那封寄出去,與接手者同時寄給同一個人。兩邊都在
        BEGIN IMMEDIATE 裡檢查對方的存在,誰先拿到寫鎖誰成立。
        交棒被拒時回 False:呼叫端下一輪再試(那個子紀錄一定會收斂)。
        """
        did = str(delivery_id or "")
        if not did:
            return False
        with self._lock:
            conn = self._ensure_conn_locked()
            with self._txn(conn):
                inflight = conn.execute(
                    "SELECT 1 FROM deliveries WHERE parent_id=? AND state IN"
                    " (?,?,?) LIMIT 1",
                    (did, SUBMITTING, PREPARED, UNKNOWN)).fetchone()
                if inflight:
                    return False
                cur = conn.execute(
                    "UPDATE deliveries SET body_text='', superseded_by=?,"
                    " note=?, updated_at=? WHERE delivery_id=?",
                    (str(by or "")[:64],
                     (str(note)[:300] if note else "補寄鏈已由較新紀錄接手"),
                     _now(), did))
                return bool(cur.rowcount)

    def clear_body(self, delivery_id: str, note: str = "") -> bool:
        """補寄鏈明確關閉(不經收件人狀態變動)→ 清掉 payload。→ 有清到嗎。

        給「同 business_key 已有較新的存活寄送」「已無暫時性待補收件人」
        這類【不改收件人狀態、只結鏈】的出口用;失敗拋 LedgerUnavailable。
        """
        did = str(delivery_id or "")
        if not did:
            return False
        with self._lock:
            conn = self._ensure_conn_locked()
            with self._txn(conn):
                if note:
                    cur = conn.execute(
                        "UPDATE deliveries SET body_text='', note=?,"
                        " updated_at=? WHERE delivery_id=? AND body_text!=''",
                        (str(note)[:300], _now(), did))
                else:
                    cur = conn.execute(
                        "UPDATE deliveries SET body_text='', updated_at=?"
                        " WHERE delivery_id=? AND body_text!=''", (_now(), did))
                return bool(cur.rowcount)

    def newer_sibling_takeover(self, business_key: str, *,
                               than_created_at: float) -> tuple:
        """同 business_key、較新的【初次】紀錄能不能接走補寄義務。
        → (verdict, 較新紀錄裡已確認送達的收件人 list, 接手者 delivery_id)。
        verdict: "takeover"(可,舊鏈結案)/"wait"(等它收斂)/
        ""(沒有/沒本錢,舊鏈續扛)。

        ★已送達名單一併帶回★(外審 AE-3 第 1 輪 F2):bodyless PARTIAL
        sibling 送達了 A、暫時被拒 B —— verdict 是 ""(它沒本錢扛),
        但 A 已經收到了:呼叫端要先把 A 回寫舊親紀錄再補,不然 A 會
        再收一封。

        ★同一把 key 只留最新一條補寄鏈★(外審 AE-1 第 1 輪 P1-1):
        工作層每次重試都 begin 新的一筆 —— 每筆各自走 resends_owed 的話,
        SMTP 恢復後同一份通知會寄多次。但【接走義務】要有本錢
        (外審 R3 P1-03):舊 JSON 匯入的紀錄天生沒有 body ——
        混版部署期,舊程式寫下的較新 bodyless 紀錄若光憑「較新」就讓
        舊鏈 clear_body,唯一的 durable payload 就被刪掉,兩筆都不再
        actionable = 永久漏寄。所以:

        * 較新者已 CONFIRMED、或自己有 body(能扛補寄)→ "takeover"。
        * 較新者 bodyless 且結果未定(SUBMITTING/PREPARED/UNKNOWN)→
          "wait":它可能已送達,先等回查收斂 —— 收斂成 CONFIRMED 就
          takeover,收斂成 FAILED 就落到下一條。
        * 其餘(bodyless 且已收斂 FAILED/PARTIAL)→ "":沒本錢也沒送達,
          舊的 payload-bearing 鏈繼續扛。

        ★讀不到 → raise,不回 takeover★(外審 AE-1 第 1 輪 P1-2):
        呼叫端把 takeover 當成「可以結鏈」的證據,而結鏈會刪掉唯一的
        durable payload,不可逆。「跳過本輪」必須由呼叫端自己明寫。
        """
        try:
            with self._lock:
                conn = self._ensure_conn_locked()
                rows = conn.execute(
                    "SELECT state, body_text, recipients, delivery_id FROM"
                    " deliveries WHERE business_key=? AND parent_id=''"
                    " AND created_at>?",
                    (str(business_key), float(than_created_at))).fetchall()
        except sqlite3.Error as e:
            raise LedgerUnavailable(
                "這一刻查不出 %s 有沒有較新的同 key 紀錄" % business_key
            ) from e
        delivered: set = set()
        for _st, _b, raw, _id in rows:
            try:
                states = _clean_recipients(json.loads(raw or "{}"))
            except (TypeError, ValueError):
                states = {}
            delivered.update(a for a, st in states.items()
                             if st == R_CONFIRMED)
        able = [r for r in rows if str(r[0]) == CONFIRMED or str(r[1] or "")]
        if able:
            return "takeover", sorted(delivered), str(able[0][3])
        if any(str(st) in (SUBMITTING, PREPARED, UNKNOWN)
               for st, _b, _r, _id in rows):
            return "wait", sorted(delivered), ""
        return "", sorted(delivered), ""

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
        ★已被接手的鏈(superseded_by 非空)也不列★(外審 2026-08-17 P2-01):
        它的義務已經轉給較新的那一筆,再列就會對【已經送達的事】發出
        「始終沒收到」的告警,誘導人工重寄。
        讀不到就拋 —— 空清單會被讀成「沒有人在等補寄」。
        """
        rows = self._select("parent_id='' AND superseded_by=''", ())
        out = []
        for rec in rows:
            todo = recipients_needing_retry(rec.get("recipients") or {})
            if todo:
                out.append((rec["delivery_id"], todo))
        return sorted(out)

    # ── 維護 ───────────────────────────────────────────────────────────────
    def scrub_stale_bodies(self) -> int:
        """把超過保留期的 body 清掉(公開、自帶交易)。→ 清了幾筆。

        ★[外審 AD-3 第 2 輪 P1] 要掛在【保證週期性】的路徑上★
        啟動時+begin 交易裡的掃除,對「常駐好幾週、不再寄信」的行程
        永遠不會跑 —— 內文就超過宣稱的 3 天保留期。回查
        (`Reconciler.run_once`)在兩支程式都是排程驅動、與寄信量無關,
        每一輪開頭呼叫這裡。
        """
        with self._lock:
            conn = self._ensure_conn_locked()
            with self._txn(conn):
                return self._scrub_stale_bodies_locked(conn)

    def _scrub_stale_bodies_locked(self, conn) -> int:
        """把超過保留期的 body 清掉(狀態不動)。★獨立於「之後還有沒有信」★

        (在呼叫端的交易裡執行)全數送達的清除靠 `_mutate_recipients_locked`,
        明確放棄靠 `abandon_recipient_retry`/`clear_body`;這裡是隱私天花板:
        不管鏈開著沒有,body 最多留 3 天。
        ★清到「鏈還開著」的要大聲講★(抑制的出口不可以是無聲的):
        body 一清,`resends_owed` 就不再回報它 —— 那等於放棄補寄,
        不能只有一行 info。只記 id/state/category,不記內文與收件人。
        """
        cutoff = _now() - BODY_RETAIN_SEC
        rows = conn.execute(
            "SELECT delivery_id, state, category FROM deliveries"
            " WHERE body_text!='' AND created_at<?", (cutoff,)).fetchall()
        cur = conn.execute(
            "UPDATE deliveries SET body_text='' WHERE body_text!=''"
            " AND created_at<?", (cutoff,))
        for did, state, category in rows:
            if str(state) != CONFIRMED:
                logging.error(
                    "[delivery] ★%s(state=%s,category=%s)的內文到 3 天"
                    "保留上限仍未收斂 → 已清除,不再自動補寄★(到期放棄)",
                    did, state, category)
        if cur.rowcount:
            logging.info("[delivery] 清掉 %d 筆逾 %.0f 天的信件內文",
                         cur.rowcount, BODY_RETAIN_SEC / 86400.0)
        return cur.rowcount

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
