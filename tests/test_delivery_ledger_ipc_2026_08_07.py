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

    def test_two_processes_do_not_erase_each_other(self, tmp_path):
        """★核心★ 兩個 process 各寫 N 筆，最後兩邊的紀錄都要在。"""
        path = str(tmp_path / "ledger.json")
        DeliveryLedger(path=path)              # 先建立檔案
        procs = [mp.Process(target=_writer, args=(path, tag, 12))
                 for tag in ("main", "consult")]
        for p in procs:
            p.start()
        for p in procs:
            p.join(60)
            assert p.exitcode == 0, f"writer 失敗 exitcode={p.exitcode}"

        fresh = DeliveryLedger(path=path)
        keys = {r["business_key"] for r in fresh._records.values()}
        missing_main = [f"main:{i}" for i in range(12) if f"main:{i}" not in keys]
        missing_consult = [f"consult:{i}" for i in range(12)
                           if f"consult:{i}" not in keys]
        assert not missing_main and not missing_consult, (
            f"★互相覆蓋★ 遺失 main={missing_main} consult={missing_consult}")

    def test_a_stale_copy_must_not_revert_another_processes_update(self,
                                                                   tmp_path):
        """★lost update 的精確形狀★（突變驗證教我的）

        我第一版寫成「A 載入 → B 新增一筆 → A 再寫」——那抓不到缺陷，因為
        `dict.update()` 只會【新增】，B 的新紀錄不會被移除。

        真正的 lost update 是：**A 手上有一份【B 後來改過的那筆】的舊副本**。
        A 一寫回，B 的更新就被自己的舊副本蓋回去。
        """
        path = str(tmp_path / "ledger.json")
        a = DeliveryLedger(path=path)
        shared = a.begin(business_key="shared", category="t",
                         recipients=["a@x.tw"])          # PREPARED，落地

        b = DeliveryLedger(path=path)                    # B 載入(看到 PREPARED)
        b.settle(shared)                                 # B 把它結案 → CONFIRMED

        # A 手上那筆仍是 PREPARED。它寫別的東西時，不可以把 B 的結案蓋回去。
        other = a.begin(business_key="a:2", category="t", recipients=["a@x.tw"])
        a.settle(other)

        fresh = DeliveryLedger(path=path)
        state = fresh._records[shared]["state"]
        assert state != "prepared", (
            "★B 的結案被 A 的舊副本蓋回去了★ 這就是 lost update")
        keys = {r["business_key"] for r in fresh._records.values()}
        assert {"shared", "a:2"} <= keys, keys

    def test_it_does_not_overwrite_when_the_disk_is_unreadable(self, tmp_path):
        """★寫回前讀不到磁碟 → 不寫★（不能拿記憶體整份去蓋）。

        ★情境要讓「記憶體」與「磁碟」真的不同★（突變驗證教我的）：
        我第一版只有一個 ledger 物件，它的記憶體本來就含有磁碟上那筆，
        所以「拿記憶體去蓋」看起來沒有損失。要有【另一個 process 寫的、
        而我記憶體裡沒有的】那一筆，才看得出差別。
        """
        path = str(tmp_path / "ledger.json")
        a = DeliveryLedger(path=path)
        did_a = a.begin(business_key="a:1", category="t", recipients=["a@x.tw"])
        a.settle(did_a)

        b = DeliveryLedger(path=path)
        did_b = b.begin(business_key="b:1", category="t", recipients=["b@x.tw"])
        b.settle(did_b)                                  # 磁碟上有 a:1 + b:1

        import cmuh_common.delivery_ledger as dl
        real = dl.safe_load_json_ex
        dl.safe_load_json_ex = lambda *a, **k: ({}, "error")
        try:
            # A 記憶體裡【沒有】b:1。此刻磁碟讀不到 → 不可以拿記憶體去蓋。
            a.begin(business_key="a:2", category="t", recipients=["a@x.tw"])
        finally:
            dl.safe_load_json_ex = real

        fresh = DeliveryLedger(path=path)
        keys = {r["business_key"] for r in fresh._records.values()}
        assert "b:1" in keys, (
            f"★讀不到磁碟時仍用記憶體覆寫,把別的程式的紀錄清掉了★:{keys}")

    def test_a_lock_failure_still_writes(self, tmp_path, monkeypatch):
        """★fail-open 是刻意的★ 鎖不到就不寫 = 為了避免「可能覆蓋」而造成
        「一定丟失」。退化成舊行為 + 警告，比靜默丟資料好。"""
        path = str(tmp_path / "ledger.json")
        led = DeliveryLedger(path=path)
        monkeypatch.setattr("builtins.open",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("no")))
        try:
            did = led.begin(business_key="z", category="t", recipients=["a@x.tw"])
        finally:
            monkeypatch.undo()
        assert did, "鎖不到就整個不寫 → 這一筆消失了"

    def test_every_mutator_marks_itself_dirty(self):
        """★接線★ 合併只寫「本 process 動過的」—— 忘記標記就等於那次變更不會落地。"""
        src = textwrap.dedent(inspect.getsource(DeliveryLedger))
        tree = ast.parse(src)
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            calls = {n.func.attr for n in ast.walk(fn)
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute)}
            if "_save_locked" not in calls:
                continue
            # ★判準要是「有沒有改到紀錄」,不是「有沒有存檔」★
            #   `flush()` 只是把【已經標記過】的 dirty 再寫一次,它自己不改
            #   任何紀錄。把它也要求標記 dirty,守衛就從「檢查性質」退化成
            #   「檢查長相」—— 而且逼人為了過關寫出無意義的 add。
            #   會改到紀錄 = 對 `rec[...]` 或 `self._records[...]` 做指派。
            mutates = any(
                isinstance(t, ast.Subscript)
                for n in ast.walk(fn) if isinstance(n, ast.Assign)
                for t in n.targets)
            if not mutates:
                continue
            assert "add" in calls, (
                f"★{fn.name} 會改紀錄又存檔,卻沒有標記 dirty★ "
                "那次變更不會被合併寫入")


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


class TestLockAcquisitionRetries:
    """★[2026-08-10 CI] LK_LOCK 是「每秒 1 次、共 10 次」的輪詢★

    對方連續背靠背持鎖（慢磁碟 fsync 數百 ms × 幾十次連寫）時，等待方的
    10 個整秒採樣點可能全部落在「對方持鎖中」→ OSError → fail-open 無鎖
    寫入 → 互相覆蓋。鎖要有界重試把輪詢相位打散；三輪都失敗才 fail-open
    （語意不變：寧可可能覆蓋，不要一定丟失）。
    """

    @staticmethod
    def _led(tmp_path):
        return DeliveryLedger(path=str(tmp_path / "l.json"))

    def test_a_transient_lock_failure_is_retried(self, tmp_path, monkeypatch):
        import msvcrt
        calls = {"n": 0}
        real = msvcrt.locking

        def _flaky(fd, mode, nbytes):
            if mode == msvcrt.LK_LOCK:
                calls["n"] += 1
                if calls["n"] <= 2:
                    raise OSError("鎖被佔住(前兩次)")
            return real(fd, mode, nbytes)

        monkeypatch.setattr(msvcrt, "locking", _flaky)
        monkeypatch.setattr(
            "cmuh_common.delivery_ledger.time.sleep", lambda s: None)
        led = self._led(tmp_path)
        with led._interprocess_lock():
            pass
        assert calls["n"] == 3, f"★暫時性的鎖失敗沒有被重試★:{calls['n']}"

    def test_three_failures_still_fail_open(self, tmp_path, monkeypatch,
                                            caplog):
        """★反方向★ 重試不可以變成永遠等（fail-open 的語意要保留）。"""
        import logging as _lg

        import msvcrt

        def _always(fd, mode, nbytes):
            if mode == msvcrt.LK_LOCK:
                raise OSError("永遠鎖不到")

        monkeypatch.setattr(msvcrt, "locking", _always)
        monkeypatch.setattr(
            "cmuh_common.delivery_ledger.time.sleep", lambda s: None)
        led = self._led(tmp_path)
        with caplog.at_level(_lg.WARNING):
            with led._interprocess_lock():
                pass                      # ★仍然要走得進來（fail-open）★
        assert any("取不到帳本檔案鎖" in r.getMessage()
                   for r in caplog.records), "fail-open 沒有留下警告"
