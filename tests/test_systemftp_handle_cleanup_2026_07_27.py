# -*- coding: utf-8 -*-
"""[2026-07-27 實機故障根因] 隱藏桌面 systemftp 殘留累積 → 資源耗盡 → 整個早上收不到會診。

實機時間線(2026-07-27):
  06:27  (8,'EnumWindows','記憶體資源不足') / can't start new thread /
         (1450,'CreateProcess','系統資源不足')      ← desktop heap、handle 耗盡
  06:42~ 「等不到登入視窗(systemftp 疑似已達『最多兩個』上限)」持續失敗到 08:17
  06:48  連續失敗 3 次 → 寄健康告警信給團隊
  08:25  使用者手動重啟 → 08:40 第一次就成功

根因:`_automation_on_hidden` 把 `win32process.CreateProcess` 的回傳值【整個丟掉】,
於是不知道自己開的是哪個 PID,只能靠前後快照猜。猜錯/驗證失敗(log 實際出現
「[cleanup] pid 5388 已非 systemftp(PID 重用?),略過」)就跳過不殺 →
每輪輪詢留一個在隱藏桌面。而孤兒清掃 `_cleanup_orphan_systemftp` 用【視窗列舉】
做正面識別,資源耗盡時它自己也失效 —— 復原機制被它要修的狀況弄壞。
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _src():
    path = os.path.join(os.path.dirname(__file__), "..", "src", "consult_query.py")
    return open(path, encoding="utf-8").read()


def _code_only(src: str) -> str:
    src = re.sub(r'("""|\'\'\')(?:.|\n)*?\1', "", src)
    return "\n".join(line.split("#")[0] for line in src.splitlines())


def _hidden_fn() -> str:
    src = _src()
    i = src.index("def _automation_on_hidden(")
    j = src.index("\ndef _run_with_sw_hide(", i)
    return _code_only(src[i:j])


def test_create_process_return_value_is_kept():
    """★根因★ 回傳值不可再被丟掉 —— 不知道自己開了哪個 PID,清理就只能用猜的。"""
    code = _hidden_fn()
    assert "_hproc, _hthread, _spawned_pid, _ = win32process.CreateProcess(" in code, \
        "要留住 handle 與 pid"
    assert "our_pids: set = {_spawned_pid}" in code, \
        "自己開的 PID 要直接納入,不可只靠快照差集"


def test_handle_based_termination_is_the_last_resort():
    """★關鍵性質★ 用 handle 終止:①handle 沒關 → 核心不回收該 PID → 不可能誤殺
    被重用 PID 的別人;②handle 來自我們自己的 CreateProcess → 必然是我們開的那個,
    不需要名稱/session/視窗列舉,資源耗盡時照樣有效。"""
    code = _hidden_fn()
    # 注意:函式內有兩個 finally(清理區段、以及關 handle 的內層),
    # 要取【含 cleanup_pids 的那一個】,不是 rindex 找到的最後一個。
    i_fin = code.index("cleanup_pids = our_pids")
    tail = code[i_fin:]
    assert "WaitForSingleObject(_hproc, 0)" in tail, "要先確認它是否仍在執行"
    assert "TerminateProcess(_hproc, 1)" in tail, "仍在執行就用 handle 強制結束"
    i_close = tail.index("close_pids(")
    i_term = tail.index("TerminateProcess(_hproc")
    assert i_close < i_term, "優雅關閉優先,handle 強制結束是最後保險"


def test_handles_are_closed():
    """handle 不關的話核心會一直保留該 PID(而且我們自己也在洩漏 handle)。"""
    code = _hidden_fn()
    tail = code[code.index("cleanup_pids = our_pids"):]
    assert "for _h in (_hthread, _hproc):" in code
    assert "_h.Close()" in code
    assert "_h.Close()" in tail, "關 handle 要在同一個清理區段內"


def test_fallback_snapshot_path_retained():
    """不可矯枉過正:快照差集的後備路徑仍要在(handle 只是多一道保險)。"""
    code = _hidden_fn()
    assert "cleanup_pids = our_pids or (_systemftp_pids() - before)" in code


def test_orphan_sweep_still_uses_positive_identification():
    """既有的隱藏桌面正面識別不可被這次改動弄掉 —— 它負責清「前世」留下的孤兒
    (本次改動只保證【本次】開的那個一定會被收掉)。"""
    src = _src()
    i = src.index("def _cleanup_orphan_systemftp(")
    seg = _code_only(src[i:i + 2000])
    assert "_hidden_desktop_pids()" in seg
    assert "_pid_session(os.getpid())" in seg
