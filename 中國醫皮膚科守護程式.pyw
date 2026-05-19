# -*- coding: utf-8 -*-
"""中國醫皮膚科守護程式 — 啟動器（雙擊執行）。實際邏輯在 src/watchdog_runner.py。

功能：定期檢查 會診查詢 / 打卡 等背景程式有沒有卡死/被誤關，
      自動 kill + 重啟。Log 寫 settings/watchdog.log。
建議：加進「安裝開機自動啟動」勾選，登入時自動以 admin 啟動，不跳 UAC。
"""
import os
import runpy
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

runpy.run_path(os.path.join(_SRC, "watchdog_runner.py"), run_name="__main__")
