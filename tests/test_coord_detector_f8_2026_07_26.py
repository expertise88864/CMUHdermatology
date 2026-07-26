# -*- coding: utf-8 -*-
"""[2026-07-26 未審模組 review] 座標偵測器與主程式的 F8 衝突。

座標偵測器(六支程式裡唯一從未被 review 過的)用 F8 記錄座標;
主程式的 F8 是「快速輸入 A126585189」,而且在 `NO_GUARD_HOTKEYS` 裡 ——
【刻意】跳過醫院視窗檢查,任何 app 都會觸發。
使用座標偵測器的時機正好是「HIS 開著、要量 F11 的像素座標」,
於是按 F8 記座標會同時把身分證字號打進當下有焦點的欄位(可能是 HIS 的醫令/病歷欄)。
"""
import inspect
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import coord_detector as cd  # noqa: E402
import main  # noqa: E402


def _code_only(src: str) -> str:
    src = re.sub(r'("""|\'\'\')(?:.|\n)*?\1', "", src)
    return "\n".join(line.split("#")[0] for line in src.splitlines())


def test_both_programs_really_use_f8():
    """釘住衝突前提:任一邊改了熱鍵,這個例外就該重新檢視(測試會轉紅提醒)。"""
    assert cd.CoordinateDetectorApp.HOTKEY == "F8"
    src = (open(main.__file__, encoding="utf-8").read())
    assert "NO_GUARD_HOTKEYS = {'F8'}" in src, "主程式 F8 仍跳過視窗檢查"


def test_f8_skips_injection_while_detector_is_open_even_if_his_is_foreground(
        monkeypatch):
    """★外審指正的真正危險情境★ 量座標的整個重點就是量【別的視窗】:偵測器只設 topmost、
    不搶前景,使用者是點開 HIS 要量的畫面、把滑鼠移到目標位置再按 F8 —— 那一刻【前景是 HIS】。
    用「偵測器是不是前景」當條件會完全失效,身分證字號照樣被打進病歷/醫令欄。
    正確條件是「偵測器還開著」。"""
    written = []

    class _KB:
        @staticmethod
        def write(text):
            written.append(text)

    monkeypatch.setattr(main.hotkey_modules, "keyboard", _KB, raising=False)
    monkeypatch.setattr(main, "_load_f8_quick_text", lambda: "A126585189")
    monkeypatch.setattr(main, "_coord_detector_window_open", lambda: True)
    # 前景刻意【不是】偵測器(就是 HIS)——舊寫法在這裡會照樣打字
    monkeypatch.setattr(main, "_get_window_text", lambda _h: "西醫門診醫師作業 V.1150720.01")
    main.script_F8_quick_text()
    assert written == [], "偵測器開著時不可注入文字(不論前景是誰)"


def test_f8_writes_normally_when_detector_not_running(monkeypatch):
    """不可矯枉過正:偵測器沒開時 F8 照常在任何 app 打字(使用者明確要的行為)。"""
    written = []

    class _KB:
        @staticmethod
        def write(text):
            written.append(text)

    monkeypatch.setattr(main.hotkey_modules, "keyboard", _KB, raising=False)
    monkeypatch.setattr(main, "_load_f8_quick_text", lambda: "A126585189")
    monkeypatch.setattr(main, "_coord_detector_window_open", lambda: False)
    monkeypatch.setattr(main, "_record_his_action", lambda *a, **k: None)
    main.script_F8_quick_text()
    assert written == ["A126585189"]


def test_detector_check_is_not_foreground_based():
    """守門:條件必須是「視窗存在」,不可退回「是不是前景」。"""
    code = _code_only(inspect.getsource(main.script_F8_quick_text))
    i_guard = code.index("if _coord_detector_window_open():")
    i_write = code.index("kb.write(text)")
    assert i_guard < i_write and "return" in code[i_guard:i_write],         "守衛要在打字之前且會 return"
    # 判斷函式本身必須看「視窗存不存在」,不可用前景。
    # (script_F8_quick_text 內另有的 GetForegroundWindow 是稽核用的——記焦點控件——合法。)
    probe = _code_only(inspect.getsource(main._coord_detector_window_open))
    assert "EnumWindows" in probe, "要列舉頂層視窗"
    assert "GetForegroundWindow" not in probe,         "不可用前景視窗當判斷條件(量座標時前景是被量的那個視窗)"


def test_f8_still_works_in_other_apps():
    """不可矯枉過正:其餘 app 維持『任何地方都能用』的既有設計。"""
    code = _code_only(inspect.getsource(main.script_F8_quick_text))
    # 例外只綁定偵測器標題,不是把整個 F8 收成「只在 HIS」
    assert code.count("return") >= 1
    assert "HOTKEY_LENIENT_CLASSES" not in code and "HOTKEY_STRICT_CLASSES" not in code, \
        "不可把 F8 改成受視窗類別限制(那會改掉使用者要的行為)"


def test_detector_pixel_read_failure_does_not_reuse_stale_color():
    """[SP-06 既有契約] 取色失敗時不可沿用舊顏色(下游會拿去餵 F11 比色)。"""
    code = _code_only(inspect.getsource(cd.CoordinateDetectorApp.update_info))
    i_fail = code.index('self.rgb_var.set("(讀取失敗)")')
    seg = code[:i_fail]
    assert "self._last_color" not in seg.split("except")[-1], \
        "失敗分支不可更新/沿用 _last_color"
