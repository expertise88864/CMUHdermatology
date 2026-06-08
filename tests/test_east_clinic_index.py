# -*- coding: utf-8 -*-
"""[perf r5] 東區休診推論索引等價性測試。

_update_grid_data 原本每格×每醫師×每時段呼叫 _doctor_has_other_ext_on_weekday 重掃整份
all_doctors_data(月曆 UI 熱路徑)。改為每次 refresh 只建一次 (lookup,weekday,session)->set
索引、O(1) 查詢。本測試對「索引查詢」與「保留的原方法」做窮舉差分，證明語意完全等價
(doc_no/doc_name 兩鍵聯集、isinstance(date) 過濾、dict 與舊式 str、排除當日)。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402

D_MON1 = date(2026, 6, 1)   # Monday   (weekday 0)
D_MON2 = date(2026, 6, 8)   # Monday   (weekday 0)
D_TUE = date(2026, 6, 2)    # Tuesday  (weekday 1)
D_WED = date(2026, 6, 3)    # Wednesday(weekday 2)


def _make_data():
    return {
        # 同一醫師同時以 doc_no 與 doc_name 為鍵(實機常見：快取用 doc_no、即時用姓名)
        "D100": {
            D_MON1: [{"session": "上午", "ext_branch": "east"}],   # east 週一上午 (dict)
            D_MON2: ["上午:7|Ext:east"],                            # east 週一上午 (舊式 str)
            D_TUE: [{"session": "下午"}],                           # 非 east 週二下午
        },
        "陳醫師": {
            D_WED: [{"session": "下午", "is_ext": True}],           # east 週三下午 (is_ext→east)
        },
        "D200": {
            D_MON1: [{"session": "晚上", "ext_branch": "east"}],    # east 週一晚上，唯一一筆
        },
        "error_doc": {"error": "network"},                          # error dict → 應跳過
        "weird": {"notadate": [{"session": "上午", "ext_branch": "east"}]},  # 非 date 鍵 → 跳過
    }


def _old_method_obj(data):
    app = main.AutomationApp.__new__(main.AutomationApp)
    app.all_doctors_data = data
    return app


def test_index_equivalent_to_old_method_exhaustive():
    data = _make_data()
    parse = main.AutomationApp._appt_item_session_ext
    index = main._build_east_weekday_index(data, parse)
    app = _old_method_obj(data)

    doc_pairs = [
        ("D100", "陳醫師"),
        ("D200", "D200name"),
        ("error_doc", "error_doc"),
        ("nope", "nope"),
        ("weird", "weird"),
    ]
    excludes = [None, D_MON1, D_MON2, D_TUE, D_WED]

    mismatches = []
    saw_true = False
    for dno, dnm in doc_pairs:
        for wd in range(7):
            for s in ("上午", "下午", "晚上"):
                for ex in excludes:
                    got = main._east_index_has_other(index, dno, dnm, wd, s, ex)
                    exp = app._doctor_has_other_ext_on_weekday(dno, dnm, wd, s, ex)
                    if got != exp:
                        mismatches.append((dno, dnm, wd, s, ex, got, exp))
                    saw_true = saw_true or got

    assert not mismatches, f"索引與原方法不等價: {mismatches[:5]}"
    assert saw_true, "測試資料未涵蓋任何 True 情境(差分無意義)"


def test_index_specific_semantics():
    """鎖定幾個關鍵語意，避免日後誤改。"""
    data = _make_data()
    parse = main.AutomationApp._appt_item_session_ext
    index = main._build_east_weekday_index(data, parse)

    # 週一上午有兩個 east 日期(D_MON1/D_MON2)，排除其一仍 True
    assert main._east_index_has_other(index, "D100", "x", 0, "上午", None) is True
    assert main._east_index_has_other(index, "D100", "x", 0, "上午", D_MON1) is True

    # 兩鍵聯集：週三下午 east 在「陳醫師」鍵下，用 doc_no="D100" 查也要 True
    assert main._east_index_has_other(index, "D100", "陳醫師", 2, "下午", None) is True

    # D200 週一晚上只有一筆 east，排除當日 → False
    assert main._east_index_has_other(index, "D200", "x", 0, "晚上", D_MON1) is False
    assert main._east_index_has_other(index, "D200", "x", 0, "晚上", None) is True

    # 非 east(週二下午) → False；error dict / 非 date 鍵被跳過 → 不入索引
    assert main._east_index_has_other(index, "D100", "x", 1, "下午", None) is False
    assert main._east_index_has_other(index, "error_doc", "error_doc", 0, "上午", None) is False
    assert main._east_index_has_other(index, "weird", "weird", 0, "上午", None) is False


# === [r5] 延後載入網路相依的行為測試 ===

def test_network_imports_bootstrap_populates_globals():
    """import main 時 requests 為 None 佔位；_ensure_network_imports 後填入真模組(冪等)。"""
    assert callable(main._ensure_network_imports)
    main._ensure_network_imports()
    assert main.requests is not None and main.requests.__name__ == "requests"
    assert main.BeautifulSoup is not None
    assert main.HTTPAdapter is not None and main.Retry is not None
    main._ensure_network_imports()  # 冪等不報錯
    assert main._network_imports_ready is True
