# -*- coding: utf-8 -*-
"""UVB批次3 F12 取消尊重守門（§3E：UD-01~04，2026-07-10）。

四個「H4/W1/W7 既有機制沒蓋到的 F12 縫隙」，修法都是小改動；main.py 有 Selenium/Tk 重依賴、
且這些是 Win32 HIS 自動化路徑，改以 inspect 原始碼守門鎖住修正、防日後回歸。

  UD-01 卡號 autofill 吞 SubsystemInterrupted → OCR 期間 F12 失效。
  UD-02 純 excimer 確認窗按「否」後無 check_stop → F12 後仍寫身份=01、兩流程並行。
  UD-03 UVB 已寫回後 51019 階段以例外/F12 收場時 W7 半套警告被繞過。
  UD-04 主路徑寫回處置欄前無最終 check_stop 閘門。
"""
import inspect
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import main  # noqa: E402


# ── UD-01：卡號 autofill 必須 re-raise SubsystemInterrupted ─────────────────────
def test_ud01_card_autofill_reraises_interrupt():
    src = inspect.getsource(main._autofill_卡號_from_醫師上次)
    assert "except SubsystemInterrupted:" in src, "UD-01: 應攔 SubsystemInterrupted"
    idx_si = src.index("except SubsystemInterrupted:")
    # 外層(最後一個)except Exception 才是會吞掉例外的那個；SubsystemInterrupted 要在它之前
    idx_outer_exc = src.rindex("except Exception:")
    assert idx_si < idx_outer_exc, "UD-01: SubsystemInterrupted 必須在外層 except Exception 之前"
    assert "raise" in src[idx_si:idx_outer_exc], "UD-01: 攔到 F12 取消必須 re-raise、不可吞"


# ── UD-02：純 excimer 確認窗返回後不論 Yes/No 一律 check_stop；身份寫入前也 check_stop ──
def test_ud02_pure_excimer_check_stop_covers_both_branches():
    src = inspect.getsource(main._f23_pure_excimer_update)
    # check_stop 必須在 if _confirmed 之前(涵蓋 Yes/No 兩分支),不再只在 Yes 分支內
    assert "_confirmed = _photo_confirm_yesno(" in src
    idx_check = src.index("check_stop()", src.index("_confirmed = _photo_confirm_yesno("))
    idx_if = src.index("if _confirmed:")
    assert idx_check < idx_if, "UD-02: check_stop 必須在 if _confirmed 之前,涵蓋 No 分支"


def test_ud02_set_identity_check_stop_first():
    src = inspect.getsource(main._set_身份_自費)
    # check_stop 應在函式 try 之前(不被 except 吞),避免 F12 後仍寫計費敏感的身份欄
    assert "check_stop()" in src
    assert src.index("check_stop()") < src.index("try:"), \
        "UD-02: _set_身份_自費 應在 try 前 check_stop"


# ── UD-03：51019 階段包 try，例外時（已改 UVB）先跳 W7 半套警告再 re-raise ──────────
def test_ud03_51019_stage_shows_w7_on_exception():
    for fn, code in ((main.script_F2_adaptive, "F2"), (main.script_F3_adaptive, "F3")):
        src = inspect.getsource(fn)
        assert "_script_code_input_adaptive(" in src
        # 51019 階段有 except，且例外分支會呼叫半套警告 + re-raise
        assert "except Exception:" in src, f"{code}: 51019 階段應包 try/except"
        assert "_show_light_code_incomplete_warning(" in src
        assert "_last_uvb_write" in src, f"{code}: 應以 _last_uvb_write 判定本次是否已改 UVB"
        # except 區塊內要有 raise(re-raise 原例外)
        exc_idx = src.index("_autofill_卡號_from_醫師上次")
        assert re.search(r"except Exception:\s*\n(?:.*\n)*?\s*raise", src[exc_idx:]), \
            f"{code}: 51019 例外分支必須 re-raise 原例外"


# ── UD-04：兩個寫回處置欄的 call site 之前都要有 check_stop ─────────────────────
def test_ud04_writeback_has_check_stop_gate():
    core = inspect.getsource(main._update_uvb_dose_core)
    assert "check_stop()" in core
    assert core.index("check_stop()", core.rindex("check_stop()") - 1) \
        < core.index("_write_tmemo_text(memo_hwnd, final_text)"), \
        "UD-04: core 寫回 final_text 前應有 check_stop"
    exc = inspect.getsource(main._f23_pure_excimer_update)
    # 無確認窗的 UPDATED 寫回前也要 check_stop
    w_idx = exc.index("_write_tmemo_text(memo_hwnd, result.new_text)")
    assert "check_stop()" in exc[:w_idx], "UD-04: excimer 無確認窗寫回前應有 check_stop"
