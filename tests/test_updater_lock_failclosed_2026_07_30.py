# -*- coding: utf-8 -*-
"""[2026-07-30 第二輪外審 P1-06] 更新鎖拿不到就退回「無鎖寫入」。

`_updater_write_lock()` 存在的唯一理由是:開機時 watchdog 幾乎同時拉起五支程式,
每支啟動都背景 `check_and_update`、全部寫同一批 `src/cmuh_common/*.py` 與同名
`.bak` → .bak 互踩、回滾還原到錯版本、混 commit。

但它有四條失敗路徑全都 `yield True`(＝照樣寫):
  1. 取不到 app dir
  2. import msvcrt 失敗
  3. 開鎖檔失敗
  4. 初始化鎖檔失敗

其中 (2) 是刻意的平台判斷(部署目標是 Windows,非 Windows 不擋);但 (1)(3)(4)
是【在 Windows 上鎖機制真的壞掉】—— 而那正是磁碟權限/防毒問題最可能發生的時刻,
也正是最需要這把鎖的時刻。在那個瞬間退回無鎖,等於這把鎖只在不需要它的時候有效。

修法:Windows 上 (1)(3)(4) 一律 fail-closed(`yield False`,本輪不寫),
只有「確定不是 Windows」才維持無鎖放行。
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common import updater  # noqa: E402


def _acquired(**patches):
    """跑一次 context manager,回它 yield 出來的值。"""
    with updater._updater_write_lock(timeout_sec=0.1) as ok:
        return ok


# ─── Windows 上鎖機制壞掉 → 不可寫 ─────────────────────────────────────────
def test_a_missing_app_dir_fails_closed(monkeypatch):
    monkeypatch.setattr(updater, "IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(updater, "get_app_dir",
                        lambda: (_ for _ in ()).throw(OSError("拿不到")))
    assert _acquired() is False, "★取不到 app dir 就不該寫★"


def test_an_unopenable_lock_file_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(updater, "get_app_dir", lambda: str(tmp_path))
    real_open = os.open

    def _boom(path, *a, **k):
        if str(path).endswith(".updater_write.lock"):
            raise OSError("被防毒鎖住")
        return real_open(path, *a, **k)

    monkeypatch.setattr(os, "open", _boom)
    assert _acquired() is False, "★開不了鎖檔就不該寫★"


def test_an_uninitialisable_lock_file_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(updater, "get_app_dir", lambda: str(tmp_path))
    monkeypatch.setattr(os, "write",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("磁碟滿")))
    assert _acquired() is False, "★初始化不了鎖檔就不該寫★"


def test_no_msvcrt_on_windows_fails_closed(tmp_path, monkeypatch):
    """★這條最容易被當成「平台判斷」而放過★

    在 Windows 上 import msvcrt 失敗不是「這不是 Windows」,而是【執行環境壞了】。
    那時退回無鎖,正是這把鎖最需要生效的情況。
    """
    monkeypatch.setattr(updater, "IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(updater, "get_app_dir", lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "msvcrt", None)
    assert _acquired() is False


# ─── 非 Windows → 維持放行(刻意) ──────────────────────────────────────────
def test_non_windows_still_passes_through(tmp_path, monkeypatch):
    """★不可矯枉過正★ 部署目標是 Windows;在 CI(Linux)或開發機上不可把更新鎖死。"""
    monkeypatch.setattr(updater, "IS_WINDOWS", False, raising=False)
    monkeypatch.setattr(updater, "get_app_dir", lambda: str(tmp_path))
    assert _acquired() is True


# ─── 正常路徑 ──────────────────────────────────────────────────────────────
@pytest.mark.skipif(not sys.platform.startswith("win"), reason="需要 msvcrt")
def test_the_lock_is_actually_acquired_and_released(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "get_app_dir", lambda: str(tmp_path))
    assert _acquired() is True
    assert _acquired() is True, "釋放之後要能再拿一次(不可自我卡死)"


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="需要 msvcrt")
def test_a_second_holder_is_refused_not_allowed_through(tmp_path, monkeypatch):
    """★核心★ 真的有人持著鎖時,第二個必須拿不到(而不是退回無鎖照寫)。"""
    monkeypatch.setattr(updater, "get_app_dir", lambda: str(tmp_path))
    with updater._updater_write_lock(timeout_sec=0.1) as first:
        assert first is True
        with updater._updater_write_lock(timeout_sec=0.1) as second:
            assert second is False, "第二個持有者不可被放行"


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="需要 msvcrt")
def test_body_exceptions_still_release_the_lock(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "get_app_dir", lambda: str(tmp_path))
    with pytest.raises(RuntimeError):
        with updater._updater_write_lock(timeout_sec=0.1):
            raise RuntimeError("body 炸了")
    assert _acquired() is True, "body 例外之後鎖必須已釋放"


# ─── 真正的兩行程整合測試(不是 mock)──────────────────────────────────────
_HOLDER = r'''
import os, sys, time
sys.path.insert(0, sys.argv[1])
from cmuh_common import updater
updater.get_app_dir = lambda: sys.argv[2]
held, release = sys.argv[3], sys.argv[4]
with updater._updater_write_lock(timeout_sec=5.0) as ok:
    if not ok:
        sys.exit(2)                       # 拿不到 → 讓父行程看得出來
    open(held, "w").close()               # 告訴父行程「我拿到了」
    for _ in range(200):                  # 最多等 20 秒,免得卡住 CI
        if os.path.exists(release):
            break
        time.sleep(0.1)
sys.exit(0)
'''


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="需要 msvcrt")
def test_two_real_processes_are_serialised(tmp_path, monkeypatch):
    """★這把鎖的存在理由就是跨行程★

    開機時 watchdog 幾乎同時拉起五支程式,每支都背景 check_and_update、全部寫同一批
    src/cmuh_common/*.py 與同名 .bak。單行程的 mock 測不到那件事 —— 這裡真的開一個
    子行程持著鎖,驗證本行程確實被拒絕(而不是退回無鎖照寫)。
    """
    import subprocess

    src_dir = os.path.join(os.path.dirname(__file__), "..", "src")
    script = tmp_path / "holder.py"
    script.write_text(_HOLDER, encoding="utf-8")
    held = tmp_path / "held.flag"
    release = tmp_path / "release.flag"

    child = subprocess.Popen(
        [sys.executable, str(script), os.path.abspath(src_dir), str(tmp_path),
         str(held), str(release)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for _ in range(100):                      # 最多等 10 秒
            if held.exists() or child.poll() is not None:
                break
            time.sleep(0.1)
        assert held.exists(), (
            "子行程沒拿到鎖,測試前提不成立:"
            f"rc={child.poll()} err={child.stderr.read()[:300] if child.stderr else b''!r}")

        monkeypatch.setattr(updater, "get_app_dir", lambda: str(tmp_path))
        with updater._updater_write_lock(timeout_sec=0.5) as ok:
            assert ok is False, "★另一個【真的行程】持著鎖,本輪必須放棄★"
    finally:
        release.write_text("go", encoding="utf-8")
        try:
            child.wait(timeout=15)
        except subprocess.TimeoutExpired:
            child.kill()

    # 子行程放掉之後,本行程要拿得到(鎖確實被釋放,不是永久卡住)
    monkeypatch.setattr(updater, "get_app_dir", lambda: str(tmp_path))
    with updater._updater_write_lock(timeout_sec=5.0) as ok:
        assert ok is True, "子行程結束後鎖仍未釋放"
