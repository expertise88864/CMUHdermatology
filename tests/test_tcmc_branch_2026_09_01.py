# -*- coding: utf-8 -*-
"""[2026-09-01 使用者] 老人醫院(臺中市立老人復健綜合醫院)門診接進總覽表。

★情境★ 張廖年峰醫師的門診移到老人醫院(週三上午),★本院已經沒有他的門診★。
主院 reg52 對他回不出任何診次 —— 不接這條來源的話,他在總覽表上整個消失。
老人醫院官網的掛號頁嵌的就是同一套 reg52 CGI(iframe src),只換 `tcmc/`
這個分院路徑,所以沿用既有分院管線:抓取→解析→合併→月曆列
「張廖年峰(老人醫院) 12人」。

★誠實揭露:版型未經實機驗證★ 寫這批時本機連不到院方主機
(appointment.cmuh.org.tw → 61.66.117.10,院外不可達),沒能先抓一份真的
HTML 對版。判斷依據是「同主機、同一支 CGI 家族,只差分院路徑」——
東區 fh1 / 惠和 wh1 / 惠盛 hs1 三家都是同一個版型。★所以這裡特別測失敗
方向★:版型不合時不可以顯示錯的人數,而且要留下查得到的線索。
"""
import importlib
import os
import queue
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from bs4 import BeautifulSoup  # noqa: E402

from cmuh_common import fetch_resilience as fr  # noqa: E402
from cmuh_common import http_client  # noqa: E402
from cmuh_common import reg52_fetch as rf  # noqa: E402
from cmuh_common.appt_utils import (  # noqa: E402
    _appt_dict_ext_branch, _calendar_branch_sort_rank,
)
from cmuh_common.reg52_branch_policy import _should_fetch_tcmc_reg52  # noqa: E402
from cmuh_common.reg52_parse import (  # noqa: E402
    parse_appt_item_for_alert, parse_doctor_info_dayoff, parse_tcmc_schedule,
)

main = importlib.import_module("main")

DOC = "張廖年峰"
DOC_NO = "D15728"

#: 老人醫院週表(fh 版型:無 table.schedule、診別含全形空格、visitDate + 已掛號)
TCMC_HTML = ("<html><body><table>"
             "<tr><td>上 午</td>"
             "<td>(101診)<div class='visitDate'><b>115/09/02</b>"
             " 已掛號：12</div></td>"
             "</tr></table></body></html>" + "。" * 600)

#: 抓得到頁面、卻不是掛號表版型(維護頁/改版)
TCMC_NOT_A_SCHEDULE = "<html><body><p>系統維護中</p></body></html>" + "。" * 600


@pytest.fixture(autouse=True)
def _isolate_resilience_state():
    """★熔斷/退避是模組層狀態★:一條測試記下的失敗會讓下一條根本不送請求
    (沿用既有 reg52 測試的做法)。"""
    fr.reset_all()
    yield
    fr.reset_all()


class _Resp:
    def __init__(self, text, status=200):
        self.text = text
        self.encoding = "big5"
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(str(self.status_code))


class _RecordingSession:
    """記下每一次請求的 URL 與 verify —— 這批要問的正是「打去哪、驗不驗」。"""

    def __init__(self, text=TCMC_HTML):
        self.calls = []
        self._text = text

    def get(self, url, **kw):
        self.calls.append((url, kw.get("verify")))
        return _Resp(self._text)


# ── 抓取層:打對主機、循對政策 ────────────────────────────────────────────
class TestTheFetchGoesToTheGeriatricHospital:
    def test_it_hits_the_tcmc_path_not_another_branch(self):
        sess = _RecordingSession()
        html = rf._fetch_tcmc_reg52_html(sess, DOC_NO, DOC)
        assert html, "版型合格的頁面應該被採用"
        assert sess.calls, "★根本沒有送出請求★"
        url = sess.calls[0][0]
        assert "/cgi-bin/tcmc/reg52.cgi" in url, url
        assert "DocNo=" + DOC_NO in url, url
        # ★不可以打到別家★(同網域、只差路徑,抄錯常數不會有任何症狀)
        assert "/wh1/" not in url and "/fh1/" not in url and "/hs1/" not in url

    def test_it_follows_the_production_verify_policy(self):
        """★憑證驗不驗要問生產的那一支★(R3-P2-02 的契約):
        院內主機清單改了,這裡要跟著改,不可以另抄一份。"""
        sess = _RecordingSession()
        rf._fetch_tcmc_reg52_html(sess, DOC_NO, DOC)
        url, verify = sess.calls[0]
        assert verify == rf.verify_policy(url)
        assert verify is False, "appointment.cmuh.org.tw 是院內主機 → 不驗憑證"

    def test_a_maintenance_page_is_a_failure_not_a_schedule(self):
        """★HTTP 200 ≠ 這是掛號表★:維護頁不可以被當成「他今天沒診」。"""
        sess = _RecordingSession(TCMC_NOT_A_SCHEDULE)
        assert rf._fetch_tcmc_reg52_html(sess, DOC_NO, DOC) is None
        ok, _remain = fr._source_backoff_allow("tcmc:" + DOC_NO)
        assert ok is False, "★語意失敗沒有記退避★ 每一輪都會再打同一個壞頁"

    def test_the_backoff_key_is_its_own(self):
        """★退避鍵不可以跟惠和共用★(同網域最容易寫錯):
        老人醫院壞掉不該把惠和一起擋住。"""
        sess = _RecordingSession(TCMC_NOT_A_SCHEDULE)
        rf._fetch_tcmc_reg52_html(sess, DOC_NO, DOC)
        assert fr._source_backoff_allow("huihe:" + DOC_NO)[0] is True
        assert fr._source_backoff_allow("tcmc:" + DOC_NO)[0] is False


class TestOnlyTheRightDoctorIsFetched:
    def test_the_relocated_doctor_is_in_the_list(self):
        assert _should_fetch_tcmc_reg52(DOC) is True

    def test_nobody_else_triggers_an_extra_request(self):
        """★每多一位就是每輪多一次對外請求★:名單要剛好。"""
        for other in ("吳伯元", "蔡李澄", "謝佳陵", "沈冠宇", ""):
            assert _should_fetch_tcmc_reg52(other) is False, other


# ── provenance:標示要由實際行為推導 ──────────────────────────────────────
class TestTheProvenanceLabelIsDerivedNotHardcoded:
    def test_the_new_branch_is_unverified_tls(self):
        """同網域=院內主機清單裡=不驗憑證 → 要跟惠和一樣被標註。"""
        assert rf.transport_trust("tcmc") == rf.TRUST_UNVERIFIED_TLS
        assert rf.transport_note("tcmc") == rf.UNVERIFIED_TLS_NOTE

    def test_it_follows_the_internal_host_list(self, monkeypatch):
        """★證明沒有第二份清單★:拿到院內 CA、主機移出 INTERNAL_HOSTS 之後,
        標示要自己消失 —— 不是靠有人記得回來改這裡。"""
        monkeypatch.setattr(http_client, "INTERNAL_HOSTS", frozenset())
        assert rf.transport_trust("tcmc") == rf.TRUST_VERIFIED
        assert rf.transport_note("tcmc") == ""


# ── 解析層 ──────────────────────────────────────────────────────────────
class TestTheScheduleParsesAsTheGeriatricBranch:
    def test_a_weekly_table_yields_tcmc_rows(self):
        import datetime
        parsed = parse_tcmc_schedule(BeautifulSoup(TCMC_HTML, "lxml"))
        wed = datetime.date(2026, 9, 2)
        assert wed in parsed, parsed
        assert wed.weekday() == 2, "前提:115/09/02 是週三"
        row = parsed[wed][0]
        assert row["session"] == "上午"
        assert row["count"] == 12
        assert row["ext_branch"] == "tcmc"

    def test_a_dayoff_row_keeps_the_branch(self):
        """★休診覆蓋只蓋同院區的列★:branch 標錯會讓老人醫院的休診
        去蓋掉別家(或蓋不掉自己)。"""
        html = ("<html><body><table id='dayoff'>"
                "<tr><th>日期</th><th>診別</th><th>代診</th></tr>"
                "<tr><td>115/09/16</td><td>上午</td><td>休診</td></tr>"
                "</table></body></html>")
        parsed = parse_doctor_info_dayoff(BeautifulSoup(html, "lxml"),
                                          assume_tcmc_branch=True)
        assert [r["ext_branch"] for rows in parsed.values() for r in rows] \
            == ["tcmc"]

    def test_the_legacy_string_form_still_carries_the_branch(self):
        """舊字串格式(磁碟快取)也要認得,否則重開程式後這一列會退化成主院列。"""
        got = parse_appt_item_for_alert("上午:12人|Ext:tcmc|Rm:101診")
        assert got is not None
        assert got[3] == "tcmc", got


# ── 共用工具/顯示 ───────────────────────────────────────────────────────
class TestTheOverviewRowLooksRight:
    def test_the_dict_is_recognised_as_a_branch_row(self):
        assert _appt_dict_ext_branch({"ext_branch": "tcmc"}) == "tcmc"

    def test_the_suffix_is_the_one_the_user_asked_for(self):
        assert main._EXT_BRANCH_DISPLAY_SUFFIX["tcmc"] == "(老人醫院)"

    def test_every_known_branch_has_a_display_suffix(self):
        """★通用守衛★:凡是抓得到的分院都必須有顯示標籤 ——
        少一個就會在總覽上變成一列沒有院區、看起來像本院的診
        (下一位新增分院的人不必記得回來改這裡)。"""
        known = [b for b in ("east", "auh", "huihe", "huisheng", "tcmc")
                 if rf.branch_url(b)]
        assert known, "★空集合不算通過★"
        missing = [b for b in known
                   if not main._EXT_BRANCH_DISPLAY_SUFFIX.get(b)]
        assert missing == [], "這些分院沒有顯示後綴:" + str(missing)

    def test_it_sorts_after_every_other_branch(self):
        """★[2026-09-02 使用者] 改成排在所有外院的最後一個★

        原本它排在「未知分院」之前;使用者要求它固定墊底 —— 日後新增分院
        (會落在未知那一格)也不可以把它擠到中間。
        """
        order = [_calendar_branch_sort_rank(b)
                 for b in ("east", "auh", "huihe", "huisheng", "tcmc")]
        assert order == sorted(order) and len(set(order)) == 5, order
        assert _calendar_branch_sort_rank("tcmc") \
            > _calendar_branch_sort_rank("something_new")


# ── ★接上去了嗎★:走生產的那條主路徑 ────────────────────────────────────
#: 主院頁:★版面完整、但一個診次都沒有★ —— 這正是「本院已經沒有他的門診」
#:   的實際形狀(契約層看的是版面在不在,不是有沒有診;見 classify_main_html)。
EMPTY_MAIN_HTML = ("<html><body><table class='schedule'>"
                   "<tr><td class='timeSlot'>上午</td>"
                   "<td class='schBox'>&nbsp;</td></tr>"
                   "</table></body></html>" + "。" * 600)


class _MainSession:
    def get(self, url, **kw):
        return _Resp(EMPTY_MAIN_HTML)


@pytest.fixture
def wired(monkeypatch):
    """把外部世界換掉,只留「老人醫院這條來源有沒有真的被走到」可觀測。

    ★測試絕對不可以碰網路★(2026-08-09 的 CI 紅/本機綠):其他三家分院與
    亞大的述詞一律關掉,老人醫院那支則換成回傳 fixture HTML 的樁。
    """
    seen = {"ui": [], "tcmc_calls": []}
    monkeypatch.setattr(main, "requests", importlib.import_module("requests"))
    monkeypatch.setattr(main, "BeautifulSoup", BeautifulSoup)
    monkeypatch.setattr(main, "_get_thread_local_reg52_session", _MainSession)
    monkeypatch.setattr(main, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(main, "_cache_set", lambda *a, **k: None)
    monkeypatch.setattr(main, "_parse_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(main, "_parse_cache_set", lambda *a, **k: None)
    monkeypatch.setattr(main, "_source_backoff_allow", lambda k: (True, 0.0))
    monkeypatch.setattr(main, "_source_throttle_allow",
                        lambda k, i=0: (True, 0.0))
    monkeypatch.setattr(main, "_source_backoff_fail", lambda k, *a: (1.0, 1))
    monkeypatch.setattr(main, "_source_backoff_success", lambda k: None)
    monkeypatch.setattr(main, "_reg52_stale_fallback", lambda *a, **k: "")
    # 「查無任何可用門診資料」那條路會重試三次、每次 sleep(2/4/6) —— 反向的
    # 那幾條測試本來就會走它,不必真的等 12 秒(monkeypatch 會還原)。
    monkeypatch.setattr(main.time, "sleep", lambda _s: None)
    # ★把【所有】對外抓取的述詞關掉,只留老人醫院那條(它的抓取已換成樁)★
    #   逐一列名的話,哪天多一個分院,這個 fixture 就會安靜地開一道網路門 ——
    #   本機看不出來,CI 上變成對醫院主機發真實請求(2026-08-09 的教訓)。
    #   `check_appointment_count` 裡的入口 AST 守衛在
    #   test_main_local_fault_2026_08_09 那支,這裡只要保證關得乾淨。
    closed = [n for n in dir(main)
              if n.startswith("_should_fetch_") and n != "_should_fetch_tcmc_reg52"]
    assert closed, "★空集合不算通過★ 述詞的命名慣例變了,這裡就沒關到任何東西"
    for pred in closed:
        monkeypatch.setattr(main, pred, lambda *a, **k: False)
    monkeypatch.setattr(main, "AUH_DOCTOR_DOCNO_MAP", {})
    monkeypatch.setattr(main, "put_ui_message",
                        lambda q, m, *a, **k: seen["ui"].append(m))

    seen["html"] = TCMC_HTML

    def _fake_tcmc(session, doc_no, doctor_name):
        seen["tcmc_calls"].append((doc_no, doctor_name))
        return seen["html"]

    seen["set_html"] = lambda h: seen.__setitem__("html", h)
    monkeypatch.setattr(main, "_fetch_tcmc_reg52_html", _fake_tcmc)
    return seen


def _run(doctor=DOC, doc_no=DOC_NO):
    main.check_appointment_count(queue.Queue(),
                                 {"name": doctor, "doc_no": doc_no})


def _clinic_rows(seen):
    """送給 UI 的門診資料裡的所有掛號列。"""
    rows = []
    for msg in seen["ui"]:
        data = getattr(msg, "data", None)
        if isinstance(data, dict) and "error" not in data:
            for items in data.values():
                rows.extend(i for i in items if isinstance(i, dict))
    return rows


class TestItIsActuallyWiredIntoTheProductionPath:
    def test_the_relocated_doctor_still_shows_up_with_a_count(self, wired):
        """★本案的核心★:本院一個診次都沒有了,總覽仍然要有
        「張廖年峰(老人醫院) 12人」這一列。

        ★這條測試走的是生產的 `check_appointment_count`★ —— 不是
        「各層零件都對」而已(那正是 2026-08 一再出現的『測試全綠、
        功能沒產出』的形狀)。"""
        _run()
        assert wired["tcmc_calls"] == [(DOC_NO, DOC)], (
            "★老人醫院那條來源根本沒有被走到★:" + str(wired["tcmc_calls"]))
        rows = _clinic_rows(wired)
        tcmc_rows = [r for r in rows if r.get("ext_branch") == "tcmc"]
        assert tcmc_rows, "★抓到了卻沒有併進送給 UI 的資料★:" + str(rows)
        assert tcmc_rows[0]["count"] == 12
        assert tcmc_rows[0]["session"] == "上午"
        # 顯示層把它組成使用者要的那一列
        label = DOC + main._EXT_BRANCH_DISPLAY_SUFFIX[tcmc_rows[0]["ext_branch"]]
        assert label == "張廖年峰(老人醫院)"

    def test_nobody_else_pays_for_this(self, wired):
        """★名單以外的醫師不可以多打一次請求★(每輪、每位都是成本)。"""
        _run(doctor="吳伯元", doc_no="D15645")
        assert wired["tcmc_calls"] == [], wired["tcmc_calls"]

    def test_a_wrong_layout_shows_nothing_and_says_why(self, wired, caplog):
        """★版型沒有實機驗證過 → 失敗方向要安全而且看得見★
        抓得到頁面卻不是掛號表版型時:①不可以生出任何一列(寧可沒有,
        不可以顯示錯的人數);②要留下一句話 —— 否則使用者看到的是
        「醫師不見了」,而查的人沒有任何線索。"""
        import logging
        wired["set_html"](TCMC_NOT_A_SCHEDULE)
        with caplog.at_level(logging.WARNING):
            _run()
        assert [r for r in _clinic_rows(wired)
                if r.get("ext_branch") == "tcmc"] == []
        assert any("老人醫院" in r.getMessage() for r in caplog.records), (
            "★版型不合卻一聲不吭★ 使用者只會看到醫師從總覽消失")
