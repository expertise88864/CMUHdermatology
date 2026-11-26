# -*- coding: utf-8 -*-
"""[2026-11-26 使用者] 指定某人多跟某位醫師(PGY／Clerk／外訓／家醫科)。

月檔欄位 `doctor_prefs`:
    {"month": [{"scope": "pgy"|"external"|"family", "code": 代號,
                "doctor": 醫師姓名, "target": 次數或 null}],
     "clerk": {梯次 id: [{"code", "doctor", "target"}]}}

* PGY／外訓／家醫科以【月】為單位,只看本月的設定。
* Clerk 跟著【梯次】:本月月檔有這一梯的鍵就用它(空清單＝已清除),
  沒有就沿用上個月月檔的同一梯 —— 跨月的梯次兩個月都有效,下個月改了也不必回頭動
  (可能已定案的)上個月。
* target 空白＝盡量多;有填＝做到目標就好,之後的調整也不會讓次數掉到目標以下。
* 只在「同一個半天換診間」或「同一天上午、下午互換」實現,不改日子、不改次數
  (見 `clinic_diversity.balance_clinics`)。
"""
from __future__ import annotations

from dataclasses import dataclass

SCOPES = ("clerk", "family", "external", "pgy")     # 同時搶同一位醫師時的優先順序
MONTH_SCOPES = ("pgy", "external", "family")
LABELS = {"pgy": "PGY", "clerk": "Clerk", "external": "外訓", "family": "家醫科"}
MAX_TARGET = 99


@dataclass(frozen=True)
class DoctorPref:
    scope: str            # pgy / clerk / external / family
    period: str           # 月份 YYYY-MM;Clerk 為梯次 id
    code: str
    doctor: str
    target: "int | None" = None

    @property
    def key(self) -> tuple:
        """與 `balance_clinics` 的人員鍵相同:(scope, 月份或梯次, 代號)。"""
        return self.scope, self.period, self.code

    def describe(self) -> str:
        goal = f"目標 {self.target} 次" if self.target else "盡量多"
        return f"{LABELS[self.scope]} {self.code} 多跟 {self.doctor}（{goal}）"


def _entry(raw, where: str, *, scoped: bool) -> tuple:
    if not isinstance(raw, dict):
        raise ValueError(f"doctor_prefs {where} 每一筆必須是物件")
    scope = raw.get("scope") if scoped else "clerk"
    if scoped and scope not in MONTH_SCOPES:
        raise ValueError(f"doctor_prefs {where} 類別必須是 pgy／external／family")
    code, doctor = raw.get("code"), raw.get("doctor")
    for name, value in (("代號", code), ("醫師", doctor)):
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError(f"doctor_prefs {where} 的{name}必須是前後無空白的文字")
    target = raw.get("target")
    if target is not None and (type(target) is not int or not 1 <= target <= MAX_TARGET):
        raise ValueError(f"doctor_prefs {where} 的次數必須是 1～{MAX_TARGET} 的整數或空白")
    return scope, code, doctor, target


def parse_doctor_prefs(raw: dict) -> tuple[list, dict]:
    """月檔 → (本月 [(scope, code, doctor, target)], {梯次: [(code, doctor, target)]})。

    形狀錯誤一律 ValueError(排班的嚴格來源會在求解前講清楚,不會安靜地少一筆)。"""
    block = (raw or {}).get("doctor_prefs")
    if block is None:
        return [], {}
    if not isinstance(block, dict) or set(block) - {"month", "clerk"}:
        raise ValueError("doctor_prefs 指定多跟醫師必須是只有 month／clerk 的物件")
    month_raw = block.get("month") or []
    clerk_raw = block.get("clerk") or {}
    if not isinstance(month_raw, list) or not isinstance(clerk_raw, dict):
        raise ValueError("doctor_prefs 的 month 必須是清單、clerk 必須是物件")
    month = [_entry(e, "month", scoped=True) for e in month_raw]
    if len({e[:3] for e in month}) != len(month):
        raise ValueError("doctor_prefs month 有重複的「類別＋代號＋醫師」")
    clerk = {}
    for bid, entries in clerk_raw.items():
        if not isinstance(bid, str) or not bid or not isinstance(entries, list):
            raise ValueError("doctor_prefs clerk 必須是 {梯次: 清單}")
        rows = [_entry(e, f"clerk[{bid}]", scoped=False)[1:] for e in entries]
        if len({e[:2] for e in rows}) != len(rows):
            raise ValueError(f"doctor_prefs clerk[{bid}] 有重複的「代號＋醫師」")
        clerk[bid] = rows
    return month, clerk


def effective_prefs(ym: str, month: dict, prev_month: "dict | None",
                    batch_ids) -> tuple[DoctorPref, ...]:
    """這個月排班要用的指定(Clerk 依梯次沿用上個月,見模組說明)。"""
    rows, clerk = parse_doctor_prefs(month)
    _prev_rows, prev_clerk = parse_doctor_prefs(prev_month or {})
    out = [DoctorPref(s, ym, c, d, t) for s, c, d, t in rows]
    for bid in batch_ids:
        source = clerk if bid in clerk else prev_clerk
        out.extend(DoctorPref("clerk", bid, c, d, t) for c, d, t in source.get(bid, []))
    return tuple(sorted(out, key=lambda p: (SCOPES.index(p.scope), p.period, p.code,
                                            p.doctor)))


def apply_edit(month: dict, ym: str, prefs, inherited, batch_ids) -> None:
    """把對話框裡的完整清單寫回本月月檔(mutator 內呼叫,可重跑)。

    prefs:對話框裡的 DoctorPref;inherited:開啟時從上個月沿用的 Clerk 部分。
    Clerk 只有跟沿用內容不同的梯次才寫鍵,沒改的照樣跟著上個月走。"""
    month_rows = [{"scope": p.scope, "code": p.code, "doctor": p.doctor, "target": p.target}
                  for p in prefs if p.scope in MONTH_SCOPES and p.period == ym]
    parse_doctor_prefs(month)          # 現有內容壞掉就不要在它上面改
    clerk_raw = dict(((month.get("doctor_prefs") or {}).get("clerk")) or {})
    for bid in batch_ids:
        mine = sorted((p.code, p.doctor, p.target) for p in prefs
                      if p.scope == "clerk" and p.period == bid)
        before = sorted((p.code, p.doctor, p.target) for p in inherited
                        if p.period == bid)
        if bid in clerk_raw or mine != before:
            clerk_raw[bid] = [{"code": c, "doctor": d, "target": t} for c, d, t in mine]
    block = {}
    if month_rows:
        block["month"] = month_rows
    if clerk_raw:
        block["clerk"] = clerk_raw
    if block:
        month["doctor_prefs"] = block
    else:
        month.pop("doctor_prefs", None)
    parse_doctor_prefs(month)          # 寫入前再驗一次形狀


def possible_sessions(inp, slots, pref: DoctorPref, key_days, prefs=None) -> int:
    """「只換診間／同一天上下午互換」最多做得到的次數(給報告說明原因用)。

    每一天:同一個半天就有這位醫師的跟診,加上「換到同一天另一個半天」—— 那個半天
    這個人必須是放假(有切片等工作就換不過去),家醫科不能換半天。鎖定時段、固定人員、
    Apply 本科 101 診換不動,只算原本就在這位醫師診的。是上限:沒有計算名額(要跟別人
    互換時還要對方也願意換)。原本有跟其他醫師的人要留一次給其他醫師。
    不在本月的日期(跨月梯次的另一半)換不動,只算原本就在這位醫師診的。
    key_days:這個人屬於這個期間的日期(Clerk 為整梯)。prefs:這個人所有的指定(預設取
    `inp.doctor_prefs`;對話框傳尚未存檔的清單)—— 指定的醫師都不必「留一次」。"""
    from datetime import date

    from .held import is_held
    from .solve_day import REST, STUDENT_SESSIONS, is_follow_slot
    kept_out = {pref.doctor} | {p.doctor for p in (inp.doctor_prefs if prefs is None else prefs)
                                if p.key == pref.key}
    total = named_other = named_moved = 0     # 跟其他(有標示)醫師的次數/其中被算成可換走的
    for iso, sessions in slots.items():
        try:
            d = date.fromisoformat(iso)
        except (TypeError, ValueError):
            continue
        if d not in key_days or not isinstance(sessions, dict):
            continue
        doctors = inp.clinic_doctors.get(d, {})

        def fixed(s, iso=iso):
            # 別的月份(跨月梯次的上個月／下個月)只是歷史或之後才排,這次換不動
            return (iso[:7] != inp.ym or s in ((getattr(inp, "locked", None) or {}).get(iso) or {})
                    or is_held(inp, iso, s, pref.code))

        rooms = {s: [r for r, ps in (sessions.get(s) or {}).items()
                     if is_follow_slot(r) and pref.code in (ps or [])]
                 for s in STUDENT_SESSIONS}
        here = {s for s, rs in rooms.items()
                if any(doctors.get(s, {}).get(r) == pref.doctor for r in rs)}
        pinned = {s for s, rs in rooms.items()
                  if pref.scope == "pgy" and pref.code in getattr(inp, "apply_pref", ())
                  and "101" in rs and d.weekday() in (1, 4)}
        movable = {s for s, rs in rooms.items() if rs and not fixed(s) and s not in pinned} - here
        clinic = {s for s in STUDENT_SESSIONS
                  if pref.doctor in (doctors.get(s) or {}).values()}

        def named(s, doctors=doctors, rooms=rooms):
            return any((doctors.get(s, {}).get(r) or "") not in kept_out | {""} for r in rooms[s])

        named_other += sum(named(s) for s in rooms if rooms[s] and s not in here)
        same = movable & clinic
        total += len(here) + len(same)
        named_moved += sum(named(s) for s in same)
        if pref.scope != "family":
            free = {t for t in clinic if not rooms[t] and not fixed(t)
                    and pref.code in ((sessions.get(t) or {}).get(REST) or [])}
            rest = movable - clinic
            cross = min(len(rest), len(free))
            total += cross
            # 換半天時先換沒標示醫師的,有標示其他醫師的盡量留下
            named_moved += max(0, cross - sum(not named(s) for s in rest))
    # 原本有跟其他醫師 → 至少留一次;換不動的那一次本來就留著,全部都可換走才扣一次
    if named_other and named_other == named_moved:
        total -= 1
    return max(0, total)


def calendar_days(inp, scope: str, period: str) -> set:
    """這個期間在行事曆上的日期:Clerk 為整梯(含尚未排班的跨月部分),其他為本月。"""
    days = set(inp.clinic_doctors) | set(inp.grid)
    if scope == "clerk":
        batch = next((b for b in inp.clerk_batches if b.id == period), None)
        return {d for d in days if batch is not None and batch.covers(d)}
    return {d for d in days if d.isoformat()[:7] == inp.ym}


def _period_slots(inp, slots, pref: DoctorPref):
    """→ (這個期間的班表, 這個人屬於這個期間的日期);Clerk 含上個月已排與下個月已定的部分。"""
    from datetime import date
    if pref.scope != "clerk":
        days = {d for d in inp.grid if d.isoformat()[:7] == inp.ym}
        return {iso: ss for iso, ss in slots.items() if iso[:7] == inp.ym}, days
    batch = next((b for b in inp.clerk_batches if b.id == pref.period), None)
    if batch is None:
        return {}, set()
    merged = {iso: ss for iso, ss in inp.prior_sessions.items() if iso[:7] < inp.ym}
    merged.update({iso: ss for iso, ss in inp.course_fixed.items() if iso[:7] > inp.ym})
    merged.update({iso: ss for iso, ss in slots.items() if iso[:7] == inp.ym})
    days = set()
    for iso in merged:
        try:
            d = date.fromisoformat(iso)
        except (TypeError, ValueError):
            continue
        if batch.covers(d):
            days.add(d)
    return merged, days


def achieved(inp, slots, pref: DoctorPref) -> int:
    """這個人實際跟到這位醫師幾次(Clerk 算整梯)。"""
    from datetime import date

    from .solve_day import STUDENT_SESSIONS, is_follow_slot
    merged, days = _period_slots(inp, slots, pref)
    n = 0
    for iso, sessions in merged.items():
        try:
            d = date.fromisoformat(iso)
        except (TypeError, ValueError):
            continue
        if d not in days or not isinstance(sessions, dict):
            continue
        for s in STUDENT_SESSIONS:
            for r, ps in (sessions.get(s) or {}).items():
                if (is_follow_slot(r) and pref.code in (ps or [])
                        and inp.clinic_doctors.get(d, {}).get(s, {}).get(r) == pref.doctor):
                    n += 1
    return n


def pref_warnings(inp, slots) -> list:
    """排完之後逐筆說明:人不在名單、醫師這段期間沒門診、沒做到目標(附原因)。"""
    out = []
    rosters = {"pgy": inp.pgy_roster, "external": inp.external_roster,
               "family": inp.family_roster}
    for pref in getattr(inp, "doctor_prefs", ()) or ():
        head = f"指定多跟醫師：{LABELS[pref.scope]} {pref.code}"
        if pref.scope == "clerk":
            batch = next((b for b in inp.clerk_batches if b.id == pref.period), None)
            if batch is None or pref.code not in batch.members:
                out.append(f"{head}（梯次 {pref.period}）已不在此梯名單，設定未套用")
                continue
            head += f"（梯次 {pref.period}）"
        elif pref.code not in rosters[pref.scope]:
            out.append(f"{head} 已不在本月名單，設定未套用")
            continue
        merged, days = _period_slots(inp, slots, pref)
        if not any(pref.doctor in (inp.clinic_doctors.get(d, {}).get(s) or {}).values()
                   for d in calendar_days(inp, pref.scope, pref.period)
                   for s in ("上午", "下午")):
            out.append(f"{head} 指定的 {pref.doctor} 在這段期間的門診表沒有門診，設定未套用")
            continue
        if not pref.target:
            continue
        got = achieved(inp, slots, pref)
        if got >= pref.target:
            continue
        line = f"{head} 指定 {pref.doctor}：目標 {pref.target}、實際 {got}"
        cap = possible_sessions(inp, merged, pref, days)
        if cap < pref.target:
            line += (f"；這個人有排跟診的半天裡，{pref.doctor} 的門診最多只能換到約 {cap} 次"
                     "（不會為了跟診改日子）")
        else:
            line += "；名額被優先順序較高的人占用，或受固定人員、鎖定時段、Apply 本科 101 診限制"
        out.append(line)
    return out
