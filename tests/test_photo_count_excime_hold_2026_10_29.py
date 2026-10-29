# -*- coding: utf-8 -*-
"""[2026-10-29 實機] F2 照光更新三個問題(處置原文來自使用者回報,無病人識別資料):

1. 次數補零 '(02)':parse 讀到 2,但替換只找字面 '(2)' → 次數沒改 → round-trip verify
   失敗 → F2 只跳警告不更新。
2. 漏打 r 的 'excime':分流認得(判純 excimer),劑量更新卻只認完整 'excimer' →
   四行 excimer 只有第一行沒改到日期。
3. 使用者定案:照光行最後一個箭頭子句是 hold/stop → 整行不動、分流也不算。
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import uvb_dose as u  # noqa: E402

TODAY = date(2026, 10, 29)

UVB_MTX = "\r\n".join([
    "UVB: 500 mj/cm2 (02) on (2026/10/27) add 100 each time, fixed at 1000, w1+4n, "
    "take picture on 2023/5/18, ",
    "try MTX 3# on (2023/7/20) -> 6# on (2023/8/10) -> 5# on (2024/4/11) -> 4 # on (2024/6/6) "
    "-> hold, as prophylaxis due to go abroad -> 3# on (2024/12/26) -> hold -> 3# on (2026/6/29) ",
    "有告知病人，藥物有肝臟骨髓受損可能與致畸胎, 性，服藥過程與停藥，三個月內，禁止懷孕及捐, 血"])


def _changed(old: str, new: str) -> dict:
    return {i: b for i, (a, b) in enumerate(zip(old.splitlines(), new.splitlines(), strict=True)) if a != b}


class TestZeroPaddedCount:
    @pytest.mark.parametrize("src, old, new, want", [
        ("x (02) y", 2, 3, "x (03) y"),
        ("x (09) y", 9, 10, "x (10) y"),
        ("x (2) y", 2, 3, "x (3) y"),
        ("x （ 07 ） y", 7, 8, "x （ 08 ） y"),
        ("x (12) (2) y", 2, 3, "x (12) (3) y"),     # 不可吃到 (12) 的尾巴
    ])
    def test_replace_count_token(self, src, old, new, want):
        assert u._replace_count_token(src, old, new) == want

    def test_the_reported_uvb_line_updates(self):
        r = u.update_uvb_in_text(UVB_MTX, TODAY)
        assert r.action == u.UvbAction.UPDATED and r.new_dose == 600 and r.new_count == 3
        changed = _changed(UVB_MTX, r.new_text)
        assert list(changed) == [0], "MTX 那幾行不可以被動到"
        assert "600 mj/cm2 (03) on (2026/10/29)" in changed[0]
        assert u.uvb_written_back_ok(r.new_text, 600, 3, TODAY)


class TestExcimeTypo:
    LINES = [
        "excime  右側耳上方 2000 mj/cm2 on (2026/10/27) add 100 each, MAX 2000 mj/cm2 ",
        "excimer 右側頭頂 2000 mj/cm2 on (2026/10/27) add 100 each, MAX 2000 mj/cm2 ",
        "excimer 左側前面 2000 mj/cm2 on (2026/10/27) add 100 each, MAX 2000 mj/cm2 ",
        "excimer 左側耳後兩個一起照 2000 mj/cm2 on (2026/10/27) add 100 each, MAX 2000 mj/cm2 ",
        "IL on (2024/7/9) (2026/9/22) (2026/10/20) ",
        "OMP taper to 5# TIW->3# on (2024/6/11) -> BIW on (2025/4/8) ",
        "explain baricitinib -> hold due to much improved -> (2025/8/26) education ritlecitinib",
    ]

    def test_all_four_excimer_lines_get_todays_date(self):
        memo = "\r\n".join(self.LINES)
        r = u.update_uvb_in_text(memo, TODAY)
        assert r.action == u.UvbAction.UPDATED
        changed = _changed(memo, r.new_text)
        assert sorted(changed) == [0, 1, 2, 3]
        assert all("on (2026/10/29)" in changed[i] for i in range(4))
        assert changed[0].startswith("excime  右側耳上方 2000 mj/cm2")

    def test_chinese_marker_is_recognized(self):
        memo = "準分子 600 mj/cm2 on (2026/10/27) add 30 each, MAX 900"
        r = u.update_uvb_in_text(memo, TODAY)
        assert r.action == u.UvbAction.UPDATED and "630 mj/cm2 on (2026/10/29)" in r.new_text


class TestHeldLines:
    HELD = "脖子*1 excimer 1240 mj/cm2, add 10 each, MAX 1600 -> hold due to improved"
    ACTIVE = "左眼皮內側*1 excimer 540 mj/cm2 on (2026/10/25) add 40 each, MAX 800"

    @pytest.mark.parametrize("line, held", [
        (HELD, True),
        ("UVB 500 (5) on (2026/9/1) add 30 each time, MAX 1000 → stop", True),
        ("UVB 500 (5) on (2026/9/1) add 30, MAX 1000 -> stopped due to pregnancy", True),
        ("excimer 600 -> hold -> 650 on (2026/10/20) add 30 each, MAX 900", False),  # 恢復了
        ("UVB 500 (5) on (2026/9/1) add 30, MAX 1000, MTX 3# -> hold", False),       # hold 可能指藥
        ("explain baricitinib -> hold due to much improved", False),                 # 不是照光
        ("excimer 600 mj/cm2 on (2026/10/20) add 30 each, MAX 900", False),
    ])
    def test_which_lines_are_held(self, line, held):
        assert u._is_held_photo_line(line) is held

    def test_held_line_is_left_untouched(self):
        memo = "\r\n".join([self.ACTIVE, "OMP on (2026/03/30) ", self.HELD,
                            "pic on (2024/10/24) (2024/12/24) (2025/4/14) ..."])
        r = u.update_uvb_in_text(memo, TODAY)
        assert r.action == u.UvbAction.UPDATED
        changed = _changed(memo, r.new_text)
        assert list(changed) == [0] and "580 mj/cm2 on (2026/10/29)" in changed[0]
        assert r.new_text.split("\r\n")[2] == self.HELD
        assert r.new_text.count("\r\n") == memo.count("\r\n"), "換行要原樣保留"

    def test_only_held_orders_are_not_an_active_phototherapy(self):
        assert u.detect_phototherapy_kind(self.HELD, TODAY) == "none"
        held_uvb = "UVB 500 mj/cm2 (5) on (2026/10/20) add 30 each time, MAX 1000 -> hold"
        assert u.detect_phototherapy_kind(held_uvb + "\n" + self.ACTIVE, TODAY) == "pure_excimer"

    def test_a_held_excimer_continuation_without_the_keyword_is_untouched(self):
        """[外審 PR #13 第 1 輪] 續行沒寫 excimer 字眼也要認得出暫停。"""
        memo = ("excime cheek 500mj/cm2 (10) on (2026/10/27) add 30 each time, MAX 1000\r\n"
                "scalp 600mj/cm2 (20) on (2026/10/27) add 30 each time, MAX 1000 -> hold")
        r = u.update_uvb_in_text(memo, TODAY)
        assert r.action == u.UvbAction.UPDATED
        changed = _changed(memo, r.new_text)
        assert list(changed) == [0] and "530mj/cm2 (11) on (2026/10/29)" in changed[0]
        # 恢復過的續行 / 帶藥物劑量的續行照舊當進行中
        assert not u._is_held_photo_line(
            "scalp 600mj/cm2 (20) on (2026/10/27) add 30 each time, MAX 1000 -> hold -> 600 on (2026/10/27)")

    def test_a_single_held_line_is_not_updated(self):
        r = u.update_uvb_in_text(self.HELD, TODAY)
        assert r.action != u.UvbAction.UPDATED and r.new_text is None
