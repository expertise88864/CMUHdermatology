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
