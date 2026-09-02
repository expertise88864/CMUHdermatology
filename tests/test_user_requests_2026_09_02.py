# -*- coding: utf-8 -*-
"""[2026-09-02 使用者] 三件使用者要求。

1. 總覽表:老人醫院那一列排在★所有外院的最後一個★。
2. 設定頁的醫師清單:依醫師代號★數字由小到大★排序。
3. 照光熱鍵:認得「開始療程」的醫囑寫法
   `(2026/8/29) Start phototherapy, start 400mj, add 100 each time,
    upper limit: 1200mj`
   —— 關鍵字與劑量之間夾著逗號與一個起始詞,四個舊分支都比不到。
"""
import importlib
import inspect
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.appt_utils import _calendar_branch_sort_rank  # noqa: E402
from cmuh_common.uvb_dose import (  # noqa: E402
    parse_uvb_line, update_uvb_in_text,
)

main = importlib.import_module("main")


# ══ 1. 老人醫院排在所有外院最後 ══════════════════════════════════════════
class TestTheGeriatricRowSortsLast:
    def test_it_is_after_every_other_branch(self):
        others = ("east", "auh", "huihe", "huisheng")
        tcmc = _calendar_branch_sort_rank("tcmc")
        for b in others:
            assert _calendar_branch_sort_rank(b) < tcmc, b

    def test_it_is_even_after_an_unknown_future_branch(self):
        """★這是「最後一個」的重點★:日後新增分院(落在未知那一格)
        也不可以把老人醫院擠到中間。"""
        assert _calendar_branch_sort_rank("some_new_branch") < \
            _calendar_branch_sort_rank("tcmc")

    def test_a_main_hospital_row_is_not_ranked_here(self):
        """★這條測試自己更正過★:我原本斷言「本院(None)排在所有分院前面」——
        但 `None` 與 `east` 都回 0,那個性質★不存在於這支函式★。
        本院/分院之分是呼叫端用 `zone_bucket` 做的(分院一律排進最後一桶),
        這支只排【分院之間】的先後。宣稱要對得上實作。
        """
        assert _calendar_branch_sort_rank(None) == 0
        code = inspect.getsource(main.AutomationApp._update_grid_data)
        assert "zone_bucket" in code and "_calendar_branch_sort_rank" in code

    def test_the_existing_branch_order_is_unchanged(self):
        order = [_calendar_branch_sort_rank(b)
                 for b in ("east", "auh", "huihe", "huisheng")]
        assert order == [0, 1, 2, 3], order


# ══ 2. 設定頁醫師代號由小到大 ════════════════════════════════════════════
class TestDoctorCodesSortNumerically:
    def test_a_shorter_code_is_smaller(self):
        """★字串排序會把 D6175 排到 D15645 後面★(逐字元比大小),
        而使用者要的是【數字】由小到大。"""
        k = main._doctor_code_sort_key
        assert k("D6175") < k("D14355") < k("D15728") < k("D35819")

    def test_prefixed_and_bare_codes_compare_by_number(self):
        """代號有兩種寫法(`D15728` 與純數字 `101823`)—— 都以數字比。"""
        k = main._doctor_code_sort_key
        assert k("D35819") < k("101358") < k("101823")

    def test_the_real_default_list_comes_out_ascending(self):
        """★用生產的那份清單量★(不是自己編的例子)。"""
        from cmuh_common.app_settings import DEFAULT_DOCTOR_SETTINGS
        codes = [d["doc_no"] for d in sorted(
            DEFAULT_DOCTOR_SETTINGS,
            key=lambda d: main._doctor_code_sort_key(d["doc_no"]))]
        nums = [int("".join(c for c in x if c.isdigit())) for x in codes]
        assert nums == sorted(nums), codes

    def test_a_code_without_digits_sorts_last_not_first(self):
        """★打錯/空白的代號不可以跳到清單頂端★ —— 當成 0 的話它會看起來
        像最小的代號,使用者會以為那是一筆正常資料。"""
        k = main._doctor_code_sort_key
        assert k("D6175") < k("") and k("D6175") < k("待補")
        assert k("101823") < k("")

    def test_the_treeview_refresh_sorts(self):
        """★接線★:helper 存在但沒人用 = 畫面順序不變。"""
        src = inspect.getsource(main.AutomationApp.refresh_doctors_treeview)
        assert "_doctor_code_sort_key" in src and "sorted(" in src, src

    def test_adding_a_doctor_re_sorts(self):
        """新增一律插在最後 —— 不重排的話新加的醫師會停在尾巴。"""
        src = inspect.getsource(main.AutomationApp._add_doctor)
        assert "_sort_doctors_treeview" in src, src


# ══ 3. 照光:開始療程的醫囑寫法 ═══════════════════════════════════════════
REAL = ("(2026/8/29)  Start phototherapy, start 400mj, add 100 each time,"
        " upper limit: 1200mj")


class TestTheStartOfCoursePhototherapyOrder:
    def test_the_real_case_now_parses(self):
        """★使用者實機那一行★:日期在最前、沒有次數、劑量寫成 `start 400mj`。"""
        info = parse_uvb_line(REAL)
        assert info is not None, "★仍然認不得 → 照光熱鍵不會動作★"
        assert info.dose == 400
        assert info.increase == 100
        assert info.max_dose == 1200
        assert info.last_date == date(2026, 8, 29)
        assert info.count is None, "第一次開始照光本來就還沒有次數"

    def test_it_updates_to_the_next_dose(self):
        """認得之後要算對:400 + 100 = 500,日期換成今天,上限 1200 不變。"""
        r = update_uvb_in_text(REAL, today=date(2026, 9, 2))
        assert r.action == "updated", r.action
        assert r.new_dose == 500
        assert "start 500mj" in r.new_text
        assert "(2026/09/02)" in r.new_text
        assert "upper limit: 1200mj" in r.new_text

    def test_a_frequency_is_not_mistaken_for_a_dose(self):
        """★核心安全條件★ `start 3 times per week` 的 3 是次數不是劑量。
        新分支要求數字後面★緊跟 mj★ —— 少了這條就會把 3 當成劑量,
        然後把病人的照光劑量寫成 103。

        ★反例要只靠這條規則分勝負★:第一版我沒有寫日期,於是整行本來就會
        因為「找不到日期」而回 None —— 把 mj 守衛整個拿掉也照樣 None,
        突變假綠燈。其他欄位(日期/加量/上限)必須齊全,
        讓唯一能擋下它的東西就是那個守衛。
        """
        bad = ("(2026/8/29) phototherapy, start 3 times per week,"
               " add 100 each time, upper limit: 1200mj")
        assert parse_uvb_line(bad) is None, (
            "★把「一週 3 次」的 3 當成劑量 → 下次會寫成 103mj★")

    def test_the_ceiling_is_still_not_taken_as_the_dose(self):
        """既有的安全條件不可以被新分支繞過:上限關鍵字後的數字是上限。"""
        assert parse_uvb_line("UVB max: 1000mj, MAX 1000") is None

    def test_existing_shapes_still_parse_the_same(self):
        """★對照組★:既有寫法逐位元不變(新分支不可以改到它們)。"""
        info = parse_uvb_line(
            "UVB 500 mj/cm2 (10) on (2026/7/8) add 30 each time, MAX: 800")
        assert info is not None
        assert (info.dose, info.count, info.increase, info.max_dose) == \
            (500, 10, 30, 800)

    def test_a_chinese_start_word_also_parses(self):
        """混中英是這個 repo 的常態,起始詞的中文寫法一樣要認得。"""
        info = parse_uvb_line(
            "(2026/8/29) phototherapy 起始 400mj, add 100 each time,"
            " upper limit: 1200mj")
        assert info is not None and info.dose == 400
