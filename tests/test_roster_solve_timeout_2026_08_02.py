# -*- coding: utf-8 -*-
"""[2026-08-02 補審] 求解器「沒算完」被講成「無解」。

CP-SAT 逾時回 `UNKNOWN`、模型異常回 `MODEL_INVALID`,兩者都不是 `INFEASIBLE`,
但 `solve_duty` 原本只看「有沒有拿到 assignments」,一律歸成 infeasible,
接著 `_diagnose` 還會斷言:

    停用「色塊連週」→ 仍無解（與色塊無關）
    若仍無解：多半是 請假/指定 彼此衝突，請檢查預檢警告與當月請假密度。

那是程式沒有驗證過的推斷。實際後果:使用者照著去翻請假與指定,找一個根本不存在的
衝突,而真正該做的只是重按一次;更糟的是連 `need_confirm_color` 那條路都被吞掉 ——
他甚至沒被問到「要不要放寬色塊連週」,而那可能正是可解的那條路。

這正是 solve_day.py 裡那條「★措辭鐵律:只陳述【程式確知】的事★」註解記下的教訓
(當時為了補假警告的措辭,連兩輪被外審抓)。同一個模組群、同一個毛病。

本檔不需要 ortools:直接替換 `_build_and_solve`,測的是「拿到什麼狀態就說什麼話」。
"""
import os
import sys
from datetime import date


sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster import solve_rvs  # noqa: E402
from cmuh_common.roster.model import Member, SolveContext  # noqa: E402
from cmuh_common.roster.report import build_report  # noqa: E402


def _ctx():
    ctx = SolveContext(
        scope="r", year=2026, month=8,
        members=[Member("A", "甲", "R1"), Member("B", "乙", "R2")],
        holidays=set(), leaves={}, must_duty={}, annual_holiday={},
        locks={}, ledger={}, week_colors={}, prev_last_weekend=None)
    return ctx.prepare()


def _solver_always(name, assignments=None):
    """把求解器換成「一律回這個狀態」。"""
    def _fake(_ctx, _scope, _level):
        return name, assignments
    return _fake


# ─── 逾時 ≠ 無解 ───────────────────────────────────────────────────────────
def test_timeout_is_not_reported_as_infeasible(monkeypatch):
    """★核心★ 每一層都逾時 → 狀態不可以是 infeasible。"""
    monkeypatch.setattr(solve_rvs, "_build_and_solve", _solver_always("UNKNOWN"))
    res = solve_rvs.solve_duty(_ctx())

    assert res.status == "timeout", f"實際 status={res.status}"
    text = "\n".join(res.diagnosis)
    assert "無解" not in text.replace("不是】「無解」", "").replace(
        "這【不是】「無解」", ""), f"不可宣稱無解:{text}"
    assert "請勿據此去調整請假或指定" in text, "要明講不要去翻請假(那正是實際的傷害)"


def test_timeout_diagnosis_names_the_levels_that_gave_up(monkeypatch):
    """訊息要說出【哪幾層】沒算完,否則使用者無從判斷是偶發還是每次都這樣。"""
    monkeypatch.setattr(solve_rvs, "_build_and_solve", _solver_always("UNKNOWN"))
    res = solve_rvs.solve_duty(_ctx())

    text = "\n".join(res.diagnosis)
    assert "還沒算完" in text
    assert "L0" in text and "L3" in text, f"至少要點名首層與色塊那層:{text}"


def test_a_timeout_on_the_colour_level_alone_still_counts(monkeypatch):
    """前三層真的無解、但【色塊那一層逾時】→ 仍不可說「與色塊無關」,
    因為那條路根本沒跑完;使用者也還沒被問到要不要放寬色塊。"""
    def _fake(_ctx, _scope, level):
        if level == solve_rvs.L3_NO_COLOR:
            return "UNKNOWN", None
        return "INFEASIBLE", None

    monkeypatch.setattr(solve_rvs, "_build_and_solve", _fake)
    res = solve_rvs.solve_duty(_ctx())

    assert res.status == "timeout"
    assert "與色塊無關" not in "\n".join(res.diagnosis)


def test_report_renders_a_timeout_as_its_diagnosis(monkeypatch):
    """報告的 else 分支寫的是「求解器例外，詳見 automation_ui.log」——
    逾時不是例外,而且那個 log 裡不會有任何 traceback 可看。"""
    monkeypatch.setattr(solve_rvs, "_build_and_solve", _solver_always("UNKNOWN"))
    ctx = _ctx()
    res = solve_rvs.solve_duty(ctx)

    text = build_report(ctx, res, "R 排班")
    assert "求解器例外" not in text
    assert "還沒算完" in text


# ─── 不可矯枉過正:真的無解仍要說無解 ────────────────────────────────────────
def test_a_real_infeasible_is_still_infeasible(monkeypatch):
    monkeypatch.setattr(solve_rvs, "_build_and_solve",
                        _solver_always("INFEASIBLE"))
    res = solve_rvs.solve_duty(_ctx())

    assert res.status == "infeasible"
    assert "自動放寬到底仍無解" in "\n".join(res.diagnosis)
    assert "與色塊無關" in "\n".join(res.diagnosis), "真的測過才可以這樣說"


def test_a_successful_solve_is_unaffected(monkeypatch):
    ctx = _ctx()
    plan = {d: ("A" if d.day % 2 else "B") for d in ctx.days}
    monkeypatch.setattr(solve_rvs, "_build_and_solve",
                        _solver_always("OPTIMAL", plan))
    res = solve_rvs.solve_duty(ctx)

    assert res.status == "ok"
    assert res.assignments == plan
    assert res.last_weekend and res.last_weekend["person"] in ("A", "B")


def test_need_confirm_color_path_is_unaffected(monkeypatch):
    """前三層無解、色塊層【真的可解】→ 仍要問使用者要不要放寬(不可變成 timeout)。"""
    ctx = _ctx()
    plan = {d: "A" for d in ctx.days}

    def _fake(_c, _s, level):
        if level == solve_rvs.L3_NO_COLOR:
            return "OPTIMAL", plan
        return "INFEASIBLE", None

    monkeypatch.setattr(solve_rvs, "_build_and_solve", _fake)
    res = solve_rvs.solve_duty(ctx)

    assert res.status == "need_confirm_color"
    assert date(2026, 8, 1) in ctx.days          # 情境自我說明:確實有排到日期


# ─── [第1輪外審] 拿到解之後,仍不可暗示「更嚴格的層級無解」────────────────────
def test_a_relaxed_solution_says_the_strict_level_merely_gave_up(monkeypatch):
    """★L0 逾時、L1 有解 → 報告會寫「有規則被放寬」★

    那句話讀起來像「嚴格規則滿足不了」,但程式並沒有證明 L0 無解 —— 它只是沒算完。
    使用者會以為這個月真的緊到排不出來,而其實重按一次就可能拿到 L0 的結果。
    """
    ctx = _ctx()
    plan = {d: "A" for d in ctx.days}

    def _fake(_c, _s, level):
        if level == solve_rvs.L0_FULL:
            return "UNKNOWN", None
        return "OPTIMAL", plan

    monkeypatch.setattr(solve_rvs, "_build_and_solve", _fake)
    res = solve_rvs.solve_duty(ctx)

    assert res.status == "ok"
    text = build_report(ctx, res, "R 排班")
    assert "未得出結論" in text, f"沒有說出 L0 只是沒算完:{text}"
    assert "L0" in text


def test_need_confirm_color_does_not_claim_infeasible_after_a_timeout(monkeypatch):
    """自動層級全部逾時、色塊層有解 → 不可以說「在不動色塊的前提下無解」。"""
    ctx = _ctx()
    plan = {d: "A" for d in ctx.days}

    def _fake(_c, _s, level):
        if level == solve_rvs.L3_NO_COLOR:
            return "OPTIMAL", plan
        return "UNKNOWN", None

    monkeypatch.setattr(solve_rvs, "_build_and_solve", _fake)
    res = solve_rvs.solve_duty(ctx)

    assert res.status == "need_confirm_color"
    text = "\n".join(res.diagnosis)
    assert "在不動色塊連週規則的前提下無解" not in text, text
    assert "未得出結論" in text


# ─── [第2輪外審] 模型異常不是逾時 ───────────────────────────────────────────
def test_model_invalid_is_not_reported_as_a_timeout(monkeypatch):
    """★我在修「宣稱不確知的事」時,自己又宣稱了一次★

    MODEL_INVALID 被歸進 timeout,訊息還說「多半是機器當下負載過重、請稍後重試」。
    模型異常重試一百次也一樣 —— 那是程式的臭蟲,要叫使用者回報、看 log。
    """
    monkeypatch.setattr(solve_rvs, "_build_and_solve",
                        _solver_always("MODEL_INVALID"))
    res = solve_rvs.solve_duty(_ctx())

    assert res.status == "error", f"實際 status={res.status}"
    text = "\n".join(res.diagnosis)
    assert "負載" not in text and "稍後" not in text, f"不可叫他重試:{text}"
    assert "回報" in text and "模型異常" in text


def test_model_invalid_shows_its_diagnosis_in_the_report(monkeypatch):
    """報告的 error 分支原本一律寫「求解器例外,詳見 automation_ui.log」——
    模型異常沒有 traceback,那句話會把使用者送去看一個沒有答案的 log。"""
    monkeypatch.setattr(solve_rvs, "_build_and_solve",
                        _solver_always("MODEL_INVALID"))
    ctx = _ctx()
    res = solve_rvs.solve_duty(ctx)

    text = build_report(ctx, res, "R 排班")
    assert "模型異常" in text


def test_timeout_and_model_invalid_are_reported_separately(monkeypatch):
    """一層逾時、一層模型異常 → 模型異常優先(重試沒用),但兩者都要被點名。"""
    def _fake(_c, _s, level):
        if level == solve_rvs.L0_FULL:
            return "UNKNOWN", None
        return "MODEL_INVALID", None

    monkeypatch.setattr(solve_rvs, "_build_and_solve", _fake)
    res = solve_rvs.solve_duty(_ctx())

    assert res.status == "error"
    assert "MODEL_INVALID" in "\n".join(res.diagnosis)
