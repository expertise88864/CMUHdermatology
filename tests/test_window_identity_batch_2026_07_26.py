# -*- coding: utf-8 -*-
"""[2026-07-26 審查] 視窗辨識群 —— 同一個病灶的四項修正。

病灶:**視窗/控制項只用 class(+title)辨識,而且開迴路動作不回讀驗證。**
四項都會「表面上完全正常」:視窗有、按鈕點得到、log 一路綠,但打在錯的對象上,
或根本沒完成卻回報完成。
"""
import inspect
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402


def _code_only(src: str) -> str:
    """剝掉註解與 docstring —— 說明文字裡就有這些字串,不剝的話斷言會恆真。"""
    src = re.sub(r'("""|\'\'\')(?:.|\n)*?\1', "", src)
    return "\n".join(line.split("#")[0] for line in src.splitlines())


# ── ① 療程欄定位:要分排 + 結構驗證,不可直接取最左窄欄位 ──────────────────────
def _fake_edits(monkeypatch, fields):
    """fields: [(hwnd, left, top, width)] —— 模擬 EnumChildWindows 的結果。"""
    class _R:
        def __init__(self, l=0, t=0, r=2000, b=2000):
            self.left, self.top, self.right, self.bottom = l, t, r, b

    monkeypatch.setattr(main.ctypes, "windll", main.ctypes.windll)  # 保持真物件
    calls = {"i": 0}

    def _enum(_parent, cb, _lp):
        for f in fields:
            cb(f[0], 0)
        return True
    return _enum, calls


def test_treatment_field_requires_two_narrow_fields_on_one_row(monkeypatch):
    """★寫錯欄位★ rel_y 80-135 是寬頻帶,可能同時框到療程排與診斷排。
    混在一起再依 left 排序,最左的窄欄位未必是療程 → 療程次數寫進別的欄位,
    而且寫回不回讀。必須先分排、且該排要有療程+類別兩個窄欄位。"""
    code = _code_only(inspect.getsource(main._find_療程_edit_hwnd))
    assert "candidate_rows" in code, "必須先依 top 分排再挑"
    assert "len(row[1]) >= 2" in code, "該排要有兩個窄欄位(療程+類別)"
    assert "len(candidate_rows) != 1" in code and "return 0" in code, \
        "找不到或分不出唯一一排 → 回 0 交人工,不可猜"
    # 舊寫法(直接取最左)不可再存在
    assert "narrow[0][0]" not in code


# ── ② Round 4:用可觀察的事實判定成功,不可無條件 return True ──────────────────
def test_round4_uses_popup_closed_as_success_proxy():
    code = _code_only(inspect.getsource(main._f9_f10_round4_submit_and_confirm))
    assert "_f9_f10_wait_consent_popup_closed(" in code, \
        "結尾要用「同意書 popup 是否關閉」判定,不可無條件 return True"
    # 每一條 return 都不可以是「沒驗證就說成功」
    tail = code[code.rindex("if dlg2:"):]
    assert "return _f9_f10_wait_consent_popup_closed(" in tail


def test_round4_reports_failure_when_warning_dialog_stays_open():
    code = _code_only(inspect.getsource(main._f9_f10_round4_submit_and_confirm))
    i_loop = code.index("IsWindow(dlg)")
    assert "return False" in code[i_loop:], \
        "IDYES 後對話框仍在 → 送出未被接受,必須回報失敗"


def test_round4_manual_handover_is_not_reported_as_success():
    """PID 取不到時是【交醫師手動按是】,不是流程完成 —— 回 True 會讓呼叫端記
    「整段 F9/F10 流程完成」、UI 顯示「操作完成」,醫師看到就走了。"""
    code = _code_only(inspect.getsource(main._f9_f10_round4_submit_and_confirm))
    i = code.index("if not popup_pid:")
    seg = code[i:i + 400]
    assert "return False" in seg, "交人工不可回報成功"


def test_consent_popup_wait_helper_fails_closed():
    code = _code_only(inspect.getsource(main._f9_f10_wait_consent_popup_closed))
    assert "IsWindow(popup_hwnd)" in code
    assert "return True" in code and "return False" in code


# ── ③ 前景保護器:class 不足以辨識醫院視窗;追蹤的 hwnd 要清 ───────────────────
def test_foreground_protector_requires_pid_not_just_class():
    code = _code_only(inspect.getsource(main._ForegroundProtector))
    assert "_is_hospital_window" in code, "要有 class+PID 的判定"
    helper = _code_only(inspect.getsource(main._ForegroundProtector._is_hospital_window))
    assert "_get_window_pid(" in helper and "self._his_pid" in helper
    # [外審 R3] 取不到 HIS PID 時【不可】退回只比 class:HOSPITAL_WINDOW_CLASSES 裡有
    # #32770(任何程式的標準對話框)→ 別的程式一跳警告框就被當成醫院搶焦點,
    # 保護器反而把使用者正在用的視窗搶走。認不出 HIS 就不做保護。
    i = helper.index("if not self._his_pid:")
    assert "return False" in helper[i:i + 200], "認不出 HIS 時不可放行"


def test_foreground_protector_clears_dead_tracked_hwnd():
    """★HWND 會被回收★ 追蹤的視窗關掉後不清空,同一個數字之後可能屬於別的視窗
    (甚至 HIS 自己的),那時 SetForegroundWindow 就把焦點拉到使用者沒在用的視窗。"""
    code = _code_only(inspect.getsource(main._ForegroundProtector._run))
    i_clear = code.index("self.tracked_user_hwnd = 0")
    i_restore = code.index("SetForegroundWindow(self.tracked_user_hwnd)")
    assert i_clear < i_restore, "清除死 hwnd 必須在 restore 之前"


# ── ④ TOrMain / 片語 popup:排除殘留視窗 + 限定 HIS 行程 ─────────────────────
@pytest.mark.parametrize("fn_name,cls", [
    ("script_F9_F10_consent_form_adaptive", "TOrMain"),
    ("_select_phrase_and_return", "TfrmOrrSentence"),
])
def test_window_waits_exclude_stale_and_require_pid(fn_name, cls):
    """★打在舊視窗上★ `_wait_for_window` 找到就回傳 —— 上一次流程留下沒關的視窗
    會被立刻回傳,後續操作全打在舊視窗上,而且表面完全正常。"""
    fn = getattr(main, fn_name, None)
    assert fn is not None, f"{fn_name} 不存在(改名了?請同步更新本測試)"
    code = _code_only(inspect.getsource(fn))
    assert "_collect_windows_by_class(" in code, f"{cls}:要先拍既有視窗快照"
    i_snap = code.index("_collect_windows_by_class(")
    i_wait = code.index('_wait_for_window("' + cls)
    assert i_snap < i_wait, "快照必須在觸發新視窗之前拍"
    assert "exclude_hwnds=" in code and "require_pid=" in code


def test_wait_for_window_supports_exclude_hwnds():
    sig = inspect.signature(main._wait_for_window)
    assert "exclude_hwnds" in sig.parameters
    code = _code_only(inspect.getsource(main._wait_for_window))
    assert "exclude_hwnds=exclude_hwnds" in code, "要真的傳下去(不能只收參數不用)"


def test_collect_windows_by_class_is_bounded():
    """列舉要有上限,不可因為排除邏輯出錯就無限迴圈卡住熱鍵緒。"""
    code = _code_only(inspect.getsource(main._collect_windows_by_class))
    assert "range(" in code, "必須有上限防禦"


# ── 外審補強 ────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("fn_name", [
    "script_F9_F10_consent_form_adaptive",
    "_select_phrase_and_return",
])
def test_zero_pid_fails_closed_on_identity_sensitive_paths(fn_name):
    """★外審★ `require_pid=0` 等於【關掉】行程過濾 —— 另一個 HIS instance 的視窗會被
    選中,最後對錯的行程/病人送出同意書。Round 4 對取不到 PID 早就 fail-closed,
    這兩條同樣是身分敏感路徑,取捨必須一致。"""
    code = _code_only(inspect.getsource(getattr(main, fn_name)))
    i = code.index("if not _his_pid:")
    assert "return False" in code[i:i + 400], "取不到 PID 必須中止,不可帶 0 往下走"
    assert code.index("if not _his_pid:") < code.index("_collect_windows_by_class("), \
        "PID 檢查要在拍快照/送命令之前"


def test_phrase_popup_close_wait_targets_the_exact_handle():
    """★外審★ 等 popup 關若用 class 全域找:(a) 殘留舊 popup 會讓每個片語都白等滿
    5 秒;(b) 沒關成功卻回 True → 片語根本沒帶回,流程照樣進 Round 4 送出同意書。"""
    code = _code_only(inspect.getsource(main._select_phrase_and_return))
    tail = code[code.index("帶回"):]
    assert "IsWindow(phrase_popup)" in tail, "要等這一個 hwnd 關,不可用 class 全域找"
    assert '_find_window_by_class_title("TfrmOrrSentence"' not in tail
    assert tail.rstrip().endswith("return False"), "逾時未關必須回報未完成"


def test_protector_rejects_unknown_candidate_pid_when_his_pid_known():
    """★外審★ HIS PID 已知時,候選視窗 PID 取不到不可放行 —— 那正是「class 撞名的
    別程式視窗」最可能發生的情況,放行等於這個修正沒做。"""
    code = _code_only(
        inspect.getsource(main._ForegroundProtector._is_hospital_window))
    assert "(not pid)" not in code, "PID 取不到不可視為醫院視窗"
    assert "_get_window_pid(hwnd) == self._his_pid" in code


def test_recycled_hwnd_is_not_treated_as_the_tracked_window(monkeypatch):
    """★外審 R2 行為測試★ 視窗在兩次輪詢(300ms)之間關閉、HWND 又被【回收】給另一個
    視窗時,IsWindow 仍為真 → 舊寫法會把焦點搶去那個毫不相干的新視窗。
    連 (PID, class) 身分一起比對才擋得住。"""
    p = main._ForegroundProtector()
    state = {"pid": 111, "cls": "Chrome_WidgetWin_1"}
    monkeypatch.setattr(main, "_get_window_pid", lambda _h: state["pid"])
    monkeypatch.setattr(main, "_get_class_name_of", lambda _h: state["cls"])
    monkeypatch.setattr(main.ctypes.windll.user32, "IsWindow", lambda _h: 1)

    p._track(9999)
    assert p._tracked_still_valid(), "同一個視窗要維持有效"

    # 同一個 HWND 值,但已經是別的行程/別的視窗(HWND 被回收)
    state.update(pid=222, cls="Notepad")
    assert not p._tracked_still_valid(), "HWND 被回收後不可再當成原本追蹤的視窗"


def test_tracked_identity_cleared_when_window_gone(monkeypatch):
    p = main._ForegroundProtector()
    monkeypatch.setattr(main, "_get_window_pid", lambda _h: 0)   # 視窗已消失
    monkeypatch.setattr(main, "_get_class_name_of", lambda _h: "")
    monkeypatch.setattr(main.ctypes.windll.user32, "IsWindow", lambda _h: 0)
    p._track(9999)
    assert not p._tracked_still_valid()


def test_same_pid_same_class_reuse_is_detected(monkeypatch):
    """★外審 R4★ 只比 (PID, class) 擋不住「同一個程式關掉一個視窗、又開一個同 class
    的新視窗」剛好拿到同一個 HWND(例:瀏覽器關一個視窗再開一個)。標題必須納入身分。"""
    p = main._ForegroundProtector()
    state = {"title": "報告 A - Chrome"}
    monkeypatch.setattr(main, "_get_window_pid", lambda _h: 111)
    monkeypatch.setattr(main, "_get_class_name_of", lambda _h: "Chrome_WidgetWin_1")
    monkeypatch.setattr(main.ctypes.windll.user32, "IsWindow", lambda _h: 1)
    monkeypatch.setattr(main.ctypes.windll.user32, "GetWindowTextLengthW",
                        lambda _h: len(state["title"]))

    def _get_text(_h, buf, _n):
        buf.value = state["title"]
        return len(state["title"])
    monkeypatch.setattr(main.ctypes.windll.user32, "GetWindowTextW", _get_text)

    p._track(9999)
    assert p._tracked_still_valid()
    state["title"] = "報告 B - Chrome"      # 同 PID 同 class,換了視窗
    assert not p._tracked_still_valid(), "同程式同 class 的新視窗也要被偵測到"
