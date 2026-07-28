# -*- coding: utf-8 -*-
"""[2026-07-28 使用者需求]「針對程式後續擴充性進行完整深度優化處理」。

兩個原語,都是從**今天的實際摩擦**抽出來的,不是憑感覺重構:

1. `cmuh_common.his_contract` —— 院方改版時,校正版本與選單 id 原本散在 main.py 的
   三個相隔數千行的位置,而且 4 支測試各自硬編碼版本字串。2026-07-28 校正到 1150722
   實際動到 5 個檔。收攏後「下次改版 = 改一支檔」。

2. `cmuh_common.alert_dedupe` —— 「inflight / 寄成功才終局去重 / 失敗下次重試」這套
   樣板在 main.py 被重寫了四次(8+ 個模組級全域)。寫錯的後果是**告警永久滅音**,
   而那看起來跟一切正常一模一樣 —— 正是不該靠每次重寫的東西。
"""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common import his_contract as hc  # noqa: E402
from cmuh_common.alert_dedupe import AlertDeduper, DailyOnce  # noqa: E402


# ─── HIS 契約單一宣告處 ────────────────────────────────────────────────────
def test_calibrated_version_has_evidence():
    """★規約的重點是憑據★ 選單 id 猜錯 = 熱鍵打到別的功能 = 寫錯病歷。
    改校正版本卻沒寫下「憑什麼」→ current_calibration() 直接丟例外,啟動就炸,
    不會拖到下次改版才發現沒有依據。"""
    c = hc.current_calibration()
    assert c.version == hc.CALIBRATED_VERSION
    assert c.evidence.strip(), "校正必須寫下憑據"
    assert c.date.count("-") == 2


def test_missing_history_entry_fails_loudly(monkeypatch):
    monkeypatch.setattr(hc, "CALIBRATED_VERSION", "9999999")
    try:
        hc.current_calibration()
    except AssertionError as e:
        assert "沒有紀錄" in str(e)
    else:
        raise AssertionError("改了版本卻沒補歷史,必須丟例外")


def test_history_is_newest_first_and_unique():
    versions = [c.version for c in hc.CALIBRATION_HISTORY]
    assert len(versions) == len(set(versions)), "同一版本不該有兩筆"
    assert versions == sorted(versions, reverse=True), "由新到舊"


def test_main_aliases_point_at_the_single_source():
    """main.py 只能是別名 —— 若又有一份自己的字面值,兩邊就會漂移。"""
    import main
    assert main._HIS_CALIBRATED_VERSION == hc.CALIBRATED_VERSION
    for name in ("MENU_ID_代碼輸入", "MENU_ID_FINISH_NO_PRINT", "MENU_ID_同意書",
                 "MENU_ID_類別字首", "MENU_ID_代碼字首", "MENU_ID_名稱輸入"):
        assert getattr(main, name) == getattr(hc, name), name


def test_menu_ids_are_declared_literally_in_one_place():
    """★安全防線★ 數值必須以字面值宣告在 his_contract,不可再從別處計算/推導。"""
    src = open(os.path.join(os.path.dirname(__file__), '..', 'src',
                            'cmuh_common', 'his_contract.py'),
               encoding='utf-8').read()
    for line in ("MENU_ID_代碼輸入 = 219", "MENU_ID_FINISH_NO_PRINT = 277",
                 "MENU_ID_同意書 = 670", 'CALIBRATED_VERSION = "1150722"'):
        assert line in src, f"缺少字面宣告:{line}"


def test_main_no_longer_declares_menu_ids_literally():
    """回歸守門:main.py 不得再出現「= 數字」的選單 id 宣告(那正是散落的來源)。"""
    import re
    src = open(os.path.join(os.path.dirname(__file__), '..', 'src', 'main.py'),
               encoding='utf-8').read()
    bad = re.findall(r"^MENU_ID_\w+\s*=\s*\d+", src, re.M)
    assert not bad, f"main.py 又出現字面值宣告:{bad}"


# ─── 告警去重器 ────────────────────────────────────────────────────────────
def test_sends_once_then_dedups():
    d = AlertDeduper("t")
    calls = []
    assert d.send_once("k", lambda: calls.append(1) or True) is True
    assert d.send_once("k", lambda: calls.append(1) or True) is False
    assert len(calls) == 1


def test_failed_send_can_retry():
    """★最容易寫錯、而且錯了沒人發現的一條★ 先進 notified 再寄 → 一次 SMTP 故障
    就把該告警永久滅音,而「永久滅音」看起來跟「一切正常」一模一樣。"""
    d = AlertDeduper("t")
    calls = []
    assert d.send_once("k", lambda: calls.append(1) or False) is False
    assert d.already_sent("k") is False
    assert d.send_once("k", lambda: calls.append(1) or True) is True
    assert len(calls) == 2
    assert d.send_once("k", lambda: calls.append(1) or True) is False
    assert len(calls) == 2


def test_exception_counts_as_failure_and_does_not_escape():
    d = AlertDeduper("t")

    def _boom():
        raise RuntimeError("smtp down")

    assert d.send_once("k", _boom) is False, "例外視為失敗"
    assert d.already_sent("k") is False, "例外不可終局去重"
    assert d.pending() == set(), "例外也要歸還 inflight,否則從此不再重試"


def test_truthy_non_bool_is_not_treated_as_success():
    """send 必須回真正的成功與否。SMTP 部分收件人被拒是【正常返回】,
    那種情況要能重試 —— 不可用「沒丟例外」當成功。"""
    d = AlertDeduper("t")
    assert d.send_once("k", lambda: 0) is False
    assert d.already_sent("k") is False


def test_inflight_blocks_concurrent_duplicates():
    """★兩段式的存在理由★ 有些告警是丟背景緒寄的;佔用必須在 spawn 之前完成,
    否則兩次呼叫之間會各堆一條 60 秒逾時的執行緒。"""
    d = AlertDeduper("t")
    started = threading.Event()
    release = threading.Event()
    calls = []

    def _slow():
        calls.append(1)
        started.set()
        release.wait(2)
        return True

    t = threading.Thread(target=lambda: d.send_once("k", _slow), daemon=True)
    t.start()
    assert started.wait(2)
    assert d.pending() == {"k"}
    assert d.send_once("k", lambda: calls.append(1) or True) is False
    release.set()
    t.join(2)
    assert len(calls) == 1


def test_claim_release_is_the_async_form():
    d = AlertDeduper("t")
    assert d.claim("k") is True
    assert d.claim("k") is False, "已佔用不可重複佔"
    d.release("k", False)
    assert d.claim("k") is True, "失敗歸還後要能再佔"
    d.release("k", True)
    assert d.claim("k") is False, "成功後終局去重"


def test_persist_probe_covers_restart_dedup():
    """跨重啟去重:磁碟說「這個 key 之前通知過」→ 不重寄。
    (HIS 改版通知的持久化需求,先預留介面。)"""
    seen = {"1150629@1150722"}
    d = AlertDeduper("t", persist_probe=lambda k: k in seen)
    calls = []
    assert d.send_once("1150629@1150722", lambda: calls.append(1) or True) is False
    assert calls == []
    assert d.send_once("其他", lambda: calls.append(1) or True) is True


def test_persist_probe_failure_is_treated_as_not_sent():
    """磁碟壞掉不可讓告警靜默 —— probe 出錯時保守視為「還沒通知過」。"""
    def _boom(_k):
        raise OSError("disk")

    d = AlertDeduper("t", persist_probe=_boom)
    assert d.send_once("k", lambda: True) is True


def test_daily_once_is_per_key_per_day():
    t = DailyOnce("t")
    assert t.should_run("a", "2026-07-28") is True
    assert t.should_run("a", "2026-07-28") is False
    assert t.should_run("b", "2026-07-28") is True
    assert t.should_run("a", "2026-07-29") is True


# ─── main.py 的接線 ────────────────────────────────────────────────────────
def test_main_uses_the_shared_deduper_not_its_own_sets():
    """回歸守門:不可再回頭自己開 notified/inflight 集合。"""
    src = open(os.path.join(os.path.dirname(__file__), '..', 'src', 'main.py'),
               encoding='utf-8').read()
    for gone in ("_audit_alert_sent_summaries", "_audit_alert_inflight_summaries",
                 "_audit_mismatch_notified", "_audit_mismatch_inflight",
                 "_audit_notify_lock"):
        assert gone not in src, f"{gone} 應已由 AlertDeduper 取代"
    assert "_AUDIT_HEALTH_ALERTS = _AlertDeduper(" in src
    assert "_MISMATCH_ALERTS = _AlertDeduper(" in src
