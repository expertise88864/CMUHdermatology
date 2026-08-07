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
"""
from __future__ import annotations

import logging
import threading
from contextlib import contextmanager
import time
import uuid
from typing import Optional

from cmuh_common.atomic_io import atomic_write_json, safe_load_json_ex
from cmuh_common.paths import get_settings_dir

LEDGER_FILENAME = "delivery_ledger.json"

# ── 整筆寄送的狀態 ──────────────────────────────────────────────────────────
PREPARED = "prepared"        # 已建檔，還沒真的送出
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

RETAIN_DAYS = 45              # 舊紀錄保留天數（需大於任何 business 的前瞻視窗）
_DAY_SEC = 86400.0


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


class DeliveryLedger:
    """寄送帳本。執行緒安全；每次變更都原子落地（跨重啟可恢復）。"""

    def __init__(self, path: "Optional[str]" = None, *,
                 retain_days: int = RETAIN_DAYS):
        import os
        self.path = path or os.path.join(get_settings_dir(), LEDGER_FILENAME)
        self._retain_days = retain_days
        self._lock = threading.RLock()
        self._records: dict = {}
        # ★[2026-08-07 外審第 8 輪 P1-01] 本 process 自己動過的紀錄★
        #   寫回時只寫這些,其餘從磁碟重讀後合併 —— 見 `_save_locked`。
        self._dirty: set = set()
        self._load_failed = False
        self._load()

    # ── 持久化 ─────────────────────────────────────────────────────────────
    def _load(self) -> None:
        data, status = safe_load_json_ex(self.path, {}, backup_on_corrupt=False)
        if status == "error":
            # ★讀不到 ≠ 沒有★ 直接當空的會讓所有 business_key 看起來「沒寄過」
            # → 整批重寄。標記起來，之後不可以用空紀錄覆蓋磁碟。
            self._load_failed = True
            logging.error("[delivery] 帳本暫時讀不到(檔案仍在?) → 本次執行"
                          "【不會】覆寫它，且視為所有紀錄都還在：%s", self.path)
            return
        if status == "corrupt":
            self._load_failed = True
            logging.error("[delivery] 帳本內容損壞，已保留原檔不覆寫：%s", self.path)
            return
        if isinstance(data, dict):
            self._records = {k: v for k, v in data.items() if isinstance(v, dict)}

    @contextmanager
    def _interprocess_lock(self):
        """跨 process 檔案鎖(sidecar `.lock`)。

        ★[2026-08-07 外審第 8 輪 P1-01]★ `threading.RLock` 只鎖得住同一個
        process 裡的執行緒。但這本帳是【主程式與會診程式共用】的 —— 兩個
        不同的 process。舊寫法是「把本 process 記憶體裡的整份紀錄覆蓋整個檔案」,
        於是:

            main    讀到 {A}          consult 讀到 {A}
            main    寫回 {A,B}
            consult 寫回 {A,C}        ← B 永久消失

        `os.replace` 是原子的,但它保證的是「不會寫出半個 JSON」,
        擋不住 lost update。

        ★取不到鎖時仍然寫(fail-open)★:鎖不到就不寫,等於為了避免「可能覆蓋」
        而造成「一定丟失」。退化成舊行為 + 一行警告,比靜默丟資料好。

        ★只實作 Windows★:本產品(systemftp/win32gui/隱藏桌面)只跑在 Windows,
        CI 也是。加一條永遠不會被執行的 POSIX 分支,是「看起來比較周全」的死路 ——
        它無法被任何測試涵蓋,只會帶進型別債與假的安心感。非 Windows 環境
        直接退化成只有執行緒鎖(見下面的 fail-open 說明)。
        """
        import os as _os
        lock_path = self.path + ".lock"
        fh = None
        locked = False
        try:
            try:
                import msvcrt  # noqa: PLC0415  (Windows-only,見上面說明)
                _os.makedirs(_os.path.dirname(lock_path) or ".", exist_ok=True)
                fh = open(lock_path, "a+b")
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
                locked = True
            except ImportError:
                logging.debug("[delivery] 非 Windows 環境 → 沒有跨 process 檔案鎖")
            except Exception:
                logging.warning("[delivery] 取不到帳本檔案鎖 → 本次仍寫入"
                                "(可能與另一個程式互相覆蓋)", exc_info=True)
            yield
        finally:
            if fh is not None:
                if locked:
                    try:
                        import msvcrt  # noqa: PLC0415
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    except Exception:
                        logging.debug("[delivery] 釋放帳本檔案鎖失敗", exc_info=True)
                try:
                    fh.close()
                except Exception:
                    pass

    def _save_locked(self) -> None:
        if self._load_failed:
            logging.warning("[delivery] 本次執行曾讀不到帳本 → 不寫回"
                            "(避免用不完整的內容覆蓋磁碟)")
            return
        with self._interprocess_lock():
            # ★鎖內重讀 → 合併 → 寫★(外審第 8 輪 P1-01)
            #   只把【本 process 動過的】紀錄蓋上去;其餘一律以磁碟為準。
            #   delivery_id 是全域唯一的,所以兩個 process 不可能改到同一筆 ——
            #   衝突只會是「用我手上的舊副本蓋掉對方的新版本」,而這個合併
            #   剛好精確地避開它。順帶讓本 process 看得到對方新增的紀錄。
            disk, status = safe_load_json_ex(self.path, {},
                                             backup_on_corrupt=False)
            merged = {}
            if status == "ok" and isinstance(disk, dict):
                merged = {k: v for k, v in disk.items() if isinstance(v, dict)}
            elif status in ("error", "corrupt"):
                # 這一刻讀不到磁碟 → 不能合併,也不能拿記憶體整份去蓋。
                logging.warning("[delivery] 寫回前讀不到磁碟內容 → 本次不寫回"
                                "(避免覆蓋掉別的程式的紀錄)")
                return
            for did in self._dirty:
                rec = self._records.get(did)
                if rec is not None:
                    merged[did] = rec
            self._records = merged
            self._prune_locked()
            try:
                atomic_write_json(self.path, self._records)
                self._dirty.clear()
            except Exception:
                logging.warning("[delivery] 帳本寫入失敗(記憶體仍有紀錄)",
                                exc_info=True)

    # ── 生命週期 ───────────────────────────────────────────────────────────
    def begin(self, *, business_key: str, category: str, recipients: list,
              subject: str = "", message_id: str = "",
              attachment_hash: str = "") -> str:
        """登記一次即將寄出的信。回傳 delivery_id。

        ★必須在真正送出【之前】呼叫★ —— 這樣即使送出當下斷電，重啟後看到的是
        一筆 SUBMITTING，而不是「什麼都沒發生」。
        """
        did = new_delivery_id()
        with self._lock:
            self._records[did] = {
                "delivery_id": did,
                "business_key": str(business_key),
                "category": str(category),
                "subject": str(subject)[:200],
                "message_id": str(message_id),
                "attachment_hash": str(attachment_hash),
                "state": PREPARED,
                "recipients": {str(r).strip().lower(): R_UNKNOWN
                               for r in recipients if str(r).strip()},
                "created_at": _now(),
                "updated_at": _now(),
                "attempts": 0,
                "note": "",
            }
            self._dirty.add(did)
            self._save_locked()
        return did

    def mark_submitting(self, delivery_id: str) -> None:
        with self._lock:
            rec = self._records.get(delivery_id)
            if rec is None:
                return
            rec["state"] = SUBMITTING
            rec["attempts"] = int(rec.get("attempts", 0)) + 1
            rec["updated_at"] = _now()
            self._dirty.add(delivery_id)
            self._save_locked()

    def settle(self, delivery_id: str, *, refused: "Optional[dict]" = None,
               unknown: bool = False, failed: bool = False,
               note: str = "") -> str:
        """寫入結果並算出整筆狀態。回傳新的整筆狀態。

        refused: smtplib 回傳的 {收件人: (碼, 訊息)}；空 dict = 全部送達。
        unknown: 逾時等「可能已送達」→ 所有還沒有結論的收件人記為 UNKNOWN。
        failed : 確定沒送出（例如連線階段就失敗）。
        """
        with self._lock:
            rec = self._records.get(delivery_id)
            if rec is None:
                return FAILED
            states = dict(rec.get("recipients") or {})
            if failed:
                for addr in states:
                    states[addr] = R_PERMANENT if states[addr] == R_PERMANENT \
                        else R_TRANSIENT
            elif unknown:
                for addr in states:
                    if states[addr] == R_UNKNOWN:
                        states[addr] = R_UNKNOWN      # 保持不明，等回查
            else:
                bad = {}
                for addr, info in (refused or {}).items():
                    code = info[0] if isinstance(info, (tuple, list)) and info \
                        else info
                    bad[str(addr).strip().lower()] = classify_refusal(code)
                for addr in states:
                    states[addr] = bad.get(addr, R_CONFIRMED)
            rec["recipients"] = states
            rec["state"] = FAILED if failed else summarize(states)
            if note:
                rec["note"] = str(note)[:300]
            rec["updated_at"] = _now()
            self._dirty.add(delivery_id)
            self._save_locked()
            return rec["state"]

    def resolve_unknown(self, delivery_id: str, *, delivered: bool,
                        note: str = "") -> str:
        """Message-ID 回查的結果：找到＝送達、確定不存在＝失敗（可重寄）。"""
        with self._lock:
            rec = self._records.get(delivery_id)
            if rec is None:
                return FAILED
            states = dict(rec.get("recipients") or {})
            for addr, st in states.items():
                if st == R_UNKNOWN:
                    states[addr] = R_CONFIRMED if delivered else R_TRANSIENT
            rec["recipients"] = states
            rec["state"] = summarize(states)
            rec["note"] = (note or ("寄件備份查到" if delivered else "寄件備份查無"))[:300]
            rec["updated_at"] = _now()
            self._dirty.add(delivery_id)
            self._save_locked()
            return rec["state"]

    # ── 查詢 ───────────────────────────────────────────────────────────────
    def get(self, delivery_id: str) -> dict:
        with self._lock:
            return dict(self._records.get(delivery_id) or {})

    def has_live_delivery(self, business_key: str) -> bool:
        """這個 business_key 還有沒有「未被否證」的寄送。

        True → 不要再寄（已送達、或結果不明還沒查清楚）。
        這正是取代「UNKNOWN 到底算成功還算失敗」那個二選一的地方。
        """
        with self._lock:
            return any(r.get("business_key") == business_key
                       and r.get("state") in LIVE_STATES
                       for r in self._records.values())

    def unresolved(self) -> list:
        """所有還是 UNKNOWN 的紀錄（給回查流程用），舊的排前面。"""
        with self._lock:
            out = [dict(r) for r in self._records.values()
                   if r.get("state") == UNKNOWN]
        return sorted(out, key=lambda r: r.get("created_at") or 0)

    def stuck_submitting(self, older_than_sec: float = 600.0) -> list:
        """卡在 SUBMITTING 超過一段時間的紀錄 —— 多半是上次送到一半就被砍。

        它們與 UNKNOWN 同樣需要回查，不可以直接當失敗重寄。
        """
        cutoff = _now() - older_than_sec
        with self._lock:
            out = [dict(r) for r in self._records.values()
                   if r.get("state") == SUBMITTING
                   and (r.get("updated_at") or 0) < cutoff]
        return sorted(out, key=lambda r: r.get("created_at") or 0)

    def needs_recipient_retry(self) -> list:
        """(delivery_id, [該補寄的收件人]) —— 只含暫時性被拒者。"""
        with self._lock:
            out = []
            for did, rec in self._records.items():
                todo = recipients_needing_retry(rec.get("recipients") or {})
                if todo:
                    out.append((did, todo))
        return sorted(out)

    # ── 維護 ───────────────────────────────────────────────────────────────
    def _prune_locked(self) -> None:
        """剪掉太舊的【已收斂】紀錄。UNKNOWN / SUBMITTING 一律保留 ——
        還沒查清楚的東西不可以因為過期就被當成沒發生過。"""
        cutoff = _now() - self._retain_days * _DAY_SEC
        keep = {}
        for did, rec in self._records.items():
            if rec.get("state") in (UNKNOWN, SUBMITTING, PREPARED):
                keep[did] = rec
                continue
            if (rec.get("updated_at") or 0) >= cutoff:
                keep[did] = rec
        dropped = len(self._records) - len(keep)
        if dropped:
            logging.info("[delivery] 剪掉 %d 筆逾 %d 天的已收斂紀錄",
                         dropped, self._retain_days)
        self._records = keep
