# -*- coding: utf-8 -*-
"""統一 log 設定。搬自原主程式 line 612-645、原打卡程式 line 143-167。

提供：
- QueueHandler：log → Queue，給 UI 顯示用（避免 UI 卡死）
- setup_logging：RotatingFileHandler，上限 5MB × 3 份備份
"""
import logging
from logging.handlers import RotatingFileHandler
from queue import Queue


class QueueHandler(logging.Handler):
    """搬自原主程式 line 613-619。"""

    def __init__(self, log_queue: Queue):
        super().__init__()
        self.log_queue = log_queue

    def emit(self, record: logging.LogRecord) -> None:
        self.log_queue.put(record)


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
    try:
        handler = RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count,
            encoding='utf-8', delay=True,
        )
    except TypeError:
        handler = RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count, encoding='utf-8')
    logging.basicConfig(level=level, format=fmt, handlers=[handler])
    return handler


def attach_queue_handler(log_queue: Queue, level: int = logging.INFO) -> QueueHandler:
    """加上 QueueHandler 把 log 也送到 UI Queue。"""
    qh = QueueHandler(log_queue)
    qh.setLevel(level)
    logging.getLogger().addHandler(qh)
    return qh
