# -*- coding: utf-8 -*-
"""[第九輪 §5] watchdog 的時間基準:喚醒/時鐘跳動守衛 + 進展觀察模型。

舊的陳舊判定 `time.time() - st_mtime > max_stale` 有兩個牆上時鐘才會犯的錯:
  * 系統睡眠(診間電腦午休/隔夜):醒來時每支程式的 mtime 都停在睡前,age 一律 ≥ 睡眠
    時間;被監看程式的 heartbeat 醒來才補寫、watchdog 醒來卻立刻 tick —— 誰先醒是
    擲硬幣,watchdog 先醒就 ★kill 一支完全健康的程式★。
  * 時鐘往回:age 變小/負,卡死的程式看起來很新。

修法兩層缺一不可:(1) 每 tick 比較 Δ牆上時鐘 與 Δ醒著時間,差太多 → 本 tick 隔離
(第四態 LOG_CLOCK_JUMP,不動手)並重設進展基準;(2) 年齡改成「最後一次觀察到
mtime/size 變化距今★醒著★多久」。只有 (1) 的話隔離只保護第一個 tick,第二個 tick
又拿睡前的 mtime 算年齡 → 照樣 kill。

本檔用可控的假時鐘(牆上/醒著分開推進)★跑真的 `ensure_program()`★,而且每個
「不動手」都有一條「時鐘平穩時同樣設定會動手」的正向對照 —— 證明 fake 打得到
kill/start 路徑,「沒動手」是守衛擋的。
"""
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cmuh_common.single_instance as si  # noqa: E402
import cmuh_common.watchdog_core as wc  # noqa: E402
from cmuh_common.watchdog_core import (  # noqa: E402
    LOG_ABSENT, LOG_CLOCK_JUMP, LOG_OK, log_status,
)

MAX_STALE = 180
_REAL_AWAKE = wc._awake_now          # 在任何 fixture 動手之前抓住原始函式


# ─── 假時鐘:牆上時鐘與醒著時間分開推進 ─────────────────────────────────────
class _Clock:
    def __init__(self):
        self.wall = 1_700_000_000.0
        self.awake = 10_000.0

    def run(self, sec):
        """正常運轉:兩個時鐘一起走。"""
        self.wall += sec
        self.awake += sec

    def sleep(self, sec):
        """系統睡眠:牆上時鐘走、醒著時間不走。"""
        self.wall += sec

    def set_back(self, sec):
        """時鐘被往回調。"""
        self.wall -= sec


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    c = _Clock()
    wc._reset_clock_state()
    monkeypatch.setattr(wc, "_wall_now", lambda: c.wall)
    monkeypatch.setattr(wc, "_awake_now", lambda: c.awake)
    yield c
    wc._reset_clock_state()


def _log(path: Path, clock: _Clock, age_sec: float, text="x") -> Path:
    """建一個 log,mtime 設成「牆上時鐘 age_sec 秒前」。"""
    path.write_text(text, encoding="utf-8")
    t = clock.wall - age_sec
    os.utime(path, (t, t))
    return path


# ─── 1. 進展模型:年齡用醒著的時間累積 ───────────────────────────────────────
def test_age_counts_awake_time_not_wall_time(tmp_path, clock):
    """★核心★:睡 1 小時醒來 → 隔離一輪 → 下一輪年齡從 0 用醒著的時間算,
    不是「mtime 距牆上時鐘 3630 秒」。舊版第二輪就 kill。"""
    p = _log(tmp_path / "a.log", clock, age_sec=10)
    wc._note_tick()
    assert log_status(p, MAX_STALE)[0] is False

    clock.sleep(3600)
    wc._note_tick()                                    # 醒來第一輪:隔離
    assert log_status(p, MAX_STALE) == (False, 0.0, LOG_CLOCK_JUMP)

    clock.run(30)
    wc._note_tick()                                    # 第二輪:正常判定
    stale, age, state = log_status(p, MAX_STALE)
    assert state == LOG_OK
    assert stale is False, f"健康程式醒來 30 秒就被判陳舊(age={age})"
    assert abs(age - 30) < 1e-6


def test_a_change_in_the_log_resets_the_age(tmp_path, clock):
    p = _log(tmp_path / "a.log", clock, age_sec=100)
    wc._note_tick()
    assert log_status(p, MAX_STALE)[1] == pytest.approx(100)
    clock.run(50)
    _log(p, clock, age_sec=0, text="xy")               # 內容/size/mtime 都變
    assert log_status(p, MAX_STALE)[1] == 0.0


def test_a_truly_hung_program_is_still_caught_after_wake(tmp_path, clock):
    """抑制要有出口:隔離只有一輪。醒來後程式真的不再寫 log,
    max_stale 醒著的秒數之後照樣要抓到。"""
    p = _log(tmp_path / "a.log", clock, age_sec=10)
    wc._note_tick()
    clock.sleep(3600)
    wc._note_tick()                                    # 隔離
    log_status(p, MAX_STALE)                           # 生產上每一輪都會觀測每支程式
    clock.run(MAX_STALE + 1)
    wc._note_tick()
    stale, age, state = log_status(p, MAX_STALE)
    assert (stale, state) == (True, LOG_OK)
    assert age == pytest.approx(MAX_STALE + 1)


def test_first_observation_uses_the_wall_age_when_the_clock_is_settled(tmp_path, clock):
    """沒有歷史時牆上時鐘的 mtime 年齡是唯一的資訊 —— 剛啟動就發現卡死的程式
    要立刻抓到,不可以「第一次觀測一律算新」(那會讓沒狀態檔的 --once 永遠不重啟)。"""
    p = _log(tmp_path / "a.log", clock, age_sec=10_000)
    wc._note_tick()
    stale, age, state = log_status(p, MAX_STALE)
    assert (stale, state) == (True, LOG_OK) and age == pytest.approx(10_000)


def test_first_observation_under_quarantine_does_not_trust_the_wall_age(tmp_path, clock):
    """反例要落在規則的時間窗裡:★第一次觀測發生在隔離的那一輪★。
    若仍用牆上年齡當基準,隔離結束後的第一輪就拿灌水的年齡去 kill。"""
    wc._note_tick()
    clock.sleep(3600)
    wc._note_tick()                                    # 隔離
    p = _log(tmp_path / "a.log", clock, age_sec=3600)  # 睡前寫的,這一輪才第一次看到
    assert log_status(p, MAX_STALE) == (False, 0.0, LOG_CLOCK_JUMP)
    clock.run(30)
    wc._note_tick()
    stale, age, state = log_status(p, MAX_STALE)
    assert (stale, state) == (False, LOG_OK) and age == pytest.approx(30)


# ─── 2. 守衛:什麼算跳動、隔離幾輪 ───────────────────────────────────────────
def test_sleep_quarantines_exactly_one_tick(clock):
    wc._note_tick()
    clock.sleep(3600)
    assert wc._note_tick() == pytest.approx(3600)
    assert wc._CLOCK["quarantined"] is True
    clock.run(30)
    assert wc._note_tick() == pytest.approx(0)
    assert wc._CLOCK["quarantined"] is False


def test_a_clock_set_back_quarantines_too(clock):
    wc._note_tick()
    clock.run(30)
    clock.set_back(7200)
    assert wc._note_tick() == pytest.approx(-7200)
    assert wc._CLOCK["quarantined"] is True


def test_small_drift_does_not_quarantine(clock):
    wc._note_tick()
    clock.run(30)
    clock.wall += 5                                    # NTP 微調
    wc._note_tick()
    assert wc._CLOCK["quarantined"] is False


def test_the_first_tick_of_a_process_never_quarantines(clock):
    """沒有上一 tick、也沒有狀態檔 → 沒有東西可比 → 不隔離(退回牆上時鐘年齡)。"""
    assert wc._note_tick() == 0.0
    assert wc._CLOCK["quarantined"] is False


# ─── 3. 跨行程(--once 每 2 分鐘起一個新行程)───────────────────────────────
def test_a_new_process_detects_the_sleep_from_the_state_file(tmp_path, clock):
    """`--once` 沒有行程內的上一 tick:要靠落盤的 (wall, awake) 與各 log 基準。"""
    p = _log(tmp_path / "a.log", clock, age_sec=10)
    wc._note_tick()
    log_status(p, MAX_STALE)
    wc._flush_clock_state()                            # tick 結束:基準落盤(run_one_tick 會做)
    assert Path(wc._clock_state_path()).exists()

    wc._reset_clock_state()                            # 模擬新行程
    clock.sleep(3600)
    wc._note_tick()
    assert wc._CLOCK["quarantined"] is True, "新行程要從狀態檔看出睡過"
    assert log_status(p, MAX_STALE)[2] == LOG_CLOCK_JUMP
    wc._flush_clock_state()

    wc._reset_clock_state()                            # 再下一個 --once
    clock.run(30)
    wc._note_tick()
    stale, age, state = log_status(p, MAX_STALE)
    assert (stale, state) == (False, LOG_OK)
    assert age == pytest.approx(30), "基準重設要跨行程生效,不然這一輪就 kill"


def test_a_reboot_resets_the_baselines_loaded_from_the_file(tmp_path, clock):
    """重開機後「醒著的時間」從 0 重數,狀態檔裡的基準卻是上一次開機的單位。
    上次開機沒多久就寫下的基準(seen_awake 很小)配上這次已經醒很久的 watchdog
    → 年齡憑空變大 → kill。跳動守衛會在第一輪抓到(Δawake 為負),而★基準重設★
    是讓下一輪不 kill 的那一步 —— 沒有它,隔離只保護一輪。"""
    p = _log(tmp_path / "a.log", clock, age_sec=10)
    clock.awake = 100.0                                # 上次開機:才醒 100 秒
    wc._note_tick()
    log_status(p, MAX_STALE)                           # 基準 seen_awake ≈ 90
    wc._flush_clock_state()                            # 落盤
    wc._reset_clock_state()                            # 重開機 + 新行程
    clock.run(60)
    clock.awake = 5000.0                               # 這次開機已醒 5000 秒
    wc._note_tick()
    assert wc._CLOCK["quarantined"] is True
    clock.run(30)
    wc._note_tick()
    stale, age, state = log_status(p, MAX_STALE)
    assert (stale, state) == (False, LOG_OK)
    assert age == pytest.approx(30), f"基準沒重設,年齡跨了開機界線:{age}"


def test_a_corrupt_state_file_means_no_history(tmp_path, clock):
    Path(wc._clock_state_path()).parent.mkdir(parents=True, exist_ok=True)
    Path(wc._clock_state_path()).write_text("{not json", encoding="utf-8")
    clock.sleep(3600)
    assert wc._note_tick() == 0.0                      # 沒歷史 → 不隔離、不炸
    assert wc._CLOCK["quarantined"] is False


def test_an_unreadable_state_file_means_no_history(clock, monkeypatch):
    """`safe_load_json` 自己會把壞 JSON 吞成預設值,所以上一條量不到「讀取拋例外」
    那條路;這裡讓讀取真的拋(磁碟/權限)—— 守衛不可以因此讓整個 tick 炸掉,
    要退成「沒有歷史」。"""
    def _boom(*a, **k):
        raise OSError("disk unplugged")
    monkeypatch.setattr(wc, "safe_load_json", _boom)
    clock.sleep(3600)
    assert wc._note_tick() == 0.0
    assert wc._CLOCK["quarantined"] is False


def test_the_state_file_lives_in_the_isolated_settings_dir(tmp_path):
    assert str(Path(wc._clock_state_path()).resolve()).startswith(
        str(tmp_path.resolve())), "狀態檔跑到真的 settings/ 去了"


def test_the_state_file_keeps_the_log_baselines(tmp_path, clock):
    p = _log(tmp_path / "a.log", clock, age_sec=10)
    wc._note_tick()
    log_status(p, MAX_STALE)
    wc._flush_clock_state()                            # tick 結束
    data = json.loads(Path(wc._clock_state_path()).read_text(encoding="utf-8"))
    assert data["wall"] == clock.wall and data["awake"] == clock.awake
    key = os.path.normcase(os.path.abspath(str(p)))
    assert key in data["logs"] and set(data["logs"][key]) == {"mtime", "size", "seen_awake"}


# ─── 4. 整合:真的走 ensure_program() ────────────────────────────────────────
def _prog(tmp_path, mutex=""):
    (tmp_path / "x.pyw").write_text("", encoding="utf-8")
    return {"name": "打卡", "pyw": str(tmp_path / "x.pyw"),
            "log_path": "a.log", "max_stale_sec": MAX_STALE,
            "process_match": "x", "pid_name": "autoclock", "mutex_name": mutex}


class _Actions:
    def __init__(self):
        self.killed, self.started = [], []


def _run(monkeypatch, tmp_path, *, pids, mutex_held=False, mutex=""):
    acts = _Actions()
    monkeypatch.setattr(wc, "find_matching_pids", lambda *a, **k: list(pids))
    monkeypatch.setattr(wc, "_wmic_find_pids", lambda *a, **k: [])
    monkeypatch.setattr(wc, "_find_pids_holding_mutex", lambda *a, **k: [7])
    monkeypatch.setattr(wc, "start_program",
                        lambda *a, **k: acts.started.append(a) or 4321)
    monkeypatch.setattr(wc, "kill_pids_verified",
                        lambda p, *a, **k: acts.killed.extend(p) or list(p))
    monkeypatch.setattr(wc, "claim_action_lock", lambda *a, **k: True)
    monkeypatch.setattr(wc, "_record_restart_and_check_crash_loop", lambda *a, **k: True)
    monkeypatch.setattr(wc.time, "sleep", lambda *_: None)
    monkeypatch.setattr(si, "is_instance_running", lambda _n: mutex_held)
    monkeypatch.setattr(wc, "_ROOT", tmp_path)
    msg = wc.ensure_program(_prog(tmp_path, mutex), "pythonw.exe", [],
                            my_pid=1, mode="outer", cfg={})
    return msg, acts


def _wake_up(tmp_path, clock):
    """睡前 log 是新的;睡 1 小時醒來,watchdog 先醒(程式還沒補 heartbeat)。

    ★刻意不在睡前觀測這個 log★:有了睡前基準,進展模型光靠「醒著的時間沒走」就
    不會判陳舊 —— 那樣守衛有沒有作用根本分不出勝負。最危險、也是守衛真正 load-bearing
    的一格是「第一次觀測就發生在醒來那一輪」(watchdog 醒來才第一次看這支程式、
    或 --once 沒有這個 log 的基準),此時牆上時鐘的 mtime 年齡是灌了水的。"""
    _log(tmp_path / "a.log", clock, age_sec=10)
    wc._note_tick()
    clock.sleep(3600)
    wc._note_tick()


def test_case2_does_not_kill_a_healthy_program_right_after_wake(tmp_path, monkeypatch, clock):
    _wake_up(tmp_path, clock)
    msg, acts = _run(monkeypatch, tmp_path, pids=[1])
    assert acts.killed == [], f"醒來第一輪就殺了健康程式:{msg}"
    assert "時鐘" in msg or "喚醒" in msg
    # 隔離結束後的第一輪也不可以拿灌水的年齡去 kill(基準要從醒來那一刻起算)
    clock.run(30)
    wc._note_tick()
    msg2, acts2 = _run(monkeypatch, tmp_path, pids=[1])
    assert acts2.killed == [] and msg2.startswith("✓"), msg2


def test_case2_still_kills_when_the_clock_is_settled(tmp_path, monkeypatch, clock):
    """正向對照:同樣的 log 年齡、時鐘平穩(真的過了 1 小時沒寫)→ 要 kill。
    證明上一條的「沒殺」是守衛擋的,不是 fake 根本打不到 kill。"""
    _log(tmp_path / "a.log", clock, age_sec=10)
    wc._note_tick()
    clock.run(3600)
    wc._note_tick()
    msg, acts = _run(monkeypatch, tmp_path, pids=[1])
    assert acts.killed == [1] and msg.startswith("⟳")


def test_the_mutex_branch_does_not_kill_after_wake(tmp_path, monkeypatch, clock):
    _wake_up(tmp_path, clock)
    msg, acts = _run(monkeypatch, tmp_path, pids=[], mutex_held=True, mutex="Local\\x")
    assert acts.killed == [] and acts.started == []
    assert "時鐘" in msg or "喚醒" in msg


def test_the_mutex_branch_still_kills_when_the_clock_is_settled(tmp_path, monkeypatch, clock):
    _log(tmp_path / "a.log", clock, age_sec=10)
    wc._note_tick()
    clock.run(3600)
    wc._note_tick()
    msg, acts = _run(monkeypatch, tmp_path, pids=[], mutex_held=True, mutex="Local\\x")
    assert acts.killed == [7] and msg.startswith("⟳")


def test_fallback2_does_not_start_a_duplicate_after_wake(tmp_path, monkeypatch, clock):
    """找不到 PID、沒 mutex、但 log 存在:平常「log 很舊 → 啟動」;剛醒來時
    「很舊」不可信 → 本輪不啟動。"""
    _wake_up(tmp_path, clock)
    msg, acts = _run(monkeypatch, tmp_path, pids=[], mutex_held=False)
    assert acts.started == [] and "不啟動" in msg


def test_fallback2_still_starts_when_the_clock_is_settled(tmp_path, monkeypatch, clock):
    _log(tmp_path / "a.log", clock, age_sec=10)
    wc._note_tick()
    clock.run(3600)
    wc._note_tick()
    msg, acts = _run(monkeypatch, tmp_path, pids=[], mutex_held=False)
    assert len(acts.started) == 1 and msg.startswith("▶")


def test_fallback2_trusts_a_fresh_log_when_the_clock_is_settled(tmp_path, monkeypatch, clock):
    _log(tmp_path / "a.log", clock, age_sec=10)
    wc._note_tick()
    msg, acts = _run(monkeypatch, tmp_path, pids=[], mutex_held=False)
    assert acts.started == [] and msg.startswith("✓")


def test_the_non_ok_states_are_told_apart(tmp_path, monkeypatch, clock, caplog):
    """「不存在」「讀不到」「時鐘剛跳動」處置不同,訊息不可以壓成一格。"""
    import logging
    # 不存在:走既有的 warning
    monkeypatch.setattr(wc, "find_matching_pids", lambda *a, **k: [1])
    monkeypatch.setattr(wc, "_wmic_find_pids", lambda *a, **k: [])
    monkeypatch.setattr(wc, "_ROOT", tmp_path)
    wc._note_tick()
    with caplog.at_level(logging.WARNING):
        msg_absent = wc.ensure_program(_prog(tmp_path), "pythonw.exe", [],
                                       my_pid=1, mode="outer", cfg={})
    assert "不存在" in msg_absent
    assert log_status(tmp_path / "a.log", MAX_STALE)[2] == LOG_ABSENT
    # 時鐘剛跳動:⏭ 訊息,不是「讀不到」
    _wake_up(tmp_path, clock)
    msg_jump, _ = _run(monkeypatch, tmp_path, pids=[1])
    assert msg_jump.startswith("⏭") and "讀不到" not in msg_jump


# ─── 5. 接線 ────────────────────────────────────────────────────────────────
def test_run_one_tick_notes_the_clock_before_any_early_return(monkeypatch):
    """設定檔讀不到的那一輪也要記時鐘,否則下一輪的「上一 tick」是更早的。"""
    seen = []
    monkeypatch.setattr(wc, "_note_tick", lambda: seen.append(True) or 0.0)
    monkeypatch.setattr(wc, "config_load_failed", lambda: True)
    wc.run_one_tick("outer")
    assert seen == [True]


def test_run_one_tick_flushes_the_baselines_even_on_an_early_return(monkeypatch):
    """`--once` 跑完就結束:tick 結束不 flush,本輪新建的基準就永遠丟了,新行程
    每次都是「第一次觀測」→ 喚醒守衛在 --once 上等於沒裝。early-return 那些輪也要。"""
    flushed = []
    monkeypatch.setattr(wc, "_flush_clock_state", lambda: flushed.append(True))
    monkeypatch.setattr(wc, "config_load_failed", lambda: True)
    wc.run_one_tick("outer")
    assert flushed == [True]


def test_the_baselines_made_during_a_tick_survive_into_the_next_process(tmp_path, clock, monkeypatch):
    """★真的走 run_one_tick★:本輪 ensure_program 建的基準,下一個行程要拿得到。
    (上一條只證明 flush 被呼叫;這一條證明 flush 存的是【本輪】的基準。)"""
    p = _log(tmp_path / "a.log", clock, age_sec=10)
    monkeypatch.setattr(wc, "config_load_failed", lambda: False)
    monkeypatch.setattr(wc, "load_config", lambda: {"master_enabled": True, "programs": []})
    monkeypatch.setattr(wc, "find_pythonw", lambda: "pythonw.exe")
    monkeypatch.setattr(wc, "list_python_processes", lambda: [])
    wc.run_one_tick("outer")                           # tick 開頭存的是空基準
    log_status(p, MAX_STALE)                           # 模擬本輪內 ensure_program 觀測
    wc.run_one_tick("outer")                           # 這一輪結束要把它 flush 出去
    wc._reset_clock_state()                            # 新行程
    clock.run(30)
    wc._note_tick()
    key = os.path.normcase(os.path.abspath(str(p)))
    assert key in wc._CLOCK["logs"], "新行程沒拿到上一輪的基準"


def test_the_awake_clock_is_a_positive_number_and_falls_back_to_monotonic(monkeypatch):
    """`_REAL_AWAKE` 是在任何 fixture 動手之前抓下來的原始函式。"""
    v = _REAL_AWAKE()
    assert isinstance(v, float) and v > 0
    monkeypatch.setattr(os, "name", "posix")           # 非 Windows → 退回 monotonic
    w = _REAL_AWAKE()
    assert abs(w - time.monotonic()) < 1.0, "非 Windows 要退回 monotonic"
