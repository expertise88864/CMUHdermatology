# -*- coding: utf-8 -*-
"""批次 K（外部 review P1-01A / P1-02）。

P1-01A 兩支會對 HIS 送輸入的無人看顧程式，在「查不出來」時仍照常啟動。
P1-02  兩套 recovery 解析已經 drift：bootstrap 嚴格、updater 寬鬆。
"""
from __future__ import annotations

import ast
import io
import json
import os

import pytest

import bootstrap_recovery as br
from cmuh_common import updater

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HIS_INPUT_LAUNCHERS = [
    "中國醫皮膚科打卡程式.pyw",
    "中國醫皮膚科會診查詢程式.pyw",
]
NON_HIS_LAUNCHERS = [
    "中國醫皮膚科守護程式.pyw",
    "中國醫皮膚科排班程式.pyw",
    "中國醫皮膚科點座標偵測程式.pyw",
]


def _read(name):
    with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as f:
        return f.read()


def _guard_of(name):
    """取那支 `_recover_incomplete_update` 的 AST。"""
    tree = ast.parse(_read(name))
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef)
                and n.name == "_recover_incomplete_update")


# ── P1-01A：查不出來 ≠ 安全 ─────────────────────────────────────────────
class TestUnknownIsNotSafe:

    @pytest.mark.parametrize("name", HIS_INPUT_LAUNCHERS)
    def test_no_path_returns_true_without_checking(self, name):
        """★這是 finding★

        `import bootstrap_recovery` 失敗、或 `recover_and_report` 自己拋例外時，
        原本都 `return True` 照常啟動。而「復原模組自己載不進來」正是它被更新
        換到一半的樣子 —— 磁碟混版機率最高的那一刻，卻放行。
        """
        func = _guard_of(name)
        handlers = [h for h in ast.walk(func)
                    if isinstance(h, ast.ExceptHandler)]
        assert handlers, f"{name}：找不到例外處理（測試失效了）"
        for h in handlers:
            returns = [n for n in ast.walk(h) if isinstance(n, ast.Return)]
            assert returns, f"{name}：例外分支沒有回傳值"
            for r in returns:
                assert not (isinstance(r.value, ast.Constant)
                            and r.value.value is True), (
                    f"{name}：★查不出來卻回 True★")

    @pytest.mark.parametrize("name", HIS_INPUT_LAUNCHERS)
    def test_every_failure_path_leaves_a_trace(self, name):
        """無聲退出必須留下可查的東西，否則現場只看到「排程跑了但沒動作」。"""
        func = _guard_of(name)
        for h in [n for n in ast.walk(func)
                  if isinstance(n, ast.ExceptHandler)]:
            calls = {n.func.id for n in ast.walk(h)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            assert "_report_startup_crash" in calls, (
                f"{name}：例外分支沒有寫 startup_crash.log")

    @pytest.mark.parametrize("name", HIS_INPUT_LAUNCHERS)
    def test_no_failure_path_pops_a_dialog(self, name):
        """★[2026-08-02 外審第 2 輪 P1] 我把自己禁止的東西加了回去★

        `_report_startup_crash` 會同步彈 `MessageBoxW`。這兩支是 ONLOGON 排程
        （而且 MultipleInstances=IgnoreNew）—— 沒有人會按那個「確定」，行程就
        永遠停在那裡，走不到 SystemExit(3)，之後每次排程又被 IgnoreNew 忽略。
        結果是打卡／會診★無限期停擺★，排程紀錄上連非零離開碼都看不到。
        """
        func = _guard_of(name)
        for h in [n for n in ast.walk(func)
                  if isinstance(n, ast.ExceptHandler)]:
            for call in [n for n in ast.walk(h)
                         if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Name)
                         and n.func.id == "_report_startup_crash"]:
                flags = {kw.arg: kw.value for kw in call.keywords}
                assert "show_dialog" in flags, (
                    f"{name}：復原失敗路徑沒有關掉彈窗")
                value = flags["show_dialog"]
                assert isinstance(value, ast.Constant) and value.value is False, (
                    f"{name}：★無人看顧的路徑仍會跳窗★")

    @pytest.mark.parametrize("name", HIS_INPUT_LAUNCHERS)
    def test_the_reporter_actually_honours_the_flag(self, name):
        """光是傳參數不夠 —— `_report_startup_crash` 本身要真的據此跳過彈窗。"""
        tree = ast.parse(_read(name))
        reporter = next(n for n in ast.walk(tree)
                        if isinstance(n, ast.FunctionDef)
                        and n.name == "_report_startup_crash")
        assert any(a.arg == "show_dialog" for a in reporter.args.kwonlyargs), (
            f"{name}：_report_startup_crash 沒有 show_dialog 參數")
        # MessageBoxW 必須在「早退」之後才可能被呼叫
        guards = [n for n in ast.walk(reporter) if isinstance(n, ast.If)
                  and isinstance(n.test, ast.UnaryOp)
                  and isinstance(n.test.op, ast.Not)
                  and isinstance(n.test.operand, ast.Name)
                  and n.test.operand.id == "show_dialog"
                  and any(isinstance(x, ast.Return) for x in n.body)]
        assert guards, f"{name}：show_dialog=False 時沒有早退"
        boxes = [n for n in ast.walk(reporter)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "MessageBoxW"]
        assert boxes, f"{name}：找不到 MessageBoxW（測試失效了）"
        assert min(b.lineno for b in boxes) > max(g.lineno for g in guards), (
            f"{name}：彈窗排在早退之前，旗標形同虛設")

    @pytest.mark.parametrize("name", NON_HIS_LAUNCHERS + [
        "中國醫皮膚科主程式.pyw"])
    def test_the_attended_and_untouched_programs_keep_the_dialog(self, name):
        """★空集合不算通過★ 只有「無人看顧的復原失敗」那條路徑該關掉彈窗；
        真正的啟動崩潰仍然要讓現場看得到。"""
        tree = ast.parse(_read(name))
        boxes = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "MessageBoxW"]
        assert boxes, f"{name}：啟動崩潰的彈窗不見了"

    @pytest.mark.parametrize("name", HIS_INPUT_LAUNCHERS)
    def test_the_exit_code_distinguishes_did_not_run(self, name):
        """★「已有一份在跑」用 0，「這一輪沒做事」要非零★

        排程紀錄看得出差別，才查得到「為什麼今天沒打卡」。
        """
        tree = ast.parse(_read(name))
        guards = [n for n in ast.walk(tree) if isinstance(n, ast.If)
                  and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                          and c.func.id == "_recover_incomplete_update"
                          for c in ast.walk(n.test))]
        assert guards, f"{name}：沒有把退出掛在復原結果上"
        codes = [r.exc.args[0].value for g in guards
                 for r in ast.walk(g)
                 if isinstance(r, ast.Raise) and isinstance(r.exc, ast.Call)
                 and isinstance(r.exc.func, ast.Name)
                 and r.exc.func.id == "SystemExit"
                 and r.exc.args and isinstance(r.exc.args[0], ast.Constant)]
        assert codes, f"{name}：找不到帶離開碼的 SystemExit"
        assert all(c != 0 for c in codes), (
            f"{name}：★用 0 離開會和「已有一份在跑」混在一起★")

    @pytest.mark.parametrize("name", NON_HIS_LAUNCHERS)
    def test_the_non_his_programs_are_unchanged(self, name):
        """★空集合不算通過★ 這條規則不可以無聲擴散到不碰 HIS 的程式。

        守護程式尤其不可以擋：它是無人看顧下唯一會反覆重試復原的東西。
        """
        assert "safe_to_start" not in _read(name)


# ── P1-02：不要維護兩套復原引擎 ─────────────────────────────────────────
class TestThereIsOnlyOneJournalParser:

    @pytest.mark.parametrize("payload,why", [
        ({}, "空物件"),
        ({"schema": 1}, "沒有 files"),
        ({"schema": 1, "files": []}, "files 是空的"),
        ({"schema": 99, "files": [{"target": "x", "existed_before": True}]},
         "schema 不認得"),
        ({"schema": 1, "files": [{"existed_before": True}]}, "缺 target"),
        ({"schema": 1, "files": "not a list"}, "files 型別錯"),
        ([1, 2, 3], "根本不是物件"),
    ])
    def test_both_engines_agree_on_what_is_unreadable(self, payload, why):
        """★雙向差異測試★ 同一份 payload，兩邊必須做出同一個判斷。"""
        strict, _ = br.parse_journal(payload)
        shared, _ = updater._strict_parse_journal(payload)
        assert strict is None, f"{why}：bootstrap 應該判為看不懂"
        assert shared is None, f"{why}：updater 也必須判為看不懂"

    def test_both_engines_agree_on_a_good_journal(self):
        """★空集合不算通過★ 正常的日誌兩邊都要讀得出來、而且內容一致。"""
        payload = {"schema": br.JOURNAL_SCHEMA,
                   "files": [{"target": "C:\\app\\a.py",
                              "existed_before": True, "staged": ""}]}
        strict, _ = br.parse_journal(payload)
        shared, _ = updater._strict_parse_journal(payload)
        assert strict == shared
        assert strict is not None and len(strict) == 1

    def test_the_updater_does_not_keep_its_own_loose_parser(self):
        """結構釘子：`_recover_locked` 不可以再自己 `payload.get("files")`。"""
        import inspect
        import textwrap
        src = textwrap.dedent(inspect.getsource(updater._recover_locked))
        tree = ast.parse(src)
        loose = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "get"
                 and n.args and isinstance(n.args[0], ast.Constant)
                 and n.args[0].value == "files"]
        assert loose == [], "updater 又長出自己的一套解析"
        assert "_strict_parse_journal" in src


class TestAnUnreadableJournalIsLeftAlone:

    def _seed(self, tmp_path, payload):
        p = tmp_path / updater.JOURNAL_FILENAME
        p.write_text(json.dumps(payload), encoding="utf-8")
        return p

    def test_the_updater_no_longer_deletes_the_evidence(self, tmp_path):
        """★這是 drift 的實際後果★

        bootstrap 判 UNKNOWN 並保留證據 → 使用者選「仍要啟動」→ updater 上場，
        原本會把同一份 JSON 當成空陣列、判定「沒事」→ 刪掉那份唯一的證據。
        """
        journal = self._seed(tmp_path, {"schema": 99, "files": [{"x": 1}]})

        updater.recover_incomplete_update(str(tmp_path))

        assert journal.exists(), "★看不懂的日誌被刪掉了★"

    def test_an_unreadable_journal_rolls_nothing_back(self, tmp_path):
        """看不懂就【什麼都不要動】—— 照著猜的規則搬檔案比不搬更危險。"""
        target = tmp_path / "a.py"
        target.write_text("現在的內容", encoding="utf-8")
        (tmp_path / "a.py.bak").write_text("舊的內容", encoding="utf-8")
        self._seed(tmp_path, {"schema": 99,
                              "files": [{"target": str(target),
                                         "existed_before": True}]})

        updater.recover_incomplete_update(str(tmp_path))

        assert target.read_text(encoding="utf-8") == "現在的內容"

    def test_a_good_journal_still_rolls_back(self, tmp_path):
        """★空集合不算通過★ 別讓「一律不動」變成新的失效模式。"""
        target = tmp_path / "a.py"
        target.write_text("新版", encoding="utf-8")
        (tmp_path / "a.py.bak").write_text("舊版", encoding="utf-8")
        assert updater._write_commit_journal(
            str(tmp_path), [(str(target), True, "")]) is True

        updater.recover_incomplete_update(str(tmp_path))

        assert target.read_text(encoding="utf-8") == "舊版"

    def test_an_unimportable_strict_parser_still_refuses_to_guess(
            self, tmp_path, monkeypatch):
        """★突變驗證抓到：`_strict_parse_journal` 的 except 分支沒被走到過★

        我原本的測試是把整支 `_strict_parse_journal` 換掉，所以那個
        「借不到就回 None」的分支從來沒有被執行 —— 把它改成退回寬鬆解析，
        測試照樣全綠。這裡真的讓 `import bootstrap_recovery` 失敗。
        """
        import sys
        monkeypatch.setitem(sys.modules, "bootstrap_recovery", None)

        files, why = updater._strict_parse_journal(
            {"schema": 1, "files": [{"target": "x", "existed_before": True}]})

        assert files is None, "★借不到嚴格解析卻自己猜了★"
        assert why

    def test_an_unreadable_journal_says_so_out_loud(self, tmp_path, caplog):
        """★「刻意什麼都不做」與「撞到例外」要分得出來★

        突變把 `if files is None:` 改成 `if False:` 之後，程式會在
        `len(None)` 炸掉、被最外層接住 —— 結果看起來一樣（沒回滾、日誌還在），
        但那是【撞出來的】。判準要看它有沒有講出自己的判斷。
        """
        self._seed(tmp_path, {"schema": 99, "files": [{"x": 1}]})
        with caplog.at_level("ERROR"):
            updater.recover_incomplete_update(str(tmp_path))
        assert any("交易日誌看不懂" in r.getMessage() for r in caplog.records), (
            f"沒有說明為什麼不做：{caplog.text}")

    def test_losing_the_strict_parser_does_not_fall_back_to_loose(
            self, tmp_path, monkeypatch):
        """★借不到嚴格解析時不可以退回寬鬆版★ 那正是 drift 長回來的地方。"""
        target = tmp_path / "a.py"
        target.write_text("新版", encoding="utf-8")
        (tmp_path / "a.py.bak").write_text("舊版", encoding="utf-8")
        assert updater._write_commit_journal(
            str(tmp_path), [(str(target), True, "")]) is True

        monkeypatch.setattr(updater, "_strict_parse_journal",
                            lambda payload: (None, "假裝借不到"))
        updater.recover_incomplete_update(str(tmp_path))

        assert target.read_text(encoding="utf-8") == "新版", "不該動任何檔案"
        assert (tmp_path / updater.JOURNAL_FILENAME).exists(), "日誌要留著"
