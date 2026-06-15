# -*- coding: utf-8 -*-
"""Abbreviation default migration, sorting, and length guard tests."""
import ctypes
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import abbrev_engine as ae  # noqa: E402


def _default_map() -> dict[str, str]:
    return {item["abbrev"]: item["expansion"] for item in ae.DEFAULT_ITEMS}


def test_requested_default_abbreviations_are_present():
    defaults = _default_map()

    assert defaults["st"] == "keep stable"
    assert defaults["sd"] == "seborrheic dermatitis"
    assert defaults["se"] == "subacute eczema"
    assert defaults["mf"] == "medication and follow up"
    assert defaults["nt"] == "next time:"
    assert defaults["pred"] == "no DM/HBV/HCV"
    assert defaults["cert"] == \
        "患者因上述皮膚疾病，曾於da_zh至本院皮膚科門診就醫治療，建議持續追蹤。"
    # [v8] 已退役的醫師代碼縮寫不再出現在預設清單
    for code in ("D15645", "D15728", "D6175", "D20191", "D28592", "D34899",
                 "101823", "D31352", "101358", "D14355", "D35819"):
        assert code not in defaults


def test_cert_default_renders_dynamic_visit_date():
    template = _default_map()["cert"]

    assert ae.render_expansion(template, datetime(2026, 5, 31)) == \
        "患者因上述皮膚疾病，曾於2026年5月31日至本院皮膚科門診就醫治療，建議持續追蹤。"


def test_old_config_adds_new_defaults_once_and_preserves_custom_text(tmp_path):
    path = tmp_path / "abbrev_settings.json"
    path.write_text(json.dumps({
        "enabled": True,
        "items": [
            {"abbrev": "st", "expansion": "my custom stable text"},
            {"abbrev": "zz", "expansion": "custom"},
        ],
    }), encoding="utf-8")

    cfg = ae.load_config(str(path))
    values = {item["abbrev"]: item["expansion"] for item in cfg.items}
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert cfg.schema_version == ae.ABBREV_CONFIG_SCHEMA_VERSION
    assert values["st"] == "my custom stable text"
    assert values["sd"] == "seborrheic dermatitis"
    assert values["mf"] == "medication and follow up"
    assert values["pred"] == "no DM/HBV/HCV"
    assert values["cert"].startswith("患者因上述皮膚疾病")
    assert saved["schema_version"] == ae.ABBREV_CONFIG_SCHEMA_VERSION
    assert [item["abbrev"] for item in saved["items"]] == sorted(
        (item["abbrev"] for item in saved["items"]), key=str.casefold)


def test_current_schema_does_not_restore_manually_deleted_default(tmp_path):
    path = tmp_path / "abbrev_settings.json"
    path.write_text(json.dumps({
        "schema_version": ae.ABBREV_CONFIG_SCHEMA_VERSION,
        "enabled": True,
        "items": [{"abbrev": "zz", "expansion": "custom"}],
    }), encoding="utf-8")

    cfg = ae.load_config(str(path))

    assert cfg.items == [{"abbrev": "zz", "expansion": "custom"}]


def test_v5_config_restores_requested_defaults_only(tmp_path):
    path = tmp_path / "abbrev_settings.json"
    path.write_text(json.dumps({
        "schema_version": 5,
        "enabled": True,
        "items": [{"abbrev": "zz", "expansion": "custom"}],
    }), encoding="utf-8")

    cfg = ae.load_config(str(path))
    values = {item["abbrev"]: item["expansion"] for item in cfg.items}

    assert values["nt"] == "next time:"
    assert values["se"] == "subacute eczema"
    assert values["zz"] == "custom"
    assert "mf" not in values
    # [v8] 退役醫師代碼不再被還原
    for code in ("D15645", "101823", "D35819"):
        assert code not in values


def test_v7_config_removes_retired_doctor_defaults(tmp_path):
    path = tmp_path / "abbrev_settings.json"
    path.write_text(json.dumps({
        "schema_version": 7,
        "enabled": True,
        "items": [
            {"abbrev": "D15645", "expansion": "FEX7DL"},
            {"abbrev": "101823", "expansion": "L6464646"},
            # 即使 user 改過 expansion 也要移除
            {"abbrev": "D28592", "expansion": "my-changed-password"},
            {"abbrev": "zz", "expansion": "custom"},
            {"abbrev": "st", "expansion": "keep stable"},
        ],
    }), encoding="utf-8")

    cfg = ae.load_config(str(path))
    values = {item["abbrev"]: item["expansion"] for item in cfg.items}
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert cfg.schema_version == ae.ABBREV_CONFIG_SCHEMA_VERSION
    assert saved["schema_version"] == ae.ABBREV_CONFIG_SCHEMA_VERSION
    for code in ("D15645", "101823", "D28592"):
        assert code not in values
    assert values["zz"] == "custom"
    assert values["st"] == "keep stable"


def test_load_config_persist_migrations_false_does_not_rewrite_file(tmp_path):
    """[review C 2026-06-12] 匯入用唯讀解析：遷移仍套用在「回傳的 cfg」，但匯入
    來源檔(使用者 USB 上的備份)位元組原樣、不可被改寫。"""
    path = tmp_path / "import_source.json"
    original = json.dumps({
        "schema_version": 5,
        "enabled": True,
        "items": [{"abbrev": "zz", "expansion": "custom"}],
    })
    path.write_text(original, encoding="utf-8")

    cfg = ae.load_config(str(path), persist_migrations=False)

    assert path.read_text(encoding="utf-8") == original  # 來源檔原樣
    values = {item["abbrev"] for item in cfg.items}
    assert "zz" in values
    assert "nt" in values  # v5 遷移(還原 nt/se)仍套用在回傳值
    # 對照組：預設 persist_migrations=True 會寫回(自家設定檔自動修復行為不變)
    path2 = tmp_path / "own_settings.json"
    path2.write_text(original, encoding="utf-8")
    ae.load_config(str(path2))
    assert path2.read_text(encoding="utf-8") != original


def test_to_dict_sorts_abbreviations_case_insensitively():
    cfg = ae.AbbrevConfig(items=[
        {"abbrev": "zz", "expansion": "3"},
        {"abbrev": "Aa", "expansion": "1"},
        {"abbrev": "bb", "expansion": "2"},
    ])

    assert [item["abbrev"] for item in cfg.to_dict()["items"]] == [
        "Aa", "bb", "zz",
    ]


def test_engine_skips_overlong_abbreviation(monkeypatch):
    class FakeKeyboard:
        def on_press(self, _callback):
            return object()

        def unhook(self, _hook):
            pass

    monkeypatch.setattr(ae, "detect_external_expander", lambda: None)
    engine = ae.AbbrevEngine(FakeKeyboard())
    overlong = "x" * (ae.MAX_ABBREV_LENGTH + 1)

    engine.install(ae.AbbrevConfig(enabled=True, items=[
        {"abbrev": "ok", "expansion": "kept"},
        {"abbrev": overlong, "expansion": "skipped"},
    ]))

    assert engine._lookup == {"ok": "kept"}
    assert engine._max_abbrev_len == 2


def test_replace_cooldown_timer_waits_only_remaining_time():
    import inspect

    source = inspect.getsource(ae.AbbrevEngine._do_replace)

    assert "self._cooldown_until - time.monotonic()" in source
    assert "threading.Timer(self.COOLDOWN_SEC" not in source


def test_native_edit_path_shortens_cooldown(monkeypatch):
    """原生欄位快路徑（同步、不碰剪貼簿）應把 cool-down 大幅縮短，連續展開更即時。"""
    monkeypatch.setattr(ae, "_replace_native_edit_suffix", lambda *a, **k: True)
    engine = ae.AbbrevEngine(object())
    engine._suppressing = True
    engine._cooldown_until = ae.time.monotonic() + engine.COOLDOWN_SEC

    engine._do_replace(3, "expanded ", "cert", "cert ")

    remaining = engine._cooldown_until - ae.time.monotonic()
    assert remaining <= engine.NATIVE_EDIT_COOLDOWN_SEC + 0.05
    assert remaining < engine.COOLDOWN_SEC


def test_replace_clears_suppress_when_cooldown_timer_cannot_start(monkeypatch):
    class FakeKeyboard:
        def send(self, _key):
            pass

        def write(self, _text):
            pass

    class BrokenTimer:
        def __init__(self, *_args, **_kwargs):
            self.daemon = False

        def start(self):
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr(ae, "_clipboard_get_text", lambda: None)
    monkeypatch.setattr(ae, "_clipboard_set_text", lambda _text: False)
    monkeypatch.setattr(ae.threading, "Timer", BrokenTimer)
    monkeypatch.setattr(ae.AbbrevEngine, "PRE_BACKSPACE_DELAY_SEC", 0)
    engine = ae.AbbrevEngine(FakeKeyboard())
    engine._suppressing = True
    engine._cooldown_until = ae.time.monotonic() + 10

    engine._do_replace(2, "expanded", "ok")

    assert engine._suppressing is False


def test_focused_window_handle_prefers_child_edit_control(monkeypatch):
    class FakeUser32:
        @staticmethod
        def GetForegroundWindow():
            return 100

        @staticmethod
        def GetWindowThreadProcessId(_hwnd, _process_id):
            return 7

        @staticmethod
        def GetGUIThreadInfo(_thread_id, info_pointer):
            info = ctypes.cast(
                info_pointer, ctypes.POINTER(ae._GUITHREADINFO)).contents
            info.hwndFocus = 200
            return 1

    class FakeWindll:
        user32 = FakeUser32()

    monkeypatch.setattr(ae.ctypes, "windll", FakeWindll())

    assert ae._get_focused_window_handle() == 200


def test_native_edit_replacement_verifies_suffix_before_replacing(monkeypatch):
    calls = []
    monkeypatch.setattr(ae, "_get_focused_window_handle", lambda: 123)
    monkeypatch.setattr(ae, "_is_native_edit_control", lambda _hwnd: True)
    monkeypatch.setattr(ae, "_get_edit_selection", lambda _hwnd: (5, 5))
    monkeypatch.setattr(ae, "_read_window_text", lambda _hwnd: "xxst ")
    monkeypatch.setattr(
        ae,
        "_replace_edit_selection",
        lambda hwnd, start, end, text: calls.append(
            (hwnd, start, end, text)) or True,
    )

    assert ae._replace_native_edit_suffix("st ", "keep stable ", 0)
    assert calls == [(123, 2, 5, "keep stable ")]


def test_native_edit_replacement_rejects_changed_focus(monkeypatch):
    focused_handles = iter((123, 456))
    monkeypatch.setattr(
        ae, "_get_focused_window_handle", lambda: next(focused_handles))
    monkeypatch.setattr(ae, "_is_native_edit_control", lambda _hwnd: True)
    monkeypatch.setattr(ae, "_get_edit_selection", lambda _hwnd: (3, 3))
    monkeypatch.setattr(ae, "_read_window_text", lambda _hwnd: "st ")
    monkeypatch.setattr(
        ae,
        "_replace_edit_selection",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("must not replace text after focus changed")),
    )

    assert not ae._replace_native_edit_suffix("st ", "keep stable ", 0)


def test_replace_uses_native_edit_path_without_clipboard(monkeypatch):
    monkeypatch.setattr(ae, "_replace_native_edit_suffix", lambda *_args: True)
    monkeypatch.setattr(
        ae,
        "_clipboard_get_text",
        lambda: (_ for _ in ()).throw(
            AssertionError("native edit replacement must not read clipboard")),
    )
    engine = ae.AbbrevEngine(object())
    engine._suppressing = True

    engine._do_replace(3, "keep stable ", "st", "st ")

    assert engine._suppressing is False


def test_clipboard_open_retries_short_contention(monkeypatch):
    class FakeUser32:
        def __init__(self):
            self.calls = 0

        def OpenClipboard(self, _owner):
            self.calls += 1
            return self.calls == 3

    user32 = FakeUser32()
    monkeypatch.setattr(ae.time, "sleep", lambda _seconds: None)

    assert ae._open_clipboard_with_retry(user32)
    assert user32.calls == 3


def test_clipboard_write_does_not_clear_existing_data_when_allocation_fails(monkeypatch):
    events = []

    class FakeKernel32:
        @staticmethod
        def GlobalAlloc(_flags, _size):
            events.append("alloc")
            return 0

    class FakeUser32:
        @staticmethod
        def EmptyClipboard():
            events.append("empty")
            return True

    class FakeWindll:
        kernel32 = FakeKernel32()
        user32 = FakeUser32()

    monkeypatch.setattr(ae, "_ensure_win32_configured", lambda: None)
    monkeypatch.setattr(ae.ctypes, "windll", FakeWindll())

    assert not ae._clipboard_set_text("new text")
    assert events == ["alloc"]


def test_clipboard_write_frees_memory_when_lock_fails(monkeypatch):
    freed = []

    class FakeKernel32:
        @staticmethod
        def GlobalAlloc(_flags, _size):
            return 777

        @staticmethod
        def GlobalLock(_handle):
            return 0

        @staticmethod
        def GlobalFree(handle):
            freed.append(handle)

    class FakeWindll:
        kernel32 = FakeKernel32()
        user32 = object()

    monkeypatch.setattr(ae, "_ensure_win32_configured", lambda: None)
    monkeypatch.setattr(ae.ctypes, "windll", FakeWindll())

    assert not ae._clipboard_set_text("new text")
    assert freed == [777]


def test_ime_detection_allows_alphanumeric_mode_inside_chinese_ime(monkeypatch):
    class FakeImm32:
        @staticmethod
        def ImmGetContext(_hwnd):
            return 99

        @staticmethod
        def ImmGetCompositionStringW(*_args):
            return 0

        @staticmethod
        def ImmGetConversionStatus(_himc, _conversion, _sentence):
            return True

        @staticmethod
        def ImmGetOpenStatus(_himc):
            return True

        @staticmethod
        def ImmReleaseContext(_hwnd, _himc):
            return True

    class FakeWindll:
        imm32 = FakeImm32()

    monkeypatch.setattr(ae, "_ensure_imm_configured", lambda: None)
    monkeypatch.setattr(ae, "_get_focused_window_handle", lambda: 200)
    monkeypatch.setattr(ae.ctypes, "windll", FakeWindll())

    assert not ae.should_skip_for_input_method()


def _install_ime_control_stub(monkeypatch, conv_mode, open_status,
                              ime_wnd=4321):
    """[v7] 模擬「跨行程 WM_IME_CONTROL 查 IME」路徑:設定 ImmGetDefaultIMEWnd 與
    _send_message_timeout(回 conversion mode / open status)。回傳記錄送出的查詢。"""
    class FakeImm32:
        @staticmethod
        def ImmGetDefaultIMEWnd(_hwnd):
            return ime_wnd

    monkeypatch.setattr(ae, "_ensure_imm_configured", lambda: None)
    monkeypatch.setattr(ae, "_get_focused_window_handle", lambda: 200)

    class FakeWindll:
        imm32 = FakeImm32()
    monkeypatch.setattr(ae.ctypes, "windll", FakeWindll())

    sent = []

    def fake_send(hwnd, message, wparam=0, lparam=0, timeout_ms=80):
        sent.append((hwnd, message, wparam))
        if message == ae._WM_IME_CONTROL and wparam == ae._IMC_GETCONVERSIONMODE:
            return True, conv_mode
        if message == ae._WM_IME_CONTROL and wparam == ae._IMC_GETOPENSTATUS:
            return True, open_status
        return False, 0

    monkeypatch.setattr(ae, "_send_message_timeout", fake_send)
    return sent


def test_ime_control_chinese_mode_skips(monkeypatch):
    """跨行程查到「IME 開啟 + NATIVE 中文模式」→ 跳過展開(就是這次要修的情境)。"""
    _install_ime_control_stub(
        monkeypatch, conv_mode=ae._IME_CMODE_NATIVE, open_status=1)
    assert ae.should_skip_for_input_method() is True


def test_ime_control_english_mode_allows(monkeypatch):
    """IME 開啟但英文模式(NATIVE off)→ 允許展開。"""
    _install_ime_control_stub(monkeypatch, conv_mode=0, open_status=1)
    assert ae.should_skip_for_input_method() is False


def test_ime_control_closed_allows(monkeypatch):
    """IME 關閉(直接英數)即使 conversion 殘留 NATIVE 也允許展開。"""
    _install_ime_control_stub(
        monkeypatch, conv_mode=ae._IME_CMODE_NATIVE, open_status=0)
    assert ae.should_skip_for_input_method() is False


def test_ime_detection_falls_back_to_foreground_context(monkeypatch):
    seen_handles = []

    class FakeImm32:
        @staticmethod
        def ImmGetContext(hwnd):
            seen_handles.append(hwnd)
            return 99 if hwnd == 100 else 0

        @staticmethod
        def ImmGetCompositionStringW(*_args):
            return 0

        @staticmethod
        def ImmGetConversionStatus(_himc, conversion_pointer, _sentence):
            conversion = ctypes.cast(
                conversion_pointer, ctypes.POINTER(ae.wintypes.DWORD)).contents
            conversion.value = ae._IME_CMODE_NATIVE
            return True

        @staticmethod
        def ImmGetOpenStatus(_himc):
            return False

        @staticmethod
        def ImmReleaseContext(_hwnd, _himc):
            return True

    class FakeUser32:
        @staticmethod
        def GetForegroundWindow():
            return 100

    class FakeWindll:
        imm32 = FakeImm32()
        user32 = FakeUser32()

    monkeypatch.setattr(ae, "_ensure_imm_configured", lambda: None)
    monkeypatch.setattr(ae, "_get_focused_window_handle", lambda: 200)
    monkeypatch.setattr(ae.ctypes, "windll", FakeWindll())

    assert ae.should_skip_for_input_method()
    assert seen_handles == [200, 100]
