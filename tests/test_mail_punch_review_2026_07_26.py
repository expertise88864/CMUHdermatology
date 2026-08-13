# -*- coding: utf-8 -*-
"""[2026-07-26 未審模組 review] smtp_mail / punch_status。

兩支都是「告警的最後一哩」:壞掉的表現是【該來的信沒來】或【假的告警一直來】,
而不是當機 —— 正是這批一路在修的「故障看起來跟正常一樣」。
"""
import inspect
import logging
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import punch_status as ps  # noqa: E402
from cmuh_common import smtp_mail as sm  # noqa: E402


def _code_only(src: str) -> str:
    src = re.sub(r'("""|\'\'\')(?:.|\n)*?\1', "", src)
    return "\n".join(line.split("#")[0] for line in src.splitlines())


# ── smtp:部分收件人被拒不可回報「已寄出」 ─────────────────────────────────────
def test_partial_recipient_refusal_is_reported(monkeypatch, caplog):
    """★假成功★ smtplib 只有在【全部】收件人被拒時才拋 SMTPRecipientsRefused;
    只有一部分被拒(信箱打錯/對方信箱滿)是【正常返回】並把被拒者放在回傳值裡。
    舊版丟掉回傳值 → 那些人永遠收不到止掛提醒/會診通知,而 log 說「已寄出 → 全部人」。"""
    monkeypatch.setattr(sm, "_send_once",
                        lambda *a, **k: {"bad@x.com": (550, b"no such user")})
    monkeypatch.setattr(sm, "load_credentials", lambda: {
        "host": "h", "port": 587, "username": "u", "password": "p",
        "use_tls": True, "from_address": "u@x.com", "from_name": "n"})
    with caplog.at_level(logging.INFO):
        refused = sm.send_mail(["ok@x.com", "bad@x.com"], "主旨", "內文")
    assert refused == {"bad@x.com": (550, b"no such user")}, "要把被拒清單回給呼叫端"
    msgs = [r.getMessage() for r in caplog.records]
    assert any("部分收件人被拒" in m and "bad@x.com" in m for m in msgs), \
        "被拒的人一定要出現在 log"
    # 「已寄出」那行不可再宣稱送給了被拒的人
    sent_lines = [m for m in msgs if "已寄出" in m]
    assert sent_lines and all("bad@x.com" not in m for m in sent_lines), \
        "成功訊息不可包含沒收到信的人"


def test_all_delivered_keeps_original_message(monkeypatch, caplog):
    """不可誤報:全部送達時訊息與行為維持原樣。"""
    monkeypatch.setattr(sm, "_send_once", lambda *a, **k: {})
    monkeypatch.setattr(sm, "load_credentials", lambda: {
        "host": "h", "port": 587, "username": "u", "password": "p",
        "use_tls": True, "from_address": "u@x.com", "from_name": "n"})
    with caplog.at_level(logging.INFO):
        refused = sm.send_mail(["a@x.com", "b@x.com"], "主旨", "內文")
    assert refused == {}
    msgs = [r.getMessage() for r in caplog.records]
    assert not any("部分收件人被拒" in m for m in msgs)
    assert any("已寄出" in m and "a@x.com" in m and "b@x.com" in m for m in msgs)


def test_send_once_returns_refused_dict():
    """★行為★ 部分收件人被拒時,那份清單必須回到呼叫端手上。

    ★[2026-08-08] 這個測試原本釘的是實作的【字串長相】★
    它斷言原始碼裡有 `return server.send_message(msg) or {}`。於是外審第 10 輪
    把 `_submit` 改成可觀測階段的低階流程(MAIL/RCPT/DATA 分開,才分得出
    「伺服器還沒收到內容」與「已收到但沒回應」)之後,這個測試轉紅 ——
    但它要守的那個不變量其實完全沒有被破壞。
    測試釘住的應該是性質,不是實作長什麼樣子。改成【真的跑一次】。
    """
    class _FakeServer:
        """只長出 `_send_once` 真的會用到的那幾個方法。"""

        def __init__(self):
            self.data_called = False

        def ehlo(self):
            pass

        def starttls(self, **kw):
            pass

        def login(self, *a):
            pass

        def ehlo_or_helo_if_needed(self):
            pass

        def mail(self, addr, opts):
            return (250, b"ok")

        def rcpt(self, addr, opts):
            # 452 = 信箱滿(暫時性)。smtplib 對「只有一部分被拒」是正常返回。
            return (452, b"mailbox full") if addr == "bad@x.com" else (250, b"ok")

        # [外審 2026-08-12 P1-04 之後] 生產自己拆 DATA 成三段呼叫
        def docmd(self, cmd, args=""):
            self.data_called = True          # 走到 DATA 了
            return (354, b"go ahead")

        def send(self, payload):
            pass

        def getreply(self):
            return (250, b"queued")

    class _CtxOf:
        """包成 `with smtplib.SMTP(...) as server` 的形狀。"""

        def __init__(self, server):
            self._s = server

        def __enter__(self):
            return self._s

        def __exit__(self, *e):
            return False

    msg = sm._build_message("me@x.com", "我", ["ok@x.com", "bad@x.com"],
                            "主旨", "內文")
    cred = {"host": "h", "use_tls": True, "username": "u", "password": "p"}
    for port, attr in ((465, "SMTP_SSL"), (587, "SMTP")):
        srv = _FakeServer()
        real = getattr(sm.smtplib, attr)
        # 用預設引數把這一圈的 srv 綁進去(late binding 會讓每一圈都指到最後一個)
        setattr(sm.smtplib, attr, lambda *a, _s=srv, **k: _CtxOf(_s))
        try:
            refused = sm._send_once(dict(cred, port=port), msg, 30.0)
        finally:
            setattr(sm.smtplib, attr, real)
        assert srv.data_called, f"port={port}: 沒有真的送出郵件內容"
        assert "bad@x.com" in refused, (
            f"port={port}: ★部分被拒的收件人沒有回到呼叫端手上★ "
            "那些人一封都沒收到，而 log 會寫「已寄出」")
        assert "ok@x.com" not in refused, (
            f"port={port}: 送達的人不該在拒收清單裡")


# ── smtp:帳密檔暫時讀不到 ≠ 沒設定 ──────────────────────────────────────────
def test_transient_credential_read_error_reuses_last_good(monkeypatch, caplog):
    """★所有告警一起消失★ 檔案還在、只是被防毒/備份鎖住。舊版把 default 當成合法內容
    → password 空 → is_configured() False → 呼叫端「自然靜默跳過」,一行 log 都沒有。"""
    monkeypatch.setattr(sm.CREDENTIALS_FILE.__class__, "exists", lambda self: True)
    sm._LAST_GOOD_CREDENTIALS.clear()
    sm._LAST_GOOD_CREDENTIALS.update({"username": "u@x.com", "password": "secret"})
    monkeypatch.setattr(sm, "safe_load_json_ex", lambda *a, **k: ({}, "error"))
    with caplog.at_level(logging.WARNING):
        cred = sm.load_credentials()
    assert cred["password"] == "secret", "要沿用上一次成功讀到的設定,寄信不中斷"
    assert any("暫時讀不到" in r.getMessage() for r in caplog.records)
    sm._LAST_GOOD_CREDENTIALS.clear()


def test_transient_read_error_without_cache_is_loud(monkeypatch, caplog):
    """開機後第一次就讀不到 → 只能停用,但【一定要留 error log】,不可靜默。"""
    monkeypatch.setattr(sm.CREDENTIALS_FILE.__class__, "exists", lambda self: True)
    sm._LAST_GOOD_CREDENTIALS.clear()
    monkeypatch.setattr(sm, "safe_load_json_ex", lambda *a, **k: ({}, "error"))
    with caplog.at_level(logging.ERROR):
        cred = sm.load_credentials()
    assert cred["password"] == ""
    assert any("所有通知信都不會寄出" in r.getMessage() for r in caplog.records)


# ── punch:表格不存在【就是】今天沒打卡(2026-07-16 已定案,不可再改)───────────
def test_missing_table_must_not_be_treated_as_portal_change():
    """★這是我 2026-07-26 犯過的錯,外審擋下★ 空的 ASP.NET GridView(當日尚無刷卡)
    本來就【完全不渲染 <table>】→ 表格不存在 = 今天還沒打卡,不是 portal 改版。
    當成改版會在「早上未打卡」——最該顯示未打卡的時刻——變成「查詢失敗」而隱藏最重要
    的訊號。commit d9f38be 已為此撤回過一次完整實作,main.py 也留有同樣說明。"""
    raw = inspect.getsource(ps.read_today_swipes)
    assert "找不到打卡紀錄表格" not in raw, "不可把表格不存在當成錯誤"
    assert "if (!tbl)" not in raw, "不可加表格存在性偵測"
    assert 'querySelectorAll("#Gv_attppre tbody tr")' in raw,         "維持直接掃列:表格不在 → 0 列 → 正常顯示未打卡"


def test_system_date_fallback_is_always_logged():
    """★外審★ `lb_systime` 存在卻是空字串/ISO 日期/改版成不含「年」的格式時【不會拋例外】,
    舊寫法 `except` 記不到 → 靜默沿用本機日期,時鐘一偏差就挑錯今日列而誤報未打卡。"""
    code = _code_only(inspect.getsource(ps.read_today_swipes))
    assert "_sys_date_parsed" in code, "要有明確的解析成功旗標"
    assert "if not _sys_date_parsed:" in code, "沒解析成功就要記錄(不只例外時)"
    i_flag = code.index("if not _sys_date_parsed:")
    assert "logging.warning" in code[i_flag:i_flag + 400], "降級一定要看得見"


def test_send_mail_declares_dict_return_type():
    """★外審★ 回傳型別註記要跟實際契約一致,否則嚴格呼叫端(型別檢查/IDE)會以為是 None。"""
    # 模組有 `from __future__ import annotations` → 註記是字串,不是型別物件。
    ann = inspect.signature(sm.send_mail).return_annotation
    assert ann in ("dict", dict), f"回傳註記應為 dict,實際 {ann!r}"


def test_classify_contract_unchanged():
    """三態判定是純函式,本次不可改動其語意。"""
    assert ps.classify(False, False) == ps.PUNCH_OFF
    assert ps.classify(True, True) == ps.PUNCH_OK
    assert ps.classify(True, False) == ps.PUNCH_FAIL


def test_cleared_config_is_not_resurrected_by_later_read_error(monkeypatch, caplog):
    """★外審 R2★ 使用者刻意把設定清空({})之後,下一次暫時讀取失敗不可把【舊帳密復活】,
    否則已經關掉的寄信會自己又打開。成功讀到就是現況,空 dict 也算。"""
    monkeypatch.setattr(sm.CREDENTIALS_FILE.__class__, "exists", lambda self: True)
    sm._LAST_GOOD_CREDENTIALS.clear()

    # 1) 先成功讀到一份有效帳密 → 進快取
    monkeypatch.setattr(sm, "safe_load_json_ex",
                        lambda *a, **k: ({"username": "u@x.com",
                                          "password": "secret"}, "ok"))
    assert sm.load_credentials()["password"] == "secret"

    # 2) 使用者把設定清空,成功讀到 {} → 快取必須跟著清掉
    monkeypatch.setattr(sm, "safe_load_json_ex", lambda *a, **k: ({}, "ok"))
    assert sm.load_credentials()["password"] == ""
    assert not sm._LAST_GOOD_CREDENTIALS, "成功讀到空設定就要清快取"

    # 3) 之後暫時讀不到 → 不可復活舊帳密
    monkeypatch.setattr(sm, "safe_load_json_ex", lambda *a, **k: ({}, "error"))
    with caplog.at_level(logging.ERROR):
        cred = sm.load_credentials()
    assert cred["password"] == "", "已清空的設定不可被讀取失敗復活"
    sm._LAST_GOOD_CREDENTIALS.clear()


def test_alert_wrapper_does_not_ignore_refused_recipients():
    """★外審 R3★ main 的告警包裝原本丟掉 send_mail 的回傳值、一律回 True →
    呼叫端把告警記成「已寄出」並永久去重 → 被拒的人這輩子都收不到,而且無跡可循。"""
    import main
    code = _code_only(inspect.getsource(main._send_alert_email_via_smtp))
    assert "refused = send_mail(" in code, "不可丟掉回傳值"
    assert "if refused:" in code and "logging.error(" in code, \
        "有人沒收到就要用 error 級別講清楚是誰"
    i_check = code.index("if refused:")
    # ★[#71] 回傳值從 `return True` 換成 `return SendResult(True)`
    i_return = code.index("return SendResult(True)", i_check)
    assert "logging.error(" in code[i_check:i_return], "log 要在回傳之前"
