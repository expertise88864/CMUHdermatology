# -*- coding: utf-8 -*-
"""W6(2026-07-03):會診重試前清理只殺『本任務期間新出現的』systemftp PID
(= 目前 PID − 任務開始前快照),絕不再 taskkill /IM 全機(會殺掉使用者手動開的住院
系統)。使用者既有實例在 before 快照中 → 永不誤殺;before=None 或無孤兒 → 不動作。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402


def test_kill_systemftp_no_longer_kills_anything(monkeypatch):
    """★[2026-08-04 外審第 3 輪 P1-06] 這支原本斷言「只殺新孤兒」★

    它守的性質是「絕不碰任務前就存在的實例」。現在守得更強：**什麼都不殺**。

    原因是「任務期間新出現」並不等於「是我們的」—— 醫師在本次任務執行中手動開的
    住院系統也不在 before 快照裡，於是會落進差集被 `taskkill /F`。而這條路會在任何
    可重試錯誤前執行，包括寄信失敗：

        會診查完 → 醫師手動開 HIS → SMTP timeout → 醫師的 HIS 被強殺

    改成收掉【我們自己的 session】（對確切主畫面送 WM_CLOSE 並回讀確認），
    差集只留作證據。
    """
    calls = []
    monkeypatch.setattr(cq, "_systemftp_pids", lambda: {100, 200, 300})
    monkeypatch.setattr(cq, "_session_close", lambda _w: None)
    monkeypatch.setattr(cq.subprocess, "run",
                        lambda args, **k: calls.append(args))

    cq._kill_systemftp(before_pids={100})

    assert calls == [], f"★仍然會強殺行程★：{calls}"


def test_cleanup_closes_our_own_session(monkeypatch):
    """不殺行程，但要收掉自己的 session —— 否則重試會撞上自己上一輪的 wedged 實例。"""
    closed = []
    monkeypatch.setattr(cq, "_systemftp_pids", lambda: {100, 200})
    monkeypatch.setattr(cq, "_session_close", lambda why: closed.append(why))

    cq._kill_systemftp(before_pids={100})

    assert closed, "沒有收掉自己的 session"


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
