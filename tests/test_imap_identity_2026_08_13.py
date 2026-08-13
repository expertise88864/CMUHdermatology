# -*- coding: utf-8 -*-
"""[批次AD-4] IMAP identity 統一(外審 2026-08-12 P1-06 + P2-03/04/07)。

★UID 只在【同一個帳號、同一個 mailbox、同一個 UIDVALIDITY 世代】裡才有
意義★。觸發 journal 原本只用 uid 當鍵:信箱重建(UIDVALIDITY 改變)或
換 IMAP 帳號後,新的一封信可能拿到同一個 uid —— add 蓋掉舊待辦、done
結掉錯的世代、開機補跑分不出哪一封。遠端指令收據(P2-07)同病。
標已讀是一條【新的】連線(P1-06 下半):掃描到 STORE 之間世代變了,
同一個 uid 已指向別封信 —— STORE 會把不相干的信標成已讀。
"""
import importlib
import io
import json
import os
import sys
import time

import pytest

NL = chr(10)
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

cq = importlib.import_module("consult_query")
ir = importlib.import_module("cmuh_common.imap_reader")

SRC = io.open(os.path.join(REPO_ROOT, "src", "consult_query.py"),
              encoding="utf-8").read()
IR_SRC = io.open(os.path.join(REPO_ROOT, "src", "cmuh_common",
                              "imap_reader.py"), encoding="utf-8").read()


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    import cmuh_common.paths as paths
    monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
    yield


class TestMailboxIdentity:
    def test_no_account_means_empty(self, monkeypatch):
        monkeypatch.setattr(ir, "_load_imap_settings",
                            lambda: {"username": ""})
        assert ir.mailbox_identity() == ""

    def test_the_account_is_hashed_not_plaintext(self, monkeypatch):
        """鍵會進磁碟上的 journal —— 不可以把帳號明文散出去。"""
        monkeypatch.setattr(ir, "_load_imap_settings",
                            lambda: {"username": "Doctor@GMail.com "})
        ident = ir.mailbox_identity()
        assert ident.endswith(":INBOX")
        assert "doctor" not in ident.lower().replace(":inbox", "")
        # 正規化:大小寫/空白不影響身分
        monkeypatch.setattr(ir, "_load_imap_settings",
                            lambda: {"username": "doctor@gmail.com"})
        assert ir.mailbox_identity() == ident


class TestTheJournalKeyCarriesTheGeneration:
    def test_the_key_is_namespaced(self):
        key = cq._trigger_journal_add("42", "doc@x.tw", "9", "T:INBOX")
        assert key == "T:INBOX|9|42"
        pending, ok = cq._trigger_journal_pending()
        assert ok and pending[key]["uid"] == "42"
        assert pending[key]["uidvalidity"] == "9"

    def test_missing_generation_refuses_to_land(self):
        """★鍵不穩定就不落地★(fail-closed;信留在未讀,下一輪重來)。"""
        assert cq._trigger_journal_add("42", "doc@x.tw", "", "T:INBOX") == ""
        assert cq._trigger_journal_add("42", "doc@x.tw", "9", "") == ""
        pending, ok = cq._trigger_journal_pending()
        assert ok and pending == {}

    def test_same_uid_in_a_new_generation_is_a_new_entry(self):
        """信箱重建後同一個 uid=另一封信 —— 不可以互相蓋。"""
        k1 = cq._trigger_journal_add("42", "a@x.tw", "9", "T:INBOX")
        k2 = cq._trigger_journal_add("42", "b@x.tw", "10", "T:INBOX")
        assert k1 != k2
        pending, _ = cq._trigger_journal_pending()
        assert pending[k1]["sender"] == "a@x.tw"
        assert pending[k2]["sender"] == "b@x.tw"


class TestHandoffIsFailClosedOnIdentity:
    @staticmethod
    def _run(monkeypatch, uidvalidity, identity="T:INBOX"):
        seen, jobs, undone = [], [], []
        monkeypatch.setattr(ir, "mark_uids_seen",
                            lambda uids, **kw: seen.append((list(uids), kw))
                            or True)
        monkeypatch.setattr(cq, "trigger_job_async",
                            lambda *a, **k: jobs.append((a, k)))
        monkeypatch.setattr(cq, "_undo_trigger_dedup",
                            lambda a: undone.append(a))
        # ★身分是【參數】,不是 handoff 自己載的★(外審 AD-4 第 1 輪 P1-3)
        cq._handoff_email_triggers([("7", "doc@x.tw", True)], ["doc@x.tw"],
                                   uidvalidity=uidvalidity, identity=identity)
        return seen, jobs, undone

    def test_no_identity_means_no_handoff(self, monkeypatch):
        """★掃描帶不回帳號身分 → 同樣不接手★(與缺世代同一個 fail-closed)。"""
        seen, jobs, undone = self._run(monkeypatch, uidvalidity="9",
                                       identity="")
        assert not seen and not jobs
        assert undone == ["doc@x.tw"]

    def test_no_uidvalidity_means_no_handoff(self, monkeypatch):
        """★不接手=不落地、不標已讀★ 信留 UNSEEN 下輪重來;去重窗要撤銷
        (否則五分鐘後它被去重的終局處置標掉:工作沒做、信卻消失)。"""
        seen, jobs, undone = self._run(monkeypatch, uidvalidity="")
        assert not seen and not jobs
        assert undone == ["doc@x.tw"]
        pending, _ = cq._trigger_journal_pending()
        assert pending == {}

    def test_a_normal_handoff_passes_the_generation_all_the_way(
            self, monkeypatch):
        seen, jobs, _ = self._run(monkeypatch, uidvalidity="9")
        # 標已讀帶著掃描時的世代(新連線要驗)
        assert seen and seen[0][0] == ["7"]
        assert seen[0][1].get("expect_uidvalidity") == "9"
        assert seen[0][1].get("expect_identity") == "T:INBOX", (
            "標已讀也要驗帳號(掃描後換憑證,同 uid 可能在別的帳號)")
        # 交給 worker 的是 journal 鍵(結案用),不是裸 uid
        assert jobs and jobs[0][1]["trigger_uids"] == ("T:INBOX|9|7",)

    def test_the_scanner_caller_passes_the_generation(self):
        """★接線★ 呼叫端不傳 uidvalidity 的話,fail-closed 會把【所有】
        email 觸發擋死 —— 沒有出口的 fail-closed。"""
        i = SRC.index("_handoff_email_triggers(" + NL)
        block = SRC[i:i + 600]
        assert "uidvalidity=str(" in block, (
            "★掃描端沒把 UIDVALIDITY 傳進 handoff★")
        assert "identity=str(" in block, (
            "★掃描端沒把帳號身分傳進 handoff★(身分要與掃描同源)")
        assert 'result["uidvalidity"] = read_uidvalidity(conn)' in IR_SRC, (
            "★check_trigger 沒把 UIDVALIDITY 放進結果★")


class TestMarkSeenVerifiesTheGeneration:
    class _Conn:
        def __init__(self, uv=b"10"):
            self._uv = uv
            self.stored = []

        def login(self, *a):
            pass

        def select(self, *a, **k):
            return ("OK", [b"1"])

        def response(self, key):
            return ("OK", [self._uv] if key == "UIDVALIDITY" else [None])

        def uid(self, *a):
            self.stored.append(a)
            return ("OK", [b""])

        def shutdown(self):
            pass

        def sock(self):
            pass

    def test_a_changed_generation_refuses_to_store(self, monkeypatch):
        """掃描到 STORE 之間信箱被重建 → 同一個 uid 已指向【別封信】。"""
        conn = self._Conn(uv=b"10")
        monkeypatch.setattr(ir, "_load_imap_settings",
                            lambda: {"host": "h", "port": 993,
                                     "username": "u", "password": "p"})
        monkeypatch.setattr(ir.imaplib, "IMAP4_SSL",
                            lambda *a, **k: conn)
        monkeypatch.setattr(ir, "_set_active", lambda c, tag="": None)
        monkeypatch.setattr(ir, "_clear_active", lambda c: None)
        monkeypatch.setattr(ir, "_force_close_conn", lambda c: None)
        assert ir.mark_uids_seen(["7"], expect_uidvalidity="9") is False
        assert conn.stored == [], "★世代變了還 STORE★ 標掉的是不相干的信"

    def test_the_same_generation_stores(self, monkeypatch):
        conn = self._Conn(uv=b"9")
        monkeypatch.setattr(ir, "_load_imap_settings",
                            lambda: {"host": "h", "port": 993,
                                     "username": "u", "password": "p"})
        monkeypatch.setattr(ir.imaplib, "IMAP4_SSL",
                            lambda *a, **k: conn)
        monkeypatch.setattr(ir, "_set_active", lambda c, tag="": None)
        monkeypatch.setattr(ir, "_clear_active", lambda c: None)
        monkeypatch.setattr(ir, "_force_close_conn", lambda c: None)
        assert ir.mark_uids_seen(["7"], expect_uidvalidity="9") is True
        assert conn.stored, "同一個世代要照常標"


class TestReplaySurvivesOneBadRecord:
    def test_a_bad_timestamp_does_not_kill_the_whole_replay(
            self, monkeypatch):
        """★[P2-04] 一筆壞資料不可以讓整輪補跑中止★"""
        path = cq._trigger_journal_path()
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "T|9|1": {"sender": "bad@x.tw", "at": "not-a-number",
                          "uid": "1"},
                "T|9|2": {"sender": "good@x.tw", "at": time.time(),
                          "uid": "2"},
            }, f)
        jobs = []
        monkeypatch.setattr(cq, "trigger_job_async",
                            lambda *a, **k: jobs.append(k))
        n = cq.resume_pending_triggers()
        assert n == 1, "★一筆壞時間戳讓好的那筆也補不了★"
        assert jobs[0]["override_recipients"] == ["good@x.tw"]
        # 壞的那筆走「過時結案」出口,不會永遠留著
        pending, _ = cq._trigger_journal_pending()
        assert "T|9|1" not in pending


class TestRemoteReceiptsAreNamespaced:
    def test_the_receipt_key_includes_the_account(self, monkeypatch):
        """★[P2-07] 換帳號後相同的 UIDVALIDITY+UID 可能撞號★"""
        claimed = []
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(cq, "_claim_remote_command",
                            lambda key: claimed.append(key) or True)
        monkeypatch.setattr(cq, "_run_remote_command", lambda a, s: None)
        monkeypatch.setattr(cq, "_ack_command_mail", lambda u, w, **k: True)
        cq._poll_remote_commands(
            {"allowed_trigger_senders": ["doc@x.tw"]},
            {"items": [{"uid": "7", "sender": "doc@x.tw",
                        "authenticated": True, "expired": False,
                        "subject": "皮膚科會診重開", "age_sec": 1.0}],
             "error": None, "uidvalidity": "9",
             "mailbox_identity": "T:INBOX"})
        assert claimed == ["T:INBOX|9:7"]

    def test_no_identity_refuses_all_actionable(self, monkeypatch):
        ran = []
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(cq, "_claim_remote_command", lambda key: True)
        monkeypatch.setattr(cq, "_run_remote_command",
                            lambda a, s: ran.append(a))
        monkeypatch.setattr(cq, "_ack_command_mail", lambda u, w, **k: True)
        cq._poll_remote_commands(
            {"allowed_trigger_senders": ["doc@x.tw"]},
            {"items": [{"uid": "7", "sender": "doc@x.tw",
                        "authenticated": True, "expired": False,
                        "subject": "皮膚科會診重開", "age_sec": 1.0}],
             "error": None, "uidvalidity": "9"})
        assert not ran, "★取不到帳號身分還執行★ 收據鍵不穩定,可能重複執行"


class TestStaleStoreChecksTheReply:
    def test_the_stale_store_looks_at_typ(self):
        """★[P2-03] NO/BAD 是正常返回不拋★ 當成功的話陳舊觸發信一直
        UNSEEN,每輪重掃、重發告警。"""
        i = IR_SRC.index('typ, _d = conn.uid("store", id_list')
        after = IR_SRC[i:i + 300]
        assert 'if typ != "OK":' in after, "STORE 的回覆沒有被檢查"


class TestLegacyReceiptsAreMigrated:
    """★[外審 AD-4 第 1/2 輪 P1-1]★ 部署後 30 分鐘內,剛執行過的指令收據
    還是舊鍵 `{uv}:{uid}`。★不可以改綁到目前帳號★(第 2 輪):舊收據沒記
    帳號,換帳號不清收據 —— 盲目綁的話 A 的收據會壓掉 B 的合法新指令。
    改成【曖昧 fail-closed】:比對到就不執行、不回信,等 24h TTL 過期。"""

    def _seed_legacy(self, *, done, age_sec=0.0):
        import json
        with open(cq._remote_receipt_path(), "w", encoding="utf-8") as f:
            json.dump({"9:7": {"at": time.time() - age_sec,
                               "done": done}}, f)

    def test_legacy_receipts_are_never_rebound(self):
        """★沒有證據就不認領★ 舊鍵原地不動(靠 TTL 自然過期)。"""
        self._seed_legacy(done=True)
        assert cq._remote_command_was_done("T:INBOX|9:7") is True, (
            "done=True 的曖昧舊收據要壓掉過期回信(不然使用者被叫去重寄)")
        import json
        data = json.load(open(cq._remote_receipt_path(), encoding="utf-8"))
        assert "9:7" in data, "★舊收據被改綁/刪除了★ 它不屬於任何確定的帳號"
        assert "T:INBOX|9:7" not in data

    def test_a_recently_executed_command_is_not_rerun_after_deploy(
            self, monkeypatch):
        """端到端:部署前剛 claim 過的指令,部署後同一輪【不可以】再執行。"""
        self._seed_legacy(done=False)          # claim 過、還沒 done
        ran = []
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(cq, "_run_remote_command",
                            lambda a, s: ran.append(a))
        monkeypatch.setattr(cq, "_ack_command_mail", lambda u, w, **k: True)
        cq._poll_remote_commands(
            {"allowed_trigger_senders": ["doc@x.tw"]},
            {"items": [{"uid": "7", "sender": "doc@x.tw",
                        "authenticated": True, "expired": False,
                        "subject": "皮膚科會診重開", "age_sec": 1.0}],
             "error": None, "uidvalidity": "9",
             "mailbox_identity": "T:INBOX"})
        assert not ran, "★部署前剛執行過的指令又跑了一次★(收據沒被遷移)"

    def test_the_ambiguity_expires_with_the_ttl(self, monkeypatch):
        """★抑制要有出口★ 過了 24h TTL,同一把鍵就要能正常執行。"""
        self._seed_legacy(done=False,
                          age_sec=cq._REMOTE_RECEIPT_TTL_SEC + 60)
        ran = []
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(cq, "_run_remote_command",
                            lambda a, s: ran.append(a))
        monkeypatch.setattr(cq, "_ack_command_mail", lambda u, w, **k: True)
        cq._poll_remote_commands(
            {"allowed_trigger_senders": ["doc@x.tw"]},
            {"items": [{"uid": "7", "sender": "doc@x.tw",
                        "authenticated": True, "expired": False,
                        "subject": "皮膚科會診重開", "age_sec": 1.0}],
             "error": None, "uidvalidity": "9",
             "mailbox_identity": "T:INBOX"})
        assert ran == ["reboot"], "★曖昧變成永久封鎖★ 沒有出口的 fail-closed"


class TestTerminalAcksCarryTheGeneration:
    """★[外審 AD-4 第 1 輪 P1-2]★ 終局處置的標已讀也是一條新連線 ——
    不帶世代/身分的話,世代守衛整個被繞過。"""

    def test_the_terminal_ack_passes_generation_and_identity(
            self, monkeypatch):
        seen = []
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(ir, "mark_uids_seen",
                            lambda uids, **kw: seen.append(kw) or True)
        monkeypatch.setattr(cq, "_run_remote_command",
                            lambda a, s: pytest.fail("不該執行"))
        cq._poll_remote_commands(
            {"allowed_trigger_senders": ["doc@x.tw"]},
            {"items": [{"uid": "7", "sender": "doc@x.tw",
                        "authenticated": True, "expired": False,
                        "subject": "皮膚科會診重開 亂寫 一堆", "age_sec": 1.0}],
             "error": None, "uidvalidity": "9",
             "mailbox_identity": "T:INBOX"})
        assert seen and seen[0].get("expect_uidvalidity") == "9"
        assert seen[0].get("expect_identity") == "T:INBOX"

    def test_no_generation_means_no_terminal_ack(self, monkeypatch):
        """世代變了,同 uid 可能是另一封不相干的信 —— STORE 會把它永久跳過。"""
        seen = []
        monkeypatch.setattr(cq, "_this_machine_name", lambda: "PC-1")
        monkeypatch.setattr(ir, "mark_uids_seen",
                            lambda uids, **kw: seen.append(list(uids)) or True)
        monkeypatch.setattr(cq, "_run_remote_command",
                            lambda a, s: pytest.fail("不該執行"))
        cq._poll_remote_commands(
            {"allowed_trigger_senders": ["doc@x.tw"]},
            {"items": [{"uid": "7", "sender": "doc@x.tw",
                        "authenticated": True, "expired": False,
                        "subject": "皮膚科會診重開 亂寫 一堆", "age_sec": 1.0}],
             "error": None, "uidvalidity": "",
             "mailbox_identity": "T:INBOX"})
        assert not seen, "★缺世代還標已讀★ 可能標掉另一封不相干的信"

    def test_the_scheduler_final_ack_is_generation_gated(self):
        """★接線★ 排程器的終局處置(_final_uids)同一條規則。"""
        i = SRC.index("mark_uids_seen(" + NL
                      + "                                        _final_uids,")
        block = SRC[i - 600:i + 300]
        assert "expect_uidvalidity=_fuv" in block
        assert "expect_identity=_fid" in block
        assert "if _fuv and _fid:" in block, "缺世代/身分要跳過,不是照標"


class TestMarkSeenVerifiesTheAccount:
    """★[外審 AD-4 第 1 輪 P1-3]★ mark_uids_seen 自己又載了一次憑證 ——
    掃描之後換帳號,同 uid(甚至同 UIDVALIDITY)在另一個帳號指向不相干
    的信。帳號不符就不連線、不 STORE。"""

    def test_a_changed_account_refuses_to_store(self, monkeypatch):
        monkeypatch.setattr(ir, "_load_imap_settings",
                            lambda: {"host": "h", "port": 993,
                                     "username": "b@x.tw", "password": "p"})
        monkeypatch.setattr(
            ir.imaplib, "IMAP4_SSL",
            lambda *a, **k: pytest.fail("帳號不符還去連線"))
        # 掃描端的身分:同一台伺服器、另一個帳號(生產形狀=完整設定)
        ident_a = ir._identity_from_settings(
            {"host": "h", "port": 993, "username": "a@x.tw"})
        assert ir.mark_uids_seen(["7"], expect_uidvalidity="9",
                                 expect_identity=ident_a) is False

    def test_the_same_account_proceeds(self, monkeypatch):
        """反方向:同帳號不可以被誤擋(守衛要能過好人)。"""
        conn = TestMarkSeenVerifiesTheGeneration._Conn(uv=b"9")
        monkeypatch.setattr(ir, "_load_imap_settings",
                            lambda: {"host": "h", "port": 993,
                                     "username": "a@x.tw", "password": "p"})
        monkeypatch.setattr(ir.imaplib, "IMAP4_SSL", lambda *a, **k: conn)
        monkeypatch.setattr(ir, "_set_active", lambda c, tag="": None)
        monkeypatch.setattr(ir, "_clear_active", lambda c: None)
        monkeypatch.setattr(ir, "_force_close_conn", lambda c: None)
        # ★身分要用掃描當下的【完整】設定算★(host 也在指紋裡,批次AE-2)
        ident_a = ir._identity_from_settings(
            {"host": "h", "port": 993, "username": "a@x.tw"})
        assert ir.mark_uids_seen(["7"], expect_uidvalidity="9",
                                 expect_identity=ident_a) is True


class TestReplaySurvivesEveryBadTimestampShape:
    """★[外審 AD-4 第 1 輪 P2-4]★ 巨大整數 float() 拋的是 OverflowError;
    NaN/Infinity/未來時間不拋,卻會被當成「很新」而補跑陳舊請求。"""

    def _seed(self, records):
        import json
        with open(cq._trigger_journal_path(), "w", encoding="utf-8") as f:
            json.dump(records, f)

    def test_overflow_nan_and_future_do_not_kill_or_replay(self, monkeypatch):
        self._seed({
            "T|9|1": {"sender": "huge@x.tw", "at": 10 ** 400, "uid": "1"},
            "T|9|2": {"sender": "nan@x.tw", "at": float("nan"), "uid": "2"},
            "T|9|3": {"sender": "future@x.tw", "at": time.time() + 86400,
                      "uid": "3"},
            "T|9|4": {"sender": "good@x.tw", "at": time.time(), "uid": "4"},
        })
        jobs = []
        monkeypatch.setattr(cq, "trigger_job_async",
                            lambda *a, **k: jobs.append(k))
        n = cq.resume_pending_triggers()
        assert n == 1, "★壞時間戳把整輪打掉或被當成很新★"
        assert jobs[0]["override_recipients"] == ["good@x.tw"]
        pending, _ = cq._trigger_journal_pending()
        assert set(pending) == {"T|9|4"}, (
            "壞的三筆要走過時結案出口,不可以留著")


class TestIdentityCoversTheWholeEndpoint:
    """★[批次AE-2,外審 2026-08-13 P2-01]★ 指紋要含 host/port/mailbox
    整組:同一個帳號名掛在兩台伺服器上,UIDVALIDITY+UID 可能撞號 ——
    只 hash 帳號的話兩邊身分相同,收據/journal 會互認。"""

    @staticmethod
    def _ident(monkeypatch, **s):
        monkeypatch.setattr(ir, "_load_imap_settings", lambda: dict(s))
        return ir.mailbox_identity()

    def test_the_host_is_part_of_the_identity(self, monkeypatch):
        a = self._ident(monkeypatch, host="server-a.example", port=993,
                        username="doctor@example.com")
        b = self._ident(monkeypatch, host="server-b.example", port=993,
                        username="doctor@example.com")
        assert a and b and a != b, (
            "★同帳號、不同伺服器 → 身分相同★ 撞號的 UID 會互認收據")

    def test_the_port_is_part_of_the_identity(self, monkeypatch):
        a = self._ident(monkeypatch, host="h", port=993, username="u")
        b = self._ident(monkeypatch, host="h", port=143, username="u")
        assert a != b

    def test_host_case_and_whitespace_are_normalized(self, monkeypatch):
        a = self._ident(monkeypatch, host=" IMAP.X.tw ", port=993,
                        username="u")
        b = self._ident(monkeypatch, host="imap.x.tw", port=993,
                        username="u")
        assert a == b, "host 大小寫/空白不可以變成不同身分"

    def test_legacy_identity_is_the_old_username_only_formula(
            self, monkeypatch):
        """★舊公式要逐字釘住★ —— 對不上就比對不到部署前寫下的收據,
        「剛執行過的指令再跑一次」就會復發(AD-4 P1-1 的形狀)。"""
        import hashlib
        monkeypatch.setattr(ir, "_load_imap_settings",
                            lambda: {"host": "h", "port": 993,
                                     "username": "Doc@X.tw "})
        want = hashlib.sha256(b"doc@x.tw").hexdigest()[:12] + ":INBOX"
        assert ir.legacy_mailbox_identity() == want
        assert ir.mailbox_identity() != want, (
            "新舊公式一樣的話,這批什麼都沒改")


class TestOldNamespaceReceiptsStayAmbiguous:
    """★[批次AE-2]★ 身分公式改版後,部署前一刻剛執行過的遠端指令
    (收據在舊命名空間)不可以再執行一次 —— 曖昧 fail-closed + 24h TTL
    出口,與 AD-4 第 2 輪的裸鍵處理同一個立場。"""

    @staticmethod
    def _idents(monkeypatch):
        monkeypatch.setattr(ir, "_load_imap_settings",
                            lambda: {"host": "h", "port": 993,
                                     "username": "u"})
        return ir.mailbox_identity(), ir.legacy_mailbox_identity()

    @staticmethod
    def _seed_receipts(data):
        io.open(cq._remote_receipt_path(), "w",
                encoding="utf-8").write(json.dumps(data))

    def test_a_fresh_old_identity_receipt_blocks_execution(self, monkeypatch):
        new_i, old_i = self._idents(monkeypatch)
        self._seed_receipts({f"{old_i}|9:7": {"at": time.time()}})
        assert cq._claim_remote_command(f"{new_i}|9:7") is False, (
            "★部署前剛執行過的指令,改版後又跑了一次★")

    def test_the_ttl_is_still_the_exit(self, monkeypatch):
        """抑制要有出口:過期的舊收據不再擋(真的換了端點也最多擋 24h)。"""
        new_i, old_i = self._idents(monkeypatch)
        self._seed_receipts({f"{old_i}|9:7": {
            "at": time.time() - cq._REMOTE_RECEIPT_TTL_SEC - 60}})
        assert cq._claim_remote_command(f"{new_i}|9:7") is True

    def test_done_under_the_old_identity_suppresses_the_expiry_reply(
            self, monkeypatch):
        new_i, old_i = self._idents(monkeypatch)
        self._seed_receipts({f"{old_i}|9:7": {"at": time.time(),
                                              "done": True}})
        assert cq._remote_command_was_done(f"{new_i}|9:7") is True, (
            "舊命名空間的 done 也要壓掉過期回信 —— 不然使用者收到假的過期通知")


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
