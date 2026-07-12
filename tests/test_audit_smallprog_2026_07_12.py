# -*- coding: utf-8 -*-
"""小程式批次1+其餘 回歸測試(2026-07-12 未審區域計畫書補修)。

SP-01 scaling target_size 防呆(行為);SP-03/05/06/07 源碼層守衛。
SP-02(JS+消費端 canary)、SP-04(scaling dict 整包替換)緩修。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.hotkey_scaling import (  # noqa: E402
    configure_hotkey_scaling, _scaled_xy,
)


def _read_src(*parts):
    p = os.path.join(os.path.dirname(__file__), "..", "src", *parts)
    with open(p, encoding="utf-8") as f:
        return f.read()


# ── SP-01 target_size 無效不 crash、不縮到左上角 ────────────────────────────
def test_sp01_zero_target_size_no_zero_scale():
    configure_hotkey_scaling(True, "1920x1080", (0, 0))
    assert _scaled_xy(960, 540) == (960, 540), "scale=0 把座標縮到左上角"


def test_sp01_none_target_size_no_crash():
    configure_hotkey_scaling(True, "1920x1080", (None, None))   # 不得拋 TypeError
    assert _scaled_xy(960, 540) == (960, 540)


def test_sp01_valid_target_still_scales():
    configure_hotkey_scaling(True, "1920x1080", (1280, 1024))
    sx, sy = _scaled_xy(1920, 1080)
    assert sx != 1920 or sy != 1080, "有效 target 未生效縮放(過度收緊)"


# ── SP-05 coord hook 跨緒安全回呼(源碼守衛) ────────────────────────────────
def test_sp05_hotkey_safe_callback():
    src = _read_src("coord_detector.py")
    assert "_on_hotkey_safe" in src and "winfo_exists()" in src, \
        "SP-05 未用安全跨緒回呼"


# ── SP-06 取色失敗不更新 _last_pos(源碼守衛) ───────────────────────────────
def test_sp06_color_fail_no_pos_update():
    src = _read_src("coord_detector.py")
    body = src[src.find("def update_info"):src.find("def setup_hotkey")]
    # except 分支只設失敗狀態,成功分支才更新 _last_pos(移到 else)
    assert "(讀取失敗)" in body and "else:" in body, "SP-06 取色失敗仍更新 _last_pos"


# ── SP-07 登入 alert 回實際文字(源碼守衛) ──────────────────────────────────
def test_sp07_alert_returns_actual_text():
    src = _read_src("cmuh_common", "punch_status.py")
    assert '"帳號/密碼錯誤"' not in src or "_alert.text" in src, \
        "SP-07 仍硬編帳密錯誤、未回實際 alert 文字"
    assert "_alert.text" in src, "SP-07 未讀實際 alert 文字"


# ── SP-03 idle 門檻提高(源碼守衛) ──────────────────────────────────────────
def test_sp03_idle_threshold_raised():
    src = _read_src("main.py")
    assert "idle_required_sec=30" in src, "SP-03 未把自動重啟 idle 門檻提高到 30s"
