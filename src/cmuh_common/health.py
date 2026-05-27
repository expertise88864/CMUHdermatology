# -*- coding: utf-8 -*-
"""共用 health monitor — 給 main / consult_query / autoclock 用。

功能：
  1. **記憶體監看**：每 N 分鐘記錄 process RSS；超過警告/critical 閾值警告
     + (可選) 持續 N 次 → os._exit(1) 讓 watchdog 重啟 (防 slow memory leak)
  2. **網路 reachable 檢查**：socket connect smtp.gmail.com:587 看是否通
  3. **時鐘漂移偵測**：time.time vs time.monotonic 比對，系統時鐘大幅跳動 → WARN
  4. **硬碟空間監看**：log 目錄 free space <500MB WARN，<100MB CRITICAL
"""
from __future__ import annotations

import logging
import os
import shutil as _shutil
import socket
import threading
import time
from pathlib import Path
from typing import Optional

_started_lock = threading.Lock()
_started_for: set = set()  # already-started identifiers
_self_process_lock = threading.Lock()
_self_process = None


def _coerce_float(value, default: float, *, min_value: float) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        out = default
    return max(min_value, out)


def _coerce_int(value, default: int, *, min_value: int) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        out = default
    return max(min_value, out)


def _normalize_health_monitor_args(ram_warn_mb, ram_crit_mb, interval_sec,
                                   crit_persistence_ticks):
    warn_mb = _coerce_float(ram_warn_mb, 400.0, min_value=1.0)
    crit_mb = _coerce_float(ram_crit_mb, 800.0, min_value=warn_mb)
    interval = _coerce_int(interval_sec, 300, min_value=5)
    persistence_ticks = _coerce_int(crit_persistence_ticks, 6, min_value=1)
    return warn_mb, crit_mb, interval, persistence_ticks


def _get_self_process():
    global _self_process
    if _self_process is not None:
        return _self_process
    with _self_process_lock:
        if _self_process is not None:
            return _self_process
        try:
            import psutil
            _self_process = psutil.Process()
            try:
                _self_process.cpu_percent(interval=None)
            except Exception:
                pass
            return _self_process
        except Exception:
            return None


def _clear_self_process(process) -> None:
    global _self_process
    with _self_process_lock:
        if _self_process is process:
            _self_process = None


def _get_rss_mb() -> Optional[float]:
    """回傳本 process 的 Resident Set Size (MB)；psutil 不可用就回 None。"""
    p = None
    try:
        p = _get_self_process()
        if p is None:
            return None
        return p.memory_info().rss / (1024 * 1024)
    except Exception:
        _clear_self_process(p)
        return None


def _get_self_stats() -> Optional[dict]:
    """[2026-05-25 v15] 取本 process 的 RSS/CPU%/thread 數，用來印 [stats] 心跳。
    psutil 不可用就回 None — caller 自行跳過 stats log。
    cpu_percent(interval=None) 用上次呼叫到現在的累積樣本，第一次呼叫會回 0.0。
    """
    p = None
    try:
        p = _get_self_process()
        if p is None:
            return None
        with p.oneshot():
            return {
                "rss_mb": p.memory_info().rss / (1024 * 1024),
                "cpu_pct": p.cpu_percent(interval=None),
                "threads": p.num_threads(),
            }
    except Exception:
        _clear_self_process(p)
        return None


def _network_reachable(host: str = "smtp.gmail.com", port: int = 587,
                       timeout: float = 5.0) -> bool:
    """TCP connect 看 host:port 是否通；用來判斷網路是否 down。"""
    try:
        sock = socket.create_connection((host, port), timeout=timeout)
        try:
            sock.close()
        except Exception:
            pass
        return True
    except Exception:
        return False


def _disk_free_mb(path: str) -> Optional[float]:
    """回傳 path 所在磁碟的 free space (MB)；失敗回 None。"""
    try:
        return _shutil.disk_usage(path).free / (1024 * 1024)
    except Exception:
        return None


def _flush_logging_handlers_nonblocking() -> None:
    """os._exit 前盡量 flush log，但不可卡在 logging handler lock。"""
    try:
        root_logger = logging.getLogger()
        for h in list(root_logger.handlers):
            lock = getattr(h, "lock", None)
            acquired = False
            try:
                if lock is not None:
                    acquired = lock.acquire(blocking=False)
                    if not acquired:
                        continue
                stream = getattr(h, "stream", None)
                if stream is not None and hasattr(stream, "flush"):
                    stream.flush()
                else:
                    h.flush()
            except Exception:
                pass
            finally:
                if lock is not None and acquired:
                    try:
                        lock.release()
                    except Exception:
                        pass
    except Exception:
        pass


def _health_loop(tag: str, ram_warn_mb: float, ram_crit_mb: float,
                  interval_sec: int, network_check: bool,
                  auto_restart_on_crit: bool,
                  crit_persistence_ticks: int,
                  disk_check_path: str) -> None:
    """背景監看迴圈。"""
    logging.info(
        "[health/%s] monitor 啟動 — RAM warn=%dMB crit=%dMB interval=%ds "
        "network_check=%s auto_restart_on_crit=%s",
        tag, ram_warn_mb, ram_crit_mb, interval_sec, network_check,
        auto_restart_on_crit)

    consecutive_high_ram = 0
    last_network_down_log = 0.0
    last_disk_warn = 0.0
    # 時鐘漂移：紀錄 (time.time, time.monotonic) 配對，每 tick 比較 delta
    last_wall = time.time()
    last_mono = time.monotonic()
    # [v15 2026-05-25] stats heartbeat：每 600s 印一行 rss/cpu/threads
    # 供長期觀察優化效果用 (對比改動前後)
    last_stats_log = 0.0
    STATS_INTERVAL_SEC = 600.0

    while True:
        try:
            # ─── stats heartbeat (rss/cpu/threads) ─────────────────
            # [v15 2026-05-25] 即使 RAM 沒超標也每 10 分鐘 log 一筆，方便長期
            # 觀察 (對照 CPU 優化前後變化、抓 thread leak、看 RSS 漸增)
            now_stats = time.monotonic()
            if now_stats - last_stats_log >= STATS_INTERVAL_SEC:
                stats = _get_self_stats()
                if stats is not None:
                    logging.info(
                        "[health/%s][stats] rss=%.0fMB cpu=%.1f%% threads=%d",
                        tag, stats["rss_mb"], stats["cpu_pct"],
                        stats["threads"])
                last_stats_log = now_stats

            # ─── RAM ───────────────────────────────────────────────
            rss_mb = _get_rss_mb()
            if rss_mb is None:
                logging.debug("[health/%s] RAM stats unavailable; "
                              "continuing network/disk checks", tag)
            elif rss_mb >= ram_crit_mb:
                consecutive_high_ram += 1
                logging.critical(
                    "[health/%s] RAM=%.0fMB ≥ critical %dMB (連續 %d 次)；"
                    "考慮重啟",
                    tag, rss_mb, ram_crit_mb, consecutive_high_ram)
                # [A] 若連續超過 critical 達 N 次 → 自動重啟 (記憶體 leak 防護)
                if (auto_restart_on_crit
                        and consecutive_high_ram >= crit_persistence_ticks):
                    logging.critical(
                        "[health/%s] RAM 連續 %d 次 (~%d 分鐘) 都 ≥ critical → "
                        "os._exit(1) 強制重啟 process (外層 watchdog 會接手)",
                        tag, consecutive_high_ram,
                        consecutive_high_ram * interval_sec // 60)
                    _flush_logging_handlers_nonblocking()
                    os._exit(1)
            elif rss_mb >= ram_warn_mb:
                consecutive_high_ram += 1
                logging.warning(
                    "[health/%s] RAM=%.0fMB ≥ warn %dMB (連續 %d 次)",
                    tag, rss_mb, ram_warn_mb, consecutive_high_ram)
            else:
                if consecutive_high_ram > 0:
                    logging.info(
                        "[health/%s] RAM=%.0fMB 已降回安全範圍 (之前連續 %d 次)",
                        tag, rss_mb, consecutive_high_ram)
                consecutive_high_ram = 0
                logging.debug("[health/%s] RAM=%.0fMB OK", tag, rss_mb)

            # ─── 網路 ──────────────────────────────────────────────
            if network_check:
                now = time.time()
                if not _network_reachable():
                    if now - last_network_down_log > 60:
                        logging.warning(
                            "[health/%s] 網路 reachable check 失敗 "
                            "(smtp.gmail.com:587 連不上) — 影響 SMTP/IMAP",
                            tag)
                        last_network_down_log = now

            # ─── 時鐘漂移 (E) ─────────────────────────────────────
            # 正常情況下，每 interval_sec 跑一次，兩個時鐘各自 +interval_sec
            # 如果系統時鐘 wall 跳了 (NTP / 手動改 / 休眠醒)，delta 會差很多
            cur_wall = time.time()
            cur_mono = time.monotonic()
            wall_delta = cur_wall - last_wall
            mono_delta = cur_mono - last_mono
            drift = abs(wall_delta - mono_delta)
            if drift > 10.0:  # > 10 秒漂移 (一般 NTP 微調是 <1 秒)
                logging.warning(
                    "[health/%s] 時鐘漂移偵測：實際 %.1fs 但 monotonic %.1fs "
                    "(差 %.1fs)。可能 NTP 校正或睡眠喚醒。",
                    tag, wall_delta, mono_delta, drift)
            last_wall = cur_wall
            last_mono = cur_mono

            # ─── 硬碟空間 (F) ─────────────────────────────────────
            free_mb = _disk_free_mb(disk_check_path)
            if free_mb is not None:
                now = time.time()
                if free_mb < 100:
                    if now - last_disk_warn > 300:  # 每 5 分鐘最多一筆
                        logging.critical(
                            "[health/%s] 硬碟剩 %.0fMB < 100MB CRITICAL "
                            "→ log/cache 可能寫不進去！",
                            tag, free_mb)
                        last_disk_warn = now
                elif free_mb < 500:
                    if now - last_disk_warn > 600:  # 每 10 分鐘
                        logging.warning(
                            "[health/%s] 硬碟剩 %.0fMB < 500MB", tag, free_mb)
                        last_disk_warn = now

        except Exception:
            logging.exception("[health/%s] tick 例外", tag)

        time.sleep(interval_sec)


def start_health_monitor(tag: str,
                          ram_warn_mb: float = 400.0,
                          ram_crit_mb: float = 800.0,
                          interval_sec: int = 300,
                          network_check: bool = False,
                          auto_restart_on_crit: bool = False,
                          crit_persistence_ticks: int = 6,
                          disk_check_path: Optional[str] = None) -> bool:
    """啟動 daemon thread 監看本 process 的健康度。

    tag: log 標籤 (e.g. "main", "consult", "autoclock")
    ram_warn_mb / ram_crit_mb: WARN / CRITICAL 閾值 (MB)
    interval_sec: 多久檢查一次 (預設 300s)
    network_check: 是否做 smtp.gmail.com:587 reachable test
    auto_restart_on_crit: [A] 連續 crit_persistence_ticks 次都 ≥ crit_mb →
        主動 os._exit(1) 讓外層 watchdog 重啟 (防 slow memory leak 拖垮系統)
    crit_persistence_ticks: 連續幾 tick 都超 crit 才觸發自殺 (預設 6 = ~30 分鐘)
    disk_check_path: 硬碟空間檢查路徑 (None → 用本 process cwd)

    回傳 True = 已啟動；False = 已啟動過 (同 tag 不重複啟)。
    """
    ram_warn_mb, ram_crit_mb, interval_sec, crit_persistence_ticks = (
        _normalize_health_monitor_args(ram_warn_mb, ram_crit_mb, interval_sec,
                                       crit_persistence_ticks)
    )
    with _started_lock:
        if tag in _started_for:
            return False
        _started_for.add(tag)
    if disk_check_path is None:
        disk_check_path = os.getcwd()
    t = threading.Thread(
        target=_health_loop,
        args=(tag, ram_warn_mb, ram_crit_mb, interval_sec, network_check,
               auto_restart_on_crit, crit_persistence_ticks, disk_check_path),
        name=f"HealthMonitor-{tag}",
        daemon=True,
    )
    t.start()
    return True
