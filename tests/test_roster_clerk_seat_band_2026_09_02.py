# -*- coding: utf-8 -*-
"""[RS-33 使用者 2026-09-02] Clerk 整梯跟診時段控制在 7-11 個。

使用者原話:
> 讓每位 Clerk 學生兩週 Course 內(總計週一至週五早上下午共 10 個時段,
> 2 週共 20 個時段,2 個時段是開會(週三下午),切片室只需要安排 1-3 個時段
> (每個人),因此讓跟診時段控制在每個人總共 7-11 個跟診時段左右
> (盡量滿足,若無法也沒關係,例如可能遇到假日/連假可能會無法滿足,
>  先滿足其他 Clerk 排班規定之後,讓控制跟診時段的條件順位在後方一點)

★兩端的性質不同,所以做法也不同★
* 上限(11)★可執行★:座位可以留空 —— 到頂的人不再入座(他去放假)。
  這是唯一會改變排班結果的地方。
* 下限(7)★不可執行★:座位本來就是能填就填(`_seat` 是 min-first)。
  填不到 7 只可能是整梯根本沒那麼多可坐的時段(假日/連假/診間少/請假)。
  所以下限做成★點名★,講事實、不假裝補得出來,而且只在★整梯走完★之後講。

★「順位在後方」怎麼落實★:上限只影響【就座】,而就座排在照光/治療室/
切片室(含配額與期限)全部排完之後 —— 其他 Clerk 規則要用的人力一個都不會
被它擋掉,它只決定「剩下的座位要不要再塞人」。
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.model import ClerkBatch  # noqa: E402
from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.solve_day import (  # noqa: E402
    BIOPSY, CLERK_SEAT_TARGET_MAX, CLERK_SEAT_TARGET_MIN, PHOTO, REST,
    TREATMENT, DaySolveInput, FairCounters, clerk_batches_ended_by,
    clerk_seat_band_warnings, is_follow_slot, month_solve_day, solve_session,
)
from cmuh_common.roster.storage import RosterStorage  # noqa: E402

MON = date(2026, 9, 7)          # 週一
BK = "bx"


def _ck(code):
    return ("clerk", BK, code)


def _seated(slots):
    """那一個時段實際坐進診間的人(跟診)。"""
    out = []
    for slot, people in (slots or {}).items():
        if not is_follow_slot(slot):
            continue
        out.extend(people or [])
    return out


# ══ 上限:到頂就留空 ══════════════════════════════════════════════════════
class TestTheSeatCapLeavesTheSeatEmpty:
    def test_a_clerk_at_the_cap_is_not_seated(self):
        """★核心★ 已經跟滿上限的人不再入座 —— 那一格★留空★。

        (`_seat` 的第一鍵已經是跟診次數 min-first,所以把「超過上限」放進
         排序鍵永遠不會生效:次數最多的人本來就排在最後,只有在其他人都一樣多
         時才會被選到 —— 而那正是要擋下來的情況。要讓總量停在上限,
         唯一的辦法是那一格不坐人。)
        """
        fc = FairCounters()
        fc.seat[_ck("1")] = CLERK_SEAT_TARGET_MAX
        slots, _log = solve_session(
            MON, "上午", ["101"], pgy_avail=[], clerk_avail=["1"],
            biopsy_open=False, fc=fc, batch_key=BK)
        assert _seated(slots) == [], f"★到上限仍被排進跟診★:{slots}"
        assert "1" in (slots.get(REST) or []), f"該去放假:{slots}"

    def test_someone_below_the_cap_still_gets_the_seat(self):
        """★對照組★:還沒到上限的人照常入座(不可以矯枉過正把大家都停掉)。"""
        fc = FairCounters()
        fc.seat[_ck("1")] = CLERK_SEAT_TARGET_MAX
        fc.seat[_ck("2")] = CLERK_SEAT_TARGET_MAX - 1
        slots, _log = solve_session(
            MON, "上午", ["101"], pgy_avail=[], clerk_avail=["1", "2"],
            biopsy_open=False, fc=fc, batch_key=BK)
        assert _seated(slots) == ["2"], f"該由還沒到上限的 2 入座:{slots}"

    def test_one_below_the_cap_exactly_at_the_boundary(self):
        """★邊界★:剛好 MAX-1 還能坐(坐完正好 MAX),等於 MAX 就不能。"""
        fc = FairCounters()
        fc.seat[_ck("1")] = CLERK_SEAT_TARGET_MAX - 1
        slots, _log = solve_session(
            MON, "上午", ["101"], pgy_avail=[], clerk_avail=["1"],
            biopsy_open=False, fc=fc, batch_key=BK)
        assert _seated(slots) == ["1"]
        assert fc.seat[_ck("1")] == CLERK_SEAT_TARGET_MAX

    def test_the_overflow_step_does_not_spin_forever(self):
        """★`_seat` 回 None 時刻意不動 pool★ —— 溢位那一步是 `while` 迴圈,
        不 break 就會空轉到掛。房多、人少、全部到頂的組合會走到它。"""
        fc = FairCounters()
        for c in ("1", "2"):
            fc.seat[_ck(c)] = CLERK_SEAT_TARGET_MAX
        slots, _log = solve_session(                 # 不可以卡住
            MON, "上午", ["101", "102", "103"], pgy_avail=[],
            clerk_avail=["1", "2"], biopsy_open=False, fc=fc, batch_key=BK)
        assert _seated(slots) == [], slots
        assert sorted(slots.get(REST) or []) == ["1", "2"], slots

    def test_the_cap_is_recomputed_between_rooms(self):
        """★每一格重算★:同一時段內前面幾間坐掉之後,有人剛好到頂 ——
        後面那幾間就不可以再排他。"""
        fc = FairCounters()
        fc.seat[_ck("1")] = CLERK_SEAT_TARGET_MAX - 1   # 坐一次就到頂
        slots, _log = solve_session(
            MON, "上午", ["101", "102"], pgy_avail=[], clerk_avail=["1"],
            biopsy_open=False, fc=fc, batch_key=BK)
        assert _seated(slots) == ["1"], f"只該坐一格:{slots}"
        assert fc.seat[_ck("1")] == CLERK_SEAT_TARGET_MAX

    def test_the_cap_is_per_batch_not_per_code(self):
        """★計數以(梯次, 代號)為鍵★(RS-26):代號跨梯重用,別梯的 1 號
        跟滿了,不可以害這一梯的 1 號坐不到。"""
        fc = FairCounters()
        fc.seat[("clerk", "other", "1")] = CLERK_SEAT_TARGET_MAX + 5
        slots, _log = solve_session(
            MON, "上午", ["101"], pgy_avail=[], clerk_avail=["1"],
            biopsy_open=False, fc=fc, batch_key=BK)
        assert _seated(slots) == ["1"], f"別梯的次數不該算到這裡:{slots}"


class TestTheCapIsLastInPriority:
    def test_the_biopsy_room_is_not_affected(self):
        """★順位在後方★:切片室排在就座之前,而且不看跟診上限 ——
        已經跟滿的人照樣要去切片(切片有自己的配額規則,那是別條要求)。"""
        fc = FairCounters()
        fc.seat[_ck("1")] = CLERK_SEAT_TARGET_MAX + 5   # 遠超上限
        slots, _log = solve_session(
            MON, "上午", ["101"], pgy_avail=[], clerk_avail=["1"],
            biopsy_open=True, fc=fc, batch_key=BK)
        assert slots.get(BIOPSY) == ["1"], f"★上限擋到了切片室★:{slots}"

    def test_pgy_seating_is_not_affected(self):
        """★只管 Clerk★:PGY 的公平是月度的,這條上限不可以碰到他們。

        (三位 PGY 才有人真的坐進診間 —— 前兩位被照光/治療室吃掉。
         `fc` 裡★兩種鍵都設高★:不論誤植的實作是用 pgy 還是 clerk 的
         命名空間查上限,P1 都會被擋掉 → 反例真的只靠這條規則分勝負。)
        """
        fc = FairCounters()
        for p in ("P1", "P2", "P3"):
            fc.seat[("pgy", p)] = CLERK_SEAT_TARGET_MAX + 3
            fc.seat[("clerk", BK, p)] = CLERK_SEAT_TARGET_MAX + 3
        slots, _log = solve_session(
            MON, "上午", ["101", "102"], pgy_avail=["P1", "P2", "P3"],
            clerk_avail=[], biopsy_open=False, fc=fc, batch_key=BK)
        assert _seated(slots), f"★PGY 被 Clerk 的上限擋住★:{slots}"


# ══ 下限:只點名,不假裝補得出來 ═══════════════════════════════════════════
class TestTheLowerBoundIsOnlyReported:
    def _batch(self, members=("1", "2")):
        return ClerkBatch(id=BK, start_monday=MON, members=list(members))

    def test_a_clerk_below_the_target_is_named(self):
        out = clerk_seat_band_warnings(
            [self._batch()], {(BK, "1"): CLERK_SEAT_TARGET_MIN - 1,
                              (BK, "2"): CLERK_SEAT_TARGET_MIN})
        assert any("跟診時段偏少" in w and "1×" in w for w in out), out

    def test_it_states_the_fact_not_an_impossible_demand(self):
        """★訊息只能陳述程式確知的事★(這個 repo 一再踩到的教訓):
        排班補不出不存在的時段,所以不可以寫成「應該要排到 7 次」。"""
        out = clerk_seat_band_warnings(
            [self._batch()], {(BK, "1"): 3, (BK, "2"): 9})
        assert out and "無法自行補足" in out[0], out
        assert "應該" not in out[0], out[0]

    def test_everyone_inside_the_band_says_nothing(self):
        """★對照組★:落在區間內就不要出聲(否則每次排班都在洗版)。"""
        out = clerk_seat_band_warnings(
            [self._batch()], {(BK, "1"): CLERK_SEAT_TARGET_MIN,
                              (BK, "2"): CLERK_SEAT_TARGET_MAX})
        assert out == [], out

    def test_above_the_cap_is_named_as_a_manual_edit(self):
        """★自動排班排不出超過上限的結果★(到頂就留空)——
        所以它一旦出現就是手動改的,要講清楚由使用者決定。"""
        out = clerk_seat_band_warnings(
            [self._batch()], {(BK, "1"): CLERK_SEAT_TARGET_MAX + 1,
                              (BK, "2"): CLERK_SEAT_TARGET_MAX})
        assert any("跟診時段偏多" in w for w in out), out
        assert any("手動" in w for w in out), out

    def test_a_batch_with_no_members_is_skipped(self):
        """★空集合不算通過,但也不要對空梯次亂點名★"""
        assert clerk_seat_band_warnings([self._batch(members=())], {}) == []

    def test_only_ids_limits_the_scope(self):
        """與切片室點名同一個約定:只對本月確有被排的梯次示警,
        否則邊界梯次每個月都被誤報一次。"""
        out = clerk_seat_band_warnings(
            [self._batch()], {(BK, "1"): 1}, only_ids={"other"})
        assert out == [], out


class TestTheShortfallWaitsForTheCourseToEnd:
    """★會固定誤報的警告=沒有警告★:梯次跨月時,排完第一個月必然不足。"""

    B = ClerkBatch(id=BK, start_monday=MON, members=["1", "2"])

    def test_an_unfinished_course_is_not_called_short(self):
        out = clerk_seat_band_warnings(
            [self.B], {(BK, "1"): 2, (BK, "2"): 2}, ended_ids=set())
        assert out == [], f"★梯次還沒走完就喊偏少★:{out}"

    def test_a_finished_course_is(self):
        """★對照組★:閘門不是「永遠閉嘴」。"""
        out = clerk_seat_band_warnings(
            [self.B], {(BK, "1"): 2, (BK, "2"): 2}, ended_ids={BK})
        assert any("跟診時段偏少" in w for w in out), out

    def test_the_gate_does_not_silence_the_over_cap_half(self):
        """★只擋偏少那一半★:次數只會往上加,超過上限在中途就已經是定局,
        而且★早點講才來得及改回去★。"""
        out = clerk_seat_band_warnings(
            [self.B], {(BK, "1"): CLERK_SEAT_TARGET_MAX + 1, (BK, "2"): 9},
            ended_ids=set())
        assert any("跟診時段偏多" in w for w in out), out
        assert not any("偏少" in w for w in out), out

    def test_ended_by_uses_the_last_day_of_the_course(self):
        """梯次 = 兩週(起始那天算第一天)→ 9/07 起的梯次最後一天是 9/20。"""
        assert clerk_batches_ended_by([self.B], date(2026, 9, 20)) == {BK}
        assert clerk_batches_ended_by([self.B], date(2026, 9, 19)) == set()

    def test_broken_batch_data_does_not_raise(self):
        """壞梯次資料(人工合併月檔後很常見)只是不點名,不可以炸掉整個驗證。"""
        class _Bad:
            id = "bad"
        assert clerk_batches_ended_by([_Bad()], MON) == set()


# ══ 端到端:真的排一梯出來 ═══════════════════════════════════════════════
def _grid(start: date, days: int, rooms):
    grid = {}
    d = start
    while d < start + timedelta(days=days):
        if d.weekday() < 5:
            grid[d] = {"上午": list(rooms),
                       "下午": [] if d.weekday() == 2 else list(rooms)}
        d += timedelta(days=1)
    return grid


def _follow_counts(day_slots, members):
    out = dict.fromkeys(members, 0)
    for _iso, sessions in (day_slots or {}).items():
        for _s, slots in (sessions or {}).items():
            for slot, people in (slots or {}).items():
                if not is_follow_slot(slot):
                    continue
                for p in people or []:
                    if p in out:
                        out[p] += 1
    return out


class TestAWholeCourse:
    MEMBERS = ["C1", "C2", "C3"]

    def _solve(self, rooms, days=30):
        grid = _grid(date(2026, 9, 1), days, rooms)
        return month_solve_day(DaySolveInput(
            ym="2026-09", grid=grid, pgy_roster=["P1", "P2"],
            clerk_batches=[ClerkBatch("b1", MON, list(self.MEMBERS))],
            biopsy_open={"b1": {}}, leaves={}, locked={}))

    def test_a_roomy_course_lands_inside_the_band(self):
        """★使用者要的畫面★:時段夠多時,每個人都落在 7-11 —— 上限把
        原本會衝到十幾次的人擋下來,而不是讓少數人跟到滿。"""
        day_slots, _log, warns = self._solve(["101", "102", "103"])
        cnt = _follow_counts(day_slots, self.MEMBERS)
        band = {c: n for c, n in cnt.items()
                if not CLERK_SEAT_TARGET_MIN <= n <= CLERK_SEAT_TARGET_MAX}
        assert not band, f"★有人跑出 7-11★:{cnt}"
        assert not any("跟診時段偏" in w for w in warns), warns

    def test_a_starved_course_is_reported_not_faked(self):
        """★時段真的不夠時★(使用者說的連假:整梯只開診到 9/09 就沒了)——
        補不出來就照實說,不假裝排得出 7 次。"""
        day_slots, _log, warns = self._solve(["101"], days=9)
        cnt = _follow_counts(day_slots, self.MEMBERS)
        assert min(cnt.values()) < CLERK_SEAT_TARGET_MIN, cnt
        assert any("跟診時段偏少" in w for w in warns), warns

    def test_the_solver_never_exceeds_the_cap(self):
        day_slots, _log, _w = self._solve(["101", "102", "103", "104", "105"])
        cnt = _follow_counts(day_slots, self.MEMBERS)
        assert max(cnt.values()) <= CLERK_SEAT_TARGET_MAX, cnt


# ══ 現況點名(手改之後要有人再說一次)══════════════════════════════════════
class TestTheServiceChecksTheSavedRoster:
    """★求解當下說過的話,手改之後要有人再說一次★(與 RS-28 同一個理由)。
    而且★上限那一半只有在這裡才量得到★:求解器排不出超額。"""

    def _svc(self, tmp_path, day_slots):
        st = RosterStorage(str(tmp_path))
        st.save_config({"pgy_members": [{"id": "P1"}], "r_members": [],
                        "vs_members": [], "room_capacity": 3})
        st.save_clinic_template({"template": {
            str(w): {"上午": [{"room": "101"}], "下午": [{"room": "101"}]}
            for w in range(5)}})
        st.save_clerk_batches([{"id": "b1", "start_monday": MON.isoformat(),
                                "members": ["C1", "C2"]}])
        st.save_month("2026-09", {"day_slots": day_slots})
        return RosterService(st)

    def _rows(self, c1_times, c2_times=0, pgy_times=0):
        """把跟診格直接寫進月檔(＝使用者在月曆上手動排的樣子)。

        ★格子必須落在梯次自己的兩週內★:逐日歸屬用的是勝者梯次,寫到梯次
        範圍外的日子會被算成「不屬於任何梯次」而完全不計 —— 那量到的就不是
        這條規則了。18 格 = 兩週平日早/午扣掉兩個週三下午。
        """
        slots = []
        for i in range(14):
            d = MON + timedelta(days=i)
            if d.weekday() >= 5:
                continue
            for sess in ("上午", "下午"):
                if d.weekday() == 2 and sess == "下午":
                    continue            # 週三下午開會,不排
                slots.append((d.isoformat(), sess))
        assert len(slots) >= max(c1_times, c2_times, pgy_times)
        out: dict = {}
        for who, n in (("C1", c1_times), ("C2", c2_times), ("P1", pgy_times)):
            for iso, sess in slots[:n]:
                room = out.setdefault(iso, {}).setdefault(
                    sess, {}).setdefault("101", [])
                room.append(who)
        return out

    def test_a_manual_edit_over_the_cap_is_named(self, tmp_path):
        svc = self._svc(tmp_path, self._rows(CLERK_SEAT_TARGET_MAX + 1, 9))
        out = svc.validate_course_seat_band("2026-09")
        assert any("跟診時段偏多" in w and "C1" in w for w in out), out

    def test_pgy_in_the_same_room_is_not_counted_as_a_clerk(self, tmp_path):
        """★跟診房裡 PGY 與 Clerk 坐在一起★,而 PGY 一整個月本來就遠超 11 次
        —— 區間點名不可以把他們算進來。

        (擋住這件事的是「點名★只讀 `b.members`★」,不是次數統計那一側的
         過濾:那道過濾在每個可達狀態下都分不出勝負,已當死碼刪掉。
         這條測試釘的是前者 —— 改成迭代 counts 的鍵就會紅。)
        """
        rows = self._rows(0, 0, pgy_times=CLERK_SEAT_TARGET_MAX + 4)
        svc = self._svc(tmp_path, rows)
        out = svc.validate_course_seat_band("2026-09")
        assert not any("偏多" in w for w in out), f"PGY 被算成 Clerk:{out}"

    def test_a_month_with_nothing_scheduled_says_nothing(self, tmp_path):
        svc = self._svc(tmp_path, {})
        assert svc.validate_course_seat_band("2026-09") == []

    def test_it_is_wired_into_the_month_validation(self, tmp_path):
        """★沒有呼叫端 = 這個點名不存在★:使用者看到的是月驗證那個面板。"""
        svc = self._svc(tmp_path, self._rows(CLERK_SEAT_TARGET_MAX + 1, 9))
        assert any("跟診時段偏多" in w
                   for w in svc.quick_validate_day("2026-09")), \
            "月驗證面板沒有帶出跟診區間點名"


def test_the_follow_slot_rule_has_one_definition():
    """★特別格以外的一律是跟診★ —— 週期統計與 service 走同一份定義。"""
    assert not any(is_follow_slot(s)
                   for s in (PHOTO, TREATMENT, BIOPSY, REST))
    assert is_follow_slot("101") and is_follow_slot("門診A")


def test_the_target_band_matches_what_the_user_asked_for():
    """使用者定的是 7-11。改這兩個數字是政策變更,要有人明確決定。"""
    assert (CLERK_SEAT_TARGET_MIN, CLERK_SEAT_TARGET_MAX) == (7, 11)


# ══ 未來的鎖定跟診要先預留(外審 RS-33 R1 P2)═══════════════════════════════
class TestFutureLockedFollowSlotsAreReserved:
    """★主迴圈是時序的★:較晚日期鎖定的跟診,走到那天才由 `replay_counters`
    計入 `fc.seat`。只看當下的計數,較早的日子會把他自動排到 11 次,鎖定那一次
    再加上去 = 整梯 12 次 —— ★自動排班自己突破了它宣稱的上限★,而事後的點名
    還會把它歸因成「手動調整」。與切片配額的「未來鎖定要先扣」同一個判準。"""

    MEMBERS = ["C1", "C2", "C3"]

    def _run(self, locked=None, course_fixed=None):
        grid = _grid(date(2026, 9, 1), 30, ["101", "102", "103"])
        return month_solve_day(DaySolveInput(
            ym="2026-09", grid=grid, pgy_roster=["P1", "P2"],
            clerk_batches=[ClerkBatch("b1", MON, list(self.MEMBERS))],
            biopsy_open={"b1": {}}, leaves={}, locked=locked or {},
            course_fixed=course_fixed or {}))

    def _last_day_locked(self):
        """梯次最後一個上班日(9/18 週五)鎖一格 C1 的跟診。"""
        return {date(2026, 9, 18).isoformat(): {"上午": {"101": ["C1"]}}}

    def test_the_total_still_stops_at_the_cap(self):
        day_slots, _log, _w = self._run(locked=self._last_day_locked())
        cnt = _follow_counts(day_slots, self.MEMBERS)
        assert cnt["C1"] <= CLERK_SEAT_TARGET_MAX, \
            f"★鎖定那一次讓整梯超過上限★:{cnt}"

    def test_the_locked_slot_is_still_kept(self):
        """★預留不是把他排除★:鎖定那一格原樣保留,而且真的算他一次。"""
        locked = self._last_day_locked()
        day_slots, _log, _w = self._run(locked=locked)
        assert day_slots["2026-09-18"]["上午"] == {"101": ["C1"]}
        assert _follow_counts(day_slots, self.MEMBERS)["C1"] >= 1

    def test_a_fixed_slot_in_the_next_month_counts_too(self):
        """★相鄰月份的既定時段★(RS-29):梯次跨月時,下個月那幾格也是他的
        —— 位置一律排在本月之後 → 同樣要預留。

        (梯次改成 9/21 起,第二週才會落到十月;本月仍有 8 個上班日,
         C1 不預留的話會被排滿 11 次。)
        """
        grid = _grid(date(2026, 9, 1), 30, ["101", "102", "103"])
        nxt = {i: {"上午": {"101": ["C1"]}}
               for i in ("2026-10-01", "2026-10-02")}
        day_slots, _log, _w = month_solve_day(DaySolveInput(
            ym="2026-09", grid=grid, pgy_roster=["P1", "P2"],
            clerk_batches=[ClerkBatch("b1", date(2026, 9, 21),
                                      list(self.MEMBERS))],
            biopsy_open={"b1": {}}, leaves={}, locked={},
            course_days={"2026-10-01", "2026-10-02"}, course_fixed=nxt))
        cnt = _follow_counts(day_slots, self.MEMBERS)
        assert cnt["C1"] <= CLERK_SEAT_TARGET_MAX - 2, \
            f"★下個月的既定跟診沒有被預留★:{cnt}"

    def test_a_past_locked_slot_is_not_charged_twice(self):
        """★已經跑過的鎖定不可以再預留一次★:它已經在 `fc.seat` 裡了 ——
        重複扣會讓那個人白白少跟一次(切片配額的 `o > _now` 正是為此存在)。"""
        early = {(MON + timedelta(days=1)).isoformat():
                 {"上午": {"101": ["C1"]}}}
        day_slots, _log, _w = self._run(locked=early)
        cnt = _follow_counts(day_slots, self.MEMBERS)
        assert cnt["C1"] == CLERK_SEAT_TARGET_MAX, \
            f"過去的鎖定被扣了兩次:{cnt}"

    def test_only_the_batch_members_are_reserved(self):
        """RF-10:鎖定格裡的未知/已換梯代號不得吃掉這一梯的額度。

        ★誠實標註★:沒有任何一個突變能單獨讓這條紅 —— 預留表是
        `for c in _members` 建的,非成員的鍵永遠讀不到,所以盤點那一側再濾
        一次是死碼(已刪)。留著這條是釘住「讀取端以成員為定義域」這個性質。
        """
        locked = {date(2026, 9, 18).isoformat():
                  {"上午": {"101": ["路人甲"]}}}
        day_slots, _log, _w = self._run(locked=locked)
        cnt = _follow_counts(day_slots, self.MEMBERS)
        assert max(cnt.values()) == CLERK_SEAT_TARGET_MAX, \
            f"別人的鎖定不該吃掉本梯的額度:{cnt}"

    def test_a_locked_biopsy_or_rest_does_not_eat_the_follow_quota(self):
        """★特別格不是跟診★:同一個人較晚被鎖進切片室/放假,不可以佔掉他的
        跟診額度 —— 那會讓他整梯少跟一次,而且理由是憑空的。

        ★鎖在整梯最後一個時段★(9/18 下午):鎖在中間的話,那一格過去之後
        預留就歸零,後面剩下的時段還補得回 11 次 —— 反例會分不出勝負。
        """
        locked = {date(2026, 9, 18).isoformat():
                  {"下午": {BIOPSY: ["C1"], REST: ["C2"]}}}
        day_slots, _log, _w = self._run(locked=locked)
        cnt = _follow_counts(day_slots, self.MEMBERS)
        assert cnt["C1"] == CLERK_SEAT_TARGET_MAX, f"切片被當成跟診:{cnt}"
        assert cnt["C2"] == CLERK_SEAT_TARGET_MAX, f"放假被當成跟診:{cnt}"
