# -*- coding: utf-8 -*-
"""PGY/Clerk 逐時段填充器（設計文件 §3.6 五步驟＋放假；純函式、決定性）。

每時段輸入：跟診診間(房號升冪)、可用 PGY、可用 Clerk、診間容量、切片室是否開。
六步驟（各為一個可替換 FillStep，順序 = PIPELINE）：
  1 治療室Step   ← 1 位 PGY（總治療室次數最少者；週三下午另計 tx_wed_pm 公平）
  2 切片室Step   ← 1 位 Clerk（僅切片室開；優先本梯未輪過切片者）
  3 ClerkSeed    每個開診診間各放 1 位 Clerk（房號序、就座公平輪轉）
  4 PgyMix       逐欄補 PGY（先補到「有 1 人的診間」形成 1C+1P；無 Clerk 月直接填診）
  5 ClerkOverflow 剩 Clerk 補進剩餘容量
  6 RestStep     還沒位子 → 放假（放假次數輪平均）

決定性鐵律：一切輪選用 key=(次數, 上次輪到日期 or date.min, 代號) 取最小。
不硬塞：治療室無 PGY / 切片室開但無 Clerk → 記警告，不填。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from cmuh_common.roster.model import STUDENT_SESSIONS, is_weekend

TREATMENT = "治療室"
BIOPSY = "切片室"
REST = "放假"
WED = 2


def _pick(cands: list, count_map: dict, last_map: dict):
    """公平輪選：次數最少 → 最久沒輪到 → 代號字典序（決定性）。"""
    return min(cands, key=lambda p: (count_map.get(p, 0),
                                     last_map.get(p, date.min), p))


@dataclass
class FairCounters:
    tx_total: dict = field(default_factory=dict)     # PGY 治療室總次數
    tx_wed_pm: dict = field(default_factory=dict)    # PGY 週三下午治療室次數
    rest: dict = field(default_factory=dict)         # 放假次數（PGY+Clerk）
    biopsy_done: dict = field(default_factory=dict)  # Clerk 本梯切片次數
    seat: dict = field(default_factory=dict)         # 診間就座次數（公平輪轉）
    last_tx: dict = field(default_factory=dict)
    last_rest: dict = field(default_factory=dict)
    last_biopsy: dict = field(default_factory=dict)
    last_seat: dict = field(default_factory=dict)


@dataclass
class SessionCtx:
    d: date
    session: str
    rooms: list                       # 跟診房（升冪）
    pgy: list                         # 可用 PGY（步驟會消耗）
    clerk: list                       # 可用 Clerk
    biopsy_open: bool
    capacity: int
    fc: FairCounters
    room_slots: dict = field(default_factory=dict)

    @property
    def wed_pm(self) -> bool:
        return self.d.weekday() == WED and self.session == "下午"


class FillStep:
    def run(self, ctx: SessionCtx, slots: dict, log: list) -> None:  # noqa: ARG002
        raise NotImplementedError


class TreatmentStep(FillStep):
    def run(self, ctx, slots, log):
        if not ctx.pgy:
            log.append(f"⚠ {ctx.session} 治療室無 PGY 可排（全請假？）")
            return
        fc = ctx.fc
        if ctx.wed_pm:                          # 週三下午：先比 tx_wed_pm 再比總次數
            pick = min(ctx.pgy, key=lambda p: (
                fc.tx_wed_pm.get(p, 0), fc.tx_total.get(p, 0),
                fc.last_tx.get(p, date.min), p))
        else:
            pick = _pick(ctx.pgy, fc.tx_total, fc.last_tx)
        ctx.pgy.remove(pick)
        slots[TREATMENT] = [pick]
        fc.tx_total[pick] = fc.tx_total.get(pick, 0) + 1
        if ctx.wed_pm:
            fc.tx_wed_pm[pick] = fc.tx_wed_pm.get(pick, 0) + 1
        fc.last_tx[pick] = ctx.d
        log.append(f"{ctx.session} 治療室 ← PGY {pick}"
                   + ("（週三下午）" if ctx.wed_pm else ""))


class BiopsyStep(FillStep):
    def run(self, ctx, slots, log):
        # 週三下午切片室硬性關閉（C3 定案）→ 即使手動格網誤設為開，也不排。
        if not ctx.biopsy_open or ctx.wed_pm:
            return
        if not ctx.clerk:
            log.append(f"⚠ {ctx.session} 切片室開放但無 Clerk 可排")
            return
        fc = ctx.fc
        undone = [c for c in ctx.clerk if fc.biopsy_done.get(c, 0) == 0]
        pick = _pick(undone or ctx.clerk, fc.biopsy_done, fc.last_biopsy)
        ctx.clerk.remove(pick)
        slots[BIOPSY] = [pick]
        fc.biopsy_done[pick] = fc.biopsy_done.get(pick, 0) + 1
        fc.last_biopsy[pick] = ctx.d
        log.append(f"{ctx.session} 切片室 ← Clerk {pick}")


def _seat(ctx, pool, room):
    pick = _pick(pool, ctx.fc.seat, ctx.fc.last_seat)
    pool.remove(pick)
    ctx.room_slots[room].append(pick)
    ctx.fc.seat[pick] = ctx.fc.seat.get(pick, 0) + 1
    ctx.fc.last_seat[pick] = ctx.d
    return pick


class ClerkSeedStep(FillStep):
    def run(self, ctx, slots, log):
        for r in ctx.rooms:
            if not ctx.clerk:
                break
            _seat(ctx, ctx.clerk, r)


class PgyMixStep(FillStep):
    def run(self, ctx, slots, log):
        # (a) 優先補「已坐 1 位 Clerk」的診間第 2 位 → 形成 1C+1P 混搭
        #     （Clerk 少於診間數時，先配對再說，不先去佔空房）
        for r in ctx.rooms:
            if not ctx.pgy:
                return
            if len(ctx.room_slots[r]) == 1 < ctx.capacity:
                _seat(ctx, ctx.pgy, r)
        # (b) 再填空診間的第 1、2 位（PGY 只優先到第 2 位；第 3 位起留給 Clerk
        #     overflow — 見 §3.6 步驟 4/5）。無 Clerk 月即由此直填診間。
        for slot in range(min(ctx.capacity, 2)):
            for r in ctx.rooms:
                if not ctx.pgy:
                    return
                if len(ctx.room_slots[r]) == slot:
                    _seat(ctx, ctx.pgy, r)


class ClerkOverflowStep(FillStep):
    def run(self, ctx, slots, log):
        for r in ctx.rooms:
            while len(ctx.room_slots[r]) < ctx.capacity and ctx.clerk:
                _seat(ctx, ctx.clerk, r)


class RestStep(FillStep):
    def run(self, ctx, slots, log):
        rest_people = sorted(ctx.pgy + ctx.clerk)
        if not rest_people:
            return
        for p in rest_people:
            ctx.fc.rest[p] = ctx.fc.rest.get(p, 0) + 1
            ctx.fc.last_rest[p] = ctx.d
        slots[REST] = rest_people
        log.append(f"{ctx.session} 放假：{'、'.join(rest_people)}")


PIPELINE = [TreatmentStep(), BiopsyStep(), ClerkSeedStep(),
            PgyMixStep(), ClerkOverflowStep(), RestStep()]


def solve_session(d: date, session: str, rooms: list, pgy_avail: list,
                  clerk_avail: list, biopsy_open: bool, fc: FairCounters,
                  capacity: int = 2, pipeline=None) -> tuple:
    """單一時段填充 → (slots, log)。slots: {房/治療室/切片室/放假: [代號,...]}。"""
    ctx = SessionCtx(
        d=d, session=session, rooms=sorted(rooms),
        pgy=sorted(pgy_avail), clerk=sorted(clerk_avail),
        biopsy_open=biopsy_open, capacity=capacity, fc=fc,
        room_slots={r: [] for r in sorted(rooms)})
    slots: dict = {}
    log: list = []
    for step in (pipeline or PIPELINE):
        step.run(ctx, slots, log)
    for r in ctx.rooms:                          # 房間格（含空房不輸出）
        if ctx.room_slots[r]:
            slots[r] = ctx.room_slots[r]
    return slots, log


@dataclass
class DaySolveInput:
    ym: str
    grid: dict                    # {date: {session: [rooms]}}（clinic_grid.month_grid）
    pgy_roster: list              # 該月 PGY 代號
    clerk_roster: list            # 該月 Clerk 代號（無 Clerk 月＝[]）
    biopsy_open: dict = field(default_factory=dict)   # {iso: {session: bool}}
    leaves: dict = field(default_factory=dict)        # {"pgy":{c:set},"clerk":{c:set}}
    capacity: int = 2


def _avail(roster: list, leave_map: dict, d: date) -> list:
    return sorted(p for p in roster if d not in (leave_map.get(p) or set()))


def month_solve_day(inp: DaySolveInput) -> tuple:
    """整月逐（工作日×早/午）填充 → (day_slots, log, warnings)。

    day_slots: {iso: {session: {slot: [代號]}}}；warnings: 人話警告清單。
    治療室每個非假日工作日的每個時段都需 1 PGY（含週三下午，即使跟診關閉）。
    """
    fc = FairCounters()
    day_slots: dict = {}
    log: list = []
    warnings: list = []
    pgy_leave = (inp.leaves.get("pgy") or {})
    clerk_leave = (inp.leaves.get("clerk") or {})

    for d in sorted(inp.grid):
        if is_weekend(d):
            continue
        iso = d.isoformat()
        for session in STUDENT_SESSIONS:
            rooms = (inp.grid.get(d) or {}).get(session) or []
            # 週三下午跟診關閉但治療室照開 → 該時段仍需跑（rooms 為 []）
            biopsy = bool((inp.biopsy_open.get(iso) or {}).get(session))
            slots, slog = solve_session(
                d, session, rooms,
                _avail(inp.pgy_roster, pgy_leave, d),
                _avail(inp.clerk_roster, clerk_leave, d),
                biopsy, fc, inp.capacity)
            day_slots.setdefault(iso, {})[session] = slots
            log.append(f"{d.month}/{d.day}({'一二三四五六日'[d.weekday()]}) "
                       + "；".join(slog))
            warnings.extend(f"{d.month}/{d.day} {ln.lstrip('⚠ ')}"
                            for ln in slog if ln.startswith("⚠"))

    # 切片室輪不到：本梯（此處以整月近似）仍 biopsy_done==0 的 Clerk
    if inp.clerk_roster:
        missed = [c for c in sorted(inp.clerk_roster)
                  if fc.biopsy_done.get(c, 0) == 0]
        if missed:
            warnings.append("切片室輪不到（本月內未排到）：" + "、".join(missed))
    return day_slots, log, warnings
