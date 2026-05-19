# -*- coding: utf-8 -*-
"""共用 health monitor — 給 main / consult_query / autoclock 用。

功能：
  1. **記憶體監看**：每 N 分鐘記錄 process RSS；超過警告閾值就 WARNING (持
     續超過 critical 閾值會 log CRITICAL，由外層 watchdog 決定要不要重啟)
  2. **基本網路 reachable 檢查**：socket connect 一個已知主機 (gmail.com:443)
     用 5s timeout，網路斷了 30s 內可知

設計：純 stdlib + psutil，不引入新依賴。所有 check 都在獨立 daemon thread
跑，不阻塞呼叫端。多次 import 也只啟一個 thread (singleton guard)。

使用：
    from cmuh_common.health import start_health_monitor
    start_health_monitor("main", ram_warn_mb=400, ram_crit_mb=800)
"""
from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Optional

_started_lock = threading.Lock()
_started_for: set = set()  # already-started identifiers


def _get_rss_mb() -> Optional[float]:
    """回傳本 process 的 Resident Set Size (MB)；psutil 不可用就回 None。"""
    try:
        import psutil
        return psutil.Process().memory_info().rss / (1024 * 1024)
    except Exception:
        return None


def _network_reachable(host: str = "smtp.gmail.com", port: int = 587,
                       timeout: float = 5.0) -> bool:
    """TCP connect 看 host:port 是否通；用來判斷網路是否 down。
    不送任何 protocol，只看 TCP 三次握手 → 對 SMTP/IMAP server 都安全 (Gmail
    不會把連到 :587 但不 STARTTLS 的 client 列為 abuse)。"""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            sock.close()
        except Exception:
            pass
        return True
    except Exception:
        return False


def _health_loop(tag: str, ram_warn_mb: float, ram_crit_mb: float,
                  interval_sec: int, network_check: bool) -> None:
    """背景監看迴圈。"""
    logging.info("[health/%s] monitor 啟動 — RAM warn=%dMB crit=%dMB interval=%ds "
                  "network_check=%s",
                  tag, ram_warn_mb, ram_crit_mb, interval_sec, network_check)
    consecutive_high_ram = 0
    last_network_down_log = 0.0
    while True:
        try:
            rss_mb = _get_rss_mb()
            if rss_mb is None:
                # psutil 不可用 — 沒得監測，sleep 久一點
                time.sleep(interval_sec * 6)
                continue

            # RAM 警告分層
            if rss_mb >= ram_crit_mb:
                consecutive_high_ram += 1
                logging.critical(
                    "[health/%s] RAM=%.0fMB ≥ critical %dMB (連續 %d 次)；考慮重啟",
                    tag, rss_mb, ram_crit_mb, consecutive_high_ram)
            elif rss_mb >= ram_warn_mb:
                consecutive_high_ram += 1
                logging.warning(
                    "[health/%s] RAM=%.0fMB ≥ warn %dMB (連續 %d 次)",
                    tag, rss_mb, ram_warn_mb, consecutive_high_ram)
            else:
                if consecutive_high_ram > 0:
                    logging.info(
                        "[health/%s] RAM=%.0fMB 已降回安全範圍 (之前連續 %d 次警告)",
                        tag, rss_mb, consecutive_high_ram)
                consecutive_high_ram = 0
                # 健康時 INFO log；節制不每 5 分鐘一次，每 30 分鐘一次就好
                # 但仍寫 debug 方便 grep
                logging.debug("[health/%s] RAM=%.0fMB OK", tag, rss_mb)

            # 網路檢查 (可選)
            if network_check:
                now = time.time()
                if not _network_reachable():
                    if now - last_network_down_log > 60:
                        logging.warning(
                            "[health/%s] 網路 reachable check 失敗 "
                            "(smtp.gmail.com:587 連不上) — 影響 SMTP/IMAP",
                            tag)
                        last_network_down_log = now

        except Exception:
            logging.exception("[health/%s] tick 例外", tag)

        time.sleep(interval_sec)


def start_health_monitor(tag: str,
                          ram_warn_mb: float = 400.0,
                          ram_crit_mb: float = 800.0,
                          interval_sec: int = 300,
                          network_check: bool = False) -> bool:
    """啟動 daemon thread 監看本 process 的健康度。

    tag: 用來識別此監看器在 log 裡的標籤 (e.g. "main", "consult", "autoclock")
    ram_warn_mb: RSS 達此值寫 WARNING log
    ram_crit_mb: RSS 達此值寫 CRITICAL log
    interval_sec: 多久檢查一次 (預設 300s = 5 分鐘)
    network_check: 是否做 TCP reachable 檢查 (適用 consult_query 等需要 SMTP 的)

    回傳 True = 已啟動；False = 已啟動過 (同 tag 不會重複啟)。
    """
    with _started_lock:
        if tag in _started_for:
            return False
        _started_for.add(tag)
    t = threading.Thread(
        target=_health_loop,
        args=(tag, ram_warn_mb, ram_crit_mb, interval_sec, network_check),
        name=f"HealthMonitor-{tag}",
        daemon=True,
    )
    t.start()
    return True
