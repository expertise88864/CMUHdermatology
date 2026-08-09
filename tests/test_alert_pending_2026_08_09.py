# -*- coding: utf-8 -*-
"""[#71 批次 Z 下半] 「結果不明」的**暫時性**抑制。

★問題★ `_send_alert_email_via_smtp` 遇到 `DeliveryOutcomeUnknown` 時回 True，
呼叫端就寫下**永久**去重記號。批次 U 把 UNKNOWN 回查接上去之後，前提變了：

    回查 → 寄件備份【查無】 → ledger.resolve_unknown(delivered=False)
         → 帳本說「可以重寄」
         但 alert_email_sent.json 說「已寄過」→ marker 贏
         → 那一則止掛提醒【永遠不會再寄】

**兩個真相來源互相矛盾，而錯的那個贏。**

★為什麼不能只把 marker 拿掉★
回 False → 呼叫端釋放寄送權 → 下一輪掃描再寄 → 醫師每輪收到重複提醒。
那比現況更糟。需要的是第三種狀態：不重寄，但也不宣稱已送達。

★最重要的一條：抑制一定要有出口★
IMAP 長期不可用時回查永遠拿不到答案。沒有出口的話，這個機制會從「防重複」
變成**永久靜默漏寄** —— 2026-08-05 事故那個形狀。
"""
import importlib
import io
import json
import os
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from cmuh_common import alert_state as ast_mod  # noqa: E402

MAX = ast_mod.PENDING_MAX_AGE_SEC


# ══ 純邏輯（decide / is_suppressing）═══════════════════════════════════════
def _entry(age=0.0, now=10_000.0, did="d1"):
    return ast_mod.new_pending_entry(delivery_id=did, message_id="<m@x>",
                                     business_key="alert:nk", now=now - age)


class TestDecide:
    def test_a_delivered_record_is_promoted(self):
        assert ast_mod.decide_pending(_entry(), "confirmed", 10_000.0) == \
            ast_mod.PENDING_PROMOTE

    def test_a_partial_delivery_also_counts_as_delivered(self):
        """有人收到了就不可以整則重寄（其餘的人走帳本的補寄路徑）。"""
        assert ast_mod.decide_pending(_entry(), "partial", 10_000.0) == \
            ast_mod.PENDING_PROMOTE

    def test_a_failed_record_releases_the_suppression(self):
        assert ast_mod.decide_pending(_entry(), "failed", 10_000.0) == \
            ast_mod.PENDING_RELEASE

    @pytest.mark.parametrize("state", ["unknown", "submitting", "prepared",
                                       "", None, "   "])
    def test_an_unresolved_record_is_kept(self, state):
        """★三態不可以摺成兩態★ 查不出來就繼續等，不是「可以重寄」。"""
        assert ast_mod.decide_pending(_entry(age=1.0), state, 10_000.0) == \
            ast_mod.PENDING_KEEP

    def test_an_aged_unresolved_record_expires(self):
        """★出口★ 查不出結果又等太久 → 解除抑制（呼叫端會告警）。"""
        assert ast_mod.decide_pending(_entry(age=MAX + 1), "", 10_000.0) == \
            ast_mod.PENDING_EXPIRE

    def test_the_ledger_answer_beats_the_age(self):
        """已經有答案就用答案，不要因為久了就當成查不出來。"""
        assert ast_mod.decide_pending(_entry(age=MAX * 10), "confirmed",
                                      10_000.0) == ast_mod.PENDING_PROMOTE

    def test_the_state_comparison_is_case_insensitive(self):
        assert ast_mod.decide_pending(_entry(), "CONFIRMED", 10_000.0) == \
            ast_mod.PENDING_PROMOTE

    def test_the_outcome_set_is_closed(self):
        got = {ast_mod.PENDING_KEEP, ast_mod.PENDING_PROMOTE,
               ast_mod.PENDING_RELEASE, ast_mod.PENDING_EXPIRE}
        assert len(got) == 4, "回傳值撞名 → 呼叫端分不出來"


class TestSuppression:
    def test_a_fresh_entry_suppresses(self):
        assert ast_mod.is_suppressing(_entry(age=1.0), 10_000.0) is True

    def test_an_aged_entry_stops_suppressing_without_any_sweep(self):
        """★核心★ 出口不可以依賴掃描有沒有跑。

        如果「解除抑制」只發生在掃描裡，那麼掃描本身出問題（執行緒沒起來、
        程式那段時間沒開）就等於**永久抑制** —— 而永久抑制正是這整段
        程式要避免的東西。所以逾期的當下述詞就不再抑制。
        """
        assert ast_mod.is_suppressing(_entry(age=MAX + 1), 10_000.0) is False

    def test_nothing_is_not_suppression(self):
        for v in (None, {}, ""):
            assert ast_mod.is_suppressing(v, 10_000.0) is False

    @pytest.mark.parametrize("bad", ["不是數字", None, [], {"a": 1},
                                     float("nan"), float("inf")])
    def test_a_broken_since_is_treated_as_very_old(self, bad):
        """★方向★ 看不懂當成「剛剛才寫的」的話，一筆壞資料＝**永久抑制**。

        看不懂就讓它逾期 —— 逾期會告警，而永久抑制不會。
        """
        e = dict(_entry())
        e["since"] = bad
        # ★用真實量級的 now★ 壞值分兩種下場：拋例外的回 inf，falsy 的走
        #   `x or 0` 變成 epoch(0.0)。兩種都是「很舊」，但 epoch 要在真實
        #   時鐘下才看得出來 —— 假的 now=10_000 距離 epoch 只有 2.8 小時，
        #   量到的是我挑的假時鐘，不是被測的性質。
        now = 2_000_000_000.0
        assert ast_mod.is_suppressing(e, now) is False
        assert ast_mod.decide_pending(e, "", now) == ast_mod.PENDING_EXPIRE

    def test_suppressed_keys_skips_the_expired(self):
        recs = {"fresh": _entry(age=1.0), "old": _entry(age=MAX + 1)}
        assert ast_mod.suppressed_keys(recs, 10_000.0) == {"fresh"}


class TestPendingPersistence:
    def test_a_missing_file_is_not_an_error(self, tmp_path):
        got = ast_mod.load_alert_pending(str(tmp_path / "nope.json"))
        assert got.records == {} and got.unreadable is False

    def test_entries_survive_a_round_trip(self, tmp_path):
        p = str(tmp_path / "p.json")
        recs = {"nk": _entry()}
        with io.open(p, "w", encoding="utf-8") as fh:
            json.dump(recs, fh)
        got = ast_mod.load_alert_pending(p)
        assert got.records["nk"]["delivery_id"] == "d1"

    def test_non_dict_entries_are_dropped(self, tmp_path):
        p = str(tmp_path / "p.json")
        with io.open(p, "w", encoding="utf-8") as fh:
            json.dump({"good": _entry(), "junk": "字串"}, fh)
        assert set(ast_mod.load_alert_pending(p).records) == {"good"}

    def test_the_retention_filter_is_not_applied_on_load(self, tmp_path):
        """★載入時不可以丟掉舊的★

        在載入時就丟掉，等於讓「解除抑制」悄悄發生而**不告警** ——
        而告警正是逾期那條路最重要的產物。
        """
        p = str(tmp_path / "p.json")
        with io.open(p, "w", encoding="utf-8") as fh:
            json.dump({"old": _entry(age=MAX * 100)}, fh)
        assert "old" in ast_mod.load_alert_pending(p).records

    def test_a_failed_load_does_not_overwrite_the_disk(self, tmp_path,
                                                       monkeypatch):
        """讀不到就不可以拿空的去覆蓋 —— 覆蓋＝那幾則會被重寄。"""
        p = str(tmp_path / "p.json")
        monkeypatch.setattr(ast_mod, "load_json_dict_ex",
                            lambda *a, **k: ({}, "error"))
        d = ast_mod.pending_for_save(p, {"new": _entry()}, load_failed=True)
        assert d.should_write is False

    def test_a_recovered_load_merges_instead_of_replacing(self, tmp_path):
        """★自我修復★ 防毒放手之後就自動接回來，不必等下次重啟。"""
        p = str(tmp_path / "p.json")
        with io.open(p, "w", encoding="utf-8") as fh:
            json.dump({"disk": _entry()}, fh)
        d = ast_mod.pending_for_save(p, {"new": _entry()}, load_failed=True)
        assert d.should_write and set(d.payload) == {"disk", "new"}


# ══ 接線（main.py）════════════════════════════════════════════════════════
@pytest.fixture(scope="module")
def m():
    return importlib.import_module("main")


class _Led:
    def __init__(self, states=None):
        self.states = dict(states or {})
        self.asked = []

    def state_of(self, did):
        self.asked.append(did)
        return self.states.get(did, "")


class _App:
    """只帶這段流程需要的欄位（不建 Tk）—— 方法從生產類別借。"""

    def __init__(self, m, tmp_path, led=None):
        import threading
        self._m = m
        self._alert_state_lock = threading.Lock()
        self._alert_email_sent = {}
        self._alert_sent_load_failed = False
        self._alert_email_pending = {}
        self._alert_pending_load_failed = False
        self._alert_pending_dirty = False
        self._alert_email_inflight = set()
        self._led = led
        for name in ("_has_alert_email_been_sent", "_alert_dedup_hit_locked",
                     "_claim_alert_email", "_release_alert_email_claim",
                     "_mark_alert_email_pending", "_save_alert_pending_locked",
                     "_sweep_alert_pending", "_promote_alert_pending",
                     "_release_alert_pending", "_mark_alert_email_sent",
                     "_retry_alert_pending_save",
                     "_mark_alert_email_sent_locked",
                     "_drop_alert_pending_if_same"):
            setattr(self, name,
                    getattr(m.AutomationApp, name).__get__(self, _App))


@pytest.fixture
def app(m, tmp_path, monkeypatch):
    monkeypatch.setattr(m, "get_conf_path",
                        lambda name: os.path.join(str(tmp_path), name))
    led = _Led()
    monkeypatch.setattr(m, "_get_alert_ledger", lambda: led)
    a = _App(m, tmp_path, led)
    a._led = led
    return a


class _Res:
    def __init__(self, unknown=True, did="d1"):
        self.sent = True
        self.unknown = unknown
        self.delivery_id = did
        self.message_id = "<m@x>"
        self.business_key = "alert:nk"

    def __bool__(self):
        return self.sent


class TestWiring:
    def test_unknown_writes_pending_not_the_permanent_marker(self, app):
        """★驗收 1★ UNKNOWN → 不寫永久記號、有寫 pending。"""
        app._mark_alert_email_pending("nk", _Res())
        assert "nk" not in app._alert_email_sent, "★寫了永久記號★"
        assert "nk" in app._alert_email_pending

    def test_a_pending_entry_blocks_a_resend(self, app):
        """★驗收 2★ 抑制期間不重寄。"""
        app._mark_alert_email_pending("nk", _Res())
        assert app._has_alert_email_been_sent("nk") is True
        assert app._claim_alert_email("nk") is False

    def test_an_expired_pending_allows_a_resend(self, app):
        """★驗收 6 的一半★ 逾期就不再抑制（不必等掃描）。"""
        app._mark_alert_email_pending("nk", _Res())
        app._alert_email_pending["nk"]["since"] -= MAX + 1
        assert app._has_alert_email_been_sent("nk") is False
        assert app._claim_alert_email("nk") is True

    def test_pending_survives_a_restart(self, app, m, tmp_path):
        """★驗收 8★ 不落地的話，重啟就等於解除抑制 → 重寄。"""
        app._mark_alert_email_pending("nk", _Res())
        again = ast_mod.load_alert_pending(
            os.path.join(str(tmp_path), ast_mod.PENDING_FILENAME))
        assert "nk" in again.records

    def test_a_confirmed_record_is_promoted_to_the_permanent_marker(self, app):
        """★驗收 3★ 回查查到 → 升級成永久記號，pending 消失。"""
        app._mark_alert_email_pending("nk", _Res())
        app._led.states["d1"] = "confirmed"
        app._sweep_alert_pending(now=_now(app) + 1)
        assert "nk" in app._alert_email_sent
        assert "nk" not in app._alert_email_pending

    def test_a_failed_record_releases_the_suppression(self, app):
        """★驗收 4★ 回查查無 → pending 消失，下一輪會重寄。"""
        app._mark_alert_email_pending("nk", _Res())
        app._led.states["d1"] = "failed"
        app._sweep_alert_pending(now=_now(app) + 1)
        assert "nk" not in app._alert_email_pending
        assert "nk" not in app._alert_email_sent, "查無卻寫了已寄記號"
        assert app._claim_alert_email("nk") is True

    def test_an_unresolved_record_is_left_alone(self, app):
        """★驗收 5★ 查不出來 → 三態不可摺成兩態。"""
        app._mark_alert_email_pending("nk", _Res())
        app._led.states["d1"] = "unknown"
        app._sweep_alert_pending(now=_now(app) + 1)
        assert "nk" in app._alert_email_pending
        assert "nk" not in app._alert_email_sent

    def test_an_expired_entry_is_swept_and_reported(self, app, caplog):
        """★驗收 6★ 逾期 → 告警 + 解除抑制（★不可以無限抑制★）。"""
        import logging
        app._mark_alert_email_pending("nk", _Res())
        app._alert_email_pending["nk"]["since"] -= MAX + 1
        with caplog.at_level(logging.ERROR):
            app._sweep_alert_pending(now=_now(app))
        assert "nk" not in app._alert_email_pending
        assert any("解除" in r.getMessage() for r in caplog.records), \
            "★解除抑制卻沒有告警 —— 沒人知道那一則可能漏了★"

    def test_a_ledger_that_cannot_answer_does_not_release(self, app):
        """問不到帳本 ≠ 沒送到。維持原狀，下一輪再問。"""
        def _boom(did):
            raise RuntimeError("讀不到")
        app._led.state_of = _boom
        app._mark_alert_email_pending("nk", _Res())
        app._sweep_alert_pending(now=_now(app) + 1)
        assert "nk" in app._alert_email_pending

    def test_no_ledger_at_all_still_expires(self, app, m, monkeypatch):
        """★出口不可以依賴帳本★ 沒有帳本時，逾期那條路仍然要走得通。"""
        monkeypatch.setattr(m, "_get_alert_ledger", lambda: None)
        app._mark_alert_email_pending("nk", _Res())
        app._alert_email_pending["nk"]["since"] -= MAX + 1
        app._sweep_alert_pending(now=_now(app))
        assert "nk" not in app._alert_email_pending

    def test_a_normal_success_never_becomes_pending(self, app):
        """★驗收 9 反方向★ 正常成功的路徑不可以因此變成 pending。"""
        app._mark_alert_email_sent("nk")
        assert app._alert_email_pending == {}
        assert app._has_alert_email_been_sent("nk") is True

    def test_a_record_without_a_delivery_id_still_expires(self, app):
        """帳本登記失敗（沒有 delivery_id）→ 永遠問不到，只能靠逾期。"""
        app._mark_alert_email_pending("nk", _Res(did=""))
        app._alert_email_pending["nk"]["since"] -= MAX + 1
        app._sweep_alert_pending(now=_now(app))
        assert "nk" not in app._alert_email_pending


def _now(app):
    import time
    return time.time()


# ══ 接上去了才算數 ════════════════════════════════════════════════════════
class TestItIsActuallyWired:
    @staticmethod
    def _tree():
        import ast as _ast
        return _ast.parse(io.open(os.path.join(REPO_ROOT, "src", "main.py"),
                                  encoding="utf-8").read())

    def test_both_send_paths_branch_on_unknown(self):
        """★兩條寄送路徑都要分流★ 漏掉一條，那條就還是寫永久記號。"""
        text = io.open(os.path.join(REPO_ROOT, "src", "main.py"),
                       encoding="utf-8").read()
        assert text.count("_mark_alert_email_pending(nk, _res)") == 2, (
            "止掛信有兩條寄送路徑（行事曆通知 + 遠期背景掃描）—— "
            "只改一條的話，另一條仍會把 UNKNOWN 寫成永久記號")

    def test_the_sweep_has_a_caller(self):
        """★[wired-up-or-it-does-not-exist]★ 沒有呼叫端＝那個 docstring 是假的。"""
        text = io.open(os.path.join(REPO_ROOT, "src", "main.py"),
                       encoding="utf-8").read()
        assert "after=self._sweep_alert_pending" in text, (
            "★掃描沒有被接上任何輪次 → 抑制永遠不會解除★")

    def test_the_sweep_runs_after_the_reconcile(self):
        """掃描要用的是收斂【之後】的帳本狀態。"""
        import ast as _ast
        import inspect
        import textwrap
        m = importlib.import_module("main")
        src = textwrap.dedent(inspect.getsource(m._kick_off_alert_reconcile))
        tree = _ast.parse(src)
        at = {}
        for n in _ast.walk(tree):
            if isinstance(n, _ast.Call):
                name = getattr(n.func, "id", None)
                if name:
                    at.setdefault(name, n.lineno)
        assert at["_reconcile_alert_deliveries"] < at["after"], \
            "掃描排在回查前面 → 每一輪看到的都是上一輪的結果"

    def test_the_sweep_still_runs_when_the_reconcile_blows_up(self, m,
                                                              monkeypatch):
        """★出口不可以依賴回查成功★ 那正是「出口依賴一個會壞的東西」。"""
        import threading
        hit = threading.Event()

        def _boom(*a, **k):
            raise RuntimeError("回查炸了")

        monkeypatch.setattr(m, "_reconcile_alert_deliveries", _boom)
        assert m._kick_off_alert_reconcile(after=hit.set) is True
        assert hit.wait(5.0), "★回查失敗就不掃描 → 抑制永遠不會逾期解除★"

    def test_the_send_helper_returns_the_result_type(self, m):
        """★回傳型別要釘住★ 退回裸 bool 的話，`getattr` 會讓抑制安靜失效。"""
        import ast as _ast
        import inspect
        import textwrap
        src = textwrap.dedent(inspect.getsource(m._send_alert_email_via_smtp))
        returns = [n for n in _ast.walk(_ast.parse(src))
                   if isinstance(n, _ast.Return) and n.value is not None]
        assert returns, "找不到任何 return（測試自己失效了）"
        for r in returns:
            assert isinstance(r.value, _ast.Call) and \
                getattr(r.value.func, "id", "") == "SendResult", \
                f"第 {r.lineno} 行的 return 不是 SendResult"

    def test_the_result_is_boolean_compatible(self, m):
        """四個舊呼叫端寫 `bool(...)`，兩個寫 `if ...` —— 不可以偷偷改行為。"""
        assert bool(m.SendResult(True)) is True
        assert bool(m.SendResult(False)) is False
        assert bool(m.SendResult(True, unknown=True)) is True, \
            "★結果不明仍然是「不要重寄」★"


# ══ 外審 2026-08-09（批次 AB 第 1 輪）═════════════════════════════════════
class TestGenerationRace:
    """★#1★ 掃描是「先取快照 → 做 I/O → 再改」。

    那段空窗裡，同一個 notify_key 可能已經逾期、被重寄、又寫了一筆【新的】
    抑制。舊那一輪若照 key 無條件刪，刪掉的是**新的**那一筆 ——
    抑制消失 → 下一輪再寄 → 而且會一直循環。
    """

    def test_a_replaced_entry_is_not_deleted_by_the_old_sweep(self, app):
        old = ast_mod.new_pending_entry(delivery_id="old", now=1.0)
        app._alert_email_pending["nk"] = dict(old)
        # 掃描期間被換成新的一筆（逾期 → 重寄 → 又 UNKNOWN）
        app._mark_alert_email_pending("nk", _Res(did="new"))
        new_gen = app._alert_email_pending["nk"]["gen"]
        # 舊那一輪拿著【舊的】快照回來要解除
        app._release_alert_pending("nk", old, "舊快照")
        assert "nk" in app._alert_email_pending, "★新的那一筆被舊快照刪掉了★"
        assert app._alert_email_pending["nk"]["gen"] == new_gen

    def test_a_replaced_entry_is_not_promoted_by_the_old_sweep(self, app):
        old = ast_mod.new_pending_entry(delivery_id="old", now=1.0)
        app._alert_email_pending["nk"] = dict(old)
        app._mark_alert_email_pending("nk", _Res(did="new"))
        app._promote_alert_pending("nk", old)
        assert "nk" not in app._alert_email_sent, (
            "★用舊快照的結果替新的那一筆寫下永久記號 → 新的那則永遠不會再寄★")
        assert "nk" in app._alert_email_pending

    def test_the_same_entry_is_still_deleted(self, app):
        """★反方向★ 世代比對不可以變成「永遠不刪」。"""
        app._mark_alert_email_pending("nk", _Res())
        entry = dict(app._alert_email_pending["nk"])
        app._release_alert_pending("nk", entry, "同一筆")
        assert "nk" not in app._alert_email_pending

    def test_two_entries_have_different_generations(self):
        a = ast_mod.new_pending_entry(delivery_id="d", now=1.0)
        b = ast_mod.new_pending_entry(delivery_id="d", now=1.0)
        assert a["gen"] != b["gen"], "同樣的輸入必須產生不同的世代"
        assert ast_mod.same_pending_generation(a, a) is True
        assert ast_mod.same_pending_generation(a, b) is False

    def test_an_entry_without_a_gen_falls_back_to_content(self):
        """舊格式（磁碟上還沒有 gen 的那些）也要能比對。"""
        a = {"since": 1.0, "delivery_id": "d", "message_id": "<m>"}
        assert ast_mod.same_pending_generation(a, dict(a)) is True
        b = dict(a, delivery_id="other")
        assert ast_mod.same_pending_generation(a, b) is False

    def test_nothing_never_matches(self):
        """分不出來就當成不同 —— 少刪一次只是下輪再處理，錯刪會重複寄信。"""
        assert ast_mod.same_pending_generation(None, None) is False
        assert ast_mod.same_pending_generation({"gen": "x"}, None) is False


class TestPersistenceIsRetried:
    """★#2★ 寫失敗不可以就這樣算了。

    抑制只活在記憶體的話，重啟後既沒有永久記號、也沒有 pending ——
    那一則結果不明的提醒會被重寄，即使帳本裡那筆仍是 UNKNOWN。
    """

    def test_a_failed_write_is_marked_dirty(self, app, m, monkeypatch):
        def _boom(path, payload):
            raise OSError("磁碟忙")
        monkeypatch.setattr(m, "_atomic_write_json", _boom)
        app._mark_alert_email_pending("nk", _Res())
        assert app._alert_pending_dirty is True, "★寫失敗卻沒留下任何痕跡★"

    def test_the_next_sweep_retries_the_write(self, app, m, monkeypatch,
                                              tmp_path):
        calls = []
        real = m._atomic_write_json

        def _flaky(path, payload):
            calls.append(path)
            if len(calls) == 1:
                raise OSError("磁碟忙")
            return real(path, payload)

        monkeypatch.setattr(m, "_atomic_write_json", _flaky)
        app._mark_alert_email_pending("nk", _Res())
        assert app._alert_pending_dirty is True
        app._sweep_alert_pending(now=_now(app) + 1)
        assert app._alert_pending_dirty is False, "★下一輪沒有重試落地★"
        again = ast_mod.load_alert_pending(
            os.path.join(str(tmp_path), ast_mod.PENDING_FILENAME))
        assert "nk" in again.records, "重試之後仍然沒有落地"

    def test_a_successful_write_clears_the_flag(self, app):
        app._alert_pending_dirty = True
        app._mark_alert_email_pending("nk", _Res())
        assert app._alert_pending_dirty is False

    def test_the_retry_is_a_noop_when_clean(self, app):
        assert app._retry_alert_pending_save() is False


class TestFutureTimestamps:
    """★#3★ `since > now` 時年齡是負的 → 被判成「很新」。

    寫進去之後系統時間被往回調（診間電腦對時、換電池、手動改），或 `since`
    被寫成一個很大但有限的數字，就會安靜地抑制那則臨床提醒好幾年 ——
    而文件寫著上限是六小時。★又一個沒有出口的 fail-closed★
    """

    def test_a_far_future_timestamp_is_treated_as_invalid(self):
        e = ast_mod.new_pending_entry(delivery_id="d", now=2_000_000_000.0)
        now = 1_000_000_000.0            # 時鐘被往回調了 30 年
        assert ast_mod.is_suppressing(e, now) is False
        assert ast_mod.decide_pending(e, "", now) == ast_mod.PENDING_EXPIRE

    def test_a_small_clock_skew_is_tolerated(self):
        """★反方向★ 正常的幾秒偏差不可以害它立刻逾期。

        ★反例要用【絕對】的量，不可以從被測常數推出來★
        第一版寫 `now + PENDING_CLOCK_SKEW_SEC / 2` —— 把容忍度改成 0
        的話，測試資料也跟著變成 0，於是那個突變照樣全綠：
        反例沒有隔離被測的那條規則。這裡直接要求「至少容忍 30 秒」，
        那是診間電腦 NTP 對時的正常抖動量級。
        """
        now = 1_000_000_000.0
        e = ast_mod.new_pending_entry(delivery_id="d", now=now + 30.0)
        assert ast_mod.is_suppressing(e, now) is True
        assert ast_mod.decide_pending(e, "", now) == ast_mod.PENDING_KEEP

    def test_the_sweep_releases_a_future_dated_entry(self, app):
        app._mark_alert_email_pending("nk", _Res())
        app._alert_email_pending["nk"]["since"] = _now(app) + 86400 * 365
        app._sweep_alert_pending(now=_now(app))
        assert "nk" not in app._alert_email_pending, (
            "★未來的時間戳讓抑制永遠不會逾期★")


# ══ 外審 2026-08-09（批次 AB 第 2 輪）═════════════════════════════════════
class TestPromotionHasNoWindow:
    """★#1★ 升級時「刪抑制」與「立永久記號」之間不可以有空窗。

    上一版是兩次分開上鎖，順序還是「先刪、後立」。那兩次之間，
    通知掃描緒呼叫 `_claim_alert_email()` 會**兩個去重來源都看不到**
    → 取得寄送權 → 重寄一封帳本已經確認送達的信。
    修好了漏寄，卻換來重複寄送 —— 修正必須放在一起判斷。
    """

    def test_no_claim_can_slip_between_delete_and_mark(self, app, m,
                                                       monkeypatch):
        """★核心★ 在【刪掉 pending 的那一刻】插進去搶寄送權，必須搶不到。

        做法：把落地那一步換成 hook —— 它在鎖內、已經刪掉 pending 之後
        被呼叫。若永久記號還沒立，這時 `_claim_alert_email` 就會成功。
        """
        app._mark_alert_email_pending("nk", _Res())
        entry = dict(app._alert_email_pending["nk"])
        app._led.states["d1"] = "confirmed"
        seen = {}
        real = app._save_alert_pending_locked

        def _hook():
            # 這一刻 pending 已經被刪掉了 —— 永久記號立了沒有？
            seen["marked"] = "nk" in app._alert_email_sent
            seen["pending"] = "nk" in app._alert_email_pending
            real()

        app._save_alert_pending_locked = _hook
        app._promote_alert_pending("nk", entry)
        assert seen["pending"] is False, "測試自己失效了（pending 還在）"
        assert seen["marked"] is True, (
            "★刪掉抑制的當下永久記號還沒立 → 那個空窗會被掃描緒插進來重寄★")

    def test_a_concurrent_claim_never_wins_during_promotion(self, app):
        """真的開一條緒去搶，重複跑幾次都不可以搶到。"""
        import threading
        for _ in range(50):
            app._alert_email_sent.clear()
            app._alert_email_pending.clear()
            app._mark_alert_email_pending("nk", _Res())
            entry = dict(app._alert_email_pending["nk"])
            app._led.states["d1"] = "confirmed"
            won = []

            def _claimer(won=won):
                for _ in range(200):
                    if app._claim_alert_email("nk"):
                        won.append(True)
                        app._release_alert_email_claim("nk")
                        return

            t = threading.Thread(target=_claimer)
            t.start()
            app._promote_alert_pending("nk", entry)
            t.join(5.0)
            assert not won, "★升級期間有人搶到寄送權 → 重複寄信★"

    def test_promotion_still_ends_with_the_permanent_marker(self, app):
        """★反方向★ 不可以為了消除空窗就乾脆不升級。"""
        app._mark_alert_email_pending("nk", _Res())
        entry = dict(app._alert_email_pending["nk"])
        app._led.states["d1"] = "confirmed"
        app._promote_alert_pending("nk", entry)
        assert "nk" in app._alert_email_sent
        assert "nk" not in app._alert_email_pending


class TestShutdownFlushesPending:
    """★#2★ 只在掃描裡重試 → 「寫失敗 → 磁碟恢復 → 還沒輪到掃描就重啟」
    這條路上抑制只活在記憶體，重啟後那則結果不明的信會被重寄。
    自動更新重啟很頻繁，不是罕見路徑。
    """

    @staticmethod
    def _src(name):
        import ast as _ast
        import io as _io
        import os as _os
        tree = _ast.parse(_io.open(_os.path.join(REPO_ROOT, "src", "main.py"),
                                   encoding="utf-8").read())
        for n in _ast.walk(tree):
            if isinstance(n, _ast.FunctionDef) and n.name == name:
                return n
        raise AssertionError(f"找不到 {name}（測試自己失效了）")

    def test_both_exit_paths_retry_the_pending_write(self):
        """兩條退出路徑（更新重啟、關閉）都要補寫。"""
        import ast as _ast
        import io as _io
        import os as _os
        text = _io.open(_os.path.join(REPO_ROOT, "src", "main.py"),
                        encoding="utf-8").read()
        tree = _ast.parse(text)
        hits = 0
        for n in _ast.walk(tree):
            if not isinstance(n, _ast.Call):
                continue
            f = n.func
            if isinstance(f, _ast.Attribute) and \
                    f.attr == "_retry_alert_pending_save":
                hits += 1
        # 掃描開頭 1 次 + 兩條退出路徑各 1 次
        assert hits >= 3, (
            f"只有 {hits} 個呼叫端 —— 更新重啟／關閉其中一條沒有補寫，"
            "那條路上抑制紀錄會憑空消失 → 重寄")

    def test_the_retry_sits_next_to_the_ledger_flush(self):
        """要和帳本排空放在一起（同一個「退出前收尾」的臨界點）。"""
        import io as _io
        import os as _os
        text = _io.open(_os.path.join(REPO_ROOT, "src", "main.py"),
                        encoding="utf-8").read()
        for anchor in ("_flush_delivery_ledger_before_exit()\n"
                       "                # ★[外審第 2 輪 #2]",
                       "_flush_delivery_ledger_before_exit()\n"
                       "            # ★[外審第 2 輪 #2]"):
            if anchor in text:
                return
        raise AssertionError("補寫沒有接在退出前的帳本排空旁邊")

    def test_a_dirty_flag_survives_a_failed_retry(self, app, m, monkeypatch):
        """★最後一次也失敗★ 旗標要留著（不可以假裝成功）。"""
        def _boom(path, payload):
            raise OSError("還是不行")
        monkeypatch.setattr(m, "_atomic_write_json", _boom)
        app._mark_alert_email_pending("nk", _Res())
        assert app._alert_pending_dirty is True
        assert app._retry_alert_pending_save() is True
        assert app._alert_pending_dirty is True, "★失敗卻把旗標清掉了★"
