# -*- coding: utf-8 -*-
"""[批次 Y] 排程版 watchdog 的入口必須是固定的 `.pyw`。

★外審 2026-08-09 P1-01★
`安裝開機自動啟動.ps1` 以前把每 2 分鐘的 task 註冊成
`pythonw.exe "<app>/src/watchdog_runner.py" --once` —— **不經過 launcher**。
`current.txt` 切版之後：其他五支跑新版，watchdog 每兩分鐘永遠跑 `<app>/src`
的舊版；`CMUH_APP_DIR` / `CMUH_LAUNCHER` 根本沒被設過。
**最後一道復原防線自己停在舊版，而且沒有任何地方會說出來。**

★改 installer 不夠★ 已部署的電腦不會再跑一次安裝腳本。所以要在執行期偵測
並改寫，**改完回讀確認**，失敗要講出來（不可以假成功）。
"""
import os
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from cmuh_common import watchdog_task as wt  # noqa: E402

_LEGACY = 'pythonw.exe "C:\\App\\src\\watchdog_runner.py" --once'
_GOOD = 'pythonw.exe "C:\\App\\中國醫皮膚科守護程式.pyw" --once'


# ── 判準本身 ─────────────────────────────────────────────────────────────
def test_the_legacy_action_is_recognised():
    assert wt.action_is_legacy(_LEGACY) is True


def test_the_launcher_action_is_not_legacy():
    assert wt.action_is_legacy(_GOOD) is False


def test_an_empty_action_is_not_called_legacy():
    """★查不到 ≠ 是舊的★ 空字串不可以觸發改寫（那是「不知道」）。"""
    for v in ("", None, "   "):
        assert wt.action_is_legacy(v) is False


def test_a_launcher_action_that_also_mentions_the_runner_is_fine():
    """launcher 內部本來就會跑 watchdog_runner.py —— 有 launcher 就算對。"""
    mixed = ('pythonw.exe "C:\\App\\中國醫皮膚科守護程式.pyw" --once '
             '# 內部跑 watchdog_runner.py')
    assert wt.action_is_legacy(mixed) is False


def test_the_desired_action_points_at_the_launcher():
    got = wt.desired_action("C:\\App", pythonw="pyw.exe")
    assert wt.LAUNCHER_NAME in got and "--once" in got
    assert wt.action_is_legacy(got) is False


# ── migrate 的每一條出口 ─────────────────────────────────────────────────
@pytest.fixture
def app(tmp_path):
    (tmp_path / wt.LAUNCHER_NAME).write_text("", encoding="utf-8")
    return str(tmp_path)


def _stub(monkeypatch, queries, change_rc=0):
    """queries 依序回傳 (action, reason)；change_rc 是 /Change 的結果。"""
    seq = list(queries)
    calls = []

    def _q(task_name=wt.TASK_NAME):
        return seq.pop(0) if seq else (None, wt.UNREADABLE)

    def _run(args, timeout=20.0):
        calls.append(args)
        return (change_rc, "")

    monkeypatch.setattr(wt, "query_action", _q)
    monkeypatch.setattr(wt, "_run", _run)
    return calls


def test_an_already_correct_task_is_left_alone(app, monkeypatch):
    calls = _stub(monkeypatch, [(_GOOD, "")])
    assert wt.migrate_if_legacy(app) == wt.OK_ALREADY
    assert calls == [], "已經是對的卻還去改寫"


def test_a_legacy_task_is_migrated_and_verified(app, monkeypatch):
    calls = _stub(monkeypatch, [(_LEGACY, ""), (_GOOD, "")])
    assert wt.migrate_if_legacy(app) == wt.MIGRATED
    assert calls and "/Change" in calls[0]


def test_a_change_that_did_not_take_is_reported_as_failed(app, monkeypatch):
    """★改完一定要回讀★ `/Change` 回 0 不代表真的寫進去了。"""
    _stub(monkeypatch, [(_LEGACY, ""), (_LEGACY, "")])   # 回讀仍是舊的
    assert wt.migrate_if_legacy(app) == wt.FAILED


def test_a_failing_change_command_is_reported(app, monkeypatch):
    _stub(monkeypatch, [(_LEGACY, "")], change_rc=1)
    assert wt.migrate_if_legacy(app) == wt.FAILED


def test_an_unreadable_task_is_not_assumed_correct(app, monkeypatch):
    """★核心★ 查不到現況就當成「已經是對的」＝在不知道的情況下宣稱沒問題。"""
    calls = _stub(monkeypatch, [(None, wt.UNREADABLE)])
    assert wt.migrate_if_legacy(app) == wt.UNREADABLE
    assert calls == [], "查不到卻還是去改寫了"


def test_no_task_is_a_distinct_outcome(app, monkeypatch):
    _stub(monkeypatch, [(None, wt.NO_TASK)])
    assert wt.migrate_if_legacy(app) == wt.NO_TASK


def test_a_missing_launcher_blocks_the_rewrite(tmp_path, monkeypatch):
    """★launcher 不在就不可以改★ 改了會變成排程指向不存在的檔，比現況更糟。"""
    calls = _stub(monkeypatch, [(_LEGACY, "")])
    assert wt.migrate_if_legacy(str(tmp_path)) == wt.FAILED
    assert calls == []


def test_the_outcome_set_is_closed():
    got = {wt.OK_ALREADY, wt.MIGRATED, wt.NO_TASK, wt.UNREADABLE, wt.FAILED}
    assert len(got) == 5, "回傳值撞名了 —— 呼叫端會分不出來"


# ── 接線：installer 與 --once ─────────────────────────────────────────────
def test_the_installer_no_longer_registers_the_src_script():
    """★PS1 的排程定義不得再指向 src 底下那支★"""
    with open(os.path.join(REPO_ROOT, "安裝開機自動啟動.ps1"),
              encoding="utf-8") as fh:
        text = fh.read()
    # ★要查【指派】那一行，不是組命令那一行★
    #   `$tr` 用的是 `$scriptFullPath`；真正決定跑哪支檔的是它的指派。
    #   我第一版只看 `$tr`，那是一個永遠不會變的字串 —— 測不到任何東西。
    assigns = [ln.strip() for ln in text.splitlines()
               if ln.strip().startswith("$scriptFullPath =")]
    assert assigns, "找不到 $scriptFullPath 的指派（測試自己失效了）"
    body = "\n".join(assigns)
    assert "ScriptRelPath" not in body, (
        "排程仍用 ScriptRelPath 直接跑 src 底下那支 —— 切版後 watchdog 跑舊版："
        + body)
    assert "$pywPath" in body, "排程沒有改用 .pyw 啟動器：" + body


def test_the_once_path_runs_the_migration():
    """★接上去了才算數★ 不接的話已部署的機器永遠不會被修好。"""
    import ast
    import inspect
    import textwrap
    import importlib
    runner = importlib.import_module("watchdog_runner")
    src = textwrap.dedent(inspect.getsource(runner._run_once_via_core))
    names = {n.func.id for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_migrate_scheduled_task_if_legacy" in names, (
        "--once 沒有檢查排程 —— 已部署的機器不會自己修好")


def test_the_migration_never_blocks_the_watchdog_tick(monkeypatch):
    """★遷移失敗不可以擋住本輪的 watchdog 工作★（它是最後一道防線）"""
    import importlib
    runner = importlib.import_module("watchdog_runner")

    def _boom(*a, **k):
        raise RuntimeError("schtasks 掛了")

    monkeypatch.setattr(wt, "migrate_if_legacy", _boom)
    assert runner._migrate_scheduled_task_if_legacy() == "error"
