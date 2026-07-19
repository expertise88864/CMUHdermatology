# -*- coding: utf-8 -*-
"""稽核健康監控(GPT-5.6 第三輪批次三):讓稽核從「被動鑑識」變成「主動偵測」。

原狀況:verify_chain/verify_generations 寫得再好,只有測試或事故後人工執行,就不是
detection control。帳本遺失(佇列丟棄/落地失敗)只寫 log 靜默;回讀 mismatch(改版寫錯
病歷的第一時間訊號)沒人即時知道。

修法:①開機 +12s 與每日 07:20 自動 audit_health_check(verify_generations + 遺失計數,
異常寄信,同一問題每 process 一次)。②writer 落地時 outcome=mismatch → 即時寄信(同功能
同日去重)。③設定頁顯示健康狀態 + 手動檢查鈕。④PII 縱深防禦(sanitize 落地前消毒,
測試在 test_action_ledger)。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402
from cmuh_common import action_ledger as al  # noqa: E402


def _health_env(monkeypatch, tmp_path, snap=None):
    """隔離健康檢查環境:指到 tmp 帳本(重置 singleton)、重置去重、攔截寄信。回 sent list。"""
    monkeypatch.setattr(main, "get_conf_path", lambda name: str(tmp_path / name))
    monkeypatch.setattr(main, "_action_ledger_singleton", None)
    monkeypatch.setattr(main, "_audit_alert_sent_summaries", set())
    monkeypatch.setattr(main, "_audit_alert_inflight_summaries", set())
    monkeypatch.setattr(main, "_ledger_dropped", 0)
    monkeypatch.setattr(main, "_ledger_write_failures", 0)
    monkeypatch.setattr(main, "_load_alert_recipients", lambda: ["dev@example.com"])
    sent = []
    monkeypatch.setattr(main, "_send_alert_email_via_smtp",
                        lambda subj, body, rcpts, **k:
                        sent.append((subj, body)) or True)
    if snap is not None:
        monkeypatch.setattr(
            main, "_action_ledger",
            lambda: type("L", (), {"health_check": lambda s, **k: dict(snap)})())
    return sent


# ── audit_health_check ───────────────────────────────────────────────────────
def test_healthy_ledger_no_email(monkeypatch, tmp_path):
    lg = al.ActionLedger(str(tmp_path / al.LEDGER_FILENAME))
    lg.record(al.SURFACE_HIS_MENU, "F2", value="51017")
    sent = _health_env(monkeypatch, tmp_path)
    snap = main.audit_health_check()
    assert snap["ok"] is True and sent == [], "健康時不寄信"


def test_tampered_ledger_emails_once(monkeypatch, tmp_path):
    # 帳本被竄改 → error + 寄信;同一問題同 process 只寄一次
    lg = al.ActionLedger(str(tmp_path / al.LEDGER_FILENAME))
    lg.record(al.SURFACE_HIS_MENU, "F2", value="51017")
    p = lg.path
    open(p, "w", encoding="utf-8").write(
        open(p, encoding="utf-8").read().replace("51017", "99999"))
    sent = _health_env(monkeypatch, tmp_path)
    snap = main.audit_health_check()
    assert snap["level"] == "error" and len(sent) == 1
    assert "稽核" in sent[0][0]
    main.audit_health_check()
    assert len(sent) == 1, "同一問題每個 process 只寄一次"


def test_dropped_records_warn_and_email(monkeypatch, tmp_path):
    sent = _health_env(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_ledger_dropped", 3)
    snap = main.audit_health_check()
    assert snap["level"] == "warn" and len(sent) == 1
    assert "遺失" in sent[0][0] or "遺失" in sent[0][1]


def test_notify_false_checks_without_email(monkeypatch, tmp_path):
    # 設定頁手動檢查:只顯示,不重複寄信
    sent = _health_env(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_ledger_dropped", 3)
    snap = main.audit_health_check(notify=False)
    assert snap["level"] == "warn" and sent == []


def test_health_check_never_raises(monkeypatch, tmp_path):
    _health_env(monkeypatch, tmp_path,
                snap={"ok": False, "level": "error", "verified": 0, "summary": "x"})
    monkeypatch.setattr(main, "_load_alert_recipients",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    snap = main.audit_health_check()          # 不得拋
    assert snap["level"] == "error"


def test_anchor_without_files_is_error_not_healthy(tmp_path):
    # [codex P1] anchor 只在寫過紀錄後才存在:.jsonl 全被刪、anchor 還在 ≠ 初始狀態,
    # 是整本被刪 → 必須 error;否則刪光帳本反而回報健康、監控不寄信。
    lg = al.ActionLedger(str(tmp_path / al.LEDGER_FILENAME))
    lg.record(al.SURFACE_HIS_MENU, "F2", value="51017")
    os.remove(lg.path)                                 # 刪掉帳本、留下 anchor
    snap = al.health_snapshot(lg.path)
    assert snap["level"] == "error" and "刪除" in snap["summary"]


def test_failed_alert_email_retries_on_next_check(monkeypatch, tmp_path):
    # [codex P1] 寄失敗不得永久消耗去重 key(否則一次 SMTP 故障就把偵測控制滅音)
    sent = _health_env(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_ledger_dropped", 3)     # → warn
    ok = {"v": False}
    attempts = []
    monkeypatch.setattr(main, "_send_alert_email_via_smtp",
                        lambda s, b, r, **k: attempts.append(s) or ok["v"])
    main.audit_health_check()
    assert len(attempts) == 1
    main.audit_health_check()
    assert len(attempts) == 2, "上次寄失敗 → 下次檢查應重試"
    ok["v"] = True
    main.audit_health_check()
    assert len(attempts) == 3
    main.audit_health_check()
    assert len(attempts) == 3, "寄成功後同一問題不再寄"
    assert sent == []                                   # sent list 是舊 stub,未用


def test_no_recipients_does_not_consume_dedup(monkeypatch, tmp_path):
    _health_env(monkeypatch, tmp_path)
    monkeypatch.setattr(main, "_ledger_dropped", 3)
    monkeypatch.setattr(main, "_load_alert_recipients", lambda: [])
    main.audit_health_check()
    assert main._audit_alert_sent_summaries == set(), \
        "無收件人不得標記已寄(之後設定好收件人要能寄出)"


def test_health_check_holds_ledger_lock(monkeypatch, tmp_path):
    # [codex P2] 活體檢查必須走 ledger 的持鎖方法,避免與寫入/輪替並行讀到暫態誤報竄改
    src = inspect.getsource(main.audit_health_check)
    assert "_action_ledger().health_check(" in src, "活體檢查應走持鎖的 health_check"
    lock_src = inspect.getsource(al.ActionLedger.health_check)
    assert "with self._lock:" in lock_src


# ── mismatch 即時通知 ────────────────────────────────────────────────────────
def _sync_thread(monkeypatch):
    monkeypatch.setattr(main.threading, "Thread",
                        lambda target=None, **k: type(
                            "T", (), {"start": lambda s: target()})())


def test_mismatch_notify_dedups_per_action_per_day(monkeypatch):
    monkeypatch.setattr(main, "_audit_mismatch_notified", set())
    monkeypatch.setattr(main, "_audit_mismatch_inflight", set())
    monkeypatch.setattr(main, "_load_alert_recipients", lambda: ["dev@example.com"])
    sent = []
    monkeypatch.setattr(main, "_send_alert_email_via_smtp",
                        lambda subj, body, rcpts, **k: sent.append(subj) or True)
    _sync_thread(monkeypatch)
    main._notify_audit_mismatch("F2 UVB 劑量", "回讀不符")
    main._notify_audit_mismatch("F2 UVB 劑量", "回讀不符")     # 同功能同日 → 去重
    main._notify_audit_mismatch("F3 身份", "回讀不符")          # 不同功能 → 寄
    assert len(sent) == 2
    assert "回讀不符" in sent[0]


def test_mismatch_failed_send_can_retry(monkeypatch):
    # [codex P1] 寄失敗不進 notified → 同功能之後再 mismatch 仍會通知
    monkeypatch.setattr(main, "_audit_mismatch_notified", set())
    monkeypatch.setattr(main, "_audit_mismatch_inflight", set())
    monkeypatch.setattr(main, "_load_alert_recipients", lambda: ["dev@example.com"])
    ok = {"v": False}
    attempts = []
    monkeypatch.setattr(main, "_send_alert_email_via_smtp",
                        lambda s, b, r, **k: attempts.append(s) or ok["v"])
    _sync_thread(monkeypatch)
    main._notify_audit_mismatch("F2 UVB 劑量", "d")
    assert len(attempts) == 1 and main._audit_mismatch_notified == set()
    ok["v"] = True
    main._notify_audit_mismatch("F2 UVB 劑量", "d")
    assert len(attempts) == 2 and len(main._audit_mismatch_notified) == 1
    main._notify_audit_mismatch("F2 UVB 劑量", "d")
    assert len(attempts) == 2, "寄成功後同日同功能不再寄"


def test_writer_loop_triggers_mismatch_notify(monkeypatch):
    # 帳本落地 outcome=mismatch → writer 觸發即時通知
    notified = []
    monkeypatch.setattr(main, "_notify_audit_mismatch",
                        lambda action, detail: notified.append(action))
    monkeypatch.setattr(main, "_action_ledger",
                        lambda: type("L", (), {"record": lambda s, *a, **k: True})())
    q = main.Queue(maxsize=8)
    q.put_nowait((al.SURFACE_HIS_FIELD, "F2 UVB 劑量",
                  {"outcome": al.OUTCOME_MISMATCH, "detail": "回讀不符"}, "ts"))
    q.put_nowait((al.SURFACE_HIS_FIELD, "F3 療程",
                  {"outcome": al.OUTCOME_OK}, "ts"))
    q.put_nowait(None)                        # 哨兵收工
    main._ledger_writer_loop(q)
    assert notified == ["F2 UVB 劑量"], "只有 mismatch 觸發即時通知"


# ── 佈線守門 ─────────────────────────────────────────────────────────────────
def test_startup_and_daily_checks_wired():
    src = open(main.__file__, encoding="utf-8").read()
    assert '"audit-health-startup", audit_health_check' in src, "開機應排稽核健康檢查"
    assert '"audit-health-daily"' in src, "每日應排稽核健康檢查"


def test_settings_ui_shows_audit_health():
    src = inspect.getsource(main.AutomationApp._build_canary_settings)
    assert "_audit_health_var" in src and "檢查稽核帳本" in src, \
        "設定頁應顯示稽核健康狀態(遺失/毀損不可只留在 log 靜默)"
    btn_src = inspect.getsource(main.AutomationApp._check_audit_health_ui)
    assert "bg_executor.submit" in btn_src, "健康檢查是同步檔案 IO,不可在 UI 緒直接跑"
    assert "notify=False" in btn_src, "手動檢查只顯示,不重複寄信"
    # [codex P3] BoundedThreadPoolExecutor 滿載回「已失敗的 Future」而非 raise →
    # 必須用 done callback 檢查,否則 UI 永遠卡在「檢查中…」
    assert "add_done_callback" in btn_src, "須檢查 submit 回傳的 Future(滿載不 raise)"
