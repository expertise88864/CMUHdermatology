# -*- coding: utf-8 -*-
"""[2026-08-02 使用者回報]「為何會一直出現 windows 通知:自動打卡-更新後重啟失敗,
新版本無法啟動」。

那句話的意思是:`restart_self` spawn 的新行程在 0.6 秒內就死了,舊行程依設計保留
繼續跑(所以【打卡沒有中斷】)。但當時只記得到 exit code —— pythonw +
DETACHED_PROCESS 沒有 console,子行程的 traceback 會完全消失,等於無從查起。
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))


def _src():
    return open(os.path.join(os.path.dirname(__file__), '..', 'src',
                             'cmuh_common', 'paths.py'), encoding='utf-8').read()


def _restart_self_body() -> str:
    s = _src()
    i = s.index("def restart_self(")
    return s[i:i + 6000]


def test_child_stderr_is_captured():
    """spawn 時必須把子行程的 stdout/stderr 導向暫存檔,否則死因無從得知。"""
    body = _restart_self_body()
    assert "stdout=_errf" in body and "stderr=subprocess.STDOUT" in body


def test_early_death_logs_the_stderr_tail():
    """★這才是使用者要的答案★ 早夭時要把 stderr 尾巴記進 log,不能只記 exit code。"""
    body = _restart_self_body()
    i = body.index("新行程啟動後立即結束")
    seg = body[i:i + 500]
    assert "_child_stderr_tail()" in seg
    assert "stderr" in seg


def test_stderr_tail_is_honest_when_unavailable():
    """讀不到就說讀不到 —— 不可回空字串讓 log 看起來像「沒有錯誤」。"""
    body = _restart_self_body()
    i = body.index("def _child_stderr_tail")
    seg = body[i:i + 800]
    for phrase in ("未能建立", "沒有留下任何 stderr", "讀不到"):
        assert phrase in seg, f"缺少誠實回報:{phrase}"


def test_temp_file_is_removed_on_early_death():
    """早夭路徑要清掉暫存檔(那條路徑會重複發生,不可累積)。"""
    body = _restart_self_body()
    i = body.index("新行程啟動後立即結束")
    seg = body[i:i + 600]
    assert "os.remove(_err_path)" in seg


def test_parent_releases_handle_after_confirming_alive():
    """確認存活後父行程要放掉自己的 handle(子行程仍持有)。"""
    body = _restart_self_body()
    # 注意:「確認新行程存活」這句話在 docstring 裡也有一份,不可用 index() 當 anchor
    # (我第一版就抓到 docstring 那一個,順序判斷整個顛倒)。改用只出現在程式碼的字串。
    i_close = body.index("_errf.close()       # 父行程放掉自己的 handle")
    i_death = body.index("新行程啟動後立即結束")
    i_teardown = body.index("on_confirmed()")
    assert i_death < i_close < i_teardown,         "放掉 handle 要在「早夭處理之後、破壞性拆解之前」"


def test_alive_poll_budget_still_shorter_than_mutex_retry():
    """★既有設計不可被打破★ 父行程等 0.6s、子行程搶 mutex 重試 1.5s ——
    父行程必須先確認存活、再放 mutex,子行程才搶得到。"""
    from cmuh_common import paths
    from cmuh_common.single_instance import ensure_single_instance
    budget = paths._SPAWN_ALIVE_POLLS * paths._SPAWN_ALIVE_INTERVAL_SEC
    import inspect
    m = re.search(r"retry_sec: float = ([\d.]+)",
                  inspect.getsource(ensure_single_instance))
    assert m, "找不到 retry_sec 預設值"
    assert budget < float(m.group(1)), (
        f"存活確認 {budget}s 必須短於 mutex 重試 {m.group(1)}s")


def test_old_err_files_are_swept_on_next_spawn():
    """★[2026-08-02 補審第 5 輪]★ 成功重啟時子行程會【持有 handle 直到自己結束】,
    父行程刪不掉(Windows 不允許刪除他人開啟中的檔)。若不在下次 spawn 時清掃,
    每一次成功重啟都會在 %TEMP% 永久留下一個檔 —— 更新/閒置重啟每天都會發生。"""
    body = _restart_self_body()
    i_sweep = body.index("sweep_old_restart_err_files(_tmpdir)")
    i_open = body.index('_errf = open(_err_path, "wb")')
    assert i_sweep < i_open, "要先清掃再建新檔"


def test_popen_failure_removes_the_temp_file():
    """Popen 失敗 → 子行程根本沒起來,沒人持有這個檔 → 當場就該刪掉。"""
    src = _src()
    i = src.index("Popen 失敗 → 子行程根本沒起來")
    seg = src[i:i + 400]
    assert "os.remove(_err_path)" in seg


def test_sweep_removes_only_old_files_of_our_own_naming(tmp_path):
    """★測真正的函式,不是複製一份邏輯來測★
    只刪超過保留期、且只刪自己命名樣式的檔;別人的檔一律不動。"""
    import os as _os
    import time as _time
    from cmuh_common.paths import sweep_old_restart_err_files
    now = _time.time()
    old_f = tmp_path / "cmuh_restart_autoclock_123.err"
    new_f = tmp_path / "cmuh_restart_autoclock_456.err"
    other = tmp_path / "別人的檔.err"
    other2 = tmp_path / "cmuh_restart_不是err.txt"
    for f in (old_f, new_f, other, other2):
        f.write_bytes(b"x")
    for f in (old_f, other, other2):
        _os.utime(f, (0, now - 200000))
    removed = sweep_old_restart_err_files(str(tmp_path), now=now)
    assert removed == 1
    assert not old_f.exists(), "舊的自家檔要被清掉"
    assert new_f.exists(), "新的不動"
    assert other.exists() and other2.exists(), "★不可波及別人的檔★"


def test_sweep_never_raises_on_a_locked_or_missing_dir():
    """清理失敗不可影響重啟本身。"""
    from cmuh_common.paths import sweep_old_restart_err_files
    assert sweep_old_restart_err_files("Z:/根本不存在的目錄") == 0
