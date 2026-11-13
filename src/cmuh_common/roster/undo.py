"""[2026-11-13 使用者] 排班還原點:排錯之後回到先前排好的版本(類似 Ctrl+Z)。

* 每一次【排班類動作】落地前(自動排班、只排未鎖定人員、局部重排、清除未鎖定、
  復原本身)把當下的班表存成一個還原點,每個範圍保留最近 `MAX_UNDO` 個。
  ★手動改單一格不算一步★(使用者定案)—— 復原時若會蓋掉之後的手動修改,
  預覽的變動清單會列出來。
* 範圍:`day` = PGY/Clerk/外訓/家醫科日排班;`r`、`vs` = 值班表。
* 還原點存在月檔的 `undo` 欄位,跟著月檔同步;只存班表與它的報告,
  不存請假、名單、鎖定設定(復原不會動那些)。

本檔只有純函式;寫檔、帳本與週六切片的連動在 service。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date

MAX_UNDO = 5
KEYS = ("day", "r", "vs")


def day_snapshot(month: dict) -> dict:
    return {"day_slots": deepcopy(month.get("day_slots") or {}),
            "day_report": month.get("day_report") or ""}


def duty_snapshot(month: dict, scope: str) -> dict:
    return {"duty": deepcopy(month.get(f"{scope}_duty") or {}),
            "last_weekend": deepcopy((month.get("last_weekend") or {}).get(scope)),
            "report": month.get(f"report_{scope}") or ""}


def snapshot(month: dict, key: str) -> dict:
    if key not in KEYS:
        raise ValueError(f"未知的還原範圍：{key}")
    return day_snapshot(month) if key == "day" else duty_snapshot(month, key)


def _content(key: str, data: dict):
    """還原點裡「班表本身」的部分(報告不算內容差異)。"""
    return (data or {}).get("day_slots" if key == "day" else "duty") or {}


def push(month: dict, key: str, action: str, ts: str) -> bool:
    """把【目前】的班表存成還原點。班表是空的、或與最新還原點相同時不存。"""
    data = snapshot(month, key)
    if not _content(key, data):
        return False
    entries = month.setdefault("undo", {}).setdefault(key, [])
    if entries and _content(key, entries[-1].get("data")) == _content(key, data):
        return False
    entries.append({"ts": str(ts), "action": str(action), "data": data})
    del entries[:-MAX_UNDO]
    return True


def entries(month: dict, key: str) -> list:
    """還原點清單,最新的在前:[{"ts", "action"}]。"""
    raw = ((month.get("undo") or {}).get(key) or [])
    return [{"ts": str(e.get("ts") or ""), "action": str(e.get("action") or "")}
            for e in reversed(raw) if isinstance(e, dict) and isinstance(e.get("data"), dict)]


def find(month: dict, key: str, ts: str) -> dict:
    for e in reversed((month.get("undo") or {}).get(key) or []):
        if isinstance(e, dict) and str(e.get("ts")) == str(ts) and isinstance(e.get("data"), dict):
            return e
    raise ValueError("這個還原點已經不存在（可能已被其他電腦更新），請重新開啟復原視窗")


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
