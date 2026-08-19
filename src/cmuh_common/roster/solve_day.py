# -*- coding: utf-8 -*-
"""PGY/Clerk 逐時段填充器（設計文件 §3.6；純函式、決定性）。

每時段輸入：跟診診間(房號升冪)、可用 PGY、可用 Clerk、診間容量、切片室是否開。
七步驟（各為一個可替換 FillStep，順序 = PIPELINE）：
  1 照光Step     ← 1 位 PGY（**每個時段一律要 1 位**，含週三下午；最優先；照光總次數
                  最少者，週三下午另計 photo_wed_pm 公平）
  2 治療室Step   ← 1 位 PGY（**週三下午休診不排**；其餘時段皆排；治療室總次數最少者）
  3 切片室Step   ← 1 位 Clerk（僅切片室開；[2026-07-24 修訂] 開放就排好排滿，
                  本梯切片次數最少者優先＝每人至少一次、次數差 ≤1、同日早午不連切）
  4 ClerkSeed    每個開診診間各放 1 位 Clerk（房序=決定性洗牌、就座公平輪轉）
  5 PgyMix       逐欄補 PGY（先補到「有 1 人的診間」形成 1C+1P；無 Clerk 月直接填診）
  6 ClerkOverflow 剩 Clerk 補進剩餘容量
  [2026-07-24 使用者] 跟診房多樣性：就座輪選在「總次數/Apply偏好」之後加比
  「跟過這間診的次數」(少者先)與「上次就是這間」懲罰(反連排)；診間處理順序改
  決定性抖動洗牌(原固定房號升冪→人少於房時永遠只填低房號,學生從跟不到 103/105)
  ——被填的房與 1C+1P 配對組合逐日變化,一起跟診的人自然錯開。
  7 RestStep     還沒位子 → 放假（★純殘量，不做平均；理由見下★）

★放假次數【不】做平均，而且做不到★（2026-08-03 使用者實測提問後更正）
  本行原本寫「放假次數輪平均」—— 那是【錯的】：`fc.rest` / `fc.last_rest`
  從頭到尾只被 RestStep 寫入，三支選人 key（照光、治療室、就座）都沒有讀它。
  更重要的是，就算補上也達不到目的，因為：

      放假 = 可排時段 − 工作時段

  每個時段的工作名額是固定的（照光 1、治療室 1、診間 = 房數 × 容量），
  而照光/治療室/跟診三者各自都已按次數平均。於是【工作次數平均】與
  【放假次數平均】在「每人可排時段不同」時是互斥的：工作攤平之後，
  放假必然跟著可排時段走 —— 在場越久的人，剩下沒位子的時段就越多。

  實例（2026-08，21 個平日 = 42 個半天）：
      A 照光11+治療9+跟診16+放假6 = 42 ← 全月零請假，滿額在場
      B 11+10+15+4 = 40    C 10+10+14+4 = 38    D 10+9+13+4 = 36
  A 的放假比別人多 2，正是因為他比 D 多在 6 個半天，而多出來的名額有限。
  要讓放假數字齊頭，就得把工作從 A 挪給請假多的人 —— 那會破壞照光/治療的
  平均（使用者要的正是那一項）。★所以這是刻意不做，不是漏做。★

先照光、再治療室，兩者各消耗 1 位 PGY，剩餘 PGY 才與 Clerk 一起進診間。
決定性鐵律：一切輪選用 key=(次數, 決定性抖動, 代號) 取最小；抖動＝crc32(日期|時段|
用途|代號)——同輸入恆同結果（可重跑重現），但逐日/逐時段變化 → 平手時打散，不會
鎖死「同人固定同時段」的節拍（見 _jitter）。
不硬塞：照光/治療室無 PGY → 記警告，不填（貪婪填充器無法硬性保證滿足，缺人時
以警告呈現）；切片室當日全體已切/全請假 → 靜默留空（同日不重複是規則，非異常）
——整梯輪不到或次數不均由月底警告點名。
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


def _idle_today(fc: "FairCounters", k, d: date) -> int:
    """[2026-07-27 使用者] 0＝今天還沒有任何工作（優先給事做）、1＝今天已有工作。

    早上時段人人皆 0（本日尚未開排）→ 此項不生效，等同舊行為；下午才真正分道，
    讓「早上放假的人」優先補上位子，避免整天放假。純函式。"""
    return 0 if fc.worked_day.get(k) != d else 1


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
    # [2026-07-24 使用者] 跟診房多樣性：每人×每房次數（盡量輪過各診,不固定跟同房）
    seat_room: dict = field(default_factory=dict)    # {(ck, 房): 次數}
    last_seat_room: dict = field(default_factory=dict)  # {ck: 房} 上次跟的房(反連排)
    # [2026-07-25 使用者] 同伴多樣性：兩人同一診間共事次數（避免「1、2 號永遠一起
    # 跟 101」——房多樣性只管「誰跟哪一間」,管不到「誰跟誰」）。鍵=兩人 ck 排序後的
    # tuple,故與人員命名空間一致（Clerk 跨梯不互相繼承）。
    pair: dict = field(default_factory=dict)         # {(ck_a, ck_b): 共事次數}
    # [2026-07-27 使用者] 反「整天放假」：記錄每人最近一次【有工作】的日期
    # （照光/治療室/切片室/跟診皆算）。下午輪選時「今天還沒有任何工作」者優先，
    # 讓每人每天盡量至少有半天有事做,而不是早上放假下午又放假。
    worked_day: dict = field(default_factory=dict)   # {ck: 最近有工作的日期}
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
    # [codex R1] 房內每位已就座者的【真實 ck】(角色感知)。同伴計數不可用「本次候選池
    # 的 ck resolver」去推斷房內既有者——PgyMixStep 進場時房裡坐的是 Clerk,用 _pgy_ck
    # 推會標成 ("pgy", 代號),與 replay_counters 的 ("clerk", 梯次, 代號) 永遠對不上
    # (代號跨梯重用時還會誤繼承別梯的配對史)。
    seat_ck: dict = field(default_factory=dict)
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
        fc.worked_day[_pgy_ck(ctx, pick)] = ctx.d      # 反整天放假：今日已有工作
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
        fc.worked_day[_pgy_ck(ctx, pick)] = ctx.d      # 反整天放假：今日已有工作
        log.append(f"{ctx.session} 治療室 ← PGY {pick}")


class BiopsyStep(FillStep):
    def run(self, ctx, slots, log):
        # 週三下午切片室硬性關閉（C3 定案）→ 即使手動格網誤設為開，也不排。
        if not ctx.biopsy_open or ctx.wed_pm:
            return
        fc = ctx.fc
        bk = ctx.batch_key
        # [2026-07-24 使用者·修訂] 切片室開放就【排好排滿】：每人整梯至少一次、
        # 不限一次（2、3 次都行），但同梯次數要一樣 → key 以「本梯切片次數最少者
        # 優先」輪選（min-first 天生保證 spread ≤1，且所有人輪過一遍前不會有人
        # 排到第二次＝at-least-once 自動達成）。舊版「每人一次就好、之後留空」
        # 造成切片室大量空著、Clerk 卻在放假（使用者附圖）→ 廢除。
        # 放假是最後一步（RestStep）：切片/診間都填完剩下的人才放假。
        # 同日早+午不得同一人（次數平手時早上切過者仍可能中選）→ 明確排除。
        cands = [c for c in ctx.clerk if fc.last_biopsy.get((bk, c)) != ctx.d]
        if not cands:
            return
        # 次數公平第一（整梯次數差 ≤1）；[2026-07-27] 平手時「今天還沒事做」者先，
        # 助攻反整天放假（不動搖切片次數公平）。
        pick = min(cands, key=lambda c: (
            fc.biopsy_done.get((bk, c), 0),
            _idle_today(fc, _clerk_ck(ctx, c), ctx.d),
            _jitter(ctx.d, ctx.session, "biopsy", c), c))
        ctx.clerk.remove(pick)
        slots[BIOPSY] = [pick]
        fc.biopsy_done[(bk, pick)] = fc.biopsy_done.get((bk, pick), 0) + 1
        fc.last_biopsy[(bk, pick)] = ctx.d
        fc.worked_day[_clerk_ck(ctx, pick)] = ctx.d    # 反整天放假：今日已有工作
        log.append(f"{ctx.session} 切片室 ← Clerk {pick}")


def _pgy_ck(ctx, p):
    return ("pgy", p)                    # PGY 代號整月穩定 → 全月共用


def _clerk_ck(ctx, c):
    return ("clerk", ctx.batch_key, c)   # Clerk 代號跨梯會重用 → 依梯次命名空間


def _pair_key(a, b) -> tuple:
    """同伴計數的對稱鍵（兩人 ck 排序）——(A,B) 與 (B,A) 視為同一組。"""
    sa, sb = str(a), str(b)
    return (sa, sb) if sa <= sb else (sb, sa)


def _seat(ctx, pool, room, ck, prefer: frozenset = frozenset()):
    """依 ck(人)命名空間的座位公平計數輪選並就座。

    key＝(今日尚無工作, 座位次數, 非偏好者, 該房次數, 連排懲罰, 同伴次數,
    抖動, 代號)：
    [2026-07-27 使用者] 首鍵＝「今天還沒有任何工作的人最優先」——目標是每人每天
    至少有半天有事做,不要早上放假下午又放假。放在最前面才有效（放在次數之後等於
    幾乎不生效：早上放假的人座位次數本來就較低,原本就會被選,失效的正是「次數偏高
    但整天閒著」這個真正要救的情形）。不會破壞長期公平：早上放假者當日至多補到 1
    個位子,仍少於整天有課者的 2 個,座位次數自我校正（實測 spread 仍 ≤1）。
    早上時段人人皆「今日尚無工作」→ 此鍵在上午不生效,上午行為與舊版相同。
    次之總次數最少者優先（公平）；平手時 prefer 先上（Apply 本科 101 週二/五）；
    [2026-07-24] 再比「跟過這間診的次數」少者先、罰「上一次跟診就是這間」（反連排）；
    [2026-07-25 使用者] 再比「與本房已就座者共事過幾次」少者先——房多樣性只管
    「誰跟哪一間」,管不到「誰跟誰」,故仍可能固定同兩人成對（如 1、2 號總是一起）。
    最後決定性抖動打散。"""
    rk = str(room).strip()
    fc = ctx.fc
    seated = ctx.seat_ck.setdefault(room, [])   # [codex R1] 房內既有者的真實 ck

    def _key(p):
        k = ck(ctx, p)
        pair_cost = sum(fc.pair.get(_pair_key(k, q), 0) for q in seated)
        return (_idle_today(fc, k, ctx.d),   # ★今天還沒事做的人最優先（反整天放假）
                fc.seat.get(k, 0),
                0 if p in prefer else 1,
                fc.seat_room.get((k, rk), 0),
                1 if fc.last_seat_room.get(k) == rk else 0,
                pair_cost,
                _jitter(ctx.d, ctx.session, "seat", p), p)
    pick = min(pool, key=_key)
    pool.remove(pick)
    k = ck(ctx, pick)
    for q in seated:                       # 與本房已就座者各記一次共事
        pk = _pair_key(k, q)
        fc.pair[pk] = fc.pair.get(pk, 0) + 1
    ctx.room_slots[room].append(pick)
    seated.append(k)                       # 保存真實 ck 供後續同房者比對
    fc.seat[k] = fc.seat.get(k, 0) + 1
    fc.seat_room[(k, rk)] = fc.seat_room.get((k, rk), 0) + 1
    fc.last_seat_room[k] = rk
    fc.last_seat[k] = ctx.d
    fc.worked_day[k] = ctx.d               # 反整天放假：今日已有工作
    return pick


def _room_order(ctx, pref_first: bool = False) -> list:
    """[2026-07-24 使用者] 診間處理順序＝決定性抖動洗牌（逐日/逐時段變化）。

    原固定房號升冪：學生少於診間數時永遠只填低房號（都跟 101/102,從輪不到
    103/105）,且 1C+1P 配對房固定 → 改洗牌後被填的房與配對組合天天不同。
    pref_first：Apply 本科生效日（週二/五且有勾選者）把 101 提到最前,
    偏好者的平手決勝不會先被洗到前面的別房消耗掉。"""
    rooms = sorted(ctx.rooms, key=lambda r: (
        _jitter(ctx.d, ctx.session, "roomorder", str(r)), str(r)))
    if (pref_first and ctx.apply_pref
            and ctx.d.weekday() in APPLY_PREF_WEEKDAYS):
        rooms.sort(key=lambda r: 0 if str(r).strip() == APPLY_PREF_ROOM else 1)
    return rooms


class ClerkSeedStep(FillStep):
    def run(self, ctx, slots, log):
        for r in _room_order(ctx):
            if not ctx.clerk:
                break
            _seat(ctx, ctx.clerk, r, _clerk_ck)


class PgyMixStep(FillStep):
    def run(self, ctx, slots, log):
        rooms = _room_order(ctx, pref_first=True)
        # (a) 優先補「已坐 1 位 Clerk」的診間第 2 位 → 形成 1C+1P 混搭
        #     （Clerk 少於診間數時，先配對再說，不先去佔空房）
        for r in rooms:
            if not ctx.pgy:
                return
            if len(ctx.room_slots[r]) == 1 < ctx.capacity:
                _seat(ctx, ctx.pgy, r, _pgy_ck, prefer=ctx.room_pref(r))
        # (b) 再填空診間的第 1、2 位（PGY 只優先到第 2 位；第 3 位起留給 Clerk
        #     overflow — 見 §3.6 步驟 4/5）。無 Clerk 月即由此直填診間。
        for slot in range(min(ctx.capacity, 2)):
            for r in rooms:
                if not ctx.pgy:
                    return
                if len(ctx.room_slots[r]) == slot:
                    _seat(ctx, ctx.pgy, r, _pgy_ck, prefer=ctx.room_pref(r))


class ClerkOverflowStep(FillStep):
    def run(self, ctx, slots, log):
        for r in _room_order(ctx):
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


def day_input_fingerprint(inp: "DaySolveInput") -> str:
    """這一次求解【吃到的全部輸入】的識別(見 `roster.fingerprint`)。

    ★逐欄列舉會腐爛,所以走 dataclass 的欄位本身★(外審排班第 1 輪 P1-02):
    PGY/Clerk 的求解結果在預覽視窗裡可能停留很久,期間他機同步進來的請假、
    Clerk 梯次、停診、門診模板、PGY 名單、已鎖定時段…… 任何一項變了,舊解
    再套用下去就是把最新狀態整批蓋掉(請假的人又被排上、剛停診的診間又有人)。
    ★正規化的實作只有一份★(`roster.fingerprint`):R/VS 那一側用的是同一個
    函式 —— 兩邊各寫一套的話,遲早只有一邊被修好。
    """
    from cmuh_common.roster.fingerprint import input_fingerprint  # noqa: PLC0415
    return input_fingerprint(inp)


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
    _room_slots = {s: ps for s, ps in slots.items()
                   if s not in (PHOTO, TREATMENT, BIOPSY, REST)}
    _working = {p for ps in _room_slots.values() for p in ps}
    _working |= {p for s in (PHOTO, TREATMENT, BIOPSY) for p in slots.get(s, [])}
    # [2026-07-27 使用者] 反整天放假的「今日已有工作」也要回放：早上鎖定/既存格
    # 裡有工作的人，下午自動排班時不得再被當成「今天閒著」而優先補位。
    for p in _working:
        if p in pgy_set or p in clerk_set:
            fc.worked_day[_ck(p)] = d
    for slot, people in slots.items():
        if slot in (PHOTO, TREATMENT, BIOPSY):
            continue
        for p in people:
            if p not in pgy_set and p not in clerk_set:
                continue                  # RF-10：未知代號不計座位/放假（不誤繼承）
            k = _ck(p)
            if slot == REST:
                fc.rest[k] = fc.rest.get(k, 0) + 1
                fc.last_rest[k] = d
            else:                         # 跟診：連同房多樣性計數一起回放
                fc.seat[k] = fc.seat.get(k, 0) + 1
                fc.last_seat[k] = d
                rk = str(slot).strip()
                fc.seat_room[(k, rk)] = fc.seat_room.get((k, rk), 0) + 1
                fc.last_seat_room[k] = rk
    # [2026-07-25] 同伴計數同樣回放：鎖定/跨月既存格若不算共事次數,後續未鎖時段
    # 會以為這兩人沒配過而繼續把他們湊在一起。同房內兩兩各記一次。
    # （獨立一圈跑,不可併進上面的 for——併進去會逐房重跑而重複計數。）
    for slot, people in slots.items():
        if slot in (PHOTO, TREATMENT, BIOPSY, REST):
            continue
        known = [p for p in people if p in pgy_set or p in clerk_set]
        for i, a in enumerate(known):
            for b in known[i + 1:]:
                pk = _pair_key(_ck(a), _ck(b))
                fc.pair[pk] = fc.pair.get(pk, 0) + 1


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

    ★[使用者定案 2026-08-02] 週三下午照光的「補半天假」機制已整個取消★
    原本：週三下午場次無法整除 PGY 人數時，多值的人記一筆欠假，之後在有空檔的時段
    優先放假抵銷；為了讓月底才超額的人也能用月初的空檔補到，還跑了兩趟求解。
    取消的理由：配額＝場次÷人數取整，**PGY 人數多於週三下午場次時配額是 0**
    （例：4 個週三、5 位 PGY——很常見），於是每個值到一次的人都被記欠假，
    月底警告近乎每月都跳，成了噪音。使用者決定整個拿掉。
    保留下來的是「週三下午照光次數盡量平均」（PhotoStep 以 photo_wed_pm 為主鍵）
    與月底的次數統計（person_course_stats 的 photo_wed_pm）——要不要給半天假，
    由使用者看統計自行斟酌、手動於月曆安排。
    """
    return _solve_month_once(inp)[:3]


def _solve_month_once(inp: DaySolveInput) -> tuple:
    """單趟整月填充 → (day_slots, log, warnings, fc)。fc 供測試檢視公平計數。"""
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
        counts = {c: fc.biopsy_done.get((b.id, c), 0) for c in sorted(b.members)}
        missed = [c for c, n in counts.items() if n == 0]
        if missed:
            warnings.append(f"切片室輪不到（梯次 {b.id}，本梯內未排到）："
                            + "、".join(missed))
        # [2026-07-24 使用者] 同梯切片次數要一樣：min-first 輪選天生 spread ≤1，
        # 差距 >1 必是請假/鎖定/切片開放時段不足所致 → 點名讓使用者手動調整。
        # （跨月梯次只解到半途時計數已含上月回放，不會誤報。）
        elif counts and max(counts.values()) - min(counts.values()) > 1:
            warnings.append(
                f"切片室次數不均（梯次 {b.id}，同梯應盡量一致）："
                + "、".join(f"{c}×{n}" for c, n in counts.items())
                + " —— 多因請假/鎖定時段所致，可手動於月曆調整")
    return day_slots, log, warnings, fc


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
