# -*- coding: utf-8 -*-
"""統一 log 設定。搬自原主程式 line 612-645、原打卡程式 line 143-167。

提供：
- QueueHandler：log → Queue，給 UI 顯示用（避免 UI 卡死）
- setup_logging：RotatingFileHandler，上限 5MB × 3 份備份
"""
import logging
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

    注意：本函式只 basicConfig 一次；多次呼叫第二次會被忽略。
    .pyw 無 console，因此不附加 StreamHandler。
    """
    # 【清理 2026-05-21】delay 參數自 Python 3.9 已存在（README 要 Py 3.10+），TypeError fallback 死分支
    handler = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count,
        encoding='utf-8', delay=True,
    )
    logging.basicConfig(level=level, format=fmt, handlers=[handler])
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
