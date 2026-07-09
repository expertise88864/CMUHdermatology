# -*- coding: utf-8 -*-
"""M6 + L3 回歸:卡號 OCR 的 winsdk runtime 安裝守門 + PHI 暫存檔保證清除（2026-07-09）。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common import ditto_card_ocr as d  # noqa: E402


# ══ M6：預設不在生產機 runtime pip install winsdk(供應鏈風險)═══════════════════
def test_m6_no_runtime_install_by_default(monkeypatch):
    monkeypatch.delenv("CMUH_ALLOW_WINSDK_AUTOINSTALL", raising=False)
    called = {"run": False}
    monkeypatch.setattr("subprocess.run",
                        lambda *a, **k: called.__setitem__("run", True))
    monkeypatch.setattr(d, "_OCR_ROOT_CACHE", None)
    d._bg_install_winsdk()
    assert called["run"] is False, "預設不得 runtime pip install(供應鏈風險)"
    assert d._OCR_ROOT_CACHE is None


def test_m6_optin_installs_with_no_window_flag(monkeypatch):
    monkeypatch.setenv("CMUH_ALLOW_WINSDK_AUTOINSTALL", "1")
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["creationflags"] = kwargs.get("creationflags")
        class _R:
            returncode = 0
        return _R()

    monkeypatch.setattr("subprocess.run", fake_run)
    monkeypatch.setattr(d, "_OCR_ROOT_CACHE", None)
    d._bg_install_winsdk()   # winsdk import 之後會失敗(未安裝)→ 進 except,但 subprocess 已被叫
    assert seen.get("cmd") and "winsdk" in seen["cmd"], "opt-in 時應執行 pip install winsdk"
    if os.name == "nt":
        assert seen.get("creationflags") == 0x08000000, "應帶 CREATE_NO_WINDOW 不閃 console"


# ══ L3：PHI 暫存 PNG 一律清除(早退/例外也不留)══════════════════════════════════
class _StubImg:
    """只需支援 read_card_from_image 早退路徑會用到的 save()。"""
    width = 100
    height = 40

    def save(self, path):
        with open(path, "wb") as f:
            f.write(b"phi-png")


def test_l3_temp_png_removed_on_early_return(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "_ocr_words_of_png", lambda *a, **k: [])
    monkeypatch.setattr(d, "find_card_column_x", lambda words: None)  # 觸發早退
    d.read_card_from_image(_StubImg(), tmp_dir=str(tmp_path), save_debug=False)
    assert not os.path.exists(os.path.join(str(tmp_path), "_ditto_card_full.png")), \
        "早退路徑必須清除含 PHI 的暫存 PNG"


def test_l3_temp_png_removed_on_exception(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("OCR 例外")
    monkeypatch.setattr(d, "_ocr_words_of_png", boom)
    with pytest.raises(RuntimeError):
        d.read_card_from_image(_StubImg(), tmp_dir=str(tmp_path), save_debug=False)
    assert not os.path.exists(os.path.join(str(tmp_path), "_ditto_card_full.png")), \
        "例外路徑(try/finally)也必須清除 PHI 暫存 PNG"


def test_l3_save_debug_keeps_png(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "_ocr_words_of_png", lambda *a, **k: [])
    monkeypatch.setattr(d, "find_card_column_x", lambda words: None)
    d.read_card_from_image(_StubImg(), tmp_dir=str(tmp_path), save_debug=True)
    assert os.path.exists(os.path.join(str(tmp_path), "_ditto_card_full.png")), \
        "save_debug=True 時刻意保留供除錯"
