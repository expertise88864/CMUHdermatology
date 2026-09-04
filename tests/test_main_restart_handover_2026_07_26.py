# -*- coding: utf-8 -*-
"""[2026-07-26 審查] 主程式重啟:破壞性拆解必須延後到「確認新行程存活」之後。

舊版 `_restart_app` 在 spawn 前就 `_cleanup_for_exit()`(拔熱鍵、收 Chrome/executor)
+ `root.destroy()` + 釋放 mutex,於是 `restart_self` 內「新行程早夭 → 保留舊行程不退出」
的保護 return 回來時,舊行程其實已經被拆光:沒有熱鍵、沒有視窗、mutex 也放了 ——
主程式等於整個消失,診間要人工重開。自動更新會走這條路徑,不是罕見情境。
"""
import inspect
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402
from cmuh_common import paths, single_instance  # noqa: E402


def _code_only(src: str) -> str:
    """剝掉註解【與 docstring】，只留程式碼。

    docstring 也要剝:本檔的說明文字裡就寫著 `_cleanup_for_exit()`、
    `release_single_instance()` 這些字串,不剝的話「必須呼叫 X」的斷言會被說明文字
    滿足 → 測試恆真、抓不到真正的回歸。"""
    src = re.sub(r'("""|\'\'\')(?:.|\n)*?\1', "", src)
    out = []
    for line in src.splitlines():
        i = line.find("#")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)


def test_restart_defers_teardown_until_handover_confirmed():
    code = _code_only(inspect.getsource(main.AutomationApp._restart_app))
    assert "on_confirmed=" in code, "拆解必須交給 restart_self 的 on_confirmed"
    i_def = code.index("def _teardown_for_handover")
    i_call = code.index("restart_self(")
    assert i_def < i_call, "拆解要包成 callback 傳進去,不可在 spawn 前直接執行"
    body = code[i_def:i_call]
    for must in ("_cleanup_for_exit()", "self.root.destroy()",
                 "release_single_instance()"):
        assert must in body, f"{must} 必須在確認接手之後才做"
    # spawn 之前不可再有任何一項破壞性動作
    before = code[:i_def]
    for must_not in ("_cleanup_for_exit()", "self.root.destroy()",
                     "release_single_instance()"):
        assert must_not not in before, f"{must_not} 仍在 spawn 前執行"


def test_mutex_released_before_slow_teardown_steps():
    """★時間預算★ 新行程搶 mutex 只重試 1.5s,而本 callback 已在 spawn 後 0.6s 才被呼叫。
    `_flush_ledger_before_exit` 上限就有 2.0s、`_cleanup_for_exit` 還要收 Chrome/executor
    —— 釋放 mutex 若排在它們後面必定超時:新行程跳「已在執行中」而退出,舊行程隨後也
    照樣拆掉退出 → 一個主程式都不剩。故快而關鍵的兩件事(拔熱鍵、放 mutex)必須排最前面。"""
    # [第九輪 §4] 順序現在由兩個階段保證:`_preready_for_handover`(子行程回報「即將搶
    # mutex」時)只做拔熱鍵 + 放 mutex;`_teardown_for_handover`(子行程 READY 後)才做
    # 排空佇列 / 收 Chrome。wait_for_handover 保證 preready 先於 confirmed。
    import ast as _ast
    tree = _ast.parse(inspect.getsource(main.AutomationApp._restart_app).lstrip())
    fns = {n.name: n for n in _ast.walk(tree) if isinstance(n, _ast.FunctionDef)}
    pre = _ast.dump(fns["_preready_for_handover"])
    slow = _ast.dump(fns["_teardown_for_handover"])
    assert "safe_unhook_all_hotkeys" in pre and "release_single_instance" in pre
    assert "_flush_ledger_before_exit" not in pre and "_cleanup_for_exit" not in pre, \
        "慢的拆解不可以在 PRE-READY 做"
    assert "_flush_ledger_before_exit" in slow and "_cleanup_for_exit" in slow
    assert "release_single_instance" not in slow, "mutex 只在 PRE-READY 放一次"
    # 快的一步在原始碼裡的順序:先拔熱鍵、再放 mutex(拔熱鍵順帶消除新舊行程同時吃熱鍵的空窗)
    i_unhook = pre.index("safe_unhook_all_hotkeys")
    i_release = pre.index("release_single_instance")
    assert i_unhook < i_release
    from main import _LEDGER_FLUSH_TIMEOUT_SEC
    retry_sec = inspect.signature(
        single_instance.ensure_single_instance).parameters["retry_sec"].default
    assert _LEDGER_FLUSH_TIMEOUT_SEC > retry_sec, (
        "此測試存在的前提就是「排空可能比重試窗還久」;若不再成立請重新檢視順序理由")


def test_execv_fallback_skipped_when_handover_callback_given():
    """★外審★ os.execv 取代本行程、成功時永不返回 → on_confirmed 一定不會被呼叫。
    呼叫端傳 on_confirmed 就是要求「確認接手後才拆解」,走這條 fallback 會讓稽核排空/
    釋放 mutex/收 Chrome 全部跳過。有 callback 時必須寧可不重啟也不走它。"""
    code = _code_only(inspect.getsource(paths.restart_self))
    i_except = code.index("except Exception as e:")
    tail = code[i_except:]
    i_guard = tail.index("if on_confirmed is not None:")
    i_execv = tail.index("os.execv(")
    assert i_guard < i_execv, "fallback 之前必須先擋掉「有 callback」的情況"
    guard_body = tail[i_guard:i_execv]
    assert "return" in guard_body, "有 callback 時必須直接 return,保留完好的舊行程"


def test_restart_flushes_audit_ledger_before_exit():
    """稽核寫入緒是 daemon,行程退出會直接砍掉 → 重啟前剛入列的動作紀錄會憑空消失。
    關閉路徑(_on_close)早就排空了,重啟路徑漏掉 —— 而自動更新重啟比關閉頻繁得多。"""
    code = _code_only(inspect.getsource(main.AutomationApp._restart_app))
    assert "_flush_ledger_before_exit()" in code
    i_flush = code.index("_flush_ledger_before_exit()")
    i_cleanup = code.index("_cleanup_for_exit()")
    assert i_flush < i_cleanup, "排空要在拆解之前(executor/緒被收掉後就寫不進去了)"


def test_spawn_alive_window_stays_inside_mutex_retry_window():
    """★這兩個常數有依賴關係★ 釋放 mutex 延後到 on_confirmed 之後才做,
    所以新行程必須【還在 ensure_single_instance 的重試窗內】就等得到 mutex。
    restart_self 的存活輪詢窗一旦調大(或 retry_sec 調小)到反轉,重啟就會變成
    「新行程說已在執行中而退出、舊行程照樣被拆掉」= 程式整個消失。"""
    poll_window = paths._SPAWN_ALIVE_POLLS * paths._SPAWN_ALIVE_INTERVAL_SEC
    retry_sec = inspect.signature(
        single_instance.ensure_single_instance).parameters["retry_sec"].default
    assert poll_window < retry_sec, (
        f"存活輪詢 {poll_window}s 必須小於 mutex 重試窗 {retry_sec}s")
    # 留一點餘裕,不要剛好貼著
    assert poll_window * 2 <= retry_sec
