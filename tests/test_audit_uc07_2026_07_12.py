# -*- coding: utf-8 -*-
"""UC-07 回歸測試(2026-07-12 未審區域計畫書 §3/§8A 補修)。

Step C 跨日期「劑量=MAX」(capped)續行,已進 decay 區間(>7 天)不再靜默 bump 成今天
(原劑量未 ×0.75/×0.5 衰退,例 14 天前 800 應→600);且同款段不得進
uncertain_other_triplets(否則醫師按 Yes 仍 kept-dose 寫回未衰退值 —— codex 兩輪
指出的深層交互)。≤7 天與同日期續行、非 capped 第二療程行、excimer 行為不變。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.uvb_dose import update_uvb_in_text  # noqa: E402

T = date(2026, 7, 12)

_DRIVER = "UVB 800 mj/cm2 (10) on (2026/7/8) fixed at 800"


# ── 主重現(§8A 記錄的精確輸入):跨日期 capped + 14 天 → 不 bump ───────────────
def test_uc07_cross_date_capped_decay_not_bumped():
    txt = _DRIVER + "\nUVB 800 mj/cm2 (5) on (2026/6/28) fixed at 800"
    r = update_uvb_in_text(txt, today=T)
    assert r.action == "updated"
    lines = (r.new_text or "").split("\n")
    assert "(11)" in lines[0] and "(2026/07/12)" in lines[0]  # 驅動行正常更新
    # 14 天前 capped 續行:count/date/dose 全部原樣(修正前被 bump 成今天仍寫 800)
    assert lines[1] == "UVB 800 mj/cm2 (5) on (2026/6/28) fixed at 800", \
        f"14 天前 capped 續行被動到:{lines[1]!r}"


def test_uc07_capped_decay_segment_excluded_from_uncertain():
    # [codex] capped 且 >7 天的段不得進 uncertain —— 否則 Step C 跳過後,醫師對
    # uncertain 按 Yes 仍會 kept-dose bump 寫回未衰退劑量(800 而非 600)。
    txt = _DRIVER + "\nUVB 800 mj/cm2 (5) on (2026/6/28) fixed at 800"
    r = update_uvb_in_text(txt, today=T)
    assert not getattr(r, "uncertain_other_triplets", None)


# ── [codex P1] 同一行混合劑量:primary 非 capped + 續行 capped ────────────────
def test_uc07_same_line_mixed_dose_capped_continuation_excluded():
    # 不可 parse 整行拿 primary dose 判 capped(500<800 會誤放行)→ 須逐 triplet
    # 取「緊鄰該 triplet 前方的劑量」。
    txt = ("UVB 500 mj/cm2 (10) on (2026/7/8) add 30, MAX: 800 "
           "/ 800 mj/cm2 (5) on (2026/6/28)")
    r = update_uvb_in_text(txt, today=T)
    assert r.action == "updated"
    nt = r.new_text or ""
    assert "530" in nt and "(11)" in nt                    # 主段正常更新(500+30)
    assert "800 mj/cm2 (5) on (2026/6/28)" in nt           # capped 舊續行原樣保留
    assert not getattr(r, "uncertain_other_triplets", None)  # 也不得進 Yes/No


# ── 行內無 MAX 的跨日期 capped 段:Step C 拿驅動行 MAX 判 capped 而跳過,
#    uncertain 端該行 parse 失敗拿不到行 MAX → 須退驅動行 MAX,不然仍漏回寫 ──────
def test_uc07_capped_via_driver_max_no_line_max_excluded():
    txt = _DRIVER + "\nUVB 800 mj/cm2 (5) on (2026/6/28)"
    r = update_uvb_in_text(txt, today=T)
    lines = (r.new_text or "").split("\n")
    assert lines[1] == "UVB 800 mj/cm2 (5) on (2026/6/28)"
    assert not getattr(r, "uncertain_other_triplets", None)


# ── 邊界:7 天(保持劑量正確)照 bump;8 天(×0.75 區)不動 ──────────────────────
def test_uc07_boundary_day7_bumps_day8_does_not():
    r7 = update_uvb_in_text(
        _DRIVER + "\nUVB 800 mj/cm2 (5) on (2026/7/5) fixed at 800", today=T)
    line7 = (r7.new_text or "").split("\n")[1]
    assert "(6)" in line7 and "(2026/07/12)" in line7, \
        f"7 天 capped 續行未 bump(行為退化):{line7!r}"
    r8 = update_uvb_in_text(
        _DRIVER + "\nUVB 800 mj/cm2 (5) on (2026/7/4) fixed at 800", today=T)
    line8 = (r8.new_text or "").split("\n")[1]
    assert "(5)" in line8 and "(2026/7/4)" in line8, \
        f"8 天 capped 續行仍被 bump:{line8!r}"
    assert not getattr(r8, "uncertain_other_triplets", None)


def test_uc07_cross_date_capped_within_7_days_still_bumped():
    # ≤7 天不需衰退(2-6 天加量 cap MAX 結果=kept-dose)→ 既有安全 bump 行為保留
    txt = _DRIVER + "\nUVB 800 mj/cm2 (5) on (2026/7/6) fixed at 800"
    r = update_uvb_in_text(txt, today=T)
    line2 = (r.new_text or "").split("\n")[1]
    assert "(6)" in line2 and "(2026/07/12)" in line2, \
        f"6 天前 capped 續行未 bump(行為退化):{line2!r}"


# ── 行為保留:非 capped(dose<其行 MAX)第二療程行仍進 uncertain 問醫師 ─────────
def test_uc07_noncapped_cross_date_still_uncertain():
    txt = _DRIVER + "\nUVB 600 mj/cm2 (5) on (2026/6/28) fixed at 900"
    r = update_uvb_in_text(txt, today=T)
    line2 = (r.new_text or "").split("\n")[1]
    assert "(5)" in line2 and "(2026/6/28)" in line2       # Step C 不動(非 capped)
    u = getattr(r, "uncertain_other_triplets", None)
    assert u and u[0]["count"] == 5, "非 capped 第二療程行不該被排除出 uncertain"


# ── 行為保留:緊鄰 (count) 前無劑量數字的 excimer 行(v20.13 原始情境)仍問醫師 ──
def test_uc07_excimer_line_without_leading_dose_still_uncertain():
    txt = ("re- excimer 800 upper back (37) (2026/6/28) add 10mJ each time\n"
           "UVB 800 mj/cm2 (8) on (2026/7/8) add 50, fixed at 1000")
    r = update_uvb_in_text(txt, today=T)
    u = getattr(r, "uncertain_other_triplets", None)
    assert u and u[0]["count"] == 37, "excimer 跨日期行(14 天)不該被排除出 uncertain"
