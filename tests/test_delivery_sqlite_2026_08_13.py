# -*- coding: utf-8 -*-
"""[批次AD-2] 寄送帳本遷 SQLite WAL(外審 2026-08-12 P1-02/03/08,使用者定案)。

三條 P1 是同一個病灶的三個面:
  * P1-02 跨 process 鎖 fail-open → 取不到就無鎖寫入,last-writer-wins;
  * P1-03 `begin()` 寫回失敗仍回 delivery_id → 「send 前先留下 SUBMITTING」
    其實只保證了記憶體;
  * P1-08 `has_live_delivery()` + `begin()` 是兩個操作 → 接成閘門就是 TOCTOU。
SQLite 交易(BEGIN IMMEDIATE)+ synchronous=FULL 讓三條變成結構性質。
"""
from contextlib import closing
import importlib
import os
import sqlite3
import sys
import time

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

dl = importlib.import_module("cmuh_common.delivery_ledger")


def _led(tmp_path, name="ledger.json"):
    return dl.DeliveryLedger(path=str(tmp_path / name))


class TestBeginIsDurable:
    def test_a_returned_id_is_already_on_disk(self, tmp_path):
        """★P1-03 的契約★ begin() 回來=那一筆已經在磁碟上 ——
        用【另一個連線/另一個實例】看得到才算數,不是問自己的記憶體。"""
        led = _led(tmp_path)
        did = led.begin(business_key="bk", category="t",
                        recipients=["a@x.tw"])
        other = _led(tmp_path)               # 模擬另一個 process
        assert other.state_of(did) == dl.SUBMITTING, (
            "★begin 回傳了 id,磁碟上卻沒有那一筆★")

    def test_synchronous_full_is_pinned(self, tmp_path):
        """WAL 預設建議 NORMAL,但 NORMAL 斷電可能丟最近幾筆 commit ——
        「回傳=已落地」靠的就是 FULL。這裡直接問資料庫,不是看程式碼長相。"""
        led = _led(tmp_path)
        with led._lock:
            mode = led._conn.execute("PRAGMA synchronous").fetchone()[0]
        assert mode in (2, 3), f"synchronous={mode}(2=FULL/3=EXTRA 才算落地)"
        with led._lock:
            jm = led._conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert str(jm).lower() == "wal"


class TestAtomicBusinessKeyClaim:
    """★P1-08★ 查與插在同一筆交易裡 —— 沒有 TOCTOU 窗。"""

    def test_a_live_key_refuses_a_second_claim(self, tmp_path):
        a, b = _led(tmp_path), _led(tmp_path)
        did = a.begin_if_no_live(business_key="k", category="t",
                                 recipients=["a@x.tw"])
        assert did, "第一個 claim 要成功"
        assert b.begin_if_no_live(business_key="k", category="t",
                                  recipients=["a@x.tw"]) == "", (
            "★兩個 process 都 claim 到同一把 key★ 就是重複寄送")

    def test_a_refuted_key_can_be_claimed_again(self, tmp_path):
        led = _led(tmp_path)
        did = led.begin_if_no_live(business_key="k", category="t",
                                   recipients=["a@x.tw"])
        led.settle(did, failed=True)         # 確定沒寄出 → 不再 live
        assert led.begin_if_no_live(business_key="k", category="t",
                                    recipients=["a@x.tw"]) != "", (
            "已被否證的 key 要能再寄(否則漏寄沒有出口)")

    def test_unknown_still_blocks(self, tmp_path):
        """UNKNOWN 屬於 live:重寄的風險大於漏寄,要等回查否證。"""
        led = _led(tmp_path)
        did = led.begin_if_no_live(business_key="k", category="t",
                                   recipients=["a@x.tw"])
        led.settle(did, unknown=True)
        assert led.begin_if_no_live(business_key="k", category="t",
                                    recipients=["a@x.tw"]) == ""


class TestReconcilePassClaimIsAtomic:
    """★P1-02 的另一半★ 回查節流的讀-比-寫在同一筆交易裡。"""

    def test_only_one_instance_wins_a_window(self, tmp_path):
        a, b = _led(tmp_path), _led(tmp_path)
        now = time.time()
        got = [a.claim_reconcile_pass(now=now, every_sec=600),
               b.claim_reconcile_pass(now=now, every_sec=600)]
        assert got == [True, False], (
            f"★同一個時間窗被搶到兩次★:{got} —— 兩支程式會同時回查同一批")

    def test_the_next_window_can_be_claimed(self, tmp_path):
        led = _led(tmp_path)
        assert led.claim_reconcile_pass(now=1000.0, every_sec=600)
        assert not led.claim_reconcile_pass(now=1100.0, every_sec=600)
        assert led.claim_reconcile_pass(now=1700.0, every_sec=600)

    def test_a_broken_db_runs_rather_than_silencing(self, tmp_path,
                                                    monkeypatch):
        """資料庫壞掉 → 照跑(True)。回 False 會在永久壞掉時變成
        【永遠不回查】而且一個字都不說 —— 寧可兩邊都多跑一輪。"""
        led = _led(tmp_path)
        led._close_quietly()
        monkeypatch.setattr(
            dl.sqlite3, "connect",
            lambda *a, **k: (_ for _ in ()).throw(
                dl.sqlite3.OperationalError("db locked")))
        assert led.claim_reconcile_pass(now=time.time(), every_sec=600) is True


class TestLegacyJsonIsImported:
    """既有的 delivery_ledger.json 要被併進來 —— 換儲存層不可以把
    「已寄過」的紀錄弄丟(那等於整批重寄)。"""

    @staticmethod
    def _write_legacy(tmp_path, records: dict):
        import json
        (tmp_path / "delivery_ledger.json").write_text(
            json.dumps(records, ensure_ascii=False), encoding="utf-8")

    def test_old_records_survive_the_migration(self, tmp_path):
        self._write_legacy(tmp_path, {
            "old1": {"business_key": "bk1", "category": "consult",
                     "state": dl.CONFIRMED,
                     "recipients": {"a@x.tw": dl.R_CONFIRMED},
                     "created_at": time.time(), "updated_at": time.time()},
        })
        led = dl.DeliveryLedger(
            path=str(tmp_path / "delivery_ledger.json"))
        assert led.has_live_delivery("bk1"), (
            "★舊帳沒被併進來★ 這個 key 會被重寄")

    def test_reimport_never_overwrites_sqlite_updates(self, tmp_path):
        """★匯入是 INSERT OR IGNORE★ 舊版程式在更新空窗期還會寫 JSON;
        但 SQLite 裡已經收斂的那一筆,不可以被 JSON 裡的舊狀態蓋回去。"""
        self._write_legacy(tmp_path, {
            "d1": {"business_key": "bk", "category": "t",
                   "state": dl.UNKNOWN,
                   "recipients": {"a@x.tw": dl.R_UNKNOWN},
                   "created_at": 1.0, "updated_at": 1.0},
        })
        path = str(tmp_path / "delivery_ledger.json")
        led = dl.DeliveryLedger(path=path)
        led.resolve_unknown("d1", delivered=True)     # SQLite 裡收斂成 CONFIRMED
        again = dl.DeliveryLedger(path=path)          # 重啟 → 再匯入一次
        assert again.state_of("d1") == dl.CONFIRMED, (
            "★重新匯入把已收斂的狀態蓋回 UNKNOWN★")

    def test_bad_legacy_values_are_coerced_at_the_border(self, tmp_path):
        """壞時間戳(字串/NaN)在【進門時】轉成 0.0(很舊),不躺進資料庫。"""
        self._write_legacy(tmp_path, {
            "d1": {"business_key": "bk", "category": "t", "state": dl.UNKNOWN,
                   "recipients": {"a@x.tw": dl.R_UNKNOWN},
                   "created_at": "bad", "updated_at": float("nan")},
        })
        led = dl.DeliveryLedger(path=str(tmp_path / "delivery_ledger.json"))
        rows = led.unresolved()
        assert [r["delivery_id"] for r in rows] == ["d1"]
        assert rows[0]["updated_at"] == 0.0

    def test_a_corrupt_legacy_json_does_not_break_startup(self, tmp_path):
        (tmp_path / "delivery_ledger.json").write_text("{broken",
                                                       encoding="utf-8")
        led = dl.DeliveryLedger(path=str(tmp_path / "delivery_ledger.json"))
        did = led.begin(business_key="bk", category="t",
                        recipients=["a@x.tw"])       # SQLite 本身照常可用
        assert led.state_of(did) == dl.SUBMITTING


class TestCommitFailureIsNeverSwallowed:
    def test_a_failed_commit_raises_instead_of_pretending(self, tmp_path):
        """★P1-03 的另一half★ COMMIT 才是「落地」的那一刻 —— COMMIT 炸掉
        還正常返回,就是把「沒落地」講成「落地了」(舊 fail-open 的翻版)。
        """
        led = _led(tmp_path)
        did = led.begin(business_key="bk", category="t",
                        recipients=["a@x.tw"])

        class _CommitBomb:
            """只在 COMMIT 時爆炸的連線包裝(其餘全部委派)。"""

            def __init__(self, real):
                self._real = real

            def execute(self, sql, *a):
                if sql == "COMMIT":
                    raise sqlite3.OperationalError("disk I/O error")
                return self._real.execute(sql, *a)

            def __getattr__(self, name):
                return getattr(self._real, name)

        with led._lock:
            led._conn = _CommitBomb(led._conn)
        with pytest.raises(dl.LedgerUnavailable):
            led.settle(did)

    def test_the_transaction_takes_the_write_lock_up_front(self):
        """`_txn` 的 docstring 宣稱「一開始就拿寫鎖,整段互斥」——
        DEFERRED 的讀-改-寫要到升級時才碰鎖,宣稱與實作必須一致。"""
        import inspect
        import textwrap
        src = textwrap.dedent(inspect.getsource(dl.DeliveryLedger._txn))
        assert '"BEGIN IMMEDIATE"' in src, (
            "★_txn 沒有用 BEGIN IMMEDIATE★ 讀-改-寫不再從頭互斥")


class TestAFailureInsideATransactionReleasesTheLock:
    """★[外審 AD-2 第 1 輪 P2]★ 非 SQL 例外(binding 溢位、回呼 bug)只要
    沒 ROLLBACK,交易就開著不放:本 process 之後全是 "cannot start a
    transaction within a transaction",別的 process 等到 busy_timeout ——
    ★卡住的持有者把整本帳拖下水★,之後所有寄送都沒有帳。
    """

    def test_a_non_sql_exception_rolls_back_and_the_ledger_stays_usable(
            self, tmp_path):
        led = _led(tmp_path)
        did = led.begin(business_key="bk", category="t",
                        recipients=["a@x.tw"])

        def _buggy(states):
            raise RuntimeError("回呼裡的 bug")

        with pytest.raises(RuntimeError):
            led._mutate_recipients_locked(did, _buggy)
        # ★同一個實例要能繼續寫★(交易有回捲)
        assert led.settle(did) == dl.CONFIRMED
        # ★別的實例也要能寫★(寫鎖有釋放)
        other = _led(tmp_path)
        did2 = other.begin(business_key="bk2", category="t",
                           recipients=["b@x.tw"])
        assert other.state_of(did2) == dl.SUBMITTING

    def test_an_overflowing_legacy_attempts_is_coerced_not_exploding(
            self, tmp_path):
        """binding 前就轉乾淨:int() 對超過 64-bit 的值不拋,binding 才炸。"""
        import json
        (tmp_path / "delivery_ledger.json").write_text(json.dumps({
            "d1": {"business_key": "bk", "category": "t", "state": dl.UNKNOWN,
                   "recipients": {"a@x.tw": dl.R_UNKNOWN},
                   "created_at": 1.0, "updated_at": 1.0,
                   "attempts": 10**30},
        }), encoding="utf-8")
        led = dl.DeliveryLedger(path=str(tmp_path / "delivery_ledger.json"))
        assert led.state_of("d1") == dl.UNKNOWN, "溢位的 attempts 讓整筆沒進來"
        did = led.begin(business_key="bk2", category="t",
                        recipients=["b@x.tw"])       # 帳本沒有被拖下水
        assert led.state_of(did) == dl.SUBMITTING


class TestSchemaVersionIsARealLedger:
    def test_the_version_is_written_by_hand_not_derived(self, tmp_path):
        led = _led(tmp_path)
        with closing(sqlite3.connect(led.path)) as c, c:
            v = c.execute(
                "SELECT value FROM meta WHERE key='schema_version'"
            ).fetchone()
        assert v and v[0] == str(dl._SCHEMA_VERSION)


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
