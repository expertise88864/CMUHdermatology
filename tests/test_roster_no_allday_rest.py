# -*- coding: utf-8 -*-
"""[2026-07-27 使用者] 反「整天放假」：若非放假不可，也盡量讓每人每天至少有半天
有事做（跟診/照光/治療/切片皆算），而不是早上放假下午又放假。

★[RS-25 2026-08-24 使用者] 這一條降為【平手時的決勝】★——使用者的新要求是
「跟診次數不能有人兩週跟了 7 次、有人跟了 10 次」。原本把「今天還沒事做」放在
座位次數【前面】,會讓跟診次數偏高但今天閒著的人再拿一個位子(本檔原本的例子
就是 9 次 vs 3 次)—— 那正是 7 vs 10 的來源。
現在:次數少者永遠先,次數一樣時才輪到「今天還沒事做的人優先」。
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster.model import ClerkBatch  # noqa: E402
from cmuh_common.roster.solve_day import (  # noqa: E402
    BIOPSY, PHOTO, REST, TREATMENT, DaySolveInput, FairCounters,
    month_solve_day, replay_counters, solve_session,
)

_SPECIAL = (PHOTO, TREATMENT, BIOPSY, REST)


def test_seat_count_beats_idleness_when_they_conflict():
    """★[RS-25] 兩者衝突時,跟診次數優先★:H 已經跟了 9 次、L 只有 3 次 ——
    就算 H 今天整天還沒事做,這個位子仍然要給 L。

    (2026-07-27 那一版的行為相反:H 會再拿一次 → 12 次 vs 3 次。
     使用者 2026-08-24:「跟診次數不能有人兩周跟了 7 次有人跟了 10 次」。)
    """
    fc = FairCounters()
    d = date(2026, 8, 3)
    ck_hi, ck_lo = ("clerk", "bx", "H"), ("clerk", "bx", "L")
    fc.seat[ck_hi] = 9                        # H 跟診較多
    fc.seat[ck_lo] = 3                        # L 跟診較少
    fc.worked_day[ck_lo] = d                  # L 今天早上有工作、H 沒有
    slots, _log = solve_session(
        d, "下午", ["101"], pgy_avail=[], clerk_avail=["H", "L"],
        biopsy_open=False, fc=fc, capacity=1, batch_key="bx")
    assert slots["101"] == ["L"], f"跟診次數多的人又拿了一次: {slots}"
    assert slots[REST] == ["H"]


def test_idle_person_still_wins_a_tie():
    """★反整天放假仍然有效,只是降為平手決勝★:次數一樣時,今天還沒事做的人
    先補位(絕大多數時段都是這個情形)。"""
    fc = FairCounters()
    d = date(2026, 8, 3)
    fc.seat[("clerk", "bx", "H")] = 5
    fc.seat[("clerk", "bx", "L")] = 5
    fc.worked_day[("clerk", "bx", "H")] = d   # H 今天早上有工作、L 整天還閒著
    slots, _log = solve_session(
        d, "下午", ["101"], pgy_avail=[], clerk_avail=["H", "L"],
        biopsy_open=False, fc=fc, capacity=1, batch_key="bx")
    # ★反例要靠這一鍵分勝負★:這一天的決定性抖動偏好 H(見 `_jitter`),
    #   所以「今天還沒事做的人先」若不生效,選中的會是 H。
    assert slots["101"] == ["L"], f"平手時沒有優先給整天閒著的人: {slots}"


def test_morning_unaffected_everyone_idle():
    """上午時段人人皆『今日尚無工作』→ 首鍵全平手，退回原本的次數公平（行為不變）。"""
    fc = FairCounters()
    d = date(2026, 8, 3)
    fc.seat[("clerk", "bx", "H")] = 9
    fc.seat[("clerk", "bx", "L")] = 3
    slots, _log = solve_session(
        d, "上午", ["101"], pgy_avail=[], clerk_avail=["H", "L"],
        biopsy_open=False, fc=fc, capacity=1, batch_key="bx")
    assert slots["101"] == ["L"], "上午仍應由次數少者優先（不受反整天放假影響）"


def test_all_work_kinds_count_as_worked_today():
    """照光/治療室/切片室/跟診都算「今天有事做」——只要有其一，下午就不再被當閒人。"""
    d = date(2026, 8, 3)
    for kind in ("photo", "tx", "biopsy", "seat"):
        fc = FairCounters()
        if kind in ("photo", "tx"):
            am, _ = solve_session(d, "上午", [], pgy_avail=["P"], clerk_avail=[],
                                  biopsy_open=False, fc=fc)
            assert am[PHOTO] == ["P"]
            assert fc.worked_day[("pgy", "P")] == d
        elif kind == "biopsy":
            am, _ = solve_session(d, "上午", [], pgy_avail=[], clerk_avail=["C"],
                                  biopsy_open=True, fc=fc, batch_key="bx")
            assert am[BIOPSY] == ["C"]
            assert fc.worked_day[("clerk", "bx", "C")] == d
        else:
            am, _ = solve_session(d, "上午", ["101"], pgy_avail=[],
                                  clerk_avail=["C"], biopsy_open=False, fc=fc,
                                  batch_key="bx")
            assert am["101"] == ["C"]
            assert fc.worked_day[("clerk", "bx", "C")] == d


def test_replay_marks_worked_today_from_locked_morning():
    """鎖定/既存的早上時段也要餵進『今日已有工作』——否則手動鎖定的人下午會被
    誤判成整天閒著而插隊補位。"""
    fc = FairCounters()
    d = date(2026, 8, 3)
    replay_counters(fc, d, "上午", {PHOTO: ["P"], "101": ["C"], REST: ["D"]},
                    "bx", pgy_set={"P", "D"}, clerk_set={"C"})
    assert fc.worked_day.get(("pgy", "P")) == d          # 照光算有工作
    assert fc.worked_day.get(("clerk", "bx", "C")) == d  # 跟診算有工作
    assert fc.worked_day.get(("pgy", "D")) != d          # 放假不算


def test_biopsy_prefers_idle_on_count_tie():
    """切片室平手決勝也偏好今天沒事做的人（不動搖切片次數公平：次數仍是首鍵）。"""
    fc = FairCounters()
    d = date(2026, 8, 3)
    fc.biopsy_done[("bx", "X")] = 0
    fc.biopsy_done[("bx", "Y")] = 0
    fc.worked_day[("clerk", "bx", "Y")] = d       # Y 今天已有工作
    slots, _log = solve_session(
        d, "下午", [], pgy_avail=[], clerk_avail=["X", "Y"],
        biopsy_open=True, fc=fc, batch_key="bx")
    assert slots[BIOPSY] == ["X"], "平手時應選今天還沒事做的 X"


def _weekday_grid(start: date, end: date, rooms_am, rooms_pm):
    grid, bio = {}, {}
    d = start
    while d <= end:
        if d.weekday() < 5:
            pm = [] if d.weekday() == 2 else list(rooms_pm)
            grid[d] = {"上午": list(rooms_am), "下午": pm}
            bio[d.isoformat()] = {"上午": True, "下午": d.weekday() != 2}
        d += timedelta(days=1)
    return grid, bio


def test_month_allday_rest_only_on_closed_afternoons():
    """整月驗收：整天放假只會發生在【下午全院無診】的日子（週三）——其餘日子
    每個人每天至少有一個時段有事做。同時確認公平未被犧牲：
    PGY 照光/治療室差 ≤1、[RS-24] Clerk ★跟診差 ≤1★ 且切片次數一致。

    ([RS-24] 切片室改成配額平均、由跟診最多者去 —— 兩邊的次數要一起平。)"""
    grid, bio = _weekday_grid(date(2026, 8, 3), date(2026, 8, 28),
                              ["101", "103"], ["101", "102", "105"])
    batch = ClerkBatch("b1", date(2026, 8, 3), ["1", "2", "3", "4", "5"])
    day_slots, _log, _w = month_solve_day(DaySolveInput(
        ym="2026-08", grid=grid, pgy_roster=["A", "B", "C", "D"],
        clerk_batches=[batch], biopsy_open={batch.id: bio}))
    photo, tx, biopsy, seat = {}, {}, {}, {}
    idle_days: list = []
    for iso in sorted(day_slots):
        d = date.fromisoformat(iso)
        sessions = day_slots[iso]
        people, worked = set(), set()
        for slots in sessions.values():
            for slot, ps in (slots or {}).items():
                people |= set(ps)
                if slot != REST:
                    worked |= set(ps)
                for p in ps:
                    if slot == PHOTO:
                        photo[p] = photo.get(p, 0) + 1
                    elif slot == TREATMENT:
                        tx[p] = tx.get(p, 0) + 1
                    elif slot == BIOPSY:
                        biopsy[p] = biopsy.get(p, 0) + 1
                    elif slot != REST:          # 跟診房(照光/治療室/切片之外)
                        seat[p] = seat.get(p, 0) + 1
        idle = people - worked
        if idle and d.weekday() != 2:
            idle_days.append((iso, sorted(idle)))

    def spread(counts, keys):
        vals = [counts.get(k, 0) for k in keys]
        return max(vals) - min(vals)
    assert spread(photo, "ABCD") <= 1, f"PGY 照光不均: {photo}"
    assert spread(tx, "ABCD") <= 1, f"PGY 治療室不均: {tx}"
    assert spread(seat, batch.members) == 0, \
        f"[RS-34] Clerk 跟診次數要完全一致: {seat}"
    # ★整天放假唯一的合法理由★:那個人已經跟滿整梯的配額,座位因此留空。
    _target = seat[batch.members[0]]
    for iso, idle in idle_days:
        wd = '一二三四五六日'[date.fromisoformat(iso).weekday()]
        others = [p for p in idle if p not in batch.members]
        assert not others, f"{iso}(週{wd}) ★非 Clerk 整天放假★: {others}"
        short = [p for p in idle if seat.get(p, 0) != _target]
        assert not short, (
            f"{iso}(週{wd}) ★還沒跟滿就整天放假★: "
            f"{[(p, seat.get(p, 0)) for p in short]}(配額 {_target})")
    assert spread(biopsy, batch.members) <= 1, f"Clerk 切片不均: {biopsy}"
