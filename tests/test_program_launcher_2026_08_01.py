# -*- coding: utf-8 -*-
"""[2026-08-01 P2-06 第五刀(a)] 外部程式啟動層。

★這一刀的價值不是「main.py 少了幾行」★
這些函式原本是 `AutomationApp` 裡不碰 `self` 的 method —— 被誤放進類別的模組函式。
但真正讓它們測不到的是**每一支都直接 `messagebox.showerror(...)`**：
「要跑哪個腳本、單一實例怎麼判、哪種錯誤該說什麼」這些真正的邏輯，
只有開得起 Tk 視窗才驗得到，實務上等於沒驗。

改成回傳 `LaunchOutcome` 之後，下面這些才第一次成為可測的東西：
  * ★單一實例：先查再啟動★（重複實例會重複打卡／重複寄信）
  * ★「已在執行」不是失敗★ —— 兩者原本都只是 `return`，分不出來，
    也就不可能驗證「已在執行時不該彈錯誤視窗」。
  * ★使用者看到的字串是契約★ 診間使用者看慣了那幾句話，搬家不可以順手改。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import program_launcher as pl  # noqa: E402


# ─── ★單一實例：先查再啟動★ ──────────────────────────────────────────────
def test_an_already_running_program_is_not_launched_again(monkeypatch):
    """★重複打卡／重複寄信就是這樣來的★ 已在執行時連 spawn 都不可以呼叫。"""
    calls = []
    monkeypatch.setattr(pl, "is_instance_running", lambda m: True)
    monkeypatch.setattr(pl, "launch_app_script",
                        lambda *a, **k: calls.append(a))

    out = pl.launch_helper_script(pl.AUTOCLOCK)
    assert calls == [], "★已在執行就完全不可以再啟動一次★"
    assert out.already_running is True


def test_already_running_is_not_a_failure(monkeypatch):
    """★「刻意不啟動」與「啟動失敗」是兩件事★

    原本兩者都只是 `return`，呼叫端分不出來 —— 也就無從保證「已在執行時不彈
    錯誤視窗」。現在 `failed` 明確為 False，`_show_launch_error` 才不會誤彈。
    """
    monkeypatch.setattr(pl, "is_instance_running", lambda m: True)
    monkeypatch.setattr(pl, "launch_app_script", lambda *a, **k: None)
    out = pl.launch_helper_script(pl.AUTOCLOCK)
    assert out.failed is False
    assert out.error_message == "", "不可以有任何要顯示給使用者的錯誤"


def test_the_mutex_is_checked_before_spawning(monkeypatch):
    """順序：先查、才啟動。反過來的話連按兩下仍會有兩個實例。"""
    order = []
    monkeypatch.setattr(pl, "is_instance_running",
                        lambda m: order.append("check") or False)
    monkeypatch.setattr(pl, "launch_app_script",
                        lambda *a, **k: order.append("spawn"))
    pl.launch_helper_script(pl.AUTOCLOCK)
    assert order == ["check", "spawn"]


def test_no_mutex_means_no_instance_check(monkeypatch):
    """沒有單一實例需求的程式（排班、座標偵測）不該被擋。"""
    monkeypatch.setattr(pl, "is_instance_running",
                        lambda m: pytest.fail("不該查 mutex"))
    monkeypatch.setattr(pl, "launch_app_script", lambda *a, **k: None)
    assert pl.launch_helper_script(pl.SCHEDULER).ok is True


def test_args_are_passed_through(monkeypatch):
    """★打卡按鈕一定要帶 --configure-if-empty★

    沒帶的話：沒有設定的電腦會進背景模式、autoclock 靜默結束，
    設定視窗再也叫不出來（2026-08-02 使用者定案的那條路）。
    """
    seen = {}
    monkeypatch.setattr(pl, "is_instance_running", lambda m: False)
    monkeypatch.setattr(pl, "launch_app_script",
                        lambda name, **k: seen.update(name=name, **k))
    pl.launch_helper_script(pl.AUTOCLOCK)
    assert seen["args"] == ("--configure-if-empty",)


# ─── ★使用者看到的字串是契約★ ────────────────────────────────────────────
def test_a_missing_script_says_which_file_and_where_to_look(monkeypatch):
    """措辭一字不改地保留 —— 搬家的驗收標準是「使用者看到的完全一樣」。"""
    def _missing(*a, **k):
        raise FileNotFoundError("nope")
    monkeypatch.setattr(pl, "launch_app_script", _missing)

    out = pl.launch_helper_script(pl.SCHEDULER)
    assert out.failed is True
    assert out.error_title == "啟動失敗"
    assert out.error_message == (
        "找不到排班程式檔案: 中國醫皮膚科排班程式.pyw\n\n"
        "請確認主程式與排班程式在同一個資料夾中。")


def test_the_peer_wording_defaults_to_the_generic_form(monkeypatch):
    """原本四支的措辭不一致（兩支寫程式名、兩支寫「該程式」）—— 照原樣保留，
    不趁搬家統一（那是另一個決定，不該夾帶）。"""
    def _missing(*a, **k):
        raise FileNotFoundError("nope")
    monkeypatch.setattr(pl, "launch_app_script", _missing)
    out = pl.launch_helper_script(pl.COORDINATE_DETECTOR)
    assert "請確認主程式與該程式在同一個資料夾中。" in out.error_message


def test_an_unexpected_error_is_reported_with_its_cause(monkeypatch):
    def _boom(*a, **k):
        raise PermissionError("拒絕存取")
    monkeypatch.setattr(pl, "launch_app_script", _boom)
    out = pl.launch_helper_script(pl.AUTOCLOCK)
    assert out.failed is True
    assert out.error_message == "無法啟動打卡程式:\n拒絕存取"


def test_launching_a_local_program_reports_a_bad_path(monkeypatch):
    def _missing(_p):
        raise FileNotFoundError()
    monkeypatch.setattr(pl.os, "startfile", _missing, raising=False)
    out = pl.open_local_program(r"D:\不存在\x.exe")
    assert out.failed is True
    assert out.error_message == (
        "找不到指定的程式！\n\n請確認路徑是否正確:\nD:\\不存在\\x.exe")


def test_opening_a_url_reports_failure(monkeypatch):
    monkeypatch.setattr(pl.webbrowser, "open",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("壞了")))
    out = pl.open_url("https://example.invalid")
    assert out.error_title == "開啟失敗"
    assert out.error_message == "無法開啟網頁:\n壞了"


def test_a_successful_launch_has_nothing_to_show(monkeypatch):
    monkeypatch.setattr(pl, "launch_app_script", lambda *a, **k: None)
    out = pl.launch_helper_script(pl.SCHEDULER)
    assert out.ok and not out.failed and out.error_message == ""


# ─── ★搬家會踩到的 __file__ 陷阱★ ────────────────────────────────────────
def test_open_file_at_line_takes_the_path_from_the_caller(monkeypatch):
    """★這支原本用 `__file__` 取路徑★

    寫在 main.py 裡時 `__file__` 就是 main.py，正確；搬進本模組之後會變成
    **本檔**，開出來是錯的檔案，而且【不會有任何錯誤訊息】—— 只會安靜地開錯。
    所以路徑必須由知道自己是誰的呼叫端傳進來。
    """
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(pl.open_file_at_line)))
    # 去 docstring —— 它自己就在解釋「不可以用 __file__」，不剝掉會比對到說明文字
    # （這個 repo 反覆踩到的自我命中，所以一律用 AST 剝，不靠眼睛）
    fn = tree.body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)
            and isinstance(fn.body[0].value.value, str)):
        fn.body = fn.body[1:]
    src = ast.unparse(tree)
    assert "__file__" not in src, \
        "搬家後不可以再用 __file__ —— 那會指到本模組，安靜地開錯檔案"

    seen = []
    monkeypatch.setattr(pl.subprocess, "Popen",
                        lambda args, **k: seen.append(args))
    monkeypatch.setattr("shutil.which", lambda exe: "C:\\fake\\" + exe)
    pl.open_file_at_line(r"C:\app\main.py", 42)
    assert seen and seen[0][-1].endswith("main.py:42")


def test_kill_orphan_chromedriver_survives_a_missing_psutil(monkeypatch):
    """psutil 是選用相依 —— 缺了只代表清不掉孤兒，不可以拋例外。

    （這支在退出流程裡跑，拋例外會讓關閉卡住。）
    """
    import builtins
    real_import = builtins.__import__

    def _no_psutil(name, *a, **k):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", _no_psutil)
    pl.kill_orphan_chromedriver()          # 不得拋出


def test_the_shared_layer_does_not_import_tkinter():
    """★共用層不可以相依 UI★

    原本每支都直接彈 messagebox，所以這些邏輯只有開得起 Tk 才測得到 ——
    也就等於沒測。這條斷言把那個相依永久擋在外面。
    """
    import ast
    path = os.path.join(os.path.dirname(__file__), "..", "src",
                        "cmuh_common", "program_launcher.py")
    tree = ast.parse(open(path, encoding="utf-8").read())
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            names.append(node.module or "")
    assert not [n for n in names if n.split(".")[0] == "tkinter"], \
        f"program_launcher 不可以 import tkinter（實際: {names}）"


# ─── ★[2026-08-01 外審 P3] log 訊息也是契約★ ─────────────────────────────
# 第一版把四支收斂成一支之後，log 改用程式名組出來（`Launching 排班程式: …`）。
# 但原字串**不是規則的**：排班的失敗訊息是 `Failed to launch scheduler`（沒有
# program），其餘三支是 `Failed to launch ○○ program`；「已在執行」那句也各自不同。
# 組不回來就是組不回來 —— 而 log 是事後查問題的依據，改掉會讓既有搜尋方式失效。
# 而且當時的測試只驗 `LaunchOutcome` 的 UI 文字，一句 log 都沒抓，所以完全沒發現。
_ORIGINAL_LOGS = {
    "SCHEDULER": {
        "log_launching": "Launching scheduler program: %s",
        "log_not_found": "Scheduler script not found: %s",
        "log_failed": "Failed to launch scheduler: %s",
        "log_already_running": "",
    },
    "AUTOCLOCK": {
        "log_launching": "Launching autoclock program: %s (--configure-if-empty)",
        "log_not_found": "Autoclock script not found: %s",
        "log_failed": "Failed to launch autoclock program: %s",
        "log_already_running": "Autoclock program is already running; skip launch",
    },
    "COORDINATE_DETECTOR": {
        "log_launching": "Launching coordinate detector program: %s",
        "log_not_found": "Coordinate detector script not found: %s",
        "log_failed": "Failed to launch coordinate detector program: %s",
        "log_already_running": "",
    },
    "CONSULT_QUERY": {
        "log_launching": "Launching consult query program: %s",
        "log_not_found": "Consult query script not found: %s",
        "log_failed": "Failed to launch consult query program: %s",
        "log_already_running":
            "Consult query program is already running; skip launch",
    },
}


@pytest.mark.parametrize("name", sorted(_ORIGINAL_LOGS))
def test_the_log_templates_match_the_pre_refactor_strings(name):
    """搬家不可以改 log —— 那是事後查問題的依據。"""
    program = getattr(pl, name)
    for field, expected in _ORIGINAL_LOGS[name].items():
        assert getattr(program, field) == expected, \
            f"{name}.{field} 與搬家前不一致"


def test_the_launch_log_renders_exactly_as_before(monkeypatch, caplog):
    """★真的跑一次，看 log 實際長什麼樣★ 只比對模板字串還不夠：
    參數順序接錯的話模板對、渲染出來仍然是錯的。"""
    import logging as _lg
    monkeypatch.setattr(pl, "launch_app_script", lambda *a, **k: None)
    with caplog.at_level(_lg.INFO):
        pl.launch_helper_script(pl.SCHEDULER)
    assert ("Launching scheduler program: 中國醫皮膚科排班程式.pyw"
            in [r.getMessage() for r in caplog.records])


def test_the_failure_log_renders_the_exception(monkeypatch, caplog):
    import logging as _lg

    def _boom(*a, **k):
        raise PermissionError("拒絕存取")
    monkeypatch.setattr(pl, "launch_app_script", _boom)
    with caplog.at_level(_lg.ERROR):
        pl.launch_helper_script(pl.SCHEDULER)
    assert "Failed to launch scheduler: 拒絕存取" in \
        [r.getMessage() for r in caplog.records]


def test_the_already_running_log_renders(monkeypatch, caplog):
    import logging as _lg
    monkeypatch.setattr(pl, "is_instance_running", lambda m: True)
    with caplog.at_level(_lg.INFO):
        pl.launch_helper_script(pl.CONSULT_QUERY)
    assert "Consult query program is already running; skip launch" in \
        [r.getMessage() for r in caplog.records]


def test_every_helper_program_carries_its_own_log_strings():
    """★空集合／漏填不算通過★ 新增一支輔助程式時，四條 log 不可以留空
    （留空的話 `logging.info("")` 會印出空行，等於那次啟動沒有紀錄）。"""
    programs = [v for v in vars(pl).values()
                if isinstance(v, pl.HelperProgram)]
    assert len(programs) == 4, f"預期 4 支輔助程式，實際 {len(programs)}"
    for p in programs:
        assert p.log_launching and p.log_not_found and p.log_failed, \
            f"{p.what} 少了 log 模板"
        if p.single_instance_mutex:
            assert p.log_already_running, \
                f"{p.what} 有單一實例鎖卻沒有「已在執行」的 log"
