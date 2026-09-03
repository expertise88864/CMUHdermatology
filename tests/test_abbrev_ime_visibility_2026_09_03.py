# -*- coding: utf-8 -*-
"""[AB-09] 縮寫在 HIS 以外「安靜地不展開」→ 要看得見、分得開、而且不會被拔掉。

使用者回報「縮寫只有 HIS 有效」。程式碼裡★沒有★任何「只在 HIS 才展開」的閘門:
掛的是全域鍵盤 hook,`_try_expand` 沒有視窗判斷,非原生欄位走剪貼簿/keystroke
fallback。唯一★會依當前聚焦視窗而不同★的閘門是輸入法判斷 —— 而 Windows 是
★逐視窗★記住中/英模式的,所以「HIS 展得開、瀏覽器展不開」完全可能同時成立。

舊版的問題不是行為錯,是★它不說話★:跳過只寫 `logging.debug`(而 log level 是
INFO)、畫面零回饋 → 使用者只看得到「沒反應」,只能歸因成「這個功能不支援這裡」。

本檔釘住三件事:
  1. 兩個原因(組字中 / 中文模式)★分得開★ —— 它們的處置完全不同;
  2. 跳過會留下 INFO 紀錄並通報 UI,且★限流是 per reason★;
  3. 每一條 unhook 全域 hook 的路徑都要★重掛★ abbrev(熱鍵註冊失敗那條原本漏了,
     會讓縮寫在所有程式含 HIS 一起失效,retry 用完就得重啟)。
"""
import ast
import io
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cmuh_common.abbrev_engine as ae  # noqa: E402
from cmuh_common.abbrev_engine import AbbrevConfig, AbbrevEngine  # noqa: E402

_MAIN_PY = os.path.join(os.path.dirname(__file__), "..", "src", "main.py")


# ─── helpers ────────────────────────────────────────────────────────────────
class _FakeKb:
    """AbbrevEngine 只在 install/送鍵時碰 kb;本檔全部停在 _try_expand 之前。"""

    pressed: set = set()

    def is_pressed(self, key):
        return key in self.pressed

    def on_press(self, *a, **k):
        return object()

    def unhook(self, *a, **k):
        return None

    def send(self, *a, **k):
        return None

    def write(self, *a, **k):
        return None


def _engine(lookup=None):
    eng = AbbrevEngine(_FakeKb())
    lookup = lookup or {"nev": "nevus, benign appearing"}
    eng._lookup = dict(lookup)
    eng._max_abbrev_len = max(len(k) for k in lookup)
    eng._cfg = AbbrevConfig(enabled=True, skip_when_ime_active=True)
    return eng


_FOCUSED_HWND = 200
_FOREGROUND_HWND = 300


def _stub_ime_control(monkeypatch, conv_mode, open_status, ime_wnd=4321,
                      comp_size=0, comp_raises=False, child_has_context=True):
    """模擬跨行程 WM_IME_CONTROL 路徑(現代 TSF IME 走這條)。

    `comp_size` 是「WM 查詢成功時★同時★正在組字」的組字字元數 —— 外審 r1 P2
    指出的真實組合。`child_has_context=False` 模擬★舊控制項只在前景最上層視窗
    開 IMM context★(外審 r2 P2)。回傳 probed 以便斷言查了哪些視窗。
    """
    probed = []

    class FakeImm32:
        @staticmethod
        def ImmGetDefaultIMEWnd(_hwnd):
            return ime_wnd

        @staticmethod
        def ImmGetContext(hwnd):
            probed.append(hwnd)
            if comp_raises:
                raise OSError("IMM 讀取失敗")
            if hwnd == _FOCUSED_HWND and not child_has_context:
                return 0
            return 777

        @staticmethod
        def ImmGetCompositionStringW(_himc, _flag, _buf, _len):
            return comp_size

        @staticmethod
        def ImmReleaseContext(_hwnd, _himc):
            return True

    class FakeUser32:
        @staticmethod
        def GetForegroundWindow():
            return _FOREGROUND_HWND

    class FakeWindll:
        imm32 = FakeImm32()
        user32 = FakeUser32()

    monkeypatch.setattr(ae, "_ensure_imm_configured", lambda: None)
    monkeypatch.setattr(ae, "_get_focused_window_handle", lambda: _FOCUSED_HWND)
    monkeypatch.setattr(ae.ctypes, "windll", FakeWindll())

    def fake_send(hwnd, message, wparam=0, lparam=0, timeout_ms=80):
        if message == ae._WM_IME_CONTROL and wparam == ae._IMC_GETCONVERSIONMODE:
            return True, conv_mode
        if message == ae._WM_IME_CONTROL and wparam == ae._IMC_GETOPENSTATUS:
            return True, open_status
        return False, 0

    monkeypatch.setattr(ae, "_send_message_timeout", fake_send)
    return probed


def _stub_legacy_imm(monkeypatch, comp_size=0, conv_ok=False, conv_value=0,
                     open_status=0):
    """模擬舊 IMM 路徑(WM_IME_CONTROL 查不到 → 退回 ImmGetContext)。"""
    class FakeImm32:
        @staticmethod
        def ImmGetDefaultIMEWnd(_hwnd):
            return 0                      # 沒有 IME 視窗 → 走舊路徑

        @staticmethod
        def ImmGetContext(_hwnd):
            return 999

        @staticmethod
        def ImmGetCompositionStringW(_himc, _flag, _buf, _len):
            return comp_size

        @staticmethod
        def ImmGetConversionStatus(_himc, conv_ref, _sent_ref):
            if not conv_ok:
                return 0
            conv_ref._obj.value = conv_value
            return 1

        @staticmethod
        def ImmGetOpenStatus(_himc):
            return open_status

        @staticmethod
        def ImmReleaseContext(_hwnd, _himc):
            return True

    class FakeWindll:
        imm32 = FakeImm32()

    monkeypatch.setattr(ae, "_ensure_imm_configured", lambda: None)
    monkeypatch.setattr(ae, "_get_focused_window_handle", lambda: _FOCUSED_HWND)
    monkeypatch.setattr(ae.ctypes, "windll", FakeWindll())


def _main_func(name):
    """回傳 src/main.py 裡該函式的 AST 節點(找不到就直接失敗,不靜默跳過)。"""
    tree = ast.parse(io.open(_MAIN_PY, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"main.py 找不到函式 {name} —— 這條守衛已經失效")


def _called_names(node):
    out = set()
    for c in ast.walk(node):
        if isinstance(c, ast.Call):
            f = c.func
            out.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return out


# ─── 1. 兩個原因分得開(處置不同) ───────────────────────────────────────────
def test_chinese_mode_reports_native(monkeypatch):
    _stub_ime_control(monkeypatch, conv_mode=ae._IME_CMODE_NATIVE, open_status=1)
    assert ae.input_method_skip_reason() == ae.IME_SKIP_NATIVE


def test_english_mode_reports_no_reason(monkeypatch):
    """IME 開著但英文模式 → 允許展開(這正是 v6 修過的『英文模式被誤擋』)。"""
    _stub_ime_control(monkeypatch, conv_mode=0, open_status=1)
    assert ae.input_method_skip_reason() is None


def test_ime_closed_reports_no_reason(monkeypatch):
    _stub_ime_control(monkeypatch, conv_mode=ae._IME_CMODE_NATIVE, open_status=0)
    assert ae.input_method_skip_reason() is None


def test_composing_reports_composing(monkeypatch):
    """★組字中和中文模式必須分開★:前者打完就好,後者不切英數就永遠展不開。"""
    _stub_legacy_imm(monkeypatch, comp_size=4)
    assert ae.input_method_skip_reason() == ae.IME_SKIP_COMPOSING


def test_composing_under_a_modern_ime_reports_composing(monkeypatch):
    """★外審 r1 P2★:現代 TSF 輸入法 WM 查詢會成功,而組字中★同時★是
    open+NATIVE。只看模式就會全部歸成「中文模式」→ 使用者打到一半被叫去
    「按 Shift 切英數」,那是錯的指示,IME_SKIP_COMPOSING 也就永遠到不了。

    (上一條測試刻意讓 ImmGetDefaultIMEWnd 回 0 只走 legacy 路徑,所以量不到
     這個真實組合 —— 反例沒有隔離到規則本身。)"""
    _stub_ime_control(monkeypatch, conv_mode=ae._IME_CMODE_NATIVE,
                      open_status=1, comp_size=6)
    assert ae.input_method_skip_reason() == ae.IME_SKIP_COMPOSING


def test_chinese_mode_without_composition_is_still_native(monkeypatch):
    """反例要只靠這條規則分勝負:補了組字探測之後,★沒在組字★的中文模式
    仍然必須回 NATIVE(否則就再也不會叫使用者去切英數了)。"""
    _stub_ime_control(monkeypatch, conv_mode=ae._IME_CMODE_NATIVE,
                      open_status=1, comp_size=0)
    assert ae.input_method_skip_reason() == ae.IME_SKIP_NATIVE


def test_composition_is_found_on_the_foreground_window(monkeypatch):
    """★外審 r2 P2★:有些舊控制項只在★前景最上層視窗★開 IMM context ——
    legacy 路徑本來就有這條 fallback。新的組字探測若不跟著做,聚焦子視窗查
    不到就當成「沒在組字」→ 又退回錯的「按 Shift 切英數」。

    (前一條測試的 fake 讓聚焦子視窗永遠拿得到 context,所以量不到這個情境。)"""
    probed = _stub_ime_control(monkeypatch, conv_mode=ae._IME_CMODE_NATIVE,
                               open_status=1, comp_size=6,
                               child_has_context=False)
    assert ae.input_method_skip_reason() == ae.IME_SKIP_COMPOSING
    assert probed == [_FOCUSED_HWND, _FOREGROUND_HWND], \
        "要先問聚焦子視窗,查不到才退到前景最上層視窗"


def test_no_context_anywhere_stays_native(monkeypatch):
    """反例要只靠這條規則分勝負:兩個視窗都拿不到 context 時★不可以★猜成
    組字中(會把真正需要切英數的情況說成「打完就好」)。"""
    class _NoCtx:
        @staticmethod
        def GetForegroundWindow():
            return _FOCUSED_HWND          # 前景就是聚焦視窗 → 沒有第二條路可走

    _stub_ime_control(monkeypatch, conv_mode=ae._IME_CMODE_NATIVE,
                      open_status=1, comp_size=6, child_has_context=False)
    monkeypatch.setattr(ae.ctypes.windll, "user32", _NoCtx())
    assert ae.input_method_skip_reason() == ae.IME_SKIP_NATIVE


def test_english_mode_does_not_probe_composition(monkeypatch):
    """英文模式是放行,不必也不該去讀組字狀態(多一次跨行程呼叫在打字路徑上)。"""
    probed = _stub_ime_control(monkeypatch, conv_mode=0, open_status=1,
                               comp_size=9)
    assert ae.input_method_skip_reason() is None
    assert probed == [], "放行的情況不該去探組字"


def test_a_failing_composition_probe_falls_back_to_native(monkeypatch):
    """探測失敗時要維持舊分類(中文模式),不可以整個判斷崩掉或誤報組字中。"""
    _stub_ime_control(monkeypatch, conv_mode=ae._IME_CMODE_NATIVE,
                      open_status=1, comp_raises=True)
    assert ae.input_method_skip_reason() == ae.IME_SKIP_NATIVE


def test_legacy_conversion_native_reports_native(monkeypatch):
    _stub_legacy_imm(monkeypatch, conv_ok=True, conv_value=ae._IME_CMODE_NATIVE)
    assert ae.input_method_skip_reason() == ae.IME_SKIP_NATIVE


def test_legacy_conversion_english_reports_no_reason(monkeypatch):
    _stub_legacy_imm(monkeypatch, conv_ok=True, conv_value=0)
    assert ae.input_method_skip_reason() is None


def test_legacy_open_status_fallback_reports_native(monkeypatch):
    """conversion 讀不到的舊 IME → 退回 OpenStatus,仍要給得出原因。"""
    _stub_legacy_imm(monkeypatch, conv_ok=False, open_status=1)
    assert ae.input_method_skip_reason() == ae.IME_SKIP_NATIVE


@pytest.mark.parametrize("reason", [None, ae.IME_SKIP_NATIVE, ae.IME_SKIP_COMPOSING])
def test_the_boolean_wrapper_still_agrees_with_the_reason(monkeypatch, reason):
    """舊的布林 API 必須是新函式的薄包裝 —— 別的呼叫端還在用它。"""
    monkeypatch.setattr(ae, "input_method_skip_reason", lambda: reason)
    assert ae.should_skip_for_input_method() is (reason is not None)


# ─── 2. 看得見:INFO 紀錄 + 兩個原因訊息不同 ───────────────────────────────
def test_the_skip_is_recorded_at_info_not_debug(caplog):
    """★這就是本批的重點★:舊版寫 debug,而 log level 是 INFO → 一行都不會留。"""
    eng = _engine()
    with caplog.at_level(logging.INFO):
        eng._note_ime_skip(ae.IME_SKIP_NATIVE, "nev")
    assert [r for r in caplog.records if r.levelno >= logging.INFO
            and "nev" in r.getMessage()], "跳過必須留下 INFO 等級的紀錄"


def test_the_two_reasons_get_different_messages(caplog):
    """訊息要能分開★處置不同★的原因:中文模式要講怎麼解,組字中不必。"""
    eng = _engine()
    with caplog.at_level(logging.INFO):
        eng._note_ime_skip(ae.IME_SKIP_NATIVE, "nev")
        eng._note_ime_skip(ae.IME_SKIP_COMPOSING, "nev")
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.INFO]
    assert len(msgs) == 2
    native, composing = msgs
    assert native != composing
    assert "英數" in native, "中文模式要告訴使用者怎麼救"
    assert "英數" not in composing, "組字中不該叫使用者去切輸入法(打完就好)"


def test_try_expand_notifies_and_does_not_expand(monkeypatch):
    """整條路走一次:命中縮寫 → IME 擋下 → ★不展開、但要通報★。"""
    eng = _engine()
    monkeypatch.setattr(ae, "input_method_skip_reason",
                        lambda: ae.IME_SKIP_NATIVE)
    replaced = []
    monkeypatch.setattr(eng, "_do_replace",
                        lambda *a, **k: replaced.append(a))
    seen = []
    eng.set_ime_skip_notifier(lambda reason, abbrev: seen.append((reason, abbrev)))

    assert eng._try_expand("nev", " ") is False
    assert replaced == [], "被 IME 擋下就不可以送鍵"
    assert eng._ime_skipped is True
    assert seen == [(ae.IME_SKIP_NATIVE, "nev")]


def test_no_notification_when_the_input_method_allows_it(monkeypatch):
    """反例要只靠這條規則分勝負:輸入法放行時★不可以★通報(否則變成洗版)。"""
    eng = _engine()
    monkeypatch.setattr(ae, "input_method_skip_reason", lambda: None)
    monkeypatch.setattr(eng, "_do_replace", lambda *a, **k: None)
    seen = []
    eng.set_ime_skip_notifier(lambda reason, abbrev: seen.append(reason))

    assert eng._try_expand("nev", " ") is True
    assert seen == []


# ─── 3. 限流:不洗版,但也不可以把不同原因一起吃掉 ─────────────────────────
def test_a_repeat_within_the_window_is_not_notified():
    eng = _engine()
    seen = []
    eng.set_ime_skip_notifier(lambda reason, abbrev: seen.append(reason))
    eng._note_ime_skip(ae.IME_SKIP_NATIVE, "nev")
    eng._note_ime_skip(ae.IME_SKIP_NATIVE, "nev")
    assert len(seen) == 1


def test_it_speaks_again_after_the_window():
    eng = _engine()
    seen = []
    eng.set_ime_skip_notifier(lambda reason, abbrev: seen.append(reason))
    eng._note_ime_skip(ae.IME_SKIP_NATIVE, "nev")
    # 把上次通報的時間往回推超過限流窗口(不睡 60 秒)
    eng._ime_skip_last_notice[ae.IME_SKIP_NATIVE] -= (
        eng.IME_SKIP_NOTICE_INTERVAL_SEC + 1)
    eng._note_ime_skip(ae.IME_SKIP_NATIVE, "nev")
    assert len(seen) == 2


def test_the_rate_limit_is_per_reason():
    """★單一全域時間戳會把第二個原因整個吃掉★ —— 而那兩件事的處置不同。"""
    eng = _engine()
    seen = []
    eng.set_ime_skip_notifier(lambda reason, abbrev: seen.append(reason))
    eng._note_ime_skip(ae.IME_SKIP_NATIVE, "nev")
    eng._note_ime_skip(ae.IME_SKIP_COMPOSING, "nev")
    assert seen == [ae.IME_SKIP_NATIVE, ae.IME_SKIP_COMPOSING]


# ─── 4. 韌性:通報壞掉不可以拖垮打字 ───────────────────────────────────────
def test_a_failing_notifier_does_not_break_typing(monkeypatch):
    eng = _engine()
    monkeypatch.setattr(ae, "input_method_skip_reason",
                        lambda: ae.IME_SKIP_NATIVE)

    def boom(reason, abbrev):
        raise RuntimeError("UI 佇列壞了")

    eng.set_ime_skip_notifier(boom)
    assert eng._try_expand("nev", " ") is False   # 例外不可以逸出到 hook 緒


def test_without_a_notifier_it_still_logs(caplog):
    eng = _engine()                               # 預設沒有 notifier
    with caplog.at_level(logging.INFO):
        eng._note_ime_skip(ae.IME_SKIP_NATIVE, "nev")
    assert any("nev" in r.getMessage() for r in caplog.records)


# ─── 5. main.py 接線:沒有呼叫端的功能等於不存在 ───────────────────────────
def test_the_engine_is_actually_given_the_notifier():
    assert "set_ime_skip_notifier" in _called_names(
        _main_func("_ensure_abbrev_engine")), \
        "引擎建立時沒接上通報 → 這個功能對使用者不存在"


def test_the_hotkey_failure_branch_reinstalls_the_abbrev_hook():
    """★本批修的真缺陷★:setup_hotkeys 開頭 unhook_all 會把 abbrev 一起拔掉,
    原本只有成功路徑重掛。註冊拋例外 → 縮寫在★所有程式(含 HIS)★一起失效,
    而且 retry 五次用完就再也不重掛,只能重啟主程式。"""
    fn = _main_func("setup_hotkeys")
    handlers = [h for h in ast.walk(fn) if isinstance(h, ast.ExceptHandler)
                and any("熱鍵註冊失敗" in n.value
                        for n in ast.walk(h)
                        if isinstance(n, ast.Constant) and isinstance(n.value, str))]
    assert handlers, "找不到熱鍵註冊失敗的處理分支 —— 守衛已失效"
    assert any("_install_abbrev_listeners" in _called_names(h) for h in handlers)


def test_every_unhook_path_either_exits_or_reinstalls():
    """涵蓋★性質所及的所有函式★,不是只釘一個檔案位置:任何拔掉全域 hook 的
    函式,不是退出流程就必須重掛 abbrev —— 否則縮寫會在所有程式一起靜默失效。"""
    tree = ast.parse(io.open(_MAIN_PY, encoding="utf-8").read())
    exit_paths = {"_cleanup_for_exit", "_restart_app", "_teardown_for_handover"}
    found, offenders = set(), []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        calls = _called_names(node)
        if "safe_unhook_all_hotkeys" not in calls:
            continue
        found.add(node.name)
        if node.name in exit_paths:
            continue
        if "_install_abbrev_listeners" not in calls:
            offenders.append(f"{node.name}:{node.lineno}")
    assert found, "掃不到任何 unhook 路徑 —— 空集合不算通過"
    assert exit_paths <= found, (
        f"退出路徑白名單對不上實際程式碼(改名了?):{exit_paths - found}")
    assert not offenders, f"這些路徑拔了 hook 卻沒重掛:{offenders}"


def test_the_notification_never_touches_tk_from_the_hook_thread():
    """通報由 keyboard hook 緒呼叫 → 只能走佇列,碰 Tk 會不定時炸掉整個 UI。"""
    fn = _main_func("_on_abbrev_ime_skip")
    calls = _called_names(fn)
    assert "put_ui_message" in calls
    for forbidden in ("showwarning", "showinfo", "_show_notice", "config", "after"):
        assert forbidden not in calls, f"_on_abbrev_ime_skip 不可以碰 {forbidden}"


def test_the_status_message_tells_the_user_what_to_do():
    """使用者的困惑是「這裡不能用」,訊息要把真正的原因與解法講出來。"""
    import main                                    # noqa: PLC0415

    app = main.AutomationApp.__new__(main.AutomationApp)
    sent = []

    class FakeQueue:
        def put_nowait(self, msg):
            sent.append(msg)

    app.ui_queue = FakeQueue()
    app._on_abbrev_ime_skip(ae.IME_SKIP_NATIVE, "nev")
    app._on_abbrev_ime_skip(ae.IME_SKIP_COMPOSING, "nev")

    texts = [getattr(m, "text", "") for m in sent]
    assert len(texts) == 2
    assert "nev" in texts[0] and "Shift" in texts[0]
    assert "Shift" not in texts[1], "組字中不該叫使用者去切輸入法"
