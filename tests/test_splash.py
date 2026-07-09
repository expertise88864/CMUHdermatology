# -*- coding: utf-8 -*-
"""splash 啟動視窗（白框孤兒回歸測試, 2026-07-09）。

實機案例：另一台電腦開主程式後，桌面殘留一個白色無邊框置頂方框。根因：
show() 蓋內容中途失敗（破損 tk 的 ttk 元件建不出來等）時，self._top 尚未賦值
→ close() 變 no-op → 半成品白色 Toplevel 永遠留在桌面。修正為「建好才現身＋
失敗即銷毀＋close 有 withdraw fallback」。無顯示器則整檔跳過。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

try:
    import tkinter as tk
    from tkinter import ttk
    _r = tk.Tk()
    ttk.Progressbar(_r)
    _r.destroy()
    _HAS_TK = True
except Exception:
    _HAS_TK = False

pytestmark = pytest.mark.skipif(not _HAS_TK, reason="無可用顯示器/或 tk 安裝不完整")

from cmuh_common import splash as splash_mod  # noqa: E402
from cmuh_common.splash import StartupSplash  # noqa: E402


@pytest.fixture
def root():
    try:
        r = tk.Tk()
    except tk.TclError as e:
        pytest.skip(f"tk 建立失敗：{e}")
    r.withdraw()
    yield r
    try:
        r.destroy()
    except Exception:
        pass


def _visible_toplevels(r) -> list:
    out = []
    for w in r.winfo_children():
        try:
            if isinstance(w, tk.Toplevel) and w.winfo_viewable():
                out.append(w)
        except Exception:
            continue
    return out


def test_show_close_roundtrip_no_leftover(root):
    sp = StartupSplash(root, "載入中…")
    sp.show()
    assert sp._top is not None
    sp.close()
    root.update()
    assert sp._top is None
    assert _visible_toplevels(root) == []       # 不留任何可見殘窗


def test_show_partial_failure_leaves_no_visible_orphan(root, monkeypatch):
    """[白框根因] 蓋內容中途失敗（模擬破損 tk 的 Progressbar）→ 不得留下可見的
    白色殘窗；且 close() 照常可呼叫不炸。修正前：殘窗可見且 close 無法移除。"""
    def boom(*_a, **_k):
        raise tk.TclError("broken ttk theme")
    monkeypatch.setattr(splash_mod.ttk, "Progressbar", boom)
    sp = StartupSplash(root, "載入中…")
    sp.show()                                    # 內部失敗，不拋出
    root.update()
    assert _visible_toplevels(root) == []        # 半成品絕不可見
    sp.close()                                   # no-op 也不可炸


def test_close_destroy_failure_falls_back_to_withdraw(root, monkeypatch):
    """destroy 失敗 → withdraw fallback 至少把白框藏起來。"""
    sp = StartupSplash(root, "載入中…")
    sp.show()
    top = sp._top
    assert top is not None
    monkeypatch.setattr(top, "destroy",
                        lambda: (_ for _ in ()).throw(tk.TclError("stuck")))
    sp.close()
    root.update()
    assert not top.winfo_viewable()              # 已被 withdraw 藏起
    assert sp._top is None
    try:
        top.destroy()                            # 清理
    except Exception:
        pass


def test_top_assigned_immediately_source_guard():
    """守門：self._top 必須在 Toplevel 建立後立即賦值（close 永遠找得到），
    且 show 失敗路徑要銷毀殘窗——防止日後改回「最後才賦值」的舊寫法。"""
    import inspect
    src = inspect.getsource(StartupSplash.show)
    assert src.index("self._top = top") < src.index("top.withdraw()")
    assert "top.deiconify()" in src              # 蓋好才現身
