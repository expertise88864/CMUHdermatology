"""Read-only, privacy-safe projections for consult and clock diagnostics."""

from __future__ import annotations

import json
import math
import sqlite3
import time
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from cmuh_common.runtime_diagnostics import last_success, latest_run


_STAGE_LABELS = {
    "query": "HIS 查詢", "parse": "清單解析", "roster": "名單判定", "redact": "去識別",
    "prepare": "寄送準備", "send": "寄送", "reconcile": "寄送核對",
    "browser": "瀏覽器準備", "website": "網站載入",
    "login": "登入", "read": "讀取紀錄",
    "submit": "送出打卡", "confirm": "官方紀錄確認", "done": "完成",
}
_OUTCOME_LABELS = {
    "started": "進行中", "ok": "完成", "read_failed": "查詢失敗",
    "roster_unknown": "名單未知", "empty_roster": "確定空名單",
    "no_new": "沒有新會診", "pending": "寄送待確認",
    "refused": "確認拒收", "accepted": "寄送端接受",
    "failed": "失敗", "no_record": "無官方紀錄",
    "read_unknown": "讀取不明", "click_pending": "點擊待確認",
    "official_confirmed": "官方已確認", "auth_failed": "帳密錯誤",
    "skipped": "略過", "dry_run": "模擬執行",
}
_ERROR_LABELS = {
    "none": "無", "timeout": "逾時", "auth": "認證", "read": "讀取",
    "parse": "解析", "privacy": "去識別", "transport": "寄送",
    "refusal": "拒收", "ledger": "寄送帳本", "portal": "網站",
    "storage": "儲存", "unknown": "未分類",
}
_REASON_LABELS = {
    "none": "無", "verify_his": "請人工核對 HIS 會診清單",
    "verify_delivery": "請核對寄件備份，勿直接重寄",
    "fix_recipient": "請檢查收件設定與拒收原因",
    "check_portal": "請檢查打卡網站狀態",
    "verify_clock": "請至官方系統確認，勿重複點擊",
    "fix_credentials": "請檢查帳號密碼",
    "check_storage": "請檢查本機儲存空間或權限",
    "check_job": "請檢查執行紀錄並人工核對結果",
}
_LEDGER_STATES = {"prepared", "submitting", "unknown", "confirmed",
                  "partial", "failed"}
_RECIPIENT_STATES = {"confirmed", "transient_refused", "permanent_refused",
                     "unknown"}


def read_consult_delivery(path: str | Path) -> dict | None:
    """Only state/time and recipient-state counts leave the ledger read."""
    path = Path(path)
    if not path.is_file():
        return None
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=0.1)) as conn:
            conn.execute("PRAGMA query_only=ON")
            row = conn.execute(
                "SELECT state,updated_at,recipients,created_at FROM deliveries "
                "WHERE category='consult' AND parent_id='' "
                "ORDER BY created_at DESC, rowid DESC "
                "LIMIT 1").fetchone()
    except (OSError, sqlite3.Error):
        return None
    if not row or row[0] not in _LEDGER_STATES:
        return None
    try:
        observed_at = float(row[1])
        created_at = float(row[3])
    except (TypeError, ValueError, OverflowError):
        return None
    if (not math.isfinite(observed_at) or observed_at <= 0 or
            observed_at > time.time() + 60 or
            not math.isfinite(created_at) or created_at <= 0 or
            created_at > observed_at + 60):
        return None
    counts = {state: 0 for state in _RECIPIENT_STATES}
    if isinstance(row[2], str) and len(row[2]) <= 16384:
        try:
            recipient_map = json.loads(row[2])
            if isinstance(recipient_map, dict):
                for value in recipient_map.values():
                    if value in counts:
                        counts[value] += 1
        except (TypeError, ValueError):
            pass
    return {"state": row[0], "observed_at": observed_at,
            "created_at": created_at, "counts": counts}


def read_clock_state(path: str | Path) -> dict | None:
    """Read a bounded daily snapshot; names never leave this function."""
    path = Path(path)
    try:
        if not path.is_file() or path.stat().st_size > 262144:
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
        observed_at = path.stat().st_mtime
    except (OSError, UnicodeError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("date"), str):
        return None
    keys = {}
    for key in ("clock_done", "click_pending", "auth_failed"):
        items = raw.get(key)
        keys[key] = {tuple(item) for item in items
                     if isinstance(item, (list, tuple)) and len(item) == 2
                     and all(isinstance(value, str) for value in item)} \
            if isinstance(items, list) else set()
    counts = {"clock_done": len(keys["clock_done"]),
              "click_pending": len(keys["click_pending"] - keys["clock_done"]),
              "auth_failed": len(keys["auth_failed"])}
    return {"date": raw["date"], "counts": counts,
            "observed_at": observed_at}


def _when(value: float | None) -> str:
    return datetime.fromtimestamp(value).strftime("%m/%d %H:%M:%S") if value else "無紀錄"


def _freshness(value: float | None, now: float, max_age: float) -> str:
    if value is None:
        return "無資料"
    age = now - value
    if age < -60:
        return "時間異常，請核對系統時鐘"
    if age <= max_age:
        return f"最新（{int(max(0, age) // 60)} 分鐘前）"
    return f"過期（{int(age // 60)} 分鐘前；不是目前執行狀態）"


def consult_summary(events: list[dict], delivery: dict | None, *,
                    now: float | None = None) -> str:
    instant = time.time() if now is None else now
    event = latest_run(events, now=instant)
    if event and event["stage"] == "parse":
        # The hidden HIS worker may finish parsing after its caller has timed
        # out or even after another attempt has settled. An intermediate parse
        # observation cannot supersede a completed result from the same run.
        query_boundary = max(
            (item["observed_at"] for item in events
             if item["run_id"] == event["run_id"] and
             item["stage"] == "query" and
             item["outcome"] in {"started", "ok"} and
             item["observed_at"] <= event["observed_at"]), default=0)
        terminal = [item for item in events
                    if item["run_id"] == event["run_id"] and
                    item["observed_at"] <= event["observed_at"] and (
                        (item["stage"] == "query" and
                         item["outcome"] in {"ok", "read_failed"} and
                         item["observed_at"] >= query_boundary) or
                        (item["stage"] == "send" and item["outcome"] in {
                            "accepted", "refused", "pending", "failed"}) or
                        (item["stage"] == "roster" and item["outcome"] in {
                            "ok", "roster_unknown", "empty_roster"}) or
                        (item["stage"] == "done" and item["outcome"] in {
                            "empty_roster", "no_new", "skipped", "failed"}))]
        if terminal:
            event = max(terminal, key=lambda item: item["observed_at"])
        elif query_boundary:
            event = max(
                (item for item in events
                 if item["run_id"] == event["run_id"] and
                 item["stage"] == "query" and item["outcome"] == "started" and
                 item["observed_at"] == query_boundary),
                key=lambda item: item["observed_at"], default=event)
    observed = event["observed_at"] if event else None
    stage = _STAGE_LABELS.get(event["stage"], "尚未執行") if event else "尚未執行"
    outcome = _OUTCOME_LABELS.get(event["outcome"], "未知") if event else "無紀錄"
    error = _ERROR_LABELS.get(event["error"], "未分類") if event else "無"
    reason = _REASON_LABELS.get(event["reason"], "無") if event else "無"
    query_state = "無紀錄"
    if event:
        current = [item for item in events
                   if item["run_id"] == event["run_id"] and
                   item["observed_at"] <= event["observed_at"]]
        query_events = [item for item in current if item["stage"] == "query"
                        and item["outcome"] != "started"]
        query_start_at = max(
            (item["observed_at"] for item in current
             if item["stage"] == "query" and item["outcome"] == "started"),
            default=0)
        roster_events = [item for item in current if item["stage"] == "roster"
                         and item["observed_at"] >= query_start_at and
                         item["outcome"] in {"ok", "roster_unknown",
                                                 "empty_roster"}]
        latest_query = max(query_events, key=lambda item: item["observed_at"],
                           default=None)
        latest_roster = max(roster_events, key=lambda item: item["observed_at"],
                            default=None)
        if latest_query and latest_query["outcome"] == "read_failed":
            query_state = "查詢失敗"
        elif latest_roster:
            query_state = {"ok": "名單已讀取", "roster_unknown": "名單未知",
                           "empty_roster": "確定空名單"}[latest_roster["outcome"]]
        elif latest_query:
            query_state = "名單已讀取"
        if event["stage"] == "query" and event["outcome"] == "started":
            query_state = "讀取中"
    ledger_line = "尚無寄送帳本紀錄"
    if delivery:
        state = delivery["state"]
        counts = delivery["counts"]
        if state in ("prepared", "submitting", "unknown"):
            ledger_state = "寄送待確認"
            ledger_reason = _REASON_LABELS["verify_delivery"]
        elif state == "confirmed":
            ledger_state = "寄送端接受"
            ledger_reason = "無"
        elif state == "partial":
            ledger_state = "部分收件人確認拒收"
            ledger_reason = _REASON_LABELS["fix_recipient"]
        else:
            ledger_state = "確認拒收／未送出"
            ledger_reason = _REASON_LABELS["fix_recipient"]
        ledger_line = (f"最新寄送：{ledger_state}（{_when(delivery['observed_at'])}；"
                       f"接受 {counts['confirmed']}／暫拒 {counts['transient_refused']}／"
                       f"永久拒收 {counts['permanent_refused']}／不明 {counts['unknown']}）")
        belongs_to_active_send = (
            event is not None and event["stage"] in {"prepare", "send"}
            and delivery["created_at"] >= event["run_started_at"] - 1)
        if not event or belongs_to_active_send:
            stage, outcome, observed = "寄送核對", ledger_state, delivery["observed_at"]
            error = "寄送" if state in ("partial", "failed") else "無"
            if (state in ("prepared", "submitting", "unknown") and event
                    and event["stage"] == "send" and event["error"] != "none"):
                error = _ERROR_LABELS.get(event["error"], "未分類")
            reason = ledger_reason
        elif state != "confirmed":
            # A later poll can be skipped while the earlier ledger entry still
            # needs reconciliation. Keep that actionable state visible without
            # replacing a newer query failure or other observation.
            if ledger_state not in outcome:
                outcome += "；另有" + ledger_state
            if ledger_reason not in reason:
                reason = (ledger_reason if reason == "無" else
                          reason + "；" + ledger_reason)
    success = last_success(events, domain="consult")
    if delivery and delivery["state"] == "confirmed":
        success = max(success or 0, delivery["observed_at"])
    if query_state == "名單未知" and _REASON_LABELS["verify_his"] not in reason:
        reason = (_REASON_LABELS["verify_his"] if reason == "無" else
                  reason + "；" + _REASON_LABELS["verify_his"])
    if query_state == "名單未知" and error == "無":
        error = "解析"
    if (event and observed is not None and outcome == _OUTCOME_LABELS["started"]
            and instant - observed > 20 * 60):
        outcome = "上次進行中紀錄已過期，執行結果不明"
    return (f"階段：{stage}｜狀態：{outcome}｜查詢結果：{query_state}\n"
            f"狀態時間：{_when(observed)}｜資料：{_freshness(observed, instant, 20 * 60)}\n"
            f"最後成功：{_when(success)}｜錯誤分類：{error}\n"
            f"人工處理：{reason}\n{ledger_line}")


def clock_summary(events: list[dict], state: dict | None, *,
                  today: date | None = None, now: float | None = None) -> str:
    instant = time.time() if now is None else now
    day = (today or date.today()).isoformat()
    event = latest_run(events, now=instant)
    if event and datetime.fromtimestamp(event["observed_at"]).date().isoformat() != day:
        event = None
    observed = event["observed_at"] if event else None
    stage = _STAGE_LABELS.get(event["stage"], "尚未執行") if event else "尚未執行"
    outcome = _OUTCOME_LABELS.get(event["outcome"], "未知") if event else "無紀錄"
    error = _ERROR_LABELS.get(event["error"], "未分類") if event else "無"
    reason = _REASON_LABELS.get(event["reason"], "無") if event else "無"
    if event:
        # One scheduled run may process several accounts. An official result
        # for the last account must not hide an earlier account's final failure.
        unresolved = [item for item in events
                      if item["run_id"] == event["run_id"] and
                      item["observed_at"] <= event["observed_at"] and
                      datetime.fromtimestamp(item["observed_at"]).date().isoformat() == day and
                      item["stage"] == "done" and item["outcome"] in {
                          "read_unknown", "failed", "click_pending", "auth_failed"}]
        if unresolved and event["outcome"] == "official_confirmed":
            last_unresolved = max(unresolved, key=lambda item: item["observed_at"])
            stage, outcome = "執行彙總", "部分帳號未確認"
            error = _ERROR_LABELS.get(last_unresolved["error"], "未分類")
            reason = _REASON_LABELS.get(last_unresolved["reason"], "無")
    counts = {"clock_done": 0, "click_pending": 0, "auth_failed": 0}
    state_time = None
    if state and state["date"] == day:
        counts = state["counts"]
        state_time = state["observed_at"]
    if counts["click_pending"]:
        reason = _REASON_LABELS["verify_clock"]
        if not event or event["outcome"] == "started":
            stage, outcome, observed = "官方紀錄確認", "點擊待確認", state_time
        elif event["outcome"] != "click_pending":
            outcome += "；今日另有點擊待確認"
    elif counts["auth_failed"] and not event:
        stage, outcome, observed = "登入", "帳密錯誤", state_time
        reason = _REASON_LABELS["fix_credentials"]
    elif counts["clock_done"] and not event:
        stage, outcome, observed = "官方紀錄確認", "官方已確認", state_time
    if counts["auth_failed"] and (event or counts["click_pending"]):
        if "帳密錯誤" not in outcome:
            outcome += "；今日另有帳密錯誤"
        action = _REASON_LABELS["fix_credentials"]
        if action not in reason:
            reason = action if reason == "無" else reason + "；" + action
    success = last_success(events, domain="clock")
    if (event and observed is not None and outcome == _OUTCOME_LABELS["started"]
            and instant - observed > 5 * 60):
        outcome = "上次進行中紀錄已過期，執行結果不明"
    return (f"階段：{stage}｜最近觀察：{outcome}\n"
            f"狀態時間：{_when(observed)}｜資料：{_freshness(observed, instant, 5 * 60)}\n"
            f"最後官方確認：{_when(success)}｜錯誤分類：{error}\n"
            f"人工處理：{reason}\n"
            f"今日持久化：已確認 {counts['clock_done']}／點擊待確認 "
            f"{counts['click_pending']}／帳密錯誤 {counts['auth_failed']}")
