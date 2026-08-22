# -*- coding: utf-8 -*-
"""四段式決策報告（設計文件 §9）：輸入 → 預檢 → 過程 → 結算/警告。

輸出純文字（monospace 對齊），同步用於：UI 報告視圖、月份檔 report 欄位、
automation_ui.log。使用者要求「清楚了解排班的邏輯、哪些人沒被排到、有哪些問題」。
"""
from __future__ import annotations

from cmuh_common.roster.model import SolveContext, day_point, is_weekend
from cmuh_common.roster.ledger import fair_share

_WD = "一二三四五六日"
_SEV_MARK = {"error": "✗", "warn": "⚠", "info": "・"}


def _fmt_day(d) -> str:
    return f"{d.month}/{d.day}(週{_WD[d.weekday()]})"


def build_report(ctx: SolveContext, result, scope_label: str) -> str:
    """result: solve_rvs.SolveResult。scope_label 例: "R 排班" / "VS 排班"。"""
    lines = []
    lines.append(f"═══ {ctx.year}/{ctx.month:02d} {scope_label}決策報告 ═══")

    # [輸入]
    names = "、".join(f"{m.name or m.id}" for m in ctx.members)
    lines.append(f"[輸入] 成員: {names}")
    if ctx.ledger:
        led = "  ".join(f"{mid}:{v:+.1f}" for mid, v in sorted(ctx.ledger.items())
                        if mid in ctx.member_ids())
        lines.append(f"       帳本結轉: {led or '（皆 0）'}")
    n_leave = sum(len(v) for v in ctx.leaves.values())
    lines.append(f"       請假 {n_leave} 天｜指定 "
                 f"{sum(len(v) for v in ctx.must_duty.values())} 天｜"
                 f"年度假日指定 {len(ctx.annual_holiday)} 天｜"
                 f"鎖定 {len(ctx.locks)} 格")
    lines.append(f"       本月總點數 {ctx.total_points()}"
                 f"（公平份額 {fair_share(ctx.total_points(), len(ctx.members)):.2f}/人）")

    # [預檢]
    lines.append("[預檢]")
    if result.prechecks:
        for c in result.prechecks:
            lines.append(f"  {_SEV_MARK.get(c.severity, '?')} [{c.rule_id}] {c.msg}")
    else:
        lines.append("  ✓ 無警告")

    # [過程]
    lines.append("[過程]")
    if result.status == "ok":
        lines.append(f"  求解層級: {result.level_name}")
        if result.level_used:
            lines.append("  ⚠ 有規則被放寬，請留意上方預檢與下方結算")
        # 成功時也可能帶診斷（例如較嚴格層級只是逾時、並未被證明無解）
        lines.extend(f"  ⚠ {s}" for s in result.diagnosis)
        for d in ctx.days:
            mid = result.assignments.get(d)
            if mid is None:
                continue
            m = ctx.member_by_id(mid)
            pts = day_point(d, ctx.holidays, ctx.params)
            tag = result.reasons.get(d, "")
            lines.append(f"  {_fmt_day(d):>12} {m.name if m else mid:<6}"
                         f" {pts}點  [{tag}]")
    elif result.status == "precheck_failed":
        lines.append("  ✗ 預檢有錯誤（見上），未進行求解。請先解決衝突。")
    elif result.status == "need_confirm_color":
        lines.extend(f"  ⚠ {s}" for s in result.diagnosis)
    elif result.status in ("infeasible", "timeout"):
        # timeout 與 infeasible 都靠 diagnosis 說明；不可落到下面那句「求解器例外」，
        # 逾時並不是例外，而且那句會讓使用者去翻一個根本沒有 traceback 的 log。
        lines.extend(f"  ✗ {s}" for s in result.diagnosis)
    elif result.diagnosis:
        lines.extend(f"  ✗ {s}" for s in result.diagnosis)
    else:
        lines.append("  ✗ 求解器例外，詳見 automation_ui.log")

    # [結算]
    if result.status == "ok":
        lines.append("[結算]")
        lines.append("  成員      平日  假日  總班  點數   目標    新帳本")
        total = ctx.total_points()
        share = fair_share(total, len(ctx.members))
        for m in ctx.members:
            pts = result.points_by_person.get(m.id, 0)
            old = float(ctx.ledger.get(m.id, 0.0))
            new = round(old + (pts - share), 2)
            lines.append(
                f"  {m.name or m.id:<8}"
                f"{result.weekday_counts.get(m.id, 0):>4}"
                f"{result.weekend_counts.get(m.id, 0):>6}"
                f"{result.duty_counts.get(m.id, 0):>6}"
                f"{pts:>6}"
                f"{result.targets.get(m.id, 0):>8.2f}"
                f"{new:>+9.2f}")
        if result.last_weekend:
            lines.append(f"  最後週末: {result.last_weekend['saturday']} → "
                         f"{result.last_weekend['person']}（供下月色塊/銜接）")

    # [警告] 摘要（error/warn 集中重列，方便掃視）
    bad = [c for c in result.prechecks if c.severity in ("error", "warn")]
    lines.append("[警告]")
    if bad or result.status != "ok":
        for c in bad:
            lines.append(f"  {_SEV_MARK[c.severity]} {c.msg}")
        if result.status != "ok":
            lines.append(f"  ✗ 本次狀態: {result.status}")
    else:
        lines.append("  （無）")
    return "\n".join(lines)


def build_final_state_report(*, year: int, month: int, scope_label: str,
                             members, duty: dict, holidays: set, params,
                             ledger: dict) -> str:
    """★定案留底要的是「最終狀態」,不是「當初怎麼求解的」★(外審 2026-08-22
    P1-03)。

    `build_report` 描述的是【那一次自動求解】:哪天誰值班、各人幾點、新帳本
    多少。Auto Accept 之後只要有人手動換一天班,那份報告就與事實不符 ——
    而定案 PDF 直接印它,於是留底文件裡的班表/點數與月檔、帳本互相矛盾,
    ★而且它看起來完全正常★。這個函式改從【正典狀態】重建:月檔的 duty、
    設定的點數規則、帳本現在的餘額。

    duty: {date: member_id}(只含本月、真的有人的格);ledger: {mid: 餘額}。
    ★名單外的人也要列出來★:換班換給已離開名單的人時,靜靜略過會讓留底
    文件少一天班 —— 那正是這一條 finding 要消滅的東西。
    """
    names = {m.id: (m.name or m.id) for m in members}
    lines = [f"═══ {year}/{month:02d} {scope_label}最終班表（定案留底）═══",
             "（依定案當下的月檔排班與帳本重建；含自動排班後的人工調整）"]
    lines.append("[最終班表]")
    if not duty:
        lines.append("  （本月沒有任何值班紀錄）")
    for d in sorted(duty):
        mid = duty[d]
        pts = day_point(d, holidays, params)
        mark = "" if mid in names else "  ← 已不在目前名單"
        lines.append(f"  {_fmt_day(d):>12} {names.get(mid, mid):<8}"
                     f" {pts}點{mark}")
    lines.append("[結算]")
    lines.append("  成員      平日  假日  總班  點數    帳本餘額")
    listed = [m.id for m in members]
    extra = [mid for mid in sorted(set(duty.values())) if mid not in listed]
    for mid in listed + extra:
        days_m = [d for d, x in duty.items() if x == mid]
        we = sum(1 for d in days_m if is_weekend(d) or d in holidays)
        pts = sum(day_point(d, holidays, params) for d in days_m)
        tail = "  ← 已不在目前名單" if mid in extra else ""
        lines.append(
            f"  {names.get(mid, mid):<8}"
            f"{len(days_m) - we:>4}{we:>6}{len(days_m):>6}{pts:>6}"
            f"{float(ledger.get(mid, 0.0)):>+11.2f}{tail}")
    return "\n".join(lines)
