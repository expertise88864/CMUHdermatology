# -*- coding: utf-8 -*-
"""[外審第四輪] 一組「UNKNOWN 必須保持 UNKNOWN」的收斂。

第四輪外審沒有新的 P1,三個新 P2 + 兩個 P3 全部是同一條不變式的殘餘:
★查不到 / 讀不到 / 寫不進去,都不等於「確定沒有」★。

  R4-P2-01  retention:目錄列舉失敗 → `glob.glob()` 吞成空清單 →
            摘要說「沒有過期檔案」(其實是連裡面有什麼都沒看到)。
  R4-P2-02  打卡:portal 系統日期解析不出來 → 沿用本機日期作答且 error=None
            → 本機時鐘差一天就對醫師誤報「未打卡」。
  R4-P2-03  SQLite:`disk I/O error` / 我們自己寫錯的 SQL 被判成「檔案損壞」
            → 走上唯一會毀掉 30 天歷史快取的那條路。
  R4-P3-03  快取:payload 全部序列化失敗 → 被讀成「這位醫師查到沒有資料」
            → DELETE 掉舊 row。
  R4-P3-02  假名:註解宣稱「不可用字典反推」,但 salt 是公開常數 —— 宣稱不成立。

另外自查到同一病灶的第四處:`main.run_retention_sweep` 只看 `res.failed`,
於是 `stat_failed`/新欄位對主程式完全隱形(背景清掃緒用的是 `res.clean`)。
"""
import importlib
import inspect
import os
import sqlite3
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import debug_privacy as dp  # noqa: E402
from cmuh_common import punch_status as ps  # noqa: E402
from cmuh_common import sqlite_cache as sc  # noqa: E402
from cmuh_common.retention import (  # noqa: E402
    RetentionRule, default_rules, sweep,
)


def _rule(directory, patterns=("*.png",), days=7.0):
    return RetentionRule("測試", str(directory), patterns, days)


def _aged(tmp_path, name, days):
    p = tmp_path / name
    p.write_text("x", encoding="utf-8")
    t = time.time() - days * 86400.0
    os.utime(p, (t, t))
    return p


# ══ R4-P2-01:列舉失敗 ≠ 目錄是空的 ═══════════════════════════════════════
class TestAnUnlistableDirectoryIsNotAnEmptyOne:
    def test_it_is_reported_instead_of_looking_clean(self, tmp_path,
                                                     monkeypatch):
        """★核心★ 目錄在、裡面有過期的 PHI 檔,但這一刻列舉不了
        (ACL/防毒/網路碟)→ 不可以回報 clean。"""
        _aged(tmp_path, "old_patient.png", 99)

        def _boom(_p=None):
            raise PermissionError(13, "Access is denied")
        monkeypatch.setattr(os, "scandir", _boom)
        res = sweep([_rule(tmp_path)])
        assert res.enumeration_failed == {"測試": 1}, res
        assert not res.clean, "★連裡面有什麼都沒看到,卻說保留期沒問題★"
        assert "列舉不了" in res.summary(), res.summary()
        assert res.deleted == {}, res

    def test_the_expired_file_is_still_on_disk(self, tmp_path, monkeypatch):
        """★這才是重點★:報告要 degraded,是因為那個過期的 PHI 檔還在。"""
        _aged(tmp_path, "old_patient.png", 99)
        real = os.scandir
        monkeypatch.setattr(
            os, "scandir",
            lambda _p=None: (_ for _ in ()).throw(PermissionError(13, "x")))
        sweep([_rule(tmp_path)])
        monkeypatch.setattr(os, "scandir", real)
        assert "old_patient.png" in os.listdir(tmp_path)

    def test_an_unreadable_directory_is_not_an_absent_one(self, tmp_path,
                                                          monkeypatch):
        """★兩種「不在」要分開★:目錄不存在是契約上的靜默跳過;
        目錄 stat 不到(ACL)則是「連存不存在都不知道」——
        `os.path.isdir()` 把兩者都吞成 False,所以不能再用它。"""
        real = os.stat
        target = str(tmp_path)

        def _spy(path, *a, **k):
            if str(path) == target:
                raise PermissionError(13, "Access is denied")
            return real(path, *a, **k)
        monkeypatch.setattr(os, "stat", _spy)
        res = sweep([_rule(tmp_path)])
        assert res.directory_failed == {"測試": 1}, res
        assert not res.clean, res
        assert "目錄狀態問不到" in res.summary(), res.summary()

    def test_a_genuinely_absent_directory_stays_silent(self, tmp_path):
        """★不可矯枉過正★:規則指向不存在的目錄是既有契約(靜默跳過),
        變成 degraded 的話每台機器都會永遠 degraded。"""
        res = sweep([_rule(tmp_path / "no_such_dir")])
        assert res.clean and res.directory_failed == {}, res
        assert res.enumeration_failed == {}, res

    def test_a_healthy_sweep_is_still_clean(self, tmp_path):
        """★對照組★:一切正常 → clean、該刪的刪掉、摘要不得出現那兩句。"""
        _aged(tmp_path, "old.png", 99)
        _aged(tmp_path, "new.png", 0.1)
        res = sweep([_rule(tmp_path)])
        assert res.clean, res
        assert res.deleted == {"測試": 1}, res
        assert "列舉不了" not in res.summary()
        assert "目錄狀態問不到" not in res.summary()


class TestTheNewMatcherKeepsGlobSemantics:
    """換掉 `glob.glob()` 之後,選檔行為必須逐位元一樣 ——
    ★一個修正必須連同它新開的可能性一起判斷★:多刪比少刪更糟。"""

    def test_a_dotfile_is_not_matched_by_a_star_pattern(self, tmp_path):
        """glob 的隱藏檔慣例:`*` 不吃開頭是 `.` 的名字。
        少了這一條,`*.before-reset-*` 會開始刪 dotfile。"""
        _aged(tmp_path, ".hidden.png", 99)
        _aged(tmp_path, "shown.png", 99)
        res = sweep([_rule(tmp_path)])
        assert res.deleted == {"測試": 1}, res
        assert ".hidden.png" in os.listdir(tmp_path), "★多刪了隱藏檔★"

    def test_a_dot_pattern_still_matches_a_dotfile(self, tmp_path):
        """對照組:樣式自己以 `.` 開頭時就該吃得到(與 glob 一致)。"""
        _aged(tmp_path, ".hidden.png", 99)
        res = sweep([_rule(tmp_path, patterns=(".*.png",))])
        assert res.deleted == {"測試": 1}, res

    def test_a_file_matching_two_patterns_is_handled_once(self, tmp_path):
        """同一個檔被兩個樣式命中時只處理一次 —— 否則第二次 `os.remove`
        會 FileNotFoundError,被記成「刪不掉」而永遠 degraded。"""
        _aged(tmp_path, "a.png", 99)
        res = sweep([_rule(tmp_path, patterns=("*.png", "a.*"))])
        assert res.deleted == {"測試": 1} and res.failed == {}, res

    def test_the_shipped_rules_are_all_flat_patterns(self):
        """★守衛★ 比對是對【單層檔名】做的:含路徑分隔字元的樣式永遠比不中,
        那會是一個安靜的保留期漏洞。本模組出貨的規則都必須是單層。"""
        rules = default_rules(os.path.join("C:", "x", "settings"))
        assert rules, "★空集合不算通過★"
        bad = [(r.label, p) for r in rules for p in r.patterns
               if "/" in p or "\\" in p]
        assert bad == [], f"這些樣式跨層、永遠比不中:{bad}"


def test_the_main_sweep_uses_the_authoritative_predicate():
    """★自查★ 判準只能有一份:`run_retention_sweep` 原本只看 `res.failed`,
    於是「年齡讀不到 / 目錄問不到」對主程式完全隱形 —— 同一份結果,
    背景清掃緒記 error、主程式記 info。"""
    main = importlib.import_module("main")
    src = inspect.getsource(main.run_retention_sweep)
    assert "if not res.clean:" in src, (
        "★主程式沒有問權威判準★ 保留期的降級狀態會對它隱形")


# ══ R4-P2-02:日期不確定 ≠ 今天沒打卡 ═════════════════════════════════════
class _Elem:
    """撐得住生產登入序列的最小元素替身(clear/send_keys 都會被呼叫)。"""

    def __init__(self, text=""):
        self.text = text

    def clear(self):
        pass

    def send_keys(self, *_a):
        pass


class _FakeDriver:
    """只夠 `read_today_swipes` 走完的最小替身(登入已成功之後那一段)。"""

    def __init__(self, systime_text):
        self._systime = systime_text
        self.rows = []
        self.login_clicks = 0

    def delete_all_cookies(self):
        pass

    def get(self, url):
        pass

    def find_element(self, by, value):
        if value == "lb_systime":
            return _Elem(self._systime)
        return _Elem("")

    def execute_script(self, script, *a):
        if "Gv_attppre" in script:
            return self.rows
        self.login_clicks += 1
        return None


@pytest.fixture
def _selenium_stub(monkeypatch):
    """★不可以真的碰 selenium/portal★:把 read_today_swipes 需要的等待與
    元素查找換成本地替身,只留下「日期能不能確認」這件事可觀測。"""
    pytest.importorskip("selenium")
    import selenium.webdriver.support.ui as _ui

    class _Wait:
        def __init__(self, driver, timeout):
            self._d = driver

        def until(self, _cond):
            return _Elem()
    monkeypatch.setattr(_ui, "WebDriverWait", _Wait)
    import selenium.webdriver.support.expected_conditions as _ec
    for cond in ("element_to_be_clickable", "presence_of_element_located",
                 "visibility_of_element_located"):
        monkeypatch.setattr(_ec, cond, lambda *_a: None)
    # 生產路徑有一個 `_time.sleep(0.5)`(等 PostBack)—— 測試不必真的等。
    monkeypatch.setattr(ps._time, "sleep", lambda _s: None)
    return monkeypatch


class TestAnUnverifiableSystemDateIsNotAnAnswer:
    def test_it_reports_a_query_failure_instead_of_no_punch(
            self, _selenium_stub, caplog):
        """★核心★ portal 日期解析不出來(改版/空字串)→ 不可以拿本機日期
        去挑「今日」的列然後宣稱查詢成功。那個答案會變成假的「未打卡」。"""
        drv = _FakeDriver("2026-09-02")        # 不含「年」→ 舊版會靜默沿用本機日期
        drv.rows = [["1150902", "0801", "上班"]]
        with caplog.at_level("ERROR"):
            swipes, err = ps.read_today_swipes(drv, "u", "p")
        assert swipes == []
        assert err, "★仍然宣稱查詢成功★ 呼叫端會把它當成確定沒打卡"
        assert "無法確認" in str(err)
        assert any("無法確認打卡系統日期" in r.getMessage()
                   for r in caplog.records)

    def test_that_failure_is_not_retried(self, _selenium_stub):
        """★重登不會讓版面變得解析得出來★:判成可重試會讓整批的時間預算
        少一半,後面的帳號變成「查詢逾時(略過)」。"""
        drv = _FakeDriver("")
        _swipes, err = ps.read_today_swipes(drv, "u", "p")
        assert ps._is_retryable_punch_error(err) is False

    def test_a_verifiable_date_still_answers(self, _selenium_stub):
        """★對照組★ 日期讀得到就照常作答 —— 不可以把正常路徑一起關掉
        (那會讓「早上還沒打卡」這個最該顯示的訊號變成查詢失敗)。"""
        drv = _FakeDriver("115年09月02日 08:00:00")
        drv.rows = [["1150902", "0801", "上班"], ["1150901", "0803", "上班"]]
        swipes, err = ps.read_today_swipes(drv, "u", "p")
        assert err is None, err
        assert swipes == [("0801", "上班")], swipes


class TestRetryPolicyReadsDataNotProse:
    def test_our_own_terminal_errors_are_not_retried(self):
        e = ps.PunchError("任何說法", ps.PUNCH_ERR_TERMINAL)
        assert ps._is_retryable_punch_error(e) is False

    def test_our_own_transient_errors_are_retried(self):
        e = ps.PunchError("密碼錯誤", ps.PUNCH_ERR_TRANSIENT)
        assert ps._is_retryable_punch_error(e) is True, (
            "★帶 kind 時就不該再看字面★")

    def test_the_error_still_displays_exactly_like_a_string(self):
        """★顯示端契約不可變★:會診信件是 f-string/escape 它,
        `PunchError` 必須就是個 str。"""
        e = ps.PunchError("登入逾時/失敗", ps.PUNCH_ERR_TRANSIENT)
        assert isinstance(e, str)
        assert f"查詢失敗（{e}）" == "查詢失敗（登入逾時/失敗）"
        assert bool(e) is True

    def test_portal_prose_still_falls_back_to_the_old_rule(self):
        """沒有 kind 的(=portal 自己吐的 Alert 文字)維持既有判準 ——
        那是外部人話,這一批不假裝把它變成資料。"""
        assert ps._is_retryable_punch_error("帳號或密碼錯誤") is False
        assert ps._is_retryable_punch_error("系統忙碌請稍後") is True


# ══ R4-P2-03 / R4-P3-03:用不了 ≠ 壞掉;寫不進 ≠ 沒有 ═══════════════════
class TestOnlyProvenCorruptionMayDestroyTheCache:
    def test_device_problems_are_not_corruption(self):
        for msg in ("disk I/O error", "database or disk is full",
                    "attempt to write a readonly database",
                    "unable to open database file"):
            assert sc._is_corruption_error(
                sqlite3.OperationalError(msg)) is False, msg

    def test_our_own_sql_bug_is_not_corruption(self):
        """★我們自己寫錯 SQL 不可以毀掉使用者 30 天的門診人數★"""
        assert sc._is_corruption_error(
            sqlite3.ProgrammingError("no such column: nope")) is False

    def test_engine_confirmed_corruption_still_is(self):
        """★對照組★:引擎親口說 image 壞了,自我修復要照常運作。"""
        assert sc._is_corruption_error(
            sqlite3.DatabaseError("database disk image is malformed")) is True
        assert sc._is_corruption_error(
            sqlite3.DatabaseError("file is not a database")) is True


class TestASerializationFailureIsNotAConfirmedEmpty:
    def _fresh_db(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sc, "_db_path", lambda: str(tmp_path / "c.sqlite"))
        monkeypatch.setattr(sc, "_initialized", False, raising=False)
        monkeypatch.setattr(sc, "_conn", None, raising=False)

    def test_it_keeps_the_previous_rows(self, tmp_path, monkeypatch, caplog):
        """★核心★ 這位醫師的 payload 全部序列化失敗 → rows 空 →
        舊版會把它當成「查到沒有門診」而 DELETE 掉整段歷史。"""
        self._fresh_db(tmp_path, monkeypatch)
        sc.save_clinic_counts({"D1": {"2026-09-01": [{"count": 3}]}},
                              only_doctor_no="D1")
        assert sc.load_clinic_counts().get("D1"), "前提:先有一份舊快取"

        class _Unserializable:
            pass
        with caplog.at_level("WARNING"):
            sc.save_clinic_counts({"D1": {"2026-09-02": _Unserializable()}},
                                  only_doctor_no="D1")
        assert sc.load_clinic_counts().get("D1"), (
            "★『寫不進去』被記成『查到沒有』→ 舊快取被清掉★")
        assert any("不清舊 row" in r.getMessage() for r in caplog.records)

    def test_a_confirmed_empty_result_still_clears(self, tmp_path,
                                                   monkeypatch):
        """★對照組★ 明確查到「這位醫師沒有門診」時,舊 row 仍然要清掉 ——
        否則過期的人數會一直留在畫面上(這是 only_doctor_no 的既有契約)。"""
        self._fresh_db(tmp_path, monkeypatch)
        sc.save_clinic_counts({"D1": {"2026-09-01": [{"count": 3}]}},
                              only_doctor_no="D1")
        sc.save_clinic_counts({"D1": {}}, only_doctor_no="D1")
        assert not sc.load_clinic_counts().get("D1"), (
            "確定沒有門診時該清掉,不可以矯枉過正")


# ══ R4-P3-02:宣稱只能講到做得到的程度 ════════════════════════════════════
class TestThePseudonymClaimIsHonest:
    def test_the_dictionary_attack_actually_works(self):
        """★用做的證明那句宣稱是假的★(不是用字串比對證明)。

        salt 是公開 repo 裡的固定常數 → 任何人拿一份候選帳號清單,
        本機算 `SHA256(salt + 候選)[:8]` 就能把代號還原成帳號。
        院內帳號的候選空間又小(員工代號)。
        這條測試就是那個攻擊本身 —— 它會過,所以「不可反推」不成立。
        """
        import hashlib
        secret_account = "d15728"
        tag = dp.account_tag(secret_account)
        candidates = ["d00001", "d15727", secret_account, "d99999"]
        recovered = [c for c in candidates
                     if hashlib.sha256((dp._ACCOUNT_SALT + c).encode("utf-8"))
                     .hexdigest()[:dp.ACCOUNT_TAG_LEN] == tag]
        assert recovered == [secret_account], (
            "如果這裡對不上,代表 tag 的算法改了 —— 連帶要重寫下面那條宣稱")

    def test_the_public_contract_calls_it_a_pseudonym(self):
        """對外的那份契約(docstring)要說出它★實際是什麼★ ——
        下一個人會照它決定「這個代號可不可以外流」。

        ★這裡刻意不用「某個字不可以出現」當判準★:上面那條更正把舊句子
        引述進來解釋為什麼它是錯的,而「否認一句話」與「主張一句話」含有
        同樣的字 —— 用子字串分不出來(我第一版就是這樣紅的)。
        真正的守衛是上面那條★示範攻擊★:算法一改它就會紅,
        逼人連同這段措辭一起重寫。
        """
        doc = dp.account_tag.__doc__ or ""
        assert "假名" in doc, "要說出它實際是什麼(穩定假名)"

    def test_the_behaviour_itself_is_unchanged(self):
        """這一批只更正宣稱,不動行為(改算法會讓既有檔名分不到同一群)。"""
        assert dp.account_tag("A123") == dp.account_tag("a123")
        assert "A123" not in dp.account_tag("A123")
        assert len(dp.account_tag("A123")) == dp.ACCOUNT_TAG_LEN
