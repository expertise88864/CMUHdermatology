# -*- coding: utf-8 -*-
"""外審第 10 輪（寄送帳本 / SMTP / 截圖）八個 CONFIRMED finding。

貫穿這一輪的是同一個問題:**我們宣稱知道「信到底送出去了沒有」,但其實不知道。**

【P1-03】`_submit` 包住整個 `send_message()`,而它內部是 MAIL → RCPT → DATA。
例外拋出來時分不出走到哪一步,卻對【任何】例外都蓋上「已提交」的章。於是
MAIL/RCPT 階段的逾時（伺服器確定還沒收到內容）被當成「結果不明」→ 止掛提醒
的處理是「視為已寄、不重寄」並永久去重 → 那則提醒這輩子都不會寄出。
(main.py 當時的註解還寫著「UNKNOWN 只在 DATA 已提交時才成立」——那句是假的。)

【P1-02】反方向:DATA 之後的【非逾時】斷線根本沒有被看。標記只在
`isinstance(e, socket.timeout)` 那條分支裡被讀取,於是 `SMTPServerDisconnected`
走一般重試 → 醫師收到兩封同樣的臨床通知。

【P1-01】部分收件人被拒只被記進帳本,然後照樣更新「已通知基準」。那幾位
這一輪沒收到,下一輪也不會再寄 —— 一則臨床通知永久消失,log 上是一次成功。

【P1-04】截圖 `img.save()` 寫到一半失敗 → 殘檔留在磁碟,而此刻 artifact 還
沒建立,收尾根本不知道有這個路徑。那是沒寄出去、沒人知道的病人畫面。

【P2-05/06/08】帳本:查詢只看啟動當下的記憶體快照、寫回失敗就沒有下文、
陳舊 PREPARED 沒有任何 API 看得到。
"""
import os
import socket
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import delivery_ledger as dl  # noqa: E402
from cmuh_common import smtp_mail as sm  # noqa: E402


# ===========================================================================
# SMTP 階段
# ===========================================================================
class _Server:
    """可觀測階段的假 server。`fail_at` = 在哪一步拋 `exc`。"""

    def __init__(self, fail_at=None, exc=None, refuse=()):
        self.fail_at, self.exc, self.refuse = fail_at, exc, set(refuse)
        self.steps = []

    def _maybe(self, step):
        self.steps.append(step)
        if step == self.fail_at:
            raise self.exc

    def ehlo(self):
        pass

    def starttls(self, **kw):
        pass

    def login(self, *a):
        self._maybe("login")

    def ehlo_or_helo_if_needed(self):
        pass

    def mail(self, addr, opts):
        self._maybe("mail")
        return (250, b"ok")

    def rcpt(self, addr, opts):
        self._maybe("rcpt")
        return (452, b"full") if addr in self.refuse else (250, b"ok")

    def rset(self):
        pass

    def data(self, payload):
        self._maybe("data")
        return (250, b"queued")


class _Ctx:
    def __init__(self, s):
        self._s = s

    def __enter__(self):
        return self._s

    def __exit__(self, *e):
        return False


def _run_send_once(monkeypatch, server, recipients=("a@x.com",)):
    monkeypatch.setattr(sm.smtplib, "SMTP", lambda *a, **k: _Ctx(server))
    msg = sm._build_message("me@x.com", "我", list(recipients), "主旨", "內文")
    return sm._send_once({"host": "h", "port": 587, "use_tls": True,
                          "username": "u", "password": "p"}, msg, 30.0)


class TestTheSubmittedMarkMeansWhatItSays:

    def test_a_timeout_before_data_is_not_marked_submitted(self, monkeypatch):
        """★核心(P1-03)★ MAIL/RCPT 階段逾時 = 伺服器確定還沒收到郵件內容。
        標成「已提交」的話,止掛提醒會被當成已寄、永久去重。"""
        srv = _Server(fail_at="mail", exc=socket.timeout("timed out"))
        with pytest.raises(BaseException) as ei:
            _run_send_once(monkeypatch, srv)
        assert not getattr(ei.value, sm.SUBMITTED_ATTR, False), (
            "★MAIL 階段的逾時被標成『已提交』★ 伺服器根本還沒收到內容,"
            "這則通知會被當成已寄而永久不再寄")

    def test_a_failure_during_data_is_marked_submitted(self, monkeypatch):
        """反方向:真的走到 DATA 才算結果不明(不可以連這個也一起放掉)。"""
        srv = _Server(fail_at="data", exc=socket.timeout("timed out"))
        with pytest.raises(BaseException) as ei:
            _run_send_once(monkeypatch, srv)
        assert getattr(ei.value, sm.SUBMITTED_ATTR, False), (
            "DATA 之後的中斷沒有被標成『已提交』→ 會被重送,收件人收到兩封")

    def test_partial_refusals_survive_the_low_level_flow(self, monkeypatch):
        """逐位 RCPT 的拒收資訊要保留下來(補寄要靠它)。"""
        srv = _Server(refuse=("bad@x.com",))
        refused = _run_send_once(monkeypatch, srv,
                                 ("ok@x.com", "bad@x.com"))
        assert "bad@x.com" in refused and "ok@x.com" not in refused, refused


class TestADisconnectAfterDataIsNotRetried:

    def _drive(self, monkeypatch, exc, submitted):
        calls = {"n": 0}

        def _once(cred, msg, timeout):
            calls["n"] += 1
            if submitted:
                setattr(exc, sm.SUBMITTED_ATTR, True)
            raise exc
        monkeypatch.setattr(sm, "_send_once", _once)
        # ★用生產本來就有的 override_credentials★ 自己拼一份 cred dict 去
        #   monkeypatch `load_credentials` 的話,少一個鍵(例如 `from_name`)
        #   就會在建信時 KeyError —— 測試會「失敗」但量到的完全不是要測的東西。
        cred = {"host": "h", "port": 587, "use_tls": True, "username": "u",
                "password": "p", "from_address": "me@x.com", "from_name": ""}
        # `send_mail` 內部是 `import time as _time`(函式區域 import),
        # 所以要換掉的是 time 模組本身的 sleep。
        import time as _t
        monkeypatch.setattr(_t, "sleep", lambda _s: None)
        try:
            sm.send_mail(recipients=["a@x.com"], subject="s", body="b",
                         max_retries=2, override_credentials=cred)
        except BaseException as e:
            return type(e), calls["n"]
        return None, calls["n"]

    def test_a_disconnect_after_data_is_unknown_not_retried(self, monkeypatch):
        """★核心(P1-02)★ DATA 送出後連線斷掉 —— 伺服器可能已收下。
        重送 = 醫師收到兩封同樣的臨床通知。標記代表「走到哪一步」,
        與例外是不是 socket.timeout 完全無關。"""
        kind, n = self._drive(
            monkeypatch, sm.smtplib.SMTPServerDisconnected("gone"),
            submitted=True)
        assert kind is sm.DeliveryOutcomeUnknown, (
            f"★DATA 之後的斷線沒有被判成『結果不明』★ 得到 {kind}")
        assert n == 1, f"★重送了 {n} 次★ 伺服器可能已經收下第一封"

    def test_a_disconnect_before_data_still_retries(self, monkeypatch):
        """不可以連正常的暫時故障也一起變成不重試(那是上一版矯枉過正的方向)。"""
        kind, n = self._drive(
            monkeypatch, sm.smtplib.SMTPServerDisconnected("gone"),
            submitted=False)
        assert kind is not sm.DeliveryOutcomeUnknown, "沒送出內容的斷線應該重試"
        assert n == 3, f"應該用滿 1+2 次嘗試,實際 {n}"


# ===========================================================================
# 帳本
# ===========================================================================
class TestTheLedgerCanSeeTheDisk:

    def test_a_query_sees_what_another_process_wrote(self, tmp_path):
        """★核心(P2-05)★ A 啟動之後 B 才寫入,A 直接查那把 key。
        只看記憶體的話 A 會說「沒有」→ 接成閘門就是跨 process 重複寄送。"""
        path = str(tmp_path / "l.json")
        a = dl.DeliveryLedger(path=path)
        b = dl.DeliveryLedger(path=path)
        did = b.begin(business_key="k1", category="t", recipients=["x@y.tw"])
        b.settle(did)
        assert a.has_live_delivery("k1"), (
            "★A 看不到 B 寫的那一筆★ 它只讀啟動當下的記憶體快照")

    def test_it_refuses_to_answer_when_the_disk_is_unreadable(self, tmp_path,
                                                              monkeypatch):
        """★讀不到就不要回答★ 回 True 會無聲擋掉臨床通知(2026-08-05 的形狀),
        回 False 是把「不知道」講成「沒有」。這個取捨得由呼叫端明寫。"""
        path = str(tmp_path / "l.json")
        led = dl.DeliveryLedger(path=path)
        led.settle(led.begin(business_key="k1", category="t",
                             recipients=["x@y.tw"]))
        monkeypatch.setattr(dl, "safe_load_json_ex",
                            lambda *a, **k: ({}, "error"))
        with pytest.raises(dl.LedgerUnavailable):
            led.has_live_delivery("k1")


class TestTerminalStateEventuallyLands:

    def test_a_transient_write_failure_is_retried(self, tmp_path,
                                                  monkeypatch):
        """★P2-06★ 防毒鎖住檔案那一瞬間不可以就這樣算了 —— 終局狀態只留在
        記憶體,而磁碟上還寫著 SUBMITTING。"""
        path = str(tmp_path / "l.json")
        led = dl.DeliveryLedger(path=path)
        did = led.begin(business_key="k1", category="t", recipients=["x@y.tw"])
        led.mark_submitting(did)
        n = {"i": 0}
        real = dl.safe_load_json_ex

        def _flaky(*a, **k):
            n["i"] += 1
            return ({}, "error") if n["i"] == 1 else real(*a, **k)
        monkeypatch.setattr(dl, "safe_load_json_ex", _flaky)
        monkeypatch.setattr(dl.time, "sleep", lambda _s: None)
        led.settle(did)
        monkeypatch.undo()
        fresh = dl.DeliveryLedger(path=path)
        assert fresh._records[did]["state"] != dl.SUBMITTING, (
            "★一次暫時失敗就讓終局狀態永遠留在記憶體★ 磁碟上還是 SUBMITTING")

    def test_flush_writes_what_is_still_pending(self, tmp_path, monkeypatch):
        """★沒有『下一次異動』時的出口★ 重試也失敗、而且之後再也沒有人動
        帳本 —— `flush()` 是最後一次機會。"""
        path = str(tmp_path / "l.json")
        led = dl.DeliveryLedger(path=path)
        did = led.begin(business_key="k1", category="t", recipients=["x@y.tw"])
        led.mark_submitting(did)
        monkeypatch.setattr(dl, "safe_load_json_ex",
                            lambda *a, **k: ({}, "error"))
        monkeypatch.setattr(dl.time, "sleep", lambda _s: None)
        led.settle(did)                      # 落不了地
        monkeypatch.undo()
        led.flush()                          # 磁碟好了 → 補寫
        fresh = dl.DeliveryLedger(path=path)
        assert fresh._records[did]["state"] != dl.SUBMITTING, (
            "flush() 沒有把還沒落地的終局狀態補寫上去")


class TestStalePreparedHasAnExit:

    def test_a_stale_prepared_is_listed_and_converged(self, tmp_path):
        """★P2-08★ PREPARED = 登記了但從來沒交給 SMTP。prune 保留它、
        `has_live_delivery` 算它 live,卻沒有任何 API 看得到它 ——
        接成閘門後會永久擋住一封確定從未寄出的信。"""
        path = str(tmp_path / "l.json")
        # ★[第 3 回] 原本斷言收斂成 FAILED,那是錯的★ 新的 `begin()` 直接
        #   落地成 SUBMITTING(不再產生 PREPARED);舊檔案裡既有的 PREPARED
        #   只能收斂成 UNKNOWN,理由見 TestStalePreparedIsNotDeclaredFailed。
        led = dl.DeliveryLedger(path=path)
        did = led.begin(business_key="k1", category="t", recipients=["x@y.tw"])
        with led._lock:                      # 手動塞一筆舊格式的 PREPARED
            led._records[did]["state"] = dl.PREPARED
            led._records[did]["updated_at"] = dl._now() - 3600
        assert [r["delivery_id"] for r in led.stale_prepared()] == [did]
        assert led.converge_stale_prepared() == 1
        assert led._records[did]["state"] == dl.UNKNOWN

    def test_a_submitting_record_is_never_converged(self, tmp_path):
        """★反方向★ SUBMITTING 代表「已經交出去了」,只能靠 Message-ID 回查。
        把它一起收斂成 FAILED 會讓一封【可能已送達】的信被重寄。"""
        path = str(tmp_path / "l.json")
        led = dl.DeliveryLedger(path=path)
        did = led.begin(business_key="k1", category="t", recipients=["x@y.tw"])
        led.mark_submitting(did)
        led._records[did]["updated_at"] = dl._now() - 86400
        assert led.converge_stale_prepared() == 0
        assert led._records[did]["state"] == dl.SUBMITTING


# ===========================================================================
# P1-01 暫時性拒收要真的補寄 / P1-04 截圖殘檔 / P2-07 事件識別
# ===========================================================================
import ast          # noqa: E402
import inspect      # noqa: E402
import textwrap     # noqa: E402

import consult_query as cq  # noqa: E402


def _Art():
    """★用生產的那個 dataclass★ 手工 stub 少一個欄位,`dataclasses.replace`
    就會 AttributeError —— 測試會紅,但量到的不是要測的東西。"""
    return cq._DeliveryArtifact(
        recipients=("ok@x.com", "bad@x.com"), subject="s", text_body="b",
        html_body="", attachment=None, message_id="<m@x>",
        business_key="consult:abc|aud")


class TestTransientRefusalsAreActuallyResent:

    def test_a_transient_refusal_is_resent_to_only_that_recipient(
            self, monkeypatch):
        """★核心★ 只補寄給被拒的那幾位。往上拋讓外層重試的話,已經收到的人
        會收到第二封 —— 要補的是那幾個人,不是那封信。"""
        seen = []

        def _send(att, subj, body, rcpts, **kw):
            seen.append(list(rcpts))
            return {}                       # 補寄成功
        monkeypatch.setattr(cq, "send_via_smtp", _send)
        left = cq._resend_transient_refusals(
            _Art(), {"bad@x.com": (452, b"full")})
        assert seen == [["bad@x.com"]], (
            f"★補寄的收件人不對★ {seen} —— 只能寄給被拒的那一位")
        assert not left, f"補寄成功卻仍留在拒收清單:{left}"

    def test_a_permanent_refusal_is_not_resent(self, monkeypatch):
        """5xx(位址打錯/帳號不存在)重寄一百次也不會變好,只是浪費配額。"""
        seen = []
        monkeypatch.setattr(cq, "send_via_smtp",
                            lambda *a, **k: seen.append(1) or {})
        left = cq._resend_transient_refusals(
            _Art(), {"gone@x.com": (550, b"no such user")})
        assert not seen, "★永久拒收也去重寄了★"
        assert "gone@x.com" in left, "永久拒收要留在清單裡讓人看到"

    def test_each_retry_gets_its_own_message_id(self, monkeypatch):
        """★這裡原本斷言「沿用同一個 Message-ID」——那個設計是錯的★

        沿用的理由曾經是「對那些人來說這是同一封信」。但那些人【一封都沒
        收到】,所以根本沒有重複的問題;沿用的代價是:將來拿這個 Message-ID
        去 Gmail 寄件備份回查,找到的會是初次寄送(它成功送達了 A),
        於是把 B 誤判成也送到了。每一次補寄要自己一個 ID 才回查得出來。
        """
        got = {}
        monkeypatch.setattr(
            cq, "send_via_smtp",
            lambda att, s, b, r, **kw: got.update(kw) or {})
        cq._resend_transient_refusals(_Art(), {"bad@x.com": (452, b"full")})
        assert got.get("message_id"), "補寄沒有帶 Message-ID"
        assert got["message_id"] != "<m@x>", (
            "★補寄沿用了初次的 Message-ID★ 回查寄件備份會找到初次那一封"
            "(它送達的是【別人】),把這位收件人誤判成已送達")

    def test_an_unknown_retry_does_not_destroy_the_known_result(self,
                                                                monkeypatch):
        """★核心(第 2 回 P2-7)★ 初次送達 A、暫時拒收 B;補寄 B 時結果不明。
        不可以因此把【已經確定送達 A】這個事實也一起變成未知,也不可以往上
        拋讓整個工作重跑(那會讓 A 再收一次)。"""
        settled = []
        monkeypatch.setattr(cq, "_delivery_begin", lambda *a, **k: "rid")
        monkeypatch.setattr(cq, "_delivery_settle",
                            lambda did, **kw: settled.append(kw))

        def _boom(*a, **k):
            raise cq.DeliveryOutcomeUnknown("結果不明")
        monkeypatch.setattr(cq, "send_via_smtp", _boom)
        left = cq._resend_transient_refusals(
            _Art(), {"bad@x.com": (452, b"full")})
        assert left == {"bad@x.com": (452, b"full")}, (
            f"補寄結果不明時要保留原拒收清單:{left}")
        assert settled == [{"unknown": True}], (
            f"補寄那一筆要自己結案成 UNKNOWN:{settled}")

    def test_the_send_path_actually_calls_it(self):
        """★接線★ helper 存在但沒人呼叫 = 什麼都沒修(這正是外審對舊帳本
        `needs_recipient_retry()` 的指控:只有單元測試在用)。"""
        src = textwrap.dedent(inspect.getsource(cq._do_full_job))
        names = {n.func.id for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "_resend_transient_refusals" in names, (
            "★寄送路徑沒有呼叫補寄★ 被拒的收件人仍然永久收不到")


class TestAFailedScreenshotLeavesNothingBehind:

    def test_a_partial_write_is_cleaned_up(self, tmp_path, monkeypatch):
        """★核心(P1-04)★ 寫到一半失敗時 artifact 還沒建立,收尾不知道有這個
        路徑 —— 一張沒寄出去、沒人知道的病人畫面就留在磁碟上。"""
        from PIL import Image
        monkeypatch.setattr(cq, "SHOTS_DIR", tmp_path)
        monkeypatch.setattr(cq, "_prune_old_shots", lambda: None)
        img = Image.new("RGB", (4, 4))

        # ★反例要重現真正的失敗形狀★ 磁碟滿的時候 PIL 是【先把檔案建好、
        #   寫了一部分才失敗】。stub 若在建檔之前就拋,那就沒有殘檔可留 ——
        #   把修正退回原樣測試照樣綠,等於什麼都沒測到(突變驗證教的)。
        def _boom(self, fp, *a, **k):
            with open(fp, "wb") as f:
                f.write(b"PNG-partial")
            raise OSError("磁碟滿了")
        monkeypatch.setattr(Image.Image, "save", _boom)
        with pytest.raises(OSError):
            cq._materialize_shot(img)
        # 只看截圖本身:tmp_path 裡可能有別的模組建立的工作目錄。
        left = sorted(p.name for p in tmp_path.iterdir()
                      if p.is_file() and ("consult" in p.name
                                          or p.name.endswith(".part")))
        assert not left, f"★留下了寫到一半的病人畫面★:{left}"

    def test_a_successful_write_produces_a_readable_png(self, tmp_path,
                                                        monkeypatch):
        """★不可以為了清乾淨而讓正常路徑壞掉★ PIL 是從副檔名推格式的,
        暫存檔名沒講清楚格式的話,每一張截圖都會存不了(比原本的問題嚴重)。"""
        from PIL import Image
        monkeypatch.setattr(cq, "SHOTS_DIR", tmp_path)
        monkeypatch.setattr(cq, "_prune_old_shots", lambda: None)
        out = cq._materialize_shot(Image.new("RGB", (4, 4), (1, 2, 3)))
        assert out.exists() and out.suffix == ".png", out
        assert Image.open(out).size == (4, 4)
        assert not list(tmp_path.glob("*.part")), "暫存檔沒有被改名掉"


class TestTheConsultEventKeyIsStableAndCarriesNoPHI:

    def test_the_same_roster_gives_the_same_key(self):
        rows = ["1234567 王小明 8/8 10:00", "7654321 李小華 8/8 11:00"]
        k1 = cq._consult_business_key(rows, ["a@x.tw", "b@x.tw"])
        k2 = cq._consult_business_key(list(reversed(rows)), ["b@x.tw", "a@x.tw"])
        assert k1 == k2, f"同一批會診卻得到兩把鑰匙:{k1} vs {k2}"

    def test_a_different_audience_is_a_different_delivery(self):
        rows = ["1234567 王小明 8/8 10:00"]
        assert (cq._consult_business_key(rows, ["team@x.tw"])
                != cq._consult_business_key(rows, ["doctor@x.tw"])), (
            "同一份清單寄給團隊、寄給觸發醫師本人,是兩件不同的寄送")

    def test_no_chart_number_appears_in_the_key(self):
        """★帳本會落地成磁碟上的 JSON★ 病歷號不可以出現在裡面。"""
        key = cq._consult_business_key(["1234567 王小明 8/8 10:00"],
                                       ["a@x.tw"])
        assert "1234567" not in key, f"★病歷號被寫進帳本的 key★:{key}"
        assert "王小明" not in key, key


    def test_the_send_path_computes_the_key(self):
        """★接線★ helper 算得再對,建 artifact 時沒傳進去也是白搭
        (突變驗證抓到的:把建構處改成 `business_key=""`,只測 helper 的
         那三個測試照樣全綠)。"""
        src = textwrap.dedent(inspect.getsource(cq._do_full_job))
        for n in ast.walk(ast.parse(src)):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "_DeliveryArtifact"):
                kw = {k.arg: k.value for k in n.keywords}
                val = kw.get("business_key")
                assert (isinstance(val, ast.Call)
                        and isinstance(val.func, ast.Name)
                        and val.func.id == "_consult_business_key"), (
                    "★建 artifact 時沒有算事件識別★ 帳本會退回用主旨當 key")
                return
        pytest.fail("找不到 _DeliveryArtifact 的建構處")


# ===========================================================================
# 外審第 10 輪【第 2 回】—— 其中兩個 P1 是上一回的修正自己開的洞
# ===========================================================================
class TestTheThreeOutcomesOfDATA:
    """`smtplib.SMTP.data()` 有三種結局,而且【形狀不一樣】。

    上一回改成低階流程時只處理了一種,另外兩種各自變成一個 P1:
      ① 對 DATA 指令的回應不是 354 → raise SMTPDataError,內容還沒送出。
      ② 送完內容後的最終回應不是 250 → 它【回傳】tuple,不 raise。
      ③ 傳輸中斷 → 其他例外。只有這一種是真的結果不明。
    """

    class _Srv(_Server):
        def __init__(self, data_result=(250, b"queued"), data_exc=None):
            super().__init__()
            self._res, self._exc = data_result, data_exc
            self.reset_called = False

        def rset(self):
            self.reset_called = True

        def data(self, payload):
            if self._exc:
                raise self._exc
            return self._res

    def test_a_final_rejection_is_not_reported_as_success(self, monkeypatch):
        """★核心(②)★ 伺服器明確拒絕(554),我們卻回報成功、還把已通知基準
        往前推 —— 那批會診從此不會再寄給任何人。"""
        srv = self._Srv(data_result=(554, b"rejected"))
        with pytest.raises(sm.smtplib.SMTPDataError):
            _run_send_once(monkeypatch, srv)
        assert srv.reset_called, "被拒之後要 rset,不可以讓連線留在半途"

    def test_a_final_rejection_is_definite_not_unknown(self, monkeypatch):
        """明確被拒 = 確定沒送出,不是「可能送到一半」。"""
        srv = self._Srv(data_result=(451, b"try later"))
        with pytest.raises(BaseException) as ei:
            _run_send_once(monkeypatch, srv)
        assert not getattr(ei.value, sm.SUBMITTED_ATTR, False), (
            "★明確的拒絕被標成『結果不明』★ 它會變成不重試、而且被當成已寄")

    def test_a_pre_content_4xx_stays_retryable(self, monkeypatch):
        """★核心(①)★ 對 DATA 指令回 451 → smtplib 在【送出內容之前】就
        SMTPDataError。止掛提醒若把它當成 UNKNOWN,會永久去重一封確定
        沒寄出的信 —— 正是上一回要修的那個病灶,換個位置又做了一次。"""
        srv = self._Srv(data_exc=sm.smtplib.SMTPDataError(451, b"busy"))
        with pytest.raises(sm.smtplib.SMTPDataError) as ei:
            _run_send_once(monkeypatch, srv)
        assert not getattr(ei.value, sm.SUBMITTED_ATTR, False), (
            "★內容根本還沒送出,卻被標成『已提交』★")

    def test_a_transport_loss_is_still_unknown(self, monkeypatch):
        """③ 不可以連真正的結果不明也一起放掉(那會變成重複寄送)。"""
        srv = self._Srv(data_exc=sm.smtplib.SMTPServerDisconnected("gone"))
        with pytest.raises(BaseException) as ei:
            _run_send_once(monkeypatch, srv)
        assert getattr(ei.value, sm.SUBMITTED_ATTR, False)


class TestTheLedgerLockOrderMatchesTheWriters:

    def test_refresh_takes_the_same_lock_order_as_mutators(self):
        """★死結★ 所有 mutator 都是「先 self._lock、再檔案鎖」
        (`settle()` 在 `with self._lock:` 裡呼叫 `_save_locked()`)。
        `_refresh_locked` 反過來拿的話,兩個執行緒對撞就互等。"""
        src = textwrap.dedent(inspect.getsource(dl.DeliveryLedger._refresh_locked))
        tree = ast.parse(src)
        order = []
        for n in ast.walk(tree):
            if isinstance(n, ast.With):
                for item in n.items:
                    e = item.context_expr
                    if isinstance(e, ast.Attribute) and e.attr == "_lock":
                        order.append("thread")
                    elif (isinstance(e, ast.Call)
                          and isinstance(e.func, ast.Attribute)
                          and e.func.attr == "_interprocess_lock"):
                        order.append("file")
        assert order[:2] == ["thread", "file"], (
            f"★鎖序與寫入端相反★:{order} —— 兩個執行緒對撞會互等")

    def test_a_concurrent_write_is_not_lost_to_a_stale_snapshot(self, tmp_path):
        """重讀不可以把【本執行緒剛寫進記憶體】的那筆蓋掉。"""
        led = dl.DeliveryLedger(path=str(tmp_path / "l.json"))
        did = led.begin(business_key="k1", category="t", recipients=["a@x.tw"])
        led.settle(did)
        assert led.has_live_delivery("k1")
        assert did in led._records, "重讀把自己剛寫的那筆弄丟了"


class TestRecoveryIsWiredNotJustAvailable:
    """★有 API ≠ 會發生★ 上一回加了 `flush()` 與 `converge_stale_prepared()`,
    但整個 repo 只有測試在呼叫 —— 註解卻寫著「程式結束時會再試一次」。"""

    def test_a_new_ledger_converges_stale_prepared_by_itself(self, tmp_path):
        path = str(tmp_path / "l.json")
        a = dl.DeliveryLedger(path=path)
        did = a.begin(business_key="k1", category="t", recipients=["a@x.tw"])
        with a._lock:                        # 舊格式:磁碟上是 PREPARED
            a._records[did]["state"] = dl.PREPARED
            a._records[did]["updated_at"] = dl._now() - 3600
            a._dirty.add(did)
            a._save_locked()
        fresh = dl.DeliveryLedger(path=path)         # 開機
        assert fresh._records[did]["state"] == dl.UNKNOWN, (
            "★開機沒有自動收斂舊格式的陳舊 PREPARED★ 它會永遠留著、把 key 擋住")

    def test_flush_is_registered_for_process_exit(self):
        src = textwrap.dedent(
            inspect.getsource(dl.DeliveryLedger._wire_lifecycle))
        assert "atexit.register" in src, "一般結束沒有補寫出口"
        assert "converge_stale_prepared" in src, "開機沒有收斂陳舊 PREPARED"

    def test_the_os_exit_paths_flush_too(self):
        """★atexit 不夠★ 會診程式有兩條 `os._exit()` 出口,它不跑 atexit。"""
        src = inspect.getsource(cq)
        tree = ast.parse(src)
        exits = 0
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            body = ast.dump(fn)
            if "'_exit'" not in body:
                continue
            exits += 1
            assert "_flush_delivery_ledger" in body, (
                f"★{fn.name} 直接 os._exit,帳本的變更會消失★")
        assert exits >= 2, f"預期至少兩條 os._exit 出口,只找到 {exits}"


class TestTheUnsignedKeyStillSeparatesEvents:

    def test_two_unsigned_deliveries_on_different_subjects_differ(self):
        """★核心(第 2 回 P2-6)★ 解析失敗時,退化 key 上一版只有收件人雜湊 ——
        於是每一次解析失敗的寄送都是同一把鑰匙,不分日期、不分主旨。"""
        a = cq._consult_business_key(None, ["t@x.tw"], "會診 A")
        b = cq._consult_business_key(None, ["t@x.tw"], "會診 B")
        assert a != b, f"★不同事件共用同一把鑰匙★:{a}"

    def test_the_same_unsigned_job_retried_keeps_its_key(self):
        """同一個工作重試要得到同一把鑰匙(否則稽核紀錄會爆量)。"""
        assert (cq._consult_business_key(None, ["t@x.tw"], "會診 A")
                == cq._consult_business_key([], ["t@x.tw"], "會診 A"))


# ===========================================================================
# 外審第 10 輪【第 3 回】
# ===========================================================================
class TestAPersistentTransientRefusalIsNotAbandoned:
    """★核心(第 3 回 P1-1)★ 信箱滿、greylisting 不會在毫秒之間好起來。
    「同一輪連按兩次」幾乎必然全部失敗,而失敗之後主流程照樣把已通知基準
    往前推 —— 那位收件人就永久收不到這則臨床通知了。"""

    def _clear(self):
        with cq._pending_refusal_lock:
            cq._pending_refusal_retries.clear()

    def test_it_is_scheduled_for_a_later_round(self, monkeypatch):
        self._clear()
        cq._schedule_refusal_retry(_Art(), {"bad@x.com": (452, b"full")}, "poll")
        assert len(cq._pending_refusal_retries) == 1, "沒有排進退避佇列"
        assert cq._pending_refusal_retries[0]["due_at"] > 0

    def test_a_due_retry_is_attempted_and_backs_off_again(self, monkeypatch):
        """仍失敗 → 用下一段更長的退避再排一次,不是就地放棄。"""
        self._clear()
        monkeypatch.setattr(cq, "_delivery_begin", lambda *a, **k: "rid")
        monkeypatch.setattr(cq, "_delivery_settle", lambda *a, **k: None)
        monkeypatch.setattr(cq, "send_via_smtp",
                            lambda *a, **k: {"bad@x.com": (452, b"full")})
        cq._schedule_refusal_retry(_Art(), {"bad@x.com": (452, b"full")}, "poll")
        cq._pending_refusal_retries[0]["due_at"] = 0.0
        cq._drain_pending_refusal_retries(now=100.0)
        assert len(cq._pending_refusal_retries) == 1, "沒有重新排程"
        assert cq._pending_refusal_retries[0]["attempt"] == 1
        assert (cq._pending_refusal_retries[0]["due_at"]
                == 100.0 + cq._REFUSAL_RETRY_BACKOFF_SEC[1]), "退避沒有變長"
        self._clear()

    def test_a_successful_retry_leaves_the_queue_empty(self, monkeypatch):
        self._clear()
        monkeypatch.setattr(cq, "_delivery_begin", lambda *a, **k: "rid")
        monkeypatch.setattr(cq, "_delivery_settle", lambda *a, **k: None)
        monkeypatch.setattr(cq, "send_via_smtp", lambda *a, **k: {})
        cq._schedule_refusal_retry(_Art(), {"bad@x.com": (452, b"full")}, "poll")
        cq._pending_refusal_retries[0]["due_at"] = 0.0
        cq._drain_pending_refusal_retries(now=100.0)
        assert not cq._pending_refusal_retries, "補寄成功卻還留在佇列裡"

    def test_giving_up_says_who_never_got_it(self, monkeypatch, caplog):
        """★用完退避不可以無聲吞掉★ 記憶體佇列會被重啟清空,所以「誰沒收到」
        一定要留在 log 裡,不能假裝寄成功了。"""
        self._clear()
        monkeypatch.setattr(cq, "_delivery_begin", lambda *a, **k: "rid")
        monkeypatch.setattr(cq, "_delivery_settle", lambda *a, **k: None)
        monkeypatch.setattr(cq, "send_via_smtp",
                            lambda *a, **k: {"bad@x.com": (452, b"full")})
        cq._schedule_refusal_retry(_Art(), {"bad@x.com": (452, b"full")}, "poll")
        with cq._pending_refusal_lock:
            cq._pending_refusal_retries[0]["attempt"] = (
                len(cq._REFUSAL_RETRY_BACKOFF_SEC) - 1)
            cq._pending_refusal_retries[0]["due_at"] = 0.0
        with caplog.at_level("ERROR"):
            cq._drain_pending_refusal_retries(now=100.0)
        assert not cq._pending_refusal_retries
        assert any("bad@x.com" in r.getMessage() for r in caplog.records), (
            "★放棄時沒有講出是誰沒收到★")

    def test_the_send_path_schedules_what_it_could_not_fix(self):
        """★接線★ 主流程馬上要推進已通知基準,不排程就等於永久漏寄。"""
        src = textwrap.dedent(inspect.getsource(cq._do_full_job))
        names = {n.func.id for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "_schedule_refusal_retry" in names, "沒有把補不完的排進退避佇列"
        assert "_drain_pending_refusal_retries" in names, "沒有人處理到期的補寄"


class TestFlushHoldsTheLock:

    def test_flush_saves_while_holding_the_lock(self):
        """★關機緒與寄送緒會同時動這本帳★ `_dirty` 在迭代中被改、或剛加進來
        的標記被 clear() 一起清掉卻沒落地。"""
        src = textwrap.dedent(inspect.getsource(dl.DeliveryLedger.flush))
        tree = ast.parse(src)
        for n in ast.walk(tree):
            if isinstance(n, ast.With):
                inner = {c.func.attr for c in ast.walk(n)
                         if isinstance(c, ast.Call)
                         and isinstance(c.func, ast.Attribute)}
                if "_save_locked" in inner:
                    return
        pytest.fail("★flush() 在鎖外面存檔★ 併發變更會遺失或炸掉")


class TestStalePreparedIsNotDeclaredFailed:

    def test_begin_lands_as_submitting(self, tmp_path):
        """★把不該存在的區別拿掉★ 落地的 PREPARED 之所以危險,是因為
        `mark_submitting` 的寫回是 fail-open 的:它只改到記憶體、信卻寄出去了,
        磁碟上留下的一樣是 PREPARED。登記當下就記成 SUBMITTING,
        「可能已交出去」是安全的方向。"""
        led = dl.DeliveryLedger(path=str(tmp_path / "l.json"))
        did = led.begin(business_key="k", category="t", recipients=["a@x.tw"])
        assert led.get(did)["state"] == dl.SUBMITTING

    def test_a_legacy_stale_prepared_becomes_unknown_not_failed(self, tmp_path):
        """★不可以推論成「確定沒寄出」★ 那個推論的前提(狀態轉移一定落得了地)
        並不成立。判成 FAILED 會把一封可能已送達的信寫成沒送出:稽核造假,
        接成閘門後還會放行重寄。UNKNOWN 才是誠實的答案。"""
        led = dl.DeliveryLedger(path=str(tmp_path / "l.json"))
        did = led.begin(business_key="k", category="t", recipients=["a@x.tw"])
        with led._lock:                      # 手動塞一筆舊格式的 PREPARED
            led._records[did]["state"] = dl.PREPARED
            led._records[did]["updated_at"] = dl._now() - 3600
        assert led.converge_stale_prepared() == 1
        assert led.get(did)["state"] == dl.UNKNOWN, (
            "★被判成確定沒寄出★ 但寫回是 fail-open 的,這推論不成立")
        assert any(r["delivery_id"] == did for r in led.unresolved()), (
            "收斂之後要進得了既有的 Message-ID 回查路徑")


class TestEveryForcedExitFlushes:

    def test_the_health_monitor_can_flush_before_killing(self):
        from cmuh_common import health as hl
        loop = textwrap.dedent(inspect.getsource(hl._health_loop))
        assert "pre_exit_callback" in loop, "RAM 保護的 os._exit 沒有結束前回呼"
        # ★回呼是選用的:沒有它時【仍然要結束】★ 把 os._exit 縮進
        #   `if pre_exit_callback is not None:` 裡面的話,沒帶回呼的行程
        #   就永遠不會被重啟 —— RAM 洩漏防護整個失效。用 AST 判斷它在不在
        #   那個 if 底下,而不是比對字串長相。
        tree = ast.parse(loop)
        guarded = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.If) and "pre_exit_callback" in ast.dump(n.test):
                guarded |= {id(x) for x in ast.walk(
                    ast.Module(body=n.body, type_ignores=[]))}
        exits = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "_exit"]
        assert exits, "找不到 os._exit"
        assert any(id(e) not in guarded for e in exits), (
            "★os._exit 被縮進『有回呼才做』的分支裡★ 沒帶回呼的行程不會重啟了")

    def test_both_programs_pass_a_pre_exit_callback(self):
        import re
        for path in ("src/consult_query.py", "src/main.py"):
            txt = open(path, encoding="utf-8").read()
            m = re.search(r"start_health_monitor\((.{0,200})", txt, re.S)
            assert m and "pre_exit_callback" in m.group(1), (
                f"{path} 的 health monitor 沒有帶結束前補寫")

    def test_mains_normal_close_flushes_the_delivery_ledger(self):
        """★這與 health monitor 的回呼是【兩條不同的路】★
        主程式平常是走自己的關閉流程 + `os._exit(0)` 結束的(不跑 atexit),
        那裡原本只排空【動作稽核】帳本,寄送帳本完全沒被收尾。
        (突變驗證抓到的:只檢查 health monitor 的那個測試照樣綠。)
        """
        tree = ast.parse(open("src/main.py", encoding="utf-8").read())
        found = []
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            names = {n.func.id for n in ast.walk(fn)
                     if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
            if "_flush_ledger_before_exit" not in names:
                continue
            if fn.name.startswith("_flush"):
                continue                       # 定義本身不算
            # ★不可以找到一個就 return★(這個測試自己第一版就是這樣寫的)
            #   main.py 有【三】條結束路徑:關閉、更新重啟、交接。
            #   只檢查第一個找到的,另外兩條漏掉也照樣綠 —— 而更新重啟
            #   正是最頻繁的那一條。
            found.append(fn.name)
            assert "_flush_delivery_ledger_before_exit" in names, (
                f"★{fn.name} 只排空稽核帳本,寄送帳本的終局狀態會消失★")
        assert len(found) >= 3, (
            f"預期至少三條結束路徑(關閉/更新重啟/交接),只找到 {found}")


# ===========================================================================
# 外審第 10 輪【第 4 回】
# ===========================================================================
class TestAMissedRecipientSurvivesARestart:
    """★核心(第 4 回 P1-1)★ 退避佇列在記憶體裡,程式一重啟就忘光 ——
    連「講清楚誰沒收到」的告警都不會執行,因為佇列已隨 process 消失。
    但【帳本是落地的】:重啟之後 `needs_recipient_retry()` 仍看得到。"""

    def _ledger(self, tmp_path, monkeypatch, age_sec):
        led = dl.DeliveryLedger(path=str(tmp_path / "l.json"))
        did = led.begin(business_key="k1", category="consult",
                        recipients=["ok@x.tw", "bad@x.tw"], subject="會診清單")
        led.settle(did, refused={"bad@x.tw": (452, b"full")})
        # ★老化必須【落地】★（2026-08-09）
        #   `needs_recipient_retry()` 現在會先從磁碟重讀（帳本跨 process 共用）。
        #   只改記憶體裡的 `created_at` 會被重讀蓋回原值，這個 fixture 就等於
        #   沒有老化 —— 測試量到的不是「掛太久的那一筆」。
        led._records[did]["created_at"] = dl._now() - age_sec
        led._dirty.add(did)
        led.flush()
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        return led, did

    def test_a_stale_pending_retry_is_closed_out_and_alerted(
            self, tmp_path, monkeypatch, caplog):
        led, did = self._ledger(tmp_path, monkeypatch, age_sec=7200)
        assert [d for d, _ in led.needs_recipient_retry()] == [did]
        alerts = []
        monkeypatch.setattr(cq, "_alert_missed_recipients",
                            lambda who, subj, why: alerts.append((who, why)))
        with caplog.at_level("ERROR"):
            cq._close_out_stale_recipient_retries()
        assert not led.needs_recipient_retry(), (
            "★結案之後不該再被列出★ 否則每一輪都會重複告警")
        assert alerts and "bad@x.tw" in alerts[0][0], (
            f"★沒有告警說誰沒收到★:{alerts}")
        assert any("bad@x.tw" in r.getMessage() for r in caplog.records)

    def test_a_fresh_pending_retry_is_left_to_the_backoff_queue(
            self, tmp_path, monkeypatch):
        """★不可以搶在退避窗口內就判死★ 那會把一個還會自己好的暫態,
        變成一則「送不到」的告警 + 不再補寄。"""
        led, did = self._ledger(tmp_path, monkeypatch, age_sec=60)
        alerts = []
        monkeypatch.setattr(cq, "_alert_missed_recipients",
                            lambda *a: alerts.append(a))
        cq._close_out_stale_recipient_retries()
        assert [d for d, _ in led.needs_recipient_retry()] == [did], (
            "還在退避窗口內就被結案了")
        assert not alerts

    def test_the_job_calls_the_close_out(self):
        """★接線★ 沒人呼叫的話,重啟之後那筆就永遠掛在帳上、沒有人知道。"""
        src = textwrap.dedent(inspect.getsource(cq._do_full_job))
        names = {n.func.id for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "_close_out_stale_recipient_retries" in names


class TestTheRestartBranchAlsoFlushes:

    def test_pre_exit_runs_before_restart_callback(self):
        """★核心(第 4 回 P2-2)★ 主程式有傳 `restart_callback`,而它成功時會
        【自己 os._exit】—— 所以「沒有 restart_callback」那個分支永遠走不到。
        收尾必須在呼叫 restart_callback【之前】。"""
        from cmuh_common import health as hl
        src = textwrap.dedent(inspect.getsource(hl._health_loop))
        tree = ast.parse(src)
        pre_line = restart_line = None
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name):
                if n.func.id == "pre_exit_callback" and pre_line is None:
                    pre_line = n.lineno
                if n.func.id == "restart_callback":
                    restart_line = n.lineno
        assert pre_line is not None and restart_line is not None, (
            f"pre={pre_line} restart={restart_line}")
        assert pre_line < restart_line, (
            "★收尾排在 restart_callback 後面★ 它成功時會自己結束行程,"
            "後面那段永遠不會執行")


# ===========================================================================
# 外審第 10 輪【第 5 回】—— 上一回的修正自己開的洞
# ===========================================================================
class TestASuccessfulRetryClosesTheOriginalRecord:
    """★核心(第 5 回)★ 補寄是自己一筆(自己的 Message-ID,回查才問得出答案),
    但「這位收件人到底收到了沒有」的答案必須回寫到【初次】那一筆。

    不回寫的話:初次紀錄永遠掛著暫時被拒 → 一小時後
    `_close_out_stale_recipient_retries()` 把它判成「始終沒收到」、標成永久
    失敗、寄開發者告警 → 人工照著告警轉寄 = 醫師收到【重複的臨床通知】。
    上一回為了「不漏寄」加的東西,自己製造了一條「誤報漏寄」的路。
    """

    def _setup(self, tmp_path, monkeypatch):
        led = dl.DeliveryLedger(path=str(tmp_path / "l.json"))
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        origin = led.begin(business_key="k1", category="consult",
                           recipients=["ok@x.tw", "bad@x.tw"], subject="會診")
        led.settle(origin, refused={"bad@x.tw": (452, b"full")})
        return led, origin

    def test_the_origin_no_longer_needs_retry_after_a_successful_resend(
            self, tmp_path, monkeypatch):
        led, origin = self._setup(tmp_path, monkeypatch)
        assert [d for d, _ in led.needs_recipient_retry()] == [origin]
        monkeypatch.setattr(cq, "send_via_smtp", lambda *a, **k: {})
        left = cq._resend_transient_refusals(
            _Art(), {"bad@x.tw": (452, b"full")}, "poll", origin_did=origin)
        assert not left
        assert not led.needs_recipient_retry(), (
            "★補寄成功了,初次紀錄卻還掛著暫時被拒★ 一小時後會被誤報成漏收,"
            "人工照著告警轉寄就是重複的臨床通知")
        assert led.get(origin)["recipients"]["bad@x.tw"] == dl.R_CONFIRMED

    def test_a_still_refused_recipient_stays_pending(self, tmp_path,
                                                     monkeypatch):
        """★反方向★ 補寄仍被拒的,不可以被順手結掉(那才是真的漏寄)。"""
        led, origin = self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(cq, "send_via_smtp",
                            lambda *a, **k: {"bad@x.tw": (452, b"full")})
        cq._resend_transient_refusals(
            _Art(), {"bad@x.tw": (452, b"full")}, "poll", origin_did=origin)
        assert [d for d, _ in led.needs_recipient_retry()] == [origin], (
            "仍然被拒卻被結掉了 —— 這位收件人的漏寄會就此消失")

    def test_the_retry_record_points_back_at_the_original(self, tmp_path,
                                                          monkeypatch):
        """關聯要留在帳上,不能只靠命名慣例。"""
        led, origin = self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(cq, "send_via_smtp", lambda *a, **k: {})
        cq._resend_transient_refusals(
            _Art(), {"bad@x.tw": (452, b"full")}, "poll", origin_did=origin)
        kids = [r for r in led._records.values()
                if r.get("parent_id") == origin]
        assert kids, "補寄那一筆沒有指回初次紀錄"

    def test_the_send_path_passes_the_origin(self):
        """★接線★ 不傳 origin 的話,回寫永遠不會發生。"""
        src = textwrap.dedent(inspect.getsource(cq._do_full_job))
        for n in ast.walk(ast.parse(src)):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "_resend_transient_refusals"):
                assert any(k.arg == "origin_did" for k in n.keywords), (
                    "★沒有把初次的 delivery id 傳下去★ 補寄成功也回寫不了")
                return
        pytest.fail("找不到補寄呼叫點")

    def test_retry_records_do_not_double_count_as_pending(self, tmp_path,
                                                          monkeypatch):
        """★同一位收件人只能有一個權威狀態★ 補寄那一筆也被列成待補寄的話,
        同一個人會被重複結案、重複告警,帳上的待辦數還會隨補寄次數膨脹。"""
        led, origin = self._setup(tmp_path, monkeypatch)
        monkeypatch.setattr(cq, "send_via_smtp",
                            lambda *a, **k: {"bad@x.tw": (452, b"full")})
        cq._resend_transient_refusals(
            _Art(), {"bad@x.tw": (452, b"full")}, "poll", origin_did=origin)
        pending = led.needs_recipient_retry()
        assert [d for d, _ in pending] == [origin], (
            f"★補寄紀錄自己也被列成待補寄★:{[d for d, _ in pending]}")

    def test_a_mixed_case_recipient_is_also_written_back(self, tmp_path,
                                                         monkeypatch):
        """★正規化要與 begin() 一致★ 帳上的 key 是小寫,補寄拿到的位址卻是
        設定檔原樣(只 strip、沒 lower)。只要收件人有一個大寫字母,回寫就
        對不上 —— 初次紀錄繼續掛著暫時被拒,一小時後誤報漏收。
        (前一回才修掉這條路,換成大小寫又走了一次。)"""
        led = dl.DeliveryLedger(path=str(tmp_path / "l.json"))
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        origin = led.begin(business_key="k1", category="consult",
                           recipients=["OK@X.tw", "Bad@X.tw"], subject="會診")
        led.settle(origin, refused={"Bad@X.tw": (452, b"full")})
        assert [d for d, _ in led.needs_recipient_retry()] == [origin]
        monkeypatch.setattr(cq, "send_via_smtp", lambda *a, **k: {})
        cq._resend_transient_refusals(
            _Art(), {"Bad@X.tw": (452, b"full")}, "poll", origin_did=origin)
        assert not led.needs_recipient_retry(), (
            "★大小寫不同就回寫不到★ 這位收件人會被誤報成始終沒收到")
