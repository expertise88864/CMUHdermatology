# -*- coding: utf-8 -*-
"""[穩定性 批次SF] 會診查詢:三個「卡住的持有者」還沒被量。

SF-1 退出路徑:`exit_action` 在建立那條唯一會 `os._exit` 的 `_shutdown` 緒
     【之前】,先做 `_session_close(...)` —— 而它會走到
     `_dismiss_blocking_modals` → `enum_children` → raw `GetWindowText()`,
     systemftp 凍結時永久不返回。托盤回呼卡死 → 托盤沒收掉、`_shutdown`
     根本沒被建立 → ★行程永遠不會結束★;而 `running` 已清掉 → log 停更
     → watchdog 起的新實例撞上舊實例仍持有的 mutex → 依設計靜默退出
     → 會診查詢停擺且無任何 log。

SF-2 `_outlook_available` 沒有任何 single-flight:每輪 poll 都開一條
     `OutlookAvailCheck` + 一個 COM apartment,Outlook 卡住時逐條堆積。
     ★而且它是 `send_via_outlook` 的門★ —— 批次SE 的 single-flight 在
     Outlook 真的卡住時根本走不到。

SF-3 `_flow_lock` 是 non-blocking acquire,所以不堆積等待緒 —— 它的失效
     更安靜:持鎖者永不釋放,之後每一輪只印一行 INFO 然後跳過,
     heartbeat/tick 全部正常,會診查詢再也不會執行。
"""
import ast
import importlib
import io
import os
import re
import sys
import threading
import time

import pytest

NL = chr(10)
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

cq = importlib.import_module("consult_query")
w32 = importlib.import_module("cmuh_common.win32_safe")

SRC = io.open(os.path.join(REPO_ROOT, "src", "consult_query.py"),
              encoding="utf-8").read()


def _fn_src(name):
    tree = ast.parse(SRC)
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(SRC, n) or ""
    raise AssertionError(f"consult_query 找不到 {name}")


def _fn_body_code(name):
    """函式【可執行的部分】(去掉 docstring,`ast.unparse` 也不會保留註解)。

    ★負向斷言不可以只剝註解★ docstring 不是註解 —— 而「為什麼不可以碰
    logging」這句解釋本身就含有 `logging` 這個字面,足以讓斷言通過。
    (剝註解那一招今天已經救過五次,這是它的第六個變形。)
    """
    tree = ast.parse(SRC)
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            body = n.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            return NL.join(ast.unparse(st) for st in body)
    raise AssertionError(f"consult_query 找不到 {name}")


def _strip_comments(text):
    """★負向斷言之前一定要剝註解★

    「為什麼不可以這樣寫」的說明裡就含有那個字面 —— 不剝的話,那句解釋
    自己會讓斷言通過。這一輪已經在別的批次踩過五次。
    """
    return NL.join(re.sub(r"#.*$", "", ln) for ln in text.splitlines())


@pytest.fixture(autouse=True)
def _no_leaked_exit_threads():
    """★測試不可以留下會呼叫真實 `os._exit` 的緒★

    這不是潔癖:`_arm_exit_deadline`/`_force_exit` 真的會建立一條「睡飽就
    `os._exit`」的 daemon 緒。monkeypatch 在測試結束時就把 `_exit_now` 還原了,
    而那條緒是【之後】才醒來的 —— 它會在整個 pytest 跑到一半把行程殺掉,
    ★而且是以 exit code 0 殺掉,測試結果變成假綠★(我第一版就是這樣:
    量到 rc=0 卻其實有三個測試是紅的)。
    """
    yield
    # 先等它們自然收尾(正常情況是毫秒級);還活著的才是真的洩漏
    # —— 那種會睡滿寬限期再 `os._exit`,必須當場擋下來。
    for t in list(threading.enumerate()):
        if t.name in ("ConsultForceExit", "ConsultExitDeadline"):
            t.join(3.0)
    leaked = [t for t in threading.enumerate()
              if t.name in ("ConsultForceExit", "ConsultExitDeadline")
              and t.is_alive()]
    assert not leaked, (
        f"★留下了會呼叫真實 os._exit 的緒:{[t.name for t in leaked]}★ "
        "它會在稍後把 pytest 殺掉並產生假綠")


# ══ SF-1 退出必須有硬性期限 ═════════════════════════════════════════════
class TestExitHasAHardDeadline:
    def test_the_deadline_is_armed_before_any_blocking_cleanup(self):
        """★順序就是這個修正的全部內容★

        期限掛在 `_session_close` 之後的話,卡住的正是那一行 —— 期限永遠
        沒機會被掛上去。
        """
        body = _strip_comments(_fn_src("exit_action"))
        arm = body.index("_arm_exit_deadline()")
        for blocking in ("_session_close(", "_abort_bde_shutdown_on_exit(",
                         "tray_icon_object", "ConsultShutdown"):
            assert blocking in body, blocking
            assert arm < body.index(blocking), (
                f"硬性期限必須在 {blocking} 之前掛上")

    def test_a_wedged_cleanup_still_kills_the_process(self, monkeypatch):
        """收尾永遠不返回時,期限仍然把行程結束掉。

        ★patch 的是 `_force_exit` 而不是 `_hard_exit`★ —— 讓真的
        `_force_exit` 跑起來會建立一條「睡飽就 `os._exit`」的緒,
        它稍後會把 pytest 殺掉(見 `_no_leaked_exit_threads`)。
        """
        killed = []
        monkeypatch.setattr(cq, "_force_exit",
                            lambda reason, code=1: killed.append((reason, code)))
        cq._arm_exit_deadline(0.05)
        deadline = time.time() + 5.0
        while not killed and time.time() < deadline:
            time.sleep(0.02)
        assert killed, "收尾卡死時期限沒有把行程結束掉"
        assert killed[0][1] == 0, "使用者要求的退出應以 0 結束(與正常路徑一致)"

    def test_the_deadline_survives_running_being_cleared(self, monkeypatch):
        """★不可以用 `_sleep_while_running`★

        `exit_action` 緊接著就 `running.clear()`,那個 helper 會立刻返回 ——
        期限等於沒掛。這裡直接把 `running` 清掉再驗它照樣會開火。
        """
        assert "_sleep_while_running" not in _strip_comments(
            _fn_src("_arm_exit_deadline"))
        killed = []
        was_set = cq.running.is_set()
        monkeypatch.setattr(cq, "_force_exit",
                            lambda reason, code=1: killed.append(reason))
        cq.running.clear()
        try:
            cq._arm_exit_deadline(0.05)
            deadline = time.time() + 5.0
            while not killed and time.time() < deadline:
                time.sleep(0.02)
        finally:
            if was_set:
                cq.running.set()
        assert killed, "running 被清掉之後期限就不開火了"

    def test_a_failed_guard_thread_is_reported_not_swallowed(self):
        """★[外審 SF 第 1 輪 P1-2]★ 期限掛不上時要【講出來】。

        我第一版只是 log 一行然後回 None,呼叫端照樣走進 `_session_close`
        —— 那條路一卡住就沒有任何東西會結束這個行程,而承諾看起來還在。
        """
        orig = threading.Thread

        def _boom(*a, **k):
            raise RuntimeError("can't start new thread")

        threading.Thread = _boom
        try:
            assert cq._arm_exit_deadline(0.05) is False
        finally:
            threading.Thread = orig

    def test_exit_skips_the_unbounded_cleanup_when_it_cannot_arm(
            self, monkeypatch):
        """★沒有保險絲就不可以走進無界的收尾★

        `_session_close` 是退場路上唯一沒有上限的一段(raw GetWindowText)。
        寧可留下一個沒收乾淨的 HIS session,也不可以留下一個殭屍行程 ——
        後者會讓 watchdog 的每一次救援都被靜默擋退。
        """
        monkeypatch.setattr(cq, "_arm_exit_deadline", lambda *a, **k: False)
        monkeypatch.setattr(cq, "_exit_started", False)
        monkeypatch.setattr(cq, "tray_icon_object", None)
        monkeypatch.setattr(cq, "_bde_shutdown_pending", True)
        closed, aborted, logged, ran, died = [], [], [], [], []
        monkeypatch.setattr(cq, "_session_close",
                            lambda why: closed.append(why))
        monkeypatch.setattr(cq, "_abort_bde_shutdown_on_exit",
                            lambda: aborted.append(1))
        monkeypatch.setattr(cq, "_exit_now", lambda code=0: died.append(code))
        for lvl in ("info", "error", "warning", "critical"):
            monkeypatch.setattr(cq.logging, lvl,
                                lambda *a, **k: logged.append(a))
        monkeypatch.setattr(cq.subprocess, "run",
                            lambda *a, **k: ran.append(a))

        class _T:      # ★真的 Thread 會跑到 `_shutdown` → 真的殺掉 pytest★
            def __init__(self, *, target, name=None, daemon=None):
                self.target = target

            def start(self):
                pass

        monkeypatch.setattr(cq.threading, "Thread", _T)
        cq.running.set()
        try:
            cq.exit_action()
        finally:
            cq.running.set()
        assert died == [0], "沒有保險絲時沒有立刻結束行程"
        assert not closed, "★沒有保險絲卻仍走進可能永久阻塞的 session 收尾★"
        # ★[外審 SF 第 2 輪 P1]★ logging 也會拿 handler lock ——
        #   把卡點從 Win32 換到 logging,原本的殭屍行程照樣發生。
        assert not logged, "★沒有保險絲卻仍寫 log(handler lock 可能卡住)★"
        assert not aborted, (
            "★那個 helper 要拿 `_bde_watch_lock` 並寫 log,不是無界安全的★")
        assert ran and ran[0][0] == ["shutdown", "/a"], (
            "已排定的重開機沒有被取消(使用者按退出、機器卻照樣重開)")

    def test_exit_dies_even_if_the_shutdown_thread_cannot_start(
            self, monkeypatch):
        """`os._exit` 只寫在 `_shutdown` 裡 —— 那條緒開不出來就永遠不會死。"""
        monkeypatch.setattr(cq, "_exit_started", False)
        monkeypatch.setattr(cq, "tray_icon_object", None)
        monkeypatch.setattr(cq, "_arm_exit_deadline", lambda *a, **k: True)
        monkeypatch.setattr(cq, "_session_close", lambda why: None)
        monkeypatch.setattr(cq, "_abort_bde_shutdown_on_exit", lambda: None)
        died = []
        monkeypatch.setattr(cq, "_exit_now", lambda code=0: died.append(code))

        def _boom(*a, **k):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(cq.threading, "Thread", _boom)
        cq.running.set()
        try:
            cq.exit_action()
        finally:
            cq.running.set()
        assert died == [0], "收尾緒開不出來時行程沒有被結束"


# ══ 保證退場的那條路自己不可以卡住(外審 SF 第 1 輪 P1-1)═══════════════
class TestTheGuaranteedExitPathCannotBlock:
    """★我第一版的「保證退場」自己就會卡住★

    每個升級點都先 `logging.critical(...)`(要拿 handler lock,而那把鎖很可能
    正被卡死的那條緒持有),而 `_hard_exit` 雖然把 handler flush 改成非阻塞,
    卻接著 `_flush_delivery_ledger()` → `DeliveryLedger.flush()` 會【無界】等
    一把 RLock 並寫檔。保證會死的那條路上,一件會等別人的事都不能有。
    """

    def test_the_fuse_is_armed_before_anything_that_can_block(self):
        body = _strip_comments(_fn_src("_force_exit"))
        fuse = body.index("_exit_now_after")
        assert "logging" in body and "_hard_exit" in body
        assert fuse < body.index("logging"), "保險絲要在寫 log 之前"
        assert fuse < body.index("_hard_exit"), "保險絲要在帳本 flush 之前"

    def test_it_dies_immediately_when_the_fuse_cannot_be_armed(self,
                                                               monkeypatch):
        """沒有保險絲就不可以再去試那些會等別人的事。"""
        died, logged, flushed = [], [], []
        monkeypatch.setattr(cq, "_exit_now", lambda code=0: died.append(code))
        monkeypatch.setattr(cq.logging, "critical",
                            lambda *a, **k: logged.append(a))
        monkeypatch.setattr(cq, "_hard_exit",
                            lambda *a, **k: flushed.append(a))

        def _boom(*a, **k):
            raise RuntimeError("can't start new thread")

        monkeypatch.setattr(cq.threading, "Thread", _boom)
        cq._force_exit("測試", code=3)
        assert died == [3]
        assert not logged, "★保險絲掛不上還去寫 log★(handler lock 可能卡住)"
        assert not flushed, "★保險絲掛不上還去 flush 帳本★(RLock 無界)"

    def test_a_blocked_logger_still_ends_the_process(self, monkeypatch):
        """logging 永遠不返回時,保險絲照樣把行程結束掉。"""
        died = []
        unblock = threading.Event()
        monkeypatch.setattr(cq, "_exit_now", lambda code=0: died.append(code))
        monkeypatch.setattr(cq, "_hard_exit", lambda *a, **k: None)
        monkeypatch.setattr(cq, "_FORCE_EXIT_GRACE_SEC", 0.05)
        monkeypatch.setattr(cq.logging, "critical",
                            lambda *a, **k: unblock.wait(10))
        t = threading.Thread(target=lambda: cq._force_exit("x", code=7),
                             daemon=True)
        t.start()
        deadline = time.time() + 5.0
        while not died and time.time() < deadline:
            time.sleep(0.02)
        # ★放行並等它真的結束,才讓 monkeypatch 還原★
        #   不等的話,那條緒會在【還原之後】才走到真正的 `os._exit`。
        unblock.set()
        t.join(10)
        for x in list(threading.enumerate()):
            if x.name == "ConsultForceExit":
                x.join(10)
        assert died == [7], "logging 卡住時保險絲沒有開火"
        assert not t.is_alive()

    def test_the_escalation_sites_do_not_log_before_arming(self):
        """★三個升級點都要走 `_force_exit`★

        在它之前自己 `logging.critical` 的話,那一行就是新的卡點。
        """
        for fn in ("run_consult_flow", "_note_flow_lock_skipped"):
            body = _strip_comments(_fn_src(fn))
            assert "_force_exit(" in body, fn
            assert "logging.critical" not in body, (
                f"{fn} 在掛保險絲之前自己寫了 critical log")
        guard = _strip_comments(_fn_src("_arm_exit_deadline"))
        assert "_force_exit(" in guard
        assert "logging.critical" not in guard

    def test_the_raw_process_exit_lives_in_exactly_two_places(self):
        """★裸的 `os._exit` 只能出現在那兩個原語裡★

        這不是風格潔癖。散落各處的 `os._exit` 測試攔不住 —— 而一條測試緒
        真的把 pytest 殺掉的話,pytest 會以那個 code 結束,★已經紅掉的測試
        變成綠的★。本批實際發生過兩次(一次 rc=0 但其實三個測試是紅的)。
        集中成兩個原語之後,測試只要 patch 它們就再也不會被殺掉。
        """
        # ★用 AST 掃,不用原始碼字面★ docstring/註解裡就有 `os._exit()`
        #   這幾個字 —— 掃字面的話,講解本身會把它算成違規(反過來也一樣)。
        tree = ast.parse(SRC)
        offenders = set()
        for n in ast.walk(tree):
            if not isinstance(n, ast.FunctionDef):
                continue
            body = n.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            code = NL.join(ast.unparse(st) for st in body)
            if "._exit(" in code:
                offenders.add(n.name)
        # `exit_action` 只是因為 `_shutdown` 巢狀在它裡面才會被列到;
        # 巢狀那層自己也在集合裡,所以真正的斷言是「沒有第三個地方」。
        assert offenders <= {"_exit_now", "_hard_exit"}, (
            f"★裸的 os._exit 出現在不該出現的地方:{sorted(offenders)}★")

    def test_exit_now_touches_nothing_that_can_wait(self):
        """★這個原語只能做一件事★ 多一行都是新的卡點。"""
        body = _fn_body_code("_exit_now")
        for forbidden in ("logging", "flush", "acquire", "open(", "Lock"):
            assert forbidden not in body, f"_exit_now 不可以碰 {forbidden}"
        assert body.strip() == "os._exit(code)", (
            f"_exit_now 只能有那一行,現在是:{body!r}")


# ══ SF-2 Outlook 可用性探測要有界 ═══════════════════════════════════════
class TestOutlookProbeIsBounded:
    def setup_method(self):
        w32._stranded.pop("OutlookAvailCheck", None)

    teardown_method = setup_method

    def test_a_wedged_probe_does_not_pile_up_threads(self):
        """★這是原始缺陷★ Outlook 卡住是持續性的,每輪 poll 再開一條 =
        COM apartment 逐條堆積,一天數百條,永遠不收斂。"""
        release = threading.Event()
        orig = cq._outlook_probe
        cq._outlook_probe = lambda: (release.wait(30), True)[1]
        try:
            results = [cq._outlook_available(0.05) for _ in range(12)]
            assert not any(results), "探測卡住時必須回 False"
            live = [t for t in threading.enumerate()
                    if t.name == "OutlookAvailCheck" and t.is_alive()]
            assert len(live) <= w32.MAX_STRANDED_PER_NAME, (
                f"卡住的探測堆積了 {len(live)} 條(上限 "
                f"{w32.MAX_STRANDED_PER_NAME})")
        finally:
            release.set()
            cq._outlook_probe = orig
            time.sleep(0.2)

    def test_it_goes_through_the_shared_bounded_helper(self):
        """★不要再手寫第四份 single-flight★

        `call_with_timeout` 已經備齊八件事(有界等待/原子佔位/未 start 也算
        佔/條件釋放/start 失敗釋放/同名上限…),而且已經外審過。
        """
        body = _strip_comments(_fn_src("_outlook_available"))
        assert "call_with_timeout(" in body
        assert "threading.Thread" not in body, "不可以自己再開一條裸 thread"
        assert ".join(" not in body

    def test_a_probe_that_says_yes_is_reported_available(self):
        orig = cq._outlook_probe
        cq._outlook_probe = lambda: True
        try:
            assert cq._outlook_available(2.0) is True
        finally:
            cq._outlook_probe = orig

    def test_a_probe_that_raises_is_reported_unavailable(self):
        """fail-open 的方向要正確:探測不出來 = 對我們來說不可用。"""
        orig = cq._outlook_probe

        def _boom():
            raise OSError("COM 壞了")

        cq._outlook_probe = _boom
        try:
            assert cq._outlook_available(2.0) is False
        finally:
            cq._outlook_probe = orig

    def test_the_probe_pairs_coinitialize_with_couninitialize(self):
        body = _fn_src("_outlook_probe")
        assert "CoInitialize()" in body
        assert "CoUninitialize()" in body
        assert "finally:" in body, "CoUninitialize 必須在 finally(否則洩 apartment)"


# ══ SF-3 流程鎖卡死要被量到 ═════════════════════════════════════════════
class TestFlowLockWedgeIsDetected:
    def setup_method(self):
        cq._flow_lock_held_since[0] = 0.0
        cq._flow_wedge_restart_requested[0] = False

    teardown_method = setup_method

    def _skip_with_holder_age(self, age_sec):
        """模擬「有人持鎖 age_sec 秒」時被擋下的那一輪 → 有沒有升級重啟。"""
        killed = []
        orig = cq._force_exit
        # ★patch `_force_exit` 而不是 `_hard_exit`★ 讓真的 `_force_exit` 跑起來
        #   會建立一條「睡飽就 `os._exit`」的緒 —— 它會在稍後把 pytest 殺掉,
        #   而且是以 exit code 0/1 殺掉 → 測試結果變成假綠(實際發生過)。
        cq._force_exit = lambda reason, code=1: killed.append((reason, code))
        cq._flow_lock_held_since[0] = time.monotonic() - age_sec
        try:
            cq._note_flow_lock_skipped("poll")
        finally:
            cq._force_exit = orig
        return killed

    def test_a_normal_overlap_does_not_restart_anything(self):
        """合法上限約 20 分鐘(3 次 attempt),十分鐘的重疊是正常的。"""
        assert not self._skip_with_holder_age(600.0)

    def test_a_wedged_holder_escalates_to_restart(self):
        """★原始缺陷★ 這把鎖不會自癒,而所有觀測點都說一切正常。"""
        killed = self._skip_with_holder_age(cq._FLOW_LOCK_WEDGED_SEC + 60.0)
        assert killed, "持鎖超過上限卻沒有升級 → 會診查詢永久停擺且無聲"
        assert killed[0][1] == 1, "卡死重啟要以非 0 結束(讓 watchdog 接手)"

    def test_it_escalates_only_once(self):
        assert self._skip_with_holder_age(cq._FLOW_LOCK_WEDGED_SEC + 60.0)
        assert not self._skip_with_holder_age(cq._FLOW_LOCK_WEDGED_SEC + 60.0)

    def test_no_recorded_holder_never_escalates(self):
        """★沒有起始時間 = 不知道持有多久★,不可以當成「卡了很久」。

        (`0.0` 減出來是一個巨大的差值 —— 把「不知道」當成某個確定答案,
         正是這個專案一路在修的病灶。)
        """
        killed = []
        orig = cq._force_exit
        cq._force_exit = lambda reason, code=1: killed.append(reason)
        cq._flow_lock_held_since[0] = 0.0
        try:
            cq._note_flow_lock_skipped("poll")
        finally:
            cq._force_exit = orig
        assert not killed

    def test_the_threshold_is_above_the_gate_takeover(self):
        """系統已認定超過 45 分鐘的工作是死的;門檻低於它會誤殺健康的長工作。"""
        assert cq._FLOW_LOCK_WEDGED_SEC > 45 * 60

    def test_the_holder_timestamp_is_cleared_before_the_lock_is_released(self):
        """★順序★ 先 release 的話,下一條緒可能已經寫進自己的起始時間,
        我們接著把它抹成 0 —— 那條的持有時間永遠量不到,判定對它失效。
        """
        body = _strip_comments(_fn_src("_do_full_job"))
        clear = body.rindex("_flow_lock_held_since[0] = 0.0")
        release = body.rindex("_flow_lock.release()")
        assert clear < release

    def test_the_job_records_when_it_took_the_lock(self):
        """沒有這一行的話,`_note_flow_lock_skipped` 永遠量到 0 → 判定失效。"""
        body = _strip_comments(_fn_src("_do_full_job"))
        assert "_flow_lock_held_since[0] = time.monotonic()" in body

    def test_the_skip_path_goes_through_the_measuring_helper(self):
        """★被擋下來是這把鎖唯一會被外界看見的時刻★ —— 不在這裡量就沒得量。"""
        body = _strip_comments(_fn_src("_do_full_job"))
        assert "_note_flow_lock_skipped(" in body


# ══ SF-4 守衛自己要有出口 ═══════════════════════════════════════════════
class TestTheStrandedWorkerGuardHasAnExit:
    """★這是批次SC(同日上午)那個修正自己開的洞★

    single-flight 把「資源爆炸」換成了「永久服務拒絕」:worker 卡在 raw
    `GetWindowText` 永不返回 → 之後每一輪都拿得到 `_flow_lock`、進來、
    被守衛擋掉、正常釋放鎖。資源不再累積(守衛達成目的),但會診查詢
    ★永遠★不會再執行,而且流程鎖卡死判定完全看不見它(鎖每輪都放掉了)。
    唯一能終結 native-blocked thread 的手段是重啟,而守衛加上去之後,
    已經沒有任何一條路會走到重啟。
    """

    def setup_method(self):
        cq._last_hidden_worker = None
        cq._last_hidden_worker_since[0] = 0.0

    teardown_method = setup_method

    @staticmethod
    def _stuck(age_sec):
        release = threading.Event()
        t = threading.Thread(target=lambda: release.wait(30), daemon=True)
        t.start()
        cq._last_hidden_worker = t
        cq._last_hidden_worker_since[0] = time.monotonic() - age_sec
        return release, t

    def _run(self, monkeypatch, age_sec):
        release, stuck = self._stuck(age_sec)
        killed = []
        monkeypatch.setattr(cq, "load_config", lambda: {})
        monkeypatch.setattr(cq, "_ensure_hidden_desktop", lambda: 12345)
        monkeypatch.setattr(
            cq, "_force_exit",
            lambda reason, code=1: killed.append((reason, code)))
        try:
            with pytest.raises(RuntimeError, match="本輪略過"):
                cq.run_consult_flow("test")
        finally:
            release.set()
            stuck.join(5)
        return killed

    def test_a_briefly_stranded_worker_does_not_restart_anything(
            self, monkeypatch):
        """HIS 只是暫時忙 → 那條 worker 自己的 deadline 到期就會結束。"""
        assert not self._run(monkeypatch, 600.0)

    def test_a_permanently_stranded_worker_escalates_to_restart(
            self, monkeypatch):
        """★原始缺陷★ 沒有這條出口的話,會診查詢永久停擺而且無從自癒。"""
        killed = self._run(
            monkeypatch, cq._HIDDEN_WORKER_STRANDED_MAX_SEC + 60.0)
        assert killed, "放生的 worker 卡到上限卻沒有升級重啟"
        assert killed[0][1] == 1, "要以非 0 結束,讓外層 watchdog 接手"

    def test_an_unstamped_strand_never_escalates(self):
        """★「不知道從什麼時候開始」不可以當成「卡了很久」★

        (`monotonic() - 0.0` 是一個巨大的數字 —— 把讀不到當成確定答案,
         正是這個專案一路在修的病灶。)
        """
        release = threading.Event()
        t = threading.Thread(target=lambda: release.wait(10), daemon=True)
        t.start()
        cq._last_hidden_worker = t
        cq._last_hidden_worker_since[0] = 0.0
        killed = []
        orig = cq._force_exit
        cq._force_exit = lambda reason, code=1: killed.append(reason)
        try:
            src = _strip_comments(_fn_src("run_consult_flow"))
            assert "if since and stranded >=" in src
            assert not killed
        finally:
            cq._force_exit = orig
            release.set()
            t.join(5)

    def test_the_timeout_path_stamps_when_the_strand_began(self):
        """沒有這個時間戳,出口就永遠量不到年齡 → 出口等於不存在。"""
        src = _strip_comments(_fn_src("run_consult_flow"))
        timeout_at = src.index("if t.is_alive():")
        stamp = src.index("_last_hidden_worker_since[0] = time.monotonic()")
        assert timeout_at < stamp, "時間戳要蓋在【逾時放生】那條路上"

    def test_a_normal_finish_clears_both_the_ref_and_the_stamp(self):
        src = _strip_comments(_fn_src("run_consult_flow"))
        assert "_last_hidden_worker = None" in src
        assert "_last_hidden_worker_since[0] = 0.0" in src

    def test_the_cap_is_far_above_the_normal_timeout(self):
        assert (cq._HIDDEN_WORKER_STRANDED_MAX_SEC
                > 4 * cq._HIDDEN_WORKER_TIMEOUT_SEC)


# ══ SF-5 旗標檔要「取得」,不是「看到」 ═════════════════════════════════
class TestFlagFilesAreClaimedNotObserved:
    def setup_method(self):
        cq._flag_claim_warned_at.clear()

    teardown_method = setup_method

    def test_an_undeletable_flag_is_not_consumed(self, tmp_path, monkeypatch):
        """★原始缺陷★ 刪不掉卻照做 → 每一次 tick(0.5~5 秒)都是一次新要求:
        RUNNOW = 不斷查 HIS 並寄信直到配額耗盡;
        RELOAD = 排程被反覆重建,輪詢 job 的下次執行時間跟著被重設,
                 ★輪詢永遠不會觸發★。
        """
        flag = tmp_path / "runnow.flag"
        flag.write_text("x", encoding="utf-8")

        def _denied(_p):
            raise PermissionError("被防毒鎖住")

        monkeypatch.setattr(cq.os, "unlink", _denied)
        assert cq._claim_flag_file(flag) is False
        assert flag.exists(), "刪不掉時不應該假裝拿走了"

    def test_a_deletable_flag_is_claimed_once(self, tmp_path):
        flag = tmp_path / "runnow.flag"
        flag.write_text("x", encoding="utf-8")
        assert cq._claim_flag_file(flag) is True
        assert cq._claim_flag_file(flag) is False, "同一個旗標不可以被拿兩次"

    def test_the_warning_is_throttled(self, tmp_path, monkeypatch):
        """這條 error 在每一次 tick 都會走到 —— 不節流就換成 log 被洗掉。"""
        flag = tmp_path / "runnow.flag"
        flag.write_text("x", encoding="utf-8")
        monkeypatch.setattr(
            cq.os, "unlink",
            lambda _p: (_ for _ in ()).throw(PermissionError("x")))
        seen = []
        monkeypatch.setattr(cq.logging, "error",
                            lambda *a, **k: seen.append(a))
        for _ in range(20):
            cq._claim_flag_file(flag)
        assert len(seen) == 1, f"沒有節流(印了 {len(seen)} 次)"

    def test_the_scheduler_claims_both_flags(self):
        """★接線★ helper 沒有被呼叫的話,上面那些性質一件都不會發生。"""
        src = _strip_comments(_fn_src("scheduler_loop"))
        assert "_claim_flag_file(RUNNOW_FLAG)" in src
        assert "_claim_flag_file(RELOAD_FLAG)" in src
        assert "RUNNOW_FLAG.unlink()" not in src, "還留著舊的「刪不掉也照做」"
        assert "RELOAD_FLAG.unlink()" not in src


# ══ SF-7 journal 讀不到 ≠ 沒有待辦 ═════════════════════════════════════
class TestAnUnreadableJournalIsNotAnEmptyOne:
    def test_pending_propagates_the_read_failure(self, tmp_path, monkeypatch):
        import cmuh_common.atomic_io as aio
        import cmuh_common.paths as paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        monkeypatch.setattr(aio, "safe_load_json_ex",
                            lambda *a, **k: ({}, "error"))
        data, ok = cq._trigger_journal_pending()
        assert data == {}
        assert ok is False, "★讀不到被壓成「沒有待辦」★"

    def test_resume_does_not_treat_unreadable_as_nothing_to_do(
            self, tmp_path, monkeypatch):
        """★原始缺陷★ 那些 uid 對應的觸發信已經是 \\Seen,IMAP 再也掃不到 ——
        當成「沒有待辦」等於讓醫師的會診請求無聲消失。"""
        import cmuh_common.atomic_io as aio
        import cmuh_common.paths as paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        monkeypatch.setattr(aio, "safe_load_json_ex",
                            lambda *a, **k: ({}, "error"))
        fired = []
        alerted = []
        monkeypatch.setattr(cq, "trigger_job_async",
                            lambda *a, **k: fired.append(a))
        monkeypatch.setattr(cq, "_alert_trigger_journal_unreadable",
                            lambda: alerted.append(1))
        assert cq.resume_pending_triggers() == 0
        assert not fired
        assert alerted, "★讀不到卻沒有告警 = 沒有人會知道請求不見了★"

    def test_a_readable_journal_still_resumes(self, tmp_path, monkeypatch):
        """反面:正常情況照樣補跑（守衛不可以把功能一起關掉）。"""
        import cmuh_common.paths as paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        cq._trigger_journal_add("42", "doc@x.tw", "9", "T:INBOX")
        fired = []
        monkeypatch.setattr(cq, "trigger_job_async",
                            lambda *a, **k: fired.append(k))
        assert cq.resume_pending_triggers() == 1
        assert fired and fired[0]["override_recipients"] == ["doc@x.tw"]


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
