# -*- coding: utf-8 -*-
"""[2026-08-03 實機] BDE 初始化失敗被誤報成「帳號密碼可能被停用」。

使用者收到「連續失敗 22 次／請確認帳號密碼是否被院方改過停用」，手動開程式才
看到真正的畫面是 `An error occurred while attempting to initialize the Borland
Database Engine (error $250E)` —— HIS 根本沒起來，帳密完全沒問題。
外觀之所以一樣，是因為那個 modal 把登入視窗擋成 disabled（實機視窗清單：
`TFrmLogin(vis=1,en=0)` ＋ 一個 enabled 的對話框）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402

_REAL = ("An error occurred while attempting to initialize the "
         "Borland Database Engine (error $250E)")


def test_detects_the_real_dialog_text_and_returns_the_code():
    assert cq.bde_error_code_in([_REAL]) == "$250E"


def test_scans_every_text_not_just_the_first():
    """MessageBox 的標題是「住院醫囑系統」，內文在 Static 子控制項 → 要全掃。"""
    assert cq.bde_error_code_in(["住院醫囑系統", _REAL, "確定"]) == "$250E"


def test_other_bde_codes_also_recognised():
    """$250E 只是這次遇到的碼；別的初始化碼一樣是「HIS 起不來」。"""
    assert cq.bde_error_code_in([_REAL.replace("$250E", "$2501")]) == "$2501"


def test_marker_without_code_still_reports():
    """有 BDE 字樣但抓不到碼 → 仍要判定為這一類（不可回 None 而退回帳密訊息）。"""
    assert cq.bde_error_code_in(
        ["Cannot initialize the Borland Database Engine"]) == "(未知碼)"


def test_unrelated_dialogs_do_not_match():
    """一般訊息通知不得被誤判 —— 否則真的帳密問題會被講成 BDE。"""
    for txt in ("登入失敗，密碼錯誤", "住院醫囑系統", "", "會診通知單回覆"):
        assert cq.bde_error_code_in([txt]) is None
    assert cq.bde_error_code_in([]) is None


def test_no_window_text_is_carried_into_the_message():
    """★隱私邊界★ 只回錯誤碼，不得把視窗文字帶出去（診斷訊息會進 log 與告警信）。"""
    secret = ("Borland Database Engine (error $250E) 病人 王小明 "
              "病歷號 1234567")
    code = cq.bde_error_code_in([secret])
    assert code == "$250E"
    assert "王小明" not in code and "1234567" not in code


def test_startup_blocked_is_a_separate_non_retryable_error():
    """要與 LoginNotCompleted 分開：處置不同，而且都不可重試。"""
    assert issubclass(cq.HISStartupBlocked, RuntimeError)
    assert not issubclass(cq.HISStartupBlocked, cq.LoginNotCompleted)


def test_retry_loop_treats_startup_blocked_as_fatal():
    """源碼守衛：fatal 判定要含 HISStartupBlocked，否則會白白重試 3 次。"""
    import inspect
    src = inspect.getsource(cq)
    i = src.index("fatal = isinstance(")
    window = src[i:i + 240]
    assert "HISStartupBlocked" in window, "fatal 判定漏了 HISStartupBlocked"


def test_bde_is_checked_before_the_password_message():
    """★真正的病灶★ BDE 檢查必須排在「登入視窗還在 → 說帳密」之前，
    否則永遠先命中帳密訊息，使用者又被導去錯的方向。"""
    import inspect
    src = inspect.getsource(cq._wait_main_window_after_login) \
        if hasattr(cq, "_wait_main_window_after_login") else inspect.getsource(cq)
    i_bde = src.index("detect_bde_startup_error(our_pids)")
    i_login = src.index("raise LoginNotCompleted(")
    assert i_bde < i_login, "BDE 判定要在帳密訊息之前"


def test_detection_scans_hidden_windows_too():
    """★[codex P1] SW_HIDE 模式的 stealth thread 會把該行程所有可見視窗藏起來★
    只掃可見視窗 → 正好在實際會用到的路徑上永遠偵測不到，又退回誤導訊息。
    被隱藏不等於不存在：它照樣是擋住登入視窗的那個 modal。"""
    import inspect
    src = inspect.getsource(cq.detect_bde_startup_error)
    assert "visible_only=False" in src, "必須連不可見視窗一起掃"
    assert "visible_only=True" not in src


def test_message_says_not_a_password_problem():
    """告警信/log 的措辭要明講「不是帳密問題」——這正是這次被誤導的點。"""
    import inspect
    src = inspect.getsource(cq._bde_blocked)
    assert "不是帳號密碼問題" in src
    assert "重開機" in src
    assert "BDE 起不來,再登一百次也一樣" in src


def test_detected_inside_the_wait_loop_not_only_after_timeout():
    """★[codex P2] 對話框啟動當下就出現,等滿 120 秒才認得等於每輪白等兩分鐘★
    偵測要在等待迴圈【裡面】,而且節流(每 5 秒)以免每 0.4 秒列舉全部視窗。"""
    import inspect
    src = inspect.getsource(cq._wait_main_window_after_login)
    i_loop = src.index("while time.time() < deadline:")
    i_detect = src.index("detect_bde_startup_error(our_pids)")
    assert i_detect > i_loop, "偵測必須在等待迴圈內,不能只在逾時之後"
    assert "next_bde_check" in src, "要節流,不可每輪都列舉全部視窗"


def test_both_call_sites_share_one_message_builder():
    """兩處都用同一個 _bde_blocked → 措辭不會各改各的而漸行漸遠。"""
    import inspect
    src = inspect.getsource(cq._wait_main_window_after_login)
    assert src.count("_bde_blocked(") == 2
    assert isinstance(cq._bde_blocked("$250E", set(), 0, None, set()),
                      cq.HISStartupBlocked)
