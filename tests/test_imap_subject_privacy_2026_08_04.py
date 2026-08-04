# -*- coding: utf-8 -*-
"""IMAP debug 不可以把信件主旨原文寫進 log（2026-08-04 外審 P2-05，實機證實）。

診間 `consult_query.log` 裡這行一天出現 **3850 次**：

    （最近未讀主旨樣本…）：'Microsoft Outlook 測試郵件' | '安全性快訊' | …

那個信箱收到的【任何】信件主旨都會被寫進 log。這次撈到的剛好無害，但機制是確認
的 —— 只要收到含病人姓名或床號的信，那些字就會進到一個沒有 Email 保存政策的
log 檔。

★診斷價值幾乎沒有損失★：這行的用途是「確認觸發信有沒有進收件匣」，真正回答那件
事的是 `matched` 的數字；而使用者本來就打得開那個信箱。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.imap_reader import _subject_fingerprint  # noqa: E402


def test_a_patient_name_never_appears_in_the_fingerprint():
    """★這就是要擋的東西★"""
    subject = "王小明 1234567 病理報告 C16(18A)"
    fp = _subject_fingerprint(subject)

    for leaked in ("王小明", "1234567", "C16", "18A", "病理"):
        assert leaked not in fp, f"指紋洩漏了 {leaked!r}：{fp}"


def test_the_fingerprint_is_stable_across_polls():
    """同一封信每輪要算出同一個指紋 —— 否則看不出「收件匣有沒有在變動」。"""
    assert _subject_fingerprint("同一封信") == _subject_fingerprint("同一封信")


def test_different_subjects_get_different_fingerprints():
    """不同的信要分得出來，否則這行 log 完全沒有資訊。"""
    assert _subject_fingerprint("信件甲") != _subject_fingerprint("信件乙")


def test_the_length_is_kept_because_it_is_not_identifying():
    """長度留著（幫助辨認「是不是我那封」），但長度本身不足以還原內容。"""
    assert "len=5" in _subject_fingerprint("12345")


def test_whitespace_only_and_empty_are_handled():
    assert "len=0" in _subject_fingerprint("")
    assert "len=0" in _subject_fingerprint("   ")
    assert "len=0" in _subject_fingerprint(None)


def test_the_caller_no_longer_formats_raw_samples():
    """★接線本身也要被測到★

    `_subject_fingerprint` 再安全，呼叫端若仍把原文塞進 log 也是白搭。
    這裡確認 scheduler 那段不再用 `repr(s)` 逐一輸出主旨。
    """
    import inspect
    import consult_query as cq

    src = inspect.getsource(cq._scheduler_loop) if hasattr(
        cq, "_scheduler_loop") else inspect.getsource(cq)
    i = src.index("最近未讀")
    seg = src[i - 400:i + 400]
    assert "repr(s) for s in" not in seg, (
        "呼叫端仍把主旨原文逐一寫進 log")
    assert "主旨樣本" not in seg, (
        "訊息仍宣稱在給主旨樣本 —— 措辭要對得上實際內容")


def test_the_collector_stores_fingerprints_not_raw_subjects():
    """★收集端也要被測到★（突變驗證抓到，本 session 第四次同一形狀）

    上面驗了指紋函式本身、也驗了呼叫端的格式化，但沒有任何一支確認
    `check_trigger` 真的把【指紋】放進 samples —— 把收集那行改回存原文，
    它們照樣全綠，而 log 又會開始印主旨。

    `check_trigger` 要真的連 IMAP 才跑得起來，所以用 AST 檢查那個 append
    的引數確實經過 `_subject_fingerprint`。
    """
    import ast
    import inspect
    import textwrap

    from cmuh_common import imap_reader

    tree = ast.parse(textwrap.dedent(inspect.getsource(imap_reader.check_trigger)))
    guarded = None
    for node in ast.walk(tree):
        # 找 result["samples"].append(...)
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"):
            continue
        tgt = node.func.value
        if not (isinstance(tgt, ast.Subscript)
                and isinstance(tgt.slice, ast.Constant)
                and tgt.slice.value == "samples"):
            continue
        arg = node.args[0] if node.args else None
        guarded = (isinstance(arg, ast.Call)
                   and isinstance(arg.func, ast.Name)
                   and arg.func.id == "_subject_fingerprint")
    assert guarded is not None, "找不到 samples 的收集點（測試失效了）"
    assert guarded, "★samples 收的是主旨原文，不是指紋★"
