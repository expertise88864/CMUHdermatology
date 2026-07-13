# -*- coding: utf-8 -*-
"""2026-07-13 使用者需求：個別醫師止掛提醒『分三級』優先刷新間隔 + 反 bot 抖動。

  門檻-10(near)     → 每 30 分刷新該醫師一次
  門檻-5 (mid)      → 每 15 分刷新該醫師一次
  門檻-3 (critical) → 每 10 分刷新該醫師一次（只刷該醫師，不連動他人）
每級間隔再套 ±(10%~20%) 隨機抖動（避免固定節拍被判為 bot）。

抖動候選對齊「檢查喚醒間隔」(30 秒)且【內縮排程延遲邊際 guard】→ 即使排程喚醒比目標
晚(延遲 <guard),實際觸發間隔仍落在 ±[10%,20%]、且絕不進位回基準。這修掉 codex 兩輪
指出的問題:(pass1) 舊版每 2 分喚醒 → 9 分目標被進位成 10 分＝0% 抖動;(pass2) 純網格
假設完美對齊、未計 schedule 於工作後排程 + master loop 5s pump 的喚醒延遲 → -10% 候選
可能被延成 -4%。本檔鎖住:
  1. margin=3 / 5 / 10 三級邊界語意;
  2. 三級基準 10/15/30 分 + 候選 [t,t+guard] 完整落在 ±[10%,20%] 子帶、對齊網格、不含基準;
  3. 以【對抗式延遲喚醒】(每步延遲隨機 0..guard)模擬「實際觸發間隔」→ 恆在 ±[10%,20%]、
     絕不等於基準;
  4. dynamic_cl_checker 走 tier + 只刷該醫師 + 30s 喚醒 的原始碼守門。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.threshold_policy import is_near_alert_threshold  # noqa: E402
import main  # noqa: E402


def _sessions(count):
    return [{"session": "上午", "count": count}]


def test_three_tier_margin_boundaries():
    tmap = {(0, "上午"): 100}          # 週一上午門檻 100
    assert is_near_alert_threshold(_sessions(90), 0, tmap, margin=10) is True
    assert is_near_alert_threshold(_sessions(90), 0, tmap, margin=5) is False
    assert is_near_alert_threshold(_sessions(90), 0, tmap, margin=3) is False
    assert is_near_alert_threshold(_sessions(95), 0, tmap, margin=5) is True
    assert is_near_alert_threshold(_sessions(95), 0, tmap, margin=3) is False
    assert is_near_alert_threshold(_sessions(97), 0, tmap, margin=3) is True
    assert is_near_alert_threshold(_sessions(89), 0, tmap, margin=10) is False


def test_priority_refresh_tier_bases():
    base = main.PRIORITY_REFRESH_TIER_BASE
    assert base["critical"] == 10 * 60
    assert base["mid"] == 15 * 60
    assert base["near"] == 30 * 60
    assert base["critical"] < base["mid"] < base["near"]


def test_jitter_choices_band_covers_full_range_under_drift_exclude_base():
    step = main.PRIORITY_REFRESH_CHECK_SECONDS
    guard = main.PRIORITY_REFRESH_DRIFT_GUARD
    for tier, base in main.PRIORITY_REFRESH_TIER_BASE.items():
        choices = main.PRIORITY_REFRESH_JITTER_CHOICES[tier]
        assert choices, f"{tier} 應有抖動候選"
        for v in choices:
            assert v % step == 0, f"{tier} 候選 {v} 未對齊 {step}s 網格"
            assert v != base, f"{tier} 候選不得等於基準"
            # [t, t+guard] 兩端都須在 ±[10%,20%]（內縮 guard → 延遲後仍在帶內）
            for r in (v, v + guard):
                off = abs(r - base)
                assert base * 0.10 - 1e-9 <= off <= base * 0.20 + 1e-9, \
                    f"{tier} 候選 {v}(+{r - v}) 偏離 {off}s 逸出 ±[10%,20%]"
        assert any(v < base for v in choices) and any(v > base for v in choices), \
            f"{tier} 抖動應正負皆有"


def test_interval_for_tier_returns_a_choice():
    for tier in ("critical", "mid", "near"):
        pool = set(main.PRIORITY_REFRESH_JITTER_CHOICES[tier])
        for _ in range(300):
            assert main.AutomationApp._priority_refresh_interval_for_tier(tier) in pool


def test_realized_under_adversarial_delayed_wakes_stays_in_band():
    # [codex pass2] 對抗式模擬：schedule 於工作後排程 + master loop 5s pump → 喚醒點非完美
    # 對齊,每步延遲隨機落在 (0, guard]。取「首個 elapsed≥target 的喚醒」為實際觸發,驗證
    # 即使最壞延遲,實際間隔仍在 ±[10%,20%]、且絕不等於基準。
    import random as _r
    guard = main.PRIORITY_REFRESH_DRIFT_GUARD

    def realized_with_delay(target, delays):
        elapsed = 0.0
        for step_delay in delays:
            elapsed += step_delay
            if elapsed >= target:
                return elapsed
        return elapsed

    for tier, base in main.PRIORITY_REFRESH_TIER_BASE.items():
        for _ in range(400):
            target = main.AutomationApp._priority_refresh_interval_for_tier(tier)
            # 每次喚醒間隔 = 檢查間隔 + 0..pump 抖動；用最壞情況 guard 上限對抗
            delays = [guard * (0.5 + 0.5 * _r.random())
                      for _ in range(int(target // 10) + 5)]
            r = realized_with_delay(target, delays)
            assert r != base, f"{tier} 實際觸發落在基準={base}（固定節拍、bot 樣）"
            off = abs(r - base)
            assert base * 0.10 - 1e-9 <= off <= base * 0.20 + 1e-9, \
                f"{tier} 延遲後實際觸發 {r} 逸出 ±[10%,20%]（延遲 {r - target:.0f}s）"


def test_two_minute_wake_would_have_collapsed_to_base():
    # 反向釘位：舊版 2 分(120s)喚醒會把負向抖動目標進位回基準(=0% 抖動)。證明改細喚醒的必要。
    base = main.PRIORITY_REFRESH_TIER_BASE["critical"]        # 600s
    target = base - base // 10                                # -10% = 540s
    elapsed = 0
    while elapsed < target:
        elapsed += 120                                        # 舊版每 2 分喚醒
    assert elapsed == base, "示範：2 分喚醒下 540s 目標被進位成 600s＝0% 抖動（故改細喚醒+guard）"


def test_dynamic_checker_uses_tier_single_doctor_and_30s_wake():
    src = inspect.getsource(main.AutomationApp.start_background_tasks)
    assert "_doctor_alert_proximity_tier(" in src, "應依鄰近等級(tier)決定間隔"
    assert "_priority_refresh_interval_for_tier(" in src
    assert "self._trigger_refresh, False, [doc]" in src, "只刷該醫師，不連動其他醫師"
    assert "plan[0] != tier" in src, "tier 改變應即刻改用新間隔"
    # 30s 喚醒（細於抖動網格 → 抖動忠實呈現）
    assert "PRIORITY_REFRESH_CHECK_SECONDS).seconds" in src, "優先刷新應以 30s 喚醒"


def test_proximity_tier_method_lists_three_tiers():
    tier_src = inspect.getsource(main.AutomationApp._doctor_alert_proximity_tier)
    assert "margin=3" in tier_src and "margin=5" in tier_src and "margin=10" in tier_src
    for name in ('"critical"', '"mid"', '"near"'):
        assert name in tier_src, f"tier 判定應含 {name}"
