# -*- coding: utf-8 -*-
"""[2026-08-06 使用者] 止掛提醒新增黃建仁/謝佳陵。

規格：黃建仁 週三早 60；謝佳陵 週四早/週四晚/週五午 都 75。
兩位（連同既有的沈冠宇）提醒開關【預設關】——多台電腦同跑會重複寄信，
使用者只在自己那台手動勾開（沿用 2026-08-05 外審 P2-11 的定案）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.settings_defaults import default_threshold_settings  # noqa: E402
from cmuh_common.threshold_policy import (  # noqa: E402
    DEFAULT_THRESHOLDS, build_doctor_threshold_map,
)


def test_default_thresholds_have_the_new_slots():
    assert DEFAULT_THRESHOLDS["huang_wed_morning"] == 60
    assert DEFAULT_THRESHOLDS["hsieh_thu_morning"] == 75
    assert DEFAULT_THRESHOLDS["hsieh_thu_night"] == 75
    assert DEFAULT_THRESHOLDS["hsieh_fri_afternoon"] == 75


def test_huang_map_is_wednesday_morning_only():
    m = build_doctor_threshold_map("黃建仁", {})
    assert m == {(2, "上午"): 60}, m           # weekday 2 = 週三


def test_hsieh_map_covers_the_three_slots():
    m = build_doctor_threshold_map("謝佳陵", {})
    assert m == {(3, "上午"): 75,              # 週四早
                 (3, "晚上"): 75,              # 週四晚
                 (4, "下午"): 75}, m           # 週五午


def test_user_override_beats_the_default():
    """設定頁改過的值要壓過原廠預設（與沈/陳同機制）。"""
    m = build_doctor_threshold_map("謝佳陵", {"hsieh_thu_night": 90})
    assert m[(3, "晚上")] == 90
    assert m[(3, "上午")] == 75                # 沒改的維持預設


def test_alert_flags_default_off():
    """★使用者定案★ 沈冠宇/黃建仁/謝佳陵預設關（多台同跑會重複寄信）。"""
    d = default_threshold_settings()
    assert d["alert_shen_enabled"] is False
    assert d["alert_huang_enabled"] is False
    assert d["alert_hsieh_enabled"] is False
    assert d["alert_chen_enabled"] is False    # 既有行為不變


def _main_src() -> str:
    path = os.path.join(os.path.dirname(__file__), "..", "src", "main.py")
    return open(path, encoding="utf-8").read()


def test_main_wires_both_doctors_end_to_end():
    """九處接線的源碼守衛：漏任何一處，勾了開關也不會有提醒（或存不進檔）。"""
    src = _main_src()
    # 判斷點
    assert 'doc_name == "黃建仁" and self.alert_huang_enabled.get()' in src
    assert 'doc_name == "謝佳陵" and self.alert_hsieh_enabled.get()' in src
    # threshold map 集合
    assert '"黃建仁": self._get_doctor_threshold_map("黃建仁")' in src
    assert '"謝佳陵": self._get_doctor_threshold_map("謝佳陵")' in src
    # 存檔
    assert "self.threshold_settings['alert_huang_enabled']" in src
    assert "self.threshold_settings['alert_hsieh_enabled']" in src
    # 設定頁 UI
    assert "啟用 [黃建仁]" in src and "啟用 [謝佳陵]" in src
    assert "'huang_wed_morning': '三早:'" in src
    assert "'hsieh_thu_morning': '四早:'" in src
    assert "'hsieh_thu_night': '四晚:'" in src
    assert "'hsieh_fri_afternoon': '五午:'" in src
    # 還原預設對照 + 影子變數
    assert "('alert_huang_enabled', 'alert_huang_enabled', False)" in src
    assert "('alert_hsieh_enabled', 'alert_hsieh_enabled', False)" in src
    assert src.count("self.val_alert_huang = self.alert_huang_enabled.get()") >= 3
    assert src.count("self.val_alert_hsieh = self.alert_hsieh_enabled.get()") >= 3
