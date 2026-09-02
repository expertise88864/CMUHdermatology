# -*- coding: utf-8 -*-
"""[R3-P2-01 / R3-P2-04 R1 P1-2] `setup_logging` 不可以靠 `basicConfig`。

`logging.basicConfig` 的語意是★「root 已經有 handler 就整個不做事」★,
而★任何在它之前發生的 module-level `logging.warning(...)` 都會讓 Python
隱式裝一個 stderr handler★ —— 例如單例判定,它必然發生在 logging 設定之前。

一旦如此:檔案 handler 永遠裝不上 → log 檔一行都不會寫 → 而 watchdog 是靠
log 的 mtime 判「陳舊」的 → ★健康的行程被反覆殺掉重啟★。
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.logging_setup import setup_logging  # noqa: E402


@pytest.fixture
def clean_root():
    root = logging.getLogger()
    saved, level = list(root.handlers), root.level
    for h in saved:
        root.removeHandler(h)
    try:
        yield root
    finally:
        for h in list(root.handlers):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass
        for h in saved:
            root.addHandler(h)
        root.setLevel(level)


def _file_handlers(root, path):
    return [h for h in root.handlers
            if isinstance(h, RotatingFileHandler)
            and os.path.abspath(h.baseFilename) == os.path.abspath(path)]


def test_it_attaches_even_when_root_already_has_a_handler(clean_root,
                                                          tmp_path):
    """★核心★:root 已經被別人裝過 handler(例如隱式 basicConfig),
    檔案 handler 仍然要裝上去。"""
    clean_root.addHandler(logging.StreamHandler())     # 模擬隱式 basicConfig
    log = str(tmp_path / "x.log")
    setup_logging(log)
    assert _file_handlers(clean_root, log), \
        "★root 已有 handler 就整個不裝檔案 handler★"


def test_the_log_file_actually_gets_written(clean_root, tmp_path):
    """★不是只看有沒有掛上去★:真的寫一行進去 —— watchdog 看的是 mtime。"""
    clean_root.addHandler(logging.StreamHandler())
    log = str(tmp_path / "y.log")
    setup_logging(log)
    logging.getLogger("t").error("hello")
    logging.shutdown()
    assert os.path.exists(log) and os.path.getsize(log) > 0, "log 檔沒有內容"


def test_a_module_level_warning_before_setup_does_not_break_it(clean_root,
                                                               tmp_path):
    """★真實的觸發形狀★:設定之前有人呼叫了 module-level `logging.warning`
    (那會隱式裝 handler)。"""
    logging.warning("這一行會讓 Python 隱式 basicConfig")
    log = str(tmp_path / "z.log")
    setup_logging(log)
    logging.getLogger("t").error("after")
    logging.shutdown()
    assert os.path.exists(log) and os.path.getsize(log) > 0


def test_calling_twice_does_not_double_attach(clean_root, tmp_path):
    """★維持原本的語意★:同一個檔重複呼叫不可以裝兩份(會寫兩行)。"""
    log = str(tmp_path / "w.log")
    first = setup_logging(log)
    again = setup_logging(log)
    assert again is first
    assert len(_file_handlers(clean_root, log)) == 1


def test_a_different_file_gets_its_own_handler(clean_root, tmp_path):
    """★對照組★:去重是以【檔案】為單位,不是「有裝過就不再裝」。"""
    a, b = str(tmp_path / "a.log"), str(tmp_path / "b.log")
    setup_logging(a)
    setup_logging(b)
    assert _file_handlers(clean_root, a) and _file_handlers(clean_root, b)


def test_the_level_is_applied(clean_root, tmp_path):
    clean_root.addHandler(logging.StreamHandler())
    setup_logging(str(tmp_path / "l.log"), level=logging.WARNING)
    assert clean_root.level == logging.WARNING
