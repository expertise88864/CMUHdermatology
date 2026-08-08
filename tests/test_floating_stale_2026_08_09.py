# -*- coding: utf-8 -*-
"""[任務 #22] 浮動視窗沒有「快取／即時」的概念。

主表格早就有：`result["from_cache"]` → 狀態列寫「已載入上次快取，等待更新」
（main.py:11752 / 9467）。**浮動視窗把快取的燈號畫得跟即時的一模一樣** ——
醫師瞥一眼看到的是一個【看起來就是現在】的號碼，實際可能是幾分鐘前、甚至是
上一節門診留下的。浮窗是「瞥一眼」用的，那正是最不該說謊的地方。

★宣稱要對得上實際知道的事★ 這與 `is_live_final`（止掛寄信資格）、
`degraded_sources`（來源退回舊資料）是同一條原則，只是換到顯示面。
"""
import importlib

import pytest

fc = importlib.import_module("cmuh_common.floating_clinic")


def _rs(**kw):
    base = dict(room="101", slot="早上", doctor="陳醫師", light="12",
                waiting=3, fetched=True)
    base.update(kw)
    return fc.RoomStatus(**base)


def test_a_live_room_is_not_marked_stale():
    assert fc.room_card_view(_rs()).get("stale") is False


def test_a_cached_room_is_marked_stale():
    """★核心★ 快取的燈號必須標得出來。"""
    assert fc.room_card_view(_rs(stale=True))["stale"] is True


def test_the_light_is_still_shown_when_stale():
    """★標記 ≠ 藏起來★ 舊號碼仍然比沒有號碼有用，只是不可以假裝它是新的。"""
    v = fc.room_card_view(_rs(stale=True))
    assert v["light"] == "12"
    assert v["waiting"] == "3"


def test_the_default_is_live():
    """沒有明講就是即時 —— 但那要求上游【真的】把 from_cache 傳進來（見下面）。"""
    assert fc.RoomStatus(room="101").stale is False


@pytest.mark.parametrize("kw,why", [
    (dict(error=True), "離線"),
    (dict(stopped=True), "未開診"),
    (dict(closed=True), "關診"),
])
def test_stale_is_meaningless_without_a_light(kw, why):
    """★只有在真的宣稱一個號碼時，「舊資料」才有意義★

    「離線／未開診／關診」本來就沒有在講一個即時號碼，再標一次只是雜訊。
    """
    v = fc.room_card_view(_rs(stale=True, **kw))
    assert v["stale"] is False, f"{why} 不該再標一次舊資料"


def test_the_main_program_actually_passes_from_cache():
    """★接上去了才算數★

    `RoomStatus` 多一個欄位、預設 False —— 上游若沒傳，這個功能就是一句
    永遠為真的謊（每一筆都說是即時的）。用 AST 檢查真的有 `stale=` 這個
    關鍵字引數，而且值取自 `from_cache`。
    """
    import ast
    import inspect
    import os
    import sys
    sys.path.insert(0, os.path.join(
        os.path.dirname(__file__), "..", "src"))
    import main  # noqa: PLC0415
    src = inspect.getsource(main)
    found = []
    for node in ast.walk(ast.parse(src)):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "RoomStatus"):
            continue
        kws = {k.arg for k in node.keywords}
        if "stale" in kws:
            found.append(ast.dump(node))
    assert found, "★主程式從來沒有把 from_cache 傳給 RoomStatus★ —— 那個欄位永遠是 False"
    assert any("from_cache" in d for d in found), (
        "傳了 stale 但值不是取自 from_cache")


def test_the_renderer_reads_the_flag():
    """畫卡片的地方要真的用到它，否則資料模型對了、畫面照舊說謊。"""
    import ast
    import inspect
    import textwrap
    src = textwrap.dedent(inspect.getsource(fc.ClinicFloatingWindow._build_card))
    subs = [n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Subscript)
            and isinstance(n.slice, ast.Constant)
            and n.slice.value == "stale"]
    assert len(subs) >= 2, (
        f"畫卡片時只用到 {len(subs)} 次 `stale`（預期至少 2：文字標記 + 燈號顏色）")


# ── 排版:警語不可以被 32pt 的燈號蓋掉（外審 P2）─────────────────────────────
# 第一版把警語畫在 (pad, 74)、燈號畫在 (pad, 58) 且字級 32 —— 同一個 x、字框重疊，
# 而燈號【後畫】會蓋掉警語開頭；高 DPI 只會更糟。★正好在最需要它的時候被蓋掉★。
W = fc.ClinicFloatingWindow


def test_the_stale_note_row_clears_the_light():
    """警語的那一列要在燈號字框【之外】。

    這裡不量字（測試環境沒有 Tk 顯示），改成把排版寫成【宣告式的算式】並檢查
    它的不變量：`_LIGHT_HALF_H` 是 32pt 字半高的上界（涵蓋 150% DPI）。
    """
    assert W._STALE_ROW_Y >= W._LIGHT_ROW_Y + W._LIGHT_HALF_H, (
        f"警語列 {W._STALE_ROW_Y} 落在燈號字框內"
        f"（燈號中心 {W._LIGHT_ROW_Y} ± {W._LIGHT_HALF_H}）")


def test_the_half_height_bound_covers_high_dpi():
    """`_LIGHT_HALF_H` 要真的是上界：32pt 在 150% DPI 下約 64px 高、半高 32。"""
    assert W._LIGHT_HALF_H >= 32, (
        "半高上界不夠 —— 高 DPI 的機器上燈號仍然會壓到警語")


def test_a_stale_card_is_tall_enough_for_its_extra_row():
    assert W._CARD_H_OPEN_STALE >= W._STALE_ROW_Y + 12, (
        "卡片不夠高，警語會被畫到卡片外面")
    assert W._CARD_H_OPEN_STALE > W._CARD_H_OPEN, (
        "多了一列卻沒有長高 —— 那就是還在重疊")


def test_card_height_has_exactly_one_source_of_truth():
    """★高度只能有一個地方算★

    `_build_card` 與 `_content_height` 以前各寫一次三元式。一改就會漂，
    而漂掉的樣子是「視窗高度與卡片總高對不上」：最後一張被裁掉或多一塊黑。
    """
    import ast
    import inspect
    import textwrap
    src = inspect.getsource(fc)
    tree = ast.parse(src)
    users = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        body = textwrap.dedent(ast.unparse(node))
        if "_CARD_H_OPEN" in body and node.name != "_card_h":
            users.add(node.name)
    assert users == set(), (
        f"這些函式自己算卡片高，沒走 `_card_h`：{sorted(users)}")


def test_both_layout_and_height_go_through_card_h():
    import ast
    import inspect
    src = inspect.getsource(fc)
    callers = set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.FunctionDef):
            continue
        for n in ast.walk(node):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "_card_h"):
                callers.add(node.name)
    assert {"_build_card", "_content_height"} <= callers, (
        f"排版與高度計算沒有共用 `_card_h`：{sorted(callers)}")


def test_the_note_string_is_shared_between_measuring_and_drawing():
    """量寬度與畫出來要用同一個字串，否則寬度算錯、警語被右緣裁掉。

    ★要逐一盤點【兩個】使用點★ 第一版只檢查「`_STALE_NOTE` 這個名字有出現」
    —— 把量寬度那側換成別的字串仍然全綠（畫的那側還在用它）。突變抓到之後改成
    要求兩個函式各自都引用它。
    """
    import ast
    import inspect
    users = set()
    for node in ast.walk(ast.parse(inspect.getsource(fc))):
        if not isinstance(node, ast.FunctionDef):
            continue
        for n in ast.walk(node):
            if isinstance(n, ast.Name) and n.id == "_STALE_NOTE":
                users.add(node.name)
    assert {"_content_width", "_build_card"} <= users, (
        f"量寬度與繪製沒有共用同一個字串：{sorted(users)}")
    assert "上次快取" in fc._STALE_NOTE
