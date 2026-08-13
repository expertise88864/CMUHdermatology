# -*- coding: utf-8 -*-
"""[批次AD-3] durable outbox:Sent 查無 → 用落地的文字自動補寄(P1-05)。

★使用者定案(2026-08-13):只落地文字★ 會診通知的附件是 PHI 截圖,
依既有隱私定案【不落地】—— 補寄信註明「附件依隱私政策未保留,
請至 HIS 查看」。醫師仍會收到通知不漏接,隱私姿態不變。

★有界性是本批的安全核心★:補寄不可能變成迴圈 ——
補寄紀錄(有 parent_id)永不再補;原信已有子紀錄就不再補(跨重啟
也算得出來,子紀錄在資料庫裡);登記不了就不寄(沒有帳的補寄,
查無時會再補一次,破壞有界性)。
"""
import importlib
import io
import os
import sqlite3
import sys
import time

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

dl = importlib.import_module("cmuh_common.delivery_ledger")
dr = importlib.import_module("cmuh_common.delivery_reconcile")


def _aged_unknown(tmp_path, *, body="會診清單:3F 王O明 皮膚科照會",
                  recipients=("a@x.tw",), msgid="<m1@x>"):
    """一筆【夠老、有 Message-ID、UNKNOWN】的紀錄 —— 回查會挑到它。"""
    led = dl.DeliveryLedger(path=str(tmp_path / "ledger.json"))
    did = led.begin(business_key="bk", category="consult",
                    recipients=list(recipients), subject="皮膚科會診通知",
                    message_id=msgid, body_text=body)
    led.settle(did, unknown=True)
    with sqlite3.connect(led.path) as c:
        c.execute("UPDATE deliveries SET created_at=? WHERE delivery_id=?",
                  (time.time() - 7200, did))
    return led, did


def _capture_send(monkeypatch, result=None, exc=None):
    import cmuh_common.smtp_mail as sm
    sent = []

    def _fake(**kw):
        sent.append(kw)
        if exc is not None:
            raise exc
        return result or {}

    monkeypatch.setattr(sm, "send_mail", _fake)
    return sent


class TestSentMissTriggersATextOnlyResend:
    def test_the_resend_actually_goes_out(self, tmp_path, monkeypatch):
        led, did = _aged_unknown(tmp_path)
        sent = _capture_send(monkeypatch)
        n = dr.Reconciler(lambda: led).run_once(finder=lambda m: False)
        assert n == 1
        assert len(sent) == 1, "★查無之後沒有補寄★ 那封臨床通知就這樣消失了"
        kw = sent[0]
        assert kw["recipients"] == ["a@x.tw"]
        assert "會診清單:3F 王O明" in kw["body"], "補寄要用【落地的】原文"
        assert "自動補寄" in kw["body"]
        assert "附件依隱私政策未保留" in kw["body"], (
            "★沒講清楚為什麼沒有附件★ 醫師會以為信壞掉了")
        assert "HIS" in kw["body"]

    def test_the_resend_is_its_own_ledger_record(self, tmp_path, monkeypatch):
        led, did = _aged_unknown(tmp_path)
        _capture_send(monkeypatch)
        dr.Reconciler(lambda: led).run_once(finder=lambda m: False)
        with sqlite3.connect(led.path) as c:
            kids = c.execute(
                "SELECT delivery_id, state, message_id FROM deliveries"
                " WHERE parent_id=?", (did,)).fetchall()
        assert len(kids) == 1, "補寄要有自己的一筆帳(自己的 Message-ID)"
        assert kids[0][1] == dl.CONFIRMED           # send 成功 → 入帳
        assert kids[0][2] and kids[0][2] != "<m1@x>", (
            "補寄要用【新的】Message-ID,不然回查分不出兩封")
        # ★[外審 AD-3 第 1 輪 P1-1] 補寄成功要回寫【原信】★
        #   不回寫的話原信停在暫時被拒 → 一小時後被「始終沒收到」誤報漏收
        #   → 人工照著轉寄 = 重複的臨床通知。
        assert led.state_of(did) == dl.CONFIRMED, (
            "★補寄送達了,原信卻還掛著沒送到★")
        assert led.needs_recipient_retry() == [], (
            "★原信的收件人還在待補清單上★ 結案路徑會誤報漏收")

    def test_an_unknown_resend_confirms_the_parent_when_found_in_sent(
            self, tmp_path, monkeypatch):
        """★P1-1 下半★ 補寄當下結果不明 → 之後在寄件備份查到 = 原信那幾位
        其實收到了 —— 也要回寫,不然照樣誤報漏收。"""
        import cmuh_common.smtp_mail as sm
        led, did = _aged_unknown(tmp_path)
        _capture_send(monkeypatch, exc=sm.DeliveryOutcomeUnknown("逾時"))
        # ★鏡射生產順序★:_settle_one 是「先 resolve(查無)→ 再補寄」。
        #   不先 resolve 的話,原信自己還是 UNKNOWN,第二輪回查會直接把它
        #   查成已送達 —— 反例就隔離不了「傳播」這條規則。
        rec = led.get(did)                          # 補寄用 resolve 前的快照
        led.resolve_unknown(did, delivered=False)   # 查無 → FAILED(暫時被拒)
        kid = dr.Reconciler(lambda: led)._resend_from_body_text(led, rec)
        assert led.state_of(kid) == dl.UNKNOWN
        with sqlite3.connect(led.path) as c:        # 讓補寄那筆到達回查年齡
            c.execute("UPDATE deliveries SET created_at=? WHERE delivery_id=?",
                      (time.time() - 7200, kid))
        n = dr.Reconciler(lambda: led).run_once(finder=lambda m: True)
        assert n >= 1
        assert led.state_of(kid) == dl.CONFIRMED
        assert led.state_of(did) == dl.CONFIRMED, (
            "★補寄查到已送達,原信卻沒有回寫★ 結案路徑會誤報漏收")
        assert led.needs_recipient_retry() == []

    def test_only_unconfirmed_recipients_get_the_resend(
            self, tmp_path, monkeypatch):
        led, did = _aged_unknown(tmp_path,
                                 recipients=("ok@x.tw", "miss@x.tw"))
        led.confirm_recipients(did, ["ok@x.tw"])    # 這位已確認送達
        with sqlite3.connect(led.path) as c:        # 確認後仍要是可回查的老紀錄
            c.execute("UPDATE deliveries SET state=? WHERE delivery_id=?",
                      (dl.UNKNOWN, did))
        sent = _capture_send(monkeypatch)
        dr.Reconciler(lambda: led).run_once(finder=lambda m: False)
        assert sent and sent[0]["recipients"] == ["miss@x.tw"], (
            "★已送達的人又收到一封★ 重複的臨床通知")


class TestTheResendIsBounded:
    def test_a_resend_record_is_never_resent(self, tmp_path):
        """★迴圈的斷點★ 補寄自己查無時,不可以再補寄。"""
        led, did = _aged_unknown(tmp_path)
        child = {"delivery_id": "kid", "parent_id": did,
                 "body_text": "內文", "recipients": {"a@x.tw": dl.R_UNKNOWN},
                 "subject": "s", "business_key": "bk", "category": "consult"}
        assert dr.Reconciler(lambda: led)._resend_from_body_text(
            led, child) == ""

    def test_an_original_is_resent_at_most_once(self, tmp_path, monkeypatch):
        led, did = _aged_unknown(tmp_path)
        sent = _capture_send(monkeypatch)
        rec = led.get(did)
        r = dr.Reconciler(lambda: led)
        assert r._resend_from_body_text(led, rec) != ""
        assert r._resend_from_body_text(led, rec) == "", (
            "★同一筆原信補寄了兩次★ 子紀錄在資料庫裡,跨重啟也要算得出來")
        assert len(sent) == 1

    def test_the_claim_is_one_transaction(self):
        """★查子紀錄+登記要在同一筆交易★(外審 AD-3 第 1 輪 P1-2)
        拆兩步就是 TOCTOU:兩支程式的回查各查到「沒補過」,各寄一封。"""
        import inspect
        import textwrap
        src = textwrap.dedent(
            inspect.getsource(dl.DeliveryLedger.claim_resend_child))
        i = src.index("_txn")
        assert src.index("SELECT 1 FROM deliveries WHERE parent_id=?") > i, (
            "查子紀錄在交易外 —— TOCTOU")

    def test_a_second_claim_for_the_same_parent_is_refused(self, tmp_path):
        led, did = _aged_unknown(tmp_path)
        a = led.claim_resend_child(did, business_key="bk", category="t",
                                   recipients=["a@x.tw"])
        assert a
        assert led.claim_resend_child(did, business_key="bk", category="t",
                                      recipients=["a@x.tw"]) == ""
        assert led.get(a)["body_text"] == "", (
            "★補寄紀錄不落地 body★ 它永不再補,留著只是暴露面")

    def test_no_stored_body_means_no_resend(self, tmp_path, monkeypatch):
        """舊版寫的紀錄沒有 body_text → 沒得補,維持告警即止(不編內容)。"""
        led, did = _aged_unknown(tmp_path, body="")
        sent = _capture_send(monkeypatch)
        n = dr.Reconciler(lambda: led).run_once(finder=lambda m: False)
        assert n == 1                       # 收斂照常
        assert not sent, "★沒有落地內容卻寄了東西★ 那內容是編出來的"

    def test_a_failed_begin_means_no_send(self, tmp_path, monkeypatch):
        """登記不了就不寄:沒有帳的補寄,下次查無會再補 → 破壞有界性。"""
        led, did = _aged_unknown(tmp_path)
        sent = _capture_send(monkeypatch)
        rec = led.get(did)
        monkeypatch.setattr(
            led, "claim_resend_child",
            lambda *a, **k: (_ for _ in ()).throw(dl.LedgerUnavailable("鎖死")))
        assert dr.Reconciler(lambda: led)._resend_from_body_text(
            led, rec) == ""
        assert not sent, "★沒有帳的補寄★ 查無時會再補一次"


class TestResendOutcomesAreHonest:
    def test_an_unknown_resend_waits_for_reconcile(self, tmp_path, monkeypatch):
        import cmuh_common.smtp_mail as sm
        led, did = _aged_unknown(tmp_path)
        _capture_send(monkeypatch, exc=sm.DeliveryOutcomeUnknown("逾時"))
        rec = led.get(did)
        new_did = dr.Reconciler(lambda: led)._resend_from_body_text(led, rec)
        assert new_did
        assert led.state_of(new_did) == dl.UNKNOWN, (
            "補寄結果不明就要誠實記 UNKNOWN,交給下一輪回查")

    def test_a_failed_resend_is_recorded_failed(self, tmp_path, monkeypatch):
        led, did = _aged_unknown(tmp_path)
        _capture_send(monkeypatch, exc=RuntimeError("SMTP 掛了"))
        rec = led.get(did)
        new_did = dr.Reconciler(lambda: led)._resend_from_body_text(led, rec)
        assert new_did
        assert led.state_of(new_did) == dl.FAILED


class TestTheBodyHasItsOwnRetention:
    """★[外審 AD-3 第 1 輪 P1-3]★ 內文可能含臨床資訊 —— 只在「還可能需要
    補寄」的窗口裡保留,且清除★不依賴之後還有沒有信要寄★。"""

    def test_a_terminal_state_clears_the_body(self, tmp_path):
        led = dl.DeliveryLedger(path=str(tmp_path / "ledger.json"))
        did = led.begin(business_key="bk", category="t",
                        recipients=["a@x.tw"], body_text="臨床內文")
        assert led.get(did)["body_text"] == "臨床內文"
        led.settle(did, refused={})                 # → CONFIRMED(終局)
        assert led.get(did)["body_text"] == "", (
            "★收斂之後內文還留著★ 它只為補寄而存在")

    def test_an_unknown_record_keeps_its_body(self, tmp_path):
        """反方向:還沒收斂的不可以清 —— 清了就沒得補寄。"""
        led = dl.DeliveryLedger(path=str(tmp_path / "ledger.json"))
        did = led.begin(business_key="bk", category="t",
                        recipients=["a@x.tw"], body_text="臨床內文")
        led.settle(did, unknown=True)
        assert led.get(did)["body_text"] == "臨床內文"

    def test_a_long_running_process_scrubs_without_restart_or_new_mail(
            self, tmp_path):
        """★[外審 AD-3 第 2 輪 P1]★ 常駐好幾週、不再寄信的行程:啟動掃除與
        begin 交易永遠不會跑 —— 掃除要掛在回查(排程驅動、與寄信量無關)
        的每一輪開頭。★同一個實例、不重開、不寄新信★。"""
        led = dl.DeliveryLedger(path=str(tmp_path / "ledger.json"))
        # 要有 Message-ID:沒有的話會被「無法查證,逾期結案」那條路收掉,
        # 反例就量不到掃除本身(狀態變了是別條規則做的)。
        did = led.begin(business_key="bk", category="t",
                        recipients=["a@x.tw"], message_id="<keep@x>",
                        body_text="臨床內文")
        led.settle(did, unknown=True)
        with sqlite3.connect(led.path) as c:
            c.execute("UPDATE deliveries SET created_at=? WHERE delivery_id=?",
                      (time.time() - dl.BODY_RETAIN_SEC - 60, did))
        # 回查這一輪甚至查不出結果(IMAP 不可用)—— 掃除仍要發生
        n = dr.Reconciler(lambda: led).run_once(finder=lambda m: None)
        assert n == 0
        assert led.get(did)["body_text"] == "", (
            "★不重啟、不寄新信的常駐行程永遠不掃★ 內文超過宣稱的保留期")
        assert led.state_of(did) == dl.UNKNOWN

    def test_startup_scrubs_stale_bodies_without_needing_new_mail(
            self, tmp_path):
        """★清除不依賴「之後還有信」★ 卡死的 UNKNOWN 永不被 prune,
        它的內文卻不可以跟著永存 —— 啟動時就掃。"""
        path = str(tmp_path / "ledger.json")
        led = dl.DeliveryLedger(path=path)
        did = led.begin(business_key="bk", category="t",
                        recipients=["a@x.tw"], body_text="臨床內文")
        led.settle(did, unknown=True)
        with sqlite3.connect(led.path) as c:
            c.execute("UPDATE deliveries SET created_at=? WHERE delivery_id=?",
                      (time.time() - dl.BODY_RETAIN_SEC - 60, did))
        led._close_quietly()
        fresh = dl.DeliveryLedger(path=path)        # 啟動,不寄任何新信
        assert fresh.get(did)["body_text"] == "", (
            "★逾期內文只靠下一次寄信才清★ 不再寄信的機器上它永遠留著")
        assert fresh.state_of(did) == dl.UNKNOWN, "狀態不可以被掃除動到"


class TestSchemaV2Migration:
    def test_a_v1_database_gains_the_body_column(self, tmp_path):
        """既有的 v1 資料庫(沒有 body_text 欄)開起來要自動升級。"""
        db = str(tmp_path / "ledger.sqlite3")
        with sqlite3.connect(db) as c:
            c.execute("CREATE TABLE deliveries ("
                      " delivery_id TEXT PRIMARY KEY,"
                      " business_key TEXT NOT NULL DEFAULT '',"
                      " parent_id TEXT NOT NULL DEFAULT '',"
                      " category TEXT NOT NULL DEFAULT '',"
                      " subject TEXT NOT NULL DEFAULT '',"
                      " message_id TEXT NOT NULL DEFAULT '',"
                      " attachment_hash TEXT NOT NULL DEFAULT '',"
                      " state TEXT NOT NULL,"
                      " recipients TEXT NOT NULL DEFAULT '{}',"
                      " created_at REAL NOT NULL,"
                      " updated_at REAL NOT NULL,"
                      " attempts INTEGER NOT NULL DEFAULT 0,"
                      " note TEXT NOT NULL DEFAULT '')")
            c.execute("CREATE TABLE meta (key TEXT PRIMARY KEY,"
                      " value TEXT NOT NULL)")
            c.execute("INSERT INTO meta VALUES ('schema_version', '1')")
            c.execute("INSERT INTO deliveries (delivery_id, state,"
                      " created_at, updated_at) VALUES ('old1', 'unknown',"
                      " 1.0, 1.0)")
        led = dl.DeliveryLedger(path=db)
        did = led.begin(business_key="bk", category="t",
                        recipients=["a@x.tw"], body_text="新格式的內容")
        assert led.get(did)["body_text"] == "新格式的內容"
        assert led.get("old1")["body_text"] == "", "舊列的預設要是空字串"
        with sqlite3.connect(db) as c:
            v = c.execute("SELECT value FROM meta WHERE"
                          " key='schema_version'").fetchone()[0]
        assert v == "2", "schema_version 要跟著升(人維護的帳)"


class TestTheCallersActuallyStoreTheBody:
    """★接線★ 落地能力沒接到寄送路徑=補寄永遠沒得補(wired or it
    doesn't exist)。"""

    def test_consult_passes_its_text_body(self):
        src = io.open(os.path.join(REPO_ROOT, "src", "consult_query.py"),
                      encoding="utf-8").read()
        i = src.index("def _delivery_begin(")
        block = src[i:src.index("def ", i + 10)]
        assert "body_text=delivery.text_body" in block, (
            "★會診的 begin 沒有帶 body_text★ 會診通知查無後沒得補寄")

    def test_the_alert_sender_gates_the_body_on_optin(self):
        """★共用 helper 預設不落地★(外審 AD-3 第 1 輪 P1-3):讀回稽核不符
        等呼叫端的內文含病歷號 —— 只有止掛那兩條臨床路徑 opt-in。"""
        src = io.open(os.path.join(REPO_ROOT, "src", "main.py"),
                      encoding="utf-8").read()
        i = src.index("def _send_alert_email_via_smtp(")
        block = src[i:i + 7000]
        assert 'if durable_body else ""' in block, (
            "★止掛 helper 的 body 落地沒有被 opt-in 把關★ 病歷號會進帳本")
        assert "durable_body: bool = False" in block, "預設必須是【不落地】"
        assert src.count("durable_body=True") == 2, (
            "opt-in 的呼叫端數量變了 —— 只有止掛兩條臨床路徑可以落地")
        j = src.index("def _notify_audit_mismatch(")
        audit = src[j:src.index("def _send_alert_email_via_smtp(")]
        assert "durable_body" not in audit, (
            "★讀回稽核告警的內文含病歷號,不可以落地★")


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
