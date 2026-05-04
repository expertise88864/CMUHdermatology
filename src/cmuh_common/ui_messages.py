# -*- coding: utf-8 -*-
"""UI 執行緒 → 主執行緒訊息協定。搬自原主程式 line 218-296。

取代 ('status', str) 等 tuple 協定，改用 frozen=True、slots=True dataclass，
讓型別檢查器抓得到欄位錯字，且 instance 不可被誤改。
"""
from dataclasses import dataclass
from datetime import date
from queue import Queue
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
    ui_queue.put(msg)
