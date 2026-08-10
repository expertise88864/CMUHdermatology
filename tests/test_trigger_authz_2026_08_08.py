# -*- coding: utf-8 -*-
"""外審第 11 輪（IMAP 觸發授權 / 設定交易 / 止掛門檻）。

【F1】`require_authenticated_trigger` 預設是 False —— 那個預設的實際意義是:
**任何能寄信到這個信箱的人,都可以遠端啟動一次 HIS 會診查詢,並讓一封含全院
會診清單的信被寄出去。** 只要把 `From` 偽造成白名單醫師就成立,而 From 本來
就是寄件者自填的純文字。之所以一直不敢打開,是怕「功能靜默失效」——
那個顧慮的正確解法不是把門開著,而是不讓它靜默(擋下來就主動告警)。

【F2】strict 模式本身也可以被繞過:`Authentication-Results` 這個 header 攻擊者
可以自己塞一段進自己的信裡,只要 authserv-id 寫成 `mx.google.com` 就會被
「可信」判準收下,再與真正 Gmail 那段(寫著 fail)串在一起搜關鍵字 → dmarc=pass
找得到 → 偽造的 From 被當成已驗證。

【F5】設定存檔:live state 在 commit 成功之前就被改掉了。UI 顯示「一個檔都沒有
變更」,背景執行緒卻已經在用沒存進去的新門檻與新開關。
"""
import ast
import inspect
import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402
from cmuh_common import imap_reader as ir  # noqa: E402


# ===========================================================================
# F2 Authentication-Results 偽造
# ===========================================================================
def _headers(*pairs) -> bytes:
    return ("".join(f"{k}: {v}\r\n" for k, v in pairs) + "\r\n").encode("utf-8")


class TestOnlyTheReceivingServersVerdictCounts:

    def test_a_forged_segment_claiming_gmail_is_not_trusted(self):
        """★核心★ 攻擊者在自己的信裡塞一段 `mx.google.com; dmarc=pass`。
        Gmail 真正加的那一段在【最上方】而且寫著 fail。
        兩段串在一起搜關鍵字的話,偽造那段就贏了。"""
        raw = _headers(
            ("Authentication-Results",
             "mx.google.com; dmarc=fail header.from=evil.example"),
            ("Authentication-Results",
             "mx.google.com; dmarc=pass header.from=cmuh.example"),
            ("From", "doctor@cmuh.example"),
            ("Subject", "會診"),
        )
        _subj, _frm, auth = ir._parse_trigger_headers(raw)
        assert "dmarc=pass" not in auth, (
            "★偽造的 Authentication-Results 被採信了★ 只要在自己的信裡多寫"
            "一段冒用收件伺服器名稱的 header 就能繞過驗證")

    def test_the_genuine_topmost_verdict_still_works(self):
        """★不可以連正常情況一起擋掉★ 只有一段、來自收件伺服器 → 照樣採信。"""
        raw = _headers(
            ("Authentication-Results",
             "mx.google.com; dmarc=pass header.from=cmuh.example"),
            ("From", "doctor@cmuh.example"),
        )
        _subj, _frm, auth = ir._parse_trigger_headers(raw)
        assert "dmarc=pass" in auth

    def test_an_upstream_relay_verdict_is_not_trusted(self):
        """轉寄站加的那一段(authserv-id 不是我們的)不採信。"""
        raw = _headers(
            ("Authentication-Results",
             "relay.example.org; dmarc=pass header.from=cmuh.example"),
            ("From", "doctor@cmuh.example"),
        )
        _subj, _frm, auth = ir._parse_trigger_headers(raw)
        assert auth == ""

    def test_a_lookalike_authserv_id_is_not_trusted(self):
        """`evil.mx.google.com` 只是攻擊者自己寫進 header 的字串。"""
        assert not ir._authserv_is_trusted(
            "evil.mx.google.com; dmarc=pass header.from=x")
        assert ir._authserv_is_trusted("mx.google.com; dmarc=pass")


# ===========================================================================
# F1 觸發授權
# ===========================================================================
class TestTriggerRequiresAuthenticationByDefault:

    def test_the_default_is_fail_closed(self):
        """★核心★ 預設值就是這個功能的實際安全姿態 —— 沒有人會去改它。"""
        assert cq.DEFAULT_CONFIG["require_authenticated_trigger"] is True, (
            "★預設不要求驗證★ 任何能寄信到這個信箱的人都能遠端觸發 HIS 查詢")

    def test_an_unparseable_sender_never_triggers(self):
        """★那條 fallback 不可以再存在★ 它繞過白名單【與】驗證。"""
        src = inspect.getsource(cq)
        assert "__no_sender__" not in src
        assert "此路徑已永久關閉" in src

    def test_a_rejected_whitelisted_sender_is_alerted(self, monkeypatch):
        """★這是「預設打開」能成立的前提★ 舊版不敢打開,理由是怕功能靜默
        失效。擋下來就主動說,兩種可能都被涵蓋:我們判定太嚴(該調整)、
        或真的有人在偽造(更該讓人知道)。"""
        sent = {}
        cq._trigger_reject_alert_at = 0.0

        class _T:
            def __init__(self, target=None, **k):
                self._t = target

            def start(self):
                self._t()
        monkeypatch.setattr(cq.threading, "Thread", _T)
        monkeypatch.setattr(cq, "_developer_alert_recipients",
                            lambda: ["dev@x.tw"])
        import cmuh_common.smtp_mail as sm
        monkeypatch.setattr(sm, "send_mail",
                            lambda **k: sent.update(k) or {})
        cq._alert_trigger_rejected(["doctor@cmuh.example"])
        assert sent, "★被擋下來的合法觸發沒有任何人會知道★"
        assert "doctor@cmuh.example" in sent["body"]

    def test_the_alert_is_throttled(self, monkeypatch):
        """一封被拒的信不可以變成一串告警。"""
        calls = []
        cq._trigger_reject_alert_at = 0.0

        class _T:
            def __init__(self, target=None, **k):
                self._t = target

            def start(self):
                calls.append(1)
        monkeypatch.setattr(cq.threading, "Thread", _T)
        cq._alert_trigger_rejected(["a@x.tw"])
        cq._alert_trigger_rejected(["a@x.tw"])
        assert len(calls) == 1, f"沒有節流:{len(calls)}"
        cq._trigger_reject_alert_at = 0.0


# ===========================================================================
# F5 設定存檔:commit 成功之前不可以動 live state
# ===========================================================================
class TestNoLiveStateBeforeCommit:

    def _src(self):
        import main
        return textwrap.dedent(
            inspect.getsource(main.AutomationApp.save_all_settings))

    def _commit_line(self, tree):
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "_atomic_write_json_multi"):
                return n.lineno
        pytest.fail("找不到 commit 呼叫")

    def test_live_state_is_only_assigned_after_the_commit(self):
        """★核心★ commit 可能失敗;失敗時 UI 說「一個檔都沒有變更」,
        背景執行緒卻已經在用沒存進去的新門檻與新開關。
        畫面說沒改、行為卻改了,是最難查的那種不一致。"""
        tree = ast.parse(self._src())
        commit_at = self._commit_line(tree)
        watched = {"threshold_settings", "r_doctor_map",
                   "alert_email_recipients"}
        early = []
        for n in ast.walk(tree):
            if not isinstance(n, ast.Assign):
                continue
            for t in n.targets:
                base = t.value if isinstance(t, ast.Subscript) else t
                if (isinstance(base, ast.Attribute)
                        and base.attr in watched
                        and isinstance(base.value, ast.Name)
                        and base.value.id == "self"
                        and n.lineno < commit_at):
                    early.append((base.attr, n.lineno))
        assert not early, (
            f"★這些 live state 在 commit 之前就被改掉了★:{early}")

    def test_the_alert_snapshot_syncs_after_the_commit(self):
        tree = ast.parse(self._src())
        commit_at = self._commit_line(tree)
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "_sync_alert_enabled_snapshot"):
                assert n.lineno > commit_at, (
                    "★存檔失敗時背景緒仍會拿到新的開關快照★")
                return
        pytest.fail("找不到 _sync_alert_enabled_snapshot 呼叫")


    def test_the_reject_branch_actually_calls_the_alert(self):
        """★接線★ helper 存在但沒人呼叫 = 靜默失效照舊
        (突變驗證抓到的:只測 helper 的那個測試,把呼叫點刪掉照樣綠)。"""
        src = inspect.getsource(cq)
        tree = ast.parse(src)
        # ★不可以「找到第一個就 return」★ 提到這個鍵的 if 不只一個
        #   (還有一次性遷移那一個)。第一版就是這樣,量到的是遷移函式。
        found = False
        for n in ast.walk(tree):
            if not isinstance(n, ast.If):
                continue
            if "require_authenticated_trigger" not in ast.dump(n.test):
                continue
            names = {c.func.id for c in ast.walk(n)
                     if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
            if "_alert_trigger_rejected" in names:
                found = True
        assert found, "★擋下來卻不告警★ 那正是舊版不敢把預設打開的理由"


# ===========================================================================
# F3 觸發信的持久化接手
# ===========================================================================
class TestATriggerSurvivesARestart:
    r"""★核心★ 舊流程是「標 \Seen → 回到排程器 → 起 worker」。這中間程式
    結束/重啟的話,那封信已經不是 UNSEEN,永遠不會再被掃到 —— 醫師乾等一個
    不會來的結果。新流程:先落地 → 才標已讀 → 才觸發。"""

    def _isolate(self, tmp_path, monkeypatch):
        import cmuh_common.paths as paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        return tmp_path

    def test_the_work_lands_before_the_mail_is_marked_seen(self, tmp_path,
                                                           monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        order = []
        monkeypatch.setattr(cq, "_trigger_journal_add",
                            lambda uid, s: order.append("journal") or True)
        import cmuh_common.imap_reader as _ir
        monkeypatch.setattr(_ir, "mark_uids_seen",
                            lambda uids: order.append("seen") or True)
        monkeypatch.setattr(cq, "trigger_job_async",
                            lambda *a, **k: order.append("trigger"))
        # ★生產的形狀是 (uid, 寄件人, 是否通過驗證)★ 少了第三欄,
        #   handoff 會把它當成未驗證而整批跳過。
        cq._handoff_email_triggers([("11", "doc@x.tw", True)],
                                   ["doc@x.tw"])
        assert order == ["journal", "seen", "trigger"], (
            f"★順序錯了★:{order} —— 標已讀不可以早於工作落地")

    def test_a_journal_failure_leaves_the_mail_unread(self, tmp_path,
                                                      monkeypatch):
        """★落地失敗就不要標已讀★ 信留在 UNSEEN,下一輪重來。
        寧可多觸發一次,也不要漏掉一次會診請求。"""
        self._isolate(tmp_path, monkeypatch)
        marked = []
        monkeypatch.setattr(cq, "_trigger_journal_add", lambda uid, s: False)
        import cmuh_common.imap_reader as _ir
        monkeypatch.setattr(_ir, "mark_uids_seen",
                            lambda uids: marked.append(uids))
        monkeypatch.setattr(cq, "trigger_job_async",
                            lambda *a, **k: marked.append("trigger"))
        # ★生產的形狀是 (uid, 寄件人, 是否通過驗證)★ 少了第三欄,
        #   handoff 會把它當成未驗證而整批跳過。
        cq._handoff_email_triggers([("11", "doc@x.tw", True)],
                                   ["doc@x.tw"])
        assert not marked, f"★工作沒落地卻標了已讀/觸發了★:{marked}"

    def test_a_pending_entry_is_resumed_on_startup(self, tmp_path,
                                                   monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        assert cq._trigger_journal_add("42", "doc@x.tw")
        got = []
        monkeypatch.setattr(cq, "trigger_job_async",
                            lambda *a, **k: got.append((a, k)))
        assert cq.resume_pending_triggers() == 1
        assert got and got[0][1]["override_recipients"] == ["doc@x.tw"]
        assert got[0][1]["trigger_uids"] == ("42",)

    def test_a_stale_pending_entry_is_not_resumed(self, tmp_path, monkeypatch):
        """★會診清單是「現在」的狀態★ 補寄一份六小時前的請求,醫師拿到的
        是與當下不符的資料(與 IMAP 的陳舊觸發信過濾同一個道理)。"""
        self._isolate(tmp_path, monkeypatch)
        cq._trigger_journal_add("42", "doc@x.tw")
        data, _ok = cq._trigger_journal_pending()
        data["42"]["at"] = 0.0
        cq._trigger_journal_save(data)
        got = []
        monkeypatch.setattr(cq, "trigger_job_async",
                            lambda *a, **k: got.append(k))
        assert cq.resume_pending_triggers() == 0
        assert not got
        assert not cq._trigger_journal_pending()[0], "過時的沒有被結案"

    def test_a_finished_job_clears_the_journal_entry(self, tmp_path,
                                                    monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        cq._trigger_journal_add("42", "doc@x.tw")
        cq._trigger_journal_done("42")
        assert not cq._trigger_journal_pending()[0]

    def test_the_scheduler_actually_resumes(self):
        """★接線★ 沒人呼叫的話,那些被標成已讀的觸發信就永遠消失了
        (「有 API」不等於「會發生」——這一輪外審已經點名過兩次)。"""
        src = textwrap.dedent(inspect.getsource(cq.scheduler_loop))
        names = {n.func.id for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "resume_pending_triggers" in names

    def test_the_poll_defers_marking(self):
        """★接線★ 沒有 defer 的話,`check_trigger` 還是會先標已讀。"""
        src = inspect.getsource(cq)
        for n in ast.walk(ast.parse(src)):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "check_trigger"):
                kw = {k.arg: k.value for k in n.keywords}
                v = kw.get("defer_mark_matched")
                assert isinstance(v, ast.Constant) and v.value is True, (
                    "★命中信仍在工作落地之前就被標成已讀★")
                return
        pytest.fail("找不到 check_trigger 呼叫")


# ===========================================================================
# 外審第 11 輪【第 2 回】—— 六項都是第 1 回的修正自己開的洞
# ===========================================================================
class TestExistingDeploymentsAreMigrated:

    def test_an_old_settings_file_with_false_is_turned_on(self):
        """★核心★ 只改 `DEFAULT_CONFIG` 保護不到【已經存在的設定檔】——
        而診間那台一定有(設定頁存過檔就會把整份寫下來)。"""
        saved = {"require_authenticated_trigger": False}
        cq._migrate_trigger_authz(saved)
        assert saved["require_authenticated_trigger"] is True

    def test_a_deliberate_opt_out_after_migration_is_respected(self):
        """★遷移只做一次★ 使用者在遷移【之後】自己關掉,是知情的選擇。"""
        saved = {"require_authenticated_trigger": False,
                 cq._TRIGGER_AUTHZ_MIGRATION_KEY: True}
        cq._migrate_trigger_authz(saved)
        assert saved["require_authenticated_trigger"] is False

    def test_the_loader_runs_the_migration(self):
        src = textwrap.dedent(inspect.getsource(cq.load_config))
        names = {n.func.id for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "_migrate_trigger_authz" in names


class TestTheJournalNeverOverwritesOnReadFailure:

    def test_add_fails_when_the_journal_cannot_be_read(self, tmp_path,
                                                       monkeypatch):
        """★核心★ 讀不到就回空字典、再把只有新 uid 的內容寫回去 ——
        帳上原本那幾筆【已標成已讀、再也掃不到】的待辦就永久消失了。
        又是「讀不到被當成確定的答案」。"""
        import cmuh_common.paths as paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        assert cq._trigger_journal_add("1", "a@x.tw")
        import cmuh_common.atomic_io as aio
        monkeypatch.setattr(aio, "safe_load_json_ex",
                            lambda *a, **k: ({}, "error"))
        assert cq._trigger_journal_add("2", "b@x.tw") is False, (
            "★讀不到卻照樣寫★ 會把既有待辦蓋掉")
        monkeypatch.undo()
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        assert "1" in cq._trigger_journal_pending()[0], "既有待辦被蓋掉了"

    def test_done_does_not_write_on_read_failure(self, tmp_path, monkeypatch):
        """結案也一樣:讀不到就不要寫。多補跑一次(重複一封)遠比把別人的
        待辦蓋掉好。

        ★誠實註記(突變驗證量出來的)★ 把 `done` 裡那道守衛拿掉,這個測試
        仍然是綠的 —— 因為 `data.pop(uid, None)` 在空字典上本來就回 None,
        後面的存檔根本不會執行。也就是說這個性質【由結構保證】,那道守衛是
        多一層明講,不是它在承載這件事。保留守衛(意圖要看得見),但不假裝
        這個測試證明了它。
        """
        import cmuh_common.paths as paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        cq._trigger_journal_add("1", "a@x.tw")
        cq._trigger_journal_add("2", "b@x.tw")
        import cmuh_common.atomic_io as aio
        monkeypatch.setattr(aio, "safe_load_json_ex",
                            lambda *a, **k: ({}, "error"))
        cq._trigger_journal_done("1")
        monkeypatch.undo()
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        assert set(cq._trigger_journal_pending()[0]) == {"1", "2"}


class TestARequeuedJobKeepsItsUid:

    def test_a_requeued_uid_is_not_settled(self, monkeypatch):
        """★核心★ 拿不到 `_flow_lock` 時工作【還沒做】就被排進補跑佇列。
        那時把 journal 結案的話:補跑前當機 → 信早就標成已讀、journal 也空了
        → 永久漏信。"""
        done = []
        monkeypatch.setattr(cq, "_trigger_journal_done", done.append)
        monkeypatch.setattr(cq, "_do_full_job",
                            lambda *a, **k: k["requeued_out"].append("7"))

        class _Lease:
            pass
        monkeypatch.setattr(cq._consult_job_gate, "acquire_lease",
                            lambda k: _Lease())
        monkeypatch.setattr(cq._consult_job_gate, "release",
                            lambda k, l: None)
        monkeypatch.setattr(cq, "_drain_pending_retriggers", lambda: None)

        class _T:
            def __init__(self, target=None, **k):
                self._t = target

            def start(self):
                self._t()
        monkeypatch.setattr(cq.threading, "Thread", _T)
        cq.trigger_job_async("email", override_recipients=["a@x.tw"],
                             trigger_uids=("7",))
        assert done == [], f"★工作還沒做就把 journal 結案了★:{done}"

    def test_the_queue_carries_uids(self):
        src = textwrap.dedent(inspect.getsource(cq._enqueue_pending_retrigger))
        assert "trigger_uids" in src, "補跑佇列沒有把 uid 帶著走"


class TestTerminalDispositionsAreAcknowledged:

    def test_blocked_and_deduped_uids_get_marked(self):
        """★核心★ 命中信改成延後標記之後,只有被接受的會走 handoff。
        被拒絕的每 20 秒重掃一次;被去重的合法信五分鐘後又變成可執行,
        把「已略過」的請求延後重跑。"""
        src = inspect.getsource(cq)
        assert "_final_uids" in src
        tree = ast.parse(src)
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "mark_uids_seen"
                    and n.args and getattr(n.args[0], "id", "") == "_final_uids"):
                return
        pytest.fail("★終局處置的信沒有被標已讀★ 會一直重複掃到")


# ===========================================================================
# 外審第 11 輪【第 3 回】
# ===========================================================================
class TestAnExceptionDoesNotSettleTheJournal:

    def _drive(self, monkeypatch, boom: bool):
        done = []
        monkeypatch.setattr(cq, "_trigger_journal_done", done.append)

        def _job(*a, **k):
            if boom:
                raise RuntimeError("CoInitialize 失敗")
        monkeypatch.setattr(cq, "_do_full_job", _job)

        class _Lease:
            pass
        monkeypatch.setattr(cq._consult_job_gate, "acquire_lease",
                            lambda k: _Lease())
        monkeypatch.setattr(cq._consult_job_gate, "release", lambda k, l: None)
        monkeypatch.setattr(cq, "_drain_pending_retriggers", lambda: None)

        class _T:
            def __init__(self, target=None, **k):
                self._t = target

            def start(self):
                try:
                    self._t()
                except Exception:
                    pass
        monkeypatch.setattr(cq.threading, "Thread", _T)
        cq.trigger_job_async("email", override_recipients=["a@x.tw"],
                             trigger_uids=("9",))
        return done

    def test_an_unexpected_error_keeps_the_journal_entry(self, monkeypatch):
        """★核心(第 3 回)★ `finally` 在成功與例外兩種情況都會跑。
        工作拋錯時信【已經標成已讀】—— 結案的話重啟也補不回來,
        醫師既收不到結果、也收不到失敗通知。"""
        assert self._drive(monkeypatch, boom=True) == [], (
            "★工作拋錯卻把 journal 結案了★ 這封請求永久消失")

    def test_a_normal_completion_still_settles(self, monkeypatch):
        """★反方向★ 正常做完要結案,否則下次開機會重複補跑。"""
        assert self._drive(monkeypatch, boom=False) == ["9"]


class TestEveryTerminalUidIsAcknowledged:

    def test_all_three_kinds_are_marked(self):
        """blocked / unverified / dedup 三類都要收尾 —— 少一類,那幾封信
        就會每 20 秒被重掃一次。"""
        # ★用 AST 找呼叫,不要用字元視窗★ 視窗開多大是猜的,
        #   剛好切在中間就會誤報(第一版就是這樣:4000 字元不夠長)。
        tree = ast.parse(inspect.getsource(cq))
        args = {a.id for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id == "_collect_final_uids"
                for a in n.args if isinstance(a, ast.Name)}
        for name in ("blocked", "unverified", "dedup_skipped"):
            assert name in args, (
                f"★{name} 沒有被收尾★ 那幾封信會每 20 秒被重掃一次")

    def test_multiple_mails_from_one_sender_all_get_uids(self):
        """★一位寄件人可能寄了好幾封★ 以寄件人為鍵的 dict 只留最後一個 uid,
        其餘那幾封沒有人 acknowledge。"""
        src = inspect.getsource(cq)
        i = src.index("_uid_of: dict = {}")
        seg = src[i:i + 400]
        assert "setdefault" in seg and "append" in seg, (
            "★同一位寄件人的多封信只留下一個 uid★")


class TestTheMigrationIsPersisted:

    def test_it_writes_the_marker_back(self, tmp_path, monkeypatch):
        """★核心(第 3 回)★ 只改記憶體的話:每次啟動都重新遷移,而註解承諾的
        「遷移後可以明確 opt-out」根本不成立 —— 使用者改回 false,
        下次開機又被打開。"""
        written = {}
        import cmuh_common.atomic_io as aio
        monkeypatch.setattr(aio, "atomic_write_json",
                            lambda p, d, **k: written.update(d))
        saved = {"require_authenticated_trigger": False}
        cq._migrate_trigger_authz(saved)
        assert written.get(cq._TRIGGER_AUTHZ_MIGRATION_KEY) is True, (
            "★marker 沒有寫回磁碟★ 下次開機會再遷移一次")
        assert written.get("require_authenticated_trigger") is True

    def test_a_write_failure_still_fails_closed(self, monkeypatch):
        """★安全姿態不因存檔失敗而退讓★"""
        import cmuh_common.atomic_io as aio
        monkeypatch.setattr(
            aio, "atomic_write_json",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        saved = {"require_authenticated_trigger": False}
        cq._migrate_trigger_authz(saved)
        assert saved["require_authenticated_trigger"] is True


# ===========================================================================
# 外審第 11 輪【重開後第 1 回】
# ===========================================================================
class TestAJournalFailureAlsoUndoesTheDedupReservation:
    """★核心★ 兩個各自正確的修正組合出來的洞。

    `_trigger_is_duplicate()` 在 handoff 之前就把「這位五分鐘內處理過」寫下去。
    journal 沒落地時信留在未讀(對的)—— 但下一輪它會撞上去重分支,而去重是
    【終局處置】會把信標成已讀(也是對的)。
    合起來:工作沒做、journal 沒紀錄、信卻永久消失,醫師還收到一封誤導性的
    「已處理過」通知。
    """

    def test_the_reservation_is_released(self, tmp_path, monkeypatch):
        import cmuh_common.paths as paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        monkeypatch.setattr(cq, "_persist_trigger_dedup_locked", lambda: None)
        assert cq._trigger_is_duplicate("doc@x.tw") is False   # 建立預約
        monkeypatch.setattr(cq, "_trigger_journal_add", lambda uid, s: False)
        import cmuh_common.imap_reader as _ir
        monkeypatch.setattr(_ir, "mark_uids_seen", lambda uids: True)
        cq._handoff_email_triggers([("11", "doc@x.tw", True)], ["doc@x.tw"])
        assert cq._trigger_is_duplicate("doc@x.tw") is False, (
            "★去重預約沒有撤銷★ 下一輪會把這封信當成『已處理過』而標成已讀,"
            "工作卻從來沒有執行")

    def test_a_successful_handoff_keeps_the_reservation(self, tmp_path,
                                                        monkeypatch):
        """★反方向★ 正常落地時去重要留著,否則同一封會被重複處理。"""
        import cmuh_common.paths as paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        monkeypatch.setattr(cq, "_persist_trigger_dedup_locked", lambda: None)
        cq._trigger_is_duplicate("doc2@x.tw")
        monkeypatch.setattr(cq, "_trigger_journal_add", lambda uid, s: True)
        import cmuh_common.imap_reader as _ir
        monkeypatch.setattr(_ir, "mark_uids_seen", lambda uids: True)
        monkeypatch.setattr(cq, "trigger_job_async", lambda *a, **k: None)
        cq._handoff_email_triggers([("12", "doc2@x.tw", True)], ["doc2@x.tw"])
        assert cq._trigger_is_duplicate("doc2@x.tw") is True


class TestAnEarlierForgedMailCannotBlockALaterGenuineOne:

    def test_authentication_is_bound_to_each_mail(self):
        """★核心★ `senders_seen` 以地址去重:同一輪先掃到一封偽造的未驗證信,
        後面那封【合法且已驗證】的就不會被加進 `authenticated_senders` ——
        攻擊者只要持續寄較早的偽造信,就能讓那位醫師的授權觸發長期失效。"""
        # ★又是字元視窗★ 我上一個測試才剛因為視窗長度不夠而誤報,這裡
        #   第一版又犯一次。用 AST:`matched_uids.append(...)` 的引數必須是
        #   三元組(uid, 寄件人, 是否通過驗證)。
        from cmuh_common import imap_reader as _ir
        tree = ast.parse(inspect.getsource(_ir))
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "append"
                    and isinstance(n.func.value, ast.Subscript)
                    and getattr(n.func.value.slice, "value", "")
                    == "matched_uids"):
                assert (n.args and isinstance(n.args[0], ast.Tuple)
                        and len(n.args[0].elts) == 3), (
                    "★uid 沒有帶著自己的驗證結果★ 驗證仍以寄件人聚合,"
                    "較早的偽造信可以把同一位的合法信一起壓掉")
                return
        pytest.fail("找不到 matched_uids 的記錄處")

    def test_the_handoff_only_takes_authenticated_mails(self, tmp_path,
                                                        monkeypatch):
        """同一位寄件人同時有一封合法已驗證信與一封偽造未驗證信 →
        只有前者可以觸發。"""
        import cmuh_common.paths as paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        got = []
        monkeypatch.setattr(cq, "_trigger_journal_add", lambda uid, s: True)
        import cmuh_common.imap_reader as _ir
        monkeypatch.setattr(_ir, "mark_uids_seen", lambda uids: True)
        monkeypatch.setattr(cq, "trigger_job_async",
                            lambda *a, **k: got.append(k.get("trigger_uids")))
        cq._handoff_email_triggers(
            [("bad", "doc@x.tw", False), ("good", "doc@x.tw", True)],
            ["doc@x.tw"])
        assert got == [("good",)], f"★偽造那封也被拿去觸發了★:{got}"


class TestAnUnparseableSenderIsAlsoAcknowledged:

    def test_the_no_from_branch_marks_seen(self):
        """★不標已讀的話★ 同一封畸形信每 20 秒重新 FETCH + INTERNALDATE 查詢
        並寫一行 error,直到六小時過時 —— 持續投遞就能把有效診斷訊息洗掉。"""
        tree = ast.parse(inspect.getsource(cq))
        for n in ast.walk(tree):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "_collect_final_uids" and n.args
                    and isinstance(n.args[0], ast.List)
                    and [getattr(e, "value", None) for e in n.args[0].elts] == [""]):
                return
        pytest.fail("★無法解析 From 的那幾封沒有被標已讀★")


class TestTogglingACheckboxIsNotACommit:

    def test_no_live_sync_on_toggle(self):
        """★核心★ 勾選【不是】設定生效的時點。舊版在 checkbox callback 裡就
        同步影子快照(背景掃描讀那一份)—— 使用者只是勾一下看看、還沒按儲存,
        背景就可能立刻寄出一封原設定不允許的止掛信;取消勾選則會立刻停掉
        本來該有的提醒。存檔失敗時更糟:UI 說「一個檔都沒有變更」,行為卻已經改了。
        """
        import main
        src = inspect.getsource(main.AutomationApp)
        tree = ast.parse(textwrap.dedent(src))
        for fn in ast.walk(tree):
            if (isinstance(fn, ast.FunctionDef)
                    and fn.name == "on_doctor_alert_change"):
                names = {n.func.attr for n in ast.walk(fn)
                         if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Attribute)}
                assert "_sync_alert_enabled_snapshot" not in names, (
                    "★勾選就同步背景快照★ 那等於沒按儲存也生效")
                return
        pytest.fail("找不到 on_doctor_alert_change")


class TestOptOutStillGetsTheDurableHandoff:

    def test_an_unauthenticated_mail_is_journaled_when_strict_is_off(
            self, tmp_path, monkeypatch):
        """★核心(第 2 回)★ 使用者明確關掉驗證時,未驗證的信【是】被接受的。
        上一版仍把它們全部濾掉 → 掉到「找不到 uid」的後備路徑直接觸發
        (沒有 journal),而終局標記又把那封信標成已讀 ——
        中途一中止,請求就消失了。"""
        import cmuh_common.paths as paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        added, seen = [], []
        monkeypatch.setattr(cq, "_trigger_journal_add",
                            lambda uid, s: added.append(uid) or True)
        import cmuh_common.imap_reader as _ir
        monkeypatch.setattr(_ir, "mark_uids_seen", lambda uids: seen.append(uids))
        monkeypatch.setattr(cq, "trigger_job_async", lambda *a, **k: None)
        cq._handoff_email_triggers([("5", "doc@x.tw", False)], ["doc@x.tw"],
                                   require_auth=False)
        assert added == ["5"], (
            "★關掉 strict 時,被接受的信沒有走持久化接手★ 中止就永久消失")

    def test_strict_mode_still_rejects_unauthenticated(self, tmp_path,
                                                       monkeypatch):
        """★反方向★ strict 開著時當然不可以接受。"""
        import cmuh_common.paths as paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        added = []
        monkeypatch.setattr(cq, "_trigger_journal_add",
                            lambda uid, s: added.append(uid) or True)
        import cmuh_common.imap_reader as _ir
        monkeypatch.setattr(_ir, "mark_uids_seen", lambda uids: True)
        monkeypatch.setattr(cq, "trigger_job_async", lambda *a, **k: None)
        cq._handoff_email_triggers([("5", "doc@x.tw", False)], ["doc@x.tw"],
                                   require_auth=True)
        assert added == []


class TestPartialJournalFailureKeepsTheDedup:

    def test_dedup_survives_when_one_uid_landed(self, tmp_path, monkeypatch):
        """★核心(第 2 回)★ 同一位寄件人兩封:一封落地了、工作正在跑,
        另一封沒落地。這時撤銷去重,那封沒落地的下一輪就會再開一個工作 ——
        同一位醫師同時被服務兩次,正是去重要防的事。"""
        import cmuh_common.paths as paths
        monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
        monkeypatch.setattr(cq, "_persist_trigger_dedup_locked", lambda: None)
        cq._trigger_is_duplicate("doc3@x.tw")          # 建立預約
        monkeypatch.setattr(cq, "_trigger_journal_add",
                            lambda uid, s: uid == "ok")
        import cmuh_common.imap_reader as _ir
        monkeypatch.setattr(_ir, "mark_uids_seen", lambda uids: True)
        monkeypatch.setattr(cq, "trigger_job_async", lambda *a, **k: None)
        cq._handoff_email_triggers(
            [("ok", "doc3@x.tw", True), ("bad", "doc3@x.tw", True)],
            ["doc3@x.tw"])
        assert cq._trigger_is_duplicate("doc3@x.tw") is True, (
            "★有一封落地了卻撤銷了去重★ 下一輪會為同一位再開一個工作")


    def test_the_terminal_marking_is_gated_on_strict_mode(self):
        """★接線★ 只測 helper 的話,把輪詢迴圈裡那道 strict 判斷拿掉照樣綠
        (突變驗證抓到的)。關掉 strict 時,那些信【是要被處理的】——
        標成已讀等於把一封已被接受、卻還沒登記的請求丟掉。"""
        tree = ast.parse(inspect.getsource(cq))
        for n in ast.walk(tree):
            if not isinstance(n, ast.If):
                continue
            if "require_authenticated_trigger" not in ast.dump(n.test):
                continue
            for c in ast.walk(n):
                if (isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                        and c.func.id == "_collect_final_uids"
                        and any(k.arg == "only_unauth" for k in c.keywords)):
                    return
        pytest.fail("★終局標記沒有被 strict 設定管住★ 關掉 strict 時會丟掉請求")


class TestCollectFinalUids:
    """★純函式,直接測★ 它從巢狀函式抽出來之後才測得到
    (突變驗證抓到的:`only_unauth` 失效時,上面那些接線測試全都照樣綠)。"""

    MAP = {"a@x.tw": [("1", True), ("2", False)],
           "b@x.tw": [("3", False)]}

    def test_it_takes_every_uid_by_default(self):
        assert cq._collect_final_uids(["a@x.tw"], self.MAP) == ["1", "2"]

    def test_only_unauth_skips_the_authenticated_ones(self):
        """★核心★ 「這位寄件人整體通過、但他還有別的偽造信」時,只有偽造那幾封
        是終局處置。把已驗證那封也標成已讀 = 把一封【要被處理的】請求丟掉。"""
        assert cq._collect_final_uids(["a@x.tw"], self.MAP,
                                      only_unauth=True) == ["2"], (
            "★only_unauth 失效★ 已驗證的信被一起標成已讀了")

    def test_it_is_case_insensitive_and_tolerates_junk(self):
        assert cq._collect_final_uids(["A@X.TW "], self.MAP) == ["1", "2"]
        assert cq._collect_final_uids(["nobody@x.tw"], self.MAP) == []
        assert cq._collect_final_uids(None, self.MAP) == []
