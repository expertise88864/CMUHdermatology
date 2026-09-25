"""Explain provable roster capacity bounds without claiming solver optimality.

This module reads the same input and course accounting as the solver.  A free
clinic seat is an optimistic opportunity, not a promise that all trainees can
use it simultaneously after higher-priority duties have been assigned.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from fractions import Fraction

from .course_balance import external_biopsy_open
from .day_explanation import DayCourseExplanation
from .session_leave import on_leave
from .solve_day import (
    BIOPSY, PHOTO, TREATMENT, STUDENT_SESSIONS, TWO_PGY_PHOTO_ONLY, DaySolveInput,
    apply_locked_adjustments, arbitration_order, batch_biopsy_slots,
    day_owner_batch, is_follow_slot,
)
from .training_bands import available_slots, week_slots

DAY_FEASIBILITY_HEADING = "【需求缺口與容量診斷】"


@dataclass(frozen=True)
class RequirementCapacity:
    scope: str
    course: str
    code: str
    kind: str
    minimum: int
    target: int
    available_half_days: int
    possible_sessions: int
    shared_seats: int
    actual: int
    unquantified_sessions: int = 0
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class DayFeasibility:
    rows: tuple[RequirementCapacity, ...]
    proven_shortages: tuple[str, ...]
    manual_notes: tuple[str, ...] = ()


def _clinic_capacity(inp: DaySolveInput, code: str, slots: set,
                     *, pgy: bool = False, adjacent_grid: dict | None = None
                     ) -> tuple[int, int, int, tuple[str, ...]]:
    possible = shared = unknown = 0
    blockers = []
    for d, session in sorted(slots):
        iso = d.isoformat()
        if iso[:7] < inp.ym:
            # This month's solve cannot add a seat to the previous month.
            locked = (inp.prior_sessions.get(iso) or {}).get(session) or {}
            fixed_reason = "前月已固定，本月不能重排"
        elif iso[:7] > inp.ym:
            locked = (inp.course_fixed.get(iso) or {}).get(session)
            fixed_reason = "鄰月已鎖定或定案"
        else:
            locked = (inp.locked.get(iso) or {}).get(session)
            fixed_reason = "已鎖定"
        if locked is not None:
            personal = int(any(code in (people or ()) for r, people in locked.items()
                               if is_follow_slot(r)))
            seats = sum(len(people or ()) for r, people in locked.items()
                        if is_follow_slot(r))
            if not personal:
                blockers.append(f"{iso} {session} {fixed_reason}")
        else:
            grid = inp.grid.get(d)
            if grid is None:
                grid = (adjacent_grid or {}).get(d)
            if grid is None:
                # A free neighboring session needs the adjacent clinic grid.
                unknown += 1
                continue
            rooms = [r for r in grid.get(session, ()) if is_follow_slot(r)]
            seats = len(rooms) * inp.capacity
            personal = int(seats > 0)
            if not personal:
                blockers.append(f"{iso} {session} 無可跟診門診")
            elif pgy:
                required = (1 if d.weekday() == 2 and session == "下午"
                            or (len(inp.pgy_roster) == 2
                                and (d.weekday(), session) in TWO_PGY_PHOTO_ONLY)
                            else 2)
                present = sum(not on_leave(inp, "pgy", person, d, session)
                              for person in inp.pgy_roster)
                if present <= required:
                    personal = 0
                    blockers.append(f"{iso} {session} 必要照光／治療室占用全部可用 PGY")
        possible += personal
        shared += seats
    return possible, shared, unknown, tuple(blockers)


def _leave_blockers(inp: DaySolveInput, scope: str, code: str,
                    *, batch: dict | None = None) -> tuple[str, ...]:
    if batch is not None:
        start, end = date.fromisoformat(batch["start"]), date.fromisoformat(batch["end"])
        available = batch["available_slots"][code]
        days = (date.fromordinal(n) for n in range(start.toordinal(), end.toordinal() + 1))
        return tuple(f"{d.isoformat()} {s} 請假" for d in days
                     if d.isoformat() in inp.course_days
                     for s in STUDENT_SESSIONS if (d, s) not in available)
    return tuple(f"{d.isoformat()} {s} 請假" for d in sorted(inp.grid)
                 if d.isoformat()[:7] == inp.ym and d.weekday() < 5
                 and d not in inp.holidays
                 for s in STUDENT_SESSIONS
                 if (scope != "family" or (d, s) in inp.family_follow.get(code, set()))
                 and on_leave(inp, scope, code, d, s))


def _clerk_shared_follow_capacity(inp: DaySolveInput, batch: dict,
                                  adjacent_grid: dict | None) -> tuple[int, int]:
    """Optimistic whole-course total; fixed sessions count actual Clerk seats."""
    available_by_person = batch["available_slots"]
    candidates = set().union(*(set(slots) for slots in available_by_person.values()))
    order = arbitration_order(inp)
    total = unknown = 0
    for d, session in sorted(candidates):
        owner = day_owner_batch(order, d)
        if owner is None or owner.id != batch["id"]:
            continue
        iso = d.isoformat()
        if iso[:7] < inp.ym:
            fixed = (inp.prior_sessions.get(iso) or {}).get(session) or {}
        elif iso[:7] > inp.ym:
            fixed = (inp.course_fixed.get(iso) or {}).get(session)
        else:
            fixed = (inp.locked.get(iso) or {}).get(session)
        if fixed is not None:
            total += sum(person in available_by_person
                         for room, people in fixed.items() if is_follow_slot(room)
                         for person in (people or ()))
        else:
            grid = inp.grid.get(d)
            if grid is None:
                grid = (adjacent_grid or {}).get(d)
            if grid is None:
                unknown += 1
                continue
            rooms = [r for r in grid.get(session, ()) if is_follow_slot(r)]
            present = sum((d, session) in slots
                          for slots in available_by_person.values())
            total += min(present, len(rooms) * inp.capacity)
    return total, unknown


def _retained_clerk_leave_conflicts(
        inp: DaySolveInput, batch: dict) -> tuple[tuple[str, str, str], ...]:
    """A saved assignment can survive a later leave edit; it is not proof of lost capacity."""
    members = set(batch["members"])
    available = batch["available_slots"]
    order = arbitration_order(inp)
    conflicts = []
    for iso, sessions in (batch.get("slots") or {}).items():
        if not isinstance(sessions, dict):
            continue
        try:
            d = date.fromisoformat(iso)
        except (TypeError, ValueError):
            continue
        owner = day_owner_batch(order, d)
        if owner is None or owner.id != batch["id"]:
            continue
        for session, cells in (sessions or {}).items():
            if not isinstance(cells, dict):
                continue
            for room, people in cells.items():
                if not is_follow_slot(room):
                    continue
                conflicts.extend(
                    (iso, session, person) for person in (people or ())
                    if person in members and (d, session) not in available.get(person, set()))
    return tuple(sorted(set(conflicts)))


def _saved_follow_outside_availability(
        slots: dict, code: str, available: set,
        *, ym: str) -> tuple[tuple[str, str], ...]:
    conflicts = []
    for iso, sessions in (slots or {}).items():
        if not isinstance(sessions, dict):
            continue
        if iso[:7] != ym:
            continue
        try:
            d = date.fromisoformat(iso)
        except (TypeError, ValueError):
            continue
        for session, cells in (sessions or {}).items():
            if not isinstance(cells, dict):
                continue
            if any(code in (people or ()) for room, people in cells.items()
                   if is_follow_slot(room)) and (d, session) not in available:
                conflicts.append((iso, session))
    return tuple(sorted(set(conflicts)))


def _clerk_biopsy_coverage(inp: DaySolveInput, batch: dict, seats: set) -> int:
    """Only immutable saved work consumes a seat; current unlocked work can move."""
    members = set(batch["members"])
    start, end = batch["start"], batch["end"]
    done = set()
    occupied = set()
    for iso, sessions in (batch.get("slots") or {}).items():
        if not start <= iso <= end or not isinstance(sessions, dict):
            continue
        for session, cells in sessions.items():
            if not isinstance(cells, dict):
                continue
            fixed = (iso[:7] < inp.ym
                     or (iso[:7] == inp.ym
                         and session in (inp.locked.get(iso) or {}))
                     or (iso[:7] > inp.ym
                         and session in (inp.course_fixed.get(iso) or {})))
            if not fixed:
                continue
            assigned = {str(person) for person in (cells.get(BIOPSY) or ())
                        if str(person) in members}
            if assigned:
                done.update(assigned)
                if (iso, session) in seats:
                    occupied.add((iso, session))
    return len(seats - occupied) + len(done)


def diagnose_day(inp: DaySolveInput, explanation: DayCourseExplanation,
                 data: dict, current_slots: dict | None = None,
                 adjacent_grid: dict | None = None) -> DayFeasibility:
    """Build preflight/current/preview diagnostics from one authoritative snapshot."""
    rows = []
    shortages = []
    manual_notes = []
    order = arbitration_order(inp)
    biopsy_slots, _ = batch_biopsy_slots(inp, order)
    locked_keys, locked_bx, _, _, _ = apply_locked_adjustments(
        inp, order, biopsy_slots)
    batches = {b["id"]: b for b in data["batches"]}
    retained_conflicts = {
        bid: _retained_clerk_leave_conflicts(inp, batch)
        for bid, batch in batches.items()}
    prior_fixed_counts: dict[tuple[str, str], int] = {}
    for bid, seats in biopsy_slots.items():
        if bid not in batches:
            # Arbitration may include an earlier neighboring batch whose
            # saved fixed work was absorbed, but which is not in this view.
            continue
        members = set(batches[bid]["members"])
        for iso, session in tuple(seats):
            if iso[:7] >= inp.ym:
                continue
            # The previous month cannot be changed by this solve.  An open but
            # empty seat there is not an opportunity; an assignment is one
            # already-consumed seat even if its session was not explicitly locked.
            prior = (inp.prior_sessions.get(iso) or {}).get(session) or {}
            person = next((str(p) for p in (prior.get(BIOPSY) or ())
                           if str(p) in members), None)
            if person is None:
                seats.discard((iso, session))
            elif (iso, session) not in locked_keys.get(bid, set()):
                locked_keys.setdefault(bid, set()).add((iso, session))
                prior_fixed_counts[(bid, person)] = (
                    prior_fixed_counts.get((bid, person), 0) + 1)

    for row in explanation.training:
        if row.scope == "Clerk":
            batch = batches[row.course]
            retained_personal = any(person == row.code for _, _, person
                                    in retained_conflicts[row.course])
            planned = batch["available_slots"][row.code]
            available = {(d, s) for d, s in planned
                         if (owner := day_owner_batch(order, d)) is not None
                         and owner.id == row.course}
            arbitration_blockers = tuple(
                f"{d.isoformat()} {s} 梯次重疊，{owner.id} 優先"
                for d, s in sorted(planned - available)
                if (owner := day_owner_batch(order, d)) is not None)
            leave_blockers = (_leave_blockers(inp, "clerk", row.code, batch=batch)
                              + arbitration_blockers)
        else:
            scope = "family" if row.scope == "家醫科" else "external"
            available = available_slots(inp, scope, row.code)
            leave_blockers = _leave_blockers(inp, scope, row.code)
            retained_slots = _saved_follow_outside_availability(
                current_slots or {}, row.code, available, ym=inp.ym)
            retained_personal = bool(retained_slots)
            if retained_slots:
                manual_notes.append(
                    f"{row.scope} {row.code} 已排跟診但目前請假／不可參與："
                    + "、".join(f"{iso} {session}" for iso, session in retained_slots[:5])
                    + "；此人不據現有可參與時段證明跟診容量不足")
        possible, shared, unknown, blockers = _clinic_capacity(
            inp, row.code, available, adjacent_grid=adjacent_grid)
        rows.append(RequirementCapacity(
            row.scope, row.course, row.code, "跟診", row.minimum, row.target,
            len(available), possible, shared, row.follow, unknown,
            blockers + leave_blockers))
        if (unknown == 0 and not retained_personal
                and possible < row.minimum and row.follow < row.minimum):
            shortages.append(
                f"{row.scope} {row.course} {row.code} 跟診：最低 {row.minimum}，"
                f"個人最多可用 {possible} 個半日，至少缺 {row.minimum - possible}；"
                "可檢查請假、停診與鎖定格")

        if row.scope == "Clerk":
            all_bio = biopsy_slots.get(row.course, set())
            free = all_bio - locked_keys.get(row.course, set())
            fixed = (len(locked_bx.get((row.course, row.code), ()))
                     + prior_fixed_counts.get((row.course, row.code), 0))
            personal = fixed + sum((date.fromisoformat(iso), session) in available
                                   for iso, session in free)
            actual = row.biopsy
            rows.append(RequirementCapacity(
                row.scope, row.course, row.code, "切片", 1, 1,
                len(available), personal, len(all_bio), actual,
                blockers=tuple(f"{iso} {session} 切片開放"
                               for iso, session in sorted(all_bio))))
            if personal < 1 and actual < 1:
                shortages.append(
                    f"Clerk {row.course} {row.code} 切片：最低 1，"
                    "沒有可用切片席位；可檢查請假、切片開放與鎖定格")
        else:
            for half in (0, 1):
                half_available = {(d, s) for d, s in available
                                  if (d.day > 14) == bool(half)}
                if not half_available:
                    continue
                open_slots = set()
                for d, s in half_available:
                    if not external_biopsy_open(inp, d, s):
                        continue
                    locked = (inp.locked.get(d.isoformat()) or {}).get(s)
                    if locked is None or row.code in (locked.get(BIOPSY) or ()):
                        open_slots.add((d, s))
                rows.append(RequirementCapacity(
                    row.scope, row.course, row.code,
                    "切片 1–14 日" if half == 0 else "切片 15 日至月底",
                    1, 1, len(half_available), len(open_slots),
                    len(open_slots), row.biopsy_by_half[half]))
                if not open_slots and row.biopsy_by_half[half] < 1:
                    label = "1–14 日" if half == 0 else "15 日至月底"
                    shortages.append(
                        f"{row.scope} {row.code} {label} 切片：目標 1，"
                        "在可參與時段中沒有開放且未鎖給他人的切片席位；"
                        "可檢查切片開放、請假及鎖定格")

    for b in data["batches"]:
        members = sorted(set(b["members"]))
        personal_caps = {
            r.code: r.possible_sessions for r in rows
            if r.scope == "Clerk" and r.course == b["id"] and r.kind == "跟診"}
        shared_minimum = sum(min(9, personal_caps.get(person, 9))
                             for person in members)
        follow_seats, unknown_follow = _clerk_shared_follow_capacity(
            inp, b, adjacent_grid)
        conflicts = retained_conflicts[b["id"]]
        if conflicts:
            manual_notes.append(
                f"Clerk {b['id']} 請假與保留班表衝突，不能以現有可參與時段證明"
                "全梯席位不足：" + "、".join(
                    f"{iso} {session} {person} 已排跟診但目前請假／不可參與"
                    for iso, session, person in conflicts[:5])
                + (f"，另 {len(conflicts) - 5} 個" if len(conflicts) > 5 else ""))
        if (members and not unknown_follow and not conflicts
                and follow_seats < shared_minimum):
            shortages.append(
                f"Clerk {b['id']} 全梯跟診總席位不足：扣除個人本來就無法達到的"
                f"診數後仍需 {shared_minimum} 席，樂觀上界只有 {follow_seats} 席，"
                f"至少缺 {shared_minimum - follow_seats} 席；"
                "可檢查門診停診、梯次重疊、請假及鎖定格")
        seats = biopsy_slots.get(b["id"], set())
        coverage = _clerk_biopsy_coverage(inp, b, seats)
        if members and coverage < len(members):
            dates = "、".join(f"{iso} {session}" for iso, session in sorted(seats)) or "無"
            shortages.append(
                f"Clerk {b['id']} 切片席位容量不足：{len(members)} 人各需至少 1 席，"
                f"已實排成員與其餘可用單人席位最多涵蓋 {coverage} 人，"
                f"至少缺 {len(members) - coverage} 席。"
                f"目前席位：{dates}；可增加整梯有效開放時段或調整梯次人數")

    for row in explanation.pgy:
        available = available_slots(inp, "pgy", row.code)
        leave_blockers = _leave_blockers(inp, "pgy", row.code)
        retained_slots = _saved_follow_outside_availability(
            current_slots or {}, row.code, available, ym=inp.ym)
        if retained_slots:
            manual_notes.append(
                f"PGY {row.code} 已排跟診但目前請假／不可參與："
                + "、".join(f"{iso} {session}" for iso, session in retained_slots[:5])
                + "；請先處理保留班表與請假衝突")
        for week, ws in week_slots(available).items():
            possible, shared, unknown, blockers = _clinic_capacity(
                inp, row.code, ws, pgy=True)
            label = f"{week[0]}-W{week[1]:02d}"
            actual = dict(row.weekly_follows).get(label, 0)
            rows.append(RequirementCapacity(
                "PGY", label, row.code, "跟診", 1, 2, len(ws),
                possible, shared, actual, unknown,
                blockers + tuple(b for b in leave_blockers
                                 if date.fromisoformat(b[:10]).isocalendar()[:2] == week)))
            if possible < 1 and actual < 1 and not unknown:
                shortages.append(
                    f"PGY {row.code} {label} 跟診：最低 1，"
                    "沒有可用門診半日；先檢查停診、請假與鎖定格")

        if row.manual_reduction:
            peers = [p for p in explanation.pgy if p.code != row.code]
            if peers:
                photo_gap = Fraction(row.photo) - Fraction(
                    sum(p.photo for p in peers), len(peers))
                total_gap = Fraction(row.total) - Fraction(
                    sum(p.total for p in peers), len(peers))
                manual_notes.append(
                    f"PGY {row.code} 設定 {row.manual_reduction:+}："
                    f"實排照光相對同儕 {float(photo_gap):+.2f}、"
                    f"總工作量相對同儕 {float(total_gap):+.2f}；"
                    "這是相對目標，需以本人設 0 的獨立試排核對實際影響")
            protected = []
            for iso, sessions in (current_slots or {}).items():
                if not isinstance(sessions, dict):
                    continue
                if iso[:7] != inp.ym:
                    continue
                try:
                    d = date.fromisoformat(iso)
                except (TypeError, ValueError):
                    continue
                for session, cells in sessions.items():
                    if not isinstance(cells, dict):
                        continue
                    for room, duty in ((PHOTO, "照光"), (TREATMENT, "治療室")):
                        if row.code not in (cells.get(room) or ()):
                            continue
                        cause = None
                        if session in (inp.locked.get(iso) or {}):
                            cause = "鎖定"
                        elif (len(inp.pgy_roster) == 2 and room == PHOTO
                              and (d.weekday(), session) in TWO_PGY_PHOTO_ONLY):
                            cause = "兩位 PGY 特殊輪替"
                        elif not any(other != row.code and not on_leave(
                                inp, "pgy", other, d, session)
                                for other in inp.pgy_roster):
                            cause = "其他 PGY 均請假／不可用，必要工作需有人負責"
                        if cause:
                            protected.append(f"{iso} {session} {duty} {cause}")
            if protected:
                manual_notes.append(
                    f"PGY {row.code} 已排必要工作中的不可直接移除時段："
                    + "、".join(protected[:8])
                    + (f"，另 {len(protected) - 8} 個" if len(protected) > 8 else ""))
            weekly_actual = dict(row.weekly_follows)
            one_follow = [f"{week[0]}-W{week[1]:02d}"
                          for week in week_slots(available)
                          if weekly_actual.get(f"{week[0]}-W{week[1]:02d}", 0) == 1]
            if one_follow:
                manual_notes.append(
                    f"PGY {row.code} 這些週次跟診已在最低 1 次，"
                    "不可藉刪除跟診強行減量：" + "、".join(one_follow))

    return DayFeasibility(tuple(rows), tuple(shortages), tuple(manual_notes))


def format_day_feasibility(report: DayFeasibility) -> str:
    lines = [DAY_FEASIBILITY_HEADING,
             "最低需求為求解器優先處理的軟目標；照光、治療室、請假、鎖定與診間容量是硬限制。",
             "可用跟診席位是個人樂觀上界，未完整扣除同時段競爭；不能據此宣稱必可達標。"]
    for r in report.rows:
        seats = (f"{r.possible_sessions} 個可跟半日／共用 {r.shared_seats} 席"
                 if r.kind == "跟診" else f"{r.possible_sessions} 個個人可能席位")
        if r.unquantified_sessions:
            seats += f"；另有鄰月 {r.unquantified_sessions} 個半日未量化診間席位"
        lines.append(f"{r.scope} {r.course} {r.code} {r.kind}："
                     f"最低 {r.minimum}／目標 {r.target}，"
                     f"可參與 {r.available_half_days} 半日，可用 {seats}，實排 {r.actual}")
        if r.actual < r.minimum and (r.unquantified_sessions or
                                     r.possible_sessions >= r.minimum):
            lines.append(
                f"  實排距最低尚差 {r.minimum - r.actual}；目前未證明容量不足。"
                "可檢查高優先工作、同時段人員競爭與保留班表。")
        if r.blockers and r.kind == "跟診":
            lines.append("  具體受限時段：" + "、".join(r.blockers[:5])
                         + (f"，另 {len(r.blockers) - 5} 個" if len(r.blockers) > 5 else ""))
    lines.append("可證明的容量缺口：" + ("；".join(report.proven_shortages)
                                      if report.proven_shortages else "目前未發現；不代表全域可行"))
    if report.manual_notes:
        lines.append("補充診斷：" + "；".join(report.manual_notes))
    lines.append("家醫可跟診設定是可參與時段，不代表每格必排；切片席位不可多人共用。")
    return "\n".join(lines)
