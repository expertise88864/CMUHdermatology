# -*- coding: utf-8 -*-
"""[批次SH] 使用者定案 2026-08-11：

① 登入冷卻要跨重啟。`_login_cooldown_until` 原本只在記憶體裡 —— 任何一次
   行程重啟（自動更新、手動、卡死升級重啟、watchdog）都把 15 分鐘的防鎖定
   冷卻清成 0，下一輪立刻又送一次帳密。防護的用意正是「同一組帳密不要密集
   送出」，而重啟恰好是它最沒有防備的時刻。

② 「資源耗盡」納入既有的閒置重開機看守。建不出隱藏桌面只有兩個原因，
   處置完全相反：USER object 配額耗光（累積的，重開機真的會修好）vs
   群組原則不允許（永久的，重開一百次也一樣）。分辨方法：★本行程之內
   曾經成功過嗎★。分不出來就保守不重開。

   ★而且不可以拿「查詢成功」當恢復★：SW_HIDE 後備模式下查詢照樣成功，
   USER object 卻還是耗盡的、每輪還是在送帳密 —— 那正是要修的事。
"""
import ast
import importlib
import io
import os
import re
import sys

import pytest

NL = chr(10)
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

cq = importlib.import_module("consult_query")
ka = importlib.import_module("cmuh_common.consult_keepalive")

SRC = io.open(os.path.join(REPO_ROOT, "src", "consult_query.py"),
              encoding="utf-8").read()


def _fn_src(name):
    for n in ast.walk(ast.parse(SRC)):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(SRC, n) or ""
    raise AssertionError(f"找不到 {name}")


def _strip_comments(text):
    """★負向斷言先剝註解★（解釋「為什麼不可以」的那句話裡就有那個字面）。"""
    return NL.join(re.sub(r"#.*$", "", ln) for ln in text.splitlines())


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    import cmuh_common.paths as paths
    monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
    cq._login_cooldown_until = 0.0
    cq._job_fail_streak = 0
    cq._job_fail_last_alert = 0.0
    cq._hidden_desktop_state.update({"streak": 0, "ever_ok": False})
    cq._reboot_reasons.clear()
    yield
    cq._login_cooldown_until = 0.0
    cq._hidden_desktop_state.update({"streak": 0, "ever_ok": False})
    cq._reboot_reasons.clear()


# ══ ① 登入冷卻跨重啟 ════════════════════════════════════════════════════
class TestTheLoginCooldownSurvivesARestart:
    def test_it_is_written_and_read_back(self):
        """★核心★ 重啟就把防鎖定保護清成 0 = 那個保護在最需要的時刻不存在。"""
        cq._set_login_cooldown_until(1_800_000_000.0)
        cq._login_cooldown_until = 0.0          # 模擬行程重啟(記憶體歸零)
        cq._load_job_fail_state()
        assert cq._login_cooldown_until == 1_800_000_000.0

    def test_a_successful_login_clears_it_on_disk_too(self, monkeypatch):
        """恢復之後也要落地 —— 否則重啟又把舊冷卻讀回來,白等 15 分鐘。"""
        cq._set_login_cooldown_until(1_800_000_000.0)
        monkeypatch.setattr(cq, "_bde_reboot_cancel", cq.threading.Event())
        cq._note_job_success()
        cq._login_cooldown_until = 12345.0
        cq._load_job_fail_state()
        assert cq._login_cooldown_until == 0.0

    def test_every_cooldown_write_goes_through_the_setter(self):
        """★直接指派全域變數的話那一次就不會落地★（守衛比紀律可靠）。"""
        for fn in ("_cold_start_session_impl", "_note_job_success"):
            body = _strip_comments(_fn_src(fn))
            assert "_login_cooldown_until =" not in body, (
                f"{fn} 直接指派了全域變數,那次冷卻不會落地")
            assert "_set_login_cooldown_until(" in body, fn

    def test_an_old_state_file_without_the_key_is_still_usable(self, tmp_path):
        """舊版寫的檔沒有這個鍵 —— 不可以因此整份忽略(那會連 streak 都丟)。"""
        import json
        p = os.path.join(str(tmp_path), os.path.basename(
            cq._job_fail_state_path()))
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"schema": cq._JOB_FAIL_STATE_SCHEMA,
                       "streak": 4, "last_alert_ts": 0.0}, f)
        cq._load_job_fail_state()
        assert cq._job_fail_streak == 4
        assert cq._login_cooldown_until == 0.0

    def test_a_garbage_cooldown_is_ignored(self, tmp_path):
        import json
        p = os.path.join(str(tmp_path), os.path.basename(
            cq._job_fail_state_path()))
        for bad in ("x", -5, None):
            with open(p, "w", encoding="utf-8") as f:
                json.dump({"schema": cq._JOB_FAIL_STATE_SCHEMA, "streak": 0,
                           "last_alert_ts": 0.0,
                           "login_cooldown_until": bad}, f)
            cq._login_cooldown_until = 0.0
            cq._load_job_fail_state()
            assert cq._login_cooldown_until == 0.0, bad

    def test_a_far_future_value_is_still_capped_by_the_policy(self):
        """壞資料(時鐘跳動/檔案被改)不可以把程式冷凍住 —— 既有守衛要仍然有效。"""
        assert ka.login_cooldown_remaining(
            1e12, 0.0) == 0.0


# ══ ② 資源耗盡 → 閒置重開機 ═════════════════════════════════════════════
class TestResourceExhaustionArmsTheIdleReboot:
    def test_a_streak_after_a_success_arms_the_watch(self, monkeypatch):
        """★核心★ 曾經成功過、現在連續建不出來 = USER object 耗盡,重開機可修。"""
        armed = []
        monkeypatch.setattr(cq, "_schedule_reboot_watch",
                            lambda reason, detail: armed.append(reason))
        cq._note_hidden_desktop_result(True)
        for _ in range(cq._HIDDEN_DESKTOP_FAIL_STREAK_MAX):
            cq._note_hidden_desktop_result(False)
        assert armed == ["RESOURCE"]

    def test_a_machine_that_never_succeeded_is_not_rebooted(self, monkeypatch):
        """★分辨得出來就不要猜★ 從沒成功過 = 群組原則/權限,重開一百次也一樣
        —— 而 24 小時上限擋得住迴圈,擋不住「每天白重開一次」。"""
        armed = []
        monkeypatch.setattr(cq, "_schedule_reboot_watch",
                            lambda reason, detail: armed.append(reason))
        for _ in range(cq._HIDDEN_DESKTOP_FAIL_STREAK_MAX * 3):
            cq._note_hidden_desktop_result(False)
        assert not armed

    def test_the_scheduler_is_idempotent_past_the_threshold(self,
                                                            monkeypatch):
        """★[外審 SH 第 1 輪 P3] 門檻用 `>=` 不是 `==`★

        看守可能因為別的理由退場（24 小時 give_up、shutdown 被拒），那時
        streak 早就超過門檻 —— 用 `==` 的話這個事故從此再也沒有人看著。
        代價由排程端的冪等性吸收：已在站崗就只是續命，不重複開緒、不刷 log。
        """
        armed = []
        monkeypatch.setattr(cq, "_schedule_reboot_watch",
                            lambda reason, detail: armed.append(reason))
        cq._note_hidden_desktop_result(True)
        for _ in range(cq._HIDDEN_DESKTOP_FAIL_STREAK_MAX * 2):
            cq._note_hidden_desktop_result(False)
        assert len(armed) > 1, "超過門檻之後就不再排程 → 看守一旦退場就永久失守"

    def test_scheduling_twice_does_not_start_two_watches(self, monkeypatch):
        started = []
        monkeypatch.setattr(cq.threading, "Thread",
                            lambda **k: type("T", (), {
                                "start": lambda _s: started.append(k.get("name"))})())
        cq._schedule_reboot_watch("RESOURCE", "x")
        cq._schedule_reboot_watch("RESOURCE", "x")
        try:
            assert len(started) == 1, started
        finally:
            cq._bde_watch_active = False

    def test_a_successful_query_does_not_count_as_recovery(self, monkeypatch):
        """★這是最容易搞錯的一條★ SW_HIDE 後備模式下查詢【照樣成功】,
        而 USER object 還是耗盡的、每輪還是在送帳密 —— 那正是要修的事。
        用「查詢成功」當恢復,這個守衛就靜默失效。"""
        cancel = cq.threading.Event()
        monkeypatch.setattr(cq, "_bde_reboot_cancel", cancel)
        cq._reboot_reasons.add("RESOURCE")
        cq._note_job_success()
        assert not cancel.is_set(), "★查詢成功把資源耗盡的站崗解除了★"
        assert "RESOURCE" in cq._reboot_reasons, "原因被錯誤地結掉了"

    def test_a_successful_query_still_cancels_the_bde_watch(self, monkeypatch):
        """反面:BDE 那條的恢復訊號【就是】查詢成功,不可以一起關掉。"""
        cancel = cq.threading.Event()
        monkeypatch.setattr(cq, "_bde_reboot_cancel", cancel)
        cq._reboot_reasons.add("BDE")
        cq._note_job_success()
        assert cancel.is_set()

    def test_the_observation_is_wired_into_both_call_sites(self):
        """★接線★ 沒有呼叫端的話,上面那些性質一件都不會發生。"""
        for fn in ("run_consult_flow", "_rebuild_schedule"):
            assert "_note_hidden_desktop_result(" in _strip_comments(
                _fn_src(fn)), fn

    def test_the_recheck_does_not_double_count(self):
        """★重開前的再驗會再呼叫 `_ensure_hidden_desktop`★ ——
        如果記錄寫在那個函式裡面,再驗一次就把 streak 又加一。"""
        assert "_note_hidden_desktop_result" not in _strip_comments(
            _fn_src("_ensure_hidden_desktop"))


class TestTheWatchRechecksBeforeRebooting:
    def test_it_rechecks_and_bails_when_cleared(self, monkeypatch):
        """看守可能在半夜開火,而休息時段(00-06)根本不輪詢 —— 最後一次觀測
        可能是幾小時前。動手的那一刻要確認狀況還在。"""
        monkeypatch.setattr(cq, "_ensure_hidden_desktop", lambda: 999)
        closed = []
        monkeypatch.setattr(cq._user32, "CloseDesktop",
                            lambda h: closed.append(h))
        cq._reboot_reasons.add("RESOURCE")
        assert cq._reboot_all_conditions_cleared() is True
        assert closed == [999], "再驗時開的 handle 沒有關掉 → 每次驗都洩一個"

    def test_it_proceeds_when_still_broken(self, monkeypatch):
        monkeypatch.setattr(cq, "_ensure_hidden_desktop", lambda: None)
        cq._reboot_reasons.add("RESOURCE")
        assert cq._reboot_all_conditions_cleared() is False

    def test_no_reasons_means_nothing_to_do(self):
        assert cq._reboot_all_conditions_cleared() is True

    def test_the_loop_actually_rechecks(self):
        body = _strip_comments(_fn_src("_bde_reboot_watch_loop"))
        assert "_reboot_all_conditions_cleared(" in body
        i = body.index("_reboot_all_conditions_cleared(")
        j = body.index("_save_last_auto_reboot_ts(")
        assert i < j, "要在寫入時間戳/下 shutdown 之前就驗"


class TestTwoIncidentsAreTrackedIndependently:
    """★[外審 SH 第 1 輪 P1]★ 我第一版用【單一槽位】記「這次是為了什麼站崗」，
    但 BDE 與資源耗盡是兩個各自獨立、可以同時存在的事故。後到的把先到的
    覆寫掉，而看守只有一條 —— 於是一邊恢復就把另一邊【還沒好】的站崗
    一起解除，而且 RESOURCE 一旦錯過排程時機就再也不會重排。
    """

    def test_one_recovery_does_not_cancel_the_other(self, monkeypatch):
        cancel = cq.threading.Event()
        monkeypatch.setattr(cq, "_bde_reboot_cancel", cancel)
        cq._reboot_reasons.update({"BDE", "RESOURCE"})
        cq._clear_reboot_reason("BDE")
        assert not cancel.is_set(), "★另一個原因還沒好就解除站崗了★"
        assert cq._reboot_reasons == {"RESOURCE"}

    def test_the_last_recovery_cancels_the_watch(self, monkeypatch):
        cancel = cq.threading.Event()
        monkeypatch.setattr(cq, "_bde_reboot_cancel", cancel)
        cq._reboot_reasons.update({"BDE", "RESOURCE"})
        cq._clear_reboot_reason("BDE")
        cq._clear_reboot_reason("RESOURCE")
        assert cancel.is_set()

    def test_clearing_a_reason_we_never_watched_does_nothing(self,
                                                             monkeypatch):
        cancel = cq.threading.Event()
        monkeypatch.setattr(cq, "_bde_reboot_cancel", cancel)
        cq._clear_reboot_reason("BDE")
        assert not cancel.is_set(), "沒站崗卻下了取消令(會誤消掉之後的事故)"

    def test_a_hidden_desktop_recovery_leaves_the_bde_watch_alone(
            self, monkeypatch):
        """★finding 2★ 隱藏桌面成功不可以無條件動共用的取消令。"""
        cancel = cq.threading.Event()
        monkeypatch.setattr(cq, "_bde_reboot_cancel", cancel)
        cq._reboot_reasons.add("BDE")
        cq._hidden_desktop_state["streak"] = 2
        cq._note_hidden_desktop_result(True)
        assert not cancel.is_set(), "★把還沒好的 BDE 看守一起解除了★"
        assert cq._reboot_reasons == {"BDE"}

    def test_the_recheck_resets_the_streak_so_it_can_rearm(self, monkeypatch):
        """★finding 3★ 再驗成功若不重置 streak，之後每次失敗都是 4、5、6…
        整段故障期間再也不會重新排定重開機。"""
        monkeypatch.setattr(cq, "_ensure_hidden_desktop", lambda: 999)
        monkeypatch.setattr(cq._user32, "CloseDesktop", lambda h: None)
        cq._hidden_desktop_state.update(
            {"streak": cq._HIDDEN_DESKTOP_FAIL_STREAK_MAX, "ever_ok": True})
        cq._reboot_reasons.add("RESOURCE")
        assert cq._reboot_all_conditions_cleared() is True
        assert cq._hidden_desktop_state["streak"] == 0
        assert not cq._reboot_reasons
        armed = []
        monkeypatch.setattr(cq, "_schedule_reboot_watch",
                            lambda reason, detail: armed.append(reason))
        for _ in range(cq._HIDDEN_DESKTOP_FAIL_STREAK_MAX):
            cq._note_hidden_desktop_result(False)
        assert armed, "再驗成功之後就再也 arm 不起來了"

    def test_a_bde_reason_blocks_the_recheck_from_cancelling(self,
                                                             monkeypatch):
        """BDE 無法重驗（它的恢復是事件式的）→ 一律當成仍在，不可以取消重開。"""
        monkeypatch.setattr(cq, "_ensure_hidden_desktop", lambda: 999)
        monkeypatch.setattr(cq._user32, "CloseDesktop", lambda h: None)
        cq._reboot_reasons.add("BDE")
        assert cq._reboot_all_conditions_cleared() is False



class TestTheCancelEventIsTouchedOnlyInsideTheLock:
    """★[外審 SH 第 2 輪 P1]★ 我上一版是「鎖內移除原因 → 放鎖 → 下取消令」。

    新事故若剛好擠在中間完成「登記原因 + 清令 + 世代 +1」，我們接著把令又
    set 回去 —— 看守醒來看到令還在就退場，接力的那條也立刻看到同一個令而
    退場，最後 `_reboot_reasons` 還有東西、`_bde_watch_active` 卻是 False：
    ★事故完全失去看守★，而休息時段根本不會有下一輪觀測把它救回來。

    競態不好用時序測，但「Event 只能在臨界區內被動」這個不變式可以
    ★決定性地★量到：`_bde_watch_lock` 是非重入鎖，持有時再取一定失敗。
    """

    @staticmethod
    def _spy(monkeypatch, calls):
        class _E(cq.threading.Event):
            def _assert_locked(self, what):
                got = cq._bde_watch_lock.acquire(blocking=False)
                if got:
                    cq._bde_watch_lock.release()
                calls.append((what, not got))

            def set(self):
                self._assert_locked("set")
                super().set()

            def clear(self):
                self._assert_locked("clear")
                super().clear()

        e = _E()
        monkeypatch.setattr(cq, "_bde_reboot_cancel", e)
        return e

    def test_the_last_recovery_sets_it_inside_the_lock(self, monkeypatch):
        calls = []
        self._spy(monkeypatch, calls)
        cq._reboot_reasons.add("BDE")
        cq._clear_reboot_reason("BDE")
        assert ("set", True) in calls, f"★set() 跑到臨界區外★:{calls}"

    def test_scheduling_clears_it_inside_the_lock(self, monkeypatch):
        calls = []
        self._spy(monkeypatch, calls)
        monkeypatch.setattr(cq.threading, "Thread",
                            lambda **k: type("T", (), {"start": lambda _s: None})())
        try:
            cq._schedule_reboot_watch("RESOURCE", "x")
        finally:
            cq._bde_watch_active = False
        assert ("clear", True) in calls, f"★clear() 跑到臨界區外★:{calls}"

    @staticmethod
    def _critical_sections(fn_name):
        """該函式裡的 `with` 區塊 → [區塊內的程式碼]（去註解、去 docstring）。"""
        fn = next(n for n in ast.walk(ast.parse(SRC))
                  if isinstance(n, ast.FunctionDef) and n.name == fn_name)
        return [NL.join(ast.unparse(st) for st in w.body)
                for w in ast.walk(fn) if isinstance(w, ast.With)]

    def test_schedule_uses_exactly_one_critical_section(self):
        """★順序對了還不夠,要在【同一個】臨界區裡★

        拆成兩段的話,兩段之間就是一個窗口 —— 恢復訊號可以擠進去,
        把新事故剛清掉的取消令又 set 回去。突變驗證量出來:只驗順序的話,
        「拆成兩段」這個反例是綠的。
        """
        sections = self._critical_sections("_schedule_reboot_watch")
        assert len(sections) == 1, (
            f"★分成 {len(sections)} 段臨界區★ 中間的窗口會讓事故失去看守")
        for op in ("_reboot_reasons.add", "_bde_reboot_cancel.clear()",
                   "_bde_watch_gen += 1", "_bde_watch_active"):
            assert op in sections[0], f"{op} 不在那個臨界區裡"

    def test_clearing_uses_exactly_one_critical_section(self):
        sections = self._critical_sections("_clear_reboot_reason")
        assert len(sections) == 1, f"分成 {len(sections)} 段"
        assert "_reboot_reasons.discard" in sections[0]
        assert "_bde_reboot_cancel.set()" in sections[0]

    def test_schedule_registers_and_clears_without_an_early_return(self):
        """★單一臨界區★ 中途早退的話,清令/世代就變成有條件執行。"""
        body = _strip_comments(_fn_src("_schedule_reboot_watch"))
        head = body[:body.index("spawn = not _bde_watch_active")]
        assert "return" not in head
        assert "_reboot_reasons.add(" in head
        assert "_bde_reboot_cancel.clear()" in head
        assert "_bde_watch_gen += 1" in head


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
