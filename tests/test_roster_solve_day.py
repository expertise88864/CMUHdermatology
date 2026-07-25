# -*- coding: utf-8 -*-
"""PGY/Clerk 開診格網 + 五步驟填充器（純函式，無 ortools）。"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster.clinic_grid import is_session_open, month_grid  # noqa: E402
from cmuh_common.roster.model import ClerkBatch  # noqa: E402
from cmuh_common.roster.solve_day import (  # noqa: E402
    BIOPSY, PHOTO, REST, TREATMENT, DaySolveInput, FairCounters,
    month_solve_day, replay_counters, solve_session,
)

# 2026-08：週一 3/10/17/24/31；週三 5/12/19/26
_TEMPLATE = {
    "0": {"上午": [{"room": "101"}, {"room": "103"}],
          "下午": [{"room": "101"}]},
    "2": {"上午": [{"room": "102"}],
          "下午": [{"room": "102"}]},              # 週三下午應被強制關閉
}


# ─── clinic_grid ────────────────────────────────────────────────────────────
def test_month_grid_template_expansion():
    g = month_grid("2026-08", _TEMPLATE, holidays=set())
    assert g[date(2026, 8, 3)]["上午"] == ["101", "103"]     # 週一
    assert g[date(2026, 8, 3)]["下午"] == ["101"]
    assert is_session_open(g, date(2026, 8, 3), "上午")


def test_month_grid_wed_pm_closed_and_holiday_excluded():
    g = month_grid("2026-08", _TEMPLATE, holidays={date(2026, 8, 3)})
    assert date(2026, 8, 3) not in g                         # 假日休診
    assert g[date(2026, 8, 5)]["下午"] == []                 # 週三下午關閉
    assert g[date(2026, 8, 5)]["上午"] == ["102"]
    # 週末不在格網
    assert date(2026, 8, 1) not in g


def test_month_grid_self_paid_excluded_and_overrides():
    tmpl = {"0": {"上午": [{"room": "101"}, {"room": "105", "is_self_paid": True}]}}
    ov = {"2026-08-10": {"上午": {"closed_rooms": ["101"], "added_rooms": ["108"]}}}
    g = month_grid("2026-08", tmpl, set(), overrides=ov)
    assert g[date(2026, 8, 3)]["上午"] == ["101"]            # 自費 105 排除
    assert g[date(2026, 8, 10)]["上午"] == ["108"]           # 101 關、108 加


# ─── solve_session 五步驟 ───────────────────────────────────────────────────
def test_no_clerk_month_columns_fill():
    """無 Clerk：照光 1 PGY、治療室 1 PGY，其餘 PGY 逐欄填診。
    [2026-07-23] 平手決勝改決定性抖動（打散固定早/午配對）→ 不釘死誰在哪格，
    只驗語意：四格各 1 人、四人不重複、無切片/放假。"""
    fc = FairCounters()
    slots, _log = solve_session(
        date(2026, 8, 3), "上午", ["101", "102"],
        pgy_avail=["A", "B", "C", "D"], clerk_avail=[],
        biopsy_open=False, fc=fc)
    assert len(slots[PHOTO]) == 1 and len(slots[TREATMENT]) == 1
    assert len(slots["101"]) == 1 and len(slots["102"]) == 1
    assigned = [*slots[PHOTO], *slots[TREATMENT], *slots["101"], *slots["102"]]
    assert sorted(assigned) == ["A", "B", "C", "D"]          # 全上、不重複
    assert BIOPSY not in slots and REST not in slots


def test_mixed_one_clerk_one_pgy():
    """照光+治療室各吃 1 PGY 後，剩 1 PGY 與 Clerk 配成 1C+1P 混搭。"""
    fc = FairCounters()
    slots, _log = solve_session(
        date(2026, 8, 3), "上午", ["101"],
        pgy_avail=["A", "B", "C"], clerk_avail=["1"], biopsy_open=False, fc=fc)
    assert slots[PHOTO] == ["A"] and slots[TREATMENT] == ["B"]
    assert slots["101"] == ["1", "C"]                        # Clerk 先、PGY 後


def test_fewer_clerks_than_rooms_pairs_first():
    """Clerk 少於診間：剩餘 PGY 先與已坐 Clerk 的診間配對，而非先佔空房。
    [2026-07-24] 診間順序改決定性洗牌（房多樣性）→ 不釘死配對發生在 101，
    只驗語意：恰有一房＝Clerk+PGY 配對、另一房沒人不輸出。"""
    fc = FairCounters()
    slots, _log = solve_session(
        date(2026, 8, 3), "上午", ["101", "102"],
        pgy_avail=["A", "B", "C"], clerk_avail=["1"], biopsy_open=False, fc=fc)
    assert slots[PHOTO] == ["A"] and slots[TREATMENT] == ["B"]
    paired = [r for r in ("101", "102") if r in slots]
    assert len(paired) == 1, f"應恰有一房成對: {slots}"       # 沒人的房不輸出
    assert slots[paired[0]] == ["1", "C"]                    # 配成 1C+1P(Clerk先)


def test_biopsy_assign_and_prefer_undone():
    fc = FairCounters()
    fc.biopsy_done[("", "2")] = 1                            # "2" 本梯已輪過
    slots, _log = solve_session(
        date(2026, 8, 3), "上午", ["101"],
        pgy_avail=["A"], clerk_avail=["1", "2"],
        biopsy_open=True, fc=fc)
    assert slots[PHOTO] == ["A"]                             # 照光先吃掉唯一 PGY
    assert slots[BIOPSY] == ["1"]                            # 未輪過者優先（不受抖動影響）
    assert slots["101"] == ["2"]


def test_biopsy_fresh_pair_one_in_biopsy_one_in_room():
    """兩位皆未輪過：切片取其一、另一位進診間（平手由決定性抖動決定，不釘死誰）。"""
    fc = FairCounters()
    slots, _log = solve_session(
        date(2026, 8, 3), "上午", ["101"],
        pgy_avail=["A"], clerk_avail=["1", "2"],
        biopsy_open=True, fc=fc)
    assert len(slots[BIOPSY]) == 1 and len(slots["101"]) == 1
    assert sorted([*slots[BIOPSY], *slots["101"]]) == ["1", "2"]


def test_biopsy_open_but_no_clerk_stays_silent():
    """[2026-07-24 使用者] 切片室空下來沒關係 → 開放但無 Clerk 不再逐時段警告
    （噪音）；真正整梯輪不到者由月底「切片室輪不到」警告點名。"""
    fc = FairCounters()
    slots, log = solve_session(
        date(2026, 8, 3), "上午", ["101"],
        pgy_avail=["A"], clerk_avail=[], biopsy_open=True, fc=fc)
    assert BIOPSY not in slots
    assert not any("切片室" in ln for ln in log)


def test_biopsy_left_empty_when_all_done():
    """[2026-07-24 使用者] 每人整梯一次就好：全員都輪過 → 切片室空下來，
    Clerk 改進診間，不硬塞、不警告。"""
    fc = FairCounters()
    fc.biopsy_done[("bx", "1")] = 1
    fc.biopsy_done[("bx", "2")] = 1
    slots, log = solve_session(
        date(2026, 8, 3), "上午", ["101"],
        pgy_avail=["A", "B"], clerk_avail=["1", "2"],
        biopsy_open=True, fc=fc, batch_key="bx")
    assert BIOPSY not in slots, f"全員輪過仍排切片: {slots}"
    assert sorted(slots["101"]) == ["1", "2"]                # 改進診間
    assert not any("⚠" in ln for ln in log)


def test_biopsy_not_morning_and_afternoon_same_person_same_day():
    """[2026-07-24 使用者] 同一人不得同日早+午都切片：早上切過→次數=1，
    下午不再是候選（唯一 Clerk 時下午切片室留空、人進診間）。"""
    fc = FairCounters()
    d = date(2026, 8, 3)
    am, _ = solve_session(d, "上午", ["101"], pgy_avail=["A", "B", "C"],
                          clerk_avail=["1"], biopsy_open=True, fc=fc,
                          batch_key="bx")
    assert am[BIOPSY] == ["1"]
    pm, _ = solve_session(d, "下午", ["101"], pgy_avail=["A", "B", "C"],
                          clerk_avail=["1"], biopsy_open=True, fc=fc,
                          batch_key="bx")
    assert BIOPSY not in pm, f"同日下午又切片: {pm}"
    assert "1" in pm.get("101", []), "下午應改進診間跟診"


def test_biopsy_exactly_once_per_clerk_over_month():
    """[2026-07-24 使用者] 整月切片開好開滿 → 每位 Clerk 恰好一次（不是至少
    一次），之後所有時段切片室留空。"""
    fc = FairCounters()
    clerks = ["K1", "K2", "K3"]
    d = date(2026, 8, 3)
    filled = 0
    for _ in range(10):                       # 10 個工作日早診、全開切片
        if d.weekday() < 5 and d.weekday() != 2:
            slots, _ = solve_session(d, "上午", ["101"], [], list(clerks),
                                     True, fc, batch_key="bx")
            filled += 1 if BIOPSY in slots else 0
        d += timedelta(days=1)
    assert filled == 3, f"應恰排 3 次(每人一次)後留空: {filled}"
    assert all(fc.biopsy_done.get(("bx", c), 0) == 1 for c in clerks), \
        f"每人恰一次: {fc.biopsy_done}"


def test_treatment_no_pgy_warns_not_forced():
    fc = FairCounters()
    slots, log = solve_session(
        date(2026, 8, 3), "上午", ["101"],
        pgy_avail=[], clerk_avail=["1"], biopsy_open=False, fc=fc)
    assert TREATMENT not in slots
    assert any("治療室無 PGY" in ln for ln in log)
    assert slots["101"] == ["1"]                             # Clerk 仍照排


def test_wed_pm_photo_only():
    """週三下午：只排照光（治療室休診），沒位子者放假。"""
    fc = FairCounters()
    slots, _log = solve_session(
        date(2026, 8, 5), "下午", [],                        # 週三下午跟診關閉
        pgy_avail=["A", "B"], clerk_avail=[], biopsy_open=False, fc=fc)
    picked = slots[PHOTO][0]
    other = "B" if picked == "A" else "A"
    assert picked in ("A", "B") and len(slots[PHOTO]) == 1
    assert TREATMENT not in slots                            # 週三下午治療室不排
    assert fc.photo_wed_pm.get(picked) == 1                  # 週三下午照光計數
    assert slots[REST] == [other]                            # 沒位子 → 放假


def test_wed_pm_biopsy_forced_closed():
    """週三下午即使 biopsy_open=True，切片室仍硬性關閉（C3）。"""
    fc = FairCounters()
    slots, _log = solve_session(
        date(2026, 8, 5), "下午", [],
        pgy_avail=["A"], clerk_avail=["1"], biopsy_open=True, fc=fc)
    assert BIOPSY not in slots
    assert slots[REST] == ["1"]                              # Clerk 沒位子→放假


def test_capacity3_clerk_overflow_before_third_pgy():
    """容量 3：照光+治療室各 1 PGY 後，診間第 3 位留給 Clerk overflow（非第 2 個 PGY）。
    [2026-07-23] 不釘死是哪位 PGY，驗結構：房內 = Clerk、PGY、Clerk（C-P-C）。"""
    fc = FairCounters()
    slots, _log = solve_session(
        date(2026, 8, 3), "上午", ["101"],
        pgy_avail=["A", "P1", "P2"], clerk_avail=["1", "2"],
        biopsy_open=False, fc=fc, capacity=3)
    pgys = {"A", "P1", "P2"}
    assert len(slots[PHOTO]) == 1 and slots[PHOTO][0] in pgys
    assert len(slots[TREATMENT]) == 1 and slots[TREATMENT][0] in pgys
    room = slots["101"]
    assert len(room) == 3
    assert room[0] in ("1", "2") and room[2] in ("1", "2")   # 1、3 位是 Clerk
    assert room[1] in pgys                                    # 第 2 位是剩下的 PGY
    assert sorted([slots[PHOTO][0], slots[TREATMENT][0], room[1]]) \
        == sorted(pgys)                                       # 三位 PGY 全上、不重複
    assert REST not in slots


def test_photo_priority_over_treatment_when_scarce():
    """只有 1 PGY：照光最優先拿到人，治療室湊不到人 → 警告不硬塞。"""
    fc = FairCounters()
    slots, log = solve_session(
        date(2026, 8, 3), "上午", ["101"],
        pgy_avail=["A"], clerk_avail=[], biopsy_open=False, fc=fc)
    assert slots[PHOTO] == ["A"]                             # 照光一定要，先拿
    assert TREATMENT not in slots                            # 治療室沒人
    assert any("治療室無 PGY" in ln for ln in log)


def test_photo_fairness_rotates():
    """照光每時段必排且輪平均：連續 3 時段三人各輪 1 次（順序由抖動決定，不釘死）。"""
    fc = FairCounters()
    picks = []
    for _ in range(3):                                       # 連續 3 個時段
        slots, _l = solve_session(date(2026, 8, 3), "上午", [],
                                  ["A", "B", "C"], [], False, fc)
        picks.append(slots[PHOTO][0])
    assert sorted(picks) == ["A", "B", "C"]                  # 各 1 次 = 輪平均


def test_determinism_same_input():
    def run():
        fc = FairCounters()
        return solve_session(date(2026, 8, 3), "上午", ["101", "102"],
                             ["A", "B", "C"], ["1", "2"], True, fc)[0]
    assert run() == run()


def test_photo_not_fixed_to_same_session_over_month():
    """[2026-07-23 使用者] 反固定配對：2 位 PGY 整月排班，早上照光不得永遠同一人
    （舊 LRU 平手決勝在早/午雙時段節拍下會鎖死 A 恆早、B 恆午）；抖動打散後，
    整月照光/治療室總次數仍平均（spread ≤1）。"""
    pgy = ["A", "B"]
    grid = {}
    d = date(2026, 8, 3)
    while d <= date(2026, 8, 28):
        if d.weekday() < 5:
            grid[d] = {"上午": ["101"],
                       "下午": [] if d.weekday() == 2 else ["101"]}
        d += timedelta(days=1)
    day_slots, _log, _w = month_solve_day(DaySolveInput(
        ym="2026-08", grid=grid, pgy_roster=pgy))
    am = [s["上午"][PHOTO][0] for s in day_slots.values()
          if PHOTO in (s.get("上午") or {})]
    pm = [s["下午"][PHOTO][0] for s in day_slots.values()
          if PHOTO in (s.get("下午") or {})]
    assert len(set(am)) > 1, f"早上照光不得固定同一人: {am}"
    assert len(set(pm)) > 1, f"下午照光不得固定同一人: {pm}"
    totals = {p: (am + pm).count(p) for p in pgy}
    assert abs(totals["A"] - totals["B"]) <= 1, f"整月照光仍需平均: {totals}"


# ─── month_solve_day ────────────────────────────────────────────────────────
def test_month_solve_day_no_clerk():
    grid = month_grid("2026-08", _TEMPLATE, set())
    inp = DaySolveInput(ym="2026-08", grid=grid,
                        pgy_roster=["A", "B", "C"], clerk_batches=[])
    day_slots, log, warnings = month_solve_day(inp)
    mon = day_slots["2026-08-03"]["上午"]
    assert mon[TREATMENT]                                    # 週一早有治療室
    assert log
    assert not any("切片" in w for w in warnings)            # 無 Clerk → 無切片警告
    # 註：3 位 PGY／4 個週三下午無法整除 → 必有人多值一次；本情境每時段都滿編、
    # 全月無空檔可補假 → 會有「補假未排到」警告，屬預期（提醒手動安排半天假）。


def test_locked_session_preserved_and_counted():
    """鎖定時段原樣保留，且餵進公平計數 → 未鎖時段對齊（治療室不重複選同人）。"""
    grid = month_grid("2026-08", _TEMPLATE, set())
    locked = {"2026-08-03": {"上午": {TREATMENT: ["C"], "101": ["A"], "103": ["B"]}}}
    inp = DaySolveInput(ym="2026-08", grid=grid, pgy_roster=["A", "B", "C"],
                        clerk_batches=[], locked=locked)
    day_slots, _log, _w = month_solve_day(inp)
    assert day_slots["2026-08-03"]["上午"] == locked["2026-08-03"]["上午"]  # 原樣
    # C 已在鎖定時段值治療室(tx=1) → 同日下午治療室改選 A/B（tx 公平）
    assert day_slots["2026-08-03"]["下午"][TREATMENT][0] in ("A", "B")


def test_month_solve_day_biopsy_missed_warning():
    grid = month_grid("2026-08", _TEMPLATE, set())
    batch = ClerkBatch("b1", date(2026, 8, 3), ["1", "2", "3"])
    inp = DaySolveInput(
        ym="2026-08", grid=grid, pgy_roster=["A"],
        clerk_batches=[batch], biopsy_open={})               # 切片室全程不開
    _ds, _log, warnings = month_solve_day(inp)
    assert any("切片室輪不到" in w for w in warnings)         # 3 人都沒輪到


def test_month_solve_day_clerk_only_within_batch():
    """跨梯次：batch1 成員不得排進 batch2 涵蓋的日期（P1 修正）。"""
    grid = month_grid("2026-08", _TEMPLATE, set())
    b1 = ClerkBatch("b1", date(2026, 8, 3), ["1"])       # 8/3–8/16
    b2 = ClerkBatch("b2", date(2026, 8, 17), ["9"])      # 8/17–8/30
    inp = DaySolveInput(ym="2026-08", grid=grid, pgy_roster=["A", "B"],
                        clerk_batches=[b1, b2])
    day_slots, _log, _w = month_solve_day(inp)

    def _people(iso):
        out = set()
        for sess in day_slots.get(iso, {}).values():
            for who in sess.values():
                out.update(who)
        return out
    assert "1" in _people("2026-08-03") and "9" not in _people("2026-08-03")
    assert "9" in _people("2026-08-17") and "1" not in _people("2026-08-17")


def test_clerk_fairness_resets_per_batch():
    """代號跨梯重用：新梯的 '1' 不應繼承舊梯 '1' 的座位數而被冷落。"""
    one_room = {str(wd): {"上午": [{"room": "101"}]} for wd in range(5)}
    grid = month_grid("2026-08", one_room, set())
    b1 = ClerkBatch("b1", date(2026, 8, 3), ["1"])       # 前兩週只有 "1"，天天就座
    b2 = ClerkBatch("b2", date(2026, 8, 17), ["1", "2"])  # 後兩週 "1","2"（"1" 是新人）
    inp = DaySolveInput(ym="2026-08", grid=grid, pgy_roster=["A"],
                        clerk_batches=[b1, b2])
    day_slots, _log, _w = month_solve_day(inp)
    # 收集 b2 期間 101 診間坐過的人
    seated_b2 = set()
    for iso, sess in day_slots.items():
        if iso >= "2026-08-17":
            seated_b2.update((sess.get("上午") or {}).get("101", []))
    assert "1" in seated_b2 and "2" in seated_b2          # 新梯 "1" 有被公平排到


# ─── RF-02：鎖定日掉出開診格網（假日）→ 原樣保留＋警告 ───────────────────────
def test_rf02_locked_out_of_grid_preserved_and_warned():
    grid = month_grid("2026-08", _TEMPLATE, holidays={date(2026, 8, 3)})  # 8/3 變假日
    assert date(2026, 8, 3) not in grid
    locked = {"2026-08-03": {"上午": {TREATMENT: ["A"], "101": ["B"]}}}
    inp = DaySolveInput(ym="2026-08", grid=grid, pgy_roster=["A", "B", "C"],
                        clerk_batches=[], locked=locked)
    day_slots, _log, warnings = month_solve_day(inp)
    assert day_slots["2026-08-03"]["上午"] == locked["2026-08-03"]["上午"]  # 原樣
    assert any("鎖定" in w and "格網" in w for w in warnings)


# ─── RF-08：梯次重疊 → 警告點名被忽略的梯次，決定性勝者不變 ──────────────────
def test_rf08_batch_overlap_warns_and_deterministic():
    grid = month_grid("2026-08", _TEMPLATE, set())
    b1 = ClerkBatch("b1", date(2026, 8, 3), ["1"])
    b2 = ClerkBatch("b2", date(2026, 8, 3), ["5"])       # 同起始日 → 重疊
    inp = DaySolveInput(ym="2026-08", grid=grid, pgy_roster=["A"],
                        clerk_batches=[b1, b2])
    day_slots, _log, warnings = month_solve_day(inp)
    assert any("梯次重疊" in w and "b2" in w for w in warnings)
    people = set()
    for sess in day_slots.values():
        for slots in sess.values():
            for who in slots.values():
                people.update(who)
    assert "1" in people and "5" not in people            # 勝者 b1，b2 的 "5" 不出現


# ─── RF-10：鎖定內含請假者 / 非名單代號 → 警告；未知代號不污染計數 ──────────
def test_rf10_locked_on_leave_warns():
    grid = month_grid("2026-08", _TEMPLATE, set())
    locked = {"2026-08-03": {"上午": {TREATMENT: ["C"]}}}
    inp = DaySolveInput(ym="2026-08", grid=grid, pgy_roster=["A", "B", "C"],
                        clerk_batches=[], locked=locked,
                        leaves={"pgy": {"C": {date(2026, 8, 3)}}})
    day_slots, _log, warnings = month_solve_day(inp)
    assert day_slots["2026-08-03"]["上午"][TREATMENT] == ["C"]   # 原樣保留
    assert any("已請假" in w for w in warnings)


def test_rf10_locked_off_roster_warns():
    grid = month_grid("2026-08", _TEMPLATE, set())
    locked = {"2026-08-03": {"上午": {"101": ["9"]}}}          # 9 不在任何名單
    inp = DaySolveInput(ym="2026-08", grid=grid, pgy_roster=["A", "B"],
                        clerk_batches=[], locked=locked)
    day_slots, _log, warnings = month_solve_day(inp)
    assert day_slots["2026-08-03"]["上午"]["101"] == ["9"]      # 原樣保留
    assert any("不在本月" in w and "名單" in w for w in warnings)


def test_rf09_cross_month_biopsy_continuity():
    """RF-09：上月已輪切片者，本月不再被當「本梯未輪過」重複優先。"""
    grid = month_grid("2026-08", _TEMPLATE, set())
    b = ClerkBatch("b", date(2026, 7, 27), ["1", "2"])       # 7/27 起跨進 8 月
    inp = DaySolveInput(
        ym="2026-08", grid=grid, pgy_roster=["A"], clerk_batches=[b],
        biopsy_open={"2026-08-03": {"上午": True}},
        prior_sessions={"2026-07-30": {"上午": {BIOPSY: ["1"]}}})
    day_slots, _log, _w = month_solve_day(inp)
    assert day_slots["2026-08-03"]["上午"][BIOPSY] == ["2"]   # "1" 上月已輪 → 選 "2"


def test_rf09_cross_month_missed_warning_excludes_prior():
    """RF-09：月底 missed 警告以整梯計，不誤報上月已輪過的人。"""
    grid = month_grid("2026-08", _TEMPLATE, set())
    b = ClerkBatch("b", date(2026, 7, 27), ["1", "2"])
    inp = DaySolveInput(                                      # 8 月切片全程不開
        ym="2026-08", grid=grid, pgy_roster=["A"], clerk_batches=[b],
        prior_sessions={"2026-07-30": {"上午": {BIOPSY: ["1"]}}})
    _ds, _log, warnings = month_solve_day(inp)
    missed = [w for w in warnings if "切片室輪不到" in w]
    assert missed and "2" in missed[0] and "1" not in missed[0]


def test_rf10_replay_counters_skips_unknown_codes():
    """未知代號不進座位/切片命名空間；治療室裸代號照計。"""
    fc = FairCounters()
    slots = {TREATMENT: ["A"], BIOPSY: ["Zstale"], "101": ["9"]}
    replay_counters(fc, date(2026, 8, 3), "上午", slots, "b2",
                    pgy_set={"A"}, clerk_set={"5"})
    assert fc.tx_total.get("A") == 1                          # 治療室仍計數
    assert ("b2", "Zstale") not in fc.biopsy_done             # 換梯代號不污染切片
    assert ("clerk", "b2", "9") not in fc.seat               # 未知代號不佔座位計數


# ─── Apply 本科優先（2026-07-23 使用者）────────────────────────────────────────
def test_apply_pref_wins_ties_on_tue_fri_101():
    """勾選者在週二/週五的 101 診【次數平手時】恆優先（壓過抖動，跨多個日期驗證）。"""
    from cmuh_common.roster.solve_day import PgyMixStep, SessionCtx
    for iso in ("2026-08-04", "2026-08-07", "2026-08-11", "2026-08-14",
                "2026-08-18", "2026-08-21"):                 # 二/五 交錯
        for session in ("上午", "下午"):
            fc = FairCounters()
            ctx = SessionCtx(
                d=date.fromisoformat(iso), session=session, rooms=["101"],
                pgy=["A", "B"], clerk=[], biopsy_open=False, capacity=2,
                fc=fc, room_slots={"101": []}, apply_pref=frozenset({"B"}))
            PgyMixStep().run(ctx, {}, [])
            assert ctx.room_slots["101"][0] == "B", \
                f"{iso} {session}: 平手時 Apply 者應先進 101"


def test_apply_pref_never_beats_fairness():
    """公平第一：Apply 者座位次數落後才輪得到別人？——反向：Apply 者次數【較多】時,
    次數較少者恆先上（偏好不得壓過次數）。"""
    from cmuh_common.roster.solve_day import PgyMixStep, SessionCtx
    fc = FairCounters()
    fc.seat[("pgy", "B")] = 1                               # Apply 者已多坐過一次
    ctx = SessionCtx(
        d=date(2026, 8, 4), session="上午", rooms=["101"],
        pgy=["A", "B"], clerk=[], biopsy_open=False, capacity=2,
        fc=fc, room_slots={"101": []}, apply_pref=frozenset({"B"}))
    PgyMixStep().run(ctx, {}, [])
    assert ctx.room_slots["101"][0] == "A", "次數較少者恆優先（公平>偏好）"


def test_apply_pref_only_tue_fri_101():
    """偏好只作用在 週二/週五 × 101：其他日/其他房 room_pref 為空。"""
    from cmuh_common.roster.solve_day import SessionCtx
    fc = FairCounters()

    def ctx_for(iso, room):
        return SessionCtx(d=date.fromisoformat(iso), session="上午",
                          rooms=[room], pgy=[], clerk=[], biopsy_open=False,
                          capacity=2, fc=fc, room_slots={room: []},
                          apply_pref=frozenset({"B"}))
    assert ctx_for("2026-08-04", "101").room_pref("101") == {"B"}   # 週二 101
    assert ctx_for("2026-08-07", "101").room_pref("101") == {"B"}   # 週五 101
    assert ctx_for("2026-08-05", "101").room_pref("101") == frozenset()  # 週三
    assert ctx_for("2026-08-04", "102").room_pref("102") == frozenset()  # 別房


# ─── 2026-07-25 使用者規則：週三下午配額補假 / 同伴多樣性 ────────────────────
def _weekday_grid(start, end, rooms_am, rooms_pm=None):
    """建平日格網（週三下午跟診關閉＝[]，但該時段仍需排照光）。"""
    g, d = {}, start
    while d <= end:
        if d.weekday() < 5:
            g[d] = {"上午": list(rooms_am),
                    "下午": [] if d.weekday() == 2 else list(
                        rooms_am if rooms_pm is None else rooms_pm)}
        d += timedelta(days=1)
    return g


def test_wed_pm_photo_split_evenly_two_people_four_wednesdays():
    """[使用者例] 兩人、四個週三下午 → 一定是一人 2 次（獨立於總照光次數平均）。"""
    grid = _weekday_grid(date(2026, 8, 3), date(2026, 8, 28), ["101"])
    wed = [d for d in grid if d.weekday() == 2]
    assert len(wed) == 4, "2026/8 應有 4 個週三（8/5,12,19,26）"
    day_slots, _log, _w = month_solve_day(DaySolveInput(
        ym="2026-08", grid=grid, pgy_roster=["A", "B"]))
    picks = [day_slots[d.isoformat()]["下午"][PHOTO][0] for d in sorted(wed)]
    assert sorted(picks.count(p) for p in ("A", "B")) == [2, 2], \
        f"四個週三下午應 2/2 平分：{picks}"


def test_wed_pm_over_quota_records_rest_owed_then_paid_when_slack():
    """[使用者] 週三下午無法整除 → 多值的人記「補半天假」;債務留到之後
    「排班允許（人多於位子）」的時段才兌現（當下那個時段他正在照光,不可能同時放假）。"""
    fc = FairCounters()
    # 3 個週三下午、2 位 PGY → 配額 1，第 2 次即超額
    for d in (date(2026, 8, 5), date(2026, 8, 12), date(2026, 8, 19)):
        solve_session(d, "下午", [], pgy_avail=["A", "B"], clerk_avail=[],
                      biopsy_open=False, fc=fc, wed_pm_quota=1)
    over = [p for p in ("A", "B") if fc.photo_wed_pm.get(p, 0) > 1]
    assert len(over) == 1, f"3 場 2 人 → 恰一人超額：{fc.photo_wed_pm}"
    assert fc.rest_owed.get(over[0], 0) == 1, "超額當下應記下一次補假債務"

    # 之後遇到人多於位子的時段 → 該人最後入座 → 放假抵銷。
    # （把他的照光/治療室次數墊高,確保這次不會又被那兩步徵召走。）
    owed_p = over[0]
    fc.photo_total[owed_p] = 99
    fc.tx_total[owed_p] = 99
    slots, _log = solve_session(
        date(2026, 8, 20), "上午", ["101"],       # 照光1+治療1+診2 = 4 位
        pgy_avail=[owed_p, "C", "D", "E", "F"], clerk_avail=[],
        biopsy_open=False, fc=fc)
    assert owed_p in slots[REST], f"欠補假者應被留到放假：{slots}"
    assert fc.rest_owed.get(owed_p, 0) == 0, "放到假 → 債務還清"


def test_rest_owed_only_costs_clinic_not_photo_or_treatment():
    """補假只從【跟診】扣（使用者:PGY 不一定要跟診）,不得動照光/治療室的次數平均——
    若欠假者當下被照光/治療室徵召,他就照常上班,債務順延到下一個有空檔的時段。"""
    fc = FairCounters()
    fc.rest_owed["B"] = 1
    # 全員照光/治療室次數皆 0 → B 仍可能被這兩步選走(公平不受補假影響)
    slots, _log = solve_session(
        date(2026, 8, 3), "上午", ["101"],
        pgy_avail=["A", "B", "C", "D", "E"], clerk_avail=[],
        biopsy_open=False, fc=fc)
    if slots.get(PHOTO) == ["B"] or slots.get(TREATMENT) == ["B"]:
        assert fc.rest_owed["B"] == 1, "被徵召上班 → 債務順延,不得憑空消失"
    else:
        assert "B" in slots[REST] and fc.rest_owed["B"] == 0


def test_rest_owed_cleared_then_normal_fairness():
    """欠額還清後立刻回到一般座位公平（不會持續少班）。"""
    fc = FairCounters()
    fc.rest_owed["B"] = 1
    fc.photo_total["B"] = fc.tx_total["B"] = 99   # 確保 B 不被照光/治療室徵召
    pool = ["B", "C", "D", "E", "F"]
    slots1, _l1 = solve_session(
        date(2026, 8, 3), "上午", ["101"], pgy_avail=list(pool),
        clerk_avail=[], biopsy_open=False, fc=fc)
    assert "B" in slots1[REST] and fc.rest_owed["B"] == 0, "先還清這次補假"

    slots2, _l2 = solve_session(                  # 同樣配置再跑一次
        date(2026, 8, 4), "上午", ["101"], pgy_avail=list(pool),
        clerk_avail=[], biopsy_open=False, fc=fc)
    assert "B" not in slots2.get(REST, []), \
        "債務已清 → 座位次數最少的 B 應回到優先入座,不再被特別留去放假"


def test_pair_diversity_avoids_fixed_partners():
    """[使用者] 一起跟診的人不要固定同一組：整月下來,同一組合不應壟斷。
    2 Clerk + 4 PGY、2 房 → 照光/治療室各吃掉 1 PGY,剩 2 PGY 與 2 Clerk 配成
    兩組 1C+1P；若無同伴多樣性會固定同一種配對。"""
    grid = _weekday_grid(date(2026, 8, 3), date(2026, 8, 28), ["101", "102"])
    batch = ClerkBatch(id="B1", start_monday=date(2026, 8, 3),
                       members=["1", "2"])
    day_slots, _log, _w = month_solve_day(DaySolveInput(
        ym="2026-08", grid=grid, pgy_roster=["A", "B", "C", "D"],
        clerk_batches=[batch]))
    pairs = []
    for sessions in day_slots.values():
        for slots in sessions.values():
            for slot, people in slots.items():
                if slot in (PHOTO, TREATMENT, BIOPSY, REST):
                    continue
                if len(people) == 2:
                    pairs.append(tuple(sorted(people)))
    assert pairs, "應有成對跟診的房"
    assert len(set(pairs)) > 1, f"配對組合不得整月固定同一種：{set(pairs)}"


def test_pair_counts_replayed_from_locked_sessions():
    """鎖定/既存格的共事次數要回放,否則後續時段會以為這兩人沒配過而一直湊在一起。"""
    fc = FairCounters()
    replay_counters(fc, date(2026, 8, 3), "上午",
                    {"101": ["A", "1"], "102": ["B"]}, "B1",
                    pgy_set={"A", "B"}, clerk_set={"1"})
    assert fc.pair.get(_pair_of(("pgy", "A"), ("clerk", "B1", "1"))) == 1
    assert len(fc.pair) == 1, f"單人房(102 只有 B)不得產生配對：{fc.pair}"


def _pair_of(a, b):
    from cmuh_common.roster.solve_day import _pair_key
    return _pair_key(a, b)


# ─── codex deep 第一輪 findings 的回歸測試 ──────────────────────────────────
def test_pair_key_uses_real_role_of_seated_people():
    """[codex R1] 同伴鍵必須用房內既有者的【真實角色 ck】,不可用本次候選池的 resolver
    推斷：PgyMixStep 進場時房裡坐的是 Clerk,若用 _pgy_ck 推會標成 ("pgy",代號),
    與 replay_counters 寫的 ("clerk",梯次,代號) 永遠對不上（代號跨梯重用還會誤繼承）。"""
    fc = FairCounters()
    solve_session(date(2026, 8, 3), "上午", ["101"],
                  pgy_avail=["A", "B", "C"], clerk_avail=["1"],
                  biopsy_open=False, fc=fc, batch_key="B1")
    keys = list(fc.pair)
    assert len(keys) == 1, f"1C+1P 應恰一組配對：{fc.pair}"
    k = keys[0]
    assert any("clerk" in s and "B1" in s for s in k), \
        f"Clerk 一方必須是梯次命名空間的鍵,不得被標成 pgy：{k}"
    # 與 replay 寫出的鍵一致 → 鎖定/跨月的配對史才查得到
    fc2 = FairCounters()
    replay_counters(fc2, date(2026, 8, 3), "上午", {"101": ["A", "1"]}, "B1",
                    pgy_set={"A", "B", "C"}, clerk_set={"1"})
    assert set(fc2.pair) == {_pair_of(("pgy", "A"), ("clerk", "B1", "1"))}
    solved_pair_shape = {tuple(sorted(k)) for k in fc.pair}
    replay_pair_shape = {tuple(sorted(k)) for k in fc2.pair}
    assert solved_pair_shape and replay_pair_shape
    assert next(iter(solved_pair_shape))[0].startswith("('clerk'"), \
        "求解與回放的鍵格式須一致"


def test_locked_wed_pm_over_quota_records_debt():
    """[codex R1] 鎖定的週三下午照光若超過配額 → 也要記補假債（否則該補的假被漏掉）。"""
    fc = FairCounters()
    replay_counters(fc, date(2026, 8, 5), "下午", {PHOTO: ["A"]}, "",
                    pgy_set={"A"}, clerk_set=set(), wed_pm_quota=0)
    assert fc.rest_owed.get("A") == 1, "鎖定格的超額照光也要記債"


def test_locked_rest_clears_debt_no_double_compensation():
    """[codex R1] 欠債者在鎖定時段已放到假 → 債務扣除,不得日後再補一次（補兩次半天）。"""
    fc = FairCounters()
    fc.rest_owed["A"] = 1
    replay_counters(fc, date(2026, 8, 6), "上午", {REST: ["A"]}, "",
                    pgy_set={"A"}, clerk_set=set())
    assert fc.rest_owed["A"] == 0, "鎖定格已放假 → 債務還清"


def test_unpaid_rest_debt_is_warned_not_silently_dropped():
    """[codex R1] 超額出現在月底、之後沒有空檔可補 → 必須點名警告,不得靜默丟棄。"""
    # 只給一天(週三)：該日下午照光必超額(配額 0),且全月再無其他時段可補假
    grid = {date(2026, 8, 26): {"上午": ["101"], "下午": []}}
    day_slots, _log, warnings = month_solve_day(DaySolveInput(
        ym="2026-08", grid=grid, pgy_roster=["A", "B"]))
    msg = [w for w in warnings if "未能排到補假時段" in w]
    assert msg, f"未兌現的補假債務應點名警告：{warnings}"
    # [codex R3] 警告不得宣稱程式沒驗證過的事：本情境班表裡其實【有人放假】
    # （只是不是欠債者本人）→ 不可寫「整月各時段皆滿編/無可用空檔」。
    rested = any(REST in slots
                 for sessions in day_slots.values()
                 for slots in sessions.values())
    assert rested, "本情境應有人放假（另一位 PGY）"
    for bad in ("滿編", "無可用空檔"):
        assert bad not in msg[0], f"警告不得宣稱未驗證的原因：{msg[0]}"


def test_debt_from_last_wednesday_is_backfilled_into_earlier_slack():
    """[codex R2] 超額發生在【月底最後一個週三】,但月初有空檔 → 兩趟求解要真的把
    補假排進去（單趟貪婪只會事後警告,月初空檔白白浪費）。"""
    # 只排兩個工作日：週一(有空檔:5 人搶 4 位)、月底週三(下午照光造成超額)
    grid = {date(2026, 8, 3): {"上午": ["101"], "下午": []},     # 週一,有空檔
            date(2026, 8, 26): {"上午": [], "下午": []}}         # 月底週三
    pgy = ["A", "B", "C", "D", "E"]
    day_slots, log, warnings = month_solve_day(DaySolveInput(
        ym="2026-08", grid=grid, pgy_roster=pgy))
    over = [p for p in pgy
            if day_slots["2026-08-26"]["下午"].get(PHOTO) == [p]]
    assert len(over) == 1, "月底週三下午應有一位照光者"
    owed_p = over[0]
    # 該人應在【月初那個有空檔的時段】被補到半天假
    mon = day_slots["2026-08-03"]
    rested = set(mon["上午"].get(REST, [])) | set(mon["下午"].get(REST, []))
    assert owed_p in rested, (
        f"月底超額者 {owed_p} 應回填到月初空檔補假；"
        f"週一={mon}，警告={warnings}")
    assert not any("補假未排到" in w for w in warnings), \
        f"已補到假就不該再警告：{warnings}"
    assert any("補週三下午半天假" in ln for ln in log)


def test_two_pass_does_not_double_compensate():
    """第二趟預掛債務後不得重複累加（否則同一次超額補到兩次半天假）。"""
    grid = {}
    d = date(2026, 8, 3)
    while d <= date(2026, 8, 28):                # 整月每天都有大量空檔
        if d.weekday() < 5:
            grid[d] = {"上午": ["101"], "下午": [] if d.weekday() == 2
                       else ["101"]}
        d += timedelta(days=1)
    pgy = ["A", "B", "C", "D", "E", "F", "G"]
    day_slots, _log, warnings = month_solve_day(DaySolveInput(
        ym="2026-08", grid=grid, pgy_roster=pgy))
    wed = [d for d in grid if d.weekday() == 2]
    counts = {}
    for dd in wed:
        p = day_slots[dd.isoformat()]["下午"].get(PHOTO)
        if p:
            counts[p[0]] = counts.get(p[0], 0) + 1
    quota = len(wed) // len(pgy)                  # 4 // 7 = 0
    total_over = sum(max(0, n - quota) for n in counts.values())
    assert total_over > 0, "本情境應有超額（4 場 7 人,配額 0）"
    assert not any("補假未排到" in w for w in warnings), \
        f"空檔充足 → 應全部補到假：{warnings}"


def test_locked_photo_non_pgy_code_does_not_create_debt():
    """[codex R2] 鎖定格允許保留過期/非名單/誤植代號（只警告不改內容）→ 那些代號
    不得冒出「要補半天假」的假債（後續還會變成假警告）。"""
    fc = FairCounters()
    replay_counters(fc, date(2026, 8, 5), "下午", {PHOTO: ["幽靈代號"]}, "",
                    pgy_set={"A"}, clerk_set=set(), wed_pm_quota=0)
    assert fc.rest_owed == {}, f"非現役 PGY 不得產生補假債：{fc.rest_owed}"
    assert fc.photo_wed_pm.get("幽靈代號") == 1, "照光次數統計維持既有容錯"
