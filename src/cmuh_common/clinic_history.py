# -*- coding: utf-8 -*-
"""Clinic statistics-history helpers."""
from __future__ import annotations

import statistics
from datetime import datetime, date
from typing import Any, Callable


def duration_stats(durations: Any) -> tuple[list[float], list[float], float | None]:
    """Return (all durations, valid durations, avg minutes) after outlier trim."""
    dur_list = [float(x) for x in (durations or [])]
    if not dur_list:
        return [], [], None
    # [CL-02 audit 2026-07-12] 以中位數為中心裁剪離群 —— 原本用「含離群的平均」定帶,單一
    # 卡住(超長)的計時會把帶整個拉高、離群反而落在帶內;全被裁掉時回退中位數(非放回全體含離群)。
    med = statistics.median(dur_list)
    valid_data = [x for x in dur_list if (med * 0.5) <= x <= (med * 2.0)]
    if not valid_data:
        valid_data = [med]
    final_avg_sec = sum(valid_data) / len(valid_data)
    return dur_list, valid_data, round(final_avg_sec / 60, 1)


def upsert_session_stat(
    history: list,
    *,
    today_str: str,
    week_str: str,
    room_code: Any,
    doc_name: str,
    completed_count: int,
    durations: Any,
    session: str,
    closing_time: str = "",
    total_reg: Any = None,
    phototherapy: int = 0,
    canonical_session: Callable[[Any], str] = str,
    match_room: bool = True,
    allow_empty_sample: bool = True,
) -> tuple[list, bool]:
    """Insert or update one clinic session stat row.

    Returns (history, changed). Empty duration samples are ignored unless a
    closing time or total registration count is present and allow_empty_sample
    is true.
    """
    if not doc_name:
        return history, False
    dur_list, valid_data, final_avg_min = duration_stats(durations)
    has_dur = bool(dur_list)
    # ★[2026-07-27 外審 P2] 完成人數是「頁面實測值」,與有沒有看診時長樣本無關★
    #   原本 completed_count 只在 `if has_dur:` 內寫入 → 程式在診次進行到一半才開啟
    #   (沒觀察到任何完成轉換、durations 是空的)時,實測到的 15 人會被存成 0。
    #   於是磁碟上「completed_count=0 且無樣本」同時代表「真的 0 人」與「沒記到」,
    #   下游怎麼猜都是錯的。改成一律保存實測值,並標記 completion_observed,
    #   讓下游不必推測。
    try:
        observed_completed = max(0, int(completed_count))
    except (TypeError, ValueError):
        observed_completed = None
    closing = (closing_time or "").strip()
    if not has_dur and (not allow_empty_sample or (not closing and total_reg is None)):
        return history, False

    out = [dict(row) for row in history if isinstance(row, dict)]
    session_key = canonical_session(session)
    record_found = False
    for record in out:
        if record.get("date") != today_str or record.get("doctor") != doc_name:
            continue
        if match_room and str(record.get("room", "")) != str(room_code):
            continue
        if canonical_session(record.get("session")) != session_key:
            continue

        record["room"] = room_code
        record["session"] = session_key
        if has_dur:
            record["completed_count"] = completed_count
            record["avg_time_min"] = final_avg_min
            record["raw_sample_count"] = len(dur_list)
            record["valid_sample_count"] = len(valid_data)
        elif observed_completed is not None:
            # 沒有新的時長樣本,但完成人數仍是實測值 → 保存。只在往上時更新:
            # 同一診次內完成數單調不減,這樣單次讀取異常(頁面暫時解析成 0)
            # 不會把已記錄的 24 蓋掉。
            try:
                prev_completed = int(record.get("completed_count", 0) or 0)
            except (TypeError, ValueError):
                prev_completed = 0
            if observed_completed > prev_completed:
                record["completed_count"] = observed_completed
        if observed_completed is not None:
            record["completion_observed"] = True
        if closing:
            record["closing_time"] = closing
        if total_reg is not None:
            record["total_reg"] = total_reg
        record["phototherapy"] = phototherapy
        record_found = True
        break

    if not record_found:
        out.append({
            "date": today_str,
            "week": week_str,
            "room": room_code,
            "session": session_key,
            "doctor": doc_name,
            "completed_count": (completed_count if has_dur
                                else (observed_completed or 0)),
            "completion_observed": observed_completed is not None,
            "avg_time_min": final_avg_min if has_dur else 0.0,
            "raw_sample_count": len(dur_list) if has_dur else 0,
            "valid_sample_count": len(valid_data) if has_dur else 0,
            "closing_time": closing,
            "total_reg": total_reg if total_reg is not None else None,
            "phototherapy": phototherapy,
        })
    return out, True


def remove_doctor_history(history: list, doc_name: str) -> list:
    """Return history rows excluding one doctor and malformed rows."""
    return [
        row for row in history
        if isinstance(row, dict) and row.get("doctor") != doc_name
    ]


def last_closing_time(history: list, doc_name: str, weekday_int: int,
                      session_str: str,
                      canonical_session: Callable[[Any], str] = str) -> str | None:
    if not doc_name:
        return None
    session_key = canonical_session(session_str)
    matches = []
    for row in history:
        if not isinstance(row, dict):
            continue
        if row.get("doctor") != doc_name:
            continue
        if canonical_session(row.get("session")) != session_key:
            continue
        if not row.get("closing_time"):
            continue
        try:
            row_date = datetime.strptime(row["date"], "%Y/%m/%d")
        except Exception:
            continue
        if row_date.weekday() == weekday_int:
            matches.append(row)
    if not matches:
        return None
    matches.sort(key=lambda x: x["date"], reverse=True)
    return matches[0].get("closing_time")


def prev_session_closing_clock(history: list, room_code: Any, doc_name: str,
                               prev_session: str, today_str: str,
                               canonical_session: Callable[[Any], str] = str) -> str:
    if not prev_session or not doc_name or not room_code:
        return "—"
    prev_key = canonical_session(prev_session)
    best = ""
    for row in history:
        if not isinstance(row, dict):
            continue
        if row.get("doctor") != doc_name:
            continue
        if str(row.get("room", "")) != str(room_code):
            continue
        if canonical_session(row.get("session")) != prev_key:
            continue
        if row.get("date") != today_str:
            continue
        closing = row.get("closing_time") or ""
        if closing:
            best = closing
    return best if best else "—"


def monthly_slot_metric_avgs(history: list, doc_name: str, room_code: Any,
                             session_cn: str, cutoff: date,
                             canonical_session: Callable[[Any], str] = str
                             ) -> tuple[str, str, str]:
    if not doc_name or not room_code:
        return ("-", "-", "-")
    session_key = canonical_session(session_cn)
    totals, comps, photos = [], [], []
    for row in history:
        if not isinstance(row, dict):
            continue
        if row.get("doctor") != doc_name:
            continue
        if str(row.get("room", "")) != str(room_code):
            continue
        if canonical_session(row.get("session")) != session_key:
            continue
        try:
            row_date = datetime.strptime(row["date"], "%Y/%m/%d").date()
        except Exception:
            continue
        if row_date < cutoff:
            continue
        total_reg = row.get("total_reg")
        if total_reg is not None and total_reg != "":
            try:
                totals.append(float(total_reg))
            except (TypeError, ValueError):
                pass
        # ★[2026-07-27 外審 P2] 只排除【舊格式、無從得知】的列,不做推測★
        #   upsert_session_stat 現在一律保存頁面實測的完成人數並標記
        #   completion_observed,所以有標記的列(含真的 0 人)都是實測值,照算。
        #   2026-07-27 之前寫下的列沒有這個標記,其中「completed_count=0 且完全
        #   沒有樣本」的形狀同時代表「真的 0 人」與「當時被 has_dur 閘門丟掉的
        #   實測值」—— 兩者在磁碟上無法區分,只能排除,否則會系統性把完成平均
        #   拉低(畫面出現「掛號 30 / 完成 12」這種對不起來的數)。
        #   這批舊列會隨 30 天視窗自然淘汰。
        try:
            _comp = float(row.get("completed_count", 0))
        except (TypeError, ValueError):
            pass
        else:
            if row.get("completion_observed") is True:
                comps.append(_comp)
            else:
                _samples = 0
                for _k in ("valid_sample_count", "raw_sample_count"):
                    try:
                        _samples = max(_samples, int(row.get(_k, 0) or 0))
                    except (TypeError, ValueError):
                        pass
                if not (_comp == 0 and _samples == 0):
                    comps.append(_comp)
        photo = row.get("phototherapy")
        if photo is not None and photo != "":
            try:
                photos.append(float(photo))
            except (TypeError, ValueError):
                pass

    def fmt(values: list[float]) -> str:
        if not values:
            return "-"
        return str(int(round(sum(values) / len(values))))

    return (fmt(totals), fmt(comps), fmt(photos))


def historical_duration_totals(history: list, doc_name: str,
                               cutoff: date) -> tuple[float, int]:
    hist_min = 0.0
    hist_count = 0
    for record in history:
        if not isinstance(record, dict) or record.get("doctor") != doc_name:
            continue
        try:
            row_date = datetime.strptime(record.get("date", ""), "%Y/%m/%d").date()
        except Exception:
            continue
        if row_date < cutoff:
            continue
        avg_min = record.get("avg_time_min", 0)
        count = record.get("valid_sample_count", 0)
        try:
            count = int(count)
            avg_min = float(avg_min)
        except (TypeError, ValueError):
            continue
        if count > 0:
            hist_min += avg_min * count
            hist_count += count
    return hist_min, hist_count


def all_time_average_text(history_totals: tuple[float, int],
                          current_durations: Any = None) -> str:
    total_minutes, total_count = history_totals
    # [CL-07 audit 2026-07-12] 先 coerce 再比較 —— 原本 `if x > 0` 作用於未轉型的 x,
    # 混型(字串)輸入會 TypeError 而非優雅回 "-"。
    valid_current = []
    for _x in (current_durations or []):
        try:
            _v = float(_x)
        except (TypeError, ValueError):
            continue
        if _v > 0:
            valid_current.append(_v)
    if valid_current:
        total_minutes += sum(valid_current) / 60.0
        total_count += len(valid_current)
    if total_count > 0:
        return f"{(total_minutes / total_count):.1f}"
    return "-"
