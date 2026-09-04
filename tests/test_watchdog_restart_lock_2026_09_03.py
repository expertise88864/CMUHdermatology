# -*- coding: utf-8 -*-
"""[第九輪 §6] 啟動歷史鎖逾時 → 本 tick 不授權重啟,不做不持鎖的讀改寫。

舊版 `_restart_history_lock()` 逾時 `break` 後不持鎖繼續 load → modify → save。
daemon 與 `--once` 撞上時:A 讀、B 讀、A 加 X、B 加 Y、A 存、B 存 → X 掉了 →
crash-loop 計數被低估 → 保護失效。少重啟一輪的代價很低(下一 tick 再問),
所以逾時改成「UNKNOWN,本輪不動手」。

★但「鎖檔根本建不出來」(目錄不可寫等持續狀況)仍 fail-open★:那不會自己解除,
fail-closed 會讓 watchdog 永遠不重啟任何程式;改成據實警告一次。

本檔★真的走 ensure_program()★驗三個動手的地方,每個「不動手」都有「鎖空著時
同樣設定會動手」的正向對照。
"""
import ast
import errno
import inspect
import logging
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cmuh_common.single_instance as si  # noqa: E402
import cmuh_common.watchdog_core as wc  # noqa: E402
from cmuh_common.watchdog_core import (  # noqa: E402
    RESTART_AUTH_CRASH_LOOP, RESTART_AUTH_LOCK_BUSY, RESTART_AUTH_OK,
    _authorize_restart,
)


_REAL_SIDECAR_PATHS = wc._unsaved_sidecar_paths   # 在 fixture 動手前抓住生產的路徑函式


@pytest.fixture(autouse=True)
def _fast_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(wc, "_restart_history_path",
                        lambda: str(tmp_path / "h.json"))
    monkeypatch.setattr(wc, "RESTART_HISTORY_LOCK_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr(wc, "suspend_auto_updates", lambda *a, **k: "")
    # 「第一次寫失敗」的時間戳 sidecar:兩個候選位置都導向本測試的 tmp
    # (生產的第二候選是 %TEMP%,不導向會跨測試/跨 session 互相看到)。
    monkeypatch.setattr(
        wc, "_unsaved_sidecar_paths",
        lambda name: [str(tmp_path / f"unsaved_{name.encode('utf-8').hex()}.json"),
                      str(tmp_path / f"unsaved_fb_{name.encode('utf-8').hex()}.json")])
    # 假的牆上時鐘★從真實現在起算★:log 的 mtime 是用真 time.time() 設的,假時鐘若從
    # 固定值起算,年齡會變成負數 → 陳舊的 log 看起來很新 → 動手的測試全部量錯規則。
    clock = {"t": time.time()}
    monkeypatch.setattr(wc, "_wall_now", lambda: clock["t"])
    wc._RESTART_HISTORY.clear()
    wc._SUSPENDED_UNTIL.clear()
    wc._LOCK_DEGRADED_WARNED[0] = False
    wc._reset_clock_state()
    yield clock
    wc._RESTART_HISTORY.clear()
    wc._SUSPENDED_UNTIL.clear()
    wc._reset_clock_state()


def _hold_lock(age_sec: float = 0.0) -> Path:
    """模擬另一個 watchdog 正持有鎖(鎖檔存在)。age_sec>10 就是持鎖者崩潰留下的殘骸。"""
    lock = Path(wc._restart_history_path() + ".lock")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text("", encoding="utf-8")
    if age_sec:
        t = time.time() - age_sec
        os.utime(lock, (t, t))
    return lock


# ─── 1. 授權判定本身 ────────────────────────────────────────────────────────
def test_a_busy_lock_means_no_authorization_and_no_unlocked_write(tmp_path):
    hist = Path(wc._restart_history_path())
    hist.write_text('{"history": {"x": [1.0]}, "suspended_until": {}}', encoding="utf-8")
    before = hist.read_text(encoding="utf-8")
    _hold_lock()

    assert _authorize_restart("x") == RESTART_AUTH_LOCK_BUSY
    assert hist.read_text(encoding="utf-8") == before, "鎖忙時不可以做不持鎖的讀改寫"
    assert wc._RESTART_HISTORY.get("x", []) == [], "記憶體也不該記下這次啟動"


def test_a_free_lock_authorizes_and_records():
    assert _authorize_restart("x") == RESTART_AUTH_OK
    assert len(wc._RESTART_HISTORY["x"]) == 1
    assert not Path(wc._restart_history_path() + ".lock").exists(), "離開要刪鎖檔"


def test_a_stale_lock_is_taken_over():
    """持鎖者崩潰留下的鎖檔(>10s)不可以把 watchdog 永遠擋住。"""
    _hold_lock(age_sec=20)
    assert _authorize_restart("x") == RESTART_AUTH_OK


def test_busy_is_not_the_same_as_crash_loop(monkeypatch):
    """兩者處置不同:鎖忙=下一輪再問;crash loop=暫停一段時間。"""
    _hold_lock()
    assert _authorize_restart("x") == RESTART_AUTH_LOCK_BUSY
    monkeypatch.setattr(wc, "_record_restart_and_check_crash_loop", lambda _n: False)
    assert _authorize_restart("x") == RESTART_AUTH_CRASH_LOOP


def test_the_raw_recorder_raises_when_busy():
    """直接呼叫舊函式時,鎖忙要用例外講出來,不可以再靜默 fail-open。"""
    _hold_lock()
    with pytest.raises(wc._RestartHistoryLockBusy):
        wc._record_restart_and_check_crash_loop("x")


def test_an_uncreatable_lock_file_fails_open_but_warns_once(monkeypatch, caplog):
    """鎖檔建不出來是★持續★狀況(目錄不可寫),不會自己解除 → 不能 fail-closed,
    否則 watchdog 永遠不重啟任何程式;但要據實警告(一次)。"""
    real_open = os.open

    def _denied(path, *a, **k):
        if str(path).endswith(".lock"):
            raise PermissionError("settings/ read-only")
        return real_open(path, *a, **k)
    monkeypatch.setattr(os, "open", _denied)

    with caplog.at_level(logging.WARNING):
        assert _authorize_restart("x") == RESTART_AUTH_OK
        assert _authorize_restart("x") == RESTART_AUTH_OK
    warns = [r for r in caplog.records if "建不出來" in r.getMessage()]
    assert len(warns) == 1, "要警告,而且只警告一次(不洗版)"


# ─── 2. 三個動手的地方,真的走 ensure_program ─────────────────────────────────
def _prog(tmp_path, mutex=""):
    (tmp_path / "x.pyw").write_text("", encoding="utf-8")
    log = tmp_path / "a.log"
    log.write_text("x", encoding="utf-8")
    t = time.time() - 10_000                          # 陳舊
    os.utime(log, (t, t))
    return {"name": "打卡", "pyw": str(tmp_path / "x.pyw"),
            "log_path": "a.log", "max_stale_sec": 180,
            "process_match": "x", "pid_name": "autoclock", "mutex_name": mutex}


class _Acts:
    def __init__(self):
        self.killed, self.started = [], []


def _run(monkeypatch, tmp_path, *, pids, mutex_held=False, mutex=""):
    acts = _Acts()
    monkeypatch.setattr(wc, "find_matching_pids", lambda *a, **k: list(pids))
    monkeypatch.setattr(wc, "_wmic_find_pids", lambda *a, **k: [])
    monkeypatch.setattr(wc, "_find_pids_holding_mutex", lambda *a, **k: [7])
    monkeypatch.setattr(wc, "start_program",
                        lambda *a, **k: acts.started.append(a) or 4321)
    monkeypatch.setattr(wc, "kill_pids_verified",
                        lambda p, *a, **k: acts.killed.extend(p) or list(p))
    monkeypatch.setattr(wc, "claim_action_lock", lambda *a, **k: True)
    monkeypatch.setattr(wc.time, "sleep", lambda *_: None)
    monkeypatch.setattr(si, "is_instance_running", lambda _n: mutex_held)
    monkeypatch.setattr(wc, "_ROOT", tmp_path)
    wc._note_tick()
    msg = wc.ensure_program(_prog(tmp_path, mutex), "pythonw.exe", [],
                            my_pid=1, mode="outer", cfg={})
    return msg, acts


def test_stale_log_does_not_kill_while_the_lock_is_busy(tmp_path, monkeypatch):
    _hold_lock()
    msg, acts = _run(monkeypatch, tmp_path, pids=[1])
    assert acts.killed == [] and msg.startswith("⏭") and "鎖忙" in msg


def test_stale_log_still_kills_when_the_lock_is_free(tmp_path, monkeypatch):
    """正向對照:同樣設定、鎖空著 → 要 kill(證明 fake 打得到 kill)。"""
    msg, acts = _run(monkeypatch, tmp_path, pids=[1])
    assert acts.killed == [1] and msg.startswith("⟳")


def test_half_dead_does_not_kill_while_the_lock_is_busy(tmp_path, monkeypatch):
    _hold_lock()
    msg, acts = _run(monkeypatch, tmp_path, pids=[], mutex_held=True, mutex="Local\\x")
    assert acts.killed == [] and msg.startswith("⏭") and "鎖忙" in msg


def test_half_dead_still_kills_when_the_lock_is_free(tmp_path, monkeypatch):
    msg, acts = _run(monkeypatch, tmp_path, pids=[], mutex_held=True, mutex="Local\\x")
    assert acts.killed == [7] and msg.startswith("⟳")


def test_not_running_does_not_start_while_the_lock_is_busy(tmp_path, monkeypatch):
    _hold_lock()
    msg, acts = _run(monkeypatch, tmp_path, pids=[], mutex_held=False)
    assert acts.started == [] and msg.startswith("⏭") and "鎖忙" in msg


def test_not_running_still_starts_when_the_lock_is_free(tmp_path, monkeypatch):
    msg, acts = _run(monkeypatch, tmp_path, pids=[], mutex_held=False)
    assert len(acts.started) == 1 and msg.startswith("▶")


def test_crash_loop_message_is_still_told_apart_from_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(wc, "_record_restart_and_check_crash_loop", lambda _n: False)
    msg, acts = _run(monkeypatch, tmp_path, pids=[1])
    assert acts.killed == [] and msg.startswith("⛔") and "鎖忙" not in msg


# ─── 3. 接線:三個地方都改問 _authorize_restart ────────────────────────────────
def test_every_action_site_asks_the_authorizer():
    tree = ast.parse(inspect.getsource(wc.ensure_program))
    names = [c.func.id for c in ast.walk(tree)
             if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)]
    assert names.count("_authorize_restart") == 3, names
    assert "_record_restart_and_check_crash_loop" not in names, \
        "有動手的地方繞過了授權判定(鎖忙時會拿到例外或不持鎖讀改寫)"


# ─── 4. 外審 r1 P2-1:不動手就不可以燒掉 90 秒的動作鎖 ────────────────────────
def _run_real_action_lock(monkeypatch, tmp_path, *, pids, prog=None):
    """同 _run,但★不 stub claim_action_lock★:動作鎖用真的檔案,才量得到
    「這一輪沒動手、下一輪卻被自己留下的鎖擋住」。
    `prog` 可傳入以跨兩個 tick 沿用同一份 log(重建 log 會讓 mtime 變動 → 進展模型判成
    「有進展」→ 第二輪看起來很新,量到的就不是動作鎖那條規則)。"""
    acts = _Acts()
    prog = prog or _prog(tmp_path)
    monkeypatch.setattr(wc, "LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(wc, "find_matching_pids", lambda *a, **k: list(pids))
    monkeypatch.setattr(wc, "_wmic_find_pids", lambda *a, **k: [])
    monkeypatch.setattr(wc, "start_program",
                        lambda *a, **k: acts.started.append(a) or 4321)
    monkeypatch.setattr(wc, "kill_pids_verified",
                        lambda p, *a, **k: acts.killed.extend(p) or list(p))
    monkeypatch.setattr(wc.time, "sleep", lambda *_: None)
    monkeypatch.setattr(si, "is_instance_running", lambda _n: False)
    monkeypatch.setattr(wc, "_ROOT", tmp_path)
    wc._note_tick()
    msg = wc.ensure_program(prog, "pythonw.exe", [],
                            my_pid=1, mode="outer", cfg={"action_lock_seconds": 90})
    return msg, acts


def test_a_busy_history_lock_does_not_burn_the_action_lock(tmp_path, monkeypatch):
    """★兩個 tick 連跑★:第一輪鎖忙不動手 → 動作鎖必須被撤回;第二輪鎖空 → 要動手。
    舊版第一輪留下 90s 動作鎖,第二輪被「lock 還新」擋掉,「下輪再判」變成兩輪後。"""
    prog = _prog(tmp_path)                              # 兩輪沿用同一份陳舊 log
    hold = _hold_lock()
    msg1, acts1 = _run_real_action_lock(monkeypatch, tmp_path, pids=[1], prog=prog)
    assert acts1.killed == [] and msg1.startswith("⏭") and "鎖忙" in msg1
    assert not wc._lock_path_for("打卡").exists(), "沒動手卻留下了 90 秒動作鎖"

    hold.unlink()                                       # 另一個 watchdog 寫完了
    msg2, acts2 = _run_real_action_lock(monkeypatch, tmp_path, pids=[1], prog=prog)
    assert acts2.killed == [1] and msg2.startswith("⟳"), f"下一輪應該動手:{msg2}"


def test_an_actual_action_keeps_the_action_lock(tmp_path, monkeypatch):
    """反例要只靠這條規則分勝負:真的動手那一輪,動作鎖★要留著★(B/C 節流仍成立)。"""
    msg, acts = _run_real_action_lock(monkeypatch, tmp_path, pids=[1])
    assert acts.killed == [1] and msg.startswith("⟳")
    assert wc._lock_path_for("打卡").exists()


def test_release_only_removes_our_own_action_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(wc, "LOCK_DIR", tmp_path / "locks")
    lock = wc._lock_path_for("打卡")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_bytes(b"424242 1700000000")              # 別的 watchdog 剛動過手
    assert wc.release_action_lock("打卡") is False and lock.exists()
    lock.write_bytes(f"{os.getpid()} 1700000000".encode())
    assert wc.release_action_lock("打卡") is True and not lock.exists()
    assert wc.release_action_lock("打卡") is False        # 沒鎖檔:冪等


# ─── 5. 外審 r1 P2-2:歷史寫不下去 ≠ 授權成功 ─────────────────────────────────
def test_a_failed_history_save_does_not_authorize(monkeypatch):
    # 只讓歷史檔寫失敗(不認得的 EIO → 不是持續型、走時間窗口);sidecar 要能寫,
    # 否則量到的是「連時間戳都沒地方寫 → 立即降級」那條規則。
    _history_write_fails(monkeypatch, lambda: OSError(errno.EIO, "io"))
    assert _authorize_restart("x") == wc.RESTART_AUTH_HISTORY_UNSAVED
    assert wc._RESTART_HISTORY.get("x", []) == [], "沒記下的啟動,記憶體也要撤回"


def _history_write_fails(monkeypatch, exc_factory, *, sidecars_too=False):
    """讓★歷史檔★的 atomic_write_json 拋例外;sidecar(時間戳)照常能寫,除非 sidecars_too。"""
    real = wc.atomic_write_json
    state = {"fail": True}

    def _maybe(path, data, **k):
        if state["fail"] and (sidecars_too or str(path).endswith("h.json")):
            raise exc_factory()
        return real(path, data, **k)
    monkeypatch.setattr(wc, "atomic_write_json", _maybe)
    return state


def test_persistent_save_failure_degrades_after_the_window_with_a_warning(monkeypatch, caplog, _fast_lock):
    """寫不進去是持續狀況時,fail-closed 會讓 watchdog 永遠不重啟任何程式 →
    第一次失敗起算、過了 HISTORY_UNSAVED_DEGRADE_AFTER_SEC 就降級授權(WARNING 明講);
    寫成功就清掉時間戳。★用時間不用行程內計數★:--once 每輪都是新行程。"""
    clock = _fast_lock
    state = _history_write_fails(monkeypatch, lambda: OSError(errno.EIO, "flaky disk"))
    # ★反例的規模不可以從被測常數推出來★:時間推進用固定秒數,並釘住常數本身
    # (常數若改了,是這條斷言先紅,而不是突變永遠不紅)。
    assert wc.HISTORY_UNSAVED_DEGRADE_AFTER_SEC == 180.0
    with caplog.at_level(logging.WARNING):
        assert _authorize_restart("x") == wc.RESTART_AUTH_HISTORY_UNSAVED   # 記下第一次失敗
        clock["t"] += 179
        assert _authorize_restart("x") == wc.RESTART_AUTH_HISTORY_UNSAVED   # 窗口內仍不動手
        clock["t"] += 2                                                      # 181s
        assert _authorize_restart("x") == RESTART_AUTH_OK, "過了窗口要降級授權(抑制要有出口)"
    assert any("降級" in r.getMessage() for r in caplog.records)
    state["fail"] = False                                # 磁碟恢復
    assert _authorize_restart("x") == RESTART_AUTH_OK
    assert wc._unsaved_since_get("x") is None, "寫成功要清掉時間戳"


def test_the_degrade_window_survives_a_new_once_process(monkeypatch, _fast_lock):
    """★外審 r2 P2-1★:`--once` 每兩分鐘都是全新行程。第一次失敗的時間戳必須落盤,
    新行程才走得到出口;行程內計數永遠到不了門檻。"""
    clock = _fast_lock
    _history_write_fails(monkeypatch, lambda: OSError(errno.EIO, "flaky disk"))
    assert _authorize_restart("x") == wc.RESTART_AUTH_HISTORY_UNSAVED
    wc._RESTART_HISTORY.clear()                          # 新行程:記憶體歸零
    clock["t"] += 181                                    # 固定秒數,不從常數推
    assert _authorize_restart("x") == RESTART_AUTH_OK, "新行程從 sidecar 看不到第一次失敗的時間"


def test_a_persistent_error_type_degrades_immediately(monkeypatch, caplog):
    """權限/路徑/磁碟滿是可確認的持續狀況:不必等窗口,第一次就降級(WARNING)。"""
    _history_write_fails(monkeypatch, lambda: PermissionError("ACL"))
    with caplog.at_level(logging.WARNING):
        assert _authorize_restart("x") == RESTART_AUTH_OK
    assert any("持續狀況" in r.getMessage() for r in caplog.records)


def test_when_even_the_timestamp_cannot_be_written_it_degrades_immediately(monkeypatch, caplog):
    """settings/ 與 %TEMP% 都寫不進去 = 機器狀態已壞;沒有地方記時間戳就不能靠窗口,
    立即降級,不可以永遠不動手。"""
    _history_write_fails(monkeypatch, lambda: OSError(errno.EIO, "io"), sidecars_too=True)
    with caplog.at_level(logging.WARNING):
        assert _authorize_restart("x") == RESTART_AUTH_OK
    assert any("沒地方寫" in r.getMessage() for r in caplog.records)


def test_two_programs_timestamps_never_clobber_each_other(monkeypatch):
    """★外審 r3 P2★:daemon 與 --once 同時為不同程式記/清時間戳。共用一個 JSON 的
    讀改寫會互相蓋掉(丟掉時間戳=窗口重設、把舊時間戳帶回=下次暫時性失敗立即降級)。
    每個程式一個檔 → 任何交錯順序下,各自的值都等於自己最後一次操作。
    ★用生產的路徑函式★(fixture 的假路徑會繞過「每個程式一個檔」這條規則本身);
    settings/ 由 conftest 導向 tmp,所以不會寫到真的目錄。"""
    monkeypatch.setattr(wc, "_unsaved_sidecar_paths", _REAL_SIDECAR_PATHS)
    # 模擬兩個行程交錯:A 先「讀」(什麼都沒讀到),B 寫入,A 再寫入 —— 共用檔的 RMW 會讓 B 消失
    assert wc._unsaved_since_get("a") is None
    assert wc._unsaved_since_set("b", 200.0, 0) is True
    assert wc._unsaved_since_set("a", 100.0, 0) is True
    assert wc._unsaved_since_get("a") == 100.0
    assert wc._unsaved_since_get("b") == 200.0, "A 的寫入把 B 的時間戳蓋掉了"
    # 反向:A 清除自己,不可以動到 B;B 清除後 A 也不受影響
    wc._unsaved_since_clear("a")
    assert wc._unsaved_since_get("a") is None and wc._unsaved_since_get("b") == 200.0
    wc._unsaved_since_clear("b")
    assert wc._unsaved_since_get("b") is None
    wc._unsaved_since_clear("b")                         # 冪等


# ─── 7. 外審 r4 P2:清不掉的舊時間戳不可以被沿用 ────────────────────────────
def _remove_fails(monkeypatch, times=None):
    """讓 sidecar 的 os.remove 拋暫時性錯誤:times=None 永遠失敗;否則失敗 times 次後成功。"""
    real_remove = os.remove
    seen = {"n": 0}

    def _flaky(path, *a, **k):
        if "unsaved_" in str(path):
            seen["n"] += 1
            if times is None or seen["n"] <= times:
                raise OSError(errno.EACCES, "sharing violation", None, 32)
        return real_remove(path, *a, **k)
    monkeypatch.setattr(os, "remove", _flaky)
    return seen


def test_a_transient_remove_error_is_retried_and_the_file_really_goes(monkeypatch, tmp_path):
    wc._unsaved_since_set("x", 100.0, 0)
    seen = _remove_fails(monkeypatch, times=1)
    wc._unsaved_since_clear("x")
    assert seen["n"] >= 2, "第一次失敗要重試"
    assert not any(p.name.startswith("unsaved_") for p in tmp_path.iterdir()), \
        "重試成功就該真的刪掉,不是留標記"
    assert wc._unsaved_since_get("x") is None


def test_a_persistently_failing_remove_still_clears_via_a_marker(monkeypatch, tmp_path):
    """刪不掉就寫「已清除」標記(寫入通常比刪除容易成功):讀取端要看成沒有時間戳。"""
    wc._unsaved_since_set("x", 100.0, 0)
    _remove_fails(monkeypatch, times=None)
    wc._unsaved_since_clear("x")
    assert any(p.name.startswith("unsaved_") for p in tmp_path.iterdir()), "前提:檔還在"
    assert wc._unsaved_since_get("x") is None, "殘留檔裡的舊時間戳被沿用了"


def test_a_residual_timestamp_from_an_older_generation_is_not_reused(monkeypatch):
    """★兜底那一層★:就算清除與標記都失敗、舊時間戳原封不動留著,只要歷史檔在它之後
    成功落盤過(世代序號前進了),讀取端就要把它當殘留 —— 否則第一次暫時性寫失敗就拿
    舊窗口立即降級成一筆未記錄的授權。"""
    assert _authorize_restart("x") == RESTART_AUTH_OK          # 成功落盤 → 世代 1
    assert wc._HISTORY_GENERATION[0] == 1
    stale = time.time() - 1000                                  # 早已超過窗口的殘留,記的是世代 0
    assert wc._unsaved_since_set("x", stale, 0) is True
    _history_write_fails(monkeypatch, lambda: OSError(errno.EIO, "io"))
    assert _authorize_restart("x") == wc.RESTART_AUTH_HISTORY_UNSAVED, \
        "殘留的舊時間戳被沿用 → 立即降級成未記錄的授權"
    assert wc._unsaved_generation_get("x") == 1, "重新起算時要記下現在的世代"


def test_the_degrade_exit_is_still_reachable_after_the_clock_is_set_back(monkeypatch, _fast_lock):
    """★外審 r5 P2★:世代若用歷史檔 mtime(牆上時鐘),時鐘回撥後 mtime「在未來」,
    每一輪都把有效的失敗起點當殘骸重設,180 秒出口永遠到不了 → --once 長時間不救援。
    用檔案內容裡的序號就與時鐘無關:回撥之後照樣 181 秒抵達出口。"""
    clock = _fast_lock
    assert _authorize_restart("x") == RESTART_AUTH_OK          # 成功落盤(mtime = 真實現在)
    clock["t"] -= 3600                                          # 系統時鐘被往回校正一小時
    _history_write_fails(monkeypatch, lambda: OSError(errno.EIO, "io"))
    assert _authorize_restart("x") == wc.RESTART_AUTH_HISTORY_UNSAVED   # 記下起點(回撥後的時間)
    clock["t"] += 179
    assert _authorize_restart("x") == wc.RESTART_AUTH_HISTORY_UNSAVED   # 窗口內
    clock["t"] += 2
    assert _authorize_restart("x") == RESTART_AUTH_OK, "時鐘回撥後出口永遠到不了(mtime 世代的回歸)"


def test_history_unsaved_skips_this_tick_and_releases_the_action_lock(tmp_path, monkeypatch):
    _history_write_fails(monkeypatch, lambda: OSError(errno.EIO, "io"))   # 只壞歷史檔
    msg, acts = _run_real_action_lock(monkeypatch, tmp_path, pids=[1])
    assert acts.killed == [] and msg.startswith("⏭") and "寫入失敗" in msg
    assert not wc._lock_path_for("打卡").exists()


# ─── 6. 外審 r1 P2-3:暫時性 OSError 要重試,不是立刻 fail-open ──────────────
def test_a_transient_lock_open_error_is_retried_not_failed_open(monkeypatch, caplog):
    real_open = os.open
    seen = {"n": 0}

    def _flaky(path, *a, **k):
        if str(path).endswith(".lock"):
            seen["n"] += 1
            if seen["n"] == 1:
                raise OSError(32, "sharing violation")    # 防毒短暫攔住
        return real_open(path, *a, **k)
    monkeypatch.setattr(os, "open", _flaky)

    with caplog.at_level(logging.WARNING):
        assert _authorize_restart("x") == RESTART_AUTH_OK
    assert seen["n"] >= 2, "第一次失敗要重試,不是放棄"
    assert wc._LOCK_DEGRADED_WARNED[0] is False, "暫時性錯誤不可以被當成持續狀況 fail-open"
    assert not any("建不出來" in r.getMessage() for r in caplog.records)
    assert not Path(wc._restart_history_path() + ".lock").exists()


def _lock_open_always_raises(monkeypatch, exc_factory):
    real_open = os.open

    def _denied(path, *a, **k):
        if str(path).endswith(".lock"):
            raise exc_factory()
        return real_open(path, *a, **k)
    monkeypatch.setattr(os, "open", _denied)


def test_a_sharing_violation_that_persists_past_the_deadline_is_busy_not_fail_open(monkeypatch, caplog):
    """★外審 r2 P2-2★:sharing/lock violation 是暫時性的;整段 timeout 都拿不到 → 當「忙」
    (本輪不動手),不可以退到不持鎖的讀改寫(那正是 lost update 的來源)。"""
    _lock_open_always_raises(monkeypatch,
                             lambda: OSError(errno.EACCES, "sharing violation", None, 32))
    with caplog.at_level(logging.WARNING):
        assert _authorize_restart("x") == RESTART_AUTH_LOCK_BUSY
    assert wc._LOCK_DEGRADED_WARNED[0] is False
    assert not any("建不出來" in r.getMessage() for r in caplog.records)


def test_an_unknown_os_error_that_persists_is_treated_as_busy(monkeypatch):
    """不認得的錯誤碼 → 當暫時性(少動手一輪的代價低於把持續狀況猜錯)。"""
    _lock_open_always_raises(monkeypatch, lambda: OSError(errno.EIO, "io error"))
    assert _authorize_restart("x") == RESTART_AUTH_LOCK_BUSY


def test_a_missing_lock_directory_fails_open_with_a_warning(monkeypatch, caplog):
    """路徑不存在是可確認的持續狀況 → fail-open + WARNING(否則 watchdog 永遠不重啟)。"""
    _lock_open_always_raises(monkeypatch, lambda: FileNotFoundError("no such dir"))
    with caplog.at_level(logging.WARNING):
        assert _authorize_restart("x") == RESTART_AUTH_OK
    assert any("建不出來" in r.getMessage() for r in caplog.records)
