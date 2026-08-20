# -*- coding: utf-8 -*-
"""[2026-07-27 使用者] 兩件事：

1. 連休段可依【手動指定】拆段：使用者要 9/25-27 給 Z、9/28 給 K，舊版因
   「週末連休段須同一人」直接 precheck_failed，怎麼指定都排不出來。
2. 週六切片可【右鍵強制指定人選】，且指定要能撐過之後所有重排（手改值班、
   請假變動、重跑自動排班）。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster.model import (  # noqa: E402
    Member, SolveContext, build_duty_blocks,
)
from cmuh_common.roster.rules import split_block_runs  # noqa: E402
from cmuh_common.roster.saturday_biopsy import (  # noqa: E402
    assign_saturday_biopsy,
)
from cmuh_common.roster.solve_rvs import solve_duty  # noqa: E402

_SEP_HOL = {date(2026, 9, 25), date(2026, 9, 28)}      # 9/25(五)、9/28(一) 國定假日
_MEMBERS = [Member("K", "陳翊嘉", "R3"), Member("C", "林于喬", "R2"),
            Member("Z", "賴奕彰", "R1")]


# ─── 1. 連休段依指定拆段 ────────────────────────────────────────────────────
def test_split_runs_by_directives():
    days = [date(2026, 9, d) for d in (25, 26, 27, 28)]
    directed = {days[0]: "Z", days[1]: "Z", days[2]: "Z", days[3]: "K"}
    runs = split_block_runs(days, directed)
    assert [[d.day for d in r] for r in runs] == [[25, 26, 27], [28]]


def test_split_runs_undirected_days_join_previous_run():
    """只指定頭尾 → 中間未指定日併入前段（9/25 Z 帶著 26、27，9/28 獨立給 K）。"""
    days = [date(2026, 9, d) for d in (25, 26, 27, 28)]
    runs = split_block_runs(days, {days[0]: "Z", days[3]: "K"})
    assert [[d.day for d in r] for r in runs] == [[25, 26, 27], [28]]


def test_split_runs_no_or_single_directive_keeps_whole_block():
    """無指定、或全段同一人 → 單一段（＝原本「整段同一人」行為完全不變）。"""
    days = [date(2026, 9, d) for d in (25, 26, 27, 28)]
    assert len(split_block_runs(days, {})) == 1
    assert len(split_block_runs(days, {days[1]: "Z"})) == 1
    assert len(split_block_runs(days, {days[0]: "Z", days[3]: "Z"})) == 1


def test_holiday_chain_builds_four_day_block():
    """前提確認：9/25(五假)+26(六)+27(日)+28(一假) 會被鏈成同一個連休段。"""
    blocks = build_duty_blocks(2026, 9, _SEP_HOL)
    b = next(b for b in blocks if date(2026, 9, 26) in b.days)
    assert [d.day for d in b.days] == [25, 26, 27, 28]
    assert b.saturday == date(2026, 9, 26)


def test_september_split_directive_solves_as_requested():
    """使用者實際情境：9/25-27 指定 Z、9/28 指定 K → 求解成功且完全照指定。"""
    ctx = SolveContext(
        scope="r", year=2026, month=9, members=_MEMBERS, holidays=_SEP_HOL,
        annual_holiday={date(2026, 9, 25): "Z", date(2026, 9, 26): "Z",
                        date(2026, 9, 27): "Z", date(2026, 9, 28): "K"},
        ledger={"C": 1.3, "K": -0.7, "Z": -0.7})
    res = solve_duty(ctx)
    assert res.status == "ok", f"仍排不出來: {res.status}"
    for day, who in ((25, "Z"), (26, "Z"), (27, "Z"), (28, "K")):
        assert res.assignments[date(2026, 9, day)] == who, \
            f"9/{day} 應為 {who}: {res.assignments.get(date(2026, 9, day))}"
    assert any(c.severity == "warn" and "拆成" in c.msg for c in res.prechecks)


def test_last_weekend_person_taken_from_saturday():
    """跨月銜接記錄的「上月最後週末」要取【週六】當天的人——連休段可能往前鏈入
    週五假日（days[0]=週五），拆段後 days[0] 未必是值週末的人。"""
    ctx = SolveContext(
        scope="r", year=2026, month=9, members=_MEMBERS, holidays=_SEP_HOL,
        annual_holiday={date(2026, 9, 25): "K", date(2026, 9, 26): "Z",
                        date(2026, 9, 27): "Z", date(2026, 9, 28): "Z"},
        ledger={})
    res = solve_duty(ctx)
    assert res.status == "ok"
    assert res.last_weekend["saturday"] == "2026-09-26"
    assert res.last_weekend["person"] == "Z", \
        f"應取週六當天的人,不是 days[0](9/25 的 K): {res.last_weekend}"


def _svc_with_members(tmp_path):
    from cmuh_common.roster.service import RosterService
    from cmuh_common.roster.storage import RosterStorage
    st = RosterStorage(str(tmp_path))
    st.save_config({
        "r_members": [m.to_dict() for m in _MEMBERS],
        "vs_members": [{"id": "D", "name": "D醫師"}],
        "points": {"weekday": 1, "weekend": 2, "national_holiday": 1},
    })
    return RosterService(st), st


def test_split_result_can_be_accepted(tmp_path):
    """[2026-07-27 事故] 求解成功後【套用】仍被「結果已過期（連休段 9/25 起已非
    同一人）」擋下 → 使用者看得到結果卻永遠存不進去。套用檢查也要以拆出的段為單位。"""
    svc, st = _svc_with_members(tmp_path)
    st.save_holiday_duty({"r": {"2026-09-25": "Z", "2026-09-26": "Z",
                                "2026-09-27": "Z", "2026-09-28": "K"},
                          "vs": {}})
    ym = "2026-09"
    # ★生產的呼叫形狀★:run_solve 蓋 month_revision(RS-13)
    res = svc.run_solve("r", ym)
    assert res.status == "ok"
    svc.accept_solution("r", ym, res)              # 不得拋「排班結果已過期」
    duty = st.load_month(ym)["r_duty"]
    for day, who in ((25, "Z"), (26, "Z"), (27, "Z"), (28, "K")):
        assert (duty.get(f"2026-09-{day}") or {}).get("person") == who


def test_split_does_not_warn_pair_broken(tmp_path):
    """依指定拆段是預期結果 → 套用後不該一直跳「成對被改破」。"""
    svc, st = _svc_with_members(tmp_path)
    st.save_holiday_duty({"r": {"2026-09-25": "Z", "2026-09-26": "Z",
                                "2026-09-27": "Z", "2026-09-28": "K"},
                          "vs": {}})
    ym = "2026-09"
    svc.accept_solution("r", ym, svc.run_solve("r", ym))
    msgs = [c.msg for c in svc.quick_validate("r", ym)]
    assert not any("成對被改破" in m for m in msgs), msgs
    assert not any("成對不完整" in m for m in msgs), msgs


def test_unsplit_block_broken_manually_still_warns(tmp_path):
    """反面：沒有指定的連休段被手動排成不同人 → 仍要警告（防護沒被拆段機制吃掉）。"""
    svc, st = _svc_with_members(tmp_path)
    ym = "2026-08"                                  # 8/8(六)、8/9(日) 無假日鏈
    svc.set_cell("r", ym, date(2026, 8, 8), "Z")
    svc.set_cell("r", ym, date(2026, 8, 9), "K")
    msgs = [c.msg for c in svc.quick_validate("r", ym)]
    assert any("成對被改破" in m for m in msgs), msgs


# ─── 2. 週六切片手動指定 ────────────────────────────────────────────────────
def _sat_kwargs(**over):
    base = dict(year=2026, month=8, members=_MEMBERS,
                duty={}, leaves={}, counts={})
    base.update(over)
    return base


def test_biopsy_override_wins_over_duty_link():
    """手動指定蓋過「值班連動」：8/8 值班是 C，但指定 K → 切片給 K。"""
    assign, _notes = assign_saturday_biopsy(**_sat_kwargs(
        duty={date(2026, 8, 8): "C"},
        overrides={date(2026, 8, 8): "K"}))
    assert assign[date(2026, 8, 8)]["person"] == "K"
    assert assign[date(2026, 8, 8)]["reason"] == "手動指定"


def test_biopsy_override_counts_toward_balance():
    """指定者照樣累計次數 → 之後的週六次數平衡會把它算進去（不會連續都給同一人）。"""
    assign, _notes = assign_saturday_biopsy(**_sat_kwargs(
        overrides={date(2026, 8, 1): "K", date(2026, 8, 8): "K"}))
    # 8/1、8/8 指定 K（K 累計 2）→ 8/15 自動排應輪到另一位（C）
    assert assign[date(2026, 8, 15)]["person"] == "C"
    assert assign[date(2026, 8, 15)]["reason"] == "次數平衡"


def test_biopsy_override_on_leave_kept_with_note():
    """指定者當日請假 → 仍照指定排入，只附註提醒（與鎖定格同語意，不靜默改人）。"""
    assign, notes = assign_saturday_biopsy(**_sat_kwargs(
        leaves={"K": {date(2026, 8, 8)}},
        overrides={date(2026, 8, 8): "K"}))
    assert assign[date(2026, 8, 8)]["person"] == "K"
    assert any("手動指定切片 K" in n and "請假" in n for n in notes)


def test_biopsy_override_outside_pair_ignored_with_note():
    """指定的人不是本月 R2/R3 → 忽略並附註，改回自動（不寫入幽靈代號）。"""
    assign, notes = assign_saturday_biopsy(**_sat_kwargs(
        overrides={date(2026, 8, 8): "Z"}))       # Z 是 R1，不在切片配對
    assert assign[date(2026, 8, 8)]["person"] in ("C", "K")
    assert assign[date(2026, 8, 8)]["reason"] != "手動指定"
    assert any("不是本月 R2/R3" in n for n in notes)


def test_service_pin_survives_duty_edit(tmp_path):
    """service 層：指定切片後再手改該週六的值班 → 指定不得被連動重排洗掉。"""
    from cmuh_common.roster.service import RosterService
    from cmuh_common.roster.storage import RosterStorage
    st = RosterStorage(str(tmp_path))
    st.save_config({
        "r_members": [m.to_dict() for m in _MEMBERS],
        "vs_members": [{"id": "D", "name": "D醫師"}],
        "points": {"weekday": 1, "weekend": 2, "national_holiday": 1},
    })
    svc = RosterService(st)
    ym, sat = "2026-08", date(2026, 8, 8)
    svc.set_biopsy_person(ym, sat, "K")
    month = svc.storage.load_month(ym)
    assert month["biopsy_override"][sat.isoformat()] == "K"
    assert month["saturday_biopsy"][sat.isoformat()]["person"] == "K"
    # 手改值班給 C（值班連動本會把切片改成 C）→ 指定仍在
    svc.set_cell("r", ym, sat, "C")
    month = svc.storage.load_month(ym)
    assert month["saturday_biopsy"][sat.isoformat()]["person"] == "K", \
        "手動指定被值班連動洗掉了"
    # 清除指定 → 回到值班連動（C）
    svc.set_biopsy_person(ym, sat, None)
    month = svc.storage.load_month(ym)
    assert "biopsy_override" not in month or not month["biopsy_override"]
    assert month["saturday_biopsy"][sat.isoformat()]["person"] == "C"
