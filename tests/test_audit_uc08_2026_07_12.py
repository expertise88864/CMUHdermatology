# -*- coding: utf-8 -*-
"""UC-08 回歸測試(2026-07-12 未審區域計畫書 §3/§8A 補修)。

兩件套:
(A) format_uvb_line 日期替換改用 parse 時的 date_span(最先換)—— 行內同值日期出現
    兩次時不再換錯欄位(str.find 換第一個 ≠ parse 選中的那個)。
(B) Step C/_detect_uncertain_triplets:triplet 中段夾另一個裸日期 → 配對歸屬不明,
    不 bump/不納 uncertain —— Step A 更新後殘留的重複日期不再把驅動行自己的 count
    湊成幽靈 triplet 再 +1(count+2、行內兩處日期都被改、new_count 與文字不一致)。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.uvb_dose import (  # noqa: E402
    format_uvb_line, parse_uvb_line, update_uvb_in_text)

T = date(2026, 7, 12)


# ── 主重現(§8A 記錄的精確輸入):count 只 +1、僅 parse 選中的日期被換 ───────────
def test_uc08_duplicate_date_count_plus_one_only():
    txt = "UVB 500 mj/cm2 (10) 2026/7/8 done, next on (2026/7/8) add 30, MAX: 800"
    r = update_uvb_in_text(txt, today=T)
    assert r.action == "updated"
    nt = r.new_text or ""
    assert "(11)" in nt and "(12)" not in nt, f"count 應恰 +1:{nt!r}"
    assert r.new_count == 11                       # 回傳值與文字一致
    assert "2026/07/12 done" in nt                 # parse 選中的裸日期 → 今天
    assert "next on (2026/7/8)" in nt, \
        f"殘留的重複日期應原樣保留(不可被幽靈 triplet 改掉):{nt!r}"


def test_uc08_phantom_triplet_not_in_uncertain():
    # Step C 跳過的幽靈 triplet 也不得轉進 uncertain(否則 Yes 又 kept-dose 再 bump)
    txt = "UVB 500 mj/cm2 (10) 2026/7/8 done, next on (2026/7/8) add 30, MAX: 800"
    r = update_uvb_in_text(txt, today=T)
    assert not getattr(r, "uncertain_other_triplets", None)


# ── (A) 日期替換用 parse span:同值日期在 parse 選中處之前也不會被換錯 ─────────
def test_uc08_format_replaces_parse_time_span_not_first_find():
    # v20.15 型:行首日期與劑量後日期同值;parse 優先選劑量後那個 → 只能換它
    txt = "(2026/7/8) UVB 500 mj/cm2 (10) mark (2026/7/8), add 30, MAX: 800"
    r = update_uvb_in_text(txt, today=T)
    assert r.action == "updated", f"span 修正前此型會 SANITY_FAIL:{r.action}"
    nt = r.new_text or ""
    assert nt.startswith("(2026/7/8)"), f"行首日期不可被換(parse 選的是後者):{nt!r}"
    assert "mark (2026/07/12)" in nt


def test_uc08_format_uvb_line_unit_span_replacement():
    txt = "UVB 500 mj/cm2 (10) 2026/7/8 done, next on (2026/7/8) add 30, MAX: 800"
    info = parse_uvb_line(txt)
    assert info is not None and info.date_span is not None
    out = format_uvb_line(info, new_dose=530, new_count=11, today=T)
    assert "2026/07/12 done" in out and "next on (2026/7/8)" in out
    assert "(11)" in out and "(12)" not in out


# ── (B) Step B 附加行同款:重複日期不再 count+2 ───────────────────────────────
def test_uc08_step_b_additional_line_duplicate_date():
    txt = ("UVB 500 mj/cm2 (10) on (2026/7/8) add 30, MAX: 800\n"
           "UVB 600 mj/cm2 (5) 2026/7/8 done, next on (2026/7/8) add 30, MAX: 800")
    r = update_uvb_in_text(txt, today=T)
    assert r.action == "updated" and r.additional_lines_updated == 1
    line2 = (r.new_text or "").split("\n")[1]
    assert "(6)" in line2 and "(7)" not in line2, f"附加行 count 應恰 +1:{line2!r}"
    assert "next on (2026/7/8)" in line2           # 殘留日期原樣
    assert not getattr(r, "uncertain_other_triplets", None)


# ── 行為保留:中段乾淨的同日期續行照 bump(v20.12 既有行為)──────────────────
def test_uc08_clean_middle_same_date_continuation_still_bumps():
    txt = ("局部 手 UVB: 1500 mj/cm2 (136) on (2026/7/8) "
           "/ new for back 1500mj/cm2 (44) on (2026/7/8) "
           "add 50 each time, fixed at 1500")
    r = update_uvb_in_text(txt, today=T)
    assert r.action == "updated"
    nt = r.new_text or ""
    assert "(137)" in nt and "(45)" in nt          # 主段+續行都更新
    assert nt.count("(2026/07/12)") == 2
    assert "(2026/7/8)" not in nt


# ── 行為保留:跨行不受影響(第二行不同日期的 excimer 不動)─────────────────────
def test_uc08_unrelated_different_date_line_untouched():
    txt = ("UVB 500 mj/cm2 (10) on (2026/7/8) add 30, MAX: 800\n"
           "excimer light (25) 1000mJ on (2025/01/15) 2 shot, fixed at 1000")
    r = update_uvb_in_text(txt, today=T)
    assert r.action == "updated"
    assert "(25) 1000mJ on (2025/01/15)" in (r.new_text or "")
