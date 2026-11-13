"""[2026-11-13 使用者] 排班還原點:排錯之後回到先前排好的版本(類似 Ctrl+Z)。

* 每一次【排班類動作】落地前(自動排班、只排未鎖定人員、局部重排、清除未鎖定、
  復原本身)把當下的班表存成一個還原點,每個範圍保留最近 `MAX_UNDO` 個。
  ★手動改單一格不算一步★(使用者定案)—— 復原時若會蓋掉之後的手動修改,
  預覽的變動清單會列出來。
* 範圍:`day` = PGY/Clerk/外訓/家醫科日排班;`r`、`vs` = 值班表。
* 還原點存在月檔的 `undo` 欄位,跟著月檔同步;只存班表、它的報告與報告的識別,
  不存請假、名單(復原不會動那些)。
* ★鎖定不被復原改動★:目前鎖定的格子(日排班的鎖定時段、值班的鎖定格)保持
  現狀,復原只套用到沒鎖的部分(`restored_content`);預覽與實際復原用同一份結果。

本檔只有純函式;寫檔、帳本與週六切片的連動在 service。
"""
from __future__ import annotations

import hashlib
import json
import uuid
from copy import deepcopy
from datetime import date

MAX_UNDO = 5
KEYS = ("day", "r", "vs")


def _check(key: str) -> None:
    if key not in KEYS:
        raise ValueError(f"未知的還原範圍：{key}")


def _roster_field(key: str) -> str:
    return "day_slots" if key == "day" else f"{key}_duty"


def _report_field(key: str) -> str:
    return "day_report" if key == "day" else f"report_{key}"


def digest_field(key: str) -> str:
    """報告識別的欄位名(與 service 的 `report_digest_key` 相同)。"""
    return f"report_digest_{key}"


def snapshot(month: dict, key: str) -> dict:
    """目前的班表 + 報告 + 報告原本的識別(復原時原樣放回,不重新蓋章)。"""
    _check(key)
    data = {"roster": deepcopy(month.get(_roster_field(key)) or {}),
            "report": month.get(_report_field(key)) or "",
            "digest": month.get(digest_field(key))}
    if key != "day":
        data["last_weekend"] = deepcopy((month.get("last_weekend") or {}).get(key))
    return data


def content_token(month: dict, key: str) -> str:
    """目前班表內容的識別:預覽之後班表若被改過(例:他機同步),復原要先重新預覽。"""
    _check(key)
    payload = {"roster": month.get(_roster_field(key)) or {}}
    if key == "day":
        payload["locks"] = month.get("day_locks") or {}
    return hashlib.sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def push(month: dict, key: str, action: str, ts: str, *,
         allow_empty: bool = False) -> bool:
    """把【目前】的班表存成還原點。與最新還原點相同時不存;班表是空的也不存
    (除非 `allow_empty` —— 復原前的那一份即使是空的也要留,才能再復原回來)。"""
    data = snapshot(month, key)
    if not data["roster"] and not allow_empty:
        return False
    entries = month.setdefault("undo", {}).setdefault(key, [])
    if entries and (entries[-1].get("data") or {}).get("roster") == data["roster"]:
        return False
    entries.append({"id": uuid.uuid4().hex[:12], "ts": str(ts),
                    "action": str(action), "data": data})
    del entries[:-MAX_UNDO]
    return True


def _valid(entry) -> bool:
    return (isinstance(entry, dict) and entry.get("id")
            and isinstance(entry.get("data"), dict)
            and isinstance(entry["data"].get("roster"), dict))


def entries(month: dict, key: str) -> list:
    """還原點清單,最新的在前:[{"id", "ts", "action"}]。"""
    _check(key)
    raw = ((month.get("undo") or {}).get(key) or [])
    return [{"id": str(e["id"]), "ts": str(e.get("ts") or ""),
             "action": str(e.get("action") or "")}
            for e in reversed(raw) if _valid(e)]


def find(month: dict, key: str, entry_id: str) -> dict:
    _check(key)
    for e in (month.get("undo") or {}).get(key) or []:
        if _valid(e) and str(e["id"]) == str(entry_id):
            return e
    raise ValueError("這個還原點已經不存在（可能已被其他電腦更新），請重新開啟復原視窗")


def restored_content(month: dict, key: str, data: dict) -> dict:
    """復原後的班表:還原點的內容,但【目前鎖定的格子保持現狀】。"""
    _check(key)
    old = deepcopy(data.get("roster") or {})
    current = month.get(_roster_field(key)) or {}
    if key == "day":
        for iso, sessions in (month.get("day_locks") or {}).items():
            for session, on in (sessions or {}).items():
                if not on:
                    continue
                kept = (current.get(iso) or {}).get(session)
                if kept is None:
                    (old.get(iso) or {}).pop(session, None)
                    if iso in old and not old[iso]:
                        del old[iso]
                else:
                    old.setdefault(iso, {})[session] = deepcopy(kept)
        return old
    out = {}
    for iso in sorted(set(old) | set(current)):
        now = current.get(iso)
        if isinstance(now, dict) and now.get("locked"):
            out[iso] = deepcopy(now)              # 目前鎖定的格:不動
        elif isinstance(old.get(iso), dict):
            cell = dict(old[iso])
            cell["locked"] = False                # 鎖定狀態以【目前】為準(目前沒鎖)
            out[iso] = cell
    return out


def duty_changes(before: dict, after: dict) -> list:
    """值班表逐日的變動 → 人話清單(預覽用)。"""
    lines = []
    for iso in sorted(set(before or {}) | set(after or {})):
        b = ((before or {}).get(iso) or {}).get("person") or ""
        a = ((after or {}).get(iso) or {}).get("person") or ""
        if b == a:
            continue
        try:
            d = date.fromisoformat(iso)
            label = f"{d.month}/{d.day}({'一二三四五六日'[d.weekday()]})"
        except (TypeError, ValueError):
            label = str(iso)
        lines.append(f"{label}：{b or '（無）'} → {a or '（無）'}")
    return lines
