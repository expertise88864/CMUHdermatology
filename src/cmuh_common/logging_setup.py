# -*- coding: utf-8 -*-
"""統一 log 設定。搬自原主程式 line 612-645、原打卡程式 line 143-167。

提供：
- QueueHandler：log → Queue，給 UI 顯示用（避免 UI 卡死）
- setup_logging：RotatingFileHandler，上限 5MB × 3 份備份
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from queue import Empty, Full, Queue


class QueueHandler(logging.Handler):
    """搬自原主程式 line 613-619。"""

    def __init__(self, log_queue: Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.log_queue.put_nowait(record)
            return
        except Full:
            pass
        except Exception:
            self.handleError(record)
            return

        try:
            self.log_queue.get_nowait()
        except Empty:
            pass
        except Exception:
            self.handleError(record)
            return

        try:
            self.log_queue.put_nowait(record)
        except Full:
            pass
        except Exception:
            self.handleError(record)


def setup_logging(
    log_file: str,
    level: int = logging.INFO,
    max_bytes: int = 5 * 1024 * 1024,
    backup_count: int = 3,
    fmt: str = '%(asctime)s [%(levelname)s] %(threadName)s: %(message)s',
) -> RotatingFileHandler:
    """設定主 logger（RotatingFileHandler）。回傳 handler 以便外部需追加 handler 時使用。

    同一個 log 檔重複呼叫 → 回原本那個 handler（不會裝兩份、不會寫兩行）。
    .pyw 無 console，因此不附加 StreamHandler。

    ★不可以靠 `basicConfig` 裝檔案 handler★(外審 R3-P2-04 R1 P1-2 / R3-P2-01):
    `basicConfig` 的語意是「root 已經有 handler 就整個不做事」,而★任何在這之前
    發生的 module-level `logging.warning(...)` 都會讓 Python 隱式裝一個 stderr
    handler★(例如單例判定 —— 它必然發生在 logging 設定之前)。那之後檔案
    handler 就再也裝不上:log 檔一行都不會寫,而 watchdog 是靠 log 的 mtime 判
    「陳舊」的 → 健康的行程被反覆殺掉重啟。改成★明確把 handler 掛上去★,
    不管 root 現在有沒有別人。
    """
    # 【清理 2026-05-21】delay 參數自 Python 3.9 已存在（README 要 Py 3.10+），TypeError fallback 死分支
    root = logging.getLogger()
    want = os.path.abspath(log_file)
    for h in list(root.handlers):
        if (isinstance(h, RotatingFileHandler)
                and os.path.abspath(getattr(h, "baseFilename", "")) == want):
            root.setLevel(level)
            return h                   # 同一個檔已經設過 → 維持原本的語意
    handler = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count,
        encoding='utf-8', delay=True,
    )
    handler.setFormatter(logging.Formatter(fmt))
    root.addHandler(handler)
    root.setLevel(level)
    return handler


def attach_queue_handler(
    log_queue: Queue,
    level: int = logging.INFO,
    *,
    replace_existing: bool = False,
) -> QueueHandler:
    """加上 QueueHandler 把 log 也送到 UI Queue。"""
    root = logging.getLogger()
    for handler in list(root.handlers):
        if (
            isinstance(handler, QueueHandler)
            and getattr(handler, "log_queue", None) is log_queue
        ):
            handler.setLevel(level)
            return handler
        if replace_existing and isinstance(handler, QueueHandler):
            root.removeHandler(handler)
            handler.close()

    qh = QueueHandler(log_queue)
    qh.setLevel(level)
    root.addHandler(qh)
    return qh


def attach_stream_handler(
    formatter: logging.Formatter | None = None,
    level: int = logging.INFO,
    *,
    stream=None,
    replace_existing: bool = False,
) -> logging.StreamHandler:
    """Add a StreamHandler without stacking duplicates on repeated setup."""
    root = logging.getLogger()
    target_stream = stream if stream is not None else sys.stderr
    for handler in list(root.handlers):
        if type(handler) is logging.StreamHandler and handler.stream is target_stream:
            handler.setLevel(level)
            if formatter is not None:
                handler.setFormatter(formatter)
            return handler
        if replace_existing and type(handler) is logging.StreamHandler:
            root.removeHandler(handler)
            handler.close()

    handler = logging.StreamHandler(stream)
    handler.setLevel(level)
    if formatter is not None:
        handler.setFormatter(formatter)
    root.addHandler(handler)
    return handler
