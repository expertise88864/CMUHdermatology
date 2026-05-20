# -*- coding: utf-8 -*-
"""熱鍵座標縮放工具 — 給多解析度自動腳本用。

【重構 2026-05-21】從 scheduler.py 抽出來。原本 scheduler.py 內定義、
main.py 卻直接 `_scaled_xy(...)` 卻沒 import 也沒 def — dangling reference，
熱鍵真的觸發會 NameError。抽到 cmuh_common 後兩支入口都 import 同一份。

設計：
  - HOTKEY_ADAPTIVE_STATE 是 per-process module-level dict
  - 預設 enabled=False，`_scaled_xy(x, y)` 直接回 `(int(x), int(y))` 不動
  - 呼叫 configure_hotkey_scaling(True, base_version, target_size) 啟用後，
    `_scaled_xy` 會依比例縮放座標到當前螢幕解析度
"""
from __future__ import annotations

from typing import Optional

HOTKEY_SUPPORTED_RESOLUTIONS = ((1920, 1080), (1280, 1024), (1024, 768))

_HOTKEY_BASE_SIZE = {
    "1920x1080": (1920, 1080),
    "1280x1024": (1280, 1024),
    "1024x768": (1024, 768),
}

HOTKEY_ADAPTIVE_STATE = {
    "enabled": False,
    "base_version": None,
    "base_size": (0, 0),
    "target_size": (0, 0),
    "scale_x": 1.0,
    "scale_y": 1.0,
}


def configure_hotkey_scaling(enabled: bool,
                              base_version: Optional[str] = None,
                              target_size: Optional[tuple] = None) -> None:
    """設定熱鍵腳本的座標縮放。

    Args:
        enabled: True 啟用縮放
        base_version: "1920x1080" / "1280x1024" / "1024x768"
        target_size: (width, height) — 當前螢幕實際解析度
    """
    HOTKEY_ADAPTIVE_STATE["enabled"] = bool(enabled)
    HOTKEY_ADAPTIVE_STATE["base_version"] = base_version
    if not enabled or base_version not in _HOTKEY_BASE_SIZE or not target_size:
        HOTKEY_ADAPTIVE_STATE["base_size"] = (0, 0)
        HOTKEY_ADAPTIVE_STATE["target_size"] = (0, 0)
        HOTKEY_ADAPTIVE_STATE["scale_x"] = 1.0
        HOTKEY_ADAPTIVE_STATE["scale_y"] = 1.0
        return
    base_w, base_h = _HOTKEY_BASE_SIZE[base_version]
    target_w, target_h = int(target_size[0]), int(target_size[1])
    HOTKEY_ADAPTIVE_STATE["base_size"] = (base_w, base_h)
    HOTKEY_ADAPTIVE_STATE["target_size"] = (target_w, target_h)
    HOTKEY_ADAPTIVE_STATE["scale_x"] = target_w / float(base_w)
    HOTKEY_ADAPTIVE_STATE["scale_y"] = target_h / float(base_h)


def _scaled_xy(x, y, base_version_hint: Optional[str] = None):
    """套用當前 scaling 後的座標。disabled 時直接回原值。"""
    state = HOTKEY_ADAPTIVE_STATE
    if not state["enabled"]:
        return int(x), int(y)
    if base_version_hint and state.get("base_version") not in (None, base_version_hint):
        return int(x), int(y)
    sx = state.get("scale_x", 1.0)
    sy = state.get("scale_y", 1.0)
    return int(round(x * sx)), int(round(y * sy))
