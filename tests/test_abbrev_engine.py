# -*- coding: utf-8 -*-
"""縮寫速寫引擎測試 — 純邏輯部分 (render token / 外部展開程式偵測 / install 暫停)。

IME 偵測 (should_skip_for_input_method) 依賴 Win32 IMM API，無法在 CI 純邏輯
測試，故不在此涵蓋。
"""
import os
import sys
from datetime import datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import cmuh_common.abbrev_engine as ae  # noqa: E402
from cmuh_common.abbrev_engine import (  # noqa: E402
    DEFAULT_ITEMS,
    AbbrevConfig,
    AbbrevEngine,
    _maybe_migrate_legacy,
    detect_external_expander,
    render_expansion,
)


# ─── render_expansion token ──────────────────────────────────────────────

def test_render_da_slash_date():
    now = datetime(2026, 5, 28, 23, 34)
    assert render_expansion("da", now) == "(2026/5/28)"


def test_render_da1_time():
    now = datetime(2026, 5, 28, 23, 34)
    assert render_expansion("da1", now) == "23:34"


def test_render_da2_datetime():
    now = datetime(2026, 5, 28, 9, 5)
    assert render_expansion("da2", now) == "(2026/5/28) 09:05"


def test_render_da_plus_minus_days():
    now = datetime(2026, 5, 28)
    assert render_expansion("da+3", now) == "(2026/5/31)"
    assert render_expansion("da-7", now) == "(2026/5/21)"


def test_render_da_zh_chinese_date():
    now = datetime(2026, 5, 28)
    assert render_expansion("da_zh", now) == "2026年5月28日"
    assert render_expansion("da_zh-21", now) == "2026年5月7日"


def test_render_token_boundary_not_inside_word():
    """token 邊界：data / Adam 內的 da 不該被替換。"""
    now = datetime(2026, 5, 28)
    assert render_expansion("data backup", now) == "data backup"
    assert render_expansion("Adam", now) == "Adam"


def test_render_token_in_sentence():
    now = datetime(2026, 5, 28)
    out = render_expansion("拆線 on da", now)
    assert out == "拆線 on (2026/5/28)"


# ─── 外部文字展開程式偵測 ─────────────────────────────────────────────────

def test_detect_external_phraseexpress(monkeypatch):
    monkeypatch.setattr(
        ae, "_list_process_names",
        lambda: {"chrome.exe", "phraseexpress.exe", "explorer.exe"})
    assert detect_external_expander() == "phraseexpress.exe"


def test_detect_external_autohotkey(monkeypatch):
    monkeypatch.setattr(
        ae, "_list_process_names",
        lambda: {"chrome.exe", "autohotkeyu64.exe"})
    assert detect_external_expander() == "autohotkeyu64.exe"


def test_detect_external_none(monkeypatch):
    monkeypatch.setattr(
        ae, "_list_process_names",
        lambda: {"chrome.exe", "explorer.exe", "notepad.exe"})
    assert detect_external_expander() is None


def test_detect_external_case_insensitive(monkeypatch):
    # _list_process_names 回的應該已是小寫，但保險測一下大寫不命中
    monkeypatch.setattr(
        ae, "_list_process_names",
        lambda: {"PhraseExpress.exe"})  # 大寫 — 不該命中 (名單比對是小寫)
    assert detect_external_expander() is None


def test_detect_external_empty_process_list(monkeypatch):
    monkeypatch.setattr(ae, "_list_process_names", lambda: set())
    assert detect_external_expander() is None


def test_detect_external_swallows_exception(monkeypatch):
    def _boom():
        raise RuntimeError("tasklist failed")
    monkeypatch.setattr(ae, "_list_process_names", _boom)
    # 不該 raise，回 None
    assert detect_external_expander() is None


# ─── install() 在外部程式執行時暫停 ──────────────────────────────────────

class _FakeKb:
    """假的 keyboard 模組，只提供 on_press / unhook。"""
    def __init__(self):
        self.hooked = False

    def on_press(self, cb):
        self.hooked = True
        return object()

    def unhook(self, h):
        self.hooked = False


def _make_engine():
    return AbbrevEngine(_FakeKb())


def test_install_pauses_when_external_present(monkeypatch):
    monkeypatch.setattr(ae, "_list_process_names",
                        lambda: {"phraseexpress.exe"})
    eng = _make_engine()
    cfg = AbbrevConfig(enabled=True,
                       items=[{"abbrev": "da", "expansion": "test"}])
    eng.install(cfg)
    assert eng.is_installed() is False
    assert eng._external_expander == "phraseexpress.exe"


def test_install_hooks_when_no_external(monkeypatch):
    monkeypatch.setattr(ae, "_list_process_names",
                        lambda: {"notepad.exe"})
    eng = _make_engine()
    cfg = AbbrevConfig(enabled=True,
                       items=[{"abbrev": "da", "expansion": "test"}])
    eng.install(cfg)
    assert eng.is_installed() is True
    assert eng._external_expander is None


def test_install_disabled_does_not_hook(monkeypatch):
    monkeypatch.setattr(ae, "_list_process_names", lambda: {"notepad.exe"})
    eng = _make_engine()
    cfg = AbbrevConfig(enabled=False,
                       items=[{"abbrev": "da", "expansion": "test"}])
    eng.install(cfg)
    assert eng.is_installed() is False


def test_install_rehooks_after_external_disappears(monkeypatch):
    """外部程式出現 → 暫停；之後消失 → 重 install 應恢復掛 hook。"""
    eng = _make_engine()
    cfg = AbbrevConfig(enabled=True,
                       items=[{"abbrev": "da", "expansion": "test"}])
    # 1. 外部程式在 → 暫停
    monkeypatch.setattr(ae, "_list_process_names",
                        lambda: {"phraseexpress.exe"})
    eng.install(cfg)
    assert eng.is_installed() is False
    # 2. 外部程式消失 → 重 install → 恢復
    monkeypatch.setattr(ae, "_list_process_names", lambda: {"notepad.exe"})
    eng.install(cfg)
    assert eng.is_installed() is True
    assert eng._external_expander is None


# ─── ef 預設展開 + 舊版自動升級 ───────────────────────────────────────────

def test_ef_default_expansion_has_follow_up():
    """[v7] ef 預設展開應為含 'and follow up' 的新版。"""
    ef = next(d for d in DEFAULT_ITEMS if d["abbrev"] == "ef")
    assert ef["expansion"] == (
        "excisional biopsy and follow up, inform post-op 3x scar formation")


def test_migrate_legacy_ef_to_new():
    """[v7] user 沿用舊版 ef 預設 → 自動升級為新版。"""
    items = [{"abbrev": "ef",
              "expansion": "excisional biopsy, inform post-op 3x scar formation"}]
    changed = _maybe_migrate_legacy(items)
    assert changed is True
    assert items[0]["expansion"] == (
        "excisional biopsy and follow up, inform post-op 3x scar formation")


def test_migrate_legacy_ef_preserves_user_custom():
    """[v7] user 手動改過的 ef → 不該被升級覆蓋。"""
    items = [{"abbrev": "ef", "expansion": "my own ef text"}]
    changed = _maybe_migrate_legacy(items)
    assert changed is False
    assert items[0]["expansion"] == "my own ef text"


def test_migrate_legacy_cert_to_zh_date():
    """user 沿用舊版 cert（西式 da 日期）→ 自動升級為中文 da_zh 版本。"""
    items = [{"abbrev": "cert",
              "expansion": "患者因上述皮膚疾病，曾於da至本院皮膚科門診就醫治療，建議持續追蹤。"}]
    changed = _maybe_migrate_legacy(items)
    assert changed is True
    assert items[0]["expansion"] == (
        "患者因上述皮膚疾病，曾於da_zh至本院皮膚科門診就醫治療，建議持續追蹤。")


def test_migrate_legacy_cert_preserves_user_custom():
    """user 手動改過的 cert → 不該被升級覆蓋。"""
    items = [{"abbrev": "cert", "expansion": "我自己的診斷書文字 da"}]
    changed = _maybe_migrate_legacy(items)
    assert changed is False
    assert items[0]["expansion"] == "我自己的診斷書文字 da"


def test_replace_timing_constants_ordered():
    """[v7] 寧慢求對：確認延遲常數有被拉長 (deletion 正確性)。"""
    assert AbbrevEngine.PRE_BACKSPACE_DELAY_SEC >= 0.10
    assert AbbrevEngine.POST_BACKSPACE_DELAY_SEC >= 0.03
    assert AbbrevEngine.POST_PASTE_DELAY_SEC >= 0.25
    # COOLDOWN 必須 >= 整個替換流程時間，避免冷卻太早結束被重觸
    total = (AbbrevEngine.PRE_BACKSPACE_DELAY_SEC
             + AbbrevEngine.POST_BACKSPACE_DELAY_SEC
             + AbbrevEngine.POST_PASTE_DELAY_SEC)
    assert AbbrevEngine.COOLDOWN_SEC >= total


# ─── [v9] _suppressing 自癒：卡住超過 cooldown+margin 自動重置 ──────────────

def test_suppressing_selfheal_after_cooldown(monkeypatch):
    """模擬 worker thread 異常未清 _suppressing → 下次按鍵超過 cooldown
    餘裕後應自動重置，不會永久卡死。"""
    import time as _t
    monkeypatch.setattr(ae, "_list_process_names", lambda: {"notepad.exe"})
    eng = _make_engine()
    cfg = AbbrevConfig(enabled=True,
                       items=[{"abbrev": "da", "expansion": "test"}])
    eng.install(cfg)
    # 模擬卡死：_suppressing 卡 True，且 cooldown 早已過期
    eng._suppressing = True
    eng._cooldown_until = _t.monotonic() - 100  # 遠超過期限

    class _E:
        name = "a"
    eng._handle_event(_E())  # 應觸發自癒
    assert eng._suppressing is False, "卡死的 _suppressing 應被自癒重置"


def test_suppressing_blocks_within_cooldown(monkeypatch):
    """cooldown 期間內 _suppressing 仍應正常擋住（不誤觸自癒）。"""
    import time as _t
    monkeypatch.setattr(ae, "_list_process_names", lambda: {"notepad.exe"})
    eng = _make_engine()
    cfg = AbbrevConfig(enabled=True,
                       items=[{"abbrev": "da", "expansion": "test"}])
    eng.install(cfg)
    eng._suppressing = True
    eng._cooldown_until = _t.monotonic() + 10  # 還在 cooldown 內

    class _E:
        name = "a"
    eng._handle_event(_E())
    assert eng._suppressing is True, "cooldown 內不該誤清 _suppressing"


# ─── 縮寫必須是「完整的字」才觸發（不可在字尾誤觸，如 persist→st）───────────────

def test_abbrev_triggers_only_on_whole_word(monkeypatch):
    monkeypatch.setattr(ae, "_list_process_names", lambda: {"notepad.exe"})
    monkeypatch.setattr(ae, "should_skip_for_input_method", lambda: False)
    eng = _make_engine()
    eng.install(AbbrevConfig(enabled=True, items=[
        {"abbrev": "st", "expansion": "keep stable"}]))
    # 攔截實際展開（避免真的送鍵）；_try_expand 命中且通過邊界檢查時會設 _suppressing=True
    monkeypatch.setattr(eng, "_do_replace", lambda *a, **k: None)

    def did_trigger(buf: str) -> bool:
        eng._suppressing = False
        eng._cooldown_until = 0.0
        eng._try_expand(buf, " ")
        return eng._suppressing

    assert did_trigger("st") is True          # 完整字 → 展開
    assert did_trigger("persist") is False    # 出現在字尾(persi+st) → 不展開
    assert did_trigger("test") is False       # te+st → 不展開
    assert did_trigger("1st") is False        # 數字開頭 1st → 不展開
    assert did_trigger("(st") is True         # 標點後 → 視為完整字 → 展開
    assert did_trigger("，st") is True         # 全形標點後 → 視為邊界 → 展開
    assert did_trigger("拆st") is False        # 中文字在前(黏在字裡) → 不展開


def test_da_abbrev_boundary_english_and_chinese(monkeypatch):
    """user 回報案例：'da' 日期縮寫只能在「空白/標點/字首」後觸發。
    英文字母在前 (clida) 或中文字在前 (病灶da) 都是黏在別的字裡 → 不展開。"""
    monkeypatch.setattr(ae, "_list_process_names", lambda: {"notepad.exe"})
    monkeypatch.setattr(ae, "should_skip_for_input_method", lambda: False)
    eng = _make_engine()
    eng.install(AbbrevConfig(enabled=True, items=[
        {"abbrev": "da", "expansion": "da"}]))
    monkeypatch.setattr(eng, "_do_replace", lambda *a, **k: None)

    def did_trigger(buf: str) -> bool:
        eng._suppressing = False
        eng._cooldown_until = 0.0
        eng._try_expand(buf, " ")
        return eng._suppressing

    assert did_trigger("da") is True          # 字首 → 展開日期
    assert did_trigger("(da") is True         # 標點後 → 展開
    assert did_trigger("clida") is False      # 英文字母在前 → 不展開(回報案例)
    assert did_trigger("agenda") is False     # 英文單字字尾 → 不展開
    assert did_trigger("病灶da") is False      # 中文字在前 → 不展開


def test_handle_event_typing_word_then_space_does_not_misfire(monkeypatch):
    """整合：逐字打 'persist' 再按空白，不應觸發展開（_suppressing 保持 False）。"""
    monkeypatch.setattr(ae, "_list_process_names", lambda: {"notepad.exe"})
    monkeypatch.setattr(ae, "should_skip_for_input_method", lambda: False)
    eng = _make_engine()
    eng.install(AbbrevConfig(enabled=True, items=[
        {"abbrev": "st", "expansion": "keep stable"}]))
    monkeypatch.setattr(eng, "_do_replace", lambda *a, **k: None)

    class _K:
        def __init__(self, n):
            self.name = n

    for chh in "persist":
        eng._handle_event(_K(chh))
    eng._handle_event(_K("space"))
    assert eng._suppressing is False, "persist 結尾的 st 不該誤觸展開"
