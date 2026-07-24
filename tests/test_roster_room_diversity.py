# -*- coding: utf-8 -*-
"""[2026-07-24 使用者] 跟診房多樣性：每人盡量輪過 101~105 各診、不固定都跟同一診、
不連排同房；診間處理順序決定性洗牌（人少於房時不再永遠只填低房號）；
Apply 本科 101 週二/五偏好不因洗牌失效；RF-09 房計數跨月延續。"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster.model import ClerkBatch  # noqa: E402
from cmuh_common.roster.solve_day import (  # noqa: E402
    BIOPSY, PHOTO, REST, TREATMENT, DaySolveInput, FairCounters, SessionCtx,
    _room_order, month_solve_day, replay_counters,
)

_SPECIAL = (PHOTO, TREATMENT, BIOPSY, REST)


def _weekday_grid(ym: str, rooms: list) -> dict:
    """整月每個工作日早/午都開同一組診間（週三下午照制度關閉）。"""
    y, m = int(ym[:4]), int(ym[5:7])
    grid = {}
    d = date(y, m, 1)
    while d.month == m:
        if d.weekday() < 5:
            pm = [] if d.weekday() == 2 else list(rooms)
            grid[d] = {"上午": list(rooms), "下午": pm}
        d += timedelta(days=1)
    return grid


def _follow_seq(day_slots: dict) -> dict:
    """{人: [(iso, session, 房), ...]}（時間序；只算跟診格）。"""
    seq: dict = {}
    for iso in sorted(day_slots):
        for session in ("上午", "下午"):
            for slot, people in (day_slots[iso].get(session) or {}).items():
                if slot in _SPECIAL:
                    continue
                for p in people:
                    seq.setdefault(p, []).append((iso, session, slot))
    return seq


def test_few_students_no_longer_stuck_in_lowest_room():
    """學生少於診間數：原固定房號升冪 → 唯一被排跟診的人永遠在 101、
    103/105 從輪不到 → 洗牌後整月各診都該有人跟過、每人跟過 ≥2 間。"""
    inp = DaySolveInput(ym="2026-08",
                        grid=_weekday_grid("2026-08", ["101", "103", "105"]),
                        pgy_roster=["P1", "P2", "P3"])   # 照光+治療室吃 2、跟診 1
    day_slots, _log, _w = month_solve_day(inp)
    seq = _follow_seq(day_slots)
    rooms_used = {room for s in seq.values() for _, _, room in s}
    assert rooms_used == {"101", "103", "105"}, f"有診整月沒人跟: {rooms_used}"
    for p, s in seq.items():
        assert len({room for _, _, room in s}) >= 2, f"{p} 整月只跟同一診: {s}"


def test_room_counts_balanced_per_person():
    """每人跟過各診的次數盡量平均（兩診每日早午都開、2 人就座 → 每人 101/102
    次數差 ≤2 且都 >0）。"""
    inp = DaySolveInput(ym="2026-08",
                        grid=_weekday_grid("2026-08", ["101", "102"]),
                        pgy_roster=["P1", "P2", "P3", "P4"])
    day_slots, _log, _w = month_solve_day(inp)
    for p, s in _follow_seq(day_slots).items():
        c101 = sum(1 for _, _, r in s if r == "101")
        c102 = sum(1 for _, _, r in s if r == "102")
        assert c101 > 0 and c102 > 0, f"{p} 沒輪過其中一診 (101={c101},102={c102})"
        assert abs(c101 - c102) <= 2, f"{p} 房分布失衡 (101={c101},102={c102})"


def test_no_long_same_room_streak():
    """反連排：任何人不得連續 4 次以上跟同一診（房計數+連排懲罰壓抑長串；
    總次數公平仍是主鍵,偶發 3 連可容忍——實測本情境每人最長 3、分布 9/10）。"""
    inp = DaySolveInput(ym="2026-08",
                        grid=_weekday_grid("2026-08", ["101", "102"]),
                        pgy_roster=["P1", "P2", "P3", "P4"])
    day_slots, _log, _w = month_solve_day(inp)
    for p, s in _follow_seq(day_slots).items():
        rooms = [r for _, _, r in s]
        cur = longest = 1
        for i in range(1, len(rooms)):
            cur = cur + 1 if rooms[i] == rooms[i - 1] else 1
            longest = max(longest, cur)
        assert longest <= 3, f"{p} 連 {longest} 次同診: {rooms}"


def test_room_order_is_shuffled_but_deterministic():
    """洗牌＝決定性抖動：同輸入恆同序（可重現）、逐日變化（整月至少兩種順序）、
    永遠是原診間集合的一個排列。"""
    orders = set()
    for day in range(3, 29):
        d = date(2026, 8, day)
        if d.weekday() >= 5:
            continue
        ctx = SessionCtx(d=d, session="上午", rooms=["101", "102", "103"],
                         pgy=[], clerk=[], biopsy_open=False, capacity=2,
                         fc=FairCounters(), room_slots={})
        order = _room_order(ctx)
        assert order == _room_order(ctx)                     # 決定性
        assert sorted(order) == ["101", "102", "103"]        # 排列不增減
        orders.add(tuple(order))
    assert len(orders) >= 2, "整月房序恆同 → 洗牌沒生效"


def test_room_order_puts_101_first_on_apply_pref_days():
    """Apply 本科生效日（週二/五且有勾選者）101 必在最前——偏好者的平手決勝
    不會先被洗到前面的別房消耗掉；非生效日/沒勾選者則不強制。"""
    def order(d, pref):
        ctx = SessionCtx(d=d, session="上午", rooms=["099", "101", "103"],
                         pgy=[], clerk=[], biopsy_open=False, capacity=2,
                         fc=FairCounters(), room_slots={},
                         apply_pref=frozenset(pref))
        return _room_order(ctx, pref_first=True)
    for day in (4, 7, 11, 14, 18, 21, 25, 28):               # 2026-08 的二/五
        assert order(date(2026, 8, day), {"B"})[0] == "101"
    got_non_first = any(order(date(2026, 8, day), {"B"})[0] != "101"
                        for day in (3, 5, 6, 10, 12, 13, 17, 19, 20))
    assert got_non_first, "非二/五也恆 101 開頭 → pref_first 條件失效?"


def test_replay_counters_feeds_room_diversity():
    """鎖定/RF-09 回放要連房計數一起餵（否則重排/跨月後房多樣性歸零重算）。"""
    fc = FairCounters()
    replay_counters(fc, date(2026, 8, 3), "上午", {"101": ["c1"], "102": ["A"]},
                    "bt", pgy_set={"A"}, clerk_set={"c1"})
    assert fc.seat_room[(("clerk", "bt", "c1"), "101")] == 1
    assert fc.last_seat_room[("clerk", "bt", "c1")] == "101"
    assert fc.seat_room[(("pgy", "A"), "102")] == 1


def test_rf09_cross_month_room_continuity():
    """跨月梯次：上月 c1 常跟 101、c2 常跟 102 → 本月首時段換房（整梯房多樣性,
    不因跨月歸零）。"""
    b = ClerkBatch("bt", date(2026, 8, 24), ["c1", "c2"])
    prior = {}
    for day in (24, 25, 26, 27):                             # 8/24~8/27 早診
        prior[f"2026-08-{day}"] = {"上午": {"101": ["c1"], "102": ["c2"]}}
    inp = DaySolveInput(ym="2026-09",
                        grid={date(2026, 9, 1): {"上午": ["101", "102"]}},
                        pgy_roster=["P1", "P2"],             # 照光+治療室吃光 PGY
                        clerk_batches=[b], prior_sessions=prior)
    day_slots, _log, _w = month_solve_day(inp)
    slots = day_slots["2026-09-01"]["上午"]
    assert slots["101"] == ["c2"] and slots["102"] == ["c1"], \
        f"上月房計數未延續: {slots}"
