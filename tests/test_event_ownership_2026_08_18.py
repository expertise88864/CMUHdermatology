# -*- coding: utf-8 -*-
"""[批次AE-6] 初次寄送的事件所有權(外審 2026-08-18 全審第六輪 P1-01/P2-01)。

★P1-01★ 補寄側從 AE-1 到 AE-5 一路把仲裁做齊了(claim_resend_child /
supersede / newer sibling / superseded fence),★初次寄送卻從來不問可不可以
寄★。而自動更新交棒【刻意】先放開單例互斥、才收背景工作
(`bg_executor.shutdown(wait=False, cancel_futures=True)` 不會取消
【已經在跑】的 future):舊 generation 的 SMTP 還在飛、去重檔還沒寫,
新 generation 就掃到同一個事件並自己 begin 一筆 —— 同一則臨床通知寄兩封,
不需要任何異常,走的是正常的自動更新路徑。

★P2-01★ 同 business_key 的兄弟紀錄已送達,只被拿來當【送出閘門】、
沒有回寫親紀錄:掃描端說「還欠補寄」、claim 說「不准補,他已經收到」、
親紀錄說「他沒收到」—— 三方各執一詞,直到 body 到期結案時對【已經送到的
事】發出「始終沒收到,請人工確認/轉寄」的告警(誘導人工重複通知)。

★測試形狀★ 審查指定:用【兩個 DeliveryLedger instance 指向同一個 SQLite
檔】(= 兩個 process / 兩個 generation),真正做 transaction interleaving,
不是同一個物件呼叫兩次。
"""
import importlib
import inspect
import os
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

dl = importlib.import_module("cmuh_common.delivery_ledger")

T0 = 1_000_000.0
KEY = "consult:eventK|aud1"


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
    """兩個 instance、同一個 DB 檔 = 兩個 process(或交棒中的兩個 generation)。"""
    db = str(tmp_path / "delivery.sqlite3")
    old = dl.DeliveryLedger(path=db)
    new = dl.DeliveryLedger(path=db)
    yield old, new
    old._close_quietly()
    new._close_quietly()


def _as_epoch_equal(rec_a, rec_b) -> bool:
    """兩筆紀錄的 created_at 是不是同一刻(本檔的凍結時鐘應該讓它成立)。"""
    return dl._as_epoch((rec_a or {}).get("created_at")) == \
        dl._as_epoch((rec_b or {}).get("created_at"))


def _claim(led, *, key=KEY, recipients=("a@x.tw", "b@x.tw"), subject="會診"):
    return led.claim_initial_delivery(
        business_key=key, category="consult", recipients=list(recipients),
        subject=subject, message_id="", body_text="內文")


def _branch_body(src: str, header: str) -> str:
    """`if <header>` 這個分支的【主體】原始碼。

    ★不要用固定字元窗★:多寫兩行註解就會把 `return` 推出視窗外,守衛於是
    量到「這個分支沒有結束這一輪」—— 那是守衛自己壞掉,不是程式壞掉
    (2026-08-18 實際發生過一次)。改用縮排界定分支。
    """
    lines = src.splitlines()
    for i, line in enumerate(lines):
        if header in line:
            base = len(line) - len(line.lstrip())
            body = []
            for nxt in lines[i + 1:]:
                if nxt.strip() and (len(nxt) - len(nxt.lstrip())) <= base:
                    break
                body.append(nxt)
            assert body, "分支主體是空的:%s" % header
            return "\n".join(body)
    raise AssertionError("找不到分支:%s" % header)


# ── 一、四種交錯:同一事件同時只有一個 sender ──────────────────────────────
class TestOnlyOneSenderOwnsAnEvent:

    def test_initial_a_vs_initial_b(self, clock, two_generations):
        """★兩個初次 sender 同時來 → 只有一個拿得到★(P1-01 的原形)。"""
        a, b = two_generations
        did_a = _claim(a)
        assert did_a, "第一個 sender 應該取得所有權"
        assert a.state_of(did_a) == dl.SUBMITTING
        assert _claim(b) == "", "★同一事件被寄了兩封★ 第二個 sender 不該拿到"
        # 對方確定失敗之後,所有權要放出來(抑制自帶出口)。
        a.settle(did_a, failed=True)
        assert b.state_of(did_a) == dl.FAILED
        assert _claim(b), "對方已 FAILED,第二個 sender 必須能接手"

    def test_initial_first_then_auto_resend(self, clock, two_generations):
        """初次寄送在飛 → 同事件的自動補寄這一輪不出手(不會兩封同時飛)。"""
        a, b = two_generations
        old_did = a.begin(business_key=KEY, category="consult",
                          recipients=["a@x.tw"], subject="會診",
                          body_text="內文")
        a.settle(old_did, failed=True)          # 舊鏈:a 暫時性被拒,欠補寄
        clock.advance(60)
        assert _claim(b), "新一輪的初次寄送應該拿得到所有權"
        assert a.claim_resend_child(
            old_did, business_key=KEY, category="consult",
            recipients=["a@x.tw"], subject="會診") == "", \
            "★同事件已有較新的初次寄送在飛,補寄不可以同時出手★"

    def test_auto_resend_first_then_initial(self, clock, two_generations):
        """補寄子紀錄在飛 → 新的初次寄送這一輪不出手(反方向的交錯)。"""
        a, b = two_generations
        parent = a.begin(business_key=KEY, category="consult",
                         recipients=["a@x.tw"], subject="會診",
                         body_text="內文")
        a.settle(parent, failed=True)
        child = a.claim_resend_child(parent, business_key=KEY,
                                     category="consult",
                                     recipients=["a@x.tw"], subject="會診")
        assert child, "前置:補寄子紀錄要先 claim 成功"
        clock.advance(60)
        assert _claim(b, recipients=["a@x.tw"]) == "", \
            "★補寄正在飛,初次寄送不可以同時寄給同一個人★"
        a.settle(child, refused={})             # 補寄送達 → 子紀錄收斂
        assert _claim(b, recipients=["a@x.tw"]), \
            "補寄已收斂,新的初次寄送必須能開始(出口存在)"

    def test_restart_handover_old_generation_still_in_flight(
            self, clock, two_generations):
        """★審查點名的那條路★ 自動更新交棒:舊 generation 的 SMTP 還在飛
        (紀錄 SUBMITTING、去重檔還沒寫),新 generation 已經起來並掃到同一
        事件 —— 新的必須寄不出去。"""
        old_gen, new_gen = two_generations
        flying = _claim(old_gen)                # 舊 generation:登記=送出中
        assert flying
        clock.advance(3)                        # 交棒:單例互斥已放開
        assert _claim(new_gen) == "", \
            "★交棒期間同一則會診通知寄了兩封★"
        # 舊 generation 被砍掉 → 那一筆由回查/收斂判成確定沒送出 → 才放行。
        old_gen.settle(flying, failed=True)
        assert _claim(new_gen), "舊紀錄已收斂成 FAILED,新 generation 要能寄"


# ── 二、所有權講的是「此刻誰在寄」,不是「今天寄過了」 ─────────────────────
class TestOwnershipIsAboutWhoIsSendingNow:

    def test_a_concluded_earlier_send_does_not_block_the_next_one(
            self, clock, two_generations):
        """★核心反例★ business_key 是【內容識別】——17:00 報過的同一份
        未簽清單,20:00 還沒簽完就要再報一次。把已結案(CONFIRMED)也當成
        「有人負責」= 一天只准通知一次,20:00 的提醒被靜默丟掉。"""
        a, b = two_generations
        first = _claim(a)
        a.settle(first, refused={})
        assert a.state_of(first) == dl.CONFIRMED
        clock.advance(3 * 3600)
        assert _claim(b), "★已結案的過去式不該擋住下一次通知★"

    def test_the_settle_to_child_claim_transition_is_fenced(self, clock,
                                                            two_generations):
        """★審查 AE-6 第 1 輪 P1-1★ settle 成 PARTIAL 與建立補寄子紀錄是
        【兩筆交易】—— 中間那個空隙裡「結果未定」已經不成立、子紀錄還不
        存在。新 generation 這時 claim 得到的話,會把整封再寄一次,而剛剛
        收到的 A 就收到重複的臨床通知(交棒本來就會停在這裡)。"""
        a, b = two_generations
        first = _claim(a)
        a.settle(first, refused={"b@x.tw": (421, b"later")})
        assert a.state_of(first) == dl.PARTIAL
        assert (a.get(first) or {}).get("body_text"), "前置:補寄鏈還開著"
        clock.advance(1)                    # 還沒建立子紀錄的那一瞬間
        assert _claim(b) == "", \
            "★settle 與補寄之間的空隙讓整封又寄了一次★"

    def test_the_handover_lease_expires(self, clock, two_generations):
        """★審查 AE-6 第 2 輪 P1-1★ 補寄鏈的【整個壽命】不是所有權:
        `body_text` 代表的是「還欠一次補寄」的義務(成功/放棄/回查/到期
        才消失)。拿它當 fence 會讓 20:00 的排程提醒、醫師手動再按一次
        統統靜默消失 —— 漏寄比重複嚴重。租約只涵蓋交接那一瞬間。"""
        a, b = two_generations
        first = _claim(a)
        a.settle(first, refused={"b@x.tw": (421, b"later")})
        assert _claim(b) == "", "前置:剛 settle 完的那一刻要擋"
        clock.advance(3 * 3600)             # 鏈還開著,但交接早就結束了
        assert (a.get(first) or {}).get("body_text"), \
            "前置:這個情境的重點就是【鏈仍然開著】"
        assert _claim(b), \
            "★整條補寄鏈的壽命都被當成所有權★ 20:00 的提醒被靜默丟掉"

    def test_an_existing_child_takes_over_the_fence(self, clock,
                                                    two_generations):
        """有子紀錄之後就由既有的 in-flight 檢查接手:子紀錄已收斂
        (沒有人正在寄)→ 下一則相同內容的提醒不該被擋。"""
        a, b = two_generations
        first = _claim(a, recipients=["a@x.tw", "b@x.tw"])
        a.settle(first, refused={"b@x.tw": (421, b"later")})
        child = a.claim_resend_child(first, business_key=KEY,
                                     category="consult",
                                     recipients=["b@x.tw"], subject="會診")
        assert child, "前置:補寄子紀錄要開得出來"
        a.settle(child, failed=True)        # 補寄失敗 → 沒有人正在寄
        clock.advance(30)
        assert _claim(b, recipients=["a@x.tw", "b@x.tw"]), \
            "★沒有任何 sender 在飛卻還擋著★"

    def test_the_chain_closing_releases_the_event(self, clock,
                                                  two_generations):
        """★出口之二★ 補寄鏈收斂(結案/放棄)清掉 payload → 立刻放行,
        不必等租約到期。"""
        a, b = two_generations
        first = _claim(a)
        a.settle(first, refused={"b@x.tw": (421, b"later")})
        assert _claim(b) == ""
        a.clear_body(first, note="補寄鏈結案")
        clock.advance(1)
        assert _claim(b), "★鏈已結案卻還擋著★ 下一輪的提醒就漏掉了"

    def test_an_unresolved_send_does_block_until_it_resolves(
            self, clock, two_generations):
        """UNKNOWN(逾時,可能已送達)★要擋★ —— 但出口存在:回查判定後放行。"""
        a, b = two_generations
        first = _claim(a)
        a.settle(first, unknown=True)
        assert a.state_of(first) == dl.UNKNOWN
        assert _claim(b) == "", "★結果不明時再寄一封 = 可能重複的臨床通知★"
        a.resolve_unknown(first, delivered=False)
        assert _claim(b), "回查確定沒送出 → 必須放行(抑制要有出口)"


# ── 三、同事件的結論要回寫,不能只當送出閘門(P2-01) ───────────────────────
class TestSiblingConclusionsAreWrittenBack:

    def test_a_sibling_delivery_heals_the_parent(self, clock,
                                                 two_generations):
        """兄弟紀錄已送達 → 親紀錄上那一位要變成【已送達】,不是停在暫時被拒。"""
        a, b = two_generations
        older = a.begin(business_key=KEY, category="consult",
                        recipients=["a@x.tw", "c@x.tw"], subject="會診",
                        body_text="內文")
        a.settle(older, refused={"a@x.tw": (421, b"later"),
                                 "c@x.tw": (421, b"later")})
        clock.advance(60)
        # 另一支 process 的同 key 紀錄把 a@ 送到了(c@ 仍被拒)。
        sib = b.begin(business_key=KEY, category="consult",
                      recipients=["a@x.tw"], subject="會診")
        b.settle(sib, refused={})
        clock.advance(60)
        # 舊鏈這一輪只補得到 c@(a@ 已由兄弟送達)——★而且 a@ 要被回寫★。
        child = a.claim_resend_child(older, business_key=KEY,
                                     category="consult",
                                     recipients=["a@x.tw", "c@x.tw"],
                                     subject="會診")
        assert child, "c@ 還欠補寄,這一次 claim 應該成立"
        assert sorted((a.get(child) or {}).get("recipients") or {}) == \
            ["c@x.tw"], "已由兄弟送達的人不可以再寄一次"
        parent = b.get(older) or {}
        assert parent["recipients"]["a@x.tw"] == dl.R_CONFIRMED, \
            "★兄弟的送達結論沒有回寫親紀錄★ 帳上永遠停在暫時被拒"

    def test_an_older_success_must_not_heal_a_newer_failure(self, clock,
                                                            two_generations):
        """★審查 AE-6 第 1 輪 P1-2★ 17:00 那封送到了、20:00 這封被暫時拒收
        —— 兩者 business_key 相同(清單沒變)。舊事件的送達不可以拿來替新
        事件背書:那會讓 20:00 的紀錄變 CONFIRMED、payload 被清、補寄與漏收
        告警一起消失,而 SMTP 明明說那位沒收到 20:00 這一封。"""
        a, b = two_generations
        old = a.begin(business_key=KEY, category="consult",
                      recipients=["a@x.tw"], subject="會診 17:00")
        a.settle(old, refused={})                    # 17:00:確定送達
        assert a.state_of(old) == dl.CONFIRMED
        clock.advance(3 * 3600)
        new = b.begin(business_key=KEY, category="consult",
                      recipients=["a@x.tw"], subject="會診 20:00",
                      body_text="內文")
        b.settle(new, refused={"a@x.tw": (421, b"later")})   # 20:00:被拒
        clock.advance(60)
        child = b.claim_resend_child(new, business_key=KEY,
                                     category="consult",
                                     recipients=["a@x.tw"],
                                     subject="會診 20:00")
        assert child, "★舊事件的送達把新事件判成已達★ 20:00 這封就不補了"
        parent = a.get(new) or {}
        assert parent["recipients"]["a@x.tw"] == dl.R_TRANSIENT, \
            "★舊事件的結論被寫進新紀錄★ 帳上說收到了,其實沒有"
        assert parent.get("body_text"), "payload 不可以因為舊事件而被清掉"

    def test_a_same_instant_older_sibling_must_not_heal_either(
            self, clock, two_generations):
        """★審查 AE-6 第 2 輪 P2★ 時間戳相同(粗解析度時鐘、匯入的舊資料)
        時,`>=` 會把同一刻的【舊】紀錄放進來,缺陷原樣復發 —— 旁邊的
        live_newer 檢查本來就是嚴格大於。這裡刻意不推進時鐘。"""
        a, b = two_generations
        old = a.begin(business_key=KEY, category="consult",
                      recipients=["a@x.tw"], subject="會診 17:00")
        a.settle(old, refused={})
        new = b.begin(business_key=KEY, category="consult",   # 同一刻
                      recipients=["a@x.tw"], subject="會診 20:00",
                      body_text="內文")
        b.settle(new, refused={"a@x.tw": (421, b"later")})
        assert _as_epoch_equal(a.get(old), b.get(new)), \
            "前置:這一條就是要測【時間戳相同】"
        child = b.claim_resend_child(new, business_key=KEY,
                                     category="consult",
                                     recipients=["a@x.tw"],
                                     subject="會診 20:00")
        assert child, "★同一刻的舊紀錄又把新事件判成已達★"
        assert (a.get(new) or {})["recipients"]["a@x.tw"] == dl.R_TRANSIENT

    def test_delivery_beats_a_conflicting_permanent_refusal(self, clock,
                                                            two_generations):
        """★審查 AE-6 第 1 輪 P2★ 一個兄弟說已送達、另一個說 550(信箱設定
        改過就會這樣)→ 帳上必須是【已送達】。反過來寫的話,有送達證據卻
        記成查無此人,鏈也不會結案(payload 留到過期才清)。"""
        a, b = two_generations
        parent = a.begin(business_key=KEY, category="consult",
                         recipients=["a@x.tw"], subject="會診",
                         body_text="內文")
        a.settle(parent, refused={"a@x.tw": (421, b"later")})
        clock.advance(60)
        bad = b.begin(business_key=KEY, category="consult",
                      recipients=["a@x.tw"], subject="會診")
        b.settle(bad, refused={"a@x.tw": (550, b"no such user")})
        good = b.begin(business_key=KEY, category="consult",
                       recipients=["a@x.tw"], subject="會診")
        b.settle(good, refused={})
        clock.advance(60)
        assert a.claim_resend_child(parent, business_key=KEY,
                                    category="consult",
                                    recipients=["a@x.tw"],
                                    subject="會診") == ""
        rec = b.get(parent) or {}
        assert rec["recipients"]["a@x.tw"] == dl.R_CONFIRMED, \
            "★有送達證據卻被記成永久被拒★ 送達是最強的結論"

    def test_the_healed_chain_closes_instead_of_alerting(self, clock,
                                                         two_generations):
        """★被守的行為★ 全部待補的人都已由兄弟送達 → 這條鏈要能【結案】,
        而不是掛著「還有人沒收到」等 body 到期後誤發人工轉寄告警。"""
        a, b = two_generations
        older = a.begin(business_key=KEY, category="consult",
                        recipients=["a@x.tw"], subject="會診",
                        body_text="內文")
        a.settle(older, refused={"a@x.tw": (421, b"later")})
        assert a.needs_recipient_retry(), "前置:此刻帳上確實欠 a@ 一次補寄"
        clock.advance(60)
        sib = b.begin(business_key=KEY, category="consult",
                      recipients=["a@x.tw"], subject="會診")
        b.settle(sib, refused={})
        clock.advance(60)
        assert a.claim_resend_child(older, business_key=KEY,
                                    category="consult",
                                    recipients=["a@x.tw"],
                                    subject="會診") == "", \
            "沒有人還需要補寄 → 不該再開子紀錄"
        owed = [d for d, _addrs in b.needs_recipient_retry() if d == older]
        assert not owed, \
            "★帳上還掛著『a@ 沒收到』★ body 到期就會誤發人工轉寄告警"


# ── 四、接線:兩個初次寄送點都要先問所有權 ────────────────────────────────
class TestBothInitialSendersAskFirst:

    def test_the_consult_initial_send_claims_the_event(self):
        import consult_query as cq
        src = inspect.getsource(cq._delivery_begin)
        assert "claim_initial_delivery(" in src, \
            "★初次寄送沒有向帳本取得事件所有權★"
        # ★走 claim 的只有【明講自己是事件初次寄送】的呼叫端★:佇列退避的
        #   直登記後備(拿不到 origin_did 時 parent_id 也是空的)必須還是
        #   begin —— 用「parent_id 是空的」當判準會把它也當成初次寄送。
        assert "led.begin(" in _branch_body(src, "if not claim_event:"), \
            "非事件初次寄送那條路仍走 begin"
        assert "claim_event=True" in inspect.getsource(cq._do_full_job), \
            "★初次寄送點沒有宣告自己要 claim★"
        assert "claim_event" not in inspect.getsource(
            cq._resend_transient_refusals), \
            "★佇列補寄不可以宣稱事件所有權★(它的 business_key 是 K|retryN)"

    def test_the_legacy_queue_path_never_claims_an_event(self):
        """★行為★ 佇列退避的直登記後備(拿不到 origin_did 時 `parent_id`
        也是空的)★不可以★走事件所有權那條路 —— 它拿到的 `_CLAIM_TAKEN`
        會被當成 delivery_id 一路用下去(拿去 settle 一筆不存在的紀錄),
        而信照樣寄出去。"""
        import consult_query as cq
        led = cq._get_ledger()
        assert led is not None
        key = "consult:legacyK|aud"
        owner = led.claim_initial_delivery(
            business_key=key, category="consult",
            recipients=["a@x.tw"], subject="會診")
        assert owner, "前置:這個事件此刻確實有人在寄"
        art = cq._DeliveryArtifact(
            recipients=("a@x.tw",), subject="會診", text_body="內文",
            html_body="", attachment=None,
            message_id="<m@x>", business_key=key)
        did = cq._delivery_begin(art, "17:00")      # claim_event 預設 False
        assert did and did != cq._CLAIM_TAKEN, \
            "★退避重試被當成事件初次寄送★ 呼叫端會拿哨兵值當 delivery_id"

    def test_the_consult_send_site_skips_when_taken(self):
        """所有權被別人拿走 → 這一輪不寄、不留 PHI 截圖、不推已通知基準。"""
        import consult_query as cq
        src = inspect.getsource(cq._do_full_job)
        seg = _branch_body(src, "_CLAIM_TAKEN:")
        assert "_discard_undelivered_shot(delivery)" in seg, \
            "★沒寄出去的病人畫面留在磁碟上★"
        assert "return" in seg, "被別人拿走就要結束這一輪(不可繼續往下寄)"
        assert src.index("_CLAIM_TAKEN") < src.index("send_via_smtp("), \
            "所有權判斷必須在真正送出之前"

    def test_the_alert_initial_send_claims_the_event(self):
        """止掛提醒同理;★拿不到就回 False★ —— 呼叫端才不會記去重
        (記了的話那位 sender 若失敗,這則提醒就永遠不會再寄)。"""
        import main
        src = inspect.getsource(main._send_alert_email_via_smtp)
        i = src.index("claim_initial_delivery(")
        i_send = src.index("send_mail(")
        assert i < i_send, "取得所有權必須在真正送出之前"
        assert "SendResult(False)" in src[i:i_send], \
            "★拿不到所有權卻回成功★ 呼叫端會記去重 → 下一輪不再嘗試"
