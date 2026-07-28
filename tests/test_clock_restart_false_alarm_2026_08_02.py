# -*- coding: utf-8 -*-
"""[2026-08-02 使用者回報] 沒有執行打卡程式的電腦一直跳
「自動打卡 — 更新後重啟失敗:新版本無法啟動」。

追下去有兩個缺陷:

★根因★ `_check_update_in_background` 的背景緒在【任何設定閘門之前】就啟動。
  一台根本沒有打卡設定檔的電腦,只要 autoclock 被啟動一次,更新檢查就會
  「發現新版 → restart_program」,而新行程照設計立刻結束(無設定 → main() 直接
  返回)→ 被誤報成「新版本無法啟動」。2026-07-28 推了 8 個版本,於是每次啟動都中。

★訊息陳述程式並不確知的事★ `restart_self` 只看得到「子行程 0.6 秒內結束」,
  分不出「崩潰」與「照設計自行結束」。對一台不跑打卡的電腦宣稱「新版本無法啟動」
  既不正確、也全是噪音。
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common import paths  # noqa: E402


def _src(rel: str) -> str:
    return open(os.path.join(os.path.dirname(__file__), '..', 'src', rel),
                encoding='utf-8').read()


def _code_only(src: str) -> str:
    src = re.sub(r'("""|\'\'\')(?:.|\n)*?\1', "", src)
    return "\n".join(line.split("#")[0] for line in src.splitlines())


# ─── 根因:更新檢查不可跑在設定閘門之前 ───────────────────────────────────
def test_update_checker_starts_after_the_config_gate():
    """★根因★ 不跑打卡的機器根本不該觸發更新重啟。"""
    code = _code_only(_src('autoclock.py'))
    i_main = code.index("def main(")
    body = code[i_main:]
    i_checker = body.index('name="ClockUpdateChecker"')
    i_gate = body.index("if not accounts_data:")
    assert i_gate < i_checker, "更新檢查緒必須在『確定要進背景模式』之後才啟動"


def test_update_checker_still_runs_for_a_configured_machine():
    """不可矯枉過正:真的在跑打卡的機器仍要檢查更新。"""
    code = _code_only(_src('autoclock.py'))
    i_main = code.index("def main(")
    body = code[i_main:]
    i_checker = body.index('name="ClockUpdateChecker"')
    i_sched = body.index("scheduler_loop")
    assert i_checker < i_sched, "要在排程迴圈起跑前就開始檢查"


# ─── 訊息:分辨崩潰與照設計結束 ───────────────────────────────────────────
def test_restart_self_reports_an_orderly_exit_distinctly():
    body = _src('cmuh_common/paths.py')
    i = body.index("def restart_self(")
    seg = body[i:i + 7000]
    assert "outcome = classify_child_exit(rc, tail)" in seg
    assert "return outcome" in seg
    # 判定邏輯本身抽成純函式才測得到真的行為(見下方的子行程測試);
    # 早夭分支只負責呼叫它。
    assert "_NO_STDERR_MARKERS" not in seg,         "★不可再用『stderr 是空的』當判準★ 正常啟動 log 會讓它永遠不成立"


def test_spawn_outcome_constants_are_distinct():
    vals = {paths.SPAWN_FAILED, paths.SPAWN_CHILD_CRASHED,
            paths.SPAWN_CHILD_EXITED_ORDERLY}
    assert len(vals) == 3


def test_empty_stderr_markers_cover_every_honest_placeholder():
    """`_child_stderr_tail()` 讀不到時會回誠實的說明字串 —— 那些也算「沒有 stderr」,
    否則「讀不到暫存檔」會被誤判成崩潰。"""
    body = _src('cmuh_common/paths.py')
    i = body.index("def _child_stderr_tail")
    seg = body[i:i + 900]
    for marker in paths._NO_STDERR_MARKERS:
        if marker:
            assert marker in seg, f"標記與實際回傳字串不一致:{marker}"


def test_autoclock_only_warns_on_a_real_crash():
    """★核心★ 照設計自行結束 → 只記 log,不跳通知。"""
    code = _code_only(_src('autoclock.py'))
    i = code.index("def restart_program(")
    body = code[i:i + 3000]
    i_ok = body.index("_SPAWN_CHILD_EXITED_ORDERLY")
    i_notify = body.index("_notify_restart_failed()")
    assert i_ok < i_notify, "先判斷是否為正常結束"
    seg = body[i_ok:i_notify]
    assert "return" in seg, "正常結束要直接 return,不可往下跳通知"


def test_real_crash_still_notifies():
    """不可矯枉過正:真的崩潰仍要跳通知(那是使用者需要知道的)。"""
    code = _code_only(_src('autoclock.py'))
    i = code.index("def restart_program(")
    body = code[i:i + 3000]
    assert "_notify_restart_failed()" in body
    assert "新行程未能存活" in _src('autoclock.py')


# ─── 行為測試:用真的子行程產生 stderr(不是看原始碼)───────────────────────
def _run_child(code: str):
    """跑一個真的子行程,回 (returncode, 合併的 stdout+stderr)。

    ★這才是外審要的測試★ 之前只檢查原始碼字串與分支順序,完全沒發現
    「正常啟動 log 會讓 orderly 永遠不成立」——因為那要真的看到子行程的輸出。
    """
    import subprocess
    cp = subprocess.run([sys.executable, "-c", code],
                        capture_output=True, text=True)
    return cp.returncode, (cp.stdout or "") + (cp.stderr or "")


def test_normal_startup_log_plus_exit_zero_is_orderly():
    """★核心★ autoclock 的 _setup_clock_logging 會把 log 接到 stderr,
    「=== autoclock vX 啟動 ===」在設定閘門之前就寫出去了 → tail 永遠不是空的。
    用「stderr 是空的」當判準等於整個判定形同虛設(第一版就是這樣錯的)。"""
    rc, tail = _run_child(
        "import sys, logging;"
        "logging.basicConfig(stream=sys.stderr, level=logging.INFO);"
        "logging.info('=== autoclock v2026.07.28.8 啟動 ===');"
        "logging.info('[autoclock] 未設定,結束');"
        "sys.exit(0)")
    assert rc == 0 and tail.strip(), "前提:確實有輸出且結束碼 0"
    assert paths.classify_child_exit(rc, tail) == paths.SPAWN_CHILD_EXITED_ORDERLY


def test_a_real_traceback_is_classified_as_crash():
    rc, tail = _run_child("import nonexistent_module_xyz")
    assert rc != 0
    assert paths.classify_child_exit(rc, tail) == paths.SPAWN_CHILD_CRASHED


def test_traceback_with_exit_zero_is_still_a_crash():
    """罕見但要擋:印了 traceback 卻以 0 結束 → 仍算崩潰。"""
    fake = "Traceback (most recent call last):\n  File x\nValueError: boom"
    assert paths.classify_child_exit(0, fake) == paths.SPAWN_CHILD_CRASHED


def test_nonzero_exit_without_traceback_is_a_crash():
    assert paths.classify_child_exit(1, "just a log line") == paths.SPAWN_CHILD_CRASHED


def test_unparseable_returncode_is_conservative():
    assert paths.classify_child_exit(None, "") == paths.SPAWN_CHILD_CRASHED
