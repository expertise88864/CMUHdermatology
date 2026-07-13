# -*- coding: utf-8 -*-
"""2026-07-13 使用者需求：個別醫師止掛提醒『分兩級』優先刷新間隔。

  門檻-10(near)     → 每 60 分刷新該醫師一次
  門檻-3 (critical) → 每 15 分刷新該醫師一次（只刷該醫師，不連動他人）

底層用 is_near_alert_threshold 的 margin 區分兩級；本檔鎖住：
  1. margin=3 / margin=10 的邊界語意；
  2. AutomationApp._priority_refresh_interval_for_tier 的兩級間隔範圍；
  3. dynamic_cl_checker 走 tier（含升降級即刻改間隔、只刷該醫師）的原始碼守門。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.threshold_policy import is_near_alert_threshold  # noqa: E402
import main  # noqa: E402


def _sessions(count):
    return [{"session": "上午", "count": count}]


def test_two_tier_margin_boundaries():
    tmap = {(0, "上午"): 100}          # 週一上午門檻 100
    # 90 → 進門檻-10(near) 但未進門檻-3
    assert is_near_alert_threshold(_sessions(90), 0, tmap, margin=10) is True
    assert is_near_alert_threshold(_sessions(90), 0, tmap, margin=3) is False
    # 97 → 進門檻-3(critical)
    assert is_near_alert_threshold(_sessions(97), 0, tmap, margin=3) is True
    # 89 → 兩級皆未進
    assert is_near_alert_threshold(_sessions(89), 0, tmap, margin=10) is False


def test_priority_refresh_interval_ranges():
    crit = main.AutomationApp._priority_refresh_interval_for_tier("critical")
    near = main.AutomationApp._priority_refresh_interval_for_tier("near")
    assert 14 * 60 <= crit <= 16 * 60, "critical(門檻-3) 應約 15 分 ±1"
    assert 58 * 60 <= near <= 62 * 60, "near(門檻-10) 應約 60 分 ±2"
    assert near > crit                                   # 越接近門檻刷越快


def test_dynamic_checker_uses_tier_and_single_doctor():
    src = inspect.getsource(main.AutomationApp.start_background_tasks)
    assert "_doctor_alert_proximity_tier(" in src, "應依鄰近等級(tier)決定間隔"
    assert "_priority_refresh_interval_for_tier(" in src
    # 只刷該醫師：submit(self._trigger_refresh, False, [doc])
    assert "self._trigger_refresh, False, [doc]" in src, "只刷該醫師，不連動其他醫師"
    # tier 升/降級即刻改用新間隔（plan[0] != tier 時重設）
    assert "plan[0] != tier" in src, "tier 改變應即刻改用新間隔"


def test_proximity_tier_method_exists():
    tier_src = inspect.getsource(main.AutomationApp._doctor_alert_proximity_tier)
    assert "margin=3" in tier_src and "margin=10" in tier_src
    assert '"critical"' in tier_src and '"near"' in tier_src
