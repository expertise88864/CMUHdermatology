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
import ast
import importlib
import io
import os

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
cq = importlib.import_module("consult_query")
dr = importlib.import_module("cmuh_common.delivery_reconcile")
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
    cq._RECONCILER.last_ts = 0.0
    yield
    cq._RECONCILER.last_ts = 0.0


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
    led = _Led([_rec(age=dr.MIN_AGE_SEC - 1)])
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
    assert cq._RECONCILER.last_ts == 0.0, "沒事做卻推進了節流時間戳"
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
                  now=10_000.0 + dr.EVERY_SEC + 1)
    assert n2 == 1


def test_only_a_few_are_looked_up_per_pass(monkeypatch):
    led = _Led([_rec(did="d%d" % i) for i in range(20)])
    n, seen = _run(monkeypatch, led, [True] * 20)
    assert len(seen) == dr.MAX_PER_PASS == n


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
    # ★實作搬到共用模組了（外審 P1-04）→ 要檢查【真的那一份】★
    #   繼續掃 `cq._reconcile_unknown_deliveries`（現在只是個轉呼叫）的話，
    #   這條測試會靜默地變成「掃一個永遠不含 stuck_submitting 的殼」。
    src = textwrap.dedent(inspect.getsource(dr.Reconciler.run_once))
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
    assert n == dr.MAX_PER_PASS
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


# ══ 外審 2026-08-09 P1-04：主程式也必須自己收斂 ═══════════════════════════
class TestBothProgramsReconcile:
    """★寫進這本帳的有兩支程式，收斂的卻只有一支★

    止掛提醒信是【主程式】寄的，走的是同一個 `DeliveryLedger`、同一個檔。
    回查原本只寫在 `consult_query.py` —— 只跑主程式、沒裝會診查詢的診間電腦，
    那些 UNKNOWN 與卡住的 SUBMITTING **永遠不會有人去收斂**。
    一旦把帳本接成寄送閘門，它們會永久擋住同一個 business key 的重寄。
    """

    @staticmethod
    def _main_src(name):
        import ast
        import io
        import os
        path = os.path.join(REPO_ROOT, "src", "main.py")
        return ast.parse(io.open(path, encoding="utf-8").read())

    def test_main_has_a_reconciler_driver(self):
        import ast
        tree = self._main_src("main.py")
        fns = {n.name for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert "_reconcile_alert_deliveries" in fns, (
            "★主程式沒有回查驅動 → 只跑主程式的機器永遠不收斂★")

    def test_main_actually_calls_it(self):
        """★有函式不算數，要有呼叫端★（wired-up-or-it-does-not-exist）"""
        import ast
        tree = self._main_src("main.py")
        called = {n.func.id for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "_reconcile_alert_deliveries" in called, (
            "★定義了卻沒有人呼叫 —— 那個 docstring 的宣稱是假的★")

    def test_main_uses_the_shared_implementation(self):
        """兩邊必須是【同一份】實作，不可以各寫一份（會漂）。"""
        import io
        import os
        text = io.open(os.path.join(REPO_ROOT, "src", "main.py"),
                       encoding="utf-8").read()
        assert "delivery_reconcile" in text, "主程式沒有用共用模組"

    def test_the_consult_wrapper_delegates(self):
        """會診端只剩轉呼叫 —— 實作不可以又長回本檔（兩份會漂）。"""
        import ast
        import inspect
        import textwrap
        src = textwrap.dedent(inspect.getsource(cq._reconcile_unknown_deliveries))
        attrs = {n.func.attr for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert attrs == {"run_once"}, f"沒有單純轉呼叫共用實作：{attrs}"


class TestCrossProcessThrottle:
    """★節流必須跨 process★

    兩支程式各拿一個記憶體時間戳的話，同一台機器上兩邊會【同時】對同一批
    紀錄開 IMAP、同時 `resolve_unknown()`。而 `_save_once_locked()` 的合併
    規則明文寫著「delivery_id 是全域唯一的，所以兩個 process 不可能改到
    同一筆」—— 兩邊都回查會直接推翻那個前提。
    """

    class _Claimy(_Led):
        def __init__(self, records, claims):
            super().__init__(records)
            self._claims = list(claims)
            self.claim_calls = []

        def claim_reconcile_pass(self, *, now, every_sec):
            self.claim_calls.append((now, every_sec))
            return self._claims.pop(0) if self._claims else False

    def test_a_lost_claim_skips_the_pass(self, monkeypatch):
        led = self._Claimy([_rec()], claims=[False])
        n, seen = _run(monkeypatch, led, [True])
        assert (n, seen) == (0, []), "★沒搶到宣告卻還是去回查了★"
        assert led.claim_calls, "根本沒有去搶跨 process 的宣告"

    def test_a_won_claim_runs_the_pass(self, monkeypatch):
        led = self._Claimy([_rec()], claims=[True])
        n, _seen = _run(monkeypatch, led, [True])
        assert n == 1

    def test_the_claim_is_not_asked_when_there_is_nothing_to_do(self,
                                                                monkeypatch):
        """★沒事做就不要碰跨 process 的鎖★（也不該推進時間戳）"""
        led = self._Claimy([], claims=[True])
        _run(monkeypatch, led, [True])
        assert led.claim_calls == []

    def test_a_broken_claim_falls_back_instead_of_going_silent(self,
                                                              monkeypatch):
        """★宣告壞掉不可以變成永久沉默★

        「搶不到就永遠不回查」會把一個看得見的競態換成一個安靜的漏收斂 ——
        那正是 2026-08-05 事故的形狀（沒有出口的 fail-closed）。
        """
        class _Boom(_Led):
            def claim_reconcile_pass(self, *, now, every_sec):
                raise RuntimeError("鎖壞了")

        led = _Boom([_rec()])
        n, _seen = _run(monkeypatch, led, [True])
        assert n == 1, "★宣告壞掉就完全不回查 —— 沒有出口的 fail-closed★"

    def test_an_old_ledger_without_the_method_still_reconciles(self,
                                                               monkeypatch):
        """舊版帳本（沒有 claim 方法）→ 退回本行程節流，不是停擺。"""
        led = _Led([_rec()])
        assert not hasattr(led, "claim_reconcile_pass")
        n, _seen = _run(monkeypatch, led, [True])
        assert n == 1


class TestClaimOnTheRealLedger:
    """`claim_reconcile_pass` 自己的契約（用真的帳本、真的檔案）。"""

    @staticmethod
    def _led(tmp_path):
        from cmuh_common.delivery_ledger import DeliveryLedger
        return DeliveryLedger(path=str(tmp_path / "delivery.json"))

    def test_the_first_pass_is_claimed(self, tmp_path):
        led = self._led(tmp_path)
        assert led.claim_reconcile_pass(now=1000.0, every_sec=600.0) is True

    def test_a_second_pass_too_soon_is_refused(self, tmp_path):
        led = self._led(tmp_path)
        led.claim_reconcile_pass(now=1000.0, every_sec=600.0)
        assert led.claim_reconcile_pass(now=1100.0, every_sec=600.0) is False

    def test_a_later_pass_is_allowed(self, tmp_path):
        led = self._led(tmp_path)
        led.claim_reconcile_pass(now=1000.0, every_sec=600.0)
        assert led.claim_reconcile_pass(now=1700.0, every_sec=600.0) is True

    def test_a_second_process_sees_the_first_ones_claim(self, tmp_path):
        """★核心★ 這正是「跨 process」的意思：另一個【物件】也要被擋下。"""
        a, b = self._led(tmp_path), self._led(tmp_path)
        assert a.claim_reconcile_pass(now=1000.0, every_sec=600.0) is True
        assert b.claim_reconcile_pass(now=1001.0, every_sec=600.0) is False, (
            "★另一支程式沒有被擋下 → 兩邊會同時覆蓋彼此的收斂結果★")

    def test_an_unreadable_stamp_means_never_run_not_just_ran(self, tmp_path):
        """★讀不到＝當成沒跑過★

        當成「剛剛才跑過」的話，檔案永久壞掉時就變成【永遠不回查】，
        而且一個字都不會說。寧可多跑一輪。
        """
        led = self._led(tmp_path)
        import io
        io.open(led.path + ".reconcile", "w", encoding="utf-8").write("壞掉的內容")
        assert led.claim_reconcile_pass(now=1000.0, every_sec=600.0) is True

    def test_an_unwritable_stamp_still_lets_the_pass_run(self, tmp_path,
                                                        monkeypatch):
        """寫不進去也要讓這一輪跑掉（沒有出口的 fail-closed 更糟）。"""
        led = self._led(tmp_path)
        import builtins
        real_open = builtins.open

        def _open(path, mode="r", *a, **k):
            if str(path).endswith(".reconcile") and "w" in mode:
                raise OSError("唯讀")
            return real_open(path, mode, *a, **k)

        monkeypatch.setattr(builtins, "open", _open)
        assert led.claim_reconcile_pass(now=1000.0, every_sec=600.0) is True


class TestMalformedRecordsDoNotKillThePass:
    """★外審 2026-08-09 P2-01★ 一筆壞資料不可以讓整輪回查中止。"""

    def test_a_broken_timestamp_does_not_abort_the_pass(self, monkeypatch):
        """原本 `float()` 在 try 之外 → 一筆壞的把【每一輪】都打掉。"""
        led = _Led([_rec(did="bad") | {"created_at": "不是數字"},
                    _rec(did="good")])
        n, seen = _run(monkeypatch, led, [True, True])
        assert n >= 1, "★一筆壞掉的時間戳把整輪都打掉了★"
        assert "<a@b>" in seen
        assert ("good", True) in led.resolved, "好的那一筆被壞的那一筆連累了"

    @pytest.mark.parametrize("bad", ["不是數字", "2026-08-09", [], {"a": 1}])
    def test_a_broken_timestamp_is_treated_as_old_not_young(self, monkeypatch,
                                                            bad):
        """★方向★ 當成「很新」的話，壞掉的那一筆會被年齡門檻永遠濾掉。

        ★測試資料必須真的走進那個 except★ 第一版用 `None` —— 但
        `rec.get(...) or 0` 會先把 None 變成 0，`float(0)` 不會拋例外，
        於是這條測試根本沒有踩到被測的那一行（突變驗證抓到的）。
        """
        led = _Led([_rec(did="bad") | {"created_at": bad}])
        n, _seen = _run(monkeypatch, led, [True])
        assert n == 1, "看不懂的時間戳被當成『剛剛才建立』→ 永遠不會被收斂"

    def test_a_broken_timestamp_does_not_block_the_giveup_path(self,
                                                               monkeypatch):
        """沒有 Message-ID + 時間戳壞掉 → 一樣要有出口（不可以卡在年齡判定）。"""
        led = _Led([_rec(did="d1", msgid="") | {"created_at": "壞"}])
        # 壞掉的時間戳被當成 epoch（很舊）→ 用真實量級的 now 才看得出差別。
        # 假的 now=10_000 只有 2.8 小時，還沒到 24 小時門檻。
        _run(monkeypatch, led, [True], now=200_000.0)
        assert led.resolved == [("d1", False)]

    def test_a_missing_created_at_is_still_reconciled(self, monkeypatch):
        led = _Led([{"delivery_id": "d1", "message_id": "<a@b>"}])
        n, _seen = _run(monkeypatch, led, [True])
        assert n == 1


class TestRecordsWithoutAMessageIdHaveAnExit:
    """★外審 2026-08-09 P2-03★ 查不出來的紀錄不可以永遠掛著。

    回查完全靠 Message-ID。沒有它的紀錄永遠進不了 `ripe` —— 於是永遠停在
    LIVE_STATES，一接上寄送閘門就永久擋住那個 business key，而且沒有任何
    地方會說出來（沒有出口的 fail-closed）。
    """

    def test_a_young_record_without_a_message_id_is_left_alone(self,
                                                               monkeypatch):
        """還早 → 不動它（`make_msgid()` 之後也許補得回來）。"""
        led = _Led([_rec(msgid="", age=60.0)])
        _run(monkeypatch, led, [True])
        assert led.resolved == []

    def test_an_aged_record_without_a_message_id_is_closed_out(self,
                                                               monkeypatch):
        led = _Led([_rec(msgid="", age=dr.NO_MESSAGE_ID_GIVE_UP_SEC + 1)])
        _run(monkeypatch, led, [True])
        assert led.resolved == [("d1", False)], (
            "★永遠查不出結果的紀錄沒有出口 → 永久擋住那個 business key★")

    def test_the_closeout_direction_is_resendable(self, monkeypatch):
        """★方向要選會被人發現的那一邊★

        結成「沒送到」＝可能重複寄一封；放著不管＝該寄的永遠不寄且沒人知道。
        """
        led = _Led([_rec(msgid="", age=dr.NO_MESSAGE_ID_GIVE_UP_SEC + 1)])
        _run(monkeypatch, led, [True])
        assert led.resolved and led.resolved[0][1] is False

    def test_the_closeout_does_not_burn_the_throttle_window(self, monkeypatch):
        """結案不需要 IMAP → 不該因此把回查的節流窗口用掉。"""
        led = _Led([_rec(msgid="", age=dr.NO_MESSAGE_ID_GIVE_UP_SEC + 1)])
        _run(monkeypatch, led, [True])
        assert cq._RECONCILER.last_ts == 0.0, "沒開 IMAP 卻推進了節流時間戳"

    def test_a_closeout_failure_does_not_abort_the_pass(self, monkeypatch):
        class _Grumpy(_Led):
            def resolve_unknown(self, did, *, delivered, note=""):
                if did == "nomsg":
                    raise RuntimeError("寫不進去")
                return super().resolve_unknown(did, delivered=delivered,
                                               note=note)

        led = _Grumpy([_rec(did="nomsg", msgid="",
                            age=dr.NO_MESSAGE_ID_GIVE_UP_SEC + 1),
                       _rec(did="good")])
        n, _seen = _run(monkeypatch, led, [True])
        assert n == 1 and ("good", True) in led.resolved


# ══ 外審第 2 輪 ═══════════════════════════════════════════════════════════
class TestTheLedgerItselfSurvivesBadTimestamps:
    """★#2★ `Reconciler` 的容錯是在 `unresolved()` 回傳【之後】才跑的。

    帳本自己那三個查詢方法本來直接拿原始欄位當 sort key。磁碟上的 JSON
    只要有【一筆】`created_at` 是字串，`sorted()` 比較 str 與 float 就拋
    `TypeError` —— 整份清單列不出來，呼叫端捕捉後回 0，
    **所有正常的紀錄也從此永遠不收斂**。一筆壞資料害死全部。
    """

    @staticmethod
    def _led(tmp_path, records):
        import json
        from cmuh_common.delivery_ledger import DeliveryLedger
        p = tmp_path / "delivery_ledger.json"
        with io.open(str(p), "w", encoding="utf-8") as fh:
            json.dump(records, fh)
        return DeliveryLedger(path=str(p))

    def _mixed(self, tmp_path, bad_created):
        return self._led(tmp_path, {
            "bad": {"delivery_id": "bad", "state": "unknown",
                    "message_id": "<b@x>", "created_at": bad_created,
                    "recipients": {"a@b": "unknown"}},
            "good": {"delivery_id": "good", "state": "unknown",
                     "message_id": "<g@x>", "created_at": 1.0,
                     "recipients": {"a@b": "unknown"}},
        })

    def test_unresolved_does_not_raise_on_a_string_timestamp(self, tmp_path):
        led = self._mixed(tmp_path, "2026-08-09")
        got = {r["delivery_id"] for r in led.unresolved()}
        assert got == {"bad", "good"}, (
            "★一筆字串時間戳讓整份待回查清單列不出來★")

    def test_unresolved_does_not_raise_on_a_dict_timestamp(self, tmp_path):
        led = self._mixed(tmp_path, {"nested": 1})
        assert len(led.unresolved()) == 2

    def test_stuck_submitting_does_not_raise_either(self, tmp_path):
        led = self._led(tmp_path, {
            "s1": {"delivery_id": "s1", "state": "submitting",
                   "created_at": "壞", "updated_at": "壞",
                   "recipients": {"a@b": "unknown"}},
        })
        assert len(led.stuck_submitting(older_than_sec=1.0)) == 1, (
            "壞掉的 updated_at 讓卡住的紀錄永遠不算卡住")

    def test_a_whole_pass_still_settles_the_good_record(self, tmp_path,
                                                        monkeypatch):
        """★端到端★ 用真的帳本跑一輪，好的那一筆要收斂。"""
        led = self._mixed(tmp_path, "2026-08-09")
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        n = cq._reconcile_unknown_deliveries(now=1e9, finder=lambda m: True)
        assert n == 2, "壞資料把整輪打掉了"


class TestNonFiniteTimestamps:
    """★#5★ `json.load()` 接受 NaN／Infinity，而 `float()` 不會拋。

    NaN 參與的比較【永遠是 False】，於是同一個壞值在兩條路上往
    **相反** 方向出錯：
      * 年齡門檻 `now - nan >= MIN_AGE` → False → 永遠進不了回查清單；
      * 放棄門檻 `nan < 24h` → False → 反而【立刻】被結案，跳過保護期。
    """

    @pytest.mark.parametrize("bad", [float("nan"), float("inf"),
                                     float("-inf")])
    def test_a_nonfinite_timestamp_is_treated_as_old(self, monkeypatch, bad):
        led = _Led([_rec(did="d1") | {"created_at": bad}])
        n, _seen = _run(monkeypatch, led, [True])
        assert n == 1, "★非有限數的時間戳讓紀錄永遠卡在 LIVE★"

    def test_a_nan_record_without_a_message_id_keeps_its_grace_period(
            self, monkeypatch):
        """★反方向★ NaN 不可以讓它跳過 24 小時保護期被提早結案。

        `_created_at` 把 NaN 當成 epoch(0.0)，所以在【假的】小 now 之下
        年齡還不夠 —— 保護期仍然成立。
        """
        led = _Led([_rec(did="d1", msgid="") | {"created_at": float("nan")}])
        _run(monkeypatch, led, [True], now=1000.0)
        assert led.resolved == [], "NaN 讓紀錄跳過保護期被立刻結案"

    def test_the_ledger_helper_rejects_nonfinite(self):
        from cmuh_common.delivery_ledger import _as_epoch
        for bad in (float("nan"), float("inf"), "x", [], {}, None):
            assert _as_epoch(bad) == 0.0, bad
        assert _as_epoch(12.5) == 12.5


class TestReconcileIsOffTheClinicalPath:
    """★#3★ 回查會開 IMAP（每筆 12 秒 timeout、一輪最多 5 筆）。

    同步串在刷新 worker 上 = IMAP 不可達時，掛號資料在送出第一個查詢
    之前就先卡 60 秒以上，而且每 10 分鐘重來一次。
    為了觀測寄信結果而延後臨床資料，方向完全反了。
    """

    @staticmethod
    def _main():
        import importlib
        return importlib.import_module("main")

    def test_the_refresh_worker_only_kicks_it_off(self):
        """刷新路徑上只可以出現【點火】，不可以出現同步呼叫。"""
        tree = ast.parse(io.open(os.path.join(REPO_ROOT, "src", "main.py"),
                                 encoding="utf-8").read())
        worker = None
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "run_parallel_checks":
                worker = n
                break
        assert worker is not None, "找不到刷新 worker（測試自己失效了）"
        called = {c.func.id for c in ast.walk(worker)
                  if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        assert "_kick_off_alert_reconcile" in called, "刷新路徑沒有點火回查"
        assert "_reconcile_alert_deliveries" not in called, (
            "★同步呼叫回查 → IMAP 掛掉時掛號資料要等它★")

    def test_the_kickoff_returns_without_waiting(self, monkeypatch):
        """★核心★ finder 慢的時候，點火本身必須立刻返回。"""
        import threading
        import time as _t
        m = self._main()
        started = threading.Event()
        release = threading.Event()

        def _slow(now=None, finder=None):
            started.set()
            release.wait(5.0)
            return 0

        monkeypatch.setattr(m, "_reconcile_alert_deliveries", _slow)
        t0 = _t.monotonic()
        assert m._kick_off_alert_reconcile() is True
        elapsed = _t.monotonic() - t0
        try:
            assert elapsed < 1.0, f"點火等了 {elapsed:.2f} 秒 —— 那就是同步的"
            assert started.wait(5.0), "背景那條根本沒開起來"
        finally:
            release.set()

    def test_a_second_kickoff_is_refused_while_one_is_running(self,
                                                              monkeypatch):
        """★single-flight★ IMAP 慢的時候，每次刷新各開一條會累積成一堆卡住的連線。"""
        import threading
        m = self._main()
        release = threading.Event()
        started = threading.Event()

        def _slow(now=None, finder=None):
            started.set()
            release.wait(5.0)
            return 0

        monkeypatch.setattr(m, "_reconcile_alert_deliveries", _slow)
        try:
            assert m._kick_off_alert_reconcile() is True
            assert started.wait(5.0)
            assert m._kick_off_alert_reconcile() is False, (
                "★上一輪還在跑就又開了一條★")
        finally:
            release.set()

    def test_the_flag_is_released_after_the_worker_finishes(self, monkeypatch):
        """★旗標一定要放掉★ 卡住的話回查就永遠不再發生（安靜地）。"""
        import threading
        m = self._main()
        done = threading.Event()

        def _quick(now=None, finder=None):
            done.set()
            return 0

        monkeypatch.setattr(m, "_reconcile_alert_deliveries", _quick)
        assert m._kick_off_alert_reconcile() is True
        assert done.wait(5.0)
        for _ in range(200):
            if m._kick_off_alert_reconcile():
                return
            threading.Event().wait(0.02)
        raise AssertionError("★旗標沒有放掉 → 回查從此永遠不會再跑★")

    def test_a_crashing_worker_still_releases_the_flag(self, monkeypatch):
        import threading
        m = self._main()
        hit = threading.Event()

        def _boom(now=None, finder=None):
            hit.set()
            raise RuntimeError("炸了")

        monkeypatch.setattr(m, "_reconcile_alert_deliveries", _boom)
        assert m._kick_off_alert_reconcile() is True
        assert hit.wait(5.0)
        for _ in range(200):
            if m._kick_off_alert_reconcile():
                return
            threading.Event().wait(0.02)
        raise AssertionError("★worker 拋例外後旗標卡住★")
