# -*- coding: utf-8 -*-
"""[2026-08-02 外部 code review 批次E / P1-02] HTTP 200 ≠ 資料有效。

★三個地方把「連得上」當成「內容可信」★

  亞大  ：缺掛號欄位只記一行 warning，之後照樣 `_cache_set` ＋
          `_source_backoff_success` ＋ `_circuit_record_success`
          —— **維護頁被當成健康的成功頁，還把熔斷器重置了**。
  三分院：`last_error` 只在 `RequestException` 時設定，語意失敗（內容太短、
          缺 `div.visitDate`／`table#dayoff`）完全不記 backoff／熔斷，
          於是每一輪都再打一次同一個壞頁。
  主院  ：`_cache_set` 在解析【之前】。壞頁進了快取之後，三次 retry 都拿同一份
          重解析，而且在 TTL 內一直有效 —— 每次都失敗，卻連一次重新連線都沒有。

修法是把狀態拆成三態（SUCCESS / TRANSPORT_ERROR / SEMANTIC_INVALID），
只有 SUCCESS 才可以寫 good cache、清 backoff、重置熔斷器。
"""
import ast
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import reg52_contract as rc  # noqa: E402
from cmuh_common import reg52_fetch as rf     # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

_GOOD_BRANCH = ("<html><body>" + "x" * 600
                + '<div class="visitDate">2026/08/03</div></body></html>')
_MAINTENANCE = ("<html><body>" + "系統維護中，造成不便敬請見諒。" * 40
                + "</body></html>")
_LOGIN_PAGE = ("<html><body>" + "請先登入" * 200 + "</body></html>")


# ─── 分類器 ───────────────────────────────────────────────────────────────
def test_a_real_schedule_page_is_success():
    got = rc.classify_branch_html(_GOOD_BRANCH)
    assert got.ok and got.usable_html == _GOOD_BRANCH


@pytest.mark.parametrize("body,reason", [
    ("", "page_too_short"),
    ("<html>短</html>", "page_too_short"),
    (_MAINTENANCE, "missing_schedule_markup"),
    (_LOGIN_PAGE, "missing_schedule_markup"),
])
def test_a_page_without_the_schedule_markup_is_semantic_invalid(body, reason):
    got = rc.classify_branch_html(body)
    assert got.status == rc.SEMANTIC_INVALID
    assert got.reason == reason


def test_an_invalid_outcome_never_hands_out_html():
    """★壞頁不可以被當成資料用★ `usable_html` 只有 SUCCESS 才有內容。"""
    got = rc.classify_branch_html(_MAINTENANCE)
    assert got.usable_html == ""


def test_the_outcome_never_carries_page_content():
    """維護頁可能夾帶任何東西，而 log 是會被整包交給開發者的。"""
    got = rc.classify_branch_html(_MAINTENANCE)
    blob = f"{got.reason} {got.describe()}"
    assert "維護" not in blob and "見諒" not in blob


# ─── 縱深防禦：就算有人把 outcome 建錯，也不可以漏出內容 ──────────────────
# 突變驗證抓到：分類器【從不】替 SEMANTIC_INVALID 填 `html`，所以
# `usable_html` 與 `describe()` 裡的防禦目前走不到 —— 拿掉也沒有測試轉紅。
# 它們擋的是【未來】：哪天有人為了「順便留個線索」而在無效結果上帶原文，
# 那一刻壞頁就會被當成資料用、原文也會進 log。所以直接建出那個不一致的狀態釘住。
def test_a_forged_invalid_outcome_still_hands_out_nothing():
    forged = rc.FetchOutcome(rc.SEMANTIC_INVALID, html="<html>維護中</html>",
                             reason="missing_schedule_markup", length=99)
    assert forged.usable_html == "", "★非 SUCCESS 一律不可以交出 html★"
    assert forged.ok is False


def test_a_forged_invalid_outcome_does_not_describe_its_content():
    forged = rc.FetchOutcome(rc.SEMANTIC_INVALID, html="系統維護中請見諒",
                             reason="missing_schedule_markup", length=8)
    assert "維護" not in forged.describe()


def test_auh_uses_its_own_contract():
    """亞大版型與分院不同，用文字標記判。"""
    assert rc.classify_auh_html("x" * 600 + "已掛號").ok
    assert rc.classify_auh_html("x" * 600 + "visitDate").ok
    bad = rc.classify_auh_html(_MAINTENANCE)
    assert bad.status == rc.SEMANTIC_INVALID
    assert bad.reason == "missing_booking_field"


def test_main_needs_parsed_slots():
    """主院要等解析完才判得出來 —— 版面看起來沒問題也可能一個時段都沒有。"""
    assert rc.classify_main_html("x" * 600, parsed_slots=3).ok
    got = rc.classify_main_html("x" * 600, parsed_slots=0)
    assert got.status == rc.SEMANTIC_INVALID and got.reason == "no_slots_parsed"


# ─── 分院 fetcher：語意失敗要記 backoff／熔斷 ─────────────────────────────
class _Resp:
    def __init__(self, text):
        self.text = text
        self.encoding = ""

    def raise_for_status(self):
        return None


class _Session:
    def __init__(self, text):
        self._text = text

    def get(self, url, **k):
        return _Resp(self._text)


@pytest.fixture
def resilience(monkeypatch):
    """記錄 backoff／熔斷器被怎麼呼叫。"""
    calls = {"backoff_ok": 0, "backoff_fail": 0,
             "circuit_ok": 0, "circuit_fail": 0}
    monkeypatch.setattr(rf, "_circuit_is_tripped", lambda s: False)
    monkeypatch.setattr(rf, "_source_backoff_allow", lambda k: (True, 0.0))
    monkeypatch.setattr(rf, "_source_backoff_success",
                        lambda k: calls.__setitem__("backoff_ok",
                                                    calls["backoff_ok"] + 1))
    monkeypatch.setattr(rf, "_source_backoff_fail",
                        lambda *a: (calls.__setitem__("backoff_fail",
                                                      calls["backoff_fail"] + 1),
                                    (0.0, 1))[1])
    monkeypatch.setattr(rf, "_circuit_record_success",
                        lambda s: calls.__setitem__("circuit_ok",
                                                    calls["circuit_ok"] + 1))
    monkeypatch.setattr(rf, "_circuit_record_fail",
                        lambda s: (calls.__setitem__("circuit_fail",
                                                     calls["circuit_fail"] + 1),
                                   False)[1])
    return calls


@pytest.mark.parametrize("fetch", ["_fetch_east_district_reg52_html",
                                   "_fetch_huihe_reg52_html",
                                   "_fetch_huisheng_reg52_html"])
def test_a_maintenance_page_counts_as_a_failure(fetch, resilience):
    """★核心★ 維護頁必須累加 backoff —— 否則每一輪都再打一次。"""
    got = getattr(rf, fetch)(_Session(_MAINTENANCE), "1234", "王醫師")
    assert got is None, "維護頁不可以被當成掛號表回傳"
    assert resilience["backoff_fail"] == 1, "★語意失敗也要記 backoff★"
    assert resilience["backoff_ok"] == 0, "不可以宣告成功"


@pytest.mark.parametrize("fetch", ["_fetch_east_district_reg52_html",
                                   "_fetch_huisheng_reg52_html"])
def test_a_maintenance_page_also_trips_the_breaker(fetch, resilience):
    """east / huisheng 有熔斷器（huihe 原本就沒有，這一刀不改它）。"""
    getattr(rf, fetch)(_Session(_MAINTENANCE), "1234", "王醫師")
    assert resilience["circuit_fail"] == 1
    assert resilience["circuit_ok"] == 0


@pytest.mark.parametrize("fetch", ["_fetch_east_district_reg52_html",
                                   "_fetch_huihe_reg52_html",
                                   "_fetch_huisheng_reg52_html"])
def test_a_good_page_still_succeeds(fetch, resilience):
    """反方向：正常的掛號表照樣成功、照樣清 backoff（不可矯枉過正）。"""
    got = getattr(rf, fetch)(_Session(_GOOD_BRANCH), "1234", "王醫師")
    assert got == _GOOD_BRANCH
    assert resilience["backoff_ok"] == 1
    assert resilience["backoff_fail"] == 0


# ─── 亞大：維護頁不可以進快取、不可以重置熔斷器 ──────────────────────────
def test_auh_maintenance_page_is_not_cached(monkeypatch, resilience):
    """★這是最嚴重的一處★ 原本缺欄位照樣 `_cache_set` + 重置熔斷器。"""
    cached = []
    monkeypatch.setattr(rf, "_cache_set",
                        lambda k, v: cached.append((k, v)))
    monkeypatch.setattr(rf, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(rf, "_get_thread_local_reg52_external_session",
                        lambda: _Session(_MAINTENANCE))
    got = rf._fetch_auh_reg52_html(None, "方心禹")
    assert cached == [], "★維護頁不可以進快取★"
    assert resilience["circuit_ok"] == 0, "★不可以重置熔斷器★"
    assert resilience["circuit_fail"] == 1
    assert resilience["backoff_fail"] == 1
    assert got == "", "沒有 stale 就回空字串"


def test_auh_falls_back_to_the_last_good_cache(monkeypatch, resilience):
    """★不可以覆蓋 good cache★ 壞頁把好資料蓋掉比抓不到還糟。"""
    monkeypatch.setattr(rf, "_cache_set",
                        lambda k, v: pytest.fail("不該寫快取"))
    monkeypatch.setattr(rf, "_cache_get",
                        lambda key, ttl, **k: "上一份好的 HTML"
                        if ttl == rf.REG52_STALE_CACHE_SECONDS else None)
    monkeypatch.setattr(rf, "_get_thread_local_reg52_external_session",
                        lambda: _Session(_MAINTENANCE))
    assert rf._fetch_auh_reg52_html(None, "方心禹") == "上一份好的 HTML"


def test_auh_good_page_is_cached(monkeypatch, resilience):
    good = "x" * 600 + "已掛號"
    cached = []
    monkeypatch.setattr(rf, "_cache_set", lambda k, v: cached.append(v))
    monkeypatch.setattr(rf, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(rf, "_get_thread_local_reg52_external_session",
                        lambda: _Session(good))
    assert rf._fetch_auh_reg52_html(None, "方心禹") == good
    assert cached == [good]
    assert resilience["circuit_ok"] == 1


# ─── 主院：解析完才可以寫快取 ─────────────────────────────────────────────
def _main_src() -> str:
    tree = ast.parse(io.open(os.path.join(REPO_ROOT, "src", "main.py"),
                             encoding="utf-8").read())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "check_appointment_count")
    stripped = ast.parse(ast.unparse(fn)).body[0]
    body = getattr(stripped, "body", [])
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        stripped.body = body[1:] or [ast.Pass()]
    return ast.unparse(stripped)


def test_the_main_page_is_cached_only_after_parsing():
    """★壞頁進了快取，三次 retry 都拿它重解析★

    原本 `_cache_set(cache_main_key, ...)` 在抓完就做。改成解析之後、
    確認 `parsed_slots > 0` 才寫。
    """
    src = _main_src()
    i_classify = src.index("_classify_main_html")
    i_cache = src.index("_cache_set(cache_main_key")
    assert i_classify < i_cache, "要先判定語意，才可以寫主院快取"


def test_both_main_fetch_paths_defer_the_verdict():
    """★main.py 有【兩條】主院抓取路徑★（併行 closure 與順序路徑）

    第一次改的時候我只改到其中一條 —— 旗標設在 closure 的區域變數裡外面看不到，
    而 cache_set 卻改到了另一條。這支釘住兩條都不在抓取當下寫快取。
    """
    src = _main_src()
    assert src.count("_cache_set(cache_main_key") == 1, \
        "主院快取只能有一個寫入點（解析後那個）"
    assert "main_fetched_fresh" in src


def test_a_semantically_invalid_main_page_records_backoff():
    src = _main_src()
    i = src.index("_classify_main_html")
    tail = src[i:]
    assert "_source_backoff_fail" in tail
    assert "_source_backoff_success" in tail, "好的那條路仍要清 backoff"
