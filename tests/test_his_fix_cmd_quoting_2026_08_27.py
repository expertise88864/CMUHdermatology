# -*- coding: utf-8 -*-
"""[外審 2026-08-27 P2-06] `修正HIS熱鍵ID.cmd` 的提權路徑不可以插進
PowerShell 原始碼。

原本:`Start-Process -FilePath '%~f0' ...` —— `%~f0` 被 cmd 展開後直接落在
PowerShell 的★單引號字串裡★。空白與中文沒事,但單引號是那個字串的結束符:
安裝在 `C:\\Users\\O'Connor\\CMUH App\\` 之類的合法 Windows 路徑時,
PowerShell 解析就斷在路徑中間 → ★這支急救工具完全無法提權★
(而它正是 HIS 改版打歪熱鍵時、不等推版的唯一通道)。

修法:路徑先進環境變數,PowerShell 只引用 `$env:CMUH_ELEVATE_TARGET`——
路徑成為【資料】而不是【原始碼】。
"""
import os
import re
import shutil
import subprocess
import sys

import pytest

CMD = os.path.join(os.path.dirname(__file__), "..", "修正HIS熱鍵ID.cmd")
#: 合法但會打斷 PowerShell 單引號字串/帶 cmd 元字元的安裝路徑。
NASTY_PATHS = (
    r"C:\CMUH App\修正HIS熱鍵ID.cmd",          # 空白 + 中文
    r"C:\Users\O'Connor\CMUH\修正HIS熱鍵ID.cmd",  # ★單引號★
    r"C:\a&b\c;d\修正HIS熱鍵ID.cmd",           # cmd/PS 元字元
)


def _cmd_text() -> str:
    with open(CMD, encoding="utf-8") as f:
        return f.read()


def _elevate_line() -> str:
    for ln in _cmd_text().splitlines():
        if "Start-Process" in ln and "RunAs" in ln:
            return ln
    raise AssertionError("找不到提權那一行")


class TestThePathIsDataNotSource:
    def test_the_target_path_is_not_interpolated_into_powershell(self):
        """★靜態性質★:提權那一行不可以出現任何 `%...%` / `%~f0` 展開 ——
        cmd 的展開發生在 PowerShell 看到這串字之前,展開進去的東西就是
        原始碼的一部分。"""
        line = _elevate_line()
        assert "%~f0" not in line, line
        assert not re.search(r"%[A-Za-z_~][^%]*%", line), (
            f"提權行仍把 cmd 變數展開進 PowerShell 原始碼:{line}")

    def test_it_reads_the_path_from_the_environment(self):
        """路徑要走環境變數,而且★真的有設★(只引用不設定 = 提權目標為空)。"""
        line = _elevate_line()
        assert "$env:CMUH_ELEVATE_TARGET" in line, line
        assert re.search(r'^\s*set\s+"CMUH_ELEVATE_TARGET=%~f0"\s*$',
                         _cmd_text(), re.MULTILINE), "沒有設定該環境變數"

    def test_the_elevated_marker_is_still_passed(self):
        """★不可以為了修引號而弄丟 /elevated★:沒有它,提權後的那份會再
        試著提權一次 → UAC 無限迴圈(檔頭註解說明的第二道保險)。"""
        assert "'/elevated'" in _elevate_line()


@pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell"),
                    reason="需要 Windows PowerShell(CI 為 windows-latest)")
def test_powershell_receives_every_nasty_path_intact():
    """★真的量給 PowerShell 看★:靜態檢查證明不了「這樣寫解析得動」——
    用 `.cmd` 裡★同一串★命令,把三個合法安裝路徑逐一餵進去,
    Start-Process 換成回報用的樁,看 -FilePath 收到的是不是原字串。

    (三個路徑在同一次 PowerShell 啟動裡跑完:一次 spawn ≈ 1 秒,
     三次就是三秒 —— 測試套件的時間也是成本。)
    """
    invoke = _elevate_line().split("-Command", 1)[1].strip().strip('"')
    script = (
        "function Start-Process { param($FilePath, $ArgumentList, $Verb) "
        "Write-Output \"GOT=$FilePath\" }\n"
        + "\n".join(
            f"$env:CMUH_ELEVATE_TARGET = @'\n{p}\n'@\n{invoke}"
            for p in NASTY_PATHS))
    r = subprocess.run(["powershell", "-NoProfile", "-Command", script],
                       capture_output=True, text=True, timeout=120)
    got = [ln[4:] for ln in r.stdout.splitlines() if ln.startswith("GOT=")]
    assert r.returncode == 0, r.stderr
    assert got == list(NASTY_PATHS), (got, r.stderr)
