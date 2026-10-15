# -*- coding: utf-8 -*-
"""[2026-10-16] 型別債棘輪的 11 條規則改成同時跑 —— 只改「何時跑」，不改「跑什麼」。

CI 實測循序 11 趟 pyright 要 139–308 秒，讓 Windows job 逼近 45 分鐘上限而被取消。
這裡逐條鎖住：每條規則恰好跑一次、各用自己的設定檔、結果與循序版逐字相同、
輸出與完成順序無關、任何一條失敗都讓關卡失敗、同時數量有上限。
pyright 本身換成假的（真跑要好幾分鐘），假的報告由「哪條規則被打開」決定。
"""
import contextlib
import io
import json
import os
import random
import sys
import threading
import time

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))

import type_debt  # noqa: E402

_RULES = sorted([
    "reportArgumentType", "reportAttributeAccessIssue", "reportCallIssue",
    "reportIndexIssue", "reportOperatorIssue", "reportOptionalMemberAccess",
    "reportOptionalOperand", "reportOptionalSubscript", "reportReturnType",
    "reportAssignmentType", "reportPossiblyUnbound",
])


def _fake_report(cfg: dict) -> dict:
    """依「被打開成 error 的規則」產生報告；也夾帶別的規則與非 error 診斷當干擾。"""
    enabled = [r for r in _RULES if cfg.get(r) == "error"]
    diags = []
    for rule in enabled:
        for i in range(len(rule) % 4 + 1):
            diags.append({"file": f"C:/x/repo/src/m{i}.py", "severity": "error",
                          "rule": rule, "message": f"{rule} problem {i}"})
        diags.append({"file": "C:/x/repo/src/w.py", "severity": "warning",
                      "rule": rule, "message": "only a warning"})
    diags.append({"file": "C:/x/repo/src/z.py", "severity": "error",
                  "rule": "reportSomethingAlwaysOn", "message": "not ours"})
    return {"generalDiagnostics": diags}


@pytest.fixture
def fake_pyright(monkeypatch):
    """把 `_run_pyright` 換掉；記錄每次呼叫的設定檔路徑、規則、同時數量。"""
    state = {"calls": [], "active": 0, "peak": 0, "seen_files": [],
             "lock": threading.Lock(), "fail": None, "delay": 0.02}
    rng = random.Random(1016)

    def run(cfg, what, config_path=type_debt.TEMP_CONFIG):
        enabled = [r for r in _RULES if cfg.get(r) == "error"]
        with state["lock"]:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
            state["calls"].append((tuple(enabled), config_path))
            delay = state["delay"] * rng.random()
        # 真的寫檔再刪，確認並行時檔案不會互相覆蓋或殘留
        with open(config_path, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh)
        try:
            time.sleep(delay)
            with open(config_path, encoding="utf-8") as fh:
                assert json.load(fh) == cfg, "設定檔被別的規則覆蓋了"
            if state["fail"] in enabled:
                raise RuntimeError(f"[type-debt] 無法解析 pyright 輸出（{what}）")
            return _fake_report(cfg)
        finally:
            os.remove(config_path)
            with state["lock"]:
                state["active"] -= 1

    monkeypatch.setattr(type_debt, "_run_pyright", run)
    return state


def test_parallel_results_equal_sequential_and_each_rule_runs_once(fake_pyright):
    sequential = type_debt._collect_rule_diagnostics(_RULES, jobs=1)
    fake_pyright["calls"].clear()
    parallel = type_debt._collect_rule_diagnostics(_RULES, jobs=4)
    assert parallel == sequential
    assert set(parallel) == set(_RULES)
    ran = [enabled for enabled, _path in fake_pyright["calls"]]
    assert sorted(ran) == sorted((r,) for r in _RULES), "每條規則恰好一次、各自單獨打開"
    assert all(sum(v.values()) > 0 for v in parallel.values())


def test_each_rule_uses_its_own_config_in_the_repo_root(fake_pyright):
    type_debt._collect_rule_diagnostics(_RULES, jobs=4)
    paths = [path for _enabled, path in fake_pyright["calls"]]
    assert len(set(paths)) == len(_RULES)
    for (enabled,), path in ((e, p) for e, p in fake_pyright["calls"]):
        # include 是相對設定檔位置解析的：必須留在 repo 根目錄
        assert os.path.dirname(path) == type_debt.REPO_ROOT
        assert os.path.basename(path) == f".pyright_type_debt.{enabled}.json"
    leftovers = [n for n in os.listdir(type_debt.REPO_ROOT)
                 if n.startswith(".pyright_type_debt")]
    assert leftovers == []


def test_concurrency_is_real_and_bounded(fake_pyright):
    fake_pyright["delay"] = 0.2
    type_debt._collect_rule_diagnostics(_RULES, jobs=4)
    assert 2 <= fake_pyright["peak"] <= 4
    fake_pyright["peak"] = 0
    type_debt._collect_rule_diagnostics(_RULES, jobs=1)
    assert fake_pyright["peak"] == 1


def test_the_first_rule_runs_alone_before_the_pool(fake_pyright):
    """一次性的 pyright／node 環境準備不可被多個行程同時觸發。"""
    fake_pyright["delay"] = 0.05
    type_debt._collect_rule_diagnostics(_RULES, jobs=4)
    assert fake_pyright["calls"][0][0] == (_RULES[0],)


def test_one_failing_rule_fails_the_ratchet(fake_pyright, monkeypatch):
    fake_pyright["fail"] = _RULES[5]
    with pytest.raises(RuntimeError):
        type_debt._collect_rule_diagnostics(_RULES, jobs=4)
    monkeypatch.setattr(type_debt, "_load_baseline",
                        lambda: {r: {} for r in _RULES})
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = type_debt._main_guarded([])
    assert rc == 1
    assert [n for n in os.listdir(type_debt.REPO_ROOT)
            if n.startswith(".pyright_type_debt")] == []


def test_output_does_not_depend_on_completion_order(fake_pyright, monkeypatch):
    baseline = type_debt._collect_rule_diagnostics(_RULES, jobs=1)
    # 製造「新增 + 消失」兩種輸出
    baseline[_RULES[0]] = {"src/old.py||gone|x = 1": 1}
    monkeypatch.setattr(type_debt, "_load_baseline", lambda: baseline)
    outputs = []
    for jobs in ("1", "4", "4", "3"):
        fake_pyright["delay"] = 0.1
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = type_debt.main(["--jobs", jobs])
        outputs.append((rc, buf.getvalue()))
    assert outputs[0][0] == 1, "新增的診斷仍然要紅"
    assert all(o == outputs[0] for o in outputs)


def test_jobs_must_be_positive(fake_pyright):
    with contextlib.redirect_stdout(io.StringIO()):
        assert type_debt.main(["--jobs", "0"]) == 2
    with pytest.raises(ValueError):
        type_debt._collect_rule_diagnostics(_RULES, jobs=0)
    assert fake_pyright["calls"] == []


def test_duplicate_rules_are_rejected(fake_pyright):
    with pytest.raises(ValueError):
        type_debt._collect_rule_diagnostics([_RULES[0], _RULES[0]], jobs=4)


def test_rule_names_cannot_escape_the_config_file_name():
    with pytest.raises(ValueError):
        type_debt._rule_config_path("../evil")


def test_ci_runs_the_authoritative_per_rule_path():
    """CI 那一步仍然是預設的逐條模式（不是 --fast），也沒有自行改成循序以外的語意。"""
    with open(os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml"),
              encoding="utf-8") as fh:
        text = fh.read()
    assert "run: python scripts/type_debt.py\n" in text
