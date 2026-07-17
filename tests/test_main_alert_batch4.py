# -*- coding: utf-8 -*-
"""批次 4:主程式止掛提醒 MN-01~06 回歸測試。

止掛通知子系統的核心邏輯藏在 main.py 深層巢狀 closure(_notify_worker /
guarded_run_update / run_update 尾端),無法直接呼叫;沿用本 repo 既有慣例
(見 test_alert_email_dedup.py)——能抽成純函式者做行為測試,其餘以 AST/原始碼
守門鎖住修正、防日後回歸。

MN-01 email 先寄再跳通知(通知改非阻塞 winotify,失敗才 fallback MessageBox)
MN-02 第二次提醒:前次寄失敗者補寄一次
MN-03 半夜 00-07 點放慢輪詢(180-300s)——純函式 _clinic_refresh_seconds
MN-04 已寄記錄保留期 7→21 天(涵蓋總覽 13 天視窗)
MN-05 guarded_run_update 例外要記 log,不再靜默吞掉
MN-06 DND 第二次提醒(已寄過)狀態文字不再誤導「僅寄 email」
"""
import ast
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import main  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MAIN_SRC = ROOT / "src" / "main.py"


# ─── 共用:抽出指定名稱的 FunctionDef 節點 ──────────────────────────────────

def _find_funcdef(name: str) -> ast.FunctionDef:
    tree = ast.parse(MAIN_SRC.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"找不到函式 {name}(測試需更新)")


def _call_names_in(node: ast.AST):
    """回傳 (name, lineno) list,涵蓋節點內所有函式呼叫(屬性或裸名)。"""
    out = []
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            tgt = n.func
            name = (tgt.attr if isinstance(tgt, ast.Attribute)
                    else getattr(tgt, "id", ""))
            out.append((name, n.lineno))
    return out


# ─── MN-03:半夜輪詢節流(純函式) ─────────────────────────────────────────

def test_clinic_refresh_seconds_night_is_throttled():
    """00-07 點間隔落在 180-300s;白天 45-75s。多跑幾次涵蓋 random 範圍。"""
    for hour in range(0, 7):
        for _ in range(50):
            s = main._clinic_refresh_seconds(hour)
            assert 180 <= s <= 300, f"hour={hour} 應放慢,得 {s}"


def test_clinic_refresh_seconds_daytime_unchanged():
    for hour in (7, 8, 12, 18, 23):
        for _ in range(50):
            s = main._clinic_refresh_seconds(hour)
            assert 45 <= s <= 75, f"hour={hour} 應維持,得 {s}"


def test_reg64_micro_ttl_night_vs_day():
    assert all(main._reg64_micro_ttl_seconds(h) == 170 for h in range(0, 7))
    assert all(main._reg64_micro_ttl_seconds(h) == 50 for h in (7, 12, 23))


# ─── MN-04:已寄記錄保留期涵蓋遠期診次 ───────────────────────────────────

def test_alert_retain_days_covers_overview_window():
    """保留期須 > 總覽 13 天視窗,否則遠期診次重啟後重寄。"""
    assert main.ALERT_EMAIL_SENT_RETAIN_DAYS >= 21


def test_alert_sent_record_for_future_session_survives_reload():
    """key=明天診次、value=8 天前(寄出日):以 21 天保留期過濾後仍在;
    若沿用舊 7 天保留期則會被剪(證明修正必要)。"""
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    key = f"{tomorrow}_上午_張三_main"
    sent_day = (date.today() - timedelta(days=8)).isoformat()
    data = {key: sent_day}

    new_cutoff = (date.today()
                  - timedelta(days=main.ALERT_EMAIL_SENT_RETAIN_DAYS)).isoformat()
    assert key in main._filter_recent_alert_sent(data, new_cutoff)

    old_cutoff = (date.today() - timedelta(days=7)).isoformat()
    assert key not in main._filter_recent_alert_sent(data, old_cutoff)


# ─── MN-01:email 先寄、通知在後且優先非阻塞 winotify ─────────────────────

def test_notify_worker_sends_email_before_toast():
    """_notify_worker 內 email 寄送必須排在通知彈窗之前(避免阻塞式 MessageBox
    卡住/程式關閉而漏寄)。"""
    fn = _find_funcdef("_notify_worker")
    calls = _call_names_in(fn)
    send_lines = [ln for name, ln in calls if name == "_send_alert_email_via_smtp"]
    toast_lines = [ln for name, ln in calls
                   if name in ("show_winotify_toast", "show_windows_notification")]
    assert send_lines, "_notify_worker 未見寄信呼叫(測試需更新)"
    assert toast_lines, "_notify_worker 未見通知呼叫(測試需更新)"
    assert max(send_lines) < min(toast_lines), "email 必須排在通知彈窗之前"


def test_notify_worker_prefers_nonblocking_winotify():
    """通知優先用非阻塞 winotify,失敗才 fallback 阻塞式 MessageBox。"""
    fn = _find_funcdef("_notify_worker")
    names = {name for name, _ in _call_names_in(fn)}
    assert "show_winotify_toast" in names, "通知未優先用非阻塞 winotify"


# ─── MN-02:第二次提醒補寄(前次寄失敗者) ────────────────────────────────

def test_notify_worker_uses_shared_atomic_email_claim():
    """[2026-07-17] MN-02 的意圖(前次寄失敗要能補寄、寄成功不得重寄)現在由共用的原子
    寄送權 _claim_alert_email 實作,取代原本 `lvl == 1 or (lvl == 2 and not 已寄)` 的
    非原子條件 —— 因為新增了第二條寄信路徑(遠期止掛背景掃描),而「已寄」記號是寄成功後
    才寫的,兩條路徑各自「先查再寄」會同一診次寄兩封。

    claim 同時擋「已寄過」與「另一條路徑正在寄」;寄失敗會釋放且不留記號 → 下次觸發
    (含 lvl=2 與背景掃描)仍會重試,MN-02 的補寄語意因此保留且更通用。
    實際行為(失敗可重試、成功不重寄、兩路徑不重複)由
    tests/test_stop_signup_future_scan_2026_07_17.py 以行為測試涵蓋。"""
    fn = _find_funcdef("_notify_worker")
    src = ast.get_source_segment(MAIN_SRC.read_text(encoding="utf-8"), fn)
    assert "_claim_alert_email" in src, "寄信前須取得共用的原子寄送權"
    assert "_release_alert_email_claim" in src, "寄完(含失敗)須釋放寄送權,否則永久卡死"
    assert "_mark_alert_email_sent" in src, "寄成功才可留下永久去重記號"


# ─── MN-05:輪詢例外要可見 ────────────────────────────────────────────────

def test_guarded_run_update_logs_exceptions():
    """guarded_run_update 須攔截例外並記 log(不再靜默吞進 future)。"""
    fn = _find_funcdef("guarded_run_update")
    has_except = any(isinstance(n, ast.ExceptHandler) for n in ast.walk(fn))
    assert has_except, "guarded_run_update 缺 except(例外會被 future 靜默吞掉)"
    names = {name for name, _ in _call_names_in(fn)}
    assert "exception" in names or "error" in names, "例外未記 log"


# ─── MN-06:DND 第二次提醒狀態文字不誤導 ────────────────────────────────

def test_dnd_status_text_conditional_on_will_email():
    """DND 分支須依『是否真的會寄 email』決定狀態文字,不再一律『僅寄 email』。"""
    src = MAIN_SRC.read_text(encoding="utf-8")
    assert "_will_email" in src, "DND 狀態文字未依實際寄信與否判斷"
    assert "第二次提醒略過" in src, "缺『已寄過→略過』的據實文字分支"
