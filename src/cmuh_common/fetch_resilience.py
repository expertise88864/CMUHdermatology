# -*- coding: utf-8 -*-
"""外部資料來源的韌性層：TTL 快取、解析快取、熔斷器、來源退避/節流。
（P2-06 分層第四刀(b) 2026-08-01，從 main.py 搬入）

【為什麼這一族要一起搬】
它們是 reg52/reg64 抓取的「別把院方主機打爆、也別讓一個掛掉的來源拖垮整批」那一層。
量過：這幾個函式用到的模組級可變狀態（快取字典、熔斷器計數、退避/節流狀態）
在 main.py 裡**沒有任何其他使用者** —— 是一個真正封閉的子系統，可以連狀態整組搬。

【刻意不搬的兩類】
  * `_get_thread_local_reg52_*_session`：它們呼叫 `_register_reg_session`
    （session 生命週期 / atexit 清理），main.py 還有 4 個呼叫點 —— 那是別的關切。
  * `_should_fetch_*_reg52`：那是業務設定（哪些醫師在哪個分院），不是韌性。

【所有時間差都用 `time.monotonic()`，★不用 `time.time()`★】
（2026-08-02 外部 code review P2-01）

這一族存的每一個時間戳都只有一個用途：**算經過了多久**。牆上時鐘會被 NTP 校正、
手動改時間、時區/日光節約調整往前或往後跳，而這裡的四個狀態都會因此壞掉：

  * 退避（最嚴重）：`next_allowed_ts = now + delay` 存的是絕對時間。時鐘往回跳
    兩小時，那個來源就被自己退避擋住兩小時 —— 而且沒有任何東西會提早解除。
  * 節流：時鐘往回跳之後 `now - last_ts` 是負的，於是一直被判成「太頻繁」。
  * TTL／解析快取：時鐘往前跳 → 全部瞬間過期（只是多打幾次，較輕）。

★誠實的代價★ monotonic 在 Windows 上是 `GetTickCount64`。它在系統睡眠/休眠
期間是否繼續前進，不同 Windows 版本的說法並不一致，我沒有在這台機器上實測過。
若它不計睡眠時間，診間電腦過夜喚醒後，快取最多會多撐一個 TTL（解析快取 3 分鐘、
reg52 各來源數分鐘）才更新 —— 上限是有界的。相對地，牆上時鐘往回跳造成的退避
鎖死是**沒有上限**的。兩害相權取其輕。

★時間戳不外流★ 這四個 store 的時間戳沒有任何模組外的讀者（量過），也從不顯示
給使用者，所以換基準不會讓任何「資料時間」顯示錯亂。要顯示時間的地方請自己取
`datetime.now()`。

【狀態是模組級的，測試要自己清乾淨】
本模組刻意維持 main.py 原本的形狀（模組級 dict + 一把鎖），沒有改成類別 ——
這一刀是**搬家，不是重新設計**。代價是測試之間會互相污染，所以提供 `reset_all()`
給測試用（生產路徑不呼叫它）。
"""
from __future__ import annotations

import hashlib
import logging
import threading
import time

from cmuh_common.memory_cache import trim_oldest_entries


_ttl_cache_lock = threading.Lock()

_ttl_cache_store = {}

_parse_cache_store = {}

_source_backoff_state = {}

_source_throttle_state = {}

_TTL_CACHE_MAX_ENTRIES = 512

_PARSE_CACHE_MAX_ENTRIES = 256

_SOURCE_STATE_MAX_ENTRIES = 128

# =============================================================================
# [O36] 來源級熔斷器（Circuit Breaker）
# 同個來源（east/auh/huisheng）連續失敗 N 次後暫停嘗試，避免「每 5 分鐘重複等
# 2 秒 timeout」的累積消耗。
# [2026-06-16 韌性] 改為「跳閘後逾 RESET 窗(30 分鐘)自動重置、放行一次重試」——
# 原本一旦跳閘要重啟程式才恢復:醫院端短暫維護(剛好 3 次失敗)就會讓該來源整個
# session(可能一整個下午)都沒資料,使用者只看到「無資料」卻不知是被熔斷。改為
# 定時自我恢復:來源復原就 success 清掉;仍掛則再累積跳閘,不會回到狂打 timeout。
# =============================================================================
# source_key → {"fails": int, "tripped_at": monotonic 或 None}
_CIRCUIT_BREAKER_STATE: dict[str, dict] = {}

_CIRCUIT_BREAKER_LOCK = threading.Lock()

_CIRCUIT_BREAKER_THRESHOLD = 3        # 連續 3 次失敗 → tripped

_CIRCUIT_BREAKER_RESET_SEC = 1800.0   # 跳閘逾 30 分鐘 → 自動重置,放行一次重試

PARSE_CACHE_TTL_SECONDS = 180

SOURCE_BACKOFF_BASE_SECONDS = 2

SOURCE_BACKOFF_MAX_SECONDS = 90


def _circuit_record_fail(source: str) -> bool:
    """記錄失敗，回傳是否剛跳過閾值。"""
    with _CIRCUIT_BREAKER_LOCK:
        st = _CIRCUIT_BREAKER_STATE.setdefault(source, {"fails": 0, "tripped_at": None})
        st["fails"] += 1
        if st["fails"] == _CIRCUIT_BREAKER_THRESHOLD:
            st["tripped_at"] = time.monotonic()
            return True  # 剛跳閾
        return False


def _circuit_record_success(source: str) -> None:
    """成功 → 重置計數。"""
    with _CIRCUIT_BREAKER_LOCK:
        _CIRCUIT_BREAKER_STATE.pop(source, None)


def _circuit_is_tripped(source: str) -> bool:
    """是否仍熔斷中。跳閘逾 RESET 窗 → 自動重置並放行一次重試(回 False)。"""
    with _CIRCUIT_BREAKER_LOCK:
        st = _CIRCUIT_BREAKER_STATE.get(source)
        if not st or st["fails"] < _CIRCUIT_BREAKER_THRESHOLD:
            return False
        ta = st.get("tripped_at")
        if ta is not None and (time.monotonic() - ta) >= _CIRCUIT_BREAKER_RESET_SEC:
            _CIRCUIT_BREAKER_STATE.pop(source, None)
            logging.info("[circuit] 來源 %s 熔斷逾 %d 分鐘,自動重置重試",
                         source, int(_CIRCUIT_BREAKER_RESET_SEC // 60))
            return False
        return True


def _cache_get(cache_key, ttl_seconds, evict_expired=True):
    now = time.monotonic()
    with _ttl_cache_lock:
        row = _ttl_cache_store.get(cache_key)
        if not row:
            return None
        ts, val = row
        if now - ts > ttl_seconds:
            if evict_expired:
                _ttl_cache_store.pop(cache_key, None)
            return None
        return val


def _cache_set(cache_key, value):
    with _ttl_cache_lock:
        _ttl_cache_store[cache_key] = (time.monotonic(), value)
        trim_oldest_entries(_ttl_cache_store, _TTL_CACHE_MAX_ENTRIES)


def _parse_cache_get(parser_key, html_text):
    h = hashlib.sha1(html_text.encode("utf-8", errors="ignore")).hexdigest()
    key = (parser_key, h)
    now = time.monotonic()
    with _ttl_cache_lock:
        row = _parse_cache_store.get(key)
        if not row:
            return None
        ts, val = row
        if now - ts > PARSE_CACHE_TTL_SECONDS:
            _parse_cache_store.pop(key, None)
            return None
        return val


def _parse_cache_set(parser_key, html_text, parsed):
    h = hashlib.sha1(html_text.encode("utf-8", errors="ignore")).hexdigest()
    key = (parser_key, h)
    with _ttl_cache_lock:
        _parse_cache_store[key] = (time.monotonic(), parsed)
        trim_oldest_entries(_parse_cache_store, _PARSE_CACHE_MAX_ENTRIES)


def _source_backoff_allow(source_key):
    now = time.monotonic()
    with _ttl_cache_lock:
        row = _source_backoff_state.get(source_key)
        if not row:
            return True, 0.0
        next_allowed_ts, fail_count = row
        remain = max(0.0, next_allowed_ts - now)
        return remain <= 0.0, remain


def _source_backoff_fail(source_key, base_seconds=None, max_seconds=None):
    now = time.monotonic()
    base = SOURCE_BACKOFF_BASE_SECONDS if base_seconds is None else base_seconds
    max_delay = SOURCE_BACKOFF_MAX_SECONDS if max_seconds is None else max_seconds
    with _ttl_cache_lock:
        row = _source_backoff_state.get(source_key)
        fail_count = (row[1] + 1) if row else 1
        delay = min(base * (2 ** (fail_count - 1)), max_delay)
        _source_backoff_state[source_key] = (now + delay, fail_count)
        trim_oldest_entries(_source_backoff_state, _SOURCE_STATE_MAX_ENTRIES)
        return delay, fail_count


def _source_backoff_success(source_key):
    with _ttl_cache_lock:
        _source_backoff_state.pop(source_key, None)


def _source_throttle_allow(source_key, interval_seconds):
    now = time.monotonic()
    with _ttl_cache_lock:
        # ★預設值必須是 None，不能是 0.0★
        #   原本 `get(source_key, 0.0)` 是靠 `time.time()` 有一億七千萬那麼大才
        #   成立的：`now - 0.0` 遠大於任何 interval，所以「沒紀錄」＝放行。
        #   改用 monotonic 之後那個數字在 Windows 上是【開機以來的秒數】——
        #   剛開機時可能只有幾十秒，`now - 0.0 < interval` 成真，於是每天早上
        #   第一次抓取會被自己的節流擋掉。沒紀錄要明確地表示成「沒紀錄」。
        last_ts = _source_throttle_state.get(source_key)
        if last_ts is not None and now - last_ts < interval_seconds:
            return False, max(0.0, interval_seconds - (now - last_ts))
        _source_throttle_state[source_key] = now
        trim_oldest_entries(
            _source_throttle_state,
            _SOURCE_STATE_MAX_ENTRIES,
            timestamp_of=lambda stamp: stamp,
        )
        return True, 0.0


def reset_all() -> None:
    """把所有模組級狀態清空。★測試專用★（生產路徑不呼叫）。

    這一族是模組級狀態，測試之間會互相污染 —— 熔斷器被前一支測試打到跳閘，
    後一支就會拿到「拒絕連線」而看不出原因。與其讓每支測試各自去 monkeypatch
    內部變數，不如提供一個明確的入口。
    """
    with _ttl_cache_lock:
        _ttl_cache_store.clear()
        _parse_cache_store.clear()
        _source_backoff_state.clear()
        _source_throttle_state.clear()
    with _CIRCUIT_BREAKER_LOCK:
        _CIRCUIT_BREAKER_STATE.clear()
