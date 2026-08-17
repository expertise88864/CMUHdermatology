# -*- coding: utf-8 -*-
"""[批次AE-3] resend 所有權與額度語意(外審 2026-08-13 R3 三條 P1)。

★P1-01★ durable claim ≠ delivery attempt:額度只數「真正跨過 SMTP
邊界」的嘗試(mark_submitting,已 fsync)—— 連續兩次 claim 後、send 前
crash,不可以在【零次 SMTP】的情況下把臨床通知 abandon。claim 總數另有
硬背擋(反覆在同一點中斷=機器壞了,要放棄+告警,不無限開子紀錄)。

★P1-02★ 佇列與 durable 兩個 executor 要共用同一個 recipient 仲裁:
「此刻仍暫時被拒、且沒有任何子紀錄已送達過」在 claim 交易內驗 ——
子紀錄送達了、還沒回寫親紀錄的瞬間,親紀錄是舊的,不能只信它。

★P1-03★ 較新同 key 紀錄要【有本錢】才能接走補寄義務:混版部署匯入的
bodyless 紀錄光憑「較新」就讓舊鏈 clear_body,唯一的 payload 就沒了。
"""
import importlib
import os
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
    monkeypatch.setattr(dr, "EVERY_SEC", 0.0)
    return c


def _led(tmp_path, name="ledger.sqlite3"):
    return dl.DeliveryLedger(path=str(tmp_path / name))


def _capture_send(monkeypatch, result=None, exc=None):
    """假的 send_mail —— ★要用生產的呼叫形狀★:真的 SMTP 在 RCPT 全部
    回答完、DATA 之前會呼叫 `on_rcpt_result`(嘗試邊界+逐位拒收落地都在
    那裡)。假的不呼叫的話,額度永遠不會遞增 —— 測試量到的是一個生產
    不會發生的情境。"""
    import cmuh_common.smtp_mail as sm
    sent = []

    def _fake(**kw):
        sent.append(kw)
        refused = dict(result or {})
        cb = kw.get("on_rcpt_result")
        if cb is not None:
            ok = cb(accepted=[a for a in (kw.get("recipients") or [])
                              if a not in refused],
                    refused=dict(refused))
            if not ok and kw.get("require_durable_rcpt"):
                raise sm.RcptResultNotDurable("逐位結果落不了地 → 不送")
        if exc is not None:
            raise exc
        return refused

    monkeypatch.setattr(sm, "send_mail", _fake)
    return sent


def _failed_parent(led, clock, *, recipients=("a@x.tw",), bk="bk",
                   body="會診清單:3F 王O明 皮膚科照會", msgid="<m1@x>"):
    did = led.begin(business_key=bk, category="consult",
                    recipients=list(recipients), subject="皮膚科會診通知",
                    message_id=msgid, body_text=body)
    led.settle(did, unknown=True)
    led.resolve_unknown(did, delivered=False)
    return did


class TestClaimIsNotAnAttempt:
    """★R3 P1-01★ durable claim 必須是 durable work,不是 at-most-N-claims。"""

    def test_two_pre_smtp_crashes_do_not_abandon(self, tmp_path, clock,
                                                 monkeypatch):
        led = _led(tmp_path)
        did = _failed_parent(led, clock)
        alerts = []
        r = dr.Reconciler(lambda: led,
                          missed_alert=lambda w, s, y: alerts.append(y))
        # crash #1:claim COMMIT,send_mail 一次都沒跑
        c1 = led.claim_resend_child(did, business_key="bk",
                                    category="consult",
                                    recipients=["a@x.tw"],
                                    message_id="<c1@x>")
        assert c1
        clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
        r._settle_one(led, led.get(c1), lambda m: False)   # c1 → FAILED
        # crash #2:又是 claim 後、send 前死掉
        c2 = led.claim_resend_child(did, business_key="bk",
                                    category="consult",
                                    recipients=["a@x.tw"],
                                    message_id="<c2@x>")
        assert c2, "★pre-SMTP crash 的 claim 把後續 claim 擋死★"
        r._settle_one(led, led.get(c2), lambda m: False)   # c2 → FAILED
        # 第三次恢復:SMTP 終於能跑 —— 額度(實際嘗試)還是 0,必須寄得出去
        sent = _capture_send(monkeypatch)
        clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
        dr.Reconciler(lambda: led,
                      missed_alert=lambda w, s, y: alerts.append(y)
                      ).run_once(now=clock.t, finder=lambda m: None)
        assert len(sent) == 1, (
            "★零次 SMTP 就被 abandon★ claim 被當成 delivery attempt "
            "(durable at-most-N-claims,不是 durable work)")
        assert not alerts, "沒放棄就不該告警"
        assert led.state_of(did) == dl.CONFIRMED

    def test_the_claims_backstop_stops_a_flapping_machine(
            self, tmp_path, clock, monkeypatch, caplog):
        """★背擋的出口★ 反覆在 claim 與 send 之間中斷的機器,不可以無限
        開子紀錄 —— 到頂就明確放棄+告警(那台機器要人工修)。"""
        led = _led(tmp_path)
        did = _failed_parent(led, clock)
        for i in range(dl.RESEND_MAX_CLAIMS):
            c = led.claim_resend_child(did, business_key="bk",
                                       category="consult",
                                       recipients=["a@x.tw"],
                                       message_id=f"<c{i}@x>")
            assert c, "背擋之內的 claim 要能過"
            led.settle(c, failed=True)          # 收斂,attempts 仍是 0
        assert led.claim_resend_child(did, business_key="bk",
                                      category="consult",
                                      recipients=["a@x.tw"],
                                      message_id="<c9@x>") == "", (
            "★沒有 claim 硬背擋★ 壞機器會無限開子紀錄")
        alerts = []
        sent = _capture_send(monkeypatch)
        clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
        with caplog.at_level("ERROR"):
            dr.Reconciler(lambda: led,
                          missed_alert=lambda w, s, y: alerts.append(y)
                          ).run_once(now=clock.t, finder=lambda m: None)
        assert not sent
        assert alerts and "硬背擋" in alerts[-1], (
            "★背擋到頂只會每輪沉默重試★ 放棄要說出來")
        assert led.get(did)["body_text"] == "", "放棄=鏈關,payload 要清"

    def test_real_attempts_still_hit_the_budget(self, tmp_path, clock,
                                                monkeypatch):
        """反方向:真正跨過 SMTP 邊界的失敗照樣吃額度(追打要有上限)。"""
        led = _led(tmp_path)
        _failed_parent(led, clock)
        sent = _capture_send(monkeypatch, exc=RuntimeError("SMTP 掛了"))
        alerts = []
        r = dr.Reconciler(lambda: led,
                          missed_alert=lambda w, s, y: alerts.append(y))
        for _ in range(3):
            clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
            r.run_once(now=clock.t, finder=lambda m: None)
        assert len(sent) == dl.RESEND_MAX_AUTO, "額度要對真嘗試生效"
        assert alerts, "額度用盡要走告警"


class TestRecipientOwnershipIsUnified:
    """★R3 P1-02★ 兩個 executor、一個仲裁:已送達的人絕不再收一封。"""

    @staticmethod
    def _partial(led, clock):
        did = led.begin(business_key="bk", category="consult",
                        recipients=["ok@x.tw", "miss@x.tw"],
                        subject="皮膚科會診通知", message_id="<m@x>",
                        body_text="內文")
        led.settle(did, refused={"miss@x.tw": (421, "busy")})
        return did

    def test_claim_sees_child_confirmations_inside_the_txn(
            self, tmp_path, clock):
        """★sequence A★ 佇列子紀錄送達了、還沒回寫親紀錄 —— 親紀錄還掛著
        暫時被拒。claim 交易要自己看見子紀錄的已送達集合。"""
        led = _led(tmp_path)
        did = self._partial(led, clock)
        q = led.claim_resend_child(did, business_key="bk|retry1",
                                   category="consult",
                                   recipients=["miss@x.tw"],
                                   message_id="<q@x>",
                                   kind=dl.KIND_QUEUE_RETRY)
        led.mark_submitting(q)
        led.settle(q, refused={})               # 送達;★回寫親紀錄前 crash★
        assert led.claim_resend_child(did, business_key="bk",
                                      category="consult",
                                      recipients=["miss@x.tw"],
                                      message_id="<a@x>") == "", (
            "★只信親紀錄的舊狀態★ 剛送達的人會再收到一封臨床通知")

    def test_claim_narrows_to_still_transient(self, tmp_path, clock):
        led = _led(tmp_path)
        did = self._partial(led, clock)
        # ok 已送達(親紀錄權威狀態)—— claim 帶著過時的兩人名單來
        kid = led.claim_resend_child(did, business_key="bk",
                                     category="consult",
                                     recipients=["ok@x.tw", "miss@x.tw"],
                                     message_id="<a@x>")
        assert kid
        assert sorted(led.get(kid)["recipients"]) == ["miss@x.tw"], (
            "★名單沒有在交易內縮小★ 已送達的 ok 會再收一封")

    def test_a_delayed_queue_item_does_not_resend_a_confirmed_recipient(
            self, tmp_path, clock, monkeypatch):
        """★sequence B★ 排程卡住超過一小時,durable 路先補達並回寫;
        醒來的舊佇列項不可以拿著過時的拒收清單直接再寄。"""
        cq = importlib.import_module("consult_query")
        led = _led(tmp_path)
        did = self._partial(led, clock)
        kid = led.claim_resend_child(did, business_key="bk",
                                     category="consult",
                                     recipients=["miss@x.tw"],
                                     message_id="<a@x>")
        led.mark_submitting(kid)
        led.settle(kid, refused={})
        led.confirm_recipients(did, ["miss@x.tw"])   # durable 路做完了
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        smtp = []
        monkeypatch.setattr(cq, "send_via_smtp",
                            lambda *a, **k: smtp.append(a) or {})
        delivery = cq._DeliveryArtifact(
            recipients=("ok@x.tw", "miss@x.tw"), subject="皮膚科會診通知",
            text_body="內文", html_body="", attachment=None,
            message_id="<m@x>", business_key="bk")
        left = cq._resend_transient_refusals(
            delivery, {"miss@x.tw": (421, "busy")}, origin_did=did)
        assert not smtp, (
            "★醒來的佇列項沒問帳本就寄★ miss 已由 durable 路送達,又收一封")
        assert left == {}, "已送達 → 拒收清單要結案,不是留著再排"

    def test_an_unrecorded_refusal_is_not_closed_as_delivered(
            self, tmp_path, clock, monkeypatch):
        """★外審 AE-3 第 1 輪 F1★ 「親紀錄已無 TRANSIENT」不等於送達。

        忠實情境:帳本【持續】寫不進去(settle 失敗,連補寫拒收也失敗)
        —— 被 SMTP 明確拒收的那位停在 UNKNOWN。舊寫法把他從拒收清單
        靜靜移除(不重試、不告警);正確行為是留著等下一輪。
        """
        cq = importlib.import_module("consult_query")
        led = _led(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["ok@x.tw", "miss@x.tw"],
                        subject="皮膚科會診通知", message_id="<m@x>",
                        body_text="內文")
        assert led.get(did)["recipients"]["miss@x.tw"] == dl.R_UNKNOWN
        monkeypatch.setattr(
            led, "record_refusals",
            lambda *a, **k: (_ for _ in ()).throw(
                dl.LedgerUnavailable("帳本還是寫不進去")))
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        smtp = []
        monkeypatch.setattr(cq, "send_via_smtp",
                            lambda *a, **k: smtp.append(a) or {})
        delivery = cq._DeliveryArtifact(
            recipients=("ok@x.tw", "miss@x.tw"), subject="皮膚科會診通知",
            text_body="內文", html_body="", attachment=None,
            message_id="<m@x>", business_key="bk")
        left = cq._resend_transient_refusals(
            delivery, {"miss@x.tw": (421, "busy")}, origin_did=did)
        assert not smtp, "帳上狀態不明 → 這一輪不寄(交給退避/回查)"
        assert "miss@x.tw" in left, (
            "★把【明確被拒】的收件人當成已送達結案★ 不重試也不告警,"
            "那封臨床通知就這樣消失了")

    def test_a_known_refusal_survives_message_id_reconciliation(
            self, tmp_path, clock, monkeypatch):
        """★外審 AE-3 第 2 輪 P1★ 完整生產序列:

        部分送達(A 收到、B 被 4xx 拒)→ settle 寫不進帳(帳上全是
        UNKNOWN)→ 退避佇列排入 → ★Message-ID 回查是整封粒度★:
        在寄件備份查到這封信(因為 A 收到了)就把所有 UNKNOWN 判成
        已送達 —— B 的拒收若沒先落地,就被永久覆蓋成 CONFIRMED,
        補寄路徑看到 CONFIRMED 結案 = 沉默漏寄。
        """
        cq = importlib.import_module("consult_query")
        led = _led(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["ok@x.tw", "miss@x.tw"],
                        subject="皮膚科會診通知", message_id="<m@x>",
                        body_text="內文")
        # settle 失敗 → 帳上兩位都還是 UNKNOWN(只有記憶體知道 B 被拒)
        assert led.get(did)["recipients"]["miss@x.tw"] == dl.R_UNKNOWN
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        delivery = cq._DeliveryArtifact(
            recipients=("ok@x.tw", "miss@x.tw"), subject="皮膚科會診通知",
            text_body="內文", html_body="", attachment=None,
            message_id="<m@x>", business_key="bk")
        cq._schedule_refusal_retry(delivery, {"miss@x.tw": (421, "busy")},
                                   "email", origin_did=did)
        # ★整封粒度的回查★:寄件備份查到 → 所有 UNKNOWN 判成已送達
        # ★要真的進得了回查清單★ settle 從未成功 → 紀錄停在 SUBMITTING,
        #   要卡超過 STUCK_SUBMITTING_AFTER_SEC 才會被撿去回查;
        #   沒跨過這個門檻的話,兩條路都是「什麼都沒發生」,量不到規則。
        clock.advance(dr.STUCK_SUBMITTING_AFTER_SEC + 100)
        dr.Reconciler(lambda: led).run_once(now=clock.t,
                                            finder=lambda m: True)
        assert led.get(did)["recipients"]["ok@x.tw"] == dl.R_CONFIRMED
        assert led.get(did)["recipients"]["miss@x.tw"] == dl.R_TRANSIENT, (
            "★明確被 4xx 拒收的人被整封回查判成已送達★ 之後補寄路徑"
            "看到 CONFIRMED 就結案,那封臨床通知永遠不會再寄")
        # 佇列到期 → 仍認得他該補,而且真的寄出去
        smtp = []
        monkeypatch.setattr(cq, "send_via_smtp",
                            lambda *a, **k: smtp.append(a) or {})
        left = cq._resend_transient_refusals(
            delivery, {"miss@x.tw": (421, "busy")}, origin_did=did)
        assert smtp and list(smtp[0][3]) == ["miss@x.tw"]
        assert left == {}

    def test_a_ledger_outage_does_not_lose_the_refusal(
            self, tmp_path, clock, monkeypatch):
        """★外審 AE-3 第 3 輪 P1★ 帳本停機時,送出當下與排入佇列時的落地
        【都失敗】—— 4xx 只在記憶體。帳本恢復後、佇列還沒到期(退避 2 分,
        回查門檻 10 分),整封回查會先把那位 UNKNOWN 判成已送達,而落地
        規則(只覆蓋 UNKNOWN)之後永遠救不回來。回查【之前】要把所有
        待補寄項的拒收再落地一次。"""
        cq = importlib.import_module("consult_query")
        led = _led(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["ok@x.tw", "miss@x.tw"],
                        subject="皮膚科會診通知", message_id="<m@x>",
                        body_text="內文")
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        monkeypatch.setattr(cq, "_RECONCILER",
                            dr.Reconciler(lambda: led, tag="delivery"))
        with monkeypatch.context() as outage:      # ★帳本停機中★
            outage.setattr(
                led, "record_refusals",
                lambda *a, **k: (_ for _ in ()).throw(
                    dl.LedgerUnavailable("帳本停機")))
            delivery = cq._DeliveryArtifact(
                recipients=("ok@x.tw", "miss@x.tw"), subject="皮膚科會診通知",
                text_body="內文", html_body="", attachment=None,
                message_id="<m@x>", business_key="bk")
            cq._schedule_refusal_retry(
                delivery, {"miss@x.tw": (421, "busy")}, "email",
                origin_did=did)
            assert led.get(did)["recipients"]["miss@x.tw"] == dl.R_UNKNOWN
        # 帳本恢復;佇列還沒到期(2 分鐘後),但回查這一輪就先跑了
        # ★要真的進得了回查清單★ settle 從未成功 → 紀錄停在 SUBMITTING,
        #   要卡超過 STUCK_SUBMITTING_AFTER_SEC 才會被撿去回查;
        #   沒跨過這個門檻的話,兩條路都是「什麼都沒發生」,量不到規則。
        clock.advance(dr.STUCK_SUBMITTING_AFTER_SEC + 100)
        cq._reconcile_unknown_deliveries(now=clock.t, finder=lambda m: True)
        assert led.get(did)["recipients"]["miss@x.tw"] == dl.R_TRANSIENT, (
            "★停機期間的拒收被整封回查吃掉★ 帳本恢復後沒有人補記,"
            "那位收件人就被永久判成已送達")

    def test_the_memory_queue_is_the_last_line_when_the_stash_fails(
            self, tmp_path, clock, monkeypatch):
        """★兩層各自要能單獨救回來★:寄存處也寫不進去(磁碟滿/權限)時,
        這筆 4xx 只剩【本 process 的記憶體佇列】—— 會診自己的回查入口
        必須在收斂之前把它補記進帳本。
        (反例要隔離這一條:所以把寄存處那層拿掉。)"""
        cq = importlib.import_module("consult_query")
        led = _led(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["ok@x.tw", "miss@x.tw"],
                        subject="皮膚科會診通知", message_id="<m@x>",
                        body_text="內文")
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        monkeypatch.setattr(cq, "_RECONCILER",
                            dr.Reconciler(lambda: led, tag="delivery"))
        monkeypatch.setattr(dr, "stash_refusal",
                            lambda *a, **k: False)   # ★寄存處也失效★
        monkeypatch.setattr(dr, "_load_stash_entries",
                            lambda: ([], True))
        with monkeypatch.context() as outage:
            outage.setattr(
                led, "record_refusals",
                lambda *a, **k: (_ for _ in ()).throw(
                    dl.LedgerUnavailable("帳本停機")))
            delivery = cq._DeliveryArtifact(
                recipients=("ok@x.tw", "miss@x.tw"), subject="皮膚科會診通知",
                text_body="內文", html_body="", attachment=None,
                message_id="<m@x>", business_key="bk")
            cq._schedule_refusal_retry(
                delivery, {"miss@x.tw": (421, "busy")}, "email",
                origin_did=did)
        clock.advance(dr.STUCK_SUBMITTING_AFTER_SEC + 100)
        cq._reconcile_unknown_deliveries(now=clock.t, finder=lambda m: True)
        assert led.get(did)["recipients"]["miss@x.tw"] == dl.R_TRANSIENT, (
            "★寄存處失效時就沒有第二層★ 記憶體裡的拒收被整封回查吃掉")

    def test_reconcile_skips_an_origin_whose_refusal_cannot_land(
            self, tmp_path, clock, monkeypatch):
        """落地仍失敗(帳本還沒好)→ 那一筆這一輪不准被回查收斂:
        帳面不完整就不要下結論。"""
        cq = importlib.import_module("consult_query")
        led = _led(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["ok@x.tw", "miss@x.tw"],
                        subject="皮膚科會診通知", message_id="<m@x>",
                        body_text="內文")
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        monkeypatch.setattr(cq, "_RECONCILER",
                            dr.Reconciler(lambda: led, tag="delivery"))
        monkeypatch.setattr(
            led, "record_refusals",
            lambda *a, **k: (_ for _ in ()).throw(
                dl.LedgerUnavailable("帳本還沒好")))
        delivery = cq._DeliveryArtifact(
            recipients=("ok@x.tw", "miss@x.tw"), subject="皮膚科會診通知",
            text_body="內文", html_body="", attachment=None,
            message_id="<m@x>", business_key="bk")
        cq._schedule_refusal_retry(delivery, {"miss@x.tw": (421, "busy")},
                                   "email", origin_did=did)
        # ★要真的進得了回查清單★ settle 從未成功 → 紀錄停在 SUBMITTING,
        #   要卡超過 STUCK_SUBMITTING_AFTER_SEC 才會被撿去回查;
        #   沒跨過這個門檻的話,兩條路都是「什麼都沒發生」,量不到規則。
        clock.advance(dr.STUCK_SUBMITTING_AFTER_SEC + 100)
        n = cq._reconcile_unknown_deliveries(now=clock.t,
                                             finder=lambda m: True)
        assert n == 0, "帳面不完整的那一筆不可以在這一輪被收斂"
        assert led.get(did)["recipients"]["miss@x.tw"] == dl.R_UNKNOWN

    def test_the_other_program_also_honours_the_pending_refusal(
            self, tmp_path, clock, monkeypatch):
        """★外審 AE-3 第 4 輪 P1★ 排除清單不可以只活在會診那個 process ——
        主程式跑的是【同一本帳】的回查,它不知道會診手上有沒落地的 4xx,
        帳本一恢復就會用整封粒度的結論把逐位證據蓋掉。
        寄存處是檔案,兩支程式都看得到。"""
        cq = importlib.import_module("consult_query")
        import cmuh_common.paths as paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        led = dl.DeliveryLedger(path=str(tmp_path / "delivery_ledger.json"))
        did = led.begin(business_key="bk", category="consult",
                        recipients=["ok@x.tw", "miss@x.tw"],
                        subject="皮膚科會診通知", message_id="<m@x>",
                        body_text="內文")
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        with monkeypatch.context() as outage:      # ★帳本停機中★
            outage.setattr(
                led, "record_refusals",
                lambda *a, **k: (_ for _ in ()).throw(
                    dl.LedgerUnavailable("帳本停機")))
            delivery = cq._DeliveryArtifact(
                recipients=("ok@x.tw", "miss@x.tw"), subject="皮膚科會診通知",
                text_body="內文", html_body="", attachment=None,
                message_id="<m@x>", business_key="bk")
            cq._schedule_refusal_retry(
                delivery, {"miss@x.tw": (421, "busy")}, "email",
                origin_did=did)
        # ★另一支程式(主程式)★:自己的 ledger 實例、自己的 Reconciler,
        #   完全不知道會診的記憶體佇列 —— 它先跑回查。
        other = dl.DeliveryLedger(path=str(tmp_path / "delivery_ledger.json"))
        clock.advance(dr.STUCK_SUBMITTING_AFTER_SEC + 100)
        dr.Reconciler(lambda: other, tag="delivery/alert").run_once(
            now=clock.t, finder=lambda m: True)
        assert other.get(did)["recipients"]["miss@x.tw"] == dl.R_TRANSIENT, (
            "★主程式用整封粒度的結論把逐位拒收蓋成已送達★ "
            "排除清單只活在會診 process = 沒有防到")

    def test_a_concurrent_stash_is_not_lost_by_a_drain(
            self, tmp_path, clock, monkeypatch):
        """★外審 AE-3 第 5 輪 P1★ 兩支程式交錯:主程式 drain 到一半,
        會診寫入【另一筆】拒收 —— 共用單一 JSON 的讀-改-寫會把它蓋掉
        (主程式寫回自己的殘餘),那筆之後就會被誤判成已送達。
        每筆一個檔:drain 只刪自己補記成功的那個檔,不碰別人的。"""
        import cmuh_common.paths as paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        led = _led(tmp_path)
        did_a, did_b = "aaaa1111", "bbbb2222"
        assert dr.stash_refusal(did_a, {"a@x.tw": (421, "busy")},
                                now=clock.t)

        def _rec(d, refused):
            if str(d) == did_a:
                # ★交錯點★:另一支程式在這一刻寫入 B
                dr.stash_refusal(did_b, {"b@x.tw": (421, "busy")},
                                 now=clock.t)
            return []

        monkeypatch.setattr(led, "record_refusals", _rec)
        dr.Reconciler(lambda: led)._drain_refusal_stash(led, clock.t)
        entries, ok = dr._load_stash_entries()
        assert ok
        assert any((r or {}).get("delivery_id") == did_b
                   for _p, r in entries), (
            "★同時寫入的另一筆拒收被 drain 的寫回蓋掉★ 它之後會被"
            "整封回查誤判成已送達(共用檔的讀-改-寫沒有跨 process 序列化)")
        assert not any((r or {}).get("delivery_id") == did_a
                       for _p, r in entries), "補記成功的那筆要移除"

    def test_an_unreadable_stash_entry_blocks_then_expires(
            self, tmp_path, clock, monkeypatch):
        """讀不懂的寄存檔:不知道它指哪一筆 → 只能全部擋住;
        ★但封鎖半徑很大,所以一天後要有出口★(刪掉並大聲講)。"""
        import io as _io
        import os as _os
        import cmuh_common.paths as paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        led = _led(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["a@x.tw"], message_id="<m@x>",
                        body_text="內文")
        led.settle(did, unknown=True)
        _os.makedirs(dr._stash_dir(), exist_ok=True)
        bad = _os.path.join(dr._stash_dir(), "broken.json")
        _io.open(bad, "w", encoding="utf-8").write("{ not json")
        clock.advance(dr.MIN_AGE_SEC + 100)
        assert dr.Reconciler(lambda: led).run_once(
            now=clock.t, finder=lambda m: True) == 0, (
            "讀不懂的寄存檔 → 這一輪誰都不收斂")
        _os.utime(bad, (0, 0))                  # 讓它「很舊」
        assert dr.Reconciler(lambda: led).run_once(
            now=clock.t, finder=lambda m: True) == 1, (
            "★讀不懂的檔永遠擋著★ 一筆壞檔停掉整台機器的收斂")
        assert not _os.path.exists(bad)

    def test_an_unreadable_stash_stops_every_convergence(
            self, tmp_path, clock, monkeypatch):
        """讀不到寄存處 = 不知道哪幾筆的帳面不完整 → 這一輪誰都不收斂
        (空的當成「沒有待補記」會直接放行本該被擋的那幾筆)。"""
        import cmuh_common.paths as paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        led = dl.DeliveryLedger(path=str(tmp_path / "delivery_ledger.json"))
        did = led.begin(business_key="bk", category="consult",
                        recipients=["a@x.tw"], message_id="<m@x>",
                        body_text="內文")
        led.settle(did, unknown=True)
        monkeypatch.setattr(dr, "_load_stash_entries",
                            lambda: ([], False))
        clock.advance(dr.MIN_AGE_SEC + 100)
        assert dr.Reconciler(lambda: led).run_once(
            now=clock.t, finder=lambda m: True) == 0
        assert led.state_of(did) == dl.UNKNOWN

    def test_the_stash_has_an_exit(self, tmp_path, clock, monkeypatch):
        """★抑制要有出口★ 七天都補不進帳本 → 不再擋(但要大聲講),
        否則一筆補不上的拒收會讓整台機器的收斂永遠停擺。"""
        import cmuh_common.paths as paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        led = dl.DeliveryLedger(path=str(tmp_path / "delivery_ledger.json"))
        did = led.begin(business_key="bk", category="consult",
                        recipients=["a@x.tw"], message_id="<m@x>",
                        body_text="內文")
        led.settle(did, unknown=True)
        assert dr.stash_refusal(did, {"a@x.tw": (421, "busy")}, now=clock.t)
        monkeypatch.setattr(
            led, "record_refusals",
            lambda *a, **k: (_ for _ in ()).throw(
                dl.LedgerUnavailable("一直壞著")))
        clock.advance(dr.MIN_AGE_SEC + 100)
        assert dr.Reconciler(lambda: led).run_once(
            now=clock.t, finder=lambda m: True) == 0      # 還在保護期
        clock.advance(dr._STASH_TTL_SEC + 100)
        assert dr.Reconciler(lambda: led).run_once(
            now=clock.t, finder=lambda m: True) == 1, (
            "★過了保留期還在擋★ 一筆補不上的拒收停掉整台機器的收斂")

    def test_persisting_a_refusal_never_reopens_a_terminal_recipient(
            self, tmp_path, clock):
        """★外審 AE-3 第 3 輪 P1(第 2 條)★ PERMANENT 是【已經結束】的
        狀態:5xx 位址錯誤、或補寄上限用盡後的明確放棄。被延遲的佇列項
        握著一張舊的 421 把它重新打開,就是繞過那些上限再寄一次 ——
        抑制的出口被自己的復原機制拆掉。"""
        led = _led(tmp_path)
        did = self._partial(led, clock)
        gone = led.abandon_recipient_retry(did, note="補寄上限用盡")
        assert gone == ["miss@x.tw"]
        assert led.record_refusals(did, {"miss@x.tw": (421, "busy")}) == [], (
            "★已經放棄的收件人被舊的 421 重新打開★ 繞過補寄上限")
        assert led.get(did)["recipients"]["miss@x.tw"] == dl.R_PERMANENT

    def test_persisting_a_refusal_never_overwrites_a_confirmation(
            self, tmp_path, clock):
        """反方向:落地拒收不可以推翻【後來真的送達】的結論 ——
        推翻的話,已經收到的人會再收一封。"""
        led = _led(tmp_path)
        did = self._partial(led, clock)
        kid = led.claim_resend_child(did, business_key="bk",
                                     category="consult",
                                     recipients=["miss@x.tw"],
                                     message_id="<a@x>")
        led.mark_submitting(kid)
        led.settle(kid, refused={})
        led.confirm_recipients(did, ["miss@x.tw"])      # 補寄成功了
        assert led.record_refusals(did, {"miss@x.tw": (421, "busy")}) == []
        assert led.get(did)["recipients"]["miss@x.tw"] == dl.R_CONFIRMED

    def test_a_confirmed_recipient_is_closed(self, tmp_path, clock,
                                             monkeypatch):
        """反方向:帳上【明確記著已送達】才可以把拒收結案(守衛要能過)。"""
        cq = importlib.import_module("consult_query")
        led = _led(tmp_path)
        did = self._partial(led, clock)
        kid = led.claim_resend_child(did, business_key="bk",
                                     category="consult",
                                     recipients=["miss@x.tw"],
                                     message_id="<a@x>")
        led.mark_submitting(kid)
        led.settle(kid, refused={})
        led.confirm_recipients(did, ["miss@x.tw"])
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        monkeypatch.setattr(cq, "send_via_smtp",
                            lambda *a, **k: pytest.fail("已送達還再寄"))
        delivery = cq._DeliveryArtifact(
            recipients=("ok@x.tw", "miss@x.tw"), subject="皮膚科會診通知",
            text_body="內文", html_body="", attachment=None,
            message_id="<m@x>", business_key="bk")
        assert cq._resend_transient_refusals(
            delivery, {"miss@x.tw": (421, "busy")}, origin_did=did) == {}

    def test_an_unreadable_claim_does_not_cross_the_smtp_boundary(
            self, tmp_path, clock, monkeypatch):
        """★外審 AE-3 第 1 輪 F3★ claim 後讀不回名單(暫時性讀取失敗)→
        沿用手上的舊名單會寄給【剛被別人送達】的人,而且那位不在子紀錄
        的帳上。讀不到就不跨 SMTP 邊界。"""
        cq = importlib.import_module("consult_query")
        led = _led(tmp_path)
        did = self._partial(led, clock)
        real_get = led.get
        monkeypatch.setattr(
            led, "get",
            lambda d: {} if d != did else real_get(d))   # 子紀錄讀不到
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        smtp = []
        monkeypatch.setattr(cq, "send_via_smtp",
                            lambda *a, **k: smtp.append(a) or {})
        delivery = cq._DeliveryArtifact(
            recipients=("ok@x.tw", "miss@x.tw"), subject="皮膚科會診通知",
            text_body="內文", html_body="", attachment=None,
            message_id="<m@x>", business_key="bk")
        left = cq._resend_transient_refusals(
            delivery, {"miss@x.tw": (421, "busy")}, origin_did=did)
        assert not smtp, "★讀不到就用舊名單寄★ 可能寄給剛被送達的人"
        assert "miss@x.tw" in left, "沒寄成就要留在拒收清單(下輪退避)"

    def test_the_sweep_does_not_send_when_the_claim_is_unreadable(
            self, tmp_path, clock, monkeypatch):
        """durable 端同一條規則(兩邊各寫一套就會有一邊靜默失效)。"""
        led = _led(tmp_path)
        did = _failed_parent(led, clock)
        sent = _capture_send(monkeypatch)
        real_get = led.get
        r = dr.Reconciler(lambda: led)
        rec = real_get(did)
        monkeypatch.setattr(led, "get",
                            lambda d: {} if d != did else real_get(d))
        assert r._resend_from_body_text(led, rec) == ""
        assert not sent

    def test_queue_claim_yields_to_an_inflight_auto_child(
            self, tmp_path, clock):
        led = _led(tmp_path)
        did = self._partial(led, clock)
        kid = led.claim_resend_child(did, business_key="bk",
                                     category="consult",
                                     recipients=["miss@x.tw"],
                                     message_id="<a@x>")
        assert kid                              # auto in-flight
        assert led.claim_resend_child(did, business_key="bk|retry1",
                                      category="consult",
                                      recipients=["miss@x.tw"],
                                      message_id="<q@x>",
                                      kind=dl.KIND_QUEUE_RETRY) == "", (
            "★兩個 sender 同時在飛★ 同一位收件人可能收到兩封")

    def test_the_sweep_yields_to_an_inflight_queue_child(
            self, tmp_path, clock, monkeypatch):
        led = _led(tmp_path)
        did = self._partial(led, clock)
        q = led.claim_resend_child(did, business_key="bk|retry1",
                                   category="consult",
                                   recipients=["miss@x.tw"],
                                   message_id="<q@x>",
                                   kind=dl.KIND_QUEUE_RETRY)
        assert q                                # queue in-flight
        sent = _capture_send(monkeypatch)
        clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
        dr.Reconciler(lambda: led).run_once(now=clock.t,
                                            finder=lambda m: None)
        assert not sent, "佇列的補寄還沒收斂,durable 路不可以搶著寄"


class TestTakeoverNeedsCapital:
    """★R3 P1-03★ 較新同 key 紀錄要「已送達或自己有 body」才能接走義務。"""

    def test_a_bodyless_newer_sibling_waits(self, tmp_path, clock,
                                            monkeypatch):
        """混版部署:舊程式寫的較新紀錄由 JSON 匯入,天生沒有 body ——
        它結果未定時只能等,不可以讓唯一的 payload 被 GC。"""
        led = _led(tmp_path)
        p0 = _failed_parent(led, clock, bk="K")
        clock.advance(60)
        led.begin(business_key="K", category="consult",
                  recipients=["a@x.tw"], message_id="<new@x>",
                  body_text="")                  # bodyless、SUBMITTING
        sent = _capture_send(monkeypatch)
        clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
        dr.Reconciler(lambda: led).run_once(now=clock.t,
                                            finder=lambda m: None)
        assert not sent, "較新那封可能已送達 → 等它收斂,不搶寄"
        assert led.get(p0)["body_text"] != "", (
            "★bodyless 較新紀錄把唯一的 payload GC 掉★ 兩筆都不再"
            " actionable = 永久漏寄(混版部署)")

    def test_a_bodyless_newer_failure_hands_the_duty_back(
            self, tmp_path, clock, monkeypatch):
        led = _led(tmp_path)
        p0 = _failed_parent(led, clock, bk="K")
        clock.advance(60)
        p1 = led.begin(business_key="K", category="consult",
                       recipients=["a@x.tw"], message_id="<new@x>",
                       body_text="")
        led.settle(p1, failed=True)             # bodyless 且確定失敗
        sent = _capture_send(monkeypatch)
        clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
        dr.Reconciler(lambda: led).run_once(now=clock.t,
                                            finder=lambda m: None)
        assert len(sent) == 1, (
            "★沒本錢的較新紀錄把義務帶進墳墓★ 舊的 payload-bearing 鏈"
            "要接回去補")
        assert led.state_of(p0) == dl.CONFIRMED

    def test_a_bodyless_partial_sibling_hands_back_only_the_missing(
            self, tmp_path, clock, monkeypatch):
        """★外審 AE-3 第 1 輪 F2★ bodyless PARTIAL sibling:送到了 A、
        暫時被拒 B。verdict 是 ""(它沒本錢扛),舊鏈接回義務 ——
        但 A 已經收到了,只能補 B。"""
        led = _led(tmp_path)
        p0 = led.begin(business_key="K", category="consult",
                       recipients=["a@x.tw", "b@x.tw"], message_id="<m0@x>",
                       subject="皮膚科會診通知", body_text="內文")
        led.settle(p0, unknown=True)
        led.resolve_unknown(p0, delivered=False)     # A、B 都待補
        clock.advance(60)
        p1 = led.begin(business_key="K", category="consult",
                       recipients=["a@x.tw", "b@x.tw"], message_id="<m1@x>",
                       body_text="")                # bodyless(混版匯入)
        led.settle(p1, refused={"b@x.tw": (421, "busy")})   # PARTIAL:A 送達
        assert led.state_of(p1) == dl.PARTIAL
        sent = _capture_send(monkeypatch)
        clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
        dr.Reconciler(lambda: led).run_once(now=clock.t,
                                            finder=lambda m: None)
        assert len(sent) == 1, "B 仍未送達 → 舊鏈要接回義務補他"
        assert sent[0]["recipients"] == ["b@x.tw"], (
            "★較新 sibling 送達的 A 又收到一封★ 重複的臨床通知")
        assert led.get(p0)["recipients"]["a@x.tw"] == dl.R_CONFIRMED, (
            "較新紀錄送達的人要回寫舊親紀錄,帳面才誠實")

    def test_the_claim_excludes_recipients_confirmed_by_a_sibling(
            self, tmp_path, clock):
        """同 business_key 底下任何一筆的已送達都要擋(不只自己的子紀錄)。"""
        led = _led(tmp_path)
        p0 = led.begin(business_key="K", category="consult",
                       recipients=["a@x.tw", "b@x.tw"], message_id="<m0@x>",
                       body_text="內文")
        led.settle(p0, refused={"a@x.tw": (421, "busy"),
                                "b@x.tw": (421, "busy")})
        clock.advance(60)
        p1 = led.begin(business_key="K", category="consult",
                       recipients=["a@x.tw"], message_id="<m1@x>",
                       body_text="")
        led.settle(p1, refused={})                   # sibling 把 A 送到了
        kid = led.claim_resend_child(p0, business_key="K",
                                     category="consult",
                                     recipients=["a@x.tw", "b@x.tw"],
                                     message_id="<c@x>")
        assert kid
        assert sorted(led.get(kid)["recipients"]) == ["b@x.tw"], (
            "★只看自己的子紀錄★ sibling 送達的 A 會再收一封")

    def test_a_confirmed_newer_sibling_takes_over(self, tmp_path, clock,
                                                  monkeypatch):
        led = _led(tmp_path)
        p0 = _failed_parent(led, clock, bk="K")
        clock.advance(60)
        p1 = led.begin(business_key="K", category="consult",
                       recipients=["a@x.tw"], message_id="<new@x>",
                       body_text="")
        led.settle(p1, refused={})              # 較新那筆送達了
        sent = _capture_send(monkeypatch)
        clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
        dr.Reconciler(lambda: led).run_once(now=clock.t,
                                            finder=lambda m: None)
        assert not sent
        assert led.get(p0)["body_text"] == "", "義務已被送達的較新者接走"


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
