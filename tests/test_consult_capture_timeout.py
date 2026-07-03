# -*- coding: utf-8 -*-
"""W11(2026-07-03):PrintWindow 截圖包逾時 —— 視窗凍結時不卡死流程,逾時/失敗一律
raise 交重試。"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402


def test_capture_returns_impl_result(monkeypatch):
    monkeypatch.setattr(cq, "_capture_window_image_impl", lambda h: "IMG")
    assert cq.capture_window_image(123) == "IMG"


def test_capture_raises_on_timeout(monkeypatch):
    monkeypatch.setattr(cq, "_CAPTURE_TIMEOUT_SEC", 0.1)
    monkeypatch.setattr(cq, "_capture_window_image_impl",
                        lambda h: time.sleep(5))
    t0 = time.monotonic()
    with pytest.raises(RuntimeError):
        cq.capture_window_image(123)
    assert time.monotonic() - t0 < 2.0   # 沒等滿 5 秒(未被凍結卡死)


def test_capture_raises_on_impl_error(monkeypatch):
    def boom(h):
        raise RuntimeError("視窗尺寸異常")
    monkeypatch.setattr(cq, "_capture_window_image_impl", boom)
    with pytest.raises(RuntimeError):
        cq.capture_window_image(123)
