# -*- coding: utf-8 -*-
"""外審補漏回歸測試(2026-07-12,codex GPT-5.6-sol deep audit)。

近 3-4 天未外審的 commit 補審後,確認 4 條 CONFIRMED:
  U1  遞減醫囑動詞與數字間夾中性字("decrease dose by 50 each time")仍須判為遞減,
      不得被 branch(b) 當 +50(該減反增 800→850)。
  U2  MAX 數字後接「.分隔日期」("until 2026.9.1")不可當上限 → 否則略過真 MAX:800
      寫回 830 破上限;但句尾句點 "MAX: 800." 仍要正常抓 800。
  U4  純自費 excimer 寫身份(01,計費欄)前須有最終 F12 閘門(script_F2/F3)。
  U5  純自費 excimer 「確認後」分支寫回處置欄前須有最終 F12 閘門。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.uvb_dose import (  # noqa: E402
    update_uvb_in_text, _find_uvb_increase, _UVB_MAX_RE,
)

TODAY = date(2026, 7, 10)


def _run(text):
    r = update_uvb_in_text(text, today=TODAY)
    return r, (getattr(r, "new_text", None) or "")


# ── U1:遞減動詞與數字間夾中性字(dose/dosage/劑量/the)仍判遞減 ──────────────────
def test_u1_decrease_dose_by_not_increase():
    text = "UVB 800 mj/cm2 (30) on (2026/7/8), decrease dose by 50 each time, MAX: 1000"
    r, nt = _run(text)
    assert "850" not in nt, f"'decrease dose by 50' 被當 +50 寫回 850(U1):{nt!r}"
    assert r.action != "updated", f"遞減醫囑被自動更新(U1):{r.action} {nt!r}"


def test_u1_decrease_filler_variants_direct():
    # 直接測 _find_uvb_increase:夾中性字的遞減一律回 None(不當加量)
    for seg in ("decrease dose by 50 each time",
                "reduce dosage by 50 each time",
                "decrease the dose by 50 each time",
                "每次減 劑量 50",  # 中文中性字
                "lower dose 50 each time"):
        assert _find_uvb_increase(seg) is None, f"'{seg}' 應判遞減卻回加量值"


def test_u1_plain_increase_still_detected():
    # 沒有遞減動詞的正常加量仍要抓到(確認 U1 沒過度收緊)
    assert _find_uvb_increase("50 each time") == 50
    assert _find_uvb_increase("add 50") == 50
    assert _find_uvb_increase("dose 50 each time") == 50  # 只有中性字、無遞減 → 仍加量


def test_u1_end_to_end_plain_increase_updates():
    text = "UVB 500 mj/cm2 (10) on (2026/7/8) 50 each time, MAX: 800"
    r, nt = _run(text)
    assert r.action == "updated" and "550" in nt, f"純 'N each time' 未當加量:{r.action} {nt!r}"


# ── U2:點分隔日期不可當 MAX;句尾句點仍要抓數字 ───────────────────────────────
def test_u2_dotted_date_does_not_break_max():
    text = "UVB 780 mj/cm2 (10) on (2026/7/7) add 50, treat until 2026.9.1, MAX: 800"
    r, nt = _run(text)
    assert r.action == "updated", f"未更新:{r.action} {nt!r}"
    assert "830" not in nt, f"until 2026.9.1 的 2026 被當 MAX、破上限寫回 830(U2):{nt!r}"
    assert "800 mj/cm2" in nt, f"未 cap 在真正的 MAX 800:{nt!r}"


def test_u2_max_re_rejects_dotted_year():
    # "until 2026.9.1" 的 2026 不可被 _UVB_MAX_RE 當上限
    m = _UVB_MAX_RE.search("treat until 2026.9.1")
    assert m is None or m.group(1) != "2026", f"點分日期年份被當 MAX:{m and m.group(1)!r}"


def test_u2_max_re_still_matches_sentence_period():
    # 句尾句點 "MAX: 800." 仍要抓到 800(不誤殺)
    m = _UVB_MAX_RE.search("MAX: 800.")
    assert m is not None and m.group(1) == "800", f"句尾句點誤殺 MAX 800:{m and m.group(1)!r}"
    m2 = _UVB_MAX_RE.search("MAX: 800")
    assert m2 is not None and m2.group(1) == "800"


def test_u2_slash_dash_dates_still_rejected():
    # 既有 UC-04 行為不得回退:/ - 分隔日期年份仍不可當 MAX
    for s in ("treat until 2026/9/1", "until 2026-9-1"):
        m = _UVB_MAX_RE.search(s)
        assert m is None or m.group(1) != "2026", f"'{s}' 年份被當 MAX:{m and m.group(1)!r}"


# ── U4/U5:純自費 excimer 寫回/寫身份前的最終 F12 閘門(原始碼層,防回退) ──────────
def _main_src():
    p = os.path.join(os.path.dirname(__file__), '..', 'src', 'main.py')
    with open(p, encoding='utf-8') as f:
        return f.read()


def _check_stop_before(src, needle, window=12):
    """needle 這行之前 window 行內須出現 check_stop()。"""
    lines = src.splitlines()
    idx = next((i for i, ln in enumerate(lines) if needle in ln), None)
    assert idx is not None, f"找不到目標行:{needle!r}"
    return any("check_stop()" in lines[j] for j in range(max(0, idx - window), idx))


def test_u4_identity_write_has_stop_gate_f2():
    src = _main_src()
    assert _check_stop_before(src, '_set_身份_自費("01", label="F2")'), \
        "script_F2 寫身份前缺 check_stop 閘門(U4)"


def test_u4_identity_write_has_stop_gate_f3():
    src = _main_src()
    assert _check_stop_before(src, '_set_身份_自費("01", label="F3")'), \
        "script_F3 寫身份前缺 check_stop 閘門(U4)"


def test_u5_confirmed_branch_write_has_stop_gate():
    src = _main_src()
    # 「確認後」分支的 _write_tmemo_text 前須有 check_stop;用其專屬 log 字串定位該寫回區塊
    lines = src.splitlines()
    idx = next((i for i, ln in enumerate(lines)
                if "(確認後)劑量已更新" in ln), None)
    assert idx is not None, "找不到『確認後』寫回區塊"
    # 該 log 在 _write_tmemo_text 成功分支內;往上 8 行內須見 check_stop()
    assert any("check_stop()" in lines[j] for j in range(max(0, idx - 8), idx)), \
        "確認後分支寫回前缺 check_stop 閘門(U5)"


def _func_body(src, func_def):
    start = src.find(func_def)
    assert start != -1, f"找不到 {func_def}"
    nxt = src.find("\ndef ", start + 1)
    return src[start:nxt if nxt != -1 else len(src)]


# ── U3/UD-01b:卡號(計費欄)寫回前的 F12 閘門 ────────────────────────────────
def test_u3_card_write_has_stop_gate():
    src = _main_src()
    lines = src.splitlines()
    idx = next((i for i, ln in enumerate(lines)
                if "_wm_settext_timeout(card_hwnd, result.card)" in ln), None)
    assert idx is not None, "找不到卡號寫回行"
    assert any("check_stop()" in lines[j] for j in range(max(0, idx - 6), idx)), \
        "卡號寫回前缺 check_stop 閘門(U3/UD-01b)"


# ── F3:#32770 取不到 HIS 行程 PID 時 fail-closed(不自動按是) ─────────────────
def test_f3_round4_fail_closed_on_zero_pid():
    body = _func_body(_main_src(), "def _f9_f10_round4_submit_and_confirm")
    assert "if not popup_pid:" in body, "round4 缺 popup_pid==0 的 fail-closed 分支(F3)"
    # fail-closed 分支須在自動 PostMessage IDYES 之前
    assert body.index("if not popup_pid:") < body.index("IDYES"), \
        "fail-closed 分支須在自動按是(IDYES)之前(F3)"


# ── F1:同意書 popup 只認【本 HIS 行程新開】、排除既有 stale popup ──────────────
def test_f1_consent_popup_scoped_by_pid_and_exclude():
    src = _main_src()
    assert "or_pid = _get_window_pid(or_hwnd)" in src, "未取 HIS 行程 PID(F1)"
    assert "stale_popup = _find_window_by_class_title" in src, "未快照既有 popup(F1)"
    assert "require_pid=or_pid" in src, "同意書 popup 查找未帶 require_pid=or_pid(F1)"
    assert "exclude_hwnd=stale_popup" in src, "同意書 popup 查找未排除 stale_popup(F1)"
