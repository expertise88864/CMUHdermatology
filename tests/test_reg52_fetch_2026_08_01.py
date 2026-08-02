# -*- coding: utf-8 -*-
"""[2026-08-01 P2-06 第四刀(b)-ii] 院外 reg52 抓取 + HTTP session 註冊表。

★這一層真正的邏輯是「韌性接線」，不是 HTTP★
HTTP 本身 requests 已經測過了。這裡要問的是：來源掛掉的時候我們有沒有守規矩 ——
退避有沒有記、熔斷器有沒有跳、抓不到時會不會拿舊快取來墊。
這些在 main.py 裡的時候一支測試都沒有，而它們決定的是「會不會每 45 秒重打一次
正在維護的院方主機」。

★接縫★ `_get_thread_local_reg52_external_session` 是模組級函式 →
monkeypatch 掉就能餵假的 session 進來，完全不碰網路。
"""
import os
import sys

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import fetch_resilience as fr  # noqa: E402
from cmuh_common import http_session_registry as reg  # noqa: E402
from cmuh_common import reg52_fetch as rf  # noqa: E402

_GOOD_HTML = ('<html><body>' + 'x' * 600
              + '<div class="visitDate"><b>115/08/03</b></div></body></html>')


class _Resp:
    def __init__(self, text="", status=200):
        self.text = text
        self.status_code = status
        self.encoding = None

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code}")


class _FakeSession:
    """可設定的假 session：回應由 `plan` 決定（可以是例外）。"""

    def __init__(self, plan):
        self.plan = list(plan)
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        item = self.plan.pop(0) if self.plan else _Resp(_GOOD_HTML)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture(autouse=True)
def _clean():
    fr.reset_all()
    yield
    fr.reset_all()


@pytest.fixture
def fake(monkeypatch):
    def _install(plan):
        s = _FakeSession(plan)
        monkeypatch.setattr(rf, "_get_thread_local_reg52_external_session",
                            lambda: s)
        return s
    return _install


# ─── 退避：失敗要記、下一輪要被擋住 ───────────────────────────────────────
def test_a_failed_fetch_records_backoff(fake):
    """★不記退避 = 院方維護時我們每一輪都再打一次★"""
    fake([requests.exceptions.ConnectionError("boom")] * 4)
    assert rf._fetch_huihe_reg52_html(None, "12345", "王醫師") is None
    ok, remain = fr._source_backoff_allow("huihe:12345")
    assert ok is False and remain > 0


def test_backoff_short_circuits_the_next_attempt_without_touching_the_network(
        fake):
    """★被退避擋住時【完全不可以連線】★ 否則退避等於沒有。"""
    fr._source_backoff_fail("huihe:12345",
                            fr.SOURCE_BACKOFF_BASE_SECONDS, 999)
    s = fake([_Resp(_GOOD_HTML)])
    assert rf._fetch_huihe_reg52_html(None, "12345", "王醫師") is None
    assert s.calls == [], "退避期間不該發出任何 request"


def test_a_successful_fetch_clears_the_backoff(fake, monkeypatch):
    """★來源恢復之後要立刻回到正常節奏★ 成功卻不清退避的話，
    院方修好了我們還在退避裡等 —— 而退避是指數成長的。"""
    fr._source_backoff_fail("huihe:12345",
                            fr.SOURCE_BACKOFF_BASE_SECONDS, 999)
    assert fr._source_backoff_allow("huihe:12345")[0] is False
    # [2026-08-02 P2-01] 退避改用 time.monotonic()（牆上時鐘往回跳會把來源
    # 鎖死，見 fetch_resilience 的說明）—— 這裡要推進的是 monotonic。
    import time as _t
    real = _t.monotonic()
    monkeypatch.setattr(fr.time, "monotonic", lambda: real + 99999)  # 退避到期
    fake([_Resp(_GOOD_HTML)])
    assert rf._fetch_huihe_reg52_html(None, "12345", "王醫師") is not None
    assert "huihe:12345" not in fr._source_backoff_state,         "成功之後退避紀錄要清掉，不是等它自然過期"


# ─── 內容把關：拿到 200 不等於拿到掛號表 ──────────────────────────────────
def test_a_short_or_unrecognisable_page_is_not_accepted(fake):
    """★200 OK 不等於「這是掛號表」★ 院方導到登入頁/錯誤頁也是 200。
    沒有這道把關，上層會把一張空表當成「這位醫師這週沒診」。"""
    fake([_Resp("太短"), _Resp("<html>" + "y" * 900 + "</html>")])
    assert rf._fetch_huihe_reg52_html(None, "12345", "王醫師") is None


def test_it_retries_the_other_encoding_variant(fake):
    """Docname 先 Big5 再 UTF-8 —— 第一個變體不行要試第二個。"""
    s = fake([_Resp("太短"), _Resp(_GOOD_HTML)])
    assert rf._fetch_huihe_reg52_html(None, "12345", "王醫師") is not None
    assert len(s.calls) == 2, "兩個編碼變體都要試"


def test_the_docno_gets_the_d_prefix_for_the_dayoff_table(fake):
    """reg52.cgi 的休診表只在 DocNo=D12345 時出現 —— 少了前綴休診資料整片消失。"""
    s = fake([_Resp(_GOOD_HTML)])
    rf._fetch_huihe_reg52_html(None, "12345", "王醫師")
    assert "DocNo=D12345" in s.calls[0]


# ─── 熔斷器：只有掛熔斷器的來源才會跳 ─────────────────────────────────────
def test_a_tripped_circuit_skips_the_fetch_entirely(fake):
    src = "east"
    for _ in range(fr._CIRCUIT_BREAKER_THRESHOLD):
        fr._circuit_record_fail(src)
    s = fake([_Resp(_GOOD_HTML)])
    assert rf._fetch_east_district_reg52_html(None, "12345", "王醫師") is None
    assert s.calls == [], "熔斷中不該發出任何 request"


def test_a_success_clears_the_east_circuit(fake):
    fake([_Resp(_GOOD_HTML)])
    fr._circuit_record_fail("east")
    rf._fetch_east_district_reg52_html(None, "12345", "王醫師")
    assert fr._circuit_is_tripped("east") is False


# ─── 亞大：抓不到時拿舊快取來墊 ───────────────────────────────────────────
def test_auh_falls_back_to_a_stale_cache_when_the_fetch_fails(fake,
                                                              monkeypatch):
    """★整片空白 vs 舊資料★ 抓不到時顯示「N 分鐘前的資料」遠優於什麼都沒有。
    這正是 `_cache_get(..., evict_expired=False)` 存在的理由。"""
    monkeypatch.setitem(rf.AUH_DOCTOR_DOCNO_MAP, "王醫師", "D999")
    fake([_Resp(_GOOD_HTML)])
    first = rf._fetch_auh_reg52_html(None, "王醫師")
    assert first is not None, "先抓一次把快取填起來"

    fr._source_backoff_success("auh:王醫師")     # 清掉退避，確保是走快取而不是被擋
    fake([requests.exceptions.ConnectionError("院方掛了")] * 3)
    again = rf._fetch_auh_reg52_html(None, "王醫師")
    assert again is not None, "抓不到時要拿舊快取來墊，不可以回 None"


def test_auh_returns_none_for_an_unknown_doctor(fake):
    """不在對照表裡的醫師沒有 DocNo → 不可以硬組一個 URL 去打。"""
    s = fake([_Resp(_GOOD_HTML)])
    # 回的是空字串（不是 None）—— 釘住【實際】的契約，不是我以為的那個
    assert not rf._fetch_auh_reg52_html(None, "不存在的醫師")
    assert s.calls == [], "沒有 DocNo 就不可以硬組 URL 去打"


# ─── session 註冊表 ───────────────────────────────────────────────────────
def test_sessions_are_registered_and_can_be_cleared():
    class _Adapter:
        def __init__(self):
            self.poolmanager = type("P", (), {"cleared": False,
                                              "clear": lambda s: setattr(
                                                  s, "cleared", True)})()

    class _S:
        def __init__(self):
            self.adapters = {"https://": _Adapter()}

    s = _S()
    reg.register_session(s)
    assert s in reg._all_reg_sessions
    reg.clear_all_sessions()
    assert s.adapters["https://"].poolmanager.cleared is True
    assert len(reg._all_reg_sessions) == 0


def test_the_registry_is_a_weakset_so_dead_sessions_do_not_pile_up():
    """★用 WeakSet 而不是 set★ 執行緒結束、session 沒人引用時要能被回收；
    普通 set 會讓跑一整天的程式累積一堆死 session。"""
    import gc
    from weakref import WeakSet

    class _S:
        adapters: dict = {}

    assert isinstance(reg._all_reg_sessions, WeakSet)
    s = _S()
    reg.register_session(s)
    assert len(reg._all_reg_sessions) == 1
    del s
    gc.collect()
    assert len(reg._all_reg_sessions) == 0


def test_clearing_survives_a_broken_session():
    """★退出路徑不可以拋★ 一個壞掉的 session 不能讓其餘的都清不掉。"""
    class _Bad:
        @property
        def adapters(self):
            raise RuntimeError("壞了")

    class _Good:
        def __init__(self):
            self.cleared = False
            outer = self

            class _P:
                def clear(self_inner):
                    outer.cleared = True
            self.adapters = {"https://": type("A", (), {"poolmanager": _P()})()}

    good = _Good()
    reg.register_session(_Bad())
    reg.register_session(good)
    reg.clear_all_sessions()          # 不可拋
    assert good.cleared is True


def test_the_atexit_hook_is_registered():
    """★這是它唯一的觸發時機★ 沒註冊 = 退出時不會斷連。"""
    import inspect
    src = inspect.getsource(reg)
    assert "atexit.register(" in src


# ─── 搬家本身 ──────────────────────────────────────────────────────────────
def test_main_still_exposes_the_old_private_names():
    import main
    for name in ("_register_reg_session", "_session_http_guard",
                 "_fetch_auh_reg52_html", "_fetch_huisheng_reg52_html",
                 "_fetch_east_district_reg52_html", "_fetch_huihe_reg52_html",
                 "AUH_DOCTOR_DOCNO_MAP"):
        assert hasattr(main, name), f"{name} 不見了"
