# -*- coding: utf-8 -*-
"""[RS-34 → RS-35] Clerk 跟診次數的容許範圍。

★使用者改過一次定案,兩句話都要記下來★:
* 2026-09-02(RS-34):「雖然限制 7-11 班,但是★每個人都要平均一致★,
  例如全部人都是 9 班、全部人都是 8 班等等」;
* 2026-09-03(RS-35,★現行★):「重新改回 盡量不要整天放假沒錯,但是
  ★不要求每人跟診次數完全一致(可以正負一★,例如有人七次有人八次可以接受,
  有人十次有人九次可以接受)」。

★為什麼會改回來★:要做到全距 0 就得把多出來的座位留空,而留空必然有人
放假 —— 貪婪求解又會把留空丟在整梯的最後幾格,於是最早跟滿的那個人在最後
一天整天沒事做。實測那個配置:「10/11/10/11/10 且★無人整天放假★」變成
「全員 10 但★一人整天放假★」。使用者權衡之後把「不要整天放假」放回前面。

實測帳(480 種配置):第二趟把★全距>1 的 8 種全部收掉★,
代價是整天放假從 2392 增為 2398 人次(嚴格一致時是 2570)。

★為什麼 RS-25 的「次數最少者先坐」不夠★
它只保證全距 ≤1,而且切片室配額用完之後,「今天誰還能坐診」就不再由跟診
次數決定 —— 實測 1 間診 3 個人跑出 ★9/10/11(全距 2)★。
(把 7-11 的上限拿掉也一樣是 9/10/11 —— ★不是上限造成的★。)

★做法:第一趟量、第二趟配額★(與切片室 RS-24 同一句話:
「多出來的時段留空,寧可空著也不讓誰多」)
整梯到底有幾個座位可坐★預測不出來★:它是「房數×容量 − 那一節 PGY 佔掉的」,
而 PGY 佔幾個由照光/治療室/RS-15 兩位 PGY 月的規則決定。所以用量的:
第一趟照常排 → 數出每人實際坐了幾次 → 算出「每人該坐幾次」→ 第二趟以它為
硬上限重排。搆不到就往下收斂一階,直到大家都做得到。
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cmuh_common.roster.solve_day as sd  # noqa: E402
from cmuh_common.roster.model import ClerkBatch  # noqa: E402
from cmuh_common.roster.solve_day import (  # noqa: E402
    BIOPSY, CLERK_EQUALIZE_MAX_PASSES, CLERK_SEAT_MAX_SPREAD,
    CLERK_SEAT_TARGET_MAX, REST,
    DaySolveInput, FairCounters, _clerk_equal_cost, _clerk_equal_seat_caps,
    is_follow_slot, month_solve_day, solve_session,
)

MON = date(2026, 9, 7)          # 週一;梯次 9/07～9/20
ROOMS = ["101", "102", "103", "104", "105"]


def _grid(nam, npm, start=date(2026, 9, 1), days=30):
    g, d = {}, start
    while d < start + timedelta(days=days):
        if d.weekday() < 5:
            g[d] = {"上午": ROOMS[:nam],
                    "下午": [] if d.weekday() == 2 else ROOMS[:npm]}
        d += timedelta(days=1)
    return g


def _bio(on=True, start=MON):
    out, d = {}, start
    while d < start + timedelta(days=14):
        if d.weekday() < 5:
            out[d.isoformat()] = {s: on for s in ("上午", "下午")
                                  if not (d.weekday() == 2 and s == "下午")}
        d += timedelta(days=1)
    return out


def _counts(ds, members, slot_kind="follow"):
    out = dict.fromkeys(members, 0)
    for _iso, sess in (ds or {}).items():
        for _s, slots in (sess or {}).items():
            for slot, ppl in (slots or {}).items():
                ok = (is_follow_slot(slot) if slot_kind == "follow"
                      else slot == BIOPSY)
                if not ok:
                    continue
                for p in ppl or []:
                    if p in out:
                        out[p] += 1
    return out


def _solve(nam, npm, n, *, npgy=2, bopen=True, leaves=None, start=MON,
           days=30, members=None):
    mem = members or [f"C{i}" for i in range(1, n + 1)]
    ds, log, warns = month_solve_day(DaySolveInput(
        ym="2026-09", grid=_grid(nam, npm, days=days),
        pgy_roster=[f"P{i}" for i in range(1, npgy + 1)],
        clerk_batches=[ClerkBatch("b1", start, list(mem))],
        biopsy_open={"b1": _bio(bopen, start)},
        leaves={"clerk": leaves or {}}, locked={}))
    return ds, warns, mem


# ══ 使用者要的那件事 ═══════════════════════════════════════════════════════
class TestEveryoneGetsTheSameNumber:
    def test_the_case_that_used_to_be_uneven(self):
        """★使用者抱怨的形狀★:1 間診 3 個人,舊行為是 9/10/11(★全距 2★)。
        現在要收進 ±1 —— 但★不必壓到 0★(RS-35)。"""
        ds, _w, mem = _solve(1, 1, 3)
        c = _counts(ds, mem)
        assert max(c.values()) - min(c.values()) <= CLERK_SEAT_MAX_SPREAD, c

    def test_a_range_of_shapes_are_all_within_the_band(self):
        """★不是只修好那一個例子★:診間數/人數/PGY 人數的組合都要在 ±1 內。"""
        bad = {}
        for nam, npm, n, npgy in ((1, 1, 3, 2), (1, 2, 4, 2), (2, 1, 5, 3),
                                  (2, 3, 5, 2), (3, 3, 4, 4), (5, 5, 6, 2),
                                  (1, 3, 2, 1), (3, 1, 6, 3)):
            ds, _w, mem = _solve(nam, npm, n, npgy=npgy)
            c = _counts(ds, mem)
            if max(c.values()) - min(c.values()) > CLERK_SEAT_MAX_SPREAD:
                bad[(nam, npm, n, npgy)] = c
        assert not bad, f"這些配置跟診全距 >1:{bad}"

    def test_a_spread_of_one_is_left_alone(self):
        """★全距 1 不可以再被壓★(RS-35 的核心):壓它要多留空,
        而留空必然有人放假 —— 使用者把「不要整天放假」放在前面。
        判準:餵一份【全勤者已經差 1】的公平計數,不可以再產生上限。"""
        fc = FairCounters()
        for c, n in (("C1", 9), ("C2", 10), ("C3", 10)):
            fc.seat[("clerk", "b1", c)] = n
        inp = DaySolveInput(
            ym="2026-09", grid=_grid(3, 3), pgy_roster=["P1", "P2"],
            clerk_batches=[ClerkBatch("b1", MON, ["C1", "C2", "C3"])],
            biopsy_open={}, leaves={}, locked={})
        assert _clerk_equal_seat_caps(inp, fc, {}) is None

    def test_the_biopsy_counts_stay_level_too(self):
        """★不可以拿切片的公平去換跟診的公平★(RS-24 是使用者另一條定案)。"""
        ds, _w, mem = _solve(1, 1, 3)
        b = _counts(ds, mem, "biopsy")
        assert len(set(b.values())) == 1, f"切片次數被弄不均了:{b}"

    def test_it_still_respects_the_upper_bound(self):
        """RS-33 的 7-11 沒有被這一批取消。"""
        ds, _w, mem = _solve(5, 5, 3)
        c = _counts(ds, mem)
        assert max(c.values()) <= CLERK_SEAT_TARGET_MAX, c

    def test_it_does_not_drag_everyone_down_when_someone_is_on_leave(self):
        """★請假的人拉不到同樣多,不可以把其他人一起拉下來★ ——
        那是拿別人的跟診機會去換一個補不回來的一致。
        (使用者早就講過:假日/連假排不到「也沒關係」。)"""
        lv = {"C1": {MON + timedelta(days=i) for i in (0, 1, 2, 3)}}
        ds, _w, mem = _solve(3, 3, 4, leaves=lv)
        c = _counts(ds, mem)
        rest = [c[m] for m in mem if m != "C1"]
        assert len(set(rest)) == 1, f"全勤者之間仍要一致:{c}"
        assert c["C1"] < rest[0], f"請假者本來就會少:{c}"
        assert rest[0] >= CLERK_SEAT_TARGET_MAX - 1, \
            f"★沒請假的人被拉下來了★:{c}"


# ══ 配額怎麼算 ═════════════════════════════════════════════════════════════
class TestHowTheTargetIsComputed:
    def _fc(self, **seats):
        fc = FairCounters()
        for c, n in seats.items():
            fc.seat[("clerk", "b1", c)] = n
        return fc

    def _inp(self, leaves=None, start=MON):
        return DaySolveInput(
            ym="2026-09", grid=_grid(3, 3), pgy_roster=["P1", "P2"],
            clerk_batches=[ClerkBatch("b1", start, ["C1", "C2", "C3"])],
            biopsy_open={}, leaves={"clerk": leaves or {}}, locked={})

    def test_already_level_needs_no_second_pass(self):
        """★已經一致就不要再排一趟★:第二趟會改變班表(座位留空 = 有人
        多放半天),白跑一趟只是把好好的班表弄壞。"""
        caps = _clerk_equal_seat_caps(
            self._inp(), self._fc(C1=9, C2=9, C3=9), {})
        assert caps is None

    def test_the_target_is_the_lowest_plus_the_tolerance(self):
        """★目標 = 最少的人 + 容許範圍★(9/10/11 → 上限 10):那正好是容許
        範圍的上緣,壓到那裡就夠 —— 再往下壓只是白白留空(RS-35)。"""
        caps = _clerk_equal_seat_caps(
            self._inp(), self._fc(C1=9, C2=10, C3=11), {})
        assert caps == {("b1", c): 10 for c in ("C1", "C2", "C3")}, caps

    def test_a_spread_within_the_tolerance_needs_no_pass(self):
        """★9/10/10 已經在 ±1 內 → 不再重排★(RS-34 時代會再壓成 9/9/9)。"""
        assert _clerk_equal_seat_caps(
            self._inp(), self._fc(C1=9, C2=10, C3=10), {}) is None

    def test_each_pass_lowers_the_target_by_at_least_one(self):
        """★不降就不會停★:同一個數字反覆重排會一直得到同一個結果。
        這裡「最少的人 + 1」仍是 10(＝上一趟的上限),所以下一階必須是 9。"""
        prev = {("b1", c): 10 for c in ("C1", "C2", "C3")}
        caps = _clerk_equal_seat_caps(
            self._inp(), self._fc(C1=9, C2=9, C3=11), prev)
        assert caps == {("b1", c): 9 for c in ("C1", "C2", "C3")}, caps

    def test_a_leaver_does_not_lower_the_target(self):
        """★分母只算整梯全勤的人★:C3 請假只拿到 3 次,不可以把 C1/C2
        的上限從「最少的全勤者 + 1」拉下來。"""
        lv = {"C3": {MON + timedelta(days=1)}}
        caps = _clerk_equal_seat_caps(
            self._inp(leaves=lv), self._fc(C1=10, C2=12, C3=3), {})
        assert caps[("b1", "C1")] == 11, caps

    def test_everyone_on_leave_falls_back_to_the_whole_batch(self):
        lv = {c: {MON + timedelta(days=1)} for c in ("C1", "C2", "C3")}
        caps = _clerk_equal_seat_caps(
            self._inp(leaves=lv), self._fc(C1=9, C2=10, C3=11), {})
        assert caps[("b1", "C1")] == 10, caps

    def test_an_unfinished_course_is_left_alone(self):
        """★跨月梯次在第一個月只排得到一半★:拿那時候的總數算配額,會把
        整梯的人壓到半個梯次的量。梯次走完的那個月才算。"""
        caps = _clerk_equal_seat_caps(
            self._inp(start=date(2026, 9, 28)),   # 第二週落在十月
            self._fc(C1=2, C2=3, C3=4), {})
        assert caps is None, caps

    def test_previous_caps_are_kept(self):
        """★已經拉齊的梯次要留著它的上限★:下一趟拿掉就退回原樣,
        永遠收斂不了。"""
        prev = {("b2", "X"): 5}
        caps = _clerk_equal_seat_caps(
            self._inp(), self._fc(C1=9, C2=10, C3=11), prev)
        assert caps[("b2", "X")] == 5, caps


# ══ 收斂迴圈本身 ═══════════════════════════════════════════════════════════
class TestTheConvergenceLoop:
    def _inp2(self):
        return DaySolveInput(
            ym="2026-09", grid={}, pgy_roster=[],
            clerk_batches=[ClerkBatch("b1", MON, ["C1", "C2"])],
            biopsy_open={}, leaves={}, locked={})

    def _fc2(self, a, b):
        f = FairCounters()
        f.seat[("clerk", "b1", "C1")] = a
        f.seat[("clerk", "b1", "C2")] = b
        return f

    def test_a_spread_of_one_costs_the_same_as_zero(self):
        """★RS-35 的核心★:全距 0 與全距 1 一樣好 —— 不可以為了把 1 壓成 0
        而多留一個空位(那會多一個人整天沒事做)。"""
        inp = self._inp2()
        assert (_clerk_equal_cost(inp, self._fc2(9, 10), {})[0]
                == _clerk_equal_cost(inp, self._fc2(9, 9), {})[0])

    def test_a_spread_of_two_costs_more(self):
        """★對照組★:超過 ±1 就要被扣分(否則這一整套等於關掉)。"""
        inp = self._inp2()
        assert (_clerk_equal_cost(inp, self._fc2(9, 11), {})[0]
                > _clerk_equal_cost(inp, self._fc2(9, 10), {})[0])

    def test_fewer_whole_day_rests_wins(self):
        """★第二鍵是整天放假人次★(使用者把它排在跟診一致之上)。"""
        inp = self._inp2()
        busy = {"2026-09-07": {"上午": {"101": ["C1"]},
                               "下午": {"101": ["C1"]}}}
        idle = {"2026-09-07": {"上午": {REST: ["C1"]},
                               "下午": {REST: ["C1"]}}}
        assert (_clerk_equal_cost(inp, self._fc2(9, 9), busy)
                < _clerk_equal_cost(inp, self._fc2(9, 9), idle))

    def test_wednesday_all_day_rest_does_not_count(self):
        """★週三不算★:下午全院無診是設計如此,不是排班偷懶。"""
        inp = self._inp2()
        wed = {"2026-09-09": {"上午": {REST: ["C1"]},
                              "下午": {REST: ["C1"]}}}
        assert _clerk_equal_cost(inp, self._fc2(9, 9), wed)[1] == 0

    def test_fuller_wins_when_the_rest_is_equal(self):
        """兩鍵都一樣時,★留空比較少★的那一趟勝出。"""
        inp = self._inp2()
        assert (_clerk_equal_cost(inp, self._fc2(9, 9), {})
                < _clerk_equal_cost(inp, self._fc2(8, 8), {}))

    def test_it_keeps_the_best_pass_not_the_last(self, monkeypatch):
        """★收斂不了就交出最好的那一趟★ —— 最後一趟不保證比先前好
        (它的上限是被硬降下來的:每一趟至少降 1,壓到最後只會愈壓愈少)。

        ★這一條沒有自然的反例★:實測每一趟都比前一趟好,所以「交出最後
        一趟」在真實輸入下量不出差別。改成直接釘契約 —— 把代價換成
        「第一趟最好、之後愈來愈差」,再逼迴圈跑滿,回來的必須是第一趟。
        """
        monkeypatch.setattr(sd, "CLERK_EQUALIZE_MAX_PASSES", 3)
        n = [0]

        def _cost(_inp, _fc, _ds):
            n[0] += 1
            return (n[0],)                      # 第一趟最小 = 最好

        monkeypatch.setattr(sd, "_clerk_equal_cost", _cost)
        monkeypatch.setattr(                    # 永不回 None → 迴圈跑滿
            sd, "_clerk_equal_seat_caps",
            lambda inp, fc, caps: {("b1", c): max(0, caps.get(("b1", c), 9) - 1)
                                   for c in ("C1", "C2", "C3")})

        def _mk():
            return DaySolveInput(
                ym="2026-09", grid=_grid(1, 1), pgy_roster=["P1", "P2"],
                clerk_batches=[ClerkBatch("b1", MON, ["C1", "C2", "C3"])],
                biopsy_open={"b1": _bio()}, leaves={}, locked={})

        first = sd._solve_month_once(_mk())[0]
        assert month_solve_day(_mk())[0] == first, \
            "★交出來的不是代價最小的第一趟★"

    def test_the_pass_budget_is_bounded(self):
        assert 2 <= CLERK_EQUALIZE_MAX_PASSES <= 10


# ══ 不可以誤傷別的規則 ═════════════════════════════════════════════════════
class TestItDoesNotBreakTheOtherRules:
    def test_pgy_are_untouched(self):
        """配額只給 Clerk;PGY 的公平是月度的,而且他們沒有梯次。"""
        fc = FairCounters()
        fc.seat[("pgy", "P1")] = 30
        slots, _log = solve_session(
            MON, "上午", ["101", "102"], pgy_avail=["P1", "P2", "P3"],
            clerk_avail=[], biopsy_open=False, fc=fc, batch_key="b1",
            seat_cap={"P1": 0})
        seated = [p for sl, pp in slots.items() if is_follow_slot(sl)
                  for p in pp or []]
        assert seated, f"★PGY 被 Clerk 的配額擋住★:{slots}"

    def test_a_direct_solve_session_call_has_no_quota(self):
        """★None = 不設上限★:直接呼叫 `solve_session` 的測試/工具腳本
        算不出整梯的配額(那要看整月的格網)。"""
        fc = FairCounters()
        fc.seat[("clerk", "b1", "C1")] = 3
        slots, _log = solve_session(
            MON, "上午", ["101"], pgy_avail=[], clerk_avail=["C1"],
            biopsy_open=False, fc=fc, batch_key="b1")
        assert [p for sl, pp in slots.items() if is_follow_slot(sl)
                for p in pp or []] == ["C1"], slots

    def test_the_cap_leaves_the_seat_empty_rather_than_overfill(self):
        """★寧可空著也不讓誰多★(與切片室 RS-24 同一句話)。"""
        fc = FairCounters()
        fc.seat[("clerk", "b1", "C1")] = 4
        slots, _log = solve_session(
            MON, "上午", ["101"], pgy_avail=[], clerk_avail=["C1"],
            biopsy_open=False, fc=fc, batch_key="b1", seat_cap={"C1": 4})
        assert not [p for sl, pp in slots.items() if is_follow_slot(sl)
                    for p in pp or []], slots
        assert "C1" in (slots.get(REST) or []), slots

    def test_locked_days_are_still_kept(self):
        """兩趟求解都要原樣保留鎖定格(第二趟不是「重排全部」)。"""
        iso = (MON + timedelta(days=1)).isoformat()
        ds, _log, _w = month_solve_day(DaySolveInput(
            ym="2026-09", grid=_grid(1, 1), pgy_roster=["P1", "P2"],
            clerk_batches=[ClerkBatch("b1", MON, ["C1", "C2", "C3"])],
            biopsy_open={"b1": _bio()}, leaves={},
            locked={iso: {"上午": {"101": ["C3"]}}}))
        assert ds[iso]["上午"] == {"101": ["C3"]}, ds[iso]

    def test_the_month_solve_still_returns_three_values(self):
        """收斂迴圈是包在 `month_solve_day` 裡的 —— 契約不可以外洩
        (`_solve_month_once` 回四個值,呼叫端只該看到三個)。"""
        out = month_solve_day(DaySolveInput(
            ym="2026-09", grid=_grid(3, 3), pgy_roster=["P1", "P2"],
            clerk_batches=[], biopsy_open={}, leaves={}, locked={}))
        assert len(out) == 3 and isinstance(out[0], dict)


# ══ 外審 RS-34 R1 的兩條 P1 ════════════════════════════════════════════════
class TestWhoCountsAsFullAttendance:
    """★P1-1★ 定義域是「這一梯真的排得到班的日子」,不是 14 個日曆日。
    在週末/國定假日勾一天假,不可以把人判成非全勤 —— 那會改掉配額的分母,
    整份班表跟著變。"""

    def _inp(self, leaves=None, holidays=(), course_days=None):
        cd = set() if course_days is None else course_days
        return DaySolveInput(
            ym="2026-09", grid=_grid(3, 3), pgy_roster=["P1", "P2"],
            clerk_batches=[ClerkBatch("b1", MON, ["C1", "C2", "C3"])],
            biopsy_open={}, leaves={"clerk": leaves or {}}, locked={},
            holidays=set(holidays), course_days=cd, course_clinic_days=set(cd))

    def _b(self, inp):
        return inp.clerk_batches[0]

    def test_a_weekend_leave_does_not_break_full_attendance(self):
        sat = MON + timedelta(days=5)          # 週六
        assert sat.weekday() == 5
        inp = self._inp(leaves={"C1": {sat}})
        assert "C1" in sd.clerk_full_attendance(
            inp, self._b(inp), inp.leaves["clerk"]), "週末請假被當成缺勤"

    def test_a_holiday_leave_does_not_break_full_attendance(self):
        hol = MON + timedelta(days=1)          # 國定假日(不在格網裡)
        inp = self._inp(leaves={"C1": {hol}}, holidays=[hol])
        assert "C1" in sd.clerk_full_attendance(
            inp, self._b(inp), inp.leaves["clerk"]), "假日請假被當成缺勤"

    def test_a_closed_clinic_day_leave_does_not_break_it_either(self):
        """★外審 RS-34 R2 P1 的實際資料形狀★:`month_grid` 對每個非假日平日
        都會寫入一個鍵,★即使兩個時段的診間清單都是空的★(全日停診)——
        所以「在 `course_days` 裡」不代表那天跟診排得到人。
        用真實形狀重現:`grid[d] = {"上午": [], "下午": []}`,而且那一天
        ★仍然留在 `course_days` 裡★。"""
        closed = MON + timedelta(days=1)
        inp = self._inp(leaves={"C1": {closed}},
                        course_days={closed.isoformat()})
        inp.grid[closed] = {"上午": [], "下午": []}
        assert "C1" in sd.clerk_full_attendance(
            inp, self._b(inp), inp.leaves["clerk"]), "全日停診請假被當成缺勤"
        assert closed not in sd.clerk_schedulable_days(inp, self._b(inp))

    def test_a_cross_month_closed_day_uses_the_clinic_day_set(self):
        """跨月的那半段本月格網看不到 → 要靠 `course_clinic_days`
        (它是三個月一起算的,而且只收「真的有診間」的日子)。"""
        nxt = date(2026, 10, 1)
        b = ClerkBatch("b1", date(2026, 9, 21), ["C1", "C2"])
        inp = DaySolveInput(
            ym="2026-09", grid=_grid(3, 3), pgy_roster=["P1"],
            clerk_batches=[b], biopsy_open={},
            leaves={"clerk": {"C1": {nxt}}}, locked={},
            course_days={nxt.isoformat()},        # 在格網鍵裡
            # 有算(非空),但★那天不在裡面★ = 那天沒有跟診診間
            course_clinic_days={MON.isoformat()})
        assert "C1" in sd.clerk_full_attendance(inp, b, inp.leaves["clerk"])

    def test_a_cross_month_open_day_still_counts(self):
        """★對照組★:跨月那天真的有診間,請假就算缺勤。"""
        nxt = date(2026, 10, 1)
        b = ClerkBatch("b1", date(2026, 9, 21), ["C1", "C2"])
        inp = DaySolveInput(
            ym="2026-09", grid=_grid(3, 3), pgy_roster=["P1"],
            clerk_batches=[b], biopsy_open={},
            leaves={"clerk": {"C1": {nxt}}}, locked={},
            course_days={nxt.isoformat()},
            course_clinic_days={nxt.isoformat()})
        assert "C1" not in sd.clerk_full_attendance(inp, b, inp.leaves["clerk"])

    def test_a_working_day_leave_still_counts(self):
        """★對照組★:真的排得到班的那天請假,仍然算缺勤。"""
        wd = MON + timedelta(days=1)
        inp = self._inp(leaves={"C1": {wd}})
        assert "C1" not in sd.clerk_full_attendance(
            inp, self._b(inp), inp.leaves["clerk"])

    def test_this_months_grid_wins_for_days_it_contains(self):
        """★本月的格網最權威★:它直接說得出那天有沒有跟診診間,而
        `course_days`/`course_clinic_days` 本來就是從同一份格網算出來的。
        跨月那半段本月格網看不到,才輪到後者。"""
        wd = MON + timedelta(days=1)             # 本月、而且有診間
        inp = self._inp(leaves={"C1": {wd}},
                        course_days={MON.isoformat()})   # 刻意漏掉那天
        assert "C1" not in sd.clerk_full_attendance(
            inp, self._b(inp), inp.leaves["clerk"]),             "本月格網說那天有診間,就是可跟診日"

    def test_it_changes_the_quota_denominator(self):
        """★這條規則真的會改結果★:C3 在週末請假,配額仍是三個人的平均。"""
        sat = MON + timedelta(days=5)
        fc = FairCounters()
        for c, n in (("C1", 9), ("C2", 10), ("C3", 11)):
            fc.seat[("clerk", "b1", c)] = n
        caps = _clerk_equal_seat_caps(self._inp(leaves={"C3": {sat}}), fc, {})
        assert caps[("b1", "C1")] == 10, caps


class TestALeaverWithMoreThanTheOthers:
    """★P1-2★ 收斂判定只看全勤者的話,「請假者反而比較多」會被當成已收斂
    (9/9/10 直接交出去)。鎖定格與跨月回放都會把次數加進公平計數,
    所以這是可達狀態。"""

    def _inp(self, leaves=None):
        return DaySolveInput(
            ym="2026-09", grid=_grid(3, 3), pgy_roster=["P1", "P2"],
            clerk_batches=[ClerkBatch("b1", MON, ["C1", "C2", "C3"])],
            biopsy_open={}, leaves={"clerk": leaves or {}}, locked={})

    def _fc(self, **seats):
        fc = FairCounters()
        for c, n in seats.items():
            fc.seat[("clerk", "b1", c)] = n
        return fc

    def test_it_is_not_treated_as_converged(self):
        """★超出容許範圍才算★:請假者多 1 是可以接受的(RS-35),
        多 2 才要處理。"""
        lv = {"C3": {MON + timedelta(days=1)}}
        assert _clerk_equal_seat_caps(
            self._inp(lv), self._fc(C1=9, C2=9, C3=10), {}) is None,             "多 1 在容許範圍內,不該重排"
        caps = _clerk_equal_seat_caps(
            self._inp(lv), self._fc(C1=9, C2=9, C3=11), {})
        assert caps is not None, "★請假者多 2 卻被當成已收斂★"
        assert caps[("b1", "C3")] == 10, caps

    def test_repeated_passes_do_not_keep_lowering_the_full_attenders(self):
        """★外審 RS-35 R1 P2-1★:全勤者已經在容許範圍內、只有請假者因為
        鎖定格/跨月回放而超出時,★不可以逐趟把全勤者一起壓低★ ——
        那是拿他們的跟診機會去追一個這一次求解改不動的數,而且會製造不必要的
        整天放假(正好違反 RS-35 的優先序)。

        判準:同一個狀態連餵三趟,上限必須★停在同一個數★(停手的訊號)。
        """
        lv = {"C3": {MON + timedelta(days=1)}}
        inp, fc = self._inp(lv), self._fc(C1=9, C2=9, C3=11)
        caps: dict = {}
        seen = []
        for _ in range(3):
            nxt = _clerk_equal_seat_caps(inp, fc, caps)
            assert nxt is not None, "★還超出卻回 None(會被誤當成收斂)★"
            seen.append(nxt[("b1", "C1")])
            if nxt == caps:
                break
            caps = nxt
        assert len(set(seen)) == 1, f"★全勤者被逐趟壓低★:{seen}"

    def test_an_unresolvable_batch_is_not_reported_as_converged(self):
        """★回 None 只代表「全部落在容許範圍內」★(外審 R1 P2-1):
        還超出、但改不動時要回同一組上限,呼叫端才會停手並交出【最好的那一趟】
        —— 回 None 的話 `month_solve_day` 會把★最後一趟★誤當成收斂交出去。"""
        lv = {"C3": {MON + timedelta(days=1)}}
        inp, fc = self._inp(lv), self._fc(C1=9, C2=9, C3=11)
        caps = {("b1", c): 10 for c in ("C1", "C2", "C3")}
        nxt = _clerk_equal_seat_caps(inp, fc, caps)
        assert nxt == caps, f"★改不動時要回同一組上限★:{nxt}"

    def test_it_does_not_drag_the_full_attenders_down(self):
        """★目標是「最少的全勤者 + 1」,不是再往下壓★ —— 壓下去也追不上
        (多出來的次數來自這一次求解改不動的東西)。"""
        lv = {"C3": {MON + timedelta(days=1)}}
        prev = {("b1", c): 11 for c in ("C1", "C2", "C3")}
        caps = _clerk_equal_seat_caps(
            self._inp(lv), self._fc(C1=9, C2=9, C3=11), prev)
        assert caps[("b1", "C1")] == 10, f"全勤者被拉低了:{caps}"

    def test_the_loop_stops_instead_of_spinning(self, monkeypatch):
        """★同一組上限就停手★:再排一趟也不會變,繼續壓只是白跑。"""
        seen = []
        real = sd._solve_month_once

        def _spy(inp, seat_cap=None):
            seen.append(seat_cap)
            return real(inp, seat_cap=seat_cap)

        monkeypatch.setattr(sd, "_solve_month_once", _spy)
        monkeypatch.setattr(
            sd, "_clerk_equal_seat_caps",
            lambda inp, fc, caps: {("b1", "C1"): 3} if not caps else dict(caps))
        month_solve_day(self._inp())
        assert len(seen) == 2, f"該停手卻多排了:{len(seen)} 趟"

    def test_it_says_so_out_loud(self):
        """★停手要據實回報★ —— 使用者再按幾次自動排班都一樣,
        訊息要講清楚差距來自哪裡。"""
        out = sd.clerk_seat_uneven_warnings(
            [ClerkBatch("b1", MON, ["C1", "C2", "C3"])],
            {("b1", "C1"): 9, ("b1", "C2"): 9, ("b1", "C3"): 11})
        assert out and "跟診次數相差超過" in out[0], out
        assert "鎖定" in out[0] and "手動" in out[0], out[0]

    def test_a_level_batch_says_nothing(self):
        assert sd.clerk_seat_uneven_warnings(
            [ClerkBatch("b1", MON, ["C1", "C2"])],
            {("b1", "C1"): 9, ("b1", "C2"): 9}) == []

    def test_a_spread_of_one_says_nothing(self):
        """★求解器合法排得出 9/10★(RS-35 容許 ±1)—— 在這裡點名等於
        「系統要求你去修一個它自己認為正確的班表」。"""
        assert sd.clerk_seat_uneven_warnings(
            [ClerkBatch("b1", MON, ["C1", "C2"])],
            {("b1", "C1"): 9, ("b1", "C2"): 10}) == []

    def test_a_leaver_who_is_behind_says_nothing(self):
        """★請假者比全勤者少多少都不點名★(那是他自己請假、補不回來;
        RS-33 的「偏少」另外點)。"""
        assert sd.clerk_seat_uneven_warnings(
            [ClerkBatch("b1", MON, ["C1", "C2", "C3"])],
            {("b1", "C1"): 2, ("b1", "C2"): 10, ("b1", "C3"): 10},
            base_ids={("b1", "C2"), ("b1", "C3")}) == []

    def test_a_leaver_who_is_ahead_still_says_so(self):
        """★對照組★:請假者反而【高出】容許範圍仍要點名
        (鎖定格/跨月回放做得到)。"""
        out = sd.clerk_seat_uneven_warnings(
            [ClerkBatch("b1", MON, ["C1", "C2", "C3"])],
            {("b1", "C1"): 12, ("b1", "C2"): 10, ("b1", "C3"): 10},
            base_ids={("b1", "C2"), ("b1", "C3")})
        assert out and "C1×12" in out[0], out

    def test_only_ids_limits_the_scope(self):
        assert sd.clerk_seat_uneven_warnings(
            [ClerkBatch("b1", MON, ["C1", "C2"])],
            {("b1", "C1"): 9, ("b1", "C2"): 8}, only_ids=set()) == []

    def test_the_solver_emits_it(self, monkeypatch):
        """★接線★:求解器交出不一致的班表時,警告一定要跟著出來。

        ★自然情境造不出來★:試過用 `prior_sessions` 給 C3 兩次上個月的
        跟診,九月這一趟★自己就把它補平了★(9/9/7 + 2 = 9/9/9)—— 那正是
        這一批在做的事,反例反而證明它有效。所以這裡直接釘契約:把代價換成
        「愈後面愈好」,逼求解器交出被硬壓過的那一趟(它是不平的),
        警告就必須出現。
        """
        monkeypatch.setattr(sd, "CLERK_EQUALIZE_MAX_PASSES", 2)
        n = [10]

        def _cost(_inp, _fc, _ds):
            n[0] -= 1
            return (n[0],)                      # 後面的比較好

        monkeypatch.setattr(sd, "_clerk_equal_cost", _cost)
        monkeypatch.setattr(          # 只壓 C1 → 交出來的那一趟一定不平
            sd, "_clerk_equal_seat_caps",
            lambda inp, fc, caps: {("b1", "C1"): 2} if not caps
            else dict(caps))
        ds, _log, warns = month_solve_day(self._inp())
        c = _counts(ds, ["C1", "C2", "C3"])
        assert len(set(c.values())) > 1, f"前提:交出來的那一趟要是不平的:{c}"
        assert any("跟診次數相差超過" in w for w in warns), (warns, c)


class TestTheServiceComputesTheClinicDays:
    """★`course_clinic_days` 要真的算得出來★(外審 RS-34 R2 P1)。

    `month_grid` 對每個非假日平日都會寫入一個鍵,即使兩個時段的診間清單
    都是空的 —— 全日停診(把當天所有診間都關掉)就是這個形狀。
    """

    YM = "2026-09"

    def _svc(self, closed_iso=None, closed_next=None):
        from cmuh_common.roster.service import RosterService
        from cmuh_common.roster.storage import RosterStorage
        import tempfile
        st = RosterStorage(tempfile.mkdtemp())
        st.save_config({"pgy_members": [{"id": "P1"}], "r_members": [],
                        "vs_members": [], "room_capacity": 2})
        st.save_clinic_template({"template": {
            str(w): {"上午": [{"room": "101"}], "下午": [{"room": "101"}]}
            for w in range(5)}})
        st.save_clerk_batches([{"id": "b1", "start_monday": MON.isoformat(),
                                "members": ["C1", "C2"]}])
        def _ov(iso):
            return {iso: {s: {"closed_rooms": ["101"]}
                          for s in ("上午", "下午")}}
        month: dict = {}
        if closed_iso:
            month["grid_overrides"] = _ov(closed_iso)
        st.save_month(self.YM, month)
        if closed_next:
            st.save_month(closed_next[:7], {"grid_overrides": _ov(closed_next)})
        return RosterService(st)

    def test_a_fully_closed_weekday_is_not_a_clinic_day(self):
        closed = (MON + timedelta(days=1)).isoformat()
        inp = self._svc(closed).build_day_input(self.YM)
        assert closed in inp.course_days, \
            "前提:全日停診那天★仍在★格網鍵裡(這正是問題的形狀)"
        assert closed not in inp.course_clinic_days, \
            "★全日停診卻被當成可跟診日★"

    def test_an_open_weekday_is(self):
        """★對照組★:沒關診的日子要在裡面(不然這份集合只是永遠空著)。"""
        inp = self._svc().build_day_input(self.YM)
        assert MON.isoformat() in inp.course_clinic_days

    def test_it_reaches_the_solver(self):
        """★沒有傳下去 = 這份集合不存在★。"""
        closed = (MON + timedelta(days=1)).isoformat()
        inp = self._svc(closed).build_day_input(self.YM)
        assert inp.course_clinic_days, "沒有算/沒有傳給求解器"
        b = inp.clerk_batches[0]
        assert date.fromisoformat(closed) not in sd.clerk_schedulable_days(
            inp, b)

    def test_a_fully_closed_day_next_month_is_excluded_too(self):
        """★相鄰月份也要濾★:梯次可以跨月,下個月那一天全日停診時,
        在那天請假一樣不該被判成缺勤。"""
        nxt = "2026-10-01"                       # 週四
        inp = self._svc(closed_next=nxt).build_day_input(self.YM)
        assert nxt in inp.course_days, "前提:它仍在格網鍵裡"
        assert nxt not in inp.course_clinic_days,             "★下個月的全日停診沒被濾掉★"

    def test_the_neighbouring_months_are_included(self):
        """梯次可以跨月 —— 相鄰月份的診間日也要算進來,否則跨月那半段
        永遠被當成「沒有診」。"""
        inp = self._svc().build_day_input(self.YM)
        assert any(i.startswith("2026-10") for i in inp.course_clinic_days), \
            "下個月的診間日沒被算進來"
        assert any(i.startswith("2026-08") for i in inp.course_clinic_days), \
            "上個月的診間日沒被算進來"
