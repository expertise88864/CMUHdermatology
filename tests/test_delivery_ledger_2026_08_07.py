# -*- coding: utf-8 -*-
"""寄送帳本（跨 poll／跨重啟的送達狀態）— 2026-08-07 外審 AT/AW。

【它要解決什麼】此前「信寄出去了沒」只有布林兩態，於是 `DeliveryOutcomeUnknown`
（DATA 已提交、等最終 250 逾時 —— 很可能已送達）只能二選一：
  當失敗 → 下一輪重寄 → 醫師收到重複的臨床通知；
  當成功 → 萬一真的沒送到，那一則永遠不補。
而且重啟後記憶體去重全消失。本模組把 UNKNOWN 誠實記成第三態，之後用
Message-ID 回查收斂；並且每位收件人各自有狀態（部分拒收不再永久漏收）。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import delivery_ledger as dl  # noqa: E402


def _led(tmp_path, **kw):
    return dl.DeliveryLedger(str(tmp_path / "ledger.json"), **kw)


# ── 狀態機（純函式）────────────────────────────────────────────────────────
def test_summarize_all_delivered():
    assert dl.summarize({"a@x": dl.R_CONFIRMED, "b@x": dl.R_CONFIRMED}) \
        == dl.CONFIRMED


def test_summarize_partial():
    assert dl.summarize({"a@x": dl.R_CONFIRMED, "b@x": dl.R_TRANSIENT}) \
        == dl.PARTIAL


def test_summarize_all_refused_is_failed():
    assert dl.summarize({"a@x": dl.R_TRANSIENT, "b@x": dl.R_PERMANENT}) \
        == dl.FAILED


def test_any_unknown_dominates():
    """★保守★ 只要有一位還不知道，整筆就不可以宣稱成功或失敗。"""
    assert dl.summarize({"a@x": dl.R_CONFIRMED, "b@x": dl.R_UNKNOWN}) \
        == dl.UNKNOWN
    assert dl.summarize({"a@x": dl.R_PERMANENT, "b@x": dl.R_UNKNOWN}) \
        == dl.UNKNOWN


def test_no_recipients_is_failed():
    assert dl.summarize({}) == dl.FAILED


@pytest.mark.parametrize("code,expect", [
    (250, dl.R_TRANSIENT), (421, dl.R_TRANSIENT), (450, dl.R_TRANSIENT),
    (550, dl.R_PERMANENT), (553, dl.R_PERMANENT), (599, dl.R_PERMANENT),
    (None, dl.R_TRANSIENT), ("bad", dl.R_TRANSIENT),
])
def test_classify_refusal(code, expect):
    """★看不懂的碼當暫時性★ 重寄一位的代價 << 永久丟掉一則臨床通知。"""
    assert dl.classify_refusal(code) == expect


def test_only_transient_gets_retried():
    states = {"ok@x": dl.R_CONFIRMED, "soft@x": dl.R_TRANSIENT,
              "hard@x": dl.R_PERMANENT, "huh@x": dl.R_UNKNOWN}
    assert dl.recipients_needing_retry(states) == ["soft@x"], \
        "已送達不可重寄(重複轟炸)、永久拒收重寄無用、UNKNOWN 要先驗證"
    assert dl.permanently_refused(states) == ["hard@x"]


# ── 生命週期 ───────────────────────────────────────────────────────────────
def test_begin_then_confirm(tmp_path):
    led = _led(tmp_path)
    did = led.begin(business_key="bk1", category="clinical",
                    recipients=["A@X.tw", " b@x.tw "])
    # ★[2026-08-08 外審第 10 輪第 3 回] 登記當下就是 SUBMITTING★
    #   落地的 PREPARED 之所以被拿掉:`mark_submitting` 的寫回是 fail-open 的,
    #   它只改到記憶體、信卻寄出去了,磁碟上留下的一樣是 PREPARED —— 於是
    #   「停在 PREPARED = 確定沒寄出」這個推論不成立,卻會被拿去判死。
    assert led.get(did)["state"] == dl.SUBMITTING
    assert set(led.get(did)["recipients"]) == {"a@x.tw", "b@x.tw"}, "收件人要正規化"
    led.mark_submitting(did)
    assert led.get(did)["attempts"] == 1
    assert led.settle(did, refused={}) == dl.CONFIRMED


def test_partial_refusal_records_each_recipient(tmp_path):
    led = _led(tmp_path)
    did = led.begin(business_key="bk", category="clinical",
                    recipients=["ok@x.tw", "soft@x.tw", "hard@x.tw"])
    state = led.settle(did, refused={"soft@x.tw": (450, b"try later"),
                                     "hard@x.tw": (550, b"no such user")})
    assert state == dl.PARTIAL
    rec = led.get(did)["recipients"]
    assert rec["ok@x.tw"] == dl.R_CONFIRMED
    assert rec["soft@x.tw"] == dl.R_TRANSIENT
    assert rec["hard@x.tw"] == dl.R_PERMANENT
    assert led.needs_recipient_retry() == [(did, ["soft@x.tw"])], \
        "★只補寄暫時性被拒的那位★ 舊做法整輪一個布林 → 被拒者永遠漏收"


def test_unknown_stays_unknown_until_verified(tmp_path):
    led = _led(tmp_path)
    did = led.begin(business_key="bk", category="clinical",
                    recipients=["a@x.tw"])
    assert led.settle(did, unknown=True) == dl.UNKNOWN
    assert [r["delivery_id"] for r in led.unresolved()] == [did]
    # 這筆還沒被否證 → 不可以再寄一次
    assert led.has_live_delivery("bk") is True


def test_resolve_unknown_delivered(tmp_path):
    led = _led(tmp_path)
    did = led.begin(business_key="bk", category="clinical",
                    recipients=["a@x.tw"])
    led.settle(did, unknown=True)
    assert led.resolve_unknown(did, delivered=True) == dl.CONFIRMED
    assert led.unresolved() == []
    assert led.has_live_delivery("bk") is True      # 確定送達 → 仍然不必重寄


def test_resolve_unknown_not_found_allows_resend(tmp_path):
    """★關鍵★ 回查【確定查無】才可以重寄 —— 這是「漏寄」的唯一補救路徑。"""
    led = _led(tmp_path)
    did = led.begin(business_key="bk", category="clinical",
                    recipients=["a@x.tw"])
    led.settle(did, unknown=True)
    assert led.resolve_unknown(did, delivered=False) == dl.FAILED
    assert led.has_live_delivery("bk") is False, "已否證 → 可以重寄了"
    assert led.needs_recipient_retry() == [(did, ["a@x.tw"])]


def test_failed_delivery_does_not_block_resend(tmp_path):
    led = _led(tmp_path)
    did = led.begin(business_key="bk", category="clinical",
                    recipients=["a@x.tw"])
    led.settle(did, failed=True, note="連線階段就失敗")
    assert led.get(did)["state"] == dl.FAILED
    assert led.has_live_delivery("bk") is False


def test_confirmed_blocks_resend(tmp_path):
    led = _led(tmp_path)
    did = led.begin(business_key="bk", category="clinical",
                    recipients=["a@x.tw"])
    led.settle(did, refused={})
    assert led.has_live_delivery("bk") is True
    assert led.has_live_delivery("another") is False


# ── 跨重啟 ─────────────────────────────────────────────────────────────────
def test_survives_restart(tmp_path):
    """★核心價值★ 重啟後仍知道「那一則結果不明、還沒查清楚」。"""
    path = str(tmp_path / "ledger.json")
    led = dl.DeliveryLedger(path)
    did = led.begin(business_key="bk", category="clinical",
                    recipients=["a@x.tw"])
    led.settle(did, unknown=True)

    reborn = dl.DeliveryLedger(path)          # 模擬程式重啟
    assert reborn.has_live_delivery("bk") is True, \
        "★重啟後忘記 UNKNOWN★ 同一批會診會被再判為 new 而重寄"
    assert [r["delivery_id"] for r in reborn.unresolved()] == [did]


def test_submitting_left_over_after_a_crash_is_surfaced(tmp_path):
    """送到一半被砍 → 重啟後看到 SUBMITTING，必須當成待查而非沒發生。"""
    path = str(tmp_path / "ledger.json")
    led = dl.DeliveryLedger(path)
    did = led.begin(business_key="bk", category="clinical",
                    recipients=["a@x.tw"])
    led.mark_submitting(did)

    reborn = dl.DeliveryLedger(path)
    assert reborn.has_live_delivery("bk") is True, "半途而廢不可以當成沒寄過"
    assert [r["delivery_id"] for r in reborn.stuck_submitting(older_than_sec=-1)] \
        == [did]


def test_unreadable_ledger_never_overwrites_disk(tmp_path):
    """★與 alert_email_sent 同樣的教訓★ 讀不到 ≠ 沒有紀錄。

    [SQLite 版] 資料庫檔壞掉時:所有讀寫一律 `LedgerUnavailable`
    (不把「讀不到」講成「沒有」),而且★絕不覆寫那個檔★ ——
    直接當空的 → 所有 business_key 看起來都沒寄過 → 整批重寄。
    """
    path = str(tmp_path / "delivery_ledger.sqlite3")
    with open(path, "wb") as f:
        f.write(b"this is not a database " * 40)
    before = open(path, "rb").read()

    hurt = dl.DeliveryLedger(path)
    with pytest.raises(dl.LedgerUnavailable):
        hurt.begin(business_key="bk2", category="clinical",
                   recipients=["b@x.tw"])
    with pytest.raises(dl.LedgerUnavailable):
        hurt.has_live_delivery("bk2")
    assert hurt.state_of("whatever") == "", "state_of 讀不到=空字串(不知道)"
    assert open(path, "rb").read() == before, \
        "★讀不到卻把檔案覆寫掉★ 磁碟上的已寄紀錄被抹掉 → 大量重寄"


# ── 修剪 ───────────────────────────────────────────────────────────────────
def test_prune_keeps_unresolved_forever(tmp_path):
    """★UNKNOWN 不可以因為過期就被當成沒發生過★（那等於偷偷允許重寄）。"""
    led = _led(tmp_path, retain_days=0)
    old_unknown = led.begin(business_key="u", category="clinical",
                            recipients=["a@x.tw"])
    led.settle(old_unknown, unknown=True)
    old_done = led.begin(business_key="d", category="clinical",
                         recipients=["b@x.tw"])
    led.settle(old_done, refused={})
    # 把兩筆都推到「很久以前」，再觸發一次 prune
    # [SQLite 版] 沒有記憶體快照可改 —— 直接改資料庫(模擬時間流逝)
    import sqlite3
    with sqlite3.connect(led.path) as _c:
        _c.execute("UPDATE deliveries SET updated_at=0.0")
    led.begin(business_key="trigger-prune", category="clinical",
              recipients=["c@x.tw"])

    assert led.get(old_unknown), "★UNKNOWN 被剪掉了★ 之後會重寄"
    assert not led.get(old_done), "已收斂的舊紀錄應該被剪掉(避免無限膨脹)"


# ── 接線：會診寄送路徑 ─────────────────────────────────────────────────────
def test_consult_send_no_longer_discards_refused():
    """★AW 核心★ send_via_smtp 不可以再丟掉 send_mail 的被拒收件人回傳值。

    smtplib 只有【全部】被拒才拋例外；部分被拒是正常返回。舊版整句丟掉 →
    四人裡有一位收不到，這一輪仍算完全成功、基準照樣更新 → 那位永遠不補寄。
    """
    import inspect
    import consult_query as cq
    src = inspect.getsource(cq.send_via_smtp)
    assert "refused = send_mail(" in src, "★又把 send_mail 的回傳值丟掉了★"
    assert "return refused" in src


def test_consult_records_every_send_in_the_ledger():
    """寄送前 begin、寄送後 settle —— 送出當下斷電也留得下待查紀錄。"""
    import inspect
    import consult_query as cq
    src = inspect.getsource(cq._do_full_job)
    i_begin = src.index("_delivery_begin(")
    i_send = src.index("send_via_smtp(")
    i_settle = src.index("_delivery_settle(")
    assert i_begin < i_send < i_settle, "順序必須是 begin → 送出 → settle"


def test_unknown_is_recorded_for_later_verification():
    import inspect
    import consult_query as cq
    src = inspect.getsource(cq._do_full_job)
    i = src.index("isinstance(last_err, DeliveryOutcomeUnknown)")
    assert "_delivery_settle(_did, unknown=True)" in src[i:i + 600], \
        "★結果不明沒進帳本★ 重啟後就再也不知道有一筆待查"


def test_ledger_failures_never_block_sending():
    """★fail-open★ 帳本是觀測用的，壞掉不可以害信寄不出去。"""
    import inspect
    import consult_query as cq
    for fn in (cq._get_ledger, cq._delivery_begin, cq._delivery_settle):
        assert "except Exception" in inspect.getsource(fn), \
            f"{fn.__name__} 必須吞掉自己的錯誤（帳本不可阻擋寄信）"


# ── 接線：止掛提醒路徑 ─────────────────────────────────────────────────────
def test_alert_send_records_into_the_ledger():
    """止掛提醒也要 begin→settle（與會診共用同一本帳、同一套 contract）。"""
    import inspect
    import main
    src = inspect.getsource(main._send_alert_email_via_smtp)
    i_begin = src.index("_led.begin(")
    i_send = src.index("send_mail(")
    assert i_begin < i_send, "begin 必須在真正送出之前"
    assert "_settle(refused=refused)" in src, "成功路徑要寫回逐位收件人結果"


def test_alert_unknown_and_failure_are_distinguished_in_the_ledger():
    """★核心★ 「結果不明」與「確定失敗」在帳本裡必須是不同狀態。

    兩者都回 False/True 給呼叫端是舊的二元世界；帳本要留下可回查的第三態，
    否則永遠沒辦法用 Message-ID 把它收斂。
    """
    import inspect
    import main
    src = inspect.getsource(main._send_alert_email_via_smtp)
    i_unknown = src.index("except DeliveryOutcomeUnknown")
    assert "_settle(unknown=True)" in src[i_unknown:i_unknown + 300], \
        "★結果不明沒記成 UNKNOWN★ 之後無從回查"
    i_generic = src.index("except Exception as e:")
    assert "_settle(failed=True)" in src[i_generic:i_generic + 200], \
        "確定失敗要記成 FAILED（才允許重寄）"


def test_alert_ledger_is_fail_open():
    """帳本壞掉不可以害止掛提醒寄不出去。"""
    import inspect
    import main
    assert "except Exception" in inspect.getsource(main._get_alert_ledger)
    src = inspect.getsource(main._send_alert_email_via_smtp)
    assert "if _led is not None" in src, "帳本不可用時要能繼續寄信"


def test_both_senders_share_one_ledger_file():
    """會診與止掛必須寫同一本帳（否則 business_key 各自為政、無從彙總）。"""
    import consult_query as cq
    import main
    from cmuh_common.delivery_ledger import DeliveryLedger
    import inspect
    assert "DeliveryLedger()" in inspect.getsource(cq._get_ledger)
    assert "DeliveryLedger()" in inspect.getsource(main._get_alert_ledger)
    # 預設路徑一致（都落在 settings/delivery_ledger.json）
    assert DeliveryLedger.__init__.__defaults__[0] is None
