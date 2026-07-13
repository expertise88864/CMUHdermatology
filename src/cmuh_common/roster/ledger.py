# -*- coding: utf-8 -*-
"""點數帳本（設計文件 §5 ledger.json / §6 月結）。

語意：正值＝多值了（下月目標調低）；負值＝欠的（下月目標調高、多排償還）。
月結公式：new_ledger[p] = old_ledger[p] + (points_p − 月總點數/人數)
（solver 的目標就是讓 points_p ≈ 月總/人數 − old_ledger[p]，理想時 new≈0。）

同月重排：先 rollback_month 移除該月舊分錄影響，再 settle_month 重記——
history 每筆存 {month, scope, deltas:{person: delta}}，rollback 即反向相減。
人員異動：reset_member 歸零（設計文件 3.2 R / V6 定案）。
"""
from __future__ import annotations

import logging

# history 只供「同月重排時回滾」用；超過此月數的舊分錄不會再被回滾 → 修剪避免無限膨脹。
HISTORY_KEEP_MONTHS = 24


def _trim_history(ledger: dict, keep_months: int = HISTORY_KEEP_MONTHS) -> None:
    hist = ledger.get("history") or []
    months = sorted({e.get("month") for e in hist if e.get("month")}, reverse=True)
    if len(months) <= keep_months:
        return
    keep = set(months[:keep_months])
    ledger["history"] = [e for e in hist if e.get("month") in keep]


def fair_share(total_points: float, n_members: int) -> float:
    if n_members <= 0:
        return 0.0
    return total_points / n_members


def settle_month(ledger: dict, scope: str, month: str,
                 points_by_person: dict) -> dict:
    """把該月結果記入帳本（會先自動回滾同月同 scope 舊分錄）。

    points_by_person: {member_id: 本月實得點數}
    回傳更新後 ledger（就地修改並回傳，呼叫端負責 save）。
    """
    rollback_month(ledger, scope, month)
    n = len(points_by_person)
    share = fair_share(sum(points_by_person.values()), n)
    book = ledger.setdefault(scope, {})
    deltas = {}
    for pid, pts in points_by_person.items():
        delta = round(pts - share, 4)
        book[pid] = round(book.get(pid, 0.0) + delta, 4)
        deltas[pid] = delta
    ledger.setdefault("history", []).append(
        {"month": month, "scope": scope, "deltas": deltas})
    _trim_history(ledger)      # [OPT-4] 舊分錄不再被回滾 → 限制 history 大小
    return ledger


def rollback_month(ledger: dict, scope: str, month: str) -> bool:
    """移除該月該 scope 的分錄影響（重排前呼叫）。回傳是否有回滾。"""
    hist = ledger.setdefault("history", [])
    book = ledger.setdefault(scope, {})
    rolled = False
    kept = []
    for entry in hist:
        if entry.get("month") == month and entry.get("scope") == scope:
            for pid, delta in (entry.get("deltas") or {}).items():
                if pid in book:
                    book[pid] = round(book[pid] - float(delta), 4)
            rolled = True
        else:
            kept.append(entry)
    ledger["history"] = kept
    return rolled


def reset_member(ledger: dict, scope: str, member_id: str) -> None:
    """人員異動（離職/新人）→ 餘額歸零，並清掉該員在本 scope history 的 deltas。

    [RF-21 2026-07-13] 與 sync_members 的 RF-14 同因：餘額歸零但 history 分錄留著
    → 打破「餘額 = history deltas 總和」不變式 → 之後【同月 resettle】時 rollback 會
    從已歸零的餘額憑空再扣一次舊 delta，settle 再加回新 delta——若排班沒變就完全抵銷
    （多值一班的人帳本永遠顯示 0），變了就生成幻影 ±（實測 2026-08 vs 換人重排後
    D:+1.0/R:-1.0，真實應為 +0.8/-0.2）。歸零＝該員過往貢獻一併作廢，deltas 必須同步
    清掉（只清本 scope，同 id 可能存在另一 scope）。"""
    book = ledger.setdefault(scope, {})
    if member_id in book and book[member_id]:
        logging.info("[roster.ledger] %s/%s 帳本 %.2f → 0（人員異動歸零）",
                     scope, member_id, book[member_id])
    book[member_id] = 0.0
    for e in (ledger.get("history") or []):
        if e.get("scope") == scope:
            (e.get("deltas") or {}).pop(member_id, None)


def sync_members(ledger: dict, scope: str, member_ids: list) -> None:
    """名單同步：新人補 0；已移除者刪除餘額（=作廢，V5 定案）。"""
    book = ledger.setdefault(scope, {})
    for mid in member_ids:
        book.setdefault(mid, 0.0)
    for mid in list(book):
        if mid not in member_ids:
            logging.info("[roster.ledger] %s/%s 已離開名單，餘額 %.2f 作廢",
                         scope, mid, book[mid])
            del book[mid]
            # RF-14：作廢＝餘額與歷史貢獻一併作廢，同步清掉「本 scope」history 內該員
            # deltas，避免「移除→重加→同月 resettle」時 rollback 憑空再扣一次生成欠款。
            # 只清本 scope（同 id 可能存在於另一 scope，清錯會造成鏡像雙重計入）。
            for e in (ledger.get("history") or []):
                if e.get("scope") == scope:
                    (e.get("deltas") or {}).pop(mid, None)
