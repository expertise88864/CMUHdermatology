# -*- coding: utf-8 -*-
"""批次 CQ-BA:會診查詢的備用 HIS 帳號(2026-08-24 使用者定案)。

規則(每一條下面都有一個【只靠它分勝負】的反例):
  1. 只有 `LoginNotCompleted`(帳密送出去了、登入沒完成)才算一次「帳密被拒」;
  2. 主帳號★當天★連續三次才切換,兩次不切;
  3. 備用帳密要兩欄都有,半套設定不算(否則會拿空密碼再去失敗一次);
  4. 一次帳密送出至多算一次(同一個失敗會被好幾層看到);
  5. 沒送出帳密的失敗不算;
  6. 出口是【隔天】:過 00:00 自動切回主帳號、次數歸零;
  7. 切換★不重置登入冷卻★;
  8. 狀態跨重啟(自動更新/watchdog 重啟不該讓次數歸零),而且三個鍵是一組;
  9. 信件標頭寫的是【這一次真的送出去的那一組】,而且★永遠不含密碼★。
"""
import ast
import inspect
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
import consult_query as cq  # noqa: E402

PRIMARY = {"username": "101358", "password": "pw-primary"}
BOTH = dict(PRIMARY, backup_username="999999", backup_password="pw-backup")


@pytest.fixture(autouse=True)
def _clean_account_state(monkeypatch, tmp_path):
    """每個測試從乾淨的帳號狀態開始,而且★不碰真正的狀態檔★。"""
    monkeypatch.setattr(cq, "_his_account_day", "", raising=False)
    monkeypatch.setattr(cq, "_his_primary_fails", 0, raising=False)
    monkeypatch.setattr(cq, "_his_using_backup", False, raising=False)
    monkeypatch.setattr(cq, "_his_last_login_account", None, raising=False)
    monkeypatch.setattr(cq, "_his_login_attempt", 0, raising=False)
    monkeypatch.setattr(cq, "_job_fail_state_path",
                        lambda: str(tmp_path / "state.json"))
    def _boom():
        raise AssertionError("★錯誤路徑不可以去讀設定檔★:讀檔失敗會把已經"
                             "分類好的 LoginNotCompleted 換成別的例外")
    monkeypatch.setattr(cq, "load_config", _boom)
    yield


def _reject_once(cfg=None, *, user="101358", backup=False):
    """模擬「送出一次帳密 → 被拒」(token 化:結果只消費自己那一顆)。"""
    tok = cq._note_his_credentials_sent(user, backup)
    cq._note_his_login_rejected(cfg, tok)


# ══ 1. 只有 LoginNotCompleted 才算 ═══════════════════════════════════════
class TestOnlyARejectedPasswordCounts:
    """★計數點就在「登入視窗還在」那一個判準上★ —— 反例走的是真正的
    `_wait_main_window_after_login`,不是另外寫一個假的分類器。"""

    @staticmethod
    def _wait_ok(monkeypatch, token=None):
        """走真正的成功出口(主畫面出現且 enabled)。"""
        monkeypatch.setattr(cq, "find_windows",
                            lambda *a, **k: [777])
        monkeypatch.setattr(cq.win32gui, "IsWindowEnabled", lambda h: True)
        return cq._wait_main_window_after_login(set(), visible_only=True,
                                                timeout_sec=5.0,
                                                cfg=dict(BOTH),
                                                login_token=token)

    @staticmethod
    def _wait(monkeypatch, *, bde, login_still_there):
        monkeypatch.setattr(cq, "detect_bde_startup_error",
                            lambda pids: "BDE-1" if bde else None)
        monkeypatch.setattr(
            cq, "_bde_blocked",
            lambda *a, **k: cq.HISStartupBlocked("BDE 起不來"))
        monkeypatch.setattr(cq, "_describe_windows_for_diag",
                            lambda *a, **k: "")
        monkeypatch.setattr(
            cq, "find_windows",
            lambda *a, **k: [123] if login_still_there else [])
        tok = cq._note_his_credentials_sent("101358", False)
        with pytest.raises(Exception) as ei:
            cq._wait_main_window_after_login(set(), visible_only=True,
                                             timeout_sec=0.0, cfg=dict(BOTH),
                                             login_token=tok)
        return ei.value

    def test_login_not_completed_counts(self, monkeypatch):
        err = self._wait(monkeypatch, bde=False, login_still_there=True)
        assert isinstance(err, cq.LoginNotCompleted)
        assert cq._his_primary_fails == 1

    def test_the_his_startup_block_does_not_count(self, monkeypatch):
        """★BDE 起不來與帳密無關★:那台電腦的環境壞了,換一組帳號去送
        只是讓第二組也一起走進註定失敗的流程 —— 使用者定案「只在
        LoginNotCompleted 才用備用帳號」。

        ★反例只靠這條規則分勝負★:帳密【已經送出去了】(所以嘗試序號有動,
        「沒送帳密不算」那條規則擋不住它),差別只在失敗的種類。
        """
        err = self._wait(monkeypatch, bde=True, login_still_there=True)
        assert isinstance(err, cq.HISStartupBlocked)
        assert cq._his_primary_fails == 0, "BDE 起不來被當成帳密被拒"

    def test_a_generic_timeout_does_not_count(self, monkeypatch):
        """登入視窗不在了、主畫面也沒出現 = 另一種失敗(通用 RuntimeError)。"""
        err = self._wait(monkeypatch, bde=False, login_still_there=False)
        assert type(err) is RuntimeError
        assert cq._his_primary_fails == 0


class TestASuccessBreaksTheStreak:
    """★使用者說的是【連續】三次★ —— 中間成功一次就把連續打斷了。
    HIS 偶發不穩(早上失敗、中午成功、下午再失敗兩次)不該被算成「帳密不能用」。
    """

    def test_a_successful_login_resets_the_count(self, monkeypatch):
        for _ in range(2):
            _reject_once(BOTH)
        assert cq._his_primary_fails == 2
        tok = cq._note_his_credentials_sent("101358", False)
        assert TestOnlyARejectedPasswordCounts._wait_ok(monkeypatch,
                                                        tok) == 777
        assert cq._his_primary_fails == 0
        # 打斷之後要重新數三次
        for _ in range(2):
            _reject_once(BOTH)
        assert cq._his_using_backup is False
        _reject_once(BOTH)
        assert cq._his_using_backup is True

    def test_a_success_does_not_bring_the_backup_back(self):
        """出口是隔天,不是「備用成功了就切回去」(使用者定案)。"""
        for _ in range(3):
            _reject_once(BOTH)
        tok = cq._note_his_credentials_sent("999999", True)
        cq._note_his_login_succeeded(tok)
        assert cq._his_using_backup is True
        assert cq._his_primary_fails == 3

    def test_a_success_with_no_credential_send_changes_nothing(self):
        """沒送帳密就沒有新的證據(重用既有 session 不代表帳密剛被接受)。"""
        for _ in range(2):
            _reject_once(BOTH)
        cq._note_his_login_succeeded()
        assert cq._his_primary_fails == 2


# ══ 2~5. 切換的判準 ═════════════════════════════════════════════════════
class TestTheSwitchToTheBackupAccount:
    def test_two_failures_are_not_enough(self):
        for _ in range(2):
            _reject_once(BOTH)
        assert cq._his_using_backup is False
        assert cq._current_his_account(BOTH)[0] == "101358"

    def test_the_third_failure_switches(self):
        for _ in range(3):
            _reject_once(BOTH)
        assert cq._his_using_backup is True
        user, pw, is_backup, _day = cq._current_his_account(BOTH)
        assert (user, pw, is_backup) == ("999999", "pw-backup", True)

    def test_a_half_configured_backup_is_never_used(self):
        """★半套設定不算★:只填代號沒填密碼的話,切過去等於拿一組空密碼
        再去失敗一次 —— 比不切還糟。"""
        half = dict(PRIMARY, backup_username="999999", backup_password="")
        for _ in range(3):
            _reject_once(half)
        assert cq._his_using_backup is False
        assert cq._current_his_account(half)[0] == "101358"

    def test_one_credential_send_counts_at_most_once(self):
        """★同一次失敗會被好幾層看到★(冷啟動分類器、上層 handler、job 迴圈)。
        使用者要數的是「帳密被拒幾次」,不是「幾個 handler 看到它」。

        ★反例只靠這條規則分勝負★:帳密確實送出去了一次(所以不是「沒送帳密
        不算」那條),而且是 LoginNotCompleted(所以不是失敗種類那條)。
        """
        tok = cq._note_his_credentials_sent("101358", False)
        for _ in range(3):
            cq._note_his_login_rejected(BOTH, tok)
        assert cq._his_primary_fails == 1
        assert cq._his_using_backup is False

    def test_a_failure_with_no_credential_send_does_not_count(self):
        """帳密根本沒送出去(例:找不到輸入框、視窗不是我們的)→ 不是證據。"""
        for _ in range(3):
            cq._note_his_login_rejected(BOTH)
        assert cq._his_primary_fails == 0
        assert cq._his_using_backup is False

    def test_it_never_reads_the_config_file_on_the_error_path(self):
        """★錯誤路徑不可以再去讀設定檔★:讀檔失敗會把一個已經分類好的
        `LoginNotCompleted` 換成別的例外 —— 那樣登入冷卻就不會被設,3 分鐘
        節奏又會再送一次帳密,正好是冷卻要防的那件事(fixture 裡的
        `load_config` 一被呼叫就會爆炸)。
        """
        tok = cq._note_his_credentials_sent("101358", False)
        cq._note_his_login_rejected(token=tok)   # 沒有 cfg 也不可以去讀檔
        assert cq._his_primary_fails == 1

    def test_the_backup_being_rejected_does_not_switch_back(self):
        """備用也被拒 → 不來回切(出口是隔天);主帳號的次數也不再往上加。"""
        for _ in range(3):
            _reject_once(BOTH)
        _reject_once(BOTH, user="999999", backup=True)
        assert cq._his_using_backup is True
        assert cq._his_primary_fails == 3


# ══ 6. 出口:隔天 ════════════════════════════════════════════════════════
class TestTheExitIsTheNextDay:
    def test_the_next_day_returns_to_the_primary_account(self, monkeypatch):
        for _ in range(3):
            _reject_once(BOTH)
        assert cq._his_using_backup is True
        monkeypatch.setattr(cq, "_his_today", lambda: "2999-12-31")
        user, pw, is_backup, _day = cq._current_his_account(BOTH)
        assert (user, is_backup) == ("101358", False), "換日沒有切回主帳號"
        assert pw == "pw-primary"
        assert cq._his_primary_fails == 0, "換日沒有把失敗次數歸零"

    def test_the_same_day_keeps_the_backup(self, monkeypatch):
        """★反例要分得開「換日」與「每次呼叫都歸零」★:同一天內重複詢問
        不可以把狀態沖掉,否則三次永遠湊不齊。"""
        monkeypatch.setattr(cq, "_his_today", lambda: "2026-08-24")
        for _ in range(3):
            _reject_once(BOTH)
        for _ in range(5):
            assert cq._current_his_account(BOTH)[2] is True
        assert cq._his_primary_fails == 3

    def test_three_more_failures_switch_again_the_next_day(self, monkeypatch):
        """使用者定案的完整循環:隔天回主帳號,當天再失敗三次才又切過去。"""
        monkeypatch.setattr(cq, "_his_today", lambda: "2026-08-24")
        for _ in range(3):
            _reject_once(BOTH)
        monkeypatch.setattr(cq, "_his_today", lambda: "2026-08-25")
        assert cq._current_his_account(BOTH)[2] is False
        for _ in range(2):
            _reject_once(BOTH)
        assert cq._current_his_account(BOTH)[2] is False
        _reject_once(BOTH)
        assert cq._current_his_account(BOTH)[2] is True


# ══ 7. 切換不是繞過冷卻的旁路 ═══════════════════════════════════════════
def test_switching_accounts_does_not_reset_the_login_cooldown(monkeypatch):
    """★冷卻的用意是「同一段時間內不要對院方密集送帳密」★ —— 換一組帳號送
    並不會讓那件事變得安全。使用者定案:備用帳號等這一輪冷卻結束才上場。"""
    monkeypatch.setattr(cq, "_login_cooldown_until", 12345.0, raising=False)
    for _ in range(3):
        _reject_once(BOTH)
    assert cq._his_using_backup is True
    assert cq._login_cooldown_until == 12345.0, "換帳號把登入冷卻清掉了"


# ══ 8. 狀態跨重啟 ═══════════════════════════════════════════════════════
class TestTheAccountStateSurvivesARestart:
    def test_it_round_trips(self, monkeypatch):
        """自動更新/watchdog 重啟不該讓「今天已經失敗兩次」歸零 —— 那正是
        既有登入冷卻落地(批次SH)當初的同一個理由。"""
        monkeypatch.setattr(cq, "_his_today", lambda: "2026-08-24")
        for _ in range(2):
            _reject_once(BOTH)
        cq._save_job_fail_state()
        monkeypatch.setattr(cq, "_his_account_day", "")
        monkeypatch.setattr(cq, "_his_primary_fails", 0)
        monkeypatch.setattr(cq, "_his_using_backup", False)
        cq._load_job_fail_state()
        assert (cq._his_account_day, cq._his_primary_fails) == ("2026-08-24", 2)
        # 重啟後再失敗一次就是第三次 → 切換
        _reject_once(BOTH)
        assert cq._his_using_backup is True

    def test_the_using_backup_flag_round_trips(self, monkeypatch):
        monkeypatch.setattr(cq, "_his_today", lambda: "2026-08-24")
        for _ in range(3):
            _reject_once(BOTH)
        cq._save_job_fail_state()
        monkeypatch.setattr(cq, "_his_using_backup", False)
        cq._load_job_fail_state()
        assert cq._his_using_backup is True

    def test_a_state_without_its_day_is_not_adopted(self, monkeypatch):
        """★三個鍵是一組★:採用一個沒有歸屬日期的「正在用備用」,等於做出一個
        【永遠不會換日】的狀態 —— 出口就這樣沒了。"""
        with open(cq._job_fail_state_path(), "w", encoding="utf-8") as f:
            json.dump({"schema": cq._JOB_FAIL_STATE_SCHEMA, "streak": 0,
                       "last_alert_ts": 0.0, "his_using_backup": True,
                       "his_primary_fails": 3}, f)
        cq._load_job_fail_state()
        assert cq._his_using_backup is False
        assert cq._his_account_day == ""


# ══ 9. 信件標頭 ═════════════════════════════════════════════════════════
class TestTheMailHeaderNamesTheAccount:
    def test_it_reports_the_account_actually_used(self):
        """★寫的是【觀測到的事實】,不是從目前狀態回推★:這封信的資料是用哪
        一組帳號查出來的。狀態可能在查詢與寄信之間又變了(例如剛好跨日)。

        ★反例只靠這條規則分勝負★:目前狀態說「主帳號」,而這一輪真的送出去的
        是備用帳號 —— 兩者故意不一致。
        """
        cq._note_his_credentials_sent("999999", True)
        assert cq._his_using_backup is False       # 狀態說主帳號
        note = cq._his_account_note(BOTH)
        assert "備用帳號" in note and "999999" in note

    def test_it_falls_back_to_the_current_account(self):
        note = cq._his_account_note(BOTH)
        assert "主帳號" in note and "101358" in note

    def test_the_password_is_never_in_the_note(self):
        cq._note_his_credentials_sent("999999", True)
        note = cq._his_account_note(BOTH)
        assert "pw-backup" not in note and "pw-primary" not in note

    def test_the_html_header_carries_the_note(self):
        note = "目前使用備用帳號登入（999999）"
        html = cq._build_consult_email_html("2026/8/24", "1523", "intro", "",
                                            note)
        assert "系統自動擷取　·　目前使用備用帳號登入" in html
        i, j = html.index("系統自動擷取"), html.index("會診通知單")
        assert i > j, "帳號註記不在信首的標題區"

    def test_an_empty_note_changes_nothing(self):
        """沒有註記時不可以留下一個空的分隔點(「系統自動擷取　·　」)。"""
        html = cq._build_consult_email_html("2026/8/24", "1523", "i", "", "")
        assert "系統自動擷取　·　" not in html


# ══ 10. 接上去了沒有(沒有呼叫端 = 那個宣稱是假的)══════════════════════════
def _fn(name):
    tree = ast.parse(inspect.getsource(cq))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"找不到 {name}")


def _calls(node):
    return {n.func.id for n in ast.walk(node)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}


class TestItIsActuallyWiredUp:
    @pytest.mark.parametrize("fn", ["_cold_start_session_impl",
                                    "_run_with_sw_hide"])
    def test_both_login_paths_go_through_the_accessor(self, fn):
        """★兩條路都要算數★:只改一條的話,「已經換成備用了」在另一條路上
        完全沒有效果 —— 後備模式仍然每輪送主帳號。"""
        node = _fn(fn)
        assert "_current_his_account" in _calls(node), (
            f"{fn} 沒有走帳號存取器(可能直接讀 cfg['username'])")
        reads = [n for n in ast.walk(node)
                 if isinstance(n, ast.Subscript)
                 and isinstance(n.value, ast.Name) and n.value.id == "cfg"
                 and isinstance(n.slice, ast.Constant)
                 and n.slice.value in ("username", "password",
                                       "backup_username", "backup_password")]
        assert not reads, f"{fn} 仍直接讀 cfg['username'/'password']"
        assert "_note_his_credentials_sent" in _calls(node), (
            f"{fn} 送出帳密後沒有登記是哪一組")

    def test_the_mail_passes_the_account_note(self):
        node = _fn("_do_full_job")
        for n in ast.walk(node):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "_build_consult_email_html"):
                assert len(n.args) == 5, "信件沒有帶上帳號註記"
                assert isinstance(n.args[4], ast.Name)
                assert n.args[4].id == "_account_note"
                break
        else:
            raise AssertionError("找不到 _build_consult_email_html 的呼叫")

    def test_the_plain_text_body_carries_it_too(self):
        """不支援 HTML 的客戶端看到的是純文字版 —— 兩份要一致。"""
        src = inspect.getsource(cq._do_full_job)
        assert "_account_note = _his_account_note(cfg, token=_flow_token)" in src, (
            "信件標頭沒有吃查詢結果帶回來的 token")
        assert "text_parts.append(_account_note)" in src

    def test_the_session_carries_its_login_token(self):
        """★session 要真的掛上 token★:標頭測試用的是假 session,量不到
        冷啟動真的有沒有掛 —— 來源層釘住 `sess.login_token = _login_token`。"""
        src = inspect.getsource(cq._cold_start_session_impl)
        assert "sess.login_token = _login_token" in src

    def test_the_rejection_counter_sits_at_the_login_check(self):
        """★計數只有一個地方★:兩條登入路徑都收斂到這個判準,而且它就是
        「帳密送出去了、登入沒完成」被判定出來的那一刻。"""
        node = _fn("_wait_main_window_after_login")
        assert "_note_his_login_rejected" in _calls(node)
        src = inspect.getsource(cq)
        assert src.count("_note_his_login_rejected(cfg, login_token)") == 1, (
            "計數點不只一個 → 同一次失敗可能被算好幾遍")


def test_the_defaults_have_the_backup_keys():
    for k in ("backup_username", "backup_password"):
        assert cq.DEFAULT_CONFIG[k] == ""


# ══ 外審第 1 輪 P1:兩條登入路徑共用冷卻閘門與失敗分類 ════════════════════
class TestBothLoginPathsShareTheCooldown:
    """★「切換帳號不重置冷卻」不能只在一條路上成立★(外審 CQ-BA R1 P1)。
    隱藏桌面建不起來的機器走 SW_HIDE 後備模式,那條路原本不查也不設冷卻 ——
    第三次失敗切到備用之後,下一個觸發會立刻把【第二組】帳密也送出去。
    """

    def test_the_fallback_path_refuses_to_start_during_a_cooldown(
            self, monkeypatch):
        """★反例:冷卻中連 systemftp 都不該開★ —— 用「一被呼叫就爆炸」的
        `_systemftp_pids` 證明流程在送帳密之前就停住了。"""
        monkeypatch.setattr(cq, "_login_cooldown_until",
                            cq.time.time() + 600, raising=False)

        def _no(*a, **k):
            raise AssertionError("冷卻中還去開 systemftp/送帳密")
        monkeypatch.setattr(cq, "_systemftp_pids", _no)
        with pytest.raises(RuntimeError) as ei:
            cq._run_with_sw_hide(dict(BOTH))
        assert "登入冷卻中" in str(ei.value)

    def test_the_cooldown_gate_lets_a_clear_run_through(self, monkeypatch):
        """★反例要分得開「閘門」與「整條路都不能跑」★:沒有冷卻時,流程要
        真的走到下一步(這裡用同一個哨兵證明它越過了閘門)。"""
        monkeypatch.setattr(cq, "_login_cooldown_until", 0.0, raising=False)

        def _sentinel(*a, **k):
            raise AssertionError("越過閘門了")
        monkeypatch.setattr(cq, "_systemftp_pids", _sentinel)
        with pytest.raises(AssertionError, match="越過閘門了"):
            cq._run_with_sw_hide(dict(BOTH))

    @pytest.mark.parametrize("fn", ["_cold_start_session_impl",
                                    "_run_with_sw_hide"])
    def test_both_paths_use_the_shared_gate_and_classifier(self, fn):
        node = _fn(fn)
        calls = _calls(node)
        assert "_login_cooldown_gate" in calls, f"{fn} 沒有查登入冷卻"
        assert "_note_login_failure_cooldown" in calls, f"{fn} 失敗後沒有設冷卻"
        # ★判準只留一份★:不可以自己再寫一套分類/計時
        src = ast.dump(node)
        assert "BDE_COOLDOWN_SECONDS" not in src, f"{fn} 自己又寫了一份分類"
        assert "login_cooldown_remaining" not in src, f"{fn} 自己又寫了一份閘門"


class TestTheLoginFailureClassifier:
    @pytest.fixture(autouse=True)
    def _zero(self, monkeypatch):
        monkeypatch.setattr(cq, "_set_login_cooldown_until",
                            lambda t, **k: self.seen.append(t))
        self.seen: list = []
        monkeypatch.setattr(cq, "_set_login_cooldown_until",
                            lambda t, **k: self.seen.append(t))

    def test_a_bde_block_gets_the_bde_cooldown(self):
        cq._note_login_failure_cooldown(cq.HISStartupBlocked("bde"), False)
        assert len(self.seen) == 1
        assert self.seen[0] - cq.time.time() > (
            cq._keepalive.LOGIN_COOLDOWN_SECONDS + 60), "用了登入冷卻的長度"

    def test_login_not_completed_gets_the_login_cooldown(self):
        cq._note_login_failure_cooldown(cq.LoginNotCompleted("x"), False)
        assert len(self.seen) == 1

    def test_a_failure_before_the_credentials_went_out_sets_nothing(self):
        """★帳密還沒送出去就失敗不必冷卻★(例:等不到登入視窗)——
        那不是「同一組帳密被密集送出」的情境,冷卻只會白白跳過一輪查詢。"""
        cq._note_login_failure_cooldown(RuntimeError("等不到登入視窗"), False)
        assert self.seen == []

    def test_any_failure_after_the_credentials_went_out_does(self):
        cq._note_login_failure_cooldown(RuntimeError("主畫面沒出現"), True)
        assert len(self.seen) == 1


# ══ 外審第 1 輪 P2:換日不可以沿用昨天的備用帳號 session ═══════════════════
class _FakeSession:
    def __init__(self, is_backup, account_day):
        self.is_backup = is_backup
        self.account_day = account_day
        self.in_use = False
        self.pid = 4321
        self.started_at = 0.0


class TestTheSessionDoesNotOutliveTheDay:
    """★出口是隔天★:換日只在「要重新登入」時判斷的話,昨天用備用帳號建立的
    常駐 session 過了 00:00 還在服務 —— 切回主帳號被延後到它自己結束為止。"""

    @pytest.fixture
    def _harness(self, monkeypatch):
        closed: list = []
        monkeypatch.setattr(cq, "_ensure_no_unmanaged_sessions",
                            lambda **k: None)
        monkeypatch.setattr(cq, "_session_death_reason", lambda s: "")
        monkeypatch.setattr(cq._keepalive, "session_needs_restart",
                            lambda a, b: False)
        monkeypatch.setattr(cq, "_session_close_if_current",
                            lambda s, why: closed.append(why) or True)
        monkeypatch.setattr(cq, "_cold_start_session", lambda cfg: "COLD")
        monkeypatch.setattr(cq, "_his_today", lambda: "2026-08-25")
        return closed

    def test_yesterdays_backup_session_is_retired(self, _harness, monkeypatch):
        monkeypatch.setattr(cq, "_psession",
                            _FakeSession(True, "2026-08-24"), raising=False)
        assert cq._acquire_session(dict(BOTH)) == "COLD"
        assert _harness and "換日" in _harness[0]

    def test_todays_backup_session_is_reused(self, _harness, monkeypatch):
        """同一天內不可以每次取用都重登(那會逼近院方的鎖定門檻)。"""
        sess = _FakeSession(True, "2026-08-25")
        monkeypatch.setattr(cq, "_psession", sess, raising=False)
        assert cq._acquire_session(dict(BOTH)) is sess
        assert _harness == []

    def test_a_stale_primary_session_is_not_retired(self, _harness,
                                                     monkeypatch):
        """★只對備用動手★:主帳號的 session 本來就是今天想要的那一組,
        為了換日把它關掉只是多一次登入 —— 這條反例只靠這個條件分勝負
        (它與上一條的差別只有 `is_backup`)。"""
        sess = _FakeSession(False, "2026-08-24")
        monkeypatch.setattr(cq, "_psession", sess, raising=False)
        assert cq._acquire_session(dict(BOTH)) is sess
        assert _harness == []

    def test_a_login_that_crosses_midnight_keeps_the_day_it_started(
            self, _harness, monkeypatch):
        """★日期要在【選帳號的當下】蓋章★(外審 CQ-BA 第 2 輪 P2):登入要等
        主畫面,最久 120 秒 —— 23:59 決定用備用帳號、00:00 主畫面才出現的話,
        事後再問一次「今天是幾號」會把它蓋成【今天的】備用 session,
        換日判斷就再也收不掉它,備用帳號會一路用滿隔天。

        ★反例只靠這條規則分勝負★:帳號的選擇本身完全正確(狀態就是備用),
        差別只在那個日期是什麼時候取的。
        """
        monkeypatch.setattr(cq, "_his_today", lambda: "2026-08-24")
        for _ in range(3):
            _reject_once(BOTH)
        _user, _pw, is_backup, day = cq._current_his_account(BOTH)
        assert (is_backup, day) == (True, "2026-08-24")
        # 登入送出後等主畫面等到跨了午夜
        monkeypatch.setattr(cq, "_his_today", lambda: "2026-08-25")
        monkeypatch.setattr(cq, "_psession", _FakeSession(is_backup, day),
                            raising=False)
        assert cq._acquire_session(dict(BOTH)) == "COLD"
        assert _harness and "換日" in _harness[0]

    def test_the_session_day_is_not_re_read_at_construction_time(self):
        """★不可以在建立 session 時再讀一次時鐘★ —— 那正是上面那條的缺陷來源。"""
        node = _fn("_cold_start_session_impl")
        for n in ast.walk(node):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "_PersistentSession"):
                kw = {k.arg: k.value for k in n.keywords}
                if "account_day" not in kw:
                    continue
                assert isinstance(kw["account_day"], ast.Name), (
                    "建立 session 時又讀了一次日期(登入可能已跨午夜)")
                break
        else:
            raise AssertionError("找不到帶 account_day 的 _PersistentSession")

    def test_a_new_session_records_the_account_it_logged_in_with(self):
        """沒有這個身分,上面那條判準根本問不出來。"""
        node = _fn("_cold_start_session_impl")
        for n in ast.walk(node):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "_PersistentSession"):
                kw = {k.arg for k in n.keywords}
                if "is_backup" in kw:
                    assert "account_day" in kw
                    break
        else:
            raise AssertionError("建立 session 時沒有記下用了哪一組帳號/哪一天")


# ══ 外審第 3 輪:登入期間跨午夜的備用 session,這一輪用完就收 ═══════════════
class TestACrossMidnightBackupSessionIsRetiredAfterTheRound:
    """★切回主帳號不可以依賴「還會有下一輪」★(外審 CQ-BA R3):原本只靠下一次
    `_acquire_session` 收掉它。改成這一輪用完就收。

    ★不在登入成功的當下立刻改登主帳號★(外審建議的做法):那等於在同一分鐘內
    對院方連送兩組不同的帳密,正是使用者定案「切換帳號不重置冷卻」要避免的事。
    """

    @pytest.fixture
    def _closed(self, monkeypatch):
        closed: list = []
        monkeypatch.setattr(cq, "_session_close_if_current",
                            lambda s, why: closed.append(why) or True)
        monkeypatch.setattr(cq, "_his_today", lambda: "2026-08-25")
        monkeypatch.setattr(cq, "_in_quiet_hours", lambda now, cfg: False)
        return closed

    def test_it_is_retired(self, _closed):
        cq._retire_session_if_no_keepalive(
            _FakeSession(True, "2026-08-24"), dict(BOTH))
        assert _closed and "午夜" in _closed[0]

    def test_a_same_day_backup_session_stays(self, _closed):
        """★反例只靠「跨日」分勝負★:一樣是備用帳號,只差日期。"""
        cq._retire_session_if_no_keepalive(
            _FakeSession(True, "2026-08-25"), dict(BOTH))
        assert _closed == []

    def test_a_cross_midnight_primary_session_stays(self, _closed):
        """★也只靠「是備用」分勝負★:主帳號本來就是今天想要的那一組。"""
        cq._retire_session_if_no_keepalive(
            _FakeSession(False, "2026-08-24"), dict(BOTH))
        assert _closed == []

    def test_it_does_not_send_a_second_set_of_credentials(self):
        """★收掉 ≠ 立刻重登★:這條路上不可以出現任何一次登入。"""
        src = inspect.getsource(cq._retire_session_if_no_keepalive)
        for bad in ("_cold_start_session", "_current_his_account",
                    "_note_his_credentials_sent"):
            assert bad not in src, f"跨午夜收尾竟然又去登入了({bad})"

    def test_the_round_that_logged_in_keeps_its_own_day(self):
        """★這一輪屬於它【開始】的那一天★ —— 收尾刻意排在查詢【之後】。

        外審 CQ-BA 第 3/4 輪建議「查詢之前就收掉、這一輪放棄」。不採納,理由
        寫在這裡免得日後有人「順手修正」成那樣:

        * 帳號的決定屬於哪一天,是在【選帳號的當下】蓋章的(同一批的另一條
          規則)。一輪 23:59 開始的查詢就是 8/24 那一輪;使用者定案的
          「隔天切回主帳號」指的是【隔天的那些輪次】,而不是要把一輪已經登入
          成功的查詢從中間砍掉。
        * 砍掉它會落進 `_automation_on_hidden` 的掉線恢復路徑
          —— 那條路的動作正是「殺掉重啟、★重新登入一次★」,也就是外審
          自己想避免的第二次送帳密。要繞開它得新增一種 fatal 例外、塞進兩處
          fatal 清單、還要在健康告警那邊豁免(不然會被記成連續失敗)。
          ★這個「最小修正」並不小,而且自己帶著新的失效模式。★
        * 代價那一邊是空的:資料是午夜前查的,信件標頭誠實寫著備用帳號。

        所以順序是【查完 → 收掉】,而不是【收掉 → 放棄】。
        """
        src = inspect.getsource(cq._automation_on_hidden)
        i_q = src.index("_query_cycle(sess")
        i_r = src.index("_retire_session_if_no_keepalive(sess, cfg)")
        assert i_q < i_r, "收尾被搬到查詢之前 → 這一輪會被整個放棄"
        assert "_cold_start_session(cfg)" not in src[i_q:i_r], (
            "查詢與收尾之間多了一次登入")


# ══ 外審 2026-08-27 P2-05:嘗試 token 化(stale-worker 交錯不可對錯帳)═════
class TestAttemptTokensSurviveWorkerInterleaving:
    """★全域最新序號會對錯帳★:session 架構允許 stale-worker 接管(>360 秒)
    —— A 送帳密後卡死、B 搶走預約再送、A 才醒來回報。舊的「全域最新序號」
    讓 A 的失敗消費掉【B 的】那一次,B 的成功被當成已處理。
    token 把嘗試與結果綁在一起,誰先醒來都對不錯。
    """

    def test_a_stale_workers_late_failure_does_not_eat_the_new_success(self):
        """外審的原始情境:A 送(主)→ B 送(主)→ A 才回報失敗 → B 成功。
        正確帳:A 失敗 1 次、B 成功把連續打斷 → 最終 fails == 0。
        ★舊實作在這裡會是 fails == 1★(A 消費了 B 的序號,B 的成功被忽略)。
        """
        tok_a = cq._note_his_credentials_sent("101358", False)
        tok_b = cq._note_his_credentials_sent("101358", False)
        cq._note_his_login_rejected(BOTH, tok_a)     # A 晚到的失敗
        assert cq._his_primary_fails == 1
        cq._note_his_login_succeeded(tok_b)          # B 的成功
        assert cq._his_primary_fails == 0, "B 的成功被 A 的舊失敗吃掉了"

    def test_the_reverse_order_still_counts_the_failure(self):
        """★反向排序也要對★:B 先成功、A 的失敗才到 —— A 那一次確實失敗過,
        照算(之後的成功不追溯塗銷更早嘗試的事實;數字只進不刪,
        連續與否由事件順序決定)。"""
        tok_a = cq._note_his_credentials_sent("101358", False)
        tok_b = cq._note_his_credentials_sent("101358", False)
        cq._note_his_login_succeeded(tok_b)
        cq._note_his_login_rejected(BOTH, tok_a)
        assert cq._his_primary_fails == 1

    def test_a_failure_is_attributed_to_its_own_account(self):
        """★失敗要記在 token 上那一組帳號頭上★:A 送出【主帳號】後卡死,
        B 已切到備用並送出【備用帳號】(全域「最後送出」= 備用);
        A 的失敗才到 —— 那是主帳號的失敗,必須累計主帳號次數。
        ★反例要讓 token 與全域不同值★(同值時讀哪邊都對,量不到)。"""
        tok_a = cq._note_his_credentials_sent("101358", False)
        cq._note_his_credentials_sent("999999", True)    # 全域最後送出=備用
        cq._note_his_login_rejected(BOTH, tok_a)
        assert cq._his_primary_fails == 1, "主帳號的失敗被記到備用頭上"

    def test_each_token_is_consumed_independently(self):
        """兩顆 token 各自對帳:A、B 都失敗 → 記兩次(舊實作只記得到一次,
        因為第二個 handler 看到的序號已被第一個消費)。"""
        tok_a = cq._note_his_credentials_sent("101358", False)
        tok_b = cq._note_his_credentials_sent("101358", False)
        cq._note_his_login_rejected(BOTH, tok_a)
        cq._note_his_login_rejected(BOTH, tok_b)
        assert cq._his_primary_fails == 2


class TestTheHeaderReadsTheServingSession:
    def test_an_explicit_token_wins_over_the_global_last_sent(self):
        """★標頭讀【查詢結果帶回來的】token★(外審 2026-08-27 兩輪):
        從組信當下的全域推測來源會說錯 —— 查詢與寄信之間別的 worker 可能
        又送過帳密;SW_HIDE 後備跑完後舊常駐 session 也可能還掛著。
        ★反例只靠來源分勝負★:token 說主帳號,全域最後送出是備用帳號。"""
        tok = cq._note_his_credentials_sent("101358", False)
        cq._note_his_credentials_sent("999999", True)     # 別的 worker
        note = cq._his_account_note(BOTH, token=tok)
        assert "主帳號" in note and "101358" in note, note

    def test_no_token_falls_back_to_the_global(self):
        cq._note_his_credentials_sent("999999", True)
        note = cq._his_account_note(BOTH)
        assert "備用帳號" in note and "999999" in note, note

    def test_both_query_paths_return_their_token(self):
        """★查詢結果要真的帶著 token 回來★:兩條路的成功出口都附上 ——
        少一條的話,那條路的信就退回全域推測(正是要移除的東西)。"""
        src_hidden = inspect.getsource(cq._automation_on_hidden)
        assert src_hidden.count(
            '(*result, getattr(sess, "login_token", None))') == 2, (
            "hidden 路徑(含掉線恢復那條)沒有把 session 的 token 附上")
        src_sw = inspect.getsource(cq._run_with_sw_hide)
        assert "roster_texts, _login_token" in src_sw, (
            "SW_HIDE 後備沒有把自己的 token 附上")



class TestConsumeAndTransitionAreOneCriticalSection:
    def test_a_success_cannot_slip_in_between_consume_and_increment(
            self, monkeypatch):
        """★消費與狀態轉移要在同一個臨界區★(外審 2026-08-27 R1):
        只鎖 `consumed` 的話 —— A 的失敗消費完 token、還沒 `+= 1` 就被切走,
        B 的成功看到 fails==0 直接返回,A 醒來再 += → 「失敗後成功」卻留下
        1 次連續失敗。鎖住整段,拿鎖的順序就是事件順序。

        佈局:A 執行緒進 rejected,在臨界區內(消費之後、+= 之前)的
        `_his_today` 掛鉤處發訊號並停留 0.3s;主執行緒此時發動 B 的成功。
        ★修好的版本★:B 拿不到鎖,等 A 做完(fails=1)才進去 → 歸零 → 0。
        ★壞版本★:B 立刻跑完(看到 0,不歸零),A 再 += → 1。
        """
        import threading as th
        import time as _t
        in_critical = th.Event()
        real_today = cq._his_today

        def _hook():
            if not in_critical.is_set():
                in_critical.set()
                _t.sleep(0.3)          # 停在臨界區內,給 B 插隊的機會
            return real_today()
        monkeypatch.setattr(cq, "_his_today", _hook)
        tok_a = cq._note_his_credentials_sent("101358", False)
        tok_b = cq._note_his_credentials_sent("101358", False)
        t = th.Thread(target=lambda: cq._note_his_login_rejected(BOTH, tok_a))
        t.start()
        assert in_critical.wait(5), "A 沒進到臨界區"
        cq._note_his_login_succeeded(tok_b)      # B 的成功試圖插隊
        t.join(5)
        assert cq._his_primary_fails == 0, (
            "B 的成功插進 A 的消費與 += 之間 → 失敗後成功仍留下連續失敗")
