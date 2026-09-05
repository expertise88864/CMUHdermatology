# -*- coding: utf-8 -*-
"""寄送帳本必須跨 process 安全（外審第 8 輪 P1-01）＋ 每筆都要有終局狀態（P1-03）。

【P1-01】這本帳是【主程式與會診程式共用】的 —— 兩個不同的 process。
`threading.RLock` 只鎖得住同一個 process 裡的執行緒。舊寫法把本 process 記憶體
裡的整份紀錄覆蓋整個檔案：

    main    讀到 {A}          consult 讀到 {A}
    main    寫回 {A,B}
    consult 寫回 {A,C}        ← B 永久消失

`os.replace` 是原子的，但它保證的是「不會寫出半個 JSON」，**擋不住 lost update**。

★外審點名既有測試的問題★：所謂「兩個 sender 共用一本帳」只驗證了預設路徑相同，
沒有真的開兩個 process、也沒有驗 lost-update。本檔用 `multiprocessing` 真的開。

【P1-03】`begin()` 之後每一條出口都必須 `settle`。舊寫法只在成功與「結果不明」
兩條路 settle，連不上／認證失敗／5xx 直接往上拋 → 那一筆永遠停在 SUBMITTING，
而 prune 不清 SUBMITTING。三次 attempt 留下兩筆假的「可能送到一半」。
"""
from contextlib import closing
import ast
import inspect
import multiprocessing as mp
import os
import sys
import textwrap

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.delivery_ledger import DeliveryLedger  # noqa: E402


def _writer(path, tag, n):
    """另一個 process：載入同一本帳，寫入自己的紀錄。"""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    from cmuh_common.delivery_ledger import DeliveryLedger as _L
    led = _L(path=path)
    for i in range(n):
        did = led.begin(business_key=f"{tag}:{i}", category="test",
                        recipients=[f"{tag}{i}@x.tw"], subject=f"{tag}-{i}")
        led.mark_submitting(did)
        led.settle(did)


class TestCrossProcessSafety:

    @staticmethod
    def _keys(tmp_path) -> set:
        import sqlite3
        with closing(sqlite3.connect(str(tmp_path / "ledger.sqlite3"))) as c, c:
            return {r[0] for r in
                    c.execute("SELECT business_key FROM deliveries")}

    def test_two_processes_do_not_erase_each_other(self, tmp_path):
        """★核心★ 兩個 process 各寫 N 筆，最後兩邊的紀錄都要在。"""
        path = str(tmp_path / "ledger.json")
        DeliveryLedger(path=path)              # 先建立資料庫
        procs = [mp.Process(target=_writer, args=(path, tag, 12))
                 for tag in ("main", "consult")]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
            assert p.exitcode == 0, f"writer 失敗 exitcode={p.exitcode}"

        keys = self._keys(tmp_path)
        missing_main = [f"main:{i}" for i in range(12) if f"main:{i}" not in keys]
        missing_consult = [f"consult:{i}" for i in range(12)
                           if f"consult:{i}" not in keys]
        assert not missing_main and not missing_consult, (
            f"★互相覆蓋★ 遺失 main={missing_main} consult={missing_consult}")

    def test_a_stale_copy_must_not_revert_another_processes_update(self,
                                                                   tmp_path):
        """★lost update 的精確形狀★

        A 手上有一份【B 後來改過的那筆】的舊副本;A 再寫別的東西時,
        不可以把 B 的更新蓋回去。[SQLite 版] 每筆讀-改-寫都在自己的
        IMMEDIATE 交易裡、沒有整份覆寫 —— 這條性質仍要有測試釘著,
        防止日後有人加回「記憶體快照 → 整份寫回」。
        """
        path = str(tmp_path / "ledger.json")
        a = DeliveryLedger(path=path)
        shared = a.begin(business_key="shared", category="t",
                         recipients=["a@x.tw"])          # SUBMITTING,落地

        b = DeliveryLedger(path=path)
        b.settle(shared)                                 # B 把它結案 → CONFIRMED

        other = a.begin(business_key="a:2", category="t", recipients=["a@x.tw"])
        a.settle(other)

        fresh = DeliveryLedger(path=path)
        assert fresh.state_of(shared) == "confirmed", (
            "★B 的結案被 A 蓋回去了★ 這就是 lost update")
        assert {"shared", "a:2"} <= self._keys(tmp_path)

    def test_one_instances_writes_never_remove_anothers_rows(self, tmp_path):
        """A 的任何寫入都不可以清掉 B 寫的紀錄(舊版的整份覆寫已不存在,
        這條測試釘住它不會回來)。"""
        path = str(tmp_path / "ledger.json")
        a = DeliveryLedger(path=path)
        b = DeliveryLedger(path=path)
        b.settle(b.begin(business_key="b:1", category="t",
                         recipients=["b@x.tw"]))
        for i in range(5):
            a.settle(a.begin(business_key=f"a:{i}", category="t",
                             recipients=["a@x.tw"]))
        assert "b:1" in self._keys(tmp_path), (
            "★A 的寫入把 B 的紀錄清掉了★")

    def test_an_unavailable_db_refuses_loudly_instead_of_writing(
            self, tmp_path, monkeypatch):
        """★[外審 2026-08-12 P1-02/03] fail-open 的路已經拆掉★

        舊版「鎖不到就照寫」;現在資料庫不可用時 `begin()` 必須
        ★拋 LedgerUnavailable★ —— 絕不回一個沒有落地的 id,
        也絕不退化成無鎖寫入。
        """
        import sqlite3 as _sq

        import cmuh_common.delivery_ledger as dl
        path = str(tmp_path / "ledger.json")
        led = DeliveryLedger(path=path)
        led._close_quietly()                       # 斷開既有連線
        monkeypatch.setattr(
            dl.sqlite3, "connect",
            lambda *a, **k: (_ for _ in ()).throw(_sq.OperationalError("鎖死")))
        import pytest as _pytest
        with _pytest.raises(dl.LedgerUnavailable):
            led.begin(business_key="z", category="t", recipients=["a@x.tw"])

    def test_every_write_happens_inside_a_transaction(self):
        """★接線★ [SQLite 版] 任何 INSERT/UPDATE/DELETE 都必須在
        `_txn`(BEGIN IMMEDIATE)裡 —— 在交易外寫,跨 process 的互斥
        就名存實亡(這正是舊版 sidecar lock fail-open 的翻版)。

        ★空集合不算通過★:一定要真的找到有寫入的方法,守衛才算跑過。
        """
        src = textwrap.dedent(inspect.getsource(DeliveryLedger))
        tree = ast.parse(src)
        # `_insert_locked`/`_prune_locked` 的契約是「在呼叫端的交易裡執行」
        # (docstring 寫明),呼叫它們的方法必須自己有 _txn —— 由下面的檢查
        # 涵蓋。`_connect_locked` 是 idempotent 的 schema bootstrap
        # (CREATE TABLE / INSERT OR IGNORE 一筆常數),autocommit 即可。
        # `_mutate_states_in_txn` 同樣是「在呼叫端的交易裡執行」的內層
        # (批次AE-5:一次 RCPT 的結果要把子紀錄拒收+親紀錄升級+嘗試邊界
        #  寫在【同一筆】交易裡,所以這一段必須能被別人的交易包起來)。
        # `_backfill_occurrence_owners_locked`(批次AE-11)同樣是內層:
        #  沿 supersession 鏈查到的所有權要與那一次仲裁寫在【同一筆】交易裡。
        in_callers_txn = {"_insert_locked", "_prune_locked",
                          "_scrub_stale_bodies_locked",
                          "_mutate_states_in_txn",
                          "_backfill_occurrence_owners_locked"}
        # `_connect_locked` 是另一種豁免:idempotent 的 schema bootstrap
        # (CREATE TABLE / INSERT OR IGNORE 一筆常數),autocommit 即可 ——
        # 它【不】要求呼叫端有交易,所以不能混進上面那一組(見下面的
        # 呼叫端檢查)。
        autocommit_ok = {"_connect_locked"}
        checked = 0
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            writes = [n for n in ast.walk(fn)
                      if isinstance(n, ast.Call)
                      and isinstance(n.func, ast.Attribute)
                      and n.func.attr in ("execute", "executemany")
                      and n.args and isinstance(n.args[0], ast.Constant)
                      and isinstance(n.args[0].value, str)
                      and n.args[0].value.lstrip().upper().startswith(
                          ("INSERT", "UPDATE", "DELETE"))]
            if not writes or fn.name in in_callers_txn \
                    or fn.name in autocommit_ok:
                continue
            calls = {n.func.attr for n in ast.walk(fn)
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute)}
            assert "_txn" in calls, (
                f"★{fn.name} 在交易外寫資料庫★ 跨 process 互斥失效")
            checked += 1
        assert checked >= 4, (
            f"守衛只掃到 {checked} 個寫入方法 —— 判準可能失效了(空集合不算過)")

        # ★豁免不可以是一張白紙★:「在呼叫端的交易裡執行」只有在【呼叫端
        #   真的有交易】時才成立,而上面那條規則掃不到「自己沒有直接寫入、
        #   只呼叫這些內層 helper」的方法(它只看直接的 INSERT/UPDATE/DELETE)。
        #   註解一直宣稱「由下面的檢查涵蓋」,但其實沒有人在檢查 ——
        #   批次AE-11 施工時發現,順手把那個前提也變成守衛。
        callers = 0
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef) \
                    or fn.name in in_callers_txn:
                continue
            calls = {n.func.attr for n in ast.walk(fn)
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute)}
            used = calls & in_callers_txn
            if not used:
                continue
            assert "_txn" in calls, (
                f"★{fn.name} 呼叫了內層寫入 {sorted(used)} 卻沒有自己的交易★"
                " 那些 helper 的契約是「在呼叫端的交易裡執行」")
            callers += 1
        assert callers >= 3, (
            f"呼叫端守衛只掃到 {callers} 個 —— 判準可能失效了(空集合不算過)")


class TestEveryDeliveryReachesATerminalState:
    """★P1-03★ `begin()` 之後每一條出口都必須 settle。"""

    def _send_block(self):
        """包住 `send_via_smtp` 的【最內層】try。

        ★lineno 最大 = 最晚開始 = 最內層★ 外層還有一個 try 把整個 attempt
        迴圈都包起來；抓到它的話，檢查的就不是寄送那一段（同一個形狀在
        test_query_delivery_split 也踩過一次）。
        """
        import consult_query as cq
        src = textwrap.dedent(inspect.getsource(cq._do_full_job))
        tree = ast.parse(src)
        cands = [n for n in ast.walk(tree) if isinstance(n, ast.Try)
                 and any(isinstance(c, ast.Call)
                         and isinstance(c.func, ast.Name)
                         and c.func.id == "send_via_smtp"
                         for c in ast.walk(
                             ast.Module(body=n.body, type_ignores=[])))]
        assert cands, "找不到 SMTP 寄送那一段 try"
        return max(cands, key=lambda n: n.lineno)

    def test_every_exit_settles(self):
        node = self._send_block()
        # 成功出口(else 或 try 尾) + 每個 except 都要有 settle
        for handler in node.handlers:
            calls = {n.func.id for n in ast.walk(handler)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            assert "_delivery_settle" in calls, (
                "★有一條例外出口沒有結案★ 那一筆會永遠停在 SUBMITTING")
        assert len(node.handlers) >= 2, (
            "至少要分開處理『結果不明』與『確定失敗』兩種")

    def test_a_definite_failure_is_recorded_as_failed_not_unknown(self):
        node = self._send_block()
        kinds = set()
        for handler in node.handlers:
            for n in ast.walk(handler):
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                        and n.func.id == "_delivery_settle"):
                    kinds |= {kw.arg for kw in n.keywords}
        assert {"unknown", "failed"} <= kinds, (
            f"★確定失敗與結果不明沒有分開★ 只看到 {kinds}")


class TestContentionIsFailClosed:
    """★[外審 2026-08-12 P1-02] 鎖競爭不可以退化成無鎖寫入★

    舊版 sidecar lock 等不到就 fail-open 照寫(互相覆蓋)。SQLite 版:
    等不到寫鎖(busy_timeout 用完)→ `LedgerUnavailable` ——
    integrity-critical 的變更【寧可失敗,不可靜默對撞】。
    settle 失敗的出口是既有的 stuck_submitting 回查,不是重寄。
    """

    def test_a_wedged_writer_makes_mutations_raise_not_corrupt(
            self, tmp_path, monkeypatch):
        import sqlite3 as _sq

        import cmuh_common.delivery_ledger as dl
        monkeypatch.setattr(dl, "_BUSY_TIMEOUT_MS", 300)   # 別等 5 秒
        path = str(tmp_path / "ledger.json")
        led = DeliveryLedger(path=path)
        did = led.begin(business_key="bk", category="t",
                        recipients=["a@x.tw"])

        # 另一條連線握住寫鎖不放(模擬另一個 process 卡在交易中)
        wedge = _sq.connect(str(tmp_path / "ledger.sqlite3"),
                            isolation_level=None)
        wedge.execute("BEGIN IMMEDIATE")
        try:
            import pytest as _pytest
            with _pytest.raises(dl.LedgerUnavailable):
                led.settle(did)            # ★不可以靜默寫入,也不可以卡死★
        finally:
            wedge.execute("ROLLBACK")
            wedge.close()
        # 鎖放掉之後同一筆要能正常結案(競爭是暫時的,不是永久故障)
        assert led.settle(did) == "confirmed"
