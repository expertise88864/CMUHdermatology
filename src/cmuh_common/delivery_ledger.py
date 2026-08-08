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

# 寫回失敗的有界重試(外審第 10 輪 P2-06)。次數刻意小:防毒/鎖競爭是毫秒級的
# 事,重試三次還不成就是真的有問題,那時候留給 `flush()` 與下一次異動,
# 而不是在這裡卡住呼叫端(它正在寄臨床通知的路徑上)。
_SAVE_ATTEMPTS = 3
_SAVE_RETRY_SEC = 0.15


class LedgerUnavailable(RuntimeError):
    """這一刻讀不到帳本,無法回答「寄過了沒有」。

    ★存在的理由★ 唯一比「答錯」更糟的是「猜一個答案卻讓人以為是查到的」。
    擋下來會停掉臨床通知(2026-08-05 就是這樣停了一個下午),放行會重複寄 ——
    這個取捨必須由接上閘門的那個呼叫端明寫,不可以藏在資料層的 except 裡。
    """

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
        self._wire_lifecycle()

    # ── 持久化 ─────────────────────────────────────────────────────────────
    def _wire_lifecycle(self) -> None:
        """把「開機收斂」與「關機補寫」接到一定會跑的地方。

        ★[2026-08-08 外審第 10 輪第 2 回 P2-3/P2-4]★ 上一回加了 `flush()` 與
        `converge_stale_prepared()`,但整個 repo 只有測試在呼叫它們 ——
        「有 API」不等於「會發生」。註解寫著「程式結束時會再試一次」,
        那句話當時是假的(又一次宣稱與實作不符)。

        接在建構子裡而不是各個呼叫端:呼叫端有兩個(主程式、會診程式),
        掛在呼叫端的東西遲早會漏掉一個 —— 那正是這一輪 P1-01 的形狀。

        ★atexit 不夠★ 會診程式有兩條 `os._exit()` 路徑(self-watchdog 與
        托盤結束),`os._exit` 不跑 atexit。那兩處另外明呼叫 `flush()`;
        這裡的 atexit 負責一般結束。
        """
        try:
            import atexit  # noqa: PLC0415
            atexit.register(self._flush_quietly)
        except Exception:
            logging.debug("[delivery] 註冊結束補寫失敗", exc_info=True)
        try:
            self.converge_stale_prepared()
        except Exception:
            logging.debug("[delivery] 開機收斂陳舊 PREPARED 失敗", exc_info=True)

    def _flush_quietly(self) -> None:
        """結束時最後一次補寫。任何失敗都不可以影響關機。"""
        try:
            self.flush()
        except Exception:
            logging.debug("[delivery] 結束前補寫帳本失敗", exc_info=True)

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
        """把本 process 動過的紀錄寫回磁碟。失敗會【有界重試】。

        ★[2026-08-08 外審第 10 輪 P2-06] 一次暫時失敗不可以就這樣算了★
        「讀不到磁碟就不寫回」的決策本身是對的(不可以拿記憶體去蓋別人的
        紀錄),但上一版失敗就直接 return,終局狀態只留在 `self._dirty` 的
        記憶體裡。防毒掃到檔案、鎖被佔住這種一瞬間的事,如果之後剛好沒有
        下一次寄送,process 就這樣結束了 —— 磁碟上那一筆永遠停在
        SUBMITTING,而我們其實早就知道它 CONFIRMED 了。
        現在:有界重試 + `flush()`(程式結束時再試一次)。
        """
        for _attempt in range(_SAVE_ATTEMPTS):
            if self._save_once_locked():
                return
            time.sleep(_SAVE_RETRY_SEC)
        logging.warning("[delivery] 帳本寫回連續 %d 次失敗 → 這些變更仍在記憶體,"
                        "會在下一次異動或程式結束時再試", _SAVE_ATTEMPTS)

    def flush(self) -> None:
        """把還沒落地的變更再寫一次(程式結束前呼叫)。

        ★存在的理由★ `_save_locked` 失敗時 `_dirty` 不會被清掉,等著下一次
        異動順便帶下去。但「下一次異動」不保證會發生 —— 沒有這個出口,
        最後一筆的終局狀態就靠運氣。
        """
        # ★整段都要握著鎖★(外審第 10 輪第 3 回 P2-3)
        #   所有正常 mutator 都是在 `with self._lock:` 裡呼叫 `_save_locked()`,
        #   只有這裡例外。而 `flush()` 的呼叫時機正是【關機執行緒】,同一時間
        #   很可能還有 daemon 寄送緒在動這本帳:`_dirty` 會在迭代中被改變、
        #   或者剛加進來的標記被 `clear()` 一起清掉卻沒有落地。
        #   `self._lock` 是 RLock,巢狀進 `_save_locked` 沒有問題。
        with self._lock:
            if not self._dirty:
                return
            self._save_locked()

    def _save_once_locked(self) -> bool:
        """實際寫一次。True = 已落地;False = 這一次沒寫成(可重試)。"""
        if self._load_failed:
            logging.warning("[delivery] 本次執行曾讀不到帳本 → 不寫回"
                            "(避免用不完整的內容覆蓋磁碟)")
            return True          # 這是【policy 決定不寫】,重試也沒有意義
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
                                "(避免覆蓋掉別的程式的紀錄);稍後重試")
                return False
            for did in self._dirty:
                rec = self._records.get(did)
                if rec is not None:
                    merged[did] = rec
            self._records = merged
            self._prune_locked()
            try:
                atomic_write_json(self.path, self._records)
                self._dirty.clear()
                return True
            except Exception:
                logging.warning("[delivery] 帳本寫入失敗(記憶體仍有紀錄)",
                                exc_info=True)
                return False

    # ── 生命週期 ───────────────────────────────────────────────────────────
    def begin(self, *, business_key: str, category: str, recipients: list,
              subject: str = "", message_id: str = "",
              attachment_hash: str = "", parent_id: str = "") -> str:
        """登記一次即將寄出的信。回傳 delivery_id。

        ★必須在真正送出【之前】呼叫★ —— 這樣即使送出當下斷電，重啟後看到的是
        一筆 SUBMITTING，而不是「什麼都沒發生」。
        """
        did = new_delivery_id()
        with self._lock:
            self._records[did] = {
                "delivery_id": did,
                "business_key": str(business_key),
                # ★補寄與初次的關聯★(外審第 10 輪第 5 回)
                #   補寄是自己一筆(自己的 Message-ID,回查才問得出答案),
                #   但「這位收件人到底收到了沒有」的答案必須回寫到【初次】
                #   那一筆 —— 否則初次紀錄永遠掛著暫時被拒,一小時後會被
                #   當成漏收而告警,人工照著告警轉寄就變成重複的臨床通知。
                "parent_id": str(parent_id or ""),
                "category": str(category),
                "subject": str(subject)[:200],
                "message_id": str(message_id),
                "attachment_hash": str(attachment_hash),
                # ★不再有「已建檔但還沒送」這個【落地的】狀態★
                #   (外審第 10 輪第 3 回 P2-4)
                #   舊設計是 begin→PREPARED、mark_submitting→SUBMITTING。
                #   問題出在寫回是 fail-open 的:`mark_submitting` 只改到記憶體、
                #   磁碟寫不進去,而信【真的寄出去了】—— 磁碟上就留著一筆
                #   PREPARED。下一個 process 開機看到它,會推論「這封確定沒送出」
                #   而收斂成 FAILED。那個推論的前提(狀態轉移一定落得了地)
                #   並不成立,於是稽核紀錄被寫成假的;接成閘門後還會放行重寄。
                #   ★把不該存在的區別拿掉★:登記的當下就是 SUBMITTING
                #   ——「可能已經交出去了」。它只能靠 Message-ID 回查收斂,
                #   永遠不會被自動判死。這是安全的方向。
                "state": SUBMITTING,
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

    def _refresh_locked(self) -> bool:
        """在跨 process 鎖內重讀磁碟,把別的 process 的紀錄併進來。

        ★[2026-08-08 外審第 10 輪 P2-05]★ 主程式與會診程式各自持有一個
        長生命週期的 `DeliveryLedger`。上一版只有 `_save_locked()` 會重讀,
        所以「自己沒寫過東西」的那一方看到的永遠是啟動當下的快照 ——
        B 寄出的那一筆,A 問起來會說「沒有」。今天沒有生產查詢端所以無害;
        一接成閘門就是跨 process 重複寄送。

        回傳 False = 這一刻讀不到磁碟(呼叫端必須自己決定怎麼辦,不可以
        把「讀不到」當成「沒有」)。
        """
        # ★鎖序必須與寫入端一致★(外審第 10 輪第 2 回 P2-5)
        #   所有 mutator 都是「先 `self._lock`、再檔案鎖」(`settle()` 是在
        #   `with self._lock:` 裡面呼叫 `_save_locked()` 的)。上一版這裡反過來
        #   拿,兩個執行緒對撞就會互等到檔案鎖 fail-open 為止。
        #   而且中間放掉 `self._lock` 的話,還可能拿一份【比另一個執行緒剛寫進
        #   記憶體的那筆還舊】的磁碟快照,回頭把它蓋掉 —— 閘門就會漏看那一筆。
        #   整段都握著 `self._lock`,兩個問題一起消失。
        with self._lock:
            with self._interprocess_lock():
                disk, status = safe_load_json_ex(self.path, {},
                                                 backup_on_corrupt=False)
                if status not in ("ok", "missing"):
                    return False
                merged = {}
                if isinstance(disk, dict):
                    merged = {k: v for k, v in disk.items()
                              if isinstance(v, dict)}
                for did in self._dirty:          # 本 process 尚未落地的優先
                    rec = self._records.get(did)
                    if rec is not None:
                        merged[did] = rec
                self._records = merged
        return True

    def has_live_delivery(self, business_key: str) -> bool:
        """這個 business_key 還有沒有「未被否證」的寄送。

        True → 不要再寄（已送達、或結果不明還沒查清楚）。
        這正是取代「UNKNOWN 到底算成功還算失敗」那個二選一的地方。

        ★讀不到磁碟時【拋例外】,不回答★(外審第 10 輪 P2-05)
        回 True(保守擋住)看起來安全,但那正是 2026-08-05 實機事故的形狀:
        一個沒有出口的 fail-closed 會把臨床功能無聲停掉。回 False 則是
        把「不知道」講成「沒有」,會重複寄。兩個都不該由這一層偷偷決定 ——
        將來把它接成閘門的人必須自己寫下要怎麼辦。
        """
        if not self._refresh_locked():
            raise LedgerUnavailable(
                "這一刻讀不到寄送帳本 → 無法判斷是否已經寄過;"
                "呼叫端必須自己決定要擋還是要放(不可以把讀不到當成沒有)")
        with self._lock:
            return any(r.get("business_key") == business_key
                       and r.get("state") in LIVE_STATES
                       for r in self._records.values())

    def unresolved(self) -> list:
        """所有還是 UNKNOWN 的紀錄（給回查流程用），舊的排前面。

        ★[2026-08-08 外審 P2] 先從磁碟重讀★
        這本帳是【跨 process 共用】的（見本檔 `_save_locked` 的說明）：`main.py`
        那支程式也會把 UNKNOWN 寫進同一個檔。只讀自己記憶體裡的快照，別的
        process 建立的 UNKNOWN 就【永遠不會】被挑去回查 —— 它一直停在
        UNKNOWN，而 UNKNOWN 屬於 `LIVE_STATES`，於是那個 business_key 也
        永遠不會再寄。`has_live_delivery()` 早就會重讀，這裡漏了。

        ★讀不到就拋例外，不回答★（與 `has_live_delivery()` 同一個立場）
        回空清單等於說「沒有待回查的」—— 那是把「讀不到」講成一個確定的答案，
        而且是往「安靜地什麼都不做」的方向。
        """
        if not self._refresh_locked():
            raise LedgerUnavailable(
                "這一刻讀不到寄送帳本 → 列不出待回查的 UNKNOWN；"
                "空清單會被誤解成「沒有待回查的」")
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

    def confirm_recipients(self, delivery_id: str, addrs: list) -> list:
        """把這幾位收件人在【這一筆】上的狀態改成已送達。回傳真的改到的。

        ★用途★(外審第 10 輪第 5 回)補寄成功時,要回頭把【初次】那一筆的
        對應收件人結掉。不回寫的話,初次紀錄會永遠停在暫時被拒,
        `needs_recipient_retry()` 一直列出它,最後被誤判成「始終沒收到」。
        """
        # ★正規化方式必須與 `begin()` 一致★(外審第 10 輪第 6 回)
        #   帳上的 key 是 `strip().lower()`,而補寄拿到的位址是【設定檔/IMAP
        #   原樣】—— 設定的正規化只有 strip、沒有 lower。收件人只要有一個
        #   大寫字母,這裡就對不上:回寫不到、初次紀錄繼續掛著暫時被拒,
        #   一小時後被誤報成漏收,人工照著告警轉寄 = 重複的臨床通知。
        #   (這正是上一回才修掉的那條路,換成大小寫又走了一次。)
        want = {str(a).strip().lower() for a in (addrs or [])}
        with self._lock:
            rec = self._records.get(delivery_id)
            if not rec:
                return []
            states = rec.get("recipients") or {}
            done = sorted(a for a in states if a in want
                          and states[a] != R_CONFIRMED)
            for a in done:
                states[a] = R_CONFIRMED
            if done:
                rec["recipients"] = states
                rec["state"] = summarize(states)
                rec["updated_at"] = _now()
                self._dirty.add(delivery_id)
                self._save_locked()
            return done

    def abandon_recipient_retry(self, delivery_id: str, note: str = "") -> list:
        """放棄補寄:把仍是【暫時性被拒】的收件人改記成永久被拒。回傳那些人。

        ★存在的理由★(外審第 10 輪第 4 回 P1-1)
        補寄的排程佇列在記憶體裡,程式一重啟就忘光。但【帳本是落地的】——
        重啟之後 `needs_recipient_retry()` 仍然看得到「這幾位還沒收到」。
        所以真正的收尾不是「佇列消失就算了」,而是:要嘛真的補寄成功,
        要嘛在帳本上明確結案並告警。這個方法負責後者 —— 結案之後
        `needs_recipient_retry()` 不會再列它,告警也就不會每輪重複。
        """
        with self._lock:
            rec = self._records.get(delivery_id)
            if not rec:
                return []
            states = rec.get("recipients") or {}
            gone = sorted(a for a, st in states.items() if st == R_TRANSIENT)
            for a in gone:
                states[a] = R_PERMANENT
            if gone:
                rec["recipients"] = states
                rec["state"] = summarize(states)
                rec["note"] = (str(note) or "補寄已放棄")[:300]
                rec["updated_at"] = _now()
                self._dirty.add(delivery_id)
                self._save_locked()
            return gone

    def stale_prepared(self, older_than_sec: float = 900.0) -> list:
        """一直停在 PREPARED 的紀錄 —— 登記了、但從來沒有交給 SMTP。

        ★[2026-08-08 外審第 10 輪 P2-08]★ `prune` 明確保留 PREPARED,
        `has_live_delivery()` 把它算成 live,但 `unresolved()` 只列 UNKNOWN、
        `stuck_submitting()` 只列 SUBMITTING —— 沒有任何 API 看得到它。
        接成閘門之後,它會永久擋住一封【確定從未開始寄送】的信。
        """
        cutoff = _now() - older_than_sec
        with self._lock:
            out = [dict(r) for r in self._records.values()
                   if r.get("state") == PREPARED
                   and (r.get("updated_at") or 0) < cutoff]
        return sorted(out, key=lambda r: r.get("created_at") or 0)

    def converge_stale_prepared(self, older_than_sec: float = 900.0) -> int:
        """把舊格式留下的陳舊 PREPARED 收斂成 UNKNOWN。回傳收斂了幾筆。

        ★這裡刻意【不是】FAILED★(外審第 10 輪第 3 回 P2-4)
        上一版把它判成「確定沒寄出」,依據是「begin 之後立刻 mark_submitting,
        所以停在 PREPARED 就代表送出前就死了」。那個推論漏掉一件事:
        **狀態轉移的寫回本身是 fail-open 的**。`mark_submitting()` 改了記憶體
        但磁碟寫不進去、信卻真的寄出去了 —— 磁碟上留下的一樣是 PREPARED。
        把它判成 FAILED 就是把一封【可能已送達】的信寫成沒送出:稽核造假,
        接成閘門之後還會放行重寄。
        「讀不到 / 沒寫成」不可以被當成某個確定的答案 —— 這是這個專案一路
        在修的同一個病灶。所以收斂到 UNKNOWN:它一樣會被 `unresolved()` 列出來,
        走既有的 Message-ID 回查路徑,而沒有任何一句話是編出來的。

        新版的 `begin()` 直接落地成 SUBMITTING,所以不會再產生 PREPARED;
        這個方法留給舊檔案裡既有的紀錄。
        """
        cutoff = _now() - older_than_sec
        n = 0
        with self._lock:
            for did, rec in self._records.items():
                if (rec.get("state") == PREPARED
                        and (rec.get("updated_at") or 0) < cutoff):
                    rec["state"] = UNKNOWN
                    rec["updated_at"] = _now()
                    rec["note"] = ("舊格式的陳舊 PREPARED:無法確定是否寄出"
                                   "(狀態轉移的寫回是 fail-open) → 待 Message-ID 回查")
                    self._dirty.add(did)
                    n += 1
        if n:
            logging.warning("[delivery] 收斂 %d 筆舊格式陳舊 PREPARED → UNKNOWN"
                            "(待回查,不可當成沒寄出)", n)
            self._save_locked()
        return n

    def needs_recipient_retry(self) -> list:
        """(delivery_id, [該補寄的收件人]) —— 只含暫時性被拒者。

        ★補寄產生的紀錄不列★(外審第 10 輪第 5 回)
        補寄是自己一筆(有 `parent_id`),但同一位收件人的「還沒收到」在
        【初次】那一筆上已經記著了。兩邊都列的話,同一位收件人會被重複
        結案、重複告警,而帳上的待辦數也會隨補寄次數膨脹。
        初次那一筆才是這位收件人的權威狀態;補寄紀錄留作嘗試的軌跡。
        """
        with self._lock:
            out = []
            for did, rec in self._records.items():
                if rec.get("parent_id"):
                    continue
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
