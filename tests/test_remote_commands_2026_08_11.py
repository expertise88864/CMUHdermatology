# -*- coding: utf-8 -*-
"""[批次SI] 遠端指令（使用者定案 2026-08-11）。

主旨格式：`[皮膚科遠端指令] <動作> <機器>`
動作＝重啟會診／重啟打卡／重開機；機器＝電腦名稱（★一律必填、一次一台★）。

★三個安全立場（每一個都有它自己的失敗故事）★

① **機器是必填，而且沒有「全部」。** 所有診間電腦共用同一個信箱、各自輪詢 ——
   不指定的話一封信會讓【每一台】都動作，對「重開機」尤其危險。
   而「全部」用共用信箱做不出來：已讀旗標是信箱全域的，第一台標掉之後其他
   機器再也搜不到那封 UNSEEN。（使用者的定案裡也沒有這一項，是我自己加的。）

② **指令一律強制驗證，不看 `require_authenticated_trigger`。** 那個設定是給
   查詢觸發的（誤觸發＝多寄一封信）。指令的代價是重啟臨床自動化、甚至重開一台
   診間電腦；`From` 是可偽造的純文字，沒有任何情況值得為它開後門。

③ **先標已讀、標不掉就不執行**（與查詢觸發【相反】）。失敗模式不對稱：
   指令遺失＝重寄一次就好；指令重複＝每一輪都重啟一次的★無限重啟迴圈★，
   而那正是「程式一直不對勁」時最可能發生的狀況。
"""
import ast
import importlib
import io
import os
import re
import sys
import threading

import pytest

NL = chr(10)
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

cq = importlib.import_module("consult_query")
ac = importlib.import_module("autoclock")
ir = importlib.import_module("cmuh_common.imap_reader")

SRC = io.open(os.path.join(REPO_ROOT, "src", "consult_query.py"),
              encoding="utf-8").read()
AC_SRC = io.open(os.path.join(REPO_ROOT, "src", "autoclock.py"),
                 encoding="utf-8").read()


def _fn_src(text, name):
    for n in ast.walk(ast.parse(text)):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(text, n) or ""
    raise AssertionError(f"找不到 {name}")


def _fn_body_code(text, name):
    """函式【可執行的部分】(去 docstring;`ast.unparse` 也不留註解)。

    ★負向斷言不可以只剝註解★ docstring 與【回信內文】裡都會出現那些字面
    (例如回信要向使用者解釋「才會真的下 shutdown /r」)。用它去斷言
    「程式碼裡沒有 shutdown」會被自己的說明文字打敗 —— 今天已經第七次。
    """
    for n in ast.walk(ast.parse(text)):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            body = n.body
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body = body[1:]
            return NL.join(ast.unparse(st) for st in body)
    raise AssertionError(f"找不到 {name}")


def _strip_comments(text):
    """★負向斷言先剝註解★（解釋「為什麼不可以」的那句話裡就有那個字面）。"""
    return NL.join(re.sub(r"#.*$", "", ln) for ln in text.splitlines())


class TestTheSubjectIsParsedStrictly:
    def test_the_three_phrases(self):
        for word, code in (("皮膚科會診重啟", "restart_consult"),
                           ("皮膚科打卡重啟", "restart_autoclock"),
                           ("皮膚科會診重開", "reboot")):
            assert cq.parse_remote_command(
                "%s PC-1" % word) == (code, "PC-1")

    def test_the_machine_may_be_omitted(self):
        """★使用者定案 2026-08-11★ 不填機器＝每一台【正在跑會診查詢】的
        電腦都做。會去收這個信箱的就是那支程式，沒在跑的看不到那封信。
        """
        assert cq.parse_remote_command("皮膚科會診重開") == ("reboot", "")
        assert cq.parse_remote_command(
            "皮膚科會診重啟") == ("restart_consult", "")

    def test_a_reply_prefix_and_extra_spaces_still_parse(self):
        """郵件客戶端會插 `Re:`、多餘空白、全形空格。"""
        assert cq.parse_remote_command(
            "Re: 　皮膚科會診重開　  PC-1 ") == ("reboot", "PC-1")
        assert cq.parse_remote_command("Re: 皮膚科會診重開") == ("reboot", "")

    def test_a_phrase_glued_to_more_text_is_not_a_command(self):
        """★短語後面必須是空白或結束★

        `皮膚科會診重開機` 不是「皮膚科會診重開」加一台叫「機」的電腦 ——
        那種主旨不執行任何東西。
        """
        assert cq.parse_remote_command("皮膚科會診重開機") == (None, "")
        assert cq.parse_remote_command("皮膚科會診重啟動") == (None, "")

    def test_an_unknown_phrase_is_not_a_command(self):
        assert cq.parse_remote_command("皮膚科會診關機 PC-1") == (None, "")
        assert cq.parse_remote_command("會診重開") == (None, "")

    def test_phrases_are_matched_exactly_not_fuzzily(self):
        """★「重開」與「重啟」差一個字，代價差很多★ —— 絕不模糊比對。"""
        for bad in ("皮膚科會診", "皮膚科重開", "會診重開", "皮膚科打卡重開"):
            assert cq.parse_remote_command(
                "%s PC-1" % bad) == (None, ""), bad

    def test_a_normal_mail_is_not_a_command(self):
        for subj in ("", "皮膚科會診觸發", "重開機 PC-1", None):
            assert cq.parse_remote_command(subj) == (None, "")

    def test_targeting(self, monkeypatch):
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        assert cq._remote_command_is_for_me("PC-1") is True
        assert cq._remote_command_is_for_me("PC-2") is False
        # ★沒指定機器 → 這一台要做★（收得到信＝正在跑會診查詢）
        assert cq._remote_command_is_for_me("") is True

    def test_dedup_is_not_the_seen_flag(self):
        """★機器可省略之後，已讀旗標就不能當去重★

        `\\Seen` 是【信箱全域】的狀態：第一台標掉之後其他台再也搜不到那封
        UNSEEN —— 於是只有一台會動作。所以改用【本機收據】。
        """
        body = _strip_comments(_fn_src(SRC, "_poll_remote_commands"))
        i = body.index("_claim_remote_command(")
        j = body.index("_run_remote_command(")
        assert i < j, "★要先把收據落地才可以執行★"

    def test_an_unknown_hostname_never_matches_a_named_target(self,
                                                              monkeypatch):
        """取不到自己的名字時，不可以「猜」自己就是【被指名】的那一台。
        （沒有指名的那種本來就與名字無關。）"""
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "")
        assert cq._remote_command_is_for_me("PC-1") is False
        assert cq._remote_command_is_for_me("") is True


class TestAuthorizationIsFailClosed:
    @staticmethod
    def _poll(monkeypatch, item, allow=("doc@x.tw",)):
        ran, marked = [], []
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(ir, "mark_uids_seen",
                            lambda uids, **kw: marked.append(list(uids)) or True)
        monkeypatch.setattr(cq, "_claim_remote_command", lambda key: True)
        monkeypatch.setattr(cq, "_run_remote_command",
                            lambda action, sender: ran.append(action))
        monkeypatch.setattr(cq, "_alert_trigger_rejected", lambda s: None)
        cq._poll_remote_commands({"allowed_trigger_senders": list(allow)},
                                 {"items": [item], "error": None,
                                  "uidvalidity": "9", "mailbox_identity": "T:INBOX"})
        return ran, marked

    @staticmethod
    def _item(**kw):
        base = {"uid": "7", "sender": "doc@x.tw", "authenticated": True,
                "subject": "皮膚科打卡重啟 PC-1", "age_sec": 10.0}
        base.update(kw)
        return base

    def test_an_authorised_command_runs(self, monkeypatch):
        ran, marked = self._poll(monkeypatch, self._item())
        assert ran == ["restart_autoclock"]
        # ★要執行的那一封【不標已讀】★:已讀是信箱全域的,標掉之後其他
        #   正在跑會診查詢的電腦就再也看不到這封信了(去重改用本機收據)。
        assert marked == [], "把要執行的指令標成已讀 → 別台再也收不到"

    def test_an_unauthenticated_sender_is_refused(self, monkeypatch):
        """★From 是可偽造的純文字★ —— 沒通過 SPF/DKIM/DMARC 就不算數。

        ★[外審 SI 第 1 輪 P2-5] 但它【要】被標已讀★：已經告警過了，
        留著只會讓它每 20 秒被 FETCH 一次 + 寫一行 log ——
        那是不需要通過驗證就能發動的資源／log DoS。
        """
        ran, marked = self._poll(monkeypatch, self._item(authenticated=False))
        assert not ran, "未通過授權卻執行了"
        assert marked == [["7"]], "終局處置沒有結案 → 可被拿來洗 log"

    def test_a_sender_outside_the_whitelist_is_refused(self, monkeypatch):
        ran, _ = self._poll(monkeypatch, self._item(sender="evil@x.tw"))
        assert not ran

    def test_a_command_for_another_machine_is_ignored(self, monkeypatch):
        ran, marked = self._poll(
            monkeypatch,
            self._item(subject="皮膚科會診重開 PC-2"))
        assert not ran
        assert not marked, "不是給這台的,連標已讀都不該做(那台還沒收到)"

    def test_the_strict_setting_cannot_open_a_backdoor(self):
        """★指令不看 `require_authenticated_trigger`★

        那個設定是給查詢觸發的。指令若也吃它，關掉它就等於「任何人都能重開
        一台診間電腦」。
        """
        body = _strip_comments(_fn_src(SRC, "_poll_remote_commands"))
        assert "require_authenticated_trigger" not in body

    def test_it_is_wired_into_the_scheduler(self):
        """★沒有呼叫端＝這個功能不存在★"""
        loop = _strip_comments(_fn_src(SRC, "scheduler_loop"))
        assert "_poll_remote_commands(" in loop
        assert "_run_imap_commands_with_timeout()" in loop


class TestClaimFirstThenExecute:
    """★這是與查詢觸發【相反】的取捨★

    收據寫不下去還執行的話，每一輪（20 秒）都會再做一次 —— 無限重啟／
    重開機迴圈，而那正是「程式一直不對勁」時最可能發生的狀況。
    指令遺失只要重寄一次；指令重複沒有人救得回來。
    """

    @staticmethod
    def _poll(monkeypatch, claim_ok):
        ran = []
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(cq, "_claim_remote_command", lambda key: claim_ok)
        monkeypatch.setattr(ir, "mark_uids_seen", lambda uids, **kw: True)
        monkeypatch.setattr(cq, "_run_remote_command",
                            lambda a, s: ran.append(a))
        cq._poll_remote_commands(
            {"allowed_trigger_senders": ["doc@x.tw"]},
            {"items": [{"uid": "7", "sender": "doc@x.tw",
                        "authenticated": True, "expired": False,
                        "subject": "皮膚科會診重開 PC-1",
                        "age_sec": 1.0}], "error": None,
             "uidvalidity": "9", "mailbox_identity": "T:INBOX"})
        return ran

    def test_a_command_whose_receipt_cannot_be_written_is_not_executed(
            self, monkeypatch):
        assert not self._poll(monkeypatch, False)

    def test_a_claimed_command_runs(self, monkeypatch):
        assert self._poll(monkeypatch, True) == ["reboot"]

    def test_claiming_happens_before_running(self):
        body = _strip_comments(_fn_src(SRC, "_poll_remote_commands"))
        assert body.index("_claim_remote_command(") < body.index(
            "_run_remote_command(")


class TestTheActionsDoTheRightThing:
    def test_restart_consult_uses_the_clean_restart_path(self, monkeypatch):
        """★不可以在背景緒直接重啟★ 那會留下舊行程 + 兩個托盤圖示
        （2026-06-03 修過一次的事）。"""
        called = []
        monkeypatch.setattr(cq, "_request_restart_for_update",
                            lambda: called.append(1))
        monkeypatch.setattr(cq, "_reply_remote_command",
                            lambda *a, **k: None)
        cq._run_remote_command("restart_consult", "doc@x.tw")
        assert called == [1]
        assert "restart_self(" not in _fn_body_code(SRC, "_run_remote_command")

    def test_reboot_goes_through_the_idle_guard(self, monkeypatch):
        """★遠端要求也不會立刻重開★ —— 走既有的閒置 30 分鐘 + 24 小時看守。"""
        armed = []
        monkeypatch.setattr(cq, "_schedule_reboot_watch",
                            lambda reason, detail: armed.append(reason))
        monkeypatch.setattr(cq, "_reply_remote_command", lambda *a, **k: None)
        cq._run_remote_command("reboot", "doc@x.tw")
        assert armed == ["REMOTE"]
        code = _fn_body_code(SRC, "_run_remote_command")
        assert "subprocess" not in code, "不可以自己下 shutdown,要走看守"

    def test_restart_autoclock_writes_a_request_not_a_kill(self, monkeypatch):
        """★不是直接砍它★ 可能砍在正在按打卡按鈕的當下。"""
        wrote = []
        monkeypatch.setattr(cq, "_write_autoclock_restart_request",
                            lambda why: wrote.append(why) or True)
        monkeypatch.setattr(cq, "_reply_remote_command", lambda *a, **k: None)
        cq._run_remote_command("restart_autoclock", "doc@x.tw")
        assert wrote
        code = _fn_body_code(SRC, "_run_remote_command")
        for forbidden in ("taskkill", "terminate(", "kill("):
            assert forbidden not in code

    def test_every_action_replies(self, monkeypatch):
        """做完要回信 —— 不然使用者不知道指令有沒有到。"""
        replies = []
        monkeypatch.setattr(cq, "_reply_remote_command",
                            lambda s, subj, body, **k: replies.append(subj))
        monkeypatch.setattr(cq, "_request_restart_for_update", lambda: None)
        monkeypatch.setattr(cq, "_schedule_reboot_watch",
                            lambda *a, **k: None)
        monkeypatch.setattr(cq, "_write_autoclock_restart_request",
                            lambda why: True)
        for a in ("restart_consult", "restart_autoclock", "reboot"):
            cq._run_remote_command(a, "doc@x.tw")
        assert len(replies) == 3

    def test_the_reply_goes_only_to_the_sender(self):
        body = _strip_comments(_fn_src(SRC, "_reply_remote_command"))
        assert "recipients=[str(sender)]" in body

    def test_an_unknown_action_code_does_nothing(self, monkeypatch):
        monkeypatch.setattr(cq, "_reply_remote_command",
                            lambda *a, **k: pytest.fail("不該回信"))
        monkeypatch.setattr(cq, "_schedule_reboot_watch",
                            lambda *a, **k: pytest.fail("不該排重開機"))
        cq._run_remote_command("rm_rf", "doc@x.tw")


class TestAutoclockRestartsItselfSafely:
    def _req(self, tmp_path, monkeypatch, **kw):
        import cmuh_common.paths as paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        from cmuh_common.atomic_io import atomic_write_json
        data = {"at": ac.time_module.time(), "why": "test"}
        data.update(kw)
        atomic_write_json(os.path.join(str(tmp_path),
                                       ac.AUTOCLOCK_RESTART_REQUEST), data)

    def test_it_restarts_when_idle(self, tmp_path, monkeypatch):
        self._req(tmp_path, monkeypatch)
        done = []
        monkeypatch.setattr(ac, "restart_program", lambda: done.append(1))
        monkeypatch.setattr(ac, "_active_clock_task_age", lambda: (None, 0.0))
        assert ac._check_restart_request() is True
        assert done == [1]
        assert not os.path.exists(ac._restart_request_path()), "請求檔沒被拿走"

    def test_it_waits_while_a_punch_task_is_running(self, tmp_path,
                                                    monkeypatch):
        """★打卡是有臨床意義的外部動作★ 不可以砍在按下去的那一刻。"""
        self._req(tmp_path, monkeypatch)
        monkeypatch.setattr(ac, "restart_program",
                            lambda: pytest.fail("在打卡途中重啟了"))
        monkeypatch.setattr(ac, "_active_clock_task_age",
                            lambda: ("mon_am_in", 3.0))
        assert ac._check_restart_request() is False
        assert os.path.exists(ac._restart_request_path()), (
            "請求檔被吃掉了 → 那次重啟永遠不會發生")

    def test_a_stale_request_is_dropped(self, tmp_path, monkeypatch):
        self._req(tmp_path, monkeypatch,
                  at=ac.time_module.time() - ac._RESTART_REQUEST_MAX_AGE_SEC - 60)
        monkeypatch.setattr(ac, "restart_program",
                            lambda: pytest.fail("執行了過期的請求"))
        monkeypatch.setattr(ac, "_active_clock_task_age", lambda: (None, 0.0))
        assert ac._check_restart_request() is False

    def test_a_request_with_no_timestamp_is_dropped(self, tmp_path,
                                                    monkeypatch):
        """時間不明＝不知道是什麼時候的 → 不執行（指令一律 fail-closed）。"""
        self._req(tmp_path, monkeypatch, at=0)
        monkeypatch.setattr(ac, "restart_program",
                            lambda: pytest.fail("執行了時間不明的請求"))
        monkeypatch.setattr(ac, "_active_clock_task_age", lambda: (None, 0.0))
        assert ac._check_restart_request() is False

    def test_an_undeletable_request_is_not_executed(self, tmp_path,
                                                    monkeypatch):
        """★拿不走就不執行★ 否則每一輪(5 秒)都會再重啟一次。"""
        self._req(tmp_path, monkeypatch)
        monkeypatch.setattr(ac, "restart_program",
                            lambda: pytest.fail("刪不掉卻照樣重啟 → 無限迴圈"))
        monkeypatch.setattr(ac, "_active_clock_task_age", lambda: (None, 0.0))
        monkeypatch.setattr(
            ac.os, "unlink",
            lambda _p: (_ for _ in ()).throw(PermissionError("locked")))
        assert ac._check_restart_request() is False

    def test_no_request_means_no_work(self, tmp_path, monkeypatch):
        import cmuh_common.paths as paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        monkeypatch.setattr(ac, "restart_program",
                            lambda: pytest.fail("沒有請求卻重啟了"))
        assert ac._check_restart_request() is False

    def test_it_is_wired_into_the_scheduler_loop(self):
        """★沒有呼叫端＝這個功能不存在★"""
        assert "_check_restart_request()" in AC_SRC

    def test_the_two_sides_agree_on_the_filename(self):
        """檔名各寫一份的話，信差寫到 A、打卡看 B —— 永遠不會重啟。"""
        assert cq.AUTOCLOCK_RESTART_REQUEST == ac.AUTOCLOCK_RESTART_REQUEST


class TestTheCommandScanKeepsThePrivacyBoundary:
    def test_only_our_own_prefix_gets_its_subject_returned(self):
        """★`check_trigger` 刻意不把主旨交出去★（那個信箱的任何主旨都可能含
        病人姓名/床號）。指令掃描必須讀主旨，所以邊界是「只回傳主旨以我們
        自己的固定前綴開頭的信」—— 其他信件連主旨都不會被讀出來。"""
        code = _fn_body_code(
            io.open(os.path.join(REPO_ROOT, "src", "cmuh_common",
                                 "imap_reader.py"), encoding="utf-8").read(),
            "check_commands")
        assert "startswith(h)" in code
        i = code.index("startswith(h)")
        j = code.index("'subject'")
        assert i < j, "★先確認是我們的指令信,才可以把主旨交出去★"

    def test_it_never_changes_flags(self):
        """標不標已讀由呼叫端決定（指令的取捨與查詢觸發相反）。"""
        code = _fn_body_code(
            io.open(os.path.join(REPO_ROOT, "src", "cmuh_common",
                                 "imap_reader.py"), encoding="utf-8").read(),
            "check_commands")
        for forbidden in ("store", "Seen", "mark_uids_seen"):
            assert forbidden not in code, forbidden



class TestTerminalDispositionsAreAcknowledged:
    """★[外審 SI 第 1 輪 P2-5]★ 沒有人會再處理的信要結案。

    不結案的話它永遠停在 UNSEEN —— 每一輪（20 秒）都要為它 FETCH header +
    FETCH INTERNALDATE 並寫一行 warning，而且每一台共用信箱的機器各做一份。
    ★不需要通過驗證★就能發動的資源與 log DoS。
    （`check_trigger` 那邊 2026-08-08 外審 F4 記過同一件事。）
    """

    @staticmethod
    def _poll(monkeypatch, item):
        acked = []
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(ir, "check_commands",
                            lambda *a, **k: {"items": [item], "error": None,
                                  "uidvalidity": "9",
                                  "mailbox_identity": "T:INBOX"})
        monkeypatch.setattr(ir, "mark_uids_seen",
                            lambda uids, **kw: acked.append(list(uids)) or True)
        monkeypatch.setattr(cq, "_run_remote_command",
                            lambda a, s: pytest.fail("不該執行"))
        monkeypatch.setattr(cq, "_alert_trigger_rejected", lambda s: None)
        cq._poll_remote_commands({"allowed_trigger_senders": ["doc@x.tw"]},
                                 {"items": [item], "error": None,
                                  "uidvalidity": "9",
                                  "mailbox_identity": "T:INBOX"})
        return acked

    def test_a_malformed_command_is_acknowledged(self, monkeypatch):
        assert self._poll(monkeypatch, {
            "uid": "7", "sender": "doc@x.tw", "authenticated": True,
            "subject": "皮膚科會診重開 PC-1 亂寫", "age_sec": 1.0,
            "expired": False}) == [["7"]]

    def test_an_expired_command_is_acknowledged(self, monkeypatch):
        """過期是【絕對】的（依伺服器收信時刻）→ 誰看到誰結案。"""
        assert self._poll(monkeypatch, {
            "uid": "7", "sender": "doc@x.tw", "authenticated": True,
            "subject": "皮膚科會診重開 PC-1", "age_sec": 99999.0,
            "expired": True}) == [["7"]]

    def test_an_expired_command_tells_the_sender_why_nothing_happened(
            self, monkeypatch):
        """★[外審 SI 第 5 輪]★ 目標對不上是【靜默】無效 —— 打錯字、那台沒在跑、
        以為可以寫「全部」，三種情況使用者收到的都是「什麼都沒發生」，
        他無從分辨，只能再猜一次。過期＝沒有任何機器接手的確定答案，
        這時要回一封說清楚。
        """
        replies = []
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(ir, "mark_uids_seen", lambda uids, **kw: True)
        monkeypatch.setattr(cq, "_run_remote_command",
                            lambda a, s: pytest.fail("過期的不該執行"))
        monkeypatch.setattr(cq, "_reply_remote_command",
                            lambda s, subj, body, **k: replies.append(body))
        cq._poll_remote_commands(
            {"allowed_trigger_senders": ["doc@x.tw"]},
            {"items": [{"uid": "7", "sender": "doc@x.tw",
                        "authenticated": True, "expired": True,
                        "subject": "皮膚科會診重開",
                        "age_sec": 99999.0}], "error": None,
             "uidvalidity": "9", "mailbox_identity": "T:INBOX"})
        assert replies, "★過期＝沒有任何機器接手,卻沒有告訴使用者★"
        assert "沒有指定" in replies[0], "要說出這封信是「不指定機器」那一種"
        assert "沒有任何一台在跑會診查詢" in replies[0]

    def test_an_expired_command_from_a_stranger_gets_no_reply(self,
                                                              monkeypatch):
        """不對偽造／未授權的位址回信（那會變成一個回信放大器）。"""
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(ir, "mark_uids_seen", lambda uids, **kw: True)
        monkeypatch.setattr(cq, "_run_remote_command",
                            lambda a, s: pytest.fail("不該執行"))
        monkeypatch.setattr(cq, "_reply_remote_command",
                            lambda *a, **k: pytest.fail("不該回信給未授權者"))
        cq._poll_remote_commands(
            {"allowed_trigger_senders": ["doc@x.tw"]},
            {"items": [{"uid": "7", "sender": "evil@x.tw",
                        "authenticated": False, "expired": True,
                        "subject": "皮膚科會診重開 PC-1",
                        "age_sec": 99999.0}], "error": None,
             "uidvalidity": "9", "mailbox_identity": "T:INBOX"})

    def test_a_command_for_another_machine_is_left_unread(self, monkeypatch):
        """★這一種不可以結案★ —— 那台機器還沒收到。
        它不會永遠留著：半小時後會變成「已過期」而被結掉。"""
        assert self._poll(monkeypatch, {
            "uid": "7", "sender": "doc@x.tw", "authenticated": True,
            "subject": "皮膚科會診重開 PC-9", "age_sec": 1.0,
            "expired": False}) == []


class TestTheScanIsBounded:
    def test_the_command_scan_has_its_own_bounded_worker(self):
        """★[外審 SI 第 2→4 輪，兩次修正才對]★

        ① 泛用的 `call_with_timeout` 逾時【不會砍 socket】（它是給 Win32 用的）
           → 卡住的 worker 占著同名額度，滿 4 條之後這個通道永久失效；
        ② 但也【不可以】跟觸發檢查共用 worker：指令掃描卡在 DNS/connect/TLS
           （還沒有 socket，`force_close_active()` 救不了）時，共用的
           single-flight 會讓之後每一輪都回「上一條還在跑」而完全不做
           `check_trigger` —— ★一個附屬功能把臨床的信件觸發永久關掉★。
        所以：自己的 worker、自己的 single-flight、同一套 force-close 保護。
        """
        # ★用 AST 可執行部分★ docstring 裡就有「`force_close_active()` 會關掉
        #   當下所有活動連線」這句話 —— 用字面掃的話，把那一行程式碼刪掉
        #   測試照樣綠（突變驗證當場量到，今天第八次踩這個坑）。
        code = _fn_body_code(SRC, "_run_imap_commands_with_timeout")
        assert "force_close_active(tag=" in code
        assert "force_close_active(clear=True, tag=" in code
        assert "_last_imap_cmd_thread" in code
        # 觸發檢查那條不可以再碰指令掃描(否則兩者又綁在一起)
        worker = _fn_body_code(SRC, "_run_imap_check_with_timeout")
        assert "check_commands(" not in worker
        assert "_last_imap_cmd_thread" not in worker

    def test_a_stranded_command_scan_does_not_block_the_next_one_forever(
            self, monkeypatch):
        """卡住的那一條要擋下一輪(不疊加),但只擋【指令掃描】自己。"""
        block = threading.Event()
        monkeypatch.setattr(ir, "check_commands",
                            lambda *a, **k: block.wait(20) and None)
        old_cmd = cq._last_imap_cmd_thread
        cq._last_imap_cmd_thread = None
        try:
            first = cq._run_imap_commands_with_timeout(timeout=0.2)
            second = cq._run_imap_commands_with_timeout(timeout=0.2)
        finally:
            block.set()
            cq._last_imap_cmd_thread = old_cmd
        assert first["error"], "逾時要據實回報"
        assert "still running" in second["error"], "沒有 single-flight → 會疊加"

    def test_a_stranded_command_scan_does_not_gate_the_clinical_trigger(
            self, monkeypatch):
        """★這一條就是第 4 輪 finding 的內容★

        指令掃描放生之後，臨床的觸發信檢查必須照常進行。
        """
        block = threading.Event()
        good = cq._empty_imap_result(None)
        good["scanned"] = 5
        monkeypatch.setattr(ir, "check_commands",
                            lambda *a, **k: block.wait(20) and None)
        monkeypatch.setattr(ir, "check_trigger", lambda *a, **k: good)
        old_cmd, old_chk = cq._last_imap_cmd_thread, cq._last_imap_thread
        cq._last_imap_cmd_thread = None
        cq._last_imap_thread = None
        try:
            cq._run_imap_commands_with_timeout(timeout=0.2)   # 放生一條
            out = cq._run_imap_check_with_timeout("kw", timeout=5.0)
        finally:
            block.set()
            cq._last_imap_cmd_thread, cq._last_imap_thread = old_cmd, old_chk
        assert not out.get("error"), (
            "★放生的指令掃描把臨床觸發檢查一起擋掉了★:" + str(out.get("error")))
        assert out["scanned"] == 5

    def test_the_scan_asks_for_every_phrase(self):
        """★少傳一個短語＝那個指令永遠收不到★（掃描只回主旨開頭符合的信）。
        突變驗證量到：寫死成單一短語，其他測試全綠。
        """
        code = _fn_body_code(SRC, "_run_imap_commands_with_timeout")
        assert "list(_REMOTE_CMD_PHRASES)" in code

    def test_the_scanner_returns_uidvalidity(self):
        """★收據的鍵要含 UIDVALIDITY★ 沒回傳的話鍵就只剩 uid ——
        信箱被重建過之後，舊收據會壓住一封剛好撞到同號的新指令。
        """
        ir_src = io.open(os.path.join(REPO_ROOT, "src", "cmuh_common",
                                      "imap_reader.py"), encoding="utf-8").read()
        code = _fn_body_code(ir_src, "check_commands")
        # ★要驗「有沒有把它【指派】進回傳值」★:只驗字面出現的話,
        #   `out` 的初始化與 `conn.response("UIDVALIDITY")` 都含那些字 ——
        #   把指派那一行刪掉,測試照樣綠(突變驗證量到)。
        assert "out['uidvalidity'] =" in code, (
            "沒有把伺服器回的 UIDVALIDITY 放進回傳值")
        assert "response('UIDVALIDITY')" in code

    def test_the_scanner_never_calls_logout(self):
        """★同檔註解明文禁止★：`logout()` 送 LOGOUT + 等回應，socket 半死
        就 hang 住整個 finally；而且先 `_clear_active` 之後連 watchdog 的
        `force_close_active()` 都救不了它。"""
        ir_src = io.open(os.path.join(REPO_ROOT, "src", "cmuh_common",
                                      "imap_reader.py"), encoding="utf-8").read()
        code = _fn_body_code(ir_src, "check_commands")
        assert "logout" not in code
        assert "_force_close_conn(conn)" in code

    def test_expiry_is_computed_by_the_production_function(self):
        """★突變驗證抓到的缺口★ 我的測試全都把 `check_commands` 假掉、
        直接餵 `expired` 旗標 —— 於是把真正算它的那段改成永遠 False，
        測試【全綠】。抽成純函式，測生產的那一段。
        """
        assert ir.command_is_expired(10.0, 1800.0) is False
        assert ir.command_is_expired(1801.0, 1800.0) is True
        # ★時間不明一律算過期★（指令 fail-closed；查詢觸發是相反的）
        assert ir.command_is_expired(None, 1800.0) is True
        # 沒設上限 → 不做這個判斷
        assert ir.command_is_expired(None, 0) is False
        assert ir.command_is_expired(99999.0, None) is False

    def test_the_scanner_uses_that_function(self):
        """★接線★ 純函式沒有被呼叫的話，上面那些性質一件都不會發生。"""
        ir_src = io.open(os.path.join(REPO_ROOT, "src", "cmuh_common",
                                      "imap_reader.py"), encoding="utf-8").read()
        assert "command_is_expired(" in _fn_body_code(ir_src, "check_commands")

    def test_marking_seen_checks_the_server_response(self):
        """★IMAP 可以【正常返回】NO/BAD 而不拋例外★

        無條件回 True 等於「標不掉卻說標好了」→ 指令每一輪重跑一次。
        """
        ir_src = io.open(os.path.join(REPO_ROOT, "src", "cmuh_common",
                                      "imap_reader.py"), encoding="utf-8").read()
        code = _fn_body_code(ir_src, "mark_uids_seen")
        assert "typ, data = conn.uid('store'" in code
        assert "if typ != 'OK'" in code


class TestTheReplyOutlivesTheRestart:
    def test_the_restart_waits_for_the_reply(self, monkeypatch):
        """★[外審 SI 第 1 輪 P2-4]★ 回信是 daemon 緒，而下一行就讓行程退出
        —— 指令做了、使用者卻沒收到回覆，於是他重寄一次＝多一次重啟。"""
        waits = []
        monkeypatch.setattr(cq, "_reply_remote_command",
                            lambda s, subj, body, wait_sec=0.0:
                            waits.append(wait_sec))
        monkeypatch.setattr(cq, "_request_restart_for_update", lambda: None)
        cq._run_remote_command("restart_consult", "doc@x.tw")
        assert waits and waits[0] > 0, "重啟前沒有等回信寄完"

    def test_the_wait_is_bounded(self, monkeypatch):
        """等，但不可以無上限地等（SMTP 也會卡）。"""
        assert 0 < cq._REMOTE_REPLY_WAIT_SEC <= 120
        slow = threading.Event()
        monkeypatch.setattr(cq.threading, "Thread",
                            lambda **k: type("T", (), {
                                "start": lambda _s: None,
                                "join": lambda _s, t: slow.set(),
                                "is_alive": lambda _s: True})())
        cq._reply_remote_command("a@b.c", "s", "b", wait_sec=0.01)
        assert slow.is_set(), "沒有 join(有上限的等待)"


class TestRejectionIsGloballyDeterministic:
    """★[外審 SI-2 第 1 輪 P1]★ 授權要在【目標比對之前】。

    授權與「這封是給誰的」無關，對每一台機器的答案都一樣 —— 所以它是
    全域確定的終局處置，誰看到誰就可以結案。放在目標比對後面的話：
    一封未授權、又指向不存在主機的信，會在每一台都走到「不是給這台的」
    而留著不動整整半小時。而掃描一次只看最新 50 封 —— 持續投遞就能把
    掃描視窗占滿，★把合法指令餓死★，而且不需要通過任何驗證。
    """

    @staticmethod
    def _poll(monkeypatch, items):
        acked, ran = [], []
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(cq, "_ack_command_mail",
                            lambda uids, why, **k: acked.append(list(uids)) or True)
        monkeypatch.setattr(cq, "_run_remote_command",
                            lambda a, s: ran.append(a))
        monkeypatch.setattr(cq, "_alert_trigger_rejected", lambda s: None)
        monkeypatch.setattr(cq, "_reply_remote_command", lambda *a, **k: None)
        cq._poll_remote_commands({"allowed_trigger_senders": ["doc@x.tw"]},
                                 {"items": items, "error": None,
                                  "uidvalidity": "9",
                                  "mailbox_identity": "T:INBOX"})
        return acked, ran

    def test_an_unauthenticated_off_target_command_is_finalized(
            self, monkeypatch):
        acked, ran = self._poll(monkeypatch, [{
            "uid": "7", "sender": "evil@x.tw", "authenticated": False,
            "expired": False, "age_sec": 1.0,
            "subject": "皮膚科會診重開 NOSUCHPC"}])
        assert not ran
        assert acked == [["7"]], (
            "★未授權又不是給這台的 → 留著不動半小時,可被拿來塞滿掃描視窗★")

    def test_a_legitimate_command_for_another_machine_is_still_left_unread(
            self, monkeypatch):
        """反面:通過授權、只是不是給這台的 —— 那台還沒收到,不可以結案。"""
        acked, ran = self._poll(monkeypatch, [{
            "uid": "7", "sender": "doc@x.tw", "authenticated": True,
            "expired": False, "age_sec": 1.0,
            "subject": "皮膚科會診重開 PC-9"}])
        assert not ran
        assert acked == [], "把別台的指令結案掉了"

    def test_authorisation_is_checked_before_targeting(self):
        body = _strip_comments(_fn_src(SRC, "_poll_remote_commands"))
        assert body.index("it.get(\"authenticated\")") < body.index(
            "_remote_command_is_for_me(")


class TestAFailedAckSuppressesTheReply:
    """★[外審 SI-2 第 2 輪 P1]★ 標不掉就不要回信。

    標不掉代表那封信還是 UNSEEN —— 下一輪、以及【每一台】機器都會再回
    一封，直到把寄信配額耗光，而且會把真正的通知洗掉。
    （上一版的回信本來有 gate 在 `acked` 上；★是我批次化時把它弄丟的★。）
    """

    @staticmethod
    def _poll(monkeypatch, ack_ok):
        replies = []
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(cq, "_ack_command_mail",
                            lambda uids, why, **k: ack_ok)
        monkeypatch.setattr(cq, "_run_remote_command", lambda a, s: None)
        monkeypatch.setattr(cq, "_reply_remote_command",
                            lambda s, subj, body, **k: replies.append(subj))
        cq._poll_remote_commands(
            {"allowed_trigger_senders": ["doc@x.tw"]},
            {"items": [{"uid": "7", "sender": "doc@x.tw",
                        "authenticated": True, "expired": True,
                        "age_sec": 99999.0,
                        "subject": "皮膚科會診重開 PC-1"}],
             "error": None, "uidvalidity": "9",
             "mailbox_identity": "T:INBOX"})
        return replies

    def test_a_successful_ack_still_replies(self, monkeypatch):
        assert self._poll(monkeypatch, True)

    def test_a_failed_ack_suppresses_the_reply(self, monkeypatch):
        assert not self._poll(monkeypatch, False), (
            "★標不掉卻回信 → 每輪每台都再回一封,把寄信配額耗光★")


class TestAcknowledgementIsBatchedAndBounded:
    """★[外審 SI-2 第 1 輪 P2]★ 一封一次連線的話，一次掃描最多 50 封
    → 50 條序列 TLS 連線（新連線 + login + select + store）跑在 scheduler
    緒上；而連線還沒建立時 `force_close_active()` 也救不了它 —— 心跳停掉，
    watchdog 反而會把臨床程式重啟。
    """

    def test_terminal_items_are_marked_in_one_call(self, monkeypatch):
        calls = []
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(cq, "_ack_command_mail",
                            lambda uids, why, **k: calls.append(list(uids)) or True)
        monkeypatch.setattr(cq, "_run_remote_command", lambda a, s: None)
        monkeypatch.setattr(cq, "_alert_trigger_rejected", lambda s: None)
        monkeypatch.setattr(cq, "_reply_remote_command", lambda *a, **k: None)
        items = [{"uid": str(i), "sender": "evil@x.tw", "authenticated": False,
                  "expired": False, "age_sec": 1.0,
                  "subject": "皮膚科會診重開 PC-1"} for i in range(20)]
        cq._poll_remote_commands({"allowed_trigger_senders": ["doc@x.tw"]},
                                 {"items": items, "error": None,
                                  "uidvalidity": "9",
                                  "mailbox_identity": "T:INBOX"})
        assert len(calls) == 1, f"開了 {len(calls)} 次連線(應該合併成一次)"
        assert len(calls[0]) == 20

    def test_actionable_commands_are_claimed_one_by_one(self, monkeypatch):
        """★claim-before-execute 不可以批次★:一封寫失敗會讓另一封被誤判成
        已記錄 —— 那封就會在下一輪再執行一次。"""
        keys, ran = [], []
        import cmuh_common.imap_reader as _ir
        monkeypatch.setattr(_ir, "mailbox_identity", lambda: "T:INBOX")
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(cq, "_claim_remote_command",
                            lambda key: keys.append(key) or True)
        monkeypatch.setattr(cq, "_ack_command_mail",
                            lambda uids, why, **k: True)
        monkeypatch.setattr(cq, "_run_remote_command",
                            lambda a, s: ran.append(a))
        monkeypatch.setattr(cq, "_reply_remote_command", lambda *a, **k: None)
        items = [{"uid": str(i), "sender": "doc@x.tw", "authenticated": True,
                  "expired": False, "age_sec": 1.0,
                  "subject": "皮膚科打卡重啟 PC-1"} for i in range(3)]
        cq._poll_remote_commands({"allowed_trigger_senders": ["doc@x.tw"]},
                                 {"items": items, "error": None,
                                  "uidvalidity": "9", "mailbox_identity": "T:INBOX"})
        assert len(ran) == 3
        assert keys == ["T:INBOX|9:0", "T:INBOX|9:1", "T:INBOX|9:2"], keys

    def test_the_ack_itself_is_bounded_and_force_closes(self):
        code = _fn_body_code(SRC, "_ack_command_mail")
        assert "force_close_active(tag=" in code
        assert "force_close_active(clear=True, tag=" in code
        assert "_last_imap_ack_thread" in code

    def test_a_stranded_ack_does_not_pile_up(self, monkeypatch):
        """★兩者都回 False 分不出來★（突變驗證量到：把 single-flight 拿掉，
        測試照樣綠）—— 要數的是「有沒有真的再開一條去連 IMAP」。
        """
        block = threading.Event()
        calls = []
        monkeypatch.setattr(
            ir, "mark_uids_seen",
            lambda uids, **kw: calls.append(list(uids)) or block.wait(20) or True)
        old = cq._last_imap_ack_thread
        cq._last_imap_ack_thread = None
        try:
            assert cq._ack_command_mail(["1"], "x", timeout=0.4) is False
            assert cq._ack_command_mail(["2"], "x", timeout=0.4) is False
            assert calls == [["1"]], (
                f"上一條還卡著卻又開了一條 IMAP 連線:{calls}")
        finally:
            block.set()
            cq._last_imap_ack_thread = old


class TestTheSubjectMustHaveExactlyThreeParts:
    def test_an_extra_token_is_not_a_command(self):
        """★[外審 SI-2 第 1 輪 P3]★ `重開機 PC-1 順便清一下` 不可以被當成
        「重開機 PC-1」執行。寬鬆的地方只有空白。"""
        assert cq.parse_remote_command(
            "[皮膚科遠端指令] 重開機 PC-1 extra") == (None, "")
        assert cq.parse_remote_command(
            "[皮膚科遠端指令] 重啟打卡 PC-1 PC-2") == (None, "")

    def test_exactly_two_tokens_still_parse(self):
        assert cq.parse_remote_command(
            "皮膚科會診重開 PC-1") == ("reboot", "PC-1")


class TestTheLocalReceiptIsTheDedup:
    """★機器名稱可省略之後，去重只能靠本機收據★（使用者定案 2026-08-11）

    已讀旗標是【信箱全域】的：第一台標掉之後其他台再也搜不到那封 UNSEEN，
    於是只有一台會動作。改成每台自己記「這封我做過了」，信件留到過期才標掉。
    """

    @pytest.fixture(autouse=True)
    def _iso(self, tmp_path, monkeypatch):
        import cmuh_common.paths as paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        yield

    def test_the_same_command_is_claimed_only_once(self):
        assert cq._claim_remote_command("9:7") is True
        assert cq._claim_remote_command("9:7") is False
        assert cq._claim_remote_command("9:8") is True

    def test_the_receipt_survives_a_restart(self):
        """★這一條就是「不會變成重開機迴圈」的關鍵★

        重開機之後那封信通常【還沒過期】，會再被掃到一次。收據沒有落地的話
        就會再重開一次，然後再一次…
        """
        assert cq._claim_remote_command("9:7") is True
        importlib.reload(cq)              # 模擬行程重啟（記憶體全清）
        try:
            assert cq._claim_remote_command("9:7") is False
        finally:
            importlib.reload(cq)

    def test_a_different_uidvalidity_is_a_different_command(self):
        """UID 只在 UIDVALIDITY 不變時才穩定；信箱重建過就可能撞號。"""
        assert cq._claim_remote_command("9:7") is True
        assert cq._claim_remote_command("10:7") is True

    def test_an_unwritable_receipt_means_do_not_execute(self, monkeypatch):
        import cmuh_common.atomic_io as aio
        monkeypatch.setattr(
            aio, "atomic_write_json",
            lambda *a, **k: (_ for _ in ()).throw(OSError("readonly")))
        assert cq._claim_remote_command("9:7") is False

    def test_an_unreadable_receipt_means_do_not_execute(self, monkeypatch):
        """★讀不到＝不知道做過沒有★ → 寧可不做（指令一律 fail-closed）。

        ★[外審 SJ 第 1 輪 P1-2]★ 上一版用 `safe_load_json` 並把它 patch 成
        【會拋例外】—— 而生產的那個 helper 把讀取錯誤吞掉、回傳 default，
        永遠不拋。等於在測一個不存在的情境，而真實情況（讀壞→當成空的→
        照樣執行）沒有任何測試。改用會回報狀態的 `safe_load_json_ex`，
        並用它真正的回傳形狀來測。
        """
        import cmuh_common.atomic_io as aio
        for status in ("error", "corrupt"):
            monkeypatch.setattr(aio, "safe_load_json_ex",
                                lambda *a, _s=status, **k: ({}, _s))
            assert cq._claim_remote_command("9:7") is False, status

    def test_a_receipt_file_that_is_not_a_dict_means_do_not_execute(
            self, monkeypatch):
        import cmuh_common.atomic_io as aio
        monkeypatch.setattr(aio, "safe_load_json_ex",
                            lambda *a, **k: (["不是字典"], "ok"))
        assert cq._claim_remote_command("9:7") is False

    def test_old_receipts_are_pruned(self):
        old = cq.time.time() - cq._REMOTE_RECEIPT_TTL_SEC - 60
        assert cq._claim_remote_command("9:old", now=old) is True
        assert cq._claim_remote_command("9:new") is True
        import json
        with open(cq._remote_receipt_path(), encoding="utf-8") as f:
            data = json.load(f)
        assert "9:old" not in data, "過期收據沒有被剪掉 → 檔案無限成長"
        assert "9:new" in data

    def test_the_retention_outlives_the_command_window(self):
        """收據保留期必須遠大於指令時效，否則過期前收據就先被剪掉→重做。"""
        assert cq._REMOTE_RECEIPT_TTL_SEC > cq._REMOTE_CMD_MAX_AGE_SEC * 10

    def test_a_corrupt_timestamp_is_dropped_not_kept(self):
        """★壞掉的時間戳要丟掉，不是留著★

        留著的話它會永遠壓住那個 uid，而 uid 會隨 UIDVALIDITY 重用。
        """
        assert cq._remote_receipt_is_fresh("壞掉", 1000.0) is False
        assert cq._remote_receipt_is_fresh(None, 1000.0) is False
        assert cq._remote_receipt_is_fresh(0, 1000.0) is False
        # ★時間戳在【未來】也是壞資料★:時鐘被往前調過,那筆會活很久很久。
        assert cq._remote_receipt_is_fresh(2000.0, 1000.0) is False
        # 剛寫下的(age=0)當然還新鮮
        assert cq._remote_receipt_is_fresh(1000.0, 1000.0) is True
        assert cq._remote_receipt_is_fresh(999.0, 1000.0) is True
        assert cq._remote_receipt_is_fresh(
            1000.0 - cq._REMOTE_RECEIPT_TTL_SEC - 1, 1000.0) is False


class TestNoTargetMeansEveryConsultMachine:
    def test_a_command_with_no_machine_runs_here(self, monkeypatch):
        """★使用者定案★「皮膚科會診重開」不填機器 → 這一台就要做。
        會去收這個信箱的就是會診查詢那支程式，沒在跑的看不到那封信。
        """
        ran, claimed = [], []
        import cmuh_common.imap_reader as _ir
        monkeypatch.setattr(_ir, "mailbox_identity", lambda: "T:INBOX")
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(cq, "_claim_remote_command",
                            lambda key: claimed.append(key) or True)
        monkeypatch.setattr(cq, "_ack_command_mail", lambda uids, why, **k: True)
        monkeypatch.setattr(cq, "_run_remote_command",
                            lambda a, s: ran.append(a))
        cq._poll_remote_commands(
            {"allowed_trigger_senders": ["doc@x.tw"]},
            {"items": [{"uid": "7", "sender": "doc@x.tw",
                        "authenticated": True, "expired": False,
                        "subject": "皮膚科會診重開", "age_sec": 1.0}],
             "error": None, "uidvalidity": "9", "mailbox_identity": "T:INBOX"})
        assert ran == ["reboot"]
        assert claimed == ["T:INBOX|9:7"]

    def test_it_is_not_marked_seen_so_other_machines_still_see_it(
            self, monkeypatch):
        """★執行過的那一封不可以標已讀★ —— 標掉之後其他正在跑會診查詢的
        電腦就再也搜不到它了（那正是「不填機器」要涵蓋的那些機器）。"""
        marked = []
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(cq, "_claim_remote_command", lambda key: True)
        monkeypatch.setattr(cq, "_ack_command_mail",
                            lambda uids, why, **k: marked.append(list(uids)) or True)
        monkeypatch.setattr(cq, "_run_remote_command", lambda a, s: None)
        cq._poll_remote_commands(
            {"allowed_trigger_senders": ["doc@x.tw"]},
            {"items": [{"uid": "7", "sender": "doc@x.tw",
                        "authenticated": True, "expired": False,
                        "subject": "皮膚科會診重開", "age_sec": 1.0}],
             "error": None, "uidvalidity": "9", "mailbox_identity": "T:INBOX"})
        assert marked == []


class TestAnExecutedCommandDoesNotLaterClaimNobodyRanIt:
    """★[外審 SJ 第 1 輪 P1-1]★ 執行過的信【刻意不標已讀】（要留給其他也在
    跑會診查詢的電腦看），所以 30 分鐘後它會再被掃到一次並走到「已過期」。
    無條件回信的話，使用者先收到成功信、再收到一封說沒人執行 ——
    他會再寄一次，於是多一次重啟／重開機。
    """

    @pytest.fixture(autouse=True)
    def _iso(self, tmp_path, monkeypatch):
        import cmuh_common.paths as paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        yield

    @staticmethod
    def _expire(monkeypatch, replies):
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(cq, "_ack_command_mail", lambda uids, why, **k: True)
        monkeypatch.setattr(cq, "_run_remote_command", lambda a, s: None)
        monkeypatch.setattr(cq, "_reply_remote_command",
                            lambda s, subj, body, **k: replies.append(
                                (subj, body)))
        cq._poll_remote_commands(
            {"allowed_trigger_senders": ["doc@x.tw"]},
            {"items": [{"uid": "7", "sender": "doc@x.tw",
                        "authenticated": True, "expired": True,
                        "subject": "皮膚科會診重開", "age_sec": 99999.0}],
             "error": None, "uidvalidity": "9", "mailbox_identity": "T:INBOX"})

    @staticmethod
    def _run_it(monkeypatch, *, crash=False):
        """跑完一次正常的執行路徑（或在執行途中掛掉）。"""
        import cmuh_common.imap_reader as _ir
        monkeypatch.setattr(_ir, "mailbox_identity", lambda: "T:INBOX")
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(cq, "_ack_command_mail", lambda uids, why, **k: True)

        def _boom(action, sender):
            raise RuntimeError("執行途中掛掉")

        monkeypatch.setattr(cq, "_run_remote_command",
                            _boom if crash else (lambda a, s: None))
        scan = {"items": [{"uid": "7", "sender": "doc@x.tw",
                           "authenticated": True, "expired": False,
                           "subject": "皮膚科會診重開", "age_sec": 1.0}],
                "error": None, "uidvalidity": "9", "mailbox_identity": "T:INBOX"}
        cfg = {"allowed_trigger_senders": ["doc@x.tw"]}
        if crash:
            with pytest.raises(RuntimeError):
                cq._poll_remote_commands(cfg, scan)
        else:
            cq._poll_remote_commands(cfg, scan)

    def test_a_command_we_already_ran_gets_no_failure_reply(self, monkeypatch):
        self._run_it(monkeypatch)
        replies = []
        self._expire(monkeypatch, replies)
        assert not replies, "★做過了卻回信說沒人做★ 使用者會再寄一次"

    def test_a_command_nobody_ran_still_gets_the_reply(self, monkeypatch):
        replies = []
        self._expire(monkeypatch, replies)
        assert replies, "真的沒人做的時候要講"

    def test_claiming_another_command_does_not_drop_the_earlier_receipt(self):
        """★剪枝不可以把還在保留期內的別筆丟掉★ → 那就是重開機迴圈。

        `_claim_remote_command` 是「先看鍵在不在，再剪枝、再寫回」。舊收據
        被剪掉不會當場出事 —— 出事的是【下一封指令進來之後】：那一封把檔案
        重寫成只剩它自己，於是前一封（信還沒過期、還在信箱裡）又變成沒做過，
        下一輪再重開一次。收據的值換形狀時，剪枝那一段最容易忘了跟著換。
        """
        assert cq._claim_remote_command("9:7") is True
        assert cq._claim_remote_command("9:8") is True
        assert cq._claim_remote_command("9:7") is False, (
            "★第一封被剪掉了★ 它的信還在信箱裡,會被再執行一次")

    def test_claiming_is_not_the_same_as_having_run_it(self, monkeypatch):
        """★[外審 SJ 第 2 輪 P1]★ 收據是【執行之前】就落地的（不然重開機之後
        會再重開一次）。只看「鍵在不在」的話，claim 到執行之間掛掉會被講成
        執行過 —— 連帶把過期通知也吃掉，使用者兩頭落空：既沒做，也沒人告訴他。
        """
        assert cq._claim_remote_command("9:7") is True
        assert cq._remote_command_was_done("9:7") is False, (
            "★claim ≠ 做完★")
        cq._mark_remote_command_done("9:7")
        assert cq._remote_command_was_done("9:7") is True

    def test_a_crash_midway_still_lets_the_user_know(self, monkeypatch):
        self._run_it(monkeypatch, crash=True)
        assert cq._claim_remote_command("T:INBOX|9:7") is False, (
            "claim 還在 → 不會重複執行")
        replies = []
        self._expire(monkeypatch, replies)
        assert replies, "★沒做完就要講★ 不然使用者以為做好了"


class TestTheExpiryReplyOnlySpeaksForThisMachine:
    """★[外審 SJ 第 2 輪 P1]★ 一台機器沒有「全體有沒有做」的資訊。

    不指定機器的信要留給每一台看，所以永遠 UNSEEN —— 晚開機、或 IMAP 斷了
    半小時的那一台，第一眼看到的就是「已過期」，它身上當然沒有收據。
    舊文案讓它代表全體宣告「沒有被任何機器執行」，而別台可能早就做完並寄了
    成功信；使用者於是再寄一次，門診中多一次重開機。
    """

    @pytest.fixture(autouse=True)
    def _iso(self, tmp_path, monkeypatch):
        import cmuh_common.paths as paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        yield

    @staticmethod
    def _reply(monkeypatch):
        sent = []
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-2")
        monkeypatch.setattr(cq, "_ack_command_mail", lambda uids, why, **k: True)
        monkeypatch.setattr(cq, "_run_remote_command", lambda a, s: None)
        monkeypatch.setattr(cq, "_reply_remote_command",
                            lambda s, subj, body, **k: sent.append((subj, body)))
        cq._poll_remote_commands(
            {"allowed_trigger_senders": ["doc@x.tw"]},
            {"items": [{"uid": "7", "sender": "doc@x.tw",
                        "authenticated": True, "expired": True,
                        "subject": "皮膚科會診重開 PC-1", "age_sec": 99999.0}],
             "error": None, "uidvalidity": "9", "mailbox_identity": "T:INBOX"})
        assert len(sent) == 1
        return sent[0]

    def test_it_does_not_claim_to_speak_for_every_machine(self, monkeypatch):
        subj, body = self._reply(monkeypatch)
        assert "沒有被任何機器執行" not in body, (
            "★這台機器不知道別台做了沒有★")
        assert "任何機器" not in subj

    def test_it_names_the_machine_that_did_not_run_it(self, monkeypatch):
        subj, body = self._reply(monkeypatch)
        assert "PC-2" in body, "要講清楚是哪一台在說話"
        assert "本機沒有執行" in subj

    def test_it_tells_the_user_not_to_resend(self, monkeypatch):
        _subj, body = self._reply(monkeypatch)
        assert "不要再寄一次" in body, (
            "★沒有這一句就等於叫他再寄一次★（別台可能早就做完了）")


class TestAMissingUidvalidityIsFailClosed:
    """★[外審 SJ 第 1 輪 P2-3]★ 收據的鍵會變成 `":<uid>"`；下一輪拿到真的
    UIDVALIDITY 之後鍵就不一樣了，同一封未讀信會【再執行一次】。
    """

    def test_nothing_is_executed_without_uidvalidity(self, monkeypatch):
        ran, marked = [], []
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(cq, "_claim_remote_command", lambda key: True)
        monkeypatch.setattr(cq, "_ack_command_mail",
                            lambda uids, why, **k: marked.append(list(uids)) or True)
        monkeypatch.setattr(cq, "_run_remote_command",
                            lambda a, s: ran.append(a))
        cq._poll_remote_commands(
            {"allowed_trigger_senders": ["doc@x.tw"]},
            {"items": [{"uid": "7", "sender": "doc@x.tw",
                        "authenticated": True, "expired": False,
                        "subject": "皮膚科會診重開", "age_sec": 1.0}],
             "error": None, "uidvalidity": ""})
        assert not ran
        assert marked == [], "不執行的也不可以標已讀(它要能自然過期)"


class TestReplyPrefixesSurviveTheScanner:
    """★[外審 SJ 第 1 輪 P2-4]★ 掃描端原本是 `subj.startswith(短語)`，而解析端
    是在任意位置 `find` —— 於是 `Re: 皮膚科會診重開` 在掃描端就被濾掉，
    根本到不了解析端，而契約明明說允許 `Re:`。★兩邊各寫一套判準，
    就會有一邊靜默失效。★
    """

    def test_the_two_sides_share_one_normaliser(self):
        scan = _fn_body_code(
            io.open(os.path.join(REPO_ROOT, "src", "cmuh_common",
                                 "imap_reader.py"), encoding="utf-8").read(),
            "check_commands")
        assert "normalize_subject(subj)" in scan
        assert "normalize_subject(subject)" in _fn_body_code(
            SRC, "parse_remote_command")

    def test_reply_prefixes_are_stripped(self):
        assert ir.normalize_subject("Re: 皮膚科會診重開") == "皮膚科會診重開"
        assert ir.normalize_subject("RE: Re: 皮膚科會診重開") == "皮膚科會診重開"
        assert ir.normalize_subject("Fwd: 　皮膚科會診重開 PC-1") == (
            "皮膚科會診重開 PC-1")
        assert ir.normalize_subject("回覆: 皮膚科會診重開") == "皮膚科會診重開"
        assert ir.normalize_subject(None) == ""

    def test_a_replied_command_still_parses(self):
        assert cq.parse_remote_command(
            "Re: 皮膚科會診重開") == ("reboot", "")

    def test_the_phrase_must_be_at_the_start(self):
        """★不可以用 `find`★:那會讓「請不要寄 皮膚科會診重開」也變成指令。"""
        assert cq.parse_remote_command(
            "請不要寄 皮膚科會診重開") == (None, "")


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
