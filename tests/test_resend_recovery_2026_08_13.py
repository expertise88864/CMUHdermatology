# -*- coding: utf-8 -*-
"""[批次AE-1] durable resend 狀態機(外審 2026-08-13 P1-01/02/03 + P2-02)。

★核心不變式★:只要帳本還認為某筆臨床信欠補寄,durable payload
(body_text)就必須還在;「欠補寄」由資料庫回答(resends_owed),
不靠 call-stack 的順序 —— 所以【每一個 crash point】之後,新的 process
只憑資料庫就能把補寄接下去。

審查點名的 crash 注入(逐一對應):
  1. Sent 查無 → resolve_unknown(False) 落地後 crash → 掃描接手照補。
  2. 子紀錄 claim COMMIT 後、send 前 crash → 子收斂 FAILED → 親再補。
  3. 子紀錄 pre-DATA 確定失敗 → 重啟後再試;上限一到明確放棄+告警。
  4. 子 UNKNOWN → Sent 查無 → 重啟 → 再試。
  5. 兩個 process 同時搶同一筆補寄 → 只有一個成功(claim 交易仲裁)。
  6. 補寄送達 → 鏈關 → body 才 GC。
  7. PARTIAL(暫時被拒)重啟後也走同一條 durable 路(P1-03)。
"""
import importlib
import io
import os
import sqlite3
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

dl = importlib.import_module("cmuh_common.delivery_ledger")
dr = importlib.import_module("cmuh_common.delivery_reconcile")

T0 = 1_000_000.0


class _Clock:
    def __init__(self, t=T0):
        self.t = float(t)

    def __call__(self):
        return self.t

    def advance(self, sec):
        self.t += float(sec)
        return self.t


@pytest.fixture()
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(dl, "_now", c)
    # 節流不是這裡的受測物(claim 機制另有測試)
    monkeypatch.setattr(dr, "EVERY_SEC", 0.0)
    return c


def _led(tmp_path, name="ledger.sqlite3"):
    return dl.DeliveryLedger(path=str(tmp_path / name))


def _capture_send(monkeypatch, result=None, exc=None):
    import cmuh_common.smtp_mail as sm
    sent = []

    def _fake(**kw):
        sent.append(kw)
        if exc is not None:
            raise exc
        return dict(result or {})

    monkeypatch.setattr(sm, "send_mail", _fake)
    return sent


def _failed_parent(led, clock, *, recipients=("a@x.tw",), bk="bk",
                   body="會診清單:3F 王O明 皮膚科照會", msgid="<m1@x>"):
    """一筆已被否證(Sent 查無)的親紀錄 —— 欠一次補寄的起點。"""
    did = led.begin(business_key=bk, category="consult",
                    recipients=list(recipients), subject="皮膚科會診通知",
                    message_id=msgid, body_text=body)
    led.settle(did, unknown=True)
    led.resolve_unknown(did, delivered=False)       # 生產順序:查無 → 否證
    return did


class TestBodySurvivesUntilTheChainCloses:
    """★P1-01 的地基★ FAILED/PARTIAL 還欠補寄 —— payload 不可以先死。"""

    def test_a_refuted_record_keeps_its_body(self, tmp_path, clock):
        led = _led(tmp_path)
        did = _failed_parent(led, clock)
        assert led.state_of(did) == dl.FAILED
        assert led.get(did)["body_text"] != "", (
            "★否證的當下就把 body 清掉★ 補寄建立前 crash,這封信永久消失"
            "(2026-08-13 P1-01 的 crash 窗口)")

    def test_a_partial_record_keeps_its_body(self, tmp_path, clock):
        led = _led(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["ok@x.tw", "miss@x.tw"],
                        message_id="<m@x>", body_text="內文")
        led.settle(did, refused={"miss@x.tw": (421, "busy")})
        assert led.state_of(did) == dl.PARTIAL
        assert led.get(did)["body_text"] != "", (
            "★PARTIAL 清 body★ 重啟後暫時被拒的那位就永遠沒得補(P1-03)")

    def test_abandon_clears_the_body(self, tmp_path, clock):
        led = _led(tmp_path)
        did = _failed_parent(led, clock)
        led.abandon_recipient_retry(did, note="測試放棄")
        assert led.get(did)["body_text"] == "", (
            "明確放棄=鏈關閉,payload 沒有理由再留")


class TestCrashRecoveryIsDbDriven:
    """★P1-01/02★ 每個 crash point 之後,新 process 只憑資料庫接手。"""

    def test_crash_after_resolve_still_resends(self, tmp_path, clock,
                                               monkeypatch):
        """crash point 1:resolve 落地後、補寄建立前 process 死掉。"""
        led = _led(tmp_path)
        did = _failed_parent(led, clock)
        # ★crash★:沒有任何補寄發生。重啟後只剩資料庫 → 掃描接手。
        sent = _capture_send(monkeypatch)
        clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
        dr.Reconciler(lambda: led).run_once(now=clock.t,
                                            finder=lambda m: None)
        assert len(sent) == 1, (
            "★resolve 之後 crash,這封信就永久消失★ —— 「欠補寄」必須由"
            "資料庫回答,不能靠 call-stack 的順序")
        assert sent[0]["recipients"] == ["a@x.tw"]
        assert "王O明" in sent[0]["body"], "要用【落地的】原文補"
        # crash point 6:補寄送達 → 回寫 → 鏈關 → body 才 GC
        assert led.state_of(did) == dl.CONFIRMED
        assert led.get(did)["body_text"] == ""
        kids = led.resend_children(did)
        assert len(kids) == 1
        assert kids[0]["kind"] == dl.KIND_AUTO_RESEND

    def test_the_snapshot_is_not_trusted(self, tmp_path, clock, monkeypatch):
        """補寄一律重讀資料庫 —— 呼叫端手上的快照跨越了 COMMIT。"""
        led = _led(tmp_path)
        did = _failed_parent(led, clock)
        sent = _capture_send(monkeypatch)
        stale = {"delivery_id": did, "parent_id": "",
                 "body_text": "快照裡的舊內容",
                 "recipients": {"snapshot@x.tw": dl.R_TRANSIENT},
                 "subject": "舊主旨", "business_key": "bk",
                 "category": "consult"}
        dr.Reconciler(lambda: led)._resend_from_body_text(led, stale)
        assert sent, "補寄本身要發生"
        assert "王O明" in sent[0]["body"] and "快照" not in sent[0]["body"], (
            "★吃了快照★ crash 之後重來只剩資料庫 —— 平時也必須走同一條路")
        assert sent[0]["recipients"] == ["a@x.tw"]

    def test_crash_between_claim_and_send_recovers(self, tmp_path, clock,
                                                   monkeypatch):
        """crash point 2:子紀錄 COMMIT 了,send_mail 一次都沒跑。"""
        led = _led(tmp_path)
        did = _failed_parent(led, clock)
        kid = led.claim_resend_child(did, business_key="bk",
                                     category="consult",
                                     recipients=["a@x.tw"], subject="s",
                                     message_id="<c1@x>")
        assert kid
        # ★crash★:send 從未執行。重啟 → 子卡 SUBMITTING → Sent 查無
        #   → 子 FAILED → 親仍欠 → 第二次嘗試。
        sent = _capture_send(monkeypatch)
        clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
        dr.Reconciler(lambda: led).run_once(now=clock.t,
                                            finder=lambda m: False)
        assert len(sent) == 1, (
            "★兩封都證明沒寄出,卻永遠不再寄★(2026-08-13 P1-02):"
            "「有任何子紀錄就不補」把 crash 過的 claim 當成已完成的工作")
        autos = [c for c in led.resend_children(did)
                 if c["kind"] == dl.KIND_AUTO_RESEND]
        assert len(autos) == 2, "第二次嘗試要有自己的一筆帳"
        assert led.state_of(did) == dl.CONFIRMED

    def test_unknown_child_sent_miss_then_restart_retries(
            self, tmp_path, clock, monkeypatch):
        """crash point 4:子 UNKNOWN → 重啟 → Sent 查無 → 再試。"""
        import cmuh_common.smtp_mail as sm
        led = _led(tmp_path)
        did = _failed_parent(led, clock)
        _capture_send(monkeypatch, exc=sm.DeliveryOutcomeUnknown("逾時"))
        kid = dr.Reconciler(lambda: led)._resend_from_body_text(
            led, led.get(did))
        assert led.state_of(kid) == dl.UNKNOWN
        # 重啟(新 Reconciler);子在寄件備份查無 → FAILED → 親再補
        sent = _capture_send(monkeypatch)
        clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
        dr.Reconciler(lambda: led).run_once(now=clock.t,
                                            finder=lambda m: False)
        assert len(sent) == 1, "★子 UNKNOWN 被否證之後沒有人再試★"
        assert led.state_of(did) == dl.CONFIRMED

    def test_definite_failures_hit_the_cap_and_abandon_loudly(
            self, tmp_path, clock, monkeypatch, caplog):
        """crash point 3+出口:確定失敗會再試;上限一到【明確放棄+告警】。"""
        led = _led(tmp_path)
        did = _failed_parent(led, clock)
        alerts = []
        rec = dr.Reconciler(lambda: led,
                            missed_alert=lambda who, subject, why:
                            alerts.append((list(who), subject, why)))
        sent = _capture_send(monkeypatch, exc=RuntimeError("SMTP 掛了"))
        for _ in range(3):                       # 上限 2 → 第三輪只能放棄
            clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
            with caplog.at_level("ERROR"):
                rec.run_once(now=clock.t, finder=lambda m: None)
        assert len(sent) == dl.RESEND_MAX_AUTO, (
            "★沒有上限★ 收不了信的信箱會被每輪追打;或★沒到上限就停★")
        assert led.get(did)["body_text"] == "", "放棄=鏈關,payload 要清"
        states = led.get(did)["recipients"]
        assert states["a@x.tw"] == dl.R_PERMANENT, "放棄要在帳上明確結案"
        assert alerts and "上限" in alerts[-1][2], (
            "★只寫 log 等於沒說★ 放棄要走漏收告警管道")
        assert any("明確放棄" in r.message for r in caplog.records)

    def test_the_claim_itself_enforces_the_cap(self, tmp_path, clock):
        """★上限的最終仲裁在 claim 交易內★(跨 process 的競態下,回查端的
        預檢讀到的子紀錄清單可能已經過時)—— 直接對 claim 量。"""
        led = _led(tmp_path)
        did = _failed_parent(led, clock)
        for i in range(dl.RESEND_MAX_AUTO):
            kid = led.claim_resend_child(did, business_key="bk",
                                         category="t",
                                         recipients=["a@x.tw"],
                                         message_id=f"<c{i}@x>")
            assert kid
            led.settle(kid, failed=True)
        assert led.claim_resend_child(did, business_key="bk", category="t",
                                      recipients=["a@x.tw"],
                                      message_id="<c9@x>") == "", (
            "★claim 自己不守上限★ 兩個 process 的預檢一交錯就超額")

    def test_a_confirmed_child_heals_the_parent_instead_of_resending(
            self, tmp_path, clock, monkeypatch):
        """★「子已確認、回寫親紀錄前 crash」的窗口★ 掃描要先把子紀錄的
        送達回寫親筆(自癒),不是看到暫時被拒就再寄 —— 那位醫師已經
        收到了,再寄就是重複的臨床通知。"""
        led = _led(tmp_path)
        did = _failed_parent(led, clock)
        kid = led.claim_resend_child(did, business_key="bk",
                                     category="consult",
                                     recipients=["a@x.tw"],
                                     message_id="<c1@x>")
        led.settle(kid, refused={})              # 子全數送達
        # ★crash★:confirm_recipients(親) 還沒跑 → 親還掛著暫時被拒
        sent = _capture_send(monkeypatch)
        clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
        dr.Reconciler(lambda: led).run_once(now=clock.t,
                                            finder=lambda m: None)
        assert not sent, "★已經送達還再寄★ 自癒(回寫)要先於補寄"
        assert led.state_of(did) == dl.CONFIRMED
        assert led.get(did)["body_text"] == ""

    def test_two_ledgers_only_one_claims(self, tmp_path, clock):
        """crash point 5:兩個 process 同時恢復同一筆 → 只有一個 sender。"""
        led_a = _led(tmp_path)
        did = _failed_parent(led_a, clock)
        led_b = dl.DeliveryLedger(path=led_a.path)
        got = [led.claim_resend_child(did, business_key="bk",
                                      category="consult",
                                      recipients=["a@x.tw"], subject="s",
                                      message_id=f"<c{i}@x>")
               for i, led in enumerate((led_a, led_b))]
        assert sorted(bool(g) for g in got) == [False, True], (
            "★兩邊都 claim 成功★ 同一封臨床通知會寄兩次")


class TestPartialJoinsTheDurablePath:
    """★P1-03★ 暫時被拒的收件人,重啟後也要有人把信真的補出去。"""

    def test_partial_restart_resends_to_the_missing_only(
            self, tmp_path, clock, monkeypatch):
        led = _led(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["ok@x.tw", "miss@x.tw"],
                        subject="皮膚科會診通知", message_id="<m@x>",
                        body_text="內文")
        led.settle(did, refused={"miss@x.tw": (421, "busy")})
        # ★重啟★:記憶體退避佇列消失。掃描要接手,而且只補沒收到的那位。
        sent = _capture_send(monkeypatch)
        clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
        dr.Reconciler(lambda: led).run_once(now=clock.t,
                                            finder=lambda m: None)
        assert len(sent) == 1, (
            "★重啟後 PARTIAL 只剩結案告警★ 帳上明明有 payload(P1-03)")
        assert sent[0]["recipients"] == ["miss@x.tw"], (
            "★已送達的人又收到一封★ 重複的臨床通知")
        assert led.state_of(did) == dl.CONFIRMED
        assert led.get(did)["body_text"] == ""

    def test_an_inflight_child_blocks_a_second_claim(self, tmp_path, clock):
        """同時最多一封 in-flight(claim 交易內仲裁,不是外面的預檢)。"""
        led = _led(tmp_path)
        did = _failed_parent(led, clock)
        assert led.claim_resend_child(did, business_key="bk", category="t",
                                      recipients=["a@x.tw"],
                                      message_id="<c1@x>")
        assert led.claim_resend_child(did, business_key="bk", category="t",
                                      recipients=["a@x.tw"],
                                      message_id="<c2@x>") == "", (
            "★in-flight 子紀錄還沒收斂就再補★ 那封可能已經送達了")

    def test_a_child_record_never_persists_a_body(self, tmp_path, clock):
        """★外審 AE-1 第 1 輪 P2-4★ 佇列補寄會把整份臨床內文傳進 begin ——
        子紀錄的 body 只是親紀錄的重複 PHI 副本,補寄從不讀它;
        資料層要強制不落地,所有建立者一起管住。"""
        led = _led(tmp_path)
        did = _failed_parent(led, clock)
        qkid = led.begin(business_key="bk|retry1", category="consult",
                         recipients=["a@x.tw"], message_id="<q@x>",
                         parent_id=did, body_text="病人清單的重複副本")
        assert led.get(qkid)["body_text"] == "", (
            "★子紀錄落地了 PHI 副本★ 沒有補寄用途,卻活到 3 天 scrub")

    def test_queue_children_do_not_count_toward_the_auto_cap(
            self, tmp_path, clock, monkeypatch):
        """退避佇列的補寄(kind='')不吃自動補寄的額度 —— 佇列自己有
        退避上限與用盡告警;durable 路的出口要獨立算。"""
        led = _led(tmp_path)
        did = _failed_parent(led, clock)
        for i in range(3):                       # 佇列曾試過三次,全失敗
            qkid = led.begin(business_key=f"bk|retry{i + 1}",
                             category="consult", recipients=["a@x.tw"],
                             message_id=f"<q{i}@x>", parent_id=did,
                             body_text="佇列照生產形狀傳進來的內文")
            led.settle(qkid, failed=True)
        sent = _capture_send(monkeypatch)
        clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
        dr.Reconciler(lambda: led).run_once(now=clock.t,
                                            finder=lambda m: None)
        assert len(sent) == 1, (
            "★佇列的失敗吃掉了 durable 的額度★ crash 恢復的那一次永遠輪不到")


class TestNewerSiblingClosesTheChain:
    def test_a_newer_live_initial_send_stops_the_resend(
            self, tmp_path, clock, monkeypatch):
        """初次確定失敗後,工作層會用同一把 key 重寄 —— 新的那筆存活,
        舊鏈就要收掉,不然同一份通知寄兩次(修正要看組合)。"""
        led = _led(tmp_path)
        did = _failed_parent(led, clock, bk="K")
        clock.advance(60)
        led.begin(business_key="K", category="consult",
                  recipients=["a@x.tw"], message_id="<new@x>",
                  body_text="重跑的那一封")           # SUBMITTING = 存活
        sent = _capture_send(monkeypatch)
        clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
        dr.Reconciler(lambda: led).run_once(now=clock.t,
                                            finder=lambda m: None)
        assert not sent, "★同一份通知寄了兩次★ 工作層的重寄已經接手了"
        assert led.get(did)["body_text"] == "", "舊鏈要明確結案"
        assert "較新" in led.get(did)["note"]

    def test_multiple_failed_parents_yield_exactly_one_resend(
            self, tmp_path, clock, monkeypatch):
        """★外審 AE-1 第 1 輪 P1-1★ 工作層三次重試=三個 FAILED 親紀錄
        (同 business_key)。每筆各自補寄的話,SMTP 恢復後同一份通知
        寄三次 —— canonical 只能有一個(最新的),舊鏈一律結束。"""
        led = _led(tmp_path)
        dids = []
        for i in range(3):
            dids.append(_failed_parent(led, clock, bk="K",
                                       msgid=f"<m{i}@x>"))
            clock.advance(60)
        sent = _capture_send(monkeypatch)
        clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
        dr.Reconciler(lambda: led).run_once(now=clock.t,
                                            finder=lambda m: None)
        assert len(sent) == 1, (
            "★三個 FAILED 親紀錄各自補寄★ 同一份通知寄了 %d 次" % len(sent))
        assert led.get(dids[0])["body_text"] == "", "舊鏈要結束"
        assert led.get(dids[1])["body_text"] == "", "舊鏈要結束"
        assert led.state_of(dids[2]) == dl.CONFIRMED, "欠補寄由最新那筆扛"

    def test_a_sibling_query_failure_keeps_the_body(
            self, tmp_path, clock, monkeypatch):
        """★外審 AE-1 第 1 輪 P1-2★ 查不出「有沒有較新」≠「有較新」——
        結鏈會刪掉唯一的 payload,不可逆;讀取失敗只能跳過本輪。"""
        led = _led(tmp_path)
        did = _failed_parent(led, clock)
        monkeypatch.setattr(
            led, "has_newer_sibling",
            lambda *a, **k: (_ for _ in ()).throw(
                dl.LedgerUnavailable("資料庫抖動")))
        sent = _capture_send(monkeypatch)
        clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
        dr.Reconciler(lambda: led).run_once(now=clock.t,
                                            finder=lambda m: None)
        assert not sent, "查不出來就不該出手"
        assert led.get(did)["body_text"] != "", (
            "★一次暫時的讀取失敗把唯一的 payload 永久刪掉★ 下輪就沒得補了")


class TestForwardSchemaGuard:
    """★P2-02★ 資料庫比程式新 → 拒開,且不可把 meta 降版。"""

    def test_a_newer_db_is_refused_and_not_downgraded(self, tmp_path, clock):
        led = _led(tmp_path)
        led._close_quietly()
        with sqlite3.connect(led.path) as c:
            c.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
        fresh = dl.DeliveryLedger(path=led.path)    # 開不起來→lazy 重試
        with pytest.raises(dl.LedgerUnavailable):
            fresh.begin(business_key="bk", category="t",
                        recipients=["a@x.tw"])
        with sqlite3.connect(led.path) as c:
            v = c.execute("SELECT value FROM meta WHERE"
                          " key='schema_version'").fetchone()[0]
        assert v == "99", (
            "★把 meta 降版改寫★ rollback 後的舊程式會讓新程式誤以為"
            "不用遷移 —— forward-incompatible 的內容被當成已相容")


class TestScrubIsALoudAbandonment:
    def test_scrub_names_still_owed_records(self, tmp_path, clock, caplog):
        """3 天隱私天花板清掉「鏈還開著」的 body = 放棄補寄 —— 要大聲講。"""
        led = _led(tmp_path)
        did = _failed_parent(led, clock)
        clock.advance(dl.BODY_RETAIN_SEC + 60)
        with caplog.at_level("ERROR"):
            led.scrub_stale_bodies()
        assert led.get(did)["body_text"] == ""
        assert any(did in r.message and "到期放棄" in r.message
                   for r in caplog.records), (
            "★無聲的放棄★ body 一清,resends_owed 就不再回報它")


class TestCloseoutDefersToTheDurablePath:
    """★接線(consult)★ 一小時結案不可以搶在 durable 補寄前面把
    暫時被拒改成永久 —— 那會把補寄的目標清空,重啟後退化回只剩告警。"""

    @staticmethod
    def _partial(led, clock):
        did = led.begin(business_key="bk", category="consult",
                        recipients=["ok@x.tw", "miss@x.tw"],
                        subject="皮膚科會診通知", message_id="<m@x>",
                        body_text="內文")
        led.settle(did, refused={"miss@x.tw": (421, "busy")})
        return did

    def test_closeout_waits_while_durable_budget_remains(
            self, tmp_path, clock, monkeypatch):
        cq = importlib.import_module("consult_query")
        led = _led(tmp_path)
        did = self._partial(led, clock)
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        alerted = []
        monkeypatch.setattr(cq, "_alert_missed_recipients",
                            lambda who, subject, why:
                            alerted.append(list(who)))
        cq._close_out_stale_recipient_retries(now=clock.t + 7200)
        assert led.get(did)["recipients"]["miss@x.tw"] == dl.R_TRANSIENT, (
            "★結案搶在 durable 補寄前面★ 目標被改成永久被拒,信永遠不補")
        assert not alerted

    def test_closeout_defers_while_a_child_is_inflight(
            self, tmp_path, clock, monkeypatch):
        """★外審 AE-1 第 1 輪 P1-3★ 額度看似用盡,但最後一次嘗試還在
        SUBMITTING/UNKNOWN —— 那封可能已送達;現在結案+告警,人工照著
        轉寄=重複的臨床通知。要等回查把它收斂(有出口:卡住的 SUBMITTING
        會被 Sent 查證,查無 Message-ID 的 24 小時逾期結案)。"""
        cq = importlib.import_module("consult_query")
        led = _led(tmp_path)
        did = self._partial(led, clock)
        kid1 = led.claim_resend_child(did, business_key="bk",
                                      category="consult",
                                      recipients=["miss@x.tw"],
                                      message_id="<c1@x>")
        led.settle(kid1, failed=True)
        kid2 = led.claim_resend_child(did, business_key="bk",
                                      category="consult",
                                      recipients=["miss@x.tw"],
                                      message_id="<c2@x>")
        led.settle(kid2, unknown=True)           # 第二次嘗試:結果未明
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        alerted = []
        monkeypatch.setattr(cq, "_alert_missed_recipients",
                            lambda who, subject, why:
                            alerted.append(list(who)))
        cq._close_out_stale_recipient_retries(now=clock.t + 7200)
        assert led.get(did)["recipients"]["miss@x.tw"] == dl.R_TRANSIENT, (
            "★最後一次嘗試結果未明就結案★ 它送達的話,告警會引導人工重寄")
        assert not alerted

    def test_closeout_fires_once_the_budget_is_spent(
            self, tmp_path, clock, monkeypatch):
        """讓路不可以變成永遠沉默 —— 額度用盡後照樣結案+告警。"""
        cq = importlib.import_module("consult_query")
        led = _led(tmp_path)
        did = self._partial(led, clock)
        for i in range(dl.RESEND_MAX_AUTO):      # 額度已用盡(全失敗)
            kid = led.claim_resend_child(did, business_key="bk",
                                         category="consult",
                                         recipients=["miss@x.tw"],
                                         message_id=f"<c{i}@x>")
            led.settle(kid, failed=True)
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        alerted = []
        monkeypatch.setattr(cq, "_alert_missed_recipients",
                            lambda who, subject, why:
                            alerted.append(list(who)))
        cq._close_out_stale_recipient_retries(now=clock.t + 7200)
        assert led.get(did)["recipients"]["miss@x.tw"] == dl.R_PERMANENT
        assert alerted == [["miss@x.tw"]], "結案要走漏收告警管道"

    def test_the_reconciler_alert_channel_is_wired(self):
        """★接線★ consult 的 Reconciler 要接漏收告警(晚綁定)。"""
        cq = importlib.import_module("consult_query")
        assert cq._RECONCILER._missed_alert is not None, (
            "★放棄補寄只寫 log★ 使用者不翻 log,等於沒說")
        src = io.open(os.path.join(REPO_ROOT, "src", "consult_query.py"),
                      encoding="utf-8").read()
        assert "missed_alert=lambda who, subject, why:" in src, (
            "告警管道要晚綁定(直接綁函式物件會讓測試 seam 靜默失效)")


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
