# -*- coding: utf-8 -*-
"""[批次RS-32b / 2026-08-30 使用者] 自動排班只排【明天起】(R/VS 值班)。

實作:`build_context(today=...)` 把 d ≤ today 的實況(含未鎖定的)放進
`SolveContext.past_duty`,`_build_and_solve` 把那些日子釘成事實 ——
有人就是那個人、空的就是空的(不套「每日恰一人」,過去沒排到不能回頭補)。
點數/班數/連值/週末上限/色塊等規則因此看得到過去(參考今天以前的資料);
指定類與請假/固定週幾等硬規則跳過過去(事實可能違反規則,不能改寫歷史)。
★不寫 locked 旗標★:UI 仍可手動編輯今天以前。真時鐘只在 UI 按鈕進入。
"""
import ast
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.solve_rvs import solve_duty  # noqa: E402
from cmuh_common.roster.storage import RosterStorage  # noqa: E402

YM = "2026-08"                     # 2026/8/1 = 週六
WED = date(2026, 8, 26)            # 注入的「今天」
THU = date(2026, 8, 27)


def _svc(tmp_path, r_members=("A", "B")):
    st = RosterStorage(str(tmp_path))
    st.save_config({
        "r_members": [{"id": mid, "name": f"名{mid}"} for mid in r_members],
        "vs_members": [{"id": "D", "name": "D醫師"}],
        "points": {"weekday": 1, "weekend": 2, "national_holiday": 1},
        "duty_range_soft": [9, 11],
    })
    return RosterService(st)


def _set_duty(svc, iso_to_person, *, locked=frozenset(), source="manual"):
    def _mut(month):
        duty = month.setdefault("r_duty", {})
        for iso, p in iso_to_person.items():
            duty[iso] = {"person": p, "locked": iso in locked,
                         "source": source}
    svc.update_month(YM, _mut)


class TestThePastIsFactNotASchedulingTarget:
    def test_past_assignments_survive_and_the_future_gets_scheduled(
            self, tmp_path):
        """過去的實況(未鎖定!)原樣保留;明天起照排;今天以前空著的維持空。"""
        svc = _svc(tmp_path)
        _set_duty(svc, {"2026-08-03": "A", "2026-08-04": "B"})
        res = svc.run_solve("r", YM, today=WED)
        assert res.status == "ok", (res.status, res.diagnosis)
        assert res.assignments[date(2026, 8, 3)] == "A"
        assert res.assignments[date(2026, 8, 4)] == "B"
        # 8/05~8/26 沒排過 → ★維持空★(不在 assignments 裡)
        assert date(2026, 8, 10) not in res.assignments
        assert WED not in res.assignments
        # 明天起每天都有人
        assert res.assignments.get(THU) in ("A", "B")
        assert res.assignments.get(date(2026, 8, 31)) in ("A", "B")

    @staticmethod
    def _future_majority(tmp_path, past_holder):
        svc = _svc(tmp_path / past_holder)
        _set_duty(svc, {f"2026-08-{d:02d}": past_holder
                        for d in range(3, 26)})
        res = svc.run_solve("r", YM, today=WED)
        assert res.status == "ok", (res.status, res.diagnosis)
        future = [p for d, p in res.assignments.items() if d > WED]
        assert future
        return max(set(future), key=future.count)

    def test_the_future_is_steered_by_past_points(self, tmp_path):
        """★參考今天以前的資料★:過去誰值得多,未來的多數就給另一個人
        (點數平衡看得到過去)。

        ★用鏡像成對斷言隔離規則★:「全部給 B」寫不得 —— 連續值班軟限制
        會刻意穿插一兩天(我第一版就是這樣紅的,那不是缺陷是設計)。
        兩份 fixture 只差「過去是誰」,未來多數方必須跟著翻轉 ——
        任何與過去無關的因素(抖動/軟限制)在兩邊相同,分勝負的只有過去。
        """
        assert self._future_majority(tmp_path, "A") == "B"
        assert self._future_majority(tmp_path, "B") == "A"

    def test_a_past_leave_conflict_does_not_block_the_future(self, tmp_path):
        """過去的實況可能與請假衝突(事後補登)—— ★不可以讓它把整個未來
        擋掉★(過去改不了,拿約束去改寫歷史只會無解)。"""
        svc = _svc(tmp_path)
        _set_duty(svc, {"2026-08-03": "A"})

        def _mut(month):
            month.setdefault("leaves", {}).setdefault("r", {})["A"] = [
                "2026-08-03"]
        svc.update_month(YM, _mut)
        res = svc.run_solve("r", YM, today=WED)
        assert res.status == "ok", (res.status, res.diagnosis)
        assert res.assignments[date(2026, 8, 3)] == "A"   # 事實保留

    def test_a_departed_member_in_the_past_is_preserved_verbatim(
            self, tmp_path):
        """人選已不在名單(離職/改代號)的過去日 → ★歷史不可因為按了一次
        自動排班而消失★。"""
        svc = _svc(tmp_path)
        _set_duty(svc, {"2026-08-03": "Z"})               # Z 不在名單
        res = svc.run_solve("r", YM, today=WED)
        assert res.status == "ok", (res.status, res.diagnosis)
        assert res.assignments[date(2026, 8, 3)] == "Z"

    def test_a_straddling_weekend_continues_with_the_actual_person(
            self, tmp_path):
        """★連休段跨過今天★:週六(過去)實際是 A → 週日(未來)接續給 A
        (「指定週六自動帶週日」的接續,過去的事實就是指定)。
        2026-08-29(六)/30(日);today=8/29。"""
        svc = _svc(tmp_path)
        _set_duty(svc, {"2026-08-29": "A"})
        res = svc.run_solve("r", YM, today=date(2026, 8, 29))
        assert res.status == "ok", (res.status, res.diagnosis)
        assert res.assignments[date(2026, 8, 30)] == "A", res.assignments

    def test_an_empty_past_weekend_half_does_not_sink_the_month(
            self, tmp_path):
        """★跨今連休段的過去那半是空的★:等式會把「空」傳染成「週日無人」
        而與每日恰一人矛盾 → 整月無解。正解:未來那半獨立指派。"""
        svc = _svc(tmp_path)                              # 8/29 沒排(空)
        res = svc.run_solve("r", YM, today=date(2026, 8, 29))
        assert res.status == "ok", (res.status, res.diagnosis)
        assert date(2026, 8, 29) not in res.assignments   # 過去維持空
        assert res.assignments.get(date(2026, 8, 30)) in ("A", "B")

    def test_a_fully_blocked_past_day_does_not_sink_the_future(
            self, tmp_path):
        """★外審 R1-1★:歷史上某天【全員】請假(事後補登)—— 核心可行性
        預檢原本檢查整月,那一天會變成 error 把明天以後整個擋掉。
        ★反例只靠「那天在過去」分勝負★:同一份請假放在未來,照樣要擋。"""
        svc = _svc(tmp_path)

        def _mut(month):
            month.setdefault("leaves", {})["r"] = {
                "A": ["2026-08-03"], "B": ["2026-08-03"]}
        svc.update_month(YM, _mut)
        res = svc.run_solve("r", YM, today=WED)
        assert res.status == "ok", (res.status, res.diagnosis)
        # 對照組:同樣全員請假的日子在【未來】→ 必須照樣擋(不可矯枉過正)
        svc2 = _svc(tmp_path / "future")

        def _mut2(month):
            month.setdefault("leaves", {})["r"] = {
                "A": ["2026-08-28"], "B": ["2026-08-28"]}
        svc2.update_month(YM, _mut2)
        res2 = svc2.run_solve("r", YM, today=WED)
        # ★斷言收斂到預檢自己的輸出★:只斷 status != ok 的話,拆掉預檢後
        # CP-SAT 自己也會 infeasible(縱深防禦),突變量不到。預檢的價值是
        # 【人話說出是哪一天、為什麼】,所以量它的訊息。
        assert res2.status == "precheck_failed", res2.status
        assert any("所有人皆請假" in str(c) for c in res2.prechecks), (
            res2.prechecks)

    @staticmethod
    def _ledger_majority(tmp_path, debtor):
        """過去整段空白 + 帳本欠帳 → 未來多數要給欠的人。"""
        svc = _svc(tmp_path / f"led-{debtor}")
        other = "B" if debtor == "A" else "A"
        svc.storage.save_ledger({"r": {other: 6.0, debtor: -6.0}, "vs": {}})
        res = svc.run_solve("r", YM, today=WED)
        assert res.status == "ok", (res.status, res.diagnosis)
        future = [p for d, p in res.assignments.items() if d > WED]
        return max(set(future), key=future.count)

    def test_point_fairness_still_discriminates_with_an_empty_past(
            self, tmp_path):
        """★外審 R1-2★:過去整段空白(被釘成全 0)而總點數仍算整月的話,
        公平目標被灌高到人人達不到 → |points-target| 總和變常數 →
        ★最高優先的點數公平項對未來完全失去鑑別力★,實際由較低優先的
        規則(班數平衡)決定 —— 帳本欠帳被靜默忽略。
        ★鏡像成對★:只差「誰欠帳」,未來多數方必須翻轉。"""
        assert self._ledger_majority(tmp_path, "A") == "A"
        assert self._ledger_majority(tmp_path, "B") == "B"

    def test_the_effective_total_counts_future_plus_active_past(
            self, tmp_path):
        """★有效總點數的定義本身★:未來全部 + 過去【排給現役成員】的;
        過去空白與離職者的日子不算(誰都拿不到的點數不進公平目標)。"""
        svc = _svc(tmp_path)
        _set_duty(svc, {"2026-08-03": "A",     # 現役 → 算
                        "2026-08-04": "Z"})    # 離職 → 不算
        from cmuh_common.roster.model import day_point
        ctx = svc.build_context("r", YM, today=WED)
        expect = (sum(day_point(d, ctx.holidays, ctx.params)
                      for d in ctx.days if d > WED)
                  + day_point(date(2026, 8, 3), ctx.holidays, ctx.params))
        assert ctx.total_points() == expect
        # 對照組:today=None → 整月(逐位元不變)
        ctx2 = svc.build_context("r", YM)
        assert ctx2.total_points() == sum(
            day_point(d, ctx2.holidays, ctx2.params) for d in ctx2.days)

    def test_no_today_means_no_change(self, tmp_path):
        """★today=None(預設)= 行為不變★:整月從頭排(含 8/3)。"""
        svc = _svc(tmp_path)
        _set_duty(svc, {"2026-08-03": "A"})
        ctx = svc.build_context("r", YM)
        assert ctx.past_cutoff is None and ctx.past_duty == {}
        res = solve_duty(ctx)
        assert res.status == "ok"
        assert date(2026, 8, 10) in res.assignments        # 全月都排


class TestTheApplyGateAndMetadata:
    def test_a_cross_day_apply_is_rejected(self, tmp_path):
        svc = _svc(tmp_path)
        res = svc.run_solve("r", YM, today=WED)
        assert res.status == "ok"
        with pytest.raises(ValueError):
            svc.accept_solution("r", YM, res, today=THU)

    def test_same_day_apply_keeps_past_metadata_and_no_lock_is_written(
            self, tmp_path):
        """套用後:過去格連 source 都原樣(手動排的不可被改標成 auto)、
        ★不寫 locked★(UI 可編輯性不變)、未來格照常落地。"""
        svc = _svc(tmp_path)
        _set_duty(svc, {"2026-08-03": "A"})
        res = svc.run_solve("r", YM, today=WED)
        svc.accept_solution("r", YM, res, today=WED)
        duty = svc.storage.load_month(YM)["r_duty"]
        past = duty["2026-08-03"]
        assert past == {"person": "A", "locked": False, "source": "manual"}, (
            past)
        assert "2026-08-10" not in duty                    # 過去空著的仍空
        assert duty[THU.isoformat()]["person"] in ("A", "B")

    def test_the_cutoff_enters_the_fingerprint(self, tmp_path):
        svc = _svc(tmp_path)
        from cmuh_common.roster.solve_rvs import rvs_input_fingerprint
        f1 = rvs_input_fingerprint(svc.build_context("r", YM, today=WED))
        f2 = rvs_input_fingerprint(svc.build_context("r", YM, today=THU))
        assert f1 != f2


def test_the_ui_passes_today_at_both_call_sites():
    """★沒有呼叫端的功能等於不存在★:R/VS 分頁的求解與套用都要帶 today。"""
    p = os.path.join(os.path.dirname(__file__), "..", "src", "cmuh_common",
                     "roster", "ui", "duty.py")
    with open(p, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for wanted in ("run_solve", "accept_solution"):
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == wanted]
        assert calls, f"找不到 {wanted} 的呼叫(測試失效)"
        for c in calls:
            assert any(k.arg == "today" for k in c.keywords), (
                f"★UI 的 {wanted} 沒帶 today★")
