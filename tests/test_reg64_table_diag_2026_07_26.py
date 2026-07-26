# -*- coding: utf-8 -*-
"""[2026-07-26 main.py 未審區段] reg64 病人清單表格缺失的診斷紀錄。

`_fetch_clinic_light` 用寫死的 `bgcolor='#fffff0'` 找病人清單表格。若網站改版,
table 會是 None → total/waiting/completed 全部維持 0,而回傳 status 仍是「更新成功」
→ 畫面顯示「現場 0 人等待」,醫師看到的是一個【空診間】而不是「讀取失敗」。

★本次刻意【不改行為】,只留證據★:我不知道這個頁面在「今天真的還沒有病人」時會不會
渲染這張表。若不會,把「表格不存在」當成改版就會在最需要顯示的時刻誤報 ——
那正是 2026-07-16 打卡 portal 撤回過的錯誤(commit d9f38be),我今天也重蹈過一次。
"""
import inspect
import logging
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402


def _code_only(src: str) -> str:
    src = re.sub(r'("""|\'\'\')(?:.|\n)*?\1', "", src)
    return "\n".join(line.split("#")[0] for line in src.splitlines())


def test_logs_once_per_room_per_day(caplog):
    main._REG64_TABLE_MISSING_LOGGED.clear()
    with caplog.at_level(logging.WARNING):
        for _ in range(4):
            main._log_reg64_table_missing_once("5A", True, False, False)
        main._log_reg64_table_missing_once("5B", False, False, True)
    hits = [r.getMessage() for r in caplog.records if "找不到病人清單表格" in r.getMessage()]
    assert len(hits) == 2, f"每個診間每天一次(兩個診間=2 則),實際 {len(hits)}"
    main._REG64_TABLE_MISSING_LOGGED.clear()


def test_log_records_the_discriminating_fact(caplog):
    """★關鍵鑑別資訊★ 燈號錨點有沒有出現:有=頁面是對的但表格不見了(可疑);
    沒有=多半根本不是該頁面。沒記這個,日後看到 log 也無法判斷。"""
    main._REG64_TABLE_MISSING_LOGGED.clear()
    with caplog.at_level(logging.WARNING):
        main._log_reg64_table_missing_once("5A", True, False, False)
    msg = [r.getMessage() for r in caplog.records][-1]
    assert "燈號錨點=有" in msg
    assert "5A" in msg
    assert "請把這行提供給開發者" in msg, "要能讓使用者知道該回報"
    main._REG64_TABLE_MISSING_LOGGED.clear()


def test_behaviour_unchanged_only_logging_added():
    """★不可趁機改行為★ 這次只加診斷。回傳的欄位與狀態字串都不能動 ——
    在沒有實機證據前把「表格不存在」升級成錯誤狀態,可能在最需要顯示的時刻誤報。"""
    code = _code_only(inspect.getsource(main.AutomationApp.fetch_clinic_light_status))
    i_tbl = code.index("table = soup.find('table'")
    i_total = code.index("total_count = 0")
    seg = code[i_tbl:i_total]
    assert "_log_reg64_table_missing_once(" in seg, "要留診斷"
    assert "return" not in seg, "不可在此提早返回(那就是改行為了)"
    assert '"status"' not in seg, "不可改動狀態字串"


def test_helper_is_module_level_not_inside_class():
    """踩過的坑:那行 `# --- 門診燈號更新迴圈 ---` 註解在第 0 欄、但後面的 def 是縮排的
    (仍在 class 內)。把模組級函式插在那裡會讓後續所有方法脫離 class —— 全套 62 個測試轉紅。"""
    assert not hasattr(main.AutomationApp, "_log_reg64_table_missing_once")
    assert callable(main._log_reg64_table_missing_once)
    # class 的關鍵方法必須還在 class 上
    for name in ("_update_clinic_lights_loop", "update_single_clinic_ui",
                 "save_all_settings"):
        assert hasattr(main.AutomationApp, name), f"{name} 不在 class 上了"


def test_high_signal_case_is_not_suppressed_by_earlier_low_signal(caplog):
    """★外審★ 蒐證的重點就是「看診中、有燈號錨點、表格卻不見」這個高訊號案例。
    同一診間清晨常因未開診而合法缺表(低訊號),若那筆先佔掉當天額度,
    真正有價值的案例就永遠不會被記下 —— 這個診斷會專門漏掉它該抓的東西。"""
    main._REG64_TABLE_MISSING_LOGGED.clear()
    with caplog.at_level(logging.WARNING):
        # 清晨:未開診、沒有燈號錨點(低訊號)
        main._log_reg64_table_missing_once("5A", False, False, True)
        # 稍後:看診中、有錨點、表格仍不見(高訊號)→ 必須另外記一次
        main._log_reg64_table_missing_once("5A", True, False, False)
        # 高訊號本身仍要節流
        main._log_reg64_table_missing_once("5A", True, False, False)
    hits = [r.getMessage() for r in caplog.records if "找不到病人清單表格" in r.getMessage()]
    assert len(hits) == 2, f"低訊號 1 + 高訊號 1,實際 {len(hits)}"
    assert any("訊號=high" in m for m in hits), "高訊號案例必須被記下"
    assert any("訊號=low" in m for m in hits)
    main._REG64_TABLE_MISSING_LOGGED.clear()
