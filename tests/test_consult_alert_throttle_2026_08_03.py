# -*- coding: utf-8 -*-
"""會診連續失敗告警的節流狀態要【落地】（2026-08-03 使用者回報「今天一直寄信」）。

【問題】
`_job_fail_streak` / `_job_fail_last_alert` 只活在記憶體。可是這支程式本來就會
被 watchdog 重啟（watchdog_config.json 的「會診查詢」：log 停更 180 秒就重啟）——
每重啟一次冷卻時間就歸零，再累積 3 次失敗又寄一封。而信裡寫的是

    「（恢復正常後不會再寄；同一波故障最多 6 小時提醒一次。）」

★宣稱與實作不符★，使用者實際收到的是一整天的重複告警。

【修法】把 streak 與最後告警時間原子寫到 settings/consult_alert_state.json，
啟動時讀回來。★讀不到不可以當成「剛剛才寄過」★ —— 告警存在的理由就是故障時
沒人會發現，用一個壞掉的節流檔把它靜音，是拿小問題換大問題。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402


def _fresh_process(monkeypatch, tmp_path):
    """模擬「程式重新啟動」：記憶體歸零，只剩磁碟上的狀態。"""
    monkeypatch.setattr(cq, "_job_fail_streak", 0, raising=False)
    monkeypatch.setattr(cq, "_job_fail_last_alert", 0.0, raising=False)
    cq._load_job_fail_state()


def _use_tmp_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(cq, "_job_fail_state_path",
                        lambda: str(tmp_path / "consult_alert_state.json"))


def _sent_subjects(monkeypatch):
    """攔截寄信；回傳一個會被填入 subject 的 list。

    告警是丟到背景 thread 寄的，所以攔在 thread 建構這一層，直接同步執行 —— 否則
    斷言可能在信還沒寄出去時就跑完（★測試不可以靠時序碰運氣★）。
    """
    sent = []

    class _Immediate:
        def __init__(self, target=None, name=None, daemon=None):
            self._target = target

        def start(self):
            self._target()

    monkeypatch.setattr(cq.threading, "Thread", _Immediate)

    import cmuh_common.smtp_mail as smtp

    def _send_mail(recipients, subject, body, **kwargs):
        sent.append(subject)
    monkeypatch.setattr(smtp, "send_mail", _send_mail)
    return sent


def _fail(monkeypatch, times):
    for _ in range(times):
        cq._note_job_failure(["dev@example.com"], "登入沒有完成")


def test_the_cooldown_survives_a_restart(monkeypatch, tmp_path):
    """★這就是「今天一直寄信」的機制★ 重啟不可以讓冷卻歸零。"""
    _use_tmp_settings(monkeypatch, tmp_path)
    sent = _sent_subjects(monkeypatch)
    _fresh_process(monkeypatch, tmp_path)

    _fail(monkeypatch, 3)
    assert len(sent) == 1, "前提：達門檻要寄一封"

    _fresh_process(monkeypatch, tmp_path)      # ← watchdog 重啟
    _fail(monkeypatch, 5)

    assert len(sent) == 1, (
        f"重啟後冷卻歸零、又寄了 {len(sent) - 1} 封（信裡卻寫「6 小時一次」）")


def test_the_streak_survives_a_restart(monkeypatch, tmp_path):
    """重啟後計數要接得下去，否則信上的「連續失敗 N 次」是錯的。"""
    _use_tmp_settings(monkeypatch, tmp_path)
    _sent_subjects(monkeypatch)
    _fresh_process(monkeypatch, tmp_path)

    _fail(monkeypatch, 2)                      # 還沒到門檻
    _fresh_process(monkeypatch, tmp_path)

    assert cq._job_fail_streak == 2, (
        f"重啟後計數歸零了（{cq._job_fail_streak}）→ 每次重啟都要重新數")


def test_success_clears_the_persisted_state(monkeypatch, tmp_path):
    """★恢復了就要寫掉★ 否則下次重啟又把舊 streak 讀回來，一次失敗就跨門檻。"""
    _use_tmp_settings(monkeypatch, tmp_path)
    _sent_subjects(monkeypatch)
    _fresh_process(monkeypatch, tmp_path)

    _fail(monkeypatch, 2)
    cq._note_job_success()
    _fresh_process(monkeypatch, tmp_path)

    assert cq._job_fail_streak == 0, "恢復正常之後不可以還記著舊的連續失敗"


def test_an_unreadable_state_file_does_not_silence_the_alert(monkeypatch,
                                                             tmp_path):
    """★讀不到 ≠ 剛剛才寄過★ 壞掉的節流檔不可以把告警靜音。"""
    _use_tmp_settings(monkeypatch, tmp_path)
    (tmp_path / "consult_alert_state.json").write_text("{壞掉的 JSON",
                                                       encoding="utf-8")
    sent = _sent_subjects(monkeypatch)
    _fresh_process(monkeypatch, tmp_path)

    _fail(monkeypatch, 3)

    assert len(sent) == 1, "節流檔壞掉就不告警了 —— 故障會變成一片安靜"


def test_a_future_timestamp_does_not_silence_the_alert_forever(monkeypatch,
                                                               tmp_path):
    """時鐘往前跳過的機器，存下來的時間戳會在未來 → 不可以永久靜音。"""
    _use_tmp_settings(monkeypatch, tmp_path)
    import time as _t
    (tmp_path / "consult_alert_state.json").write_text(
        json.dumps({"schema": 1, "streak": 0,
                    "last_alert_ts": _t.time() + 90 * 86400}),
        encoding="utf-8")
    sent = _sent_subjects(monkeypatch)
    _fresh_process(monkeypatch, tmp_path)

    _fail(monkeypatch, 3)

    assert len(sent) == 1, "未來的時間戳把告警靜音了 90 天"


def test_the_cooldown_is_recorded_before_the_mail_is_sent(monkeypatch,
                                                          tmp_path):
    """寄信是背景做的、機器隨時會被重啟 → 冷卻要先寫下去再寄。

    寧可「寫了但信沒寄成」（下一輪補寄）也不要洗信箱。
    """
    _use_tmp_settings(monkeypatch, tmp_path)

    class _Boom:
        def __init__(self, target=None, name=None, daemon=None):
            self._target = target

        def start(self):
            raise RuntimeError("寄信執行緒起不來")

    monkeypatch.setattr(cq.threading, "Thread", _Boom)
    _fresh_process(monkeypatch, tmp_path)

    try:
        _fail(monkeypatch, 3)
    except RuntimeError:
        pass

    state = json.loads(
        (tmp_path / "consult_alert_state.json").read_text(encoding="utf-8"))
    assert state["last_alert_ts"] > 0, (
        "★冷卻沒先落地★ 這時候被重啟就會立刻再寄一封")


def test_the_alert_says_which_machine_it_came_from(monkeypatch, tmp_path):
    """多台電腦各自計算節流（沒有共用狀態）→ 信裡要說得出是哪一台。"""
    _use_tmp_settings(monkeypatch, tmp_path)
    sent = _sent_subjects(monkeypatch)
    monkeypatch.setattr(cq.socket, "gethostname", lambda: "DERM-ROOM-7")
    _fresh_process(monkeypatch, tmp_path)

    _fail(monkeypatch, 3)

    assert sent and "DERM-ROOM-7" in sent[0], (
        f"看不出是哪一台寄的：{sent}")


def test_main_actually_loads_the_state_on_startup():
    """★接線本身也要被測到★

    上面每一支都自己呼叫 `_load_job_fail_state()`，所以就算 `main()` 根本沒接
    上去，它們照樣全綠 —— 而那正是 bug 還在的樣子。這裡用 AST 直接看 `main()`
    的函式體裡有沒有那個呼叫（沒有別的路徑可以驗，main() 會要求管理員權限、
    搶 mutex、進 GUI 迴圈）。
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(cq.main)))
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_load_job_fail_state" in called, (
        "main() 沒有把節流狀態讀回來 → 每次重啟冷卻都歸零（bug 原樣）")


def test_a_clock_step_back_during_the_run_does_not_silence_the_alert(
        monkeypatch, tmp_path):
    """★校時是【執行期間】發生的★（2026-08-03 外審第 1 輪 P2）

    機器先被設到未來 → 存下一個未來的時間戳 → NTP 把時鐘校回來 →
    `now - last` 變成負數 → 永遠小於冷卻時間 → 告警靜音到時鐘追上為止。
    只在啟動載入時檢查救不到：這支程式正常運作時 log 一直在更新，
    watchdog 不會重啟它，也就不會重新載入。
    """
    _use_tmp_settings(monkeypatch, tmp_path)
    sent = _sent_subjects(monkeypatch)
    _fresh_process(monkeypatch, tmp_path)

    _fail(monkeypatch, 3)
    assert len(sent) == 1, "前提：達門檻要寄一封"

    # 時鐘曾被設到 90 天後（那一封的時間戳就是未來的），現在校回來了
    monkeypatch.setattr(cq, "_job_fail_last_alert",
                        cq.time.time() + 90 * 86400, raising=False)
    _fail(monkeypatch, 3)

    assert len(sent) == 2, (
        "未來的時間戳把告警靜音了 —— 冷卻判斷沒有套用時鐘偏移守衛")


def test_the_future_timestamp_is_also_cleared_from_disk(monkeypatch,
                                                        tmp_path):
    """★丟掉之後要落地★ 否則下次重啟又把那個未來時間戳讀回來。

    ★這裡必須走「沒有收件人」那條路★（突變驗證抓到的）：正常會寄信的路徑在
    最後本來就會把 `now` 寫下去，所以就算守衛自己不落地，磁碟上照樣看不到未來
    時間戳 —— 那個斷言是被【別人的】寫入餵飽的，測不到守衛。收件人為空時會在
    寫入之前 return，守衛的落地才是唯一把它清掉的動作。
    """
    _use_tmp_settings(monkeypatch, tmp_path)
    _sent_subjects(monkeypatch)
    _fresh_process(monkeypatch, tmp_path)

    future = cq.time.time() + 90 * 86400
    monkeypatch.setattr(cq, "_job_fail_last_alert", future, raising=False)
    for _ in range(3):
        cq._note_job_failure([], "登入沒有完成")     # ← 沒有收件人

    state = json.loads(
        (tmp_path / "consult_alert_state.json").read_text(encoding="utf-8"))
    assert state["last_alert_ts"] < cq.time.time() + 3600, (
        f"磁碟上還留著未來的時間戳：{state['last_alert_ts']}"
        " → 下次重啟讀回來又是靜音")
