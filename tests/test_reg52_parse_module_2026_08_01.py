# -*- coding: utf-8 -*-
"""[2026-08-01 P2-06 分層第四刀] cmuh_common/reg52_parse.py 的分院/亞大解析器。

★這幾支搬出來之前的覆蓋率是個位數★
`_parse_fh_like_weekly_schedule` 2%、`parse_branch_schedule` 2%、
`parse_auh_reg52_schedule` 3% —— 而它們讀出來的東西直接決定：

  * 月曆上顯示的掛號人數；
  * 止掛提醒要不要寄（`is_stopped` / 休診）；
  * 診間號碼（止掛信裡「診間未提供」就是這裡沒解析到）。

解析器搬到自己的模組之後，餵一段 HTML 進去就能問「它讀出什麼」——
這就是分層的理由，不是 main.py 少幾行。

既有的 `tests/test_reg52_parsers.py` 已經涵蓋主院班表與休診表；這一檔補分院那幾支。
"""
import os
import sys

import pytest
from bs4 import BeautifulSoup

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import reg52_parse as rp  # noqa: E402


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def _visit(roc: str, tail: str) -> str:
    return f'<div class="visitDate"><b>{roc}</b> {tail}</div>'


# ─── 東區 / 惠和 / 惠盛 週表（_parse_fh_like_weekly_schedule）───────────────
_FH_ROW = (
    "<table><tr>"
    "<td>上 午</td>"                              # ★診別常見「上 午」中間有空格★
    "<td>王醫師 (G06診)" + _visit("115/08/03", "已掛號：7") + "</td>"
    "</tr><tr>"
    "<td>下午</td>"
    "<td>王醫師 (101診)" + _visit("115/08/04", "休診") + "</td>"
    "</tr></table>"
)


def test_fh_weekly_handles_the_spaced_session_label():
    """★「上 午」中間有空格★ 這是東區/惠和版型的實際寫法；
    不正規化就整列被跳過 → 那一診整週在月曆上消失。"""
    got = rp.parse_east_fh1_schedule(_soup(_FH_ROW))
    from datetime import date
    assert date(2026, 8, 3) in got
    item = got[date(2026, 8, 3)][0]
    assert item["session"] == "上午" and item["count"] == 7


def test_fh_weekly_reads_the_room_number_including_letter_prefixes():
    """診間號沒解析到，止掛信就會寫「診間未提供」（2026-06-19 使用者回報）。"""
    from datetime import date
    got = rp.parse_east_fh1_schedule(_soup(_FH_ROW))
    assert got[date(2026, 8, 3)][0]["room"] == "G06診"
    assert got[date(2026, 8, 4)][0]["room"] == "101診"


@pytest.mark.parametrize("tail,expect", [
    ("休診", "休診"), ("停診", "休診"), ("已額滿", "已額滿"),
    ("報到截止", "截止"), ("已掛號：12", 12), ("沒有數字", 0),
])
def test_fh_weekly_count_states(tail, expect):
    """★數量欄有五種狀態★ 休診/停診、額滿、截止、實際人數、讀不到。
    讀不到要回 0（而不是留空）—— 呼叫端拿它去算總數。"""
    html = f'<table><tr><td>上午</td><td>{_visit("115/08/03", tail)}</td></tr></table>'
    from datetime import date
    got = rp.parse_east_fh1_schedule(_soup(html))
    assert got[date(2026, 8, 3)][0]["count"] == expect


def test_fh_weekly_marks_the_right_branch():
    """三個分院共用同一支解析器，差別只在掛哪個 ext_branch —— 掛錯會顯示在錯的院區。"""
    from datetime import date
    d = date(2026, 8, 3)
    for fn, want in ((rp.parse_east_fh1_schedule, "east"),
                     (rp.parse_huihe_schedule, "huihe"),
                     (rp.parse_huisheng_schedule, "huisheng")):
        got = fn(_soup(_FH_ROW))
        assert got[d][0]["ext_branch"] == want
        assert got[d][0]["is_ext"] is True


def test_fh_weekly_skips_only_the_bad_date_cell():
    """★[review C2] 一格壞掉不可以讓整張表消失★"""
    from datetime import date
    html = ("<table><tr><td>上午</td><td>"
            + _visit("壞掉", "已掛號：3") + _visit("115/08/05", "已掛號：4")
            + "</td></tr></table>")
    got = rp.parse_east_fh1_schedule(_soup(html))
    assert list(got) == [date(2026, 8, 5)]


def test_fh_weekly_ignores_rows_without_a_recognisable_session():
    assert rp.parse_east_fh1_schedule(
        _soup('<table><tr><td>備註</td><td>'
              + _visit("115/08/03", "已掛號：3") + '</td></tr></table>')) == {}


# ─── 分院表（parse_branch_schedule）────────────────────────────────────────
def _branch_html(rows: str) -> str:
    return ('<form name="FrontPage_Form1"><table>'
            '<tr><th>診別</th><th>星期一</th></tr>' + rows + '</table></form>')


def test_branch_schedule_needs_both_the_form_and_the_weekday_table():
    """★兩個前提都不成立就回空 dict★ 版型改了要「什麼都沒有」，
    不可以硬解析出錯的資料（那會直接寫進月曆）。"""
    assert rp.parse_branch_schedule(_soup("<table><tr><td>上午</td></tr></table>")) == {}
    assert rp.parse_branch_schedule(
        _soup('<form name="FrontPage_Form1"><table><tr><td>沒有星期</td></tr></table></form>')) == {}


def test_branch_schedule_reads_count_room_and_session():
    from datetime import date
    html = _branch_html('<tr><td>上午</td><td>陳醫師 (A101診)'
                        + _visit("115/08/03", "已掛號：9") + '</td></tr>')
    got = rp.parse_branch_schedule(_soup(html))
    item = got[date(2026, 8, 3)][0]
    assert (item["session"], item["count"], item["room"]) == ("上午", 9, "A101診")


def test_branch_schedule_marks_full():
    from datetime import date
    html = _branch_html('<tr><td>下午</td><td>'
                        + _visit("115/08/03", "已額滿") + '</td></tr>')
    got = rp.parse_branch_schedule(_soup(html))
    assert got[date(2026, 8, 3)][0]["count"] == "已額滿"


def test_branch_schedule_skips_only_the_bad_date():
    from datetime import date
    html = _branch_html('<tr><td>上午</td><td>'
                        + _visit("亂碼", "已掛號：1")
                        + _visit("115/08/06", "已掛號：2") + '</td></tr>')
    assert list(rp.parse_branch_schedule(_soup(html))) == [date(2026, 8, 6)]


# ─── 亞大 reg52（parse_auh_reg52_schedule）────────────────────────────────
def test_auh_uses_the_main_layout_when_it_is_present():
    """亞大若是主院版型就直接沿用主院解析器，只把院區標成 auh。"""
    from datetime import date
    html = ('<table class="schedule"><tr><th>x</th></tr>'
            '<tr><td class="timeSlot">上午</td>'
            '<td class="schBox">李醫師 (201診)'
            + _visit("115/08/03", "") + '<div>已掛號：5</div></td></tr></table>')
    got = rp.parse_auh_reg52_schedule(_soup(html))
    item = got[date(2026, 8, 3)][0]
    assert item["ext_branch"] == "auh" and item["is_ext"] is True
    assert item["count"] == 5


def test_auh_falls_back_to_plain_row_text():
    """★亞大常見版型沒有 timeSlot/schBox class★
    退路是「整列文字抓 日期+已掛號」—— 沒有這條就整個亞大都讀不到。"""
    from datetime import date
    html = ("<table><tr><td>上午 115/08/03 已掛號：8 "
            "115/08/10 已掛號：11</td></tr></table>")
    got = rp.parse_auh_reg52_schedule(_soup(html))
    assert got[date(2026, 8, 3)][0]["count"] == 8
    assert got[date(2026, 8, 10)][0]["count"] == 11
    assert got[date(2026, 8, 3)][0]["ext_branch"] == "auh"


def test_auh_fallback_does_not_let_one_count_swallow_the_next_date():
    """★這一刀唯一的行為改變 —— 而且是修一個真的 bug★

    退路先把整列文字的空白【全部拿掉】（為了認得「上 午」）。於是相鄰兩組會黏成
    `已掛號：8115/08/10`，舊的貪婪 `(\\d+)` 把 `8115` 當成數量，還把下一個日期的
    `115` 吃掉 → 那一天再也配不到。

    亞大週表一列就是一個診別、欄位是一週各天 —— 也就是說這條路徑上的掛號人數
    **一直都是錯的**，而且沒有任何跡象（解析成功、有數字、只是不對）。
    """
    from datetime import date
    html = ("<table><tr><td>上午 115/08/03 已掛號：8 "
            "115/08/10 已掛號：11 115/08/17 已掛號：3</td></tr></table>")
    got = rp.parse_auh_reg52_schedule(_soup(html))
    assert [(d, got[d][0]["count"]) for d in sorted(got)] == [
        (date(2026, 8, 3), 8),
        (date(2026, 8, 10), 11),
        (date(2026, 8, 17), 3),
    ], "三天都要在，數量都要對"


@pytest.mark.parametrize("text,pairs", [
    ("115/08/03已掛號：11", [("115/08/03", "11")]),          # 結尾兩位數
    ("115/08/03已掛號：15人", [("115/08/03", "15")]),        # 後面接中文
    ("115/08/03已掛號：0", [("115/08/03", "0")]),            # 零
    ("115/08/03已掛號：123", [("115/08/03", "123")]),        # 三位數
])
def test_the_count_regex_still_reads_normal_numbers(text, pairs):
    """★防過度修正★ 非貪婪寫錯的話會只吃到一位數 —— 這幾條釘住正常情況。"""
    assert rp._RE_REG52_DATE_CNT_PAIRS.findall(text) == pairs


def test_auh_fallback_recognises_every_session_wording():
    from datetime import date
    for text, want in (("早診", "上午"), ("下午", "下午"),
                       ("夜間", "晚上"), ("晚診", "晚上")):
        html = f"<table><tr><td>{text} 115/08/03 已掛號：1</td></tr></table>"
        got = rp.parse_auh_reg52_schedule(_soup(html))
        assert got[date(2026, 8, 3)][0]["session"] == want


def test_auh_returns_empty_when_nothing_matches():
    assert rp.parse_auh_reg52_schedule(_soup("<table><tr><td>公告</td></tr></table>")) == {}


# ─── 搬家本身 ──────────────────────────────────────────────────────────────
def test_main_still_exposes_the_old_private_names():
    """★只搬家、不改呼叫端★ 別名掉了會變成 AttributeError，多半只在實機才炸。"""
    import main
    for name in ("_parse_main_hospital_schedule", "_parse_doctor_info_dayoff",
                 "_parse_east_fh1_schedule", "_parse_huihe_schedule",
                 "_parse_huisheng_schedule", "_parse_branch_schedule",
                 "_parse_auh_reg52_schedule", "_parse_appt_item_for_alert"):
        assert callable(getattr(main, name)), f"{name} 不見了"


def test_the_parsers_have_no_mutable_module_state():
    """★這一族之所以搬得動，就是因為它們沒有可變的模組級狀態★
    哪天有人在這裡加一個 dict 當快取，這支會紅 —— 那時要重新想它還算不算純解析層。
    """
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(rp))
    mutable = []
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(
                node.value, (ast.Dict, ast.List, ast.Set)):
            mutable.extend(t.id for t in node.targets
                           if isinstance(t, ast.Name))
    assert not mutable, f"解析層不該有可變的模組級狀態：{mutable}"
