# -*- coding: utf-8 -*-
"""[批次AE-9] 帳本停機期間寄出的那一封,恢復後要補登記(全審第七輪 P2-02)。

帳本不可用時依既有定案(availability-first,2026-08-05)照樣寄出 —— 那一封
沒有帳。若其中幾位被 4xx 暫時拒收,退避佇列會在【帳本恢復之後】補寄,
但它手上沒有 `origin_did`:舊路徑於是直接 `begin()` 一筆孤兒紀錄,
★繞過 `claim_resend_child` 的仲裁,也繞過事件所有權★ —— 另一個 process
同時 `claim_initial_delivery()` 就把整封再寄一次給同一批人。

修法:恢復後把「已經發生的事」補登記(已知的事實才寫),之後兩邊走同一套
仲裁。本檔釘住補登記的語意與它真的關掉了那個洞。
"""
import importlib
import inspect
import os
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

dl = importlib.import_module("cmuh_common.delivery_ledger")

KEY = "consult:outage|aud"
OCC = ("sched:2026-08-18|17:00",)


@pytest.fixture()
def two_generations(tmp_path):
    db = str(tmp_path / "delivery.sqlite3")
    a = dl.DeliveryLedger(path=db)
    b = dl.DeliveryLedger(path=db)
    yield a, b
    a._close_quietly()
    b._close_quietly()


def _adopt(led, *, states=None, occ=OCC):
    return led.adopt_initial_delivery(
        business_key=KEY, category="consult",
        recipient_states=({"a@x.tw": dl.R_CONFIRMED,
                           "b@x.tw": dl.R_TRANSIENT}
                          if states is None else states),
        subject="會診", message_id="<sent@x>", body_text="內文",
        occurrence_keys=occ)


class TestAdoptionClosesTheOwnershipHole:

    def test_the_adopted_record_owns_the_occasion(self, two_generations):
        """★核心★ 補登記之後,別的 process 就不能把整封再寄一次。"""
        a, b = two_generations
        did = _adopt(a)
        assert did
        assert b.claim_initial_delivery(
            business_key=KEY, category="consult",
            recipients=["a@x.tw", "b@x.tw"], subject="會診",
            occurrence_keys=OCC) == "", \
            "★停機期間寄出的那一封沒有帳,事件就沒有主人★ 會被再寄一次"

    def test_it_writes_only_what_is_known(self, two_generations):
        """已知的事實:被拒的照 SMTP 碼分類,沒被拒的就是送達了。"""
        a, _b = two_generations
        did = _adopt(a)
        rec = a.get(did) or {}
        assert rec["recipients"] == {"a@x.tw": dl.R_CONFIRMED,
                                     "b@x.tw": dl.R_TRANSIENT}
        assert rec["state"] == dl.PARTIAL, "整筆狀態由 summarize 推,不是硬寫"
        assert rec["body_text"] == "內文", "補寄要有 payload"
        assert "補登記" in str(rec.get("note") or ""), "帳上要看得出它是補的"

    def test_two_processes_do_not_create_parallel_parents(self,
                                                          two_generations):
        """兩支程式同時補登記 → 第二支拿到【第一支那一筆】的 id,
        不可以各開一筆平行的親紀錄(補寄仲裁就分裂了)。"""
        a, b = two_generations
        first = _adopt(a)
        again = _adopt(b)
        assert again == first, "★各開一筆平行親紀錄★ 兩條鏈會同時補寄"

    def test_the_adopted_parent_takes_resend_children(self, two_generations):
        """補登記之後,補寄走既有仲裁(claim_resend_child),不再是孤兒。"""
        a, _b = two_generations
        did = _adopt(a)
        child = a.claim_resend_child(did, business_key=KEY + "|retry1",
                                     category="consult",
                                     recipients=["b@x.tw"], subject="會診")
        assert child, "★補不進去★ 那就還是孤兒紀錄"
        assert (a.get(child) or {}).get("parent_id") == did

    def test_a_definitively_failed_history_is_not_the_owner(
            self, two_generations):
        """★審查 AE-9 第 1 輪 P1-1★ 同一次機會之前【確定沒送出】(FAILED)過
        —— 那一筆不是擁有者(判準與 claim 一致)。補登記若沿用它:
        停機期間真的寄出去的那一封沒有帳、機會仍算沒人負責(別的 process
        可以把整封再寄一次),而且已送達的人在舊紀錄上還掛著暫時被拒,
        之後會收到不必要的 durable 補寄。"""
        a, b = two_generations
        old = a.claim_initial_delivery(
            business_key=KEY, category="consult",
            recipients=["a@x.tw", "b@x.tw"], subject="會診",
            occurrence_keys=OCC)
        a.settle(old, failed=True)               # 確定沒送出
        did = _adopt(a)
        assert did and did != old, \
            "★把【確定沒送出】的舊紀錄當成擁有者★ 這一封就沒有帳"
        assert b.claim_initial_delivery(
            business_key=KEY, category="consult",
            recipients=["a@x.tw", "b@x.tw"], subject="會診",
            occurrence_keys=OCC) == "", "補登記之後這次機會要有主人"
        assert (b.get(did) or {})["recipients"]["a@x.tw"] == dl.R_CONFIRMED

    def test_a_partially_overlapping_batch_owns_the_fresh_keys(
            self, two_generations):
        """★審查 AE-9 第 1 輪 P1-2★ 併批 (u1 已被服務, u2 全新):
        沿用 u1 的親紀錄會讓 u2 沒有人佔住(可被整封重播),而且補寄會掛到
        一筆【沒有這幾位收件人事實】的紀錄上 → 開不出子紀錄,那位收件人
        最後走放棄路徑。"""
        a, b = two_generations
        u1, u2 = "trigger:ident|9|41", "trigger:ident|9|77"
        served = a.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["dr1@x.tw"],
            subject="會診", occurrence_keys=(u1,))
        a.settle(served, refused={})             # u1 這次機會已完成
        did = _adopt(a, states={"dr1@x.tw": dl.R_CONFIRMED,
                                "dr9@x.tw": dl.R_TRANSIENT},
                     occ=(u1, u2))
        assert did and did != served, "★沿用 u1 的紀錄★ u2 就沒有人佔住"
        # u2 現在有主人:別的 process 不能拿它重播整封
        assert b.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["dr9@x.tw"],
            subject="會診", occurrence_keys=(u2,)) == ""
        # u1 仍屬於原本那一筆(不可以被搶走)
        assert b._occurrence_owners_locked(
            b._ensure_conn_locked(), (u1,))[u1] == served
        # 補寄掛在【有事實】的那一筆上,開得出子紀錄
        child = a.claim_resend_child(did, business_key=KEY + "|retry1",
                                     category="consult",
                                     recipients=["dr9@x.tw"], subject="會診")
        assert child, "★補寄掛到沒有這位收件人的紀錄上★ 開不出子紀錄"

    def test_a_fully_covered_batch_reuses_the_owner(self, two_generations):
        """反方向:整批都已經被【同一筆】服務 → 用他,不要開平行親紀錄。"""
        a, _b = two_generations
        first = _adopt(a)
        again = _adopt(a)
        assert again == first

    def test_an_empty_state_map_is_refused(self, two_generations):
        """沒有收件人 = 沒有「已經發生的事」可寫 → 不要在帳上留空紀錄。"""
        a, _b = two_generations
        assert _adopt(a, states={}) == ""


class TestTheQueueAdoptsBeforeRetrying:

    def test_the_retry_path_adopts_when_it_has_no_origin(self):
        import consult_query as cq
        src = inspect.getsource(cq._resend_transient_refusals)
        i = src.index("_adopt_unledgered_send(")
        j = src.index("claim_resend_child(")
        assert i < j, "★補登記要在 claim 之前★ 否則這一輪仍是孤兒補寄"

    def test_it_maps_accepted_and_refused_correctly(self, monkeypatch,
                                                    tmp_path):
        import consult_query as cq
        led = dl.DeliveryLedger(path=str(tmp_path / "d.sqlite3"))
        monkeypatch.setattr(cq, "_get_ledger", lambda: led)
        art = cq._DeliveryArtifact(
            recipients=("a@x.tw", "b@x.tw"), subject="會診", text_body="內文",
            html_body="", attachment=None, message_id="<m@x>",
            business_key=KEY, occurrence_keys=OCC)
        did = cq._adopt_unledgered_send(art, {"b@x.tw": (421, b"later")})
        assert did
        assert (led.get(did) or {})["recipients"] == {
            "a@x.tw": dl.R_CONFIRMED, "b@x.tw": dl.R_TRANSIENT}
        led._close_quietly()

    def test_a_still_broken_ledger_falls_back_to_the_old_path(self,
                                                              monkeypatch):
        """帳本這一刻仍不可用 → 回 ""(呼叫端走舊的直接登記);
        ★不寄反而是漏寄★,方向要選對。"""
        import consult_query as cq

        class _Broken:
            def adopt_initial_delivery(self, **_kw):
                raise RuntimeError("還是開不起來")

        art = cq._DeliveryArtifact(
            recipients=("a@x.tw",), subject="會診", text_body="內文",
            html_body="", attachment=None, message_id="<m@x>",
            business_key=KEY, occurrence_keys=OCC)
        monkeypatch.setattr(cq, "_get_ledger", lambda: _Broken())
        assert cq._adopt_unledgered_send(art, {}) == ""
        monkeypatch.setattr(cq, "_get_ledger", lambda: None)
        assert cq._adopt_unledgered_send(art, {}) == ""
