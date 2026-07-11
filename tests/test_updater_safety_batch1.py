# -*- coding: utf-8 -*-
"""基建批次1 更新安全鏈回歸（§6G/§7C：IE-01~04 + EH-01，2026-07-10）。

  IE-01 下載失敗 backoff 把「失敗」變「靜默跳過」→ 第二輪檢查寫出混版本(cmuh_common 五程式共用)。
  IE-02 五程式同時 check_and_update 無跨行程互斥 → .bak 互踩/回滾錯版本/混 commit。
  IE-03 get_auto_update_suspend_until 暫時鎖檔會誤刪有效 suspend 旗標 + 過期刪的 TOCTOU。
  IE-04 updater 寫入階段不重查 suspend 旗標 → 下載期(≤5分)被抑制無效。
  EH-01 六個 .pyw 對 import 階段例外零兜底 → pythonw 靜默死亡零 log。
"""
import inspect
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from cmuh_common import updater  # noqa: E402
from cmuh_common import update_policy  # noqa: E402


# ══ IE-01：backoff 中不可 return None,要 raise(讓整批 fail-closed)═══════════════
def test_ie01_download_one_raises_during_backoff():
    updater._sha_mismatch_until["fake_ie01_key"] = time.time() + 600
    try:
        fe = {"key": "fake_ie01_key", "remote_path": "x",
              "local_filename": "src/cmuh_common/version.py",
              "version": "9.9.9", "sha256": "deadbeef"}
        with pytest.raises(ValueError):
            updater._download_one(fe, str(ROOT))
    finally:
        updater._sha_mismatch_until.pop("fake_ie01_key", None)


# ══ IE-03：暫時鎖檔不誤刪、過期不刪、只有真損壞才刪 ═══════════════════════════════
def test_ie03_expired_flag_not_deleted(tmp_path, monkeypatch):
    p = tmp_path / ".auto_update_suspended_until"
    monkeypatch.setattr(update_policy, "get_auto_update_suspend_path", lambda: str(p))
    p.write_text(str(time.time() - 10), encoding="utf-8")   # 已過期
    assert update_policy.get_auto_update_suspend_until() == 0.0
    assert p.exists(), "IE-03: 過期旗標不可刪(避免 TOCTOU 誤刪新旗標)"


def test_ie03_corrupt_flag_deleted(tmp_path, monkeypatch):
    p = tmp_path / ".auto_update_suspended_until"
    monkeypatch.setattr(update_policy, "get_auto_update_suspend_path", lambda: str(p))
    p.write_text("garbage-not-a-number", encoding="utf-8")
    assert update_policy.get_auto_update_suspend_until() == 0.0
    assert not p.exists(), "IE-03: 內容真損壞才可刪"


def test_ie03_read_lock_conservative_no_delete(tmp_path, monkeypatch):
    p = tmp_path / ".auto_update_suspended_until"
    monkeypatch.setattr(update_policy, "get_auto_update_suspend_path", lambda: str(p))
    p.write_text(str(time.time() + 9999), encoding="utf-8")   # 有效旗標

    real_open = open

    def _locked_open(path, *a, **k):
        if str(path) == str(p):
            raise OSError("simulated AV/OneDrive lock")
        return real_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", _locked_open)
    r = update_policy.get_auto_update_suspend_until()
    assert r > time.time(), "IE-03: 讀取失敗應保守視同仍抑制(fail-closed)"
    monkeypatch.undo()
    assert p.exists(), "IE-03: 讀取失敗不可刪有效旗標"


# ══ IE-02/IE-04：原始碼守門(跨行程 mutex + 寫入前重查 suspend)═══════════════════
def test_ie02_write_phase_holds_cross_process_lock():
    src = inspect.getsource(updater.check_and_update)
    assert "_updater_write_lock(" in src, "IE-02: 寫檔階段應取跨行程更新鎖"
    lock_src = inspect.getsource(updater._updater_write_lock)
    # [codex] 用 msvcrt.locking OS 鎖(跨 session、crash 自動釋放),非 Local\\ mutex、非手動 stale 判斷
    assert "msvcrt.locking" in lock_src and ".updater_write.lock" in lock_src, \
        "IE-02: 應用 msvcrt OS 鎖(跨 session、避開 stale race)"


def test_ie02_lock_acquires_and_exception_safe(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "get_app_dir", lambda: str(tmp_path))
    # 可取得
    with updater._updater_write_lock() as ok:
        assert ok is True
    # body 例外不被吞、鎖照樣釋放(finally),之後可再取得
    with pytest.raises(ValueError):
        with updater._updater_write_lock() as ok:
            assert ok is True
            raise ValueError("body")
    with updater._updater_write_lock(timeout_sec=0.5) as again:
        assert again is True, "IE-02: body 例外後鎖已釋放,應可再取得(try/finally)"


def test_ie02_lock_is_exclusive_cross_process(tmp_path):
    import subprocess
    import textwrap
    holder_code = textwrap.dedent(f"""
        import sys, time
        sys.path.insert(0, {str(ROOT / 'src')!r})
        from cmuh_common import updater
        updater.get_app_dir = lambda: {str(tmp_path)!r}
        with updater._updater_write_lock() as ok:
            print("HELD" if ok else "FAIL", flush=True)
            time.sleep(2.5)
    """)
    holder = subprocess.Popen([sys.executable, "-c", holder_code],
                              stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "HELD", "子行程未取得鎖"
        # 子行程持有中 → 本行程短逾時取鎖應失敗(跨行程互斥)
        import cmuh_common.updater as u
        _orig = u.get_app_dir
        u.get_app_dir = lambda: str(tmp_path)
        try:
            with u._updater_write_lock(timeout_sec=0.5) as ok:
                assert ok is False, "IE-02: 子行程持有鎖時本行程應取不到(跨行程互斥)"
        finally:
            u.get_app_dir = _orig
    finally:
        holder.wait(timeout=8)


def test_ie04_recheck_suspend_before_write():
    src = inspect.getsource(updater.check_and_update)
    # 寫檔前(取鎖之後)要再查一次 suspend
    lock_idx = src.index("_updater_write_lock(")
    assert "get_auto_update_suspend_until()" in src[lock_idx:], \
        "IE-04: 取鎖後、寫檔前要再查一次 suspend"


def test_ie02_downgrade_protection_after_lock():
    # [codex P2] 取鎖後要重讀磁碟版本、比對 manifest 版本,避免用過時 prepared_writes 覆蓋降版
    src = inspect.getsource(updater.check_and_update)
    lock_idx = src.index("_updater_write_lock(")
    assert "_read_ondisk_app_version(" in src[lock_idx:], \
        "codex P2: 取鎖後應重讀磁碟版本做降版防護"
    # helper 讀得到 repo 內真實版本(非空、可解析)
    v = updater._read_ondisk_app_version(str(ROOT))
    assert v and updater.parse_version(v) > (0,), "應讀到 version.py 的 CURRENT_VERSION"


def test_ie02_external_newer_version_requests_restart(tmp_path, monkeypatch):
    # [codex P2 round2] 併發下別的程式把磁碟更新到比「本行程執行版本」還新;本行程降版放棄寫檔,
    # 但因為自己在跑舊碼、磁碟已是新碼(之後 lazy import 會版本錯亂)→ 應【要求重啟】而非靜默 has_update=False。
    disk_ver = "2099.12.31.9"        # 磁碟被別的程式寫到的新版
    manifest_ver = "2099.01.01.1"    # 本批 manifest:高於(被壓低的)CURRENT_VERSION、但低於 disk_ver
    monkeypatch.setattr(updater, "CURRENT_VERSION", "1.0.0.0")   # 本行程執行版本(壓低使斷言確定)
    vp = tmp_path / "src" / "cmuh_common" / "version.py"
    vp.parent.mkdir(parents=True, exist_ok=True)
    vp.write_text('CURRENT_VERSION = "%s"\n' % disk_ver, encoding="utf-8")
    target = tmp_path / "a.py"
    target.write_text("old-a", encoding="utf-8")

    monkeypatch.setattr(updater, "get_app_dir", lambda: str(tmp_path))
    monkeypatch.setattr(updater, "is_frozen", lambda: False)
    monkeypatch.setattr(updater, "_fetch_manifest", lambda: {
        "app_version": manifest_ver,
        "files": [{"key": "a", "local_filename": "a.py"}],
    })
    monkeypatch.setattr(
        updater, "_download_one",
        lambda entry, _app_dir: (entry["key"], entry["local_filename"], manifest_ver, "new-a"))

    result = updater.check_and_update(write_files=True)

    # 降版分支:磁碟正式檔不可被覆寫
    assert target.read_text(encoding="utf-8") == "old-a", "降版放棄分支不可覆寫磁碟檔"
    # 但因磁碟版本新於本行程執行版本 → 要求重啟,且提示帶磁碟新版號
    assert result.has_update is True
    assert updater.need_restart_after_update(result) is True
    assert any(ver == disk_ver for _fn, ver in result.updated_files), \
        "updated_files 應帶磁碟新版號供 main.py 重啟提示"


# ══ EH-01：六個 .pyw 對 import 例外兜底(寫 crash log + 不吞 SystemExit)══════════
@pytest.mark.parametrize("launcher", [
    "中國醫皮膚科主程式.pyw", "中國醫皮膚科守護程式.pyw", "中國醫皮膚科打卡程式.pyw",
    "中國醫皮膚科排班程式.pyw", "中國醫皮膚科會診查詢程式.pyw", "中國醫皮膚科點座標偵測程式.pyw",
])
def test_eh01_launcher_has_startup_tolerance(launcher):
    text = (ROOT / launcher).read_text(encoding="utf-8")
    assert "runpy.run_path(" in text
    assert "try:" in text and "except Exception:" in text, \
        f"{launcher}: 應包 try/except Exception 兜底 import 例外"
    assert "startup_crash.log" in text, f"{launcher}: 應寫 startup_crash.log"
    # 只攔 Exception,不可攔 SystemExit(deps 取消/單例退出要照常穿出)
    assert "except SystemExit" not in text and "except BaseException" not in text, \
        f"{launcher}: 不可攔 SystemExit/BaseException"
    # 兜底後要 re-raise(不吞掉錯誤)
    assert text.rstrip().endswith("raise"), f"{launcher}: 兜底後應 re-raise"
