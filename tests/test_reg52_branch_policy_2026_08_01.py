# -*- coding: utf-8 -*-
"""[2026-08-01 P2-06 第四刀(c)] cmuh_common/reg52_branch_policy.py。

★「該不該去分院抓」與「怎麼抓」是兩個問題★
抓取與韌性在 `reg52_fetch`；這裡只回答前者。分開之後，改醫師名單不必碰抓取程式碼。

判斷有兩條路，任一成立就去抓：
  1. 主院 reg52 的回應本身提到了該分院（**動態**，醫師換診也跟得上）；
  2. 醫師在該分院的名單裡（**靜態**，主院頁面沒寫時的兜底）。

★兩條都要留★ 只靠動態會漏掉主院頁面沒提的情況；只靠名單則每次人事異動都要改程式。
這一檔把「兩條各自都足夠」釘住 —— 哪天有人把其中一條拿掉，會紅。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import reg52_branch_policy as pol  # noqa: E402


# ─── 東區：動態偵測 + 靜態名單，兩條各自都足夠 ────────────────────────────
def test_the_main_page_mentioning_the_branch_is_enough():
    """★動態這條★ 名單上沒有的醫師，只要主院頁面提到東區分院就要去抓 ——
    否則醫師換診之後，東區的人數與休診會整片消失，直到有人想起來改名單。"""
    assert pol._should_fetch_east_district_reg52(
        "…王小明 東區分院 …", "名單上沒有的醫師") is True


def test_being_on_the_list_is_enough():
    """★靜態這條★ 主院頁面沒提到分院時的兜底。"""
    someone = next(iter(pol.EAST_FH1_DOCTOR_NAMES))
    assert pol._should_fetch_east_district_reg52("完全沒提到分院", someone) is True


def test_neither_means_do_not_fetch():
    """★兩條都不成立就不要去打別人的主機★"""
    assert pol._should_fetch_east_district_reg52(
        "完全沒提到分院", "名單上沒有的醫師") is False


@pytest.mark.parametrize("html", [None, "", "   "])
def test_an_empty_main_page_does_not_count_as_a_mention(html):
    """★讀不到 ≠ 有提到★ 主院抓失敗時 html 可能是 None/空字串 ——
    那時只能靠名單，不可以因為「沒有證據說沒有」就去抓。"""
    assert pol._main_html_has_east_branch_clinic(html) is False
    assert pol._should_fetch_east_district_reg52(html, "名單外的人") is False


# ─── 惠和／惠盛：只有名單這條 ─────────────────────────────────────────────
def test_huihe_and_huisheng_are_list_only():
    """★三家【不是】同一個形狀 —— 連簽章都不同★

    東區是 `(html_main, doctor_name)`（動態＋靜態兩條路），惠和/惠盛只收
    `(doctor_name)` —— 主院頁面不會寫這兩家，所以名單是唯一依據。
    我寫這一檔時就照著「三家一致」的假設寫，被 `TypeError` 打回來 ——
    那正說明這個不對稱值得釘住。
    """
    for fn, names in ((pol._should_fetch_huihe_reg52, pol.HUIHE_DOCTOR_NAMES),
                      (pol._should_fetch_huisheng_reg52,
                       pol.HUISHENG_DOCTOR_NAMES)):
        someone = next(iter(names))
        assert fn(someone) is True
        assert fn("名單外的人") is False


def test_the_three_predicates_have_deliberately_different_signatures():
    """把不對稱本身釘住：哪天有人「統一」成三個都收 html，
    東區那條動態偵測就會被悄悄套到另外兩家（它們的主院頁面根本不會寫）。"""
    import inspect
    assert list(inspect.signature(
        pol._should_fetch_east_district_reg52).parameters) == [
        "html_main", "doctor_name"]
    for fn in (pol._should_fetch_huihe_reg52, pol._should_fetch_huisheng_reg52):
        assert list(inspect.signature(fn).parameters) == ["doctor_name"]


# ─── 名單本身 ──────────────────────────────────────────────────────────────
@pytest.mark.parametrize("names", ["EAST_FH1_DOCTOR_NAMES",
                                   "HUIHE_DOCTOR_NAMES",
                                   "HUISHENG_DOCTOR_NAMES"])
def test_the_lists_are_non_empty_and_contain_only_strings(names):
    """★空名單＝那家分院靜默地不再被抓★ 而且不會有任何錯誤訊息。"""
    value = getattr(pol, names)
    assert value, f"{names} 不可以是空的"
    assert all(isinstance(n, str) and n.strip() for n in value)


# ─── 搬家本身 ──────────────────────────────────────────────────────────────
def test_main_still_exposes_the_old_private_names():
    """★只搬家、不改呼叫端★（`check_appointment_count` 還在用這三個述詞）"""
    import main
    for name in ("_should_fetch_east_district_reg52", "_should_fetch_huihe_reg52",
                 "_should_fetch_huisheng_reg52",
                 "_get_thread_local_reg52_session"):
        assert callable(getattr(main, name)), f"{name} 不見了"


def test_the_main_hospital_session_getter_moved_next_to_its_twin():
    """主院與院外兩個 session getter 現在住在一起 —— 它們的差別（timeout、
    IPv4-only、retry 策略）擺在一起才看得出來。"""
    from cmuh_common import reg52_fetch
    assert callable(reg52_fetch._get_thread_local_reg52_session)
    assert callable(reg52_fetch._get_thread_local_reg52_external_session)


def test_the_policy_module_has_no_fetching_in_it():
    """★分層要真的分開★ 政策模組不該碰 HTTP —— 混進去就等於沒分。"""
    import ast
    import inspect
    code = ast.unparse(ast.parse(inspect.getsource(pol)))
    for banned in ("requests", "session", "urlopen", "timeout"):
        assert banned not in code.lower(), f"政策層不該出現 {banned}"
