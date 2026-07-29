# -*- coding: utf-8 -*-
"""[2026-08-02 第二輪外審 P1-04] anchor 寫入失敗被吞掉,record() 照樣回 True。

`record()` 的流程是:append JSONL → 更新 `_last_hash`/`_last_seq` → `_write_anchor()`。
`_write_anchor()` 明明會回傳成敗(它的 docstring 還寫著「必須回報成敗」),但
`record()` 把回傳值丟掉、無條件 `return True`。

而 anchor 存在的理由正是「讓【截尾】變得可偵測」—— 雜湊鏈自己證不了後面還有沒有
紀錄,要靠 anchor 記下末筆 seq/hash 才能發現有人把尾巴砍掉。所以 anchor 沒寫成功
＝這一筆【無法被證明完整】。

後果:
  * writer loop 只在 `record()` 回 False 時累加 `_ledger_write_failures` → 不增加
  * 關機 flush 視為完全落地
  * 健康檢查要等到下一次 audit health check 才可能發現 anchor 對不上
「本次紀錄是否完整」與 `record()` 的 bool 語意就此脫節。

修法:改回結構化結果 `RecordResult(entry_written, anchor_written, fully_verifiable)`。
它對 `if not result:` 仍為真/假可用(`__bool__` = entry_written),既有呼叫端不必改
語意;但呼叫端可以另外看 `anchor_written` 決定要不要計一筆「無法證明完整」。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common import action_ledger as mod  # noqa: E402
from cmuh_common.action_ledger import ActionLedger  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_counters(monkeypatch):
    """★writer loop 用 `global` 改模組層計數 —— 不隔離就會漏到別的測試★

    實際踩到過:`test_an_anchor_failure_does_not_swallow_the_mismatch_notification`
    讓 main._ledger_unanchored 留在 1,隨機順序下害 test_healthy_ledger_no_email
    以為稽核不健康而變紅(單獨跑卻是綠的)。
    """
    import main
    monkeypatch.setattr(main, "_ledger_unanchored", 0, raising=False)
    monkeypatch.setattr(main, "_ledger_anchor_incidents", 0, raising=False)


@pytest.fixture
def led(tmp_path):
    return ActionLedger(str(tmp_path / "ledger.jsonl"))


def _break_anchor(monkeypatch):
    def _boom(*_a, **_k):
        raise OSError("模擬：anchor 檔被鎖住")
    monkeypatch.setattr(mod, "atomic_write_json", _boom)


# ─── record() 要說出 anchor 有沒有寫成功 ───────────────────────────────────
def test_anchor_failure_is_reported_not_swallowed(led, monkeypatch):
    """★核心★ JSONL 寫成功、anchor 寫失敗 → 這一筆無法被證明完整,必須說出來。"""
    _break_anchor(monkeypatch)
    r = led.record("F1", "UVB", outcome="ok")

    assert r.entry_written is True, "紀錄本身確實落地了"
    assert r.anchor_written is False
    assert r.fully_verifiable is False, "★anchor 沒寫成 = 截尾無法偵測★"


def test_a_normal_record_is_fully_verifiable(led):
    r = led.record("F1", "UVB", outcome="ok")
    assert r.entry_written and r.anchor_written and r.fully_verifiable
    assert os.path.exists(led.anchor_path)


def test_the_result_still_works_as_a_boolean(led, monkeypatch):
    """★不可矯枉過正★ 既有呼叫端寫的是 `if not _action_ledger().record(...)`,
    語意是「紀錄有沒有落地」—— 不可因為 anchor 失敗就變成 False 而灌爆
    `_ledger_write_failures`(那個計數的意思是「這筆稽核沒寫進去」)。"""
    _break_anchor(monkeypatch)
    r = led.record("F1", "UVB", outcome="ok")
    assert bool(r) is True, "entry 有寫進去 → 布林仍為真"

    monkeypatch.undo()
    assert bool(led.record("F1", "UVB", outcome="ok")) is True


def test_a_dropped_entry_is_false_in_both_senses(led, monkeypatch):
    """真的沒寫進去(硬上限/磁碟拒寫)時,兩個旗標都要是 False。"""
    monkeypatch.setattr(ActionLedger, "_over_hard_cap", lambda _self: True)
    r = led.record("F1", "UVB", outcome="ok")
    assert bool(r) is False
    assert r.entry_written is False and r.fully_verifiable is False


# ─── 呼叫端要把它算進健康狀態(★真的跑一次,不是看原始碼有沒有那個字★)────────
#
# 我第一版這幾支只做 `assert "_ledger_unanchored" in inspect.getsource(...)`。
# 結果:patch script 在中途中止 → 宣告與健康檢查根本沒被改到,writer loop 卻已經
# 在動那兩個【不存在的名字】→ 執行時 NameError,被外層 except 吞掉,連 mismatch
# 通知都一起被跳過。而那幾支「檢查原始碼字串」的測試全部是綠的。
# 這正是這個 repo 記過的「測試給假信心」。改成真的把 writer loop 跑起來。
def _drive_writer(monkeypatch, anchor_results):
    """把 record() 換成依序回傳指定 anchor 結果的 stub,實際跑一次 writer loop。"""
    import main
    from cmuh_common.action_ledger import RecordResult
    seq = list(anchor_results)

    class _L:
        def record(self, *_a, **_k):
            return RecordResult(True, seq.pop(0) if seq else True)

    monkeypatch.setattr(main, "_action_ledger", lambda: _L())
    q = main.Queue(maxsize=16)
    for i in range(len(anchor_results)):
        q.put_nowait(("his_field", f"動作{i}", {"outcome": "ok"}, "ts", None))
    q.put_nowait(None)
    main._ledger_writer_loop(q)
    return main


def test_writer_counts_an_anchor_failure(monkeypatch):
    """★核心(runtime)★ anchor 失敗 → 尚未被涵蓋的筆數要增加。"""
    main = _drive_writer(monkeypatch, [False, False])
    assert main._ledger_unanchored == 2
    assert main._ledger_anchor_incidents == 2


def test_a_later_successful_anchor_clears_the_backlog(monkeypatch):
    """★[外審第1輪] anchor 記的是【累計】末筆 seq/hash★

    後來任何一次成功的 anchor 更新,就把先前那幾筆也一起證明了。只留一個永不歸零
    的計數 → 一次短暫失敗之後,每一次健康檢查都報「有紀錄無法證明完整」並持續寄信,
    而那時鏈其實已經完整。
    """
    main = _drive_writer(monkeypatch, [False, False, True])
    assert main._ledger_unanchored == 0, "後來成功的 anchor 應已涵蓋先前那幾筆"
    assert main._ledger_anchor_incidents == 2, "但確實發生過兩次,log 要看得到"


def test_an_anchor_failure_does_not_swallow_the_mismatch_notification(monkeypatch):
    """★NameError 那次真正的傷害★ writer 若在計數處炸掉,同一筆的 mismatch
    即時通知也會一起被外層 except 吞掉 —— 回讀不符就此無人知曉。"""
    import main
    from cmuh_common.action_ledger import RecordResult
    notified = []
    monkeypatch.setattr(
        main, "_notify_audit_mismatch",
        lambda action, detail, locator=None, ts="", expected="":
            notified.append(action))
    monkeypatch.setattr(main, "_action_ledger",
                        lambda: type("L", (), {
                            "record": lambda s, *a, **k: RecordResult(True, False)})())
    q = main.Queue(maxsize=8)
    q.put_nowait(("his_field", "F2 UVB 劑量",
                  {"outcome": "mismatch", "detail": "回讀不符"}, "ts", None))
    q.put_nowait(None)
    main._ledger_writer_loop(q)

    assert notified == ["F2 UVB 劑量"], "anchor 失敗不可連帶吃掉回讀不符通知"


def test_health_check_reports_and_is_not_green(monkeypatch):
    """★[外審第1輪] ok=False 卻留著 level="ok" → 設定頁顯示綠色 ✅★"""
    import main
    monkeypatch.setattr(main, "_ledger_unanchored", 3, raising=False)
    monkeypatch.setattr(main, "_action_ledger", lambda: type("L", (), {
        "health_check": lambda s, **k: {"ok": True, "level": "ok",
                                        "summary": "一切正常"}})())
    snap = main.audit_health_check(notify=False)

    assert snap["ok"] is False
    assert snap["level"] == "warn", f"不可留在 ok:{snap}"
    assert "3 筆" in snap["summary"]


def test_health_check_keeps_an_existing_error_level(monkeypatch):
    """★不可矯枉過正★ 已經是 error 的不可被降級成 warn。"""
    import main
    monkeypatch.setattr(main, "_ledger_unanchored", 1, raising=False)
    monkeypatch.setattr(main, "_action_ledger", lambda: type("L", (), {
        "health_check": lambda s, **k: {"ok": False, "level": "error",
                                        "summary": "鏈驗證失敗"}})())
    assert main.audit_health_check(notify=False)["level"] == "error"


def test_health_check_stays_green_when_everything_is_fine(monkeypatch):
    import main
    monkeypatch.setattr(main, "_ledger_unanchored", 0, raising=False)
    monkeypatch.setattr(main, "_action_ledger", lambda: type("L", (), {
        "health_check": lambda s, **k: {"ok": True, "level": "ok",
                                        "summary": "一切正常"}})())
    snap = main.audit_health_check(notify=False)
    assert snap["ok"] is True and snap["level"] == "ok"
