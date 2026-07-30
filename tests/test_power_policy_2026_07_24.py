# -*- coding: utf-8 -*-
"""2026-07-24 使用者：主程式開著時螢幕關不掉 → 電源策略寫進程式。

  目標：螢幕 15 分鐘後照常關閉、但主機不休眠。
  1. _keep_system_awake_display_free：只設 SYSTEM|CONTINUOUS、絕不含 DISPLAY(0x2)。
  2. _apply_screen_off_power_plan：powercfg 螢幕 15 分/睡眠休眠 0/忽略 chrome·python
     的 DISPLAY keep-awake（status driver Chrome 的 wake-lock 是螢幕關不掉主因）。
  3. 啟動接線：主緒設 execution state、powercfg 丟背景。
  另附 R/VS 線別色籤高對比釘位（一線深紅/三線深藍、白字）。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402
from cmuh_common.roster.ui.common import LINE_CHIP  # noqa: E402

_ES_DISPLAY_REQUIRED = 0x00000002


def test_execution_state_keeps_system_not_display(monkeypatch):
    called = []
    monkeypatch.setattr(main.ctypes.windll.kernel32, "SetThreadExecutionState",
                        lambda flags: called.append(flags) or 1, raising=False)
    main._keep_system_awake_display_free()
    assert called, "應呼叫 SetThreadExecutionState"
    flags = called[0]
    assert flags == (main._ES_CONTINUOUS | main._ES_SYSTEM_REQUIRED)
    assert not (flags & _ES_DISPLAY_REQUIRED), "絕不可設 DISPLAY(否則螢幕永遠不關)"


class _CP:
    def __init__(self, rc=0):
        self.returncode = rc
        self.stdout = b""
        self.stderr = b""


def test_power_plan_commands(monkeypatch):
    runs = []
    monkeypatch.setattr(main.subprocess, "run",
                        lambda cmd, **k: (runs.append(list(cmd)), _CP())[1])
    main._apply_screen_off_power_plan()
    joined = [" ".join(c) for c in runs]
    # 螢幕 15 分關（AC/DC）
    assert "powercfg /change monitor-timeout-ac 15" in joined
    assert "powercfg /change monitor-timeout-dc 15" in joined
    # 主機永不睡/不休眠
    assert "powercfg /change standby-timeout-ac 0" in joined
    assert "powercfg /change standby-timeout-dc 0" in joined
    assert "powercfg /change hibernate-timeout-ac 0" in joined
    # [codex P1] 不得動用全機 requestsoverride（永久生效且波及使用者自己的
    # Chrome/python 正當 keep-awake）——wake-lock 只在自家 driver 的
    # chrome options 關（見 test_own_chrome_disables_wakelock）。
    assert not any("requestsoverride" in j for j in joined)


def test_own_chrome_disables_wakelock():
    """自家 status driver 的 Chrome 關 Wake Lock API（只影響我們自己的實例）。
    [codex P1] navigator.wakeLock 是 Blink runtime feature → 必須用
    --disable-blink-features=WakeLock 才真正關；disable-features 為保險並列。"""
    from cmuh_common import chrome_options
    assert "WakeLock" in chrome_options._DISABLED_FEATURES.split(",")
    import inspect as _ins
    src = _ins.getsource(chrome_options.build_chrome_options)
    assert "--disable-blink-features=WakeLock" in src


def test_power_plan_reports_partial_failure(monkeypatch, caplog):
    """[codex P2] powercfg 失敗（rc≠0）→ 記 warning 點名失敗鍵，不得誤報全套成功。"""
    import logging as _lg
    monkeypatch.setattr(main.subprocess, "run",
                        lambda cmd, **k: _CP(rc=1 if "standby-timeout-ac"
                                             in cmd else 0))
    with caplog.at_level(_lg.WARNING):
        main._apply_screen_off_power_plan()
    assert any("部分未生效" in r.message and "standby-timeout-ac" in r.message
               for r in caplog.records)


def test_execution_state_rejected_logs_warning(monkeypatch, caplog):
    """[codex P2] SetThreadExecutionState 回 0（被拒）→ warning，不得誤報成功。"""
    import logging as _lg
    monkeypatch.setattr(main.ctypes.windll.kernel32, "SetThreadExecutionState",
                        lambda flags: 0, raising=False)
    with caplog.at_level(_lg.WARNING):
        main._keep_system_awake_display_free()
    assert any("被拒" in r.message for r in caplog.records)


def test_startup_wiring_main_thread_state_bg_powercfg():
    src = inspect.getsource(main.AutomationApp.start_background_tasks)
    assert "_keep_system_awake_display_free()" in src, \
        "execution state 應在主緒設定(ES_CONTINUOUS 綁呼叫緒壽命)"
    assert "_apply_screen_off_power_plan" in src, "powercfg 批次應丟背景執行"
    assert "ScreenBlackout(" in src, "黑幕須在【主緒】建立（Tk 不是 thread-safe）"
    assert "_subsystem_running" in src, \
        "busy_fn 要接本行程的自動化旗標，否則黑幕會讓 F2/F3 的螢幕擷取 OCR 擷到全黑"


def test_single_execution_state_call_site_without_display_bit():
    """main 全檔只有一處 SetThreadExecutionState 呼叫（本功能），且組出的旗標值
    無 DISPLAY bit（0x2）——防止日後有人另加 keep-display 呼叫讓螢幕又關不掉。"""
    text = open(main.__file__, encoding="utf-8").read()
    assert text.count("SetThreadExecutionState(") == 1
    assert (main._ES_CONTINUOUS | main._ES_SYSTEM_REQUIRED) & _ES_DISPLAY_REQUIRED == 0


def test_screen_off_due_arming_cycle():
    """[2026-07-24 使用者] 強制關屏 watchdog：閒置到點且上膛才送、送一次即 disarm
    （不重複轟炸→不閃爍）、一有輸入重新上膛。"""
    limit = main.SCREEN_OFF_MINUTES * 60
    assert main._screen_off_due(limit, True) == (True, False)
    assert main._screen_off_due(limit + 5, False) == (False, False)
    assert main._screen_off_due(3, False) == (False, True)
    assert main._screen_off_due(limit - 1, True) == (False, True)


def test_tick_delta_wraparound():
    """GetTickCount 32 位元約 49.7 天回繞 → 無號差值仍正確（不會算出負閒置）。"""
    assert main._tick_delta(5000, 1000) == 4000
    assert main._tick_delta(5, 0xFFFFFFFB) == 10


def test_send_monitor_off_broadcast_with_timeout():
    """HWND_BROADCAST 必須用 SendMessageTimeout(ABORTIFHUNG)——卡死視窗不得
    永久阻塞 watchdog 緒；常數釘位（SC_MONITORPOWER/關閉）。"""
    src = inspect.getsource(main._send_monitor_off)
    assert "SendMessageTimeoutW" in src
    assert "_SMTO_ABORTIFHUNG" in src
    assert main._SC_MONITORPOWER == 0xF170 and main._MONITOR_OFF == 2


def test_idle_seconds_failure_is_unknown_not_zero(monkeypatch):
    """★[2026-07-30] 這條原本斷言「失敗回 0.0」—— 那個斷言本身就是 bug。★

    回 0.0 的方向是對的（寧可不關，絕不誤關），但它讓「`GetLastInputInfo` 一直
    失敗」跟「剛剛真的有人碰過鍵盤」長得一模一樣：螢幕永遠不會關，而 log 一行都
    沒有 —— 使用者回報「15 分鐘後螢幕還是不關」查了兩次都查不出來。
    現在回 None＝【查不到】，由呼叫端決定怎麼辦並且要留下 log。
    """
    monkeypatch.setattr(main.ctypes.windll.user32, "GetLastInputInfo",
                        lambda *_a: 0, raising=False)
    assert main._idle_seconds() is None


def test_unknown_idle_neither_closes_the_screen_nor_disarms(monkeypatch):
    """查不到閒置時間 → 這輪不動作（絕不誤關），但【維持 armed】：
    等查得到的那一輪仍然要能關。若順手 disarm，一次查詢失敗就會把整段閒置期的
    關屏機會吃掉。"""
    assert main._screen_off_due(None, True) == (False, True)
    assert main._screen_off_due(None, False) == (False, False)


def test_send_monitor_off_does_not_claim_the_screen_closed():
    """★措辭鐵律★ SC_MONITORPOWER 送出後，只要系統上還有 DISPLAY power request，
    Windows 會立刻把螢幕點回來，而我們【查不到】螢幕現在是開還是關。
    log 只能說「已送出」——舊版說「已強制關閉螢幕」，實機沒關卻一直這樣寫。"""
    src = inspect.getsource(main._send_monitor_off)
    assert "已送出關閉螢幕指令" in src
    assert "已強制關閉螢幕" not in src


def test_the_watchdog_logs_when_it_cannot_tell_whether_anyone_is_idle():
    """查不到閒置時間時必須留下 warning —— 否則這個功能整台機器失效而無跡可循
    （正是這次查不出原因的直接理由）。"""
    src = inspect.getsource(main._force_screen_off_watchdog)
    assert "unknown_streak" in src
    assert "查不到使用者閒置時間" in src


def test_the_watchdog_logs_display_power_requests_before_blanking():
    """★這是「螢幕關不掉」查了兩次都缺的那份資料★
    螢幕該關卻沒關最常見的原因是某支程式掛著 DISPLAY wake lock，而那是【間歇性】
    的 —— 必須在它真的發生的那一刻記下 `powercfg /requests`。"""
    src = inspect.getsource(main._force_screen_off_watchdog)
    assert "_log_display_power_requests" in src
    assert "/requests" in inspect.getsource(main._log_display_power_requests)


def test_the_watchdog_also_raises_the_blackout():
    """SC_MONITORPOWER 無法回讀 → 使用者定案改用可回讀的全黑畫面兜底。"""
    src = inspect.getsource(main._force_screen_off_watchdog)
    assert "_request_blackout" in src


def test_the_blackout_gate_never_raises_and_defaults_to_not_blacked_out(
        monkeypatch):
    """★這條守的是所有 F1-F12★ 閘門若因例外卡在 True，所有熱鍵會從此失效。"""
    monkeypatch.setattr(main, "_screen_blackout", None, raising=False)
    assert main.screen_blackout_should_eat_this_hotkey() is False

    class _Broken:
        @property
        def active(self):
            raise RuntimeError("炸了")

    monkeypatch.setattr(main, "_screen_blackout", _Broken(), raising=False)
    assert main.screen_blackout_should_eat_this_hotkey() is False


def test_hotkeys_are_gated_while_the_blackout_is_up():
    """醫師為了喚醒螢幕按的那一下可能就是 F1/F9 —— 不可在他還看不見畫面時
    就對 HIS 寫劑量/計費。F12（中止）刻意不走這個閘門：救援鍵不可被吃掉。"""
    src = inspect.getsource(main.AutomationApp.setup_hotkeys)
    assert "_blackout_gate" in src
    assert "callback = _blackout_gate(callback, key)" in src


def test_force_off_watchdog_wired_in_startup():
    src = inspect.getsource(main.AutomationApp.start_background_tasks)
    assert "_force_screen_off_watchdog" in src, \
        "強制關屏 watchdog 應在啟動背景任務中開緒"


def test_line_chip_high_contrast():
    """[2026-07-24 使用者] 一線/三線色籤高對比：深紅 vs 深藍、白字，不再相近。"""
    r_bg, r_fg, r_lab = LINE_CHIP["r"]
    v_bg, v_fg, v_lab = LINE_CHIP["vs"]
    assert (r_lab, v_lab) == ("一線", "三線")
    assert r_fg == v_fg == "#FFFFFF"              # 深底白字
    assert r_bg != v_bg
    # 紅/藍分道：R 紅色分量壓過藍、VS 藍色分量壓過紅（數值上明確分離）
    rr, rb = int(r_bg[1:3], 16), int(r_bg[5:7], 16)
    vr, vb = int(v_bg[1:3], 16), int(v_bg[5:7], 16)
    assert rr - rb > 80 and vb - vr > 80, f"對比不足: r={r_bg}, vs={v_bg}"
