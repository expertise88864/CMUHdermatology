# -*- coding: utf-8 -*-
"""[2026-07-30 第二輪外審 P2-08] push_helper 允許缺少工具時略過本機關卡。

舊版 `step_quality_gate()` 的寫法是：

    if importlib.util.find_spec(module) is None:
        print("[略過] ... 未安裝，跳過。CI 仍會把關。")
        continue

★「CI 仍會把關」在這個專案是錯的★
  * push 是【直推 main】,GitHub CI 是推上去【之後】才跑的;而診間電腦的自動更新
    大約 5 分鐘內就把新版拉下去。CI 紅燈的時候,壞版本已經在診間了。
  * 而【工具沒裝】正是最可能發生在新機器/重灌後的情境 —— 也就是最需要關卡的時候。
    那一刻退回「不檢查」,等於這把鎖只在不需要它的時候有效
    （跟 P1-06 更新鎖犯過的錯完全一樣）。

修法:缺工具 → 中止;真的緊急走 `--emergency "理由"`,要寫理由、會大字印出來、
而且寫進 commit 訊息永久可查。
"""
import ast
import importlib.util
import io
import os

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PUSH_HELPER = os.path.join(REPO_ROOT, "scripts", "push_helper.py")


def _load():
    spec = importlib.util.spec_from_file_location("push_helper_p208",
                                                  PUSH_HELPER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ph():
    return _load()


# ─── ★核心★ 工具沒裝 → 中止，不是略過 ────────────────────────────────────
def test_a_missing_tool_aborts_the_push(ph, monkeypatch, capsys):
    """★這就是 P2-08★ 缺工具必須中止，不可印一行「已略過」就繼續推。"""
    monkeypatch.setattr(ph.importlib.util, "find_spec",
                        lambda name: None if name == "pyright" else object())
    with pytest.raises(SystemExit) as ei:
        ph.step_quality_gate()
    assert ei.value.code == 1
    out = capsys.readouterr().out
    assert "中止推送" in out
    assert "pyright" in out


def test_the_abort_message_explains_why_ci_is_not_enough(ph, monkeypatch,
                                                         capsys):
    """訊息要講清楚【為什麼】不能靠 CI —— 否則下一個人只會想辦法把這道關卡拿掉。"""
    monkeypatch.setattr(ph.importlib.util, "find_spec", lambda _n: None)
    with pytest.raises(SystemExit):
        ph.step_quality_gate()
    out = capsys.readouterr().out
    assert "直推 main" in out
    assert "5 分鐘" in out
    assert "--emergency" in out


def test_every_missing_tool_is_listed_at_once(ph, monkeypatch, capsys):
    """一次列出全部缺的，不要修一個才發現還缺下一個。"""
    monkeypatch.setattr(ph.importlib.util, "find_spec", lambda _n: None)
    with pytest.raises(SystemExit):
        ph.step_quality_gate()
    out = capsys.readouterr().out
    for tool in ("ruff", "pyright", "pytest", "pytest-cov"):
        assert tool in out


def test_no_check_runs_when_tools_are_missing(ph, monkeypatch):
    """★缺工具就不要跑「部分檢查」★
    跑一半再中止，會讓人以為「至少 ruff 過了」——實際上那份綠燈不完整。
    """
    monkeypatch.setattr(ph.importlib.util, "find_spec",
                        lambda name: None if name == "pytest" else object())
    ran = []
    monkeypatch.setattr(ph.subprocess, "run",
                        lambda cmd, **k: ran.append(cmd) or _rc(0))
    with pytest.raises(SystemExit):
        ph.step_quality_gate()
    assert ran == [], f"缺工具時不該跑任何檢查，實際跑了：{ran}"


class _RC:
    def __init__(self, code):
        self.returncode = code


def _rc(code):
    return _RC(code)


# ─── 關卡本身：新增的檢查真的有跑 ─────────────────────────────────────────
def test_the_gate_runs_ruff_pyright_pytest_and_the_ratchets(ph, monkeypatch):
    monkeypatch.setattr(ph.importlib.util, "find_spec", lambda _n: object())
    ran = []
    monkeypatch.setattr(ph.subprocess, "run",
                        lambda cmd, **k: ran.append(cmd) or _rc(0))
    ph.step_quality_gate()
    joined = [" ".join(str(x) for x in cmd) for cmd in ran]
    assert any("ruff" in j for j in joined)
    assert any("pyright" in j for j in joined), \
        "pyright 以前只在 CI 跑 —— 型別錯誤都是推上去之後才發現"
    assert any("pytest" in j for j in joined)
    assert any("check_skips.py" in j for j in joined)
    assert any("check_coverage.py" in j for j in joined)


def test_pytest_produces_the_reports_the_ratchets_need(ph, monkeypatch):
    """一次 pytest 同時產出 junit.xml 與 cov.json —— 不為了兩道棘輪多跑一次全套。"""
    monkeypatch.setattr(ph.importlib.util, "find_spec", lambda _n: object())
    ran = []
    monkeypatch.setattr(ph.subprocess, "run",
                        lambda cmd, **k: ran.append(cmd) or _rc(0))
    ph.step_quality_gate()
    pytest_cmd = next(" ".join(str(x) for x in c) for c in ran
                      if "pytest" in " ".join(str(x) for x in c)
                      and "check_" not in " ".join(str(x) for x in c))
    assert "--junitxml=junit.xml" in pytest_cmd
    assert "cov.json" in pytest_cmd


def test_the_ratchets_are_skipped_when_pytest_is_red(ph, monkeypatch, capsys):
    """★pytest 紅燈時報告不完整★ 拿不完整的報告去判只會誤導。"""
    monkeypatch.setattr(ph.importlib.util, "find_spec", lambda _n: object())
    ran = []

    def _run(cmd, **_k):
        ran.append(cmd)
        joined = " ".join(str(x) for x in cmd)
        return _rc(1 if ("pytest" in joined and "check_" not in joined) else 0)

    monkeypatch.setattr(ph.subprocess, "run", _run)
    with pytest.raises(SystemExit):
        ph.step_quality_gate()
    joined = [" ".join(str(x) for x in c) for c in ran]
    assert not any("check_skips.py" in j for j in joined)
    assert not any("check_coverage.py" in j for j in joined)
    assert "報告不完整" in capsys.readouterr().out


def test_a_red_check_aborts_the_push(ph, monkeypatch):
    monkeypatch.setattr(ph.importlib.util, "find_spec", lambda _n: object())
    monkeypatch.setattr(ph.subprocess, "run",
                        lambda cmd, **k: _rc(1 if "ruff" in " ".join(
                            str(x) for x in cmd) else 0))
    with pytest.raises(SystemExit) as ei:
        ph.step_quality_gate()
    assert ei.value.code == 1


def test_the_gate_cleans_up_its_own_artifacts(ph, monkeypatch, tmp_path):
    """junit.xml / cov.json 是關卡的中間產物，不可留在工作區被 `git add -A` 收進去。"""
    monkeypatch.setattr(ph, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(ph.importlib.util, "find_spec", lambda _n: object())
    for name in ph.GATE_ARTIFACTS:
        (tmp_path / name).write_text("x", encoding="utf-8")
    monkeypatch.setattr(ph.subprocess, "run", lambda cmd, **k: _rc(0))
    ph.step_quality_gate()
    for name in ph.GATE_ARTIFACTS:
        assert not (tmp_path / name).exists(), f"{name} 沒有被清掉"


# ─── 緊急旁路：可以繞，但要留痕 ───────────────────────────────────────────
def test_parse_args_reads_the_commit_message(ph):
    assert ph.parse_args(["prog", "hello", "world"]) == ("hello world", "")


def test_emergency_requires_a_reason(ph):
    """★不用寫理由的旁路開關，用起來跟「預設就跳過」沒兩樣★"""
    with pytest.raises(SystemExit):
        ph.parse_args(["prog", "msg", "--emergency"])
    with pytest.raises(SystemExit):
        ph.parse_args(["prog", "msg", "--emergency", "   "])


def test_emergency_reason_is_not_swallowed_into_the_commit_message(ph):
    msg, reason = ph.parse_args(["prog", "hotfix", "--emergency", "診間全掛"])
    assert msg == "hotfix"
    assert reason == "診間全掛"


def test_emergency_skips_the_checks_but_says_so_loudly(ph, monkeypatch, capsys):
    monkeypatch.setattr(ph.importlib.util, "find_spec", lambda _n: None)
    ran = []
    monkeypatch.setattr(ph.subprocess, "run",
                        lambda cmd, **k: ran.append(cmd) or _rc(0))
    ph.step_quality_gate("診間全掛，先推修正")     # 缺工具也不中止
    out = capsys.readouterr().out
    assert ran == []
    assert "緊急模式" in out
    assert "診間全掛，先推修正" in out


def test_the_emergency_reason_is_recorded_in_the_commit(ph, monkeypatch,
                                                       tmp_path):
    """★只在終端印一行，關掉視窗就沒了★
    寫進 commit 訊息後，`git log --grep` 一查就知道哪幾版是未經本機關卡的。
    """
    written = {}
    monkeypatch.setattr(ph, "REPO_ROOT", tmp_path)
    (tmp_path / ".git").mkdir()

    real_write = ph.Path.write_text

    def _spy(self, data, **kw):
        written["msg"] = data
        return real_write(self, data, **kw)

    monkeypatch.setattr(ph.Path, "write_text", _spy)
    monkeypatch.setattr(ph, "run", lambda *a, **k: _rc(0))
    ph.step5_commit("hotfix", "2026.07.31.1", "診間全掛，pytest 環境同時壞掉")

    assert "hotfix" in written["msg"]
    assert "緊急推送" in written["msg"]
    assert "診間全掛，pytest 環境同時壞掉" in written["msg"]


def test_a_normal_commit_has_no_emergency_marker(ph, monkeypatch, tmp_path):
    written = {}
    monkeypatch.setattr(ph, "REPO_ROOT", tmp_path)
    (tmp_path / ".git").mkdir()
    real_write = ph.Path.write_text

    def _spy(self, data, **kw):
        written["msg"] = data
        return real_write(self, data, **kw)

    monkeypatch.setattr(ph.Path, "write_text", _spy)
    monkeypatch.setattr(ph, "run", lambda *a, **k: _rc(0))
    ph.step5_commit("normal", "2026.07.31.1")
    assert "緊急推送" not in written["msg"]


# ─── 接線：main() 真的把旗標傳下去 ────────────────────────────────────────
def test_main_threads_the_emergency_flag_through():
    """★寫了旗標但沒接上＝旁路永遠不生效／或關卡永遠被繞★"""
    src = io.open(PUSH_HELPER, encoding="utf-8").read()
    tree = ast.parse(src)
    main_fn = next(n for n in tree.body
                   if isinstance(n, ast.FunctionDef) and n.name == "main")
    body = ast.unparse(main_fn)
    assert "parse_args(argv)" in body
    assert "step_quality_gate(emergency_reason)" in body
    assert "emergency_reason)" in body.split("step5_commit")[1][:60]


def test_the_gate_no_longer_contains_the_silent_skip():
    """★舊寫法不可以再出現★

    ★要用 ast 剝掉 docstring 與註解★：函式的 docstring 本身就在【引用舊寫法】
    解釋為什麼不能那樣做，直接對原始碼比對會被自己的說明騙過去
    （這個坑本輪已經踩過幾次）。
    """
    tree = ast.parse(io.open(PUSH_HELPER, encoding="utf-8").read())
    fn = next(n for n in tree.body
              if isinstance(n, ast.FunctionDef) and n.name == "step_quality_gate")
    body = list(fn.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)):
        body.pop(0)                       # docstring
    fn.body = body or [ast.Pass()]
    code = ast.unparse(fn)                # unparse 也會丟掉註解
    assert "continue" not in code, "缺工具不可以 continue（那就是靜默略過）"
    assert "CI 仍會把關" not in code


def test_type_debt_is_documented_as_ci_only():
    """型別債棘輪刻意不進本機關卡（要跑 11 次 pyright，本機約 10 分鐘）——
    但那個取捨要寫下來，否則下一個人會以為是漏掉了。"""
    src = io.open(PUSH_HELPER, encoding="utf-8").read()
    assert "型別債棘輪" in src
    assert "只在 CI 跑" in src


def test_the_install_hint_targets_this_interpreter(ph, monkeypatch, capsys):
    """★[外審第 1 輪] 修復指令要指名【這個】解釋器★

    工具有沒有裝是用 `find_spec`（＝跑這支腳本的直譯器）判斷的，檢查也全走
    `sys.executable`。本機常裝了好幾個 Python，裸的 `pip` 很可能屬於另一個 ——
    使用者照著裝完，這裡依舊查不到，於是每次 push 都被擋而且不知道為什麼。
    一個「照著做也修不好」的 fail-closed 關卡，最後只會被 --emergency 常態繞過。
    """
    import sys as _sys

    monkeypatch.setattr(ph.importlib.util, "find_spec", lambda _n: None)
    with pytest.raises(SystemExit):
        ph.step_quality_gate()
    out = capsys.readouterr().out
    assert _sys.executable in out, "安裝指令沒有指名目前的解釋器"
    assert "-m pip install" in out
    hint = next(ln for ln in out.splitlines() if "pip install" in ln)
    assert not hint.strip().startswith("請先裝：pip install"), \
        "不可給裸的 `pip install`（可能裝到別的 Python）"
