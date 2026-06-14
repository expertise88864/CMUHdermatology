# -*- coding: utf-8 -*-
"""縮寫速寫引擎測試 — 純邏輯部分 (render token / 外部展開程式偵測 / install 暫停)。

IME 偵測 (should_skip_for_input_method) 依賴 Win32 IMM API，無法在 CI 純邏輯
測試，故不在此涵蓋。
"""
import os
import sys
from datetime import datetime


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


def test_external_expander_autoclose_defaults_to_on(tmp_path):
    """[2026-06-08] 預設改為開啟：偵測到其他展開軟體自動關閉、改用本程式縮寫。"""
    cfg = ae.ensure_config_file(str(tmp_path / "abbrev.json"))

    assert AbbrevConfig().close_external_expander is True
    assert cfg.close_external_expander is True
    # 缺 key 的舊配置(無 close_external_expander)載入後也應補成預設 True
    import json
    p = tmp_path / "legacy.json"
    p.write_text(json.dumps({"enabled": True, "items": []}), encoding="utf-8")
    assert ae.load_config(str(p)).close_external_expander is True
    # 但使用者明確設 False 仍尊重(不被預設覆寫)
    p2 = tmp_path / "explicit_off.json"
    p2.write_text(json.dumps({"close_external_expander": False, "items": []}),
                  encoding="utf-8")
    assert ae.load_config(str(p2)).close_external_expander is False


def test_is_auto_closable():
    """[fix A/B] 專用展開程式可自動關閉；AutoHotkey/未知程式不可。"""
    assert ae.is_auto_closable("phraseexpress.exe") is True
    assert ae.is_auto_closable("PhraseExpress.EXE") is True  # 大小寫不敏感
    assert ae.is_auto_closable("autohotkey64.exe") is False
    assert ae.is_auto_closable("notepad.exe") is False
    assert ae.is_auto_closable(None) is False


def test_close_expander_cooldown_stops_kill_war(monkeypatch):
    """[fix B] 同一 exe 30 分鐘內被關 3 次(對方自動重啟)→ 冷卻、不再嘗試關，
    避免每輪監看無限互殺。"""
    monkeypatch.setattr(ae, "_list_process_names",
                        lambda: {"phraseexpress.exe"})
    kills = []
    monkeypatch.setattr(ae, "_taskkill_image",
                        lambda image: (kills.append(image), True)[1])
    with ae._expander_close_lock:
        ae._expander_close_history.clear()
    try:
        # 前 3 次允許關閉
        for i in range(3):
            assert ae.close_auto_closable_expanders() == ["phraseexpress.exe"], i
        # 第 4 次：冷卻中 → 不關、回空(改走「暫停禮讓」路徑)
        assert ae.close_auto_closable_expanders() == []
        assert len(kills) == 3
        # 視窗過期後恢復可關(把歷史時間戳改成很久以前)
        with ae._expander_close_lock:
            ae._expander_close_history["phraseexpress.exe"] = [
                t - ae._CLOSE_HISTORY_WINDOW_SEC - 1
                for t in ae._expander_close_history["phraseexpress.exe"]]
        assert ae.close_auto_closable_expanders() == ["phraseexpress.exe"]
    finally:
        with ae._expander_close_lock:
            ae._expander_close_history.clear()


def test_taskkill_timeout_is_short():
    """[fix A] taskkill timeout 必須短(≤3s)：可能被 UI thread 間接觸發，10s 凍死太久。"""
    import inspect
    src = inspect.getsource(ae._taskkill_image)
    assert "timeout=3" in src
    assert "timeout=10" not in src


def test_install_captures_closed_expanders(monkeypatch):
    """[2026-06-08] install 自動關閉專用展開程式後，_closed_expanders 應記下名稱供跳提示。"""
    state = {"running": True}
    monkeypatch.setattr(
        ae, "_list_process_names",
        lambda: {"phraseexpress.exe"} if state["running"] else {"notepad.exe"})

    def fake_kill(image):
        state["running"] = False
        return True
    monkeypatch.setattr(ae, "_taskkill_image", fake_kill)

    eng = _make_engine()
    cfg = AbbrevConfig(enabled=True, close_external_expander=True,
                       items=[{"abbrev": "da", "expansion": "test"}])
    eng.install(cfg)
    assert eng._closed_expanders == ["phraseexpress.exe"]
    assert eng.is_installed() is True

    # 下一次 install 沒有可關的 → 清空(不會殘留、不會重複跳提示)
    eng.install(cfg)
    assert eng._closed_expanders == []


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
    # close_external_expander=False → 走「禮讓暫停」路徑（不關閉對方）
    cfg = AbbrevConfig(enabled=True, close_external_expander=False,
                       items=[{"abbrev": "da", "expansion": "test"}])
    eng.install(cfg)
    assert eng.is_installed() is False
    assert eng._external_expander == "phraseexpress.exe"


def test_install_closes_dedicated_expander_then_hooks(monkeypatch):
    """close_external_expander=True：偵測到專用展開程式 → 關閉它 → 成功後掛 hook。"""
    state = {"running": True}
    monkeypatch.setattr(
        ae, "_list_process_names",
        lambda: {"phraseexpress.exe"} if state["running"] else {"notepad.exe"})

    def fake_kill(image):
        state["running"] = False
        return True
    monkeypatch.setattr(ae, "_taskkill_image", fake_kill)

    eng = _make_engine()
    cfg = AbbrevConfig(enabled=True, close_external_expander=True,
                       items=[{"abbrev": "da", "expansion": "test"}])
    eng.install(cfg)
    assert eng.is_installed() is True
    assert eng._external_expander is None


def test_install_does_not_close_autohotkey(monkeypatch):
    """AutoHotkey 不在自動關閉清單：close 開啟也只暫停、不關 AHK。"""
    monkeypatch.setattr(ae, "_list_process_names",
                        lambda: {"autohotkey64.exe"})
    killed = []
    monkeypatch.setattr(ae, "_taskkill_image",
                        lambda image: (killed.append(image), True)[1])

    eng = _make_engine()
    cfg = AbbrevConfig(enabled=True, close_external_expander=True,
                       items=[{"abbrev": "da", "expansion": "test"}])
    eng.install(cfg)
    assert eng.is_installed() is False
    assert eng._external_expander == "autohotkey64.exe"
    assert killed == []  # AHK 不應被強制關閉


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
    # close_external_expander=False → 純測「禮讓暫停 / 消失後恢復」，不觸發關閉
    cfg = AbbrevConfig(enabled=True, close_external_expander=False,
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


# ─── 游標定位 token (%|%) ─────────────────────────────────────────────────

def test_split_cursor_marker():
    assert ae.split_cursor_marker("AAA%|%BBB") == ("AAABBB", 3)
    assert ae.split_cursor_marker("no marker") == ("no marker", 0)
    assert ae.split_cursor_marker("tail end%|%") == ("tail end", 0)  # 末端=游標在最後
    assert ae.split_cursor_marker("%|%head") == ("head", 4)
    # 多個標記:第一個是游標錨點,其餘移除避免字面 %|% 外洩
    assert ae.split_cursor_marker("a%|%b%|%c") == ("abc", 2)


def _feed_and_capture_do_replace(monkeypatch, expansion, *, abbrev="bx",
                                 preserve_trailing=True):
    """安裝含 expansion 的縮寫、打 abbrev+空白,回傳 _do_replace 收到的 args。"""
    monkeypatch.setattr(ae, "_list_process_names", lambda: {"notepad.exe"})
    monkeypatch.setattr(ae, "should_skip_for_input_method", lambda: False)
    eng = _make_engine()
    eng.install(AbbrevConfig(enabled=True, preserve_trailing_space=preserve_trailing,
                             items=[{"abbrev": abbrev, "expansion": expansion}]))
    cap = {}
    monkeypatch.setattr(eng, "_do_replace",
                        lambda *a, **k: cap.update(args=a))

    class _K:
        def __init__(self, n):
            self.name = n

    for ch in abbrev:
        eng._handle_event(_K(ch))
    eng._handle_event(_K("space"))
    return cap["args"]


def test_cursor_marker_passes_offset_and_skips_trailing_space(monkeypatch):
    """有 %|% → rendered 去除標記、不補尾端空白、cursor_left=標記後字元數。"""
    # _do_replace(delete_count, rendered, matched_key, typed_suffix, cursor_left)
    args = _feed_and_capture_do_replace(monkeypatch, "AAA%|%BBB")
    assert args[1] == "AAABBB"      # 標記移除、無尾端空白
    assert args[4] == 3             # cursor_left = len("BBB")


def test_no_cursor_marker_keeps_trailing_space_and_zero_offset(monkeypatch):
    """無 %|% → 維持舊行為:補尾端空白、cursor_left=0。"""
    args = _feed_and_capture_do_replace(monkeypatch, "keep stable")
    assert args[1] == "keep stable "
    assert args[4] == 0


def test_cursor_marker_at_end_offset_zero(monkeypatch):
    """%|% 在最末 → 游標停在末端(cursor_left=0),等同無位移但仍消除標記。"""
    args = _feed_and_capture_do_replace(monkeypatch, "done%|%")
    assert args[1] == "done"
    assert args[4] == 0


def test_handle_event_backspace_correction_still_triggers(monkeypatch):
    """打錯字→backspace 修正→重打，仍應觸發展開（user 回報：cery→⌫→t→空白 = cert）。"""
    monkeypatch.setattr(ae, "_list_process_names", lambda: {"notepad.exe"})
    monkeypatch.setattr(ae, "should_skip_for_input_method", lambda: False)
    eng = _make_engine()
    eng.install(AbbrevConfig(enabled=True, items=[
        {"abbrev": "cert", "expansion": "patient cert"}]))
    monkeypatch.setattr(eng, "_do_replace", lambda *a, **k: None)

    class _K:
        def __init__(self, n):
            self.name = n

    for ch in "cery":
        eng._handle_event(_K(ch))
    eng._handle_event(_K("backspace"))   # 刪掉打錯的 y
    eng._handle_event(_K("t"))           # 改打 t → buffer 應重建為 cert
    eng._handle_event(_K("space"))       # 觸發
    assert eng._suppressing is True, "backspace 修正後的 cert 應觸發展開"


def test_handle_event_backspace_pops_one_char_not_clear(monkeypatch):
    """backspace 只刪 buffer 最後一字元，不整段清空；空 buffer 再刪也不出錯。"""
    monkeypatch.setattr(ae, "_list_process_names", lambda: {"notepad.exe"})
    eng = _make_engine()
    eng.install(AbbrevConfig(enabled=True, items=[
        {"abbrev": "cert", "expansion": "x"}]))

    class _K:
        def __init__(self, n):
            self.name = n

    for ch in "abc":
        eng._handle_event(_K(ch))
    assert eng._buffer == "abc"
    eng._handle_event(_K("backspace"))
    assert eng._buffer == "ab"
    eng._handle_event(_K("backspace"))
    eng._handle_event(_K("backspace"))
    assert eng._buffer == ""
    eng._handle_event(_K("backspace"))   # 空 buffer 再 backspace 不應報錯
    assert eng._buffer == ""


def test_handle_event_navigation_key_still_resets_buffer(monkeypatch):
    """方向鍵移動游標 → buffer 失效 → 仍應整段清空（與 backspace 行為不同）。"""
    monkeypatch.setattr(ae, "_list_process_names", lambda: {"notepad.exe"})
    eng = _make_engine()
    eng.install(AbbrevConfig(enabled=True, items=[
        {"abbrev": "cert", "expansion": "x"}]))

    class _K:
        def __init__(self, n):
            self.name = n

    for ch in "cer":
        eng._handle_event(_K(ch))
    assert eng._buffer == "cer"
    eng._handle_event(_K("left"))
    assert eng._buffer == "", "方向鍵應清空 buffer"


# ─── [2026-06-05] 空白觸發沒展開時保留 buffer,讓 backspace 改字仍能觸發 ──────

class _SyncThread:
    """讓 _try_expand 的展開 worker 同步執行,測試可確定性斷言(不靠 sleep)。"""
    def __init__(self, target=None, args=(), daemon=None, **_kwargs):
        self._target, self._args = target, args

    def start(self):
        self._target(*self._args)


def _make_event_engine(monkeypatch, items):
    monkeypatch.setattr(ae, "_list_process_names", lambda: {"notepad.exe"})
    monkeypatch.setattr(ae, "should_skip_for_input_method", lambda: False)
    monkeypatch.setattr(ae.threading, "Thread", _SyncThread)
    # 焦點控制項 HWND 固定回 focus_ref["h"](預設 100、穩定 → 不會誤清 buffer);
    # 焦點測試可改 focus_ref["h"] 模擬「滑鼠點到別的欄位」。
    focus_ref = {"h": 100}
    monkeypatch.setattr(ae, "_get_focused_window_handle", lambda: focus_ref["h"])
    eng = _make_engine()
    started = []
    # 攔截實際送鍵:只記錄被觸發的縮寫 key(a[2]),不送真鍵盤
    monkeypatch.setattr(eng, "_do_replace",
                        lambda *a, **_k: started.append(a[2]))
    eng.install(AbbrevConfig(enabled=True, items=items))

    class _K:
        def __init__(self, n):
            self.name = n

    def feed(keys):
        for k in keys:
            eng._handle_event(_K(k))

    return eng, started, feed, focus_ref


def test_space_no_match_keeps_buffer_for_backspace_edit(monkeypatch):
    """「nev 」(沒中縮寫)→backspace 刪空白→「1」→空白 ⇒ 應觸發 nev1。
    原本空白觸發無條件清空 buffer,使用者改字後只剩改的那幾字 → 抓不到完整縮寫。"""
    eng, started, feed, _focus = _make_event_engine(
        monkeypatch, [{"abbrev": "nev1", "expansion": "x"}])

    feed(["n", "e", "v", "space"])
    assert started == []             # "nev" 不是縮寫,沒展開
    assert eng._buffer == "nev "     # buffer 保留「候選 + 觸發空白」
    feed(["backspace"])
    assert eng._buffer == "nev"      # backspace 刪掉空白
    feed(["1"])
    assert eng._buffer == "nev1"     # 改字後 buffer 重建成完整縮寫
    feed(["space"])
    assert started == ["nev1"]       # 觸發展開!


def test_space_success_clears_buffer(monkeypatch):
    """成功展開後 buffer 不保留(已替換、重新開始);只有沒展開才保留。"""
    eng, started, feed, _focus = _make_event_engine(
        monkeypatch, [{"abbrev": "nev1", "expansion": "x"}])
    feed(["n", "e", "v", "1", "space"])
    assert started == ["nev1"]
    assert eng._buffer == ""


# ─── [2026-06-05] 切換欄位/視窗 → 清空 buffer,避免跨位置拼成假縮寫 ──────────

def test_focus_change_resets_buffer_no_false_trigger(monkeypatch):
    """A 欄打"ne"→滑鼠點到 B 欄(焦點 HWND 改變)→打"v1 "⇒ 不該誤觸發 nev1。
    舊欄位殘留的"ne"被清掉,B 欄只累積"v1"。"""
    eng, started, feed, focus = _make_event_engine(
        monkeypatch, [{"abbrev": "nev1", "expansion": "x"}])
    feed(["n", "e"])                  # 在 A 欄(focus=100)
    assert eng._buffer == "ne"
    focus["h"] = 200                  # 滑鼠點到 B 欄
    feed(["v", "1", "space"])
    assert started == []              # 不該觸發(ne 已被清掉)
    assert eng._buffer == "v1 "       # buffer 只剩 B 欄打的


def test_focus_change_before_trigger_blocks_expansion(monkeypatch):
    """在 A 欄打完整"nev1"→點到別處(焦點變)→按空白 ⇒ 不在新位置展開舊縮寫。"""
    eng, started, feed, focus = _make_event_engine(
        monkeypatch, [{"abbrev": "nev1", "expansion": "x"}])
    feed(["n", "e", "v", "1"])
    assert eng._buffer == "nev1"
    focus["h"] = 300
    feed(["space"])
    assert started == []              # 焦點變了 → 觸發前已清空,不展開


def test_same_focus_still_triggers(monkeypatch):
    """焦點不變(同一欄位)→正常觸發,確認焦點檢查沒誤傷正常流程。"""
    eng, started, feed, _focus = _make_event_engine(
        monkeypatch, [{"abbrev": "nev1", "expansion": "x"}])
    feed(["n", "e", "v", "1", "space"])  # focus 全程 100
    assert started == ["nev1"]


# ─── [stability r4] 剪貼簿原本為空時也要清掉展開內文 ───────────────────────

def test_clipboard_restore_handles_originally_empty_clipboard():
    """paste 路徑：剪貼簿原本為空(old_clip is None)時，仍要把我們寫入的展開內文
    清掉(寫空字串)，不能殘留 — 否則使用者下次 Ctrl+V 會貼到整段病歷展開內文。
    (_do_replace 需 Win32 視窗環境無法在 CI 跑，故以原始碼守門防回歸/被自動更新覆蓋)"""
    import pathlib
    src = pathlib.Path(ae.__file__).read_text(encoding="utf-8")
    # 還原守門條件改為只看 clip_ok（不再要求 old_clip is not None）
    assert "if clip_ok:" in src
    # old_clip 為 None 時改寫入空字串清掉我們的展開內文
    assert 'old_clip if old_clip is not None else ""' in src
    # 舊的會漏掉「空剪貼簿」的還原條件不應再存在
    assert "if old_clip is not None and clip_ok:" not in src
