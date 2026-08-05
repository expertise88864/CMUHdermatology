# -*- coding: utf-8 -*-
"""[2026-07-25 完整 code review 第二批] 「故障看起來跟正常一模一樣」

三個缺陷的共同點：出事時系統照常運轉、log 照常寫，使用者完全看不出異常。
  - 打卡：密碼過期 → AC-09 只擋單次呼叫，每分鐘 re-fire 仍重登（一窗 ~29 次、
    一天 ~145 次）→ 醫院 AD 鎖帳號 → 連本來會成功的窗也全滅。
  - 會診：已回覆而離開清單的病歷號永遠留在「已通知」基準 → 同一床【再次開會診】
    永遠算不出「新」→ 那張會診單不會通知。
  - 會診：輪詢模式整個失敗時誰都不會被通知，而「沒信」本來就是常態 → 永久故障與
    「今天沒有新會診」在使用者眼中完全一樣。
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import autoclock as ac  # noqa: E402
import consult_query as cq  # noqa: E402


# ── 打卡：帳密錯誤不得每分鐘重登 ────────────────────────────────────────────
@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    monkeypatch.setattr(ac, "_clock_state_persistence_enabled", False)
    ac._auth_failed.clear()
    yield
    ac._auth_failed.clear()


def test_auth_failure_is_remembered_per_window_and_account():
    ac._mark_auth_failed("mon_am_in", "u1")
    assert ac._is_auth_failed("mon_am_in", "u1") is True
    assert ac._is_auth_failed("mon_am_in", "u2") is False, "只擋該帳號"
    assert ac._is_auth_failed("mon_pm_out", "u1") is False, \
        "只擋該窗——下一個窗仍給一次機會（使用者可能中途改好密碼）"


def test_auth_failed_expires_next_day(monkeypatch):
    ac._auth_failed[("mon_am_in", "u1")] = "2020-01-01"
    assert ac._is_auth_failed("mon_am_in", "u1") is False, "跨日自動失效"
    ac._mark_auth_failed("mon_am_in", "u1")
    assert ac._auth_failed[("mon_am_in", "u1")] == date.today().isoformat()


def test_task_skips_accounts_blocked_by_auth_failure():
    """源碼守門：process_clock_task 必須把帳密錯誤的帳號濾掉再開 driver。"""
    import inspect
    src = inspect.getsource(ac.process_clock_task)
    assert "_is_auth_failed(schedule_key" in src, \
        "re-fire 必須跳過本窗已知帳密錯誤的帳號"
    i_filter = src.index("_is_auth_failed(schedule_key")
    i_driver = src.index("_get_or_create_clock_driver")
    assert i_filter < i_driver, "要在開 driver / 登入【之前】就濾掉"


def test_auth_error_handler_marks_the_block():
    import inspect
    src = inspect.getsource(ac.perform_clock_action)
    i_exc = src.index("except ClockAuthError")
    assert "_mark_auth_failed(" in src[i_exc:], \
        "ClockAuthError 必須記下當窗封鎖,否則每分鐘 re-fire 會重登到帳號被鎖"


def test_saving_config_clears_auth_block():
    """改好密碼(設定存檔成功) → 立即解除封鎖,不必等隔天。"""
    ac._mark_auth_failed("mon_am_in", "u1")
    ac._clear_auth_failed()
    assert ac._is_auth_failed("mon_am_in", "u1") is False


def test_auth_block_survives_restart_via_state_file(monkeypatch, tmp_path):
    """watchdog/自動更新重啟後不得又重試 29 次 → 封鎖要進 clock_state.json。"""
    monkeypatch.setattr(ac, "CLOCK_STATE_FILE", tmp_path / "clock_state.json")
    monkeypatch.setattr(ac, "_clock_state_persistence_enabled", True)
    ac._mark_auth_failed("mon_am_in", "u1")
    ac._auth_failed.clear()                       # 模擬重啟後記憶體清空
    ac._load_clock_state()
    assert ac._is_auth_failed("mon_am_in", "u1") is True


# ── 會診：基準剪枝 ──────────────────────────────────────────────────────────
def test_notified_baseline_prunes_so_reconsult_is_detected(monkeypatch):
    """★同一床再次會診必須通知得到：已離開清單的病歷號要從基準剪除。"""
    saved = {}
    monkeypatch.setattr(cq, "_notified_memory", {"A1", "B2"})
    monkeypatch.setattr(cq, "_notified_initialized", True)
    monkeypatch.setattr(cq, "_save_notified",
                        lambda charts: saved.update(v=set(charts)))
    # 目前清單只剩 B2（A1 已回覆離開）
    cur = {"B2"}
    assert not (cur - cq._load_notified()), "情境前提：沒有新病歷號"
    # 模擬程式在「無新會診」路徑要做的剪枝
    if cur != cq._load_notified():
        cq._save_notified(cur)
    assert saved["v"] == {"B2"}, "A1 應被剪除,否則它日後再會診永遠不通知"


def test_poll_no_new_path_prunes_baseline():
    """源碼守門：「無新會診」的提早 return 之前必須剪枝基準。

    ★[2026-08-04] 錨點改成變數名，不再綁死運算式★
    原本錨在整串 `_new = _poll_sig - _load_notified()`。外審 P1-04 把右手邊換成
    `_new_consult_ids(_poll_sig)`（升級時要以病歷號粒度比對，否則整份重寄）之後，
    `str.index` 直接 ValueError —— 測試紅在「找不到錨點」而不是「性質沒了」。
    掃原始碼的守衛，程式碼一搬家就失效；至少要讓錨點不綁實作細節。
    """
    import inspect
    src = inspect.getsource(cq._do_full_job)
    i_new = src.index("_new = ")          # 只錨變數名，右手邊怎麼算都可以
    i_ret = src.index("return", i_new)
    seg = src[i_new:i_ret]
    # [2026-08-05 外審第 6 輪 P2-01] 基準寫入統一走 eligibility 入口
    assert "_save_notified_if_eligible(" in seg, \
        "無新會診時也要把離開清單者從基準剪除（否則再會診永不通知）"
    assert "_save_notified(_poll_sig)" not in seg, \
        "剪枝不可繞過 eligibility 入口(未確認的短清單會剪掉還在的會診)"


# ── 會診：連續失敗要有人知道 ────────────────────────────────────────────────
def test_consecutive_failures_alert_after_threshold(monkeypatch):
    sent = []
    monkeypatch.setattr(cq, "_job_fail_streak", 0)
    monkeypatch.setattr(cq, "_job_fail_last_alert", 0.0)

    class _T:
        def __init__(self, target=None, **k):
            self._t = target

        def start(self):
            self._t()
    monkeypatch.setattr(cq.threading, "Thread", _T)
    monkeypatch.setattr("cmuh_common.smtp_mail.send_mail",
                        lambda **kw: sent.append(kw))

    for _ in range(cq._JOB_FAIL_ALERT_THRESHOLD - 1):
        cq._note_job_failure(["a@b.c"], "boom")
    assert not sent, "未達門檻不得告警（避免暫時性抖動洗信箱）"
    cq._note_job_failure(["a@b.c"], "boom")
    assert len(sent) == 1, "達門檻應寄一封告警"
    assert "連續失敗" in sent[0]["subject"]

    cq._note_job_failure(["a@b.c"], "boom")       # 冷卻期內不重寄
    assert len(sent) == 1


def test_success_resets_failure_streak(monkeypatch):
    monkeypatch.setattr(cq, "_job_fail_streak", 5)
    cq._note_job_success()
    assert cq._job_fail_streak == 0


def test_giveup_path_calls_failure_notice():
    """源碼守門：重試用盡的分支必須通報（舊版只有 email 觸發者收得到）。"""
    import inspect
    src = inspect.getsource(cq._do_full_job)
    i_giveup = src.index("已重試 %d 次仍失敗")
    assert "_note_job_failure(" in src[i_giveup:], \
        "放棄時必須累計並在達門檻時告警,否則輪詢故障無人知曉"


# ── codex deep 第一輪 findings 的回歸測試 ──────────────────────────────────
def test_clear_auth_failed_wipes_disk_state_even_in_configure_process(
        monkeypatch, tmp_path):
    """[codex] ★真實流程：tray→設定 是以 --configure 重啟的【新行程】,不啟動
    scheduler → 沒載入狀態、persistence 旗標也是 False。舊版只看記憶體 `had`,
    什麼都不做 → 回背景模式後封鎖又從磁碟載回,該帳號整窗仍被跳過(可能漏打卡)。
    解除必須直接改寫磁碟狀態,且保留其他欄位。"""
    import json
    state = tmp_path / "clock_state.json"
    today = date.today().isoformat()
    state.write_text(json.dumps({
        "date": today,
        "clock_done": [["mon_am_in", "u9"]],
        "missed_warned": ["mon_pm_out"],
        "auth_failed": [["mon_am_in", "u1"]],
    }), encoding="utf-8")
    monkeypatch.setattr(ac, "CLOCK_STATE_FILE", state)
    monkeypatch.setattr(ac, "_clock_state_persistence_enabled", False)  # --configure
    ac._auth_failed.clear()                       # 設定行程沒載入過狀態

    ac._clear_auth_failed()

    raw = json.loads(state.read_text(encoding="utf-8"))
    assert raw["auth_failed"] == [], "磁碟上的封鎖必須被清掉"
    assert raw["clock_done"] == [["mon_am_in", "u9"]], "其餘欄位不可被破壞"
    assert raw["missed_warned"] == ["mon_pm_out"]
    assert raw["date"] == today


def test_healthy_quiet_poll_resets_failure_streak():
    """[codex] 「跑成功但沒有新會診」是健康的一輪 → 必須清零連續失敗計數,
    否則零星失敗會被累加成假的連續故障,恢復後也永遠清不掉。"""
    import inspect
    src = inspect.getsource(cq._do_full_job)
    i_nonew = src.index("無新會診 → 不寄信")
    i_ret = src.index("return", i_nonew)
    assert "_note_job_success()" in src[i_nonew:i_ret], \
        "無新會診的成功輪詢必須清零 streak"
    i_base = src.index("首次建立會診基準")
    i_ret2 = src.index("return", i_base)
    assert "_note_job_success()" in src[i_base:i_ret2], \
        "首次建立基準也是成功的一輪"


def test_health_alert_goes_to_the_developer_only():
    """★[2026-08-02 使用者定案] 推翻 2026-07-25 的「寄給團隊名單」★

    使用者實際收到那封信(收件人有六位臨床同仁)之後定案:
    「這種系統提示錯誤的信件直接寄給開發者email」——系統/自動化故障訊息不該騷擾
    整組臨床人員,他們對「等不到主畫面」也無從處理。
    臨床事件(會診查詢結果、email 觸發的回信)仍照舊寄給各自的名單。

    順帶說明:原本兩個坑(email 觸發時 recipients 已被改寫成觸發醫師本人、
    團隊名單為空時不可 `or recipients` 當後備)因為收件人不再取自 cfg 而自然消失,
    但下面仍反向釘住,避免哪天有人又把 cfg 接回去。
    """
    import inspect
    src = inspect.getsource(cq._do_full_job)
    i = src.index("_note_job_failure(")
    seg = src[i:i + 200]
    assert "_developer_alert_recipients()" in seg, \
        "系統故障告警必須寄給開發者"
    assert 'cfg.get("recipients")' not in seg, \
        "不可再取自 cfg(那是臨床名單)"
    assert "or recipients" not in seg, "更不可退回被觸發者改寫過的 recipients"


def test_developer_alert_uses_the_single_declaration():
    """開發者信箱只宣告一次(cmuh_common.settings_defaults),不可各自硬編碼。"""
    from cmuh_common.settings_defaults import DEVELOPER_ALERT_EMAIL
    assert cq._developer_alert_recipients() == [DEVELOPER_ALERT_EMAIL]
    import inspect
    src = inspect.getsource(cq)
    # 注意:cfg 的 recipients / test_recipients / email_trigger_recipients 裡
    # 本來就【合法】含有開發者本人(他是臨床名單的一員),那與「告警信箱」無關。
    # 這裡要釘的是:告警用的那個函式來自單一宣告處,不是自己再定義一份。
    assert "from cmuh_common.settings_defaults import" in src
    assert "developer_alert_recipients as _developer_alert_recipients" in src
    assert "def _developer_alert_recipients" not in src, \
        "不可在 consult_query 內自己再定義一份"


def test_no_team_recipients_does_not_burn_cooldown(monkeypatch):
    """無團隊收件人 → 記 warning 但【不啟動六小時冷卻】,以免之後補得到人時反而寄不出。"""
    monkeypatch.setattr(cq, "_job_fail_streak", cq._JOB_FAIL_ALERT_THRESHOLD - 1)
    monkeypatch.setattr(cq, "_job_fail_last_alert", 0.0)
    cq._note_job_failure([], "boom")              # 達門檻但沒有收件人
    assert cq._job_fail_last_alert == 0.0, "沒寄出就不可消耗冷卻"

    sent = []

    class _T:
        def __init__(self, target=None, **k):
            self._t = target

        def start(self):
            self._t()
    monkeypatch.setattr(cq.threading, "Thread", _T)
    monkeypatch.setattr("cmuh_common.smtp_mail.send_mail",
                        lambda **kw: sent.append(kw))
    cq._note_job_failure(["team@x.y"], "boom")    # 補到收件人 → 應立刻寄得出去
    assert len(sent) == 1
