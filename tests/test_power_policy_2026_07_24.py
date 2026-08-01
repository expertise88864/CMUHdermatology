# -*- coding: utf-8 -*-
"""電源策略：主機不休眠；★螢幕逾時不由本程式決定★。

  1. _keep_system_awake_display_free：只設 SYSTEM|CONTINUOUS、絕不含 DISPLAY(0x2)。
  2. _apply_never_sleep_power_plan：powercfg 只把睡眠/休眠設 0。
  3. 啟動接線：主緒設 execution state、powercfg 丟背景。
  另附 R/VS 線別色籤高對比釘位（一線深紅/三線深藍、白字）。

★[2026-07-31 使用者定案] 自動關螢幕的兩層都已刪除★
原本有：(1) powercfg monitor-timeout 15 分鐘；(2) 閒置滿 15 分鐘的 watchdog
（廣播 SC_MONITORPOWER + 自動蓋黑幕）。使用者要求改成【設定頁的按鈕】手動觸發，
並明確定案兩層都刪、螢幕只由按鈕控制。

因此本檔移除了 8 支只測那兩層的測試（`_screen_off_due` 上膛週期、
`_send_monitor_off` 廣播與措辭、watchdog 的三支、啟動接線那支…）。
★不要因為「以前有」就把它們加回來★：那是使用者明確刪掉的功能。
黑幕本身的行為（含手動觸發的待命期）在 tests/test_screen_blackout_2026_07_30.py。
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
    main._apply_never_sleep_power_plan()
    joined = [" ".join(c) for c in runs]
    # ★[2026-07-31 使用者定案；2026-08-05 外審 P2 修正] 螢幕逾時要設 0★
    #   使用者要求刪除 15 分鐘進入螢幕關閉的模式，定案【兩層都刪、螢幕只由設定頁的
    #   按鈕控制】。第一版把指令拿掉就算數 —— 而這支測試當時也只斷言「沒有送
    #   monitor-timeout」，於是【綠燈，但使用者要刪的東西還在機器上】：
    #   `powercfg /change` 寫的是電源計畫，舊版寫下的 15 分鐘不會因為改版而消失。
    #   要真的做到「只由按鈕控制」，OS 的計時器就得是關的 → 設 0。
    assert "powercfg /change monitor-timeout-ac 0" in joined
    assert "powercfg /change monitor-timeout-dc 0" in joined
    assert not any("monitor-timeout" in j and not j.endswith(" 0")
                   for j in joined), \
        "只准設 0（永不自動關）；設成任何分鐘數都等於把 15 分鐘那層裝回來"
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
        main._apply_never_sleep_power_plan()
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
    assert "_apply_never_sleep_power_plan" in src, "powercfg 批次應丟背景執行"
    assert "ScreenBlackout(" in src, "黑幕須在【主緒】建立（Tk 不是 thread-safe）"
    assert "_subsystem_running" in src, \
        "busy_fn 要接本行程的自動化旗標，否則黑幕會讓 F2/F3 的螢幕擷取 OCR 擷到全黑"


def test_single_execution_state_call_site_without_display_bit():
    """main 全檔只有一處 SetThreadExecutionState 呼叫（本功能），且組出的旗標值
    無 DISPLAY bit（0x2）——防止日後有人另加 keep-display 呼叫讓螢幕又關不掉。"""
    text = open(main.__file__, encoding="utf-8").read()
    assert text.count("SetThreadExecutionState(") == 1
    assert (main._ES_CONTINUOUS | main._ES_SYSTEM_REQUIRED) & _ES_DISPLAY_REQUIRED == 0


def test_tick_delta_wraparound():
    """GetTickCount 32 位元約 49.7 天回繞 → 無號差值仍正確（不會算出負閒置）。"""
    assert main._tick_delta(5000, 1000) == 4000
    assert main._tick_delta(5, 0xFFFFFFFB) == 10


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


def test_the_automatic_screen_off_layers_are_really_gone():
    """★使用者定案：兩層都刪，螢幕只由設定頁的按鈕控制★

    刪除比新增更需要守衛 —— 「以前有、看起來合理」是最容易被順手加回來的東西。
    """
    import ast

    # ★用 AST，不要比對原始碼文字★ 這些名字會出現在說明「為什麼刪掉」的註解與
    # docstring 裡（本輪已經被自己的說明騙過一次）。這裡只看【真的會執行到的東西】：
    # 識別字，以及不是 docstring 的字串常數。
    tree = ast.parse(open(main.__file__, encoding="utf-8").read())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstrings.add(id(body[0].value))
    names, literals = set(), []
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            names.add(node.name)
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
              and id(node) not in docstrings):
            literals.append(node.value)

    for gone in ("_force_screen_off_watchdog", "_send_monitor_off",
                 "_screen_off_due", "SCREEN_OFF_MINUTES", "_SC_MONITORPOWER",
                 "_log_display_power_requests", "_request_blackout"):
        assert gone not in names, f"{gone} 已由使用者刪除，不可再出現"
    # ★[2026-08-05 外審 P2] 這裡原本斷言「main.py 不准出現 monitor-timeout 字樣」★
    #   那是把「不提它」當成「已經刪掉它」。實際上舊版寫進電源計畫的 15 分鐘會留在
    #   機器上，不送指令＝不會消失 —— 測試綠、而使用者要刪的東西還在。
    #   要釘的其實是「不存在任何『N 分鐘後自動關螢幕』」，所以改成:凡是
    #   monitor-timeout 的設定值都必須是 0（永不自動關）。
    pairs = [
        (elt.elts[0].value, elt.elts[1].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "cmds"
                for t in node.targets)
        for elt in getattr(node.value, "elts", [])
        if isinstance(elt, ast.Tuple) and len(elt.elts) == 2
        and all(isinstance(e, ast.Constant) for e in elt.elts)
    ]
    timeouts = [(k, v) for k, v in pairs if "monitor-timeout" in str(k)]
    assert timeouts, "找不到 monitor-timeout 設定 —— 舊版寫下的 15 分鐘就沒人清掉"
    for key, val in timeouts:
        assert str(val) == "0", \
            f"{key}={val} —— 只准 0（永不自動關）；任何分鐘數都等於把那層裝回來"
    assert not hasattr(main, "_force_screen_off_watchdog")


def test_the_blackout_is_reachable_from_the_settings_page():
    """黑幕現在唯一的入口是設定頁的按鈕 —— 按鈕掉了就等於這個功能沒了。"""
    src = inspect.getsource(main.AutomationApp._create_settings_tab)
    assert "self._blackout_now" in src, "設定頁要有「立即黑螢幕」按鈕"
    assert "立即黑螢幕" in src


def test_the_button_reports_when_the_blackout_did_not_appear():
    """★措辭鐵律★ `show()` 回的是【回讀結果】。沒黑成功一定要告訴使用者 ——
    否則按了按鈕、螢幕沒變黑、程式卻一聲不吭（就是舊版 SC_MONITORPOWER
    「log 說關了、實機沒關」查了兩次都查不出來的那個形狀）。"""
    src = inspect.getsource(main.AutomationApp._blackout_now)
    assert "沒有顯示出來" in src
    assert "_subsystem_running" in src, "自動化執行中要說明為什麼沒反應"


def test_hotkeys_are_gated_while_the_blackout_is_up():
    """醫師為了喚醒螢幕按的那一下可能就是 F1/F9 —— 不可在他還看不見畫面時
    就對 HIS 寫劑量/計費。F12（中止）刻意不走這個閘門：救援鍵不可被吃掉。"""
    src = inspect.getsource(main.AutomationApp.setup_hotkeys)
    assert "_blackout_gate" in src
    assert "callback = _blackout_gate(callback, key)" in src


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
