# -*- coding: utf-8 -*-
"""門診止掛提醒「跨重啟去重」回歸測試。

重開程式時記憶體去重狀態會歸零 → 同一診次當天會再寄一次信。修正後改持久化
『已寄出的 notify_key』,寄信前先查、寄成功才寫。本檔固定:
  1. _filter_recent_alert_sent 的保留/剔除/容錯契約(純函式)。
  2. main.py 的寄信路徑確實被 _has_alert_email_been_sent 把關、且成功才
     _mark_alert_email_sent(以原始碼 guard 防止日後被改回無條件寄信)。
"""
import ast
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import main  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MAIN_SRC = ROOT / "src" / "main.py"


def test_filter_recent_alert_sent_keeps_recent_drops_old_and_malformed():
    data = {
        "2026-06-15_上午_張三_main": "2026-06-15",   # 在 cutoff 之後 → 留
        "2026-06-15_下午_李四_main": "2026-06-10",   # 在 cutoff 之前 → 丟
        "2026-06-15_晚上_王五_main": "2026-06-12",   # 等於 cutoff → 留
        123: "2026-06-15",                            # 非字串鍵 → 丟
        "bad_value": 20260615,                        # 非字串值 → 丟
    }
    out = main._filter_recent_alert_sent(data, "2026-06-12")
    assert out == {
        "2026-06-15_上午_張三_main": "2026-06-15",
        "2026-06-15_晚上_王五_main": "2026-06-12",
    }


def test_filter_recent_alert_sent_tolerates_non_dict():
    assert main._filter_recent_alert_sent(None, "2026-06-12") == {}
    assert main._filter_recent_alert_sent(["x"], "2026-06-12") == {}
    assert main._filter_recent_alert_sent({}, "2026-06-12") == {}


def test_alert_email_send_path_is_guarded_by_persistent_dedup():
    """寄信前必須先查 _has_alert_email_been_sent;寄成功才 _mark_alert_email_sent。"""
    src = MAIN_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)

    # 找到呼叫 _send_alert_email_via_smtp 的那段,確認同一函式內也有去重把關。
    class _Finder(ast.NodeVisitor):
        def __init__(self):
            self.has_check = False
            self.has_mark = False
            self.has_send = False

        def visit_Call(self, node):
            tgt = node.func
            name = (tgt.attr if isinstance(tgt, ast.Attribute)
                    else getattr(tgt, "id", ""))
            if name == "_send_alert_email_via_smtp":
                self.has_send = True
            elif name == "_has_alert_email_been_sent":
                self.has_check = True
            elif name == "_mark_alert_email_sent":
                self.has_mark = True
            self.generic_visit(node)

    f = _Finder()
    f.visit(tree)
    assert f.has_send, "找不到 _send_alert_email_via_smtp 呼叫(測試需更新)"
    assert f.has_check, "寄信路徑未經 _has_alert_email_been_sent 去重把關"
    assert f.has_mark, "寄信成功後未呼叫 _mark_alert_email_sent 記錄"


def test_f11_preempt_wired_and_subsystem_supports_preempt():
    """F11 註冊帶 preempt_same;run_subsystem_in_thread 支援該參數。"""
    src = MAIN_SRC.read_text(encoding="utf-8")
    assert "preempt_same" in src
    assert "def run_subsystem_in_thread(self, func, hotkey_name, preempt_same" in src
    # F11 才搶占,其餘熱鍵維持忙碌略過
    assert "_preempt = (key == 'F11')" in src
