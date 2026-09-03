# -*- coding: utf-8 -*-
"""[R8-P2] 熱鍵註冊是一筆交易:中途任何一鍵失敗 → clinical hotkeys 必須歸零。

`setup_hotkeys()` 開頭 `safe_unhook_all_hotkeys()`,然後 F1~F11 逐一 `add_hotkey`,
F12(中止=救援鍵)排最後。第 k 鍵拋例外時,前 k-1 鍵★已經掛上而且仍然有效★:
畫面說「熱鍵註冊失敗」,實際上會對 HIS 寫劑量/計費的 F1~F5 還按得動,而中止鍵
可能剛好不存在。30 秒後 retry 才會再 unhook_all;第 5 次仍失敗就放棄 → 半套
熱鍵一直留到手動重啟。

[外審 r1] rollback 靠 `keyboard.unhook_all()`,而★它自己也可能失敗★(add_hotkey
會失敗的時候,正是 hook 機制不健康的時候 —— 兩者相關聯)。所以不變量不可以
依賴 unhook 成功:每個 callback 包一層★交易閘門★,只有「本交易已提交」才執行。

本檔★用假的 keyboard registry 跑真的 setup_hotkeys★(不是 AST grep 字串),釘住:
  1. 任一鍵失敗 → registry 為空(對每個失敗位置都成立);
  2. ★unhook_all 也失敗時,留在 registry 裡的 callback 一顆都不會執行★;
  3. rollback 之後 abbrev 要重掛,而且★順序是先 rollback 再重掛★;
  4. retry 有排;rollback 失敗要記 ERROR;
  5. 成功路徑 10 鍵全掛、閘門打開(證明 fake 真的把整個函式跑完、probe 打得到動作)。
"""
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402

_ALL_KEYS = ["F1", "F2", "F3", "F4", "F5", "F8", "F9", "F10", "F11", "F12"]


class _FakeKeyboard:
    """只模擬 setup_hotkeys 會碰的入口:add_hotkey / unhook_all。
    `fail_at` 是第一個會拋例外的鍵名;`unhook_all_raises` 模擬 rollback 本身失敗
    (★registry 不會被清掉★,正是外審 r1 指出的洞)。"""

    def __init__(self, fail_at=None, unhook_all_raises=False, events=None):
        self.registry = {}
        self.fail_at = fail_at
        self.unhook_all_raises = unhook_all_raises
        self.events = events if events is not None else []

    def add_hotkey(self, key, callback, suppress=False):
        if key == self.fail_at:
            self.events.append(("raise", key))
            raise RuntimeError(f"hook registry busy at {key}")
        self.registry[key] = callback
        self.events.append(("add", key))

    def unhook_all(self):
        self.events.append(("unhook_all",))
        if self.unhook_all_raises:
            raise OSError("low-level hook table corrupted")
        self.registry.clear()


class _Var:
    def __init__(self, v=None):
        self.v = v

    def get(self):
        return self.v

    def set(self, v):
        self.v = v


class _Label:
    def config(self, **kw):
        pass


class _Root:
    def __init__(self, events):
        self.events = events
        self.scheduled = []

    def after(self, delay_ms, cb):
        self.scheduled.append((delay_ms, cb))
        self.events.append(("after", delay_ms))


class _Queue:
    def put_nowait(self, msg):
        pass


def _app(monkeypatch, kb):
    """用 __new__ 造 AutomationApp,只填 setup_hotkeys 走到註冊迴圈所需的欄位。
    回 (app, actions):actions 收集真的打到 run_subsystem_in_thread 的呼叫。"""
    app = main.AutomationApp.__new__(main.AutomationApp)
    events = kb.events
    actions = []
    app._shutting_down = False
    app._heavy_modules_ready = True
    app.hotkey_profile = "1920x1080"
    app.hotkey_version = None
    app.hotkey_display_note = _Var("")
    app.hotkey_text_label = _Label()
    app.status_text = _Var("")
    app.ui_queue = _Queue()
    app.root = _Root(events)
    app.interrupt_automation = lambda: actions.append("F12")
    app.run_subsystem_in_thread = lambda f, n, **k: actions.append(n)

    def _fake_install():
        events.append(("install_abbrev",))

    app._install_abbrev_listeners = _fake_install
    monkeypatch.setattr(main.hotkey_modules, "keyboard", kb)
    monkeypatch.setattr(main, "stop_event_main", type(
        "E", (), {"is_set": staticmethod(lambda: False)})())
    # 黑幕閘門與交易閘門是兩條不同的規則;probe 只量交易閘門 → 黑幕一律放行。
    monkeypatch.setattr(main, "screen_blackout_should_eat_this_hotkey", lambda: False)
    return app, actions


def _press(kb, key):
    """模擬使用者按下已掛在 registry 裡的鍵。F8 是唯一不查前景視窗 class 的鍵
    (NO_GUARD),所以拿它當 probe 才只由交易閘門分勝負;其他鍵會先被前景
    class 守衛擋掉,量不到閘門。"""
    kb.registry[key]()


# ─── 1. 交易性:任一鍵失敗 → clinical hotkeys 歸零 ──────────────────────────
@pytest.mark.parametrize("fail_at", ["F1", "F3", "F8", "F11", "F12"])
def test_a_failure_anywhere_leaves_zero_clinical_hotkeys(monkeypatch, fail_at):
    """★F12 那格最危險★:F1~F11 全部掛好、只有中止鍵沒掛上 —— 會寫劑量的鍵
    按得動,救援鍵不存在。每個失敗位置都必須歸零,不是「大部分歸零」。"""
    kb = _FakeKeyboard(fail_at=fail_at)
    app, _ = _app(monkeypatch, kb)

    app.setup_hotkeys()

    assert kb.registry == {}, (
        f"{fail_at} 失敗後仍留下半套熱鍵:{sorted(kb.registry)}")


def test_the_registry_really_had_keys_before_the_failure(monkeypatch):
    """反例要只靠 rollback 分勝負:先證明失敗前★確實掛上了★前面的鍵,
    否則「registry 為空」可能只是因為什麼都沒掛(fake 提早 return)。"""
    kb = _FakeKeyboard(fail_at="F12")
    app, _ = _app(monkeypatch, kb)

    app.setup_hotkeys()

    added = [e[1] for e in kb.events if e[0] == "add"]
    assert added == _ALL_KEYS[:-1], "F12 之前的九鍵應該都已掛上才對"
    assert ("raise", "F12") in kb.events
    assert kb.registry == {}, "所以 rollback 必須把那九鍵全部拔掉"


# ─── 2. ★rollback 本身失敗★:留在 registry 裡的鍵一顆都不會執行 ──────────────
def test_leftover_hotkeys_are_inert_when_rollback_itself_fails(monkeypatch):
    """外審 r1 指出的洞:`unhook_all()` 拋例外 → registry 沒清 → 舊版的
    「F1~F8 仍有效、F12 不存在」原封不動。交易閘門不依賴 unhook 成功。"""
    kb = _FakeKeyboard(fail_at="F9", unhook_all_raises=True)
    app, actions = _app(monkeypatch, kb)

    app.setup_hotkeys()

    assert "F8" in kb.registry, "前提:rollback 失敗,半套確實還留在 registry"
    _press(kb, "F8")
    assert actions == [], "未提交的交易 → 留下來的鍵按了也不可以執行"


def test_the_probe_reaches_the_action_when_the_transaction_commits(monkeypatch):
    """正向對照:同一顆 F8 在★提交後★按下去要真的跑到動作 —— 證明上一條的
    「沒執行」是閘門擋的,不是 probe 根本打不到。"""
    kb = _FakeKeyboard(fail_at=None)
    app, actions = _app(monkeypatch, kb)

    app.setup_hotkeys()

    _press(kb, "F8")
    assert actions == ["F8: 快速輸入文字 (設定頁可改)"]


def test_the_abort_key_is_gated_too_when_it_is_the_last_one_in(monkeypatch):
    """失敗在 F12 且 rollback 失敗:F1~F11 全在 registry、F12 不在。這是最危險
    的一格 —— 會寫劑量的鍵存在、救援鍵不存在;閘門要把前面十一鍵全部封住。"""
    kb = _FakeKeyboard(fail_at="F12", unhook_all_raises=True)
    app, actions = _app(monkeypatch, kb)

    app.setup_hotkeys()

    assert "F12" not in kb.registry and "F8" in kb.registry
    _press(kb, "F8")
    assert actions == []


def test_a_previously_committed_set_goes_inert_when_a_new_transaction_fails(monkeypatch):
    """第二次註冊(例如解析度變更後重跑)開頭的 unhook_all 也可能失敗 →
    上一筆★已提交★的 callback 還在。交易一開始就要把 committed 清成 None,
    讓舊的一併失效,而不是「舊的照跑、新的失敗」。"""
    kb = _FakeKeyboard(fail_at=None)
    app, actions = _app(monkeypatch, kb)
    app.setup_hotkeys()                      # 第一筆:成功、提交
    old_f8 = kb.registry["F8"]
    _press(kb, "F8")
    assert actions == ["F8: 快速輸入文字 (設定頁可改)"]
    actions.clear()

    # 第二筆:開頭 unhook 失敗(舊 F8 仍掛著)。★在新交易註冊進行中★按下舊 F8 ——
    # 這是「交易開始就清 committed」唯一能分勝負的時刻:交易結束後 except 也會清,
    # 事後才按量不到這條規則。
    pressed_during = []
    orig_add = kb.add_hotkey

    def _add_and_press_old(key, callback, suppress=False):
        orig_add(key, callback, suppress)
        if key == "F3":                      # 新交易走到一半
            old_f8()
            pressed_during.append(list(actions))
    kb.add_hotkey = _add_and_press_old
    kb.unhook_all_raises = True
    kb.fail_at = "F9"
    app.setup_hotkeys()

    assert pressed_during == [[]], "新交易進行中,上一筆已提交的鍵不可以執行"
    old_f8()
    assert actions == [], "新交易失敗後同樣不可以"


def test_a_failure_after_the_commit_point_still_closes_the_gate(monkeypatch):
    """外審 r2:提交點在 F12 掛上之後,但後面的 label/UI 佇列仍可能拋例外。
    那時 committed 已等於本交易;若 rollback 的 unhook_all 又失敗,整組 callback
    會在「畫面說註冊失敗」的狀態下照樣執行。進 except 第一件事必須撤銷提交。

    (前面的測試只在 add_hotkey 階段注入失敗 —— 全在提交之前,量不到這一格。)"""
    kb = _FakeKeyboard(fail_at=None, unhook_all_raises=True)
    app, actions = _app(monkeypatch, kb)

    class _LabelThatBreaksOnce:
        """只在成功路徑那一次 config 拋例外;except 分支再呼叫時要正常,
        否則量到的是「except 分支自己也炸」,不是被測的閘門規則。"""
        calls = 0

        def config(self, **kw):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Tk label destroyed mid-update")

    app.hotkey_text_label = _LabelThatBreaksOnce()

    app.setup_hotkeys()

    assert ("add", "F12") in kb.events, "前提:失敗發生在 F12 掛上(提交)之後"
    assert "F8" in kb.registry, "前提:rollback 也失敗,整組還在 registry"
    _press(kb, "F8")
    assert actions == [], "提交後才失敗 + rollback 失敗 → 閘門仍必須關上"
    assert app._hotkey_register_retry_count == 1, "而且要照常走 retry"


def test_a_failed_rollback_is_recorded_as_an_error(monkeypatch, caplog):
    """rollback 失敗不可以假裝拔乾淨了 —— 要記 ERROR,排障時才看得到。"""
    kb = _FakeKeyboard(fail_at="F3", unhook_all_raises=True)
    app, _ = _app(monkeypatch, kb)

    with caplog.at_level(logging.ERROR):
        app.setup_hotkeys()

    assert any("rollback" in r.getMessage() and r.levelno >= logging.ERROR
               for r in caplog.records)


# ─── 3. 順序:先 rollback、再重掛 abbrev ─────────────────────────────────────
def test_rollback_happens_before_abbrev_is_reinstalled(monkeypatch):
    """反過來(先重掛 abbrev 再 unhook_all)會把剛掛好的 abbrev hook 又拔掉 ——
    那正是 AB-09 才剛修掉的「縮寫在所有程式一起失效」。"""
    kb = _FakeKeyboard(fail_at="F9")
    app, _ = _app(monkeypatch, kb)

    app.setup_hotkeys()

    ev = kb.events
    i_raise = ev.index(("raise", "F9"))
    tail = ev[i_raise + 1:]
    assert ("unhook_all",) in tail, "失敗之後必須 rollback"
    assert ("install_abbrev",) in tail, "失敗之後必須重掛 abbrev"
    assert tail.index(("unhook_all",)) < tail.index(("install_abbrev",)), \
        "順序錯了:先 unhook_all 才能重掛 abbrev,否則 abbrev 又被拔掉"


# ─── 4. retry 仍然有排 ───────────────────────────────────────────────────────
def test_retry_is_still_scheduled_after_rollback(monkeypatch):
    kb = _FakeKeyboard(fail_at="F5")
    app, _ = _app(monkeypatch, kb)

    app.setup_hotkeys()

    assert any(d == 30_000 for d, _ in app.root.scheduled), "30 秒後要重試"
    assert app._hotkey_register_retry_count == 1


def test_exhausted_retries_still_leave_zero_hotkeys(monkeypatch):
    """第 5 次仍失敗 → 放棄 retry。這是「半套熱鍵一直留到手動重啟」的路徑,
    rollback 在這一格也要成立。"""
    kb = _FakeKeyboard(fail_at="F12")
    app, _ = _app(monkeypatch, kb)
    app._hotkey_register_retry_count = 5          # 已經失敗五次

    app.setup_hotkeys()

    assert kb.registry == {}
    assert app._hotkey_register_retry_count == 6
    assert not any(d == 30_000 for d, _ in app.root.scheduled), "第六次不再排 retry"


# ─── 5. 成功路徑:fake 真的把整個函式跑完 ───────────────────────────────────
def test_success_registers_the_full_set_and_reinstalls_abbrev(monkeypatch):
    """證明這個 fake 驅動的是生產的呼叫形狀:沒有失敗時 10 鍵全掛、abbrev 重掛、
    retry 計數歸零。少一鍵都代表測試環境沒走到該走的地方。"""
    kb = _FakeKeyboard(fail_at=None)
    app, _ = _app(monkeypatch, kb)
    app._hotkey_register_retry_count = 3

    app.setup_hotkeys()

    assert sorted(kb.registry) == sorted(_ALL_KEYS)
    assert ("install_abbrev",) in kb.events
    assert app._hotkey_register_retry_count == 0
    assert not app.root.scheduled, "成功不排 retry"
    # 成功路徑只在開頭 unhook 一次(交易開始),不可以在結尾又拔掉自己
    unhooks = [e for e in kb.events if e == ("unhook_all",)]
    assert len(unhooks) == 1


def test_safe_unhook_reports_its_own_failure(monkeypatch):
    """rollback 的原語要能回報失敗;回 None/True 吞掉例外就是外審 r1 的洞。"""
    monkeypatch.setattr(main.hotkey_modules, "keyboard",
                        _FakeKeyboard(unhook_all_raises=True))
    assert main.safe_unhook_all_hotkeys() is False
    monkeypatch.setattr(main.hotkey_modules, "keyboard", _FakeKeyboard())
    assert main.safe_unhook_all_hotkeys() is True
