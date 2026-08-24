# -*- coding: utf-8 -*-
"""[批次RS-24 / 使用者回饋 2026-08-24]

同學反應:**切片室次數一樣,可是跟診次數有人多有人少**。

根因(讀程式讀出來的,不是猜的):切片室Step 排在就座【之前】,而它的輪選
【只看本梯切片次數】—— 而「那一個時段的座位夠不夠坐所有人」是浮動的:
座位多於人時,被抽去切片的人淨損失一次跟診;座位少於人時他本來就會放假
(淨損失 0)。於是「切片次數一樣」完全不保證「跟診次數一樣」,差別在你是在
寬鬆的時段還是擠的時段被抽走,而那筆帳從來沒有人記。
★不是跨月份★:RF-09 早就把同一梯次上個月的既存班表回放進公平計數
(`replay_counters` 連跟診座位都回放)。

使用者定案的規則(原話):
> 先算出這兩週切片開放的時段(一個切片時段一個人),之後做平均分配給當時
> 兩週的 clerk 學生。例如 16 個時段 5 個人,則一人應該要分配三次(取整數),
> 讓每個人切片室相同;其餘則分配到跟診座位,並且盡量要讓跟診每個人次數
> 盡量一致,最後排不進去的才放假。

(Clerk 沒有照光/治療室 —— 那兩步只從 `ctx.pgy` 取人,見 `PhotoStep`/
 `TreatmentStep`;Clerk 只有跟診與切片室。)
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.model import ClerkBatch                  # noqa: E402
from cmuh_common.roster.service import RosterService              # noqa: E402
from cmuh_common.roster.storage import RosterStorage              # noqa: E402
from cmuh_common.roster.solve_day import (                       # noqa: E402
    BIOPSY, PHOTO, REST, TREATMENT, DaySolveInput, FairCounters,
    _biopsy_forced_today as _forced, month_solve_day, solve_session,
)

MON = date(2026, 8, 3)          # 週一(梯次起始)


def _grid(start: date, days: int, rooms_am, rooms_pm, open_cap=None,
          am_only: bool = False, bid: str = "b1") -> tuple:
    """→ (grid, {梯次: {iso: {時段: bool}}}):平日早/午都開診;切片室開
    `open_cap` 個時段
    (None = 全開;週三下午恆關,不計入)。

    `am_only`＝每天只開上午那一個切片時段 —— ★同日早+午不得同一人★,
    所以要驗「一個人切好幾次」的配額,開放時段就得分散在不同天。
    """
    grid: dict = {}
    bio: dict = {}
    left = open_cap
    d = start
    while d < start + timedelta(days=days):
        if d.weekday() < 5:
            grid[d] = {"上午": list(rooms_am), "下午": list(rooms_pm)}
            day: dict = {}
            for s in (("上午",) if am_only else ("上午", "下午")):
                if d.weekday() == 2 and s == "下午":
                    continue                      # 週三下午恆關
                if left is None or left > 0:
                    day[s] = True
                    if left is not None:
                        left -= 1
            bio[d.isoformat()] = day
        d += timedelta(days=1)
    # ★切片開放是【依梯次】的★(外審 2026-08-24 P2-01):重疊梯次時,壓平的
    #   全域 map 會讓敗者的設定污染勝者。
    return grid, {bid: bio}


def _counts(day_slots: dict) -> tuple:
    """→ ({人: 跟診次數}, {人: 切片次數}, {人: 放假次數})。"""
    seat: dict = {}
    bx: dict = {}
    rest: dict = {}
    for sessions in day_slots.values():
        for slots in sessions.values():
            for slot, people in (slots or {}).items():
                for p in people or []:
                    if slot == BIOPSY:
                        bx[p] = bx.get(p, 0) + 1
                    elif slot == REST:
                        rest[p] = rest.get(p, 0) + 1
                    elif slot not in (PHOTO, TREATMENT):
                        seat[p] = seat.get(p, 0) + 1
    return seat, bx, rest


def _spread(counts: dict, keys) -> int:
    vals = [counts.get(k, 0) for k in keys]
    return max(vals) - min(vals)


# ══ 誰去切片:跟診次數最多的那一位 ════════════════════════════════════════
class TestWhoGoesToTheBiopsyRoom:
    def test_the_one_with_the_most_clinic_so_far_goes(self):
        """★這一批的核心★:被抽去切片的成本要記到帳上 —— 跟診最多的人去切片,
        兩邊的次數才會一起被拉平。

        (舊版挑的是「本梯切片次數最少者」:它管得住切片室的次數,管不住跟診 ——
         那正是同學抱怨的來源。)
        """
        fc = FairCounters()
        fc.seat[("clerk", "bx", "1")] = 2
        fc.seat[("clerk", "bx", "2")] = 5         # 跟診最多
        fc.seat[("clerk", "bx", "3")] = 3
        slots, _log = solve_session(
            MON, "上午", ["101", "102"],
            pgy_avail=[], clerk_avail=["1", "2", "3"],
            biopsy_open=True, fc=fc, batch_key="bx")
        assert slots[BIOPSY] == ["2"], f"應由跟診最多的 2 去切片: {slots}"

    def test_then_whoever_still_owes_the_most(self):
        """跟診一樣多時,配額還缺得多的人先補(整梯才收得完)。"""
        fc = FairCounters()
        slots, _log = solve_session(
            MON, "上午", ["101", "102"],
            pgy_avail=[], clerk_avail=["1", "2"],
            biopsy_open=True, fc=fc, batch_key="bx",
            biopsy_quota_left={"1": 1, "2": 3})
        assert slots[BIOPSY] == ["2"], "還缺 3 次的 2 應優先"

    def test_the_quota_is_a_ceiling(self):
        """★配額用完就不再排★:寧可讓切片室空著,也不讓某個人比別人多切
        (使用者 2026-08-24:「讓每個人切片室相同」)。"""
        fc = FairCounters()
        slots, _log = solve_session(
            MON, "上午", ["101"],
            pgy_avail=[], clerk_avail=["1", "2"],
            biopsy_open=True, fc=fc, batch_key="bx",
            biopsy_quota_left={"1": 0, "2": 0})
        assert BIOPSY not in slots, f"配額用完卻還在排: {slots}"

    def test_no_quota_means_no_ceiling(self):
        """★直接呼叫的呼叫端算不出配額★(它要看整梯的開放時段數與名單)——
        沒帶就不設上限,維持「切片室開著就有人」的舊行為。"""
        fc = FairCounters()
        slots, _log = solve_session(
            MON, "上午", ["101"], pgy_avail=[], clerk_avail=["1"],
            biopsy_open=True, fc=fc, batch_key="bx")
        assert slots[BIOPSY] == ["1"]

    def test_a_closed_biopsy_room_is_still_closed(self):
        """週三下午恆關(即使手動格網誤設為開)。"""
        fc = FairCounters()
        slots, _log = solve_session(
            date(2026, 8, 5), "下午", [],
            pgy_avail=[], clerk_avail=["1"],
            biopsy_open=True, fc=fc, batch_key="bx")
        assert BIOPSY not in slots and slots[REST] == ["1"]


# ══ 請假會讓配額排不完 → 瓶頸判準 ════════════════════════════════════════
class TestTheQuotaSurvivesLeave:
    """`_biopsy_forced_today`:★放掉這一格之後,剩下的配額與剩下的時段還配得
    起來嗎★。`todo` 是【還缺的次數】(每人重複出現他還缺幾次)。"""

    def _free(self, away: dict):
        return lambda c, iso: iso not in (away.get(c) or set())

    def test_no_pressure_while_it_still_fits(self):
        assert _forced(["1", "2"], ["a", "b", "c"], ["1", "2"],
                       self._free({})) == frozenset()

    def test_pressure_when_the_slots_run_out(self):
        assert _forced(["1", "2"], ["a"], ["1", "2"],
                       self._free({})) == frozenset({"1", "2"})

    def test_a_shared_bottleneck_is_caught_too(self):
        """★反例本體★:A、B 都只在前兩個時段在、第三個時段兩人都請假 ——
        誰都不是「最後機會」(兩人都還能上第二個),整體也還有餘裕(3 ≥ 2),
        但放掉第一個之後就只剩一個時段配兩個人。"""
        away = {"A": {"c"}, "B": {"c"}}
        assert _forced(["A", "B"], ["b", "c"], ["A", "B"],
                       self._free(away)) == frozenset({"A", "B"})

    def test_a_multi_slot_quota_counts_each_time(self):
        """★配額是次數不是人數★:A 還缺 2 次、B 缺 1 次,之後只剩 2 個時段
        → 放掉這一格就補不完了。"""
        assert _forced(["A", "A", "B"], ["b", "c"], ["A", "B"],
                       self._free({})) == frozenset({"A", "B"})

    def test_one_person_cannot_take_both_halves_of_a_day(self):
        """★反例本體★(外審 Codex 配額版 P2):A 還缺 2 次、B 缺 1 次,
        之後只剩【同一天】的早、午兩格 —— 正式排班禁止同一人同日早+午都切
        (`_biopsy_cands`),所以那兩格其實只能給 A 一次。
        匹配器若把兩個 A 都配進去,就會誤判成「今天可以先補 B」,
        最後 A 少一次、配額排不完。
        """
        assert _forced(["A", "A", "B"], ["d1", "d1"], ["A", "B"],
                       self._free({})) == frozenset({"A"})

    def test_two_different_days_are_still_two_chances(self):
        """反過來:不同天的兩格本來就配得完 → 今天不必補。"""
        assert _forced(["A", "A", "B"], ["d1", "d2", "d3"], ["A", "B"],
                       self._free({})) == frozenset()

    def test_it_only_forces_people_who_keep_the_rest_feasible(self):
        """要補誰也要看得懂:之後只剩一個時段 "b",而 B 那天請假 →
        今天補 B、把 "b" 留給 A 才排得完;今天若補 A,B 就永遠補不到了。"""
        away = {"B": {"b"}}
        assert _forced(["A", "B"], ["b"], ["A", "B"],
                       self._free(away)) == frozenset({"B"})

    def test_a_hopeless_case_still_fills_one(self):
        """本來就排不完(沒有後續時段、兩個人待補)→ 今天在的都算候選,
        至少補一個;剩下的由月底警告點名。"""
        assert _forced(["A", "B"], [], ["A", "B"],
                       self._free({})) == frozenset({"A", "B"})

    def test_the_solver_prefers_the_bottleneck_over_the_clinic_count(self):
        """瓶頸要壓過「跟診最多者」那一鍵 —— 否則配額會排不完。"""
        fc = FairCounters()
        fc.seat[("clerk", "bx", "1")] = 9         # 跟診最多,但今天不補他沒差
        slots, _log = solve_session(
            MON, "上午", ["101", "102"],
            pgy_avail=[], clerk_avail=["1", "2"],
            biopsy_open=True, fc=fc, batch_key="bx",
            biopsy_quota_left={"1": 1, "2": 1},
            biopsy_force=frozenset({"2"}))
        assert slots[BIOPSY] == ["2"]


# ══ 整月驗收(這一批真正要交付的東西)══════════════════════════════════════
class TestTheWholeBatchOutcome:
    def _solve(self, rooms_am, rooms_pm, members, days=12, leaves=None,
               open_cap=None, locked=None):
        grid, bio = _grid(MON, days, rooms_am, rooms_pm, open_cap)
        day_slots, _log, warns = month_solve_day(DaySolveInput(
            ym="2026-08", grid=grid, pgy_roster=["P1", "P2"],
            clerk_batches=[ClerkBatch("b1", MON, list(members))],
            biopsy_open=bio, leaves={"clerk": leaves or {}},
            locked=locked or {}))
        return day_slots, warns

    def test_the_users_own_example(self):
        """★使用者的例子★:16 個切片時段、5 個人 → 每人 3 次(16//5),
        多出來的 1 個時段留空。"""
        day_slots, warns = self._solve(
            ["101", "102", "103"], ["101", "102", "103"],
            ["C1", "C2", "C3", "C4", "C5"], open_cap=16)
        _seat, bx, _rest = _counts(day_slots)
        assert [bx.get(c, 0) for c in ("C1", "C2", "C3", "C4", "C5")] == \
            [3, 3, 3, 3, 3], f"每人應各 3 次: {bx}"
        assert sum(bx.values()) == 15, "多出來的那一個時段應留空"
        assert not any("不均" in w or "輪不到" in w for w in warns), warns

    def test_the_clinic_counts_are_level_too(self):
        """★同學抱怨的那一項★:整梯跟診次數全距 ≤1(沒有人請假的情況),
        而切片次數完全相同。"""
        day_slots, _warns = self._solve(
            ["101", "103"], ["101", "102", "105"],
            ["C1", "C2", "C3", "C4", "C5"])
        seat, bx, _rest = _counts(day_slots)
        keys = ("C1", "C2", "C3", "C4", "C5")
        assert _spread(seat, keys) <= 1, f"跟診不均: {seat}"
        assert _spread(bx, keys) == 0, f"切片不均: {bx}"

    def test_clinic_counts_stay_level_even_when_seats_are_scarce(self):
        """★[RS-25] 使用者:「跟診次數不能有人兩周跟了 7 次有人跟了 10 次」★

        最擠的情境(3 個人搶 1 個座位 + 1 個切片室)最容易失衡:每個時段一定
        有人放假,而「今天還沒事做的人優先」若排在次數前面,早上放假的人就會
        壓過次數更少的人再拿一次 —— 實測會跑出 8/6/6(全距 2)。
        次數優先之後收斂成全距 ≤1。
        """
        grid, bio = _grid(MON, 12, ["101"], ["101"])
        day_slots, _log, _warns = month_solve_day(DaySolveInput(
            ym="2026-08", grid=grid, pgy_roster=["P1"],
            clerk_batches=[ClerkBatch("b1", MON, ["C1", "C2", "C3"])],
            biopsy_open=bio, capacity=1))
        seat, bx, _rest = _counts(day_slots)
        keys = ("C1", "C2", "C3")
        assert _spread(seat, keys) <= 1, f"跟診不均: {seat}"
        assert _spread(bx, keys) == 0, f"切片不均: {bx}"

    def test_the_totals_are_level_too(self):
        day_slots, _warns = self._solve(
            ["101", "103"], ["101", "102", "105"],
            ["C1", "C2", "C3", "C4", "C5"])
        seat, bx, _rest = _counts(day_slots)
        keys = ("C1", "C2", "C3", "C4", "C5")
        total = {c: seat.get(c, 0) + bx.get(c, 0) for c in keys}
        assert _spread(total, keys) <= 1, f"總量不均: {total}"

    def test_fewer_slots_than_people_still_fills_what_it_can(self):
        """開放時段數 < 人數 → 配額算出來是 0;★不可以因此整梯都不排★
        (設計文件 C4:排得到的人先輪,輪不到的點名,不硬塞)。"""
        day_slots, warns = self._solve(
            ["101", "102", "103"], ["101", "102", "103"],
            ["C1", "C2", "C3", "C4", "C5"], open_cap=3)
        _seat, bx, _rest = _counts(day_slots)
        assert sum(bx.values()) == 3, f"開放的 3 個時段都要用到: {bx}"
        assert max(bx.values()) == 1, f"不可以有人切兩次: {bx}"
        assert any("輪不到" in w for w in warns), warns

    def test_someone_whose_last_chance_is_today_is_taken_today(self):
        """★誰去切片不能只看跟診次數★:配額只有 2 個時段(週一早、週二早),
        兩個人各 1 次;而 "1" 之後整段請假 —— 週一那一格若給了 "6",
        "1" 就永遠補不到(週二他不在)。

        ★反例要靠瓶頸判準分勝負★:兩人的跟診次數與配額都一樣,這一天的
        決定性抖動偏好 "6"(見 `_jitter`)—— 所以「不看瓶頸」就會挑 "6"。
        """
        grid, _bio = _grid(MON, 12, ["101", "102"], ["101", "102"])
        tue = MON + timedelta(days=1)
        bio = {"b1": {MON.isoformat(): {"上午": True},   # 只開兩個時段,分在兩天
                      tue.isoformat(): {"上午": True}}}
        away = {MON + timedelta(days=i) for i in range(1, 12)}
        day_slots, _log, warns = month_solve_day(DaySolveInput(
            ym="2026-08", grid=grid, pgy_roster=["P1"],
            clerk_batches=[ClerkBatch("b1", MON, ["1", "6"])],
            biopsy_open=bio, leaves={"clerk": {"1": away}}))
        _seat, bx, _rest = _counts(day_slots)
        assert bx.get("1", 0) == 1, f"1 的最後機會被放掉了: {bx} / {warns}"
        assert bx.get("6", 0) == 1, bx

    def test_a_locked_biopsy_counts_as_already_covered(self):
        """★鎖定時段已經指派的切片要算進配額★:它不在「還解得到的時段」裡
        (鎖定不重排),但那個人確實會切到。

        4 個開放時段(分散在 4 天)+ 2 個鎖定(都指派給 C1)= 6 → 每人 3 次。
        * C1 的配額要先扣掉那兩次 —— 不扣的話開放時段會被他再拿 2 次,
          最後變成 4 次而 C2 只有 2 次;
        * 分母也要含鎖定 —— 不含的話 cap 只有 2,C1 的配額變 0、開放時段用不完。
        """
        grid, bio = _grid(MON, 12, ["101", "102", "103"],
                          ["101", "102", "103"], open_cap=4, am_only=True)
        days = sorted(grid)
        locked = {days[-2].isoformat(): {"下午": {BIOPSY: ["C1"]}},
                  days[-1].isoformat(): {"下午": {BIOPSY: ["C1"]}}}
        day_slots, _log, _warns = month_solve_day(DaySolveInput(
            ym="2026-08", grid=grid, pgy_roster=["P1"],
            clerk_batches=[ClerkBatch("b1", MON, ["C1", "C2"])],
            biopsy_open=bio, locked=locked))
        _seat, bx, _rest = _counts(day_slots)
        assert (bx.get("C1", 0), bx.get("C2", 0)) == (3, 3), (
            f"配額沒有把鎖定算進去: {bx}")
        assert sum(bx.values()) == 6, f"開放的時段沒有用完: {bx}"

    def test_two_batches_reusing_a_code_do_not_cover_each_other(self):
        """★鎖定覆蓋的鍵要含梯次★:Clerk 代號是依梯次命名空間的 ——
        只存代號的話,第 2 梯那個 C1 的鎖定切片會讓【第 1 梯】的 C1 被當成
        「已經有著落」而永遠不補。"""
        grid, bio = _grid(MON, 26, ["101", "102", "103"],
                          ["101", "102", "103"])
        bio = {"b1": {iso: v for iso, v in bio["b1"].items()
                      if date.fromisoformat(iso) < MON + timedelta(days=14)},
               "b2": {iso: v for iso, v in bio["b1"].items()
                      if date.fromisoformat(iso) >= MON + timedelta(days=14)}}
        b2_day = [d for d in sorted(grid) if d >= MON + timedelta(days=14)][2]
        locked = {b2_day.isoformat(): {"下午": {BIOPSY: ["C1"]}}}
        day_slots, _log, warns = month_solve_day(DaySolveInput(
            ym="2026-08", grid=grid, pgy_roster=["P1"],
            clerk_batches=[ClerkBatch("b1", MON, ["C1", "C2"]),
                           ClerkBatch("b2", MON + timedelta(days=14),
                                      ["C1", "C2"])],
            biopsy_open=bio, locked=locked))
        first = {iso: v for iso, v in day_slots.items()
                 if date.fromisoformat(iso) < MON + timedelta(days=14)}
        _seat, bx, _rest = _counts(first)
        # ★第 1 梯的兩個人要一樣多★:鍵只存代號的話,第 2 梯那筆鎖定會被算成
        #   第 1 梯 C1 已經有著落,他就會少切一次。
        assert bx.get("C1", 0) >= 1, f"第 1 梯的 C1 沒輪到切片: {bx} / {warns}"
        assert bx.get("C1", 0) == bx.get("C2", 0), (
            f"第 1 梯的切片次數被別梯的鎖定影響了: {bx} / {warns}")

    def test_a_cross_month_batch_keeps_the_whole_batch_quota(self):
        """★反例本體★(外審 Codex 配額版 P1):梯次跨月時,兩個月各看到 8 個
        開放時段、5 個人。分母若只用「本月看得到的」+「上月排了幾次」,
        兩個月各算 `8//5=1` → 整梯每人只有 2 次;整梯的正解是 `16//5=3`。
        除法的餘數不可以在月份交界被吃掉。

        (`biopsy_open` 是依梯次載入的,本來就含跨月日期 —— 分母算得出來。)
        """
        # 本月的格網只涵蓋梯次的前半(第一週),後半的開放時段在格網外。
        grid, _b = _grid(MON, 5, ["101", "102", "103"], ["101", "102", "103"])
        flat: dict = {}
        opened = 0
        d = MON
        while d < MON + timedelta(days=12) and opened < 16:
            if d.weekday() < 5:
                for sess in ("上午", "下午"):
                    if d.weekday() == 2 and sess == "下午":
                        continue
                    if opened < 16:
                        flat.setdefault(d.isoformat(), {})[sess] = True
                        opened += 1
            d += timedelta(days=1)
        bio = {"b1": flat}
        in_grid = sum(1 for iso, ss in flat.items() if date.fromisoformat(iso)
                      in grid for _s, on in ss.items() if on)
        assert (opened, in_grid) == (16, 9), (opened, in_grid)
        members = ["C1", "C2", "C3", "C4", "C5"]
        day_slots, _log, _warns = month_solve_day(DaySolveInput(
            ym="2026-08", grid=grid, pgy_roster=["P1"],
            clerk_batches=[ClerkBatch("b1", MON, members)], biopsy_open=bio))
        _seat, bx, _rest = _counts(day_slots)
        assert sum(bx.values()) == 9, (
            f"本月的 9 個開放時段應該全部用掉(整梯 16 個時段 5 人 → "
            f"配額 3 次/人;分母只算本月的話 9//5=1,只會排 5 個): {bx}")

    def test_an_uneven_count_is_reported_even_when_it_is_only_one(self):
        """★配額制下「差 1 次」也是不一致★:舊的 min-first 容許差 1,
        而配額要求一樣多 —— 兩人都輪到過、只差 1 次也要點名
        (「輪不到」那條警告在這個情境下不會響,所以量得到門檻本身)。

        4 個開放時段(分散 4 天)、2 個人 → 配額各 2;C2 有 3 天請假 →
        只排得到 1 次,C1 拿滿 2 次。
        """
        grid, bio = _grid(MON, 12, ["101", "102", "103"],
                          ["101", "102", "103"], open_cap=4, am_only=True)
        days = [d for d in sorted(grid)
                if bio["b1"].get(d.isoformat())][:4]
        day_slots, _log, warns = month_solve_day(DaySolveInput(
            ym="2026-08", grid=grid, pgy_roster=["P1"],
            clerk_batches=[ClerkBatch("b1", MON, ["C1", "C2"])],
            biopsy_open=bio, leaves={"clerk": {"C2": set(days[:3])}}))
        _seat, bx, _rest = _counts(day_slots)
        assert (bx.get("C1", 0), bx.get("C2", 0)) == (2, 1), f"前提不成立: {bx}"
        assert not any("輪不到" in w for w in warns), warns
        assert any("不均" in w for w in warns), warns

    def test_a_locked_biopsy_for_an_unknown_code_does_not_inflate_the_quota(
            self):
        """★未知代號不得污染公平計數★(RF-10 的契約,`replay_counters` 也是
        這樣濾的):鎖定時段指派給已換梯/打錯的代號時,不可以算進配額分母 ——
        撐大配額會讓現役成員多排,反而不一致。"""
        grid, bio = _grid(MON, 12, ["101", "102", "103"],
                          ["101", "102", "103"], open_cap=3, am_only=True)
        days = sorted(grid)
        locked = {days[-1].isoformat(): {"下午": {BIOPSY: ["XX"]}}}
        day_slots, _log, _warns = month_solve_day(DaySolveInput(
            ym="2026-08", grid=grid, pgy_roster=["P1"],
            clerk_batches=[ClerkBatch("b1", MON, ["C1", "C2"])],
            biopsy_open=bio, locked=locked))
        _seat, bx, _rest = _counts(day_slots)
        assert bx.get("C1", 0) == 1 and bx.get("C2", 0) == 1, (
            f"未知代號把配額撐大了(3//2=1 才對): {bx}")

    def test_a_locked_slot_that_was_open_stops_counting_when_it_is_wasted(
            self):
        """★反例本體★(外審 Codex 配額版 R2):鎖定時段不重排 —— 所以
        「它本來標示開放」不代表排得到人。那一格若被鎖成未知/已換梯的代號
        (或空的),它就不是可分配的量,不可以留在配額的分母裡。

        4 個開放時段、2 個人;其中一格被鎖給未知代號 → 真正排得到的只有 3 格
        → 配額 `3//2 = 1`,兩人各 1 次、剩一格留空。分母若還算 4 格,
        配額會變 2 → 3 格全填 → 2/1 不一致。
        """
        grid, bio = _grid(MON, 12, ["101", "102", "103"],
                          ["101", "102", "103"], open_cap=4, am_only=True)
        days = [d for d in sorted(grid)
                if bio["b1"].get(d.isoformat())]
        locked = {days[0].isoformat(): {"上午": {BIOPSY: ["XX"]}}}
        day_slots, _log, _warns = month_solve_day(DaySolveInput(
            ym="2026-08", grid=grid, pgy_roster=["P1"],
            clerk_batches=[ClerkBatch("b1", MON, ["C1", "C2"])],
            biopsy_open=bio, locked=locked))
        _seat, bx, _rest = _counts(day_slots)
        assert (bx.get("C1", 0), bx.get("C2", 0)) == (1, 1), (
            f"被浪費掉的鎖定格仍算進配額: {bx}")

    def test_a_holiday_in_the_other_month_is_not_part_of_the_quota(self):
        """★反例本體★(外審 Codex 配額版 R2):切片格網的 UI 允許勾選所有平日,
        不會因為那一天後來變成國定假日而自動取消 —— 而假日在【那個月】的
        格網裡根本不存在,永遠排不到。算進分母就會把配額撐成排不完的量。

        本月格網 5 個開放時段(分散 5 天)、2 個人 → 整梯真正可排 5 格 →
        配額 `5//2 = 2`:兩人各 2 次,剩一格留空。
        把 2 個假日也算進去的話 cap 變 `7//2 = 3` → 5 格全填 → 3/2 不一致。
        """
        grid, _b = _grid(MON, 8, ["101", "102"], ["101", "102"])
        flat: dict = {}
        for d in sorted(grid)[:5]:
            flat[d.isoformat()] = {"上午": True}
        hol = [MON + timedelta(days=7), MON + timedelta(days=8)]
        for d in hol:
            flat[d.isoformat()] = {"上午": True}
        bio = {"b1": flat}
        day_slots, _log, _warns = month_solve_day(DaySolveInput(
            ym="2026-08", grid=grid, pgy_roster=["P1"],
            clerk_batches=[ClerkBatch("b1", MON, ["C1", "C2"])],
            biopsy_open=bio, holidays=set(hol)))
        _seat, bx, _rest = _counts(day_slots)
        assert (bx.get("C1", 0), bx.get("C2", 0)) == (2, 2), (
            f"假日被算進配額(7//2=3)→ 排出排不完的量: {bx}")

    def test_a_locked_biopsy_on_a_holiday_does_not_count(self):
        """★反例本體★(外審 Codex 配額版 R3):某平日後來變成假日 → 它掉出
        格網,主迴圈永遠不會處理它(RF-02:掉出格網的鎖定內容只原樣保留、
        不餵公平計數)。那一格的鎖定切片就算指派給有效成員,也不是這個月
        排得到的量 —— 算進分母會把 cap 撐高。

        3 個可排時段 + 1 個假日鎖定格、2 個人 → 正解 `3//2 = 1`,兩人各 1 次;
        把假日那格算進去會變 `4//2 = 2` → 3 格全填 → 2/1 不一致。
        """
        grid, _b = _grid(MON, 8, ["101", "102"], ["101", "102"])
        hol = MON + timedelta(days=7)
        grid.pop(hol, None)                       # 假日不在格網裡
        flat = {d.isoformat(): {"上午": True} for d in sorted(grid)[:3]}
        flat[hol.isoformat()] = {"上午": True}
        bio = {"b1": flat}
        locked = {hol.isoformat(): {"上午": {BIOPSY: ["C1"]}}}
        day_slots, _log, _warns = month_solve_day(DaySolveInput(
            ym="2026-08", grid=grid, pgy_roster=["P1"],
            clerk_batches=[ClerkBatch("b1", MON, ["C1", "C2"])],
            biopsy_open=bio, locked=locked, holidays={hol}))
        on_grid = {iso: v for iso, v in day_slots.items()
                   if date.fromisoformat(iso) in grid}
        _seat, bx, _rest = _counts(on_grid)
        assert (bx.get("C1", 0), bx.get("C2", 0)) == (1, 1), (
            f"假日的鎖定切片被算進配額: {bx}")

    def test_the_last_month_of_a_cross_month_batch_is_strict(self):
        """★反例本體★(外審 Codex 配額版 R2):跨月梯次到了【第二個月】一定
        看得到上個月的日期 —— 若拿「有沒有格網外的日期」當放寬條件,最終那
        一次的差異就永遠不會被點名。要看的是【之後】還排不排得到。

        本月格網涵蓋梯次的後半;前半的 2 個時段在格網外(已經過去),
        本月 3 個時段、2 個人 → 配額 2;C2 有 2 天請假 → 2/1 → 要示警。
        """
        start = MON - timedelta(days=7)           # 梯次從上週一開始
        grid, _b = _grid(MON, 5, ["101", "102"], ["101", "102"])
        flat: dict = {}
        for d in (start, start + timedelta(days=1)):
            flat[d.isoformat()] = {"上午": True}  # 上月那半段(格網外、已過去)
        days = sorted(grid)[:3]
        for d in days:
            flat[d.isoformat()] = {"上午": True}
        bio = {"b1": flat}
        day_slots, _log, warns = month_solve_day(DaySolveInput(
            ym="2026-08", grid=grid, pgy_roster=["P1"],
            clerk_batches=[ClerkBatch("b1", start, ["C1", "C2"])],
            biopsy_open=bio, leaves={"clerk": {"C2": set(days[:2])}}))
        _seat, bx, _rest = _counts(day_slots)
        assert (bx.get("C1", 0), bx.get("C2", 0)) == (2, 1), f"前提不成立: {bx}"
        assert any("不均" in w for w in warns), warns

    def test_rest_is_the_last_resort_until_the_quota_runs_out(self):
        """★最後排不進去的才放假★——但配額是上限:所有人的配額都用完之後,
        切片室就算開著也留空(使用者 2026-08-24:「讓每個人切片室相同」),
        那時坐不下的人就是放假。

        ★這一條是新規則與 2026-07-24 那次抱怨(「切片室空著、Clerk 卻在
        放假」)的交界★:配額還沒用完時仍然不可以有人閒著。
        """
        members = ["C1", "C2", "C3", "C4"]
        day_slots, _warns = self._solve(["101"], ["101"], members)
        _seat, bx, _rest = _counts(day_slots)
        assert len({bx.get(c, 0) for c in members}) == 1, \
            f"配額沒有讓每個人一樣多: {bx}"
        cap = bx.get(members[0], 0)
        done: dict = {}
        for iso in sorted(day_slots):
            d = date.fromisoformat(iso)
            for session in ("上午", "下午"):
                slots = day_slots[iso].get(session) or {}
                if d.weekday() == 2 and session == "下午":
                    continue          # 切片室恆關
                if slots.get(REST) and BIOPSY not in slots:
                    assert all(done.get(c, 0) >= cap for c in members), (
                        f"{iso} {session} 配額還沒用完卻讓人放假: {slots} / "
                        f"目前次數 {done}")
                for c in (slots.get(BIOPSY) or []):
                    done[c] = done.get(c, 0) + 1


def test_build_day_input_passes_the_year_holidays(tmp_path):
    """★接上去了才存在★:整梯配額的分子要濾掉假日,而假日只有服務層讀得到
    (`holidays_set`)—— 沒送進 `DaySolveInput` 的話,求解器手上的集合是空的,
    那條過濾等於沒有。"""
    st = RosterStorage(str(tmp_path))
    st.save_config({"r_members": [], "vs_members": [],
                    "pgy_members": [{"id": "P1"}]})
    st.save_holiday_duty({"r": {"2026-08-10": "X"}, "vs": {}})
    inp = RosterService(st).build_day_input("2026-08")
    assert date(2026, 8, 10) in inp.holidays
