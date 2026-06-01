# -*- coding: utf-8 -*-
"""[O17] 背景清理舊 cache 與 log，避免長期執行佔滿磁碟。

清理規則：
- automation_ui.log.1 / .2 / .3 等備份檔超過 30 天 → 刪除
- settings/debug_dumps/ 已由打卡程式 prune_debug_dumps（上限 40）控管
- settings/cache_*.json 不刪除（仍在使用），只在啟動時偵測損壞並重置
- 所有 *.bak 超過 7 天 → 刪除
- *.tmp 超過 1 天 → 刪除（殘留 tmp 通常是寫入失敗）
- __pycache__ 內超過 30 天的 .pyc → 刪除
"""
from __future__ import annotations

import logging
import os
import threading
import time
from pathlib import Path

from cmuh_common.paths import get_app_dir, get_settings_dir

DAY = 86400
_cleanup_state_lock = threading.Lock()
_cleanup_scheduled = False
_cleanup_running = False


def _safe_remove(p: Path) -> bool:
    try:
        p.unlink()
        return True
    except OSError:
        return False


def _scan_and_clean(directory: Path, *, predicate, label: str) -> int:
    """掃 directory（單層）刪除 predicate(file) 為 True 的檔。回傳刪除數。"""
    if not directory.is_dir():
        return 0
    removed = 0
    try:
        for p in directory.iterdir():
            if not p.is_file():
                continue
            try:
                if predicate(p):
                    if _safe_remove(p):
                        removed += 1
            except Exception:
                logging.debug("[cleanup] 評估 %s 失敗", p, exc_info=True)
    except OSError:
        return removed
    if removed:
        logging.info("[O17] cleanup %s: 已刪除 %d 個檔案於 %s", label, removed, directory)
    return removed


def cleanup_old_files() -> dict:
    """執行所有清理規則，回傳 {label: count} 統計。"""
    app_dir = Path(get_app_dir())
    settings_dir = Path(get_settings_dir())
    now = time.time()
    stats: dict[str, int] = {}

    # 1. log 備份檔（automation_ui.log.1, .2, .3）超過 30 天
    def is_old_log_backup(p: Path) -> bool:
        n = p.name
        if not (n.endswith('.log') or '.log.' in n or n.endswith('.log.bak')):
            return False
        # 只刪「備份」（檔名含 .log.<digit>），避免動到當前活動 log
        if not any(c.isdigit() for c in n.rsplit('.log', 1)[-1] if c):
            return False
        if '.log.' not in n and not n.endswith('.log.bak'):
            return False
        try:
            return (now - p.stat().st_mtime) > 30 * DAY
        except OSError:
            return False
    stats['log_backups'] = _scan_and_clean(app_dir, predicate=is_old_log_backup, label='log_backups')

    # 2. *.bak 超過 7 天（線上更新留下的舊版本）
    def is_old_bak(p: Path) -> bool:
        if not p.name.endswith('.bak'):
            return False
        try:
            return (now - p.stat().st_mtime) > 7 * DAY
        except OSError:
            return False
    # 掃 root + src/* 一層
    stats['bak_files'] = 0
    for d in (app_dir, app_dir / 'src', app_dir / 'src' / 'cmuh_common',
              app_dir / 'src' / 'clock'):
        stats['bak_files'] += _scan_and_clean(d, predicate=is_old_bak, label='bak_files')

    # 3. *.tmp 超過 1 天
    def is_old_tmp(p: Path) -> bool:
        if not (p.name.endswith('.tmp') or p.name.endswith('.json.tmp')):
            return False
        try:
            return (now - p.stat().st_mtime) > DAY
        except OSError:
            return False
    stats['tmp_files'] = 0
    for d in (app_dir, settings_dir):
        stats['tmp_files'] += _scan_and_clean(d, predicate=is_old_tmp, label='tmp_files')

    # 4. __pycache__ 內 30 天前的 .pyc
    pycache_removed = 0
    for pcache in app_dir.rglob('__pycache__'):
        if not pcache.is_dir():
            continue
        for p in pcache.iterdir():
            if p.is_file() and p.suffix == '.pyc':
                try:
                    if (now - p.stat().st_mtime) > 30 * DAY:
                        if _safe_remove(p):
                            pycache_removed += 1
                except OSError:
                    pass
    stats['pyc_old'] = pycache_removed
    if pycache_removed:
        logging.info("[O17] cleanup pyc_old: 已刪除 %d 個過期 .pyc", pycache_removed)

    return stats


def _release_cleanup_state() -> None:
    global _cleanup_scheduled, _cleanup_running
    with _cleanup_state_lock:
        _cleanup_scheduled = False
        _cleanup_running = False


def schedule_cleanup_in_background(executor, *, delay_seconds: int = 30) -> bool:
    """[O17] 啟動 N 秒後在背景執行緒池跑一次清理（不阻塞 UI）。"""
    global _cleanup_scheduled, _cleanup_running
    with _cleanup_state_lock:
        if _cleanup_scheduled or _cleanup_running:
            logging.debug("[O17] cleanup already scheduled or running; skip duplicate")
            return False
        _cleanup_scheduled = True

    def _run():
        global _cleanup_running
        with _cleanup_state_lock:
            if _cleanup_running:
                return
            _cleanup_running = True
        try:
            stats = cleanup_old_files()
            total = sum(stats.values())
            if total:
                logging.info("[O17] 清理完成，共移除 %d 個檔: %s", total, stats)
        except Exception:
            logging.debug("[O17] cleanup 例外", exc_info=True)
        finally:
            _release_cleanup_state()

    def _start_fallback_thread():
        try:
            threading.Thread(
                target=_run,
                name="CacheCleanupFallback",
                daemon=True,
            ).start()
        except Exception:
            _release_cleanup_state()
            logging.debug("[O17] cleanup fallback thread 啟動失敗", exc_info=True)

    def _later():
        try:
            future = executor.submit(_run)

            def _fallback_if_submit_failed(done_future):
                try:
                    exc = done_future.exception()
                except Exception as err:
                    exc = err
                if exc is not None:
                    logging.warning(
                        "[O17] cleanup executor rejected task; fallback thread: %s",
                        exc,
                    )
                    _start_fallback_thread()

            add_done_callback = getattr(future, "add_done_callback", None)
            if callable(add_done_callback):
                add_done_callback(_fallback_if_submit_failed)
            elif getattr(future, "done", lambda: False)():
                _fallback_if_submit_failed(future)
        except Exception:
            _start_fallback_thread()

    timer = threading.Timer(delay_seconds, _later)
    timer.daemon = True
    try:
        timer.start()
    except Exception:
        _release_cleanup_state()
        raise
    return True
