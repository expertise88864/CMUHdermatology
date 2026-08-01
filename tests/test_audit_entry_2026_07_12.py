# -*- coding: utf-8 -*-
"""入口批次2/3 回歸測試(2026-07-12 未審區域計畫書補修)。

EH-05 --once 分檔(簽名行為);EH-02/09、MG-03/04 以源碼層守衛防回退。
EH-06/EH-10 涉多檔重排/mode-aware+.ps1,緩修。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _read_src(*parts):
    p = os.path.join(os.path.dirname(__file__), "..", "src", *parts)
    with open(p, encoding="utf-8") as f:
        return f.read()


# ── EH-05 _setup_logging 可指定 log 檔;--once 用獨立檔 ───────────────────────
def test_eh05_setup_logging_accepts_log_path():
    import watchdog_runner
    sig = inspect.signature(watchdog_runner._setup_logging)
    assert "log_path" in sig.parameters, "EH-05 _setup_logging 未加 log_path 參數"
    src = _read_src("watchdog_runner.py")
    assert "watchdog_once.log" in src, "EH-05 --once 未用獨立 log 檔"


# ── EH-09 _setup_logging 建構失敗退化不中止 ─────────────────────────────────
def test_eh09_setup_logging_degrades_on_failure():
    src = _read_src("watchdog_runner.py")
    body = src[src.find("def _setup_logging"):src.find("def _run_once_via_core")]
    assert "except Exception:" in body and "basicConfig" in body, \
        "EH-09 _setup_logging 未於建構失敗時退化 basicConfig"


# ── EH-02 非 admin 警告 ──────────────────────────────────────────────────────
def test_eh02_admin_warning():
    src = _read_src("watchdog_runner.py")
    assert "IsUserAnAdmin" in src, "EH-02 未做非 admin 警告"


# ── MG-03 int 代號 str() + notifications .get ────────────────────────────────
def test_mg03_str_docno_and_get_notifications():
    src = _read_src("main.py")
    assert '"doc_no": str(doc_no)' in src, "MG-03 未對 doc_no 做 str()"
    assert "existing_doctor.get('notifications', False)" in src, "MG-03 notifications 未用 .get 兜底"


# ── MG-04 收父行程已死的真孤兒 chromedriver ─────────────────────────────────
def test_mg04_kills_dead_parent_orphans():
    """[2026-08-01 P2-06 第五刀(a)] 這支函式從 main.py 搬到
    `cmuh_common/program_launcher.py` —— 守的性質沒變，只是換了地址，
    所以改成【跟著函式走】而不是釘死在 main.py（見 test_main_launch_guards
    的 `_candidate_paths` 用同一個做法）。"""
    src = _read_src("cmuh_common", "program_launcher.py")
    assert "psutil.pid_exists(ppid)" in src, "MG-04 未收父行程已死的孤兒 chromedriver"
    # 而且要真的還接在退出流程上，不然搬完就成了沒人呼叫的死碼
    assert "_pl_kill_orphan_chromedriver()" in _read_src("main.py"), \
        "MG-04 清理函式已經沒有被 main.py 呼叫"
