# -*- coding: utf-8 -*-
"""批次 7 其餘：RP3-18（定案 PDF 完成通知視窗已關閉不炸）、RP3-19（梯次日期正規化）。

RP3-18 用假 parent（after 拋 TclError）+ threading.excepthook 攔截，實測背景緒
不留未捕捉例外——不需顯示器。RP3-19 為 Tk modal 對話框（__init__ 內 wait_window
會阻塞、無法在測試建構），改以原始碼守門鎖住「存正規化 ISO」的修正。
"""
import os
import sys
import threading
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

ROOT = Path(__file__).resolve().parents[1]


# ─── RP3-18：完成通知時視窗已關閉 → after 拋 TclError 應被吞掉 ────────────────
def test_rp3_18_finalize_pdf_survives_closed_window(monkeypatch):
    import tkinter as tk

    import cmuh_common.deps_runtime as deps
    from cmuh_common.roster.ui import common as ui_common

    # 避免真的去安裝 reportlab（背景 work() 內 import 失敗時的 fallback）。
    monkeypatch.setattr(deps, "ensure_dependencies", lambda *a, **k: None)

    class _FakeParent:
        def after(self, *_a, **_k):
            raise tk.TclError("application has been destroyed")

    class _FakeSvc:
        def archive_finalize_pdf(self, ym):
            return "dummy.pdf"

    errors = []
    monkeypatch.setattr(threading, "excepthook", lambda a: errors.append(a))

    ui_common.archive_finalize_pdf_async(_FakeParent(), _FakeSvc(), "2026-08")
    for t in list(threading.enumerate()):
        if t.name == "finalize-pdf":
            t.join(timeout=5)
    assert not errors, "背景緒殘留未捕捉例外（after 的 TclError 未被吞）"


# ─── RP3-19：Clerk 梯次起始日以 d.isoformat() 正規化落檔 ─────────────────────
def test_rp3_19_clerk_batch_stores_normalized_iso():
    src = (ROOT / "src" / "cmuh_common" / "roster" / "ui"
           / "settings.py").read_text(encoding="utf-8")
    assert '"start_monday": d.isoformat()' in src, "梯次起始日未正規化落檔"
    assert '"start_monday": raw' not in src, "仍以未正規化 raw 落檔（回歸）"
