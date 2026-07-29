# -*- coding: utf-8 -*-
"""[2026-08-02 補審] history 被修剪之後,重算同一個月會【靜默重複計入】。

`settle_month` / `settle_biopsy` 的冪等性完全建立在「同月舊分錄回滾得掉」之上。
[OPT-4] 為了限制檔案大小,`_trim_history` 只留最近 24 個月的分錄 —— 之後
`rollback_*` 就什麼也回滾不到,而 settle 照樣再加一次 delta。

實測(修改前):2024-01 讓 A 得 +5.0;隔 30 個月後回頭重算同一個月 → A 變成 +10.0。
原本的註解只寫「舊分錄不再被回滾」,低估了後果 —— 不是「無法復原」,是【會算錯】,
而且錯的是下個月公平目標的基準,之後每一個月都跟著偏。

`saturday_biopsy` 有一份自己的 `_trim_history`,同樣的形狀、同樣的問題;
切片累計次數正是「R2/R3 全年下來次數要一樣」的依據,重複計入會讓某人被判定成
「已經切很多次」而永遠輪不到。

修法:修剪時記下【被丟掉的最新月份】當水位線,settle 遇到水位線以下的月份就
拒絕並說清楚為什麼(既有檔沒有這個鍵 → 水位線是空的 → 行為完全不變)。
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster.ledger import (  # noqa: E402
    HISTORY_KEEP_MONTHS, can_rollback, rollback_month, settle_month,
)
from cmuh_common.roster.saturday_biopsy import (  # noqa: E402
    can_rollback as biopsy_can_rollback,
)
from cmuh_common.roster.saturday_biopsy import settle_biopsy  # noqa: E402


def _months(n: int, start_year: int = 2026):
    out = []
    y, m = start_year, 1
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return out


# ─── ledger ────────────────────────────────────────────────────────────────
def test_resettling_a_trimmed_month_is_refused_not_double_counted():
    """★核心★ 分錄被修剪之後重算同一個月,舊值會被再加一次(靜默翻倍)。"""
    led = {"r": {}, "vs": {}, "history": []}
    settle_month(led, "r", "2024-01", {"A": 10, "B": 0})
    assert led["r"]["A"] == 5.0                       # 前提:A 多值 → +5

    for ym in _months(HISTORY_KEEP_MONTHS + 6):       # 把 2024-01 擠出保留窗
        settle_month(led, "r", ym, {"A": 0, "B": 0})
    assert not [e for e in led["history"] if e["month"] == "2024-01"]
    assert not can_rollback(led, "2024-01")

    with pytest.raises(ValueError, match="修剪"):
        settle_month(led, "r", "2024-01", {"A": 10, "B": 0})
    assert led["r"]["A"] == 5.0, "★餘額被重複計入了★"


def test_a_month_still_in_history_resettles_idempotently():
    """★不可矯枉過正★ 還在保留窗內的月份,重算必須照舊冪等。"""
    led = {"r": {}, "vs": {}, "history": []}
    settle_month(led, "r", "2026-08", {"A": 10, "B": 0})
    settle_month(led, "r", "2026-08", {"A": 10, "B": 0})
    settle_month(led, "r", "2026-08", {"A": 10, "B": 0})
    assert led["r"]["A"] == 5.0
    assert led["r"]["B"] == -5.0


def test_a_fresh_ledger_behaves_exactly_as_before():
    """history 還沒滿額 → 從未修剪過 → 再舊的月份也照收(向後相容)。"""
    led = {"r": {}, "vs": {}, "history": []}
    settle_month(led, "r", "2020-01", {"A": 4, "B": 0})
    assert led["r"]["A"] == 2.0


def test_an_already_trimmed_file_without_any_marker_is_still_protected():
    """★[第1輪外審] 這是最可能中招的資料★

    我第一版把「哪些月份被丟掉」存成一個新鍵,但【既有的 ledger.json 若已被舊版
    程式修剪過,就沒有那個鍵】—— 保護等於沒有。改成從 history 自己推導之後,
    完全不需要遷移:只要 history 已滿額,比最舊保留月更早的月份一律不敢重算。
    """
    led = {"r": {"A": 5.0}, "vs": {},
           "history": [{"month": ym, "scope": "r", "deltas": {"A": 0.0}}
                       for ym in _months(HISTORY_KEEP_MONTHS)]}
    assert "history_trimmed_through" not in led      # 舊檔就是長這樣

    with pytest.raises(ValueError):
        settle_month(led, "r", "2020-01", {"A": 10, "B": 0})
    assert led["r"]["A"] == 5.0


def test_a_gap_month_inside_the_retained_window_is_still_settleable():
    """★不可矯枉過正★ history 滿額,但某個【比最舊保留月新】的月份沒有分錄
    —— 修剪只從最舊的開始丟,所以那必然是「從沒結算過」,要放行。"""
    months = _months(HISTORY_KEEP_MONTHS)
    gap = months[5]
    led = {"r": {}, "vs": {},
           "history": [{"month": ym, "scope": "r", "deltas": {}}
                       for ym in months if ym != gap]
                      + [{"month": _months(HISTORY_KEEP_MONTHS + 1)[-1],
                          "scope": "r", "deltas": {}}]}
    assert can_rollback(led, gap)
    settle_month(led, "r", gap, {"A": 4, "B": 0})
    assert led["r"]["A"] == 2.0


def test_rollback_of_a_live_month_is_unchanged():
    led = {"r": {}, "vs": {}, "history": []}
    settle_month(led, "r", "2026-08", {"A": 10, "B": 0})
    assert rollback_month(led, "r", "2026-08") is True
    assert led["r"]["A"] == 0.0


def test_the_two_scopes_do_not_shadow_each_other():
    """R 與 VS 共用同一份 history,水位線也是共用的 —— 兩邊都要被擋。"""
    led = {"r": {}, "vs": {}, "history": []}
    settle_month(led, "vs", "2024-01", {"D": 6, "E": 0})
    for ym in _months(HISTORY_KEEP_MONTHS + 6):
        settle_month(led, "r", ym, {"A": 0})
    with pytest.raises(ValueError):
        settle_month(led, "vs", "2024-01", {"D": 6, "E": 0})


# ─── 週六切片計數帳本 ──────────────────────────────────────────────────────
def test_biopsy_counts_are_not_double_counted_after_trimming():
    """切片累計次數是「R2/R3 全年次數要一樣」的依據;重複計入會讓某人被當成
    「已經切很多次」而永遠輪不到。"""
    book = {"counts": {}, "history": []}
    settle_biopsy(book, "2024-01", {date(2024, 1, 6): {"person": "K"}})
    assert book["counts"]["K"] == 1

    for ym in _months(HISTORY_KEEP_MONTHS + 6):
        settle_biopsy(book, ym, {})
    assert not biopsy_can_rollback(book, "2024-01")

    with pytest.raises(ValueError, match="修剪"):
        settle_biopsy(book, "2024-01", {date(2024, 1, 6): {"person": "K"}})
    assert book["counts"]["K"] == 1, "★切片次數被重複計入了★"


def test_biopsy_live_month_resettles_idempotently():
    """★不可矯枉過正★ 保留窗內重算仍要冪等(手改週六值班會一直觸發這條路徑)。"""
    book = {"counts": {}, "history": []}
    for _ in range(3):
        settle_biopsy(book, "2026-08", {date(2026, 8, 1): {"person": "K"}})
    assert book["counts"]["K"] == 1


# ─── [第1輪外審] 服務層:拒絕要在【改動月檔之前】───────────────────────────────
def test_service_does_not_persist_a_half_recomputed_month(tmp_path):
    """★真正的傷害是「半套」★

    `settle_biopsy` 的守門原本要到函式尾端才拋,那時 month["saturday_biopsy"]
    已經被改過了;而 `set_cell` 把這個例外當成可略過並【照樣存檔】——
    月檔的切片人選變了、biopsy.json 的次數沒動,兩邊從此不一致,使用者也看不到。
    """
    from cmuh_common.roster.service import RosterService
    from cmuh_common.roster.storage import RosterStorage

    st = RosterStorage(str(tmp_path))
    st.save_config({"r_members": [{"id": "K", "name": "乙", "level": "R2"},
                                  {"id": "W", "name": "丙", "level": "R3"}],
                    "vs_members": [], "points": {"weekday": 1, "weekend": 2,
                                                 "national_holiday": 1}})
    st.save_holiday_duty({"r": {}, "vs": {}})
    # 切片帳本已滿額,且最舊保留月遠晚於我們要編輯的月份
    st.save_biopsy({"counts": {"K": 3},
                    "history": [{"month": ym, "assign": {}}
                                for ym in _months(HISTORY_KEEP_MONTHS,
                                                  start_year=2030)]})
    st.save_month("2026-08", {"saturday_biopsy":
                              {"2026-08-01": {"person": "K"}}})
    svc = RosterService(st)

    svc.set_cell("r", "2026-08", date(2026, 8, 1), "W")   # 動週六值班 → 會觸發重排

    month = st.load_month("2026-08")
    assert month["r_duty"]["2026-08-01"]["person"] == "W", "值班本身仍要存下去"
    assert month["saturday_biopsy"] == {"2026-08-01": {"person": "K"}},         "★月檔的切片被改了,但帳本次數沒動 —— 半套★"
    assert st.load_biopsy()["counts"] == {"K": 3}

    warns = [c.msg for c in svc.quick_validate("r", "2026-08")
             if c.rule_id == "saturday_biopsy"]
    assert any("不會再自動重算" in m for m in warns),         f"要讓使用者知道這個月的切片不再自動維護:{warns}"
