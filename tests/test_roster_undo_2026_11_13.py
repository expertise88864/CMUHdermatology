# -*- coding: utf-8 -*-
"""[2026-11-13 使用者] 排班還原點(類似 Ctrl+Z):保留最近 5 步;日排班與 R/VS 都有;
手動改單一格不算一步。匿名代號、暫存資料夾。"""
import ast
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster import undo  # noqa: E402
from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.storage import (  # noqa: E402
    FinalizedMonthError, RosterStorage,
)
from test_roster_service import _cover, _result_for  # noqa: E402
from test_roster_service import _svc as _rvs_svc  # noqa: E402

YM = "2026-08"
TODAY = date(2026, 8, 5)
_ROOT = os.path.join(os.path.dirname(__file__), "..", "src", "cmuh_common", "roster")


# ── 純函式 ───────────────────────────────────────────────────────────────
def test_only_the_latest_five_are_kept_and_duplicates_are_skipped():
    month = {"day_slots": {}}
    assert undo.push(month, "day", "自動排班", "t0") is False      # 空班表不存
    for i in range(7):
        month["day_slots"] = {"2026-08-12": {"上午": {"101": [f"C{i}"]}}}
        assert undo.push(month, "day", "自動排班", f"t{i}") is True
    assert undo.push(month, "day", "自動排班", "again") is False   # 與最新一筆相同
    listed = undo.entries(month, "day")
    assert [e["ts"] for e in listed] == ["t6", "t5", "t4", "t3", "t2"]
    with pytest.raises(ValueError):
        undo.find(month, "day", "t0")
    with pytest.raises(ValueError):
        undo.snapshot(month, "nope")


def test_duty_changes_reads_like_a_list():
    before = {"2026-08-12": {"person": "A"}, "2026-08-13": {"person": "B"}}
    after = {"2026-08-12": {"person": "B"}, "2026-08-13": {"person": "B"}}
    assert undo.duty_changes(before, after) == ["8/12(三)：A → B"]


# ── 日排班 ───────────────────────────────────────────────────────────────
def _day_svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({"pgy_members": [{"id": c} for c in ("P1", "P2", "P3")],
                    "r_members": [], "vs_members": [], "room_capacity": 2})
    st.save_clinic_template({"template": {
        str(w): {"上午": [{"room": "101"}, {"room": "102"}],
                 "下午": [{"room": "101"}, {"room": "102"}]} for w in range(5)}})
    st.save_clerk_batches([{"id": "b1", "start_monday": "2026-08-03",
                            "members": ["C1", "C2", "C3"]}])
    st.save_biopsy_grid({})
    st.save_month(YM, {"day_slots": {}})
    return RosterService(st), st


def _solve_and_accept(svc):
    res = svc.run_day_solve(YM, today=TODAY)
    svc.accept_day_solution(YM, res.day_slots, "報告", expect=res, today=TODAY)


def test_day_schedule_can_be_undone_and_redone(tmp_path):
    svc, st = _day_svc(tmp_path)
    _solve_and_accept(svc)
    assert svc.undo_list("day", YM) == []            # 第一次排班前是空的,不留還原點
    first = st.load_month(YM)["day_slots"]
    month = st.load_month(YM)
    month["leaves"] = {"clerk": {"C2": ["2026-08-12", "2026-08-13"]}}
    st.save_month(YM, month)
    _solve_and_accept(svc)
    second = st.load_month(YM)["day_slots"]
    assert second != first
    entries = svc.undo_list("day", YM)
    assert [e["action"] for e in entries] == ["自動排班"]
    changes = svc.undo_preview("day", YM, entries[0]["ts"])
    assert changes and any("C2" in c for c in changes)
    svc.undo_restore("day", YM, entries[0]["ts"])
    restored = st.load_month(YM)
    assert restored["day_slots"] == first
    assert restored["leaves"] == {"clerk": {"C2": ["2026-08-12", "2026-08-13"]}}   # 請假不動
    # 復原本身也留還原點 → 可以再復原回來
    entries = svc.undo_list("day", YM)
    assert entries[0]["action"] == "復原"
    svc.undo_restore("day", YM, entries[0]["ts"])
    assert st.load_month(YM)["day_slots"] == second


def test_clear_unlocked_day_can_be_undone(tmp_path):
    svc, st = _day_svc(tmp_path)
    _solve_and_accept(svc)
    before = st.load_month(YM)["day_slots"]
    svc.clear_unlocked_day(YM)
    assert st.load_month(YM)["day_slots"] == {}
    entry = svc.undo_list("day", YM)[0]
    assert entry["action"] == "清除未鎖定"
    svc.undo_restore("day", YM, entry["ts"])
    assert st.load_month(YM)["day_slots"] == before


def test_manual_cell_edits_do_not_create_restore_points(tmp_path):
    svc, st = _day_svc(tmp_path)
    _solve_and_accept(svc)
    month = st.load_month(YM)
    cell = month["day_slots"]["2026-08-12"]["上午"]
    svc.set_day_session(YM, date(2026, 8, 12), "上午", {"101": ["C1"]}, baseline=cell)
    assert svc.undo_list("day", YM) == []


def test_undo_refuses_finalized_months_and_missing_points(tmp_path):
    svc, st = _day_svc(tmp_path)
    _solve_and_accept(svc)
    svc.clear_unlocked_day(YM)
    ts = svc.undo_list("day", YM)[0]["ts"]
    with pytest.raises(ValueError):
        svc.undo_restore("day", YM, "no-such-point")
    month = st.load_month(YM)
    month["finalized"] = True
    st.save_month(YM, month, force=True)
    with pytest.raises(FinalizedMonthError):
        svc.undo_restore("day", YM, ts)


# ── R/VS 值班 ────────────────────────────────────────────────────────────
def test_duty_schedule_undo_restores_duty_and_ledger(tmp_path):
    svc = _rvs_svc(tmp_path)
    svc.accept_solution("r", YM, _result_for(svc, YM, _cover(svc, YM, "A")))
    duty_a = svc.storage.load_month(YM)["r_duty"]
    ledger_a = dict(svc.storage.load_ledger()["r"])
    assert svc.undo_list("r", YM) == []
    svc.accept_solution("r", YM, _result_for(svc, YM, _cover(svc, YM, "B")))
    assert svc.storage.load_ledger()["r"] != ledger_a
    entry = svc.undo_list("r", YM)[0]
    assert entry["action"] == "自動排班"
    changes = svc.undo_preview("r", YM, entry["ts"])
    assert changes and all("B → A" in c for c in changes)
    svc.undo_restore("r", YM, entry["ts"])
    assert svc.storage.load_month(YM)["r_duty"] == duty_a
    assert dict(svc.storage.load_ledger()["r"]) == ledger_a      # 點數帳本跟著回到當時
    assert svc.undo_list("r", YM)[0]["action"] == "復原"
    assert svc.undo_list("vs", YM) == []                          # 各範圍互不影響


def test_duty_clear_unlocked_can_be_undone(tmp_path):
    svc = _rvs_svc(tmp_path)
    svc.accept_solution("r", YM, _result_for(svc, YM, _cover(svc, YM, "A")))
    duty = svc.storage.load_month(YM)["r_duty"]
    svc.clear_unlocked("r", YM)
    entry = svc.undo_list("r", YM)[0]
    assert entry["action"] == "清除未鎖定"
    svc.undo_restore("r", YM, entry["ts"])
    assert svc.storage.load_month(YM)["r_duty"] == duty


# ── 介面接線 ─────────────────────────────────────────────────────────────
def _src(name):
    return open(os.path.join(_ROOT, "ui", name), encoding="utf-8").read()


def test_both_tabs_have_an_undo_button_without_emoji():
    day, duty = _src("day_tab.py"), _src("duty.py")
    assert 'text="復原排班…", command=self._on_undo' in day
    assert 'text="復原…"' in duty and "self._on_undo(s)" in duty
    for src in (day, duty, _src("undo_dialog.py")):
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Call) and getattr(node.func, "attr", "") == "Button"):
                for kw in node.keywords:
                    if kw.arg == "text" and isinstance(kw.value, ast.Constant):
                        assert all(ord(ch) < 0x1F000 and ch not in "🔒" for ch in kw.value.value)
