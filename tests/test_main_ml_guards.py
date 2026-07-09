# -*- coding: utf-8 -*-
"""F 鍵 HIS 自動化中低風險 M1-M6 / L1 / L5 修正回歸（2026-07-09）。

main 可 headless import → 純邏輯項(M1 版本解析、M2 焦點嚴格判準)做行為測試;Win32-heavy
項(M3/M4/M5/L1/L5)以 inspect 原始碼守門鎖住修正。findings 出處:
docs/未審review_findings_主程式F鍵HIS自動化_2026-07-09.md。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import main  # noqa: E402


# ══ M1：HIS 版本守門(偵測+警示,不硬停)══════════════════════════════════════════
def test_m1_his_title_version_parse():
    assert main._his_title_version("西醫門診醫師作業 V.1150629.01") == "1150629"
    assert main._his_title_version("... V1150701 ...") == "1150701"
    assert main._his_title_version("西醫門診醫師作業") is None   # 無版本 → None
    assert main._his_title_version("") is None


def test_m1_warns_only_on_mismatch_and_never_on_missing(caplog):
    import logging
    # 版本不同 → WARNING
    main._his_version_checked = False
    with caplog.at_level(logging.WARNING):
        main._maybe_warn_his_version("西醫門診醫師作業 V.1150630.01")
    assert any("HIS版本" in r.message for r in caplog.records), "版本不同應警示"
    assert main._his_version_checked is True

    # 版本相同 → 不警示
    main._his_version_checked = False
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        main._maybe_warn_his_version("西醫門診醫師作業 V.1150629.01")
    assert not any("HIS版本" in r.message for r in caplog.records), "版本相同不應警示"

    # title 無版本字串 → 不動作、不假警報、flag 不設(避免早期抓不到就永久跳過)
    main._his_version_checked = False
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        main._maybe_warn_his_version("西醫門診醫師作業")
    assert not any("HIS版本" in r.message for r in caplog.records)
    assert main._his_version_checked is False


def test_m1_disable_is_deliberately_not_implemented():
    # 刻意只警示不硬停 F 鍵(版本字串誤判會鎖死醫師)→ 守門:不得出現「停用熱鍵」類硬停動作
    src = inspect.getsource(main._maybe_warn_his_version)
    assert "safe_unhook_all_hotkeys" not in src and "return" in src


# ══ M2：代碼輸入 focus 嚴格判準(previous_focus 未知時排除病歷 memo/rich)══════════
def _patch_focus(monkeypatch, focus_hwnd, cls_name):
    monkeypatch.setattr(main, "_get_thread_focus", lambda h: focus_hwnd)
    monkeypatch.setattr(main, "_get_class_name_of", lambda h: cls_name)
    monkeypatch.setattr(main, "_sleep_interruptible", lambda *a, **k: None)


def test_m2_unknown_focus_rejects_freetext_memo(monkeypatch):
    _patch_focus(monkeypatch, 500, "TMemo")   # 病歷內文區
    # previous_focus=0(讀不到)→ 嚴格:memo 不可當代碼輸入目標 → 逾時回 0
    assert main._wait_for_code_input_focus(100, previous_focus=0, timeout=0.02) == 0


def test_m2_unknown_focus_rejects_richedit(monkeypatch):
    _patch_focus(monkeypatch, 500, "TRichEdit")
    assert main._wait_for_code_input_focus(100, previous_focus=0, timeout=0.02) == 0


def test_m2_unknown_focus_rejects_generic_tedit(monkeypatch):
    # [codex] 一般 TEdit(可能是病人其他欄位)在 previous_focus 未知時也【不可】接受
    _patch_focus(monkeypatch, 500, "TEdit")
    assert main._wait_for_code_input_focus(100, previous_focus=0, timeout=0.02) == 0


def test_m2_unknown_focus_accepts_grid_inplace_edit(monkeypatch):
    _patch_focus(monkeypatch, 500, "TInplaceEdit")   # grid 內嵌編輯=代碼輸入欄(正面辨識)
    assert main._wait_for_code_input_focus(100, previous_focus=0, timeout=0.2) == 500


def test_m2_unknown_focus_accepts_stringgrid(monkeypatch):
    _patch_focus(monkeypatch, 500, "TStringGrid")
    assert main._wait_for_code_input_focus(100, previous_focus=0, timeout=0.2) == 500


def test_m2_known_focus_preserves_lenient_behavior(monkeypatch):
    # previous_focus 已知且焦點已改變 → 維持原本(input-like 即可,含 memo)
    _patch_focus(monkeypatch, 500, "TMemo")
    assert main._wait_for_code_input_focus(100, previous_focus=999, timeout=0.2) == 500


# ══ M3/M4/M5 + L1/L5 原始碼守門 ════════════════════════════════════════════════
def test_m3_popup_watcher_keys_by_hwnd_and_class_and_prunes():
    src = inspect.getsource(main._f11_popup_watcher)
    assert "key = (hwnd, cls_name)" in src, "M3: 應以 (hwnd, class) 為 key"
    assert "IsWindow(k[0])" in src, "M3: 應定期清掉已回收 hwnd 的項"
    # [codex] 同 hwnd+class 但視窗身分不同(內容變了=新實例)→ 重新處理,不沿用舊 handled
    assert "_popup_identity(" in src and "handled.get(key) != ident" in src, \
        "M3: 應以視窗身分偵測 hwnd 重用、避免同 class 新 popup 被跳過"


def test_m3_popup_identity_safe_on_invalid_hwnd():
    # 無效 hwnd 不得炸,回穩定 tuple(供 handled 比對)
    assert main._popup_identity(0) == ("", 0)


def test_m4_replace_edit_verifies_foreground_and_point():
    src = inspect.getsource(main._replace_edit_text)
    assert "_ensure_hospital_foreground(" in src, "M4: 點擊前應確保 HIS 前景"
    assert "_screen_point_in_window(" in src, "M4: 應驗證點擊點屬於目標欄位"
    # 驗證必須在實體 click 之前
    assert src.index("_screen_point_in_window(") < src.index(".click("), \
        "M4: 遮擋驗證必須在 pyautogui.click 之前"


def test_m5_precheck_normalizes_fullwidth():
    src = inspect.getsource(main._f11_precheck_card_for_phototherapy)
    assert "_f11_normalize_course_value(" in src, "M5: 療程判斷前應做全形正規化"


def test_l1_age_dialog_not_blindly_clicking_first_button():
    src = inspect.getsource(main.script_F9_F10_consent_form_adaptive)
    assert "len(buttons) == 1" in src, "L1: 單顆才直接點"
    assert 'bt in ("yes", "ok", "確定", "是")' in src, "L1: 多顆時只點 Yes/確定"


def test_l5_hotkey_guard_checks_32770_process():
    text = main.__file__  # 讀原始碼檔(guard 是巢狀函式,inspect 取不到)
    with open(text, encoding="utf-8") as f:
        content = f.read()
    assert "前景 #32770 不屬 HIS 行程" in content, \
        "L5: #32770 前景應額外驗證屬於 HIS 行程"
