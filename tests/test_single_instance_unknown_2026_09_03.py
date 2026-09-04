# -*- coding: utf-8 -*-
"""[R3-P2-04] 單一實例的「查不出來」不可以繼續被當成「拿到了」。

`ensure_single_instance()` 在 mutex API 壞掉時★一律回 True★ —— 呼叫端因此
把「不知道有沒有別人拿著」當成「安全」。排班程式早就改用三態
(`acquire_single_instance`,見 `scheduler.py`),主程式/會診/打卡/守護程式
四支還在用舊介面。

★四支的處置不一樣,而且都要有憑據★
* 主程式:★互動式★ → 沿用排班程式的作風,明確告訴使用者無法確認、由人決定
  (不 fail-closed:診間開不了程式的代價比雙開更大);
* 打卡/會診:UNKNOWN 時★再走 pidfile 這條不依賴 mutex API 的路★
  —— 兩支本來就會自報 PID。查得到活著的第二份就是「已在執行中」;
  兩條都查不出來才繼續,並記一筆 ERROR;
* 守護:沒有自報 PID → 只能 fail-open + ERROR(沒有守護程式的代價更大)。

★判準要看行為,不是拼字★(外審 R1 P2):第一版用 AST 檢查「檔案裡有沒有
呼叫過三態」「UNKNOWN 的 body 裡有沒有 logging.error」—— 那可以被
「死函式裡留一個三態呼叫 + `if False:` 裡留一個 logging.error」整組騙過去,
而且它約束的是拼法(換個等價寫法反而被判失敗)。現在改成★直接呼叫各程式的
閘門函式★,把三態餵進去,看它真的做了什麼。
"""
import importlib
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cmuh_common.single_instance as si  # noqa: E402
from cmuh_common.single_instance import (  # noqa: E402
    INSTANCE_ACQUIRED, INSTANCE_ALREADY_RUNNING, INSTANCE_UNKNOWN,
    acquire_single_instance, ensure_single_instance, startup_instance_state,
)


_SRC = os.path.join(os.path.dirname(__file__), "..", "src")


class _FakeKernel:
    """CreateMutexW 拿不到 handle → 「查不出來」那一條路。"""

    def CreateMutexW(self, *_a):
        return 0

    def CloseHandle(self, *_a):
        return 1


def _force_unknown(monkeypatch):
    monkeypatch.setattr(si, "_kernel32", lambda: _FakeKernel())
    monkeypatch.setattr(si, "_configure_create_mutex", lambda _k: None)
    monkeypatch.setattr(si, "_set_last_error", lambda _v: None)
    # ★不可以用 5(ACCESS_DENIED)/183(ALREADY_EXISTS)★:那兩個是
    #   「已在執行中」那條路,量不到「查不出來」這一條。
    monkeypatch.setattr(si, "_last_error", lambda: 1450)
    monkeypatch.setattr(si, "_instance_mutex_handles", {})


# ══ 三態本身 ══════════════════════════════════════════════════════════════
class TestTheTriState:
    def test_a_broken_mutex_api_is_unknown_not_acquired(self, monkeypatch):
        _force_unknown(monkeypatch)
        assert acquire_single_instance("X") == INSTANCE_UNKNOWN

    def test_the_old_interface_still_says_true(self, monkeypatch):
        """★相容包裝不變★:舊呼叫端的行為不可以被這一批默默改掉
        (fail-open 是刻意的,改動它要有人明確決定)。"""
        _force_unknown(monkeypatch)
        assert ensure_single_instance("X") is True

    def test_an_exception_is_unknown_too(self, monkeypatch):
        monkeypatch.setattr(si, "_kernel32",
                            lambda: (_ for _ in ()).throw(OSError("boom")))
        monkeypatch.setattr(si, "_instance_mutex_handles", {})
        assert acquire_single_instance("X") == INSTANCE_UNKNOWN

    def test_the_three_states_are_distinct(self):
        assert len({INSTANCE_ACQUIRED, INSTANCE_ALREADY_RUNNING,
                    INSTANCE_UNKNOWN}) == 3

    def test_it_does_not_hijack_the_root_logger(self, monkeypatch):
        """★不可以用 module-level `logging.warning(...)`★(外審 R1 P1-2):
        root 還沒有 handler 時它會隱式 `basicConfig()` 裝一個 stderr handler,
        之後各程式的 `setup_logging()` 就再也裝不上檔案 handler
        —— log 檔一行都不會寫,watchdog 再把健康的行程判成 log stale。"""
        root = logging.getLogger()
        saved = list(root.handlers)
        for h in saved:
            root.removeHandler(h)
        try:
            _force_unknown(monkeypatch)
            assert acquire_single_instance("X") == INSTANCE_UNKNOWN
            assert root.handlers == [], \
                f"★單例判定偷裝了 handler★:{root.handlers}"
        finally:
            for h in root.handlers[:]:
                root.removeHandler(h)
            for h in saved:
                root.addHandler(h)


# ══ pidfile:不依賴 mutex API 的第二條路 ═══════════════════════════════════
class TestThePidfileFallback:
    def test_a_live_second_instance_is_found_without_the_mutex(
            self, monkeypatch):
        """★核心★:mutex 查不出來,但 pidfile 查得到活著的第二份
        → 就是「已在執行中」(那條路完全不經過 CreateMutexW)。"""
        _force_unknown(monkeypatch)
        monkeypatch.setitem(
            sys.modules, "cmuh_common.pidfile",
            type(sys)("cmuh_common.pidfile"))
        sys.modules["cmuh_common.pidfile"].read_verified_pid = (
            lambda name: 4321)
        assert startup_instance_state("X", "autoclock") == \
            INSTANCE_ALREADY_RUNNING

    def test_no_verified_pid_stays_unknown(self, monkeypatch):
        """★查不到不等於沒有★:pidfile 可能是舊格式/psutil 不可用/對方還沒
        寫到那一步 —— 仍然是「不知道」,不可以升級成「拿到了」。"""
        _force_unknown(monkeypatch)
        monkeypatch.setitem(
            sys.modules, "cmuh_common.pidfile",
            type(sys)("cmuh_common.pidfile"))
        sys.modules["cmuh_common.pidfile"].read_verified_pid = (
            lambda name: None)
        assert startup_instance_state("X", "autoclock") == INSTANCE_UNKNOWN

    def test_a_broken_pidfile_path_stays_unknown(self, monkeypatch):
        _force_unknown(monkeypatch)
        monkeypatch.setitem(
            sys.modules, "cmuh_common.pidfile",
            type(sys)("cmuh_common.pidfile"))
        sys.modules["cmuh_common.pidfile"].read_verified_pid = (
            lambda name: (_ for _ in ()).throw(OSError("boom")))
        assert startup_instance_state("X", "autoclock") == INSTANCE_UNKNOWN

    def test_no_app_id_means_no_second_path(self, monkeypatch):
        """守護程式沒有自報 PID → 不去查(查了也只會查到別支程式)。"""
        _force_unknown(monkeypatch)
        called = []
        monkeypatch.setitem(
            sys.modules, "cmuh_common.pidfile",
            type(sys)("cmuh_common.pidfile"))
        sys.modules["cmuh_common.pidfile"].read_verified_pid = (
            lambda name: called.append(name))
        assert startup_instance_state("X") == INSTANCE_UNKNOWN
        assert called == []

    def test_a_healthy_mutex_never_touches_the_pidfile(self, monkeypatch):
        """★第二條路只在「查不出來」時走★:mutex 說得出答案時再去讀 pidfile
        只會多一次 I/O,而且可能拿到別的結論。"""
        called = []
        monkeypatch.setattr(si, "acquire_single_instance",
                            lambda *_a, **_k: INSTANCE_ACQUIRED)
        monkeypatch.setitem(
            sys.modules, "cmuh_common.pidfile",
            type(sys)("cmuh_common.pidfile"))
        sys.modules["cmuh_common.pidfile"].read_verified_pid = (
            lambda name: called.append(name))
        assert si.startup_instance_state("X", "autoclock") == \
            INSTANCE_ACQUIRED
        assert called == []


# ══ 各程式的閘門:行為 ═════════════════════════════════════════════════════
def _mod(name):
    return importlib.import_module(name)


class TestTheUnattendedGates:
    """打卡/會診/守護:照常跑,但要留下看得出來的紀錄。"""

    @pytest.mark.parametrize("mod_name", ("autoclock", "consult_query"))
    def test_it_asks_the_pidfile_with_its_own_app_id(self, mod_name,
                                                     monkeypatch):
        """★app_id 要是自己那一支★:傳錯的話會去讀別支程式的 PID 檔,
        把別人還活著當成自己還活著(或反過來)。"""
        m = _mod(mod_name)
        seen = {}
        monkeypatch.setattr(
            m, "startup_instance_state",
            lambda mutex, app_id="", retry_sec=1.5: seen.update(app_id=app_id)
            or INSTANCE_ACQUIRED)
        m.single_instance_gate()
        assert seen["app_id"] == mod_name

    @pytest.mark.parametrize("mod_name", ("autoclock", "consult_query"))
    def test_unknown_is_reported_as_an_error(self, mod_name, monkeypatch,
                                             caplog):
        m = _mod(mod_name)
        with caplog.at_level(logging.ERROR):
            m.report_single_instance_state(INSTANCE_UNKNOWN)
        assert any(r.levelno == logging.ERROR for r in caplog.records), \
            f"{mod_name} 的 UNKNOWN 沒有留下 ERROR 紀錄"

    @pytest.mark.parametrize("mod_name", ("autoclock", "consult_query"))
    def test_the_other_states_say_nothing(self, mod_name, monkeypatch,
                                          caplog):
        """★對照組★:正常啟動不可以每次都噴一行 ERROR(那會變成噪音)。"""
        m = _mod(mod_name)
        with caplog.at_level(logging.ERROR):
            m.report_single_instance_state(INSTANCE_ACQUIRED)
            m.report_single_instance_state(INSTANCE_ALREADY_RUNNING)
        assert not caplog.records, caplog.records

    def test_the_watchdog_gate_logs_and_continues(self, monkeypatch, caplog):
        m = _mod("watchdog_runner")
        monkeypatch.setattr(m, "startup_instance_state",
                            lambda *_a, **_k: INSTANCE_UNKNOWN)
        with caplog.at_level(logging.ERROR):
            state = m.single_instance_gate()
        assert state == INSTANCE_UNKNOWN, "★守護程式不可以因此不啟動★"
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_the_watchdog_gate_is_quiet_when_it_knows(self, monkeypatch,
                                                      caplog):
        m = _mod("watchdog_runner")
        monkeypatch.setattr(m, "startup_instance_state",
                            lambda *_a, **_k: INSTANCE_ACQUIRED)
        with caplog.at_level(logging.ERROR):
            assert m.single_instance_gate() == INSTANCE_ACQUIRED
        assert not caplog.records


class TestTheReportComesAfterLoggingIsSetUp:
    """★順序契約★:單例判定發生在 logging 設定【之前】,那時候記的東西進不了
    log 檔(只會掉到 stderr,而 .pyw 根本沒有 console)。所以回報那一步一定
    要排在該支程式的 logging 設定之後。"""

    @pytest.mark.parametrize("mod_name,setup_fn", (
        ("autoclock", "_setup_clock_logging"),
        ("consult_query", "_setup_logging"),
    ))
    def test_the_order(self, mod_name, setup_fn):
        import ast
        import inspect
        import textwrap
        m = _mod(mod_name)
        src = textwrap.dedent(inspect.getsource(m.main))
        tree = ast.parse(src)
        order = [n.func.id for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                 and n.func.id in (setup_fn, "report_single_instance_state")]
        assert order[:2] == [setup_fn, "report_single_instance_state"], order


class TestTheInteractiveGate:
    """主程式:不確定時問使用者,取消就離開。"""

    def _main_mod(self):
        return _mod("main")

    def test_already_running_exits(self, monkeypatch):
        m = self._main_mod()
        monkeypatch.setattr(m, "acquire_single_instance",
                            lambda *_a, **_k: INSTANCE_ALREADY_RUNNING)
        boxes = _fake_messagebox(monkeypatch, m, answer=1)
        with pytest.raises(SystemExit):
            m.single_instance_gate()
        assert boxes and "已在執行中" in boxes[0]

    def test_unknown_asks_and_cancel_exits(self, monkeypatch):
        m = self._main_mod()
        monkeypatch.setattr(m, "acquire_single_instance",
                            lambda *_a, **_k: INSTANCE_UNKNOWN)
        boxes = _fake_messagebox(monkeypatch, m, answer=2)   # 2 = IDCANCEL
        with pytest.raises(SystemExit):
            m.single_instance_gate()
        assert boxes and "無法確認" in boxes[0]

    def test_unknown_continues_when_the_user_says_ok(self, monkeypatch):
        m = self._main_mod()
        monkeypatch.setattr(m, "acquire_single_instance",
                            lambda *_a, **_k: INSTANCE_UNKNOWN)
        _fake_messagebox(monkeypatch, m, answer=1)           # 1 = IDOK
        assert m.single_instance_gate() == INSTANCE_UNKNOWN

    def test_acquired_never_shows_a_box(self, monkeypatch):
        m = self._main_mod()
        monkeypatch.setattr(m, "acquire_single_instance",
                            lambda *_a, **_k: INSTANCE_ACQUIRED)
        boxes = _fake_messagebox(monkeypatch, m, answer=1)
        assert m.single_instance_gate() == INSTANCE_ACQUIRED
        assert boxes == []


def _fake_messagebox(monkeypatch, mod, *, answer: int) -> list:
    """攔下 MessageBoxW → 回傳「顯示過的訊息」清單。"""
    shown: list = []

    class _User32:
        def MessageBoxW(self, _h, text, _title, _flags):
            shown.append(text)
            return answer

    class _Windll:
        user32 = _User32()

        def __getattr__(self, _n):          # shell32 等其它成員不在此測範圍
            raise AttributeError(_n)

    monkeypatch.setattr(mod.ctypes, "windll", _Windll(), raising=False)
    return shown


# ══ 入口真的用了那個閘門(外審 R2 P2)════════════════════════════════════════
class TestTheEntryPointsActuallyUseTheGate:
    """★閘門本身測綠了不代表它被接上去★:把 `_inst_state = single_instance_gate()`
    改成 `= INSTANCE_ACQUIRED`、或把主程式入口那一行刪掉,上面所有閘門測試
    照樣通過(外審 R2 P2)。這裡驗「入口的去留真的由閘門決定」。"""

    def test_autoclock_main_stops_when_another_one_is_running(self,
                                                              monkeypatch):
        """★行為★:閘門說「已在執行中」→ `main()` 要在做任何事之前就回來。"""
        m = _mod("autoclock")
        monkeypatch.setattr(m, "single_instance_gate",
                            lambda: INSTANCE_ALREADY_RUNNING)
        monkeypatch.setattr(m, "set_dpi_awareness",
                            lambda: pytest.fail("★被擋下來卻繼續跑了★"))
        m.main()                                  # 不可以拋、不可以往下跑

    def test_autoclock_main_goes_on_when_it_is_the_only_one(self,
                                                            monkeypatch):
        """★對照組★:拿到了就要往下跑(否則上面那條測試靠「永遠不跑」也會綠)。"""
        m = _mod("autoclock")
        marker = RuntimeError("went on")
        monkeypatch.setattr(m, "single_instance_gate",
                            lambda: INSTANCE_ACQUIRED)

        def _boom():
            raise marker
        monkeypatch.setattr(m, "set_dpi_awareness", _boom)
        with pytest.raises(RuntimeError) as e:
            m.main()
        assert e.value is marker

    def test_consult_main_exits_when_another_one_is_running(self,
                                                            monkeypatch):
        m = _mod("consult_query")
        monkeypatch.setattr(m, "is_admin", lambda: True)
        monkeypatch.setattr(sys, "argv", ["consult_query.py"])
        monkeypatch.setattr(m, "single_instance_gate",
                            lambda: INSTANCE_ALREADY_RUNNING)
        monkeypatch.setattr(m, "_setup_logging",
                            lambda: pytest.fail("★被擋下來卻繼續跑了★"))
        with pytest.raises(SystemExit):
            m.main()

    def test_watchdog_main_exits_when_another_one_is_running(self,
                                                             monkeypatch):
        m = _mod("watchdog_runner")
        monkeypatch.setattr(sys, "argv", ["watchdog_runner.py"])
        monkeypatch.setattr(m, "_setup_logging", lambda: None)
        monkeypatch.setattr(m, "_recover_incomplete_update", lambda: None)
        monkeypatch.setattr(m, "single_instance_gate",
                            lambda: INSTANCE_ALREADY_RUNNING)
        assert m.main() == 0

    def test_the_main_program_entry_calls_the_gate(self):
        """主程式的閘門在 `if __name__ == "__main__":` 裡,呼叫不到 ——
        改用結構判準,但★要精確★:必須是那個區塊裡【直接】的一句敘述
        (不是藏在 `if False:` 之類的不可達分支),而且要排在建立 tk 主視窗
        之前(熱鍵/Chrome 都是那之後才起來的)。"""
        import ast
        with open(os.path.join(_SRC, "main.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        block = None
        for n in tree.body:
            if (isinstance(n, ast.If)
                    and any(isinstance(x, ast.Name) and x.id == "__name__"
                            for x in ast.walk(n.test))):
                block = n.body
        assert block is not None, "找不到 __main__ 區塊"
        gate_at = tk_at = None
        for i, stmt in enumerate(block):
            # ★只認「敘述本身就是那一句呼叫」★(外審 R3 P2):往下鑽的話,
            #   `if False: single_instance_gate()` 也會被算成「有呼叫」——
            #   實際啟動完全跳過單例檢查,判準卻是綠的。
            if (gate_at is None
                    and isinstance(stmt, ast.Expr)
                    and isinstance(stmt.value, ast.Call)
                    and isinstance(stmt.value.func, ast.Name)
                    and stmt.value.func.id == "single_instance_gate"):
                gate_at = i
            if tk_at is None:           # tk.Tk() 只當位置參照,不必嚴格
                for c in ast.walk(stmt):
                    if (isinstance(c, ast.Call)
                            and isinstance(c.func, ast.Attribute)
                            and c.func.attr == "Tk"):
                        tk_at = i
                        break
        assert gate_at is not None, \
            "★主程式入口沒有【直接】呼叫單例閘門★(藏在 if 裡不算)"
        assert tk_at is not None and gate_at < tk_at, \
            "★單例閘門排在建立主視窗之後★"


class TestThePunchGoesThroughTheClaim:
    """★[R3-P2-04 R2 P1] 打卡的 check-then-act 要被跨行程宣告序列化★。

    宣告要包住【查刷卡表 + 送出】整段,不是只有點擊那一下 —— 兩份同時跑時,
    兩邊都會讀到「尚無紀錄」而各打一次。
    """

    def _args(self):
        from datetime import time as dt_time
        return (None, None, {"username": "u", "password": "p"}, True,
                dt_time(8, 0), dt_time(9, 0))

    def test_it_does_not_punch_when_someone_else_holds_the_claim(
            self, monkeypatch):
        from contextlib import contextmanager
        m = _mod("autoclock")
        ran = []
        monkeypatch.setattr(m, "_perform_clock_action_locked",
                            lambda *_a, **_k: ran.append(1))

        @contextmanager
        def _busy(_key, **_kw):
            yield False
        monkeypatch.setattr(m, "exclusive_claim", _busy)
        m.perform_clock_action(*self._args())
        assert ran == [], "★別人正在打卡,自己還是打下去了★"

    def test_it_punches_when_it_holds_the_claim(self, monkeypatch):
        """★對照組★:拿到宣告就要照打(否則上面那條靠「永遠不打」也會綠)。"""
        from contextlib import contextmanager
        m = _mod("autoclock")
        ran = []
        monkeypatch.setattr(m, "_perform_clock_action_locked",
                            lambda *_a, **_k: ran.append(1))

        @contextmanager
        def _free(_key, **_kw):
            yield True
        monkeypatch.setattr(m, "exclusive_claim", _free)
        m.perform_clock_action(*self._args())
        assert ran == [1]

    def test_the_key_separates_accounts_and_windows(self, monkeypatch):
        """★不同帳號/不同打卡窗要各自平行★:鍵若少了它們,兩個人上班時間
        一樣就會互相擋掉 —— 那是把「防重複」做成「少打卡」。"""
        from contextlib import contextmanager
        from datetime import time as dt_time
        m = _mod("autoclock")
        keys = []

        @contextmanager
        def _spy(key, **_kw):
            keys.append(key)
            yield True
        monkeypatch.setattr(m, "exclusive_claim", _spy)
        monkeypatch.setattr(m, "_perform_clock_action_locked",
                            lambda *_a, **_k: None)
        m.perform_clock_action(None, None, {"username": "A"}, True,
                               dt_time(8, 0), dt_time(9, 0))
        m.perform_clock_action(None, None, {"username": "B"}, True,
                               dt_time(8, 0), dt_time(9, 0))
        m.perform_clock_action(None, None, {"username": "A"}, False,
                               dt_time(8, 0), dt_time(9, 0))
        m.perform_clock_action(None, None, {"username": "A"}, True,
                               dt_time(17, 0), dt_time(18, 0))
        assert len(set(keys)) == 4, f"★鍵分不開★:{keys}"

    def test_dry_run_never_claims(self, monkeypatch):
        """測試模式不會真的送出 → 不該去搶宣告(否則測試流程會擋到真打卡)。"""
        from contextlib import contextmanager
        m = _mod("autoclock")
        used = []

        @contextmanager
        def _spy(key, **_kw):
            used.append(key)
            yield True
        monkeypatch.setattr(m, "exclusive_claim", _spy)
        monkeypatch.setattr(m, "_perform_clock_action_locked",
                            lambda *_a, **_k: None)
        m.perform_clock_action(*self._args(), dry_run=True)
        assert used == []
