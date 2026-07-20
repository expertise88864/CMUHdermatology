# -*- coding: utf-8 -*-
"""打卡狀態跨夜修復(2026-07-15 使用者回報「放跨夜仍顯示不出打卡狀態」)。

07-14 已修殘留 alert,但鏈上仍有三個跨夜洞:
  1. 失敗無重試——排程一天只有 08:00/17:03,任一次暫時性失敗＝灰燈掛到下個排程
     (跨夜情境＝整個上午看不到打卡狀態)。→ 失敗後 3 分鐘自動重試,每波上限 5 次。
  2. 壞 driver 永久卡死——pool 健康檢查(window_handles)驗不出 renderer 死亡
     ("tab crashed"),查詢失敗後不丟棄 → 每輪拿到同一個壞 driver;重試又一直刷新
     last_used,連 idle 淘汰都不觸發。→ 查詢例外時 _discard_status_driver。
  3. 早上 07:31 自動打卡後、08:00 前無查詢。→ 加 07:40 排程。
"""
import inspect
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402


# ── 修正 2:_discard_status_driver 丟棄池中 driver ────────────────────────────
import threading as _threading  # noqa: E402
import time as _time  # noqa: E402


class _FakeDriver:
    """quit 為非同步(daemon 緒)執行 → 用 Event 等待,不可立即斷言。"""

    def __init__(self):
        self.quit_event = _threading.Event()

    def quit(self):
        self.quit_event.set()


def test_discard_status_driver_empties_pool_and_quits():
    pool = main._status_driver_pool
    with pool["lock"]:
        saved = pool["driver"]
    fake = _FakeDriver()
    try:
        with pool["lock"]:
            pool["driver"] = fake
        main._discard_status_driver(fake)     # 池中正是失敗的那份 → 清池+quit
        with pool["lock"]:
            assert pool["driver"] is None, "丟棄後池應為空(下一輪重建全新 Chrome)"
        assert fake.quit_event.wait(2), "被丟棄的 driver 應被 quit(非同步)"
    finally:
        with pool["lock"]:
            pool["driver"] = saved


def test_discard_keeps_pool_when_driver_already_replaced():
    # [codex] 180s worker-age 保險允許新舊查詢重疊:舊查詢「晚到的失敗」不可把
    # 新查詢正在用的【新 driver】清掉/quit 掉——只善後失敗的那份自己。
    pool = main._status_driver_pool
    with pool["lock"]:
        saved = pool["driver"]
    old_failed = _FakeDriver()
    new_active = _FakeDriver()
    try:
        with pool["lock"]:
            pool["driver"] = new_active        # 池中已被新一輪換成新 driver
        main._discard_status_driver(old_failed)
        with pool["lock"]:
            assert pool["driver"] is new_active, "池中的新 driver 不可被清掉"
        assert old_failed.quit_event.wait(2), "失敗的舊 driver 應被善後 quit"
        assert not new_active.quit_event.is_set(), "新 driver 不可被 quit(使用中)"
    finally:
        with pool["lock"]:
            pool["driver"] = saved


def test_discard_returns_immediately_even_if_quit_hangs():
    # [codex P1] chromedriver 卡死時 quit 可能永不返回;discard 必須立即返回
    # (quit 丟 daemon 緒),否則查詢的 error 回不去、worker 旗標/重試全卡死。
    release = _threading.Event()
    quit_started = _threading.Event()

    class _HangingDriver:
        def quit(self):
            quit_started.set()
            release.wait(5)                    # 模擬卡住(上限 5s 防測試自身卡死)

    pool = main._status_driver_pool
    with pool["lock"]:
        saved = pool["driver"]
    d = _HangingDriver()
    try:
        with pool["lock"]:
            pool["driver"] = d
        t0 = _time.monotonic()
        main._discard_status_driver(d)
        elapsed = _time.monotonic() - t0
        assert elapsed < 1.0, f"quit 卡住時 discard 仍應立即返回(實測 {elapsed:.2f}s)"
        with pool["lock"]:
            assert pool["driver"] is None      # 池已清,不受卡住的 quit 影響
        assert quit_started.wait(2), "quit 應已在背景緒啟動"
    finally:
        release.set()                          # 放行 daemon 緒收尾
        with pool["lock"]:
            pool["driver"] = saved


def test_discard_kills_process_tree_when_quit_hangs_forever(monkeypatch):
    # [codex P2] quit 永久卡死不可累積 Chrome 行程樹:寬限逾時後須依該 driver 專屬的
    # chromedriver PID 砍樹(用真實 dummy 行程驗證;縮短寬限讓測試快)。
    import subprocess
    monkeypatch.setattr(main, "_STATUS_DISCARD_QUIT_GRACE_SEC", 1)
    dummy = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    class _HangForever:
        def __init__(self, proc):
            self.service = types.SimpleNamespace(
                process=types.SimpleNamespace(pid=proc.pid))

        def quit(self):
            _threading.Event().wait(30)        # 永久卡死(daemon 緒,測試結束即消滅)

    pool = main._status_driver_pool
    with pool["lock"]:
        saved = pool["driver"]
    d = _HangForever(dummy)
    try:
        with pool["lock"]:
            pool["driver"] = d
        main._discard_status_driver(d)
        try:
            dummy.wait(timeout=10)             # 寬限 1s 後應被砍 → wait 返回
        except subprocess.TimeoutExpired as exc:
            raise AssertionError("quit 永久卡死時,寬限逾時應強制結束該 driver 行程樹") from exc
        assert dummy.returncode is not None
    finally:
        if dummy.poll() is None:
            dummy.kill()
        with pool["lock"]:
            pool["driver"] = saved


def test_discard_tolerates_empty_pool_and_broken_quit():
    pool = main._status_driver_pool
    with pool["lock"]:
        saved = pool["driver"]
    try:
        with pool["lock"]:
            pool["driver"] = None
        main._discard_status_driver()          # 空池不拋

        boom_called = _threading.Event()

        class _Boom:
            def quit(self):
                boom_called.set()
                raise RuntimeError("chromedriver dead")
        with pool["lock"]:
            pool["driver"] = _Boom()
        main._discard_status_driver()          # quit 拋例外由背景緒吞掉,不影響呼叫端
        with pool["lock"]:
            assert pool["driver"] is None
        assert boom_called.wait(2)
    finally:
        with pool["lock"]:
            pool["driver"] = saved


def test_swipe_check_discards_driver_on_generic_failure():
    src = inspect.getsource(main._get_swipe_status_from_web)
    tail = src[src.index("except Exception as e:"):]
    assert "_discard_status_driver(driver)" in tail, \
        "查詢例外應丟棄【本輪失敗的】driver(帶身分比對,勿無條件清池)"


# ── 修正 1:失敗自動重試(3 分鐘、每波上限 5 次、成功歸零) ─────────────────────
class _FakeRoot:
    def __init__(self):
        self.scheduled = []
        self.cancelled = []

    def after(self, ms, fn):
        self.scheduled.append((ms, fn))
        return f"after#{len(self.scheduled)}"

    def after_cancel(self, aid):
        self.cancelled.append(aid)


def _fake_app():
    return types.SimpleNamespace(
        root=_FakeRoot(),
        _clock_status_retry_count=0,
        _clock_status_retry_after_id=None,
        _CLOCK_RETRY_DELAY_MS=main.AutomationApp._CLOCK_RETRY_DELAY_MS,
        _CLOCK_RETRY_MAX=main.AutomationApp._CLOCK_RETRY_MAX,
        _run_clock_status_retry=lambda: None,
        _maybe_retry_clock_status=None, _cancel_clock_status_retry=None)


def _bound_retry(app):
    app._cancel_clock_status_retry = types.MethodType(
        main.AutomationApp._cancel_clock_status_retry, app)
    return types.MethodType(main.AutomationApp._maybe_retry_clock_status, app)


def test_retry_scheduled_on_failure_and_capped():
    app = _fake_app()
    retry = _bound_retry(app)
    for i in range(1, 6):                       # 連續 5 次 transient 失敗 → 每次都排重試
        retry("查詢失敗", main.CLOCK_ERR_TRANSIENT)
        assert app._clock_status_retry_count == i
        assert len(app.root.scheduled) == i
        assert app.root.scheduled[-1][0] == 3 * 60 * 1000   # 3 分鐘
    retry("查詢失敗", main.CLOCK_ERR_TRANSIENT)   # 第 6 次 → 超過上限,不再排
    assert len(app.root.scheduled) == 5, "連續失敗達上限後應停止重試"


def test_retry_skips_deliberate_disable():
    app = _fake_app()
    retry = _bound_retry(app)
    retry("院外模式停用", main.CLOCK_ERR_DISABLED)   # 刻意停用 → 不重試
    assert app.root.scheduled == []
    assert app._clock_status_retry_count == 0


def test_retry_skips_auth_error_to_avoid_lockout():
    # [GPT-5.6 P1] 帳密錯(auth)絕不自動重試 —— 否則每波 5 次反覆送出會鎖帳號。
    app = _fake_app()
    retry = _bound_retry(app)
    retry("密碼/帳號錯誤", main.CLOCK_ERR_AUTH)
    assert app.root.scheduled == [], "auth 錯誤不得排自動重試(防鎖帳號)"
    assert app._clock_status_retry_count == 0


def test_auth_cancels_already_armed_transient_retry():
    # [GPT-5.6 P1 pass1] 先前 transient 失敗已排 3 分鐘重試,隨後某輪報 auth →
    # 必須【取消】那顆 pending 重試,否則它照樣帶已知錯帳密重登(多送一次鎖帳號嘗試)。
    app = _fake_app()
    retry = _bound_retry(app)
    retry("查詢失敗", main.CLOCK_ERR_TRANSIENT)          # 先排一顆
    assert app._clock_status_retry_after_id is not None
    armed = app._clock_status_retry_after_id
    retry("密碼/帳號錯誤", main.CLOCK_ERR_AUTH)           # auth 進來
    assert armed in app.root.cancelled, "auth 應取消先前已排的 transient 重試"
    assert app._clock_status_retry_after_id is None


def test_retry_unknown_kind_defaults_transient():
    # 相容:未帶 kind → 視為 transient(仍自動重試)。
    app = _fake_app()
    retry = _bound_retry(app)
    retry("查詢失敗")                            # 不傳 kind
    assert len(app.root.scheduled) == 1


def test_retry_cancels_previous_before_rescheduling():
    app = _fake_app()
    retry = _bound_retry(app)
    retry("查詢失敗", main.CLOCK_ERR_TRANSIENT)
    retry("查詢失敗", main.CLOCK_ERR_TRANSIENT)
    assert "after#1" in app.root.cancelled, "重排前應取消前一顆 after(不堆疊)"


def test_ui_error_branch_wires_retry_and_success_resets():
    src = inspect.getsource(main.AutomationApp._update_clock_status_ui)
    err_branch = src[src.index('"error" in status_data'):src.index("Invalid status_data")]
    assert "_maybe_retry_clock_status" in err_branch, "錯誤分支應觸發自動重試"
    tail = src[src.index("Invalid status_data"):]
    assert "_clock_status_retry_count = 0" in tail, "查詢成功應歸零連續失敗計數"
    assert "_cancel_clock_status_retry()" in tail, "查詢成功應取消 pending 重試"


def test_new_wave_resets_budget_but_retry_does_not():
    src = inspect.getsource(main.AutomationApp.update_clock_status_from_web)
    assert "from_retry" in src
    assert "if not from_retry:" in src and "_clock_status_retry_count = 0" in src, \
        "排程/跨日/手動觸發應重置重試預算;重試自身不重置(否則上限失效)"
    run_src = inspect.getsource(main.AutomationApp._run_clock_status_retry)
    assert "from_retry=True" in run_src


# ── 修正 3:07:40 排程(打卡程式 07:31 打完,放跨夜也能及早看到綠燈) ────────────
def test_morning_0740_schedule_added():
    src = inspect.getsource(main.AutomationApp.start_background_tasks)
    assert '"07:40"' in src and "clock-status-0740" in src, \
        "應有 07:40 打卡狀態查詢排程(跨夜情境早上及早顯示)"
    assert '"08:00"' in src and '"17:03"' in src   # 既有排程保留
