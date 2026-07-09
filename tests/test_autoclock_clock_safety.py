# -*- coding: utf-8 -*-
"""打卡安全回歸測試(W3/W4 2026-07-03)。

W4:讀刷卡表失敗須與「當日無紀錄」區分(read_ok),失敗時不可被當成無紀錄而重複打卡。
W3:點擊執行後須重讀刷卡表確認紀錄寫入才標記完成(_verify_clock_recorded),
    確認不到不標記(交 re-fire 重讀),讀取失敗一律當未確認、不誤判成功。
"""
import os
import sys
from datetime import datetime as _real_datetime, time as dt_time
from unittest.mock import Mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import autoclock as ac  # noqa: E402
from selenium.common.exceptions import (  # noqa: E402
    StaleElementReferenceException, WebDriverException,
)


class _FakeWait:
    def until(self, cond):
        return None


class _FakeDriver:
    def __init__(self, script_result, sys_time_text="115年7月3日"):
        self._script = script_result
        self._sys_time = sys_time_text

    def find_element(self, by, val):
        m = Mock()
        m.text = self._sys_time
        return m

    def execute_script(self, js, *args):
        if callable(self._script):
            return self._script()
        return self._script


def _loc(key):
    return ("id", key)


# ─── W4:get_current_swipe_info 的 read_ok ────────────────────────────────

def test_swipe_read_ok_true_when_table_empty():
    """execute_script 回空 list → read_ok=True(確定當日尚無紀錄,可放心打卡)。"""
    _sd, swipes, _last, read_ok = ac.get_current_swipe_info(
        _FakeDriver([]), _FakeWait(), _loc)
    assert read_ok is True
    assert swipes == []


def test_swipe_read_ok_false_on_exception():
    """execute_script 拋例外 → read_ok=False(讀取失敗,絕不可當無紀錄)。"""
    def boom():
        raise WebDriverException("js failed")
    _sd, _swipes, _last, read_ok = ac.get_current_swipe_info(
        _FakeDriver(boom), _FakeWait(), _loc)
    assert read_ok is False


def test_swipe_read_ok_false_on_non_list():
    """execute_script 回 None(非 list)→ read_ok=False(視為讀取異常)。"""
    _sd, _swipes, _last, read_ok = ac.get_current_swipe_info(
        _FakeDriver(None), _FakeWait(), _loc)
    assert read_ok is False


def test_swipe_read_ok_true_with_rows():
    """execute_script 成功回非空 list → read_ok=True(有讀到表;逐列解析為既有邏輯)。"""
    rows = [["115/07/03", "0731", "上班"]]
    _sd, swipes, _last, read_ok = ac.get_current_swipe_info(
        _FakeDriver(rows), _FakeWait(), _loc)
    assert read_ok is True
    assert isinstance(swipes, list)


def test_swipe_read_anchor_is_system_time_not_empty_table():
    """[2026-07-06] get_current_swipe_info 必須等 system_time(lb_systime),不可等
    swipe_table(Gv_attppre)。空的 ASP.NET GridView(當日尚無紀錄,例:早上第一次上班打卡
    前)不渲染任何 <table> → 等 Gv_attppre 會逾時 → read_ok=False → 第一次打卡永遠卡住
    (死結:無紀錄→空表→不渲染→等不到→不打卡)。上面 _FakeWait.until 是 no-op、測不到真實
    逾時,故以原始碼守門鎖住錨點,避免回歸。"""
    import inspect
    src = inspect.getsource(ac.get_current_swipe_info)
    assert 'presence_of_element_located(get_loc("system_time"))' in src
    assert 'presence_of_element_located(get_loc("swipe_table"))' not in src


# ─── W3:_verify_clock_recorded ───────────────────────────────────────────

def test_verify_true_when_record_appears(monkeypatch):
    """重讀到 read_ok + 區間內符合紀錄 → True(可標記完成)。"""
    monkeypatch.setattr(ac, "get_current_swipe_info",
                        lambda d, w, g: (None, [("0731", "上班")], None, True))
    assert ac._verify_clock_recorded(
        object(), _loc, "上班", dt_time(7, 31), dt_time(8, 0), "u1",
        timeout_sec=0.0, poll_sec=0.0) is True


def test_verify_false_on_timeout_when_no_record(monkeypatch):
    """重讀成功但一直沒有符合紀錄 → 逾時 False(不標記,交 re-fire)。"""
    monkeypatch.setattr(ac, "get_current_swipe_info",
                        lambda d, w, g: (None, [], None, True))
    monkeypatch.setattr(ac.time_module, "sleep", lambda s: None)
    assert ac._verify_clock_recorded(
        object(), _loc, "上班", dt_time(7, 31), dt_time(8, 0), "u1",
        timeout_sec=0.0, poll_sec=0.0) is False


def test_verify_read_failure_never_confirms(monkeypatch):
    """即使重讀回來『有資料』,只要 read_ok=False 就一律當未確認(不誤判成功)。"""
    monkeypatch.setattr(ac, "get_current_swipe_info",
                        lambda d, w, g: (None, [("0731", "上班")], None, False))
    monkeypatch.setattr(ac.time_module, "sleep", lambda s: None)
    assert ac._verify_clock_recorded(
        object(), _loc, "上班", dt_time(7, 31), dt_time(8, 0), "u1",
        timeout_sec=0.0, poll_sec=0.0) is False


# ─── W13:帳號設定驗證(不擋啟動,只警告) ─────────────────────────────────

def test_validate_accounts_ok():
    assert ac._validate_accounts(
        [{"username": "a01", "password": "x"}]) == []


def test_validate_accounts_flags_missing_fields():
    w = ac._validate_accounts([{"username": "", "password": ""}])
    assert any("username" in s for s in w)
    assert any("password" in s for s in w)


def test_validate_accounts_flags_duplicate_username():
    w = ac._validate_accounts([
        {"username": "a01", "password": "x"},
        {"username": "a01", "password": "y"}])
    assert any("重複" in s and "a01" in s for s in w)


def test_validate_accounts_flags_non_dict():
    w = ac._validate_accounts(["not-a-dict", {"username": "a", "password": "p"}])
    assert any("不是物件" in s for s in w)


def test_validate_accounts_total_on_non_list():
    """[codex review] 非 list 純量不可拋例外。"""
    assert ac._validate_accounts(None) == []          # None → 空(無設定)
    assert ac._validate_accounts({}) == []            # 空 dict falsy → 空
    assert any("不是清單" in s for s in ac._validate_accounts(123))  # truthy 純量 → 提示


# ─── W12:打卡任務進行中追蹤(供 self-watchdog 偵測卡住任務) ─────────────

def test_active_clock_task_scope_tracks_and_clears():
    assert ac._active_clock_task_age() == (None, 0.0)
    with ac._active_clock_task_scope("mon_am_in"):
        label, age = ac._active_clock_task_age()
        assert label == "mon_am_in" and age >= 0.0
    assert ac._active_clock_task_age() == (None, 0.0)   # 離開 scope 清空


def test_active_clock_task_scope_clears_on_exception():
    """任務中途拋例外也要清掉標記(否則 watchdog 誤判永遠卡住)。"""
    with pytest.raises(RuntimeError):
        with ac._active_clock_task_scope("x"):
            raise RuntimeError("boom")
    assert ac._active_clock_task_age()[0] is None


# ─── AC-01:窗尾防線(_clock_window_passed) ────────────────────────────────

def _freeze(monkeypatch, when: _real_datetime):
    class _FakeDatetime:
        @staticmethod
        def now():
            return when

        @staticmethod
        def combine(d, t):
            return _real_datetime.combine(d, t)
    monkeypatch.setattr(ac, "datetime", _FakeDatetime)


def test_clock_window_not_passed_within_window(monkeypatch):
    _freeze(monkeypatch, _real_datetime(2026, 7, 8, 7, 45))     # 窗內
    assert ac._clock_window_passed(dt_time(8, 0)) is False


def test_clock_window_passed_after_window(monkeypatch):
    _freeze(monkeypatch, _real_datetime(2026, 7, 8, 8, 3))      # 超窗 3 分鐘=遲到
    assert ac._clock_window_passed(dt_time(8, 0)) is True
    assert ac._clock_window_passed(dt_time(8, 0), grace_sec=60) is True   # 仍超緩衝


def test_clock_window_grace_absorbs_slight_overshoot(monkeypatch):
    _freeze(monkeypatch, _real_datetime(2026, 7, 8, 8, 0, 30))  # 超窗 30 秒
    assert ac._clock_window_passed(dt_time(8, 0)) is True                 # 無緩衝→超
    assert ac._clock_window_passed(dt_time(8, 0), grace_sec=60) is False  # 60s 緩衝內


# ─── AC-02:畸形設定消毒(_sanitize_accounts / load_config) ────────────────

def test_sanitize_accounts_schedule_null_becomes_dict():
    out = ac._sanitize_accounts(
        [{"username": "a", "password": "p", "schedule": None}])
    assert out[0]["schedule"] == {}
    # 修前這行會 AttributeError(None.get)
    assert out[0].get("schedule", {}).get("mon_am_in", False) is False


def test_sanitize_accounts_drops_non_dict_items():
    out = ac._sanitize_accounts(
        ["junk", 123, {"username": "a", "schedule": {"mon_am_in": True}}])
    assert len(out) == 1 and out[0]["username"] == "a"


def test_sanitize_accounts_non_list_is_empty():
    assert ac._sanitize_accounts(None) == []
    assert ac._sanitize_accounts("x") == []


def test_load_config_result_safe_for_schedule_get(monkeypatch):
    """AC-02 端到端:畸形設定經 load_config 後,tick 的 schedule comprehension 不崩。"""
    bad = [{"username": "a", "password": "p", "schedule": None},
           "junk",
           {"username": "b", "password": "p", "schedule": {"mon_am_in": True}}]
    monkeypatch.setattr(ac, "safe_load_json", lambda *a, **k: bad)
    accs = ac.load_config()
    filtered = [a for a in accs if a.get("schedule", {}).get("mon_am_in", False)]
    assert filtered == [{"username": "b", "password": "p",
                         "schedule": {"mon_am_in": True}}]


# ─── AC-09:帳密錯誤不可重試(ClockAuthError) ──────────────────────────────

def test_auth_error_not_a_webdriver_exception():
    """ClockAuthError 不屬 WebDriver/Stale → 不落入 login/perform 的重試 except。"""
    assert not issubclass(ac.ClockAuthError, WebDriverException)
    assert not issubclass(ac.ClockAuthError, StaleElementReferenceException)


def test_perform_clock_action_no_retry_on_auth_error(monkeypatch):
    """帳密錯誤 → login 只呼叫一次(非重試 5x),單次失敗通知。"""
    calls = {"login": 0, "fail": 0}

    def fake_login(*a, **k):
        calls["login"] += 1
        raise ac.ClockAuthError("登入被拒(可能帳號/密碼錯誤)")
    monkeypatch.setattr(ac, "login", fake_login)
    monkeypatch.setattr(ac, "_handle_clock_failure",
                        lambda *a, **k: calls.__setitem__("fail", calls["fail"] + 1))
    ac.perform_clock_action(
        object(), _FakeWait(), {"username": "u", "password": "p"},
        is_in=True, check_start=dt_time(7, 30), check_end=dt_time(8, 0),
        dry_run=False, task_label="mon_am_in")
    assert calls["login"] == 1        # 未重試
    assert calls["fail"] == 1         # 單次通知


# ─── 批次6 打卡週邊 AC-03/04/05/08（原始碼守門，避免回歸） ────────────────────
def _autoclock_src():
    return (os.path.join(os.path.dirname(__file__), "..", "src", "autoclock.py"))


def test_ac03_hard_exit_releases_driver_and_startup_sweep():
    """[AC-03] 硬退前釋放常駐 driver；啟動清掃父已死的孤兒 chromedriver。"""
    with open(_autoclock_src(), encoding="utf-8") as f:
        src = f.read()
    assert "def _cleanup_orphan_chromedrivers_at_startup" in src
    assert "_cleanup_orphan_chromedrivers_at_startup()" in src   # 有被呼叫
    # _autoclock_hard_exit 於 os._exit 前釋放 driver（放獨立緒 target=...+逾時，
    # 不阻塞硬退；codex P1）
    hx = src[src.index("def _autoclock_hard_exit"):]
    hx = hx[:hx.index("def _autoclock_self_watchdog")]
    assert "target=_release_persistent_clock_driver" in hx
    assert (hx.index("target=_release_persistent_clock_driver")
            < hx.index("_os._exit("))


def test_ac04_health_check_probe_outside_pool_lock():
    """[AC-04] window_handles 健康檢查在 pool lock 之外做（CAS 回寫）。"""
    with open(_autoclock_src(), encoding="utf-8") as f:
        src = f.read()
    assert "if pool[\"driver\"] is d:" in src        # CAS
    # window_handles 探測後才進 CAS 鎖（結構性檢查）
    assert "d.window_handles" in src


def test_ac05_configure_returns_to_background():
    """[AC-05] 設定視窗關閉（非儲存並重啟）→ 回背景模式。"""
    with open(_autoclock_src(), encoding="utf-8") as f:
        src = f.read()
    assert "_config_restart_requested" in src
    assert "if not _config_restart_requested:" in src


def test_ac08_health_monitor_passes_restart_callback():
    """[AC-08] autoclock health monitor 傳 restart_callback（不依賴外層 watchdog）。"""
    with open(_autoclock_src(), encoding="utf-8") as f:
        src = f.read()
    assert "restart_callback=lambda: restart_program(" in src
