# -*- coding: utf-8 -*-
"""UI 執行緒 → 主執行緒訊息協定。搬自原主程式 line 218-296。

取代 ('status', str) 等 tuple 協定，改用 frozen=True、slots=True dataclass，
讓型別檢查器抓得到欄位錯字，且 instance 不可被誤改。
"""
from dataclasses import dataclass
from datetime import date
from queue import Empty, Full, Queue
from typing import Any, Optional, TypeAlias, Union


@dataclass(frozen=True, slots=True)
class UiStatusMessage:
    text: str


@dataclass(frozen=True, slots=True)
class UiRefreshTickMessage:
    doctor_name: str


@dataclass(frozen=True, slots=True)
class UiClinicDataMessage:
    doctor_name: str
    data: Any
    # ★[2026-08-10 外審 SB #2] 這筆資料屬於哪一輪 refresh★
    #   殭屍 worker(被 age takeover 接管的那一輪)醒來後仍會把【舊的】
    #   掛號數丟進 ui_queue —— 沒有世代戳的話,舊資料會蓋掉新資料,
    #   連帶改變止掛提醒的判定。None = 非 refresh 來源(快照重播等),照收。
    refresh_gen: Any = None
    # [codex 2026-07-17] 這個訊息有多種來源:磁碟舊快取 fallback、漸進式部分結果(還沒併
    # 休診覆蓋)、快照重播、錯誤payload,以及【最後那筆完整成功的即時資料】。遠期止掛提醒
    # 只能用最後這種來判斷要不要寄信(拿舊/半套資料寄會寄錯,而且會把該診次永久標記已寄,
    # 害之後真的爆掉反而不提醒)。故用本旗標明確標示來源,預設 False(不解鎖提醒掃描)。
    is_live_final: bool = False


@dataclass(frozen=True, slots=True)
class UiMasterScheduleMessage:
    schedule: Any


@dataclass(frozen=True, slots=True)
class UiDutyDoctorMessage:
    doctor_name: str


@dataclass(frozen=True, slots=True)
class UiSaturdayDutyDoctorMessage:
    saturday_date: date
    doctor_name: str


@dataclass(frozen=True, slots=True)
class UiTodayVsMessage:
    doctor_name: str


@dataclass(frozen=True, slots=True)
class UiSaturdayVsMessage:
    doctor_name: str


@dataclass(frozen=True, slots=True)
class UiClockStatusMessage:
    """payload: 'querying' | dict（打卡結果或 {'error': ...}）。

    generation: 打卡查詢「世代序號」。worker 發布結果時帶自己那一輪的 gen；主緒消費端
    (唯一改 generation 者)比對後拒收過時世代 → 卡死舊 worker 晚到的結果不覆寫新一輪、
    也由消費端在 gen 相符時清 running 旗標（檢查與清旗標同在主緒＝原子,無跨緒競態）。
    None＝非 worker 結果(querying/停用/設定錯)→ 一律套用、不動旗標。"""
    status_data: Union[str, dict[str, Any]]
    generation: Optional[int] = None


@dataclass(frozen=True, slots=True)
class UiAlertInfoMessage:
    title: str
    msg: str
    need_restart: bool


@dataclass(frozen=True, slots=True)
class UiAlertErrorMessage:
    title: str
    msg: str


UiMessage: TypeAlias = Union[
    UiStatusMessage,
    UiRefreshTickMessage,
    UiClinicDataMessage,
    UiMasterScheduleMessage,
    UiDutyDoctorMessage,
    UiSaturdayDutyDoctorMessage,
    UiTodayVsMessage,
    UiSaturdayVsMessage,
    UiClockStatusMessage,
    UiAlertInfoMessage,
    UiAlertErrorMessage,
]


#: ★週期性、丟掉也會再來的訊息★(R3-P2-05)。佇列滿時優先犧牲它們。
#: 反過來說,不在這裡的就是★一次性、丟了就永遠不見★的:
#: 錯誤/提示對話框、打卡狀態 —— 那些是使用者唯一會知道「出事了」的管道。
_EXPENDABLE_UI_MESSAGES = (
    UiRefreshTickMessage,      # 定時 tick,下一輪就來
    UiStatusMessage,           # 狀態列文字,會被下一次覆蓋
    UiClinicDataMessage,       # 門診資料,每輪重抓
    UiMasterScheduleMessage,   # 班表快取,每輪重抓
    UiDutyDoctorMessage,
    UiSaturdayDutyDoctorMessage,
    UiTodayVsMessage,
    UiSaturdayVsMessage,
)


def is_expendable_ui_message(msg) -> bool:
    """這一筆訊息丟掉之後還會再來嗎?"""
    return isinstance(msg, _EXPENDABLE_UI_MESSAGES)


def _put_with_priority(ui_queue, msg) -> "bool | None":
    """★在同一個臨界區裡★做「還滿嗎 → 挑一筆丟 → 把新的放進去」。

    (外審 R3 剩餘批 P2)分成兩步的話 —— 在鎖裡驅逐、出鎖之後才 `put_nowait`
    —— ★中間會被別的 producer 補位★:我們剛騰出來的空位被搶走,接著自己的
    `put_nowait` 又拿到 Full 而被靜默放棄。正式程式有多個並行的 refresh
    worker 與背景 callback,這條路是真的會走到的。

    → True 放進去了 / False 佇列全是一次性訊息、沒有可丟的 /
      None 這不是標準 `Queue`(呼叫端退回舊路徑)。
    """
    try:
        mutex = ui_queue.mutex
        dq = ui_queue.queue
        maxsize = ui_queue.maxsize
    except AttributeError:
        return None
    try:
        with mutex:
            if maxsize <= 0 or len(dq) < maxsize:
                dq.append(msg)                 # 中間有人拿走了 → 直接放
            else:
                for i, item in enumerate(dq):
                    if is_expendable_ui_message(item):
                        del dq[i]              # 丟最舊的一筆可丟的
                        dq.append(msg)
                        break
                else:
                    # 全是一次性訊息。
                    if is_expendable_ui_message(msg):
                        return False           # 新來的下一輪還會來 → 讓路
                    # ★保底也要在同一個臨界區裡做★(外審 R2 P2):
                    #   拆成鎖外的 `get_nowait()` + `put_nowait()` 的話,
                    #   中間會被別的 producer 補位 —— 我們騰出來的空位被搶走,
                    #   自己的 put 又拿到 Full 而被靜默放棄,重要訊息照樣不見。
                    dq.popleft()
                    dq.append(msg)
            ui_queue.unfinished_tasks += 1 if len(dq) else 0
            ui_queue.not_empty.notify()
            return True
    except Exception:
        return None


def put_ui_message(ui_queue: "Queue[UiMessage]", msg: UiMessage) -> None:
    """[O15] 改 put_nowait:滿了就丟一筆,避免背景執行緒卡死。

    ★[R3-P2-05] 丟哪一筆要看臨床重要性,不是看誰最舊★:原本一律丟最舊的,
    於是佇列被一串定時 tick 塞滿時,★被犧牲的可能正是那一則錯誤通知★ ——
    而 tick 下一輪就會再來,錯誤通知丟了就永遠不見。

    順序(★全部在同一個 `Queue` 臨界區裡完成★,見 `_put_with_priority`):
    ① 先擠掉佇列裡★最舊的一筆可丟的★並把新的放進去 —— 週期性訊息越新越對
       (狀態列就是這樣:最新那一筆才是現況);
    ② 一筆可丟的都沒有(整個佇列都是一次性訊息)時才分兩種:
       新來的是週期性的 → ★放棄它★(下一輪還會來,不可以拿它換掉一則
       使用者只會看到一次的錯誤通知);新來的也是一次性的 → 保底丟最舊
       (寧可掉一筆也不要讓背景執行緒卡死)。
    """
    import logging
    try:
        ui_queue.put_nowait(msg)
        return
    except Full:
        pass
    except Exception:
        logging.debug("ui_queue put_nowait failed", exc_info=True)
        return

    placed = _put_with_priority(ui_queue, msg)
    if placed is True:
        return
    if placed is False:
        # 佇列全是一次性訊息,而新來的下一輪還會來 → 讓路。
        logging.debug("ui_queue 全是一次性訊息且已滿 → 略過週期性的 %s",
                      type(msg).__name__)
        return
    # placed is None → 不是標準 Queue,退回原本的「丟最舊」(兩步,
    #   但那條路上本來就沒有 Queue 的鎖可用)。
    try:
        ui_queue.get_nowait()
    except Empty:
        pass
    except Exception:
        logging.debug("ui_queue full and unable to drop oldest", exc_info=True)
        return
    try:
        ui_queue.put_nowait(msg)
    except Full:
        pass
    except Exception:
        logging.debug("ui_queue still full after drop, message dropped",
                      exc_info=True)
