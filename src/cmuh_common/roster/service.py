# -*- coding: utf-8 -*-
"""排班引擎 ↔ 檔案 ↔ UI 的黏合層（設計文件 §4 / 施工指南 §3）。

定位：UI（scheduler.py）**絕不**直接呼叫 solver / storage 細節，一律經本層。
本層負責：
    - 讀 config/ledger/holiday_duty/week_colors/month 檔 → 組 SolveContext
    - 求解（不落地，讓 UI 先預覽）與套用（落地：月檔 duty→last_weekend→
      report→settle_month→save_ledger→save_month）
    - 手動改格 / 鎖定 / 請假 / 指定（每次立即存檔 + 審計）
    - quick_validate：以目前月檔內容跑 precheck + 週末成對完整性檢查（不求解）

日期鍵轉換全在本層做（月檔 leaves/must_duty/duty 存 ISO 字串）；UI 一律傳
`datetime.date`、不碰字串（施工指南 §3.2 / API 地圖 §3）。

與施工指南 §3 的差異（實作時精簡）：
    - `accept_solution(scope, ym, result)` 與 `render_report(scope, ym, result)`
      **不要求呼叫端傳 ctx**——內部由 build_context 重建（storage 未變 →
      等價 ctx），避免 UI 夾帶過期/不符的 ctx。`run_solve` 仍回 SolveResult。
"""
from __future__ import annotations

import logging
import time
from datetime import date

from cmuh_common.roster.calendar_colors import week_colors_for_year
from cmuh_common.roster.ledger import settle_month
from cmuh_common.roster.model import (
    Member, RosterParams, SolveContext, day_point,
)
from cmuh_common.roster.report import build_report
from cmuh_common.roster.rules import Precheck, collect_directives, run_prechecks
from cmuh_common.roster.solve_rvs import (
    SolveResult, apply_boundary_from_prev, solve_duty,
)
from cmuh_common.roster.storage import FinalizedMonthError, RosterStorage

_SCOPE_LABEL = {"r": "R 排班", "vs": "VS 排班"}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _parse_date_map(raw: dict) -> dict:
    """{member_id: [ISO 字串,...]} → {member_id: set[date]}（壞日期略過）。"""
    out: dict = {}
    for mid, isos in (raw or {}).items():
        days: set = set()
        for iso in (isos or []):
            try:
                days.add(date.fromisoformat(iso))
            except (ValueError, TypeError):
                logging.warning("[roster.service] 壞日期略過 %s=%r", mid, iso)
        out[str(mid)] = days
    return out


class RosterService:
    def __init__(self, storage: RosterStorage):
        self.storage = storage

    # ── 讀取組裝 ────────────────────────────────────────────────────────
    def build_context(self, scope: str, ym: str) -> SolveContext:
        """讀 config/ledger/holiday_duty/week_colors/month 檔 → 已 prepare 且已套
        跨月銜接（boundary_fix）的 SolveContext。

        boundary_fix 在此就補（不只 solve_duty 內）→ 求解/驗證/過期檢查看到的
        directive 一致；solve_duty 會再冪等呼叫一次，無害。"""
        cfg = self.storage.load_config()
        month = self.storage.load_month(ym)
        y, m = int(ym[:4]), int(ym[5:7])

        members = [Member.from_dict(d)
                   for d in (cfg.get(f"{scope}_members") or [])]

        holiday_table = self.storage.load_holiday_duty()
        holidays = set(holiday_table["r"]) | set(holiday_table["vs"])
        annual = dict(holiday_table.get(scope) or {})

        leaves = _parse_date_map((month.get("leaves") or {}).get(scope) or {})
        must = _parse_date_map((month.get("must_duty") or {}).get(scope) or {})

        locks: dict = {}
        for iso, cell in (month.get(f"{scope}_duty") or {}).items():
            if cell.get("locked") and cell.get("person"):
                try:
                    locks[date.fromisoformat(iso)] = str(cell["person"])
                except (ValueError, TypeError):
                    logging.warning("[roster.service] 鎖定格壞日期略過 %r", iso)

        ledger = dict((self.storage.load_ledger().get(scope)) or {})
        # 週色：決定性自動套色（依 115 行事曆 4 週交替邏輯，涵蓋跨年邊界的
        # y-1/y/y+1）為基底 → 使用者於設定頁的手動覆蓋優先蓋上。
        week_colors: dict = {}
        for yr in (y - 1, y, y + 1):
            week_colors.update(week_colors_for_year(yr))
        week_colors.update(self.storage.load_week_colors())
        prev = self.storage.prev_month_last_weekend(ym, scope)

        ctx = SolveContext(
            scope=scope, year=y, month=m, members=members, holidays=holidays,
            leaves=leaves, must_duty=must, annual_holiday=annual, locks=locks,
            ledger=ledger, week_colors=week_colors, prev_last_weekend=prev,
            params=RosterParams.from_config(cfg))
        ctx.prepare()
        apply_boundary_from_prev(ctx)
        return ctx

    # ── 求解與落地 ──────────────────────────────────────────────────────
    def run_solve(self, scope: str, ym: str,
                  allow_disable_color: bool = False) -> SolveResult:
        """build_context → solve_duty。不落地（UI 先預覽，接受後才 accept）。"""
        ctx = self.build_context(scope, ym)
        return solve_duty(ctx, allow_disable_color=allow_disable_color)

    def render_report(self, scope: str, ym: str, result: SolveResult) -> str:
        """以目前 storage 狀態重建 ctx，產生 result 的四段式決策報告（純字串）。"""
        ctx = self.build_context(scope, ym)
        return build_report(ctx, result, _SCOPE_LABEL.get(scope, scope))

    def accept_solution(self, scope: str, ym: str, result: SolveResult) -> None:
        """使用者接受排班結果 → 落地（順序固定）：
        1. month[scope_duty] = {iso: {person, locked, source}}（鎖定格不覆蓋）
        2. month["last_weekend"][scope] = result.last_weekend
        3. month["report_"+scope] = 決策報告
        4. ledger: settle_month（內含同月回滾 → 二次 accept 不重複累計）
        5. save_ledger 先、save_month 後（月檔後存：帳本先壞仍可重跑）"""
        if result.status != "ok":
            raise ValueError(f"只能套用成功(ok)的排班結果，目前 status={result.status}")
        if result.scope != scope:
            raise ValueError(
                f"排班結果 scope={result.scope!r} 與欲套用的 {scope!r} 不符，"
                f"請用對應分頁的結果")
        month = self.storage.load_month(ym)
        if month.get("finalized"):
            raise FinalizedMonthError(f"{ym} 已定案（唯讀）；解除定案後才能套用排班")

        # result 必須仍符合「當前」輸入才落地：預覽後若 請假/指定/鎖定/名單/假日
        # 任一改動，舊 result 可能把請假者排上或違反新 directive，settle 出的帳本/
        # 報告就與實況脫節。以重建的 ctx 驗證，不符即拒絕、要求重排（寫入前）。
        ctx = self.build_context(scope, ym)
        stale = self._result_stale_reason(ctx, result)
        if stale:
            raise ValueError(f"排班結果已過期（{stale}），請重新排班")

        existing = month.get(f"{scope}_duty") or {}
        new_duty: dict = {}
        for d in sorted(result.assignments):
            iso = d.isoformat()
            old = existing.get(iso)
            if old and old.get("locked"):
                new_duty[iso] = old            # 鎖定格保留原 person/locked/source
            else:
                new_duty[iso] = {"person": result.assignments[d],
                                 "locked": False, "source": "auto"}
        month[f"{scope}_duty"] = new_duty
        month.setdefault("last_weekend", {})[scope] = result.last_weekend
        month[f"report_{scope}"] = build_report(
            ctx, result, _SCOPE_LABEL.get(scope, scope))

        ledger = self.storage.load_ledger()
        settle_month(ledger, scope, ym, result.points_by_person)
        self.storage.save_ledger(ledger)
        self.storage.save_month(ym, month)

    # ── 手動編輯（每次立即存檔 + 審計）──────────────────────────────────
    def set_cell(self, scope: str, ym: str, d: date,
                 person: "str | None", via: str = "manual") -> list:
        """改格（person=None → 清空並移除該格）。回傳改後 quick_validate 警告
        （不阻止儲存，設計文件 §16.4）。"""
        month = self.storage.load_month(ym)
        duty = month.setdefault(f"{scope}_duty", {})
        iso = d.isoformat()
        old = duty.get(iso)
        old_person = old.get("person") if old else None
        if person is None:
            duty.pop(iso, None)
        else:
            locked = bool(old.get("locked")) if old else False
            duty[iso] = {"person": person, "locked": locked, "source": via}
        self._audit(month, scope, iso, old_person, person, via)
        self.storage.save_month(ym, month)
        return self.quick_validate(scope, ym)

    def toggle_lock(self, scope: str, ym: str, d: date) -> bool:
        """切換鎖定（空格不可鎖）。回傳切換後的鎖定狀態。"""
        month = self.storage.load_month(ym)
        duty = month.setdefault(f"{scope}_duty", {})
        iso = d.isoformat()
        cell = duty.get(iso)
        if not cell or not cell.get("person"):
            return False
        cell["locked"] = not cell.get("locked", False)
        self._audit(month, scope, iso,
                    f"locked={not cell['locked']}",
                    f"locked={cell['locked']}", "lock")
        self.storage.save_month(ym, month)
        return cell["locked"]

    def set_leaves(self, scope: str, ym: str, member_id: str, dates) -> None:
        self._set_date_map(scope, ym, "leaves", member_id, dates)

    def set_must(self, scope: str, ym: str, member_id: str, dates) -> None:
        self._set_date_map(scope, ym, "must_duty", member_id, dates)

    def finalize(self, ym: str, on: bool) -> None:
        """定案/解除定案。解除需覆寫已定案月檔 → 一律 force=True。"""
        month = self.storage.load_month(ym)
        month["finalized"] = bool(on)
        self._audit(month, "-", ym, None, f"finalized={bool(on)}", "finalize")
        self.storage.save_month(ym, month, force=True)

    # ── 驗證（不求解）────────────────────────────────────────────────────
    def quick_validate(self, scope: str, ym: str) -> list:
        """驗證目前月檔（不求解）：
        - run_prechecks：可行性/指定衝突/固定週幾…（看輸入，不看實際排好的格）
        - _weekend_integrity：值班區塊是否被手動改成「不同人/漏排」
        - _manual_cell_checks：**實際排好的格**是否把請假者/非名單者排上、或
          違反 directive（run_prechecks 只把鎖定格當 directive，看不到未鎖手排格
          → 施工指南 §3.1 缺口，本層補）。"""
        ctx = self.build_context(scope, ym)
        checks = list(run_prechecks(ctx, scope))
        checks.extend(self._weekend_integrity(ctx, scope, ym))
        checks.extend(self._manual_cell_checks(ctx, scope, ym))
        return checks

    def _result_stale_reason(self, ctx: SolveContext,
                             result: SolveResult) -> "str | None":
        """result 是否已與 ctx（當前輸入）脫節；脫節回原因字串，否則 None。"""
        if set(result.assignments) != set(ctx.days):
            return "涵蓋日期與當月不符"
        mids = set(ctx.member_ids())
        # 結算基準＝result.points_by_person 的成員集（solver 對每位成員都填一筆）；
        # 與當前名單不符（含「預覽後新增成員」——舊 result 名單較小仍能通過逐格檢查）
        # → fair_share 會算在錯誤的人數/人選上，拒絕重排。
        if set(result.points_by_person) != mids:
            return "成員名單已變動（新增/移除）"
        for d, mid in result.assignments.items():
            if mid not in mids:
                return f"{d.month}/{d.day} 指派 {mid} 已不在名單"
            if ctx.on_leave(mid, d):
                return f"{d.month}/{d.day} 指派 {mid} 現已請假"
        directives, dchecks = collect_directives(ctx)
        if any(c.severity == "error" for c in dchecks):
            return "指定類（鎖定/指定/年度/跨月）出現新衝突"
        for d, (mid, src) in directives.items():
            if result.assignments.get(d) != mid:
                return f"{d.month}/{d.day} {src} {mid} 未被結果採用"
        for b in ctx.blocks:                       # 假日變動可能改變區塊分組
            persons = {result.assignments.get(x) for x in b.days}
            if len(persons) > 1:
                return (f"連休段 {b.days[0].month}/{b.days[0].day} 起"
                        f"已非同一人（假日/區塊變動）")
        # 假日/點數設定變動：assignments 仍合法但每人點數已不同 → 舊 points 會 settle
        # 出錯誤帳本（報告/targets 也過期）。以當前 ctx 重算，不一致即拒絕、要求重排。
        recomputed = {m.id: 0 for m in ctx.members}
        for d, mid in result.assignments.items():
            recomputed[mid] += day_point(d, ctx.holidays, ctx.params)
        if recomputed != dict(result.points_by_person):
            return "點數/假日設定已變動（點數與指派不一致）"
        return None

    def _manual_cell_checks(self, ctx: SolveContext,
                            scope: str, ym: str) -> list:
        """檢查實際排好的每一格（含未鎖定手排）是否合法。"""
        month = self.storage.load_month(ym)
        mids = set(ctx.member_ids())
        directives, _ = collect_directives(ctx)
        checks: list = []
        for iso, cell in (month.get(f"{scope}_duty") or {}).items():
            p = cell.get("person")
            if not p:
                continue
            try:
                d = date.fromisoformat(iso)
            except (ValueError, TypeError):
                continue
            if p not in mids:
                checks.append(Precheck(
                    "warn", "manual_cell", f"{d.month}/{d.day} 排的 {p} 不在名單"))
                continue
            if ctx.on_leave(p, d):
                checks.append(Precheck(
                    "warn", "manual_cell", f"{d.month}/{d.day} {p} 當日請假卻被排班"))
            tgt = directives.get(d)
            if tgt and tgt[0] != p:
                checks.append(Precheck(
                    "warn", "manual_cell",
                    f"{d.month}/{d.day} 應為{tgt[1]} {tgt[0]}，卻排了 {p}"))
        return checks

    # ── 內部 ────────────────────────────────────────────────────────────
    def _set_date_map(self, scope, ym, key, member_id, dates) -> None:
        month = self.storage.load_month(ym)
        table = month.setdefault(key, {}).setdefault(scope, {})
        days = sorted(d.isoformat() for d in (dates or set()))
        if days:
            table[str(member_id)] = days
        else:
            table.pop(str(member_id), None)
        self._audit(month, scope, f"{key}:{member_id}", None,
                    ",".join(days) or "（清空）", key)
        self.storage.save_month(ym, month)

    def _weekend_integrity(self, ctx: SolveContext, scope: str, ym: str) -> list:
        """對每個值班區塊，檢查現有排班是否「同一人、無遺漏」。破了 → warn。"""
        month = self.storage.load_month(ym)
        assigned: dict = {}
        for iso, cell in (month.get(f"{scope}_duty") or {}).items():
            p = cell.get("person")
            if p:
                try:
                    assigned[date.fromisoformat(iso)] = p
                except (ValueError, TypeError):
                    continue
        checks: list = []
        for b in ctx.blocks:
            persons = {assigned.get(d) for d in b.days}
            span = f"{b.days[0].month}/{b.days[0].day}-{b.days[-1].day}"
            if persons == {None}:
                continue                      # 整段尚未排 → 不算「改破」
            if None in persons:
                checks.append(Precheck(
                    "warn", "weekend_pair",
                    f"週末連休段 {span} 有日期未排班（成對不完整）"))
            elif len(persons) > 1:
                checks.append(Precheck(
                    "warn", "weekend_pair",
                    f"週末連休段 {span} 被手動排給不同人 "
                    f"{sorted(p for p in persons if p)}（成對被改破）"))
        return checks

    @staticmethod
    def _audit(month: dict, scope: str, cell: str, old, new, via: str) -> None:
        month.setdefault("audit", []).append({
            "ts": _now(), "scope": scope, "cell": cell,
            "old": old, "new": new, "via": via})
