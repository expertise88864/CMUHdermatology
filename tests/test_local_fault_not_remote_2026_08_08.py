# -*- coding: utf-8 -*-
"""[任務 #49 P2-01] 我們自己的解析器壞掉，不可以去懲罰健康的遠端主機。

`classify_*_html` 在 bs4/lxml 載不進來或炸掉時回 `parser_unavailable`。
那個分支的註解**自己就寫著**「解析器壞掉不代表頁面壞掉」—— 但它回的是
`SEMANTIC_INVALID`，而呼叫端據此：

    _source_backoff_fail(source_key)   → 指數退避，把主機擋掉數十分鐘
    _circuit_record_fail(source)       → 連續幾次就熔斷跳閘

於是本機環境一壞（換 Python、lxml wheel 沒裝好、相依衝突），**四個來源會被
一起擋掉**，整個 reg52 功能變暗，而 log 上寫的是「東區主機連續失敗」——
查的人往遠端查，查不到東西。宣稱與實作不符，而且指向錯的地方。

修法：第四態 `LOCAL_ERROR`。`ok` 仍是 False、`usable_html` 仍是空字串
（**絕對不可以拿那份 html 去用**），只是 `blames_remote` 為 False。

★兩個方向都要守★
  * 該記卻不記 → 維護頁每一輪都再打一次（P1-02 修過的那個洞會復活）。
  * 不該記卻記 → 就是這個 P2-01。
"""
import importlib

import pytest

contract = importlib.import_module("cmuh_common.reg52_contract")
fetch = importlib.import_module("cmuh_common.reg52_fetch")

_GOOD = "<div class='visitDate'>x</div>" + "。" * 600
_BAD = "<html><body>系統維護中</body></html>" + "。" * 600


class _Resp:
    def __init__(self, text):
        self.text = text
        self.encoding = "big5"

    def raise_for_status(self):
        pass


class _Sess:
    def __init__(self, text):
        self.text = text
        self.calls = 0

    def get(self, url, **kw):
        self.calls += 1
        return _Resp(self.text)


@pytest.fixture(autouse=True)
def _no_penalties(monkeypatch):
    """把 backoff／熔斷器換成可觀測的記錄器，並清掉狀態。"""
    seen = {"backoff": [], "circuit": [], "ok_backoff": [], "ok_circuit": []}
    monkeypatch.setattr(fetch, "_source_backoff_allow", lambda k: (True, 0.0))
    monkeypatch.setattr(fetch, "_source_backoff_fail",
                        lambda k, *a: seen["backoff"].append(k) or (1.0, 1))
    monkeypatch.setattr(fetch, "_source_backoff_success",
                        lambda k: seen["ok_backoff"].append(k))
    monkeypatch.setattr(fetch, "_circuit_is_tripped", lambda s: False)
    monkeypatch.setattr(fetch, "_circuit_record_fail",
                        lambda s: seen["circuit"].append(s) or False)
    monkeypatch.setattr(fetch, "_circuit_record_success",
                        lambda s: seen["ok_circuit"].append(s))
    # ★stub 要吃得下生產的呼叫形狀★ `_cache_get(key, ttl, evict_expired=…)`
    monkeypatch.setattr(fetch, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(fetch, "_cache_set", lambda *a, **k: None)
    fetch._SEEN = seen
    yield seen


def _break_the_parser(monkeypatch):
    """讓 `from bs4 import BeautifulSoup` 這一行炸掉（模擬本機環境壞了）。"""
    import builtins
    real = builtins.__import__

    def _imp(name, *a, **k):
        if name == "bs4":
            raise ImportError("lxml/bs4 裝壞了")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _imp)


# ── 契約層 ────────────────────────────────────────────────────────────────
def test_a_broken_parser_is_a_local_error(monkeypatch):
    _break_the_parser(monkeypatch)
    out = contract.classify_branch_html(_GOOD)
    assert out.status == contract.LOCAL_ERROR
    assert out.reason == "parser_unavailable"


def test_a_local_error_still_must_not_be_used(monkeypatch):
    """★不記在遠端頭上 ≠ 這份資料可以用★ 我們根本沒能確認它是不是掛號表。"""
    _break_the_parser(monkeypatch)
    out = contract.classify_branch_html(_GOOD)
    assert out.ok is False
    assert out.usable_html == ""


def test_usable_html_only_ever_yields_success_content():
    """★這條才真的釘得住 `usable_html`★

    上面那條 `test_a_local_error_still_must_not_be_used` 走的是生產路徑，
    而生產路徑建 LOCAL_ERROR 時【根本不帶 html】—— 所以把 `usable_html` 的
    判準放寬（例如改成「只擋 TRANSPORT_ERROR」），那條測試照樣綠：
    反例被前面的條件先擋住了，量不到這個守衛。
    這裡直接構造一個【帶 html】的非成功結果，勝負只由這條規則決定。
    """
    for st in (contract.LOCAL_ERROR, contract.SEMANTIC_INVALID,
               contract.TRANSPORT_ERROR):
        out = contract.FetchOutcome(st, html="<div/>", reason="x")
        assert out.usable_html == "", f"{st} 的內容不可以被拿去用"
    ok = contract.FetchOutcome(contract.SUCCESS, html="<div/>")
    assert ok.usable_html == "<div/>", "★反方向★ 成功的內容要拿得到"


def test_a_local_error_does_not_blame_the_remote(monkeypatch):
    _break_the_parser(monkeypatch)
    assert contract.classify_branch_html(_GOOD).blames_remote is False


@pytest.mark.parametrize("mk,why", [
    (lambda: contract.FetchOutcome(contract.SEMANTIC_INVALID,
                                   reason="page_too_short"), "維護頁"),
    (lambda: contract.transport_error("Timeout"), "連不上"),
])
def test_real_remote_failures_still_blame_the_remote(mk, why):
    """★反方向★ 真的是遠端的問題就要記，否則壞頁每一輪都再打一次。"""
    assert mk().blames_remote is True, why


def test_success_never_blames_anyone():
    assert contract.classify_branch_html(_GOOD).blames_remote is False
    assert contract.classify_branch_html(_GOOD).ok is True


def test_a_local_error_describes_itself_as_local(monkeypatch):
    _break_the_parser(monkeypatch)
    text = contract.classify_branch_html(_GOOD).describe()
    assert "本機" in text, f"log 要說得出是我們自己的問題：{text}"


# ── 抓取層：四個來源 ──────────────────────────────────────────────────────
# 生產的函式是私有的、簽章是 `(session, doc_no, doctor_name)`（亞大是
# `(session, doctor_name)` 且醫師必須在 AUH_DOCTOR_DOCNO_MAP 裡）——
# ★測試要用生產的呼叫形狀★，猜一個好看的公開名字會測到不存在的東西。
_AUH_DOCTOR = next(iter(fetch.AUH_DOCTOR_DOCNO_MAP))


def test_east_does_not_penalise_the_host_when_our_parser_breaks(
        monkeypatch, _no_penalties):
    _break_the_parser(monkeypatch)
    got = fetch._fetch_east_district_reg52_html(_Sess(_GOOD), "0001", "甲醫師")
    assert not got, "沒能判定就不可以拿來用"
    assert _no_penalties["backoff"] == [], (
        "★我們自己的解析器壞掉，卻把東區主機記進退避★")
    assert _no_penalties["circuit"] == [], (
        "★我們自己的解析器壞掉，卻把東區主機記進熔斷器★")


def test_east_still_penalises_a_maintenance_page(monkeypatch, _no_penalties):
    """★反方向★ 真的是壞頁就要記 —— 不記的話每一輪都再打一次。"""
    got = fetch._fetch_east_district_reg52_html(_Sess(_BAD), "0001", "甲醫師")
    assert not got
    assert _no_penalties["backoff"], "維護頁沒有被記進退避（P1-02 的洞復活）"
    assert _no_penalties["circuit"], "維護頁沒有被記進熔斷器"


def test_east_success_is_unaffected(monkeypatch, _no_penalties):
    got = fetch._fetch_east_district_reg52_html(_Sess(_GOOD), "0001", "甲醫師")
    assert got, "正常頁沒有被採用"
    assert _no_penalties["ok_backoff"] and _no_penalties["ok_circuit"]


def test_auh_has_no_parser_failure_mode_at_all(monkeypatch, _no_penalties):
    """★亞大根本不解析★ —— 所以它沒有「本機解析器壞掉」這條路。

    我第一版在這裡寫了「解析器壞掉不可以懲罰遠端」，結果測試紅在
    `got == ""` —— 因為 `classify_auh_html` 是**純文字標記**判定
    （`AUH_REQUIRED_MARKERS`），從頭到尾沒碰 BeautifulSoup。那條測試在測一條
    不存在的路，通過與否都不代表任何事。改成釘住真正的性質。
    """
    _break_the_parser(monkeypatch)
    got = fetch._fetch_auh_reg52_html(_Sess(_GOOD), _AUH_DOCTOR)
    assert got, "亞大不需要解析器，解析器壞掉不該影響它"
    assert _no_penalties["backoff"] == [] and _no_penalties["circuit"] == []


def test_if_auh_ever_starts_parsing_the_local_branch_becomes_required():
    """★守衛要跟著程式碼走★

    `_fetch_auh_reg52_html` 裡的 `not outcome.blames_remote` 分支目前是
    **防禦性的**（亞大不會回 LOCAL_ERROR）。哪天有人把 `classify_auh_html`
    改成用 BeautifulSoup，這條會紅 —— 提醒去補「本機錯不記遠端」的測試，
    而不是讓那個分支繼續掛著一個沒人驗過的宣稱。
    """
    import ast
    import inspect
    src = inspect.getsource(contract.classify_auh_html)
    assert "BeautifulSoup" not in src, (
        "classify_auh_html 開始解析了 → 請補亞大的 LOCAL_ERROR 測試")
    import textwrap
    fsrc = textwrap.dedent(inspect.getsource(fetch._fetch_auh_reg52_html))
    names = {n.attr for n in ast.walk(ast.parse(fsrc))
             if isinstance(n, ast.Attribute)}
    assert "blames_remote" in names, (
        "亞大的分流分支被拿掉了 —— 將來加解析器就會直接誤罰遠端")


def test_auh_still_penalises_a_maintenance_page(monkeypatch, _no_penalties):
    got = fetch._fetch_auh_reg52_html(_Sess(_BAD), _AUH_DOCTOR)
    assert got == ""
    assert _no_penalties["backoff"], "亞大維護頁沒被記進退避"
    assert _no_penalties["circuit"], "亞大維護頁沒被記進熔斷器"


@pytest.mark.parametrize("fn,name", [
    ("_fetch_huihe_reg52_html", "惠和"),
    ("_fetch_huisheng_reg52_html", "惠盛"),
])
def test_the_other_branches_behave_the_same(monkeypatch, _no_penalties,
                                            fn, name):
    """★逐一盤點，不是「有一個對就算過」★ 四個來源走的是四段複製的程式碼。"""
    f = getattr(fetch, fn)
    _break_the_parser(monkeypatch)
    f(_Sess(_GOOD), "0001", "甲醫師")
    assert _no_penalties["backoff"] == [], f"{name}：本機問題卻懲罰遠端"
    assert _no_penalties["circuit"] == [], f"{name}：本機問題卻記熔斷"


# ★惠和【本來就】沒有熔斷器★（`_circuit_record_fail` 只出現在 east／huisheng／
#   auh 三處）。那是這次修改【之前】就存在的不對稱，不是本批造成的 —— 這裡照
#   實寫成期望值，不假裝它有；另開待辦追。把它寫成「應該有」會讓這支測試從第一
#   天就紅，然後被加 skip，那道守衛就永遠不會再看一眼。
_HAS_CIRCUIT = {"_fetch_huihe_reg52_html": False,
                "_fetch_huisheng_reg52_html": True}


@pytest.mark.parametrize("fn,name", [
    ("_fetch_huihe_reg52_html", "惠和"),
    ("_fetch_huisheng_reg52_html", "惠盛"),
])
def test_the_other_branches_still_penalise_bad_pages(monkeypatch,
                                                     _no_penalties, fn, name):
    f = getattr(fetch, fn)
    f(_Sess(_BAD), "0001", "甲醫師")
    assert _no_penalties["backoff"], f"{name}：維護頁沒被記進退避"
    if _HAS_CIRCUIT[fn]:
        assert _no_penalties["circuit"], f"{name}：維護頁沒被記進熔斷器"


def test_the_circuit_breaker_coverage_is_recorded_honestly():
    """★不對稱要被寫下來，不是被忽略★

    上面那張 `_HAS_CIRCUIT` 表記的是【現況】。哪天有人幫惠和補上熔斷器，
    這條會紅 —— 那正是要的：提醒把表改掉，而不是讓它默默失準。
    """
    import ast
    import inspect
    src = inspect.getsource(fetch)
    sources = set()
    for n in ast.walk(ast.parse(src)):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "_circuit_record_fail" and n.args
                and isinstance(n.args[0], ast.Constant)):
            sources.add(n.args[0].value)
    assert sources == {"east", "huisheng", "auh"}, (
        f"熔斷器覆蓋的來源變了，`_HAS_CIRCUIT` 要同步更新：{sorted(sources)}")


def test_every_parser_unavailable_site_returns_local_error():
    """★三個 classify 都要改到★ 只改一個等於另外兩個仍然會誤罰遠端。

    用 AST 找 `FetchOutcome(...)` 裡 reason="parser_unavailable" 的呼叫，
    逐一檢查第一個位置引數是 `LOCAL_ERROR`（不掃字串 —— 註解裡本來就寫滿了
    這幾個字，掃字串會被自己的散文餵飽）。
    """
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(contract))
    sites = []
    for n in ast.walk(tree):
        if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "FetchOutcome"):
            continue
        reasons = [k.value.value for k in n.keywords
                   if k.arg == "reason" and isinstance(k.value, ast.Constant)]
        if "parser_unavailable" in reasons:
            first = n.args[0] if n.args else None
            sites.append(getattr(first, "id", None))
    assert sites, "找不到任何 parser_unavailable 的出口（測試自己失效了）"
    assert set(sites) == {"LOCAL_ERROR"}, (
        f"有 parser_unavailable 出口仍然算在遠端頭上：{sites}")
