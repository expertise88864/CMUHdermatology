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


def put_ui_message(ui_queue: "Queue[UiMessage]", msg: UiMessage) -> None:
    """[O15] 改 put_nowait：滿了就丟最舊一筆，避免背景執行緒卡死。"""
    import logging
    try:
        ui_queue.put_nowait(msg)
        return
    except Full:
        pass
    except Exception:
        logging.debug("ui_queue put_nowait failed", exc_info=True)
        return
    # Queue 已滿（極端情況）：丟掉最舊一筆讓新訊息進來
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
        logging.debug("ui_queue still full after drop, message dropped", exc_info=True)
