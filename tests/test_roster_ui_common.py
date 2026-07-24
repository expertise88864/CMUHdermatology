# -*- coding: utf-8 -*-
"""roster UI 純函式（不建立 Tk 視窗，可在無顯示器環境跑）。"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster.ui.common import (  # noqa: E402
    MEMBER_PALETTE, calendar_matrix, member_color, next_in_cycle, ym_add, ym_of,
)
from cmuh_common.roster.ui.day_tab import _rooms_summary, _split_codes  # noqa: E402


def test_split_codes_all_delimiters():
    assert _split_codes("A、B") == ["A", "B"]            # 頓號（畫面顯示用）
    assert _split_codes("A, B，C 、D") == ["A", "B", "C", "D"]
    assert _split_codes("") == [] and _split_codes(None) == []


def test_rooms_summary_handles_nonnumeric_rooms():
    slots = {"治療室": ["A"], "A101": ["B"], "診1": ["C", "D"], "放假": ["E"]}
    s = _rooms_summary(slots)
    assert "A101:B" in s and "診1:CD" in s               # 非數字房號也要顯示
    assert "治療室" not in s and "放假" not in s          # 特殊格不算房


def test_ym_add_wraps_year():
    assert ym_add("2026-08", 1) == "2026-09"
    assert ym_add("2026-12", 1) == "2027-01"
    assert ym_add("2026-01", -1) == "2025-12"
    assert ym_add("2026-08", -14) == "2025-06"
    assert ym_add("2026-08", 0) == "2026-08"


def test_ym_of():
    assert ym_of(date(2026, 8, 3)) == "2026-08"


def test_calendar_matrix_shape_and_placement():
    # 2026/8/1 = 週六 → 週一起始下，第一列前 5 格是 None，8/1 落在第 6 欄
    weeks = calendar_matrix(2026, 8)
    assert all(len(w) == 7 for w in weeks)
    assert weeks[0][:5] == [None] * 5
    assert weeks[0][5] == date(2026, 8, 1)      # 週六欄
    assert weeks[0][6] == date(2026, 8, 2)      # 週日欄
    # 攤平後恰好含全部 31 天、其餘為 None
    flat = [d for w in weeks for d in w]
    real = [d for d in flat if d is not None]
    assert real == [date(2026, 8, d) for d in range(1, 32)]
    assert len(flat) % 7 == 0


def test_calendar_matrix_month_starting_monday():
    # 2026/6/1 = 週一 → 第一格就是 6/1，無前導 None
    weeks = calendar_matrix(2026, 6)
    assert weeks[0][0] == date(2026, 6, 1)


def test_next_in_cycle_full_loop():
    ids = ["A", "B", "C"]
    assert next_in_cycle(None, ids) == "A"
    assert next_in_cycle("A", ids) == "B"
    assert next_in_cycle("C", ids) is None       # 尾端回 None
    assert next_in_cycle("ZZ", ids) is None       # 非名單 → 清掉
    assert next_in_cycle(None, []) is None        # 空名單


def test_member_color_stable_and_wraps():
    assert member_color(0) == MEMBER_PALETTE[0]
    assert member_color(len(MEMBER_PALETTE)) == MEMBER_PALETTE[0]   # 循環
    assert member_color(1) != member_color(0)


def test_vs_palette_disjoint_from_r_palette():
    """[2026-07-24 使用者] R 亮色盤與 VS 深色盤零重疊 → 合併月曆同格一線/三線不撞色。"""
    from cmuh_common.roster.ui.common import VS_MEMBER_PALETTE, vs_member_color
    assert set(MEMBER_PALETTE).isdisjoint(VS_MEMBER_PALETTE)
    assert len(set(VS_MEMBER_PALETTE)) == len(VS_MEMBER_PALETTE)   # 盤內不重複
    assert vs_member_color(0) == VS_MEMBER_PALETTE[0]
    assert vs_member_color(len(VS_MEMBER_PALETTE)) == VS_MEMBER_PALETTE[0]  # 循環
    # 同序位跨盤必不同色（撞色主因＝兩名單都從第 0 色起算）
    for i in range(8):
        assert member_color(i) != vs_member_color(i)


def test_tint_and_shade_colors():
    """[2026-07-24 使用者] 三線淡底深字的調色 helper：tint 變亮、shade 變暗、壞值原樣。"""
    from cmuh_common.roster.ui.common import shade_color, tint_color

    def lum(h):
        return (0.299 * int(h[1:3], 16) + 0.587 * int(h[3:5], 16)
                + 0.114 * int(h[5:7], 16))
    for c in ("#117A65", "#B9770E", "#7D3C98"):
        assert lum(tint_color(c)) > lum(c) + 60, "tint 應明顯變亮(淡底)"
        assert lum(shade_color(c)) < lum(c), "shade 應變暗(深字)"
    assert tint_color("bad") == "bad" and shade_color("") == ""   # 壞值原樣


def test_duty_cell_actually_uses_tint_for_vs():
    """[2026-07-24 事故] v2026.07.24.4 push 途中 OneDrive 把 duty.py/common.py 還原,
    tint 只剩測試、使用處全失——helper 測試照綠、實機三線仍實心色塊。
    這支直接釘「duty.py 的三線分支真的呼叫 tint/shade」,再被還原就整套紅。"""
    import inspect

    from cmuh_common.roster.ui import duty
    src = inspect.getsource(duty)
    assert "tint_color(base)" in src, "三線人員底色應為 tint_color(淡底)"
    assert "shade_color(base)" in src, "三線人員字色應為 shade_color(深字)"
    # 一線維持實心底 + fg_for 自動黑白字（結構性區分的另一半）
    assert "fg_for(base)" in src


# ── 2026-07-24 月曆易讀性/互動強化 ───────────────────────────────────────────
def test_card_body_bg_precedence_and_lightness():
    """格身淡底:平日假日＞週末＞平日白;三者相異;淡到成員色塊仍可讀(亮度夠高)。"""
    from cmuh_common.roster.ui.common import (
        CARD_BG, CARD_BODY_HOLIDAY, CARD_BODY_WEEKEND, card_body_bg,
    )
    assert card_body_bg(False, False) == CARD_BG
    assert card_body_bg(True, False) == CARD_BODY_WEEKEND
    assert card_body_bg(False, True) == CARD_BODY_HOLIDAY
    assert card_body_bg(True, True) == CARD_BODY_HOLIDAY, "假日優先於週末"
    assert len({CARD_BG, CARD_BODY_WEEKEND, CARD_BODY_HOLIDAY}) == 3

    def lum(h):
        return (0.299 * int(h[1:3], 16) + 0.587 * int(h[3:5], 16)
                + 0.114 * int(h[5:7], 16))
    for c in (CARD_BODY_WEEKEND, CARD_BODY_HOLIDAY):
        assert lum(c) > 225, "格身底色須極淡,不得蓋過成員色塊/文字對比"


def test_week_color_chip_matches_calendar_colors():
    """週色籤鍵值必須對齊 calendar_colors 的 PINK/GREEN——引擎換鍵名時這裡要跟著紅。"""
    from cmuh_common.roster.calendar_colors import GREEN, PINK
    from cmuh_common.roster.ui.common import WEEK_COLOR_CHIP
    assert set(WEEK_COLOR_CHIP) == {PINK, GREEN}
    for bg, fg, lab in WEEK_COLOR_CHIP.values():
        assert bg.startswith("#") and fg.startswith("#") and lab


def test_warn_severity_fg_covers_all_severities():
    from cmuh_common.roster.ui.common import WARN_SEVERITY_FG
    assert set(WARN_SEVERITY_FG) == {"error", "warn", "info"}
    assert len(set(WARN_SEVERITY_FG.values())) == 3


def test_duty_cell_uses_body_bg_week_chip_and_uniform_grid():
    """源碼守衛(比照 OneDrive 事故教訓):duty 月曆格真的用了格身淡底、週六週色籤、
    等寬欄/等高列;警告面板逐列依嚴重度上色。"""
    import inspect

    from cmuh_common.roster.ui import duty
    src = inspect.getsource(duty)
    assert "card_body_bg(" in src, "月曆格身應依週末/假日上淡底"
    assert "WEEK_COLOR_CHIP" in src and "week_key(d)" in src, \
        "週六標頭應顯示粉週/綠週色籤(ColorRule 週色)"
    assert 'uniform="cal"' in src and 'uniform="calrow"' in src, \
        "月曆應七欄等寬、各週等高(最大化時平均撐滿)"
    assert "WARN_SEVERITY_FG" in src and "itemconfig" in src, \
        "警告面板應逐列依嚴重度上色"
    assert "_IDLE_HINT" in src, "閒置狀態列應顯示操作提示"


def test_day_overview_marks_holiday_body_bg_and_wheel():
    """源碼守衛:day 月曆格認得平日國定假日(標頭+格身)、綁滾輪、等寬欄、閒置提示。"""
    import inspect

    from cmuh_common.roster.ui import day_tab
    src = inspect.getsource(day_tab)
    assert "card_body_bg(" in src and "CARD_HDR_HOLIDAY" in src, \
        "day 月曆格應標記平日國定假日(原本只認週末)"
    assert "holidays=holidays" in src, "_render_calendar 應把假日集合傳進格子"
    assert "bind_canvas_mousewheel(self._cal_canvas" in src, "月曆應吃滾輪"
    assert 'uniform="dcal"' in src, "day 月曆應七欄等寬"
    assert "_IDLE_HINT" in src, "閒置狀態列應顯示操作提示"


def test_mousewheel_helper_is_pointer_scoped():
    """滾輪 helper 必須以【指標落點】限定作用範圍(winfo_containing 沿 master 鏈),
    不得無條件 bind_all 亂捲其他元件——先前 codex 在總覽視窗抓過全域滾輪污染。"""
    import inspect

    from cmuh_common.roster.ui.common import bind_canvas_mousewheel
    src = inspect.getsource(bind_canvas_mousewheel)
    assert "winfo_containing" in src, "應檢查指標當下是否落在月曆區域內"
    assert "bind_all" in src, "Windows 滾輪事件不會自動到 Canvas,需 bind_all+落點檢查"
