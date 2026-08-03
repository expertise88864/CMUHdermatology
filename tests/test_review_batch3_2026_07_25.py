# -*- coding: utf-8 -*-
"""[2026-07-25 完整 code review 第三批] 資源洩漏 / 誤殺 / 壞資料 / PII / 未驗證命令

九項雜項修正的回歸測試（打卡 3、會診 3、排班 2、IMAP 1）。
"""
import inspect
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import autoclock as ac  # noqa: E402
import consult_query as cq  # noqa: E402
from cmuh_common import imap_reader  # noqa: E402
from cmuh_common.roster.model import ClerkBatch  # noqa: E402


def _code_only(src: str) -> str:
    """剝掉註解，只留程式碼——避免源碼守門測試比對到說明文字而誤判。"""
    out = []
    for line in src.splitlines():
        i = line.find("#")
        out.append(line if i < 0 else line[:i])
    return "\n".join(out)


# ── 打卡 ────────────────────────────────────────────────────────────────────
def test_health_declaration_always_returns_to_original_window():
    """失敗發生在切到宣告彈窗【之後】時，driver 會停在彈窗上 → 呼叫端等 execute
    必然逾時、每次重試又多開一個窗。finally 必須收窗並切回原視窗。"""
    src = inspect.getsource(ac.handle_health_declaration)
    assert "finally:" in src, "必須有 finally 收尾"
    tail = src[src.index("finally:"):]
    assert "switch_to.window(orig)" in tail, "finally 必須切回原視窗"
    assert "driver.close()" in tail, "finally 必須關掉多開的宣告視窗"


def test_restart_defers_teardown_until_handover_confirmed():
    """[codex R2] ★破壞性拆解必須延後到「確認新行程存活」之後★。
    舊版在 spawn 前就 running.clear()+停 tray+放 mutex → restart_self 的「新行程早夭就
    保留舊行程」保護 return 回來時，舊行程已被拆光 → 打卡程式整個消失。補一句通知
    並不能解決可用性（何況沒裝 winotify 時連通知都不會出現）。"""
    src = inspect.getsource(ac.restart_program)
    assert "on_confirmed=" in src, "拆解必須交給 restart_self 的 on_confirmed"
    code = _code_only(src)
    i_def = code.index("def _teardown_for_handover")
    i_call = code.index("restart_self(")
    assert i_def < i_call
    # 拆解動作只能出現在 on_confirmed 內（restart_self 呼叫之後不得再有拆解）
    tail = code[i_call:]
    for forbidden in ("running.clear()", "release_single_instance()",
                      "_release_persistent_clock_driver()"):
        assert forbidden not in tail, f"spawn 失敗後不得執行 {forbidden}"
    # restart_self 也要真的支援這個參數
    import inspect as _i
    from cmuh_common.paths import restart_self
    assert "on_confirmed" in _i.signature(restart_self).parameters


def test_task_gate_lease_released_when_thread_start_fails():
    """lease 在 Thread.start() 之前取得；start 拋例外時 _worker 的 finally 不會跑
    → lease 洩漏,該 schedule key 要等 90 分鐘才自癒＝整個打卡窗被靜默跳過。"""
    for fn in (ac._scheduler_tick, ac.run_immediate_test):
        src = inspect.getsource(fn)
        i = src.index(".start()")
        head = src[:i]
        assert "try:" in head, f"{fn.__name__}: start() 應包在 try 內"
        assert ".release(" in src[i:], f"{fn.__name__}: start 失敗必須釋放 lease"


# ── 會診 ────────────────────────────────────────────────────────────────────
def test_close_pids_verifies_identity_and_session():
    """強制結束前要重新確認 ①仍是 systemftp（PID 重用）②同一登入 session
    （共用/RDS 機器上別的使用者也可能開著住院醫囑系統）。"""
    src = inspect.getsource(cq._validated_systemftp_pids)
    assert "SYSTEMFTP_EXE_NAME" in src, "要確認行程名（PID 重用）"
    assert "_pid_session" in src, "要確認登入 session（共用/RDS 機器）"


def test_control_tree_dump_has_no_patient_text():
    """控制項樹 dump 每 ~15 分鐘跑一次；TRadioButton 的文字＝姓名+病房+床號+病歷號,
    不可持續寫進沒有保存期限的 log。只留 class/座標/長度即可。"""
    src = inspect.getsource(cq._extract_consult_text)
    i = src.index("控制項樹(%d 個)")
    seg = src[i:i + 400]
    assert "t={txt[:16]!r}" not in seg, "不得把控制項文字（含病人識別資料）寫進 log"
    assert "len={len(txt)}" in seg, "改記長度即可滿足結構調參需求"


def test_menu_command_id_verified_before_posting():
    """選單 ID 由位置索引 (4,8,0) 推得；院方插一項就會位移。這個 ID 會被 PostMessage
    送進住院醫囑系統 → 與預期不符時必須先核對標題，不可硬送未知命令。"""
    src = inspect.getsource(cq.resolve_menu_command_id)
    assert "_find_menu_id_by_caption" in src, "非預期 ID 必須改依確切標題定位"
    assert "return None" in src, "核對不符要放棄（回 None）"
    for fn in (cq._query_cycle, cq._run_with_sw_hide):
        s2 = inspect.getsource(fn)
        i = s2.index("resolve_menu_command_id")
        assert "cmd_id is None" in s2[i:i + 400], \
            f"{fn.__name__} 必須擋下 None,不可把 None 送進 PostMessage"


def test_menu_caption_normalization():
    """助記符與快捷鍵欄要正規化掉,否則實機標題永遠對不上。"""
    assert cq._normalize_menu_caption("我的會診清單(&M)	Ctrl+M") == "我的會診清單"
    assert cq._normalize_menu_caption("  我的會診清單 ") == "我的會診清單"
    assert cq._normalize_menu_caption(None) == ""


def test_menu_lookup_requires_exact_caption(monkeypatch):
    """[codex R1] ★只比對「含會診」會誤中『全部會診清單』『會診回覆』→ 把【別的命令】
    送進住院醫囑系統。必須以確切標題定位。"""
    menu = {0: "全部會診清單", 1: "會診回覆", 2: "我的會診清單"}

    class _U:
        def GetMenuItemCount(self, _h):
            return max(menu) + 1 if menu else 0

        def GetMenuStringW(self, _h, i, buf, _n, _f):
            idx = int(getattr(i, "value", i))      # 實際傳進來的是 ctypes.c_uint
            if idx not in menu:
                return 0
            buf.value = menu[idx]
            return len(buf.value)

    class _W:
        user32 = _U()

    monkeypatch.setattr(cq.ctypes, "windll", _W())
    monkeypatch.setattr(cq.win32gui, "GetMenuItemID", lambda _h, i: 500 + i)
    assert cq._find_menu_id_by_caption(1, cq.MENU_CAPTION_EXPECTED) == 502,         "應找到『我的會診清單』(index 2)，不得被其他含『會診』的項目誤中"

    menu.pop(2)                                   # 目標項不存在 → 必須放棄
    assert cq._find_menu_id_by_caption(1, cq.MENU_CAPTION_EXPECTED) is None


# ── 排班 ────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", [
    {"id": "b1", "members": ["C1"]},                    # 缺 start_monday
    {"id": "b2", "start_monday": "", "members": []},    # 空字串
    {"id": "b3", "start_monday": "2026-8-3"},           # 非 ISO 格式
    {"id": "b4", "start_monday": None},
])
def test_clerk_batch_bad_data_returns_none_not_raises(bad):
    """壞梯次是【設計內預期】(多機人工解 JSON 衝突)；設定頁特地容忍它讓使用者自救,
    但舊版一進 build_day_input 就拋例外 → PGY/Clerk 分頁整個畫不出來。"""
    assert ClerkBatch.from_dict(bad) is None


def test_clerk_batch_good_data_still_parses():
    b = ClerkBatch.from_dict({"id": "ok", "start_monday": "2026-08-03",
                              "members": ["C1", "C2"]})
    assert b is not None and b.start_monday == date(2026, 8, 3)
    assert b.members == ["C1", "C2"]


def test_resettle_ignores_non_month_keys(tmp_path):
    """跨月殘留鍵會虛增點數 → fair_share 與每人 delta 一起偏掉，錯帳還會結轉到下個月。
    build_export / biopsy / day_stats 都有這道過濾，只有真正寫 ledger 的這條漏了。"""
    from cmuh_common.roster.service import RosterService
    from cmuh_common.roster.storage import RosterStorage
    st = RosterStorage(str(tmp_path))
    st.save_config({"r_members": [{"id": "A", "name": "甲"}],
                    "points": {"weekday": 1, "weekend": 2,
                               "national_holiday": 1}})
    svc = RosterService(st)
    month = st.load_month("2026-08")
    month["r_duty"] = {
        "2026-08-03": {"person": "A"},      # 本月週一 = 1 點
        "2026-09-01": {"person": "A"},      # 跨月殘留鍵 → 不可計入
    }
    st.save_month("2026-08", month)
    pts = svc.resettle_from_duty("r", "2026-08")
    assert pts["A"] == 1, f"跨月鍵不得計入本月結算：{pts}"


# ── IMAP ────────────────────────────────────────────────────────────────────
def test_imap_scan_is_capped():
    """中文關鍵字必走後備路徑＝每封未讀信一次 FETCH。未讀累積數百封時單輪會超過
    IMAP_HARD_TIMEOUT → 每輪被強制關 socket → 冷卻循環 → email 觸發實質永久失效。"""
    assert isinstance(imap_reader._MAX_SCAN_IDS, int)
    assert 0 < imap_reader._MAX_SCAN_IDS <= 200
    src = inspect.getsource(imap_reader)
    assert "ids[-_MAX_SCAN_IDS:]" in src, "應只掃最新 N 封（SEARCH 序號遞增→取尾端）"


# ── codex deep 第一輪 findings 的回歸測試 ──────────────────────────────────
def test_close_pids_validates_before_wm_close(monkeypatch):
    """[codex R1] ★驗證必須在【送 WM_CLOSE 之前】：舊版先對快照 PID 的所有視窗送
    WM_CLOSE,才在 terminate 前檢查身分 → 別人的住院醫囑系統早就被關掉了。"""
    src = inspect.getsource(cq.close_pids)
    i_valid = src.index("_validated_systemftp_pids")
    i_close = src.index("find_windows")
    assert i_valid < i_close, "驗證要在列舉/關閉視窗之前"


def test_validated_pids_fail_closed_without_session(monkeypatch):
    """取不到自己的 session → 整批不動（與 _cleanup_orphan_systemftp 一致）。"""
    monkeypatch.setattr(cq, "_pid_session", lambda _p: None)
    assert cq._validated_systemftp_pids({123, 456}) == set()


def test_validated_pids_filters_reuse_and_other_session(monkeypatch):
    class _P:
        def __init__(self, pid):
            self.pid = pid

        def name(self):
            return {1: "systemftp.exe", 2: "notepad.exe",
                    3: "systemftp.exe"}[self.pid]
    monkeypatch.setattr(cq.psutil, "Process", _P)
    monkeypatch.setattr(cq.os, "getpid", lambda: 99)
    # 自己=session 1；pid1 同 session、pid2 名稱不符、pid3 別的 session
    monkeypatch.setattr(cq, "_pid_session",
                        lambda p: {99: 1, 1: 1, 2: 1, 3: 2}[p])
    assert cq._validated_systemftp_pids({1, 2, 3}) == {1}


def test_health_declaration_closes_only_new_windows():
    """[codex R1] ①切窗失敗時 current 仍是 orig,彈窗卻已存在 → 不可只在
    current != orig 時才清理；②只關相對 wins_before 新增的窗,不可關掉所有非 orig。"""
    src = inspect.getsource(ac.handle_health_declaration)
    tail = src[src.index("finally:"):]
    assert "w not in wins_before" in tail, "只能關這次新開的視窗"
    assert "if driver.current_window_handle != orig:" not in tail, \
        "不可用 current!=orig 當清理的前置條件（切窗失敗時會漏掉）"


@pytest.mark.parametrize("bad", [
    None, "batch", 123,
    {"id": "b", "start_monday": "2026-08-03", "members": "C12"},
])
def test_clerk_batch_structural_corruption_rejected(bad):
    """[codex R1] 合法 JSON 但結構壞掉：整筆是 null/字串 → 舊版 AttributeError 未被接住；
    members 是字串 → 逐字元展開成 ['C','1','2'],安靜產生錯誤名單（比拋例外更糟）。"""
    assert ClerkBatch.from_dict(bad) is None


def test_load_clerk_batches_skips_non_dict_entries(tmp_path):
    """非 dict 項在 load 的排序階段就會讓 b.get 拋 AttributeError（比 from_dict 更早）。"""
    import json
    from cmuh_common.roster.storage import RosterStorage
    st = RosterStorage(str(tmp_path))
    (tmp_path / "clerk_batches.json").write_text(json.dumps({
        "batches": [None, "junk",
                    {"id": "ok", "start_monday": "2026-08-03",
                     "members": ["C1"]}],
    }), encoding="utf-8")
    got = st.load_clerk_batches()
    assert len(got) == 1 and got[0]["id"] == "ok"
