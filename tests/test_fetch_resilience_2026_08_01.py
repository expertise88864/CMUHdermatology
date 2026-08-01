# -*- coding: utf-8 -*-
"""[2026-08-01 P2-06 第四刀(b)] cmuh_common/fetch_resilience.py。

★這一層決定「院方主機會不會被我們打爆」★
TTL 快取、解析快取、熔斷器、來源退避與節流 —— 它們在 main.py 裡的時候，
只有熔斷器有測試（`test_circuit_breaker.py` 3 支），其餘一支都沒有。
而這些是無人值守跑一整天的東西：一個沒有上限的長壽 dict 會吃光記憶體，
一個算錯的退避會在院方維護時每 45 秒重打一次。

★狀態是模組級的★ 每支測試前先 `reset_all()` —— 否則前一支把熔斷器打到跳閘，
後一支就會拿到「拒絕」而看不出原因（測試互相污染是這類模組最常見的假紅/假綠來源）。
"""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import fetch_resilience as fr  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    fr.reset_all()
    yield
    fr.reset_all()


@pytest.fixture
def clock(monkeypatch):
    """可控時鐘。本模組同時用 time.time() 與 time.monotonic()，兩個都換掉。"""
    now = {"t": 1000.0}
    monkeypatch.setattr(fr.time, "time", lambda: now["t"])
    monkeypatch.setattr(fr.time, "monotonic", lambda: now["t"])
    return now


# ─── TTL 快取 ──────────────────────────────────────────────────────────────
def test_cache_returns_the_value_within_its_ttl(clock):
    fr._cache_set("k", {"a": 1})
    assert fr._cache_get("k", 60) == {"a": 1}
    clock["t"] += 59
    assert fr._cache_get("k", 60) == {"a": 1}


def test_cache_expires_and_evicts(clock):
    fr._cache_set("k", "v")
    clock["t"] += 61
    assert fr._cache_get("k", 60) is None
    # 過期就順手清掉 → 不會在 dict 裡累積
    assert "k" not in fr._ttl_cache_store


def test_cache_can_keep_an_expired_entry_for_the_stale_fallback(clock):
    """★`evict_expired=False` 是有用途的★ 抓不到新資料時要能拿舊的來墊
    （顯示「這是 N 分鐘前的資料」遠優於整片空白）。清掉就沒得墊了。"""
    fr._cache_set("k", "v")
    clock["t"] += 61
    assert fr._cache_get("k", 60, evict_expired=False) is None
    assert "k" in fr._ttl_cache_store, "留著才有 stale fallback 可用"
    assert fr._cache_get("k", 99999) == "v"


def test_cache_miss_is_none():
    assert fr._cache_get("沒有這個 key", 60) is None


def test_cache_is_bounded():
    """★長壽 dict 一定要有上限★ 無人值守跑一整天，沒上限就是慢性記憶體洩漏。"""
    for i in range(fr._TTL_CACHE_MAX_ENTRIES + 50):
        fr._cache_set(f"k{i}", i)
    assert len(fr._ttl_cache_store) <= fr._TTL_CACHE_MAX_ENTRIES


# ─── 解析快取（同一份 HTML 不重複解析）────────────────────────────────────
def test_parse_cache_hits_on_identical_html(clock):
    fr._parse_cache_set("main", "<html>A</html>", {"parsed": 1})
    assert fr._parse_cache_get("main", "<html>A</html>") == {"parsed": 1}


def test_parse_cache_misses_on_different_html():
    fr._parse_cache_set("main", "<html>A</html>", {"parsed": 1})
    assert fr._parse_cache_get("main", "<html>B</html>") is None


def test_parse_cache_is_keyed_by_parser_too():
    """★不同解析器吃同一份 HTML 會得到不同結果★ key 少了 parser 就會互相污染 ——
    主院解析結果被當成分院的拿去用。"""
    fr._parse_cache_set("main", "<html>A</html>", {"who": "main"})
    assert fr._parse_cache_get("branch", "<html>A</html>") is None


def test_parse_cache_expires(clock):
    fr._parse_cache_set("main", "<html>A</html>", {"x": 1})
    clock["t"] += fr.PARSE_CACHE_TTL_SECONDS + 1
    assert fr._parse_cache_get("main", "<html>A</html>") is None


def test_parse_cache_is_bounded():
    for i in range(fr._PARSE_CACHE_MAX_ENTRIES + 50):
        fr._parse_cache_set("main", f"<html>{i}</html>", i)
    assert len(fr._parse_cache_store) <= fr._PARSE_CACHE_MAX_ENTRIES


# ─── 熔斷器 ────────────────────────────────────────────────────────────────
def test_circuit_trips_only_at_the_threshold():
    src = "s"
    for _ in range(fr._CIRCUIT_BREAKER_THRESHOLD - 1):
        assert fr._circuit_record_fail(src) is False
        assert fr._circuit_is_tripped(src) is False
    assert fr._circuit_record_fail(src) is True, "第 N 次才跳閘"
    assert fr._circuit_is_tripped(src) is True


def test_circuit_resets_itself_after_the_window(clock):
    """★不會自己恢復的熔斷器最糟★ 院方短暫維護就讓某來源一整個下午沒資料，
    要重啟程式才好 —— 那正是這個自動重置存在的理由。"""
    src = "s"
    for _ in range(fr._CIRCUIT_BREAKER_THRESHOLD):
        fr._circuit_record_fail(src)
    assert fr._circuit_is_tripped(src) is True
    clock["t"] += fr._CIRCUIT_BREAKER_RESET_SEC + 1
    assert fr._circuit_is_tripped(src) is False
    # 重置後計數也要歸零，否則下一次失敗就立刻又跳閘
    assert fr._circuit_record_fail(src) is False


def test_circuit_stays_tripped_inside_the_window(clock):
    src = "s"
    for _ in range(fr._CIRCUIT_BREAKER_THRESHOLD):
        fr._circuit_record_fail(src)
    clock["t"] += fr._CIRCUIT_BREAKER_RESET_SEC - 60
    assert fr._circuit_is_tripped(src) is True


def test_a_success_clears_the_circuit():
    src = "s"
    for _ in range(fr._CIRCUIT_BREAKER_THRESHOLD):
        fr._circuit_record_fail(src)
    fr._circuit_record_success(src)
    assert fr._circuit_is_tripped(src) is False


def test_circuits_are_per_source():
    """★一個來源掛掉不可以連累其他來源★（主院掛了不該讓分院也停）"""
    for _ in range(fr._CIRCUIT_BREAKER_THRESHOLD):
        fr._circuit_record_fail("a")
    assert fr._circuit_is_tripped("a") is True
    assert fr._circuit_is_tripped("b") is False


# ─── 來源退避（指數）──────────────────────────────────────────────────────
def test_backoff_allows_everything_when_there_is_no_history():
    ok, remain = fr._source_backoff_allow("s")
    assert ok is True and remain == 0.0


def test_backoff_grows_exponentially_and_is_capped():
    delays = [fr._source_backoff_fail("s")[0] for _ in range(12)]
    assert delays[0] == fr.SOURCE_BACKOFF_BASE_SECONDS
    assert delays[1] == fr.SOURCE_BACKOFF_BASE_SECONDS * 2
    assert delays[2] == fr.SOURCE_BACKOFF_BASE_SECONDS * 4
    assert max(delays) <= fr.SOURCE_BACKOFF_MAX_SECONDS, \
        "★一定要有上限★ 沒有的話連續失敗會退避到天荒地老，來源恢復了也不再試"
    assert delays[-1] == fr.SOURCE_BACKOFF_MAX_SECONDS


def test_backoff_blocks_until_the_delay_has_passed(clock):
    delay, _n = fr._source_backoff_fail("s")
    ok, remain = fr._source_backoff_allow("s")
    assert ok is False and remain == pytest.approx(delay)
    clock["t"] += delay
    assert fr._source_backoff_allow("s")[0] is True


def test_a_success_clears_the_backoff():
    fr._source_backoff_fail("s")
    assert fr._source_backoff_allow("s")[0] is False
    fr._source_backoff_success("s")
    assert fr._source_backoff_allow("s")[0] is True, "恢復之後要立刻能再試"


def test_backoff_state_is_bounded():
    for i in range(fr._SOURCE_STATE_MAX_ENTRIES + 50):
        fr._source_backoff_fail(f"s{i}")
    assert len(fr._source_backoff_state) <= fr._SOURCE_STATE_MAX_ENTRIES


# ─── 來源節流（最短間隔）──────────────────────────────────────────────────
def test_throttle_allows_the_first_call_then_blocks(clock):
    assert fr._source_throttle_allow("s", 60)[0] is True
    ok, remain = fr._source_throttle_allow("s", 60)
    assert ok is False and remain == pytest.approx(60.0)
    clock["t"] += 60
    assert fr._source_throttle_allow("s", 60)[0] is True


def test_throttle_is_per_source(clock):
    assert fr._source_throttle_allow("a", 60)[0] is True
    assert fr._source_throttle_allow("b", 60)[0] is True, "不同來源互不影響"


def test_throttle_state_is_bounded(clock):
    for i in range(fr._SOURCE_STATE_MAX_ENTRIES + 50):
        fr._source_throttle_allow(f"s{i}", 60)
    assert len(fr._source_throttle_state) <= fr._SOURCE_STATE_MAX_ENTRIES


# ─── 併發 ──────────────────────────────────────────────────────────────────
def test_the_caches_survive_concurrent_writers():
    """★這一層是多執行緒在用的★（reg52 併發抓取）—— 上限修剪與讀寫同時發生時
    不可以拋例外（dict changed size during iteration 之類）。"""
    errors = []

    def worker(n):
        try:
            for i in range(200):
                fr._cache_set(f"k{n}-{i}", i)
                fr._cache_get(f"k{n}-{i}", 60)
                fr._source_backoff_fail(f"s{n}-{i % 7}")
                fr._source_throttle_allow(f"t{n}-{i % 7}", 0)
        except Exception as e:      # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, f"併發下拋了例外：{errors[:3]}"
    assert len(fr._ttl_cache_store) <= fr._TTL_CACHE_MAX_ENTRIES


# ─── 搬家本身 ──────────────────────────────────────────────────────────────
def test_main_still_exposes_the_old_private_names():
    """★只搬家、不改呼叫端★"""
    import main
    for name in ("_cache_get", "_cache_set", "_parse_cache_get",
                 "_parse_cache_set", "_circuit_is_tripped",
                 "_circuit_record_fail", "_circuit_record_success",
                 "_source_backoff_allow", "_source_backoff_fail",
                 "_source_backoff_success", "_source_throttle_allow"):
        assert getattr(main, name) is getattr(fr, name), f"{name} 沒接到新模組"


def test_the_state_moved_with_the_functions():
    """★狀態沒跟著搬的話會出現兩份★ main 寫一份、模組讀另一份，
    快取永遠不命中、熔斷器永遠不跳 —— 而且看起來完全正常。"""
    import main
    fr._cache_set("x", 1)
    assert fr._ttl_cache_store, "測試前提"
    assert not hasattr(main, "_ttl_cache_store"), \
        "main.py 不該還留著自己的一份快取"


def test_reset_all_is_only_for_tests():
    """`reset_all` 是測試用的入口 —— 生產路徑不可以呼叫它
    （那會把熔斷器與退避全部清掉，等於把韌性關掉）。"""
    import inspect

    import main
    assert "reset_all" not in inspect.getsource(main.check_appointment_count)
