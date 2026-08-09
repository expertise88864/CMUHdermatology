# -*- coding: utf-8 -*-
"""止掛提醒的兩件事：**什麼時候不跳彈窗**、**哪些信已經寄過**。
（P2-06 分層第五刀(a) 第二批 2026-08-02，從 `AutomationApp` 搬出）

【為什麼這兩支是一組】
它們都是「要不要打擾使用者」的判斷，而且都完全不碰 `self` —— 是被誤放進類別的
模組函式。搬出來之前，勿擾窗的跨午夜邏輯與「已寄記錄」的保留期過濾都只能靠
開得起 Tk 的整支 app 才驗得到。

【★搬的過程中發現的真正問題★】
`_load_alert_email_sent` 原本用 `load_json_dict`，那一支對「檔案不存在」與
「檔案還在、只是被防毒/備份鎖住」都回預設值 `{}`。開機那一刻若剛好被鎖住：

    啟動        self._alert_email_sent = {}          ← 其實磁碟上有一整批紀錄
    寄出一封    _mark_alert_email_sent() 把 {那一封} 原子性地寫回磁碟
    結果        ★先前所有「已寄過」的紀錄永久消失★

後果是止掛提醒重複寄送（同一診次的信會再寄一次）。這與 `save_all_settings`
在 2026-07-26 修掉的是同一個病灶 ——「讀檔失敗被當成沒有資料，然後被正常寫回
覆蓋」—— 只是換成這個檔，而當時沒有一起修到。

本模組改用 `load_json_dict_ex` 分辨這三種狀態，並在寫回前**先確認磁碟現況**：
讀得到就合併（自我修復），仍讀不到就不寫（保留磁碟上的紀錄）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from cmuh_common.alert_dedupe import filter_recent_alert_sent
from cmuh_common.config_io import load_json_dict_ex


# ── 勿擾窗 ────────────────────────────────────────────────────────────────
def is_within_dnd_window(now: datetime, start_hour: int, end_hour: int) -> bool:
    """現在是不是在「只寄 email、不跳彈窗」的勿擾窗內。

    ★跨午夜是常態不是例外★ 診間的勿擾窗是 00:00–08:00，但這支要能處理任何設定：
      start < end   例如 22→23：單純區間
      start > end   例如 22→6 ：跨午夜，條件要用 or 不是 and
      start == end  視為【整天都勿擾】—— 「零長度區間」與「整天」都說得通，
                    這裡沿用主程式原本的選擇（回 True），因為勿擾只抑制彈窗、
                    不影響寄信，寧可安靜。

    ★結束時刻不含在內★（`< end`）：08:00 整就該恢復跳窗。
    """
    start_m = int(start_hour) * 60
    end_m = int(end_hour) * 60
    now_m = now.hour * 60 + now.minute
    if start_m == end_m:
        return True
    if start_m < end_m:
        return start_m <= now_m < end_m
    return now_m >= start_m or now_m < end_m


# ── 已寄止掛信的紀錄 ──────────────────────────────────────────────────────
LOAD_OK = "ok"              # 讀到了（含檔案不存在＝第一次跑）
LOAD_UNREADABLE = "unreadable"   # ★檔案還在，只是這次讀不到★


@dataclass(frozen=True)
class AlertSentLoad:
    """★不要只回一個 dict★ 「沒有紀錄」與「讀不到紀錄」的後果完全不同。"""
    records: dict = field(default_factory=dict)
    status: str = LOAD_OK

    @property
    def unreadable(self) -> bool:
        return self.status == LOAD_UNREADABLE


def load_alert_email_sent(path: str, retain_days: int,
                          today: date | None = None) -> AlertSentLoad:
    """讀『已寄止掛信』紀錄 → {notify_key: 'YYYY-MM-DD'}，只留近 N 天。

    `status` 只在**原檔還在、這次讀不到**時是 UNREADABLE（沿用
    `safe_load_json_ex` 的 "error"）。檔案不存在或內容壞掉都算 OK ——
    那兩種情況磁碟上本來就沒有可用內容，用空紀錄接手是合理的。
    """
    data, status = load_json_dict_ex(path, {}, merge_defaults=False)
    if status == "error":
        # ★不要回 {} 就算了★ 呼叫端必須知道，否則它會拿空的去覆蓋磁碟。
        return AlertSentLoad({}, LOAD_UNREADABLE)
    cutoff = ((today or date.today())
              - timedelta(days=int(retain_days))).isoformat()
    return AlertSentLoad(filter_recent_alert_sent(data, cutoff), LOAD_OK)


SAVE_WRITE = "write"          # 可以寫（正常情況）
SAVE_MERGED = "merged"        # 開機時讀不到，但現在讀到了 → 合併後寫（自我修復）
SAVE_SKIP = "skip"            # 現在仍然讀不到 → ★不要寫★，保住磁碟上的紀錄


@dataclass(frozen=True)
class AlertSentSave:
    status: str
    payload: dict = field(default_factory=dict)

    @property
    def should_write(self) -> bool:
        return self.status in (SAVE_WRITE, SAVE_MERGED)

    def describe(self) -> str:
        if self.status == SAVE_WRITE:
            return f"寫入 {len(self.payload)} 筆"
        if self.status == SAVE_MERGED:
            return f"與磁碟上的紀錄合併後寫入 {len(self.payload)} 筆"
        return "磁碟上的紀錄仍讀不到 → 本次不寫（避免蓋掉既有紀錄）"


def records_for_save(path: str, records: dict, *, load_failed: bool,
                     retain_days: int,
                     today: date | None = None) -> AlertSentSave:
    """決定這次到底該把什麼寫回磁碟。

    ★`load_failed` 是本次執行【開機時】有沒有讀失敗★
    沒失敗 → 記憶體那份就是磁碟那份的後裔，直接寫。
    失敗過 → 記憶體那份是【空的開始】，直接寫等於把使用者原本的紀錄抹掉。
             這時先再讀一次：
               讀到了 → 合併（磁碟的舊紀錄 ＋ 本次新增），順便套保留期。
                        ★這是自我修復★ 防毒放手之後就自動接回來了，
                        不必等下次重啟。
               還是讀不到 → 不寫。代價是本次的「已寄」記錄沒有落地
                        （重啟後可能重寄一封），遠小於抹掉全部紀錄。
    """
    if not load_failed:
        return AlertSentSave(SAVE_WRITE, dict(records))
    on_disk, status = load_json_dict_ex(path, {}, merge_defaults=False)
    if status == "error":
        return AlertSentSave(SAVE_SKIP)
    merged = dict(on_disk)
    merged.update(records)
    cutoff = ((today or date.today())
              - timedelta(days=int(retain_days))).isoformat()
    return AlertSentSave(SAVE_MERGED, filter_recent_alert_sent(merged, cutoff))


# ── ★UNKNOWN 的暫時性抑制★（#71 批次 Z 下半，外審 P1-03）─────────────────
#
# 【問題】`_send_alert_email_via_smtp` 遇到 `DeliveryOutcomeUnknown` 時回 True，
# 呼叫端就寫下**永久**去重記號。批次 U 把 UNKNOWN 回查接上去之後，前提變了：
#
#     回查 → 寄件備份【查無】 → ledger.resolve_unknown(delivered=False)
#          → 帳本說「可以重寄」
#          但 alert_email_sent.json 說「已寄過」→ marker 贏
#          → 那一則止掛提醒【永遠不會再寄】
#
# 兩個真相來源互相矛盾，而錯的那個贏。
#
# 【為什麼不能只把 marker 拿掉】
# 回 False → 呼叫端釋放寄送權 → 下一輪掃描再寄 → 醫師每輪收到重複提醒。
# 那比現況更糟。需要的是**第三種狀態**：不重寄、但也不宣稱已送達。
#
# 【★最重要的一條：抑制一定要有出口★】
# IMAP 長期不可用時回查永遠拿不到答案。沒有出口的話，這個機制會從「防重複」
# 變成**永久靜默漏寄** —— 正是 2026-08-05 事故那個形狀。所以超過
# `PENDING_MAX_AGE_SEC` 就解除抑制並告警：
# **寧可讓人收到一則重複，也不要讓一則該寄的永遠不寄而且沒人知道。**

PENDING_FILENAME = "alert_email_pending.json"
#: 抑制的上限。超過就解除 + 告警（不是延長抑制）。
PENDING_MAX_AGE_SEC = 6 * 3600.0

#: `decide_pending()` 的回傳值（封閉集合 —— 呼叫端可以分流，不必比對字串長相）
PENDING_KEEP = "keep"        # 還在等回查結果 → 繼續抑制
PENDING_PROMOTE = "promote"  # 帳本說送達了 → 升級成永久記號，刪 pending
PENDING_RELEASE = "release"  # 帳本說沒送到 → 刪 pending，下輪可以重寄
PENDING_EXPIRE = "expire"    # 等太久 → ★告警 + 解除抑制★

#: 帳本狀態 → 決策。★三態不可以摺成兩態★：查不出來要回 KEEP，不是 RELEASE。
_DELIVERED_STATES = ("confirmed", "partial")
_NOT_DELIVERED_STATES = ("failed",)


def new_pending_entry(*, delivery_id: str = "", message_id: str = "",
                      business_key: str = "", now: float,
                      gen: str = "") -> dict:
    """建一筆抑制紀錄。`now` 一律由呼叫端給（好測、也避免隱藏時鐘）。

    ★`gen` 是【這一筆】的身分★（外審 2026-08-09 #1）
    掃描是「先取快照 → 做 I/O → 再改」。那段空窗裡，同一個 notify_key
    可能已經逾期、被重寄、又寫了一筆【新的】抑制。舊那一輪若照著
    notify_key 去刪，刪掉的是新的那一筆 —— 於是抑制消失、下一輪再寄一次，
    而且會一直循環。所以刪除／升級前要先確認「還是我看到的那一筆」。
    `delivery_id` 不夠用：帳本登記失敗時它是空字串。
    """
    import uuid
    return {"since": float(now),
            "gen": str(gen or uuid.uuid4().hex),
            "delivery_id": str(delivery_id or ""),
            "message_id": str(message_id or ""),
            "business_key": str(business_key or "")}


def same_pending_generation(a, b) -> bool:
    """兩筆抑制紀錄是不是【同一筆】。

    ★不可以回 True 當作預設★ 分不出來就當成不同 —— 那只會少刪一次
    （下一輪再處理），而錯刪會直接造成重複寄信。
    """
    ga = str((a or {}).get("gen") or "")
    gb = str((b or {}).get("gen") or "")
    if ga and gb:
        return ga == gb
    # 舊格式（沒有 gen）→ 退回用內容比對，仍然比「無條件刪」安全。
    if a is None or b is None:
        return False
    keys = ("since", "delivery_id", "message_id")
    return all((a or {}).get(k) == (b or {}).get(k) for k in keys)


#: 允許的時鐘偏差。超過這個量的「未來」時間戳一律視為壞值（見下）。
PENDING_CLOCK_SKEW_SEC = 300.0


def pending_age(entry, now: float) -> float:
    """這筆抑制掛了多久。看不懂的 `since` → 當成【很久】（會被逼到 EXPIRE）。

    ★方向★ 看不懂當成「剛剛才寫的」的話，一筆壞資料就會造成**永久抑制** ——
    而永久抑制正是這整段程式要避免的東西。看不懂就讓它逾期，逾期會告警。

    ★[外審 2026-08-09 #3] 未來的時間戳也是壞值★
    `since > now` 時年齡是負的，`is_suppressing()` 會判成「很新」——
    抑制被延長【整個時鐘偏移量】。寫進去之後系統時間被往回調（診間電腦
    對時、換電池、手動改），或 `since` 被寫成一個很大但有限的數字，
    就會安靜地抑制掉那則臨床提醒好幾年，而文件寫著上限是六小時。
    容忍 `PENDING_CLOCK_SKEW_SEC` 的正常偏差，超過就當壞值。
    """
    import math
    try:
        since = float((entry or {}).get("since") or 0)
    except (TypeError, ValueError):
        return float("inf")
    if not math.isfinite(since):
        return float("inf")
    age = float(now) - since
    if age < -PENDING_CLOCK_SKEW_SEC:
        return float("inf")
    return age


def is_suppressing(entry, now: float) -> bool:
    """這筆 pending 現在還算不算「已經寄過」。

    ★逾期的當下就不再抑制，不必等掃描跑★
    掃描要靠刷新輪次驅動；如果抑制的解除【依賴掃描發生】，那麼掃描本身
    出問題（執行緒沒起來、程式沒開）就等於永久抑制。
    出口不可以依賴另一個會壞的東西。
    """
    if not entry:
        return False
    return pending_age(entry, now) < PENDING_MAX_AGE_SEC


def decide_pending(entry, ledger_state, now: float) -> str:
    """一筆 pending 該怎麼處理。純函式。

    `ledger_state` 是帳本裡那筆的狀態字串；**查不到／讀不到一律傳空字串**
    —— 那是「不知道」，不是「沒送到」。
    """
    state = str(ledger_state or "").strip().lower()
    if state in _DELIVERED_STATES:
        return PENDING_PROMOTE
    if state in _NOT_DELIVERED_STATES:
        return PENDING_RELEASE
    # 還不知道 → 只剩「等多久」這個問題。
    if pending_age(entry, now) >= PENDING_MAX_AGE_SEC:
        return PENDING_EXPIRE
    return PENDING_KEEP


def suppressed_keys(records, now: float) -> set:
    """目前仍在抑制中的 notify_key（逾期的不算）。"""
    return {k for k, v in (records or {}).items() if is_suppressing(v, now)}


@dataclass(frozen=True)
class AlertPendingLoad:
    """★同樣不要只回 dict★ 「沒有抑制紀錄」與「讀不到」的後果不同。"""
    records: dict = field(default_factory=dict)
    status: str = LOAD_OK

    @property
    def unreadable(self) -> bool:
        return self.status == LOAD_UNREADABLE


def load_alert_pending(path: str) -> AlertPendingLoad:
    """讀『結果不明的暫時性抑制』紀錄 → {notify_key: entry}。

    ★這裡【不】套保留期★ 過期與否由 `is_suppressing()` 依 `since` 判斷；
    在載入時就丟掉舊的，等於讓「解除抑制」這件事悄悄發生而不告警 ——
    而告警正是逾期路徑最重要的產物（`PENDING_EXPIRE`）。
    """
    data, status = load_json_dict_ex(path, {}, merge_defaults=False)
    if status == "error":
        return AlertPendingLoad({}, LOAD_UNREADABLE)
    out = {k: v for k, v in (data or {}).items() if isinstance(v, dict)}
    return AlertPendingLoad(out, LOAD_OK)


def pending_for_save(path: str, records: dict, *, load_failed: bool):
    """決定這次該把什麼 pending 寫回磁碟。與 `records_for_save` 同一套立場。

    讀失敗過 → 記憶體那份是空的開始，直接寫等於抹掉磁碟上的抑制紀錄
    （後果：那幾則結果不明的提醒會被重寄）。先再讀一次，讀到就合併。
    """
    if not load_failed:
        return AlertSentSave(SAVE_WRITE, dict(records))
    on_disk, status = load_json_dict_ex(path, {}, merge_defaults=False)
    if status == "error":
        return AlertSentSave(SAVE_SKIP)
    merged = {k: v for k, v in (on_disk or {}).items() if isinstance(v, dict)}
    merged.update(records)
    return AlertSentSave(SAVE_MERGED, merged)
