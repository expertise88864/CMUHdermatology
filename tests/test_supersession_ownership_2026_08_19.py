# -*- coding: utf-8 -*-
"""[批次AE-11] 現任擁有者只能有一份判準,而讀取端要認得【交棒】(第九輪 2 P1)。

第九輪點出兩個「所有權在某個轉換點看不見」的形狀:

* ★遷移圍籬少了一條判準★:`claim_initial_delivery` 的 legacy 分支問的是
  三條(結果未定 / settle 成 PARTIAL 的短租約交棒中 / 底下有結果未定的
  補寄),而圍籬那一份被抄成兩條 —— 剛好少了【交棒中】。於是混版期間,
  舊 generation 剛 settle 成 PARTIAL、子紀錄還沒開的那一瞬間,新
  generation 會把整封再寄一次。判準必須只有一份實作。
* ★AE-9 世代寫下的 DB 形狀★:那時的 `supersede()` 只寫 `superseded_by`,
  沒有把機會鑰匙交給接手者(AE-10 才有)。磁碟上已經存在的紀錄不會因為
  程式更新而長出 mapping:舊那一筆 FAILED、鑰匙在它身上,接手者還在飛
  但沒有鑰匙 —— 而接手者是【兄弟】不是子紀錄,兩個既有判準都看不到它。
  讀取端必須沿 `superseded_by` 走鏈。
"""
import ast
import importlib
import inspect
import os
import sqlite3
import sys
import textwrap

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

dl = importlib.import_module("cmuh_common.delivery_ledger")

KEY = "consult:ae11|aud"
O1 = "trigger:ident|9|71"
O2 = "sched:2026-08-19|20:00"


@pytest.fixture()
def led(tmp_path):
    obj = dl.DeliveryLedger(path=str(tmp_path / "delivery.sqlite3"))
    yield obj
    obj._close_quietly()


def _sql(led, stmt, args=()):
    with sqlite3.connect(led.path) as c:
        return c.execute(stmt, args).fetchall()


def _unmap(led, did):
    """把某一筆的機會鑰匙拿掉 —— 模擬 v4 舊紀錄(它們沒有 mapping)。"""
    _sql(led, "DELETE FROM delivery_occurrences WHERE delivery_id=?", (did,))


def _mappings_of(led, did):
    return {r[0] for r in _sql(
        led, "SELECT occurrence_key FROM delivery_occurrences"
             " WHERE delivery_id=?", (did,))}


def _mappings(led, key):
    return {(r[0], r[1]) for r in _sql(
        led, "SELECT delivery_id, inherited FROM delivery_occurrences"
             " WHERE occurrence_key=?", (key,))}


class TestTheHandoverStateIsAlsoOwnership:
    """遷移圍籬與 legacy 分支問的是同一件事,少一條判準就是漏擋。"""

    def _make_unmapped_handover(self, led, *, age_sec=0.0):
        """做出【交棒中】的舊紀錄:PARTIAL + payload 還在 + 沒有子紀錄,
        而且沒有自己的機會鑰匙(v4 形狀)。"""
        did = led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw", "b@x.tw"], subject="會診",
            body_text="內文", occurrence_keys=(O1,))
        assert did
        led.settle(did, refused={"b@x.tw": (451, b"try later")})
        assert led.state_of(did) == dl.PARTIAL
        _unmap(led, did)
        if age_sec:
            _sql(led, "UPDATE deliveries SET updated_at=? WHERE delivery_id=?",
                 (dl._now() - age_sec, did))
        return did

    def test_an_unmapped_handover_blocks_the_occurrence_path(self, led):
        """★第九輪 P1-01★ settle 完、子紀錄還沒開 —— 這一瞬間仍有主人。"""
        self._make_unmapped_handover(led)
        assert led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", occurrence_keys=(O2,)) == "", \
            "★交棒中的舊紀錄沒被圍籬看見★ 整封又寄了一次"

    def test_it_releases_once_the_handover_lease_expires(self, led):
        """★出口★ 租約到期(擁有者死了、沒人接手)就放行 —— 圍籬不可以
        變成永久封鎖:漏寄比重複嚴重。"""
        self._make_unmapped_handover(
            led, age_sec=dl._HANDOVER_FENCE_SEC + 60)
        assert led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", occurrence_keys=(O2,)), \
            "★租約到期還在擋★ 那個提醒從此再也寄不出去"

    def test_a_holder_with_its_own_key_is_not_fenced(self, led):
        """圍籬只針對【沒有自己的鑰匙】的舊紀錄:有 native 鍵的那一筆由
        機會鑰匙自己仲裁(下一次機會照寄)。"""
        did = led.claim_initial_delivery(
            business_key=KEY, category="consult",
            recipients=["a@x.tw", "b@x.tw"], subject="會診",
            body_text="內文", occurrence_keys=(O1,))
        led.settle(did, refused={"b@x.tw": (451, b"try later")})
        assert led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", occurrence_keys=(O2,)), \
            "★有自己鑰匙的紀錄被遷移圍籬擋住了★ 20:00 的提醒會消失"

    def test_there_is_only_one_implementation_of_the_predicate(self):
        """★判準只能有一份★:抄成兩份的必然結果就是這一輪的缺陷。
        用「短租約常數在哪些方法裡被讀到」當指紋 —— 它是那條判準獨有的。"""
        src = textwrap.dedent(inspect.getsource(dl.DeliveryLedger))
        tree = ast.parse(src)
        users = set()
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef):
                continue
            for n in ast.walk(fn):
                if isinstance(n, ast.Name) and n.id == "_HANDOVER_FENCE_SEC":
                    users.add(fn.name)
        assert users == {"_legacy_current_owner_locked"}, \
            f"★交棒判準散在 {sorted(users)}★ 判準漂移就是這樣開始的"


class TestTheOccurrenceReaderFollowsSupersession:
    """AE-9 世代寫下的形狀:鑰匙留在舊紀錄,接手者是兄弟、沒有鑰匙。"""

    def _ae9_shaped(self, led, *, heir_state=dl.SUBMITTING, hops=1):
        """做出 AE-9 世代的交棒形狀。

        ★接手者要有【自己的】機會鑰匙★:生產上它就是新 generation 為
        【下一次機會】claim 出來的那一筆(所以有 native 鍵),AE-9 的缺陷
        只在於 `supersede()` 沒有把【舊那一把】鑰匙也交給它。
        這樣安排同時讓這一組測試★只由「讀取端會不會沿鏈走」分勝負★ ——
        接手者若沒有 native 鍵,遷移圍籬(同 business_key、無鑰匙、還在飛)
        會先把重播擋掉,那就量不到鏈查詢有沒有生效了。
        交棒只設 `superseded_by`(AE-9 的 `supersede()` 就只做這件事)。
        """
        old = led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", body_text="內文", occurrence_keys=(O1,))
        assert old
        led.settle(old, failed=True)
        chain = [old]
        for i in range(hops):
            heir = led.claim_initial_delivery(
                business_key=KEY, category="consult", recipients=["a@x.tw"],
                subject="會診", body_text="內文",
                occurrence_keys=("heir:%d" % i,))
            assert heir, "接手者自己那一次機會應該 claim 得到"
            last = (i == hops - 1)
            if not last or heir_state == dl.FAILED:
                led.settle(heir, failed=True)
            _sql(led, "UPDATE deliveries SET superseded_by=?, body_text=''"
                      " WHERE delivery_id=?", (heir, chain[-1]))
            chain.append(heir)
        return chain

    def test_a_live_successor_still_owns_the_occurrence(self, led):
        """★第九輪 P1-02★ 接手者還在飛 → 那一把鑰匙有人服務。"""
        self._ae9_shaped(led)
        assert led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", occurrence_keys=(O1,)) == "", \
            "★接手者還在飛,重播又開出第三筆★ 同一位醫師會收到兩封"

    def test_the_mapping_is_backfilled_for_the_real_owner(self, led):
        """自我修復:沿鏈查到的所有權順手寫成 mapping(繼承來的 = 1),
        之後每一次查詢直接命中,歷史也查得回來。★不刪舊 mapping★。"""
        chain = self._ae9_shaped(led)
        led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", occurrence_keys=(O1,))
        assert (chain[-1], 1) in _mappings(led, O1), "接手者沒有被補登記"
        assert (chain[0], 0) in _mappings(led, O1), \
            "★舊 mapping 被搬走了★ 出口判準(兩邊都失敗才算沒人服務)會失效"

    def test_a_multi_hop_chain_is_followed(self, led):
        """接手者自己也會再被接手(P0→P1→P2)。"""
        self._ae9_shaped(led, hops=3)
        assert led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", occurrence_keys=(O1,)) == ""

    def test_a_failed_successor_leaves_the_exit_open(self, led):
        """★出口仍在★ 接手者也確定沒送出、底下沒有還在飛的補寄 → 這次機會
        還沒被服務,可以再寄(否則那把鑰匙從此永遠寄不出去)。"""
        self._ae9_shaped(led, heir_state=dl.FAILED)
        assert led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", occurrence_keys=(O1,)), \
            "★兩邊都確定失敗還在擋★ 這是靜默漏寄"

    def test_a_failed_successor_with_a_live_child_still_owns(self, led):
        """接手者失敗但它的補寄還在飛 —— 那封正要送給同一批人。"""
        chain = self._ae9_shaped(led, heir_state=dl.FAILED)
        _sql(led, "UPDATE deliveries SET body_text=? WHERE delivery_id=?",
             ("內文", chain[-1]))
        child = led.claim_resend_child(
            chain[-1], business_key=KEY, category="consult",
            recipients=["a@x.tw"], subject="會診")
        assert child
        assert led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", occurrence_keys=(O1,)) == ""

    def test_a_cycle_does_not_hang_and_does_not_block(self, led):
        """`superseded_by` 是自由文字欄位:壞資料成環不可以讓帳本卡住,
        也不可以變成永久封鎖(失效方向要是「多寄一封」)。"""
        chain = self._ae9_shaped(led, heir_state=dl.FAILED)
        _sql(led, "UPDATE deliveries SET superseded_by=? WHERE delivery_id=?",
             (chain[0], chain[-1]))
        assert led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", occurrence_keys=(O1,))

    def test_a_chain_longer_than_the_cap_falls_back_to_available(self, led,
                                                                 caplog):
        """★上限的失效方向要選對★:壞資料造出超長鏈 → 判準是「查不出來」
        =沒有接手者(可以再寄一封),不是無條件擋住(靜默漏寄);而且要大聲
        留紀錄 —— 這種資料本身就是異常。"""
        # ★鏈長不可以從被測的常數推出來★:那樣一改常數,目標也跟著移動
        #   —— 突變驗證量到的會是「測試自己的算式」,不是那條規則。
        assert dl._SUPERSEDE_MAX_HOPS < 40, "上限變大了,這條反例要跟著加長"
        chain = self._ae9_shaped(led, hops=40)
        assert led.state_of(chain[-1]) == dl.SUBMITTING
        with caplog.at_level("WARNING"):
            got = led.claim_initial_delivery(
                business_key=KEY, category="consult", recipients=["a@x.tw"],
                subject="會診", occurrence_keys=(O1,))
        assert got, "★超過上限就永遠擋住★ 那把鑰匙從此寄不出去"
        assert any("superseded_by" in str(r.msg) for r in caplog.records),             "沒有留下警告 —— 壞資料要看得見"

    def test_a_dangling_successor_is_not_an_owner(self, led):
        """指向不存在的紀錄 = 查不到接手者(不是「有人在飛」)。"""
        old = led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", body_text="內文", occurrence_keys=(O1,))
        led.settle(old, failed=True)
        _sql(led, "UPDATE deliveries SET superseded_by=? WHERE delivery_id=?",
             ("did-不存在", old))
        assert led.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", occurrence_keys=(O1,))

    def test_the_read_only_path_sees_the_chain_but_writes_nothing(self, led):
        """唯讀查詢要看得到同一件事(判準只有一份),但★不可以寫東西★。"""
        chain = self._ae9_shaped(led)
        before = _mappings(led, O1)
        assert led.served_occurrence_keys((O1,)) == {O1}
        assert _mappings(led, O1) == before, "★唯讀路徑寫了 mapping★"
        assert (chain[-1], 1) not in before
