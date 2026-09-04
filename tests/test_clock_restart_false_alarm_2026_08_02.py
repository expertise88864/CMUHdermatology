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
    # [第九輪 §4] 交棒判定抽成 wait_for_handover;早夭分支在那裡呼叫 classify_child_exit。
    assert "outcome = wait_for_handover(" in seg
    assert "return outcome" in seg
    j = body.index("def wait_for_handover(")
    assert "return classify_child_exit(rc, stderr_tail())" in body[j:j + 6000]
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
    import os
    import subprocess
    # ★兩邊都要明寫 utf-8★:`text=True` 不指定編碼時用的是 host codec
    #   (這台是 cp950/gbk)。子行程印的是中文 —— 編碼一不一致靠的是執行環境
    #   有沒有 PYTHONIOENCODING,而解碼失敗是發生在 subprocess 的讀取執行緒裡:
    #   例外變成一個 warning,`stdout` ★靜靜變成空字串★。
    #   於是「有沒有輸出」這個判準會憑環境變數決定勝負,而且失敗方向是
    #   ★假裝子行程什麼都沒印★ —— 正是這個檔要防的那種假綠燈。
    cp = subprocess.run([sys.executable, "-c", code],
                        capture_output=True, encoding="utf-8",
                        errors="replace",
                        env={**os.environ, "PYTHONIOENCODING": "utf-8"})
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


# ─── [使用者定案] 沒有設定檔的電腦不啟動 autoclock ────────────────────────
def test_background_launch_without_config_does_not_open_a_window():
    """★使用者定案★「一台沒有打卡設定檔的電腦不用啟動 autoclock」。

    原本是開設定視窗 —— 於是每一台被順手啟動過的電腦(捷徑/啟動資料夾)都會跳出
    一個沒人要設定的視窗,而且在更新檢查搬位置之前還會連帶產生假警報。
    """
    code = _code_only(_src('autoclock.py'))
    i = code.index("if not accounts_data:")
    seg = code[i:i + 900]
    # 有 --configure-if-empty 的路徑會開視窗(那是主程式按鈕/內部重啟);
    # 這裡要驗的是【沒有旗標】的冷啟動路徑 —— 它必須直接 return,不開視窗。
    i_silent = seg.index("不啟動")
    cold = seg[i_silent:]
    assert "ClockApp(" not in cold, "★冷啟動無設定時不可開設定視窗★"
    assert "return" in cold


def test_configure_flag_still_opens_the_window():
    """★設定的路必須還在★ 否則新機器永遠沒辦法第一次設定。"""
    code = _code_only(_src('autoclock.py'))
    i = code.index('"--configure" in _flags')
    seg = code[i:i + 400]
    assert "ClockApp(" in seg and "mainloop()" in seg


def test_main_button_always_expresses_intent_not_a_file_check():
    """★補審 P2★ 按鈕【不可】自己判斷設定檔在不在。

    autoclock 的閘門看的是「有沒有帳號」,而檔案可能存在卻是 `[]`(使用者刪光最後
    一個帳號)。兩邊判斷不同步 → 按鈕啟動背景模式、autoclock 靜默結束,
    設定視窗再也叫不出來。「什麼算可用設定」只由 autoclock 判斷一次。

    ★[2026-08-01 P2-06 第五刀(a)] 旗標搬到 `program_launcher.AUTOCLOCK.args`★
    守的兩件事沒變，只是換了地址：(1) 打卡按鈕一定帶 --configure-if-empty；
    (2) 呼叫端不可以自己去看設定檔。所以兩層分開釘。
    """
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    from cmuh_common import program_launcher as pl

    assert "--configure-if-empty" in pl.AUTOCLOCK.args, \
        "打卡按鈕沒帶旗標 → 沒設定的電腦會靜默結束，設定視窗再也叫不出來"

    code = _code_only(_src('main.py'))
    i = code.index("def _launch_autoclock_program(")
    seg = code[i:i + 1400]
    assert "AUTOCLOCK" in seg, "按鈕沒有用 AUTOCLOCK 那張描述表"
    assert "autoclock_config.json" not in seg, "★不可在呼叫端重複判斷可用性★"


def test_configure_if_empty_opens_the_window_when_no_accounts():
    code = _code_only(_src('autoclock.py'))
    i = code.index("if not accounts_data:")
    seg = code[i:i + 900]
    i_flag = seg.index("CONFIGURE_IF_EMPTY_FLAG in sys.argv[1:]")
    i_silent = seg.index("不啟動")
    assert i_flag < i_silent, "有旗標時先開視窗,沒有才靜默結束"
    assert "ClockApp(" in seg[i_flag:i_silent]


def test_internal_restart_carries_the_flag_only_when_this_pc_does_clocking():
    """內部重啟帶旗標 —— 但【僅限本機有打卡帳號】。

    原意(2026-08-02):使用者在設定視窗刪光最後一個帳號並存檔後,新行程若靜默
    消失,使用者會以為打卡還在跑 → 所以要把設定視窗開回來。

    ★[2026-08-06 使用者第二次回報] 那個情境的前提是「本來有帳號」★
    本來就沒有帳號的電腦不屬於它:無條件帶旗標會讓【自動更新重啟】在一台根本
    不做打卡的電腦上彈出打卡設定視窗,而且那個視窗被關掉還可能被判成
    「新版本無法啟動」——正是使用者抱怨的噪音來源。
    """
    code = _code_only(_src('autoclock.py'))
    i = code.index("def restart_program(")
    seg = code[i:i + 2500]
    assert "CONFIGURE_IF_EMPTY_FLAG not in extra" in seg
    assert "extra.append(CONFIGURE_IF_EMPTY_FLAG)" in seg
    assert "_machine_has_clock_accounts()" in seg, (
        "★又變回無條件帶旗標★ 沒設定打卡的電腦會被彈出設定視窗")


def test_launch_app_script_forwards_args():
    from cmuh_common.process_launch import launch_app_script
    import inspect
    sig = inspect.signature(launch_app_script)
    assert "args" in sig.parameters
    src = inspect.getsource(launch_app_script)
    assert "args=tuple(args)" in src, "要真的往下傳,不是只收著"


def test_flag_detection_is_order_independent():
    """★補審 P1★ 主程式按鈕帶 --configure-if-empty 啟動後,tray「設定」會再
    append --configure → argv 變成 ["--configure-if-empty", "--configure"]。
    用 `sys.argv[1] == ...` 位置判斷的話兩個分支都不匹配 → 直接落回背景模式,
    設定視窗永遠打不開。"""
    code = _code_only(_src('autoclock.py'))
    i = code.index("_flags = set(sys.argv[1:])")
    seg = code[i:i + 700]
    assert '"--configure" in _flags' in seg
    assert '"--test-login" in _flags' in seg
    assert "sys.argv[1] ==" not in code, "★不可再用位置判斷旗標★"
