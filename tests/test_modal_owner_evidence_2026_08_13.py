# -*- coding: utf-8 -*-
"""[批次AE-2/#89] P2-06 modal owner-chain ★觀測版★(只記、不改行為)。

enforcement 在沒有實機 owner 鏈數據前就上,會重演 2026-08-05 的
「fail-closed 無出口」—— Delphi modal 的 owner 常是 TApplication 隱藏
視窗,不一定是主畫面。先記證據,有數據再定規則。

觀測程式碼自己的安全性也要釘住:
* ★不讀視窗文字★(病人資料在文字裡;class 名是程式結構,不是 PHI);
* ★永不拋★(觀測失敗不可以影響按鈕流程);
* ★同形狀只記一次★(dismiss 迴圈每 0.4 秒跑;2026-07-29 的 1,568 行
  實機 log 教訓);
* owner 成環/超長鏈都要有界。
"""
import ast
import importlib
import io
import logging
import os
import sys

import pytest

NL = chr(10)
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

cq = importlib.import_module("consult_query")

SRC = io.open(os.path.join(REPO_ROOT, "src", "consult_query.py"),
              encoding="utf-8").read()


def _fn_node(name):
    for n in ast.walk(ast.parse(SRC)):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"找不到 {name}")


@pytest.fixture(autouse=True)
def _fresh_throttle():
    cq._reported_owner_chains.clear()
    yield
    cq._reported_owner_chains.clear()


def _fake_win(monkeypatch, owners, classes, enabled=None, visible=None):
    en = dict(enabled or {})
    vis = dict(visible or {})
    monkeypatch.setattr(cq.win32gui, "GetClassName",
                        lambda h: classes[h])
    monkeypatch.setattr(cq.win32gui, "IsWindowVisible",
                        lambda h: vis.get(h, True))
    monkeypatch.setattr(cq.win32gui, "IsWindowEnabled",
                        lambda h: en.get(h, True))
    monkeypatch.setattr(cq.win32gui, "GetWindow",
                        lambda h, f: owners.get(h, 0))


class TestTheChainWalk:
    def test_a_chain_is_walked_to_the_top(self, monkeypatch):
        _fake_win(monkeypatch,
                  owners={100: 200, 200: 300, 300: 0},
                  classes={100: "TFMTimeOut_1", 200: "TApplication",
                           300: "TFMNewMain"},
                  enabled={300: False})
        desc, shape = cq._owner_chain_evidence(100, 300)
        assert shape.split(";")[0] == "TFMTimeOut_1|TApplication|TFMNewMain"
        assert "main_in_chain=True" in shape, (
            "★main 狀態要進節流形狀★ unknown→known 是新的證據")
        assert "main_in_chain=True" in desc, (
            "enforcement 的候選判準就是這個 —— 觀測要記得出來")
        assert "main_disabled=True" in desc, (
            "modal 擋住時主畫面是 disabled —— 這是第二個候選判準")

    def test_a_foreign_chain_says_so(self, monkeypatch):
        _fake_win(monkeypatch, owners={100: 0},
                  classes={100: "TFMShowMessage"})
        desc, _shape = cq._owner_chain_evidence(100, 999)
        assert "main_in_chain=False" in desc

    def test_an_unknown_main_is_not_recorded_as_false(self, monkeypatch):
        """★外審 AE-2 第 1 輪 P2★ 登入路徑呼叫時不帶 session ——
        「不知道 main 在哪」記成 main_in_chain=False 是錯誤的候選判準。"""
        _fake_win(monkeypatch, owners={100: 0}, classes={100: "TX"})
        desc, _shape = cq._owner_chain_evidence(100, 0)
        assert "main=unknown" in desc
        assert "main_in_chain=False" not in desc, (
            "★把 unknown 講成 False★ 實機數據會留下錯的判準")

    def test_evidence_upgrades_when_the_main_becomes_known(
            self, monkeypatch, caplog):
        """★外審 AE-2 第 1 輪 P2(下半)★ 同一種 class 鏈,main 從 unknown
        變成 known 是【新的】證據 —— 節流形狀要含 main 狀態,第二筆
        不可以被第一筆壓掉。"""
        import types
        _fake_win(monkeypatch, owners={100: 300, 300: 0},
                  classes={100: "TX", 300: "TFMNewMain"},
                  enabled={300: False})
        with caplog.at_level(logging.INFO):
            cq._note_modal_owner_evidence(100, "TX", None)        # 登入路徑
            cq._note_modal_owner_evidence(
                100, "TX", types.SimpleNamespace(main_hwnd=300))  # 正常 session
        hits = [r for r in caplog.records if "[modal-evidence]" in r.message]
        assert len(hits) == 2, (
            "★main 變成 known 的證據被 unknown 那筆壓掉★ 觀測永遠缺關鍵欄位")
        assert "main_in_chain=True" in hits[1].message

    def test_an_owner_cycle_terminates_without_duplicates(self, monkeypatch):
        """Win32 沒保證 owner 鏈無環 —— 成環要停,不可以繞到跳數上限。"""
        _fake_win(monkeypatch, owners={100: 200, 200: 100},
                  classes={100: "A", 200: "B"})
        desc, shape = cq._owner_chain_evidence(100, 0)
        assert shape.split(";")[0] == "A|B", "★owner 成環沒有停★"
        assert desc.count(hex(100)) == 1, "同一個視窗不可以重複出現"

    def test_the_hop_cap_bounds_a_long_chain(self, monkeypatch):
        _fake_win(monkeypatch,
                  owners={i: i + 1 for i in range(1, 21)},
                  classes={i: f"C{i}" for i in range(1, 22)})
        _desc, shape = cq._owner_chain_evidence(1, 0)
        assert len(shape.split("|")) == cq._OWNER_CHAIN_MAX_HOPS, (
            "★超長鏈沒有截斷★ 觀測程式自己不可以變成風險")

    def test_win32_failures_never_raise(self, monkeypatch):
        def _boom(*a):
            raise OSError("window is gone")
        monkeypatch.setattr(cq.win32gui, "GetClassName", _boom)
        monkeypatch.setattr(cq.win32gui, "IsWindowVisible", _boom)
        monkeypatch.setattr(cq.win32gui, "IsWindowEnabled", _boom)
        monkeypatch.setattr(cq.win32gui, "GetWindow", _boom)
        desc, _shape = cq._owner_chain_evidence(100, 300)
        assert "?" in desc, "查不到 class 用占位符,不是拋例外"

    def test_the_note_swallows_even_evidence_bugs(self, monkeypatch):
        monkeypatch.setattr(
            cq, "_owner_chain_evidence",
            lambda *a: (_ for _ in ()).throw(RuntimeError("bug")))
        cq._note_modal_owner_evidence(100, "TX", None)   # 不可拋


class TestTheThrottle:
    def test_the_same_shape_is_logged_once(self, monkeypatch, caplog):
        _fake_win(monkeypatch, owners={100: 0}, classes={100: "TX"})
        with caplog.at_level(logging.INFO):
            cq._note_modal_owner_evidence(100, "TX", None)
            cq._note_modal_owner_evidence(100, "TX", None)
        hits = [r for r in caplog.records if "[modal-evidence]" in r.message]
        assert len(hits) == 1, (
            "★沒有節流★ dismiss 迴圈每 0.4 秒跑一次,log 會被同一句洗掉")

    def test_a_new_shape_is_logged_again(self, monkeypatch, caplog):
        _fake_win(monkeypatch, owners={100: 0, 200: 0},
                  classes={100: "TX", 200: "TY"})
        with caplog.at_level(logging.INFO):
            cq._note_modal_owner_evidence(100, "TX", None)
            cq._note_modal_owner_evidence(200, "TY", None)
        hits = [r for r in caplog.records if "[modal-evidence]" in r.message]
        assert len(hits) == 2


class TestObservationOnly:
    def test_no_window_text_is_read(self):
        """★病人資料在視窗文字裡★ 觀測只准碰 class/hwnd/vis/en。"""
        banned_attrs = {"GetWindowText", "GetWindowTextW", "SendMessage"}
        banned_names = {"_window_texts", "get_window_text", "window_text",
                        "enum_children"}
        for fn in ("_owner_chain_evidence", "_note_modal_owner_evidence"):
            node = _fn_node(fn)
            for m in ast.walk(node):
                if isinstance(m, ast.Attribute):
                    assert m.attr not in banned_attrs, (
                        f"★{fn} 讀了視窗文字★ {m.attr}")
                if isinstance(m, ast.Name):
                    assert m.id not in banned_names, (
                        f"★{fn} 讀了視窗文字★ {m.id}")

    def test_the_dismiss_loop_is_wired(self):
        node = _fn_node("_dismiss_blocking_modals")
        calls = {m.func.id for m in ast.walk(node)
                 if isinstance(m, ast.Call) and isinstance(m.func, ast.Name)}
        assert "_note_modal_owner_evidence" in calls, (
            "★觀測版沒接進 dismiss 迴圈★ wired or it doesn't exist")

    def test_evidence_is_taken_before_any_click(self):
        """按下去對話框就消失了(2026-08-10 教訓)—— 證據要在按之前記。"""
        node = _fn_node("_dismiss_blocking_modals")
        seg = ast.get_source_segment(SRC, node) or ""
        assert seg.index("_note_modal_owner_evidence(") \
            < seg.index("enum_children"), "★證據記在按鈕流程之後★"


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
