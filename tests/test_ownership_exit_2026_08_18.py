# -*- coding: utf-8 -*-
"""[批次AE-7] 事件所有權必須有出口(外審 2026-08-18 全審第七輪 P1)。

AE-6 把 `PREPARED/SUBMITTING/UNKNOWN` 當成「這個事件已經有人負責」——
方向對,但★那個狀態的出口不完整★:回查的 `None`(查不出來)有一大堆
合法來源,其中【IMAP 根本沒設定】的機器每一次回查都回 None。舊版的
逾期結案又只處理「沒有 Message-ID」的紀錄 —— 於是:

    SMTP 逾時 → UNKNOWN(有合法 Message-ID)
      → 每 10 分鐘回查一次,每次都 None
      → 24 小時後也不結案(有 Message-ID 就被 skip)
      → claim_initial_delivery(K) 從此永遠回 ""
      → 醫師再寄一次 email 觸發也照樣不寄,而且觸發 journal 會被結案

= ★沉默的漏掉一次臨床查詢★。本檔把「查不出來也要有上限」釘住。
"""
import importlib
import os
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

dl = importlib.import_module("cmuh_common.delivery_ledger")
dr = importlib.import_module("cmuh_common.delivery_reconcile")

T0 = 2_000_000.0
KEY = "consult:exitK|aud1"
MSGID = "<real-message-id@example>"


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
    monkeypatch.setattr(dr, "_now", c, raising=False)
    monkeypatch.setattr(dr, "EVERY_SEC", 0.0)
    return c


@pytest.fixture()
def led(tmp_path):
    obj = dl.DeliveryLedger(path=str(tmp_path / "delivery.sqlite3"))
    yield obj
    obj._close_quietly()


def _never_verifiable(_msgid):
    """生產裡最常見的那一種 finder:★IMAP 沒設定 → 永遠回 None★。"""
    return None


def _claim(led, *, key=KEY, recipients=("a@x.tw",)):
    return led.claim_initial_delivery(
        business_key=key, category="consult", recipients=list(recipients),
        subject="會診", message_id=MSGID, body_text="內文")


def _reconcile(led, clock):
    """跑一輪回查(finder 永遠查不出來)。"""
    rec = dr.Reconciler(lambda: led, tag="test")
    return rec.run_once(now=clock(), finder=_never_verifiable)


class TestUnverifiableOwnershipHasAnExit:

    def test_a_timed_out_send_blocks_while_it_is_still_young(self, clock, led):
        """前 24 小時內★要擋★:那一封可能真的送到了,再寄一次就是重複。"""
        did = _claim(led)
        led.settle(did, unknown=True)
        assert led.state_of(did) == dl.UNKNOWN
        clock.advance(600)                      # 10 分鐘後回查
        _reconcile(led, clock)
        assert led.state_of(did) == dl.UNKNOWN, "查不出來 → 維持原狀"
        assert _claim(led) == "", "結果未定的期間本來就該擋"

    def test_it_is_released_once_it_is_hopeless(self, clock, led, caplog):
        """★核心★ 掛滿 24 小時仍查不出來 → 明確結案 + 大聲講 + 解除封鎖。"""
        did = _claim(led)
        led.settle(did, unknown=True)
        clock.advance(600)
        _reconcile(led, clock)
        assert _claim(led) == ""                # 前置:此刻仍被擋
        clock.advance(dr.UNVERIFIABLE_GIVE_UP_SEC)
        with caplog.at_level("ERROR"):
            _reconcile(led, clock)
        assert led.state_of(did) == dl.FAILED, \
            "★有 Message-ID 但永遠查不出來的紀錄沒有出口★"
        assert any("Message-ID" in r.getMessage() and r.levelname == "ERROR"
                   for r in caplog.records), "解除封鎖一定要大聲講"
        assert _claim(led), \
            "★這個事件從此再也寄不出去★ 醫師重寄 email 觸發也一樣被擋"

    def test_the_original_mail_is_still_owed_after_release(self, clock, led):
        """解除封鎖不等於放生:原信仍在 durable 補寄鏈上,收件人會收到。"""
        did = _claim(led)
        led.settle(did, unknown=True)
        clock.advance(dr.UNVERIFIABLE_GIVE_UP_SEC + 600)
        _reconcile(led, clock)
        clock.advance(60)
        owed = [r["delivery_id"] for r in
                led.resends_owed(min_age_sec=0.0)]
        assert did in owed, "★結案後沒有人再看它一眼★ 那位收件人就真的漏掉了"

    def test_a_stuck_submitting_is_released_too(self, clock, led):
        """送到一半被砍(SUBMITTING)也是同一條路:回查不出來就要有上限。"""
        did = _claim(led)
        assert led.state_of(did) == dl.SUBMITTING
        clock.advance(dr.STUCK_SUBMITTING_AFTER_SEC + 60)
        _reconcile(led, clock)
        assert led.state_of(did) == dl.SUBMITTING, "還年輕 → 先擋著"
        assert _claim(led) == ""
        clock.advance(dr.UNVERIFIABLE_GIVE_UP_SEC)
        _reconcile(led, clock)
        assert led.state_of(did) == dl.FAILED, \
            "★卡住的 SUBMITTING 一樣會永久擋住 business_key★"
        assert _claim(led)

    def test_a_verifiable_send_is_never_given_up_by_age(self, clock, led,
                                                        caplog):
        """反方向:查得出來的紀錄要走【查證】那條路,不可以被年齡結案掉。

        ★狀態複查是這一條的重點★:同一輪裡回查先跑、釋放後跑,已經有結論
        的那一筆必須被跳過 —— 少了複查,雖然 `resolve_unknown` 只動
        `R_UNKNOWN` 而救回了收件人狀態,卻仍會對一筆【剛剛才確認送達】的
        紀錄大聲喊「解除事件封鎖、對方可能收到第二封、請檢查 IMAP」。
        那是假警報:它會叫人去修一個沒有壞的東西,並讓人以為信重複了。
        """
        did = _claim(led)
        led.settle(did, unknown=True)
        clock.advance(dr.UNVERIFIABLE_GIVE_UP_SEC + 600)
        rec = dr.Reconciler(lambda: led, tag="test")
        with caplog.at_level("ERROR"):
            rec.run_once(now=clock(), finder=lambda _m: True)   # 備份查到了
        assert led.state_of(did) == dl.CONFIRMED, \
            "★查得到卻被逾期結案成未送達★ 會對已收到的人再寄一封"
        assert not [r for r in caplog.records if r.levelname == "ERROR"
                    and "解除事件封鎖" in r.getMessage()], \
            "★對已確認送達的紀錄發出解除封鎖告警★ 那是假警報"

    def test_release_never_overwrites_a_record_that_already_concluded(
            self, clock, led, caplog):
        """★釋放本身的守衛★:就算呼叫端把一筆【已經有結論】的紀錄交進來,
        也不可以覆寫它。(單元層直接測那個守衛 —— 上面那條測的是呼叫端只
        交出沒有結論的那幾筆,兩層各守各的。)"""
        did = _claim(led)
        led.settle(did, refused={})              # 已確認送達
        assert led.state_of(did) == dl.CONFIRMED
        clock.advance(dr.UNVERIFIABLE_GIVE_UP_SEC + 600)
        rec = dr.Reconciler(lambda: led, tag="test")
        with caplog.at_level("ERROR"):
            rec._release_unverifiable(led, [led.get(did)], clock())
        assert led.state_of(did) == dl.CONFIRMED, \
            "★已有結論的紀錄被逾期釋放覆寫成未送達★"
        assert not [r for r in caplog.records if r.levelname == "ERROR"
                    and "解除事件封鎖" in r.getMessage()], "也不可以發假警報"

    def test_a_confirmed_lookup_that_failed_to_land_is_not_released(
            self, clock, led, caplog):
        """★審查 AE-7 第 1 輪 P2★ 查到了(Sent 有這封)但★寫不進帳本★
        (SQLite 忙碌 → LedgerUnavailable)—— 那一筆已經有答案,只是這一刻
        落不了地。若把它也當成「查不出來」,逾期釋放的第二次寫入剛好成功
        就會把【已確認送達】改寫成未送達 → 補寄鏈對已經收到的人再寄一封。
        """
        did = _claim(led)
        led.settle(did, unknown=True)
        clock.advance(dr.UNVERIFIABLE_GIVE_UP_SEC + 600)
        real = led.resolve_unknown
        state = {"n": 0}

        def _flaky(delivery_id, *, delivered, note=""):
            state["n"] += 1
            if state["n"] == 1:               # 收斂那一次:資料庫剛好忙碌
                raise dl.LedgerUnavailable("busy")
            return real(delivery_id, delivered=delivered, note=note)

        led.resolve_unknown = _flaky          # type: ignore[method-assign]
        rec = dr.Reconciler(lambda: led, tag="test")
        with caplog.at_level("ERROR"):
            rec.run_once(now=clock(), finder=lambda _m: True)
        led.resolve_unknown = real            # type: ignore[method-assign]
        assert state["n"] == 1, \
            "★查證已有結論的紀錄不可以再被逾期釋放寫一次★"
        assert led.state_of(did) == dl.UNKNOWN, \
            "落不了地就維持原狀,下一輪重寫 —— 不可以翻成【未送達】"
        assert not [r for r in caplog.records if r.levelname == "ERROR"
                    and "解除事件封鎖" in r.getMessage()]
        rec.run_once(now=clock() + dr.EVERY_SEC + 1, finder=lambda _m: True)
        assert led.state_of(did) == dl.CONFIRMED, "下一輪要能正常收斂"

    def test_the_two_causes_are_told_apart(self, clock, led, caplog):
        """沒有 Message-ID 與「有 ID 但查不出來」處置一樣,但要分得出原因
        —— 前者要補 make_msgid,後者要修 IMAP 設定。"""
        no_id = led.begin(business_key="consult:other|aud", category="consult",
                          recipients=["a@x.tw"], subject="會診", message_id="")
        led.settle(no_id, unknown=True)
        with_id = _claim(led)
        led.settle(with_id, unknown=True)
        clock.advance(dr.UNVERIFIABLE_GIVE_UP_SEC + 600)
        with caplog.at_level("ERROR"):
            _reconcile(led, clock)
        msgs = [r.getMessage() for r in caplog.records
                if r.levelname == "ERROR"]
        assert any("沒有 Message-ID" in m for m in msgs)
        assert any("IMAP" in m for m in msgs), \
            "★兩種原因混成同一句★ 看訊息的人不知道該去修哪裡"
