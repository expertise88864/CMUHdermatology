# -*- coding: utf-8 -*-
"""reg52 班表解析器的單格容錯回歸測試。

[review C2 2026-06-12] 三個解析器(_parse_main_hospital_schedule /
_parse_fh_like_weekly_schedule / _parse_east_fh1 路徑)原本單格日期解析失敗會
raise、炸掉「整個醫師」的班表;_parse_doctor_info_dayoff 早已為同一問題加過
per-row 防護(還留有 [stability] 註解)但手足函式沒同步。本檔固定「一格壞、
其餘格存活」的契約。
"""
import os
import sys

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import main  # noqa: E402


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


MAIN_SCHEDULE_HTML = """
<table class="schedule">
  <tr><th>表頭</th></tr>
  <tr>
    <td class="timeSlot AM">上午</td>
    <td class="schBox">(101診)
      <div class="visitDate"><b>115/06/19</b></div>
      <div>已掛號：12</div>
      <div class="visitDate"><b>壞掉的日期</b></div>
      <div>已掛號：5</div>
      <div class="visitDate"><b>115/06/26</b></div>
      <div>已掛號：7</div>
    </td>
  </tr>
</table>
"""


def test_main_schedule_bad_date_cell_skipped_others_survive():
    """一格 visitDate 內容無法解析 → 只跳該格,同 cell 其他日期照常解析。"""
    result = main._parse_main_hospital_schedule(_soup(MAIN_SCHEDULE_HTML))

    from datetime import date
    assert date(2026, 6, 19) in result
    assert date(2026, 6, 26) in result
    assert len(result) == 2  # 壞日期那格被跳過、不產生 key
    entry = result[date(2026, 6, 19)][0]
    assert entry["session"] == "上午"
    assert entry["count"] == 12
    assert entry["room"] == "101診"


def test_main_schedule_all_good_dates_parse():
    """正常 HTML 完整解析(防護不可影響正常路徑)。"""
    html = MAIN_SCHEDULE_HTML.replace("壞掉的日期", "115/06/21")
    result = main._parse_main_hospital_schedule(_soup(html))
    assert len(result) == 3


def test_dayoff_parser_keeps_per_row_guard():
    """既有 dayoff 解析的 per-row 防護契約:壞日期列跳過、好列存活。"""
    html = """
    <table id="dayoff">
      <tr><th>日期</th><th>診別</th><th>代診</th></tr>
      <tr><td>(合併儲存格小標)</td><td>上午</td><td>休診</td></tr>
      <tr><td>115/06/20</td><td>上午</td><td>休診</td></tr>
    </table>
    """
    result = main._parse_doctor_info_dayoff(_soup(html))
    from datetime import date
    assert date(2026, 6, 20) in result
    assert len(result) == 1


def test_safe_parse_roc_date_raises_on_garbage():
    """_safe_parse_roc_date 的 raise 契約(各解析器的防護以此為前提)。"""
    with pytest.raises(ValueError):
        main._safe_parse_roc_date("壞掉的日期")
    with pytest.raises(ValueError):
        main._safe_parse_roc_date("")
    assert main._safe_parse_roc_date("115/06/19").year == 2026


def test_idle_duration_uses_unsigned_tick_arithmetic():
    """[review C2] GetTickCount 回繞修正契約:get_idle_duration 必須用
    &0xFFFFFFFF 無號 32-bit 環算術(與 hotkey_guardian 同 idiom),否則開機
    24.8 天後閒置秒數變負 → 閒置自動重開機永遠不觸發(死循環需手動重開)。"""
    import inspect
    from cmuh_common import platform_win
    src = inspect.getsource(platform_win.get_idle_duration)
    assert src.count("& 0xFFFFFFFF") >= 2  # tick 取值與差值各一次
