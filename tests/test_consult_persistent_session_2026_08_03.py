# -*- coding: utf-8 -*-
"""[2026-08-03 使用者定案] 會診查詢改「常駐登入」。

規格：登入一次停在主畫面，每 3 分鐘(±10%)按一次會診查詢、擷取完按「回」退回
主畫面（查詢本身就是 keepalive，院方 5 分鐘閒置會強制登出）；掉線→殺掉重啟、
重新登入一次，能登入就繼續常駐；00:00-06:00 休息並收掉 session；每 6 小時定期
重啟 HIS；BDE 錯誤→使用者連續閒置 30 分鐘後自動重開機（24 小時內最多一次）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402
from cmuh_common import consult_keepalive as ka  # noqa: E402


# ─── 純政策：輪詢間隔 ────────────────────────────────────────────────────────
def test_poll_range_is_plus_minus_ten_percent():
    lo, hi = ka.poll_seconds_range(3)
    assert (lo, hi) == (162, 198)          # 180s ±10%（使用者指定 ±10%）


def test_poll_range_clamps_and_survives_garbage():
    assert ka.poll_seconds_range(0)[0] >= ka.POLL_MIN_MINUTES * 60 * 0.9 - 1
    assert ka.poll_seconds_range(999)[1] <= ka.POLL_MAX_MINUTES * 60 * 1.1 + 1
    assert ka.poll_seconds_range("bad") == ka.poll_seconds_range(3)
    assert ka.poll_seconds_range(None) == ka.poll_seconds_range(3)
    assert ka.poll_seconds_range(float("nan")) == ka.poll_seconds_range(3)


# ─── 純政策：6 小時定期重啟 ─────────────────────────────────────────────────
def test_session_restart_after_six_hours():
    t0 = 1_000_000.0
    assert not ka.session_needs_restart(t0, t0 + 6 * 3600 - 1)
    assert ka.session_needs_restart(t0, t0 + 6 * 3600)
    assert ka.session_needs_restart(t0, t0 - 10), "時鐘倒退 → 重啟(成本低,狀態穩)"


# ─── 純政策：登入冷卻 ────────────────────────────────────────────────────────
def test_login_cooldown_remaining():
    now = 5_000.0
    assert ka.login_cooldown_remaining(0.0, now) == 0.0
    assert ka.login_cooldown_remaining(now + 60, now) == 60.0
    # 異常大的未來值＝壞資料 → 不得把登入鎖死
    assert ka.login_cooldown_remaining(now + 10 * ka.LOGIN_COOLDOWN_SECONDS,
                                       now) == 0.0


# ─── 純政策：BDE 重開機門檻 ──────────────────────────────────────────────────
def test_bde_reboot_waits_while_user_recently_active():
    action, why = ka.bde_reboot_decision(29 * 60, None, 1e9)
    assert action == "wait" and "30" in why


def test_bde_reboot_allowed_when_idle_and_never_rebooted():
    action, _ = ka.bde_reboot_decision(31 * 60, None, 1e9)
    assert action == "reboot"


def test_bde_reboot_gives_up_within_24h_of_last_reboot():
    """重開後仍 BDE＝重開機修不了 → 絕不能進入重開機迴圈,改人工。"""
    now = 1e9
    action, why = ka.bde_reboot_decision(31 * 60, now - 2 * 3600, now)
    assert action == "give_up" and "人工" in why
    action, _ = ka.bde_reboot_decision(31 * 60, now - 25 * 3600, now)
    assert action == "reboot", "超過 24 小時 → 可以再試一次"


def test_bde_reboot_future_timestamp_still_blocks():
    """[codex P1 R7] 重開後時鐘倒退 → gap 為負,一樣算冷卻期內(防護要保守)。"""
    now = 1e9
    action, _ = ka.bde_reboot_decision(31 * 60, now + 3600, now)
    assert action == "give_up", "未來時間戳不得放行重開機"


def test_bde_reboot_bad_state_fails_open_to_reboot():
    action, _ = ka.bde_reboot_decision(31 * 60, "garbage", 1e9)
    assert action == "reboot", "壞狀態檔不得永久封鎖修復手段"


# ─── 常駐 session 接線（源碼守衛＋輕量功能）─────────────────────────────────
def test_session_close_without_session_is_silent():
    with cq._session_lock:
        assert cq._psession is None
    cq._session_close("測試")                  # 不得拋出


def test_default_poll_interval_is_three_minutes():
    assert cq.DEFAULT_CONFIG["poll_interval_minutes"] == 3


def test_load_config_migrates_old_default_15_to_3(monkeypatch, tmp_path):
    """既有部署存的是舊預設 15 → 一次性升級為 3;使用者之後刻意改回 15 不再覆蓋。"""
    import json
    cfg_file = tmp_path / "consult_query_config.json"
    cfg_file.write_text(json.dumps({"poll_interval_minutes": 15}),
                        encoding="utf-8")
    monkeypatch.setattr(cq, "CONFIG_FILE", cfg_file)
    cfg = cq.load_config()
    assert cfg["poll_interval_minutes"] == 3
    assert cfg["keepalive_migrated_v1"] is True
    # [codex P1 R5] 遷移必須【落地到檔案】——只在記憶體是 3、檔案還是 15 的話,
    # 下次啟動旗標擋住遷移 → 永遠退回 15,常駐被 5 分鐘登出打掉。
    persisted = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert persisted["poll_interval_minutes"] == 3
    # 使用者刻意改回 15 → 旗標已立,不得再被覆蓋
    saved = json.loads(cfg_file.read_text(encoding="utf-8"))
    saved["poll_interval_minutes"] = 15
    cfg_file.write_text(json.dumps(saved), encoding="utf-8")
    assert cq.load_config()["poll_interval_minutes"] == 15


def test_load_config_clamps_floor_at_two_minutes(monkeypatch, tmp_path):
    import json
    cfg_file = tmp_path / "c.json"
    cfg_file.write_text(json.dumps({"poll_interval_minutes": 1}),
                        encoding="utf-8")
    monkeypatch.setattr(cq, "CONFIG_FILE", cfg_file)
    assert cq.load_config()["poll_interval_minutes"] == 2


def test_settings_ui_save_keeps_three_minutes():
    """[codex P1 R5] 開設定→存檔不可把 3 夾回 5(否則常駐被 5 分鐘登出打掉)。"""
    import inspect
    src = inspect.getsource(cq.ConfigApp._collect)
    assert 'max(2, min(120, int(self.interval_var' in src
    assert 'max(5, min(120, int(self.interval_var' not in src


def test_cold_start_checks_cooldown_before_spawning():
    """冷卻檢查必須在 CreateProcess 之前——冷卻中連 HIS 都不該碰。"""
    import inspect
    src = inspect.getsource(cq._cold_start_session_impl)
    assert src.index("login_cooldown_remaining") < src.index("CreateProcess")


def test_cold_start_sets_cooldown_on_login_failure():
    import inspect
    src = inspect.getsource(cq._cold_start_session_impl)
    i_login = src.index("isinstance(e, LoginNotCompleted)")
    assert "LOGIN_COOLDOWN_SECONDS" in src[i_login:i_login + 700]
    i_bde = src.index("isinstance(e, HISStartupBlocked)")
    assert "BDE_COOLDOWN_SECONDS" in src[i_bde:i_bde + 300]


def test_any_failure_after_creds_sent_triggers_cooldown():
    """[codex P1 R6] 帳密送出後的通用失敗(非 LoginNotCompleted)也要冷卻——
    否則 3 分鐘節奏照樣每 3 分鐘送一次帳密。"""
    import inspect
    src = inspect.getsource(cq._cold_start_session_impl)
    assert "creds_sent = False" in src
    i_click = src.index("click_button(confirm)")
    assert "creds_sent = True" in src[i_click:i_click + 120]
    assert "or creds_sent" in src, "冷卻條件要涵蓋「帳密已送出」的任何失敗"


def test_recovery_is_kill_restart_relogin_once():
    """掉線恢復＝殺掉重啟、重登一次;再失敗 → 收掉並放棄(交給告警)。"""
    import inspect
    src = inspect.getsource(cq._automation_on_hidden)
    assert "殺掉重啟" in src
    assert src.count("_cold_start_session(cfg)") == 1, "只重登【一次】"
    assert src.count("_query_cycle(sess") == 2, "原始一次 + 恢復後一次"
    # 登入類失敗不得觸發「再登一次」(那正是鎖定門檻的來源)
    i_fatal = src.index("except (LoginNotCompleted, HISStartupBlocked")
    i_recover = src.index("except Exception as e:")
    assert i_fatal < i_recover


def test_return_to_main_clicks_back_with_wm_close_fallback():
    """「回」優先、右上角X(WM_CLOSE)後備;關不掉收 session 但★不拋★
    (查詢已成功,不能因退場失敗丟結果)。"""
    import inspect
    src = inspect.getsource(cq._return_to_main)
    assert '"回"' in src and "WM_CLOSE" in src
    assert "_session_close_if_current(sess" in src
    assert "raise" not in src, "退場失敗不得拋例外"


def test_quiet_hours_closes_the_session():
    import inspect
    src = inspect.getsource(cq._do_full_job)
    i_quiet = src.index("休息時段(%02d:00-%02d:00)")
    assert "_session_close(" in src[i_quiet:i_quiet + 800]
    # [codex P2 R10] 休息時段判斷要在寄信前置檢查【之前】——否則 SMTP/Outlook
    # 不可用時提早 return,session 收不到,殭屍行程掛整夜。
    assert i_quiet < src.index('mail_method = str(cfg.get("mail_method"')


def test_orphan_cleanup_spares_the_live_session():
    import inspect
    src = inspect.getsource(cq._cleanup_orphan_systemftp)
    assert "_session_pids()" in src, "活著的常駐 session 不是孤兒,不得被清掃誤殺"


def test_schedule_uses_seconds_jitter_and_sw_hide_stays_slow():
    """常駐模式 3 分鐘 ±10%;SW_HIDE 後備(每輪完整登入)必須維持 ≥15 分鐘——
    3 分鐘節奏套在冷啟動上=每小時送 20 次帳密,正是鎖定門檻的來源。"""
    import inspect
    src = inspect.getsource(cq._rebuild_schedule)
    assert "poll_seconds_range" in src and ".seconds.do(" in src
    assert "max(15, interval)" in src and ".minutes.do(" in src


def test_persistent_cadence_capped_below_his_timeout(monkeypatch):
    """[codex P1 R8] 設定 30 分鐘也要被夾到 ≤4 分鐘(±10% 最長 264s < 300s 登出線)
    ——否則 session 每輪過期,每輪都冷啟動登入,常駐等於沒做。"""
    # [codex R9/R18] 間隔從任務【結束】起算,還要扣寄信尾段:
    # SMTP 尾段 ≤60s、Outlook 尾段最壞 120s,兩種模式都必須 <300s。
    assert (ka.POLL_KEEPALIVE_CAP_MINUTES * 60 * (1 + ka.POLL_JITTER_RATIO)
            + 60) < 300
    assert (ka.POLL_KEEPALIVE_CAP_OUTLOOK_MINUTES * 60
            * (1 + ka.POLL_JITTER_RATIO) + 120) < 300
    monkeypatch.setattr(cq, "_ensure_hidden_desktop", lambda: 1234)
    monkeypatch.setattr(cq._user32, "CloseDesktop", lambda h: None,
                        raising=False)
    base = cq.load_config()
    base.update(enabled=True, poll_interval_minutes=30,
                extract_text_enabled=True, mail_method="smtp")
    monkeypatch.setattr(cq, "load_config", lambda: base)
    cq._rebuild_schedule()
    try:
        j = cq.schedule.get_jobs()[0]
        assert j.unit == "seconds" and j.latest <= 198, (j.interval, j.latest)
    finally:
        cq.schedule.clear()
    # Outlook 模式:尾段最壞 120s → 上限再夾到 2 分鐘(132s)
    base["mail_method"] = "outlook"
    cq._rebuild_schedule()
    try:
        j = cq.schedule.get_jobs()[0]
        assert j.unit == "seconds" and j.latest <= 132, (j.interval, j.latest)
    finally:
        cq.schedule.clear()


def test_runtime_desktop_loss_demotes_the_schedule():
    """[codex P1 R17] 排程建好後隱藏桌面才失效 → SW_HIDE 後備啟用時要立刻把
    排程降回 ≥15 分鐘(否則每 3 分鐘完整登入)。"""
    import inspect
    src = inspect.getsource(cq.run_consult_flow)
    i_fb = src.index("SW_HIDE 後備模式")
    assert "_demote_schedule_to_legacy()" in src[i_fb:],         "後備模式啟用前要先降速排程"
    helper = inspect.getsource(cq._demote_schedule_to_legacy)
    assert '_sched_mode != "keepalive"' in helper and "_rebuild_schedule()" in helper


def test_quiet_hours_trigger_does_not_retain_session():
    """[codex P2 R21] 休息時段的 email/手動觸發查完即收——之後沒有 keepalive,
    留著只會被登出後呆掛到 06:00。兩條成功路徑都要收。"""
    import inspect
    src = inspect.getsource(cq._automation_on_hidden)
    assert src.count("_retire_session_if_no_keepalive(sess, cfg)") == 2
    helper = inspect.getsource(cq._retire_session_if_no_keepalive)
    assert "_in_quiet_hours(" in helper
    assert "_session_close_if_current(" in helper


def test_exit_closes_the_session():
    import inspect
    src = inspect.getsource(cq.exit_action)
    assert "_session_close(" in src


def test_job_success_clears_cooldown_and_cancels_reboot_watch():
    import inspect
    src = inspect.getsource(cq._note_job_success)
    assert "_login_cooldown_until = 0.0" in src
    assert "_bde_reboot_cancel.set()" in src


# ─── BDE 閒置重開機接線 ─────────────────────────────────────────────────────
def test_bde_fatal_branch_schedules_the_reboot_watch():
    import inspect
    src = inspect.getsource(cq._do_full_job)
    i = src.index("住院醫囑系統起不來 → 不重試")
    assert "_schedule_bde_reboot_watch()" in src[i:i + 400]


def test_reboot_state_is_saved_before_shutdown_is_issued():
    """★防重開機迴圈★ 狀態先落地、shutdown 後下;順序反了=重開後狀態沒寫到,
    BDE 沒修好就會再重開,無限迴圈。"""
    import inspect
    src = inspect.getsource(cq._bde_reboot_watch_loop)
    assert src.index("_save_last_auto_reboot_ts(") < src.index('"shutdown"')
    assert '"/r"' in src, "要重開機(/r),不是關機"


def test_reboot_aborts_when_state_cannot_be_persisted():
    """[codex P1] 狀態寫不進磁碟 → 不得重開機(24 小時防護等於不存在,會迴圈)。"""
    import inspect
    src = inspect.getsource(cq._bde_reboot_watch_loop)
    i = src.index("if not _save_last_auto_reboot_ts(")
    seg = src[i:i + 400]
    assert "取消自動重開機" in seg and "return" in seg
    assert src.index("if not _save_last_auto_reboot_ts(") < src.index('"shutdown"')


def test_save_reboot_state_reports_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(cq, "get_settings_dir", lambda: str(tmp_path))
    assert cq._save_last_auto_reboot_ts(1.0) is True
    monkeypatch.setattr(cq, "atomic_write_json",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk")))
    assert cq._save_last_auto_reboot_ts(2.0) is False


def test_rejected_shutdown_rolls_back_the_timestamp(tmp_path, monkeypatch):
    """[codex P1 R2] shutdown rc≠0=根本沒有重開 → 時間戳要回滾,不得白吃 24 小時
    防護(下一次閒置窗口要能再試)。"""
    import inspect
    src = inspect.getsource(cq._bde_reboot_watch_loop)
    assert "returncode" in src or "rc = cp.returncode" in src
    i = src.index("if rc != 0:")
    assert "回滾" in src[i:i + 500]
    # 功能驗證:rc=1 → 時間戳回到原值
    monkeypatch.setattr(cq, "get_settings_dir", lambda: str(tmp_path))
    cq._save_last_auto_reboot_ts(111.0)

    class _CP:
        returncode = 1
        stdout = b""
        stderr = b""
    monkeypatch.setattr(cq, "_user_idle_seconds", lambda: 31 * 60)
    monkeypatch.setattr(cq.subprocess, "run", lambda *a, **k: _CP())
    monkeypatch.setattr(cq._bde_reboot_cancel, "wait", lambda timeout: False)
    monkeypatch.setattr(cq.ka if hasattr(cq, "ka") else cq._keepalive,
                        "bde_reboot_decision",
                        lambda idle, last, now: ("reboot", "test"))
    with cq._bde_watch_lock:
        cq._bde_watch_active = True
    try:
        cq._bde_reboot_watch_loop(cq._bde_watch_gen)
    finally:
        with cq._bde_watch_lock:
            cq._bde_watch_active = False
    assert cq._load_last_auto_reboot_ts() == 111.0, "被拒後時間戳應回滾"


def test_recovery_during_countdown_aborts_the_shutdown():
    """[codex P1 R15] shutdown 下達後的 60 秒倒數期間 HIS 恢復 → shutdown /a
    取消,並回滾時間戳;/a 失敗 → 照常重開(不得假裝取消了)。"""
    import inspect
    src = inspect.getsource(cq._bde_reboot_watch_loop)
    i_ok = src.index("_bde_reboot_cancel.wait(timeout=55.0)")
    seg = src[i_ok:]
    assert '"shutdown", "/a"' in seg.replace("[", "").replace("]", ""),         "取消要用 shutdown /a"
    assert "_rollback_ts(" in seg, "取消成功要回滾時間戳"
    assert seg.index('"/a"') < seg.index("_rollback_ts("),         "先確認 /a 成功才可回滾(否則機器照樣重開,時間戳卻被清掉)"


def test_exit_aborts_a_pending_reboot(monkeypatch):
    """[codex P1 R19] 退出程式時倒數中的重開機要 shutdown /a 取消——退出只是關
    程式,OS 的重開機不會自己消失。"""
    import inspect
    src = inspect.getsource(cq.exit_action)
    assert "_abort_bde_shutdown_on_exit()" in src
    calls = []

    class _CP:
        returncode = 0
        stdout = b""
        stderr = b""
    monkeypatch.setattr(cq.subprocess, "run",
                        lambda cmd, **k: calls.append(list(cmd)) or _CP())
    with cq._bde_watch_lock:
        cq._bde_shutdown_pending = True
    try:
        cq._abort_bde_shutdown_on_exit()
        assert ["shutdown", "/a"] in calls
        with cq._bde_watch_lock:
            assert cq._bde_shutdown_pending is False
        # 沒有排定中的重開 → 不得亂下 /a
        calls.clear()
        cq._abort_bde_shutdown_on_exit()
        assert calls == []
    finally:
        with cq._bde_watch_lock:
            cq._bde_shutdown_pending = False
        cq._bde_reboot_cancel.clear()


def test_reboot_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(cq, "get_settings_dir", lambda: str(tmp_path))
    assert cq._load_last_auto_reboot_ts() is None
    cq._save_last_auto_reboot_ts(123456.5)
    assert cq._load_last_auto_reboot_ts() == 123456.5


def test_idle_probe_failure_means_not_idle(monkeypatch):
    """GetLastInputInfo 失敗 → 回 0＝當作剛有輸入:寧可不重開,絕不誤重開。"""
    monkeypatch.setattr(cq._user32, "GetLastInputInfo",
                        lambda *_a: 0, raising=False)
    assert cq._user_idle_seconds() == 0.0


def test_new_bde_failure_wins_the_cancel_race():
    """[codex P2 R3] 成功→cancel set→新 BDE 事故(cancel 還沒被舊看守消化)：
    schedule 在鎖內先清令;舊看守醒來發現令已作廢 → 繼續站崗,不得退場。"""
    import inspect
    src = inspect.getsource(cq._schedule_bde_reboot_watch)
    i_clear = src.index("_bde_reboot_cancel.clear()")
    i_active = src.index("if _bde_watch_active:")
    assert i_clear < i_active, "清令必須在 active 檢查之前(同一把鎖內)"
    loop = inspect.getsource(cq._bde_reboot_watch_loop)
    i_wait = loop.index("_bde_reboot_cancel.wait(")
    i_recheck = loop.index("if _bde_reboot_cancel.is_set():")
    assert i_wait < i_recheck, "wait 醒來後要在鎖內重驗令是否還在"
    assert "_bde_watch_lock" in loop[i_wait:i_recheck], "重驗必須拿鎖(與 schedule 原子)"
    assert "continue" in loop[i_recheck:i_recheck + 400], "令被作廢 → 繼續站崗"


def test_teardown_race_respawns_for_new_incident():
    """[codex P2 R4] 看守決定退場→finally 之間來了新事故(schedule 看到 active=True
    沒開新緒) → finally 在鎖內發現世代前進 → 接力開新看守,不留無人看守的事故。"""
    import inspect
    src = inspect.getsource(cq._bde_reboot_watch_loop)
    i_fin = src.index("finally:")
    tail = src[i_fin:]
    assert "_bde_watch_gen != my_gen" in tail, "退場前要在鎖內驗世代"
    assert "threading.Thread" in tail, "世代前進 → 接力開新看守"
    sched = inspect.getsource(cq._schedule_bde_reboot_watch)
    assert "_bde_watch_gen += 1" in sched
    i_gen = sched.index("_bde_watch_gen += 1")
    i_act = sched.index("if _bde_watch_active:")
    assert i_gen < i_act, "世代要在 active 檢查前(同一把鎖內)遞增"


def test_stale_worker_never_kills_the_replacement_session():
    """[codex P1 R11] 逾時被接管的舊 worker 走到錯誤處理時,全域可能已是新 session
    → 一律用 _session_close_if_current(sess) 只收自己的;身分不符只終結自己行程。"""
    import inspect
    src = inspect.getsource(cq._automation_on_hidden)
    assert "_session_close_if_current(sess" in src
    assert "_session_close(" not in src, "worker 內不得無條件收全域 session"
    helper = inspect.getsource(cq._session_close_if_current)
    assert "_psession is sess" in helper, "要比對身分,不是無條件清參照"
    acq = inspect.getsource(cq._acquire_session)
    assert "_session_close_if_current(" in acq


class _FakeHandle:
    def Close(self):
        pass


def test_close_if_current_spares_the_new_session(monkeypatch):
    """功能驗證:全域已換人 → 舊 sess 只自理,新 session 原封不動。"""
    monkeypatch.setattr(cq, "_terminate_session_process", lambda s: None)
    old_sess = cq._PersistentSession(_FakeHandle(), _FakeHandle(), 111, {111})
    new_sess = cq._PersistentSession(_FakeHandle(), _FakeHandle(), 222, {222})
    with cq._session_lock:
        cq._psession = new_sess
    try:
        assert cq._session_close_if_current(old_sess, "測試:舊 worker 收尾")             is False
        with cq._session_lock:
            assert cq._psession is new_sess, "新 session 不得被舊 worker 殺掉"
        assert cq._session_close_if_current(new_sess, "測試:現任收尾") is True
        with cq._session_lock:
            assert cq._psession is None
    finally:
        with cq._session_lock:
            cq._psession = None


def test_concurrent_cold_start_is_refused(monkeypatch):
    """[codex P1 R14] 另一個冷啟動(登入)仍在進行 → 本輪直接放棄,不並行送帳密;
    逾期預約(>360s,孤兒殘留)則可搶走。"""
    import pytest as _pt
    sentinel = object()
    monkeypatch.setattr(cq, "_cold_start_session_impl",
                        lambda cfg, owner_token=None: sentinel)
    import time as _time
    with cq._session_lock:
        cq._cold_start_owner = (object(), _time.time())      # 進行中(未逾期)
    try:
        with _pt.raises(RuntimeError):
            cq._cold_start_session({})
        with cq._session_lock:                               # 逾期 → 可搶走
            cq._cold_start_owner = (object(),
                                    _time.time() - cq._COLD_START_STALE_SECONDS - 1)
        assert cq._cold_start_session({}) is sentinel
        with cq._session_lock:
            assert cq._cold_start_owner is None, "結束後要釋放預約"
    finally:
        with cq._session_lock:
            cq._cold_start_owner = None


def test_stolen_reservation_blocks_publish():
    """[codex P1 R16] 冷啟動拖太久被搶走預約 → 完成時不得發布,要自行收掉。"""
    import inspect
    src = inspect.getsource(cq._cold_start_session_impl)
    i_sess = src.index("sess = _PersistentSession(")
    seg = src[i_sess:]
    assert "stolen" in seg and "_cold_start_owner" in seg, "發布前要在鎖內驗預約"
    assert "_terminate_session_process(sess)" in seg, "被搶走 → 自行收掉剛開的行程"
    assert seg.index("stolen = ") < seg.index("_psession = sess"),         "驗證與發布必須同一把鎖內完成"


def test_cold_start_stale_threshold_exceeds_login_waits():
    """預約逾期門檻必須大於合法冷啟動上限(登入 120s+主畫面 120s),否則會
    搶走還活著的登入。"""
    assert cq._COLD_START_STALE_SECONDS > 240


def test_dethroned_worker_must_not_relogin():
    """[codex P1 R13] 已卸任的孤兒 worker 不得走「重登一次」——兩個 worker 各開
    session 會互相蓋掉參照、重複送帳密。恢復分支要先驗所有權。"""
    import inspect
    src = inspect.getsource(cq._automation_on_hidden)
    i_close = src.index('was_current = _session_close_if_current(sess, "查詢失敗')
    i_cold = src.index("sess = _cold_start_session(cfg)")
    assert i_close < i_cold
    seg = src[i_close:i_cold]
    assert "if not was_current:" in seg and "raise" in seg,         "重登之前必須驗所有權,已卸任者直接中止"


def test_in_use_session_is_never_shared(monkeypatch):
    """[codex P1 R12] 逾時孤兒仍握著 session → 奪走終結+冷啟動,絕不共用。"""
    terminated = []
    monkeypatch.setattr(cq, "_terminate_session_process",
                        lambda s: terminated.append(s.pid))
    sentinel = object()
    monkeypatch.setattr(cq, "_cold_start_session", lambda cfg: sentinel)
    stale = cq._PersistentSession(_FakeHandle(), _FakeHandle(), 333, {333})
    stale.in_use = True
    with cq._session_lock:
        cq._psession = stale
    try:
        got = cq._acquire_session({})
        assert got is sentinel, "使用中的 session 不得被第二個 worker 重用"
        assert terminated == [333], "孤兒 session 要被終結"
        with cq._session_lock:
            assert cq._psession is None or cq._psession is sentinel
    finally:
        with cq._session_lock:
            cq._psession = None


def test_idle_session_reuse_takes_the_lease(monkeypatch):
    """正常重用:取用即持有租約(in_use=True);成功一輪後歸還。"""
    monkeypatch.setattr(cq, "_session_is_alive", lambda s: True)
    monkeypatch.setattr(cq._keepalive, "session_needs_restart",
                        lambda a, b: False)
    sess = cq._PersistentSession(_FakeHandle(), _FakeHandle(), 444, {444})
    with cq._session_lock:
        cq._psession = sess
    try:
        got = cq._acquire_session({})
        assert got is sess and sess.in_use is True
        cq._session_release(sess)
        assert sess.in_use is False
    finally:
        with cq._session_lock:
            cq._psession = None


def test_success_path_releases_the_lease():
    import inspect
    src = inspect.getsource(cq._automation_on_hidden)
    assert src.count("_session_release(sess)") == 2,         "原始與恢復兩條成功路徑都要歸還租約"


def test_watch_is_singleton():
    """已在站崗就不重複開執行緒（多輪 BDE 失敗只會有一個看守）。"""
    started = []
    import threading as _th

    class _FakeThread:
        def __init__(self, *a, **k):
            started.append(k.get("name"))

        def start(self):
            pass

    orig = cq.threading.Thread
    cq.threading.Thread = _FakeThread
    try:
        with cq._bde_watch_lock:
            cq._bde_watch_active = False
        cq._schedule_bde_reboot_watch()
        cq._schedule_bde_reboot_watch()      # 第二次應該被 singleton 擋下
        assert started == ["BDERebootWatch"]
    finally:
        cq.threading.Thread = orig
        with cq._bde_watch_lock:
            cq._bde_watch_active = False
        cq._bde_reboot_cancel.set()
    assert isinstance(cq._bde_watch_lock, type(_th.Lock()))
