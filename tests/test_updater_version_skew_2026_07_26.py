# -*- coding: utf-8 -*-
"""[2026-07-26 審查] updater 降版守衛:讀不到磁碟版本 ≠ 磁碟上沒有版本。

`_commit_pending_writes` 的說明明寫它要防 version skew(部分檔新、部分檔舊),
取得跨行程寫入鎖之後也確實有一道降版守衛。但那道守衛寫成 `if (_disk_ver and ...)`,
而 `_read_ondisk_app_version` 對【檔案不存在 / 內容損壞 / 被防毒鎖住】三種情況
一律回 '' —— 於是鎖檔那一瞬間守衛整個被跳過:本批若是較舊的 manifest revision
(CDN 舊清單),就會覆蓋磁碟上別的程式剛寫好的新版 = 降版 + 版本錯亂。
與打卡設定檔、排班 config、watchdog config 是同一個病灶。
"""
import builtins
import inspect
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import updater as u  # noqa: E402


def _code_only(src: str) -> str:
    src = re.sub(r'("""|\'\'\')(?:.|\n)*?\1', "", src)
    return "\n".join(line.split("#")[0] for line in src.splitlines())


def _mk(tmp_path, content=None):
    d = tmp_path / "app"
    (d / "src" / "cmuh_common").mkdir(parents=True)
    if content is not None:
        (d / "src" / "cmuh_common" / "version.py").write_text(
            content, encoding="utf-8")
    return str(d)


def test_status_ok(tmp_path):
    app = _mk(tmp_path, 'CURRENT_VERSION = "2026.07.26.9"')
    assert u._read_ondisk_app_version_ex(app) == ("2026.07.26.9", "ok")


def test_status_missing(tmp_path):
    assert u._read_ondisk_app_version_ex(_mk(tmp_path)) == ("", "missing")


def test_status_unparsable(tmp_path):
    """檔案在但沒有 CURRENT_VERSION(寫到一半/內容損壞)。"""
    app = _mk(tmp_path, "# 寫到一半就斷了")
    assert u._read_ondisk_app_version_ex(app) == ("", "unparsable")


def test_status_error_when_locked(tmp_path, monkeypatch):
    """★關鍵★ 原檔完好、只是被防毒/備份鎖住 —— 這與「沒有版本」完全不同。"""
    app = _mk(tmp_path, 'CURRENT_VERSION = "2026.07.26.9"')
    real_open = builtins.open

    def _locked(path, *a, **k):
        if str(path).endswith("version.py"):
            raise PermissionError(13, "locked")
        return real_open(path, *a, **k)

    monkeypatch.setattr(builtins, "open", _locked)
    assert u._read_ondisk_app_version_ex(app) == ("", "error")


def test_downgrade_guard_aborts_on_transient_read_error():
    """暫時讀不到 → 無法確認本批是不是比磁碟舊 → 必須整批放棄,不可寫。
    放棄的成本只是晚一輪更新;寫下去的成本是把診間程式降版或寫成半新半舊。"""
    code = _code_only(inspect.getsource(u.check_and_update))
    assert "_read_ondisk_app_version_ex(app_dir)" in code, "要用帶狀態的版本"
    i_read = code.index("_read_ondisk_app_version_ex(app_dir)")
    i_commit = code.index("_commit_pending_writes(")
    seg = code[i_read:i_commit]
    assert '_disk_status == "error"' in seg, "要辨識暫時性讀取失敗"
    i_err = seg.index('_disk_status == "error"')
    assert "return result" in seg[i_err:], "讀不到就要在寫入之前 return"


def test_missing_and_unparsable_still_allow_repair_write():
    """missing / unparsable 代表磁碟上【沒有可信版本可比】—— 寫下去是修復,
    不可因為這次收緊而讓壞掉的安裝永遠修不回來(那會 brick)。"""
    code = _code_only(inspect.getsource(u.check_and_update))
    i_read = code.index("_read_ondisk_app_version_ex(app_dir)")
    seg = code[i_read:code.index("_commit_pending_writes(")]
    for bad in ('"missing"', '"unparsable"'):
        assert bad not in seg, f"{bad} 不可被當成中止條件(那是修復路徑)"


def test_invalid_utf8_is_unparsable_not_error(tmp_path):
    """★外審★ 內容不是合法 UTF-8 = 檔案【損壞】,不是「暫時讀不到」。
    歸到 error 會讓這台機器每一輪都放棄更新 = 永遠修不回來(brick);
    歸到 unparsable 才會走修復路徑,把完整的一批寫回去。"""
    d = tmp_path / "app" / "src" / "cmuh_common"
    d.mkdir(parents=True)
    (d / "version.py").write_bytes(b'CURRENT_VERSION = "1.0.0"\n\xff\xfe\x00bad')
    assert u._read_ondisk_app_version_ex(str(tmp_path / "app")) == ("", "unparsable")


def test_abort_records_error_so_ui_does_not_say_up_to_date():
    """★故障看起來跟正常一樣★ 呼叫端是靠 result.errors 分辨「失敗」與「已是最新」。
    不記的話 UI 會顯示「所有程式皆為最新版本」,但其實下載好的更新被丟掉了。"""
    code = _code_only(inspect.getsource(u.check_and_update))
    i_err = code.index('_disk_status == "error"')
    seg = code[i_err:code.index("_commit_pending_writes(")]
    i_append = seg.index("result.errors.append(")
    i_return = seg.index("return result")
    assert i_append < i_return, "要先記 error 再 return"
