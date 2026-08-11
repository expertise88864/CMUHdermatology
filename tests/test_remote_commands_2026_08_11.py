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
    def test_the_three_actions(self):
        for word, code in (("重啟會診", "restart_consult"),
                           ("重啟打卡", "restart_autoclock"),
                           ("重開機", "reboot")):
            assert cq.parse_remote_command(
                f"[皮膚科遠端指令] {word} PC-1") == (code, "PC-1")

    def test_a_reply_prefix_and_extra_spaces_still_parse(self):
        """郵件客戶端會插 `Re:`、多餘空白、全形空格。"""
        assert cq.parse_remote_command(
            "Re: 　[皮膚科遠端指令]　 重開機   PC-1 ") == ("reboot", "PC-1")

    def test_a_missing_machine_is_not_a_command(self):
        """★機器是必填★ 少了它，一封信會讓每一台診間電腦都動作。"""
        assert cq.parse_remote_command("[皮膚科遠端指令] 重開機") == (None, "")

    def test_an_unknown_action_is_not_a_command(self):
        assert cq.parse_remote_command(
            "[皮膚科遠端指令] 關機 PC-1") == (None, "")

    def test_actions_are_matched_exactly_not_fuzzily(self):
        """★「重開機」與「重啟會診」差一個字，代價差很多★ —— 絕不模糊比對。"""
        for bad in ("重開", "重開機器", "重啟", "重啟會診查詢"):
            assert cq.parse_remote_command(
                f"[皮膚科遠端指令] {bad} PC-1") == (None, ""), bad

    def test_a_normal_mail_is_not_a_command(self):
        for subj in ("", "皮膚科會診觸發", "重開機 PC-1", None):
            assert cq.parse_remote_command(subj) == (None, "")

    def test_targeting(self, monkeypatch):
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        assert cq._remote_command_is_for_me("PC-1") is True
        assert cq._remote_command_is_for_me("PC-2") is False
        assert cq._remote_command_is_for_me("") is False

    def test_there_is_no_broadcast_target(self):
        """★[外審 SI 第 1 輪 P1-1] 沒有「全部」★

        已讀旗標是【信箱全域】的狀態：第一台處理完就把信標掉，其他還沒
        SEARCH 的機器再也看不到那封 UNSEEN —— 廣播根本沒有廣播到。
        要做對得另外設計「UID+主機名的本機收據」，而那是為了一個
        「一封信重開所有診間電腦」的高風險功能再加一套機器。直接拿掉。
        """
        assert not hasattr(cq, "_REMOTE_CMD_ALL")
        code = _fn_body_code(SRC, "_remote_command_is_for_me")
        assert "全部" not in code

    def test_an_unknown_hostname_never_matches(self, monkeypatch):
        """取不到自己的名字時，不可以「猜」自己就是目標。"""
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "")
        assert cq._remote_command_is_for_me("PC-1") is False
        assert cq._remote_command_is_for_me("") is False


class TestAuthorizationIsFailClosed:
    @staticmethod
    def _poll(monkeypatch, item, allow=("doc@x.tw",)):
        ran, marked = [], []
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(
            ir, "check_commands",
            lambda *a, **k: {"items": [item], "error": None})
        monkeypatch.setattr(ir, "mark_uids_seen",
                            lambda uids: marked.append(list(uids)) or True)
        monkeypatch.setattr(cq, "_run_remote_command",
                            lambda action, sender: ran.append(action))
        monkeypatch.setattr(cq, "_alert_trigger_rejected", lambda s: None)
        cq._poll_remote_commands({"allowed_trigger_senders": list(allow)},
                                 {"items": [item], "error": None})
        return ran, marked

    @staticmethod
    def _item(**kw):
        base = {"uid": "7", "sender": "doc@x.tw", "authenticated": True,
                "subject": "[皮膚科遠端指令] 重啟打卡 PC-1", "age_sec": 10.0}
        base.update(kw)
        return base

    def test_an_authorised_command_runs(self, monkeypatch):
        ran, marked = self._poll(monkeypatch, self._item())
        assert ran == ["restart_autoclock"]
        assert marked == [["7"]]

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
            self._item(subject="[皮膚科遠端指令] 重開機 PC-2"))
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


class TestMarkFirstThenExecute:
    def test_a_command_that_cannot_be_marked_is_not_executed(self,
                                                             monkeypatch):
        """★這是與查詢觸發【相反】的取捨★

        標不掉還執行的話，每一輪(20 秒)都會再重啟一次 —— 無限重啟迴圈，
        而那正是「程式一直不對勁」時最可能發生的狀況。
        指令遺失只要重寄一次；指令重複沒有人救得回來。
        """
        ran = []
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(ir, "check_commands", lambda *a, **k: {
            "items": [{"uid": "7", "sender": "doc@x.tw", "authenticated": True,
                       "subject": "[皮膚科遠端指令] 重開機 PC-1",
                       "age_sec": 1.0}], "error": None})
        monkeypatch.setattr(ir, "mark_uids_seen", lambda uids: False)
        monkeypatch.setattr(cq, "_run_remote_command",
                            lambda a, s: ran.append(a))
        cq._poll_remote_commands(
            {"allowed_trigger_senders": ["doc@x.tw"]},
            {"items": [{"uid": "7", "sender": "doc@x.tw",
                        "authenticated": True, "expired": False,
                        "subject": "[皮膚科遠端指令] 重開機 PC-1",
                        "age_sec": 1.0}], "error": None})
        assert not ran

    def test_marking_happens_before_running(self):
        body = _strip_comments(_fn_src(SRC, "_poll_remote_commands"))
        assert body.index("_ack_command_mail(") < body.index(
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
        assert "startswith(prefix)" in code
        i = code.index("startswith(prefix)")
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
                            lambda *a, **k: {"items": [item], "error": None})
        monkeypatch.setattr(ir, "mark_uids_seen",
                            lambda uids: acked.append(list(uids)) or True)
        monkeypatch.setattr(cq, "_run_remote_command",
                            lambda a, s: pytest.fail("不該執行"))
        monkeypatch.setattr(cq, "_alert_trigger_rejected", lambda s: None)
        cq._poll_remote_commands({"allowed_trigger_senders": ["doc@x.tw"]},
                                 {"items": [item], "error": None})
        return acked

    def test_a_malformed_command_is_acknowledged(self, monkeypatch):
        assert self._poll(monkeypatch, {
            "uid": "7", "sender": "doc@x.tw", "authenticated": True,
            "subject": "[皮膚科遠端指令] 亂寫", "age_sec": 1.0,
            "expired": False}) == [["7"]]

    def test_an_expired_command_is_acknowledged(self, monkeypatch):
        """過期是【絕對】的（依伺服器收信時刻）→ 誰看到誰結案。"""
        assert self._poll(monkeypatch, {
            "uid": "7", "sender": "doc@x.tw", "authenticated": True,
            "subject": "[皮膚科遠端指令] 重開機 PC-1", "age_sec": 99999.0,
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
        monkeypatch.setattr(ir, "mark_uids_seen", lambda uids: True)
        monkeypatch.setattr(cq, "_run_remote_command",
                            lambda a, s: pytest.fail("過期的不該執行"))
        monkeypatch.setattr(cq, "_reply_remote_command",
                            lambda s, subj, body, **k: replies.append(body))
        cq._poll_remote_commands(
            {"allowed_trigger_senders": ["doc@x.tw"]},
            {"items": [{"uid": "7", "sender": "doc@x.tw",
                        "authenticated": True, "expired": True,
                        "subject": "[皮膚科遠端指令] 重開機 全部",
                        "age_sec": 99999.0}], "error": None})
        assert replies, "★過期＝沒有任何機器接手,卻沒有告訴使用者★"
        # ★不可以只驗「全部」這兩個字★:回信本來就會把目標原樣回聲一次,
        #   那個斷言會被【回聲】滿足而不是被指引滿足(突變驗證量到:把那句
        #   指引整行刪掉,測試照樣綠)。要驗指引本身獨有的字。
        assert "不支援" in replies[0], "要講出「不支援全部」這個常見誤解"
        assert "一次只能指定一台" in replies[0]
        assert "沒有在跑" in replies[0]

    def test_an_expired_command_from_a_stranger_gets_no_reply(self,
                                                              monkeypatch):
        """不對偽造／未授權的位址回信（那會變成一個回信放大器）。"""
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(ir, "mark_uids_seen", lambda uids: True)
        monkeypatch.setattr(cq, "_run_remote_command",
                            lambda a, s: pytest.fail("不該執行"))
        monkeypatch.setattr(cq, "_reply_remote_command",
                            lambda *a, **k: pytest.fail("不該回信給未授權者"))
        cq._poll_remote_commands(
            {"allowed_trigger_senders": ["doc@x.tw"]},
            {"items": [{"uid": "7", "sender": "evil@x.tw",
                        "authenticated": False, "expired": True,
                        "subject": "[皮膚科遠端指令] 重開機 PC-1",
                        "age_sec": 99999.0}], "error": None})

    def test_a_command_for_another_machine_is_left_unread(self, monkeypatch):
        """★這一種不可以結案★ —— 那台機器還沒收到。
        它不會永遠留著：半小時後會變成「已過期」而被結掉。"""
        assert self._poll(monkeypatch, {
            "uid": "7", "sender": "doc@x.tw", "authenticated": True,
            "subject": "[皮膚科遠端指令] 重開機 PC-9", "age_sec": 1.0,
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
        assert "force_close_active()" in code
        assert "force_close_active(clear=True)" in code
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
                                 {"items": items, "error": None})
        return acked, ran

    def test_an_unauthenticated_off_target_command_is_finalized(
            self, monkeypatch):
        acked, ran = self._poll(monkeypatch, [{
            "uid": "7", "sender": "evil@x.tw", "authenticated": False,
            "expired": False, "age_sec": 1.0,
            "subject": "[皮膚科遠端指令] 重開機 NOSUCHPC"}])
        assert not ran
        assert acked == [["7"]], (
            "★未授權又不是給這台的 → 留著不動半小時,可被拿來塞滿掃描視窗★")

    def test_a_legitimate_command_for_another_machine_is_still_left_unread(
            self, monkeypatch):
        """反面:通過授權、只是不是給這台的 —— 那台還沒收到,不可以結案。"""
        acked, ran = self._poll(monkeypatch, [{
            "uid": "7", "sender": "doc@x.tw", "authenticated": True,
            "expired": False, "age_sec": 1.0,
            "subject": "[皮膚科遠端指令] 重開機 PC-9"}])
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
                        "subject": "[皮膚科遠端指令] 重開機 PC-1"}],
             "error": None})
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
                  "subject": "[皮膚科遠端指令] 重開機 PC-1"} for i in range(20)]
        cq._poll_remote_commands({"allowed_trigger_senders": ["doc@x.tw"]},
                                 {"items": items, "error": None})
        assert len(calls) == 1, f"開了 {len(calls)} 次連線(應該合併成一次)"
        assert len(calls[0]) == 20

    def test_actionable_commands_are_still_marked_one_by_one(self,
                                                             monkeypatch):
        """★mark-before-execute 不可以批次★:一封標失敗會讓另一封被誤判成
        已結案 —— 那封就會在下一輪再執行一次。"""
        calls, ran = [], []
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(cq, "_ack_command_mail",
                            lambda uids, why, **k: calls.append(list(uids)) or True)
        monkeypatch.setattr(cq, "_run_remote_command",
                            lambda a, s: ran.append(a))
        monkeypatch.setattr(cq, "_reply_remote_command", lambda *a, **k: None)
        items = [{"uid": str(i), "sender": "doc@x.tw", "authenticated": True,
                  "expired": False, "age_sec": 1.0,
                  "subject": "[皮膚科遠端指令] 重啟打卡 PC-1"} for i in range(3)]
        cq._poll_remote_commands({"allowed_trigger_senders": ["doc@x.tw"]},
                                 {"items": items, "error": None})
        assert len(ran) == 3
        assert calls == [["0"], ["1"], ["2"]], calls

    def test_the_ack_itself_is_bounded_and_force_closes(self):
        code = _fn_body_code(SRC, "_ack_command_mail")
        assert "force_close_active()" in code
        assert "force_close_active(clear=True)" in code
        assert "_last_imap_ack_thread" in code

    def test_a_stranded_ack_does_not_pile_up(self, monkeypatch):
        """★兩者都回 False 分不出來★（突變驗證量到：把 single-flight 拿掉，
        測試照樣綠）—— 要數的是「有沒有真的再開一條去連 IMAP」。
        """
        block = threading.Event()
        calls = []
        monkeypatch.setattr(
            ir, "mark_uids_seen",
            lambda uids: calls.append(list(uids)) or block.wait(20) or True)
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
            "[皮膚科遠端指令] 重開機 PC-1") == ("reboot", "PC-1")


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
