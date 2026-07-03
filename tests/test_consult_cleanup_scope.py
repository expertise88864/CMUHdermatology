# -*- coding: utf-8 -*-
"""W6(2026-07-03):會診重試前清理只殺『本任務期間新出現的』systemftp PID
(= 目前 PID − 任務開始前快照),絕不再 taskkill /IM 全機(會殺掉使用者手動開的住院
系統)。使用者既有實例在 before 快照中 → 永不誤殺;before=None 或無孤兒 → 不動作。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402


def test_kill_systemftp_only_new_orphans(monkeypatch):
    """目前 {100,200,300},任務前 {100} → 只殺任務期間新增的 {200,300}(不碰 100)。"""
    calls = []
    monkeypatch.setattr(cq, "_systemftp_pids", lambda: {100, 200, 300})
    monkeypatch.setattr(cq.subprocess, "run",
                        lambda args, **k: calls.append(args))
    cq._kill_systemftp(before_pids={100})
    assert len(calls) == 1
    args = calls[0]
    assert "/IM" not in args and "systemftp.exe" not in args   # 不全機掃殺
    assert "/PID" in args
    assert "200" in args and "300" in args
    assert "100" not in args                                    # 使用者既有實例不動


def test_kill_systemftp_noop_when_no_new(monkeypatch):
    """任務期間沒有新增(目前 ⊆ before)→ 不動作。"""
    calls = []
    monkeypatch.setattr(cq, "_systemftp_pids", lambda: {100})
    monkeypatch.setattr(cq.subprocess, "run",
                        lambda args, **k: calls.append(args))
    cq._kill_systemftp(before_pids={100, 200})
    assert calls == []


def test_kill_systemftp_noop_when_before_none(monkeypatch):
    """未提供 before 快照 → fail-open 不動作(絕不誤殺)。"""
    calls = []
    monkeypatch.setattr(cq, "_systemftp_pids", lambda: {100, 200})
    monkeypatch.setattr(cq.subprocess, "run",
                        lambda args, **k: calls.append(args))
    cq._kill_systemftp(None)
    assert calls == []
