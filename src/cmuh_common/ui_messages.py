# -*- coding: utf-8 -*-
"""UI 執行緒 → 主執行緒訊息協定。搬自原主程式 line 218-296。

取代 ('status', str) 等 tuple 協定，改用 frozen=True、slots=True dataclass，
讓型別檢查器抓得到欄位錯字，且 instance 不可被誤改。
"""
from dataclasses import dataclass
from datetime import date
from queue import Empty, Full, Queue
from typing import Any, TypeAlias, Union


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
    """payload: 'querying' | dict（打卡結果或 {'error': ...}）"""
    status_data: Union[str, dict[str, Any]]


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
