# -*- coding: utf-8 -*-
"""HIS 寫入面契約金絲雀（2026-07-16）:疑似院方改版時 fail-closed 停止自動寫入。

採樣只用主視窗 title 版本號(選單多 owner-draw、動態文字讀不到 → 版本字串最可靠)。
DRIFT(版本與基線不符)→ _his_write_contract_ok 回 False + 疑似改版警告 → 呼叫端中止;
OK/UNKNOWN(採不到版本)/UNCALIBRATED → 放行(不因假警報/採樣失敗停整組 F 鍵)。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402
from cmuh_common import contract_canary as cc  # noqa: E402


def _sample(monkeypatch, title, baseline_fp=None):
    """以指定基線採樣(隔離基線檔),回裁決存入 main._his_write_verdict。"""
    if baseline_fp is None:
        baseline_fp = {"title_version": main._HIS_CALIBRATED_VERSION}
    monkeypatch.setattr(main, "_his_write_baseline_fp", lambda: baseline_fp)
    main._his_canary_warned = False
    main._sample_his_write_contract(title)


# ── 採樣裁決 ─────────────────────────────────────────────────────────────────
def test_sample_ok_on_matching_version(monkeypatch):
    _sample(monkeypatch, "西醫門診醫師作業 V.1150629.01")
    assert main._his_write_verdict.status == cc.STATUS_OK


def test_sample_drift_on_version_shift(monkeypatch):
    _sample(monkeypatch, "西醫門診醫師作業 V.1150701.01")
    assert main._his_write_verdict.status == cc.STATUS_DRIFT
    assert main._his_write_verdict.should_block_write is True


def test_sample_unknown_when_no_version_string(monkeypatch):
    _sample(monkeypatch, "西醫門診醫師作業")
    assert main._his_write_verdict.status == cc.STATUS_UNKNOWN
    assert main._his_write_verdict.should_block_write is False


def test_sample_uses_calibrated_baseline_from_file(monkeypatch):
    # 使用者校正過的基線(新版本)→ 該版本變 OK
    _sample(monkeypatch, "西醫門診醫師作業 V.1150701.01",
            baseline_fp={"title_version": "1150701"})
    assert main._his_write_verdict.status == cc.STATUS_OK


# ── 寫入 gate:_his_write_contract_ok ─────────────────────────────────────────
def test_gate_blocks_on_drift(monkeypatch):
    _sample(monkeypatch, "西醫門診醫師作業 V.1150701.01")   # DRIFT
    shown = {}
    monkeypatch.setattr(main, "_show_uvb_warning",
                        lambda h, t, m: shown.update(title=t, msg=m))
    assert main._his_write_contract_ok(1234, "F2 UVB") is False
    assert "改版" in shown["title"]              # 有跳疑似改版警告


def test_gate_passes_on_ok(monkeypatch):
    _sample(monkeypatch, "西醫門診醫師作業 V.1150629.01")   # OK
    called = {"warn": False}
    monkeypatch.setattr(main, "_show_uvb_warning",
                        lambda *a, **k: called.update(warn=True))
    assert main._his_write_contract_ok(1234, "F2 UVB") is True
    assert called["warn"] is False               # OK 不跳警告


def test_gate_passes_on_unknown_and_uncalibrated(monkeypatch):
    monkeypatch.setattr(main, "_show_uvb_warning", lambda *a, **k: None)
    # UNKNOWN(採不到版本)→ 放行
    _sample(monkeypatch, "西醫門診醫師作業")
    assert main._his_write_contract_ok(1, "F2") is True
    # 裁決為 None(從未採樣)→ 放行
    main._his_write_verdict = None
    assert main._his_write_contract_ok(1, "F2") is True


# ── gate 已接到危險寫入匯流點(原始碼守門) ────────────────────────────────────
def test_gate_wired_into_code_input_and_uvb():
    code_src = inspect.getsource(main._script_code_input_adaptive)
    assert "_his_write_contract_ok(hwnd, " in code_src, "醫令代碼輸入前應過金絲雀 gate"
    # gate 在送任何選單 command 之前
    assert (code_src.index("_his_write_contract_ok")
            < code_src.index("_send_yiling_menu_command")), "gate 須在送選單前"

    uvb_src = inspect.getsource(main._update_uvb_dose_core)
    assert "_his_write_contract_ok(main_hwnd, " in uvb_src, "UVB 劑量寫回前應過金絲雀 gate"
    # gate 在分流/寫回之前
    assert (uvb_src.index("_his_write_contract_ok")
            < uvb_src.index("_resolve_phototherapy_disposition")), "gate 須在寫回前"


def test_sampling_wired_into_find_window():
    src = inspect.getsource(main._find_hospital_main_window)
    assert "_sample_his_write_contract" in src, "找到主視窗時應採樣 HIS 寫入契約"


# ── 重新校正 UI(設定頁）───────────────────────────────────────────────────
def test_recalibrate_writes_current_version_as_baseline(monkeypatch, tmp_path):
    # 現況版本 1150701(異於硬編碼基線 → DRIFT)→ 校正後基線=1150701 → 變 OK
    monkeypatch.setattr(main, "_contract_baseline_singleton", None)
    monkeypatch.setattr(main, "get_conf_path",
                        lambda name: str(tmp_path / name))
    monkeypatch.setattr(main, "_find_hospital_main_window",
                        lambda: main._sample_his_write_contract(
                            "西醫門診醫師作業 V.1150701.01") or 123)
    monkeypatch.setattr(main.messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(main.messagebox, "showinfo", lambda *a, **k: None)

    # 校正前:DRIFT(現況 1150701 vs 硬編碼 1150629)
    main._sample_his_write_contract("西醫門診醫師作業 V.1150701.01")
    assert main._his_write_verdict.should_block_write is True

    app = main.AutomationApp.__new__(main.AutomationApp)
    main.AutomationApp._recalibrate_his_canary(app)

    # 基線檔已記錄 1150701
    assert main._contract_baseline().get("his_menu") == {"title_version": "1150701"}
    # 重新採樣後不再擋
    assert main._his_write_verdict.should_block_write is False


def test_recalibrate_aborts_when_no_version(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "_contract_baseline_singleton", None)
    monkeypatch.setattr(main, "get_conf_path", lambda name: str(tmp_path / name))
    monkeypatch.setattr(main, "_find_hospital_main_window",
                        lambda: main._sample_his_write_contract("西醫門診醫師作業") or 0)
    warned = {"w": False}
    monkeypatch.setattr(main.messagebox, "showwarning",
                        lambda *a, **k: warned.update(w=True))
    yes = {"asked": False}
    monkeypatch.setattr(main.messagebox, "askyesno",
                        lambda *a, **k: yes.update(asked=True) or True)
    app = main.AutomationApp.__new__(main.AutomationApp)
    main.AutomationApp._recalibrate_his_canary(app)
    assert warned["w"] is True and yes["asked"] is False   # 無版本 → 警告、不寫基線


def test_recalibrate_shows_error_not_success_when_refused(monkeypatch, tmp_path):
    # [codex] 基線檔為較新版本 schema → set 被拒 → 顯示錯誤、不可誤報「已校正」
    import json
    p = tmp_path / "contract_baseline.json"
    p.write_text(json.dumps({"schema_version": 999, "surfaces": {}}),
                 encoding="utf-8")
    monkeypatch.setattr(main, "_contract_baseline_singleton", None)
    monkeypatch.setattr(main, "get_conf_path", lambda name: str(p))
    monkeypatch.setattr(main, "_find_hospital_main_window",
                        lambda: main._sample_his_write_contract(
                            "西醫門診醫師作業 V.1150701.01") or 1)
    monkeypatch.setattr(main.messagebox, "askyesno", lambda *a, **k: True)
    calls = {"error": False, "info": False}
    monkeypatch.setattr(main.messagebox, "showerror",
                        lambda *a, **k: calls.update(error=True))
    monkeypatch.setattr(main.messagebox, "showinfo",
                        lambda *a, **k: calls.update(info=True))
    app = main.AutomationApp.__new__(main.AutomationApp)
    main.AutomationApp._recalibrate_his_canary(app)
    assert calls["error"] is True and calls["info"] is False, \
        "被拒時應顯示錯誤、不得顯示成功"
    # 原檔未被覆寫
    assert json.loads(p.read_text(encoding="utf-8"))["schema_version"] == 999


def test_canary_settings_wired_into_settings_tab():
    src = inspect.getsource(main.AutomationApp._create_settings_tab)
    assert "_build_canary_settings(left_column)" in src


def test_canary_status_text_reflects_verdict(monkeypatch):
    app = main.AutomationApp.__new__(main.AutomationApp)
    main._his_write_verdict = None
    assert "尚未偵測" in main.AutomationApp._canary_status_text(app)
    _sample(monkeypatch, "西醫門診醫師作業 V.1150701.01")   # DRIFT
    assert "改版" in main.AutomationApp._canary_status_text(app)
