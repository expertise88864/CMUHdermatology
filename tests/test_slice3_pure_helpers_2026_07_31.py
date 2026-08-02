# -*- coding: utf-8 -*-
"""[2026-07-31 P2-06 分層第三刀] 11 個純函式搬回「已經擁有那個概念的模組」。

★這一刀的重點不是 main.py 少了幾行，是這些函式終於測得到★
它們在 main.py 裡的時候，多數只被「原始碼字串比對」守著（例如
`assert "random.randint(45, 75)" in src`）—— 那種守衛證明的是「我照著我以為的
方式寫了」，不是「它算得對」。搬出來之後可以直接問行為。

分層原則：**不開雜物檔**。每個函式回到已經擁有那個概念的模組：
  appt_utils   ← reg52 schBox 解析、東區休診推論索引（同批函式早就在那裡）
  alert_dedupe ← 提醒紀錄的保留期過濾
  refresh_policy ← 輪詢間隔 / micro-cache TTL
  retention    ← 本機清掃規則（三個 rule builder 都在裡面）
  platform_win ← GetTickCount 回繞（旁邊就是 get_idle_duration）
  his_window   ← 逾時保護版的視窗標題
  uvb_dose     ← 劑量/次數的顯示文字
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.alert_dedupe import filter_recent_alert_sent  # noqa: E402
from cmuh_common.appt_utils import (  # noqa: E402
    build_east_weekday_index, east_index_has_other,
    reg52_docno_for_dayoff_table, split_schbox_by_date,
)
from cmuh_common.platform_win import tick_delta  # noqa: E402
from cmuh_common.refresh_policy import (  # noqa: E402
    clinic_refresh_seconds, reg64_micro_ttl_seconds,
)
from cmuh_common.uvb_dose import format_dose_and_count  # noqa: E402


# ─── reg52 schBox：★止掛提醒被靜默吃掉★ ──────────────────────────────────
class _FakeNode:
    """夠像 BeautifulSoup 的節點：有 get()/get_text()/children。"""

    def __init__(self, text="", classes=(), children=()):
        self._text = text
        self._classes = list(classes)
        self.children = list(children)

    def get(self, key, default=None):
        return self._classes if key == "class" else default

    def get_text(self, strip=False):
        return self._text.strip() if strip else self._text


def test_split_schbox_separates_each_date(fake_cell=None):
    """★[2026-07-26 審查] 這是「止掛提醒被靜默吃掉」的修正★

    同一格常列好幾個日期（同一診每週重複），而「止掛」是【單一日期】的狀態。
    原本用整格文字判斷 → 只要其中一天止掛，同格其他日期全被標成 is_stopped →
    止掛提醒掃描看到就 continue → 那些日期的提醒信被靜默吃掉。
    """
    d1 = _FakeNode("8/1", classes=["visitDate"])
    d2 = _FakeNode("8/8", classes=["visitDate"])
    cell = _FakeNode(children=[
        _FakeNode("內科301診"),          # 格首共用
        d1, _FakeNode("止掛"),
        d2, _FakeNode("正常"),
    ])
    header, groups = split_schbox_by_date(cell)
    assert header == "內科301診"
    assert groups[id(d1)] == "8/1止掛"
    assert groups[id(d2)] == "8/8正常", "第二個日期不可以被第一個的止掛污染"


def test_split_schbox_puts_a_whole_cell_stop_in_the_header():
    """整格都停時「止掛」寫在格首 —— 那個要套用到所有日期，不能漏掉。"""
    d1 = _FakeNode("8/1", classes=["visitDate"])
    cell = _FakeNode(children=[_FakeNode("內科301診止掛"), d1])
    header, groups = split_schbox_by_date(cell)
    assert "止掛" in header and groups[id(d1)] == "8/1"


def test_split_schbox_returns_empty_groups_on_an_unexpected_structure():
    """★結構不符要讓呼叫端退回整格文字＝維持既有行為★
    分層之後這條退路更要守住：解析不出來不可以變成「這格沒有任何日期」。"""
    cell = _FakeNode(children=[_FakeNode("沒有 visitDate 的東西")])
    header, groups = split_schbox_by_date(cell)
    assert groups == {} and header == "沒有 visitDate 的東西"


@pytest.mark.parametrize("given,expect", [
    ("12345", "D12345"), ("D12345", "D12345"), ("d12345", "d12345"),
    ("  12345  ", "D12345"), (12345, "D12345"),
])
def test_dayoff_docno_always_has_the_d_prefix(given, expect):
    """reg52.cgi 的 table#dayoff 只在 DocNo=D12345 時出現；純數字回傳的 HTML
    根本不含休診表 —— 少了這個前綴，休診資料會【安靜地】全部消失。"""
    assert reg52_docno_for_dayoff_table(given) == expect


# ─── 東區休診推論索引 ──────────────────────────────────────────────────────
def _parse(item):
    return item["session"], item["ext"]


def test_east_index_only_collects_east_sessions():
    data = {
        "D1": {
            date(2026, 8, 3): [{"session": "上午", "ext": "east"},
                               {"session": "下午", "ext": "main"}],
            date(2026, 8, 10): [{"session": "上午", "ext": "east"}],
            "不是日期的鍵": [{"session": "上午", "ext": "east"}],
        },
        "D2": {"error": "抓不到"},
    }
    idx = build_east_weekday_index(data, _parse)
    assert idx == {("D1", 0, "上午"): {date(2026, 8, 3), date(2026, 8, 10)}}


def test_east_index_query_excludes_the_date_being_asked_about():
    """★語意★ 問的是「【其他】同 weekday 有沒有東區」—— 只有自己那天不算。"""
    data = {"D1": {date(2026, 8, 3): [{"session": "上午", "ext": "east"}]}}
    idx = build_east_weekday_index(data, _parse)
    assert east_index_has_other(idx, "D1", "王醫師", 0, "上午",
                                date(2026, 8, 10)) is True
    assert east_index_has_other(idx, "D1", "王醫師", 0, "上午",
                                date(2026, 8, 3)) is False


def test_east_index_query_takes_the_union_of_docno_and_name():
    """兩個 lookup key（醫師代號、姓名）取聯集 —— 資料來源不一定用同一個。"""
    data = {"王醫師": {date(2026, 8, 3): [{"session": "上午", "ext": "east"}]}}
    idx = build_east_weekday_index(data, _parse)
    assert east_index_has_other(idx, "D1", "王醫師", 0, "上午",
                                date(2026, 8, 10)) is True


# ─── 提醒紀錄的保留期 ──────────────────────────────────────────────────────
def test_filter_recent_alert_sent_keeps_only_fresh_iso_dates():
    got = filter_recent_alert_sent(
        {"a": "2026-07-30", "b": "2026-07-01", "c": "2026-07-31"}, "2026-07-15")
    assert got == {"a": "2026-07-30", "c": "2026-07-31"}


@pytest.mark.parametrize("bad", [None, [], "字串", 42])
def test_filter_recent_alert_sent_rejects_a_non_dict(bad):
    """★讀壞的設定檔不可以炸掉提醒功能★"""
    assert filter_recent_alert_sent(bad, "2026-07-15") == {}


def test_filter_recent_alert_sent_drops_non_string_entries():
    assert filter_recent_alert_sent(
        {"a": 20260730, 5: "2026-07-30", "c": "2026-07-30"},
        "2026-07-15") == {"c": "2026-07-30"}


# ─── 輪詢間隔 / TTL ────────────────────────────────────────────────────────
def test_refresh_interval_slows_down_overnight():
    """[MN-03] 00-07 點放慢（多機夜間負載禮貌）；其餘時段 45-75 秒。

    ★這裡問的是【行為】★ 之前只有 main.py 的原始碼比對
    `assert "random.randint(45, 75)" in src` —— 那證明不了區間對不對。
    """
    for h in range(0, 7):
        assert all(180 <= clinic_refresh_seconds(h) <= 300 for _ in range(50))
    for h in range(7, 24):
        assert all(45 <= clinic_refresh_seconds(h) <= 75 for _ in range(50))


def test_the_night_interval_is_actually_slower_than_the_day_one():
    """★防空洞★ 兩個區間若重疊，上面那支也會過。"""
    assert min(clinic_refresh_seconds(3) for _ in range(50)) > 75


def test_reg64_micro_ttl_matches_the_polling_slowdown():
    assert reg64_micro_ttl_seconds(3) == 170
    assert reg64_micro_ttl_seconds(7) == 50
    assert reg64_micro_ttl_seconds(23) == 50


# ─── GetTickCount 回繞 ─────────────────────────────────────────────────────
def test_tick_delta_is_correct_across_the_49_day_wraparound():
    """★32 位元約 49.7 天回繞★ 沒有無號差值就會算出負的閒置時間。"""
    assert tick_delta(5000, 1000) == 4000
    assert tick_delta(5, 0xFFFFFFFB) == 10          # 剛好跨過回繞點
    assert tick_delta(0, 0) == 0
    assert tick_delta(0, 1) == 0xFFFFFFFF


# ─── UVB 劑量/次數的顯示文字 ───────────────────────────────────────────────
def test_dose_and_count_say_different_things_when_unknown():
    """★兩個 None 的說法不一樣是刻意的★
    劑量不明只是 "?"；次數不明要明講「未寫次數」—— 那是醫師要補的東西。"""
    assert format_dose_and_count(700, 11) == "劑量 700、次數 11"
    assert format_dose_and_count(None, 11) == "劑量 ?、次數 11"
    assert "未寫次數" in format_dose_and_count(700, None)
    assert format_dose_and_count(None, None) == "劑量 ?、次數 （未寫次數）"


# ─── 清掃規則 ──────────────────────────────────────────────────────────────
def test_retention_default_rules_take_the_settings_dir_as_an_argument(tmp_path):
    """★[第三刀] 改成收路徑，不再自己去拿 get_settings_dir()★
    三個 rule builder 本來就都收路徑，這樣才一致 —— 也才測得到。"""
    from cmuh_common.retention import default_rules
    rules = default_rules(str(tmp_path))
    assert len(rules) == 3
    dirs = [r.directory if hasattr(r, "directory") else str(r) for r in rules]
    joined = " ".join(dirs)
    assert "debug_dumps" in joined and "consult_shots" in joined


def test_main_still_exposes_the_old_private_names():
    """★這一刀只搬家、不改呼叫端★ 別名掉了會變成 AttributeError，
    而且多半只在實機才會炸。"""
    import main
    # [2026-08-01 第四刀] `_split_schbox_by_date` 不再由 main.py 匯入 ——
    # 它在 main.py 的唯一呼叫端（`_parse_main_hospital_schedule`）也搬進
    # cmuh_common/reg52_parse.py 了，那個呼叫變成模組之間的。
    # 這跟第二刀的 `_window_is_ancestor` 是同一個形狀：搬家會讓別的匯入變成死碼。
    # [2026-08-02 第五刀(a) 第二批] `_filter_recent_alert_sent` 從這份清單移除 ——
    # 它在 main.py 的唯一呼叫端（`_load_alert_email_sent`）整支搬進
    # cmuh_common/alert_state.py 了，別名跟著變成死碼。與上面第四刀的
    # `_split_schbox_by_date` 是同一個形狀：★呼叫端搬走，別名就沒有存在的理由★
    # （不是「守衛礙事所以拿掉」）。
    for name in ("_reg52_docno_for_dayoff_table",
                 "_build_east_weekday_index", "_east_index_has_other",
                 "_clinic_refresh_seconds",
                 "_reg64_micro_ttl_seconds", "_tick_delta", "_fmt_uvb_dc",
                 "_his_title_of", "_retention_rules"):
        assert callable(getattr(main, name)), f"{name} 不見了"


def test_the_two_homeless_helpers_stayed_in_main():
    """★沒有好歸屬就不要硬搬★

    `parse_color_spec`（像素顏色規格）與 `_f11_normalize_course_value`
    （療程欄全形轉半形）沒有現成的模組擁有那個概念。為了搬 19 行而開兩個
    只有一個函式的雜物檔，比留在原地更糟 —— 留著，並在計畫書寫明理由。
    """
    import ast
    import inspect

    import main
    tree = ast.parse(inspect.getsource(main.parse_color_spec))
    assert isinstance(tree.body[0], ast.FunctionDef)
    assert callable(main._f11_normalize_course_value)
    assert main._f11_normalize_course_value("１２") == "12", "全形要轉半形"
