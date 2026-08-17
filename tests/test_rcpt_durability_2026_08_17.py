# -*- coding: utf-8 -*-
"""[批次AE-4] 逐位 RCPT 結果要 durable(外審 2026-08-17 P1-01 + P2 群)。

★核心★ SMTP 在 RCPT 階段逐位回答「A 收、B 拒 421」,只要不是全部被拒
就照樣進 DATA —— 信【確實會進寄件備份】,但 B 確定沒收到。舊版把這個
逐位事實留到 send 回來、呼叫端寫帳才落地:中間 crash 的話帳上兩位都還是
UNKNOWN,重啟後整封 Message-ID 回查查到這封信,就把 B 也判成已送達 ——
不重試、不告警的沉默漏寄。

所以 crash 注入必須落在★SMTP 返回 → durable 落地★之間,不能拿後來的
`_schedule_refusal_retry()` 代替(那是另一條路)。
"""
import importlib
import os
import smtplib
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

dl = importlib.import_module("cmuh_common.delivery_ledger")
dr = importlib.import_module("cmuh_common.delivery_reconcile")
sm = importlib.import_module("cmuh_common.smtp_mail")

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


def _led(tmp_path):
    return dl.DeliveryLedger(path=str(tmp_path / "ledger.sqlite3"))


class _FakeServer:
    """只實作 `_submit` 用到的那幾個低階動作(生產的呼叫形狀)。"""

    def __init__(self, rcpt_codes, data_code=250):
        self.rcpt_codes = dict(rcpt_codes)
        self.data_code = data_code
        self.sent_payload = None

    def ehlo_or_helo_if_needed(self):
        pass

    def ehlo(self, *a):
        return 250, b"ok"

    def login(self, *a):
        pass

    def mail(self, *a):
        return 250, b"ok"

    def rcpt(self, addr, *a):
        return self.rcpt_codes.get(addr, (250, b"ok"))

    def docmd(self, cmd):
        return (354, b"go ahead") if cmd == "data" else (250, b"ok")

    def send(self, payload):
        self.sent_payload = payload

    def getreply(self):
        return self.data_code, b"queued"

    def rset(self):
        pass

    def close(self):
        pass


def _run_submit(monkeypatch, rcpt_codes, *, on_rcpt_result=None,
                require_durable_rcpt=False, recipients=("a@x.tw", "b@x.tw")):
    """跑真正的 `_send_once`(只換掉 socket 那一層)。→ (server, refused)。"""
    server = _FakeServer(rcpt_codes)
    monkeypatch.setattr(sm.smtplib, "SMTP",
                        lambda *a, **k: _CtxServer(server))
    cred = {"host": "127.0.0.1", "port": 25, "use_tls": False,
            "username": "u", "password": "p"}
    msg = sm._build_message(
        sender_address="from@x.tw", sender_name="n",
        recipients=list(recipients), subject="皮膚科會診通知",
        body="內文", attachment_path=None, html_body=None,
        message_id="<m@x>")
    refused = sm._send_once(cred, msg, 5.0, on_rcpt_result=on_rcpt_result,
                            require_durable_rcpt=require_durable_rcpt)
    return server, refused


class _CtxServer:
    def __init__(self, inner):
        self._inner = inner

    def __enter__(self):
        return self._inner

    def __exit__(self, *a):
        return False

    def __getattr__(self, name):
        return getattr(self._inner, name)


class TestRcptResultLandsBeforeData:
    """★P1-01 的地基★ 逐位結果要在信送出【之前】就是 durable 事實。"""

    def test_the_callback_fires_before_the_content_is_sent(self, monkeypatch):
        seen = {}

        def _cb(accepted, refused):
            # 這一刻內容【還沒送出去】—— 這正是它存在的理由
            seen["at_call"] = server_box[0].sent_payload
            seen["accepted"] = list(accepted)
            seen["refused"] = dict(refused)
            return True

        server_box = []
        real = _FakeServer({"b@x.tw": (421, b"busy")})
        server_box.append(real)
        monkeypatch.setattr(sm.smtplib, "SMTP",
                            lambda *a, **k: _CtxServer(real))
        cred = {"host": "127.0.0.1", "port": 25, "use_tls": False,
                "username": "u", "password": "p"}
        msg = sm._build_message(
            sender_address="from@x.tw", sender_name="n",
            recipients=["a@x.tw", "b@x.tw"], subject="s", body="b",
            attachment_path=None, html_body=None, message_id="<m@x>")
        refused = sm._send_once(cred, msg, 5.0, on_rcpt_result=_cb)
        assert seen["at_call"] is None, (
            "★callback 在內容送出之後才叫★ 那就沒有解決 crash 窗口")
        assert seen["accepted"] == ["a@x.tw"]
        assert set(seen["refused"]) == {"b@x.tw"}
        assert set(refused) == {"b@x.tw"}
        assert real.sent_payload is not None, "callback 之後才真的送出"

    def test_a_resend_does_not_send_when_the_result_cannot_land(
            self, monkeypatch):
        """補寄路徑:落不了地就不送(內容尚未送出,可安全重試)。"""
        server = _FakeServer({"b@x.tw": (421, b"busy")})
        monkeypatch.setattr(sm.smtplib, "SMTP",
                            lambda *a, **k: _CtxServer(server))
        cred = {"host": "127.0.0.1", "port": 25, "use_tls": False,
                "username": "u", "password": "p"}
        msg = sm._build_message(
            sender_address="from@x.tw", sender_name="n",
            recipients=["a@x.tw", "b@x.tw"], subject="s", body="b",
            attachment_path=None, html_body=None, message_id="<m@x>")
        with pytest.raises(sm.RcptResultNotDurable):
            sm._send_once(cred, msg, 5.0,
                          on_rcpt_result=lambda accepted, refused: False,
                          require_durable_rcpt=True)
        assert server.sent_payload is None, (
            "★落不了地卻還是把信送出去★ 那位被拒的人之後會被回查誤判"
            "成已送達")

    def test_an_initial_send_still_goes_out_when_it_cannot_land(
            self, monkeypatch):
        """初次臨床通知:availability-first —— 落不了地照樣送(既有政策)。"""
        server, refused = _run_submit(
            monkeypatch, {"b@x.tw": (421, b"busy")},
            on_rcpt_result=lambda accepted, refused: False,
            require_durable_rcpt=False)
        assert server.sent_payload is not None
        assert set(refused) == {"b@x.tw"}


class TestCrashBetweenSmtpAndTheLedger:
    """★審查點名的 crash point★:SMTP 已經回答逐位結果,程式在寫帳之前死掉。"""

    def test_a_refused_recipient_survives_a_crash_and_sent_positive(
            self, tmp_path, clock, monkeypatch):
        cq = importlib.import_module("consult_query")
        led = _led(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["a@x.tw", "b@x.tw"],
                        subject="皮膚科會診通知", message_id="<m@x>",
                        body_text="內文")
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        # ★生產路徑★:初次寄送的 callback(不是後來的退避佇列)
        cb = cq._initial_rcpt_recorder(did)
        _run_submit(monkeypatch, {"b@x.tw": (421, b"busy")},
                    on_rcpt_result=cb)
        # ★crash★:send 回來了,但 _delivery_settle / 排入佇列都沒跑到。
        assert led.get(did)["recipients"]["b@x.tw"] == dl.R_TRANSIENT, (
            "★逐位結果沒有在 DATA 之前落地★ crash 後只剩「信在寄件備份裡」")
        # 重啟:整封 Message-ID 回查查到這封信(A 確實收到了)
        clock.advance(dr.STUCK_SUBMITTING_AFTER_SEC + 100)
        dr.Reconciler(lambda: led).run_once(now=clock.t,
                                            finder=lambda m: True)
        assert led.get(did)["recipients"]["a@x.tw"] == dl.R_CONFIRMED
        assert led.get(did)["recipients"]["b@x.tw"] == dl.R_TRANSIENT, (
            "★被 421 明確拒收的人被整封回查判成已送達★ 從此沒有補寄義務"
            "也沒有告警(沉默漏寄)")

    def test_a_child_settle_failure_does_not_confirm_a_refused_recipient(
            self, tmp_path, clock, monkeypatch):
        """★審查點名的第二條★ 補寄子紀錄:B 收、C 被拒,child settle 失敗
        → 之後 Sent 查到 → 回寫親紀錄時★只能用收斂後的 R_CONFIRMED★,
        不可以拿「當初嘗試的名單」整包確認。"""
        led = _led(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["b@x.tw", "c@x.tw"],
                        subject="皮膚科會診通知", message_id="<m0@x>",
                        body_text="內文")
        led.settle(did, refused={"b@x.tw": (421, "busy"),
                                 "c@x.tw": (421, "busy")})
        kid = led.claim_resend_child(did, business_key="bk",
                                     category="consult",
                                     recipients=["b@x.tw", "c@x.tw"],
                                     message_id="<c1@x>")
        assert kid
        # RCPT:B 收、C 被拒 → 逐位結果落地(子+親);settle 之後失敗
        dr.Reconciler._rcpt_recorder(led, kid, did)(
            accepted=["b@x.tw"], refused={"c@x.tw": (550, b"no such user")})
        # ★crash / settle 失敗★:子紀錄停在 SUBMITTING
        assert led.state_of(kid) == dl.SUBMITTING
        clock.advance(dr.STUCK_SUBMITTING_AFTER_SEC + 100)
        dr.Reconciler(lambda: led).run_once(now=clock.t,
                                            finder=lambda m: True)
        pstates = led.get(did)["recipients"]
        assert pstates["b@x.tw"] == dl.R_CONFIRMED, "B 確實收到了"
        assert pstates["c@x.tw"] == dl.R_PERMANENT, (
            "★用當初嘗試的名單整包回寫★ C 被 550 明確拒收,卻變成已送達")


class TestAllRefusedStillLandsTheEvidence:
    """★外審 AE-4 第 1 輪 P2★ 全部收件人在 RCPT 被拒(唯一收件人 550)
    是最重要的那條路:舊版在 callback 之前就拋 SMTPRecipientsRefused ——
    逐位的碼丟掉(被記成暫時失敗、繼續追打不存在的信箱),而且這次確實
    跨過了 RCPT 卻沒劃嘗試邊界(attempts=0 → 額度繞過)。"""

    def test_the_callback_fires_even_when_everyone_is_refused(self,
                                                              monkeypatch):
        seen = {}
        server = _FakeServer({"gone@x.tw": (550, b"no such user")})
        monkeypatch.setattr(sm.smtplib, "SMTP",
                            lambda *a, **k: _CtxServer(server))
        cred = {"host": "127.0.0.1", "port": 25, "use_tls": False,
                "username": "u", "password": "p"}
        msg = sm._build_message(
            sender_address="from@x.tw", sender_name="n",
            recipients=["gone@x.tw"], subject="s", body="b",
            attachment_path=None, html_body=None, message_id="<m@x>")

        def _cb(accepted, refused):
            seen["accepted"] = list(accepted)
            seen["refused"] = dict(refused)
            return True

        with pytest.raises(smtplib.SMTPRecipientsRefused):
            sm._send_once(cred, msg, 5.0, on_rcpt_result=_cb)
        assert seen.get("accepted") == [], (
            "★全部被拒時 callback 完全沒跑★ 550 與嘗試邊界都丟了")
        assert set(seen["refused"]) == {"gone@x.tw"}
        assert server.sent_payload is None, "全部被拒本來就不該送出內容"

    def test_an_all_refused_resend_records_permanent_not_transient(
            self, tmp_path, clock, monkeypatch):
        """durable 補寄路徑的完整生產流程:唯一收件人 550 → 子紀錄要記成
        永久被拒(不是 generic failed)→ 往上傳 → 不再追打。"""
        led = _led(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["gone@x.tw"], subject="皮膚科會診通知",
                        message_id="<m@x>", body_text="內文")
        led.settle(did, unknown=True)
        led.resolve_unknown(did, delivered=False)

        def _all_refused(**kw):
            cb = kw.get("on_rcpt_result")
            bad = {"gone@x.tw": (550, b"no such user")}
            if cb is not None:
                cb(accepted=[], refused=dict(bad))
            raise smtplib.SMTPRecipientsRefused(bad)

        sent = []
        monkeypatch.setattr(sm, "send_mail",
                            lambda **kw: sent.append(kw) or _all_refused(**kw))
        clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
        dr.Reconciler(lambda: led).run_once(now=clock.t,
                                            finder=lambda m: None)
        kids = led.resend_children(did)
        assert kids and kids[0]["recipients"]["gone@x.tw"] == dl.R_PERMANENT, (
            "★550 被摺成 generic failed★ 會被記成暫時被拒")
        assert kids[0]["attempts"] > 0, (
            "★跨過 RCPT 卻沒劃嘗試邊界★ 額度會被繞過")
        # ★下一輪的自癒把結論往上傳★(生產節奏:回查每輪跑一次)
        clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
        dr.Reconciler(lambda: led).run_once(now=clock.t,
                                            finder=lambda m: None)
        assert led.get(did)["recipients"]["gone@x.tw"] == dl.R_PERMANENT, (
            "★550 沒有往上傳★ 親紀錄還掛著暫時被拒")
        assert len(sent) == 1, (
            "★永久被拒還再追打★ 那個信箱確定不存在,重寄一百次也不會變好")


class TestAPartialRefusalMapNeverClaimsDelivery:
    """★例外路徑的逐位結果只在涵蓋全部收件人時才可用★:
    `settle(refused=…)` 會把【不在 map 裡】的人標成已送達 —— 整封都沒
    送出去的時候,那是憑空的宣稱。(SMTPRecipientsRefused 依定義涵蓋全部;
    拿到殘缺的 map 就代表資訊不完整,寧可用 generic failed。)"""

    def test_an_incomplete_map_does_not_confirm_the_missing_one(
            self, tmp_path, clock, monkeypatch):
        led = _led(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["a@x.tw", "b@x.tw"],
                        subject="皮膚科會診通知", message_id="<m@x>",
                        body_text="內文")
        led.settle(did, refused={"a@x.tw": (421, "busy"),
                                 "b@x.tw": (421, "busy")})

        def _partial_map_failure(**kw):
            # 只帶 a 的殘缺 map(例如中間層轉包時掉了一筆)
            raise smtplib.SMTPRecipientsRefused({"a@x.tw": (550, b"no user")})

        monkeypatch.setattr(sm, "send_mail", _partial_map_failure)
        clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
        dr.Reconciler(lambda: led).run_once(now=clock.t,
                                            finder=lambda m: None)
        kid = led.resend_children(did)[0]
        assert kid["recipients"]["b@x.tw"] != dl.R_CONFIRMED, (
            "★整封都沒送出去,卻把不在拒收名單上的人標成已送達★")


class TestOnlyOneLayerOfRetry:
    """★外審 AE-4 第 2 輪 P2★ `send_mail` 內層預設再試兩次 —— 全部 421
    時一個子紀錄就做了 3 次 RCPT,兩個 auto 子紀錄=6 次,
    `RESEND_MAX_AUTO=2` 形同虛設。補寄的重試在【外層】。"""

    def test_max_retries_zero_means_one_rcpt_round(self, monkeypatch):
        """行為面:max_retries=0 → 真的只做一輪 RCPT(不是宣稱而已)。"""
        server = _FakeServer({"a@x.tw": (421, b"busy")})
        rounds = []
        real_rcpt = server.rcpt
        server.rcpt = lambda addr, *a: (rounds.append(addr)
                                        or real_rcpt(addr, *a))
        monkeypatch.setattr(sm.smtplib, "SMTP",
                            lambda *a, **k: _CtxServer(server))
        monkeypatch.setattr(sm, "_reserve_rate_limit_slot", lambda *a: None)
        monkeypatch.setattr(sm, "_rollback_rate_limit_slot", lambda *a: None)
        cred = {"host": "127.0.0.1", "port": 25, "use_tls": False,
                "username": "u", "password": "p",
                "from_address": "me@x.tw", "from_name": ""}
        # 全部被拒 → 4xx 不是永久性 → 用完重試次數後拋 RuntimeError
        with pytest.raises(RuntimeError):
            sm.send_mail(recipients=["a@x.tw"], subject="s", body="b",
                         override_credentials=cred, max_retries=0,
                         timeout=5.0)
        assert rounds == ["a@x.tw"], (
            "★內層又自己重試★ 外層的補寄額度就管不住實際嘗試次數")

    def test_the_durable_resend_asks_for_a_single_attempt(self):
        """★接線★ 補寄路徑要明說 max_retries=0(預設是 2)。"""
        import io as _io
        src = _io.open(os.path.join(REPO_ROOT, "src", "cmuh_common",
                                    "delivery_reconcile.py"),
                       encoding="utf-8").read()
        i = src.index("refused = send_mail(")
        block = src[i:i + 1200]
        assert "max_retries=0" in block, (
            "★補寄沒有關掉內層重試★ 一次 claim 可以做三次 SMTP 嘗試")


class TestEverySendSiteIsWired:
    """★接線★(wired or it doesn't exist):三條寄送路徑都要把逐位結果
    的落地 callback 傳下去 —— 少接一條,那條路的 crash 窗口原封不動。"""

    @staticmethod
    def _src(name):
        import io as _io
        return _io.open(os.path.join(REPO_ROOT, "src", name),
                        encoding="utf-8").read()

    def test_the_initial_consult_send_passes_the_recorder(self):
        src = self._src("consult_query.py")
        i = src.index("_refused = send_via_smtp(")
        block = src[i:i + 1200]
        assert "on_rcpt_result=_initial_rcpt_recorder(" in block, (
            "★初次會診通知沒接上逐位落地★ crash 窗口原封不動")

    def test_the_queue_resend_requires_durable(self):
        src = self._src("consult_query.py")
        assert "_rcpt_cb = _Rec._rcpt_recorder(led, _rid, origin_did)" in src
        assert "require_durable_rcpt=_need_durable" in src, (
            "★佇列補寄沒有要求落地★ 補寄路徑是正確性優先")

    def test_the_durable_resend_requires_durable(self):
        src = self._src("cmuh_common/delivery_reconcile.py")
        i = src.index("refused = send_mail(")
        block = src[i:i + 900]
        assert "on_rcpt_result=self._rcpt_recorder(" in block
        assert "require_durable_rcpt=True" in block

    def test_the_alert_send_passes_the_recorder(self):
        src = self._src("main.py")
        i = src.index("refused = send_mail(recipients=recipients")
        block = src[i:i + 600]
        assert "on_rcpt_result=_rcpt_landed" in block, (
            "★止掛信沒接上逐位落地★ 它走的是同一條回查路")


class TestPermanentPropagation:
    """★P2-02★ 更強的結論要單調地往上傳,不然那個不存在的信箱會一路
    吃完退避與補寄額度,最後的告警還說它是「暫時性拒收」。"""

    def test_a_550_in_a_retry_closes_the_recipient(self, tmp_path, clock,
                                                   monkeypatch):
        led = _led(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["gone@x.tw"], subject="s",
                        message_id="<m@x>", body_text="內文")
        led.settle(did, refused={"gone@x.tw": (421, "busy")})
        kid = led.claim_resend_child(did, business_key="bk",
                                     category="consult",
                                     recipients=["gone@x.tw"],
                                     message_id="<c@x>")
        led.mark_submitting(kid)
        led.settle(kid, refused={"gone@x.tw": (550, "user unknown")})
        assert led.get(kid)["recipients"]["gone@x.tw"] == dl.R_PERMANENT
        clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
        sent = []
        import cmuh_common.smtp_mail as _sm
        monkeypatch.setattr(_sm, "send_mail",
                            lambda **kw: sent.append(kw) or {})
        dr.Reconciler(lambda: led).run_once(now=clock.t,
                                            finder=lambda m: None)
        assert led.get(did)["recipients"]["gone@x.tw"] == dl.R_PERMANENT, (
            "★550 沒有往上傳★ 親紀錄還掛著暫時被拒 → 繼續追打一個"
            "不存在的信箱")
        assert not sent, "永久被拒不該再補寄"

    def test_a_confirmation_is_never_downgraded(self, tmp_path, clock):
        led = _led(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["a@x.tw"], message_id="<m@x>")
        led.settle(did, refused={})
        assert led.mark_permanently_refused(did, ["a@x.tw"]) == []
        assert led.get(did)["recipients"]["a@x.tw"] == dl.R_CONFIRMED

    def test_all_refused_5xx_keeps_the_per_recipient_codes(self, monkeypatch):
        """★SMTPRecipientsRefused 帶著逐位的碼★ —— 不可以摺成 generic
        failed(那會把「查無此人」記成暫時被拒)。"""
        cq = importlib.import_module("consult_query")
        exc = smtplib.SMTPRecipientsRefused({"gone@x.tw": (550, b"no user")})
        assert cq._recipients_refused_map(exc) == {
            "gone@x.tw": (550, b"no user")}
        wrapped = RuntimeError("SMTP 永久性錯誤")
        wrapped.__cause__ = exc
        assert cq._recipients_refused_map(wrapped), "包一層也要找得到"
        assert cq._recipients_refused_map(RuntimeError("別的錯")) == {}


class TestSupersededChainIsExplicit:
    """★P2-01★ 「已被接手」要是顯式狀態 —— 不能用「body 是空的」暗示
    【已送達】【已放棄】【已被接手】三件完全不同的事。"""

    def test_takeover_then_closeout_does_not_alert(self, tmp_path, clock,
                                                   monkeypatch):
        """完全照生產順序:回查(接手)→ 結案路徑。★不可以寄出「始終
        沒收到,請人工轉寄」★ —— 那封信剛剛已經由較新的紀錄送到了。"""
        cq = importlib.import_module("consult_query")
        led = _led(tmp_path)
        old = led.begin(business_key="K", category="consult",
                        recipients=["b@x.tw"], subject="皮膚科會診通知",
                        message_id="<m0@x>", body_text="內文")
        led.settle(old, refused={"b@x.tw": (421, "busy")})
        clock.advance(60)
        new = led.begin(business_key="K", category="consult",
                        recipients=["b@x.tw"], message_id="<m1@x>",
                        body_text="新的一封")
        led.settle(new, refused={})                 # 較新那筆送達了
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        monkeypatch.setattr(cq, "_RECONCILER",
                            dr.Reconciler(lambda: led, tag="delivery"))
        sent = []
        import cmuh_common.smtp_mail as _sm
        monkeypatch.setattr(_sm, "send_mail",
                            lambda **kw: sent.append(kw) or {})
        alerted = []
        monkeypatch.setattr(cq, "_alert_missed_recipients",
                            lambda who, subject, why: alerted.append(list(who)))
        clock.advance(dr.RESEND_OWED_MIN_AGE_SEC + 100)
        cq._reconcile_unknown_deliveries(now=clock.t, finder=lambda m: None)
        assert led.get(old)["superseded_by"] == new, (
            "接手要記成顯式狀態(不能只把 body 清掉)")
        cq._close_out_stale_recipient_retries(now=clock.t)
        assert not sent
        assert not alerted, (
            "★對已經送達的事發出「始終沒收到,請人工轉寄」★ 告警本身會"
            "誘導人工重寄 = 重複的臨床通知")
        assert led.needs_recipient_retry() == [], (
            "已被接手的鏈不可以再出現在待補寄清單裡")

    def test_a_non_superseded_chain_still_alerts(self, tmp_path, clock,
                                                 monkeypatch):
        """反方向:沒有人接手的鏈,額度用盡後照樣結案+告警(不可以被
        新的排除規則一起吃掉)。"""
        cq = importlib.import_module("consult_query")
        led = _led(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["b@x.tw"], subject="皮膚科會診通知",
                        message_id="<m@x>", body_text="內文")
        led.settle(did, refused={"b@x.tw": (421, "busy")})
        for i in range(dl.RESEND_MAX_AUTO):
            kid = led.claim_resend_child(did, business_key="bk",
                                         category="consult",
                                         recipients=["b@x.tw"],
                                         message_id=f"<c{i}@x>")
            led.mark_submitting(kid)
            led.settle(kid, failed=True)
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        alerted = []
        monkeypatch.setattr(cq, "_alert_missed_recipients",
                            lambda who, subject, why: alerted.append(list(who)))
        cq._close_out_stale_recipient_retries(now=clock.t + 7200)
        assert alerted == [["b@x.tw"]]


class TestALiveNewerSiblingBlocksTheClaim:
    """★P1-02 的最低限度★(完整的 event-level 閘門仍是待定案的政策題):
    工作層新一輪剛 begin 的那一筆此刻全是 UNKNOWN —— 只看「已送達」擋不住
    它,兩封會同時跨過 SMTP 邊界 = 重複的臨床通知。"""

    def test_an_inflight_newer_initial_send_blocks_the_resend(
            self, tmp_path, clock):
        led = _led(tmp_path)
        old = led.begin(business_key="K", category="consult",
                        recipients=["b@x.tw"], message_id="<m0@x>",
                        body_text="內文")
        led.settle(old, unknown=True)
        led.resolve_unknown(old, delivered=False)
        clock.advance(60)
        led.begin(business_key="K", category="consult",
                  recipients=["b@x.tw"], message_id="<m1@x>",
                  body_text="新的一封")            # SUBMITTING,還在飛
        assert led.claim_resend_child(old, business_key="K",
                                      category="consult",
                                      recipients=["b@x.tw"],
                                      message_id="<c@x>") == "", (
            "★同一事件的新一輪正在寄,舊鏈還去補★ 兩封重複的臨床通知")

    def test_a_settled_newer_sibling_does_not_block(self, tmp_path, clock):
        """反方向:已收斂的較新紀錄不擋 —— 它沒送到的人本來就該補
        (擋掉的話就變成漏寄)。"""
        led = _led(tmp_path)
        old = led.begin(business_key="K", category="consult",
                        recipients=["b@x.tw"], message_id="<m0@x>",
                        body_text="內文")
        led.settle(old, unknown=True)
        led.resolve_unknown(old, delivered=False)
        clock.advance(60)
        new = led.begin(business_key="K", category="consult",
                        recipients=["z@x.tw"], message_id="<m1@x>")
        # ★已收斂(而且是【已送達】的那種)★ —— 反例要讓兩條路答案不同:
        #   用 FAILED 的話,把判準放寬成 LIVE_STATES 也照樣不擋,量不到
        #   「只擋還在飛的」這條規則。它送給的是【別人】(z),所以 b 仍該補。
        led.settle(new, refused={})
        assert led.claim_resend_child(old, business_key="K",
                                      category="consult",
                                      recipients=["b@x.tw"],
                                      message_id="<c@x>") != "", (
            "★把已收斂的較新紀錄也當成擋路★ 它沒送到的人就永遠不補了")


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
