# -*- coding: utf-8 -*-
"""[2026-07-27 事故防護] push_helper 防還原檢查。

OneDrive 兩次（v2026.07.24.4 / v2026.07.27.4）在【pytest 綠燈之後、git add 之前】
把未提交的 src 還原成舊版 → 關卡驗新碼、commit 進舊碼，HEAD 自相矛盾且已推上線。
本檔釘住：指紋快照排除 bump 目標、內容變動必中止、無變動必放行、且真的接在
commit 之前呼叫。
"""
import importlib.util
import os
import sys

import pytest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, os.path.join(_ROOT, "src"))


def _load():
    path = os.path.join(_ROOT, "scripts", "push_helper.py")
    spec = importlib.util.spec_from_file_location("push_helper_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_snapshot_covers_sources_and_excludes_bump_target():
    ph = _load()
    snap = ph.snapshot_tracked_sources()
    assert "src/cmuh_common/roster/solve_day.py" in snap, "src 應納入指紋"
    assert "tests/test_push_helper_antirevert.py" in snap, "tests 應納入指紋"
    # version.py 由 bump 在關卡之後合法改寫 → 必須排除，否則每次推送都誤報
    assert "src/cmuh_common/version.py" not in snap


def test_content_change_aborts_push(capsys):
    """關卡後檔案內容變動 → 中止（fail 走 SystemExit），且點名該檔案。"""
    ph = _load()
    before = ph.snapshot_tracked_sources()
    tampered = dict(before)
    tampered["src/cmuh_common/roster/solve_day.py"] = "0" * 64
    with pytest.raises(SystemExit):
        ph.verify_unchanged_since_tests(tampered)
    out = capsys.readouterr().out
    assert "solve_day.py" in out and "已中止推送" in out


def test_new_or_deleted_file_also_aborts():
    """新增/消失的檔案同樣算變動（還原可能整檔刪除或帶回舊檔）。"""
    ph = _load()
    before = ph.snapshot_tracked_sources()
    missing = {k: v for k, v in before.items()
               if k != "src/cmuh_common/roster/solve_day.py"}
    with pytest.raises(SystemExit):
        ph.verify_unchanged_since_tests(missing)


def test_unchanged_passes():
    ph = _load()
    snap = ph.snapshot_tracked_sources()
    ph.verify_unchanged_since_tests(snap)          # 不得拋出


def test_guard_runs_before_commit_in_main():
    """順序守衛：驗證必須排在 commit 之前——否則等於沒防到（舊碼照樣進 commit）。"""
    import inspect

    ph = _load()
    src = inspect.getsource(ph.main)
    i_snap = src.index("snapshot_tracked_sources()")
    i_verify = src.index("verify_unchanged_since_tests(")
    i_commit = src.index("step5_commit(")
    i_gate = src.index("step_quality_gate()")
    assert i_gate < i_snap < i_verify < i_commit, \
        "指紋須於品質關卡後取樣、commit 前重驗"
