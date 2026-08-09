# -*- coding: utf-8 -*-
"""`scripts/ci_pytest_annotations.py` —— 讓 CI 的 pytest 失敗讀得到。

★為什麼要有這支腳本★（2026-08-09 實測）
v2026.08.09.2 的 CI `test` 紅了，而本機把 CI 每一步逐條重現全綠。要修就得知道
是哪一支測試 —— 但 `check-runs/<id>/annotations` 只回「exit code 1」，
`actions/jobs/<id>/logs` 匿名讀是 403（要 admin）。
**一道看不到原因的紅燈，最後就是被忽略的紅燈。**

★這支腳本自己也是一道「報告用」的守衛，所以同樣不可以安靜失效★
它的失效模式是：junit.xml 不見了／解析不了／沒有 failure 節點時什麼都不印 ——
那樣紅燈依舊沒有原因。下面逐條測那幾種情況都有話說。
"""
import os
import subprocess
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPT = os.path.join(REPO_ROOT, "scripts", "ci_pytest_annotations.py")


def _run(path):
    cp = subprocess.run([sys.executable, SCRIPT, str(path)],
                        cwd=REPO_ROOT, capture_output=True,
                        encoding="utf-8", errors="replace")
    return cp.returncode, cp.stdout


def _junit(tmp_path, cases):
    body = "".join(cases)
    p = tmp_path / "junit.xml"
    p.write_text(f'<?xml version="1.0"?><testsuites><testsuite>{body}'
                 f'</testsuite></testsuites>', encoding="utf-8")
    return p


def _case(name, file_="tests/t.py", line="41", kind="failure", msg="boom"):
    return (f'<testcase file="{file_}" line="{line}" name="{name}">'
            f'<{kind} message="{msg}">trace</{kind}></testcase>')


def test_a_failure_becomes_an_annotation(tmp_path):
    rc, out = _run(_junit(tmp_path, [_case("test_x")]))
    assert rc == 0
    assert "::error " in out and "tests/t.py" in out and "test_x" in out


def test_the_line_number_is_converted_to_one_based(tmp_path):
    """junit 的 `line` 是 0-based，annotation 是 1-based。差一行就指到隔壁。"""
    _rc, out = _run(_junit(tmp_path, [_case("test_x", line="41")]))
    assert "line=42" in out, out


def test_every_failure_is_emitted(tmp_path):
    """★外審 P3★ 不可以在第 N 筆截斷 —— 全紅時正是最需要看清楚的時候。

    第一版寫 `_MAX = 20`，而 docstring 說的是「逐筆」。宣稱與實作不符。
    """
    n = 37
    cases = [_case(f"test_{i}") for i in range(n)]
    _rc, out = _run(_junit(tmp_path, cases))
    for i in range(n):
        assert f"test_{i}::" in out or f": test_{i}::" in out, f"漏了 test_{i}"


def test_a_summary_annotation_lists_every_failing_test(tmp_path):
    """★總表放第一則★ GitHub 的 UI 只顯示前幾則；總表要帶得出完整清單。"""
    cases = [_case(f"test_{i}") for i in range(5)]
    _rc, out = _run(_junit(tmp_path, cases))
    first = out.strip().splitlines()[0]
    assert "pytest 失敗 5 筆" in first, first
    for i in range(5):
        assert f"test_{i}" in first, f"總表漏了 test_{i}：{first}"


def test_errors_count_too(tmp_path):
    """collection error 也是 `<error>` 節點 —— 不可以只認 `<failure>`。"""
    _rc, out = _run(_junit(tmp_path, [_case("test_e", kind="error")]))
    assert "test_e" in out


def test_a_missing_report_is_not_silence(tmp_path):
    """★沒有報告本身就是要知道的事★（pytest 可能連 collection 都沒過）"""
    rc, out = _run(tmp_path / "nope.xml")
    assert rc == 0
    assert "::error" in out and "collection" in out


def test_a_corrupt_report_is_not_silence(tmp_path):
    p = tmp_path / "junit.xml"
    p.write_text("<not xml", encoding="utf-8")
    rc, out = _run(p)
    assert rc == 0
    assert "::error" in out and "解析失敗" in out


def test_a_report_with_no_failures_says_so(tmp_path):
    """紅燈但 junit 裡沒有失敗 → 要說「原因不在這裡」，不可以什麼都不印。"""
    p = _junit(tmp_path, ['<testcase file="tests/t.py" name="ok"/>'])
    _rc, out = _run(p)
    assert "::error" in out and "沒有任何 failure" in out


def test_messages_never_contain_a_raw_newline(tmp_path):
    """annotation 的訊息含裸換行會被切斷成兩則，後半段變成無意義的雜訊。"""
    p = tmp_path / "junit.xml"
    p.write_text('<?xml version="1.0"?><testsuites><testsuite>'
                 '<testcase file="tests/t.py" line="1" name="test_n">'
                 '<failure message="a">line1\nline2\nline3</failure>'
                 '</testcase></testsuite></testsuites>', encoding="utf-8")
    _rc, out = _run(p)
    bodies = [ln for ln in out.splitlines() if ln.startswith("::error")]
    assert len(bodies) == 2, f"應該是總表 + 1 筆：{out}"
    assert "line1" in bodies[1] and "line2" in bodies[1]


def test_it_never_changes_the_job_verdict(tmp_path):
    """★報告器不可以自己決定成敗★

    workflow 用 `if: failure()` 呼叫它，pytest 自己的 exit code 才是判準。
    這支若回非零，就會把「報告」變成第二個判準 —— 而且是個沒人預期的判準。
    """
    for path in (tmp_path / "nope.xml", _junit(tmp_path, [_case("t")])):
        rc, _out = _run(path)
        assert rc == 0, f"{path} 讓報告器回了非零"


def test_the_workflow_calls_it_only_on_failure():
    """★接上去了才算數，而且要接對條件★

    接成 `always()` 的話，綠燈的那一輪也會跑它，然後印出「沒有任何 failure」
    的 error annotation —— 一則假的紅色註記，久了就沒人看 annotation 了。
    """
    with open(os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml"),
              encoding="utf-8") as fh:
        text = fh.read()
    assert "ci_pytest_annotations.py" in text, "workflow 沒有呼叫它"
    idx = text.index("ci_pytest_annotations.py")
    window = text[max(0, idx - 400):idx]
    step = window.rsplit("- name:", 1)[-1]
    assert "if: failure()" in step, f"呼叫條件不是 failure()：{step}"


@pytest.mark.parametrize("bad_line", ["", "abc", None])
def test_a_missing_or_bad_line_number_still_annotates(tmp_path, bad_line):
    """行號壞掉不可以整筆消失 —— 至少要指到那個檔。"""
    attr = "" if bad_line is None else f' line="{bad_line}"'
    p = tmp_path / "junit.xml"
    p.write_text('<?xml version="1.0"?><testsuites><testsuite>'
                 f'<testcase file="tests/t.py"{attr} name="test_b">'
                 '<failure message="m">t</failure>'
                 '</testcase></testsuite></testsuites>', encoding="utf-8")
    _rc, out = _run(p)
    assert "test_b" in out and "tests/t.py" in out
