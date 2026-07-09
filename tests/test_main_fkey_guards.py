# -*- coding: utf-8 -*-
"""F 鍵 HIS 自動化高風險破口 H1–H5 的守門回歸（2026-07-09）。

main.py 依賴 Selenium/Tk 等,headless 無法 import → 沿用 test_main_launch_guards.py 的
【原始碼/AST 檢查】模式,驗證各修正的守門敘述確實存在、不被日後改回舊寫法。findings 出處:
docs/未審review_findings_主程式F鍵HIS自動化_2026-07-09.md。
"""
import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = ROOT / "src" / "main.py"


def _func_source(name: str) -> str:
    source = MAIN.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"main.py 找不到函式：{name}")


# ── H1：F9/F10 只對 HIS 行程的 #32770 對話框自動按「是」──────────────────────────
def test_h1_round4_only_acts_on_his_process_dialog():
    src = _func_source("_f9_f10_round4_submit_and_confirm")
    assert "_get_window_pid(popup_hwnd)" in src, "H1: 應取 popup(HIS)的 PID"
    assert "require_pid=popup_pid" in src, "H1: 等/找 #32770 應限定 HIS 行程 PID"
    # dlg2 掃描應排除已處理的第一個對話框(避免重複轟)
    assert "exclude_hwnds=" in src, "H1: dlg2 掃描應可排除第一個 dlg"


def test_h1_find_window_supports_pid_filter():
    src = _func_source("_find_window_by_class_title")
    assert "require_pid" in src and "GetWindowThreadProcessId" in src, \
        "H1: _find_window_by_class_title 應支援 require_pid 比對行程"


# ── H2：同意書 Round 2/3 失敗即中止,不照樣自動送出 ──────────────────────────────
def test_h2_consent_aborts_when_round2_or_round3_fails():
    src = _func_source("script_F9_F10_consent_form_adaptive")
    assert "if not _f9_f10_round2_popup_actions(" in src, \
        "H2: Round 2 回傳值必須被檢查、失敗即中止"
    assert "if not _f9_f10_round3_phrases(" in src, \
        "H2: Round 3 回傳值必須被檢查、失敗即中止"
    assert "if not _f9_f10_round4_submit_and_confirm(" in src, \
        "H2: Round 4 回傳值也應被檢查"


# ── H3：ActivePage 切換失敗不得回 True(否則送錯同意書)────────────────────────────
def test_h3_switch_tab_returns_real_success():
    src = _func_source("_switch_tab_by_text")
    assert "return success, target_sheet" in src, \
        "H3: 第一個回傳值必須是真實 success(ActivePage 是否真的切成功)"
    assert "return True, target_sheet" not in src, \
        "H3: 不可再無條件回 True(swap 失敗仍送出=錯誤同意書事故)"


# ── H4：純 excimer 確認框後缺 check_stop / F12 取消被吞 ─────────────────────────
def test_h4_pure_excimer_respects_f12_cancel():
    src = _func_source("_f23_pure_excimer_update")
    assert "check_stop()" in src, "H4: 確認框返回後、寫回前應 check_stop()"
    assert "except SubsystemInterrupted:" in src, \
        "H4: SubsystemInterrupted 必須先攔截"
    # 攔截後必須 re-raise,不可被下面的 except Exception 吞掉
    idx_si = src.index("except SubsystemInterrupted:")
    idx_exc = src.index("except Exception:")
    assert idx_si < idx_exc, "H4: except SubsystemInterrupted 必須在 except Exception 之前"
    assert "raise" in src[idx_si:idx_exc], "H4: 攔到 SubsystemInterrupted 必須 re-raise"


# ── H5：轉診預掛表格取樣前先驗證沒被遮住(前景/未被 Chrome 蓋)──────────────────────
def test_h5_referral_sampling_verifies_not_occluded():
    src = _func_source("_referral_grid_has_appointments")
    assert "_screen_point_in_window(" in src, \
        "H5: 取樣前應用 WindowFromPoint 驗證取樣點屬於本視窗"
    # 遮擋驗證必須在 screenshot 之前
    idx_probe = src.index("_screen_point_in_window(")
    idx_shot = src.index(".screenshot(")
    assert idx_probe < idx_shot, "H5: 遮擋驗證必須在 screenshot 取樣之前"


def test_h5_helpers_exist():
    assert "def _window_is_ancestor(" in MAIN.read_text(encoding="utf-8")
    assert "def _screen_point_in_window(" in MAIN.read_text(encoding="utf-8")
