# -*- coding: utf-8 -*-
"""[2026-07-26 審查] 剩餘 P1 三項:縮寫字中展開、目標劑量被當成目前劑量、間隔衰減被跳過。"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import abbrev_engine as ae  # noqa: E402
from cmuh_common import uvb_dose as u  # noqa: E402

T = date(2026, 7, 25)


# ── ① 縮寫在原生欄位的字邊界要用【欄位真實文字】確認 ────────────────────────────
def _native(monkeypatch, text, caret, suffix):
    monkeypatch.setattr(ae, "_get_focused_window_handle", lambda: 1)
    monkeypatch.setattr(ae, "_is_native_edit_control", lambda _h: True)
    monkeypatch.setattr(ae, "_get_edit_selection", lambda _h: (caret, caret))
    monkeypatch.setattr(ae, "_read_window_text", lambda _h: text)
    monkeypatch.setattr(ae, "_replace_edit_selection", lambda *a: True)
    monkeypatch.setattr(ae, "_send_message_timeout", lambda *a, **k: (True, 0))
    monkeypatch.setattr(ae.time, "sleep", lambda _s: None)
    return ae._replace_native_edit_suffix(suffix, "keep", 0.05, cursor_left=0)


@pytest.mark.parametrize("text", ["persist ", "病灶da ", "abc1st "])
def test_native_path_refuses_mid_word_expansion(monkeypatch, text):
    """★字中展開★ 上游是拿【內部 buffer】的前一字元判斷邊界,而 buffer 在 cool-down
    期間【不收按鍵】—— 使用者在 cooldown 內打的字沒進 buffer,前綴就是空的 → 邊界檢查
    通過 → 縮寫黏在別的字尾巴上照樣展開。原生路徑手上有欄位真實文字,是唯一權威判準。"""
    suffix = text[-3:]          # 最後兩字元 + 空白
    assert _native(monkeypatch, text, len(text), suffix) == ae._NATIVE_ABORT


@pytest.mark.parametrize("text", ["x st ", " st ", "(st "])
def test_native_path_allows_real_word_start(monkeypatch, text):
    """不可誤擋:前一字元是空白/標點/字首時照常展開。"""
    assert _native(monkeypatch, text, len(text), "st ") == ae._NATIVE_REPLACED


# ── ② 「increase to N」的 N 是目標,不是目前劑量 ───────────────────────────────
@pytest.mark.parametrize("head", [
    "UVB increase to 600", "excimer light increase to 600",
    "excimer light raise to 600", "excimer light 增加到 600",
    "UVB up to 600",
])
def test_target_dose_not_taken_as_current_dose(head):
    """★超過醫師寫的目標★ 舊版把「increase to 600」的 600 當成目前劑量 →
    再 +30 寫回 630,直接超過醫師寫的 600。"""
    r = u.update_uvb_in_text(
        f"{head} mj/cm2 (10) on (2026/7/20), add 30 each time, MAX 1000", today=T)
    assert r.action != u.UvbAction.UPDATED, f"「{head}」被當成目前劑量"
    assert "630" not in (r.new_text or "")


@pytest.mark.parametrize("text", [
    "UVB 500 mj/cm2 (10) on (2026/7/20), increase 30 each time, MAX 1000",
    "excimer light 600 mj/cm2 (10) on (2026/7/20), increase 50 each time, MAX 1000",
    "excimer light 700 m j/cm2 (10) on (2026/7/20), add 30 each time, MAX 1000",
])
def test_normal_dose_lines_unaffected(text):
    assert u.update_uvb_in_text(text, today=T).action == u.UvbAction.UPDATED


# ── ③ uncertain triplet 按「是」不可把未衰減劑量標成今天 ──────────────────────
def test_uncertain_triplet_carries_decayed_dose():
    """★病人安全★ 「Yes 套用」原本只改 count/date、不重算劑量 → 間隔已進衰減區的段
    會把未衰減的舊劑量標成今天照的(8-14 天應 ×0.75)。衰減桶的 compute_new_dose 只用
    dose/max,不需要 increase,所以算得準 —— 要一起算好寫進去。"""
    today = date(2026, 7, 12)
    txt = ("UVB 800 mj/cm2 (8) on (2026/7/8) add 50, fixed at 1000\n"
           "UVB 600 mj/cm2 (5) on (2026/6/28) fixed at 900")
    got = u._detect_uncertain_triplets(txt, today)
    t = [x for x in got if x["count"] == 5]
    assert t, "非 capped 的第二療程行仍要問醫師(既有行為不可退步)"
    t = t[0]
    assert t["days_ago"] == 14
    assert t["new_dose"] == 450, f"14 天應 ×0.75 → 450,實際 {t['new_dose']}"
    assert t["old_dose"] == 600
    assert not t["dose_not_recalculated"]
    # 套用後劑量真的變了
    applied = u.apply_uncertain_updates(txt, [t])
    assert "450 mj/cm2 (6)" in applied
    assert "600 mj/cm2 (6)" not in applied


def test_uncertain_triplet_flags_when_dose_cannot_be_attributed():
    """歸屬不到該段自己的劑量時無法算衰減 → 維持既有行為(仍問醫師),
    但一定要標記,呼叫端才能告訴醫師「這行的劑量不會重算」。"""
    today = date(2026, 7, 12)
    txt = ("re- excimer 800 upper back (37) (2026/6/28) add 10mJ each time\n"
           "UVB 800 mj/cm2 (8) on (2026/7/8) add 50, fixed at 1000")
    got = u._detect_uncertain_triplets(txt, today)
    t = [x for x in got if x["count"] == 37]
    assert t, "既有行為:仍要問醫師"
    assert t[0]["dose_not_recalculated"] is True
    assert t[0]["new_dose"] is None


def test_uncertain_dialog_states_only_what_it_does():
    """訊息只能陳述程式真的會做的事:會改劑量的行要寫出來,不會改的要明講不會改。"""
    import inspect
    import re as _re
    src = inspect.getsource(main_uncertain_block())
    src = _re.sub(r'("""|\'\'\')(?:.|\n)*?\1', "", src)
    assert "dose_not_recalculated" in src, "不重算的行必須在對話框裡標出來"
    assert "劑量 {u['old_dose']}→{u['new_dose']}" in src or "old_dose" in src


def main_uncertain_block():
    import main
    return main._update_uvb_dose_core


# ── 外審補強 ────────────────────────────────────────────────────────────────
def test_no_control_characters_in_dose_regexes():
    r"""★踩過的坑★ 在腳本裡寫 regex 時 `\b` 少一層跳脫會變成真正的 U+0008 backspace
    控制字元 —— regex 看起來像 `\bup\s+to`,實際是「backspace + up」,永遠不會命中,
    而且肉眼與一般編輯器都看不出來。整個模組掃一次。"""
    import io
    src = io.open(u.__file__, encoding="utf-8").read()
    bad = [(i, repr(ln[:60])) for i, ln in enumerate(src.splitlines(), 1)
           for ch in ln if ord(ch) < 9 or ord(ch) in (11, 12) or 13 < ord(ch) < 32]
    assert not bad, f"含控制字元:{bad[:3]}"


@pytest.mark.parametrize("head", [
    "excimer light up to 600", "excimer light adjust to 600",
    "excimer light 調到 600", "excimer light 加到 600",
])
def test_excimer_target_forms_all_covered(head):
    """外審指出我只測了 UVB 的 up to,excimer 沒測到 —— 而 excimer 走的是另一組 regex。"""
    r = u.update_uvb_in_text(
        f"{head} mj/cm2 (10) on (2026/7/20), add 30 each time, MAX 1000", today=T)
    assert r.action != u.UvbAction.UPDATED
    assert "630" not in (r.new_text or "")


def test_uncertain_decay_never_rewrites_a_target_dose():
    """★外審★ 「UVB increase to 600 …(5)…」的 600 是醫師要達到的【目標】。
    衰減邏輯若把它當成目前劑量,按「是」會寫成 increase to 450 —— 直接破壞醫囑。"""
    today = date(2026, 7, 12)
    txt = ("UVB 800 mj/cm2 (8) on (2026/7/8) add 50, fixed at 1000\n"
           "UVB increase to 600 mj/cm2 (5) on (2026/6/28) MAX 1000")
    t = [x for x in u._detect_uncertain_triplets(txt, today) if x["count"] == 5]
    assert t, "仍要問醫師"
    assert t[0]["new_dose"] is None, "目標值不可被改寫"
    assert t[0]["dose_not_recalculated"] is True, "要標記讓醫師知道劑量不會重算"
    applied = u.apply_uncertain_updates(txt, t)
    assert "increase to 600" in applied, "醫師寫的目標必須原封不動"
    assert "450" not in applied
