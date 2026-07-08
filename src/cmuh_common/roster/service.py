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
import os
import time
from datetime import date

from cmuh_common.roster.calendar_colors import week_colors_for_year
from cmuh_common.roster.clinic_grid import month_grid
from cmuh_common.roster.ledger import settle_month
from cmuh_common.roster.model import (
    ClerkBatch, Member, RosterParams, SolveContext, batches_covering, day_point,
    roc,
)
from cmuh_common.roster.solve_day import DaySolveInput, month_solve_day
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

    # ── 匯出資料組裝 ────────────────────────────────────────────────────
    def build_export(self, ym: str) -> dict:
        """組裝匯出所需的整月資料（R+VS），與 storage/UI 解耦（純資料，可測）。

        duty 值為 {date: person_id}（只取有排班者）；names 為 {id: 顯示名}
        （R 用姓名、VS 用代號）；leaves 為 {id: [date,...]}。
        """
        cfg = self.storage.load_config()
        month = self.storage.load_month(ym)
        y, m = int(ym[:4]), int(ym[5:7])
        holiday_table = self.storage.load_holiday_duty()
        holidays = set(holiday_table["r"]) | set(holiday_table["vs"])
        ledger = self.storage.load_ledger()

        def scope_block(scope: str) -> dict:
            members = [Member.from_dict(d)
                       for d in (cfg.get(f"{scope}_members") or [])]
            names = {mm.id: (mm.name or mm.id) if scope == "r" else mm.id
                     for mm in members}
            duty: dict = {}
            for iso, cell in (month.get(f"{scope}_duty") or {}).items():
                p = cell.get("person")
                if p:
                    try:
                        dt = date.fromisoformat(iso)
                    except (ValueError, TypeError):
                        continue
                    if (dt.year, dt.month) != (y, m):
                        continue          # [RP3-07] 非當月鍵不計入結算,避免虛增
                    duty[dt] = p
            leaves = _parse_date_map((month.get("leaves") or {}).get(scope) or {})
            return {"members": [mm.id for mm in members], "names": names,
                    "duty": duty, "leaves": {k: sorted(v) for k, v in leaves.items()},
                    "ledger": dict((ledger.get(scope)) or {})}

        return {
            "ym": ym, "year": y, "month": m,
            "holidays": holidays,
            "params": RosterParams.from_config(cfg),
            "r": scope_block("r"), "vs": scope_block("vs"),
        }

    # ── PGY/Clerk 日排班（Phase 3）──────────────────────────────────────
    def build_day_input(self, ym: str) -> DaySolveInput:
        """組裝 PGY/Clerk 日填充器輸入（開診格網 + 名單 + 切片開放 + 請假）。"""
        cfg = self.storage.load_config()
        month = self.storage.load_month(ym)
        y, m = int(ym[:4]), int(ym[5:7])
        holidays = self.storage.holidays_set()
        template = self.storage.load_clinic_template().get("template") or {}
        grid = month_grid(ym, template, holidays,
                          month.get("grid_overrides") or {})

        pgy_roster = month.get("pgy_month_roster")
        if pgy_roster is None:                     # 未指定當月人員 → 用 config 預設代號
            pgy_roster = [str(mm.get("id")) for mm in (cfg.get("pgy_members") or [])]

        batches = [ClerkBatch.from_dict(b)
                   for b in self.storage.load_clerk_batches()]
        covering = batches_covering(batches, y, m)     # 逐日在 solve 時再依 covers 分配
        bio_all = self.storage.load_biopsy_grid()
        biopsy_open: dict = {}
        for b in covering:
            for iso, sess in (bio_all.get(b.id) or {}).items():
                try:                                   # 只採「該梯次確實涵蓋」的日期，
                    if not b.covers(date.fromisoformat(iso)):  # 忽略梯次外的過期/誤設
                        continue
                except (ValueError, TypeError):
                    continue
                biopsy_open.setdefault(iso, {}).update(sess)

        leaves = {
            "pgy": _parse_date_map((month.get("leaves") or {}).get("pgy") or {}),
            "clerk": _parse_date_map((month.get("leaves") or {}).get("clerk") or {}),
        }
        # 鎖定時段：以「目前 day_slots 內容」為鎖定值（自動排班時保留、只重排其餘）
        day_slots = month.get("day_slots") or {}
        locked: dict = {}
        for iso, sessions in (month.get("day_locks") or {}).items():
            for session, on in sessions.items():
                slots = (day_slots.get(iso) or {}).get(session)
                if on and slots is not None:
                    locked.setdefault(iso, {})[session] = slots

        # RF-09：跨月梯次公平計數延續——對每個「起始日早於本月 1 號」的 covering 梯次，
        # 讀上月檔 day_slots 中該梯 covers 的時段，供 month_solve_day 先回放進 fc。
        prior_sessions: dict = {}
        prior_pgy: set = set()
        first = date(y, m, 1)
        cross = [b for b in covering if b.start_monday < first]
        if cross:
            py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
            prev = self.storage.load_month(f"{py:04d}-{pm:02d}")
            prev_slots = prev.get("day_slots") or {}
            for iso, sessions in prev_slots.items():
                try:
                    dd = date.fromisoformat(iso)
                except (ValueError, TypeError):
                    continue
                if any(b.covers(dd) for b in cross):
                    prior_sessions.setdefault(iso, {}).update(sessions)
            prev_pgy = prev.get("pgy_month_roster")
            if prev_pgy is None:
                prev_pgy = [str(mm.get("id")) for mm in (cfg.get("pgy_members") or [])]
            prior_pgy = {str(x) for x in prev_pgy}

        return DaySolveInput(
            ym=ym, grid=grid, pgy_roster=list(pgy_roster),
            clerk_batches=covering, biopsy_open=biopsy_open, leaves=leaves,
            capacity=RosterParams.from_config(cfg).room_capacity, locked=locked,
            prior_sessions=prior_sessions, prior_pgy=prior_pgy)

    def run_day_solve(self, ym: str) -> tuple:
        """build_day_input → month_solve_day。回 (day_slots, log, warnings)，不落地。"""
        return month_solve_day(self.build_day_input(ym))

    def accept_day_solution(self, ym: str, day_slots: dict,
                            report: "str | None" = None) -> None:
        month = self.storage.load_month(ym)
        if month.get("finalized"):
            raise FinalizedMonthError(f"{ym} 已定案（唯讀）；解除定案後才能套用")
        # RF-04：落地前把「目前月檔中鎖定且有內容」的時段強制併回，不信任呼叫端帶來的
        # day_slots 對鎖定時段的處置——掉出開診格網（如事後加假日）的鎖定日不會出現在
        # solver 輸出，若整批覆蓋會靜默刪除鎖定內容並留幽靈鎖。淺拷貝勿改呼叫端 preview。
        day_slots = {iso: dict(sess) for iso, sess in (day_slots or {}).items()}
        cur = month.get("day_slots") or {}
        for iso, sessions in (month.get("day_locks") or {}).items():
            for session, on in sessions.items():
                kept = (cur.get(iso) or {}).get(session)
                if on and kept is not None:
                    day_slots.setdefault(iso, {})[session] = kept
        month["day_slots"] = day_slots
        month["day_report"] = report or ""      # 供「報告」鈕顯示落地當下的報告
        self.storage.save_month(ym, month)

    def set_day_slot(self, ym: str, d: date, session: str, slot: str,
                     people) -> None:
        """手動改某日某時段某格（slot＝照光/治療室/切片室/房號/放假；people 空→移除）。"""
        month = self.storage.load_month(ym)
        sess = (month.setdefault("day_slots", {})
                .setdefault(d.isoformat(), {}).setdefault(session, {}))
        old = sess.get(slot)
        if people:
            sess[slot] = list(people)
        else:
            sess.pop(slot, None)
        self._audit(month, "day", f"{d.isoformat()} {session} {slot}",
                    old, people, "manual")
        self.storage.save_month(ym, month)

    def set_pgy_month_roster(self, ym: str, codes) -> None:
        month = self.storage.load_month(ym)
        month["pgy_month_roster"] = [str(c) for c in codes]
        self.storage.save_month(ym, month)

    def toggle_day_lock(self, ym: str, d: date, session: str) -> bool:
        """鎖定/解鎖某日某時段（鎖定後自動排班不重排該時段）。回傳新狀態。"""
        month = self.storage.load_month(ym)
        if month.get("finalized"):
            raise FinalizedMonthError(f"{ym} 已定案（唯讀）")
        locks = month.setdefault("day_locks", {}).setdefault(d.isoformat(), {})
        new = not locks.get(session)
        if new:
            locks[session] = True
        else:
            locks.pop(session, None)
            if not locks:
                month["day_locks"].pop(d.isoformat(), None)
        self.storage.save_month(ym, month)
        return new

    def is_day_locked(self, ym: str, d: date, session: str) -> bool:
        month = self.storage.load_month(ym)
        return bool(((month.get("day_locks") or {}).get(d.isoformat())
                     or {}).get(session))

    def clear_unlocked_day(self, ym: str) -> None:
        """清除未鎖定的日排班時段（保留鎖定時段）。"""
        month = self.storage.load_month(ym)
        if month.get("finalized"):
            raise FinalizedMonthError(f"{ym} 已定案（唯讀）")
        day_locks = month.get("day_locks") or {}
        kept: dict = {}
        for iso, sessions in (month.get("day_slots") or {}).items():
            for session, slots in sessions.items():
                if (day_locks.get(iso) or {}).get(session):
                    kept.setdefault(iso, {})[session] = slots
        month["day_slots"] = kept
        month["day_report"] = ""       # 舊報告已與清除後不符 → 一併清掉，避免誤導
        self.storage.save_month(ym, month)

    # ── 本月門診停診（某診 VS 請假 → 該診間該期間不開）──────────────────
    def clinic_rooms_for_month(self, ym: str) -> list:
        """本月門診週模板出現過的所有跟診房號（供停診選擇；升冪去重、排除自費）。"""
        template = self.storage.load_clinic_template().get("template") or {}
        rooms: set = set()
        for wd_map in template.values():
            for entries in (wd_map or {}).values():
                for e in (entries or []):
                    if e.get("room") and not e.get("is_self_paid"):
                        rooms.add(str(e["room"]))
        return sorted(rooms)

    def clinic_closures(self, ym: str) -> dict:
        """回本月各 (iso, session) 被停診的房號集合：{iso: {session: [room,...]}}。"""
        ov = self.storage.load_month(ym).get("grid_overrides") or {}
        out: dict = {}
        for iso, sess_map in ov.items():
            for session, sov in (sess_map or {}).items():
                closed = list((sov or {}).get("closed_rooms") or [])
                if closed:
                    out.setdefault(iso, {})[session] = closed
        return out

    def set_clinic_closed(self, ym: str, room: str, start: date, end: date,
                          sessions, closed: bool = True) -> None:
        """在 [start, end] 的每個工作日、指定時段，將某跟診診間標記停診/恢復。

        寫入月檔 grid_overrides[iso][session]['closed_rooms']；month_grid 會據此把
        該診間排除，自動排班就不會把 PGY/Clerk 排進去。恢復＝從清單移除。

        停診時，若當月已排過班（day_slots 已有該診間的人），一併把該診間的既有指派清掉，
        讓「現有班表」也立即反映停診（否則格網仍顯示停診診間有人，直到手動重排）；但
        鎖定的時段（day_locks）不動——鎖定契約是使用者鎖了就不無聲刪除，交由使用者自行處理。

        回傳 {"cleared": 清掉的既有指派數, "skipped_locked": [(iso, session), ...]}：
        cleared>0 時已一併清空 day_report（RS-03，舊報告已與清除後不符）；skipped_locked
        為停診撞到鎖定、未自動移除的時段（RS-05），交呼叫端提示使用者自行處理。
        """
        month = self.storage.load_month(ym)
        if month.get("finalized"):
            raise FinalizedMonthError(f"{ym} 已定案（唯讀）；解除定案後才能改門診")
        room = str(room)
        sessions = [s for s in (sessions or []) if s]
        # 以「模板原始開診」判斷該室哪些日/時段真的有開，只對那些寫 override，
        # 避免對本來就沒開這室的日子（週末/假日/非該診週幾/週三下午）塞垃圾。
        template = self.storage.load_clinic_template().get("template") or {}
        base = month_grid(ym, template, self.storage.holidays_set())
        ov = month.setdefault("grid_overrides", {})
        day_slots = month.get("day_slots") or {}
        day_locks = month.get("day_locks") or {}
        cleared = 0                 # [RS-03] 實際清掉的既有指派數
        skipped_locked: list = []   # [RS-05] 撞到鎖定、未自動移除的停診時段
        for d, day in base.items():
            if d < start or d > end:
                continue
            iso = d.isoformat()
            for session in sessions:
                if room not in (day.get(session) or []):
                    continue                      # 該日該時段本來就沒開這室 → 跳過
                sess = ov.setdefault(iso, {}).setdefault(session, {})
                lst = sess.setdefault("closed_rooms", [])
                if closed and room not in lst:
                    lst.append(room)
                elif not closed and room in lst:
                    lst.remove(room)
                if not lst:                       # 清理空殼，grid_overrides 不留垃圾
                    sess.pop("closed_rooms", None)
                if not sess:
                    ov[iso].pop(session, None)
                # 停診 → 清掉既有班表中該診間的人。未鎖定才動(尊重鎖定契約);鎖定時段
                # 若正排著該診間的人,不無聲刪除 → 收集回報使用者自行處理(RS-05)。
                if closed:
                    slots = (day_slots.get(iso) or {}).get(session)
                    if slots and room in slots:
                        if (day_locks.get(iso) or {}).get(session):
                            skipped_locked.append((iso, session))
                        else:
                            slots.pop(room, None)
                            cleared += 1
            if iso in ov and not ov[iso]:
                ov.pop(iso, None)
        if cleared:
            # [RS-03] 有清掉指派 → 舊 day_report 已與現況不符,一併清空避免幽靈化。
            month["day_report"] = ""
        # [RS-05] 停診/恢復是影響班表的動作,留 audit 痕跡。
        self._audit(month, "day",
                    f"closure:{room} {start.isoformat()}~{end.isoformat()} "
                    f"{sorted(sessions)}",
                    None, "closed" if closed else "open", "closure")
        self.storage.save_month(ym, month)
        return {"cleared": cleared, "skipped_locked": skipped_locked}

    def get_leaves(self, scope: str, ym: str, member_id: str) -> set:
        """讀某人某月請假日集合（適用任一 scope：r/vs/pgy/clerk）。"""
        month = self.storage.load_month(ym)
        raw = ((month.get("leaves") or {}).get(scope) or {}).get(member_id) or []
        out: set = set()
        for iso in raw:
            try:
                out.add(date.fromisoformat(iso))
            except (ValueError, TypeError):
                continue
        return out

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

    def clear_unlocked(self, scope: str, ym: str) -> None:
        """清除未鎖定的 R/VS 值班格（保留鎖定格），一次 load/save，並清舊決策報告。

        RF-20：取代 UI 逐格 set_cell（避免整月最多 31 次 load/save + 驗證 +
        GitSync commit 造成 UI 凍結與 commit 洪水）。
        """
        month = self.storage.load_month(ym)
        if month.get("finalized"):
            raise FinalizedMonthError(f"{ym} 已定案（唯讀）")
        duty = month.get(f"{scope}_duty") or {}
        # 與逐格迴圈語意等價：只清「有 person 且未鎖」的格，保留鎖定格與無 person 殘格。
        kept = {iso: c for iso, c in duty.items()
                if c.get("locked") or not c.get("person")}
        if kept == duty:                       # 沒有未鎖已排格 → 不 save，免空 commit
            return
        month[f"{scope}_duty"] = kept
        month[f"report_{scope}"] = ""          # 舊報告已與清除後不符 → 一併清掉
        self._audit(month, scope, "clear_unlocked", None, None, "clear")
        self.storage.save_month(ym, month)

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

    def resettle_from_duty(self, scope: str, ym: str) -> dict:
        """以目前月檔『實際排班』（含手動調整/換班）重算該 scope 帳本。

        自動回滾同月同 scope 舊分錄再重記 → 帳本永遠反映最終排班（accept 之後
        又手改的格也算進去）。回傳每人本月點數。

        名單清空時仍會 settle（points 空 → 回滾該月舊分錄、不留殘餘）。已定案
        月份唯讀，拒絕重算。"""
        if self.storage.load_month(ym).get("finalized"):
            raise FinalizedMonthError(f"{ym} 已定案（唯讀）；解除定案後才能重算帳本")
        ctx = self.build_context(scope, ym)
        duty = (self.storage.load_month(ym).get(f"{scope}_duty") or {})
        points = {m.id: 0 for m in ctx.members}
        for iso, cell in duty.items():
            p = cell.get("person")
            if p not in points:
                continue
            try:
                points[p] += day_point(date.fromisoformat(iso),
                                       ctx.holidays, ctx.params)
            except (ValueError, TypeError):
                continue
        ledger = self.storage.load_ledger()
        settle_month(ledger, scope, ym, points)
        self.storage.save_ledger(ledger)
        return points

    def finalize(self, ym: str, on: bool) -> None:
        """定案/解除定案。解除需覆寫已定案月檔 → 一律 force=True。

        定案時：以最終（含手動調整/換班）的 R/VS 排班重算帳本，確保帳本＝實況。"""
        if on:
            m0 = self.storage.load_month(ym)
            hist = self.storage.load_ledger().get("history") or []
            settled = {h.get("scope") for h in hist if h.get("month") == ym}
            for scope in ("r", "vs"):
                # 有排班、或本月已有結算（可能被清空 → 需回滾）都要重算。
                # 重算失敗即讓例外上拋 → 中止定案（不留「已定案但帳本沒更新」的
                # 半套狀態）；UI 會攔截顯示錯誤並還原定案勾選。
                if m0.get(f"{scope}_duty") or scope in settled:
                    self.resettle_from_duty(scope, ym)
        month = self.storage.load_month(ym)
        month["finalized"] = bool(on)
        self._audit(month, "-", ym, None, f"finalized={bool(on)}", "finalize")
        self.storage.save_month(ym, month, force=True)

    # ── 定案 PDF 留底 ───────────────────────────────────────────────────
    def build_finalize_pdf_sections(self, ym: str) -> list:
        """組裝定案 PDF 內容：封面 + R/VS/日排班決策報告（純資料，可測）。"""
        month = self.storage.load_month(ym)
        y, m = int(ym[:4]), int(ym[5:7])
        sections = [(f"{roc(y)}年{m:02d}月 排班定案留底",
                     f"月份：{ym}\n產生時間：{_now()}\n"
                     f"（本檔為定案當下的排班快照，供存證留底）")]
        for scope, label in (("r", "R 排班決策報告"), ("vs", "VS 排班決策報告")):
            rpt = month.get(f"report_{scope}")
            if rpt:
                sections.append((label, rpt))
        if month.get("day_report"):
            sections.append(("PGY / Clerk 日排班報告", month["day_report"]))
        return sections

    def archive_finalize_pdf(self, ym: str) -> str:
        """把該月定案排班報告輸出成 PDF 存到 <roster>/finalized/。回傳路徑。
        reportlab 未安裝 → RuntimeError（呼叫端 UI 負責 lazy 安裝後重試）。"""
        from cmuh_common.roster import export_pdf
        y, m = int(ym[:4]), int(ym[5:7])
        out_dir = os.path.join(self.storage.base_dir, "finalized")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{roc(y)}年{m:02d}月定案.pdf")
        export_pdf.export(path, self.build_finalize_pdf_sections(ym))
        return path

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
        # RF-03：鎖定格人選已不在名單時，accept 會保留舊鎖定人（service 寫入時無條件保留
        # locked 格），但 solver/帳本/報告用的是另派的人 → 班表≠帳本≠預覽的分歧狀態。
        # 六項既有檢查都看不到（該鎖定 directive 被 collect 忽略），在此明確擋下並給指引。
        day_set = set(ctx.days)
        for d, mid in sorted(ctx.locks.items()):
            if d in day_set and mid not in mids:
                return (f"{d.month}/{d.day} 鎖定格的 {mid} 已不在名單，"
                        f"請先解鎖該格或改鎖名單內人選")
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
