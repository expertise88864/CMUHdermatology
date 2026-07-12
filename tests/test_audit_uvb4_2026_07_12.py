# -*- coding: utf-8 -*-
"""UVB批次4 回歸測試(2026-07-12 未審區域計畫書補修)。

UC-11 病歷矛盾(原劑量 > MAX)改交醫師確認、不再靜默壓回;
UD-05 療程欄寫入前正向把關(源碼守衛);UC-09 docstring 補回 15-21 桶。
UC-07/UC-08(dose parser Step B/C)因無 agent-C 原始重現輸入,依 §0「不確定不硬修」緩修。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.uvb_dose import update_uvb_in_text  # noqa: E402

T = date(2026, 7, 12)


# ── UC-11 原劑量 > 本行 MAX → CONFIRM_NEEDED(不靜默壓回) ──────────────────────
def test_uc11_dose_over_max_asks_confirm():
    r = update_uvb_in_text(
        "UVB 900 mj/cm2 (10) on (2026/7/6) add 50, MAX: 800", today=T)
    assert r.action == "confirm_needed", f"dose>MAX 未跳確認:{r.action}"
    assert "800" in (r.confirm_reason or "") and "900" in (r.confirm_reason or "")


def test_uc11_confirm_yes_then_applies():
    # 按 Yes 後 caller 以 skip_dose_sanity=True 重跑 → 正常套用(壓在 800)
    r = update_uvb_in_text(
        "UVB 900 mj/cm2 (10) on (2026/7/6) add 50, MAX: 800",
        today=T, skip_dose_sanity=True)
    assert r.action == "updated" and "800 mj/cm2" in (r.new_text or "")


def test_uc11_dose_equal_max_not_flagged():
    # dose == MAX(嚴格 > 不含等於)→ 正常更新,不誤攔合法固定劑量
    r = update_uvb_in_text(
        "UVB 800 mj/cm2 (10) on (2026/7/6) add 50, MAX: 800", today=T)
    assert r.action == "updated"


def test_uc11_dose_below_max_unchanged_behavior():
    r = update_uvb_in_text(
        "UVB 500 mj/cm2 (10) on (2026/7/6) add 50, MAX: 800", today=T)
    assert r.action == "updated" and "550" in (r.new_text or "")


def test_uc11_second_same_date_line_over_max_asks_confirm():
    # [codex] Step B 同日附加行:主行合法、第二行 dose>MAX → 也要跳確認(不靜默壓 800)
    txt = ("UVB 500 mj/cm2 (10) on (2026/7/6) add 30, MAX: 800\n"
           "UVB 900 mj/cm2 (5) on (2026/7/6) add 50, MAX: 800")
    assert update_uvb_in_text(txt, today=T).action == "confirm_needed"


def test_uc11_second_line_fixed_dose_not_flagged():
    # 第二行 dose==MAX(固定劑量行)不誤攔
    txt = ("UVB 500 mj/cm2 (10) on (2026/7/6) add 30, MAX: 800\n"
           "UVB 800 mj/cm2 (5) on (2026/7/6), MAX: 800")
    assert update_uvb_in_text(txt, today=T).action == "updated"


# ── UC-09 docstring 補回 15-21 ×0.5 桶 ───────────────────────────────────────
def test_uc09_docstring_lists_all_buckets():
    import cmuh_common.uvb_dose as m
    doc = m.__doc__ or ""
    assert "15-21" in doc and "0.5" in doc, "模組 docstring 未補回 15-21 ×0.5 桶"


# ── UD-05 療程欄寫入前正向把關(源碼守衛) ────────────────────────────────────
def test_ud05_therapy_field_prewrite_guard():
    p = os.path.join(os.path.dirname(__file__), "..", "src", "main.py")
    with open(p, encoding="utf-8") as f:
        src = f.read()
    body = src[src.find("def _set_療程_only"):]
    body = body[:body.find("\ndef ", 1)]
    # 寫入(WM_SETTEXT)前須先讀原值並對「非空且非個位數」擋下
    assert "_read_tmemo_text(liaocheng_hwnd)" in body, "UD-05 未在寫入前讀療程原值"
    assert 're.fullmatch(r"\\d", _療程_before)' in body, "UD-05 未做個位數正向把關"
    # 把關須在 WM_SETTEXT 之前
    assert body.index("_療程_before") < body.index("_wm_settext_timeout(liaocheng_hwnd"), \
        "UD-05 正向把關未置於寫入前"
