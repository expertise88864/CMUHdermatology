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
