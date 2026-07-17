# -*- coding: utf-8 -*-
"""會診查詢：隱藏桌面 systemftp 孤兒累積自癒（2026-07-17 實機故障）。

根因：更新重啟/硬退遺留在隱藏桌面的 systemftp 孤兒(已登入、隱形)佔滿『最多兩個』
上限 → 新實例撞多開提示、開不出登入視窗 → 每次 poll「等不到登入視窗」。啟動清掃只在
啟動跑一次抓不到運行期新增的孤兒、重試的 _kill_systemftp 又只殺本次新增 → 永久卡死。
修法：①三次都失敗放棄後清一次孤兒(_cleanup_orphan_systemftp,只殺使用者桌面無可見視窗
者＝隱藏桌面殘留,絕不動使用者手動開的住院系統)讓下一輪自癒;②失敗訊息點名疑似多開上限。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402


def test_give_up_triggers_orphan_cleanup():
    # 放棄分支(else)須呼叫 _cleanup_orphan_systemftp 讓下一輪自癒
    src = inspect.getsource(cq._do_full_job)
    give_up = src[src.index("已重試 %d 次仍失敗"):]
    assert "_cleanup_orphan_systemftp()" in give_up, \
        "三次都失敗放棄後應清一次隱藏桌面孤兒(自癒),否則卡死到重啟"


def test_login_wait_message_names_multi_instance_cap():
    # 關過多開提示卻等不到登入 → 訊息點名疑似多開上限(便於判別根因)
    src = inspect.getsource(cq._automation_on_hidden)
    assert "saw_multi_instance" in src
    assert "最多兩個" in src, "關過多開提示的失敗訊息應點名 systemftp 多開上限"


def test_orphan_cleanup_kills_only_pids_on_hidden_desktop(monkeypatch):
    # [codex] 正面識別:只殺「本 session ∩ 確實在隱藏桌面上有視窗」者。
    # 100=使用者住院系統(在使用者桌面,不在隱藏桌面)、200/300=隱藏桌面孤兒。
    monkeypatch.setattr(cq, "_systemftp_pids", lambda: {100, 200, 300})
    monkeypatch.setattr(cq, "_pid_session", lambda pid: 1)
    monkeypatch.setattr(cq.os, "getpid", lambda: 999)
    monkeypatch.setattr(cq, "_hidden_desktop_pids", lambda: {200, 300, 777})
    closed = {}
    monkeypatch.setattr(cq, "close_pids", lambda pids, **k: closed.update(pids=set(pids)))
    cq._cleanup_orphan_systemftp()
    # 只殺「本 session systemftp」∩「隱藏桌面」= {200,300};100(不在隱藏桌面)、
    # 777(在隱藏桌面但非本 session systemftp)都不動。
    assert closed.get("pids") == {200, 300}


def test_orphan_cleanup_spares_user_process_regardless_of_window_transition(monkeypatch):
    # [codex] 正解對時間免疫:使用者手動開/正在啟動(登入視窗尚未出現、可能 windowless
    # 超過任何秒數)的住院系統只要不在隱藏桌面,就【永不】被殺 —— 不靠「暫無視窗」推斷。
    monkeypatch.setattr(cq, "_systemftp_pids", lambda: {200, 500})   # 200=孤兒 500=使用者
    monkeypatch.setattr(cq, "_pid_session", lambda pid: 1)
    monkeypatch.setattr(cq.os, "getpid", lambda: 999)
    monkeypatch.setattr(cq, "_hidden_desktop_pids", lambda: {200})   # 只有 200 在隱藏桌面
    closed = {}
    monkeypatch.setattr(cq, "close_pids", lambda pids, **k: closed.update(pids=set(pids)))
    cq._cleanup_orphan_systemftp()
    assert closed.get("pids") == {200}, "不在隱藏桌面的使用者行程永不被殺(無時間競態)"


def test_orphan_cleanup_skips_when_no_session_id(monkeypatch):
    # 取不到本 session id → 保守整個跳過(多使用者/RDS 防誤殺其他 session)
    monkeypatch.setattr(cq, "_pid_session", lambda pid: None)
    called = {"hidden": False, "close": False}
    monkeypatch.setattr(cq, "_hidden_desktop_pids",
                        lambda: called.update(hidden=True) or set())
    monkeypatch.setattr(cq, "close_pids", lambda *a, **k: called.update(close=True))
    cq._cleanup_orphan_systemftp()
    assert called["close"] is False


def test_orphan_cleanup_noop_when_hidden_desktop_empty(monkeypatch):
    # 隱藏桌面上沒有任何視窗(取不到桌面/無孤兒)→ 不殺任何一個(fail-safe)
    monkeypatch.setattr(cq, "_systemftp_pids", lambda: {100, 200})
    monkeypatch.setattr(cq, "_pid_session", lambda pid: 1)
    monkeypatch.setattr(cq.os, "getpid", lambda: 999)
    monkeypatch.setattr(cq, "_hidden_desktop_pids", lambda: set())
    closed = {"called": False}
    monkeypatch.setattr(cq, "close_pids", lambda *a, **k: closed.update(called=True))
    cq._cleanup_orphan_systemftp()
    assert closed["called"] is False


def test_hidden_desktop_pids_returns_empty_when_desktop_unavailable(monkeypatch):
    # OpenDesktopW 回 0(取不到隱藏桌面)→ 回空集合(保守,上層不殺)
    monkeypatch.setattr(cq._user32, "OpenDesktopW", lambda *a, **k: 0)
    assert cq._hidden_desktop_pids() == set()
