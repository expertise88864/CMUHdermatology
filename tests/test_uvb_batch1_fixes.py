# -*- coding: utf-8 -*-
"""UVB批次1 醫療急件回歸測試（§3E：UC-01/03/04，2026-07-10）。

三條實測重現的「寫回錯劑量」P1（審查主審已複驗）：
  UC-01 跨行縫合：UVB 行缺 MAX → borrow 下一行別的治療的 MAX，把 850 改成 700。
  UC-03 遞減醫囑：`decrease N each time` 被當 +N（方向反轉）；「劑量 each time」蓋過 add。
  UC-04 `till/until`：無 \b 吃到 "still"、且年份 2026/9/1 被當 MAX → 突破醫師上限。

修正後這些都不得再產生錯誤寫回：能算就算對、算不出就 PARSE_FAIL 交醫師（不確定不動作）。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.uvb_dose import update_uvb_in_text  # noqa: E402

TODAY = date(2026, 7, 10)


def _run(text):
    r = update_uvb_in_text(text, today=TODAY)
    return r, (getattr(r, "new_text", None) or "")


# ── UC-01 跨行縫合：不得把下一行別的治療的 MAX borrow 過來改錯劑量 ──────────────
def test_uc01_no_cross_line_max_stitching():
    text = ("UVB: 850 mj/cm2 keep\n"
            "excimer 500 mj (10) on (2026/7/8) add 30 MAX 700")
    r, nt = _run(text)
    # 「keep UVB 850」本應 SILENT_SKIP/PARSE_FAIL；絕不可被縫成 700 寫回
    assert "700 mj/cm2 keep" not in nt, f"UVB 850 被縫成 700（UC-01）：{nt!r}"
    assert r.action != "updated" or "850" in nt, f"850 被改動（UC-01）：{r.action} {nt!r}"


def test_uc01_second_variant_not_stitched():
    text = ("keep UVB 850 mj/cm2\n"
            "UVB 500 mj (10) on (2026/7/8) add 30, MAX: 700")
    r, nt = _run(text)
    assert "keep UVB 700" not in nt, f"第一行 850 被第二行 MAX 縫成 700（UC-01）：{nt!r}"


def test_uc01_normal_single_line_still_updates():
    # 同行有完整 MAX 的正常單行仍要正常更新（確認 UC-01 沒過度收緊）
    text = "UVB 500 mj/cm2 (10) on (2026/7/8) add 30 each time, MAX: 800"
    r, nt = _run(text)
    assert r.action == "updated" and "530" in nt, f"正常單行未更新：{r.action} {nt!r}"


# ── UC-03 遞減醫囑不得被當加量 ─────────────────────────────────────────────────
def test_uc03_decrease_not_treated_as_increase():
    text = "UVB 800 mj/cm2 (30) on (2026/7/8), decrease 50 each time, MAX: 1000"
    r, nt = _run(text)
    # 遞減醫囑：程式不能自動 +50 寫回 850；算不出加量 → PARSE_FAIL 交醫師
    assert "850" not in nt, f"decrease 50 被當 +50 寫回 850（UC-03）：{nt!r}"
    assert r.action != "updated", f"遞減醫囑被自動更新（UC-03）：{r.action} {nt!r}"


def test_uc03_reduce_taper_variants_not_increase():
    for verb in ("reduce 50 each time", "taper 50 each visit", "lower 50 each time"):
        text = f"UVB 800 mj/cm2 (30) on (2026/7/8), {verb}, MAX: 1000"
        r, nt = _run(text)
        assert "850" not in nt, f"'{verb}' 被當 +50（UC-03）：{nt!r}"


def test_uc03_add_wins_over_dose_each_time():
    # 「劑量 150 each time」在前、明寫 add 20 在後 → 應採 add 20（+20）而非 +150
    text = "UVB: 150 mj/cm2 each time, add 20, (10) on (2026/7/8), MAX: 500"
    r, nt = _run(text)
    assert r.action == "updated", f"未更新：{r.action} {nt!r}"
    assert "170" in nt and "300" not in nt, f"increase 取到劑量 150 而非 add 20（UC-03）：{nt!r}"


def test_uc03_plain_each_time_still_works():
    # 沒有 decrease、也沒有 add 關鍵字的「N each time」仍應正常當加量
    text = "UVB 500 mj/cm2 (10) on (2026/7/8) 50 each time, MAX: 800"
    r, nt = _run(text)
    assert r.action == "updated" and "550" in nt, f"純 'N each time' 未當加量：{r.action} {nt!r}"


# ── UC-04 till/until 不得吃到 "still" 或日期年份而突破上限 ──────────────────────
def test_uc04_until_year_not_treated_as_max():
    text = "UVB 780 mj/cm2 (10) on (2026/7/7) add 50, treat until 2026/9/1, MAX: 800"
    r, nt = _run(text)
    assert r.action == "updated", f"未更新：{r.action} {nt!r}"
    # 780+50=830 但真 MAX=800 → 必須 cap 在 800，不得寫回 830（UC-04）
    assert "830" not in nt, f"until 2026 年份被當 MAX、突破上限寫回 830（UC-04）：{nt!r}"
    assert "800 mj/cm2" in nt, f"未 cap 在真正的 MAX 800：{nt!r}"


def test_uc04_still_not_treated_as_till():
    # "still 900" 內含 till，但沒有真正的 MAX 行 → 不得被當結構完整而自動更新
    text = "UVB: 500 mj/cm2 (5) on (2026/7/8) add 30, still 900 if tolerated"
    r, nt = _run(text)
    assert r.action != "updated", f"'still 900' 被當 MAX 自動更新（UC-04）：{r.action} {nt!r}"


def test_uc04_legit_until_max_still_caps():
    # 正常的 "MAX: N" 仍要正常當上限（確認 UC-04 lookahead 沒誤傷）
    text = "UVB 1180 mj/cm2 (30) on (2026/7/8) add 50 each time, MAX: 1200"
    r, nt = _run(text)
    assert r.action == "updated" and "1200 mj/cm2" in nt, \
        f"正常 MAX cap 失效（UC-04 lookahead 誤傷）：{r.action} {nt!r}"
