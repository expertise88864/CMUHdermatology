# -*- coding: utf-8 -*-
"""[2026-07-27 未審檔案 review] clinic_history 完成人數的保存與統計

★根因(外審 P2 指出)★ `upsert_session_stat` 原本把 completed_count 寫在
`if has_dur:` 裡 —— 完成人數明明是【頁面實測值】,卻因為「這一輪沒有新的看診時長
樣本」被整個丟掉。main.py:12133-12145 在「已關診、有關診時間但 durations 是空的」
時仍然會把實測的 completed_count_ui 傳進來(例如程式在診次進行到一半才開啟,
沒觀察到任何完成轉換),結果實測到 15 人被存成 0。

於是磁碟上「completed_count=0 且無樣本」同時代表「真的 0 人」與「沒記到」,
下游 monthly_slot_metric_avgs 怎麼猜都是錯的(我第一版就是在下游猜掛號數)。
修法:源頭一律保存實測值 + completion_observed 標記;下游只排除舊格式的模糊列。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.clinic_history import (  # noqa: E402
    monthly_slot_metric_avgs,
    upsert_session_stat,
)

CUTOFF = date(2026, 1, 1)


def _row(d, *, comp, samples, reg, photo=0, observed=None):
    """observed=None → 模擬 2026-07-27 之前寫下的舊列(沒有 completion_observed)。"""
    row = {"date": d, "doctor": "陳醫師", "room": "101", "session": "上午",
           "completed_count": comp, "valid_sample_count": samples,
           "raw_sample_count": samples, "total_reg": reg,
           "phototherapy": photo}
    if observed is not None:
        row["completion_observed"] = observed
    return row


def _avgs(rows):
    return monthly_slot_metric_avgs(rows, "陳醫師", "101", "上午", CUTOFF)


def _upsert(hist, comp, durations=(), closing="12:30", reg=30):
    return upsert_session_stat(
        hist, today_str="2026/07/22", week_str="三", room_code="101",
        doc_name="陳醫師", completed_count=comp, durations=list(durations),
        session="上午", closing_time=closing, total_reg=reg,
        allow_empty_sample=True)


def test_observed_completion_is_persisted_without_duration_samples():
    """★核心(外審 P2 根因)★ 已關診、有實測完成人數、但完全沒有時長樣本
    (程式在診次中途才開啟)→ 實測的 15 人必須被存下來,不可歸零。"""
    hist, changed = _upsert([], 15)
    assert changed
    assert hist[0]["completed_count"] == 15, "實測值不可被 has_dur 閘門丟掉"
    assert hist[0]["completion_observed"] is True
    assert hist[0]["valid_sample_count"] == 0, "沒有樣本就是沒有樣本,不可假造"


def test_observed_completion_updates_existing_row_monotonically():
    """後續輪詢沒有新樣本但完成數上升 → 更新;頁面暫時解析成 0 → 不可蓋掉。"""
    hist, _ = _upsert([], 15)
    hist, _ = _upsert(hist, 24, closing="12:40")
    assert hist[0]["completed_count"] == 24
    hist, _ = _upsert(hist, 0, closing="12:45")
    assert hist[0]["completed_count"] == 24, "單次讀取異常不可把 24 蓋成 0"


def test_duration_samples_still_take_precedence():
    """有時長樣本時仍走原本的完整寫入路徑(平均/樣本數一起更新)。"""
    hist, _ = _upsert([], 20, durations=[600.0] * 20)
    assert hist[0]["completed_count"] == 20
    assert hist[0]["valid_sample_count"] == 20
    assert hist[0]["avg_time_min"] == 10.0


def test_new_rows_are_all_counted_including_real_zero():
    """有 completion_observed 標記的列一律算進平均 —— 包含真的 0 人。
    下游不再做任何推測。"""
    rows = [
        _row("2026/07/20", comp=24, samples=20, reg=30, observed=True),
        _row("2026/07/21", comp=0, samples=0, reg=30, observed=True),
    ]
    _total, comp, _photo = _avgs(rows)
    assert comp == "12", f"(24+0)/2=12,實際 {comp}"


def test_legacy_ambiguous_row_is_excluded():
    """★只排除舊格式、無從得知的列★ 2026-07-27 之前的列沒有標記,
    「0 人 + 完全沒樣本」在磁碟上無法區分實測 0 與被丟掉的值 → 排除,
    否則完成平均被系統性拉低(畫面出現「掛號 30 / 完成 12」這種對不起來的數)。"""
    rows = [
        _row("2026/07/20", comp=24, samples=20, reg=30),
        _row("2026/07/21", comp=24, samples=21, reg=30),
        _row("2026/07/22", comp=0, samples=0, reg=30),   # 舊格式、模糊
        _row("2026/07/23", comp=0, samples=0, reg=30),   # 舊格式、模糊
    ]
    total, comp, _photo = _avgs(rows)
    assert comp == "24", f"完成平均應為 24,實際 {comp}"
    assert total == "30", "掛號平均不受影響(舊列的 total_reg 仍是實測值)"


def test_legacy_row_with_samples_still_counts():
    """★不可矯枉過正★ 舊列只要有樣本,完成 0 就是實測值,要算進去。"""
    rows = [_row("2026/07/20", comp=0, samples=3, reg=30)]
    _total, comp, _photo = _avgs(rows)
    assert comp == "0"


def test_exclusion_does_not_depend_on_total_reg():
    """★外審 P2★ 不可用掛號數推測 —— 排除與否只看有沒有 completion_observed。"""
    base = _row("2026/07/20", comp=24, samples=20, reg=30, observed=True)
    for reg in (0, None, 30):
        legacy = _row("2026/07/21", comp=0, samples=0, reg=reg)
        assert _avgs([base, legacy])[1] == "24", f"舊列(reg={reg})一律排除"
        new = _row("2026/07/21", comp=0, samples=0, reg=reg, observed=True)
        assert _avgs([base, new])[1] == "12", f"新列(reg={reg})一律計入"


def test_real_upsert_output_is_counted():
    """★不是自己編的資料形狀★ 直接用 upsert_session_stat 走「只知道關診時間+
    掛號數」那條路徑,它產生的列必須帶標記且被計入(不再是模糊佔位列)。"""
    hist, changed = _upsert([], 0)
    assert changed and len(hist) == 1
    assert hist[0]["completion_observed"] is True
    real = _row("2026/07/20", comp=24, samples=20, reg=30, observed=True)
    assert _avgs([real, hist[0]])[1] == "12", "新格式的真 0 人要算進去"
