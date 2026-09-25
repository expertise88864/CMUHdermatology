"""Factual course accounting shared by roster preview, sidebar and exports.

All assignment counts come from the displayed slots, including manual edits and
locks. A missed target is a fact; its cause and global optimality are not inferred.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import date, timedelta
from fractions import Fraction
from typing import Any

from .solve_day import BIOPSY, STUDENT_SESSIONS, DaySolveInput, is_follow_slot
from .training_bands import available_slots, band, week_slots

DAY_EXPLANATION_HEADING = "【實際班表統計與公平性】"


@dataclass(frozen=True)
class PGYCourseRow:
    code: str
    photo: int
    wednesday_photo: int
    treatment: int
    follow: int
    necessary: int
    total: int
    manual_reduction: int
    fair_target: Fraction
    difference: Fraction
    rest: int
    weekly_follows: tuple[tuple[str, int], ...]
    known_doctors: tuple[tuple[str, int], ...]
    unknown_doctor_follows: int


@dataclass(frozen=True)
class TrainingCourseRow:
    scope: str
    course: str
    code: str
    complete: bool
    available_half_days: int
    minimum: int
    target: int
    maximum: int
    follow: int
    biopsy: int
    weekly_follows: tuple[tuple[str, int], ...]
    known_doctors: tuple[tuple[str, int], ...]
    unknown_doctor_follows: int
    rest: int
    biopsy_by_half: tuple[int, int]


@dataclass(frozen=True)
class DayCourseExplanation:
    pgy: tuple[PGYCourseRow, ...]
    training: tuple[TrainingCourseRow, ...]
    confirmed_gaps: tuple[str, ...]


def _nonnegative_targets(totals: dict[str, int],
                         offsets: dict[str, int]) -> dict[str, Fraction]:
    """Distribute actual work with manual reductions, never showing negative work."""
    targets = {p: Fraction(0) for p in totals}
    remaining = set(totals)
    work = sum(totals.values())
    while remaining:
        base = Fraction(work - sum(offsets.get(p, 0) for p in remaining),
                        len(remaining))
        negative = {p for p in remaining if base + offsets.get(p, 0) < 0}
        if not negative:
            for p in remaining:
                targets[p] = base + offsets.get(p, 0)
            break
        remaining -= negative
    return targets


def _follow_details(slots: dict[str, Any], code: str, *, start: date, end: date,
                    doctors: dict) -> tuple[tuple[tuple[str, int], ...],
                                           tuple[tuple[str, int], ...], int]:
    weeks: Counter = Counter()
    seen_doctors: Counter = Counter()
    unknown = 0
    for iso, sessions in (slots or {}).items():
        try:
            d = date.fromisoformat(iso)
        except (ValueError, TypeError):
            continue
        if not start <= d <= end or not isinstance(sessions, dict):
            continue
        for session, cells in sessions.items():
            if session not in STUDENT_SESSIONS or not isinstance(cells, dict):
                continue
            for room, people in cells.items():
                if not is_follow_slot(room) or code not in (people or []):
                    continue
                year, week, _ = d.isocalendar()
                weeks[f"{year}-W{week:02d}"] += 1
                doctor = doctors.get(d, {}).get(session, {}).get(room, "")
                if doctor:
                    seen_doctors[doctor] += 1
                else:
                    unknown += 1
    return tuple(sorted(weeks.items())), tuple(sorted(seen_doctors.items())), unknown


def explain_day_courses(inp: DaySolveInput, data: dict[str, Any],
                        current_slots: dict[str, Any]) -> DayCourseExplanation:
    """Derive targets and actuals without changing or claiming solver optimality."""
    y, m = map(int, inp.ym.split("-"))
    first = date(y, m, 1)
    last = date(y + (m == 12), 1 if m == 12 else m + 1, 1) - timedelta(days=1)
    pgy_stats = data["pgy"]["stats"]
    people = sorted(set(data["pgy"]["roster"]))
    offsets = inp.pgy_photo_offsets
    totals = {p: (pgy_stats.get(p, {}).get("photo", 0)
                  + pgy_stats.get(p, {}).get("tx", 0)
                  + pgy_stats.get(p, {}).get("follow", 0)) for p in people}
    targets = _nonnegative_targets(totals, offsets)
    pgy_rows = []
    gaps = []
    for p in people:
        st = pgy_stats.get(p, {})
        photo, wed, tx, follow = (st.get(k, 0) for k in
                                  ("photo", "photo_wed_pm", "tx", "follow"))
        target = targets[p]
        weeks, doctors, unknown = _follow_details(
            current_slots, p, start=first, end=last, doctors=inp.clinic_doctors)
        row = PGYCourseRow(p, photo, wed, tx, follow, photo + tx,
                           totals[p], offsets.get(p, 0), target,
                           Fraction(totals[p]) - target, st.get("rest", 0),
                           weeks, doctors, unknown)
        pgy_rows.append(row)
        if abs(row.difference) > 1:
            difference = f"+{row.difference}" if row.difference > 0 else str(row.difference)
            gaps.append(f"PGY {p} 實際總量 {row.total}；相對公平目標差 {difference}")
        weekly_counts = dict(weeks)
        for week, slots in week_slots(available_slots(inp, "pgy", p)).items():
            label = f"{week[0]}-W{week[1]:02d}"
            if slots and weekly_counts.get(label, 0) < 1:
                gaps.append(f"PGY {p} {label} 跟診 0/1，低於每週最低需求")

    training = []

    def add(scope, course, roster, stats, slots, start, end, available,
            *, complete=True):
        for p in sorted(set(roster)):
            st = stats.get(p, {})
            low, target, high = ((9, 10, 11) if scope == "Clerk"
                                 else band(len(available[p])))
            weeks, doctors, unknown = _follow_details(
                slots, p, start=start, end=end, doctors=inp.clinic_doctors)
            biopsy_by_half = [0, 0]
            for iso, sessions in (slots or {}).items():
                try:
                    d = date.fromisoformat(iso)
                except (TypeError, ValueError):
                    continue
                if not start <= d <= end or not isinstance(sessions, dict):
                    continue
                for session, cells in sessions.items():
                    if session in STUDENT_SESSIONS and isinstance(cells, dict):
                        biopsy_by_half[d.day > 14] += p in (cells.get(BIOPSY) or [])
            row = TrainingCourseRow(
                scope, course, p, complete, len(available[p]), low, target, high,
                st.get("follow", 0), st.get("biopsy", 0), weeks, doctors, unknown,
                st.get("rest", 0), (biopsy_by_half[0], biopsy_by_half[1]))
            training.append(row)
            if row.follow < low and complete:
                gaps.append(f"{scope} {p} 跟診 {row.follow}/{low}，低於最低需求")
            if row.follow > high:
                gaps.append(f"{scope} {p} 跟診 {row.follow}/{high}，超過上限")
            if scope == "Clerk" and row.biopsy < 1 and complete:
                gaps.append(f"Clerk {p} 切片 {row.biopsy}/1，低於最低需求")
            if scope == "Clerk" and row.biopsy > 2:
                gaps.append(f"Clerk {p} 切片 {row.biopsy}/2，超過上限")
            if scope != "Clerk":
                for half in (0, 1):
                    if (any((d.day > 14) == bool(half) for d, _ in available[p])
                            and row.biopsy_by_half[half] < 1):
                        label = "1–14 日" if half == 0 else "15 日至月底"
                        gaps.append(f"{scope} {p} {label} 切片 0/1，低於兩週目標")

    for b in data["batches"]:
        add("Clerk", b["id"], b["members"], b["stats"], b["slots"],
            date.fromisoformat(b["start"]), date.fromisoformat(b["end"]),
            b["available_slots"], complete=b["end"][:7] <= inp.ym)
    for scope, key in (("家醫科", "family"), ("外訓", "external")):
        block = data[key]
        add(scope, inp.ym, block["roster"], block["stats"], current_slots,
            first, last, {p: available_slots(inp, key, p) for p in block["roster"]})
    return DayCourseExplanation(tuple(pgy_rows), tuple(training), tuple(gaps))


def format_day_explanation(explanation: DayCourseExplanation) -> str:
    lines = [DAY_EXPLANATION_HEADING,
             "PGY：週三下午照光包含於照光及必要工作，不重複計入總量。",
             "照光調整 0 是同儕共同基準；−1／−2 是相對同儕的目標偏移，實排差值另列，並非保證值。",
             "代號 照光 週三午 治療室 跟診 必要工作 實際總量 手動減量 公平目標 差異"]
    for r in explanation.pgy:
        lines.append(f"{r.code} {r.photo} {r.wednesday_photo} {r.treatment} "
                     f"{r.follow} {r.necessary} {r.total} {r.manual_reduction:+} "
                     f"{float(r.fair_target):.2f} {float(r.difference):+.2f}")
        weekly = "、".join(f"{w}:{n}" for w, n in r.weekly_follows) or "無"
        doctors = "、".join(f"{name}:{n}" for name, n in r.known_doctors) or "無已知醫師"
        lines.append(f"  每週跟診 {weekly}；醫師 {doctors}；醫師未標示 {r.unknown_doctor_follows}")
        peers = [other for other in explanation.pgy if other.code != r.code]
        if peers:
            mean_photo = Fraction(sum(other.photo for other in peers), len(peers))
            mean_total = Fraction(sum(other.total for other in peers), len(peers))
            photo_gap = Fraction(r.photo) - mean_photo
            total_gap = Fraction(r.total) - mean_total
            lines.append(f"  相對其他 PGY 實排平均：照光 {float(photo_gap):+.2f} 次、"
                         f"總工作量 {float(total_gap):+.2f} 次"
                         f"（同儕平均照光 {float(mean_photo):.2f}、"
                         f"總量 {float(mean_total):.2f}）")
        else:
            lines.append("  僅一位 PGY，沒有同儕可比較實排差值")
    for r in explanation.training:
        weekly = "、".join(f"{w}:{n}" for w, n in r.weekly_follows) or "無"
        doctors = "、".join(f"{name}:{n}" for name, n in r.known_doctors) or "無已知醫師"
        pending = "（課程尚未結束，待下月安排）" if not r.complete else ""
        lines.append(f"{r.scope} {r.course} {r.code}{pending}："
                     f"可參與半日 {r.available_half_days}；"
                     f"跟診 {r.follow}（最低 {r.minimum}／目標 {r.target}／上限 {r.maximum}）；"
                     f"切片 {r.biopsy}；每週 {weekly}；醫師 {doctors}"
                     f"；醫師未標示 {r.unknown_doctor_follows}")
    lines.append("已確認缺口：" + ("；".join(explanation.confirmed_gaps)
                               if explanation.confirmed_gaps else "目前沒有統計門檻缺口"))
    lines.append("最佳性：尚未證明整份班表為全域最佳。請假、鎖定、停診與診間容量可能限制目標，"
                 "此處不推定個別缺口原因。")
    return "\n".join(lines)
