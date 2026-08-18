# -*- coding: utf-8 -*-
"""[批次AE-10] 機會所有權要跨【schema 世代】與【交棒】保持連續(第八輪 2 P1)。

AE-8 把所有權移到 `delivery_occurrences`,但那張表是新的:

* ★v4 的紀錄不可能有 mapping★ —— 所有權查詢是 JOIN,查不到就等於「沒人
  負責」。於是【v4→v5 這次更新本身】就是觸發條件:舊 generation 的 SMTP
  還在飛,新 generation 一 claim 就過 → 同一則臨床通知兩封同時跨 SMTP。
* ★`supersede()` 只轉補寄義務,沒有轉機會鑰匙★ —— 接手者還在飛的期間,
  舊那一筆(FAILED、沒有 in-flight 子紀錄)持有的鑰匙被判成「沒人服務」,
  重播就開出第三筆,與接手者同時寄給同一位醫師。

兩者都是「所有權在某個轉換點斷掉」。本檔把兩個轉換點釘住。
"""
import importlib
import os
import sqlite3
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

dl = importlib.import_module("cmuh_common.delivery_ledger")

KEY = "consult:continuity|aud"
O1 = "trigger:ident|9|41"
O2 = "sched:2026-08-18|20:00"


@pytest.fixture()
def led(tmp_path):
    obj = dl.DeliveryLedger(path=str(tmp_path / "delivery.sqlite3"))
    yield obj
    obj._close_quietly()


def _unmap(led, did):
    """把某一筆的機會鑰匙拿掉 —— ★模擬 v4 舊紀錄★(它們沒有 mapping)。"""
    with sqlite3.connect(led.path) as c:
        c.execute("DELETE FROM delivery_occurrences WHERE delivery_id=?",
                  (did,))


class TestSchemaMigrationDoesNotLoseOwnership:

    def test_an_unmapped_legacy_send_still_fences(self, led):
        """★第八輪 P1-01★ 舊 generation 的 SUBMITTING(v4,沒有鑰匙)
        仍在飛 → 新 generation 這一輪不可以寄。"""
        old = led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", occurrence_keys=(O1,))
        _unmap(led, old)                       # 這一筆變成「v4 舊紀錄」
        assert led.state_of(old) == dl.SUBMITTING
        assert led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", occurrence_keys=(O2,)) == "", \
            "★v4→v5 混版期間同一則通知寄了兩封★"

    def test_an_unmapped_legacy_failed_send_with_a_live_child_fences(self,
                                                                     led):
        """舊紀錄確定沒送出、但它的補寄還在飛 —— 一樣要擋。"""
        old = led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", body_text="內文", occurrence_keys=(O1,))
        led.settle(old, failed=True)
        child = led.claim_resend_child(old, business_key=KEY,
                                       category="consult",
                                       recipients=["a@x.tw"], subject="會診")
        assert child
        _unmap(led, old)
        assert led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", occurrence_keys=(O2,)) == "", \
            "★舊紀錄的補寄正在飛,新 generation 又寄了一封整份的★"

    def test_a_settled_legacy_send_does_not_fence_forever(self, led):
        """★出口★ 舊紀錄收斂(確定沒送出、也沒有還在飛的補寄)之後就不再擋
        —— 圍籬只在遷移期間有作用,不可以變成永久封鎖。"""
        old = led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", occurrence_keys=(O1,))
        _unmap(led, old)
        led.settle(old, failed=True)
        assert led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", occurrence_keys=(O2,)), \
            "★遷移圍籬變成永久封鎖★ 那個 business_key 再也寄不出去"

    def test_a_different_business_key_is_untouched(self, led):
        """圍籬只看同一把內容識別 —— 別的事件不受影響。"""
        old = led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", occurrence_keys=(O1,))
        _unmap(led, old)
        assert led.claim_initial_delivery(
            business_key="consult:另一份清單|aud", category="consult",
            recipients=["a@x.tw"], subject="會診", occurrence_keys=(O2,))


class TestTheTwoFixesDoNotCancelEachOther:

    def test_a_legacy_successor_is_still_fenced(self, led):
        """★第八輪第 1 輪 P1(兩個修正的交互作用)★

        v5 的 p0(O1)失敗 → 還在跑的 v4 generation 建了同 business_key、
        有 body、正在 SUBMITTING 的 p1(沒有機會鑰匙)→ 回查判定 takeover
        並 supersede(p0 → p1),於是 O1 被【繼承】給 p1。

        這一來 p1 就有了 mapping —— 若圍籬只問「有沒有任何 mapping」,
        它就從圍籬裡消失了:重播 O2(沒有主人)照樣過關,而 p1 的 SMTP
        還在飛。所以圍籬要問的是★有沒有【自己 claim 到】的鑰匙★。
        """
        p0 = led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", body_text="內文", occurrence_keys=(O1,))
        led.settle(p0, failed=True)
        # v4 generation 寫的那一筆:同 key、有 body、SUBMITTING、沒有鑰匙
        p1 = led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", body_text="內文", occurrence_keys=(O2,))
        _unmap(led, p1)
        assert led.supersede(p0, by=p1, note="較新的紀錄接手")
        with sqlite3.connect(led.path) as c:
            got = {(r[0], r[1]) for r in c.execute(
                "SELECT occurrence_key, inherited FROM delivery_occurrences"
                " WHERE delivery_id=?", (p1,))}
        assert got == {(O1, 1)}, "前置:p1 只有【繼承】來的鑰匙,沒有自己的"
        assert led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", occurrence_keys=(O2,)) == "", \
            "★繼承來的鑰匙讓舊紀錄從遷移圍籬裡消失★ 它還在飛就又寄一封"

    def test_a_native_key_still_releases_the_fence(self, led):
        """反方向:接手者是【新版自己 claim 到】的(有 native 鑰匙)→
        圍籬本來就不該管它(所有權由機會鑰匙那條路負責)。"""
        p0 = led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", body_text="內文", occurrence_keys=(O1,))
        led.settle(p0, failed=True)
        p1 = led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", body_text="內文", occurrence_keys=(O2,))
        led.supersede(p0, by=p1, note="較新的紀錄接手")
        # O2 是 p1 自己的鑰匙 → 由所有權那條路擋(而不是遷移圍籬)
        assert led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", occurrence_keys=(O2,)) == ""
        # 另一把全新的機會:p1 有自己的鑰匙 → 不該被遷移圍籬擋
        led.settle(p1, refused={})
        assert led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", occurrence_keys=("sched:2026-08-19|17:00",)), \
            "★新版紀錄被遷移圍籬擋住★ 下一次機會就寄不出去"


class TestSupersedeTransfersOccurrenceOwnership:

    def test_the_successor_inherits_the_keys(self, led):
        """★第八輪 P1-02★ 交棒之後,舊那一筆的機會鑰匙也要算在接手者頭上
        —— 否則接手者還在飛時,那把鑰匙被重播就開出第三筆。"""
        p0 = led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", body_text="內文", occurrence_keys=(O1,))
        led.settle(p0, failed=True)
        p1 = led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", body_text="內文", occurrence_keys=(O2,))
        assert p1 and led.state_of(p1) == dl.SUBMITTING   # 還在飛
        assert led.supersede(p0, by=p1, note="測試交棒")
        assert led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", occurrence_keys=(O1,)) == "", \
            "★交棒沒有把機會鑰匙一起交★ 重播 O1 會與接手者同時寄"

    def test_the_exit_survives_the_transfer(self, led):
        """★出口不變★ 接手者最後也確定失敗 → O1 仍然可以重新 claim
        (兩邊都沒送出去 = 這次機會真的還沒被服務)。"""
        p0 = led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", body_text="內文", occurrence_keys=(O1,))
        led.settle(p0, failed=True)
        p1 = led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", body_text="內文", occurrence_keys=(O2,))
        led.supersede(p0, by=p1, note="測試交棒")
        led.settle(p1, failed=True)
        assert led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", occurrence_keys=(O1,)), \
            "★兩邊都確定沒送出卻不准重來★ 那次請求就漏掉了"

    def test_the_old_mapping_is_kept_not_moved(self, led):
        """不刪舊 mapping:兩邊都持有那把鑰匙(任一方還在飛就擋得住),
        而「都失敗才放行」的判準本來就會處理兩筆。"""
        p0 = led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", body_text="內文", occurrence_keys=(O1,))
        led.settle(p0, failed=True)
        p1 = led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", body_text="內文", occurrence_keys=(O2,))
        led.supersede(p0, by=p1, note="測試交棒")
        with sqlite3.connect(led.path) as c:
            owners = {r[0] for r in c.execute(
                "SELECT delivery_id FROM delivery_occurrences"
                " WHERE occurrence_key=?", (O1,))}
        assert owners == {p0, p1}, "★把舊 mapping 搬走★ 歷史就查不回來了"
