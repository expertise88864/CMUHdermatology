# -*- coding: utf-8 -*-
"""沒設定打卡的電腦不該被打卡程式打擾（2026-08-06 使用者第二次回報）。

【使用者回報】「為什麼又一直出現提示『自動打卡 更新後重啟失敗』？問題是我這台
電腦沒有執行打卡程式。」

【2026-08-02 的第一次修法為何不夠】當時改成「子行程 exit=0 且 stderr 沒有 Python
例外痕跡 → 視為 orderly，不示警」。但那擋不住所有情況：
  * 內部重啟一律附帶 `--configure-if-empty` → 沒帳號的機器會【開出打卡設定視窗】，
    使用者把它關掉後子行程的結束碼未必是 0；
  * 子行程只要在 stderr 留下任何一條 crash marker（Tk 初始化、匯入問題…）就被判 crashed。
兩者都會讓那句「新版本無法啟動」再度冒出來。

【本次修法：改用根本判準】這台機器有沒有打卡帳號。
  * 沒帳號 → 本機根本不做打卡 → 「打卡中斷」對他毫無意義 → 只記 log，不打擾。
  * 沒帳號 → 內部重啟也不再附 `--configure-if-empty`（不彈設定視窗）。
  * 有帳號 → 行為完全不變（真的重啟失敗仍必須大聲告知）。
"""
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import autoclock as ac  # noqa: E402


# ── 判準本身 ────────────────────────────────────────────────────────────────
def test_empty_config_means_no_accounts(monkeypatch):
    monkeypatch.setattr(ac, "safe_load_json", lambda *a, **k: [])
    assert ac._machine_has_clock_accounts() is False


@pytest.mark.parametrize("bad", [None, "not-a-list", 42])
def test_malformed_config_is_conservative(monkeypatch, bad):
    """壞檔/型別不對 → 保守當成「有設定」，才不會吞掉真正的重啟失敗告警。"""
    monkeypatch.setattr(ac, "safe_load_json", lambda *a, _b=bad, **k: _b)
    assert ac._machine_has_clock_accounts() is True


def test_blank_username_rows_do_not_count(monkeypatch):
    monkeypatch.setattr(ac, "safe_load_json",
                        lambda *a, **k: [{"username": "   "}, {"password": "x"}])
    assert ac._machine_has_clock_accounts() is False


def test_a_real_account_counts(monkeypatch):
    monkeypatch.setattr(ac, "safe_load_json",
                        lambda *a, **k: [{"username": "D12345", "password": "p"}])
    assert ac._machine_has_clock_accounts() is True


def test_load_failure_is_conservative(monkeypatch):
    def _boom(*a, **k):
        raise OSError("locked by antivirus")
    monkeypatch.setattr(ac, "safe_load_json", _boom)
    assert ac._machine_has_clock_accounts() is True, \
        "讀不到時要保守當成有設定(否則會吞掉真正的重啟失敗告警)"


# ── ★核心★ 沒帳號的機器不可以跳「更新後重啟失敗」 ──────────────────────────
def test_no_notification_on_a_machine_without_clock_accounts(monkeypatch):
    shown = []
    monkeypatch.setattr(ac, "safe_load_json", lambda *a, **k: [])
    monkeypatch.setattr(ac, "notify_clock_failure",
                        lambda *a, **k: shown.append(a))
    monkeypatch.setattr(ac, "WINOTIFY_AVAILABLE", True)
    ac._notify_restart_failed()
    assert shown == [], "★沒有打卡帳號的電腦仍跳了『更新後重啟失敗』★"


def test_notification_still_fires_when_accounts_exist(monkeypatch):
    """反方向：真的在打卡的機器，重啟失敗仍必須大聲告知（不可被這次修法吞掉）。"""
    shown = []
    monkeypatch.setattr(ac, "safe_load_json",
                        lambda *a, **k: [{"username": "D12345"}])
    monkeypatch.setattr(ac, "notify_clock_failure",
                        lambda *a, **k: shown.append(a))
    monkeypatch.setattr(ac, "WINOTIFY_AVAILABLE", True)
    ac._notify_restart_failed()
    assert shown, "★有帳號卻不告警★ 打卡可能真的中斷了"


# ── 沒帳號的機器也不該被彈出設定視窗 ────────────────────────────────────────
def test_restart_does_not_force_config_window_without_accounts():
    """`--configure-if-empty` 只在【本機有帳號】時才附加。

    否則自動更新重啟會在一台根本不做打卡的電腦上彈出打卡設定視窗。
    """
    src = inspect.getsource(ac.restart_program)
    i = src.index("CONFIGURE_IF_EMPTY_FLAG not in extra")
    seg = src[i:i + 200]
    assert "_machine_has_clock_accounts()" in seg, (
        "★仍無條件附加 --configure-if-empty★ 沒設定打卡的電腦會被彈出設定視窗")


def test_notify_helper_checks_accounts_before_anything_else():
    """判斷要排在最前面（不可先跳 toast 再檢查）。"""
    src = inspect.getsource(ac._notify_restart_failed)
    i_check = src.index("_machine_has_clock_accounts()")
    i_notify = src.index("notify_clock_failure(")
    assert i_check < i_notify, "要先確認本機有沒有打卡帳號，才決定要不要打擾使用者"
