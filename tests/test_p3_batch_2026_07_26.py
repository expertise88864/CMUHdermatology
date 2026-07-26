# -*- coding: utf-8 -*-
"""[2026-07-26 審查] 收尾批:靜默失敗、初始化未隔離、無上限的自動重啟。"""
import inspect
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import health  # noqa: E402


def _code_only(src: str) -> str:
    src = re.sub(r'("""|\'\'\')(?:.|\n)*?\1', "", src)
    return "\n".join(line.split("#")[0] for line in src.splitlines())


def test_code_input_warns_when_nothing_happened():
    """★靜默失敗★ 醫師按了 F1-F5 卻完全沒有東西發生(HIS 改版讓選單 id 位移就是這條),
    舊版只寫 log —— 醫師會以為醫令已經下了。熱鍵的價值就在「按了就會下」。"""
    import main
    code = _code_only(inspect.getsource(main._script_code_input_adaptive))
    # 兩條「什麼都沒發生」的路徑都要跳警告:選單命令送不出去、等不到可信焦點
    i_menu = code.index("代碼輸入 menu command 送出失敗")
    i_focus = code.index("等不到可信的代碼輸入焦點")
    assert "_show_uvb_warning(" in code[i_menu:i_menu + 600], "選單命令失敗要警告醫師"
    assert "_show_uvb_warning(" in code[i_focus:i_focus + 900], "等不到焦點要警告醫師"
    # ★外審 R2★ F1-F3 的呼叫端已有「半套狀態」警告(會說明 UVB 已被改、該怎麼收拾),
    # 更重要。這裡再跳一個通用視窗只會擋在前面延後它,程式若在第一個視窗開著時結束
    # 更會讓它完全不出現 → 那三支要傳 warn_on_silent_failure=False。
    assert "warn_on_silent_failure" in code
    for name in ("script_F1_adaptive", "script_F2_adaptive", "script_F3_adaptive"):
        caller = _code_only(inspect.getsource(getattr(main, name)))
        assert "warn_on_silent_failure=False" in caller, f"{name} 不可重複跳窗"
    for name in ("script_F4_adaptive", "script_F5_adaptive"):
        caller = _code_only(inspect.getsource(getattr(main, name)))
        assert "warn_on_silent_failure" not in caller, f"{name} 要保留警告(預設 True)"


def test_deferred_initialization_isolates_each_step():
    """★核心功能整個消失★ 原本是一條直線:前面一步拋例外,熱鍵模組載入就不會執行 ——
    使用者只會看到「按 F1 沒反應」,而畫面一切正常。"""
    import main
    code = _code_only(inspect.getsource(main.AutomationApp.deferred_initialization))
    assert "def _step(" in code and "logging.exception" in code
    for step in ("self.start_background_tasks", "self._start_hotkey_module_loading"):
        assert '_step("' in code and step in code, f"{step} 要走隔離包裝"
    # 不可再有裸呼叫(沒被 _step 包住)
    for line in code.splitlines():
        st = line.strip()
        if st.startswith("self.start_background_tasks(") or \
           st.startswith("self._start_hotkey_module_loading("):
            raise AssertionError(f"仍有未隔離的裸呼叫:{st}")


def test_health_auto_restart_has_backoff_and_cap():
    """★無限重試★ 舊版重啟失敗後不重置連續計數 → 每個 tick(5 分鐘)都再試一次,而且永遠
    不停:spawn 一直失敗(磁碟滿/防毒擋/路徑壞)時會不斷產生半死的子行程、洗爆 log,
    問題卻一點都沒解決。"""
    assert health._MAX_AUTO_RESTART_ATTEMPTS >= 1
    code = _code_only(inspect.getsource(health._health_loop))
    i_cb = code.index("restart_callback()")
    tail = code[i_cb:i_cb + 2000]
    assert "consecutive_critical_ram = 0" in tail, \
        "失敗後要歸零,否則下一個 tick 立刻又重試"
    assert "restart_attempts" in tail and "_MAX_AUTO_RESTART_ATTEMPTS" in tail
    assert "auto_restart_on_crit = False" in tail, "達上限要停止自動重啟"
    # 停止後仍要保留本行程(RAM 高雖差,但活著比消失好 —— 既有取捨不可被改掉)。
    # 只看【有 callback】那一段:後面的 `else: os._exit(1)` 是「沒有 callback、
    # 由外層 watchdog 接手」的既有分支,那條本來就該 os._exit。
    cb_branch = tail[:tail.index("                    else:")]
    assert "os._exit" not in cb_branch, "有 callback 的路徑不可殺掉本行程"
    # ★外審 R3★ RAM 回到正常代表這一波 critical 事件結束 → 重試計數要歸零。
    # 不歸零的話,三次【互不相關】的偶發失敗就會永久關掉自動重啟,而 log 寫著「連續失敗」。
    i_ok = code.index('RAM=%.0fMB OK')
    head = code[:i_ok]
    assert "restart_attempts = 0" in head[head.rindex("elif"):],         "RAM 正常的分支要把重試計數歸零"


def test_pitch_uses_smallest_gap_not_median():
    """★外審★ OCR 漏列只會讓相鄰差變成真實列距的整數倍,【不會】變小;中位數在偶數筆
    還會取到較大的那個。實例:辨識列 70/101/163(真實列距≈31)→ diffs=[31,62] →
    舊寫法取 62 → 門檻膨脹成 1.8×62,第二列與表頭距離 66 反而通過守衛 →
    漏讀最上面那張(當次)卡時,底下的舊卡號會被當成現在的卡寫進 HIS。"""
    from cmuh_common import ditto_card_ocr as d
    assert d._estimate_pitch([70, 101, 163]) == 31, "要取最小相鄰間距"
    # 守衛必須擋下:第二列(101)距表頭 97 > 1.8×31=55.8
    assert d._top_row_near_header([101, 163], [70, 101, 163], header_y=4) is False
    # 第一列有被讀到時照常放行(不可誤擋)
    assert d._top_row_near_header([35], [35, 66, 97], header_y=4) is True


def test_ledger_recorded_before_blocking_warning():
    """★外審★ `_show_uvb_warning` 是同步 MessageBoxW。稽核排在它後面 → 紀錄時間變成
    「醫師關掉視窗的時間」,而且對話框開著時程式被關閉/強制重啟,那筆紀錄根本不會產生。
    帳本契約是「動作發生的當下」。"""
    import main
    code = _code_only(inspect.getsource(main._script_code_input_adaptive))
    i_focus = code.index("等不到可信的代碼輸入焦點")
    seg = code[i_focus:i_focus + 900]
    i_ledger = seg.index("_record_his_action(")
    i_warn = seg.index("_show_uvb_warning(")
    assert i_ledger < i_warn, "稽核必須在阻塞對話框之前入列"
