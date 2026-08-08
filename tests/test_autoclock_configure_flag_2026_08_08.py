# -*- coding: utf-8 -*-
"""外審：刪光最後一個帳號後，打卡程式不可以整個消失。

【問題】從設定視窗離開時走的是 `restart_program()`，而它靠
`_machine_has_clock_accounts()` 決定要不要帶 `--configure-if-empty`。
那個 predicate 回答的是「【現在】設定檔裡有沒有帳號」—— 而 `save_config()`
已經先把帳號存成 `[]` 了，所以它讀到的是刪除【之後】的狀態。

於是：刪光最後一個帳號 → 存檔 → 重啟（沒帶旗標）→ 新行程讀到空設定就靜默
結束。設定視窗與 tray 一起消失，使用者以為打卡還在跑，實際上早就沒在跑。

同一檔案第 2275–2278 行的註解明訂「刪光最後一個帳號後應重新開啟設定視窗」——
宣稱與實作不符：那個 predicate 答的是別的問題。

【修法】從設定視窗離開的兩條路徑【自己明講】帶旗標（它們本來就知道自己是
從設定視窗來的），predicate 留給自動更新／health 重啟用 —— 那些情境沒有人
剛剛在編輯設定，「現在有沒有帳號」正好就是要問的問題。
"""
import ast
import inspect
import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import autoclock as ac  # noqa: E402


def _passes_flag(fn) -> bool:
    """這個函式裡的 `restart_program(...)` 有沒有帶 CONFIGURE_IF_EMPTY_FLAG?"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "restart_program"):
            for a in n.args:
                if (isinstance(a, ast.Name)
                        and a.id == "CONFIGURE_IF_EMPTY_FLAG"):
                    return True
    return False


class TestLeavingTheSettingsUiAlwaysKeepsTheAppAlive:

    def test_save_and_background_passes_the_flag(self):
        """★核心★ 「儲存並回背景」是刪光帳號最常見的離開方式。"""
        assert _passes_flag(ac.ClockApp.save_and_bg), (
            "★沒帶 --configure-if-empty★ 刪光最後一個帳號後,新行程會讀到空設定"
            "而靜默結束 —— 設定視窗與 tray 一起消失,使用者以為打卡還在跑")

    def test_closing_the_window_passes_the_flag(self):
        """直接按 X 離開也一樣(它同樣可能發生在刪光帳號之後)。"""
        src = inspect.getsource(ac)
        tree = ast.parse(src)
        # main() 裡「設定視窗關閉 → 回背景」那一段
        for n in ast.walk(tree):
            if not isinstance(n, ast.If):
                continue
            if "_config_restart_requested" not in ast.dump(n.test):
                continue
            for c in ast.walk(n):
                if (isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                        and c.func.id == "restart_program"
                        and any(isinstance(a, ast.Name)
                                and a.id == "CONFIGURE_IF_EMPTY_FLAG"
                                for a in c.args)):
                    return
        pytest.fail("★設定視窗按 X 離開時沒帶旗標★ 刪光帳號後程式會整個消失")

    def test_an_explicit_flag_survives_an_empty_account_list(self, monkeypatch):
        """★行為★ 明講的旗標不可以被 predicate 蓋掉 ——
        設定檔【現在】是空的正是這個情境的前提。"""
        seen = {}
        monkeypatch.setattr(ac, "_machine_has_clock_accounts", lambda: False)
        monkeypatch.setattr(
            ac, "restart_self",
            lambda extra, **k: seen.update(extra=list(extra)) or "ok")
        # `_teardown_for_handover` 是 `restart_program` 內的巢狀函式,
        # 而且只由 `restart_self(on_confirmed=...)` 呼叫 —— stub 掉
        # `restart_self` 之後它本來就不會執行。
        monkeypatch.setattr(sys, "argv", ["autoclock.py"])
        ac.restart_program(ac.CONFIGURE_IF_EMPTY_FLAG)
        assert ac.CONFIGURE_IF_EMPTY_FLAG in seen.get("extra", []), (
            f"★明講的旗標不見了★:{seen}")

    def test_a_machine_without_accounts_is_not_nagged_on_auto_update(
            self, monkeypatch):
        """★反方向(使用者 2026-08-06 回報過的事故)★
        自動更新重啟不可以在一台根本不做打卡的電腦上彈出設定視窗。"""
        seen = {}
        monkeypatch.setattr(ac, "_machine_has_clock_accounts", lambda: False)
        monkeypatch.setattr(
            ac, "restart_self",
            lambda extra, **k: seen.update(extra=list(extra)) or "ok")
        # `_teardown_for_handover` 是 `restart_program` 內的巢狀函式,
        # 而且只由 `restart_self(on_confirmed=...)` 呼叫 —— stub 掉
        # `restart_self` 之後它本來就不會執行。
        monkeypatch.setattr(sys, "argv", ["autoclock.py"])
        ac.restart_program()
        assert ac.CONFIGURE_IF_EMPTY_FLAG not in seen.get("extra", []), (
            "★沒有帳號的機器被自動更新彈出打卡設定視窗★")

    def test_a_machine_with_accounts_still_gets_the_flag(self, monkeypatch):
        """有帳號的機器,自動更新重啟仍要帶旗標(萬一設定在重啟途中變空)。"""
        seen = {}
        monkeypatch.setattr(ac, "_machine_has_clock_accounts", lambda: True)
        monkeypatch.setattr(
            ac, "restart_self",
            lambda extra, **k: seen.update(extra=list(extra)) or "ok")
        # `_teardown_for_handover` 是 `restart_program` 內的巢狀函式,
        # 而且只由 `restart_self(on_confirmed=...)` 呼叫 —— stub 掉
        # `restart_self` 之後它本來就不會執行。
        monkeypatch.setattr(sys, "argv", ["autoclock.py"])
        ac.restart_program()
        assert ac.CONFIGURE_IF_EMPTY_FLAG in seen.get("extra", [])
