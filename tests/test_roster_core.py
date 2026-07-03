# -*- coding: utf-8 -*-
"""roster 核心：model(月曆/點數/值班區塊) + ledger + storage。純邏輯，無 ortools。"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster import ledger as lg  # noqa: E402
from cmuh_common.roster import solve_rvs as sr  # noqa: E402
from cmuh_common.roster.model import (  # noqa: E402
    Member, RosterParams, SolveContext, build_duty_blocks, day_point,
    month_dates, week_key,
)
from cmuh_common.roster.report import build_report  # noqa: E402
from cmuh_common.roster.rules import Precheck  # noqa: E402
from cmuh_common.roster.storage import (  # noqa: E402
    KEEP_SNAPSHOTS, FinalizedMonthError, NewerSchemaError, RosterStorage,
)

P = RosterParams()


# ─── model：月曆/點數 ─────────────────────────────────────────────────────
def test_month_dates_and_week_key():
    days = month_dates(2026, 8)
    assert days[0] == date(2026, 8, 1) and days[-1] == date(2026, 8, 31)
    # ISO 週：週六與翌日週日同週
    assert week_key(date(2026, 8, 1)) == week_key(date(2026, 8, 2))  # 六/日
    assert week_key(date(2026, 8, 2)) != week_key(date(2026, 8, 8))


def test_day_point_weekday_weekend_holiday():
    holidays = {date(2026, 9, 28)}  # 週一教師節(假設)
    assert day_point(date(2026, 9, 21), holidays, P) == 1   # 平日
    assert day_point(date(2026, 9, 26), holidays, P) == 2   # 週六
    assert day_point(date(2026, 9, 28), holidays, P) == 1   # 平日國定假日=1
    # 假日撞週末 → 以週末 2 點計（定案）
    hol_weekend = {date(2026, 4, 5)}   # 週日
    assert day_point(date(2026, 4, 5), hol_weekend, P) == 2


# ─── model：值班區塊 ──────────────────────────────────────────────────────
def test_blocks_normal_weekend():
    blocks = build_duty_blocks(2026, 8, set())   # 2026/8/1=週六
    weekend = [b for b in blocks if b.kind == "weekend"]
    assert len(weekend) == 5                     # 8/1 8/8 8/15 8/22 8/29
    assert weekend[0].days == [date(2026, 8, 1), date(2026, 8, 2)]


def test_blocks_monday_holiday_three_day():
    # 2026/9/28(一) 為假日 → 9/26(六)+9/27(日)+9/28(一) 三連休同塊
    blocks = build_duty_blocks(2026, 9, {date(2026, 9, 28)})
    blk = [b for b in blocks if date(2026, 9, 26) in b.days][0]
    assert blk.days == [date(2026, 9, 26), date(2026, 9, 27), date(2026, 9, 28)]
    assert blk.points({date(2026, 9, 28)}, P) == 2 + 2 + 1


def test_blocks_friday_holiday_chains_backward():
    # 2026/10/9(五) 假日 + 10/10(六)國慶(週末) → 五六日同塊
    hol = {date(2026, 10, 9), date(2026, 10, 10)}
    blocks = build_duty_blocks(2026, 10, hol)
    blk = [b for b in blocks if date(2026, 10, 10) in b.days][0]
    assert blk.days[0] == date(2026, 10, 9)
    assert date(2026, 10, 11) in blk.days


def test_blocks_standalone_weekday_holiday_not_in_block():
    # 週三孤立假日不成塊（由年度指定表管）
    blocks = build_duty_blocks(2026, 8, {date(2026, 8, 12)})  # 週三
    assert all(date(2026, 8, 12) not in b.days for b in blocks)


def test_blocks_month_start_sunday_orphan():
    # 2026/11/1 = 週日 → 孤兒塊
    blocks = build_duty_blocks(2026, 11, set())
    assert blocks[0].kind == "weekend_orphan"
    assert blocks[0].days == [date(2026, 11, 1)]
    assert blocks[0].saturday is None


def test_blocks_long_holiday_run_no_overlap():
    """[codex P2] 月初週日+連假一路到下週六(春節型) → 孤兒塊不得跨進下個週末,
    兩塊不得重疊(否則上月人選被錯誤綁進下一週末)。"""
    # 2026/11/1=週日;假日 11/2(一)~11/7(六) 連續
    hol = {date(2026, 11, d) for d in (2, 3, 4, 5, 6, 7)}
    blocks = build_duty_blocks(2026, 11, hol)
    orphan = blocks[0]
    assert orphan.kind == "weekend_orphan"
    assert orphan.days == [date(2026, 11, d) for d in (1, 2, 3, 4, 5, 6)]
    sat_block = [b for b in blocks if date(2026, 11, 7) in b.days][0]
    assert date(2026, 11, 6) not in sat_block.days     # 後向鏈不吃已佔用日
    seen = [d for b in blocks for d in b.days]
    assert len(seen) == len(set(seen))                  # 無任何重疊


def test_blocks_month_end_saturday():
    # 2026/10/31 = 週六（週日在 11 月）→ 塊內只有週六
    blocks = build_duty_blocks(2026, 10, set())
    last = blocks[-1]
    assert last.days == [date(2026, 10, 31)]


# ─── ledger ───────────────────────────────────────────────────────────────
def test_ledger_settle_and_resettle_idempotent():
    led = {"r": {}, "vs": {}, "history": []}
    pts = {"a": 16, "b": 14, "c": 12}           # 平均 14
    lg.settle_month(led, "r", "2026-08", pts)
    assert led["r"] == {"a": 2.0, "b": 0.0, "c": -2.0}
    # 同月重排（不同結果）→ 先回滾舊分錄,不會累加
    lg.settle_month(led, "r", "2026-08", {"a": 14, "b": 15, "c": 13})
    assert led["r"]["a"] == 0.0 and led["r"]["b"] == 1.0 and led["r"]["c"] == -1.0
    assert len([h for h in led["history"] if h["month"] == "2026-08"]) == 1


def test_ledger_reset_and_sync():
    led = {"r": {"a": 2.0, "b": -2.0}, "vs": {}, "history": []}
    lg.reset_member(led, "r", "a")
    assert led["r"]["a"] == 0.0
    lg.sync_members(led, "r", ["b", "new"])     # a 離開→作廢; new 進來→0
    assert "a" not in led["r"] and led["r"]["new"] == 0.0 and led["r"]["b"] == -2.0


def test_ledger_history_trimmed_to_keep_months():
    """[OPT-4] history 只留近 HISTORY_KEEP_MONTHS 個月，不無限膨脹（餘額不受影響）。"""
    led = {"r": {}, "vs": {}, "history": []}
    keep = lg.HISTORY_KEEP_MONTHS
    for i in range(keep + 6):                    # 結算超過上限的月數
        ym = f"20{20 + i // 12:02d}-{i % 12 + 1:02d}"
        lg.settle_month(led, "r", ym, {"a": 2, "b": 0})
    months = {h["month"] for h in led["history"]}
    assert len(months) == keep                   # 只留近 keep 個月
    # 最舊的月份已被修剪、最新的保留
    assert "2020-01" not in months


# ─── storage ──────────────────────────────────────────────────────────────
def test_storage_month_roundtrip_snapshot_finalize(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_month("2026-08", {"r_duty": {"2026-08-01": {"person": "a"}}})
    st.save_month("2026-08", {"r_duty": {}})     # 第二次寫 → 產生快照
    snaps = list((tmp_path / "months").glob("2026-08.json.bak-*"))
    assert len(snaps) == 1
    data = st.load_month("2026-08")
    assert data["month"] == "2026-08" and data["finalized"] is False
    # 定案後未 force 不可寫
    data["finalized"] = True
    st.save_month("2026-08", data)
    try:
        st.save_month("2026-08", {"x": 1})
        assert False, "應拋 FinalizedMonthError"
    except FinalizedMonthError:
        pass
    st.save_month("2026-08", {"x": 1}, force=True)   # force 可


def test_storage_snapshot_unique_same_second(tmp_path):
    """[codex P2] 同一秒內連續存檔,快照不得互相覆蓋。"""
    st = RosterStorage(str(tmp_path))
    st.save_month("2026-09", {"v": 0})
    for i in range(3):                                # 快速連存 3 次
        st.save_month("2026-09", {"v": i + 1})
    snaps = list((tmp_path / "months").glob("2026-09.json.bak-*"))
    assert len(snaps) == 3                            # 三份快照皆保留


def test_storage_holiday_duty_and_set(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_holiday_duty({"r": {date(2026, 9, 28): "r_a"},
                          "vs": {date(2026, 10, 9): "J"}})
    t = st.load_holiday_duty()
    assert t["r"][date(2026, 9, 28)] == "r_a"
    assert st.holidays_set() == {date(2026, 9, 28), date(2026, 10, 9)}


def test_storage_week_colors_merge(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_week_colors(2026, {"2026-W31": "pink"})
    st.save_week_colors(2026, {"2026-W32": "green"})
    assert st.load_week_colors() == {"2026-W31": "pink", "2026-W32": "green"}


def test_storage_week_colors_replace_can_delete(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_week_colors(2026, {"2026-W31": "pink", "2026-W32": "green"})
    # replace=True 整組取代 → 可真正刪掉 W31（merge 做不到）
    st.save_week_colors(2026, {"2026-W32": "green"}, replace=True)
    assert st.load_week_colors() == {"2026-W32": "green"}


def test_storage_prev_month_last_weekend(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_month("2026-07", {"last_weekend": {
        "r": {"saturday": "2026-07-25", "person": "r_b"}}})
    assert st.prev_month_last_weekend("2026-08", "r") == (date(2026, 7, 25), "r_b")
    assert st.prev_month_last_weekend("2026-08", "vs") is None
    assert st.prev_month_last_weekend("2026-01", "r") is None  # 上月=去年12月,無檔


def test_storage_newer_schema_rejected(tmp_path):
    st = RosterStorage(str(tmp_path))
    p = tmp_path / "months" / "2026-08.json"
    p.write_text('{"schema_version": 99}', encoding="utf-8")
    try:
        st.load_month("2026-08")
        assert False, "應拋 NewerSchemaError"
    except NewerSchemaError:
        pass


def test_storage_save_paths_guard_newer_schema(tmp_path):
    """[codex P2] ledger/週色/年度假日表 存檔也要防「舊程式降級毀損新版檔」。"""
    st = RosterStorage(str(tmp_path))
    for name, saver in [
        ("ledger.json", lambda: st.save_ledger({"r": {}})),
        ("week_colors.json", lambda: st.save_week_colors(2026, {"2026-W31": "pink"})),
        ("holiday_duty.json", lambda: st.save_holiday_duty({"r": {}, "vs": {}})),
    ]:
        (tmp_path / name).write_text('{"schema_version": 99}', encoding="utf-8")
        try:
            saver()
            assert False, f"{name} 應拋 NewerSchemaError"
        except NewerSchemaError:
            pass


def test_storage_snapshot_pruned_to_keep(tmp_path):
    """[OPT-5] 快照數量上限 KEEP_SNAPSHOTS：連存超過上限只保留最新 N 份。"""
    st = RosterStorage(str(tmp_path))
    st.save_month("2026-10", {"v": 0})               # 首存無快照
    for i in range(KEEP_SNAPSHOTS + 3):              # 之後每存產生一份快照
        st.save_month("2026-10", {"v": i + 1})
    snaps = list((tmp_path / "months").glob("2026-10.json.bak-*"))
    assert len(snaps) == KEEP_SNAPSHOTS


# ─── solver 放寬階梯去重 / 診斷 / 失敗報告（monkeypatch _build_and_solve，無 ortools）──
def _infeasible_counter(monkeypatch):
    calls: list = []

    def fake(ctx, scope, level):
        calls.append(level)
        return "INFEASIBLE", None
    monkeypatch.setattr(sr, "_build_and_solve", fake)
    return calls


def _prep(scope, members, **kw):
    return SolveContext(scope=scope, year=2026, month=8, members=members,
                        **kw).prepare()


def test_solve_vs_skips_duplicate_levels(monkeypatch):
    """[OPT-1] VS 無 duty_range → L0/L1/L2 規則集相同 → 只求 L0，再測 L3 一次。"""
    calls = _infeasible_counter(monkeypatch)
    r = sr.solve_duty(_prep("vs", [Member("D", "D"), Member("J", "J")]))
    assert r.status == "infeasible"
    assert calls == [sr.L0_FULL, sr.L3_NO_COLOR]     # L1/L2 跳過


def test_solve_r_scope_skips_only_L2(monkeypatch):
    """[OPT-1] R 有 duty_range（L1 關）→ L0≠L1 都試、L2==L1 跳過；L3 只測一次。"""
    calls = _infeasible_counter(monkeypatch)
    r = sr.solve_duty(_prep("r", [Member("a", "甲", fixed_weekday=2),
                                  Member("b", "乙")]))
    assert r.status == "infeasible"
    assert sr.L0_FULL in calls and sr.L1_NO_RANGE in calls
    assert sr.L2_RESERVED not in calls
    assert calls.count(sr.L3_NO_COLOR) == 1


def test_diagnose_lists_single_eligible_days(monkeypatch):
    """[OPT-3] 無解診斷列出「僅 1 人可值」的緊繃日。"""
    _infeasible_counter(monkeypatch)
    ctx = _prep("vs", [Member("D", "D"), Member("J", "J")],
                leaves={"J": {date(2026, 8, 3)}})    # 8/3 只剩 D 可值
    r = sr.solve_duty(ctx)
    assert r.status == "infeasible"
    assert any("僅 1 人可值" in s for s in r.diagnosis)
    assert any("8/3" in s for s in r.diagnosis)


def test_report_failure_paths():
    """[OPT-6] build_report 對三種失敗 status 產生可辨識內容。"""
    ctx = _prep("r", [Member("a", "甲"), Member("b", "乙")])
    pf = sr.SolveResult(status="precheck_failed", scope="r",
                        prechecks=[Precheck("error", "core", "8/14 無人可值")])
    t = build_report(ctx, pf, "R 排班")
    assert "未進行求解" in t and "本次狀態: precheck_failed" in t

    inf = sr.SolveResult(status="infeasible", scope="r",
                         diagnosis=["僅 1 人可值: 8/3→僅 a"])
    t = build_report(ctx, inf, "R 排班")
    assert "僅 1 人可值" in t and "本次狀態: infeasible" in t

    nc = sr.SolveResult(status="need_confirm_color", scope="r",
                        diagnosis=["停用色塊規則後可解。"])
    assert "停用色塊規則後可解" in build_report(ctx, nc, "R 排班")


def test_member_roundtrip():
    m = Member.from_dict({"id": "r1", "name": "小明", "level": "R1",
                          "fixed_weekday": 2})
    assert m.to_dict()["fixed_weekday"] == 2
    m2 = Member.from_dict({"id": "J", "name": "張廖"})
    assert m2.fixed_weekday is None and "fixed_weekday" not in m2.to_dict()
