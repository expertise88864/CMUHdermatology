# -*- coding: utf-8 -*-
"""打卡查詢錯誤分類 + 世代序號(GPT-5.6 架構審查 P1，2026-07-16)。

1. 錯誤分類:帳密錯(auth)絕不自動重試(防鎖帳號);只有 transient 才重試。
2. 世代序號:180s age 保險允許卡死舊 worker 尚未結束就開新一輪,兩者共用 driver;
   舊 worker 晚完成時不得清新一輪的旗標、不得覆寫新結果。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402


# ── 錯誤分類 helper ──────────────────────────────────────────────────────────
def test_clock_error_carries_kind():
    e = main._clock_error("密碼錯", main.CLOCK_ERR_AUTH)
    assert e["error"] == "密碼錯" and e["error_kind"] == main.CLOCK_ERR_AUTH
    assert main._clock_error("逾時")["error_kind"] == main.CLOCK_ERR_TRANSIENT  # 預設
    assert len(main._clock_error("x" * 100)["error"]) <= 40                    # 截短


def test_error_kinds_distinct():
    kinds = {main.CLOCK_ERR_AUTH, main.CLOCK_ERR_DISABLED, main.CLOCK_ERR_TRANSIENT}
    assert len(kinds) == 3


# ── _get_swipe_status_from_web 各錯誤點的分類(原始碼守門) ──────────────────────
def test_login_alert_is_auth():
    src = inspect.getsource(main._get_swipe_status_from_web)
    # 登入撞 alert / 密碼帳號錯 → AUTH(絕不重試)
    assert "_clock_error(alert_text, CLOCK_ERR_AUTH)" in src
    assert '_clock_error("密碼/帳號錯誤", CLOCK_ERR_AUTH)' in src
    # 登入逾時(非 alert)→ TRANSIENT
    assert '_clock_error("登入逾時/失敗", CLOCK_ERR_TRANSIENT)' in src
    # driver 失敗 / 一般例外 → TRANSIENT
    assert '_clock_error("Driver失敗", CLOCK_ERR_TRANSIENT)' in src
    assert "_clock_error(str(e), CLOCK_ERR_TRANSIENT)" in src


def test_disabled_paths_tagged():
    src = inspect.getsource(main.AutomationApp.update_clock_status_from_web)
    assert '_clock_error("院外模式停用", CLOCK_ERR_DISABLED)' in src


# ── 重試閘門只認 transient(auth/disabled 不重試) ─────────────────────────────
def test_retry_gate_only_transient():
    src = inspect.getsource(main.AutomationApp._maybe_retry_clock_status)
    assert "if kind != CLOCK_ERR_TRANSIENT:" in src, "只有 transient 才自動重試"
    # 不再靠字串比對 skip-list(已淘汰)
    assert "_CLOCK_RETRY_SKIP_ERRORS" not in src


def test_ui_error_branch_passes_kind():
    src = inspect.getsource(main.AutomationApp._update_clock_status_ui)
    assert 'status_data.get("error_kind"' in src, "錯誤分支應把 error_kind 傳給重試閘門"


# ── [金絲雀 讀取面] 打卡 portal 結構改版 ──────────────────────────────────────
def test_portal_changed_kind_is_non_transient():
    # portal_changed 屬非 transient → retry gate 自動不重試(重試無用)
    assert main.CLOCK_ERR_PORTAL_CHANGED != main.CLOCK_ERR_TRANSIENT
    e = main._clock_error("疑似打卡系統改版(表格缺)", main.CLOCK_ERR_PORTAL_CHANGED)
    assert e["error_kind"] == main.CLOCK_ERR_PORTAL_CHANGED


def test_swipe_check_reports_portal_change_on_missing_table():
    src = inspect.getsource(main._get_swipe_status_from_web)
    # JS 回報表格元素是否存在
    assert 'getElementById("Gv_attppre")' in src and '"present"' in src, \
        "應回報打卡表元素是否存在(區分改版 vs 今日無打卡)"
    # 登入成功但表格元素不在 → 回 portal_changed(不重試、明示改版)
    assert "if not table_present:" in src
    assert "CLOCK_ERR_PORTAL_CHANGED" in src
    # 相容舊格式(直接回 list)
    assert "table_present, rows_data = True" in src


def test_portal_changed_not_retried(monkeypatch):
    # 行為:portal_changed 不排自動重試(走非 transient 分支)
    import types
    app = types.SimpleNamespace(
        root=types.SimpleNamespace(after=lambda *a: (_ for _ in ()).throw(
            AssertionError("portal_changed 不該排重試")),
            after_cancel=lambda *a: None),
        _clock_status_retry_count=0, _clock_status_retry_after_id=None,
        _CLOCK_RETRY_DELAY_MS=1, _CLOCK_RETRY_MAX=5,
        _cancel_clock_status_retry=lambda: None)
    main.AutomationApp._maybe_retry_clock_status(
        app, "疑似打卡系統改版", main.CLOCK_ERR_PORTAL_CHANGED)
    assert app._clock_status_retry_count == 0


# ── 世代序號:卡死舊 worker 不覆寫新結果、不清新旗標(主緒原子閘門) ───────────────
def test_generation_advanced_before_querying():
    src = inspect.getsource(main.AutomationApp.update_clock_status_from_web)
    assert "self._clock_status_generation += 1" in src, "開新一輪應遞增世代序號"
    # gen 遞增須在發布 querying 之前(晚到的舊世代結果一定排在新 querying 之後也會被拒)
    assert (src.index("self._clock_status_generation += 1")
            < src.index("status_data='querying'")), "gen 遞增須早於 querying 發布"
    # worker 一律發布帶 generation 的結果(不再自行 check-then-act 跨緒清旗標)
    assert "generation=gen)" in src, "worker 結果須帶 generation 供主緒閘控"
    assert "self._clock_status_worker_running = False" not in src, \
        "旗標清除已移到主緒消費端(worker 不再跨緒清旗標)"


def test_consumer_is_atomic_generation_gate():
    src = inspect.getsource(main.AutomationApp._on_clock_status_message)
    # generation 相符才清旗標+套用;不符直接丟棄(過時世代)
    assert "generation != self._clock_status_generation" in src, "過時世代應拒收"
    assert "self._clock_status_worker_running = False" in src, "gen 相符才由主緒清旗標"
    assert "self._update_clock_status_ui" in src


def test_generation_initialized():
    src = inspect.getsource(main.AutomationApp.__init__)
    assert "self._clock_status_generation = 0" in src


# ── [pass1 P1] auth/disabled 取消先前已排的 transient 重試 ────────────────────
def test_non_transient_cancels_pending_retry():
    src = inspect.getsource(main.AutomationApp._maybe_retry_clock_status)
    guard = src[src.index("if kind != CLOCK_ERR_TRANSIENT:"):]
    guard = guard[:guard.index("return")]
    assert "_cancel_clock_status_retry()" in guard, \
        "auth/disabled 不只不排新重試,還要取消先前 transient 已排的重試(防鎖帳號)"
