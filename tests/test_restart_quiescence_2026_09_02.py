# -*- coding: utf-8 -*-
"""[外審第五輪 R5-P2-01 / R5-P2-02] 自動重啟不可以腰斬進行中的 HIS 操作。

★第五輪的核心指控★:這個 repo 對「資料」已經很會處理 UNKNOWN,
但對「process 現在能不能死掉」沒有一個全域權威答案 —— 三條自動重啟各走各的:

  * 自動更新   → `_restart_when_hotkey_idle(force_after_max=True)`
                 15 分鐘後【明知 busy 仍重啟】;
  * reg52 升級 → 同一支但 `force_after_max=False`(正確的那條);
  * RAM 爆表   → ★完全繞過閘門★,health 監看緒直接
                 `restart_self(["--background"], hard_exit_code=1)`。

第三條最嚴重:它可以在 HIS 寫入中途結束 process(殘單、同意書半開,而且本地
state machine 還沒把結果分類完 = submitted-but-unverified),而且它的
`pre_exit_callback` ★只 flush 寄送帳本★ —— 漏了 HIS 稽核帳本
(`_flush_ledger_before_exit`)與抑制紀錄(`_retry_alert_pending_save`),
也就是「外部副作用可能已經發生,本地的補償證據卻消失了」。

★使用者定案(2026-09-02):自動機制一律不腰斬,改由人工決定。★
人工的出口 = 使用者確認 HIS 畫面後自己關掉重開(與熱鍵卡住時的既有指引一致),
所以「正在等」這件事必須看得見。
"""
import ast
import inspect
import os
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main as m  # noqa: E402


class _Root:
    def __init__(self):
        self.scheduled = []

    def after(self, _delay, cb=None):
        self.scheduled.append(cb)
        return "id"


class _Host:
    """只帶閘門需要的東西 —— 真的 AutomationApp 起不來(要 Tk/Win32)。"""

    def __init__(self, busy=True):
        self._subsystem_running = busy
        self._subsystem_lock = threading.RLock()
        self._restart_committing = False
        self.root = _Root()
        self.restarted = []
        self.notified = []

    def _restart_app(self):
        self.restarted.append(1)

    def _notify_restart_waiting(self, busy, gap):
        self.notified.append((busy, gap))


_Host._restart_when_hotkey_idle = m.AutomationApp.__dict__[
    "_restart_when_hotkey_idle"]


@pytest.fixture(autouse=True)
def _on_main_thread(monkeypatch):
    """閘門第一件事是「不在主緒就改排到主緒」——測試要走得進本體。"""
    monkeypatch.setattr("threading.current_thread", threading.main_thread)
    yield


def _busy_now():
    m._runner_1280.last_action_time = time.time()


def _idle_long_enough():
    m._runner_1280.last_action_time = (
        time.time() - m._UPDATE_RESTART_IDLE_GAP_SEC - 1)


# ══ R5-P2-01:RAM 那條要接回匯流點 ════════════════════════════════════════
class TestTheRamRestartGoesThroughTheGate:
    def test_the_health_monitor_no_longer_calls_restart_self_directly(self):
        """★核心★ health 的 `restart_callback` 不可以是「直接結束 process」。

        用 AST 找生產的那一次 `start_health_monitor(...)` 呼叫,看它實際傳什麼
        —— 不是看註解怎麼寫的。
        """
        src = inspect.getsource(m)
        call = None
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "start_health_monitor"):
                call = node
                break
        assert call is not None, "★找不到生產的 health monitor 啟動點★"
        kw = {k.arg: k.value for k in call.keywords}
        assert "restart_callback" in kw, kw.keys()
        cb = ast.unparse(kw["restart_callback"])
        assert "_restart_when_hotkey_idle" in cb, (
            "★RAM 爆表仍然繞過熱鍵閒置閘門 → 可能腰斬 HIS 寫入★:" + cb)
        assert "restart_self" not in cb, (
            "★仍然直接結束 process(少了完整收尾與接手保護)★:" + cb)

    def test_the_incomplete_pre_exit_flush_is_gone(self):
        """★收尾要完整,不是只 flush 一半★
        舊的 `pre_exit_callback=_flush_delivery_ledger_before_exit` 漏了 HIS
        稽核帳本與抑制紀錄;現在收尾統一由 `_restart_app()` 做,
        所以這個半套的 callback 不可以再留著(留著只會在每個 tick 白做一次)。
        """
        src = inspect.getsource(m)
        for node in ast.walk(ast.parse(src)):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "start_health_monitor"):
                names = {k.arg for k in node.keywords}
                assert "pre_exit_callback" not in names, (
                    "★半套的 pre_exit_callback 還在★")
                return
        pytest.fail("找不到 health monitor 啟動點")

    def test_the_confluence_point_flushes_everything(self):
        """★匯流點必須真的做完三件收尾★ —— 這是把 RAM 那條接回來的前提:
        接回一個自己也漏 flush 的地方等於沒修。

        ★用 AST 看【實際的呼叫】,不是看原始碼裡有沒有那串字★:
        第一版我用子字串比對,結果突變(把 `_flush_ledger_before_exit()` 整行
        刪掉)仍然綠 —— 因為同一個函式的註解裡就寫著那個名字
        (「而 _flush_ledger_before_exit 上限 2.0 秒…」)。
        又一次「測試的期望本身也是一種宣稱」。
        """
        import textwrap
        tree = ast.parse(textwrap.dedent(
            inspect.getsource(m.AutomationApp._restart_app)))
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                if isinstance(f, ast.Name):
                    called.add(f.id)
                elif isinstance(f, ast.Attribute):
                    called.add(f.attr)
        for fn in ("_flush_ledger_before_exit",
                   "_flush_delivery_ledger_before_exit",
                   "_retry_alert_pending_save"):
            assert fn in called, "★重啟匯流點沒有呼叫 " + fn + "★"


# ══ R5-P2-02:到頂也不強制 ════════════════════════════════════════════════
class TestNoAutomaticPathMayForce:
    def test_busy_at_the_cap_does_not_restart(self):
        """★核心★ 到延後上限、熱鍵仍忙 → 不重啟,繼續等。"""
        h = _Host(busy=True)
        _busy_now()
        h._restart_when_hotkey_idle(m._UPDATE_RESTART_MAX_DEFER_ATTEMPTS)
        assert not h.restarted, "★到頂仍強制重啟 → 腰斬 HIS 寫入★"
        assert h.root.scheduled, "★不重啟又不再重查 = 永遠不會重啟★"

    def test_the_wait_is_visible_to_the_user(self):
        """★人工出口要有人知道★:程式自己不會腰斬,那麼「一直沒重啟」這件事
        必須講給看得到 HIS 畫面的人聽,否則就是一道沒有出口的閘門。

        ★[外審 r11] 用生產的形狀:第一次到頂★。舊版先塞
        `_reg52_restart_wait_last_log = 0.0`「讓節流放行」—— 但 `time.monotonic()`
        是開機以來的秒數,0.0 的意思是「上次通知發生在開機那一刻」,於是這條測試
        變成看 CI runner 開機多久的擲骰子(開機未滿 600 秒就紅)。
        """
        h = _Host(busy=True)
        _busy_now()
        h._restart_when_hotkey_idle(m._UPDATE_RESTART_MAX_DEFER_ATTEMPTS)
        assert h.notified, "★到頂了卻沒有任何人被告知★"

    def test_the_first_notice_survives_a_freshly_booted_machine(self, monkeypatch):
        """★這條才量得到哨兵的語義★:機器剛開機(monotonic 還很小),第一次到頂的
        提醒★不可以★被節流吞掉 —— 「從未通知過」是 None,不是 0.0。"""
        monkeypatch.setattr(m, "_restart_monotonic", lambda: 5.0)   # 開機 5 秒
        h = _Host(busy=True)
        _busy_now()
        h._restart_when_hotkey_idle(m._UPDATE_RESTART_MAX_DEFER_ATTEMPTS)
        assert h.notified, "★剛開機時第一次的提醒被節流吞掉★"

    def test_the_notice_is_throttled_after_the_first_one(self, monkeypatch):
        """節流本身要還在:到頂之後每 5 秒回來一次,不可以每次都彈。
        ★時間用固定值、另外釘住常數★(推進量不從被測常數算出來,否則常數一改
        目標跟著移動、突變永遠不紅)。"""
        assert m._RESTART_WAIT_NOTICE_INTERVAL_SEC == 600.0
        now = [1000.0]
        monkeypatch.setattr(m, "_restart_monotonic", lambda: now[0])
        h = _Host(busy=True)
        _busy_now()
        h._restart_when_hotkey_idle(m._UPDATE_RESTART_MAX_DEFER_ATTEMPTS)
        assert len(h.notified) == 1

        now[0] += 599.0                       # 還在窗口內
        h._restart_gate_active = False
        _busy_now()
        h._restart_when_hotkey_idle(m._UPDATE_RESTART_MAX_DEFER_ATTEMPTS)
        assert len(h.notified) == 1, "節流失效 → 到頂後每 5 秒彈一次"

        now[0] += 2.0                         # 超過 600 秒
        h._restart_gate_active = False
        _busy_now()
        h._restart_when_hotkey_idle(m._UPDATE_RESTART_MAX_DEFER_ATTEMPTS)
        assert len(h.notified) == 2, "窗口過了卻不再提醒 → 等待又變成沒有出口"

    def test_it_restarts_as_soon_as_the_hotkeys_go_idle(self):
        """★不強制 ≠ 永不重啟★:一閒置(8 秒)就走匯流點重啟。"""
        h = _Host(busy=False)
        _idle_long_enough()
        h._restart_when_hotkey_idle(m._UPDATE_RESTART_MAX_DEFER_ATTEMPTS)
        assert h.restarted, "★閒下來了卻不重啟 → 更新/釋放記憶體永不生效★"

    def test_the_notice_says_what_the_human_can_do(self):
        """通知的內容要說得出人工出口是什麼 —— 只說「正在等」沒有用。"""
        doc = (m.AutomationApp._notify_restart_waiting.__doc__ or "")
        code = inspect.getsource(m.AutomationApp._notify_restart_waiting)
        assert "關閉" in code and "HIS" in code, code[-400:]
        assert "人工出口" in doc or "人工" in doc


class TestTheGateIsIdempotent:
    def test_repeated_requests_do_not_stack_recheck_chains(self):
        """★RAM 那條每 5 分鐘會再要求一次★(health 把「callback 回來了」讀成
        「這次沒重啟成功,下一輪再試」)。不擋的話每個 tick 都多疊一條
        after 重查鏈 —— log 與排程無限膨脹。"""
        h = _Host(busy=True)
        _busy_now()
        h._restart_when_hotkey_idle()
        first = len(h.root.scheduled)
        assert first == 1, h.root.scheduled
        for _ in range(5):
            h._restart_when_hotkey_idle()          # health 的後續 tick
        assert len(h.root.scheduled) == first, (
            "★重複請求疊出多條重查鏈★:" + str(len(h.root.scheduled)))

    def test_a_running_chain_still_advances(self):
        """★冪等不可以把自己那條鏈也擋掉★(attempts>0 是鏈自己的重查)。"""
        h = _Host(busy=True)
        _busy_now()
        h._restart_when_hotkey_idle()
        h._restart_when_hotkey_idle(1)
        assert len(h.root.scheduled) == 2, h.root.scheduled

    def test_the_flag_clears_when_the_restart_actually_happens(self):
        """重啟真的發生後旗標要放掉 —— 否則萬一 `_restart_app` 因為新行程
        早夭而返回(它有那個保護),之後就再也不會有第二次自動重啟。"""
        h = _Host(busy=False)
        _idle_long_enough()
        h._restart_when_hotkey_idle()
        assert h.restarted
        assert getattr(h, "_restart_gate_active", False) is False, (
            "★重啟返回後閘門旗標沒放掉 → 之後的自動重啟全部被冪等擋掉★")


# ══ 外審第 1 輪 R1:交棒窗的競態 ══════════════════════════════════════════
class TestTheHandoverWindowIsDrained:
    """★閘門讀到「不忙」之後,process 並不會立刻消失★

    `_restart_app()` → `restart_self()` 會先 spawn、等約 0.6 秒確認新行程活著,
    ★之後★才在 `_teardown_for_handover` 裡解除熱鍵。那段期間熱鍵仍然活著:
    醫師按下 F9 → `run_subsystem_in_thread` 在鎖內把 `_subsystem_running`
    設成 True、開始寫 HIS → 舊行程接著退出 = 被腰斬的那一筆。
    「讀 busy」與「接納新熱鍵」必須共用同一把鎖。
    """

    def test_the_commit_flag_is_set_before_restarting(self):
        h = _Host(busy=False)
        _idle_long_enough()
        seen = []

        def _spy_restart():
            # 模擬 restart_self 的交棒等待期間:此刻熱鍵仍然活著
            seen.append(h._restart_committing)
        h._restart_app = _spy_restart
        h._restart_when_hotkey_idle()
        assert seen == [True], (
            "★交棒期間沒有進入 draining → 這 0.6 秒可以開始新的 HIS 寫入★")

    def test_a_hotkey_arriving_during_the_recheck_cancels_the_restart(self):
        """★重查要在【鎖內】★:第一次讀到「不忙」之後、真正決定重啟之前,
        有人按了熱鍵 → 這一輪不可以重啟(而且要繼續排重查,不是靜靜停掉)。

        ★反例必須只靠這條規則分勝負★:第一版我在呼叫前就把
        `_subsystem_running` 設成 True —— 那會被函式【最前面】那次讀取擋下,
        根本走不到鎖內的重查,於是「把重查拿掉」這個突變照樣綠。
        現在用一把「拿到的瞬間才有人進來」的鎖來製造那個交錯。
        """
        h = _Host(busy=False)
        _idle_long_enough()

        class _RaceLock:
            def __enter__(_self):
                h._subsystem_running = True   # ★兩次讀取之間★有人開始跑
                return _self

            def __exit__(_self, *a):
                return False
        h._subsystem_lock = _RaceLock()
        h._restart_when_hotkey_idle()
        assert not h.restarted, "★鎖內重查沒做(或做了卻不採用)→ 腰斬 HIS 寫入★"
        assert h._restart_committing is False, "沒重啟就不該留著 draining 旗標"
        assert h.root.scheduled, "★不重啟又不再重查 = 永遠不會重啟★"

    def test_a_failed_handover_releases_the_hotkeys_again(self):
        """★接手失敗要收回旗標★:`restart_self` 返回代表新行程沒起來
        (它有那個保護)—— 旗標留著的話,熱鍵會被一道永遠不會結束的重啟擋死。"""
        h = _Host(busy=False)
        _idle_long_enough()
        h._restart_app = lambda: None    # 返回 = 接手失敗
        h._restart_when_hotkey_idle()
        assert h._restart_committing is False, (
            "★接手失敗卻沒收回 draining 旗標 → 熱鍵從此全部被擋★")
        assert h.root.scheduled, "接手失敗要再排一次重查"

    def test_the_flag_survives_an_exception_from_restart(self):
        """★用 finally 收回,不是靠正常返回★"""
        h = _Host(busy=False)
        _idle_long_enough()

        def _boom():
            raise RuntimeError("spawn 失敗")
        h._restart_app = _boom
        with pytest.raises(RuntimeError):
            h._restart_when_hotkey_idle()
        assert h._restart_committing is False


def test_the_dispatcher_refuses_new_hotkeys_while_committing():
    """★派送端要看得懂那個旗標,而且與 busy 在同一個臨界區★
    用 AST 確認:`_restart_committing` 的檢查就在 `_subsystem_lock` 的
    with 區塊裡(放在鎖外就還是 check-then-act)。"""
    import textwrap
    src = textwrap.dedent(
        inspect.getsource(m.AutomationApp.run_subsystem_in_thread))
    tree = ast.parse(src)
    inside = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.With):
            continue
        if "_subsystem_lock" not in ast.dump(node.items[0].context_expr):
            continue
        if "_restart_committing" in ast.dump(ast.Module(body=node.body,
                                                        type_ignores=[])):
            inside = True
    assert inside, (
        "★派送端沒有在 `_subsystem_lock` 內檢查 `_restart_committing`★ "
        "—— 讀 busy 與接納熱鍵仍然不是原子的")


def test_the_two_busy_reasons_say_different_things():
    """★處置不同的原因要分得開★:「前一個流程還沒完成」是等一下再按;
    「正在交棒重啟」是程式馬上要換版/釋放記憶體、幾秒後自己回來。

    ★判準要抓得到「兩邊講同一句話」★:第一版只斷言標題在不在,
    於是把內文改成與另一邊相同的突變照樣綠 —— 標題還在。
    改成比對兩個分支【實際傳出去的訊息字串】必須互不相同。
    """
    import textwrap
    tree = ast.parse(textwrap.dedent(
        inspect.getsource(m.AutomationApp.run_subsystem_in_thread)))
    bodies = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "_show_notice" and len(node.args) >= 2):
            body = ast.unparse(node.args[1])
            if "已略過" in body:            # 只看「這支熱鍵被擋掉」那兩種
                bodies.append(body)
    assert len(bodies) >= 2, "忙碌通知少於兩種 —— 原因沒有分開:" + str(bodies)
    # ★比對【內文】,不是整個呼叫★:標題不一樣、內文一樣的話,使用者讀到的
    #   建議仍然是錯的(叫他等一個根本沒在跑的自動化)。第一版比整個呼叫,
    #   於是「內文改成跟另一邊一樣」的突變因為標題還不同而矇混過關。
    assert len(set(bodies)) == len(bodies), (
        "★兩種忙碌的內文一模一樣★ 使用者分不出「等一下再按」與"
        "「程式正在重啟、幾秒後自己回來」:" + str(bodies))
