# -*- coding: utf-8 -*-
"""threshold_policy helpers."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.threshold_policy import (  # noqa: E402
    DEFAULT_THRESHOLDS,
    appt_item_session_and_count_text,
    build_doctor_threshold_map,
    is_near_alert_threshold,
)


def test_build_doctor_threshold_map_uses_defaults_and_overrides():
    thresholds = build_doctor_threshold_map("陳駿升", {"chen_tue_night": "130"})

    assert thresholds[(1, "晚上")] == 130
    assert thresholds[(3, "上午")] == DEFAULT_THRESHOLDS["chen_thu_morning"]
    assert build_doctor_threshold_map("其他醫師", {}) == {}


def test_shen_only_wed_night_has_a_default():
    """[2026-08-05 使用者定案] 沈冠宇:一早/一午/三午【不預設門檻】,三晚預設 100。

    ★這是這次變更的核心不變量★ 沒填數字 = 那個診次【沒有門檻】= 永遠不提醒。
    最容易寫錯的是把「沒填」當成 0 —— 門檻 0 會讓 count >= 0-10 恆真,
    「還沒設定」就變成「每一診都提醒」,方向完全相反。
    """
    thresholds = build_doctor_threshold_map("沈冠宇", {})

    assert thresholds == {(2, "晚上"): 100}, "只有三晚有門檻,另外三個診次不得出現"
    for empty_key in ("shen_mon_morning", "shen_mon_afternoon", "shen_wed_afternoon"):
        assert empty_key not in DEFAULT_THRESHOLDS, f"{empty_key} 不可有預設值"

    # 使用者自己填了才開始提醒(型別是設定頁存下來的字串)
    filled = build_doctor_threshold_map("沈冠宇", {"shen_mon_morning": "60"})
    assert filled[(0, "上午")] == 60
    assert (0, "下午") not in filled and (2, "下午") not in filled

    # 存成空字串(使用者把框清空)→ 仍然沒有門檻,不可回退成任何數字
    cleared = build_doctor_threshold_map("沈冠宇", {"shen_wed_night": ""})
    assert (2, "晚上") not in cleared, "清空三晚 = 不提醒,不可回退成預設 100"


def test_chang_is_no_longer_a_threshold_doctor():
    """張廖年峰不再做止掛提醒(整套止掛邏輯保留,只是不再有他的門檻)。"""
    assert build_doctor_threshold_map("張廖年峰", {}) == {}
    assert not [k for k in DEFAULT_THRESHOLDS if k.startswith("chang_")]
    # 舊設定檔裡殘留的 chang_* 值不得讓他復活
    assert build_doctor_threshold_map("張廖年峰", {"chang_mon_night": "130"}) == {}


def test_zero_threshold_would_always_alert():
    """釘住「不可把沒填當成 0」的理由:門檻 0 時 90 人也算接近 → 恆真。"""
    assert is_near_alert_threshold(["晚上: 0人"], 2, {(2, "晚上"): 0}, margin=10)
    assert not is_near_alert_threshold(["晚上: 0人"], 2, {}, margin=10)


def test_appt_item_session_and_count_text_handles_dict_and_legacy_text():
    assert appt_item_session_and_count_text({"session": "上午", "count": 12}) == ("上午", "12人")
    assert appt_item_session_and_count_text("下午: 55人|room=1") == ("下午", "55人")
    assert appt_item_session_and_count_text("bad") == ("", "bad")


def test_is_near_alert_threshold_skips_dayoff_and_bad_rows():
    threshold_map = {(0, "晚上"): 100, (0, "下午"): 50}

    assert is_near_alert_threshold(
        ["晚上: 90人", "上午: 休診", {"session": "下午", "count": "停診"}],
        0,
        threshold_map,
        margin=10,
    )
    assert not is_near_alert_threshold(["晚上: 89人", "bad"], 0, threshold_map, margin=10)
    assert not is_near_alert_threshold(["晚上: 100人"], "bad", threshold_map)
