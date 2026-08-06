# -*- coding: utf-8 -*-
"""關不掉的 session 要掛帳重試，不可以連參照一起丟（外審第 4 輪 P1-03）。

【為什麼這是 P1】
`_session_close` 是先 `_psession = None`、再去關窗。關失敗的時候（HIS 不回應
WM_CLOSE、hwnd 身分對不上、modal 擋著），我們已經把**唯一**認得那個 session 的
參照丟掉了。而且底下那幾條後路對它都無效：

  * systemftp 是啟動器型行程（實機證實）→ `sess.hproc` 早已 signaled →
    `TerminateProcess` 那一段根本不執行
  * `_verified_owned_pids` 只剩一個已死的 root → `close_pids` 關不到東西
  * `_kill_systemftp` 在 2026-08-04 已經改成不再 taskkill

淨結果：一個【仍然登入中】的 HIS 留在隱藏桌面上，沒有任何程式碼認得它；
下一輪看到「沒有 session」就再冷啟動登入一次。每失敗一次就多一個。

所以關不掉時把 session 掛在帳上，每輪查詢前重試，並且告警。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402


class _Sess:
    def __init__(self, pid=1860, hwnd=111):
        self.pid = pid
        self.main_hwnd = hwnd
        self.main_pid = pid
        self.main_class = cq.MAIN_CLASS
        self.our_pids = {pid}


def _clear_ledger():
    with cq._unclosed_lock:
        cq._unclosed_sessions.clear()


def test_a_failed_close_is_recorded(caplog):
    import logging as _lg
    _clear_ledger()
    s = _Sess()
    with caplog.at_level(_lg.ERROR):
        cq._note_unclosed_session(s, "測試")
    assert cq._unclosed_sessions == [s], "關不掉卻沒掛帳 → 這個 session 從此無人認領"
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "關不掉" in msgs and "仍登入中" in msgs, msgs
    _clear_ledger()


def test_the_same_session_is_not_recorded_twice():
    _clear_ledger()
    s = _Sess()
    cq._note_unclosed_session(s, "第一次")
    cq._note_unclosed_session(s, "第二次")
    assert len(cq._unclosed_sessions) == 1
    _clear_ledger()


def test_retry_closes_and_removes_it(monkeypatch):
    _clear_ledger()
    s = _Sess()
    cq._note_unclosed_session(s, "測試")
    monkeypatch.setattr(cq, "_close_session_windows", lambda _s: True)

    assert cq._retry_unclosed_sessions() == 0
    assert cq._unclosed_sessions == [], "關掉了卻還留在帳上"
    _clear_ledger()


def test_a_still_failing_session_stays_on_the_ledger(monkeypatch):
    """★關鍵★ 還是關不掉 → 留在帳上，下一輪繼續試。不可以「試過一次就算了」。"""
    _clear_ledger()
    s = _Sess()
    cq._note_unclosed_session(s, "測試")
    monkeypatch.setattr(cq, "_close_session_windows", lambda _s: False)

    assert cq._retry_unclosed_sessions() == 1
    assert cq._unclosed_sessions == [s]
    _clear_ledger()


def test_a_raising_close_does_not_drop_it(monkeypatch):
    """重試時炸掉 ≠ 關掉了。例外不可以變成「從帳上消失」。"""
    _clear_ledger()
    s = _Sess()
    cq._note_unclosed_session(s, "測試")

    def _boom(_s):
        raise OSError("PostMessage 失敗")
    monkeypatch.setattr(cq, "_close_session_windows", _boom)

    assert cq._retry_unclosed_sessions() == 1
    assert cq._unclosed_sessions == [s]
    _clear_ledger()


def test_only_the_ones_that_closed_are_removed(monkeypatch):
    """一批裡有成功有失敗 → 只移除成功的那些。"""
    _clear_ledger()
    good, bad = _Sess(pid=1), _Sess(pid=2)
    cq._note_unclosed_session(good, "a")
    cq._note_unclosed_session(bad, "b")
    monkeypatch.setattr(cq, "_close_session_windows",
                        lambda s: s.pid == 1)

    assert cq._retry_unclosed_sessions() == 1
    assert cq._unclosed_sessions == [bad]
    _clear_ledger()


def test_nothing_is_ever_dropped_no_matter_how_many(monkeypatch):
    """★一筆都不丟★（外審第 5 輪 P1-03）

    ★這一支原本把缺陷釘成了通過條件★
    舊版是 `test_hitting_the_cap_does_not_discard_anything`，只驗「舊的 8 筆
    沒被擠掉」—— 而程式碼丟掉的是【新來的第 9 筆】。註解寫著「不丟掉任何一筆」，
    測試也只檢查了不會被丟掉的那一半，缺陷剛好落在兩者之間。

    現在沒有上限：帳上只要有一筆，`_acquire_session` 就禁止冷啟動，
    所以這個清單不可能因為我們自己再開新 session 而增長。
    """
    monkeypatch.setattr(cq, "_alert_unmanaged_session", lambda *a, **k: None)
    _clear_ledger()
    made = [_Sess(pid=i) for i in range(20)]
    for s in made:
        cq._note_unclosed_session(s, "測試")

    assert cq._unclosed_sessions == made, (
        f"掛帳只留下 {len(cq._unclosed_sessions)}/{len(made)} 筆 —— 被丟掉的那些無人認領")
    assert not hasattr(cq, "_MAX_UNCLOSED"), (
        "還留著會丟棄新項目的上限")
    _clear_ledger()


def test_the_first_failure_alerts_the_developer(monkeypatch):
    """★[外審第 5 輪 P2-04] 不會自己好的事情要主動說★

    註解一路寫著「掛帳＋告警」，實際上只有 logging.error —— 而使用者看不到
    隱藏桌面、也不會去翻 log。
    """
    sent = []
    monkeypatch.setattr(cq, "_alert_unmanaged_session",
                        lambda depth, reason: sent.append((depth, reason)))
    _clear_ledger()
    cq._note_unclosed_session(_Sess(), "關不掉")
    assert sent and sent[0][0] == 1, "第一筆掛帳沒有走告警通道"
    _clear_ledger()


def test_the_alert_is_throttled(monkeypatch):
    """這種狀況不會自己好，但也不該每 3 分鐘寄一封信。"""
    calls = []
    monkeypatch.setattr(cq.threading, "Thread",
                        lambda target=None, **k: type(
                            "T", (), {"start": lambda s: calls.append(1)})())
    monkeypatch.setattr(cq, "_unmanaged_alert_at", 0.0)
    cq._alert_unmanaged_session(1, "第一次")
    cq._alert_unmanaged_session(2, "馬上又一次")
    assert len(calls) == 1, f"節流失效，寄了 {len(calls)} 封"


# ── 接線（這個 session 反覆出事的形狀）─────────────────────────────────────
def test_teardown_records_a_failed_close():
    """★接線★ `_terminate_session_process` 必須把失敗結果掛帳。

    上面幾支都直接呼叫 `_note_unclosed_session`。若 teardown 只是呼叫
    `_close_session_windows(sess)` 而不看回傳值（原本就是這樣寫的），
    那些測試照樣全綠，實機卻一個都不會掛帳。
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(cq._terminate_session_process)))
    # 必須存在「if not _close_session_windows(...): _note_unclosed_session(...)」
    ok = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        test_calls = {n.func.id for n in ast.walk(node.test)
                      if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        body_calls = {n.func.id for n in ast.walk(node) if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Name)}
        if ("_close_session_windows" in test_calls
                and "_note_unclosed_session" in body_calls):
            ok = True
    assert ok, "★關窗的回傳值沒被檢查★ 關不掉不會掛帳，那個 HIS 無人認領"


def test_every_cycle_goes_through_the_gate():
    """★接線★ 掛帳了但沒有人重試 = 只是換一個地方遺忘它。"""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(cq._acquire_session)))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_ensure_no_unmanaged_sessions" in called, (
        "每輪取用 session 之前沒有經過掛帳閘門")

    gate = ast.parse(textwrap.dedent(
        inspect.getsource(cq._ensure_no_unmanaged_sessions)))
    gate_calls = {n.func.id for n in ast.walk(gate)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_retry_unclosed_sessions" in gate_calls


# ── 閘門：有殘留就不准再登入（外審第 5 輪 P1-02）────────────────────────────
def test_a_remaining_unclosed_session_blocks_the_gate(monkeypatch):
    """★核心★ 關不掉的 session 還在 → 不可以再開一個登入。

    舊版把 `_retry_unclosed_sessions()` 的回傳值丟掉，於是掛帳只做到
    「我知道有一個關不掉的 session」，做不到它註解宣稱的「不先收就會同時有
    兩個登入中的 HIS」。
    """
    monkeypatch.setattr(cq, "_retry_unclosed_sessions", lambda: 1)
    try:
        cq._ensure_no_unmanaged_sessions()
    except cq.UnmanagedSessionError as e:
        assert "不建立新登入" in str(e)
        return
    raise AssertionError("★仍有關不掉的 session，閘門卻放行★ 會變成兩個登入")


def test_a_clean_ledger_lets_the_cycle_through(monkeypatch):
    """★反方向:不可以變成永遠擋住★ 沒有殘留就要放行。"""
    monkeypatch.setattr(cq, "_retry_unclosed_sessions", lambda: 0)
    cq._ensure_no_unmanaged_sessions()          # 不拋例外即通過


def test_a_retry_that_raises_still_blocks(monkeypatch):
    """重試本身出錯 → 帳上狀態不明 → 一樣不可以再登入（不知道不等於沒有）。"""
    def _boom():
        raise OSError("列舉失敗")
    monkeypatch.setattr(cq, "_retry_unclosed_sessions", _boom)
    monkeypatch.setattr(cq, "_alert_unmanaged_session", lambda *a, **k: None)
    _clear_ledger()
    cq._note_unclosed_session(_Sess(), "關不掉")
    try:
        cq._ensure_no_unmanaged_sessions()
    except cq.UnmanagedSessionError:
        _clear_ledger()
        return
    _clear_ledger()
    raise AssertionError("重試出錯時閘門放行了")


def test_no_cold_start_while_the_ledger_is_dirty(monkeypatch):
    """★行為★ 閘門擋下時，`_cold_start_session` 必須一次都不被呼叫。

    只測「閘門會拋例外」不夠 —— 要證明 `_acquire_session` 真的因此沒有去登入。
    """
    calls = []
    monkeypatch.setattr(cq, "_cold_start_session",
                        lambda cfg: calls.append(cfg))
    monkeypatch.setattr(cq, "_retry_unclosed_sessions", lambda: 1)
    monkeypatch.setattr(cq, "_psession", None)

    try:
        cq._acquire_session({})
    except cq.UnmanagedSessionError:
        pass
    assert calls == [], "★帳上還有關不掉的 session，卻又登入了一次★"


# ── 閘門的邊界:擋「新登入」,不擋「重用健康 session」(外審 2026-08-06 P1-02)──
def _healthy_session(monkeypatch, retries_remaining=1):
    """裝一個【健康、未被租用】的常駐 session,外加一本髒帳。"""
    sess = _Sess()
    sess.in_use = False
    sess.started_at = 1000.0
    monkeypatch.setattr(cq, "_psession", sess)
    monkeypatch.setattr(cq, "_session_death_reason", lambda s: "")
    monkeypatch.setattr(cq._keepalive, "session_needs_restart",
                        lambda started, now: False)
    monkeypatch.setattr(cq, "_retry_unclosed_sessions",
                        lambda: retries_remaining)      # 髒帳:還有關不掉的
    monkeypatch.setattr(cq, "_cold_start_session",
                        lambda cfg: (_ for _ in ()).throw(
                            AssertionError("健康 session 還在,不該冷啟動")))
    return sess


def test_healthy_session_is_reusable_even_with_a_dirty_ledger(monkeypatch):
    """★核心(P1-02)★ 帳上還有關不掉的舊 session,但常駐 session 本身健康 →
    必須能直接重用,不可以被閘門擋掉。

    【為什麼是 P1】閘門要防的是「在有未管理 session 時【再開一個】新登入」。
    舊版把它放在 `_acquire_session` 最前面,於是 15 分鐘窗口放行一次、冷啟動成功、
    新 session 健康地留在主畫面之後,下一輪(3 分鐘後)還沒檢查那個健康 session
    就先撞閘門 → 拿不到 keepalive → 很可能在院方 5 分鐘閒置上限後被登出,
    使那次好不容易的恢復完全白費。
    """
    cq._unmanaged_since = 0.0
    sess = _healthy_session(monkeypatch)
    got = cq._acquire_session({})               # 不可拋 UnmanagedSessionError
    assert got is sess, "健康的常駐 session 必須被重用"
    assert got.in_use is True, "重用時必須取得租約"
    cq._unmanaged_since = 0.0


def test_reuse_path_still_retries_closing_the_ledger(monkeypatch):
    """重用路徑【只是不擋】,清理不可以跟著消失 —— 否則有健康 session 期間
    那些關不掉的殘留永遠不會再被嘗試關閉,會一直留在隱藏桌面上。"""
    cq._unmanaged_since = 0.0
    tries = []
    sess = _Sess()
    sess.in_use = False
    sess.started_at = 1000.0
    monkeypatch.setattr(cq, "_psession", sess)
    monkeypatch.setattr(cq, "_session_death_reason", lambda s: "")
    monkeypatch.setattr(cq._keepalive, "session_needs_restart",
                        lambda started, now: False)
    monkeypatch.setattr(cq, "_retry_unclosed_sessions",
                        lambda: (tries.append(1), 1)[1])
    cq._acquire_session({})
    assert tries, "★重用路徑沒有重試關閉殘留★ 殘留會永遠留在隱藏桌面"
    cq._unmanaged_since = 0.0


def test_cold_start_is_still_blocked_after_a_dead_session(monkeypatch):
    """反方向:session 死了要冷啟動時,閘門【仍然】必須擋(這才是它的職責)。"""
    cq._unmanaged_since = 0.0
    calls = []
    sess = _Sess()
    sess.in_use = False
    sess.started_at = 1000.0
    monkeypatch.setattr(cq, "_psession", sess)
    monkeypatch.setattr(cq, "_session_death_reason", lambda s: "主畫面不見了")
    monkeypatch.setattr(cq, "_session_close_if_current", lambda s, r: None)
    monkeypatch.setattr(cq, "_retry_unclosed_sessions", lambda: 1)
    monkeypatch.setattr(cq, "_cold_start_session", lambda cfg: calls.append(cfg))
    try:
        cq._acquire_session({})
    except cq.UnmanagedSessionError:
        pass
    assert calls == [], "★session 已死要開新登入,帳上仍髒 → 必須擋★"
    cq._unmanaged_since = 0.0


def test_the_job_treats_it_as_fatal():
    """接線:這個例外不可以進 backoff 重試（三次只是再撞三次同一道閘門）。"""
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(cq._do_full_job)))
    names = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and node.targets
                and getattr(node.targets[0], "id", "") == "fatal"):
            names |= {n.id for n in ast.walk(node.value)
                      if isinstance(n, ast.Name)}
    assert "UnmanagedSessionError" in names, (
        "UnmanagedSessionError 沒有被列為 fatal → 會白白重試三次")
