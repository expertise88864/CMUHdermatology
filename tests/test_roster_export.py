# -*- coding: utf-8 -*-
"""匯出：build_export 資料組裝 + Excel/Word 產檔並讀回驗證（重依賴 importorskip）。"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster.export_common import (  # noqa: E402
    default_filename, leaves_summary, member_tally,
)
from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.storage import RosterStorage  # noqa: E402

YM = "2026-08"


def _svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({
        "r_members": [{"id": "A", "name": "甲"}, {"id": "B", "name": "乙"}],
        "vs_members": [{"id": "D", "name": "D醫師"}],
        "points": {"weekday": 1, "weekend": 2, "national_holiday": 1},
        "duty_range_soft": [9, 11],
    })
    st.save_holiday_duty({"r": {date(2026, 8, 15): "A"}, "vs": {}})
    st.save_month(YM, {
        "r_duty": {"2026-08-01": {"person": "A"},
                   "2026-08-03": {"person": "B"}},
        "vs_duty": {"2026-08-01": {"person": "D"}},
        "leaves": {"r": {"A": ["2026-08-10"]}},
    })
    return RosterService(st)


# ─── 純函式 ─────────────────────────────────────────────────────────────────
def test_build_export_structure(tmp_path):
    data = _svc(tmp_path).build_export(YM)
    assert data["year"] == 2026 and data["month"] == 8
    assert data["r"]["duty"][date(2026, 8, 1)] == "A"
    assert data["r"]["names"]["A"] == "甲"
    assert data["vs"]["names"]["D"] == "D"          # VS 用代號
    assert data["r"]["leaves"]["A"] == [date(2026, 8, 10)]
    assert date(2026, 8, 15) in data["holidays"]


def test_member_tally_and_summary(tmp_path):
    data = _svc(tmp_path).build_export(YM)
    tally = member_tally(data["r"], data["holidays"], data["params"])
    assert tally["A"] == {"wd": 0, "we": 1, "pt": 2}   # 8/1 週六
    assert tally["B"] == {"wd": 1, "we": 0, "pt": 1}   # 8/3 週一
    assert "甲(8/10)" in leaves_summary(data["r"])


def test_default_filename():
    data = {"year": 2026, "month": 7}
    assert default_filename(data, ".xlsx") == "115年07月班表.xlsx"


# ─── Excel 產檔讀回 ─────────────────────────────────────────────────────────
def test_export_xlsx_roundtrip(tmp_path):
    pytest.importorskip("openpyxl")
    from openpyxl import load_workbook

    from cmuh_common.roster import export_xlsx
    data = _svc(tmp_path).build_export(YM)
    out = tmp_path / "out.xlsx"
    export_xlsx.export(str(out), data)

    wb = load_workbook(str(out))
    assert "值班表" in wb.sheetnames and "結算" in wb.sheetnames
    ws = wb["值班表"]
    assert "115年08月" in ws["A1"].value
    # 8/1=週六 → 第一週列(row3) 週六欄(F)
    f3 = ws["F3"].value
    assert "R:甲" in f3 and "VS:D" in f3
    # 結算：甲 假日 1 班
    summ = wb["結算"]
    rows = [[c.value for c in row] for row in summ.iter_rows(min_row=2)]
    a_row = [r for r in rows if r[1] == "甲"][0]
    assert a_row[3] == 1 and a_row[5] == 2            # 假日欄=1、點數=2


# ─── Word 產檔讀回 ─────────────────────────────────────────────────────────
def test_export_docx_roundtrip(tmp_path):
    pytest.importorskip("docx")
    from docx import Document

    from cmuh_common.roster import export_docx
    data = _svc(tmp_path).build_export(YM)
    out = tmp_path / "out.docx"
    export_docx.export(str(out), data)

    doc = Document(str(out))
    text = "\n".join(p.text for p in doc.paragraphs)
    assert "115年08月" in text
    assert "醫師請假：" in text and "甲(8/10)" in text
    # 第一週表格：一線(row2)/三線(row3) 的週六欄(col6)
    t0 = doc.tables[0]
    assert t0.cell(2, 6).text == "甲"                # 一線=R
    assert t0.cell(3, 6).text == "D"                 # 三線=VS 代號


def test_export_empty_month_no_crash(tmp_path):
    """空月份（無排班）也能產檔不炸。"""
    pytest.importorskip("openpyxl")
    from cmuh_common.roster import export_xlsx
    st = RosterStorage(str(tmp_path))
    st.save_config({"r_members": [], "vs_members": []})
    data = RosterService(st).build_export(YM)
    export_xlsx.export(str(tmp_path / "empty.xlsx"), data)
    assert (tmp_path / "empty.xlsx").exists()
