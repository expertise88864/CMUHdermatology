# -*- coding: utf-8 -*-
"""[任務 #57 outbox] UNKNOWN 回查：帳本宣稱的那條路，過去一個呼叫端都沒有。

`delivery_ledger` 的模組 docstring 寫著：

> UNKNOWN 就誠實地記成 UNKNOWN，之後用 Message-ID 回查寄件備份把它收斂成
> CONFIRMED 或 FAILED。

而 `resolve_unknown()` 在生產程式碼裡**沒有任何呼叫端**。後果不是「少一個功能」：

* `LIVE_STATES` 含 UNKNOWN，`has_live_delivery()` 因此永遠回 True
  → **同一批會診永遠不會再寄**；
* 也永遠不會有人知道那封信到底送到了沒有。

宣稱與實作不符，而且是往「安靜地漏寄」的方向。這裡把那條路接起來並逐條測。

★三態不可以被摺成兩態★
`find_message_in_sent` 回 None＝查不出來。摺成「沒寄到」→ 重寄一封已經送達的
信；摺成「有寄到」→ 把真正的漏寄吞掉。兩個方向都錯。
"""
import importlib

import pytest

cq = importlib.import_module("consult_query")
imap_reader = importlib.import_module("cmuh_common.imap_reader")


class _Led:
    """只實作這個流程用得到的那幾個方法。"""

    def __init__(self, records, stuck=None):
        self._recs = list(records)
        self._stuck = list(stuck or [])
        self.resolved = []
        self.raise_on_unresolved = False

    def unresolved(self):
        if self.raise_on_unresolved:
            raise RuntimeError("讀不到")
        return list(self._recs)

    def stuck_submitting(self, older_than_sec=600.0):
        """★[2026-08-09] 假帳本要跟上生產介面★

        回查 worker 現在同時消費 `unresolved()` 與 `stuck_submitting()`。
        假帳本少一個方法，測到的就是 AttributeError 而不是被測的行為。
        """
        if self.raise_on_unresolved:
            raise RuntimeError("讀不到")
        return list(self._stuck)

    def resolve_unknown(self, did, *, delivered, note=""):
        self.resolved.append((did, delivered))
        return "confirmed" if delivered else "transient_refused"


def _rec(did="d1", msgid="<a@b>", age=3600.0, now=10_000.0, subject="會診"):
    return {"delivery_id": did, "message_id": msgid,
            "created_at": now - age, "subject": subject}


@pytest.fixture(autouse=True)
def _reset_throttle():
    cq._last_reconcile_ts = 0.0
    yield
    cq._last_reconcile_ts = 0.0


def _run(monkeypatch, led, answers, now=10_000.0, recs=None):
    monkeypatch.setattr(cq, "_get_ledger", lambda: led)
    seen = []

    def _finder(msgid):
        seen.append(msgid)
        a = answers.pop(0) if answers else None
        if isinstance(a, Exception):
            raise a
        return a

    n = cq._reconcile_unknown_deliveries(now=now, finder=_finder)
    return n, seen


def test_found_in_sent_settles_as_delivered(monkeypatch):
    led = _Led([_rec()])
    n, seen = _run(monkeypatch, led, [True])
    assert n == 1 and seen == ["<a@b>"]
    assert led.resolved == [("d1", True)]


def test_definitely_absent_settles_as_not_delivered(monkeypatch):
    led = _Led([_rec()])
    n, _seen = _run(monkeypatch, led, [False])
    assert n == 1
    assert led.resolved == [("d1", False)], (
        "確定查無就要收斂成「沒寄出去」，否則它會永遠卡在 UNKNOWN、"
        "把同一批會診也一起擋住")


def test_an_unknown_answer_changes_nothing(monkeypatch):
    """★核心★ 查不出來 → 什麼都不做，下一輪再試。"""
    led = _Led([_rec()])
    n, _seen = _run(monkeypatch, led, [None])
    assert n == 0
    assert led.resolved == [], (
        "★把「查不出來」當成一個確定的答案★ —— 這正是本專案反覆犯的那個病灶")


def test_an_exception_from_the_lookup_changes_nothing(monkeypatch):
    led = _Led([_rec()])
    n, _seen = _run(monkeypatch, led, [RuntimeError("IMAP 掛了")])
    assert n == 0 and led.resolved == []


def test_a_young_record_is_not_looked_up(monkeypatch):
    """寄件備份要一點時間才出現；太早查到「沒有」會誤判成沒寄出去。"""
    led = _Led([_rec(age=cq._RECONCILE_MIN_AGE_SEC - 1)])
    n, seen = _run(monkeypatch, led, [False])
    assert (n, seen, led.resolved) == (0, [], [])


def test_a_record_without_a_message_id_is_skipped(monkeypatch):
    """沒有 Message-ID 就問不出答案 —— 不可以拿空字串去 SEARCH。"""
    led = _Led([_rec(msgid="")])
    n, seen = _run(monkeypatch, led, [False])
    assert (n, seen, led.resolved) == (0, [], [])


def test_the_throttle_does_not_tick_when_there_is_nothing_to_do(monkeypatch):
    """★沒事做的輪次不可以推進節流時間戳★

    推進了的話，剛剛才成熟的那一筆要再等一整個節流窗口 —— 而每 3 分鐘一輪的
    常駐模式會讓它幾乎永遠等不到（每一輪都把時間戳往前推）。
    """
    led = _Led([_rec(age=1.0)])          # 還沒成熟
    _run(monkeypatch, led, [True])
    assert cq._last_reconcile_ts == 0.0, "沒事做卻推進了節流時間戳"
    led2 = _Led([_rec()])
    n, _s = _run(monkeypatch, led2, [True])
    assert n == 1, "上一輪的空轉把這一輪擋掉了"


def test_the_throttle_blocks_a_second_pass(monkeypatch):
    led = _Led([_rec()])
    n1, _s = _run(monkeypatch, led, [True])
    led2 = _Led([_rec(did="d2")])
    n2, seen2 = _run(monkeypatch, led2, [True], now=10_000.0 + 1.0)
    assert n1 == 1
    assert (n2, seen2) == (0, []), "節流沒生效 → 每 3 分鐘就開一條 IMAP 連線"


def test_the_throttle_lets_a_later_pass_through(monkeypatch):
    """★反方向★ 節流不可以變成「永遠不回查」。"""
    led = _Led([_rec()])
    _run(monkeypatch, led, [True])
    led2 = _Led([_rec(did="d2")])
    n2, _s = _run(monkeypatch, led2, [True],
                  now=10_000.0 + cq._RECONCILE_EVERY_SEC + 1)
    assert n2 == 1


def test_only_a_few_are_looked_up_per_pass(monkeypatch):
    led = _Led([_rec(did="d%d" % i) for i in range(20)])
    n, seen = _run(monkeypatch, led, [True] * 20)
    assert len(seen) == cq._RECONCILE_MAX_PER_PASS == n


def test_a_ledger_that_cannot_be_read_is_not_an_answer(monkeypatch):
    led = _Led([_rec()])
    led.raise_on_unresolved = True
    n, seen = _run(monkeypatch, led, [False])
    assert (n, seen, led.resolved) == (0, [], [])


def test_no_ledger_at_all(monkeypatch):
    monkeypatch.setattr(cq, "_get_ledger", lambda: None)
    assert cq._reconcile_unknown_deliveries(now=1.0, finder=lambda m: True) == 0


def test_the_query_cycle_actually_calls_it():
    """★接上去了才算數★ 這正是原本的缺陷：函式存在，但沒有人呼叫。"""
    import ast
    import inspect
    import textwrap
    src = textwrap.dedent(inspect.getsource(cq._do_full_job))
    names = {n.func.id for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_reconcile_unknown_deliveries" in names, (
        "★回查沒有被接進查詢輪次★ —— 那就跟沒寫一樣")


def test_reconcile_runs_before_the_closeout():
    """結案要用的是【收斂之後】的狀態，所以回查必須排在前面。"""
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(cq._do_full_job)))
    at = {}
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
            at.setdefault(n.func.id, n.lineno)
    assert at["_reconcile_unknown_deliveries"] < \
        at["_close_out_stale_recipient_retries"]


# ── imap_reader.find_message_in_sent 本身 ──────────────────────────────────
class _Conn:
    def __init__(self, select_ok=True, search=("OK", [b"1 2"])):
        self.select_ok = select_ok
        self.search = search
        self.searched = []

    def login(self, *a):
        return ("OK", [b""])

    def select(self, box, readonly=False):
        return ("OK" if self.select_ok else "NO", [b""])

    def uid(self, *args):
        self.searched.append(args)
        return self.search

    def shutdown(self):
        pass

    def close(self):
        pass


def _patch_conn(monkeypatch, conn):
    monkeypatch.setattr(imap_reader, "_load_imap_settings",
                        lambda: {"host": "h", "port": 993,
                                 "username": "u", "password": "p"})
    monkeypatch.setattr(imap_reader.imaplib, "IMAP4_SSL",
                        lambda *a, **k: conn)


def test_a_hit_is_true(monkeypatch):
    c = _Conn()
    _patch_conn(monkeypatch, c)
    assert imap_reader.find_message_in_sent("<a@b>") is True


def test_no_hit_is_false(monkeypatch):
    _patch_conn(monkeypatch, _Conn(search=("OK", [b""])))
    assert imap_reader.find_message_in_sent("<a@b>") is False


def test_no_sent_mailbox_is_unknown_not_false(monkeypatch):
    """★找不到寄件備份 ≠ 信沒寄出去★"""
    _patch_conn(monkeypatch, _Conn(select_ok=False))
    assert imap_reader.find_message_in_sent("<a@b>") is None


def test_a_search_that_did_not_return_ok_is_unknown(monkeypatch):
    _patch_conn(monkeypatch, _Conn(search=("NO", [b""])))
    assert imap_reader.find_message_in_sent("<a@b>") is None


def test_a_connection_failure_is_unknown(monkeypatch):
    monkeypatch.setattr(imap_reader, "_load_imap_settings",
                        lambda: {"host": "h", "port": 993,
                                 "username": "u", "password": "p"})

    def _boom(*a, **k):
        raise OSError("網路掛了")
    monkeypatch.setattr(imap_reader.imaplib, "IMAP4_SSL", _boom)
    assert imap_reader.find_message_in_sent("<a@b>") is None


def test_no_credentials_is_unknown(monkeypatch):
    monkeypatch.setattr(imap_reader, "_load_imap_settings",
                        lambda: {"host": "h", "port": 993,
                                 "username": "u", "password": ""})
    assert imap_reader.find_message_in_sent("<a@b>") is None


@pytest.mark.parametrize("bad", [
    '<a" OR 1=1@b>',              # 提前結束加引號字串
    "<a" + chr(92) + "@b>",       # 反斜線
    "<a" + chr(13) + chr(10) + "A001 LOGOUT@b>",   # 多送一整條 IMAP 指令
    "",
    "<" + "x" * 400 + "@b>",
])
def test_an_unsafe_message_id_never_reaches_the_server(monkeypatch, bad):
    """★注入防線★ 不安全的 Message-ID 連線都不該開。"""
    opened = []
    monkeypatch.setattr(imap_reader, "_load_imap_settings",
                        lambda: opened.append("settings") or {
                            "host": "h", "port": 993,
                            "username": "u", "password": "p"})
    monkeypatch.setattr(imap_reader.imaplib, "IMAP4_SSL",
                        lambda *a, **k: opened.append("conn") or _Conn())
    assert imap_reader.find_message_in_sent(bad) is None
    assert opened == [], f"不安全的 Message-ID 仍然開了連線：{opened}"


def test_the_message_id_is_quoted_in_the_search(monkeypatch):
    c = _Conn()
    _patch_conn(monkeypatch, c)
    imap_reader.find_message_in_sent("<a@b>")
    assert c.searched, "沒有真的送出 SEARCH"
    args = c.searched[0]
    assert args[0] == "search" and args[1:3] == ("HEADER", "Message-ID")
    assert args[3] == '"<a@b>"'


# ── 跨 process：unresolved() 必須從磁碟重讀（外審 P2）─────────────────────
# 這本帳是主程式與會診程式共用的。`unresolved()` 若只讀自己記憶體裡的快照,
# 別的 process 建立的 UNKNOWN 就永遠不會被挑去回查 —— 它一直停在 UNKNOWN,
# 而 UNKNOWN 屬於 LIVE_STATES,於是那個 business_key 也永遠不會再寄。
class TestUnresolvedIsCrossProcess:

    def _led(self, path):
        from cmuh_common.delivery_ledger import DeliveryLedger
        return DeliveryLedger(path=path)

    def test_an_unknown_written_by_another_instance_is_discovered(self, tmp_path):
        """★核心★ A 先起來,B 之後才寫入 UNKNOWN → A 必須看得到。"""
        path = str(tmp_path / "ledger.json")
        a = self._led(path)                      # A 先初始化(快照是空的)
        assert a.unresolved() == []
        b = self._led(path)                      # 另一支程式的帳本實例
        did = b.begin(business_key="k1", category="test",
                      recipients=["x@y.tw"], subject="s", message_id="<m@x>")
        b.mark_submitting(did)
        b.settle(did, unknown=True)
        assert [r["delivery_id"] for r in b.unresolved()] == [did]
        assert [r["delivery_id"] for r in a.unresolved()] == [did], (
            "★A 只看自己的記憶體快照★ 別的 process 寫的 UNKNOWN 永遠不會被回查,"
            "那個 business_key 也就永遠不會再寄")

    def test_a_ledger_that_cannot_be_refreshed_raises(self, tmp_path, monkeypatch):
        """★讀不到就拋,不回空清單★ 空清單會被讀成「沒有待回查的」。"""
        from cmuh_common.delivery_ledger import LedgerUnavailable
        path = str(tmp_path / "ledger.json")
        led = self._led(path)
        monkeypatch.setattr(type(led), "_refresh_locked", lambda self: False)
        with pytest.raises(LedgerUnavailable):
            led.unresolved()

    def test_the_reconciler_does_nothing_when_the_ledger_is_unreadable(
            self, tmp_path, monkeypatch):
        """★端對端★ 拋例外要被回查流程接住 → 這一輪什麼都不做,下一輪再試。"""
        from cmuh_common.delivery_ledger import LedgerUnavailable
        path = str(tmp_path / "ledger.json")
        led = self._led(path)

        def _boom(self):
            raise LedgerUnavailable("讀不到")

        monkeypatch.setattr(type(led), "unresolved", _boom)
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        called = []
        n = cq._reconcile_unknown_deliveries(
            now=10_000.0, finder=lambda m: called.append(m) or False)
        assert (n, called) == (0, []), "讀不到帳本卻還是去回查/收斂了"


    def test_the_other_readers_are_cross_process_too(self, tmp_path):
        """★[2026-08-09] 同一個形狀的另外兩個讀取方法★

        `needs_recipient_retry()` 被 `_close_out_stale_recipient_retries()` 用來
        找「還沒收到的收件人」。只讀自己 process 的快照 → `main.py` 記下的
        待補寄收件人永遠不會被結案、也永遠不會告警 —— 那正是那個方法存在的理由。

        （在會診程式裡它剛好排在 `unresolved()` 之後而「碰巧」是新的，
         但那是靠另一個方法的副作用，不是它自己的性質。）
        """
        path = str(tmp_path / "ledger.json")
        a = self._led(path)
        assert a.needs_recipient_retry() == []
        b = self._led(path)
        did = b.begin(business_key="k9", category="test",
                      recipients=["ok@x.tw", "bad@x.tw"], subject="s")
        b.mark_submitting(did)
        b.settle(did, refused={"bad@x.tw": (452, b"full")})
        assert [d for d, _ in b.needs_recipient_retry()] == [did]
        assert [d for d, _ in a.needs_recipient_retry()] == [did], (
            "★A 只看自己的記憶體快照★ 別的 process 記下的待補寄收件人"
            "永遠不會被結案、也永遠不會告警")

    def test_the_other_readers_raise_instead_of_returning_empty(
            self, tmp_path, monkeypatch):
        """★讀不到就拋★ 空清單會被讀成「沒有人在等補寄／沒有卡住的」。"""
        from cmuh_common.delivery_ledger import LedgerUnavailable
        led = self._led(str(tmp_path / "ledger.json"))
        monkeypatch.setattr(type(led), "_refresh_locked", lambda self: False)
        for fn in (led.needs_recipient_retry, led.stuck_submitting):
            with pytest.raises(LedgerUnavailable):
                fn()

    def test_the_closeout_does_nothing_when_the_ledger_is_unreadable(
            self, tmp_path, monkeypatch):
        """★端對端★ 拋出去要被結案流程接住 —— 不可以變成未捕捉例外。"""
        from cmuh_common.delivery_ledger import LedgerUnavailable
        led = self._led(str(tmp_path / "ledger.json"))

        def _boom(self):
            raise LedgerUnavailable("讀不到")

        monkeypatch.setattr(type(led), "needs_recipient_retry", _boom)
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        alerts = []
        monkeypatch.setattr(cq, "_alert_missed_recipients",
                            lambda *a, **k: alerts.append(a))
        cq._close_out_stale_recipient_retries()      # 不可以往上拋
        assert alerts == [], "讀不到帳本卻還是告警了（會誤報漏收）"


# ── [批次 Z / 外審 P1-04] 卡住的 SUBMITTING 也要回查 ─────────────────────
# `begin()` 已經把 SUBMITTING 落地；SMTP 送出之後、settle 之前 crash，那一筆就
# **永久**停在 SUBMITTING。而 SUBMITTING 屬於 `LIVE_STATES` —— 一旦把帳本接成
# 寄送閘門，它會**永久擋住同一批會診**。★所以這是接閘門的前置條件★。
def test_a_stuck_submitting_record_is_reconciled(monkeypatch):
    """★核心★ 卡住的 SUBMITTING 與 UNKNOWN 走同一個回查 worker。"""
    led = _Led([], stuck=[_rec(did="s1")])
    n, seen = _run(monkeypatch, led, [True])
    assert (n, seen) == (1, ["<a@b>"])
    assert led.resolved == [("s1", True)]


def test_both_sources_are_reconciled_together(monkeypatch):
    led = _Led([_rec(did="u1")], stuck=[_rec(did="s1")])
    n, seen = _run(monkeypatch, led, [True, True])
    assert n == 2 and len(seen) == 2
    assert {d for d, _ok in led.resolved} == {"u1", "s1"}


def test_a_stuck_record_that_is_absent_from_sent_is_settled_as_failed(
        monkeypatch):
    """確定查無 → 收斂成「沒寄出去」，否則它永遠卡在 SUBMITTING。"""
    led = _Led([], stuck=[_rec(did="s1")])
    n, _seen = _run(monkeypatch, led, [False])
    assert (n, led.resolved) == (1, [("s1", False)])


def test_an_unknown_answer_leaves_a_stuck_record_alone(monkeypatch):
    """★三態不可以摺成兩態★ 查不出來就維持現狀，下一輪再試。"""
    led = _Led([], stuck=[_rec(did="s1")])
    n, _seen = _run(monkeypatch, led, [None])
    assert (n, led.resolved) == (0, [])


def test_a_young_submitting_record_is_left_alone(monkeypatch):
    """正常的 SUBMITTING 只存在幾秒鐘 —— 太新的不可以當成卡住。"""
    led = _Led([], stuck=[])          # 生產由 older_than_sec 過濾
    n, seen = _run(monkeypatch, led, [True])
    assert (n, seen) == (0, [])


def test_the_worker_asks_for_aged_submitting_only():
    """★要真的傳 `older_than_sec`★ 不傳就會拿到剛剛才建立的那些。"""
    import ast
    import inspect
    import textwrap
    src = textwrap.dedent(inspect.getsource(cq._reconcile_unknown_deliveries))
    calls = [n for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
             and n.func.attr == "stuck_submitting"]
    assert calls, "★回查 worker 沒有消費 stuck_submitting★ —— 那個 API 等於不存在"
    kws = {k.arg for c in calls for k in c.keywords}
    assert "older_than_sec" in kws, "沒有指定年齡門檻，會拿到剛建立的 SUBMITTING"


def test_an_unreadable_ledger_still_does_nothing(monkeypatch):
    """兩個來源都讀不到時，一樣什麼都不做（不可以只擋一個來源）。"""
    led = _Led([_rec()], stuck=[_rec(did="s1")])
    led.raise_on_unresolved = True
    n, seen = _run(monkeypatch, led, [False])
    assert (n, seen, led.resolved) == (0, [], [])


def test_a_stuck_record_is_not_starved_by_many_unknowns(monkeypatch):
    """★[外審 P2] 餓死★ 兩個來源直接接起來再切上限，SUBMITTING 永遠輪不到。

    UNKNOWN 產生的速度可能超過「每 10 分鐘 5 筆」的消化率。餓死的那一筆
    一旦接上寄送閘門，會**永久擋住它的 business key**。
    ★依年齡全域排序★ 最久沒收斂的先查，兩個來源自然公平。
    """
    now = 10_000.0
    # 8 筆比較新的 UNKNOWN + 1 筆最舊的 SUBMITTING
    unknowns = [_rec(did="u%d" % i, age=1000.0, now=now) for i in range(8)]
    stuck = [_rec(did="s1", age=9000.0, now=now)]
    led = _Led(unknowns, stuck=stuck)
    n, seen = _run(monkeypatch, led, [True] * 9, now=now)
    assert n == cq._RECONCILE_MAX_PER_PASS
    assert "s1" in {d for d, _ok in led.resolved}, (
        "★最舊的 SUBMITTING 被一堆較新的 UNKNOWN 餓死★")


def test_the_oldest_records_are_queried_first(monkeypatch):
    """全域依年齡排序 —— 不是「先 UNKNOWN 再 SUBMITTING」。"""
    now = 10_000.0
    led = _Led([_rec(did="new_u", age=700.0, now=now)],
               stuck=[_rec(did="old_s", age=8000.0, now=now)])
    n, _seen = _run(monkeypatch, led, [True, True], now=now)
    assert n == 2
    assert [d for d, _ok in led.resolved][0] == "old_s", (
        "最舊的沒有先被查 —— 排序沒生效")
