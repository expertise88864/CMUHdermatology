# -*- coding: utf-8 -*-
"""[外審第三輪 R3-P2-02] 「走 https」不等於「驗證過對方是誰」。

惠和的掛號頁走 `https://appointment.cmuh.org.tw/...`,而那台主機在
`http_client.INTERNAL_HOSTS` 裡 —— 那份清單正是用來★關掉 SSL 憑證驗證★的
(院內憑證驗不過)。所以它實際是「有加密、但沒有驗證對方身分」:路徑上的裝置
只要能 MITM,出一張任意憑證就會被接受。

傷害模型與明文那條相同(顯示與通知內容被操縱 → 假的止掛提醒 / 假的滿診),
只是它★看起來像正常 HTTPS★,反而更容易讓人以為已驗證。
而明文那兩家早就有 provenance 標示,惠和沒有 —— 同一個偽造的數字換一家分院
就又變回「看起來已驗證」(這正是外審第 2 輪在【兩條止掛路徑】上抓過的形狀,
現在換成【分院之間】的版本)。

★使用者 2026-08-30 定案:院內 CA 拿不到 → 先做 provenance 標示★
(拿得到 CA 的話正解是 `verify=<ca bundle>`,那時標示會自動消失 —— 因為判準是
從 `is_internal()` 現算的,見下面最後一組測試。)
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import fetch_resilience as fr  # noqa: E402
from cmuh_common import http_client  # noqa: E402
from cmuh_common import reg52_fetch as rf  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_resilience_state():
    """★熔斷/backoff 是模組層狀態★:我的 parametrize 測試會讓四家各記一次
    失敗,下一條測試的 fetch 就被熔斷擋掉、根本不送請求(第一版因此 KeyError)。
    沿用既有 reg52 測試的做法。"""
    fr.reset_all()
    yield
    fr.reset_all()
from cmuh_common.reg52_fetch import (  # noqa: E402
    TRUST_PLAINTEXT, TRUST_UNKNOWN, TRUST_UNVERIFIED_TLS, TRUST_VERIFIED,
    UNVERIFIED_TLS_NOTE, UNVERIFIED_TRANSPORT_NOTE, is_plaintext_source,
    transport_note, transport_trust,
)


class TestEachBranchGetsTheRightTrustLevel:
    def test_the_internal_https_branch_is_unverified(self):
        """★核心★:惠和走 https,但主機在 INTERNAL_HOSTS → 憑證沒驗。"""
        assert transport_trust("huihe") == TRUST_UNVERIFIED_TLS

    def test_plaintext_branches_stay_plaintext(self):
        """明文那兩家的判定不可以被這次改動弄丟(它們更嚴重)。"""
        assert transport_trust("east") == TRUST_PLAINTEXT
        assert transport_trust("huisheng") == TRUST_PLAINTEXT

    def test_the_external_https_branch_is_verified(self):
        """★對照組★:亞大走外部網域 → 有驗憑證 → 不需要標註
        (全部都標等於沒標;而且會把一個真的可信的來源說成不可信)。"""
        assert transport_trust("auh") == TRUST_VERIFIED
        assert transport_note("auh") == ""

    def test_an_unknown_branch_is_unknown_not_verified(self):
        """★判不出來不可以說成已驗證★:認不得的代碼是 unknown。"""
        assert transport_trust("nope") == TRUST_UNKNOWN
        assert transport_trust(None) == TRUST_UNKNOWN

    def test_a_broken_predicate_does_not_claim_verified(self, monkeypatch):
        """★連判準自己壞掉時也不可以說成已驗證★
        (那是把「不知道」講成「已驗證」——整個 repo 的一貫要求)。"""
        # ★換掉 http_client 的名字動不到 reg52_fetch 自己 import 的那一個★
        #   (`from cmuh_common.http_client import is_internal as _is_internal`)
        #   —— 第一版就是這樣,測試綠得毫無意義。
        monkeypatch.setattr(
            rf, "_is_internal",
            lambda _u: (_ for _ in ()).throw(RuntimeError("boom")))
        assert transport_trust("huihe") == TRUST_UNKNOWN


class TestTheTwoNotesSayDifferentThings:
    def test_plaintext_and_unverified_tls_do_not_share_a_sentence(self):
        """★兩種情況的實際保證不同★:
          * 明文 HTTP:連內容都可能被改寫;
          * 未驗證 TLS:內容有加密,但無法確認對方就是那台主機。
        講得比實際知道的多或少都不行。"""
        assert transport_note("east") == UNVERIFIED_TRANSPORT_NOTE
        assert transport_note("huihe") == UNVERIFIED_TLS_NOTE
        assert UNVERIFIED_TLS_NOTE != UNVERIFIED_TRANSPORT_NOTE

    def test_the_tls_note_does_not_claim_the_content_was_altered(self):
        """未驗證 TLS 的句子不可以說成「內容無法驗證是否被改動」——
        那是明文那條的性質;這條的問題是【身分】。"""
        assert "無法確認對方" in UNVERIFIED_TLS_NOTE
        assert "加密" in UNVERIFIED_TLS_NOTE

    def test_the_legacy_helper_still_answers_the_narrow_question(self):
        """`is_plaintext_source()` 的語意不變(只問「有沒有 TLS」)——
        它現在委派新判準,但★不可以★因此把惠和也算成明文。"""
        assert is_plaintext_source("east") is True
        assert is_plaintext_source("huihe") is False


class TestTheVerdictMatchesWhatTheFetchActuallyDoes:
    """★外審 R3-P2-02 第 1 輪:「由實際行為推導」必須是真的★

    我第一版讓分類讀 `is_internal()`,而四支 fetch 裡有三支把 `verify=True`
    ★寫死★ —— 只要有人把那些主機加進 INTERNAL_HOSTS,分類就會說「程式停用了
    TLS 憑證檢查」而 fetch 其實仍在驗:給使用者一個★錯誤的保證★。
    ★而我的測試只問分類、不問 fetch,等於把那個錯誤前提釘成正確答案★
    (同一個教訓在這個 repo 已經犯過好幾次)。
    現在四支 fetch 與分類共用 `verify_policy()`,測試也兩邊都問。
    """

    @staticmethod
    def _captured_verify(fetch, *args):
        """用假 session 跑一次 fetch,回它實際傳出去的 `verify`。"""
        seen = {}

        class _Resp:
            status_code, text, encoding = 200, "", "big5"

            def raise_for_status(self):
                return None

        class _Session:
            def get(self, url, **kw):
                seen.setdefault("verify", kw.get("verify"))
                seen.setdefault("url", url)
                return _Resp()

        try:
            fetch(_Session(), *args)
        except Exception:
            pass                     # 解析/熔斷結果不是這條測試要問的
        return seen

    @pytest.mark.parametrize("branch,fetch_name,args", [
        ("east", "_fetch_east_district_reg52_html", ("D12345", "測試醫師")),
        ("huihe", "_fetch_huihe_reg52_html", ("D12345", "測試醫師")),
        ("huisheng", "_fetch_huisheng_reg52_html", ("D12345", "測試醫師")),
        ("auh", "_fetch_auh_reg52_html", ("方心禹",)),
    ])
    def test_the_https_branches_agree_with_their_classification(
            self, branch, fetch_name, args):
        """★核心不變式★:走 https 的分院,實際送出的 `verify`
        必須恰好等於「這一家被分類成已驗證」。"""
        seen = self._captured_verify(getattr(rf, fetch_name), *args)
        assert "verify" in seen, f"{branch} 的 fetch 沒有送出請求(測試失效)"
        if not str(seen["url"]).lower().startswith("https://"):
            return                   # 明文那兩家:verify 對它們沒有意義
        assert seen["verify"] is (transport_trust(branch) == TRUST_VERIFIED), (
            branch, seen["verify"], transport_trust(branch))

    def test_making_auh_internal_flips_both_the_verdict_and_the_fetch(
            self, monkeypatch):
        """★這才是「判準與行為必然一致」的證明★:把亞大的主機加進內網清單
        之後,分類變成未驗證 ★而且 fetch 真的不再驗憑證★ ——
        我第一版只斷言了前者,那句宣稱當時是假的。"""
        assert self._captured_verify(
            rf._fetch_auh_reg52_html, "方心禹")["verify"] is True   # 前提
        # ★同一條測試裡跑兩次 fetch★:第一次的語意失敗會記 backoff,
        #   第二次就被短路而根本不送請求(KeyError)。中間要重置。
        fr.reset_all()
        monkeypatch.setattr(
            http_client, "INTERNAL_HOSTS",
            set(http_client.INTERNAL_HOSTS) | {"appointment.auh.org.tw"})
        assert transport_trust("auh") == TRUST_UNVERIFIED_TLS
        assert self._captured_verify(
            rf._fetch_auh_reg52_html, "方心禹")["verify"] is False

    def test_todays_behaviour_is_unchanged(self):
        """★這一批不改任何 TLS 行為★:惠和不驗、其餘 https 驗。"""
        assert rf.verify_policy(rf.HUIHE_REG52_URL) is False
        assert rf.verify_policy(rf.AUH_REG52_BASE_URL) is True


class TestTheVerdictIsDerivedNotAHandMaintainedList:
    def test_making_a_host_internal_turns_its_branch_unverified(self,
                                                                monkeypatch):
        """★這才是「單一判準」的價值★:日後有人把某台主機加進
        INTERNAL_HOSTS(等於關掉它的 TLS 驗證),對應分院的標示要★自動出現★。

        反例用亞大:它現在是 verified、沒有標註;把它的主機加進內網清單之後,
        必須立刻變成 unverified_tls 並帶出句子 —— 不必有人記得回來改第二份清單。
        """
        assert transport_note("auh") == ""          # 前提
        monkeypatch.setattr(
            http_client, "INTERNAL_HOSTS",
            set(http_client.INTERNAL_HOSTS) | {"appointment.auh.org.tw"})
        assert transport_trust("auh") == TRUST_UNVERIFIED_TLS
        assert transport_note("auh") == UNVERIFIED_TLS_NOTE
        # ★而且那不只是「分類變了」★——見
        #   TestTheVerdictMatchesWhatTheFetchActuallyDoes:同一個改動也讓
        #   fetch 真的停止驗憑證。兩邊一起變,宣稱才成立。

    def test_removing_a_host_from_internal_clears_the_note(self, monkeypatch):
        """★出口★:哪天拿到院內 CA、把主機從清單移除(改成真的驗憑證),
        標示要自己消失 —— 不可以留下一句永遠洗不掉的「未驗證」。"""
        monkeypatch.setattr(
            http_client, "INTERNAL_HOSTS",
            {h for h in http_client.INTERNAL_HOSTS
             if h != "appointment.cmuh.org.tw"})
        assert transport_trust("huihe") == TRUST_VERIFIED
        assert transport_note("huihe") == ""


def test_both_stop_alert_paths_use_the_shared_note():
    """★沒有呼叫端的宣稱等於沒有宣稱★(這個模組的檔頭記著同一個教訓:
    上一版只宣告了常數而沒有生產程式碼讀它)。止掛提醒有【兩條】路徑,
    兩條都要走 `transport_note`。"""
    import ast
    p = os.path.join(os.path.dirname(__file__), "..", "src", "main.py")
    with open(p, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "transport_note"]
    assert len(calls) == 2, f"止掛提醒的兩條路徑要各一次,實際 {len(calls)}"
