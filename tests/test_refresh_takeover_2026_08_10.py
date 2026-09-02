# -*- coding: utf-8 -*-
"""[穩定性總體檢 批次SB #3] refresh 單飛旗標要有 age takeover。

★問題（外部第二意見 #3）★
`_refresh_worker_running` 只在 worker 的 finally 清。worker 若卡在
requests timeout 管不到的地方（原生 DNS 解析、防毒掛鉤），旗標永遠是
True → 之後所有刷新永久被去重跳過：**掛號數／門檻掃描／止掛提醒全部
停在舊資料**，而 Tk 與 heartbeat 都活著，watchdog 不會自癒。

repo 已經對打卡狀態面板修過同形狀的坑（`_CLOCK_WORKER_MAX_AGE_SEC`）——
這裡是同一套解法：逾齡就接管，generation 讓殭屍失去擁有權。
"""
import ast
import importlib
import io
import os
import sys

import pytest

NL = chr(10)
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

m = importlib.import_module("main")


def _src_of(fn_name):
    text = io.open(os.path.join(REPO_ROOT, "src", "main.py"),
                   encoding="utf-8").read()
    tree = ast.parse(text)
    for n in ast.walk(tree):
        if isinstance(n, ast.FunctionDef) and n.name == fn_name:
            return text, n
    raise AssertionError(f"找不到 {fn_name}")


class TestAgeTakeover:
    def test_the_gate_checks_the_worker_age(self):
        """★核心★ gate 必須看「上一輪跑多久了」，不是只看旗標。"""
        text, fn = _src_of("_trigger_refresh")
        seg = ast.get_source_segment(text, fn) or ""
        assert "_REFRESH_WORKER_MAX_AGE_SEC" in seg, (
            "★gate 不看年齡 → 一次卡死 = 刷新永久停擺★")
        assert "_refresh_worker_started_at" in seg

    def test_seizing_the_flag_records_the_start_time(self):
        text, fn = _src_of("_trigger_refresh")
        seg = ast.get_source_segment(text, fn) or ""
        assert "self._refresh_worker_started_at = time.time()" in seg, (
            "搶旗標沒記時間 → 年齡永遠算不出來")

    def test_the_takeover_bumps_the_generation(self):
        """接管必須 +1 世代 —— 殭屍醒來才會失去擁有權。"""
        text, fn = _src_of("_trigger_refresh")
        seg = ast.get_source_segment(text, fn) or ""
        i = seg.index("_stale_takeover")
        j = seg.index("else:", i)
        assert "self._refresh_generation += 1" in seg[i:j]

    def test_the_zombie_does_not_clear_the_new_rounds_state(self):
        """★殭屍醒來不可以清狀態★ 清掉 = 拆掉現任那一輪的去重與單飛。"""
        text, fn = _src_of("run_parallel_checks")
        seg = ast.get_source_segment(text, fn) or ""
        assert "_is_zombie" in seg, "finally 沒有殭屍判定"
        # 清旗標那行必須在「不是殭屍」的分支底下
        i = seg.index("_is_zombie = (_my_refresh_gen != self._refresh_generation)")
        j = seg.index("self._refresh_worker_running = False", i)
        between = seg[i:j]
        assert "else:" in between, "清旗標沒有被殭屍判定 gate 住"

    def test_the_zombie_check_does_not_return_inside_finally(self):
        """★return 在 finally 裡會吞掉 in-flight 例外★（ruff B012 也擋）。"""
        text, fn = _src_of("run_parallel_checks")
        for t in ast.walk(fn):
            if isinstance(t, ast.Try) and t.finalbody:
                for f in t.finalbody:
                    for r in ast.walk(f):
                        assert not isinstance(r, ast.Return), (
                            f"finally 內有 return(行 {r.lineno})")

    def test_the_default_age_limit_is_sane(self):
        """上限要蓋得住正常 worst case（多批×3重試×逾時 ≒ 數分鐘）。"""
        assert 600 <= m._REFRESH_WORKER_MAX_AGE_SEC <= 3600

    def test_a_stale_worker_is_actually_taken_over(self, monkeypatch):
        """行為測試：旗標 True + 逾齡 → gate 走「接管」路徑（世代 +1、
        重記時間、submit 新 worker）。"""
        import threading
        from collections import deque

        class _Exec:
            def __init__(self):
                self.submitted = []

            def submit(self, fn, *a, **k):
                self.submitted.append(fn)

                class _F:
                    def add_done_callback(self, cb):
                        pass
                return _F()

        class _Var:
            def set(self, *_a):
                pass

        class _Btn:
            def config(self, **_k):
                pass

        class _Host:
            _shutting_down = False
            _refresh_worker_running = True
            _refresh_worker_started_at = 0.0        # 很久以前
            _refresh_generation = 7
            _active_refresh_signature = ("old",)
            _queued_refresh_requests = deque()
            _queued_refresh_signatures = set()
            _refresh_queue_lock = threading.Lock()
            _startup_defer_full_until_priority_done = False
            _heavy_modules_ready = True
            bg_executor = _Exec()
            status_text = _Var()
            startup_phase_text = _Var()
            refresh_button = _Btn()
            all_doctors_data = {}
            _doctor_data_lock = threading.Lock()
            ui_queue = None
            _refresh_progress_total = 0
            _refresh_progress_done = 0

        _Host._trigger_refresh = m.AutomationApp.__dict__["_trigger_refresh"]
        host = _Host()
        host._trigger_refresh(False)
        assert host._refresh_generation == 8, (
            "★逾齡的旗標沒有被接管 → 一次卡死 = 刷新永久停擺★")
        assert host._refresh_worker_running is True
        assert host._refresh_worker_started_at > 0.0
        assert host.bg_executor.submitted, "接管後沒有 submit 新 worker"

    def test_a_young_worker_is_not_taken_over(self, monkeypatch):
        """★反方向★ 正常執行中的 worker 不可以被搶（會重複打掛號站）。"""
        import threading
        import time as _t
        from collections import deque

        class _Exec:
            def __init__(self):
                self.submitted = []

            def submit(self, fn, *a, **k):
                self.submitted.append(fn)

                class _F:
                    def add_done_callback(self, cb):
                        pass
                return _F()

        class _Host:
            _shutting_down = False
            _refresh_worker_running = True
            _refresh_worker_started_at = _t.time() - 30.0   # 才 30 秒
            _refresh_generation = 7
            _active_refresh_signature = ("current",)
            _queued_refresh_requests = deque()
            _queued_refresh_signatures = set()
            _refresh_queue_lock = threading.Lock()
            bg_executor = _Exec()

        _Host._trigger_refresh = m.AutomationApp.__dict__["_trigger_refresh"]
        host = _Host()
        host._trigger_refresh(False)
        assert host._refresh_generation == 7, "30 秒的 worker 被誤判成卡死"
        assert not host.bg_executor.submitted, "正常執行中卻又開了一輪"


# ══ [批次SB #6] liveness 一律 monotonic ═══════════════════════════════════
class TestLivenessUsesMonotonic:
    """★時鐘回撥會延後自救★

    scheduler 真的卡死 + NTP/網域政策把時鐘往回調：`time.time()-last_tick`
    變負 → self-watchdog 要等 wall clock 重新追上舊時間戳才會介入
    （回撥十分鐘 = 多停十分鐘）。liveness 量的是「經過了多久」，
    本來就該用 monotonic。★寫入端與比較端必須同一種基準★
    """

    @staticmethod
    def _fn_src(rel, fn_name):
        text = io.open(os.path.join(REPO_ROOT, "src", rel),
                       encoding="utf-8").read()
        tree = ast.parse(text)
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == fn_name:
                return ast.get_source_segment(text, n) or ""
        raise AssertionError(f"{rel} 找不到 {fn_name}")

    @pytest.mark.parametrize("rel,writer,watchdog", [
        ("autoclock.py", "scheduler_loop", "_autoclock_self_watchdog"),
        ("consult_query.py", "scheduler_loop", "_scheduler_self_watchdog"),
    ])
    def test_writer_and_watchdog_share_the_monotonic_base(self, rel, writer,
                                                          watchdog):
        w = self._fn_src(rel, writer)
        assert 'LIVENESS["last_tick"] = time' in w.replace("_module", ""), \
            f"{rel}:{writer} 找不到 last_tick 寫入(守衛自己失效了)"
        assert '"last_tick"] = time.monotonic()' in w.replace("_module", ""), (
            f"★{rel}:{writer} 的 last_tick 不是 monotonic★")
        d = self._fn_src(rel, watchdog)
        assert "monotonic() - last" in d.replace("_module", ""), (
            f"★{rel}:{watchdog} 的年齡計算不是 monotonic —— 與寫入端混用基準★")
        # 比較端不可以殘留 wall clock 的年齡計算
        code_only = NL.join(ln.split("#")[0] for ln in d.splitlines())
        assert "time.time() - last" not in code_only.replace("_module", ""), (
            f"★{rel}:{watchdog} 仍有 wall clock 年齡計算 —— 混用基準★")


# ══ 外審 SB 第 2 輪 ═══════════════════════════════════════════════════════
class TestZombiePayloadsAreDropped:
    """★#2★ 殭屍 worker 醒來後仍會把【舊的】掛號數丟進 ui_queue ——
    沒有世代戳的話，舊資料蓋掉新資料，連帶改變止掛提醒的判定。"""

    def test_the_message_carries_the_generation(self):
        from cmuh_common.ui_messages import UiClinicDataMessage
        msg = UiClinicDataMessage(doctor_name="D1", data={}, refresh_gen=7)
        assert msg.refresh_gen == 7
        assert UiClinicDataMessage(doctor_name="D1", data={}).refresh_gen is None

    def test_every_worker_emit_site_carries_the_generation(self):
        """★check_appointment_count 的每一個送出點都要帶戳★ 漏一個,
        那條路的舊資料就照樣蓋新資料。"""
        text, fn = _src_of("check_appointment_count")
        seg = ast.get_source_segment(text, fn) or ""
        emits = seg.count("UiClinicDataMessage(")
        stamped = seg.count("refresh_gen=")
        assert emits >= 5, f"送出點只剩 {emits} 個(守衛自己失效了?)"
        assert stamped >= emits, (
            f"★{emits} 個送出點只有 {stamped} 個帶世代戳★")

    def test_the_receiver_drops_stale_generations(self):
        text, fn = _src_of("process_ui_queue")
        seg = ast.get_source_segment(text, fn) or ""
        # ★要驗【那個比較】存在,不是驗字面出現過★ 條件被換成 `if False:`
        #   時 'refresh_gen' 字面還在別行(突變驗證抓到的)。
        assert "_mgen != self._refresh_generation" in seg, (
            "★接收端不驗世代 → 殭屍舊資料照收★")
        assert "continue" in seg[seg.index("_mgen != self."):][:600]

    def test_the_worker_config_carries_the_generation(self):
        text, fn = _src_of("run_parallel_checks")
        seg = ast.get_source_segment(text, fn) or ""
        # ★不可以只找 '_refresh_gen' 子字串★ `_my_refresh_gen` 也含它 ——
        #   把賦值刪掉,子字串測試照樣綠(突變驗證抓到的)。
        assert 'worker_config["_refresh_gen"]' in seg, (
            "worker_config 沒帶世代 → 戳永遠是 None")


class TestTakeoverCoalescesQueuedDuplicates:
    """★#3★ 接管的這一輪就是要跑這個簽名 —— 排隊裡同簽名的那筆要合併掉，
    否則接管完成後佇列接力把同一個刷新再跑一次（對掛號站雙倍請求）。"""

    def test_a_matching_queued_request_is_coalesced(self):
        import threading
        import time as _t
        from collections import deque

        class _Exec:
            def __init__(self):
                self.submitted = []

            def submit(self, fn, *a, **k):
                self.submitted.append(fn)

                class _F:
                    def add_done_callback(self, cb):
                        pass
                return _F()

        class _Var:
            def set(self, *_a):
                pass

        class _Btn:
            def config(self, **_k):
                pass

        class _Host:
            _shutting_down = False
            _refresh_worker_running = True
            _refresh_worker_started_at = 0.0
            _refresh_generation = 3
            _active_refresh_signature = ("stuck",)
            _queued_refresh_requests = deque()
            _queued_refresh_signatures = set()
            _refresh_queue_lock = threading.Lock()
            _startup_defer_full_until_priority_done = False
            _heavy_modules_ready = True
            bg_executor = _Exec()
            status_text = _Var()
            startup_phase_text = _Var()
            refresh_button = _Btn()
            all_doctors_data = {}
            _doctor_data_lock = threading.Lock()
            ui_queue = None
            _refresh_progress_total = 0
            _refresh_progress_done = 0

        _Host._trigger_refresh = m.AutomationApp.__dict__["_trigger_refresh"]
        host = _Host()
        # 先跑一次拿到「同一種請求」的簽名（進佇列那條路需要 running 且未逾齡）
        host._refresh_worker_started_at = _t.time()
        host._trigger_refresh(False)
        assert len(host._queued_refresh_signatures) == 1, "前置:請求沒進佇列"
        sig = next(iter(host._queued_refresh_signatures))
        # 現在讓它逾齡 → 同一種請求觸發接管
        host._refresh_worker_started_at = 0.0
        host._trigger_refresh(False)
        assert sig not in host._queued_refresh_signatures, (
            "★接管沒有合併同簽名的排隊請求 → 完成後會再跑一次★")
        assert not any(r[2] == sig for r in host._queued_refresh_requests)
        assert host.bg_executor.submitted, "接管沒開新 worker"


class TestFetchSlotIsBounded:
    """★#1(第2/3輪)★ 兩個許可都被卡死的 worker 抱走時：
    新 worker 不可無限期排隊；等不到不可記成【遠端失敗】（本地容量問題
    進 backoff 會在許可恢復後還拖延自己）；被抱死的容量要能回收。"""

    def setup_method(self):
        import threading
        m._reg52_slot_state["sema"] = threading.Semaphore(2)
        m._reg52_slot_state["holders"] = {}

    teardown_method = setup_method

    def test_a_timeout_raises_backoff_active_not_a_remote_failure(self):
        """★等不到 slot ≠ 遠端壞掉★ 拋 RequestException 會進
        `_source_backoff_fail` → 許可恢復後還被自己的 backoff 拖延。"""
        import pytest as _pt
        # 佔走兩個許可(沒有登記 holder → 不可回收 → 純等待逾時)
        m._reg52_slot_state["sema"].acquire()
        m._reg52_slot_state["sema"].acquire()
        with _pt.raises(m.Reg52BackoffActive):
            with m._acquire_reg52_fetch_slot(timeout=0.05):
                raise AssertionError("不該進得來")

    def test_wedged_permits_are_reclaimed(self):
        """★核心(第3輪)★ 許可被抱超過門檻 → 換新容量,新 worker 拿得到
        fresh slot(不是永遠退化成快取)。"""
        import time as _t
        m._reg52_slot_state["sema"].acquire()
        m._reg52_slot_state["sema"].acquire()
        # 模擬兩個持有者早就超過 wedged 門檻
        m._reg52_slot_state["holders"][object()] = (
            _t.monotonic() - m._REG52_SLOT_WEDGED_SEC - 1)
        m._reg52_slot_state["holders"][object()] = (
            _t.monotonic() - m._REG52_SLOT_WEDGED_SEC - 1)
        entered = []
        with m._acquire_reg52_fetch_slot(timeout=0.2):
            entered.append(1)          # ★要拿得到★
        assert entered, "★容量沒有被回收 → takeover 永遠只能退化成快取★"

    def test_young_holders_are_not_reclaimed(self):
        """★反方向★ 正常執行中的持有者不可以被換代（併發上限會失真）。"""
        import time as _t

        import pytest as _pt
        m._reg52_slot_state["sema"].acquire()
        m._reg52_slot_state["sema"].acquire()
        m._reg52_slot_state["holders"][object()] = _t.monotonic() - 5.0
        with _pt.raises(m.Reg52BackoffActive):
            with m._acquire_reg52_fetch_slot(timeout=0.05):
                raise AssertionError("不該進得來")

    def test_a_successful_acquire_is_released(self):
        for _ in range(3):
            with m._acquire_reg52_fetch_slot(timeout=1.0):
                pass
        assert not m._reg52_slot_state["holders"], "holder 沒有除名"

    def test_the_real_acquire_registers_a_holder(self):
        """★要走真的取得路徑★ 回收靠 holders 表;取得時不登記的話,
        回收永遠不會觸發(突變驗證抓到的:前面的測試都是手動塞表)。"""
        import threading
        entered = threading.Event()
        release = threading.Event()

        def _hold():
            with m._acquire_reg52_fetch_slot(timeout=1.0):
                entered.set()
                release.wait(10)

        t = threading.Thread(target=_hold, daemon=True)
        t.start()
        try:
            assert entered.wait(5), "前置:沒拿到 slot"
            assert len(m._reg52_slot_state["holders"]) == 1, (
                "★真的取得沒有登記 holder → 被抱死也回收不了★")
        finally:
            release.set()
            t.join(5)
        assert not m._reg52_slot_state["holders"], "釋放後沒除名"

    def test_all_fetch_sites_use_the_bounded_acquire(self):
        text = io.open(os.path.join(REPO_ROOT, "src", "main.py"),
                       encoding="utf-8").read()
        code_only = NL.join(ln.split("#")[0] for ln in text.splitlines())
        assert "with _reg52_cmuh_fetch_sema:" not in code_only, (
            "★仍有 fetch 站點用無界的 with semaphore★")
        assert code_only.count("with _acquire_reg52_fetch_slot():") == 4

    def test_the_acquire_actually_passes_a_timeout(self):
        import inspect
        import textwrap
        src = textwrap.dedent(inspect.getsource(m._acquire_reg52_fetch_slot))
        assert "acquire(timeout=timeout)" in src, (
            "★acquire 沒帶 timeout → 卡死的許可讓新 worker 無限期排隊★")

    def test_the_timeout_is_far_below_the_takeover_age(self):
        assert m._REG52_FETCH_SLOT_TIMEOUT_SEC * 3 < m._REFRESH_WORKER_MAX_AGE_SEC
        assert m._REG52_SLOT_WEDGED_SEC < m._REFRESH_WORKER_MAX_AGE_SEC


# ══ 外審 SB 第 4 輪:換代上限 + 重啟升級 ═══════════════════════════════════
class TestEpochCapAndEscalation:
    """★換代無上限 = 慢性自殺★ 根因持續存在時，每次換代再棄置 2 條
    native thread + 佔死 1 個 bg_executor worker（共 10 個）——
    幾小時就把 executor 吃光，回到「永久舊資料但 UI/heartbeat 都活著」。"""

    def setup_method(self):
        import threading
        m._reg52_slot_state["sema"] = threading.Semaphore(2)
        m._reg52_slot_state["holders"] = {}
        m._reg52_slot_state["epoch"] = 0
        m._reg52_slot_state.pop("exhausted", None)

    teardown_method = setup_method

    @staticmethod
    def _wedge_state():
        import time as _t
        # ★non-blocking★ 到頂之後 sema 不再被換,永久 0 permits ——
        #   阻塞式 acquire 會把測試自己鎖死(第一版就是這樣 hang 的)。
        m._reg52_slot_state["sema"].acquire(blocking=False)
        m._reg52_slot_state["sema"].acquire(blocking=False)
        m._reg52_slot_state["holders"][object()] = (
            _t.monotonic() - m._REG52_SLOT_WEDGED_SEC - 1)

    def test_replacements_stop_at_the_cap(self):
        """★核心★ 連環 wedge 下,換代次數必須被鎖在上限。"""
        import pytest as _pt
        for i in range(m._REG52_SLOT_EPOCH_CAP + 3):
            self._wedge_state()
            if i < m._REG52_SLOT_EPOCH_CAP:
                with m._acquire_reg52_fetch_slot(timeout=0.1):
                    pass
            else:
                with _pt.raises(m.Reg52BackoffActive):
                    with m._acquire_reg52_fetch_slot(timeout=0.05):
                        raise AssertionError("到頂了還拿得到")
        assert m._reg52_slot_state["epoch"] == m._REG52_SLOT_EPOCH_CAP, (
            f"★換代 {m._reg52_slot_state['epoch']} 次(上限 "
            f"{m._REG52_SLOT_EPOCH_CAP})—— native thread 無上限堆積★")
        assert m._reg52_slot_state.get("exhausted") is True

    def test_the_takeover_path_escalates_to_restart_once(self):
        """到頂之後,takeover 路徑要排入重啟升級 —— 而且只排一次。"""
        import threading
        from collections import deque

        m._reg52_slot_state["exhausted"] = True
        scheduled = []

        class _Exec:
            submitted = []

            def submit(self, fn, *a, **k):
                self.submitted.append(fn)

                class _F:
                    def add_done_callback(self, cb):
                        pass
                return _F()

        class _Var:
            def set(self, *_a):
                pass

        class _Btn:
            def config(self, **_k):
                pass

        class _Root:
            def after(self, d, cb=None):
                scheduled.append(cb)
                return "id"

        class _Host:
            _shutting_down = False
            _refresh_worker_running = True
            _refresh_worker_started_at = 0.0
            _refresh_generation = 1
            _active_refresh_signature = ("x",)
            _queued_refresh_requests = deque()
            _queued_refresh_signatures = set()
            _refresh_queue_lock = threading.Lock()
            _startup_defer_full_until_priority_done = False
            _heavy_modules_ready = True
            bg_executor = _Exec()
            status_text = _Var()
            startup_phase_text = _Var()
            refresh_button = _Btn()
            all_doctors_data = {}
            _doctor_data_lock = threading.Lock()
            ui_queue = None
            _refresh_progress_total = 0
            _refresh_progress_done = 0
            root = _Root()

            def _restart_when_hotkey_idle(self):
                pass

        _Host._trigger_refresh = m.AutomationApp.__dict__["_trigger_refresh"]
        host = _Host()
        host._trigger_refresh(False)
        # 呼叫端排的是 lambda(帶 force_after_max=False)→ 驗旗標 + 有排程即可,
        # 「排的確實是不強制版」另有 test_the_escalation_call_site_does_not_force。
        assert getattr(host, "_reg52_restart_requested", False) is True, (
            "★到頂沒有升級到重啟★")
        assert scheduled, "★到頂沒有排任何重啟回呼★"
        n_first = len(scheduled)
        # 再一次 takeover → 不可以再排(重啟風暴)
        host._refresh_worker_started_at = 0.0
        host._refresh_worker_running = True
        host._trigger_refresh(False)
        assert len(scheduled) == n_first, "★重啟升級排了不只一次★"

    def test_exhausted_state_survives_reclaim_attempts(self):
        self._wedge_state()
        m._reg52_slot_state["epoch"] = m._REG52_SLOT_EPOCH_CAP
        import pytest as _pt
        with _pt.raises(m.Reg52BackoffActive):
            with m._acquire_reg52_fetch_slot(timeout=0.05):
                pass
        assert m._reg52_slot_state.get("exhausted") is True
        # sema 沒有被換(棄置停止)
        assert m._reg52_slot_state["epoch"] == m._REG52_SLOT_EPOCH_CAP


class TestEscalationNeverForcesMidWorkflow:
    """★外審 SB 第 5 輪 → 外審第五輪 R5-P2-02 擴大到【所有】自動重啟★

    原本只有 reg52 升級走 `force_after_max=False`,自動更新到頂仍強制重啟,
    理由是「旗標卡死另有熱鍵 watchdog 兜底」—— ★那句話只對 worker thread
    已死成立★:`_hotkey_watchdog_action` 對「卡住但 thread 還活著」回
    `keep_stuck` 並明寫【絕不解鎖】(worker 可能正卡在 HIS 半寫入)。
    使用者 2026-09-02 定案:自動機制一律不腰斬,改由人工決定。
    參數因此整個拿掉 —— 沒有參數就沒有人能再把它打開。
    """

    @staticmethod
    def _host(busy=True):
        import time as _t

        class _Root:
            def __init__(self):
                self.scheduled = []

            def after(self, d, cb=None):
                self.scheduled.append(cb)
                return "id"

        class _Host:
            _subsystem_running = busy
            _subsystem_lock = __import__("threading").RLock()
            _restart_committing = False
            root = _Root()
            restarted = []
            notified = []

            def _restart_app(self):
                self.restarted.append(1)

            def _notify_restart_waiting(self, busy_, gap):
                # ★樁★:真的那支會叫 Windows 通知,測試不可以彈東西出來。
                #   記下來就好 —— 「等待要看得見」由下面的斷言驗。
                self.notified.append((busy_, gap))

        _Host._restart_when_hotkey_idle = m.AutomationApp.__dict__[
            "_restart_when_hotkey_idle"]
        h = _Host()
        # 讓 idle_gap 看起來很短(剛有熱鍵動作)
        m._runner_1280.last_action_time = _t.time()
        return h

    def test_no_automatic_path_can_force_any_more(self):
        """★這條測試自己也更正了★ 它原本斷言「更新那條到頂要強制重啟」——
        那正是 R5-P2-02 指出的缺陷,被釘成了正確答案。
        現在的不變式:閘門★沒有★強制模式可用,連參數都不存在。"""
        import inspect
        sig = inspect.signature(m.AutomationApp._restart_when_hotkey_idle)
        assert "force_after_max" not in sig.parameters, (
            "★強制模式還在★ 只要參數存在,就有人會在某條路上把它打開")
        code = NL.join(
            ln.split("#")[0]
            for ln in inspect.getsource(
                m.AutomationApp._restart_when_hotkey_idle).splitlines())
        # 到頂那個分支不可以再有「直接重啟」的出口
        assert code.count("self._restart_app()") == 1, (
            "★到頂的分支又長出一條強制重啟★:" + str(code.count("_restart_app()")))

    def test_nonforced_mode_waits_forever_while_busy(self, monkeypatch):
        """★核心★ 升級模式(force=False)到頂 + 熱鍵忙 → 不重啟、繼續等。"""
        monkeypatch.setattr(
            "threading.current_thread", __import__("threading").main_thread)
        h = self._host(busy=True)
        h._restart_when_hotkey_idle(m._UPDATE_RESTART_MAX_DEFER_ATTEMPTS)
        assert not h.restarted, "★到頂仍強制重啟 → 腰斬 HIS 寫入★"
        assert h.root.scheduled, "沒有繼續排下一次重查(變成永不重啟)"

    def test_nonforced_mode_restarts_once_idle(self, monkeypatch):
        import time as _t
        monkeypatch.setattr(
            "threading.current_thread", __import__("threading").main_thread)
        h = self._host(busy=False)
        m._runner_1280.last_action_time = (
            _t.time() - m._UPDATE_RESTART_IDLE_GAP_SEC - 1)
        h._restart_when_hotkey_idle(m._UPDATE_RESTART_MAX_DEFER_ATTEMPTS)
        assert h.restarted, "閒下來了卻不重啟(升級永不生效)"

    def test_the_escalation_call_site_goes_through_the_gate(self):
        """升級呼叫端要走閘門(而不是自己 `_restart_app()`)。
        ★不再檢查 force_after_max★:那個參數已經不存在,
        「絕不強制」現在是閘門自己的性質(上面那條測試釘住)。"""
        text, fn = _src_of("_trigger_refresh")
        seg = ast.get_source_segment(text, fn) or ""
        i = seg.index("_reg52_restart_requested = True")
        j = i + 600
        code_only = NL.join(ln.split("#")[0] for ln in seg[i:j].splitlines())
        assert "_restart_when_hotkey_idle" in code_only, (
            "★升級呼叫端沒走閘門★")
        assert "_restart_app()" not in code_only, (
            "★繞過閘門直接重啟 → 可能腰斬 HIS 寫入★")
