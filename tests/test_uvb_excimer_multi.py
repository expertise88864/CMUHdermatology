# -*- coding: utf-8 -*-
"""excimer 多段劑量回歸測試（2026-07-09 實機 bug）。

實機三張附圖：F2/F3 只改「每個處置欄第一個 excimer 關鍵字後的第一個劑量」，導致
  - 圖三：同一行第二段 `excimer light 440` 沒改（日期共用所以有改）。
  - 圖二：續行 `左右頭皮各一發: 600mj/cm2`（無 excimer 關鍵字）整段沒改。
  - 圖一：續行 `1100mJ/cm2 (68) for middle neck`（無 excimer 關鍵字）整段沒改。

根因：`_update_excimer_lines` 每行只 `search` 一個 marker、只更新其後第一個劑量。修正後
應更新【每一個】excimer 劑量段：同行多 marker + 結構完整（dose+increase+MAX）的續行。

安全底線：續行必須三者齊全（劑量/加量/上限）才視為延續，避免誤改衛教/病史裡的數字。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.uvb_dose import update_uvb_in_text  # noqa: E402

TODAY = date(2026, 7, 9)


def _run(text):
    r = update_uvb_in_text(text, today=TODAY)
    return r, (getattr(r, "new_text", None) or getattr(r, "text", None) or "")


# ── 圖三：同一行兩個 excimer marker，共用句尾日期/上限/加量 ────────────────────
def test_same_line_two_markers_both_doses_updated():
    text = ("excimer light 580mJ (151) for cheek, "
            "excimer light 440mJ (147) for R ear, "
            "on (2026/7/6) add 10mJ each time fixed at 700mJ, 3 shot")
    r, nt = _run(text)
    assert r.action == "updated"
    assert "590mJ (152)" in nt, f"第一段 580→590 未更新：{nt!r}"
    assert "450mJ (148)" in nt, f"第二段 440→450 未更新（本次修正重點）：{nt!r}"
    assert "2026/07/09" in nt, f"共用日期未更新：{nt!r}"
    assert "fixed at 700mJ" in nt, f"上限被誤改：{nt!r}"


# ── 圖二：續行無 excimer 關鍵字、無日期（只加劑量）─────────────────────────────
def test_continuation_line_no_keyword_undated_updates_dose():
    text = ("excimer light brow 1 shot/back 3 shot 960mj/cm2, "
            "add 30 mj/cm2 each time, MAX: 1000 mj/cm2\n"
            "    scalp 1 shot: 600mj/cm2, add 30 mj/cm2 each time, MAX: 1000 mj/cm2")
    r, nt = _run(text)
    assert r.action == "updated"
    assert "990mj/cm2" in nt, f"第一行 960→990 未更新：{nt!r}"
    assert "630mj/cm2" in nt, f"續行 600→630 未更新（本次修正重點）：{nt!r}"
    assert "MAX: 1000" in nt, f"上限被誤改：{nt!r}"


# ── 圖一：續行無 excimer 關鍵字、有日期、已達 fixed 上限 ──────────────────────
def test_continuation_line_no_keyword_dated_caps_at_fixed():
    text = ("eyes Excimer : 1080 mj/cm2(196) on (2026/7/6) (each 1 shot) "
            "add 20 each time, Max: 1100\n"
            "1100mJ/cm2 (68) for middle neck/ left neck on (2026/7/6) , "
            "add 20 each time, fixed at 1100 4 shot")
    r, nt = _run(text)
    assert r.action == "updated"
    assert "1100 mj/cm2(197)" in nt, f"第一行 1080→1100、次數 196→197 未更新：{nt!r}"
    # 續行：1100 已達 fixed 1100 → cap 不變；次數 68→69；日期 7/6→07/09
    assert "1100mJ/cm2 (69)" in nt, f"續行次數 68→69 未更新（本次修正重點）：{nt!r}"
    assert nt.count("2026/07/09") == 2, f"兩段日期都應更新為今天：{nt!r}"
    assert "fixed at 1100" in nt, f"fixed 上限被誤改：{nt!r}"


# ── 安全底線：續行沒有完整劑量結構（衛教/病史）→ 絕不誤改 ─────────────────────
def test_non_excimer_continuation_not_touched():
    text = ("excimer light 580mJ (151) for cheek on (2026/7/6) "
            "add 10 each time, MAX: 700\n"
            "discuss topical steroid, apply BID, f/u 2 weeks, avg 300 patients")
    r, nt = _run(text)
    assert r.action == "updated"
    assert "590mJ (152)" in nt, f"excimer 行本身應更新：{nt!r}"
    # 續行是純衛教（無 add/MAX 結構）→ 其中的 300 絕不可被當劑量更改
    assert "avg 300 patients" in nt, f"衛教行被誤改（醫療安全紅線）：{nt!r}"


# ── 安全底線：非 excimer 行插在中間 → 中斷延續，不跨越誤改 ─────────────────────
def test_continuation_chain_broken_by_non_excimer_line():
    text = ("excimer light 580mJ (151) for cheek on (2026/7/6) "
            "add 10 each time, MAX: 700\n"
            "OMP 20mg QD for 4 weeks then review\n"
            "700mj/cm2, add 10 each time, MAX: 900")
    r, nt = _run(text)
    # 第一行 excimer 更新；OMP 行中斷鏈 → 第三行雖有 dose+add+MAX 結構也不當延續（保守）
    assert "590mJ (152)" in nt, f"第一行 excimer 應更新：{nt!r}"
    assert "OMP 20mg" in nt, f"OMP 行不可被改：{nt!r}"
    assert "700mj/cm2, add 10 each time, MAX: 900" in nt, \
        f"鏈被非 excimer 行中斷後不應誤改第三行：{nt!r}"


# ── 安全底線 [codex P1-3]：前一段自己的 add/MAX 不得讓後一段的劑量被誤判掉 ──────────
def test_same_line_each_segment_has_own_add_max_both_update():
    text = ("excimer light 500mJ (10) on (2026/7/6) add 20 each time MAX 800, "
            "excimer light 600mJ (5) on (2026/7/6) add 20 each time MAX 800")
    r, nt = _run(text)
    assert r.action == "updated"
    assert "520mJ (11)" in nt, f"第一段 500→520：{nt!r}"
    assert "620mJ (6)" in nt, \
        f"第二段 600→620 被前段的 add/MAX 誤判掉（codex P1-3）：{nt!r}"


def test_same_line_each_segment_has_own_fixed_both_update():
    text = ("excimer light 810mJ (10) on (2026/7/6) increase 20 fixed at 900, "
            "excimer light 600mJ (5) on (2026/7/6) increase 20 fixed at 900")
    r, nt = _run(text)
    assert r.action == "updated"
    assert "830mJ (11)" in nt, f"第一段 810→830：{nt!r}"
    assert "620mJ (6)" in nt, \
        f"第二段 600→620 被前段的 fixed at 誤判掉（codex P1-3）：{nt!r}"


# ── 安全底線 [codex P1-4]：一段的 maintain 不得壓掉另一段的 add（維持劑量只限本段）─────
def test_same_line_maintain_on_one_segment_does_not_suppress_other():
    text = ("excimer light 500mJ (10) on (2026/7/6) maintain dose, MAX 800, "
            "excimer light 600mJ (5) on (2026/7/6) add 20 each time, MAX 800")
    r, nt = _run(text)
    assert r.action == "updated"
    # 第一段 maintain → 維持 500（次數仍 10→11、日期→今天）
    assert "500mJ (11)" in nt, f"第一段 maintain 應維持 500：{nt!r}"
    # 第二段有自己的 add 20 → 必須遞增到 620，不得被第一段的 maintain 壓掉
    assert "620mJ (6)" in nt, \
        f"第二段被前段 maintain 誤壓、沒遞增（codex P1-4）：{nt!r}"


# ── 安全底線 [codex P1-2]：同行兩段各有自己的日期 → 用自己的、不互相偷（套錯 staleness）──
def test_same_line_segments_use_own_local_dates():
    # 第一段自己有日期 7/6（距今 3 天，2-6 天桶 → +increase）、第二段自己有日期 7/2
    # （距今 7 天 → 維持同劑量）。各段用【自己的】日期算天數桶，互不干擾。
    text = ("excimer light 500mJ (10) on (2026/7/6) add 20 each time, MAX: 800, "
            "excimer light 600mJ (5) on (2026/7/2) add 20 each time, MAX: 800")
    r, nt = _run(text)
    assert r.action == "updated"
    assert "520mJ (11)" in nt, f"第一段 7/6(3天)→500+20=520、次數 10→11：{nt!r}"
    assert "600mJ (6)" in nt, f"第二段 7/2(7天)→維持 600、次數 5→6（不得被第一段日期污染）：{nt!r}"
    assert nt.count("2026/07/09") == 2, f"兩段各自的日期都應更新為今天：{nt!r}"


# ── 安全底線 [codex P1-5]：結構完整但明講「病史/過去」的續行 → 絕不改動歷史醫療文字 ──
def test_history_like_continuation_line_not_modified():
    text = ("excimer light 580mJ (151) for cheek on (2026/7/6) add 10 each time, MAX: 700\n"
            "previous regimen 500mj/cm2, add 20 each time, MAX: 800")
    r, nt = _run(text)
    assert r.action == "updated"
    assert "590mJ (152)" in nt, f"當次 excimer 應更新：{nt!r}"
    # 第二行有 dose+add+MAX 但明講 previous → 屬病史，絕不可被當延續改成 520
    assert "previous regimen 500mj/cm2" in nt, \
        f"病史行(previous)被誤改（醫療安全紅線 codex P1-5）：{nt!r}"


def test_last_time_continuation_line_not_modified():
    text = ("excimer light 580mJ (151) for cheek on (2026/7/6) add 10 each time, MAX: 700\n"
            "上次 600mj/cm2, add 20 each time, MAX: 800")
    r, nt = _run(text)
    assert r.action == "updated"
    assert "上次 600mj/cm2" in nt, f"『上次』病史行被誤改（醫療安全紅線）：{nt!r}"


# ── 安全底線 [codex P1]：只「提及」excimer（無劑量）不得讓下一行被當延續誤改 ──────
def test_bare_excimer_mention_does_not_enable_continuation():
    text = ("discuss excimer treatment options with patient\n"
            "previous regimen 500mj/cm2, add 20 each time, MAX: 800")
    r, nt = _run(text)
    # 第一行只提及 excimer、無劑量醫令 → 不啟用延續；第二行(病史)絕不可被當 excimer 改動。
    # 沒有可辨識的照光劑量醫令 → 不應更新（action != updated），病史 500 絕不可變 520。
    assert r.action != "updated", f"純提及 excimer 不該觸發更新（醫療安全紅線）：{r.action}"
    assert "520" not in nt, f"病史劑量行 500 被誤改成 520（醫療安全紅線）：{nt!r}"


# ── 真實中文處置欄（實機圖三/圖二原文）─────────────────────────────────────────
def test_real_chinese_figure3_same_line():
    text = ("excimer light 580mJ (151) for 嘴周圍, excimer light 440mJ (147) "
            "for 右下耳 (圓形), on (2026/7/6) add 10mJ each time fixed at 700mJ, 3 shot")
    r, nt = _run(text)
    assert r.action == "updated"
    assert "590mJ (152) for 嘴周圍" in nt, f"圖三第一段：{nt!r}"
    assert "450mJ (148) for 右下耳" in nt, f"圖三第二段(修正重點)：{nt!r}"
    assert "fixed at 700mJ" in nt


def test_real_chinese_figure2_continuation():
    text = ("excimer light 左右眉毛各一發/上背三發960mj/cm2, add 30 mj/cm2 each time, "
            "MAX: 1000 mj/cm2\n"
            "左右頭皮各一發: 600mj/cm2, add 30 mj/cm2 each time, MAX: 1000 mj/cm2")
    r, nt = _run(text)
    assert r.action == "updated"
    assert "上背三發990mj/cm2" in nt, f"圖二第一行：{nt!r}"
    assert "左右頭皮各一發: 630mj/cm2" in nt, f"圖二續行(修正重點)：{nt!r}"


# ── 回歸：單段（最常見）仍正確，未被多段重構破壞 ─────────────────────────────
def test_single_segment_still_works():
    text = "excimer light 500mJ (10) for face on (2026/7/6) add 20 each time, MAX: 800"
    r, nt = _run(text)
    assert r.action == "updated"
    assert "520mJ (11)" in nt, f"單段 500→520、次數 10→11：{nt!r}"
    assert "2026/07/09" in nt
