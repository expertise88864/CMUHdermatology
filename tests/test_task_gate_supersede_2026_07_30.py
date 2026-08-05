# -*- coding: utf-8 -*-
"""[2026-07-30 第二輪外審 P2-01] ActiveTaskGate 逾時接管不終止舊工作。

`stale_after_sec` 到了以後,`acquire_lease()` 會把同一個 key 再發一張 lease 給新的
tick —— 但舊的 worker 沒有被終止、沒有被通知、也不知道自己被接管了。它會繼續跑:
繼續開 Chrome、繼續對 HIS 寫入、繼續寄信。

★而且舊版連一行 log 都沒有★ —— 「一個任務卡了 45 分鐘」在正式環境完全隱形。

本輪修:(1) 接管要出聲(warning + 可選回呼);(2) 舊 worker 查得到自己被接管,
在【動作之前】退場。真正終止跑掉的工作需要子行程化,列為另案(見模組 docstring)。
"""
import logging
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common import task_gate as tg  # noqa: E402


class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, sec):
        self.t += sec


def _gate(stale=60.0, **kw):
    clock = _Clock()
    return tg.ActiveTaskGate(stale_after_sec=stale, clock=clock, **kw), clock


# ─── 接管必須出聲 ──────────────────────────────────────────────────────────
def test_a_stale_takeover_is_logged_as_a_warning(caplog):
    gate, clock = _gate(stale=60.0, label="autoclock")
    gate.acquire_lease("k")
    clock.advance(61)
    with caplog.at_level(logging.WARNING):
        assert gate.acquire_lease("k") is not None
    msgs = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("autoclock" in m and "仍在執行且無法終止" in m for m in msgs), (
        f"★逾時接管必須留下 warning★ 實際 log：{msgs}")


def test_a_normal_acquire_does_not_log_a_takeover(caplog):
    gate, _clock = _gate()
    with caplog.at_level(logging.WARNING):
        gate.acquire_lease("k")
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_the_on_supersede_callback_gets_the_key_and_age():
    seen = []
    gate, clock = _gate(stale=60.0, on_supersede=lambda k, a: seen.append((k, a)))
    gate.acquire_lease("k")
    clock.advance(90)
    gate.acquire_lease("k")
    assert len(seen) == 1
    assert seen[0][0] == "k" and seen[0][1] == pytest.approx(90.0)


def test_a_broken_on_supersede_callback_cannot_block_the_new_lease():
    """告警失敗不可害新的一輪拿不到 lease（那會讓任務整段停擺）。"""
    gate, clock = _gate(
        stale=60.0,
        on_supersede=lambda *_a: (_ for _ in ()).throw(RuntimeError("寄信炸了")))
    gate.acquire_lease("k")
    clock.advance(90)
    assert gate.acquire_lease("k") is not None


def test_the_callback_is_not_invoked_while_holding_the_internal_lock():
    """★回呼可能寄信/取別的鎖 → 在持鎖時呼叫會死鎖。★

    回呼裡再去問 gate 狀態:若 `_report_supersede` 是在持鎖時被呼叫，這裡會卡死。
    """
    result = {}
    gate, clock = _gate(
        stale=60.0,
        on_supersede=lambda k, _a: result.setdefault("age", gate.active_age_sec(k)))
    gate.acquire_lease("k")
    clock.advance(90)
    gate.acquire_lease("k")
    assert "age" in result, "回呼在持鎖時被呼叫（已死鎖）"


def test_the_supersede_count_is_observable():
    gate, clock = _gate(stale=60.0)
    assert gate.superseded_count == 0
    gate.acquire_lease("k")
    clock.advance(90)
    gate.acquire_lease("k")
    clock.advance(90)
    gate.acquire_lease("k")
    assert gate.superseded_count == 2


# ─── 舊 worker 查得到自己被接管 ────────────────────────────────────────────
def test_the_old_lease_knows_it_has_been_superseded():
    gate, clock = _gate(stale=60.0)
    old = gate.acquire_lease("k")
    assert old is not None and old.superseded is False
    clock.advance(90)
    new = gate.acquire_lease("k")
    assert new is not None
    assert old.superseded is True, "★舊 worker 必須查得出自己已被接管★"
    assert new.superseded is False


def test_nobody_holding_the_key_does_not_count_as_superseded():
    """★[外審第 1 輪] 我第一版把這件事搞錯了,而且錯的方向讓系統做得比修之前更少★

    第一版寫 `not gate.holds(self)` —— 於是「接錯人拿了 lease 但馬上放棄」也會讓舊
    worker 認為自己被接管。consult 就是這種形狀:接管者進來發現 `_flow_lock` 被舊
    worker 持著(non-blocking)→ 立刻 return 並釋放 lease → 舊 worker 跑到檢查點看到
    「沒人持有」也放棄 → **兩邊都不寄信**,email 觸發的醫師什麼都收不到。
    「沒人在做我的工作」的正確結論是「我要繼續把它做完」。
    """
    gate, clock = _gate(stale=60.0)
    old = gate.acquire_lease("k")
    assert old is not None
    clock.advance(90)
    replacement = gate.acquire_lease("k")       # 接管
    assert old.superseded is True
    gate.release("k", replacement)              # 接管者馬上放棄（拿不到下游的鎖）
    assert old.superseded is False, (
        "★沒人持有 key 時舊 worker 必須繼續做完，不可跟著放棄★")


def test_holds_and_superseded_are_not_simple_negations():
    """`holds()` 與 `superseded` 刻意不是互為反面 —— 兩者在「沒人持有」時都是 False。"""
    gate, _clock = _gate()
    lease = gate.acquire_lease("k")
    gate.release("k", lease)
    assert gate.holds(lease) is False
    assert lease.superseded is False


def test_a_lease_without_a_gate_reference_reports_not_superseded():
    """保守：查不到資訊時不可中止臨床流程。"""
    assert tg.TaskLease(key="k", token=1).superseded is False


def test_the_old_worker_release_still_cannot_clobber_the_new_lease():
    """既有行為（token 比對）不可因為 TaskLease 多了 gate 欄位而改變。"""
    gate, clock = _gate(stale=60.0)
    old = gate.acquire_lease("k")
    clock.advance(90)
    new = gate.acquire_lease("k")
    gate.release("k", old)                      # 舊 worker 收尾
    assert gate.holds(new) is True, "舊 worker 的 release 不可刪掉新 lease"
    gate.release("k", new)
    assert gate.holds(new) is False


# ─── ★查詢不可順手清掉逾時紀錄★ ───────────────────────────────────────────
def test_querying_does_not_swallow_the_pending_takeover_report(caplog):
    """舊版 `is_active`/`active_age_sec` 遇到逾時就 pop → 只要有人先查一次，
    下一次 acquire 就看不到那筆紀錄，於是【接管不會被記錄、不會告警】——
    正好把 P2-01 要修的可見性又弄丟。"""
    gate, clock = _gate(stale=60.0, label="consult")
    gate.acquire_lease("k")
    clock.advance(90)
    assert gate.is_active("k") is False         # 有人先查了
    assert gate.active_age_sec("k") is None
    with caplog.at_level(logging.WARNING):
        gate.acquire_lease("k")
    assert any("仍在執行且無法終止" in r.getMessage() for r in caplog.records), \
        "查詢過之後仍然必須報告這次接管"
    assert gate.superseded_count == 1


# ─── worker 端的 thread-local lease ────────────────────────────────────────
def test_current_worker_superseded_is_false_without_a_scope():
    assert tg.current_worker_superseded() is False


def test_current_worker_superseded_inside_a_scope():
    gate, clock = _gate(stale=60.0)
    lease = gate.acquire_lease("k")
    with tg.worker_lease_scope(lease):
        assert tg.current_worker_superseded() is False
        clock.advance(90)
        gate.acquire_lease("k")
        assert tg.current_worker_superseded() is True
    assert tg.current_worker_superseded() is False, "離開 scope 要還原"


def test_each_worker_thread_sees_its_own_lease_not_the_newest_one():
    """★這就是為什麼用 thread-local 而不是模組層變數★

    逾時接管之後【兩個 worker 同時存在】。若用一個模組層變數記「目前的 lease」，
    新 worker 一設定就把舊 worker 的身分蓋掉 → 舊 worker 反而查到新 lease、
    判定自己沒被接管，整個保護失效。
    """
    gate, clock = _gate(stale=60.0)
    old = gate.acquire_lease("k")
    old_verdict, new_verdict = {}, {}
    old_in_scope = threading.Event()
    took_over = threading.Event()

    def _old_worker():
        with tg.worker_lease_scope(old):
            old_in_scope.set()
            took_over.wait(timeout=5)
            old_verdict["superseded"] = tg.current_worker_superseded()

    t = threading.Thread(target=_old_worker, name="OldWorker")
    t.start()
    assert old_in_scope.wait(timeout=5)

    clock.advance(90)
    new = gate.acquire_lease("k")

    def _new_worker():
        with tg.worker_lease_scope(new):
            new_verdict["superseded"] = tg.current_worker_superseded()

    t2 = threading.Thread(target=_new_worker, name="NewWorker")
    t2.start()
    t2.join(timeout=5)
    took_over.set()
    t.join(timeout=5)

    assert old_verdict == {"superseded": True}, "舊 worker 必須看到自己被接管"
    assert new_verdict == {"superseded": False}, "新 worker 是現役"


def test_scopes_nest_without_leaking():
    gate, _clock = _gate()
    a = gate.acquire_lease("a")
    b = gate.acquire_lease("b")
    with tg.worker_lease_scope(a):
        with tg.worker_lease_scope(b):
            pass
        assert tg.current_worker_superseded() is False
    assert tg.current_worker_superseded() is False


def test_a_broken_lease_reports_not_superseded():
    """查詢炸掉時保守回 False —— 不因為缺資訊就中止臨床流程。"""
    class _Broken:
        @property
        def superseded(self):
            raise RuntimeError("炸了")

    with tg.worker_lease_scope(_Broken()):     # type: ignore[arg-type]
        assert tg.current_worker_superseded() is False


# ─── 呼叫端接線 ────────────────────────────────────────────────────────────
def test_autoclock_bails_out_before_punching_when_superseded():
    """★等 clock_lock 可能等很久★：等待期間 90 分鐘上限可能已到、同一個 schedule
    key 已發給新的一輪。不檢查就繼續打 = 同一時段打兩次卡。"""
    import inspect

    import autoclock
    src = inspect.getsource(autoclock.process_clock_task)
    assert "current_worker_superseded()" in src
    assert "不重複打卡" in src
    tick = inspect.getsource(autoclock._scheduler_tick)
    assert "worker_lease_scope(lease)" in tick, \
        "lease 要綁在 worker 緒上，否則 process_clock_task 深處查不到"
    assert autoclock._clock_task_gate._label == "autoclock"


def test_consult_treats_a_supersede_as_fatal_but_still_finishes_the_cleanup():
    """★不可直接 return★ 那會跳過釋放去重與失敗回信：email 觸發的醫師被卡住
    5 分鐘又收不到通知，只能乾等一個永遠不會來的結果（2026-07-30 已踩過一次）。
    故沿用 LoginNotCompleted 同一條 fatal 路徑：不重試，但收尾走完。"""
    import inspect

    import consult_query as cq
    assert issubclass(cq.JobSuperseded, RuntimeError)
    src = inspect.getsource(cq._do_full_job)
    assert "raise JobSuperseded(" in src, "要 raise 走 fatal 收尾，不是 return"
    # [2026-08-03] fatal 家族會增加（新增了 HISStartupBlocked）→ 釘「JobSuperseded
    # 在 fatal 判定裡」這個語意，而不是整行字面，否則每加一種 fatal 都會誤紅。
    i = src.index("fatal = isinstance(")
    fatal_expr = src[i:i + 240]
    assert "JobSuperseded" in fatal_expr and "LoginNotCompleted" in fatal_expr
    # 收尾（釋放去重 / 失敗回信）仍在同一條路上
    assert "_release_trigger_dedup(override_recipients)" in src
    assert "_send_failure_notice_async(override_recipients" in src
    assert cq._consult_job_gate._label == "consult"


def test_consult_requeues_an_email_trigger_blocked_by_the_flow_lock():
    """★[外審第 1 輪] gate 放行但 `_flow_lock` 被佔住時,那筆觸發整個消失★

    `trigger_job_async` 只在【gate 擋下】時排隊。逾時接管之後接管者拿到 lease、
    進 `_do_full_job` 卻發現舊 worker 還握著 `_flow_lock`(non-blocking)→ 直接
    return → email 觸發的醫師被去重卡住又收不到任何東西。
    """
    import inspect

    import consult_query as cq
    src = inspect.getsource(cq._do_full_job)
    head = src[:src.index("pythoncom = None")]
    assert "_enqueue_pending_retrigger(trigger_label, override_recipients)" in head
    assert 'trigger_label == "email"' in head, \
        "只補 email —— poll/排程本來就會自己再來，補跑只是白工"


def test_a_physician_who_was_just_served_is_dropped_from_the_requeue():
    """★[外審第 2/3 輪] 同一位醫師收到兩封的實際序列★

    t0  D 寄觸發信 → 任務開始 → 卡住 45 分鐘
    t45 D 再寄一次 → gate 逾時接管 → 接管者拿不到 `_flow_lock` → 把 D 排進補跑佇列
    t45 舊 worker 終於寄給 D（回答 t0）→ 佇列補跑又寄一次（回答 t45）
        → D 在幾秒內收到兩封幾乎一樣的清單。

    正解：舊 worker 寄成功之後，把【已經親自服務到的收件人】從佇列拿掉。
    （不採用「所有權不可逆轉移」——那會讓兩邊都不寄，見 _discard_served_retriggers。）
    """
    import consult_query as cq

    with cq._pending_retriggers_lock:
        cq._pending_retriggers.clear()
    try:
        cq._enqueue_pending_retrigger("email", ["D@example.com"])
        cq._discard_served_retriggers("email", ["d@EXAMPLE.com"])   # 大小寫不敏感
        with cq._pending_retriggers_lock:
            assert "email" not in cq._pending_retriggers, (
                "★已經親自寄給 D 了，佇列不可再補一次★")
    finally:
        with cq._pending_retriggers_lock:
            cq._pending_retriggers.clear()


def test_other_physicians_in_the_requeue_are_not_dropped():
    """只拿掉【已服務到的那些人】—— 其餘觸發者仍然必須補跑，不可連坐。"""
    import consult_query as cq

    with cq._pending_retriggers_lock:
        cq._pending_retriggers.clear()
    try:
        cq._enqueue_pending_retrigger("email", ["D@example.com", "E@example.com"])
        cq._discard_served_retriggers("email", ["D@example.com"])
        with cq._pending_retriggers_lock:
            assert cq._pending_retriggers["email"] == ["E@example.com"]
    finally:
        with cq._pending_retriggers_lock:
            cq._pending_retriggers.clear()


def test_discarding_from_an_empty_queue_is_harmless():
    import consult_query as cq

    with cq._pending_retriggers_lock:
        cq._pending_retriggers.clear()
    cq._discard_served_retriggers("email", ["D@example.com"])
    cq._discard_served_retriggers("email", [])
    cq._discard_served_retriggers("email", None)


def test_a_replacement_dispatched_before_the_send_still_skips_the_duplicate():
    """★[外審第 4 輪] 派出去之後才寄成功的那個空窗★

    `_drain_pending_retriggers()` 是【先把佇列複製走並清空、才啟動補跑 worker】。
    若舊 worker 剛好在這個空窗裡寄成功，`_discard_served_retriggers()` 面對的是空
    佇列，什麼都拿不掉 → 那個已經派出去的補跑照樣執行、照樣再寄一封。
    墓碑（tombstone）讓補跑 worker 在真正做事之前自己發現。
    """
    import consult_query as cq

    with cq._pending_retriggers_lock:
        cq._pending_retriggers.clear()
        cq._served_recipients_recent.clear()
    try:
        # 模擬：補跑已被派出（佇列已清空），舊 worker 這時才寄成功
        cq._discard_served_retriggers("email", ["D@example.com"])   # 拿不到任何東西
        cq._note_served_recipients(["D@example.com"])
        assert cq._unserved_recipients(["d@example.com"]) == [], (
            "★補跑 worker 必須查得出「這個人剛剛已經收到了」★")
    finally:
        with cq._pending_retriggers_lock:
            cq._served_recipients_recent.clear()


def test_only_the_already_served_recipients_are_dropped():
    """★[外審第 5 輪] 不可做 all-or-nothing 判斷★

    佇列是 `[D, E]`、舊 worker 已經寄給 D 但 E 還沒收到 —— 上一版回「不是全部」
    就拿原名單整批補跑，D 還是收到兩封。逐人過濾才對：
    E 照寄（少寄給等結果的醫師比多寄嚴重）、D 不重複。
    """
    import consult_query as cq

    with cq._pending_retriggers_lock:
        cq._served_recipients_recent.clear()
    try:
        cq._note_served_recipients(["D@example.com"])
        assert cq._unserved_recipients(
            ["D@example.com", "E@example.com"]) == ["E@example.com"], (
            "★D 不可再寄、E 必須照寄★")
    finally:
        with cq._pending_retriggers_lock:
            cq._served_recipients_recent.clear()


def test_the_tombstone_expires():
    """墩碑有 TTL：不可讓「三小時前寄過」永久擋掉之後的正常觸發。"""
    import time as _t

    import consult_query as cq

    with cq._pending_retriggers_lock:
        cq._served_recipients_recent.clear()
        cq._served_recipients_recent["d@example.com"] = (
            _t.time() - cq._SERVED_TOMBSTONE_TTL_SEC - 10)
    try:
        assert cq._unserved_recipients(["D@example.com"]) == ["D@example.com"]
    finally:
        with cq._pending_retriggers_lock:
            cq._served_recipients_recent.clear()


def test_an_empty_tombstone_drops_nobody():
    import consult_query as cq

    with cq._pending_retriggers_lock:
        cq._served_recipients_recent.clear()
    assert cq._unserved_recipients(["D@example.com"]) == ["D@example.com"]
    assert cq._unserved_recipients([]) == []
    assert cq._unserved_recipients(None) is None, "非 email 觸發不介入"


def test_an_email_retrigger_without_named_recipients_is_still_dispatched():
    """★[外審第 6 輪] `override is None` 的 email 補跑不可被吃掉★

    email 觸發若解析不出寄件人（malformed From），`override_recipients` 就是 None，
    而 `_do_full_job` 會退回設定裡的 `email_trigger_recipients`。我上一版用
    `if not send_to: continue` 把 None 一併當成「沒人要寄」→ 那批收件人永遠收不到
    結果，而觸發者還被去重窗卡著。墓碑只能對【指名的收件人】逐人比對。
    """
    import inspect

    import consult_query as cq
    drain = inspect.getsource(cq._drain_pending_retriggers)
    assert 'label == "email" and override is not None' in drain, (
        "★None 必須原樣派送，交給 _do_full_job 用設定收件人★")
    # 純函式層面也釘住：None 進 None 出（完全不介入）
    assert cq._unserved_recipients(None) is None


def test_the_tombstone_never_blocks_a_fresh_trigger_only_a_retrigger():
    """★墩碑只能管【補跑】★

    我第一版把檢查無條件放在 `_do_full_job` 開頭 —— 那會連【正常的新觸發】
    也一起擋掉（實測弄紅了 test_consult_decision_logic 的四支既有測試）。
    醫師在 TTL 內親自再寄一次觸發信，那是新的請求，必須照跑。
    """
    import inspect

    import consult_query as cq
    src = inspect.getsource(cq._do_full_job)
    assert "if from_retrigger and trigger_label ==" in src, (
        "_do_full_job 裡的墩碑檢查必須以 from_retrigger 為前提")
    drain = inspect.getsource(cq._drain_pending_retriggers)
    assert "_unserved_recipients(override)" in drain
    assert drain.index("_unserved_recipients(override)") <         drain.index("trigger_job_async(label"), "要在派出去【之前】檢查"


def test_the_retrigger_worker_rechecks_after_taking_the_flow_lock():
    """★[外審第 5 輪] drain 的「看墩碑 → 派送」不是原子的★

    舊 worker 可能在那兩步之間才寄成功。拿到 `_flow_lock` 代表舊 worker 已經
    完全結束（它在最外層 finally 才釋放），此刻的墩碑才是最終狀態。
    """
    import inspect

    import consult_query as cq
    src = inspect.getsource(cq._do_full_job)
    assert (src.index("_flow_lock.acquire(blocking=False)")
            < src.index("if from_retrigger and trigger_label ==")),         "重查要在【拿到 _flow_lock 之後】"


def test_the_successful_send_path_discards_the_served_recipients():
    """檢查點必須在【寄成功之後】—— 沒寄出去就不可以把補跑取消掉。"""
    import inspect

    import consult_query as cq
    # ★用 AST 找呼叫位置,不要比對參數的字面寫法★
    #   [2026-08-05] 原本是 `src.index("send_via_smtp(shot, subject")` ——
    #   批次W 把參數換成 `delivery.attachment, delivery.subject` 之後這一支
    #   就 ValueError 了。它要守的不變量是【順序】,與參數怎麼寫無關。
    import ast
    import textwrap
    src = inspect.getsource(cq._do_full_job)
    tree = ast.parse(textwrap.dedent(src))
    at = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            at.setdefault(node.func.id, node.lineno)
    assert at["send_via_smtp"] < at["_discard_served_retriggers"], (
        "撤銷補跑要在寄出【之後】")
    assert i_return_of_success(src) > at["_discard_served_retriggers"]


def i_return_of_success(src: str) -> int:
    """成功路徑那個 return 的行號(相對於函式起點)。"""
    import textwrap as _tw
    for i, line in enumerate(_tw.dedent(src).splitlines(), 1):
        if "return  # 成功就跳出" in line:
            return i
    raise AssertionError("找不到成功路徑的 return")


def test_consult_checks_before_sending_not_after():
    """檢查點必須在 send 之前 —— 寄出去就收不回來了。"""
    import inspect

    import consult_query as cq
    import ast
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(cq._do_full_job)))
    raises = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Raise)
              and isinstance(n.exc, ast.Call)
              and getattr(n.exc.func, "id", "") == "JobSuperseded"]
    sends = [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Call)
             and getattr(n.func, "id", "") == "send_via_smtp"]
    assert raises and sends
    assert max(raises) < min(sends), "supersede 檢查要在寄信【之前】"
