# -*- coding: utf-8 -*-
"""chrome_options 回歸(2026-07-13 診間實機):headless=new 白窗回歸的緩解。

某些 Chrome 版本的 --headless=new 會把本應隱藏的瀏覽器窗真的畫在桌面上
(1280x800 純白、無邊框、無工作列鈕、不能點不能拖)。緩解=headless 時把視窗
位置推到虛擬桌面外;有頭模式(打卡 GUI)必須可見,不得帶此參數。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

pytest.importorskip("selenium", reason="selenium 未安裝")

from cmuh_common.chrome_options import build_chrome_options  # noqa: E402


def test_headless_pushes_window_offscreen():
    args = build_chrome_options(headless=True).arguments
    assert "--headless=new" in args
    assert "--window-position=-32000,-32000" in args, \
        "headless 白窗回歸緩解:視窗位置須推到虛擬桌面外"


def test_headful_keeps_window_onscreen():
    args = build_chrome_options(headless=False).arguments
    assert "--headless=new" not in args
    assert not any(a.startswith("--window-position") for a in args), \
        "有頭模式(打卡 GUI)必須可見,不得推到畫面外"
