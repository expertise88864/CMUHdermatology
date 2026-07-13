# -*- coding: utf-8 -*-
"""2026-07-13 使用者需求：移除若干可勾選設定、固定行為。

  #2 主程式設定：提醒勿擾時段 / 半夜也監測 / 顯示外院分院 三個勾選移除並固定行為。
  #3 縮寫速寫：中文組字中暫停 / 保留結尾空白 / 自動關閉其他縮寫軟體 三個勾選移除，
     只要啟用縮寫速寫就一律開啟（from_dict 忽略存檔值、強制 True）。

主程式 UI/設定為原始碼守門（無法 headless 實跑）；縮寫走 load_config 行為測試。
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import abbrev_engine as ae  # noqa: E402
import main  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MAIN_SRC = (ROOT / "src" / "main.py").read_text(encoding="utf-8")


# ══ #3 縮寫：三項固定 True，即使存檔是 False ═══════════════════════════════════
def test_abbrev_three_options_forced_true_even_if_saved_false(tmp_path):
    p = tmp_path / "abbrev.json"
    p.write_text(json.dumps({
        "schema_version": ae.ABBREV_CONFIG_SCHEMA_VERSION,
        "enabled": True,
        "skip_when_ime_active": False,          # 使用者/舊檔存了 False
        "preserve_trailing_space": False,
        "close_external_expander": False,
        "items": [],
    }), encoding="utf-8")
    cfg = ae.load_config(str(p), persist_migrations=False)
    assert cfg.enabled is True
    assert cfg.skip_when_ime_active is True, "IME 暫停應固定開啟"
    assert cfg.preserve_trailing_space is True, "保留結尾空白應固定開啟"
    assert cfg.close_external_expander is True, "自動關閉其他縮寫軟體應固定開啟"


def test_abbrev_toggle_only_syncs_enabled():
    src = __import__("inspect").getsource(main.AutomationApp._abbrev_on_toggle)
    assert "self.abbrev_enabled_var.get()" in src
    # 三項固定 True、不再讀已移除的勾選變數
    assert "cfg.skip_when_ime_active = True" in src
    assert "abbrev_ime_skip_var" not in src, "不應再引用已移除的 IME 勾選變數"


# ══ #2 主程式：三個設定移除 + 行為固定（原始碼守門）══════════════════════════════
def test_removed_setting_vars_are_gone():
    for gone in ("notify_dnd_start_time_var", "notify_dnd_end_time_var",
                 "clinic_night_monitor_var", "abbrev_ime_skip_var",
                 "abbrev_trailing_space_var", "abbrev_close_external_var"):
        assert gone not in MAIN_SRC, f"{gone} 應已移除"


def test_external_clinics_forced_shown():
    assert "self.show_external_clinics = tk.BooleanVar(value=True)" in MAIN_SRC


def test_dnd_window_hardcoded_not_from_settings():
    src = __import__("inspect").getsource(main.AutomationApp._is_notification_suppressed_now)
    assert "NOTIFY_DO_NOT_DISTURB_START_HOUR" in src and "NOTIFY_DO_NOT_DISTURB_END_HOUR" in src
    assert "notify_dnd_start_time" not in src, "勿擾窗應固定、不再讀可調設定"


def test_reg64_quiet_hours_unconditional():
    src = __import__("inspect").getsource(main.AutomationApp._update_clinic_lights_loop)
    assert "if _reg64_clinic_quiet_hours(now_gate):" in src, "reg64 應固定 00–07 暫停"
    assert "_monitor_night" not in src, "不應再依賴已移除的半夜監測開關"
