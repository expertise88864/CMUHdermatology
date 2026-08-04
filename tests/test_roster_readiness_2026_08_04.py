# -*- coding: utf-8 -*-
"""病人清單要等它不再變動，不能只睡固定秒數（2026-08-04 外審 P1-03）。

【問題】
`_query_cycle` 只 `time.sleep(1.8)` 就當清單載入完了。Delphi 視窗是先建立、資料
再逐步填進去的，那一秒八【不保證】看到的是最終狀態：

  * 還沒載入 → 空清單被當成「成功且真的沒有病人」→ 基準被剪成空
                → 下一輪所有既有會診都變「新」→ ★對團隊重寄整份清單★
  * 載入到一半 → partial roster 被存成基準 → 還沒出現的病人此後不算新 → ★漏寄★

固定睡多久都治不了（慢的機器仍會失手）。要看的是【內容有沒有還在變】。

【修法】連續讀到相同才算穩定；逾時仍在變 → `roster_texts` 回 None，走既有的
「判斷不了 → fail-open 照寄、但不更新基準」通道。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402


def _scripted(reads):
    """把一連串「每次讀到什麼」腳本化。用完後一直回最後一筆。"""
    seq = list(reads)

    def _read(_hwnd):
        return seq.pop(0) if len(seq) > 1 else seq[0]
    return _read


class TestLoadingIsNotMistakenForEmpty:

    def test_a_roster_that_appears_late_is_waited_for(self):
        """★審查點名的情境★ 視窗先出現、2 秒後才有 radio。"""
        reads = [[], [], ["甲C16(1)1111111"], ["甲C16(1)1111111"],
                 ["甲C16(1)1111111"]]
        got, stable = cq._await_stable_roster(
            1, read=_scripted(reads), sleep=lambda _s: None)

        assert stable is True
        assert got == ["甲C16(1)1111111"], f"沒等到載入完成：{got}"

    def test_a_roster_that_grows_is_waited_for(self):
        """先出現 2 位、稍後增至 4 位 —— 不可以拿 2 位那份去更新基準。"""
        two = ["甲1111111", "乙2222222"]
        four = two + ["丙3333333", "丁4444444"]
        got, stable = cq._await_stable_roster(
            1, read=_scripted([two, four, four, four]),
            sleep=lambda _s: None)

        assert stable is True and got == four, f"拿到 partial roster：{got}"

    def test_a_genuinely_empty_roster_is_accepted(self):
        """★反方向:真的沒有病人也要判得出來★

        否則沒有病人的時段永遠「判斷不了」→ 每輪都 fail-open 寄信，變成天天洗信箱。
        """
        got, stable = cq._await_stable_roster(
            1, read=_scripted([[], [], [], []]), sleep=lambda _s: None)

        assert stable is True and got == [], (
            "空清單也要能被判定成穩定，否則沒病人時每輪都會寄信")

    def test_an_endlessly_changing_roster_is_reported_unstable(self,
                                                               monkeypatch):
        """一直在變 → 逾時 → 回報判斷不了（而不是把當下那份當真）。"""
        n = {"i": 0}

        def _read(_hwnd):
            n["i"] += 1
            return [f"病人{n['i']}"]        # 每次都不一樣

        ticks = {"t": 0.0}

        def _mono():
            ticks["t"] += 1.0
            return ticks["t"]
        monkeypatch.setattr(cq.time, "monotonic", _mono)

        _got, stable = cq._await_stable_roster(
            1, read=_read, sleep=lambda _s: None)

        assert stable is False, "一直在變卻回報穩定"


class TestTheUnstableRosterGoesDownTheFailOpenChannel:
    """不穩定要走既有的 `roster_texts is None` 通道。

    那條路的語意已經是「無法判斷有沒有新會診 → fail-open 照常寄信、且【不更新
    基準】」——正是我們要的處置，呼叫端不需要新增任何處理。
    """

    def test_none_means_do_not_touch_the_baseline(self):
        """釘住那個通道的語意（它是本修法的整個依據）。"""
        assert cq._consult_signature_from_roster(None) == set()

    def test_extract_returns_none_when_unstable(self, monkeypatch):
        """接線:不穩定時 `_extract_consult_text` 的第三個回傳必須是 None。"""
        monkeypatch.setattr(cq, "_await_stable_roster",
                            lambda *a, **k: (["甲1111111"], False))
        monkeypatch.setattr(cq, "enum_children", lambda _h: [])
        monkeypatch.setattr(cq, "_is_visible_below", lambda *a: True)
        monkeypatch.setattr(cq, "_find_patient_radios", lambda _c: [])
        monkeypatch.setattr(cq, "_find_text_panes", lambda _c: [])

        _t, _h, roster_texts = cq._extract_consult_text(1, {})

        assert roster_texts is None, (
            "★不穩定卻回報成有效清單★ 基準會被一份還在變的資料更新")

    def test_extract_returns_the_list_when_stable(self, monkeypatch):
        """★反方向:穩定時要正常回報★ 否則基準永遠不更新、每輪都重寄。"""
        monkeypatch.setattr(cq, "_await_stable_roster",
                            lambda *a, **k: (["甲1111111"], True))
        monkeypatch.setattr(cq, "enum_children", lambda _h: [])
        monkeypatch.setattr(cq, "_is_visible_below", lambda *a: True)
        monkeypatch.setattr(cq, "_find_patient_radios", lambda _c: [])
        monkeypatch.setattr(cq, "_find_text_panes", lambda _c: [])

        _t, _h, roster_texts = cq._extract_consult_text(1, {})

        assert roster_texts == ["甲1111111"]


def test_the_extraction_actually_waits():
    """★接線本身也要被測到★（本 session 這個形狀第七次）

    上面幾支直接呼叫 `_await_stable_roster`。若 `_extract_consult_text` 仍然
    單次讀取，它們照樣全綠 —— 而那正是 bug 還在的樣子。
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(cq._extract_consult_text)))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_await_stable_roster" in called, (
        "擷取仍然單次讀清單 → 載入中的空/半份清單會被當成有效資料")
