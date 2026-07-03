# -*- coding: utf-8 -*-
"""W7(2026-07-03):F2/F3 半套帳務精準對帳警告 —— UVB 已寫回但 51019/療程失敗時,
警告要精準列出原值→新值 + 兩個手動選項(補齊 or 改回),不自動 rollback(保守)。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main as m  # noqa: E402


def _capture_warning(monkeypatch):
    captured = {}
    monkeypatch.setattr(m, "_find_hospital_main_window", lambda: 1)
    monkeypatch.setattr(m, "_show_uvb_warning",
                        lambda hwnd, title, msg: captured.update(msg=msg))
    return captured


def test_fmt_uvb_dc_none_handling():
    assert "?" in m._fmt_uvb_dc(None, None)
    assert "未寫次數" in m._fmt_uvb_dc(500, None)
    out = m._fmt_uvb_dc(500, 10)
    assert "500" in out and "10" in out


def test_reconciliation_warning_shows_old_and_new(monkeypatch):
    cap = _capture_warning(monkeypatch)
    m._record_uvb_write("F2", 500, 10, 550, 11)
    m._show_light_code_incomplete_warning("F2", 2, uvb_already_updated=True)
    msg = cap["msg"]
    assert "已寫回" in msg
    assert "500" in msg and "550" in msg          # 原→新劑量都列出
    assert "(A)" in msg and "(B)" in msg           # 補齊 / 改回 兩選項
    assert "51019" in msg and "療程 2" in msg


def test_no_false_update_claim_when_no_uvb_change(monkeypatch):
    """本次沒有實際改動 UVB(_last_uvb_write=None)→ 不可誤稱『UVB 已更新』。"""
    cap = _capture_warning(monkeypatch)
    m._last_uvb_write = None
    m._show_light_code_incomplete_warning("F2", 2, uvb_already_updated=True)
    msg = cap["msg"]
    assert "已寫回" not in msg and "已改為" not in msg
    assert "51019" in msg


def test_reconciliation_only_for_matching_label(monkeypatch):
    """記錄是 F2 的,但警告是 F3 → 不套用(label 不符),避免張冠李戴。"""
    cap = _capture_warning(monkeypatch)
    m._record_uvb_write("F2", 500, 10, 550, 11)
    m._show_light_code_incomplete_warning("F3", 3, uvb_already_updated=True)
    msg = cap["msg"]
    assert "已改為" not in msg   # 不套用 F2 的記錄到 F3 警告
    assert "51019" in msg
