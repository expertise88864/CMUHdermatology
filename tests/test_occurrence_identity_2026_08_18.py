# -*- coding: utf-8 -*-
"""[批次AE-8] 內容識別與「這一次寄送機會」的識別分家(外審第七輪的第二件)。

`business_key` 一直同時扮演兩個角色:
  * 【內容識別】——「這是哪一份未簽清單、寄給誰」(補寄鏈的聚合鍵);
  * 【所有權識別】——「這一次寄送機會歸誰」。

兩者不是同一件事:17:00 報過的清單 20:00 沒簽完還要再報一次,內容一樣、
機會不同。混用的代價在 AE-6/AE-7 一路現形:先是把已結案也當成有人負責
(一天只准通知一次),再退成「PARTIAL + 120 秒租約」去猜交接 —— 猜錯的
兩個方向分別是【重複寄】與【靜默漏寄】。

本檔釘住:給得出機會識別時,判準乾脆且沒有時間猜測 ——
★同一次機會只有一次初次寄送,除非那一次確定沒送出★。
"""
import importlib
import inspect
import os
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

dl = importlib.import_module("cmuh_common.delivery_ledger")

T0 = 3_000_000.0
KEY = "consult:sameContent|aud1"          # 內容識別:兩次提醒完全一樣


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
    return c


@pytest.fixture()
def two_generations(tmp_path):
    db = str(tmp_path / "delivery.sqlite3")
    old = dl.DeliveryLedger(path=db)
    new = dl.DeliveryLedger(path=db)
    yield old, new
    old._close_quietly()
    new._close_quietly()


def _claim(led, occ, *, recipients=("a@x.tw", "b@x.tw")):
    return led.claim_initial_delivery(
        business_key=KEY, category="consult", recipients=list(recipients),
        subject="會診", message_id="", body_text="內文", occurrence_keys=(occ,))


class TestOneOccurrenceOneInitialSend:

    def test_the_same_occasion_is_served_once(self, clock, two_generations):
        """★交棒重跑同一個排程時段★:內容識別擋不住(它得放行 20:00),
        機會識別擋得住 —— 而且【不需要】時間租約。"""
        a, b = two_generations
        occ = "sched:2026-08-18|17:00"
        first = _claim(a, occ)
        assert first
        a.settle(first, refused={})              # 已送達
        clock.advance(3)                         # 交棒:新 generation 起來
        assert _claim(b, occ) == "", \
            "★同一次機會又寄了一封★(舊版靠 120 秒租約猜,過期就重複)"

    def test_a_partial_occasion_is_not_re_sent_even_after_the_lease(
            self, clock, two_generations):
        """★AE-6 第 2 輪 P2 的那個殘留窗口★:PARTIAL、還沒開子紀錄、
        擁有者死在這裡 —— 舊版等 120 秒租約到期就放行,A 會收到第二封。
        機會識別下:那一次機會已經被服務過,不再重來。"""
        a, b = two_generations
        occ = "sched:2026-08-18|17:00"
        first = _claim(a, occ)
        a.settle(first, refused={"b@x.tw": (421, b"later")})
        assert a.state_of(first) == dl.PARTIAL
        clock.advance(3 * 3600)                  # 遠遠超過舊的 120 秒租約
        assert _claim(b, occ) == "", \
            "★A 已經收到,卻又被寄了一整封★"

    def test_a_different_occasion_is_always_allowed(self, clock,
                                                    two_generations):
        """★反方向★ 20:00 是另一次機會:內容一樣也照寄,不然就是漏寄。"""
        a, b = two_generations
        first = _claim(a, "sched:2026-08-18|17:00")
        a.settle(first, refused={})
        clock.advance(3 * 3600)
        assert _claim(b, "sched:2026-08-18|20:00"), \
            "★20:00 的提醒被 17:00 擋掉★ 未簽的會診就沒有人再被提醒"

    def test_a_definitively_failed_occasion_may_be_retried(self, clock,
                                                           two_generations):
        """確定沒送出 → 同一次機會要能重來(沒有人收到,重寄不是重複)。"""
        a, b = two_generations
        occ = "trigger:abc"
        first = _claim(a, occ)
        a.settle(first, failed=True)
        clock.advance(30)
        assert _claim(b, occ), "★確定失敗卻不准重試★ 那次請求就漏掉了"

    def test_an_inflight_resend_of_a_failed_occasion_still_fences(
            self, clock, two_generations):
        """確定沒送出、但補寄子紀錄正在飛 → 這一輪不要再開一封整份的
        (兩封會同時送給同一批人)。"""
        a, b = two_generations
        occ = "trigger:abc"
        first = _claim(a, occ, recipients=["a@x.tw"])
        a.settle(first, failed=True)
        child = a.claim_resend_child(first, business_key=KEY,
                                     category="consult",
                                     recipients=["a@x.tw"], subject="會診")
        assert child, "前置:補寄要 claim 得出來"
        assert _claim(b, occ, recipients=["a@x.tw"]) == "", \
            "★補寄正在飛,又開了一封整份的★"
        a.settle(child, refused={})
        # 補寄成功之後,親紀錄的收件人已被回寫 → 這一次機會確實服務完了。
        assert _claim(b, occ, recipients=["a@x.tw"]) == ""

    def test_the_email_trigger_replay_is_the_same_occasion(self, clock,
                                                           two_generations):
        """★審查點名的形狀★ 同一封觸發信重播(帳號指紋|UIDVALIDITY|uid)
        = 同一次機會;醫師另外寄一封 = 新的 uid = 新的機會,照寄。"""
        a, b = two_generations
        occ1 = "trigger:ident|9|41"
        first = _claim(a, occ1)
        a.settle(first, refused={})
        assert _claim(b, occ1) == "", "重播同一封觸發信不可以再寄一次"
        assert _claim(b, "trigger:ident|9|42"), \
            "★醫師新寄的那一封被當成同一次★ 他就永遠收不到"


class TestAMergedSendOwnsEveryConstituentOccasion:

    def test_a_singleton_replay_after_a_merged_send_is_refused(
            self, clock, two_generations):
        """★審查 AE-8 第 1 輪 P1★ 佇列把兩封 email 觸發併成一次工作寄出去,
        然後在把觸發 journal 結案【之前】硬死;重啟後的補跑是【逐筆】重播
        (`resume_pending_triggers` 每筆各送一次 `trigger_uids=(uid,)`)——
        那一筆寄送必須把兩把鑰匙都佔住,否則單筆重播算出來的鑰匙沒人擁有,
        那位醫師會收到第二封一模一樣的會診結果。"""
        a, b = two_generations
        merged = a.claim_initial_delivery(
            business_key=KEY, category="consult",
            recipients=["dr1@x.tw", "dr2@x.tw"], subject="會診",
            body_text="內文",
            occurrence_keys=("trigger:ident|9|41", "trigger:ident|9|42"))
        assert merged
        a.settle(merged, refused={})            # SMTP 成功
        clock.advance(30)                       # journal 還沒結案就重啟
        assert b.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["dr1@x.tw"],
            subject="會診",
            occurrence_keys=("trigger:ident|9|41",)) == "", \
            "★併批寄送只佔住聚合鑰匙★ 逐筆重播就再寄一次"
        assert b.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["dr2@x.tw"],
            subject="會診",
            occurrence_keys=("trigger:ident|9|42",)) == "", \
            "★第二位醫師也會收到重複★"
        # 反方向:沒被併進去的第三封仍然是新的機會,照寄。
        assert b.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["dr3@x.tw"],
            subject="會診", occurrence_keys=("trigger:ident|9|43",)), \
            "★別人的新觸發被當成同一次★"

    def test_a_mixed_batch_does_not_swallow_the_fresh_request(
            self, clock, two_generations):
        """★審查 AE-8 第 2 輪 P1★ 併批裡一把鑰匙【早被服務】(上一輪寄成功、
        journal 還沒結案就重啟)、另一把是【全新的請求】—— 整批會被拒,
        但帳本必須說得出【哪一把】被服務過,呼叫端才能只結案那一封、把新的
        退回佇列。全部一起結案 = 那位醫師的請求沉默消失。"""
        a, b = two_generations
        old = a.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["dr1@x.tw"],
            subject="會診", occurrence_keys=("trigger:ident|9|41",))
        a.settle(old, refused={})
        clock.advance(30)
        merged = ("trigger:ident|9|41", "trigger:ident|9|77")
        assert b.claim_initial_delivery(
            business_key=KEY, category="consult",
            recipients=["dr1@x.tw", "dr9@x.tw"], subject="會診",
            occurrence_keys=merged) == "", "已被服務的那一把仍要擋下整批"
        served = b.served_occurrence_keys(merged)
        assert served == {"trigger:ident|9|41"}, \
            "★分不出哪一把被服務過★ 呼叫端只好整批結案 = 漏掉新請求"

    def test_the_caller_requeues_only_the_unserved_triggers(self):
        """呼叫端這一半:反推回 uid、把沒被服務的退回佇列(純函式)。"""
        import consult_query as cq
        left = cq._unserved_trigger_uids(("ident|9|41", "ident|9|77"),
                                         {"trigger:ident|9|41"})
        assert left == ("ident|9|77",), "★新請求沒有被退回佇列★"
        assert cq._unserved_trigger_uids(("ident|9|41",),
                                         {"trigger:ident|9|41"}) == ()
        # 查不出來 → 全部當成沒被服務(結案不可逆,重跑只是多查一次)
        assert cq._unserved_trigger_uids(("a", "b"), set()) == ("a", "b")

    def test_an_unreadable_ledger_requeues_everything(self, monkeypatch):
        """★方向要選對★:查不出「哪幾封被服務過」時,一律當成【都還沒】——
        結案是不可逆的(信已標已讀、journal 一清就補不回來),而多跑一輪
        只是重複一次查詢。"""
        import consult_query as cq

        class _Broken:
            def served_occurrence_keys(self, _keys):
                raise RuntimeError("資料庫這一刻讀不到")

        art = cq._DeliveryArtifact(
            recipients=("a@x.tw",), subject="會診", text_body="內文",
            html_body="", attachment=None, message_id="<m@x>",
            business_key="bk", occurrence_keys=("trigger:u1", "trigger:u2"))
        monkeypatch.setattr(cq, "_get_ledger", lambda: _Broken())
        assert cq._unserved_after_claim(art, ("u1", "u2")) == ("u1", "u2"), \
            "★讀不到就當成都被服務過★ 那幾封請求會被靜默結案"
        monkeypatch.setattr(cq, "_get_ledger", lambda: None)
        assert cq._unserved_after_claim(art, ("u1", "u2")) == ("u1", "u2")

    def test_only_the_unserved_senders_are_requeued(self, monkeypatch,
                                                    tmp_path):
        """★審查 AE-8 第 3 輪 P1★ 併批被拒、只退回其中一封時,收件人也要
        跟著只留那一封的寄件人 —— 佇列把收件人與 uid 存成兩個【各自的
        併集】,沿用整批名單會把結果再寄給【已經收到的那位】。
        uid→寄件人的對應在觸發 journal 上本來就有。"""
        import consult_query as cq
        journal = {
            "id|9|41": {"sender": "drA@x.tw", "at": 1.0},
            "id|9|77": {"sender": "drB@x.tw", "at": 2.0},
        }
        monkeypatch.setattr(cq, "_trigger_journal_load",
                            lambda: (journal, True))
        assert cq._trigger_senders_for(("id|9|77",)) == ["drB@x.tw"], \
            "★退回佇列時把已經收到的那位也帶上★ 他會收到第二封"
        assert sorted(cq._trigger_senders_for(("id|9|41", "id|9|77"))) == \
            ["drA@x.tw", "drB@x.tw"]
        # 讀不到 journal → 回空,呼叫端沿用原名單(可能重複)並記 log:
        # 重複比「請求消失」好,但不可以無聲。
        monkeypatch.setattr(cq, "_trigger_journal_load", lambda: ({}, False))
        assert cq._trigger_senders_for(("id|9|77",)) == []

    def test_the_claim_taken_branch_requeues_before_returning(self):
        import consult_query as cq
        # ★不要用固定字元窗★:多寫兩行註解就會把後面的呼叫推出視窗外,
        #   守衛於是量到「沒有重新排隊」= 守衛自己壞掉(AE-6 踩過一次)。
        src = inspect.getsource(cq._do_full_job)
        lines = src.splitlines()
        head = next(i for i, ln in enumerate(lines) if "_CLAIM_TAKEN:" in ln)
        base = len(lines[head]) - len(lines[head].lstrip())
        body = []
        for ln in lines[head + 1:]:
            if ln.strip() and (len(ln) - len(ln.lstrip())) <= base:
                break
            body.append(ln)
        seg = "\n".join(body)
        assert seg, "找不到被拒分支的主體"
        assert "_unserved_after_claim(" in seg, \
            "★被拒的那一輪沒有算出哪幾封還沒被服務★"
        assert "requeued_out.extend(" in seg and \
            "_enqueue_pending_retrigger(" in seg, \
            "★沒被服務的觸發要【不結案】且【重新排隊】★"
        assert "_trigger_senders_for(" in seg, \
            "★重新排隊時沿用整批收件人★ 已經收到的那位會再收一封"
        # ★要量到它【真的被用在那個呼叫上】★:只檢查「函式有出現」的話,
        #   把它算出來卻不傳進去(整批名單照舊)一樣會通過。
        j = seg.index("_enqueue_pending_retrigger(")
        call = seg[j:seg.index(")", j) + 1]
        assert "_left_to" in call, \
            "★算了寄件人卻沒傳給重新排隊★ 下一輪還是寄給整批"

    def test_the_consult_helper_returns_one_key_per_trigger(self):
        """會診端也要真的算出【每一封各一把】,而不是整包雜湊成一把。"""
        import consult_query as cq
        ks = cq._consult_occurrence_keys("email", ("id|9|41", "id|9|42"))
        assert len(set(ks)) == 2, "★併批被壓成一把鑰匙★"
        one = cq._consult_occurrence_keys("email", ("id|9|41",))
        assert set(one) <= set(ks), \
            "★逐筆重播算出來的鑰匙不在併批那一組裡★ 所有權就接不上"

    def test_the_occasion_does_not_depend_on_who_it_is_addressed_to(self):
        """★審查 AE-8 第 1 輪 P2★ 收件人不是機會的一部分:設定改過、
        或兩台機器的名單不同,同一次機會就會算出不同的鑰匙 —— 交棒時
        第二次整封照寄,共同收件人收到重複。"""
        import consult_query as cq
        src = inspect.getsource(cq._consult_occurrence_keys)
        assert "recipients" not in src.split('"""')[-1], \
            "★收件人又混進機會識別★"
        assert cq._consult_occurrence_keys("17:00") == \
            cq._consult_occurrence_keys("17:00"), "同一次機會=同一把鑰匙"


class TestContentIdentityStillDoesItsJob:

    def test_the_resend_chain_still_aggregates_by_content(self, clock,
                                                          two_generations):
        """兄弟仲裁(誰已經收到了)仍以【內容識別】為準 —— 兩者各司其職。"""
        a, b = two_generations
        older = a.claim_initial_delivery(
            business_key=KEY, category="consult",
            recipients=["a@x.tw", "c@x.tw"], subject="會診",
            body_text="內文", occurrence_keys=("sched:d|17:00",))
        a.settle(older, refused={"a@x.tw": (421, b"later"),
                                 "c@x.tw": (421, b"later")})
        clock.advance(60)
        sib = b.claim_initial_delivery(          # 20:00:另一次機會、同內容
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", occurrence_keys=("sched:d|20:00",))
        assert sib, "另一次機會本來就該放行"
        b.settle(sib, refused={})                # 20:00 這封 a@ 收到了
        clock.advance(60)
        child = a.claim_resend_child(older, business_key=KEY,
                                     category="consult",
                                     recipients=["a@x.tw", "c@x.tw"],
                                     subject="會診")
        assert sorted((a.get(child) or {}).get("recipients") or {}) == \
            ["c@x.tw"], "★已由同內容的較新兄弟送達的人又被補寄一次★"


class TestTheNewColumnDoesNotBreakOldData:

    def test_the_legacy_importer_row_matches_the_column_list(self):
        """★加欄位不可以把舊 JSON 匯入弄壞★:匯入那一段是手工組的 tuple,
        欄位一多就與 `_COLUMNS` 對不齊 —— 而它的失敗長相是「舊紀錄靜靜地
        沒有被匯入」(本批第一次就踩到)。用 AST 數出那個 tuple 的長度。"""
        import ast
        import inspect
        import textwrap
        src = inspect.getsource(dl.DeliveryLedger._import_legacy_locked)
        tree = ast.parse(textwrap.dedent(src))
        widths = [len(n.args[0].elts) for n in ast.walk(tree)
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "append" and n.args
                  and isinstance(n.args[0], ast.Tuple)]
        assert widths, "找不到匯入的 row tuple(守衛不可以空集合通過)"
        assert all(w == len(dl._COLUMNS) for w in widths), \
            "★匯入的欄位數與 _COLUMNS 對不齊★ 舊紀錄會整批進不來"

    def test_records_without_an_occurrence_key_still_work(self, clock,
                                                          two_generations):
        """舊資料(occurrence_key 空)照舊走 business_key 判準,不是全部放行、
        也不是全部擋死。"""
        a, b = two_generations
        did = a.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診", body_text="內文")          # 沒有機會識別
        assert did
        assert b.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診") == "", "結果未定時仍要擋(舊判準)"
        a.settle(did, refused={})
        clock.advance(3 * 3600)
        assert b.claim_initial_delivery(
            business_key=KEY, category="consult", recipients=["a@x.tw"],
            subject="會診"), "已結案的內容不該永久擋住(舊判準)"


class TestBothSendersProduceAnOccurrenceKey:

    def test_the_consult_send_site_passes_one(self):
        import consult_query as cq
        import inspect
        assert "occurrence_keys=" in inspect.getsource(cq._delivery_begin), \
            "★會診初次寄送沒有交出機會識別★ 又退回內容識別當所有權"
        art = inspect.getsource(cq._do_full_job)
        assert "_consult_occurrence_keys(" in art, \
            "★payload 組好時沒有一起固定機會識別★"

    def test_the_alert_send_site_passes_only_a_real_occasion_key(self):
        """止掛信的 `notify_key`(日期/診次/醫師)是機會識別;而退化用的
        `alert:{主旨}` ★不可以★ —— 開發者告警的主旨天天一樣,拿它當機會
        識別會讓第一封成功之後再也不會有第二封。"""
        import inspect

        import main
        src = inspect.getsource(main._send_alert_email_via_smtp)
        i = src.index("occurrence_keys=")
        occ_arg = src[i:src.index(")", i) + 1]
        assert "alert:" not in occ_arg, (
            "★退化的 alert:{主旨} 被當成機會識別★ 開發者告警的主旨天天"
            "一樣,第一封成功之後就再也不會有第二封")
        assert "business_key" in occ_arg, "止掛的 notify_key 要傳下去"
        # 對照組:business_key 那個參數【本來就】用得起退化值 —— 它是
        # 內容識別,退化到主旨只會讓不同事件混成一筆帳,不會擋掉寄送。
        j = src.index("business_key=")
        assert "alert:" in src[j:src.index(")", j) + 1], \
            "內容識別那一邊仍保留退化值(兩個參數的語意本來就不同)"

    def test_manual_triggers_always_get_their_own_occasion(self):
        """人明確按下的那一次:寧可重複寄,也不可以被靜默丟掉。"""
        import consult_query as cq
        a = cq._consult_occurrence_keys("手動")
        b = cq._consult_occurrence_keys("手動")
        assert a and b and a != b, "★手動觸發共用同一次機會★ 第二次會被擋"

    def test_the_scheduled_and_poll_keys_are_stable_across_generations(self):
        """交棒後重跑要算出【同一把】鑰匙,否則所有權形同虛設。"""
        import consult_query as cq
        k1 = cq._consult_occurrence_keys("17:00")
        k2 = cq._consult_occurrence_keys("17:00")
        assert k1 == k2 and k1[0].startswith("sched:")
        p1 = cq._consult_occurrence_keys("poll", (), {"111", "222"})
        p2 = cq._consult_occurrence_keys("poll", (), {"222", "111"})
        assert p1 == p2 and p1[0].startswith("poll:")
        assert "111" not in p1[0], "★病歷號被寫進會落地的鑰匙★ 只能放雜湊"
        assert cq._consult_occurrence_keys(
            "poll", (), {"333"}) != p1, "不同的新會診=不同機會"
