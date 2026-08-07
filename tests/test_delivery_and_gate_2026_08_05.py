# -*- coding: utf-8 -*-
"""外審第 6 輪批次AC:P1-02 / P1-06 / P1-07 / P2-02 / P2-03 / P2-06 / P2-08。"""
import ast
import inspect
import os
import sys
import textwrap

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402


# ── P1-02:同一個 hidden form 被重新顯示要被採認 ───────────────────────────
class TestConsultFormReuse:
    OURS_MAIN, OUR_PID = 5001, 16276

    def _sess(self):
        class _S:
            pid = self.OUR_PID
            our_pids = {self.OUR_PID}
            main_hwnd = self.OURS_MAIN
            main_pid = self.OUR_PID
            main_class = cq.MAIN_CLASS
            main_proc_started = None
            hproc = object()
        return _S()

    def _run(self, monkeypatch, visibility_script):
        """visibility_script: {hwnd: [每次查詢時的可見性]}"""
        monkeypatch.setattr(cq, "_session_death_reason", lambda _s: "")
        monkeypatch.setattr(cq, "resolve_menu_command_id", lambda _h: 42)
        monkeypatch.setattr(cq.win32gui, "PostMessage", lambda *a: None)
        calls = {"n": -1}

        def _vis(h):
            seq = visibility_script[h]
            return seq[min(calls["n"], len(seq) - 1)]
        monkeypatch.setattr(cq.win32gui, "IsWindowVisible", _vis)

        def _find(cls=None, pids=None, **k):
            if cls == cq.CONSULT_CLASS:
                calls["n"] += 1
                return list(visibility_script)
            return []
        monkeypatch.setattr(cq, "find_windows", _find)
        t = {"v": 1000.0}

        def _now():
            t["v"] += 0.5
            return t["v"]
        monkeypatch.setattr(cq.time, "time", _now)
        monkeypatch.setattr(cq.time, "sleep", lambda _s: None)
        got = {}

        def _cap(h, **_k):
            got["consult"] = h
            return "IMG", cq._RosterSnapshot([], True, [], [])
        monkeypatch.setattr(cq, "_capture_with_settled_roster", _cap)
        monkeypatch.setattr(cq, "_extract_consult_text", lambda *a, **k: ("", "", []))
        monkeypatch.setattr(cq, "_return_to_main", lambda *a: None)
        cq._query_cycle(self._sess(), {}, "今日會診病人")
        return got.get("consult")

    def test_a_hidden_form_reshown_is_accepted(self, monkeypatch):
        """★核心★ 上一輪 Hide 的 7002 被 HIS 重新 Show → 必須被本輪採認。

        舊寫法只認「命令後新出現的 hwnd」→ 7002 永遠在 before 集合裡 →
        每輪等滿 60 秒失敗 → session 被殺掉重登。
        """
        picked = self._run(monkeypatch, {7002: [False, False, True]})
        assert picked == 7002, f"★重新顯示的同一張會診單沒被採認★(picked={picked})"

    def test_a_form_that_stays_hidden_is_not_accepted(self, monkeypatch):
        """★反方向★ 一直隱藏的殘留 form 不是本輪的結果。"""
        try:
            self._run(monkeypatch, {7002: [False, False, False]})
        except RuntimeError as e:
            assert "等不到" in str(e)
            return
        raise AssertionError("一直隱藏的 form 被當成本輪結果")

    def test_an_already_visible_form_is_not_accepted(self, monkeypatch):
        """命令前就可見的(上一輪沒退乾淨/醫師自己的)不可採認。"""
        try:
            self._run(monkeypatch, {7002: [True, True, True]})
        except RuntimeError as e:
            assert "等不到" in str(e)
            return
        raise AssertionError("命令前就可見的 form 被當成本輪結果")


# ── P1-06:閘門放行一次後重新起算 ─────────────────────────────────────────
class TestGateReleasesOnceThenRearms:

    def _clock(self, v):
        return lambda: v

    def test_release_rearms_the_window(self, monkeypatch):
        """★核心★ 放行之後不可以變成永久開閘。

        上一版超過 15 分鐘後每一輪都放行 —— 閘門只擋前 15 分鐘,之後每 3 分鐘
        都冷啟動,掛帳清單照樣增長。放行【一次】,然後重新擋滿一個窗口。
        """
        monkeypatch.setattr(cq, "_retry_unclosed_sessions", lambda: 1)
        monkeypatch.setattr(cq, "_unmanaged_since", 0.0)
        t0 = 1000.0
        try:
            cq._ensure_no_unmanaged_sessions(now=self._clock(t0))
        except cq.UnmanagedSessionError:
            pass
        t1 = t0 + cq._UNMANAGED_BLOCK_MAX_SEC + 1
        cq._ensure_no_unmanaged_sessions(now=self._clock(t1))   # 放行一次
        # ★下一輪(3 分鐘後)必須又被擋住★
        try:
            cq._ensure_no_unmanaged_sessions(now=self._clock(t1 + 180))
        except cq.UnmanagedSessionError:
            return
        raise AssertionError("★放行之後變成永久開閘★ 每一輪都會再登入")

    def test_the_alert_no_longer_claims_never_login(self):
        """★措辭鐵律★ 告警信不可以再宣稱「不會再登入」——閘門有上限。"""
        src = inspect.getsource(cq._alert_unmanaged_session)
        assert "【不會】再登入" not in src, "信裡的宣稱與閘門行為不符"
        assert "暫停登入" in src


# ── P1-07:Outlook 逾時 = 結果不明,不重試 ────────────────────────────────
class TestOutlookTimeoutIsNotRetried:

    def test_timeout_raises_the_unknown_class(self, monkeypatch):
        class _W:
            def __init__(self, *a, **k):
                pass

            def start(self):
                pass

            def join(self, _t):
                pass

            def is_alive(self):
                return True                # 逾時:worker 還活著
        monkeypatch.setattr(cq.threading, "Thread", _W)
        try:
            cq.send_via_outlook("x.png", "s", "b", ["a@b.c"], timeout=0.1)
        except cq.DeliveryOutcomeUnknown:
            return
        raise AssertionError("★Outlook 逾時被當成可重試錯誤★ 會啟動第二個 worker")

    def test_it_is_fatal_in_the_job(self):
        tree = ast.parse(textwrap.dedent(inspect.getsource(cq._do_full_job)))
        names = set()
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and node.targets
                    and getattr(node.targets[0], "id", "") == "fatal"):
                names |= {n.id for n in ast.walk(node.value)
                          if isinstance(n, ast.Name)}
        assert "DeliveryOutcomeUnknown" in names, (
            "結果不明沒有列為 fatal → 下一個 attempt 會再寄一封")


# ── P2-02:單層重試 ───────────────────────────────────────────────────────
def test_consult_smtp_send_disables_the_inner_retry():
    """外層 3 attempts × 內層 3 submissions = 最多 9 次提交。只留外層。"""
    got = {}

    def _fake(**kw):
        got.update(kw)
    import cmuh_common.smtp_mail as sm
    real = sm.send_mail
    sm.send_mail = _fake
    try:
        cq.send_via_smtp(None, "s", "b", ["a@b.c"])
    finally:
        sm.send_mail = real
    assert got.get("max_retries") == 0, (
        f"內層仍有自己的重試(max_retries={got.get('max_retries')}) → 兩層相乘")


# ── P2-03:SMTP 逾時不退配額 ─────────────────────────────────────────────
def test_smtp_timeout_does_not_roll_back_the_quota():
    """timeout 可能發生在伺服器已收下 DATA 之後 —— 信可能已送達。

    退回配額會低估實際寄件量;結果不明時保留 reservation(最壞是少一封額度,
    不會超發)。判準:timeout 那條 raise 的路徑上不可有 rollback 呼叫。
    """
    import re

    import cmuh_common.smtp_mail as sm
    src = inspect.getsource(sm.send_mail)
    # [2026-08-06 外審] timeout 在【重試判斷之前】就分流(見 P1-03 修正)。
    # [2026-08-07 外審 P1-02] 又再分成兩條:連線階段(可重試、要退配額)與
    # 【已提交後】(結果不明、不退配額)。本測試管的是後者,故錨到那一條。
    i = src.index("if isinstance(e, socket.timeout):")
    m = re.search(r"\braise\s+\w+", src[i:])
    assert m, "找不到 timeout 的 raise（測試失效了）"
    seg = src[i:i + m.start()]
    assert "_rollback_rate_limit_slot" not in seg, (
        "★逾時仍退配額★ 已送達的信不會被計入額度")
    # 逾時必須拋「結果不明」專屬例外(才不會被外層當可重試 → 重複寄出)
    assert m.group(0).split()[-1] == "DeliveryOutcomeUnknown", (
        f"★SMTP 逾時拋的是 {m.group(0).split()[-1]}★ "
        "必須是 DeliveryOutcomeUnknown,否則外層會重試 → 可能寄兩封")
    # ★不可以先重試再說結果不明★ 從「已提交後逾時」分支到它的 raise 之間,
    # 不得出現任何重試動作 —— 否則預設 max_retries=2 時會先送出第二、三封才
    # 承認不明(止掛提醒走的正是預設值)。
    # (連線階段那條【應該】重試,所以不能只數全檔有沒有 max_retries。)
    assert "continue" not in seg and "max_retries" not in seg, (
        "★『已提交後逾時』分支裡有重試★ 那會在宣告結果不明前多送幾封")


# ── P2-06:告警寄失敗要短期重試 ──────────────────────────────────────────
def test_a_failed_unmanaged_alert_retries_soon(monkeypatch):
    class _T:
        def __init__(self, target=None, **k):
            self._t = target

        def start(self):
            self._t()                      # 同步執行 worker
    monkeypatch.setattr(cq.threading, "Thread", _T)

    def _boom(**kw):
        raise OSError("smtp down")
    import cmuh_common.smtp_mail as sm
    monkeypatch.setattr(sm, "send_mail", _boom)
    monkeypatch.setattr(cq, "_unmanaged_alert_at", 0.0)
    monkeypatch.setattr(cq.time, "time", lambda: 100000.0)

    cq._alert_unmanaged_session(1, "測試")
    # 寄失敗 → 時間戳必須被回撥成「10 分鐘後可再試」,不是整整 6 小時
    wait = (cq._unmanaged_alert_at + cq._UNMANAGED_ALERT_INTERVAL_SEC) - 100000.0
    assert wait <= 600, f"★寄失敗後要等 {wait/60:.0f} 分鐘才會再試★(沉默太久)"


# ── P2-08:截圖檔名同秒不碰撞 ────────────────────────────────────────────
def test_shot_filenames_carry_microseconds_and_pid():
    src = inspect.getsource(cq._materialize_shot)
    assert "%f" in src, "檔名只有秒級 → 同秒兩個 delivery 互相覆蓋"
    assert "getpid" in src, "多 process 同時落地仍可能同名"
