# -*- coding: utf-8 -*-
"""入口批次1其餘 回歸（§7C：EH-03 / MG-01 / MG-02，2026-07-11）。

  EH-03 scheduler.main 的 ScheduleApp 建構失敗只進 log、無可見錯誤 → 包 try+MessageBox+exit。
  MG-01 check_appointment_count 合併休診【前】把活 dict 交給 UI(漏 deepcopy)→ 月曆重繪炸。
  MG-02 自動更新需重啟時無條件 2 秒後硬砍 → 熱鍵自動化中途被切;改閘門等閒置才重啟。

以原始碼守門(entry/worker 段無法 headless 實跑),鎖住不變式防日後被改回。
"""
import inspect
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHED_SRC = ROOT / "src" / "scheduler.py"


# ══ EH-03：ScheduleApp 建構包 try + 可見錯誤框 + 乾淨退出 ═══════════════════════════
def test_eh03_scheduleapp_construction_guarded():
    text = SCHED_SRC.read_text(encoding="utf-8")
    i = text.index("app = ScheduleApp(root)")
    head_line = text[:i].rstrip().splitlines()[-1].strip()
    assert head_line == "try:", "EH-03: ScheduleApp(root) 應緊接在 try: 之後"
    tail = text[i:i + 800]
    assert "except Exception:" in tail, "EH-03: 應攔建構例外"
    assert "MessageBoxW" in tail, "EH-03: 應跳可見錯誤框(非只進 log)"
    assert "sys.exit(1)" in tail, "EH-03: 顯示錯誤後應以非零碼退出"


# ══ MG-01：休診原地合併【前】交給 UI 的 appointments_by_date 必須 deepcopy ═══════════
def test_mg01_preliminary_send_deepcopies_before_inplace_merge():
    src = inspect.getsource(main.check_appointment_count)
    merge_idx = src.index("_merge_dayoff_overrides(appointments_by_date")
    before = src[:merge_idx]
    # 合併前的送出必須是 deepcopy 快照;不可出現原樣活 dict(worker 隨後會原地改寫同一物件)
    assert "data=appointments_by_date" not in before, \
        "MG-01: 原地合併前不可把活 appointments_by_date 交給 UI(要 deepcopy)"
    assert "data=deepcopy(appointments_by_date)" in before, \
        "MG-01: 合併前的預備送出應交 deepcopy 快照"


# ══ MG-02：自動更新重啟走閘門(熱鍵忙碌延後),不再無條件硬砍 ═══════════════════════════
def test_mg02_restart_gate_exists_and_checks_hotkey_state():
    gate = inspect.getsource(main.AutomationApp._restart_when_hotkey_idle)
    assert "_subsystem_running" in gate, "MG-02: 閘門要看 subsystem 是否在跑"
    assert "last_action_time" in gate, "MG-02: 閘門要看距最後一次熱鍵動作多久"
    assert "_UPDATE_RESTART_MAX_DEFER_ATTEMPTS" in gate, "MG-02: 要有延後上限避免永不重啟"
    assert "self._restart_app()" in gate, "MG-02: 閒置/到頂時才真正重啟"


def test_mg02_need_restart_branch_uses_gate_not_direct_restart():
    text = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
    assert "self.root.after(2000, self._restart_when_hotkey_idle)" in text, \
        "MG-02: need_restart 分支應改走閘門"
    assert "self.root.after(2000, self._restart_app)" not in text, \
        "MG-02: 不可再有無條件 2 秒後直接 _restart_app 的硬砍"
