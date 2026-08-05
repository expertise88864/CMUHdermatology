# -*- coding: utf-8 -*-
"""[2026-07-29 實機故障] 會診整天失敗,最後錯誤只有「等不到主畫面」。

實機 log(另一台電腦,1,568 行)顯示:「已關閉訊息通知主畫面」**每 0.6 秒重複一次、
整整刷滿 120 秒**,然後回報等不到主畫面。原本的迴圈條件是:

    if mains and not notice:      # 只要還找得到通知視窗就【拒絕】接受主畫面

點了「確認」之後那個視窗仍然找得到 → 永遠卡在「先把通知關掉」這一步。

★不可把 visible_only 一律改成 True★:`_stealth()` 會把視窗 SW_HIDE
(2026-05-15 的既有註解寫得很清楚),可見性在那條路徑上不是有效訊號。
改用 Win32 的正規訊號:主視窗被 modal 擋住時會被 **disable**,
`IsWindowEnabled` 與可見性無關,兩條路徑都成立。
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


def _src() -> str:
    return open(os.path.join(os.path.dirname(__file__), '..', 'src',
                             'consult_query.py'), encoding='utf-8').read()


def _code_only(src: str) -> str:
    src = re.sub(r'("""|\'\'\')(?:.|\n)*?\1', "", src)
    return "\n".join(line.split("#")[0] for line in src.splitlines())


def _helper() -> str:
    s = _code_only(_src())
    i = s.index("def _wait_main_window_after_login(")
    j = s.index("def _describe_windows_for_diag(", i)
    return s[i:j]


def test_uses_is_window_enabled_not_notice_absence():
    """★核心★ 判斷「主畫面可以操作了沒」要用 IsWindowEnabled,
    不是「找不到通知視窗」。"""
    body = _helper()
    assert "win32gui.IsWindowEnabled(mains[0])" in body
    # ★用字界比對★[2026-08-05]：原本寫 `"not notice" not in body`，
    #   而後來新增的 `if not notice_actionable:`（通知視窗自己被別的 modal
    #   擋住時不要按它）剛好含有這個子字串 → 誤紅。要擋的是【變數 notice】
    #   本身被當成放行條件，不是任何以 notice 開頭的名字。
    assert not re.search(r"\bnot notice\b(?!_)", body), (
        "★不可再用『沒有通知視窗』當放行條件★")


def test_visible_only_is_a_parameter_not_hardcoded():
    """兩條路徑的可見性語意不同(隱藏桌面 vs SW_HIDE),不可寫死。"""
    body = _helper()
    assert "visible_only=visible_only" in body
    code = _code_only(_src())
    assert "_wait_main_window_after_login(our_pids, visible_only=True)" in code
    assert "_wait_main_window_after_login(our_pids, visible_only=False)" in code


def test_click_log_is_throttled_and_carries_identity():
    """★1,568 行 log 幾乎全是同一句★ 而且沒有 hwnd —— 完全分不出
    「同一個視窗一直關不掉」與「通知有一整排、關掉一個又來一個」,
    這兩者的處置完全不同。"""
    body = _helper()
    assert "clicks <= 3 or clicks % 20 == 0" in body, "要節流"
    assert "hwnd=%s" in body and "第 %d 次" in body, "要帶 hwnd 與次數"
    assert "distinct_notices" in body, "要能分辨是不是同一個視窗"


def test_timeout_message_says_what_it_actually_saw():
    """★訊息只能陳述程式確知的事★ 「等不到主畫面」什麼都沒說。
    逾時要吐出當下看到的視窗(class/可見/enabled)與按了幾次確認。"""
    code = _code_only(_src())
    i = code.index("def _describe_windows_for_diag(")
    seg = code[i:i + 1200]
    assert "GetClassName" in seg and "IsWindowVisible" in seg \
        and "IsWindowEnabled" in seg
    body = _helper()
    assert "_describe_windows_for_diag(" in body


def test_diagnostic_contains_no_window_text():
    """★隱私★ 診斷只能有 class 與旗標 —— 視窗標題/內文可能含病人資料。"""
    code = _code_only(_src())
    i = code.index("def _describe_windows_for_diag(")
    seg = code[i:i + 1200]
    assert "GetWindowText" not in seg, "★不可把視窗文字寫進診斷★"


def test_both_loops_were_replaced_by_the_helper():
    """兩處幾乎相同的迴圈已收攏 —— 否則修一邊會漏另一邊(這次就是兩邊都有)。

    註:檔內還有其他 120 秒的等待迴圈(等登入視窗、等會診視窗),那些不在本次
    範圍;所以這裡釘的是【那個錯誤的放行條件】本身,不是「所有 120 秒迴圈」。
    """
    raw = _src()
    assert "if mains and not notice:" not in raw, \
        "★錯誤的放行條件不可再存在★"
    # def 那一行也含 "(our_pids",要用賦值形式才數得到真正的呼叫點
    # [2026-08-03 常駐] 隱藏桌面路徑不再取用回傳值(主畫面 hwnd 每輪重找),
    # 但仍必須走同一個 helper;def 那一行也含 "(our_pids",要扣掉。
    calls = raw.count("_wait_main_window_after_login(our_pids") - raw.count(
        "def _wait_main_window_after_login(")
    assert calls == 2, "兩個呼叫點(常駐冷啟動 + SW_HIDE)都要走 helper"
    assert raw.count("def _wait_main_window_after_login(") == 1


def test_a_disabled_notice_is_not_clicked():
    """★[2026-08-05 實機] 按一個 disabled 的視窗是純粹的浪費★

    那天 `TFMShowMessage` 自己就被 `TFMTimeOut_1` 擋住（en=0），程式對它按了
    6 次「確認」都沒有反應，整整 120 秒的登入預算就這樣燒光，最後回報
    「登入沒有完成」並進入 15 分鐘冷卻 —— 而真正該按的那個視窗從頭到尾沒被碰。

    ★判準要看「結果有沒有被用到」，不是「有沒有呼叫」★
    突變驗證抓到的洞：把 `if not notice_actionable: continue` 換成
    `notice_actionable = True`，`IsWindowEnabled(notice[0])` 這個呼叫還在，
    純粹比對字串的版本照樣全綠 —— 而程式又會去按那個 disabled 的視窗。
    """
    import ast
    import inspect
    import textwrap

    import consult_query as cq

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(cq._wait_main_window_after_login)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        if "notice_actionable" not in names:
            continue
        # 條件在「不可操作」時必須跳過這一輪（continue），不是只記個 log
        taken = eval(  # noqa: S307 - 受控:只求值這個檔案自己的守衛條件
            compile(ast.Expression(body=node.test), "<guard>", "eval"),
            {"__builtins__": {}}, {"notice_actionable": False})
        branch = node.body if taken else node.orelse
        assert any(isinstance(n, ast.Continue) for n in ast.walk(
            ast.Module(body=branch, type_ignores=[]))), (
            "★確認了可不可操作，卻沒有據此跳過★ 還是會對 disabled 的視窗一直按")
        return
    raise AssertionError(
        "★沒有先確認通知視窗自己是不是可操作★ 會對著 disabled 的視窗一直按")
