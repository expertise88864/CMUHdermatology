# -*- coding: utf-8 -*-
"""[2026-08-04 實機] watchdog 的半死救援在 Windows 11 上完全失效。

實機 log 連續兩小時、每 60 秒印同一組警告，什麼都沒做：

    [watchdog] 打卡: mutex 持有但 log 6758s 沒更新 (>300s) — process 半死，嘗試找 PID 強制 kill
    [watchdog] 無法用 WMIC 找到 中國醫皮膚科打卡程式 的 PID；為避免誤殺其他 Python 程序，本輪不執行 broad fallback kill

舊版靠「列舉 python 行程 → 比對 cmdline 是否含啟動器檔名」找 PID，三個破口在這台
機器上【同時】成立：WMIC 已被 Win11 24H2 移除、PowerShell CIM 對權限較高的行程
回傳空 CommandLine、而實機 cmdline 是 `...\\src\\autoclock.py` 根本不含關鍵字。
改為行程自報 PID 檔（直接事實，不需列舉/不看 cmdline/不受提權影響）。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import pidfile  # noqa: E402
from cmuh_common import watchdog_core as wc  # noqa: E402


def test_write_read_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
    assert pidfile.write_pid_file("autoclock") is True
    assert pidfile.read_raw_pid("autoclock") == os.getpid()
    # 本行程就是 python → 身分驗證應通過（但 read_verified_pid 會排除「自己」）
    assert pidfile.pid_looks_like_python(os.getpid()) is True


def test_missing_or_corrupt_file_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
    assert pidfile.read_raw_pid("nope") is None
    (tmp_path / "bad.pid").write_text("not-a-number", encoding="utf-8")
    assert pidfile.read_raw_pid("bad") is None
    (tmp_path / "neg.pid").write_text("-5", encoding="utf-8")
    assert pidfile.read_raw_pid("neg") is None


def test_dead_pid_is_rejected(tmp_path, monkeypatch):
    """★PID 會被作業系統重用★ 不驗就可能誤殺別人的行程。"""
    monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
    (tmp_path / "gone.pid").write_text("999999", encoding="utf-8")
    assert pidfile.read_verified_pid("gone") is None


def test_non_python_pid_is_rejected(tmp_path, monkeypatch):
    """PID 活著但不是 python 行程（PID 被重用）→ 不得採用。"""
    monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
    monkeypatch.setattr(pidfile, "pid_looks_like_python", lambda _pid: False)
    (tmp_path / "reused.pid").write_text("4321", encoding="utf-8")
    assert pidfile.read_verified_pid("reused") is None


def test_own_pid_is_not_returned(tmp_path, monkeypatch):
    """watchdog 內嵌在同一支程式時，不可回報自己（會自殺）。"""
    monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
    pidfile.write_pid_file("self")
    assert pidfile.read_verified_pid("self") is None


def test_clear_only_removes_own_entry(tmp_path, monkeypatch):
    monkeypatch.setattr(pidfile, "get_settings_dir", lambda: str(tmp_path))
    pidfile.write_pid_file("mine")
    pidfile.clear_pid_file("mine")
    assert pidfile.read_raw_pid("mine") is None
    # 別人的 PID 檔不可被我清掉
    (tmp_path / "other.pid").write_text("4321", encoding="utf-8")
    pidfile.clear_pid_file("other")
    assert pidfile.read_raw_pid("other") == 4321


# ─── watchdog 接線 ──────────────────────────────────────────────────────────
def test_lookup_prefers_pid_file(monkeypatch):
    """★核心修正★ 有 PID 檔就直接用，完全不碰 cmdline 比對那條壞掉的路。"""
    called = []
    monkeypatch.setattr(wc, "_wmic_find_pids",
                        lambda kw, **k: called.append(kw) or [])
    import cmuh_common.pidfile as pf
    monkeypatch.setattr(pf, "read_verified_pid", lambda name: 12345)
    got = wc._find_pids_holding_mutex("中國醫皮膚科打卡程式", "mtx",
                                      pid_name="autoclock")
    assert got == [12345]
    assert called == [], "有 PID 檔時不該再走 cmdline 比對"


def test_lookup_falls_back_when_pid_file_unusable(monkeypatch):
    """PID 檔不存在/驗不過 → 退回原本的 cmdline 路徑（不可整條斷掉）。"""
    monkeypatch.setattr(wc, "_wmic_find_pids", lambda kw, **k: [777])
    import cmuh_common.pidfile as pf
    monkeypatch.setattr(pf, "read_verified_pid", lambda name: None)
    assert wc._find_pids_holding_mutex("kw", "mtx", pid_name="autoclock") == [777]
    # 沒給 pid_name（主程式等尚未自報的項目）→ 直接走舊路徑
    assert wc._find_pids_holding_mutex("kw", "mtx") == [777]


def test_pidfile_read_failure_does_not_break_lookup(monkeypatch):
    """讀 PID 檔爆炸也不能讓救援整條掛掉。"""
    monkeypatch.setattr(wc, "_wmic_find_pids", lambda kw, **k: [888])
    import cmuh_common.pidfile as pf

    def _boom(_name):
        raise OSError("disk")
    monkeypatch.setattr(pf, "read_verified_pid", _boom)
    assert wc._find_pids_holding_mutex("kw", "mtx", pid_name="autoclock") == [888]


def test_watched_entries_declare_pid_name():
    """打卡與會診都要宣告 pid_name，否則救援仍走壞掉的舊路。"""
    import inspect
    src = inspect.getsource(wc)
    assert '"pid_name": "autoclock"' in src
    assert '"pid_name": "consult_query"' in src
    # 半死 kill 路徑必須把 pid_name 傳進去
    i = src.index("half_dead_pids = _find_pids_holding_mutex(")
    assert "pid_name=prog.get(" in src[i:i + 200]


def test_both_programs_self_report_at_startup():
    """兩支程式啟動時要自報 PID——沒人寫檔，watchdog 就永遠讀不到。"""
    for path, name in (("src/autoclock.py", "autoclock"),
                       ("src/consult_query.py", "consult_query")):
        full = os.path.join(os.path.dirname(__file__), "..", path)
        text = open(full, encoding="utf-8").read()
        assert f'write_pid_file("{name}")' in text, f"{path} 未自報 PID"
