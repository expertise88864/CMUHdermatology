# -*- coding: utf-8 -*-
"""reg64（門診動態）時段工具 — main.py / scheduler.py 共用。

【重構 2026-05-21】兩支入口各有一份 byte-identical 實作，抽出避免不同步。
全部純函式，只依賴 stdlib datetime。

API:
  - reg64_time_code_from_local_clock(when=None) -> "1"/"2"/"3"
  - reg64_slot_cn(time_code) -> "早上" / "下午" / "晚上"
  - reg64_slot_label_color(time_code) -> "#XXXXXX"
  - resolve_clinic_reg64_time_code(mode, when=None) -> "1"/"2"/"3"
  - _reg64_tc_to_session_cn(time_code) -> "上午" / "下午" / "晚上"（與 reg64_slot_cn 略異）
"""
from __future__ import annotations

from datetime import datetime, time as dt_time
from typing import Optional


def reg64_time_code_from_local_clock(when: Optional[datetime] = None) -> str:
    """依本機時鐘：00:00–12:59→1(早上)，13:00–17:29→2(下午)，17:30–23:59→3(晚上)。

    [2026-06-19 使用者] 切換點改為 13:00 轉下午、17:30 轉晚上(原本 13:30 / 18:00)。
    """
    if when is None:
        when = datetime.now()
    cur = when.time()
    if cur <= dt_time(12, 59, 59):
        return "1"
    if cur <= dt_time(17, 29, 59):
        return "2"
    return "3"


def reg64_slot_cn(time_code) -> str:
    """TimeCode → 早上／下午／晚上（與門診統計 session 用語一致）。"""
    return {"1": "早上", "2": "下午", "3": "晚上"}.get(str(time_code), "")


def reg64_slot_label_color(time_code) -> str:
    """診間代號後方時段字色：早綠、午藍、晚深藍。"""
    return {"1": "#2E7D32", "2": "#1565C0", "3": "#0D47A1"}.get(str(time_code), "#78909C")


# 門診動態「顯示時段」選項（UI 僅中文，不顯示 Code 數字）
CLINIC_DISPLAY_MODE_OPTIONS = (
    ("auto", "自動（依電腦時間）"),
    ("1", "早上"),
    ("2", "下午"),
    ("3", "晚上"),
)


def _normalize_clinic_display_mode(val) -> str:
    v = str(val).strip().lower() if val is not None else "auto"
    if v in ("1", "2", "3", "auto"):
        return v
    return "auto"


def _clinic_display_mode_label(mode_key: str) -> str:
    for k, lab in CLINIC_DISPLAY_MODE_OPTIONS:
        if k == mode_key:
            return lab
    return CLINIC_DISPLAY_MODE_OPTIONS[0][1]


def _clinic_display_mode_from_label(label: str) -> str:
    for k, lab in CLINIC_DISPLAY_MODE_OPTIONS:
        if lab == label:
            return k
    return "auto"


def resolve_clinic_reg64_time_code(mode: str,
                                    when: Optional[datetime] = None) -> str:
    """自動 → 依本機時鐘；早上/下午/晚上 → 固定對應 reg64 TimeCode。"""
    m = _normalize_clinic_display_mode(mode)
    if m in ("1", "2", "3"):
        return m
    return reg64_time_code_from_local_clock(when)


def overrun_effective_time_code(tc, earlier_sessions) -> str:
    """早診/午診拖班的「有效輪詢時段」判定。純函式。

    [2026-06-19 使用者] 早診可能拖到下午(甚至晚上)才看完。時段雖依時鐘前進,但若有「更早的時段」
    今天看過診且尚未關診,就回傳【最早】那個仍在拖班的時段 → 繼續輪那一節,直到它真的關診
    (已關診 / 燈號·完成·待診 30 分鐘沒變)才前進。同一診間同時只有一節在看診,故不增加輪詢負載。

    tc:依本機時鐘算出的目前時段("1"/"2"/"3")。
    earlier_sessions:比 tc 早的各時段狀態,【由最早到最晚】排序的可疊代物,
                     每個元素為 (session_tc:int, had_activity:bool, closed:bool)。
    """
    try:
        tc_i = int(tc)
    except (TypeError, ValueError):
        return str(tc)
    for s_tc, had_activity, closed in earlier_sessions:
        if had_activity and not closed:
            return str(s_tc)   # 最早仍在拖班的時段(同診間單節 → 只會有一個)
    return str(tc_i)


def _reg64_tc_to_session_cn(time_code) -> str:
    """TimeCode → 上午／下午／晚上（注意：與 reg64_slot_cn 的「早上」略異，
    這個用「上午」是因為門診 metadata 內部就用這套字串）。"""
    return {"1": "上午", "2": "下午", "3": "晚上"}.get(str(time_code), "")


def canonical_clinic_session_str(value) -> str:
    """Normalize clinic session labels to 早上|下午|晚上."""
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    aliases = {
        "上午": "早上",
        "早診": "早上",
        "早": "早上",
        "早上": "早上",
        "下午": "下午",
        "午診": "下午",
        "午": "下午",
        "晚上": "晚上",
        "晚診": "晚上",
        "晚": "晚上",
    }
    return aliases.get(text, text)


def session_boundary_datetime(session_cn: str, now_dt: datetime) -> datetime:
    """Earliest same-day time when close detection should start for a session."""
    session = canonical_clinic_session_str(session_cn)
    if session == "早上":
        hour, minute = 12, 0
    elif session == "下午":
        hour, minute = 17, 0
    else:
        hour, minute = 21, 0
    return now_dt.replace(hour=hour, minute=minute, second=0, microsecond=0)


def prev_session_cn(session_cn: str) -> str | None:
    """Return the previous clinic session label using canonical terms."""
    session = canonical_clinic_session_str(session_cn)
    if session == "下午":
        return "早上"
    if session == "晚上":
        return "下午"
    return None


def clinic_int_count(value, default: int = 0) -> int:
    """Coerce reg64 numeric counts while rejecting blanks, booleans and fractions."""
    if value is None or value == "" or value == "-":
        return default
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == int(value) else default
    try:
        text = str(value).strip()
        if text in ("-", "--", ""):
            return default
        return int(text)
    except (TypeError, ValueError):
        return default


def reg64_clinic_quiet_hours(when: Optional[datetime] = None) -> bool:
    """半夜 00:00–07:00 暫停 reg64 輪詢(07:00 起恢復)。[2026-06-22] 由 <8 改 <7。"""
    current = when or datetime.now()
    return current.hour < 7


def reg64_next_allowed_fetch_time(when: Optional[datetime] = None) -> datetime:
    """Return the next 07:00 boundary, or when if polling is already allowed."""
    current = when or datetime.now()
    start = current.replace(hour=7, minute=0, second=0, microsecond=0)
    if current < start:
        return start
    return current


def clinic_tight_poll_window(when: Optional[datetime] = None) -> bool:
    """[2026-06-22 使用者] 是否處於「需要每分鐘輪詢」的早上門診起跑窗(08:20–12:00)。純函式。

    門診多半 08:30 準時開診、燈號開始跳號;此窗內輪詢間隔 45–75 秒隨機,確保即時抓到開診/跳號。
    窗外維持 60–90 秒隨機(避免固定節拍打爆院方限制)。"""
    t = (when or datetime.now()).time()
    return dt_time(8, 20) <= t < dt_time(12, 0)


def is_residual_stale_closed(is_closed: bool, is_stopped_dayoff: bool,
                             had_any_activity: bool, before_boundary: bool) -> bool:
    """早晨「殘留盤面」判定。純函式。

    [2026-06-22] reg64 盤面在「今天該時段的診次開診前」可能還停留在上一個看診日同時段(已關診)。
    若盤面說已關診,但【今天還沒看到任何活動 had_any_activity=False】且【還沒到該時段正常關診時間
    before_boundary=True(如早診 12:00)】→ 八成是殘留盤面(今天的診還沒開),不應視為今天已關診。
    真正排休(is_stopped_dayoff)不在此列(那是真的沒診,與時間無關)。"""
    return (bool(is_closed) and not bool(is_stopped_dayoff)
            and not bool(had_any_activity) and bool(before_boundary))
