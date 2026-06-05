# -*- coding: utf-8 -*-
"""Import 冒煙測試 —— 確保每個入口模組都能被乾淨 import。

純 import 會執行模組層級程式碼(class/常數/函式定義、所有 import 解析),因此能在
「上線前」攔截:語法錯誤、壞掉的 import、缺少的依賴、模組層 NameError。

範圍界線:方法/函式『內部』的 NameError(例如曾發生的 scheduler 漏 import
`_HOTKEY_BASE_SIZE`)不會在 import 期觸發,那類由 ruff F821 在 push 關卡 / CI 把關;
兩者互補,合起來涵蓋「import 期」與「靜態未定義名稱」兩大類低級錯誤。

實作:用『子程序』執行 import,隔離模組層副作用(single-instance mutex、DPI awareness、
log 檔建立等),避免污染 pytest 行程,也更貼近實際雙擊啟動的情境。
"""
import subprocess
import sys
from pathlib import Path

import pytest

_SRC = Path(__file__).resolve().parent.parent / "src"

# 對應 6 支 .pyw 啟動器實際 run 的模組(README 五支 + watchdog 守護)。
ENTRY_MODULES = [
    "main",
    "scheduler",
    "autoclock",
    "consult_query",
    "coord_detector",
    "watchdog_runner",
]


@pytest.mark.parametrize("module", ENTRY_MODULES)
def test_entry_module_imports_cleanly(module):
    code = f"import sys; sys.path.insert(0, {str(_SRC)!r}); import {module}"
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, (
        f"`import {module}` 失敗 (returncode={result.returncode})。\n"
        f"--- STDOUT ---\n{result.stdout}\n"
        f"--- STDERR ---\n{result.stderr}"
    )
