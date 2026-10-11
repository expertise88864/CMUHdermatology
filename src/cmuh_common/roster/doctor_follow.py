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

    @property
    def identity(self) -> tuple[str, str, str]:
        return self.scope, self.course, self.code

    @property
    def conflicts(self) -> int:
        counts = Counter((v.day, v.session) for v in self.visits)
        return sum(n > 1 for n in counts.values())

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

    def ranked(doctors):
        return tuple(sorted(doctors, key=lambda item: (-item[1], natural_key(item[0]))))

    for r in explanation.pgy:
        rows.append(DoctorFollowRow(
            "PGY", ym, ym, r.code, r.follow,
            data["pgy"]["stats"].get(r.code, {}).get("biopsy", 0),
            r.photo, r.treatment, r.rest, ranked(r.known_doctors),
            r.unknown_doctor_follows, r.visits))
    for r in explanation.training:
        batch = batches[r.course] if r.scope == "Clerk" else None
        period = f"{batch['start']} ~ {batch['end']}" if batch else ym
        block = batch if batch else data[{"外訓": "external", "家醫科": "family"}[r.scope]]
        stats = block["stats"].get(r.code, {})
        rows.append(DoctorFollowRow(
            r.scope, r.course, period, r.code, r.follow, r.biopsy,
            stats.get("photo", 0), stats.get("tx", 0), r.rest,
            ranked(r.known_doctors), r.unknown_doctor_follows, r.visits,
            not r.complete))
    order = {"Clerk": 0, "PGY": 1, "外訓": 2, "家醫科": 3}
    return tuple(sorted(rows, key=lambda r: (
        order[r.scope], r.period, natural_key(r.course), natural_key(r.code))))
