# -*- coding: utf-8 -*-
"""[2026-07-30 第二輪外審 P3-01] Ruff 全域忽略 B023。

舊設定是 `ignore = ["B023"]`,並在 ruff.toml 註明「本專案這些 closure 都在同一輪
迴圈內同步跑完,所以是誤報」。那個判斷【對當時的 18 處是對的】,但全域 ignore 有一個
致命性質:**它把未來也一起關掉了**。

舊註解自己就寫著「★未來若把這些 closure 改成延後/跨迴圈執行,務必移除此 ignore★」
—— 也就是把「記得回來拿掉」交給人的記性。而真正踩到 late-binding bug 的那一刻,
正是有人新寫了一個【延後執行】的 closure 的時候,那時 linter 完全不會出聲。

★這一檔最重要的一支是 `test_a_genuinely_deferred_closure_is_now_caught`★
把規則打開、舊處逐行 noqa,如果【新的】B023 還是不會紅,那這次改動就只是搬動雜訊。
"""
import io
import os
import re
import subprocess
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
RUFF_TOML = os.path.join(REPO_ROOT, "ruff.toml")


def _ruff(*args, cwd=REPO_ROOT) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "ruff", "check", *args],
        cwd=cwd, capture_output=True, encoding="utf-8", errors="replace",
        check=False)


# ─── 設定本身 ──────────────────────────────────────────────────────────────
def test_b023_is_no_longer_globally_ignored():
    """★核心★ 全域 ignore 會把【未來】也一起關掉。"""
    text = io.open(RUFF_TOML, encoding="utf-8").read()
    code = "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert "B023" not in code, (
        "ruff.toml 的設定區不可再出現 B023（那代表它又被整批關掉了）")
    assert re.search(r"^ignore\s*=\s*\[\s*\]", code, re.M), \
        "ignore 應該是空的"


def test_bugbear_is_still_selected():
    """把 B023 打開的前提是 B 規則集還在 —— 否則整組都沒在跑。"""
    text = io.open(RUFF_TOML, encoding="utf-8").read()
    code = "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))
    assert '"B"' in code


def test_the_reasoning_is_recorded_where_the_decision_lives():
    """判準要寫在設定檔裡 —— 逐處 noqa 只留指針，不重複論證。"""
    text = io.open(RUFF_TOML, encoding="utf-8").read()
    assert "P3-01" in text
    assert "loop 變數已經改變之後" in text, "要寫清楚什麼情況才算真 bug"
    assert "_v=v" in text, "要給出正確的修法（預設參數綁定）"


# ─── ★真正的驗收★ 新的延後 closure 會不會紅 ──────────────────────────────
_DEFERRED_SAMPLE = '''
callbacks = []
for value in range(3):
    def later():
        return value * 2      # 真的 late-binding：迴圈結束後才執行
    callbacks.append(later)
print([cb() for cb in callbacks])
'''

_SYNCHRONOUS_SAMPLE = '''
results = []
for value in range(3):
    def now():
        return value * 2
    results.append(now())     # 同一輪內就叫掉
print(results)
'''


def test_a_genuinely_deferred_closure_is_now_caught(tmp_path):
    """★這支才是這次改動的意義★

    規則打開之後，【新寫的】延後 closure 必須紅。若不紅，那這次只是把雜訊
    從 ruff.toml 搬到 18 行 noqa 而已。
    """
    f = tmp_path / "deferred.py"
    f.write_text(_DEFERRED_SAMPLE, encoding="utf-8")
    cp = _ruff("--config", RUFF_TOML, "--select", "B023", str(f), cwd=tmp_path)
    assert cp.returncode != 0, f"延後執行的 closure 應該被抓到\n{cp.stdout}"
    assert "B023" in cp.stdout


def test_a_synchronous_closure_also_trips_it_and_that_is_why_noqa_exists(
        tmp_path):
    """★誠實記錄工具的限制★

    B023 是【語法層】規則：它看不出 closure 有沒有在同一輪被叫掉，所以同步用法
    也會被標。這正是那 18 處需要 noqa 的原因 —— 不是規則沒用，是它只能提醒
    「這裡有個 closure 抓了 loop 變數，你自己確認一下」。
    """
    f = tmp_path / "sync.py"
    f.write_text(_SYNCHRONOUS_SAMPLE, encoding="utf-8")
    cp = _ruff("--config", RUFF_TOML, "--select", "B023", str(f), cwd=tmp_path)
    assert cp.returncode != 0
    assert "B023" in cp.stdout


def test_the_default_argument_fix_actually_silences_it(tmp_path):
    """ruff.toml 給的修法（預設參數綁定）要真的有效 —— 不然那句建議是空話。"""
    f = tmp_path / "bound.py"
    f.write_text('''
callbacks = []
for value in range(3):
    def later(_v=value):
        return _v * 2
    callbacks.append(later)
print([cb() for cb in callbacks])
''', encoding="utf-8")
    cp = _ruff("--config", RUFF_TOML, "--select", "B023", str(f), cwd=tmp_path)
    assert cp.returncode == 0, cp.stdout


# ─── 現存的 noqa ───────────────────────────────────────────────────────────
def _b023_noqa_lines() -> list:
    """全 repo 掃出所有 `# noqa: B023` 的行。

    ★掃描器要排除自己★：這一檔在【談論】noqa（掃描字串、docstring 舉例），
    不是在【使用】它。不排除的話這支測試會抓到自己而永遠紅 —— 又一次
    「比對原始碼被自己的說明騙過去」（本輪已經踩過好幾次）。
    """
    out = []
    for root, _dirs, files in os.walk(REPO_ROOT):
        # ★[2026-07-31] `.claude` 也要排除★
        #   spawned agent 會在 `.claude/worktrees/<名字>/` 開 git worktree ——
        #   那是【這個 repo 的另一份完整副本】。沒排除的話掃描器會把副本裡的
        #   noqa 當成本 repo 新增的，兩支守衛就同時誤紅（實際發生過）。
        #   `.gitignore` 有蓋到它，但 os.walk 不看 .gitignore。
        if any(part in root for part in
               (".git", ".claude", "__pycache__", "python_embed", ".venv")):
            continue
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            if os.path.abspath(path) == os.path.abspath(__file__):
                continue
            try:
                text = io.open(path, encoding="utf-8").read()
            except OSError:
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if "noqa: B023" in line:
                    out.append((os.path.relpath(path, REPO_ROOT), i, line))
    return out


def test_every_b023_noqa_carries_a_reason():
    """★裸的 noqa 等於小型的全域 ignore★ 沒有理由就沒人敢動它，也沒人知道它還成不成立。"""
    bare = [(p, i) for p, i, line in _b023_noqa_lines()
            if "見 ruff.toml" not in line]
    assert not bare, f"這些 noqa 沒有指向判斷依據：{bare}"


def test_the_noqa_sites_are_where_we_verified_them():
    """noqa 只該出現在 2026-07-30 逐處核對過的那三個檔案。

    多出來的檔案代表有人在別處加了 B023 抑制 —— 那需要重新判斷一次，
    不能靠這批既有的核對結果背書。
    """
    files = {p for p, _i, _line in _b023_noqa_lines()}
    expected = {
        os.path.join("src", "cmuh_common", "uvb_dose.py"),
        os.path.join("src", "main.py"),
        os.path.join("tests", "test_settings_overwrite_guard_2026_07_26.py"),
    }
    assert files == expected, (
        f"B023 noqa 出現在未核對過的檔案：{sorted(files - expected)}\n"
        f"（若確實是同步用法，請核對後把檔案加進這支測試的 expected）")


def test_the_repo_is_clean_with_b023_enabled():
    """規則打開之後整個 repo 要是綠的 —— 這是關卡真的能用的前提。"""
    cp = _ruff("--select", "B023", "src", "scripts", "tests")
    assert cp.returncode == 0, cp.stdout


@pytest.mark.parametrize("path", [
    os.path.join("src", "cmuh_common", "uvb_dose.py"),
    os.path.join("src", "main.py"),
])
def test_the_verified_sites_are_still_synchronous(path):
    """★噪音守衛：noqa 的前提要看得見★

    這兩個檔案的 B023 之所以是誤報，靠的是「submit 之後同一個 with 區塊內就
    .result()」與「re.sub 同步呼叫」。這裡釘住那兩個結構還在 —— 若哪天有人把
    join 拆掉改成跨迴圈收集，這支測試會紅，提醒回頭重判那些 noqa。
    """
    text = io.open(os.path.join(REPO_ROOT, path), encoding="utf-8").read()
    if path.endswith("main.py"):
        assert "fut_m.result()" in text and "fut_d.result()" in text, \
            "reg52 併發抓取不再是同一輪內 join → 那些 noqa 要重新判斷"
    else:
        assert "re.sub(" in text
