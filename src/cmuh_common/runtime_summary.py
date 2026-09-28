"""Read-only, privacy-safe projections for consult and clock diagnostics."""

from __future__ import annotations

import json
import math
import sqlite3
import stat
import time
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from cmuh_common.runtime_diagnostics import (
    event_not_after, event_order, last_success, latest_run,
    read_only_sqlite_uri, sqlite_temporarily_busy,
)


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
    "flow_busy": "已有任務執行中，請等候其結果",
    "order_uncertain": "診斷順序不明，請至官方系統核對",
}
_LEDGER_STATES = {"prepared", "submitting", "unknown", "confirmed",
                  "partial", "failed"}
_RECIPIENT_STATES = {"confirmed", "transient_refused", "permanent_refused",
                     "unknown"}


def _parse_consult_parent(state, updated, recipients, created) -> dict | None:
    if state not in _LEDGER_STATES:
        return None
    try:
        observed_at = float(updated)
        created_at = float(created)
    except (TypeError, ValueError, OverflowError):
        return None
    if (not math.isfinite(observed_at) or observed_at <= 0 or
            not math.isfinite(created_at) or created_at <= 0):
        return None
    try:
        datetime.fromtimestamp(observed_at)
        datetime.fromtimestamp(created_at)
    except (OverflowError, OSError, ValueError):
        return None
    if not isinstance(recipients, str) or len(recipients) > 16384:
        return None
    try:
        recipient_map = json.loads(recipients)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(recipient_map, dict):
        return None
    counts = {name: 0 for name in _RECIPIENT_STATES}
    for value in recipient_map.values():
        if not isinstance(value, str) or value not in counts:
            return None
        counts[value] += 1
    result = {"state": state, "observed_at": observed_at,
              "created_at": created_at, "counts": counts}
    if (observed_at > time.time() + 60 or created_at > time.time() + 60
            or created_at > observed_at + 60):
        result["clock_anomaly"] = True
    return result


def read_consult_delivery(path: str | Path) -> dict | None:
    """Only state/time and recipient-state counts leave the ledger read."""
    path = Path(path)
    try:
        metadata = path.stat()
    except FileNotFoundError:
        return None
    except OSError as error:
        return ({"retry_later": True} if getattr(error, "winerror", None)
                in {32, 33} else {"unreadable": True})
    if not stat.S_ISREG(metadata.st_mode):
        return {"unreadable": True}
    try:
        uri = read_only_sqlite_uri(path)
        with closing(sqlite3.connect(uri, uri=True, timeout=0.1)) as conn:
            conn.execute("PRAGMA query_only=ON")
            columns = {column[1] for column in conn.execute(
                "PRAGMA table_info(deliveries)")}
            active = (" AND (superseded_by='' OR superseded_by IS NULL)"
                      if "superseded_by" in columns else "")
            row = conn.execute(
                "SELECT rowid,state,updated_at,recipients,created_at FROM deliveries "
                "WHERE category='consult' AND parent_id=''" + active + " "
                "ORDER BY rowid DESC "
                "LIMIT 1").fetchone()
            body_flag = ("CASE WHEN body_text IS NULL OR body_text='' "
                         "THEN 0 ELSE 1 END" if "body_text" in columns else "1")
            older_attention = (conn.execute(
                "SELECT state,updated_at,recipients,created_at," + body_flag +
                " FROM deliveries WHERE category='consult' AND parent_id='' "
                "AND rowid<? AND (state IS NULL OR state!='confirmed')" +
                active + " ORDER BY rowid DESC LIMIT 257",
                (row[0],)).fetchall() if row else [])
            older_confirmed = (conn.execute(
                "SELECT rowid,state,updated_at,recipients,created_at FROM deliveries "
                "WHERE category='consult' AND parent_id='' AND rowid<? "
                "AND state='confirmed'" + active +
                " ORDER BY rowid DESC LIMIT 256",
                (row[0],)).fetchall() if row else [])
            # Confirmed history is not actionable. Aggregate its timestamp
            # without loading every recipient map on long-lived ledgers.
            older_confirmed_max = (conn.execute(
                "SELECT MAX(updated_at) FROM deliveries WHERE "
                "category='consult' AND parent_id='' AND rowid<? "
                "AND state='confirmed' AND typeof(updated_at) IN "
                "('integer','real')" + active,
                (row[0],)).fetchone()[0] if row else None)
    except sqlite3.Error as error:
        if sqlite_temporarily_busy(error):
            return {"retry_later": True}
        return {"unreadable": True}
    except OSError:
        return {"unreadable": True}
    if not row:
        return None
    snapshot = _parse_consult_parent(row[1], row[2], row[3], row[4])
    if snapshot is None:
        return {"unreadable": True}
    other_attention = {"pending": False, "partial": False, "failed": False}
    if len(older_attention) > 256:
        other_attention["scan_limited"] = True
    for state, updated, recipients, created, has_body in older_attention[:256]:
        parsed = _parse_consult_parent(state, updated, recipients, created)
        if parsed is None:
            other_attention["unknown_state"] = True
            continue
        if parsed.get("clock_anomaly"):
            other_attention["clock_anomaly"] = True
        if state in {"prepared", "submitting", "unknown"}:
            other_attention["pending"] = True
        elif state in {"partial", "failed"} and (
                has_body or parsed["counts"]["transient_refused"]):
            other_attention[state] = True
    last_confirmed_at: float | None = None
    confirmed_order_uncertain = False
    newer_confirmed_at = (snapshot["observed_at"] if
                          snapshot["state"] == "confirmed" else None)
    newest_confirmed_at = newer_confirmed_at
    for _rowid, state, updated, recipients, created in older_confirmed:
        parsed = _parse_consult_parent(state, updated, recipients, created)
        if parsed is None:
            other_attention["unknown_state"] = True
            confirmed_order_uncertain = True
            continue
        if parsed.get("clock_anomaly"):
            other_attention["clock_anomaly"] = True
        # Rowids identify parent creation order. An older parent that finished
        # later can be legitimate reconciliation; it makes the ledger's
        # completion order unknowable, but ordinary consecutive confirmations
        # must continue to show their last-success time.
        if (newer_confirmed_at is not None and
                parsed["observed_at"] > newer_confirmed_at):
            confirmed_order_uncertain = True
        if newest_confirmed_at is None:
            newest_confirmed_at = parsed["observed_at"]
        newer_confirmed_at = parsed["observed_at"]
        last_confirmed_at = max(last_confirmed_at or 0,
                                parsed["observed_at"])
    if older_confirmed_max is not None:
        try:
            historical_max = float(older_confirmed_max)
        except (TypeError, ValueError, OverflowError):
            historical_max = float("nan")
        if math.isfinite(historical_max) and historical_max > 0:
            if (newest_confirmed_at is not None and
                    historical_max > newest_confirmed_at):
                confirmed_order_uncertain = True
            last_confirmed_at = max(last_confirmed_at or 0, historical_max)
    if confirmed_order_uncertain:
        snapshot["confirmed_order_uncertain"] = True
    elif last_confirmed_at is not None:
        snapshot["last_confirmed_at"] = last_confirmed_at
    if any(other_attention.values()):
        snapshot["other_attention"] = other_attention
    return snapshot


def read_clock_state(path: str | Path) -> dict | None:
    """Read a bounded daily snapshot; names never leave this function."""
    path = Path(path)
    try:
        metadata = path.stat()
    except FileNotFoundError:
        return None
    except OSError as error:
        return ({"retry_later": True} if getattr(error, "winerror", None)
                in {32, 33} else {"unreadable": True})
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 262144:
        return {"unreadable": True}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        return ({"retry_later": True} if getattr(error, "winerror", None)
                in {32, 33} else {"unreadable": True})
    except (UnicodeError, ValueError, RecursionError):
        return {"unreadable": True}
    if not isinstance(raw, dict) or not isinstance(raw.get("date"), str):
        return {"unreadable": True}
    try:
        date.fromisoformat(raw["date"])
    except ValueError:
        return {"unreadable": True}
    keys = {}
    for key in ("clock_done", "click_pending", "auth_failed"):
        items = raw.get(key)
        if key in raw and not isinstance(items, list):
            return {"unreadable": True}
        if items and any(not isinstance(item, (list, tuple)) or len(item) != 2
                         or not all(isinstance(value, str) for value in item)
                         for item in items):
            return {"unreadable": True}
        keys[key] = {tuple(item) for item in (items or [])}
    counts = {"clock_done": len(keys["clock_done"]),
              "click_pending": len(keys["click_pending"] - keys["clock_done"]),
              "auth_failed": len(keys["auth_failed"])}
    observed_at = metadata.st_mtime
    clock_anomaly = False
    try:
        if not math.isfinite(observed_at) or observed_at <= 0:
            raise ValueError("invalid file time")
        datetime.fromtimestamp(observed_at)
    except (TypeError, ValueError, OverflowError, OSError):
        observed_at = None
        clock_anomaly = True
    snapshot = {"date": raw["date"], "counts": counts,
                "observed_at": observed_at}
    if clock_anomaly:
        snapshot["clock_anomaly"] = True
    return snapshot


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
        # out or after a later step/attempt settled. Its timing observation
        # must not replace the caller's newest query, privacy, send or done
        # observation from that run.
        caller_events = [item for item in events
                         if item["run_id"] == event["run_id"] and
                         item["stage"] != "parse" and
                         event_not_after(item, event)]
        if caller_events:
            event = max(caller_events, key=event_order)
    observed = event["observed_at"] if event else None
    stage = _STAGE_LABELS.get(event["stage"], "尚未執行") if event else "尚未執行"
    outcome = _OUTCOME_LABELS.get(event["outcome"], "未知") if event else "無紀錄"
    error = _ERROR_LABELS.get(event["error"], "未分類") if event else "無"
    reason = _REASON_LABELS.get(event["reason"], "無") if event else "無"
    if event and event["reason"] == "order_uncertain":
        reason = "診斷順序不明，請人工核對 HIS 會診清單"
    query_state = "無紀錄"
    if event:
        current = [item for item in events
                   if item["run_id"] == event["run_id"] and
                    event_not_after(item, event)]
        query_events = [item for item in current if item["stage"] == "query"
                        and item["outcome"] != "started"]
        query_start = max(
            (item for item in current
             if item["stage"] == "query" and item["outcome"] == "started"),
            key=event_order, default=None)
        roster_events = [item for item in current if item["stage"] == "roster"
                         and (query_start is None or
                              event_not_after(query_start, item)) and
                         item["outcome"] in {"ok", "roster_unknown",
                                                 "empty_roster"}]
        latest_query = max(query_events, key=event_order, default=None)
        latest_roster = max(roster_events, key=event_order, default=None)
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
    if delivery and delivery.get("retry_later"):
        ledger_line = "最新寄送：帳本暫時忙碌，寄送狀態不明，請稍後重試"
        if event:
            outcome += "；寄送狀態不明"
        else:
            stage, outcome = "寄送核對", "寄送狀態不明"
        action = _REASON_LABELS["verify_delivery"]
        if action not in reason:
            reason = action if reason == "無" else reason + "；" + action
    elif delivery and delivery.get("unreadable"):
        ledger_line = "最新寄送：帳本無法讀取，寄送狀態不明"
        if event:
            outcome += "；寄送狀態不明"
        else:
            stage, outcome = "寄送核對", "寄送狀態不明"
        error = (_ERROR_LABELS["storage"] if error == "無" else
                 error + "；" + _ERROR_LABELS["storage"])
        for action in (_REASON_LABELS["check_storage"],
                       _REASON_LABELS["verify_delivery"]):
            if action not in reason:
                reason = action if reason == "無" else reason + "；" + action
    elif delivery:
        state = delivery["state"]
        counts = delivery["counts"]
        if state in ("prepared", "submitting", "unknown"):
            ledger_state = "寄送待確認"
            ledger_reason = _REASON_LABELS["verify_delivery"]
        elif state == "confirmed":
            ledger_state = "寄送端接受"
            ledger_reason = "無"
        elif state == "partial":
            if counts["permanent_refused"]:
                ledger_state = "部分收件人確認拒收"
                ledger_reason = _REASON_LABELS["fix_recipient"]
            else:
                ledger_state = "部分送達／其餘未送達"
                ledger_reason = _REASON_LABELS["check_job"]
        else:
            ledger_state = "未送出／寄送失敗"
            ledger_reason = _REASON_LABELS["check_job"]
        ledger_line = (f"最新寄送：{ledger_state}（{_when(delivery['observed_at'])}；"
                       f"接受 {counts['confirmed']}／暫拒 {counts['transient_refused']}／"
                       f"永久拒收 {counts['permanent_refused']}／不明 {counts['unknown']}）")
        # Wall-clock overlap cannot prove that this parent ledger entry belongs
        # to the displayed invocation. Keep each source's observation separate.
        if not event:
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
        other_attention = delivery.get("other_attention") or {}
        for present, label, action in (
            (other_attention.get("pending"), "寄送待確認",
             _REASON_LABELS["verify_delivery"]),
            (other_attention.get("partial"), "部分送達／其餘未送達",
             _REASON_LABELS["check_job"]),
            (other_attention.get("failed"), "未送出／寄送失敗",
             _REASON_LABELS["check_job"]),
        ):
            if present:
                if "另有" + label not in outcome:
                    outcome += "；另有" + label
                if action not in reason:
                    reason = action if reason == "無" else reason + "；" + action
        if other_attention.get("unknown_state"):
            outcome += "；另有帳本狀態無法判讀"
            if "儲存" not in error:
                error = "儲存" if error == "無" else error + "；儲存"
            for action in (_REASON_LABELS["check_storage"],
                           _REASON_LABELS["verify_delivery"]):
                if action not in reason:
                    reason = action if reason == "無" else reason + "；" + action
        if other_attention.get("scan_limited"):
            outcome += "；較早寄送紀錄過多，摘要僅檢查最近 256 筆"
            action = _REASON_LABELS["check_job"]
            if action not in reason:
                reason = action if reason == "無" else reason + "；" + action
    if delivery and (delivery.get("clock_anomaly") or
                     (delivery.get("other_attention") or {}).get("clock_anomaly")):
        outcome += "；系統時間異常"
        action = "請核對系統時鐘"
        if action not in reason:
            reason = action if reason == "無" else reason + "；" + action
    reconcile_events = [item for item in events
                        if item["stage"] == "reconcile" and
                        item["outcome"] in {"ok", "failed"}]
    latest_reconcile = max(reconcile_events, key=event_order, default=None)
    if latest_reconcile and latest_reconcile["outcome"] == "failed":
        outcome += "；另有寄送核對失敗"
        if "寄送帳本" not in error:
            error = "寄送帳本" if error == "無" else error + "；寄送帳本"
        action = _REASON_LABELS["verify_delivery"]
        if action not in reason:
            reason = action if reason == "無" else reason + "；" + action
    untracked_pending = [
        item for item in events if item["stage"] == "send" and
        item["outcome"] == "pending" and
        item["error"] in {"ledger", "transport"}]
    if untracked_pending:
        current_pending = (event and event["stage"] == "send" and
                           event["outcome"] == "pending" and
                           event["error"] in {"ledger", "transport"})
        if not current_pending:
            outcome += "；另有無帳本寄送待確認"
        if any(item["error"] == "ledger" for item in untracked_pending):
            if "寄送帳本" not in error:
                error = "寄送帳本" if error == "無" else error + "；寄送帳本"
            action = _REASON_LABELS["check_storage"]
            if action not in reason:
                reason = action if reason == "無" else reason + "；" + action
        if any(item["error"] == "transport" for item in untracked_pending):
            if "寄送" not in error:
                error = "寄送" if error == "無" else error + "；寄送"
        action = _REASON_LABELS["verify_delivery"]
        if action not in reason:
            reason = action if reason == "無" else reason + "；" + action
    ledgerless_refusal = any(
        item["stage"] == "send" and item["outcome"] == "refused" and
        item["error"] == "ledger" and not any(
            resolved["run_id"] == item["run_id"] and
            resolved["stage"] == "send" and
            resolved["outcome"] == "accepted" and
            event_not_after(item, resolved) for resolved in events)
        for item in events)
    if ledgerless_refusal and not (
            event and event["stage"] == "send" and
            event["outcome"] == "refused" and event["error"] == "ledger"):
        outcome += "；另有無帳本確認拒收"
        for category in ("寄送帳本", "拒收"):
            if category not in error:
                error = category if error == "無" else error + "；" + category
        for action in (_REASON_LABELS["check_storage"],
                       _REASON_LABELS["fix_recipient"]):
            if action not in reason:
                reason = action if reason == "無" else reason + "；" + action
    caller_run_ids = {item["run_id"] for item in events
                      if item["stage"] not in {"parse", "reconcile"}}
    if any(item["stage"] == "parse" and
           item["run_id"] not in caller_run_ids for item in events):
        outcome += "；解析紀錄無對應流程結果"
        if "儲存" not in error:
            error = "儲存" if error == "無" else error + "；儲存"
        for action in (_REASON_LABELS["check_storage"],
                       _REASON_LABELS["verify_his"]):
            if action not in reason:
                reason = action if reason == "無" else reason + "；" + action
    diagnostic_success = last_success(events, domain="consult")
    success = diagnostic_success
    if delivery and delivery.get("state") == "confirmed":
        success = max(success or 0, delivery["observed_at"])
    if delivery and delivery.get("last_confirmed_at"):
        success = max(success or 0, delivery["last_confirmed_at"])
    if delivery and delivery.get("confirmed_order_uncertain"):
        success_label = (_when(diagnostic_success) if
                         diagnostic_success is not None else
                         "已確認寄送，但先後不明")
    else:
        success_label = _when(success)
    if query_state == "名單未知" and _REASON_LABELS["verify_his"] not in reason:
        reason = (_REASON_LABELS["verify_his"] if reason == "無" else
                  reason + "；" + _REASON_LABELS["verify_his"])
    if query_state == "名單未知" and error == "無":
        error = "解析"
    if event and event["reason"] == "order_uncertain":
        outcome += "；診斷順序不明"
        if event["error"] in {"storage", "ledger"}:
            action = _REASON_LABELS["check_storage"]
            if action not in reason:
                reason += "；" + action
        other_queries: dict[str, dict] = {}
        for item in events:
            if item["run_id"] == event["run_id"] or item["stage"] != "query":
                continue
            if item["observed_at"] < event["run_started_at"]:
                continue
            previous = other_queries.get(item["run_id"])
            if previous is None or event_order(item) > event_order(previous):
                other_queries[item["run_id"]] = item
        if any(item["outcome"] == "read_failed"
               for item in other_queries.values()):
            outcome += "；另有查詢失敗"
            if "讀取" not in error:
                error = "讀取" if error == "無" else error + "；讀取"
    if event and event["stage"] == "send" and event["error"] == "ledger":
        action = _REASON_LABELS["check_storage"]
        if action not in reason:
            reason = action if reason == "無" else reason + "；" + action
        extra = {
            "refused": ("拒收", "fix_recipient"),
            "pending": ("逾時", "verify_delivery"),
            "failed": ("寄送", "check_job"),
        }.get(event["outcome"])
        if extra:
            error += "；" + extra[0]
            action = _REASON_LABELS[extra[1]]
            if action not in reason:
                reason = action if reason == "無" else reason + "；" + action
    if (event and event["stage"] == "done" and event["outcome"] == "failed"
            and event["error"] == "ledger"):
        error += "；未分類"
        action = _REASON_LABELS["check_job"]
        if action not in reason:
            reason = action if reason == "無" else reason + "；" + action
        prior_sends = [item for item in events
                       if item["run_id"] == event["run_id"] and
                       item["stage"] == "send" and
                       event_not_after(item, event)]
        latest_send = max(prior_sends, key=event_order, default=None)
        if latest_send and latest_send["outcome"] == "refused":
            outcome += "；先前寄送確認拒收"
            if "拒收" not in error:
                error += "；拒收"
            action = _REASON_LABELS["fix_recipient"]
            if action not in reason:
                reason = action if reason == "無" else reason + "；" + action
    if (event and observed is not None and event["outcome"] == "started"
            and outcome.startswith(_OUTCOME_LABELS["started"])
            and instant - observed > 20 * 60):
        outcome = outcome.replace("進行中", "上次進行中紀錄已過期，執行結果不明", 1)
    if getattr(events, "unavailable", False):
        if not event and not delivery:
            stage, outcome = "診斷紀錄", "診斷紀錄無法讀取"
        elif "診斷紀錄無法讀取" not in outcome:
            outcome += "；診斷紀錄無法讀取"
        if "儲存" not in error:
            error = "儲存" if error == "無" else error + "；儲存"
        action = _REASON_LABELS["check_storage"]
        if action not in reason:
            reason = action if reason == "無" else reason + "；" + action
    if getattr(events, "retry_later", False):
        outcome += "；診斷紀錄暫時忙碌，請稍後重試"
    if getattr(events, "ordering_uncertain", False):
        outcome += "；執行順序不明"
        action = "請核對目前 HIS 查詢與寄送狀態"
        if action not in reason:
            reason = action if reason == "無" else reason + "；" + action
    if getattr(events, "clock_anomaly", False):
        outcome += "；系統時間異常"
        action = "請核對系統時鐘"
        if action not in reason:
            reason = action if reason == "無" else reason + "；" + action
    return (f"階段：{stage}｜狀態：{outcome}｜查詢結果：{query_state}\n"
            f"狀態時間：{_when(observed)}｜資料：{_freshness(observed, instant, 20 * 60)}\n"
            f"最後成功：{success_label}｜錯誤分類：{error}\n"
            f"人工處理：{reason}\n{ledger_line}")


def clock_summary(events: list[dict], state: dict | None, *,
                  today: date | None = None, now: float | None = None) -> str:
    instant = time.time() if now is None else now
    day = (today or date.today()).isoformat()
    state_date = state.get("date") if state else None
    state_future = isinstance(state_date, str) and state_date > day
    state_time = state.get("observed_at") if state else None
    state_time_future = (isinstance(state_time, (int, float)) and
                         math.isfinite(state_time) and
                         state_time > instant + 60)
    today_events = [item for item in events
                    if datetime.fromtimestamp(item["observed_at"]).date().isoformat()
                    == day]
    event = latest_run(today_events, now=instant,
                       show_contended_after_success=True,
                       min_run_order=getattr(events, "current_run_floor", 0))
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
                       event_not_after(item, event) and
                      datetime.fromtimestamp(item["observed_at"]).date().isoformat() == day and
                       item["stage"] == "done" and (
                           item["outcome"] in {
                               "read_unknown", "failed", "click_pending",
                               "auth_failed"} or
                           (item["outcome"] == "skipped" and
                            item["reason"] == "verify_clock"))]
        if unresolved:
            if event["outcome"] == "official_confirmed":
                stage, outcome = "執行彙總", "部分帳號未確認"
            ordered = sorted(unresolved, key=event_order)
            errors = dict.fromkeys(
                [_ERROR_LABELS.get(item["error"], "未分類")
                 for item in ordered if item["error"] != "none"] +
                ([_ERROR_LABELS.get(event["error"], "未分類")]
                 if event["error"] != "none" else []))
            reasons = dict.fromkeys(
                [_REASON_LABELS.get(item["reason"], "無")
                 for item in ordered if item["reason"] != "none"] +
                ([_REASON_LABELS.get(event["reason"], "無")]
                 if event["reason"] != "none" else []))
            error = "；".join(errors) if errors else "無"
            reason = "；".join(reasons) if reasons else "無"
        if event["reason"] == "order_uncertain":
            other_failures = [item for item in today_events
                              if item["stage"] == "done" and
                              item["observed_at"] >= event["run_started_at"] and
                              item["outcome"] in {
                                  "read_unknown", "failed", "click_pending",
                                  "auth_failed"}]
            if other_failures:
                stage, outcome = "執行彙總", "診斷順序不明，部分帳號未確認"
                error_codes = dict.fromkeys(
                    [event["error"]] + [item["error"] for item in other_failures])
                reason_codes = dict.fromkeys(
                    [event["reason"]] +
                    [item["reason"] for item in other_failures])
                error = "；".join(_ERROR_LABELS[code] for code in error_codes
                                 if code != "none") or "無"
                reason = "；".join(_REASON_LABELS[code] for code in reason_codes
                                  if code != "none") or "無"
            else:
                outcome += "；診斷順序不明"
    counts = {"clock_done": 0, "click_pending": 0, "auth_failed": 0}
    state_time = None
    if state and state.get("date") == day:
        counts = state["counts"]
        state_time = state["observed_at"]
    if counts["click_pending"]:
        action = _REASON_LABELS["verify_clock"]
        if action not in reason:
            reason = action if reason == "無" else reason + "；" + action
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
    if state and state.get("retry_later"):
        persistent_line = "今日持久化：暫時忙碌，稍後重試，數量不明"
        if not event:
            stage, outcome = "本機狀態", "持久化狀態不明，請稍後重試"
        else:
            outcome += "；持久化狀態不明，請稍後重試"
        action = _REASON_LABELS["verify_clock"]
        if action not in reason:
            reason = action if reason == "無" else reason + "；" + action
    elif state and state.get("unreadable"):
        persistent_line = "今日持久化：無法讀取，數量不明"
        if not event:
            stage, outcome = "本機狀態", "持久化狀態不明"
        elif "持久化狀態不明" not in outcome:
            outcome += "；持久化狀態不明"
        if "儲存" not in error:
            error = "儲存" if error == "無" else error + "；儲存"
        for action in (_REASON_LABELS["check_storage"],
                       _REASON_LABELS["verify_clock"]):
            if action not in reason:
                reason = action if reason == "無" else reason + "；" + action
    elif not state:
        persistent_line = "今日持久化：尚無狀態檔"
    elif state_future:
        persistent_line = "今日持久化：未來日期紀錄，數量不明"
    elif state.get("date") != day:
        persistent_line = "今日持久化：尚無今日紀錄"
    else:
        persistent_line = (f"今日持久化：已確認 {counts['clock_done']}／點擊待確認 "
                           f"{counts['click_pending']}／帳密錯誤 {counts['auth_failed']}")
    if getattr(events, "unavailable", False):
        if not event and not state:
            stage, outcome = "診斷紀錄", "診斷紀錄無法讀取"
        elif "診斷紀錄無法讀取" not in outcome:
            outcome += "；診斷紀錄無法讀取"
        if "儲存" not in error:
            error = "儲存" if error == "無" else error + "；儲存"
        action = _REASON_LABELS["check_storage"]
        if action not in reason:
            reason = action if reason == "無" else reason + "；" + action
    if getattr(events, "retry_later", False):
        outcome += "；診斷紀錄暫時忙碌，請稍後重試"
    if getattr(events, "ordering_uncertain", False):
        outcome += "；執行順序不明"
        action = "請核對官方打卡紀錄"
        if action not in reason:
            reason = action if reason == "無" else reason + "；" + action
    if (event and event["stage"] == "done" and event["outcome"] == "skipped"
            and event["reason"] == "flow_busy"):
        action = _REASON_LABELS["verify_clock"]
        if action not in reason:
            reason = action if reason == "無" else reason + "；" + action
    if getattr(events, "clock_anomaly", False):
        outcome += "；系統時間異常"
        action = "請核對系統時鐘"
        if action not in reason:
            reason = action if reason == "無" else reason + "；" + action
    if state_future or state_time_future or (state and state.get("clock_anomaly")):
        if "系統時間異常" not in outcome:
            outcome += "；系統時間異常"
        for action in ("請核對系統時鐘", _REASON_LABELS["verify_clock"]):
            if action not in reason:
                reason = action if reason == "無" else reason + "；" + action
    success = last_success(events, domain="clock")
    if (event and observed is not None and event["outcome"] == "started"
            and outcome.startswith(_OUTCOME_LABELS["started"])
            and instant - observed > 5 * 60):
        outcome = outcome.replace("進行中", "上次進行中紀錄已過期，執行結果不明", 1)
    return (f"階段：{stage}｜最近觀察：{outcome}\n"
            f"狀態時間：{_when(observed)}｜資料：{_freshness(observed, instant, 5 * 60)}\n"
            f"最後官方確認：{_when(success)}｜錯誤分類：{error}\n"
            f"人工處理：{reason}\n"
            f"{persistent_line}")
