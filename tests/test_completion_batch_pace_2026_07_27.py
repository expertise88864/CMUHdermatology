# -*- coding: utf-8 -*-
"""[2026-07-27 main.py 未審區段:tracker 統計] 同一輪多位病人一起完成時的間隔分攤。

`newly_completed = current_completed_set - last_completed_set` 是【集合】,
門診動態輪詢間隔 60 秒,同一輪出現 2 位以上完成是常態(尤其診間節奏快時)。
原本的迴圈逐筆算 `doctor_pace = now - last_valid_completion_time`,
而且【在迴圈內】就把 last_valid_completion_time 更新成 now →
只有集合迭代到的第一位拿到完整間隔,同批其餘全部拿到 0。

那些 0 之後會被 duration_stats 的中位數帶裁掉(所以不會直接把平均拉到 0),
但結果是「兩人共用 300 秒」被記成一筆 300 而不是兩筆 150 →
平均看診時間、以及用它乘上候診人數的「預估剩餘」被系統性高估。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.clinic_state import (  # noqa: E402
    apply_newly_completed,
    new_clinic_tracker,
)
from cmuh_common.clinic_history import duration_stats  # noqa: E402


def _tracker(t0=1000.0, *, skipped=True):
    tr = new_clinic_tracker("早上", t0)
    tr["first_valid_skipped"] = skipped
    tr["is_first_run"] = False
    return tr


def test_batch_of_two_splits_the_interval():
    """★核心★ 兩人在同一輪完成、距上次完成 300 秒 → 兩筆 150,不是 300 + 0。"""
    tr = _tracker(1000.0)
    tr["patient_checkin_times"] = {11: 100.0, 12: 200.0}
    assert apply_newly_completed(tr, {11, 12}, 1300.0) is True
    assert sorted(tr["durations"]) == [150.0, 150.0]
    assert tr["last_valid_completion_time"] == 1300.0


def test_single_completion_behaviour_unchanged():
    """單人完成(常態)行為完全不變。"""
    tr = _tracker(1000.0)
    tr["patient_checkin_times"] = {11: 100.0}
    assert apply_newly_completed(tr, {11}, 1300.0) is True
    assert tr["durations"] == [300.0]


def test_no_zero_samples_are_produced():
    """★回歸守門★ 任何批次大小都不可再產生 0 秒樣本(那是舊實作的產物)。"""
    for n in (2, 3, 5):
        tr = _tracker(1000.0)
        pts = set(range(100, 100 + n))
        tr["patient_checkin_times"] = {p: 100.0 for p in pts}
        apply_newly_completed(tr, pts, 1300.0)
        assert 0.0 not in tr["durations"], f"batch={n} 仍產生 0 樣本"
        assert len(tr["durations"]) == n
        assert sum(tr["durations"]) == 300.0, "總量守恆:分攤不可憑空增減時間"


def test_first_batch_is_skipped_wholesale():
    """★同批共用同一個基準★ 第一批的間隔是從程式啟動算起、不是醫師節奏,
    整批都不可取樣。原本只跳過迭代到的第一位,其餘拿著無效基準算出的 0 照樣進樣本。"""
    tr = _tracker(1000.0, skipped=False)
    tr["patient_checkin_times"] = {11: 100.0, 12: 100.0, 13: 100.0}
    assert apply_newly_completed(tr, {11, 12, 13}, 1300.0) is True
    assert tr["durations"] == [], "第一批整批不取樣"
    assert tr["first_valid_skipped"] is True
    assert len(tr["waiting_durations"]) == 3, "候診時長不受此規則影響,仍要記"


def test_photo_cases_excluded_and_counted():
    """照光/快速個案:沒進過候診名單、或停留 < 60 秒 → 計入照光數,不算看診時長。"""
    tr = _tracker(1000.0)
    tr["patient_checkin_times"] = {11: 100.0, 12: 1270.0}   # 12 停留 30 秒
    assert apply_newly_completed(tr, {11, 12, 99}, 1300.0) is True
    assert tr["phototherapy_count"] == 2, "12(停留過短)與 99(沒進過候診)"
    assert tr["durations"] == [300.0], "只有 11 是有效樣本,獨得整段間隔"


def test_all_photo_returns_false_and_does_not_move_baseline():
    """整批都是照光 → 不算有效完成,基準時間不可前進(否則下一個真病人的間隔被吃掉)。"""
    tr = _tracker(1000.0)
    tr["last_valid_completion_time"] = 900.0
    assert apply_newly_completed(tr, {77, 88}, 1300.0) is False
    assert tr["phototherapy_count"] == 2
    assert tr["last_valid_completion_time"] == 900.0
    assert tr["durations"] == []


def test_long_gap_drops_the_whole_batch():
    """間隔 ≥ 1 小時視為休息/斷線 → 整批不取樣(維持原本的 3600 秒門檻語意)。"""
    tr = _tracker(1000.0)
    tr["patient_checkin_times"] = {11: 100.0, 12: 100.0}
    apply_newly_completed(tr, {11, 12}, 1000.0 + 3600)
    assert tr["durations"] == []
    assert tr["last_valid_completion_time"] == 1000.0 + 3600, "基準仍要前進"


def test_checkin_times_are_consumed():
    """完成後要把候診起始時間移除,否則同號病人下次掛號會沿用舊時戳。"""
    tr = _tracker(1000.0)
    tr["patient_checkin_times"] = {11: 100.0, 12: 200.0}
    apply_newly_completed(tr, {11}, 1300.0)
    assert tr["patient_checkin_times"] == {12: 200.0}


def test_downstream_average_is_no_longer_inflated():
    """★端到端★ 真實節奏 150 秒/人、每輪 2 人一起被觀測到 →
    舊行為算出 5.0 分,新行為算出 2.5 分(接近真實)。"""
    tr = _tracker(0.0)
    now = 0.0
    for batch in range(4):
        pts = {batch * 2 + 1, batch * 2 + 2}
        tr["patient_checkin_times"].update({p: now for p in pts})
        now += 300.0
        apply_newly_completed(tr, pts, now)
    _all, _valid, avg_min = duration_stats(tr["durations"])
    assert avg_min == 2.5, f"平均應為 2.5 分/人,實際 {avg_min}"
