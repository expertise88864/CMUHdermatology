"""Read-only presentation of the same course snapshot used by the sidebar."""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass

from .day_explanation import FollowVisit


def natural_key(value: str) -> tuple:
    return tuple((1, int(part)) if part.isdecimal() else (0, part.casefold())
                 for part in re.split(r"(\d+)", value))


@dataclass(frozen=True)
class DoctorFollowRow:
    scope: str
    course: str
    period: str
    code: str
    follow: int
    biopsy: int
    photo: int
    treatment: int
    rest: int
    doctors: tuple[tuple[str, int], ...]
    unknown: int
    visits: tuple[FollowVisit, ...]
    ongoing: bool = False
    prefs: tuple[tuple[str, "int | None"], ...] = ()   # [2026-11-26] 指定多跟醫師(醫師, 目標)

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.scope, self.course, self.code

    @property
    def conflicts(self) -> int:
        counts = Counter((v.day, v.session) for v in self.visits)
        return sum(n > 1 for n in counts.values())

    def pref_text(self) -> str:
        """「王醫師 3/4」;沒填目標顯示「王醫師 3（盡量多）」。"""
        got = dict(self.doctors)
        return "、".join(f"{doc} {got.get(doc, 0)}/{target}" if target
                        else f"{doc} {got.get(doc, 0)}（盡量多）"
                        for doc, target in self.prefs) or "—"

    def rank_groups(self) -> tuple[str, ...]:
        groups: dict[int, list[str]] = {}
        for doctor, count in self.doctors:
            groups.setdefault(count, []).append(doctor)
        return tuple(f"{'／'.join(names)} {'各 ' if len(names) > 1 else ''}{count} 次"
                     for count, names in groups.items())


def doctor_follow_rows(data: dict, ym: str) -> tuple[DoctorFollowRow, ...]:
    explanation = data["explanation"]
    batches = {b["id"]: b for b in data["batches"]}
    rows = []
    scope_key = {"PGY": "pgy", "Clerk": "clerk", "外訓": "external", "家醫科": "family"}
    prefs: dict = {}
    for p in data.get("doctor_prefs") or ():
        prefs.setdefault((p.scope, p.period, p.code), []).append((p.doctor, p.target))

    def ranked(doctors):
        return tuple(sorted(doctors, key=lambda item: (-item[1], natural_key(item[0]))))

    for r in explanation.pgy:
        rows.append(DoctorFollowRow(
            "PGY", ym, ym, r.code, r.follow,
            data["pgy"]["stats"].get(r.code, {}).get("biopsy", 0),
            r.photo, r.treatment, r.rest, ranked(r.known_doctors),
            r.unknown_doctor_follows, r.visits,
            prefs=tuple(prefs.get(("pgy", ym, r.code), ()))))
    for r in explanation.training:
        batch = batches[r.course] if r.scope == "Clerk" else None
        period = f"{batch['start']} ~ {batch['end']}" if batch else ym
        block = batch if batch else data[{"外訓": "external", "家醫科": "family"}[r.scope]]
        stats = block["stats"].get(r.code, {})
        rows.append(DoctorFollowRow(
            r.scope, r.course, period, r.code, r.follow, r.biopsy,
            stats.get("photo", 0), stats.get("tx", 0), r.rest,
            ranked(r.known_doctors), r.unknown_doctor_follows, r.visits,
            not r.complete,
            tuple(prefs.get((scope_key[r.scope], r.course if r.scope == "Clerk" else ym,
                             r.code), ()))))
    order = {"Clerk": 0, "PGY": 1, "外訓": 2, "家醫科": 3}
    return tuple(sorted(rows, key=lambda r: (
        order[r.scope], r.period, natural_key(r.course), natural_key(r.code))))
