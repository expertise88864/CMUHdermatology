# -*- coding: utf-8 -*-
"""中國醫皮膚科會診查詢程式 — 啟動器（雙擊執行）。實際邏輯在 src/consult_query.py。"""
import os
import runpy
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

runpy.run_path(os.path.join(_SRC, "consult_query.py"), run_name="__main__")
