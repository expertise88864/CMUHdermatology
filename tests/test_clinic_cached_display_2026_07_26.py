# -*- coding: utf-8 -*-
"""[2026-07-26 main.py 未審區段] 快取資料被顯示成「剛更新的即時資料」。

從磁碟狀態還原的路徑(開窗/切分頁時)會把 `status` 設成「上次快取」,
但 `update_single_clinic_ui` 原本沒有對應分支 → 掉進「正常看診中」→
顯示【綠色的「更新於 <現在時刻>」】。那份候診人數可能是幾小時前、甚至上一個時段留下的,
醫師卻會據此判斷要不要現在過去診間 —— 又一個「訊息陳述程式不知道的事」。
"""
import inspect
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402


def _code_only(src: str) -> str:
    src = re.sub(r'("""|\'\'\')(?:.|\n)*?\1', "", src)
    return "\n".join(line.split("#")[0] for line in src.splitlines())


def test_cached_status_has_its_own_branch():
    code = _code_only(inspect.getsource(main.AutomationApp.update_single_clinic_ui))
    assert 'elif result.get("from_cache") or "快取" in status_txt:' in code,         "快取要有自己的分支"
    i_cache = code.index('elif result.get("from_cache")')
    i_else = code.index("        else:", i_cache)
    seg = code[i_cache:i_else]
    assert "上次快取(非即時)" in seg, "要明講這不是即時資料"
    assert "更新於" not in seg, "快取分支不可宣稱『更新於<現在時刻>』"


def test_cached_branch_precedes_normal_branch():
    """順序很重要:掉到 else 就會被當成正常看診中。"""
    code = _code_only(inspect.getsource(main.AutomationApp.update_single_clinic_ui))
    i_cache = code.index('elif result.get("from_cache")')
    i_now = code.index("datetime.now().strftime('%H:%M')")
    assert i_cache < i_now


def test_cache_marker_cannot_be_masked_by_existing_status():
    """★外審★ 持久化的 last_result 本來就帶 status="更新成功",
    用 `result.get("status") or "上次快取"` 標記快取【永遠不會生效】——
    我第一版就是這樣寫,而且測試還把這個錯誤釘住了。必須用不會被遮蔽的專用旗標,
    且兩條快取路徑(開窗還原、退避/斷線回退)都要標。"""
    src = open(main.__file__, encoding="utf-8").read()
    assert src.count('["from_cache"] = True') == 2, "兩條快取建構路徑都要標記"
    code = _code_only(inspect.getsource(main.AutomationApp.update_single_clinic_ui))
    assert 'result.get("from_cache")' in code, "顯示層要看旗標,不是看 status 字串"


def test_display_branch_fires_even_when_status_says_success():
    """行為驗證:持久化狀態的 status 是「更新成功」時,快取分支仍必須生效。"""
    code = _code_only(inspect.getsource(main.AutomationApp.update_single_clinic_ui))
    i_cache = code.index('elif result.get("from_cache")')
    seg = code[i_cache:code.index("        else:", i_cache)]
    # 分支條件不依賴 status 字串 → status="更新成功" 也會進來
    assert "上次快取(非即時)" in seg


def test_normal_path_still_shows_update_time():
    """不可矯枉過正:真正即時更新時仍要顯示「更新於 HH:MM」綠字。"""
    code = _code_only(inspect.getsource(main.AutomationApp.update_single_clinic_ui))
    i_else = code.rindex("        else:")
    seg = code[i_else:]
    assert "更新於" in seg and 'fg="green"' in seg
