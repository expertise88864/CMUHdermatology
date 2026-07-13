# -*- coding: utf-8 -*-
"""週六切片輪排(2026-07-13 使用者需求):R2/R3 兩人輪流;值班連動優先、
否則次數平衡(全年累計盡量平均);同月重排回滾不重複累計。"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster.model import Member, day_point  # noqa: E402
from cmuh_common.roster.saturday_biopsy import (  # noqa: E402
    assign_saturday_biopsy, biopsy_pair, format_biopsy_section,
    last_assigned_before, month_saturdays, rollback_biopsy, settle_biopsy)
from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.solve_rvs import SolveResult  # noqa: E402
from cmuh_common.roster.storage import RosterStorage  # noqa: E402

YM = "2026-08"   # 2026/8 週六 = 1, 8, 15, 22, 29
SATS = [date(2026, 8, d) for d in (1, 8, 15, 22, 29)]


def members():
    return [Member("r1", "甲", "R1", fixed_weekday=2),
            Member("r2", "乙", "R2", fixed_weekday=3),
            Member("r3", "丙", "R3", fixed_weekday=1)]


def _assign(duty=None, leaves=None, counts=None, last=None):
    return assign_saturday_biopsy(
        year=2026, month=8, members=members(), duty=duty or {},
        leaves=leaves or {}, counts=counts or {}, last_person=last)


# ─── 純邏輯 ───────────────────────────────────────────────────────────────
def test_month_saturdays():
    assert month_saturdays(2026, 8) == SATS


def test_pair_requires_r2_and_r3():
    pair, notes = biopsy_pair(members())
    assert [m.id for m in pair] == ["r2", "r3"]
    pair2, notes2 = biopsy_pair([Member("r1", "甲", "R1"),
                                 Member("r2", "乙", "R2")])
    assert pair2 == [] and any("缺 R3" in n for n in notes2)


def test_duty_linked_saturday():
    # 8/8 值班=r3 → 該週六切片必為 r3(值班連動),即使 r3 次數較多
    duty = {SATS[1]: "r3"}
    assign, _ = _assign(duty=duty, counts={"r2": 0, "r3": 99})
    assert assign[SATS[1]] == {"person": "r3", "reason": "值班連動"}


def test_duty_by_other_person_falls_back_to_balance():
    # 週六值班是 R1(非 pair)→ 按次數平衡
    duty = {SATS[0]: "r1"}
    assign, _ = _assign(duty=duty)
    assert assign[SATS[0]]["reason"] == "次數平衡"


def test_balance_alternates_when_counts_equal():
    # 全月無 pair 值班、次數相同 → r2 起頭輪替(r2,r3,r2,r3,r2 → 3:2)
    assign, _ = _assign()
    picks = [assign[s]["person"] for s in SATS]
    assert picks == ["r2", "r3", "r2", "r3", "r2"]


def test_balance_compensates_existing_skew():
    # 累計 r2=5、r3=2 → 前三個週六全給 r3 補平,再輪替
    assign, _ = _assign(counts={"r2": 5, "r3": 2})
    picks = [assign[s]["person"] for s in SATS]
    assert picks[:3] == ["r3", "r3", "r3"]
    assert picks.count("r3") == 4 and picks.count("r2") == 1


def test_duty_linked_counts_feed_balance():
    # r2 值 8/1、8/8 兩個週六(連動計入次數) → 平衡週六先補 r3 至平手,再輪替
    duty = {SATS[0]: "r2", SATS[1]: "r2"}
    assign, _ = _assign(duty=duty)
    picks = [assign[s]["person"] for s in SATS]
    assert picks[:2] == ["r2", "r2"]           # 值班連動
    assert picks[2:4] == ["r3", "r3"]          # 0:2 → 先補 r3 到 2:2
    assert abs(picks.count("r2") - picks.count("r3")) <= 1   # 終局差 ≤1


def test_cross_month_alternation_via_last_person():
    # 上月最後一次=r2、次數同 → 本月第一個平衡週六給 r3
    assign, _ = _assign(last="r2")
    assert assign[SATS[0]]["person"] == "r3"


def test_leave_excludes_candidate():
    leaves = {"r3": {SATS[0]}}
    assign, _ = _assign(leaves=leaves, counts={"r2": 9, "r3": 0})
    assert assign[SATS[0]]["person"] == "r2"   # r3 請假 → 即使次數多也給 r2


def test_both_on_leave_skips_with_note():
    leaves = {"r2": {SATS[0]}, "r3": {SATS[0]}}
    assign, notes = _assign(leaves=leaves)
    assert SATS[0] not in assign
    assert any("皆請假" in n for n in notes)


def test_duty_linked_person_on_leave_falls_back():
    # [codex P2] 值班=r2 但 r2 當日請假(手改造成的矛盾班表)→ 請假優先,
    # 切片退回次數平衡給 r3、附矛盾註記,不放大矛盾
    duty = {SATS[0]: "r2"}
    leaves = {"r2": {SATS[0]}}
    assign, notes = _assign(duty=duty, leaves=leaves)
    assert assign[SATS[0]]["person"] == "r3"
    assert assign[SATS[0]]["reason"] == "次數平衡"
    assert any("班表矛盾" in n for n in notes)


def test_settle_rollback_idempotent():
    book = {"counts": {}, "history": []}
    assign, _ = _assign()
    settle_biopsy(book, YM, assign)
    once = dict(book["counts"])
    assert once == {"r2": 3, "r3": 2}
    settle_biopsy(book, YM, assign)            # 同月重記 → 不重複累計
    assert book["counts"] == once
    assert rollback_biopsy(book, YM)
    assert all(v == 0 for v in book["counts"].values())


def test_last_assigned_before_reads_history():
    book = {"counts": {}, "history": [
        {"month": "2026-07", "assign": {"2026-07-25": "r3",
                                        "2026-07-18": "r2"}},
        {"month": "2026-08", "assign": {"2026-08-01": "r2"}},
    ]}
    assert last_assigned_before(book, "2026-08") == "r3"   # 只看 8 月前、取最近


def test_format_section_lists_entries():
    assign, notes = _assign()
    pair, _ = biopsy_pair(members())
    txt = format_biopsy_section(assign, notes, {"r2": 3, "r3": 2}, pair,
                                {"r2": "乙", "r3": "丙"})
    assert "[週六切片]" in txt and "8/1(六) → 乙" in txt and "累計 3 次" in txt


# ─── service 整合(手工 SolveResult,免 ortools)────────────────────────────
def _storage(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({
        "r_members": [m.to_dict() for m in members()],
        "vs_members": [{"id": "D", "name": "D醫師"}],
        "points": {"weekday": 1, "weekend": 2, "national_holiday": 1},
        "duty_range_soft": [9, 11],
    })
    return st


def _cover(svc, ym, person, overrides=None):
    ctx = svc.build_context("r", ym)
    a = {d: person for d in ctx.days}
    for d, p in (overrides or {}).items():
        a[d] = p
    for b in ctx.blocks:
        for x in b.days:
            a[x] = a[b.days[0]]
    return a


def _result_for(svc, ym, assignments):
    ctx = svc.build_context("r", ym)
    pts = {m.id: 0 for m in ctx.members}
    for d, mid in assignments.items():
        if mid in pts:
            pts[mid] += day_point(d, ctx.holidays, ctx.params)
    return SolveResult(status="ok", scope="r", level_used=0, level_name="L0",
                       assignments=dict(assignments), points_by_person=pts)


def test_accept_writes_biopsy_and_counts(tmp_path):
    svc = RosterService(_storage(tmp_path))
    # 8/8 週末給 r3(值班連動);其餘給 r1(非 pair → 平衡)
    a = _cover(svc, YM, "r1", {date(2026, 8, 8): "r3", date(2026, 8, 9): "r3"})
    svc.accept_solution("r", YM, _result_for(svc, YM, a))

    month = svc.storage.load_month(YM)
    sb = month["saturday_biopsy"]
    assert sb["2026-08-08"] == {"person": "r3", "reason": "值班連動"}
    assert all(iso in sb for iso in
               ("2026-08-01", "2026-08-15", "2026-08-22", "2026-08-29"))
    assert "[週六切片]" in (month.get("report_r") or "")
    book = svc.storage.load_biopsy()
    assert sum(book["counts"].values()) == 5           # 每個週六恰一人
    # 兩人差 ≤1(平衡)
    assert abs(book["counts"].get("r2", 0) - book["counts"].get("r3", 0)) <= 1


def test_reaccept_does_not_double_count(tmp_path):
    svc = RosterService(_storage(tmp_path))
    a = _cover(svc, YM, "r1")
    r = _result_for(svc, YM, a)
    svc.accept_solution("r", YM, r)
    first = dict(svc.storage.load_biopsy()["counts"])
    svc.accept_solution("r", YM, _result_for(svc, YM, a))
    assert svc.storage.load_biopsy()["counts"] == first


def test_set_cell_saturday_recomputes_linkage(tmp_path):
    svc = RosterService(_storage(tmp_path))
    a = _cover(svc, YM, "r1")
    svc.accept_solution("r", YM, _result_for(svc, YM, a))
    # 手動把 8/15 值班改成 r2 → 切片應自動改為 r2(值班連動)
    svc.set_cell("r", YM, date(2026, 8, 15), "r2")
    sb = svc.storage.load_month(YM)["saturday_biopsy"]
    assert sb["2026-08-15"] == {"person": "r2", "reason": "值班連動"}
    # quick_validate 無「連動不符」警告
    warns = [c for c in svc.quick_validate("r", YM)
             if c.rule_id == "saturday_biopsy" and c.severity == "warn"]
    assert warns == []


def test_set_leaves_recomputes_biopsy(tmp_path):
    # [codex P2] accept 後改請假 → 切片自動重排(請假者不再被排)
    svc = RosterService(_storage(tmp_path))
    a = _cover(svc, YM, "r1")
    svc.accept_solution("r", YM, _result_for(svc, YM, a))
    sb0 = svc.storage.load_month(YM)["saturday_biopsy"]
    victim = sb0["2026-08-01"]["person"]           # 8/1 原切片人
    svc.set_leaves("r", YM, victim, [date(2026, 8, 1)])
    sb = svc.storage.load_month(YM)["saturday_biopsy"]
    assert sb["2026-08-01"]["person"] != victim    # 請假者被換掉
    # 無「切片人請假」警告殘留
    warns = [c for c in svc.quick_validate("r", YM)
             if c.rule_id == "saturday_biopsy" and c.severity == "warn"]
    assert warns == []


def test_quick_validate_warns_stale_biopsy_on_leave(tmp_path):
    # 安全網:外部途徑把月檔改成「切片人請假」→ quick_validate 要警告
    svc = RosterService(_storage(tmp_path))
    month = svc.storage.load_month(YM)
    month["saturday_biopsy"] = {"2026-08-01": {"person": "r2",
                                               "reason": "次數平衡"}}
    month["leaves"] = {"r": {"r2": ["2026-08-01"]}}
    svc.storage.save_month(YM, month)
    warns = [c for c in svc.quick_validate("r", YM)
             if c.rule_id == "saturday_biopsy" and "請假" in c.msg]
    assert warns


def test_set_cell_refreshes_report_biopsy_section(tmp_path):
    # [codex P2] 手改週六格 → 已存報告的[週六切片]段同步刷新(定案 PDF 讀 report_r)
    svc = RosterService(_storage(tmp_path))
    a = _cover(svc, YM, "r1")
    svc.accept_solution("r", YM, _result_for(svc, YM, a))
    svc.set_cell("r", YM, date(2026, 8, 15), "r2")
    rpt = svc.storage.load_month(YM)["report_r"]
    assert rpt.count("[週六切片]") == 1            # 段落被取代,不是重複附加
    assert "8/15(六) → 乙（值班連動）" in rpt      # 新連動人選已入報告


def test_build_export_carries_biopsy(tmp_path):
    svc = RosterService(_storage(tmp_path))
    a = _cover(svc, YM, "r1")
    svc.accept_solution("r", YM, _result_for(svc, YM, a))
    data = svc.build_export(YM)
    assert set(data["saturday_biopsy"]) == set(SATS)
    assert set(data["saturday_biopsy"].values()) <= {"r2", "r3"}


def test_render_report_preview_contains_biopsy(tmp_path):
    svc = RosterService(_storage(tmp_path))
    a = _cover(svc, YM, "r1")
    txt = svc.render_report("r", YM, _result_for(svc, YM, a))
    assert "[週六切片]" in txt
    # 預覽不落地:biopsy.json 不應被寫入
    assert svc.storage.load_biopsy()["history"] == []
