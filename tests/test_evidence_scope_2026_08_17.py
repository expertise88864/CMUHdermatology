# -*- coding: utf-8 -*-
"""[批次AE-5] durable fact 的 scope / ownership fence(外審 2026-08-17 第五輪)。

三條 P1 的共同形狀都是「這個 durable 事實的【範圍】是什麼」:

★P1-01★ RCPT 逐位證據的壽命是【一次 SMTP 嘗試】——`send_mail` 內層的
safe retry 會對每次嘗試重跑一遍 callback:第一次 B 被 421、第二次 B 收下,
呼叫端卻只在「有拒收」時寫帳,第一次留下的 TRANSIENT 永遠沒被作廢 ——
之後 durable 補寄會再寄一封給【已經收到的人】。

★P1-02★ availability-first 的路徑上,帳本寫不進去時要在 DATA ★之前★
落到跨 process 寄存處;等 SMTP 回來才存的話,中間斷電就什麼證據都沒有,
回查把被明確拒收的那位判成已送達(沉默漏寄)。

★P1-03★ `superseded_by` 宣稱的是 durable 的 ownership transfer,那它就
必須是【資料層】的 fence:只擋掃描端的話,還活在記憶體裡的舊佇列項照樣
能從已交棒的親紀錄開新子紀錄,與接手者同時寄給同一個人。
"""
import importlib
import os
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


class _CtxServer:
    def __init__(self, inner):
        self._inner = inner

    def __enter__(self):
        return self._inner

    def __exit__(self, *a):
        return False

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _ScriptedServer:
    """照腳本回答的 SMTP:每次 `_send_once` 用下一格(模擬內層 safe retry)。"""

    def __init__(self, script):
        self.script = list(script)      # [(rcpt_codes, data_code), …]
        self.round = -1
        self.sent_payloads = []

    def _cur(self):
        i = min(self.round, len(self.script) - 1)
        return self.script[i]

    def ehlo_or_helo_if_needed(self):
        self.round += 1                 # 每次 _submit 從這裡開始

    def ehlo(self, *a):
        return 250, b"ok"

    def login(self, *a):
        pass

    def mail(self, *a):
        return 250, b"ok"

    def rcpt(self, addr, *a):
        return self._cur()[0].get(addr, (250, b"ok"))

    def docmd(self, cmd):
        return (self._cur()[1], b"go") if cmd == "data" else (250, b"ok")

    def send(self, payload):
        self.sent_payloads.append(payload)

    def getreply(self):
        return 250, b"queued"

    def rset(self):
        pass

    def close(self):
        pass


class TestRcptEvidenceIsPerAttempt:
    """★P1-01★ 帶 callback 的寄送不可以有內層重試 —— 逐位證據會跨嘗試累積。"""

    def test_a_callback_send_refuses_internal_retries(self, monkeypatch):
        """結構性守衛:有 callback 就必須 max_retries=0(不靠每個呼叫端
        自己記得 —— 守衛不可以有靜默失效的形狀)。"""
        with pytest.raises(ValueError, match="max_retries=0"):
            sm.send_mail(recipients=["a@x.tw"], subject="s", body="b",
                         override_credentials={
                             "host": "127.0.0.1", "port": 25,
                             "use_tls": False, "username": "u",
                             "password": "p", "from_address": "me@x.tw",
                             "from_name": ""},
                         max_retries=2,
                         on_rcpt_result=lambda accepted, refused: True)

    def test_one_logical_send_calls_the_callback_exactly_once(
            self, tmp_path, clock, monkeypatch):
        """★不變式:一封信 = 一次 RCPT 證據★(審查點名的序列)。

        腳本:attempt#1 → B 421 + DATA 451(★內容一個 byte 都沒送出,
        `send_mail` 本來會判定可安全重試★);attempt#2 → 全數接受 + 250。
        舊行為會呼叫 callback 兩次,而第二次的 `refused={}` 不會作廢第一次
        寫下的 TRANSIENT —— 那位【已經收到】的人之後會被 durable 補寄再寄
        一封。現在 max_retries=0:第一次失敗就結束,不會有第二次 RCPT。
        """
        calls = []
        server = _ScriptedServer([
            ({"b@x.tw": (421, b"busy")}, 451),      # attempt #1
            ({}, 354),                              # attempt #2:全數接受
        ])
        monkeypatch.setattr(sm.smtplib, "SMTP",
                            lambda *a, **k: _CtxServer(server))
        monkeypatch.setattr(sm, "_reserve_rate_limit_slot", lambda *a: None)
        monkeypatch.setattr(sm, "_rollback_rate_limit_slot", lambda *a: None)
        cred = {"host": "127.0.0.1", "port": 25, "use_tls": False,
                "username": "u", "password": "p",
                "from_address": "me@x.tw", "from_name": ""}

        def _cb(accepted, refused):
            calls.append((sorted(accepted), sorted(refused)))
            return True

        with pytest.raises(RuntimeError):
            sm.send_mail(recipients=["a@x.tw", "b@x.tw"], subject="s",
                         body="b", override_credentials=cred,
                         max_retries=0, on_rcpt_result=_cb, timeout=5.0)
        assert calls == [(["a@x.tw"], ["b@x.tw"])], (
            "★同一封信的 RCPT 證據被寫了兩次★ 第二次的「全數接受」不會"
            "作廢第一次的 TRANSIENT,已收到的人會再收到補寄")
        assert not server.sent_payloads, "451 在 354 之前 → 內容未送出"


class TestTheStashCatchesTheLedgerOutage:
    """★P1-02★ 帳本寫不進去時,證據要在 DATA 之前落到跨 process 寄存處。"""

    def _drive(self, monkeypatch, tmp_path, recorder, did):
        server = _ScriptedServer([({"b@x.tw": (421, b"busy")}, 354)])
        monkeypatch.setattr(sm.smtplib, "SMTP",
                            lambda *a, **k: _CtxServer(server))
        monkeypatch.setattr(sm, "_reserve_rate_limit_slot", lambda *a: None)
        monkeypatch.setattr(sm, "_rollback_rate_limit_slot", lambda *a: None)
        cred = {"host": "127.0.0.1", "port": 25, "use_tls": False,
                "username": "u", "password": "p",
                "from_address": "me@x.tw", "from_name": ""}
        refused = sm.send_mail(recipients=["a@x.tw", "b@x.tw"], subject="s",
                               body="b", override_credentials=cred,
                               max_retries=0, on_rcpt_result=recorder,
                               timeout=5.0)
        return server, refused

    def test_a_ledger_outage_lands_in_the_stash_before_data(
            self, tmp_path, clock, monkeypatch):
        """★審查點名的反例★ record_refusals 失敗 → stash 成功 → DATA 成功
        → ★硬 crash(呼叫端一行都沒再跑)★ → 重啟 → 回查 drain 寄存處
        → B 仍是 TRANSIENT,不是被 Sent-positive 判成已送達。"""
        cq = importlib.import_module("consult_query")
        import cmuh_common.paths as paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        led = dl.DeliveryLedger(path=str(tmp_path / "delivery_ledger.json"))
        did = led.begin(business_key="bk", category="consult",
                        recipients=["a@x.tw", "b@x.tw"],
                        subject="皮膚科會診通知", message_id="<m@x>",
                        body_text="內文")
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        real_record = led.record_refusals
        monkeypatch.setattr(
            led, "record_refusals",
            lambda *a, **k: (_ for _ in ()).throw(
                dl.LedgerUnavailable("帳本忙碌")))
        server, refused = self._drive(
            monkeypatch, tmp_path, cq._initial_rcpt_recorder(did), did)
        assert server.sent_payloads, "初次臨床通知照送(availability-first)"
        assert set(refused) == {"b@x.tw"}
        # ★crash★:呼叫端的 settle / 排入佇列 / _persist_known_refusals
        #   一行都沒跑到。唯一的證據只剩寄存處。
        entries, ok = dr._load_stash_entries()
        assert ok and any((r or {}).get("delivery_id") == did
                          for _p, r in entries), (
            "★帳本寫不進去就什麼都沒留★ 這正是斷電時唯一的證據")
        # 重啟:帳本恢復 → 回查先 drain 寄存處,再做整封回查
        monkeypatch.setattr(led, "record_refusals", real_record)
        clock.advance(dr.STUCK_SUBMITTING_AFTER_SEC + 100)
        dr.Reconciler(lambda: led).run_once(now=clock.t,
                                            finder=lambda m: True)
        states = led.get(did)["recipients"]
        assert states["a@x.tw"] == dl.R_CONFIRMED
        assert states["b@x.tw"] == dl.R_TRANSIENT, (
            "★被 421 明確拒收的人被整封回查判成已送達★ 沉默漏寄")


class TestSupersededIsAHardFence:
    """★P1-03★ 已交棒的鏈不得再產生任何工作(資料層 fence,不是掃描端)。"""

    def test_claim_is_refused_on_a_superseded_parent(self, tmp_path, clock):
        led = _led(tmp_path)
        old = led.begin(business_key="K", category="consult",
                        recipients=["b@x.tw"], message_id="<m0@x>",
                        body_text="舊內文")
        led.settle(old, refused={"b@x.tw": (421, "busy")})
        clock.advance(60)
        new = led.begin(business_key="K", category="consult",
                        recipients=["b@x.tw"], message_id="<m1@x>",
                        body_text="新內文")
        led.settle(new, refused={"b@x.tw": (421, "busy")})
        led.supersede(old, by=new)
        assert led.claim_resend_child(old, business_key="K",
                                      category="consult",
                                      recipients=["b@x.tw"],
                                      message_id="<c@x>") == "", (
            "★已交棒的親紀錄還能開新工作★ 會與接手者同時寄給同一個人")
        assert led.claim_resend_child(new, business_key="K",
                                      category="consult",
                                      recipients=["b@x.tw"],
                                      message_id="<c2@x>") != "", (
            "接手者本人要能繼續工作")

    def test_supersede_is_refused_while_a_child_is_inflight(
            self, tmp_path, clock):
        """★外審 AE-5 第 1 輪 P1(反向交錯)★ 舊佇列先 claim 出
        SUBMITTING 子紀錄,另一個 process 才 supersede —— 交棒若無條件成立,
        舊鏈那封仍會寄出去,與接手者同時寄給同一個人。
        兩邊都在 BEGIN IMMEDIATE 裡檢查對方,誰先拿到寫鎖誰成立。"""
        led = _led(tmp_path)
        old = led.begin(business_key="K", category="consult",
                        recipients=["b@x.tw"], message_id="<m0@x>",
                        body_text="舊內文")
        led.settle(old, refused={"b@x.tw": (421, "busy")})
        clock.advance(60)
        new = led.begin(business_key="K", category="consult",
                        recipients=["b@x.tw"], message_id="<m1@x>",
                        body_text="新內文")
        # ★較新那筆要先收斂★:否則 claim 會被「同 key 還在飛」那條規則
        #   擋掉(AE-4),就量不到這裡要測的 supersede 交錯。
        led.settle(new, refused={"b@x.tw": (421, "busy")})
        kid = led.claim_resend_child(old, business_key="K",
                                     category="consult",
                                     recipients=["b@x.tw"],
                                     message_id="<c@x>")
        assert kid, "先 claim(舊佇列已經拿到工作)"
        assert led.supersede(old, by=new) is False, (
            "★有結果未定的補寄時仍准交棒★ 兩條鏈會同時寄給同一個人")
        assert led.get(old)["superseded_by"] == ""
        # 那個子紀錄收斂之後,交棒才成立(出口存在,不會永遠卡著)
        led.settle(kid, failed=True)
        assert led.supersede(old, by=new) is True

    def test_a_stale_queue_item_closes_out_without_sending_or_alerting(
            self, tmp_path, clock, monkeypatch):
        """★審查點名的反例★ 舊佇列項還活在記憶體裡,親紀錄已交棒 ——
        不可以寄、不可以重排、也不可以發「始終沒收到」的告警(責任在
        接手的那一筆)。"""
        cq = importlib.import_module("consult_query")
        led = _led(tmp_path)
        old = led.begin(business_key="K", category="consult",
                        recipients=["b@x.tw"], subject="皮膚科會診通知",
                        message_id="<m0@x>", body_text="舊內文")
        led.settle(old, refused={"b@x.tw": (421, "busy")})
        clock.advance(60)
        new = led.begin(business_key="K", category="consult",
                        recipients=["b@x.tw"], message_id="<m1@x>",
                        body_text="新內文")
        led.supersede(old, by=new)
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        smtp = []
        monkeypatch.setattr(cq, "send_via_smtp",
                            lambda *a, **k: smtp.append(a) or {})
        delivery = cq._DeliveryArtifact(
            recipients=("b@x.tw",), subject="皮膚科會診通知",
            text_body="舊內文", html_body="", attachment=None,
            message_id="<m0@x>", business_key="K")
        left = cq._resend_transient_refusals(
            delivery, {"b@x.tw": (421, "busy")}, origin_did=old)
        assert not smtp, "★舊佇列項從已交棒的鏈寄出去★ 接手者也會寄=兩封"
        assert left == {}, (
            "★還留在拒收清單★ 會一路退避到用盡,然後對【別人負責的事】"
            "發出「始終沒收到」告警")


class TestPermanentIsImmediate:
    """★P2-01★ 550 是比 421 更強的結論,在 callback 當下就要升級親紀錄
    —— 不然記憶體佇列還拿著舊的 421 一路退避到用盡(約 42 分鐘),
    最後的告警把「查無此人」講成「暫時性拒收用盡」。"""

    def test_a_550_upgrades_the_parent_at_rcpt_time(self, tmp_path, clock):
        led = _led(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["gone@x.tw"], message_id="<m@x>",
                        body_text="內文")
        led.settle(did, refused={"gone@x.tw": (421, "busy")})
        assert led.get(did)["recipients"]["gone@x.tw"] == dl.R_TRANSIENT
        kid = led.claim_resend_child(did, business_key="bk",
                                     category="consult",
                                     recipients=["gone@x.tw"],
                                     message_id="<c@x>")
        dr.Reconciler._rcpt_recorder(led, kid, did)(
            accepted=[], refused={"gone@x.tw": (550, b"no such user")})
        assert led.get(did)["recipients"]["gone@x.tw"] == dl.R_PERMANENT, (
            "★550 要等回查掃到才上傳★ 中間佇列還在用舊的 421 退避")

    def test_the_child_and_parent_move_in_one_transaction(self, tmp_path,
                                                          clock, monkeypatch):
        """★外審 AE-5 第 1 輪 P2★ 子紀錄寫成功、親紀錄升級失敗的中間狀態
        不可以留在帳上:claim 看子紀錄拒絕補寄、佇列看親紀錄繼續退避,
        最後用「暫時性拒收用盡」的語氣告警一個確定不存在的信箱。
        三件事同一筆交易 → 失敗就整筆回捲。"""
        led = _led(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["gone@x.tw"], message_id="<m@x>",
                        body_text="內文")
        led.settle(did, refused={"gone@x.tw": (421, "busy")})
        kid = led.claim_resend_child(did, business_key="bk",
                                     category="consult",
                                     recipients=["gone@x.tw"],
                                     message_id="<c@x>")
        # 親紀錄那一步失敗(鎖競爭/IO)→ 整筆交易回捲
        real = led._mutate_states_in_txn
        calls = {"n": 0}

        def _boom(conn, delivery_id, fn, **kw):
            calls["n"] += 1
            if calls["n"] == 2:                 # 第二次 = 親紀錄
                raise dl.LedgerUnavailable("親紀錄寫入失敗")
            return real(conn, delivery_id, fn, **kw)

        monkeypatch.setattr(led, "_mutate_states_in_txn", _boom)
        ok = led.record_rcpt_outcome(kid, did,
                                     {"gone@x.tw": (550, b"no such user")})
        assert ok is False, "落不了地就要回 False(呼叫端在 DATA 之前中止)"
        assert led.get(kid)["recipients"]["gone@x.tw"] == dl.R_UNKNOWN, (
            "★子紀錄已 permanent、親紀錄還是 transient★ 兩邊的判斷會不一致")
        assert led.get(kid)["attempts"] == 0, "嘗試邊界也要一起回捲"
        assert led.get(did)["recipients"]["gone@x.tw"] == dl.R_TRANSIENT

    def test_a_421_does_not_upgrade_the_parent(self, tmp_path, clock):
        """反方向:暫時性拒收不可以被當成永久 —— 那會停掉該補的補寄。"""
        led = _led(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["busy@x.tw"], message_id="<m@x>",
                        body_text="內文")
        led.settle(did, refused={"busy@x.tw": (421, "busy")})
        kid = led.claim_resend_child(did, business_key="bk",
                                     category="consult",
                                     recipients=["busy@x.tw"],
                                     message_id="<c@x>")
        dr.Reconciler._rcpt_recorder(led, kid, did)(
            accepted=[], refused={"busy@x.tw": (421, b"still busy")})
        assert led.get(did)["recipients"]["busy@x.tw"] == dl.R_TRANSIENT

    def test_the_queue_treats_permanent_as_terminal(self, tmp_path, clock,
                                                    monkeypatch):
        cq = importlib.import_module("consult_query")
        led = _led(tmp_path)
        did = led.begin(business_key="bk", category="consult",
                        recipients=["gone@x.tw"], subject="皮膚科會診通知",
                        message_id="<m@x>", body_text="內文")
        led.settle(did, refused={"gone@x.tw": (550, "no such user")})
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        smtp = []
        monkeypatch.setattr(cq, "send_via_smtp",
                            lambda *a, **k: smtp.append(a) or {})
        delivery = cq._DeliveryArtifact(
            recipients=("gone@x.tw",), subject="皮膚科會診通知",
            text_body="內文", html_body="", attachment=None,
            message_id="<m@x>", business_key="bk")
        left = cq._resend_transient_refusals(
            delivery, {"gone@x.tw": (421, "busy")}, origin_did=did)
        assert not smtp
        assert left == {}, (
            "★永久被拒還留在拒收清單★ 會退避到用盡,再用「暫時性拒收」"
            "的語氣告警一個確定不存在的信箱")


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
