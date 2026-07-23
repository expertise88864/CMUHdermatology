# -*- coding: utf-8 -*-
"""PGY/Clerk 逐時段填充器（設計文件 §3.6；純函式、決定性）。

每時段輸入：跟診診間(房號升冪)、可用 PGY、可用 Clerk、診間容量、切片室是否開。
七步驟（各為一個可替換 FillStep，順序 = PIPELINE）：
  1 照光Step     ← 1 位 PGY（**每個時段一律要 1 位**，含週三下午；最優先；照光總次數
                  最少者，週三下午另計 photo_wed_pm 公平）
  2 治療室Step   ← 1 位 PGY（**週三下午休診不排**；其餘時段皆排；治療室總次數最少者）
  3 切片室Step   ← 1 位 Clerk（僅切片室開；優先本梯未輪過切片者）
  4 ClerkSeed    每個開診診間各放 1 位 Clerk（房號序、就座公平輪轉）
  5 PgyMix       逐欄補 PGY（先補到「有 1 人的診間」形成 1C+1P；無 Clerk 月直接填診）
  6 ClerkOverflow 剩 Clerk 補進剩餘容量
  7 RestStep     還沒位子 → 放假（放假次數輪平均）

先照光、再治療室，兩者各消耗 1 位 PGY，剩餘 PGY 才與 Clerk 一起進診間。
決定性鐵律：一切輪選用 key=(次數, 決定性抖動, 代號) 取最小；抖動＝crc32(日期|時段|
用途|代號)——同輸入恆同結果（可重跑重現），但逐日/逐時段變化 → 平手時打散，不會
鎖死「同人固定同時段」的節拍（見 _jitter）。
不硬塞：照光/治療室無 PGY、切片室開但無 Clerk → 記警告，不填（貪婪填充器無法硬性
保證滿足，缺人時以警告呈現）。
"""
from __future__ import annotations

import zlib
from dataclasses import dataclass, field
from datetime import date

from cmuh_common.roster.model import STUDENT_SESSIONS, is_weekend

PHOTO = "照光"        # 每時段必排 1 PGY（含週三下午），最優先
TREATMENT = "治療室"  # 每時段 1 PGY，但週三下午休診不排
BIOPSY = "切片室"
REST = "放假"
WED = 2

# [2026-07-23 使用者] 「Apply 本科」PGY 優先偏好：勾選的 PGY（至多 2 位）在
# 週二/週五（weekday 1/4）早午的 101 診跟診「優先安排」。公平最優先——偏好只做
# 座位輪選的【平手決勝】（排在座位次數之後、抖動之前），次數落後者永遠先補，
# 整月跟診次數 spread ≤1 性質不變；請假者本來就不在候選內。
APPLY_PREF_ROOM = "101"
APPLY_PREF_WEEKDAYS = (1, 4)          # 週二、週五（早午時段皆適用）


def _jitter(d: date, session: str, purpose: str, code: str) -> int:
    """[2026-07-23 使用者] 決定性抖動，取代舊的「最久沒輪到(LRU)」平手決勝。

    LRU 在「每天早/午兩時段」的固定節拍下會形成穩定輪轉週期 → 鎖死成固定配對
    （實測：2 位 PGY 時 A 永遠早上照光、B 永遠下午）。改用 crc32(日期|時段|用途|代號)：
    主鍵仍是「次數」→ 整月每人次數照樣平均（spread ≤1 性質不變）；平手時逐日/逐時段
    亂序打散，誰排早誰排午不再固定。非真亂數（不用 random/時鐘），同輸入恆同結果，
    決定性鐵律不破（重排/回放/測試皆可重現）。"""
    return zlib.crc32(f"{d.isoformat()}|{session}|{purpose}|{code}"
                      .encode("utf-8"))


def _pick(ctx: "SessionCtx", cands: list, count_map: dict, purpose: str):
    """公平輪選：次數最少 → 決定性抖動 → 代號字典序（決定性；見 _jitter）。"""
    return min(cands, key=lambda p: (count_map.get(p, 0),
                                     _jitter(ctx.d, ctx.session, purpose, p), p))


@dataclass
class FairCounters:
    photo_total: dict = field(default_factory=dict)  # PGY 照光總次數
    photo_wed_pm: dict = field(default_factory=dict)  # PGY 週三下午照光次數
    tx_total: dict = field(default_factory=dict)     # PGY 治療室總次數
    rest: dict = field(default_factory=dict)         # 放假次數（PGY+Clerk）
    biopsy_done: dict = field(default_factory=dict)  # Clerk 本梯切片次數
    seat: dict = field(default_factory=dict)         # 診間就座次數（公平輪轉）
    # last_*：最近一次輪到日期。[2026-07-23] 輪選 key 已改用 _jitter 平手決勝（LRU 會
    # 鎖死固定配對），這些欄位保留作紀錄/回放資料，不再參與輪選。
    last_photo: dict = field(default_factory=dict)
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
    batch_key: str = ""               # 切片輪替以「梯次」為單位（代號跨梯會重用）
    apply_pref: frozenset = frozenset()   # Apply 本科 PGY（101 診週二/週五平手優先）

    @property
    def wed_pm(self) -> bool:
        return self.d.weekday() == WED and self.session == "下午"

    def room_pref(self, room) -> frozenset:
        """該房此時段的「平手優先」集合：僅 101 診且週二/週五時＝apply_pref，其餘空。"""
        if (str(room).strip() == APPLY_PREF_ROOM
                and self.d.weekday() in APPLY_PREF_WEEKDAYS):
            return self.apply_pref
        return frozenset()


class FillStep:
    def run(self, ctx: SessionCtx, slots: dict, log: list) -> None:  # noqa: ARG002
        raise NotImplementedError


class PhotoStep(FillStep):
    """照光：每個時段（含週三下午）一律排 1 位 PGY，最優先。"""

    def run(self, ctx, slots, log):
        if not ctx.pgy:
            log.append(f"⚠ {ctx.session} 照光無 PGY 可排（全請假？）")
            return
        fc = ctx.fc
        if ctx.wed_pm:                          # 週三下午：先比 photo_wed_pm 再比總次數
            pick = min(ctx.pgy, key=lambda p: (
                fc.photo_wed_pm.get(p, 0), fc.photo_total.get(p, 0),
                _jitter(ctx.d, ctx.session, "photo", p), p))
        else:
            pick = _pick(ctx, ctx.pgy, fc.photo_total, "photo")
        ctx.pgy.remove(pick)
        slots[PHOTO] = [pick]
        fc.photo_total[pick] = fc.photo_total.get(pick, 0) + 1
        if ctx.wed_pm:
            fc.photo_wed_pm[pick] = fc.photo_wed_pm.get(pick, 0) + 1
        fc.last_photo[pick] = ctx.d
        log.append(f"{ctx.session} 照光 ← PGY {pick}"
                   + ("（週三下午）" if ctx.wed_pm else ""))


class TreatmentStep(FillStep):
    """治療室：除週三下午（休診）外，每個時段排 1 位 PGY（照光之後、進診間之前）。"""

    def run(self, ctx, slots, log):
        if ctx.wed_pm:                          # 週三下午治療室休診（照光另開）
            return
        if not ctx.pgy:
            log.append(f"⚠ {ctx.session} 治療室無 PGY 可排（全請假？）")
            return
        fc = ctx.fc
        pick = _pick(ctx, ctx.pgy, fc.tx_total, "tx")
        ctx.pgy.remove(pick)
        slots[TREATMENT] = [pick]
        fc.tx_total[pick] = fc.tx_total.get(pick, 0) + 1
        fc.last_tx[pick] = ctx.d
        log.append(f"{ctx.session} 治療室 ← PGY {pick}")


class BiopsyStep(FillStep):
    def run(self, ctx, slots, log):
        # 週三下午切片室硬性關閉（C3 定案）→ 即使手動格網誤設為開，也不排。
        if not ctx.biopsy_open or ctx.wed_pm:
            return
        if not ctx.clerk:
            log.append(f"⚠ {ctx.session} 切片室開放但無 Clerk 可排")
            return
        fc = ctx.fc
        bk = ctx.batch_key

        def key(c):
            return (fc.biopsy_done.get((bk, c), 0),
                    _jitter(ctx.d, ctx.session, "biopsy", c), c)
        undone = [c for c in ctx.clerk if fc.biopsy_done.get((bk, c), 0) == 0]
        pick = min(undone or ctx.clerk, key=key)         # 本梯未輪過者優先
        ctx.clerk.remove(pick)
        slots[BIOPSY] = [pick]
        fc.biopsy_done[(bk, pick)] = fc.biopsy_done.get((bk, pick), 0) + 1
        fc.last_biopsy[(bk, pick)] = ctx.d
        log.append(f"{ctx.session} 切片室 ← Clerk {pick}")


def _pgy_ck(ctx, p):
    return ("pgy", p)                    # PGY 代號整月穩定 → 全月共用


def _clerk_ck(ctx, c):
    return ("clerk", ctx.batch_key, c)   # Clerk 代號跨梯會重用 → 依梯次命名空間


def _seat(ctx, pool, room, ck, prefer: frozenset = frozenset()):
    """依 ck(人)命名空間的座位公平計數輪選並就座。

    key＝(座位次數, 非偏好者, 抖動, 代號)：次數最少者恆優先（公平第一）；次數平手時
    prefer 集合內的人先上（Apply 本科 101 診週二/週五）；再平手才由決定性抖動打散。"""
    pick = min(pool, key=lambda p: (ctx.fc.seat.get(ck(ctx, p), 0),
                                    0 if p in prefer else 1,
                                    _jitter(ctx.d, ctx.session, "seat", p), p))
    pool.remove(pick)
    ctx.room_slots[room].append(pick)
    k = ck(ctx, pick)
    ctx.fc.seat[k] = ctx.fc.seat.get(k, 0) + 1
    ctx.fc.last_seat[k] = ctx.d
    return pick


class ClerkSeedStep(FillStep):
    def run(self, ctx, slots, log):
        for r in ctx.rooms:
            if not ctx.clerk:
                break
            _seat(ctx, ctx.clerk, r, _clerk_ck)


class PgyMixStep(FillStep):
    def run(self, ctx, slots, log):
        # (a) 優先補「已坐 1 位 Clerk」的診間第 2 位 → 形成 1C+1P 混搭
        #     （Clerk 少於診間數時，先配對再說，不先去佔空房）
        for r in ctx.rooms:
            if not ctx.pgy:
                return
            if len(ctx.room_slots[r]) == 1 < ctx.capacity:
                _seat(ctx, ctx.pgy, r, _pgy_ck, prefer=ctx.room_pref(r))
        # (b) 再填空診間的第 1、2 位（PGY 只優先到第 2 位；第 3 位起留給 Clerk
        #     overflow — 見 §3.6 步驟 4/5）。無 Clerk 月即由此直填診間。
        for slot in range(min(ctx.capacity, 2)):
            for r in ctx.rooms:
                if not ctx.pgy:
                    return
                if len(ctx.room_slots[r]) == slot:
                    _seat(ctx, ctx.pgy, r, _pgy_ck, prefer=ctx.room_pref(r))


class ClerkOverflowStep(FillStep):
    def run(self, ctx, slots, log):
        for r in ctx.rooms:
            while len(ctx.room_slots[r]) < ctx.capacity and ctx.clerk:
                _seat(ctx, ctx.clerk, r, _clerk_ck)


class RestStep(FillStep):
    def run(self, ctx, slots, log):
        rest_people = sorted(ctx.pgy + ctx.clerk)
        if not rest_people:
            return
        for p in ctx.pgy:                              # 放假計數同樣分命名空間
            k = _pgy_ck(ctx, p)
            ctx.fc.rest[k] = ctx.fc.rest.get(k, 0) + 1
            ctx.fc.last_rest[k] = ctx.d
        for c in ctx.clerk:
            k = _clerk_ck(ctx, c)
            ctx.fc.rest[k] = ctx.fc.rest.get(k, 0) + 1
            ctx.fc.last_rest[k] = ctx.d
        slots[REST] = rest_people
        log.append(f"{ctx.session} 放假：{'、'.join(rest_people)}")


PIPELINE = [PhotoStep(), TreatmentStep(), BiopsyStep(), ClerkSeedStep(),
            PgyMixStep(), ClerkOverflowStep(), RestStep()]


def solve_session(d: date, session: str, rooms: list, pgy_avail: list,
                  clerk_avail: list, biopsy_open: bool, fc: FairCounters,
                  capacity: int = 2, pipeline=None, batch_key: str = "",
                  apply_pref=frozenset()) -> tuple:
    """單一時段填充 → (slots, log)。slots: {房/治療室/切片室/放假: [代號,...]}。"""
    ctx = SessionCtx(
        d=d, session=session, rooms=sorted(rooms),
        pgy=sorted(pgy_avail), clerk=sorted(clerk_avail),
        biopsy_open=biopsy_open, capacity=capacity, fc=fc,
        room_slots={r: [] for r in sorted(rooms)}, batch_key=batch_key,
        apply_pref=frozenset(apply_pref))
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
    clerk_batches: list = field(default_factory=list)  # ClerkBatch 樣（.covers/.members/.id）
    biopsy_open: dict = field(default_factory=dict)   # {iso: {session: bool}}
    leaves: dict = field(default_factory=dict)        # {"pgy":{c:set},"clerk":{c:set}}
    capacity: int = 2
    locked: dict = field(default_factory=dict)        # {iso: {session: slots}} 鎖定不重排
    # RF-09 跨月梯次延續：上月屬某跨月梯次的既存 day_slots（只餵切片/clerk 公平計數）
    prior_sessions: dict = field(default_factory=dict)  # {iso: {session: slots}}
    prior_pgy: set = field(default_factory=set)          # 上月 PGY 代號（從 replay 剔除）
    apply_pref: set = field(default_factory=set)  # Apply 本科 PGY（101 週二/五平手優先）


def _avail(roster: list, leave_map: dict, d: date) -> list:
    return sorted(p for p in roster if d not in (leave_map.get(p) or set()))


def replay_counters(fc: FairCounters, d: date, session: str, slots: dict,
                    batch_key: str, pgy_set: set, clerk_set: set) -> None:
    """把「已鎖定/既存」時段結果餵進公平計數，讓後續未鎖時段對齊（不重新分配）。
    以名單分類 PGY/Clerk 命名空間（座位/放假）；治療室→tx、切片室→biopsy。"""
    wed_pm = (d.weekday() == WED and session == "下午")
    # 照光/治療室 key 是裸代號、PGY 代號整月穩定，stale key 不污染現役者 → 不過濾。
    for p in slots.get(PHOTO, []):
        fc.photo_total[p] = fc.photo_total.get(p, 0) + 1
        if wed_pm:
            fc.photo_wed_pm[p] = fc.photo_wed_pm.get(p, 0) + 1
        fc.last_photo[p] = d
    for p in slots.get(TREATMENT, []):
        fc.tx_total[p] = fc.tx_total.get(p, 0) + 1
        fc.last_tx[p] = d
    for c in slots.get(BIOPSY, []):
        if c not in clerk_set:            # RF-10：已換梯/非名單代號不污染切片命名空間
            continue
        k = (batch_key, c)
        fc.biopsy_done[k] = fc.biopsy_done.get(k, 0) + 1
        fc.last_biopsy[k] = d

    def _ck(p):
        return ("pgy", p) if p in pgy_set else ("clerk", batch_key, p)
    for slot, people in slots.items():
        if slot in (PHOTO, TREATMENT, BIOPSY):
            continue
        target = (fc.rest, fc.last_rest) if slot == REST else (fc.seat, fc.last_seat)
        for p in people:
            if p not in pgy_set and p not in clerk_set:
                continue                  # RF-10：未知代號不計座位/放假（不誤繼承）
            k = _ck(p)
            target[0][k] = target[0].get(k, 0) + 1
            target[1][k] = d


def _warn_locked_content(warnings: list, d: date, session: str, locked_slots: dict,
                         pgy_set: set, clerk_set: set,
                         pgy_leave: dict, clerk_leave: dict) -> None:
    """RF-10：鎖定內容原樣保留，但檢核當日請假者 / 非名單代號並人話警告（不改內容）。"""
    warned_leave: set = set()
    for slot_name, people in locked_slots.items():
        for p in people:
            if p not in pgy_set and p not in clerk_set:
                warnings.append(f"{d.month}/{d.day} {session} 🔒鎖定時段內 {p} "
                                f"不在本月 PGY 名單/當日梯次——請確認")
                continue
            if slot_name == REST or p in warned_leave:  # 放假不算衝突；同人只警告一次
                continue
            leave_set = pgy_leave.get(p) if p in pgy_set else clerk_leave.get(p)
            if leave_set and d in leave_set:
                warned_leave.add(p)
                warnings.append(f"{d.month}/{d.day} {session} 🔒鎖定時段內 {p} "
                                f"當日已請假，仍照鎖定排入——請確認或解鎖重排")


def month_solve_day(inp: DaySolveInput) -> tuple:
    """整月逐（工作日×早/午）填充 → (day_slots, log, warnings)。

    day_slots: {iso: {session: {slot: [代號]}}}；warnings: 人話警告清單。
    - 治療室每個非假日工作日每時段都需 1 PGY（含週三下午，即使跟診關閉）。
    - Clerk 逐日只取「當日所屬兩週梯次」的成員（跨梯不互相借人）。
    """
    fc = FairCounters()
    day_slots: dict = {}
    log: list = []
    warnings: list = []
    pgy_leave = (inp.leaves.get("pgy") or {})
    clerk_leave = (inp.leaves.get("clerk") or {})

    # RF-09：先把上月跨月梯次的既存班表餵進 fc（只餵切片室與 clerk 座位/放假；跳過
    # 治療室與上月 PGY，避免污染本月 PGY 月度公平），讓「本梯未輪過切片」的判定與月底
    # missed 警告都以「整梯」而非「本月」為單位。
    for iso in sorted(inp.prior_sessions):
        try:
            d = date.fromisoformat(iso)
        except (ValueError, TypeError):
            continue
        batch = next((b for b in inp.clerk_batches if b.covers(d)), None)
        if batch is None:
            continue
        members = set(batch.members)
        sessions = inp.prior_sessions[iso]
        for session in STUDENT_SESSIONS:
            slots = sessions.get(session)
            if not slots:
                continue
            filtered = {}
            for slot_name, people in slots.items():
                if slot_name in (PHOTO, TREATMENT):   # 照光/治療室屬 PGY 月度公平→不跨月餵
                    continue
                keep = [p for p in people
                        if p in members and p not in inp.prior_pgy]
                if keep:
                    filtered[slot_name] = keep
            if filtered:
                replay_counters(fc, d, session, filtered, batch.id,
                                pgy_set=set(), clerk_set=members)

    solved_batch_ids: set = set()
    overlap_days: dict = {}               # {(勝者id, 敗者id): [最早重疊日, 最晚重疊日]}
    for d in sorted(inp.grid):
        if is_weekend(d):
            continue
        iso = d.isoformat()
        # RF-08：同日可能被多個梯次涵蓋（設定允許同週一多梯、或起始日打錯部分重疊）。
        # 維持與原 next() 相同的決定性勝者＝原始順序第一個；其餘梯次成員該日不排，
        # 累積重疊區間於迴圈後一次示警（點名被忽略的梯次與實際重疊日期）。
        covering_today = [b for b in inp.clerk_batches if b.covers(d)]
        batch = covering_today[0] if covering_today else None
        for loser in covering_today[1:]:
            rng = overlap_days.setdefault((batch.id, loser.id), [d, d])
            rng[0], rng[1] = min(rng[0], d), max(rng[1], d)
        clerk_members = batch.members if batch else []
        batch_key = batch.id if batch else ""
        if batch:
            solved_batch_ids.add(batch.id)
        pgy_set, clerk_set = set(inp.pgy_roster), set(clerk_members)
        for session in STUDENT_SESSIONS:
            locked_slots = (inp.locked.get(iso) or {}).get(session)
            if locked_slots is not None:          # 鎖定時段：保留原樣、只餵進計數
                day_slots.setdefault(iso, {})[session] = locked_slots
                _warn_locked_content(warnings, d, session, locked_slots,
                                     pgy_set, clerk_set, pgy_leave, clerk_leave)
                replay_counters(fc, d, session, locked_slots, batch_key,
                                pgy_set, clerk_set)
                log.append(f"{d.month}/{d.day}({'一二三四五六日'[d.weekday()]}) "
                           f"{session} 🔒鎖定（不重排）")
                continue
            rooms = (inp.grid.get(d) or {}).get(session) or []
            # 週三下午跟診關閉但治療室照開 → 該時段仍需跑（rooms 為 []）
            biopsy = bool((inp.biopsy_open.get(iso) or {}).get(session))
            slots, slog = solve_session(
                d, session, rooms,
                _avail(inp.pgy_roster, pgy_leave, d),
                _avail(clerk_members, clerk_leave, d),
                biopsy, fc, inp.capacity, batch_key=batch_key,
                apply_pref=inp.apply_pref)
            day_slots.setdefault(iso, {})[session] = slots
            log.append(f"{d.month}/{d.day}({'一二三四五六日'[d.weekday()]}) "
                       + "；".join(slog))
            warnings.extend(f"{d.month}/{d.day} {ln.lstrip('⚠ ')}"
                            for ln in slog if ln.startswith("⚠"))

    # RF-02：鎖定時段的日期若事後掉出開診格網（假日/週末），主迴圈迭代不到 → 在此
    # 一律原樣補回輸出並人話警告，絕不因格網變動而無聲刪除鎖定內容（不餵計數：該日
    # 實際休診，餵計數會扭曲治療室/放假公平輪轉）。
    for iso, sessions in sorted(inp.locked.items()):
        for session, slots in sessions.items():
            if session not in day_slots.get(iso, {}):
                day_slots.setdefault(iso, {})[session] = slots
                warnings.append(f"{iso} {session} 🔒鎖定時段不在本月開診格網"
                                f"（假日/週末？），已原樣保留，請確認是否解鎖")
                log.append(f"{iso} {session} 🔒鎖定時段不在開診格網，原樣保留")

    # RF-08：梯次重疊 → 點名被忽略的梯次與實際重疊日期（協助定位打錯的起始日）。
    for (win, lose), (d1, d2) in sorted(overlap_days.items()):
        warnings.append(
            f"梯次重疊：{d1.isoformat()}～{d2.isoformat()} 由梯次 {win} 與 {lose} "
            f"同時涵蓋，重疊日只採 {win}，{lose} 成員該期間不會被排班——請修正梯次起始日")

    # 切片室輪不到：只對「本月確有工作日被排」的梯次示警（否則邊界梯次會誤報）
    for b in inp.clerk_batches:
        if b.id not in solved_batch_ids:
            continue
        missed = [c for c in sorted(b.members)
                  if fc.biopsy_done.get((b.id, c), 0) == 0]
        if missed:
            warnings.append(f"切片室輪不到（梯次 {b.id}，本梯內未排到）："
                            + "、".join(missed))
    return day_slots, log, warnings


# ─── 週期統計（2026-07-23 使用者需求）────────────────────────────────────────
# 排班排出來之後，統計整個 course（PGY=月、Clerk=兩週梯次）每人的各類次數，
# 給 UI 側欄/報告呈現，讓「照光/治療室盡量一致、週三下午照光獨立平均、Clerk 至少
# 跟過一次切片」可被使用者直接驗證。純函式：吃 day_slots 形狀資料（含手動改過的格）。
STAT_KEYS = ("photo", "photo_wed_pm", "tx", "biopsy", "follow", "rest")


def person_course_stats(sessions_by_iso: dict, include=None,
                        start: "date | None" = None,
                        end: "date | None" = None) -> dict:
    """統計每人次數 → {code: {photo, photo_wed_pm, tx, biopsy, follow, rest}}。

    sessions_by_iso: {iso: {session: {slot: [codes]}}}（月檔 day_slots 或 preview）。
    include: 只統計這些代號（None=全部）；start/end: 只統計此日期範圍（含端點，
    Clerk 梯次跨月時由呼叫端把兩個月的 day_slots 合併餵入並以梯次起訖裁切）。
    slot 分類：照光/治療室/切片室/放假為特殊格；其餘一律視為「跟診」（房號可為任意字串）。
    壞日期鍵略過（與 storage 讀取容錯一致）。
    """
    out: dict = {}

    def bump(code, key):
        if include is not None and code not in include:
            return
        st = out.setdefault(code, dict.fromkeys(STAT_KEYS, 0))
        st[key] += 1

    for iso in sorted(sessions_by_iso or {}):
        try:
            d = date.fromisoformat(iso)
        except (ValueError, TypeError):
            continue
        if (start and d < start) or (end and d > end):
            continue
        for session, slots in (sessions_by_iso[iso] or {}).items():
            wed_pm = (d.weekday() == WED and session == "下午")
            for slot, people in (slots or {}).items():
                for p in people or []:
                    if slot == PHOTO:
                        bump(p, "photo")
                        if wed_pm:
                            bump(p, "photo_wed_pm")
                    elif slot == TREATMENT:
                        bump(p, "tx")
                    elif slot == BIOPSY:
                        bump(p, "biopsy")
                    elif slot == REST:
                        bump(p, "rest")
                    else:
                        bump(p, "follow")
    return out


def format_course_stats(pgy_stats: dict, pgy_roster: list,
                        batch_stats: list) -> str:
    """把週期統計排成 monospace 文字段（給決策報告/預覽）。

    pgy_stats: person_course_stats 結果；pgy_roster: 本月 PGY 代號（沒排到也列 0）。
    batch_stats: [{"id","start","end","members","stats"}]（每梯一筆，跨月已合併）。
    """
    lines = ["【週期次數統計】",
             "  PGY（本月）：  照光  週三午照  治療室  跟診  放假"]
    for c in sorted({*pgy_roster, *pgy_stats}):
        st = pgy_stats.get(c) or dict.fromkeys(STAT_KEYS, 0)
        lines.append(f"    {c:<8s}  {st['photo']:>3d}  {st['photo_wed_pm']:>6d}"
                     f"  {st['tx']:>5d}  {st['follow']:>3d}  {st['rest']:>3d}")
    for b in batch_stats:
        lines.append(f"  Clerk 梯次 {b['id']}（{b['start']}～{b['end']}）："
                     f"切片  跟診  放假")
        for c in sorted({*b.get("members", []), *b["stats"]}):
            st = b["stats"].get(c) or dict.fromkeys(STAT_KEYS, 0)
            mark = "  ⚠未排切片" if st["biopsy"] == 0 else ""
            lines.append(f"    {c:<8s}  {st['biopsy']:>3d}  {st['follow']:>3d}"
                         f"  {st['rest']:>3d}{mark}")
    if not batch_stats:
        lines.append("  Clerk：本月無梯次")
    return "\n".join(lines)
