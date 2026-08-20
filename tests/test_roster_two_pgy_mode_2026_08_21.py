# -*- coding: utf-8 -*-
"""[批次RS-15 / 2026-08-21 使用者] 兩位 PGY 月的日排班特別規則。

「當 PGY 只有兩位時,不能照光/治療室互卡:照光一定要有人,但二早上/
四下午/五早上只排照光,並優先把 PGY 排入跟診(101/102/103/105),治療室的
順序移到後面,且排入跟診的優先順序>Clerk。只有當該月只有兩個 PGY 時才能
這樣排。」—— 照光/治療室每時段各吃 1 位,兩人月整月互卡、完全跟不到診。

判準=該月 PGY【名單】恰 2 位(不看當日可用人數);非兩位 PGY 月行為
逐位元不變(TwoPgySeatStep 為 no-op、TreatmentStep 不跳過)。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.solve_day import (  # noqa: E402
    PHOTO, REST, TREATMENT, TWO_PGY_PHOTO_ONLY, DaySolveInput, FairCounters,
    month_solve_day, person_course_stats, solve_session,
)

TUE_AM = (date(2026, 8, 4), "上午")     # 週二早
THU_PM = (date(2026, 8, 6), "下午")     # 週四午
FRI_AM = (date(2026, 8, 7), "上午")     # 週五早
TUE_PM = (date(2026, 8, 4), "下午")     # 週二午(★不在名單內,治療室照排★)


def _session(d, session, *, pgy, clerk, rooms=("101", "102"),
             capacity=2, two=True):
    fc = FairCounters()
    return solve_session(d, session, list(rooms), pgy_avail=list(pgy),
                         clerk_avail=list(clerk), biopsy_open=False, fc=fc,
                         capacity=capacity, two_pgy_mode=two)


class TestThePhotoOnlySessions:
    def test_treatment_is_skipped_and_the_freed_pgy_follows_clinic(self):
        for d, session in (TUE_AM, THU_PM, FRI_AM):
            slots, _ = _session(d, session, pgy=["P1", "P2"], clerk=["C1"])
            assert len(slots[PHOTO]) == 1, (d, session)
            assert TREATMENT not in slots, \
                f"★{d} {session} 兩位 PGY 月仍排了治療室★"
            seated = [p for r, ps in slots.items()
                      if r not in (PHOTO, REST) for p in ps]
            other = ({"P1", "P2"} - set(slots[PHOTO])).pop()
            assert other in seated, \
                f"★被釋出的 {other} 沒有進跟診★ {slots}"

    def test_an_unlisted_session_still_staffs_treatment(self):
        d, session = TUE_PM
        slots, _ = _session(d, session, pgy=["P1", "P2"], clerk=["C1"])
        assert len(slots[PHOTO]) == 1 and len(slots[TREATMENT]) == 1
        assert set(slots[PHOTO]) | set(slots[TREATMENT]) == {"P1", "P2"}

    def test_photo_still_comes_first_when_one_pgy_is_on_leave(self):
        """照光一定要有人:只剩一位可用 → 照光拿走,沒有人跟診(不硬塞)。"""
        d, session = TUE_AM
        slots, _ = _session(d, session, pgy=["P1"], clerk=["C1"])
        assert slots[PHOTO] == ["P1"]
        assert TREATMENT not in slots
        assert all("P1" not in ps for r, ps in slots.items() if r != PHOTO)


class TestThePgyBeatsTheClerkForSeats:
    def test_the_pgy_takes_the_scarce_seat(self):
        """容量 1×單房:座位只有一個 —— 使用者定案 PGY 優先權>Clerk。
        (TwoPgySeatStep 在 ClerkSeedStep 之前;放在之後的話 Clerk 先坐滿,
        PGY 只能放假 —— 那正是這批要修掉的形狀。)"""
        d, session = TUE_AM
        slots, _ = _session(d, session, pgy=["P1", "P2"], clerk=["C1", "C2"],
                            rooms=("101",), capacity=1)
        other = ({"P1", "P2"} - set(slots[PHOTO])).pop()
        assert slots["101"] == [other], slots
        assert set(slots[REST]) == {"C1", "C2"}

    def test_three_pgy_months_keep_the_clerk_seed_order(self):
        """非兩位 PGY 月不動:同樣稀缺座位,照舊 ClerkSeed 先(1C+1P 混搭)。"""
        d, session = TUE_AM
        fc = FairCounters()
        slots, _ = solve_session(d, session, ["101"],
                                 pgy_avail=["P1", "P2", "P3"],
                                 clerk_avail=["C1"], biopsy_open=False,
                                 fc=fc, capacity=1, two_pgy_mode=False)
        assert len(slots[PHOTO]) == 1 and len(slots[TREATMENT]) == 1
        assert slots["101"] == ["C1"], "非兩位 PGY 月 Clerk 先入座的順序不得變"


class TestTheMonthLevelBehaviour:
    def _input(self, pgy):
        grid = {}
        for day in range(1, 32):
            d = date(2026, 8, day)
            if d.weekday() >= 5:
                continue
            pm = [] if d.weekday() == 2 else ["101", "102"]
            grid[d] = {"上午": ["101", "102"], "下午": pm}
        return DaySolveInput(ym="2026-08", grid=grid, pgy_roster=list(pgy))

    def test_photo_only_sessions_have_no_treatment_all_month(self):
        day_slots, _log, _warn = month_solve_day(self._input(["P1", "P2"]))
        for iso, sessions in day_slots.items():
            d = date.fromisoformat(iso)
            for session, slots in sessions.items():
                expect_skip = (d.weekday(), session) in TWO_PGY_PHOTO_ONLY
                if expect_skip:
                    assert TREATMENT not in slots, (iso, session)
                    assert PHOTO in slots, (iso, session, "照光一定要有人")
                elif d.weekday() != 2 or session != "下午":
                    assert TREATMENT in slots, (iso, session)

    def test_the_pair_actually_gets_clinic_time(self):
        """本規則的目的:兩人不再整月互卡 —— 每人整月都要有跟診次數。"""
        day_slots, _log, _warn = month_solve_day(self._input(["P1", "P2"]))
        stats = person_course_stats(
            {iso: s for iso, s in day_slots.items()}, include={"P1", "P2"})
        for p in ("P1", "P2"):
            assert stats[p]["follow"] > 0, (p, stats)
        # 照光公平性質不變:spread ≤1
        assert abs(stats["P1"]["photo"] - stats["P2"]["photo"]) <= 1

    def test_a_three_pgy_month_is_byte_identical_to_before(self):
        """★啟用條件是「恰 2 位」★:3 位月整月不得有任何「只排照光」時段。"""
        day_slots, _log, _warn = month_solve_day(self._input(["P1", "P2", "P3"]))
        for iso, sessions in day_slots.items():
            d = date.fromisoformat(iso)
            for session, slots in sessions.items():
                if d.weekday() == 2 and session == "下午":
                    continue
                assert TREATMENT in slots, (iso, session)

    def test_a_duplicated_roster_code_does_not_fake_the_mode(self):
        """名單打錯成 ["P1","P1"] 只有一個人 —— 不得因 len==2 誤啟用。"""
        day_slots, _log, _warn = month_solve_day(self._input(["P1", "P1"]))
        d_iso = "2026-08-04"
        assert TREATMENT in day_slots[d_iso]["上午"], "去重後 1 人不是兩位 PGY 月"
