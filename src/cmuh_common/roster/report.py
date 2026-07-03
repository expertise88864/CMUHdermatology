# -*- coding: utf-8 -*-
"""四段式決策報告（設計文件 §9）：輸入 → 預檢 → 過程 → 結算/警告。

輸出純文字（monospace 對齊），同步用於：UI 報告視圖、月份檔 report 欄位、
automation_ui.log。使用者要求「清楚了解排班的邏輯、哪些人沒被排到、有哪些問題」。
"""
from __future__ import annotations

from cmuh_common.roster.model import SolveContext, day_point
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
    elif result.status == "infeasible":
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
