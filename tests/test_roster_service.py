# -*- coding: utf-8 -*-
"""roster service 層（引擎↔檔案↔UI 黏合）。多數用手工 SolveResult，免 ortools；
最後一個整合測試才 importorskip("ortools")。"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster.model import day_point  # noqa: E402
from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.solve_rvs import SolveResult  # noqa: E402
from cmuh_common.roster.storage import (  # noqa: E402
    FinalizedMonthError, RosterStorage,
)

YM = "2026-08"   # 2026/8/1 = 週六


def _storage(tmp_path, r_members=("A", "B")):
    st = RosterStorage(str(tmp_path))
    st.save_config({
        "r_members": [{"id": mid, "name": f"名{mid}"} for mid in r_members],
        "vs_members": [{"id": "D", "name": "D醫師"}],
        "points": {"weekday": 1, "weekend": 2, "national_holiday": 1},
        "duty_range_soft": [9, 11],
    })
    return st


def _svc(tmp_path, **kw):
    return RosterService(_storage(tmp_path, **kw))


def _cover(svc, ym, person="A", overrides=None):
    """整月全覆蓋的假 assignments（accept 要求涵蓋全月）：預設全給 person，
    overrides 蓋特定日；最後把每個值班區塊統一成同一人（避免成對不一致）。"""
    ctx = svc.build_context("r", ym)
    a = {d: person for d in ctx.days}
    for d, p in (overrides or {}).items():
        a[d] = p
    for b in ctx.blocks:
        for x in b.days:
            a[x] = a[b.days[0]]
    return a


def _result_for(svc, ym, assignments, last_weekend=None):
    """依當前 ctx 的點數規則，由 assignments 算出「一致的」points_by_person
    的 ok 結果（accept 會用當前 ctx 重算點數核對，不一致會被判過期）。"""
    ctx = svc.build_context("r", ym)
    pts = {m.id: 0 for m in ctx.members}
    for d, mid in assignments.items():
        if mid in pts:
            pts[mid] += day_point(d, ctx.holidays, ctx.params)
    return SolveResult(
        status="ok", scope="r", level_used=0, level_name="L0",
        assignments=dict(assignments), points_by_person=pts,
        last_weekend=last_weekend)


# ─── build_context ────────────────────────────────────────────────────────
def test_build_context_fields_and_iso(tmp_path):
    st = _storage(tmp_path)
    st.save_holiday_duty({"r": {date(2026, 8, 15): "A"}, "vs": {}})
    st.save_month(YM, {
        "leaves": {"r": {"A": ["2026-08-10"]}},
        "must_duty": {"r": {"B": ["2026-08-05"]}},
        "r_duty": {"2026-08-03": {"person": "A", "locked": True,
                                  "source": "manual"}},
    })
    ctx = RosterService(st).build_context("r", YM)

    assert ctx.member_ids() == ["A", "B"]
    assert ctx.year == 2026 and ctx.month == 8
    assert ctx.leaves == {"A": {date(2026, 8, 10)}}
    assert ctx.must_duty == {"B": {date(2026, 8, 5)}}
    assert ctx.annual_holiday == {date(2026, 8, 15): "A"}
    assert date(2026, 8, 15) in ctx.holidays
    assert ctx.locks == {date(2026, 8, 3): "A"}       # 由鎖定格反推
    assert ctx.days and ctx.blocks                    # 已 prepare()
    assert ctx.params.duty_min == 9


def test_build_context_ignores_unlocked_cells(tmp_path):
    st = _storage(tmp_path)
    st.save_month(YM, {"r_duty": {
        "2026-08-03": {"person": "A", "locked": False, "source": "auto"}}})
    ctx = RosterService(st).build_context("r", YM)
    assert ctx.locks == {}                            # 非鎖定格不進 locks


# ─── accept_solution ──────────────────────────────────────────────────────
def test_accept_writes_month_ledger_lastweekend(tmp_path):
    svc = _svc(tmp_path)
    res = _result_for(svc, YM, _cover(svc, YM, "A"),
                      last_weekend={"saturday": "2026-08-29", "person": "B"})
    svc.accept_solution("r", YM, res)

    month = svc.storage.load_month(YM)
    assert month["r_duty"]["2026-08-01"] == {
        "person": "A", "locked": False, "source": "auto"}
    assert month["last_weekend"]["r"] == {
        "saturday": "2026-08-29", "person": "B"}
    assert "決策報告" in month["report_r"]

    ledger = svc.storage.load_ledger()
    # 全部給 A：A 拿走全部點數、B 0 → 兩人 delta 相反、相加為 0
    assert ledger["r"]["A"] > 0 and ledger["r"]["B"] < 0
    assert round(ledger["r"]["A"] + ledger["r"]["B"], 4) == 0.0
    assert [h for h in ledger["history"]
            if h["month"] == YM and h["scope"] == "r"]


def test_accept_preserves_locked_cell(tmp_path):
    svc = _svc(tmp_path)
    svc.storage.save_month(YM, {"r_duty": {
        "2026-08-03": {"person": "B", "locked": True, "source": "manual"}}})
    # 正常情形：鎖定 B → build_context 把它當 directive → result 8/3 也是 B（相符）
    res = _result_for(svc, YM, _cover(svc, YM, "A", overrides={date(2026, 8, 3): "B"}))
    svc.accept_solution("r", YM, res)

    month = svc.storage.load_month(YM)
    # 鎖定格保留原 person/locked/source（source 仍為 manual，非 auto）
    assert month["r_duty"]["2026-08-03"] == {
        "person": "B", "locked": True, "source": "manual"}
    assert month["r_duty"]["2026-08-01"]["source"] == "auto"


def test_resettle_from_duty_reflects_manual_edits(tmp_path):
    svc = _svc(tmp_path)
    svc.accept_solution("r", YM, _result_for(svc, YM, _cover(svc, YM, "A")))
    svc.set_cell("r", YM, date(2026, 8, 5), "B")     # 8/5(週二) 手動換給 B
    pts = svc.resettle_from_duty("r", YM)
    assert pts["B"] == 1                              # B 得 8/5 一個平日點
    hist = [h for h in svc.storage.load_ledger()["history"]
            if h["month"] == YM and h["scope"] == "r"]
    assert len(hist) == 1                             # 同月回滾重記，只一筆


def test_finalize_resettles_ledger_from_final_duty(tmp_path):
    svc = _svc(tmp_path)
    svc.accept_solution("r", YM, _result_for(svc, YM, _cover(svc, YM, "A")))
    b_before = svc.storage.load_ledger()["r"]["B"]
    svc.set_cell("r", YM, date(2026, 8, 5), "B")
    svc.finalize(YM, True)                            # 定案 → 依最終排班重算
    assert svc.storage.load_ledger()["r"]["B"] > b_before   # B 值了 8/5 → 帳本上升


def test_finalize_resettles_even_after_duty_cleared(tmp_path):
    """accept 後把 R 排班全清 → 定案仍要回滾該月結算（帳本歸零，不留舊分錄）。"""
    svc = _svc(tmp_path)
    svc.accept_solution("r", YM, _result_for(svc, YM, _cover(svc, YM, "A")))
    assert svc.storage.load_ledger()["r"]["A"] > 0
    m = svc.storage.load_month(YM)
    m["r_duty"] = {}
    svc.storage.save_month(YM, m)
    svc.finalize(YM, True)
    led = svc.storage.load_ledger()["r"]
    assert led["A"] == 0.0 and led["B"] == 0.0        # 空排班 → 該月結算回滾為 0


def test_resettle_blocked_when_finalized(tmp_path):
    svc = _svc(tmp_path)
    svc.accept_solution("r", YM, _result_for(svc, YM, _cover(svc, YM, "A")))
    svc.finalize(YM, True)
    with pytest.raises(FinalizedMonthError):
        svc.resettle_from_duty("r", YM)


def test_resettle_rolls_back_when_members_emptied(tmp_path):
    """名單清空後重算 → 仍回滾該月舊結算（不再 early-return 留殘餘）。"""
    svc = _svc(tmp_path)
    svc.accept_solution("r", YM, _result_for(svc, YM, _cover(svc, YM, "A")))
    cfg = svc.storage.load_config()
    cfg["r_members"] = []
    svc.storage.save_config(cfg)
    svc.resettle_from_duty("r", YM)
    led = svc.storage.load_ledger()
    hist = [h for h in led["history"]
            if h["month"] == YM and h["scope"] == "r"]
    assert len(hist) == 1 and hist[0]["deltas"] == {}    # 舊分錄回滾、重記為空
    assert led["r"]["A"] == 0.0 and led["r"]["B"] == 0.0  # 餘額歸零


def test_accept_rejects_non_ok(tmp_path):
    svc = _svc(tmp_path)
    with pytest.raises(ValueError):
        svc.accept_solution("r", YM, SolveResult(status="infeasible", scope="r"))


def test_accept_rejects_scope_mismatch(tmp_path):
    """r 分頁誤套 vs 結果 → 早擋，不寫錯 duty 表/帳本。"""
    svc = _svc(tmp_path)
    res = SolveResult(status="ok", scope="vs",
                      assignments={date(2026, 8, 1): "D"},
                      points_by_person={"D": 2})
    with pytest.raises(ValueError):
        svc.accept_solution("r", YM, res)
    assert svc.storage.load_ledger()["vs"] == {}    # 未落地


def test_accept_rejects_stale_lock(tmp_path):
    """預覽後鎖定又被改動（result 8/3=A 但現鎖定=B）→ 拒絕，且無半套寫入。"""
    svc = _svc(tmp_path)
    svc.storage.save_month(YM, {"r_duty": {
        "2026-08-03": {"person": "B", "locked": True, "source": "manual"}}})
    res = _result_for(svc, YM, _cover(svc, YM, "A"))   # 8/3=A 但鎖定=B
    with pytest.raises(ValueError):
        svc.accept_solution("r", YM, res)
    assert svc.storage.load_ledger()["r"] == {}     # 帳本未被 settle


def test_rf20_clear_unlocked_keeps_locked_and_clears_report(tmp_path):
    """RF-20：clear_unlocked 一次 save、僅保留鎖定格、清空 report_r。"""
    svc = _svc(tmp_path)
    svc.set_cell("r", YM, date(2026, 8, 5), "A")
    svc.set_cell("r", YM, date(2026, 8, 6), "B")
    svc.set_cell("r", YM, date(2026, 8, 7), "A")
    svc.toggle_lock("r", YM, date(2026, 8, 5))         # 鎖 8/5
    m = svc.storage.load_month(YM)
    m["report_r"] = "OLD REPORT"
    svc.storage.save_month(YM, m)
    svc.clear_unlocked("r", YM)
    reload = svc.storage.load_month(YM)
    assert set(reload["r_duty"]) == {"2026-08-05"}     # 僅鎖定格保留
    assert reload["report_r"] == ""                    # 舊報告清空


def test_rf20_clear_unlocked_finalized_guard(tmp_path):
    """RF-20：已定案月 clear_unlocked 應拋 FinalizedMonthError。"""
    svc = _svc(tmp_path)
    svc.set_cell("r", YM, date(2026, 8, 5), "A")
    m = svc.storage.load_month(YM)
    m["finalized"] = True
    svc.storage.save_month(YM, m)
    with pytest.raises(FinalizedMonthError):
        svc.clear_unlocked("r", YM)


def test_rf03_rejects_lock_person_left_roster(tmp_path):
    """RF-03：鎖定格人選被移出名單 → accept 拒絕（不寫出班表≠帳本的分歧狀態）。"""
    svc = _svc(tmp_path, r_members=("A", "B", "C"))
    svc.storage.save_month(YM, {"r_duty": {
        "2026-08-03": {"person": "C", "locked": True, "source": "manual"}}})
    cfg = svc.storage.load_config()                    # 移除 C
    cfg["r_members"] = [m for m in cfg["r_members"] if m["id"] != "C"]
    svc.storage.save_config(cfg)
    res = _result_for(svc, YM, _cover(svc, YM, "A",
                                      overrides={date(2026, 8, 3): "B"}))
    with pytest.raises(ValueError, match="鎖定格"):
        svc.accept_solution("r", YM, res)
    assert svc.storage.load_ledger()["r"] == {}       # 無半套寫入
    assert svc.storage.load_month(YM)["r_duty"]["2026-08-03"]["person"] == "C"


def test_accept_rejects_stale_after_leave_change(tmp_path):
    """預覽後才有人請假（非鎖定變動）→ 舊 result 把請假者排上 → 拒絕。"""
    svc = _svc(tmp_path)
    res = _result_for(svc, YM, _cover(svc, YM, "A"))
    svc.set_leaves("r", YM, "A", {date(2026, 8, 5)})   # 預覽後才請假
    with pytest.raises(ValueError):
        svc.accept_solution("r", YM, res)              # 8/5 排 A 但 A 已請假
    assert svc.storage.load_ledger()["r"] == {}


def test_accept_rejects_stale_after_member_added(tmp_path):
    """預覽後新增成員 C → 舊 result 名單較小，逐格檢查會過，但結算人數已變 → 拒絕。"""
    svc = _svc(tmp_path)                                # 名單 A,B
    res = _result_for(svc, YM, _cover(svc, YM, "A"))
    cfg = svc.storage.load_config()
    cfg["r_members"].append({"id": "C", "name": "名C"})   # 預覽後新增
    svc.storage.save_config(cfg)
    with pytest.raises(ValueError):
        svc.accept_solution("r", YM, res)
    assert svc.storage.load_ledger()["r"] == {}


def test_accept_rejects_stale_after_point_change(tmp_path):
    """預覽後點數規則變動（週末 2→3）→ 舊 points 已錯 → 拒絕（不 settle 錯帳本）。"""
    svc = _svc(tmp_path)
    res = _result_for(svc, YM, _cover(svc, YM, "A"))   # points 依「週末=2」算
    cfg = svc.storage.load_config()
    cfg["points"]["weekend"] = 3
    svc.storage.save_config(cfg)
    with pytest.raises(ValueError):
        svc.accept_solution("r", YM, res)
    assert svc.storage.load_ledger()["r"] == {}


def test_double_accept_no_double_count(tmp_path):
    svc = _svc(tmp_path)
    res = _result_for(svc, YM, _cover(svc, YM, "A"))
    svc.accept_solution("r", YM, res)
    a1 = svc.storage.load_ledger()["r"]["A"]
    svc.accept_solution("r", YM, res)          # 同月二次 → 內含回滾

    ledger = svc.storage.load_ledger()
    assert ledger["r"]["A"] == a1              # 不因二次而加倍
    assert len([h for h in ledger["history"]
                if h["month"] == YM and h["scope"] == "r"]) == 1


# ─── 手動編輯 ─────────────────────────────────────────────────────────────
def test_set_cell_audit_and_weekend_warning(tmp_path):
    svc = _svc(tmp_path)
    # 只排週六(8/1)、週日(8/2)未排 → 成對不完整 warn
    checks = svc.set_cell("r", YM, date(2026, 8, 1), "A")
    assert any(c.rule_id == "weekend_pair" for c in checks)
    audit = svc.storage.load_month(YM)["audit"]
    assert audit[-1]["cell"] == "2026-08-01" and audit[-1]["new"] == "A"

    # 週日排不同人 → 成對被改破 warn
    checks = svc.set_cell("r", YM, date(2026, 8, 2), "B")
    assert any(c.rule_id == "weekend_pair" for c in checks)

    # 週日改同一人 → 該段不再有 weekend_pair 警告
    checks = svc.set_cell("r", YM, date(2026, 8, 2), "A")
    assert not any(c.rule_id == "weekend_pair" for c in checks)


def test_set_cell_clear_removes(tmp_path):
    svc = _svc(tmp_path)
    svc.set_cell("r", YM, date(2026, 8, 5), "A")
    svc.set_cell("r", YM, date(2026, 8, 5), None)
    assert "2026-08-05" not in svc.storage.load_month(YM)["r_duty"]


def test_toggle_lock(tmp_path):
    svc = _svc(tmp_path)
    assert svc.toggle_lock("r", YM, date(2026, 8, 5)) is False   # 空格不可鎖
    svc.set_cell("r", YM, date(2026, 8, 5), "A")
    assert svc.toggle_lock("r", YM, date(2026, 8, 5)) is True
    assert svc.build_context("r", YM).locks == {date(2026, 8, 5): "A"}
    assert svc.toggle_lock("r", YM, date(2026, 8, 5)) is False


def test_set_leaves_and_must_roundtrip(tmp_path):
    svc = _svc(tmp_path)
    svc.set_leaves("r", YM, "A", {date(2026, 8, 10), date(2026, 8, 11)})
    svc.set_must("r", YM, "B", {date(2026, 8, 20)})
    ctx = svc.build_context("r", YM)
    assert ctx.leaves["A"] == {date(2026, 8, 10), date(2026, 8, 11)}
    assert ctx.must_duty["B"] == {date(2026, 8, 20)}

    svc.set_leaves("r", YM, "A", set())          # 清空 → 移除
    assert "A" not in svc.build_context("r", YM).leaves


def test_quick_validate_flags_invalid_manual_cell(tmp_path):
    """未鎖定手排把請假者/非名單者排上 → quick_validate 要抓到（§3.1 缺口）。"""
    svc = _svc(tmp_path)
    svc.set_leaves("r", YM, "A", {date(2026, 8, 5)})
    checks = svc.set_cell("r", YM, date(2026, 8, 5), "A")     # A 請假卻排 A
    assert any(c.rule_id == "manual_cell" for c in checks)

    checks = svc.set_cell("r", YM, date(2026, 8, 6), "ZZZ")   # 非名單
    assert any(c.rule_id == "manual_cell" and "不在名單" in c.msg
               for c in checks)


# ─── finalize ─────────────────────────────────────────────────────────────
def test_finalize_blocks_then_unlocks_save(tmp_path):
    svc = _svc(tmp_path)
    svc.set_cell("r", YM, date(2026, 8, 5), "A")
    svc.finalize(YM, True)
    with pytest.raises(FinalizedMonthError):
        svc.set_cell("r", YM, date(2026, 8, 6), "B")
    svc.finalize(YM, False)                       # 解除（force）
    svc.set_cell("r", YM, date(2026, 8, 6), "B")  # 可再編輯
    assert svc.storage.load_month(YM)["r_duty"]["2026-08-06"]["person"] == "B"


# ─── 整合：真的求解一次（需 ortools）────────────────────────────────────────
def test_run_solve_then_accept_integration(tmp_path):
    pytest.importorskip("ortools")
    svc = _svc(tmp_path, r_members=("A", "B", "C"))   # 3 人 31 天 9-11 可行
    res = svc.run_solve("r", YM)
    assert res.status == "ok", f"預期可解，實得 {res.status}: {res.diagnosis}"
    # 決定性：落地前同輸入重解一致（accept 會改帳本 → 之後解自然不同，不可比）
    assert svc.run_solve("r", YM).assignments == res.assignments

    svc.accept_solution("r", YM, res)
    month = svc.storage.load_month(YM)
    assert len(month["r_duty"]) == 31             # 每天都排到
    assert "決策報告" in month["report_r"]
    assert month["last_weekend"]["r"]["person"]
