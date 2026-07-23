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
    """Clerk 少於診間：剩餘 PGY 先與已坐 Clerk 的診間配對，而非先佔空房。"""
    fc = FairCounters()
    slots, _log = solve_session(
        date(2026, 8, 3), "上午", ["101", "102"],
        pgy_avail=["A", "B", "C"], clerk_avail=["1"], biopsy_open=False, fc=fc)
    assert slots[PHOTO] == ["A"] and slots[TREATMENT] == ["B"]
    assert slots["101"] == ["1", "C"]                        # 配成 1C+1P
    assert "102" not in slots                                # 沒人 → 不輸出空房


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


def test_biopsy_open_but_no_clerk_warns():
    fc = FairCounters()
    slots, log = solve_session(
        date(2026, 8, 3), "上午", ["101"],
        pgy_avail=["A"], clerk_avail=[], biopsy_open=True, fc=fc)
    assert BIOPSY not in slots
    assert any("切片室開放但無 Clerk" in ln for ln in log)


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
    assert log and not warnings                              # 無 Clerk → 無切片警告


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
