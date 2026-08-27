# -*- coding: utf-8 -*-
"""[批次RS-31 / 2026-08-27 使用者] 兩位 PGY 月:二早/四下/五早跟診要【週內輪替】。

PGY 反映:RS-15 讓這三個時段有一位跟診學習,但排出來會「這週全是 A、下週
全是 B」—— 因為照光挑人只看 photo_total,週內誰跟診純屬奇偶性副作用。
使用者要求改成輪替:同一週內 A/B 都要跟到診(不同時段),不要整週固定一人。

修法(仿週三下午 photo_wed_pm 的既有模式):新增週別跟診計數
`two_pgy_follow_week`,這三個時段的照光輪選以「這週已跟診多的先去照光」為
第一鍵、photo_total 降為第二鍵 —— 跟診自然輪給另一位;偏差由其後時段的
min-first 自行收斂。★本月鎖定時段的跟診在主迴圈前【整月預掃】入帳★
(外審 R1 P2:主迴圈是時序的,回放走到那天才入帳的話,未來的鎖定影響不了
更早的自動時段)—— 預掃是鎖定跟診的【唯一】寫入點,`replay_counters`
刻意不記(否則重複);上月不入帳(PGY 公平是月度的)。
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.solve_day import (  # noqa: E402
    PHOTO, REST, TREATMENT, DaySolveInput, FairCounters, _follow_week_key,
    _jitter, month_solve_day, person_course_stats, solve_session,
)

TUE = date(2026, 8, 4)               # 二早(三時段之一)
P12 = {"P1", "P2"}


def _seated(slots) -> list:
    """該時段坐進診間的人(照光/放假之外的所有格)。"""
    return [p for r, ps in slots.items()
            if r not in (PHOTO, REST, TREATMENT) for p in ps]


def _session(d, session, fc, *, pgy=("P1", "P2"), two=True):
    return solve_session(d, session, ["101", "102"], pgy_avail=list(pgy),
                         clerk_avail=[], biopsy_open=False, fc=fc,
                         capacity=2, two_pgy_mode=two)[0]


# ══ ① 規則本體:這週已跟診者不再連跟,照光把位子讓出來 ═══════════════════
class TestTheFollowRotatesWithinTheWeek:
    def test_who_followed_this_week_hands_the_seat_over(self):
        """★反例只靠週別計數分勝負★:P2 這週已跟診 1 次,而 photo_total
        明明是 P1 較少(3 vs 4)—— 舊輪選會再叫 P1 照光、P2 連跟;新規則
        讓 P2 去照光、跟診輪給 P1。"""
        fc = FairCounters()
        fc.photo_total = {"P1": 3, "P2": 4}
        fc.two_pgy_follow_week[_follow_week_key(TUE, "P2")] = 1
        slots = _session(TUE, "上午", fc)
        assert slots[PHOTO] == ["P2"], slots
        assert _seated(slots) == ["P1"], slots

    def test_photo_total_is_still_the_second_key(self):
        """★週別計數平手時仍回到 photo_total★:兩人這週都沒跟過,照光必給
        次數少的 P2 —— 不可以直接掉到抖動。

        (2026-08-14 上午的抖動偏向 P1,在測試裡驗明 —— 少了 photo_total
        這一鍵的話這個反例必翻盤,不是巧合綠燈。)"""
        d = date(2026, 8, 14)          # 五早(三時段之一)
        assert (_jitter(d, "上午", "photo", "P1")
                < _jitter(d, "上午", "photo", "P2")), "反例失去鑑別力"
        fc = FairCounters()
        fc.photo_total = {"P1": 1, "P2": 0}
        slots = _session(d, "上午", fc)
        assert slots[PHOTO] == ["P2"], slots

    def test_last_weeks_follows_do_not_leak_into_this_week(self):
        """★計數是【週別】的★:上週 P1 跟了再多次,這週重新起算 ——
        本週兩人平手 → photo_total 決定(P2 次數少去照光)。
        全域計數的話 P1 的 5 次會壓過一切、被推去照光。"""
        fc = FairCounters()
        fc.photo_total = {"P1": 1, "P2": 0}
        fc.two_pgy_follow_week[
            _follow_week_key(TUE - timedelta(days=7), "P1")] = 5
        slots = _session(TUE, "上午", fc)
        assert slots[PHOTO] == ["P2"], slots

    def test_a_three_pgy_month_ignores_the_counter(self):
        """★只在兩位 PGY 月讀取★:3 位月就算計數裡有值(鎖定回放不分模式
        會寫),照光輪選照舊走 photo_total —— P2 次數最少必去照光。"""
        fc = FairCounters()
        fc.photo_total = {"P1": 5, "P2": 0, "P3": 1}
        fc.two_pgy_follow_week[_follow_week_key(TUE, "P1")] = 1
        slots = _session(TUE, "上午", fc, pgy=("P1", "P2", "P3"), two=False)
        assert slots[PHOTO] == ["P2"], slots
        assert len(slots[TREATMENT]) == 1, "3 位月治療室照排"


# ══ ② 計數的寫入邊界 ═════════════════════════════════════════════════════
class TestTheCounterIsWrittenAtTheRightPlaces:
    def test_a_live_seat_on_a_listed_session_is_counted(self):
        fc = FairCounters()
        slots = _session(TUE, "上午", fc)
        who = _seated(slots)[0]
        assert fc.two_pgy_follow_week == {_follow_week_key(TUE, who): 1}

    def test_a_wednesday_pm_seat_is_not_counted(self):
        """週三下午治療室本就休診,釋出的入座不屬於三時段輪替 —— 不記。"""
        fc = FairCounters()
        slots = _session(date(2026, 8, 5), "下午", fc)
        assert _seated(slots), "前提:週三下午確實有人入座"
        assert fc.two_pgy_follow_week == {}

    def test_a_locked_monday_seat_is_not_a_trio_follow(self):
        """★預掃只認三時段★:週一早鎖了 P1 跟診 —— 那不是三時段的學習場,
        不可以讓二早的輪替被它牽著走。鎖定內容把兩人 photo_total 疊成平手,
        二早(自動)回到抖動決勝(偏向 P2 照光,測試裡驗明);錯把週一入帳
        的話 P1 會被強推去照光。"""
        assert (_jitter(TUE, "上午", "photo", "P2")
                < _jitter(TUE, "上午", "photo", "P1")), "反例失去鑑別力"
        mon = date(2026, 8, 3)
        locked = {
            "2026-08-03": {"上午": {PHOTO: ["P2"], "101": ["P1"]},
                           "下午": {PHOTO: ["P1"], TREATMENT: ["P2"]}},
        }
        grid = {d: {"上午": ["101", "102"], "下午": ["101", "102"]}
                for d in (mon, TUE)}
        day_slots, _log, _warn = month_solve_day(DaySolveInput(
            ym="2026-08", grid=grid, pgy_roster=["P1", "P2"], locked=locked))
        tue = day_slots["2026-08-04"]["上午"]
        assert tue[PHOTO] == ["P2"], tue
        assert _seated(tue) == ["P1"], tue

    def test_a_locked_rest_is_not_a_follow(self):
        """★放假不是跟診★:二早鎖成 P1 照光、P2【放假】—— P2 沒有跟到診,
        不可以在四午被當成「已經跟過」而強推去照光。鎖定把 P2 的 photo_total
        疊高(1 vs 2)→ 四午照 photo_total 給 P1 照光、P2 跟診;錯把放假
        入帳的話會反過來。"""
        locked = {
            "2026-08-04": {"上午": {PHOTO: ["P1"], REST: ["P2"]},
                           "下午": {PHOTO: ["P2"], TREATMENT: ["P1"]}},
            "2026-08-06": {"上午": {PHOTO: ["P2"], TREATMENT: ["P1"]}},
        }
        grid = {d: {"上午": ["101", "102"], "下午": ["101", "102"]}
                for d in (TUE, date(2026, 8, 6))}
        day_slots, _log, _warn = month_solve_day(DaySolveInput(
            ym="2026-08", grid=grid, pgy_roster=["P1", "P2"], locked=locked))
        thu = day_slots["2026-08-06"]["下午"]
        assert thu[PHOTO] == ["P1"], thu
        assert _seated(thu) == ["P2"], thu


# ══ ③ 月級:使用者要的性質本身 ════════════════════════════════════════════
def _month_input(pgy, locked=None):
    grid = {}
    for day in range(1, 32):
        d = date(2026, 8, day)
        if d.weekday() >= 5:
            continue
        pm = [] if d.weekday() == 2 else ["101", "102"]
        grid[d] = {"上午": ["101", "102"], "下午": pm}
    return DaySolveInput(ym="2026-08", grid=grid, pgy_roster=list(pgy),
                         locked=locked or {})


def _trio_seats(day_slots, monday) -> list:
    out = []
    for d, s in ((monday + timedelta(days=1), "上午"),
                 (monday + timedelta(days=3), "下午"),
                 (monday + timedelta(days=4), "上午")):
        ss = day_slots[d.isoformat()][s]
        out += [p for p in _seated(ss) if p in P12]
    return out


class TestEveryFullWeekMixesBothPgys:
    def test_no_week_is_all_the_same_person(self):
        """★使用者的原話★:「不要同一週都一個人固定跟診」—— 2026-08 的四個
        完整週,每週三個時段的跟診都要 A/B 兩人都出現。"""
        day_slots, _log, _warn = month_solve_day(_month_input(["P1", "P2"]))
        for monday in (date(2026, 8, 3), date(2026, 8, 10),
                       date(2026, 8, 17), date(2026, 8, 24)):
            seats = _trio_seats(day_slots, monday)
            assert len(seats) == 3, (monday, seats)
            assert set(seats) == P12, \
                f"★{monday} 那一週的跟診全是同一人★ {seats}"

    def test_the_photo_spread_stays_within_one(self):
        """RS-15 的照光公平性質不得被本批犧牲:月底 spread ≤1 照舊。"""
        day_slots, _log, _warn = month_solve_day(_month_input(["P1", "P2"]))
        stats = person_course_stats(dict(day_slots), include=P12)
        assert abs(stats["P1"]["photo"] - stats["P2"]["photo"]) <= 1, stats

    def test_a_locked_half_week_steers_the_live_half(self):
        """★鎖定半週、解另一半,輪替要接得上★:二早鎖成 P1 跟診,而鎖定的
        其他時段刻意把 P1 的 photo_total 疊高(3 vs 1)—— 沒有回放週別計數的
        話,五早會照 photo_total 叫 P2 照光、P1 連跟;正解是 P1 已跟過,
        五早照光給 P1、跟診輪給 P2。"""
        locked = {
            "2026-08-04": {"上午": {PHOTO: ["P2"], "101": ["P1"]},
                           "下午": {PHOTO: ["P1"], TREATMENT: ["P2"]}},
            "2026-08-05": {"上午": {PHOTO: ["P1"], TREATMENT: ["P2"]},
                           "下午": {PHOTO: ["P1"]}},
        }
        grid = {d: {"上午": ["101", "102"],
                    "下午": [] if d.weekday() == 2 else ["101", "102"]}
                for d in (date(2026, 8, 4), date(2026, 8, 5),
                          date(2026, 8, 7))}
        inp = DaySolveInput(ym="2026-08", grid=grid,
                            pgy_roster=["P1", "P2"], locked=locked)
        day_slots, _log, _warn = month_solve_day(inp)
        fri = day_slots["2026-08-07"]["上午"]
        assert fri[PHOTO] == ["P1"], fri
        assert _seated(fri) == ["P2"], fri


class TestFutureLocksSteerEarlierSessions:
    """★外審 RS-31 R1 P2★:主迴圈是時序的,鎖定時段走到那天才回放 ——
    週四/週五鎖了 P1 跟診、週二自動排班時看不到的話,P1 連跟三場。
    修法:本月鎖定的三時段跟診在主迴圈前【預先】入帳,回放走到時跳過。"""

    def _grid(self, *days):
        return {d: {"上午": ["101", "102"], "下午": ["101", "102"]}
                for d in days}

    def test_a_future_locked_follow_steers_an_earlier_session(self):
        """週四午+週五早都鎖成 P1 跟診 → 週二早(自動)的跟診必須輪給 P2。

        (★反例的鑑別力★:2026-08-04 上午的抖動偏向 P2 去照光 —— 沒有
        預先入帳的話,週二會排成 P2 照光、P1 又跟診,測試裡驗明。)"""
        assert (_jitter(TUE, "上午", "photo", "P2")
                < _jitter(TUE, "上午", "photo", "P1")), "反例失去鑑別力"
        locked = {
            "2026-08-06": {"下午": {PHOTO: ["P2"], "101": ["P1"]}},
            "2026-08-07": {"上午": {PHOTO: ["P2"], "101": ["P1"]}},
        }
        inp = DaySolveInput(
            ym="2026-08",
            grid=self._grid(TUE, date(2026, 8, 6), date(2026, 8, 7)),
            pgy_roster=["P1", "P2"], locked=locked)
        day_slots, _log, _warn = month_solve_day(inp)
        tue = day_slots["2026-08-04"]["上午"]
        assert tue[PHOTO] == ["P1"], tue
        assert _seated(tue) == ["P2"], tue

    def test_a_replayed_lock_is_not_counted_twice(self):
        """★預先入帳過的鎖定,回放走到時要跳過★:二早鎖 P1 跟診、五早鎖
        P2 跟診 → 四午(自動)時兩人本週各跟 1 次=平手,照光回到 photo_total
        (P2 次數少 → P2 照光、P1 跟診)。重複入帳的話 P1 會被灌成 2 次、
        被強推去照光 —— 平手決勝權被偷走。"""
        locked = {
            "2026-08-04": {"上午": {PHOTO: ["P2"], "101": ["P1"]},
                           "下午": {PHOTO: ["P1"], TREATMENT: ["P2"]}},
            "2026-08-06": {"上午": {PHOTO: ["P1"], TREATMENT: ["P2"]}},
            "2026-08-07": {"上午": {PHOTO: ["P1"], "101": ["P2"]}},
        }
        inp = DaySolveInput(
            ym="2026-08",
            grid=self._grid(TUE, date(2026, 8, 6), date(2026, 8, 7)),
            pgy_roster=["P1", "P2"], locked=locked)
        day_slots, _log, _warn = month_solve_day(inp)
        thu = day_slots["2026-08-06"]["下午"]
        assert thu[PHOTO] == ["P2"], thu
        assert _seated(thu) == ["P1"], thu

    def test_an_out_of_grid_lock_is_not_pre_accounted(self):
        """RF-02 的既有定案:掉出格網的鎖定【不餵計數】—— 預掃也要守同一條
        邊界(只掃格網內的日子)。8/11 不在格網卻鎖了 P1 跟診:四午(本週
        第一個活時段)必須當作兩人都還沒跟過 → photo_total 決定(P2 照光);
        預掃錯把它入帳的話 P1 會被強推去照光。"""
        locked = {
            "2026-08-11": {"上午": {PHOTO: ["P2"], "101": ["P1"]}},
            "2026-08-13": {"上午": {PHOTO: ["P1"], TREATMENT: ["P2"]}},
        }
        inp = DaySolveInput(
            ym="2026-08",
            grid=self._grid(date(2026, 8, 13), date(2026, 8, 14)),
            pgy_roster=["P1", "P2"], locked=locked)
        day_slots, _log, _warn = month_solve_day(inp)
        thu = day_slots["2026-08-13"]["下午"]
        assert thu[PHOTO] == ["P2"], thu
        assert _seated(thu) == ["P1"], thu
