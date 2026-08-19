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

import copy
import hashlib
import json
import logging
import os
import time
from datetime import date, timedelta
from typing import NamedTuple

from cmuh_common.roster.calendar_colors import week_colors_for_year
from cmuh_common.roster.clinic_grid import month_grid
from cmuh_common.roster.ledger import settle_month, sync_members
from cmuh_common.roster.model import (
    ClerkBatch, Member, RosterParams, SolveContext, batches_covering, day_point,
    roc,
)
from cmuh_common.roster.solve_day import (
    day_input_fingerprint,
    BIOPSY, PHOTO, REST, TREATMENT, DaySolveInput, month_solve_day,
    person_course_stats,
)
from cmuh_common.roster.report import build_report
from cmuh_common.roster.rules import (
    Precheck, collect_directives, run_prechecks, split_block_runs)
from cmuh_common.roster.saturday_biopsy import (
    assign_saturday_biopsy, biopsy_pair, can_rollback as biopsy_can_rollback,
    format_biopsy_section, last_assigned_before, settle_biopsy)
from cmuh_common.roster.solve_rvs import (
    SolveResult, apply_boundary_from_prev, rvs_input_fingerprint, solve_duty,
)
from cmuh_common.roster.storage import (
    FinalizedMonthError,
    RosterStorage,
    StaleRosterDataError,
)

_SCOPE_LABEL = {"r": "R 排班", "vs": "VS 排班"}
_SPECIAL_SLOTS = frozenset((PHOTO, TREATMENT, BIOPSY, REST))   # 非跟診房的特殊格


def _duty_digest(month: dict, scope: str) -> str:
    """這一份月檔的【該 scope 值班格】的識別 —— 帳本的點數就是從它算出來的。

    (外審排班 RS-6)`finalize` 要「先用月檔重算帳本、再把月檔標成唯讀」,
    兩步之間月檔若換了版本,就會留下「帳本＝A 版班表、被定案的是 B 版」——
    而定案之後是唯讀的,只能靠解除定案才救得回來。所以標定案之前要回頭確認
    它就是剛剛算過的那一份。只取 person(鎖定/來源不影響點數)。
    """
    duty = month.get(f"{scope}_duty") or {}
    canon = json.dumps({str(k): (v or {}).get("person")
                        for k, v in duty.items()},
                       ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


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


class DaySolveResult(NamedTuple):
    """一次日排班求解的結果★與它所依據的輸入的識別★。

    前三個欄位是原本的回傳值;後兩個讓 `accept_day_solution` 能問一句
    「這份結果還配得上現在的資料嗎」——沒有它們,舊解可以在任何時候被套用。
    """
    day_slots: dict
    log: list
    warnings: list
    fingerprint: str
    month_revision: str


class RosterService:
    def __init__(self, storage: RosterStorage):
        self.storage = storage

    # ── 讀取組裝 ────────────────────────────────────────────────────────
    def build_context(self, scope: str, ym: str, *,
                      month: "dict | None" = None) -> SolveContext:
        """讀 config/ledger/holiday_duty/week_colors/month 檔 → 已 prepare 且已套
        跨月銜接（boundary_fix）的 SolveContext。

        boundary_fix 在此就補（不只 solve_duty 內）→ 求解/驗證/過期檢查看到的
        directive 一致；solve_duty 會再冪等呼叫一次，無害。

        `month=` 傳入已載入的月檔 → ★用【呼叫端手上那一份】,不另外再讀一次★
        (外審排班 RS-6):重算帳本時「算點數用的 duty」與「被標定案的月檔」
        必須是同一份,分開讀就可能是兩個版本。"""
        cfg = self.storage.load_config()
        month = self.storage.load_month(ym) if month is None else month
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

        # [2026-07-13 連續值班] 上月最後 4 天已排值班 → prev_tail(連續值班軟限制
        # 的跨月常數;5 日窗最多需要往前看 4 天)。上月檔不存在時 load_month 回預設
        # (duty 空)→ prev_tail 空,規則自動退化成只看本月。
        prev_tail: dict = {}
        first = date(y, m, 1)
        prev_ym = f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"
        try:
            prev_duty = (self.storage.load_month(prev_ym)
                         .get(f"{scope}_duty") or {})
            for k in range(1, 5):
                dd = first - timedelta(days=k)
                cell = prev_duty.get(dd.isoformat()) or {}
                if cell.get("person"):
                    prev_tail[dd] = str(cell["person"])
        except Exception:
            logging.exception("[roster.service] 讀上月值班尾端失敗（略過，"
                              "連續值班限制只看本月）")

        ctx = SolveContext(
            scope=scope, year=y, month=m, members=members, holidays=holidays,
            leaves=leaves, must_duty=must, annual_holiday=annual, locks=locks,
            ledger=ledger, week_colors=week_colors, prev_last_weekend=prev,
            prev_tail=prev_tail,
            params=RosterParams.from_config(cfg))
        ctx.prepare()
        apply_boundary_from_prev(ctx)
        return ctx

    # ── 匯出資料組裝 ────────────────────────────────────────────────────
    def build_export(self, ym: str) -> dict:
        """組裝匯出所需的整月資料（R+VS），與 storage/UI 解耦（純資料，可測）。

        duty 值為 {date: person_id}（只取有排班者）；names 為 {id: 顯示名}
        （R 用姓名、VS 用代號）；leaves 為 {id: [date,...]}。

        ★整份匯出必須來自【同一個版本】★(外審排班 P2-04):本函式依序讀
        config → 月檔 → 年度假日 → 帳本,之後 `build_day_input` 又各自再讀
        一次模板/梯次/切片格網/上月檔。背景同步隨時可以插在任何兩次讀之間,
        於是一份 Excel/PDF 可能是「R/VS 值班＝舊版、PGY 格網＝新版、帳本
        ＝舊版」的拼裝品 —— 它不會毀壞資料,但那是要發出去的正式班表。
        在同一個 `write_barrier()` 內讀完就沒有這個縫(同時也擋住自己人:
        匯出期間別的執行緒的存檔會排在後面)。
        """
        with self.storage.write_barrier():
            return self._build_export_locked(ym)

    def _build_export_locked(self, ym: str) -> dict:
        """`build_export` 的本體。★呼叫端必須持有 `write_barrier`★"""
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

        # [RS-01] 供 PGY/Clerk 日排班匯出：帶上 day_slots（已排內容）與開診格網。
        try:
            day_grid = self.build_day_input(ym).grid
        except Exception:
            logging.warning("build_export 取日排班格網失敗（仍照常匯出 R/VS）",
                            exc_info=True)
            day_grid = {}
        # [週六切片] 匯出月曆的週六格附註（人名用 r names 對照）
        sat_biopsy: dict = {}
        for iso, cell in (month.get("saturday_biopsy") or {}).items():
            p = (cell or {}).get("person")
            if not p:
                continue
            try:
                dt = date.fromisoformat(iso)
            except (ValueError, TypeError):
                continue
            if (dt.year, dt.month) == (y, m):
                sat_biopsy[dt] = str(p)

        return {
            "ym": ym, "year": y, "month": m,
            "holidays": holidays,
            "params": RosterParams.from_config(cfg),
            "r": scope_block("r"), "vs": scope_block("vs"),
            "saturday_biopsy": sat_biopsy,
            "day_slots": month.get("day_slots") or {},
            "day_grid": day_grid,
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

        # [2026-07-25 審查] from_dict 對壞梯次回 None（不再拋例外）→ 濾掉
        batches = [b for b in (ClerkBatch.from_dict(x)
                               for x in self.storage.load_clerk_batches())
                   if b is not None]
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
            prior_sessions=prior_sessions, prior_pgy=prior_pgy,
            apply_pref={str(c) for c in (month.get("pgy_apply_pref") or [])})

    def run_day_solve(self, ym: str) -> "DaySolveResult":
        """build_day_input → month_solve_day。不落地。

        回傳除了結果本身,還帶著★這次求解吃到的輸入的識別★(見
        `accept_day_solution`):預覽視窗可以開很久,期間他機同步進來的請假/
        梯次/停診/名單都會讓這個舊解變成錯的。
        ★revision 在 build 之前先取★:build 期間月檔若被換掉,寧可判成過期
        (要使用者重排一次),也不要拿一份對不上的識別去放行。
        ★這裡刻意用寬鬆載入★:求解本身不寫任何檔,壞檔的代價是「算出一份
        沒意義的預覽」,而按下套用時 `_accept_day_locked` 會用嚴格快照擋下來;
        在預覽階段就丟例外只會讓使用者連畫面都打不開(2026-07-25 的分界:
        讀給人看的寬鬆、要寫回去的嚴格)。
        """
        _m, rev = self.storage.load_month_with_revision(ym)
        inp = self.build_day_input(ym)
        day_slots, log, warnings = month_solve_day(inp)
        return DaySolveResult(day_slots, log, warnings,
                              day_input_fingerprint(inp), rev)

    @staticmethod
    def _overlay_locked_sessions(month: dict, day_slots: dict) -> dict:
        """RF-04：把月檔中「鎖定且有內容」的時段強制蓋回 day_slots（淺拷貝，不改輸入）。
        掉出開診格網（如事後加假日）的鎖定日不會出現在 solver 輸出，若整批覆蓋會靜默刪除
        鎖定內容並留幽靈鎖。accept 與預覽統計共用（[codex P2] 報告統計必須＝實際落地內容）。"""
        day_slots = {iso: dict(sess) for iso, sess in (day_slots or {}).items()}
        cur = month.get("day_slots") or {}
        for iso, sessions in (month.get("day_locks") or {}).items():
            for session, on in sessions.items():
                kept = (cur.get(iso) or {}).get(session)
                if on and kept is not None:
                    day_slots.setdefault(iso, {})[session] = kept
        return day_slots

    def day_slots_with_locks(self, ym: str, day_slots: dict) -> dict:
        """預覽統計用：回傳與 accept_day_solution 相同「鎖定合併」後的 day_slots。"""
        return self._overlay_locked_sessions(self.storage.load_month(ym), day_slots)

    def accept_day_solution(self, ym: str, day_slots: dict,
                            report: "str | None" = None, *,
                            expect: "DaySolveResult | None" = None) -> None:
        """★這條路徑【不重試】★(外審排班第 1 輪 P1-01):`day_slots` 是先前
        求解算出來的整批結果,盤上若已被他機換過,重讀後把同一批舊結果再套一次
        只是把對方的修改蓋掉 —— 語意上該做的是拒絕、請使用者重排。"""
        # ★整段在同一個臨界區內★(外審 RS-2 第 1 輪 P1):驗證與寫入之間若讓
        #   背景 pull 合併月檔【以外】的檔案(名單/模板/梯次/假日),指紋比的是
        #   合併前的資料、月檔 revision 又沒變 —— 兩道關卡都通過,舊解照樣落地。
        with self.storage.write_barrier():
            self._accept_day_locked(ym, day_slots, report, expect)

    def _accept_day_locked(self, ym: str, day_slots: dict,
                           report: "str | None", expect) -> None:
        """`accept_day_solution` 的本體。★呼叫端必須持有 `write_barrier`★"""
        month, rev = self.storage.load_month_snapshot(ym)
        if month.get("finalized"):
            raise FinalizedMonthError(f"{ym} 已定案（唯讀）；解除定案後才能套用")
        # ★求解之後、按下套用之前,輸入可能已經變了★(外審排班第 1 輪 P1-02)
        #   R/VS 那一側早就這樣做(`accept_solution` 重建 ctx 再驗);日排班原本
        #   只重疊「鎖定時段」,其餘一律整批覆蓋 —— 於是預覽開著的期間,他機
        #   同步進來的請假/梯次/停診/名單/未鎖時段全部被舊解蓋回去(請假的人
        #   又被排上、剛停診的診間又有人),而畫面可能已經先刷新成新版,更難察覺。
        #   ★政策:任何 solver 相關狀態變動 → 一律拒絕、要求重排★
        #   (不去猜哪些差異可以安全合併:日排班的輸入彼此影響公平計數。)
        if expect is not None:
            if expect.month_revision != rev:
                raise ValueError(
                    "排班結果已過期（月檔已被其他電腦更新），請重新排班")
            if expect.fingerprint != day_input_fingerprint(
                    self.build_day_input(ym)):
                raise ValueError(
                    "排班結果已過期（名單／請假／Clerk 梯次／停診／門診模板等"
                    "輸入已變動），請重新排班")
        month["day_slots"] = self._overlay_locked_sessions(month, day_slots)
        month["day_report"] = report or ""      # 供「報告」鈕顯示落地當下的報告
        self.storage.save_month(ym, month, expected_revision=rev)

    def set_day_slot(self, ym: str, d: date, session: str, slot: str,
                     people) -> None:
        """手動改某日某時段某格（slot＝照光/治療室/切片室/房號/放假；people 空→移除）。"""
        def _mut(month):
            sess = (month.setdefault("day_slots", {})
                    .setdefault(d.isoformat(), {}).setdefault(session, {}))
            old = sess.get(slot)
            if people:
                sess[slot] = list(people)
            else:
                sess.pop(slot, None)
            self._audit(month, "day", f"{d.isoformat()} {session} {slot}",
                        old, people, "manual")
        self.update_month(ym, _mut)

    def set_day_session(self, ym: str, d: date, session: str, slots: dict) -> int:
        """[RS-06] 一次覆寫某(日,時段)的所有格（slots＝{slot: [人]}；空清單→移除該格）。
        一次 load、逐格 diff 才記 audit、一次 save。回傳實際變動的格數。取代
        _DayEditDialog 逐格呼叫 set_day_slot（每格各一次 load/save/git commit）。"""
        def _mut(month):
            sess = (month.setdefault("day_slots", {})
                    .setdefault(d.isoformat(), {}).setdefault(session, {}))
            changed = 0
            for slot, people in slots.items():
                old = sess.get(slot)
                new = list(people) if people else None
                if (old or None) == (new or None):
                    continue                      # 無變化 → 不記 audit、不算入變動
                changed += 1
                if new:
                    sess[slot] = new
                else:
                    sess.pop(slot, None)
                self._audit(month, "day", f"{d.isoformat()} {session} {slot}",
                            old, people, "manual")
            return changed

        # ★「沒有變動就不要存檔」仍然成立★:先試算一次(在最新月檔上),
        #   真的有變動才進 CAS 迴圈 —— 否則每次開關對話框都會寫一次檔、
        #   commit 一次 git。試算與落地都由 `_mut` 決定,不是兩套判準。
        probe, _rev = self.storage.load_month_snapshot(ym)
        if not _mut(probe):
            return 0
        return self.update_month(ym, _mut)

    def quick_validate_day(self, ym: str) -> list:
        """[RS-07] PGY/Clerk 日排班快速檢查（warn 不擋存，符合設計 §16.4）。回傳訊息清單：
        (a)請假者被排、(b)代號不在當日名單/梯次、(c)週三下午治療室/切片有人、
        (d)房容量超標、(e)停診房仍有人（兜 RS-03/05 殘留）。"""
        out: list = []
        month = self.storage.load_month(ym)
        day_slots = month.get("day_slots") or {}
        if not day_slots:
            return out
        inp = self.build_day_input(ym)
        cap = inp.capacity
        pgy_set = {str(c) for c in inp.pgy_roster}
        closures = self.clinic_closures(ym)
        for iso in sorted(day_slots):
            try:
                d = date.fromisoformat(iso)
            except (ValueError, TypeError):
                continue
            clerk_today = {str(c) for b in inp.clerk_batches
                           if b.covers(d) for c in b.members}
            valid = pgy_set | clerk_today
            leavers = {mid for mid, ds in (inp.leaves.get("pgy") or {}).items()
                       if d in ds}
            leavers |= {mid for mid, ds in (inp.leaves.get("clerk") or {}).items()
                        if d in ds}
            for session, slots in (day_slots.get(iso) or {}).items():
                closed = set((closures.get(iso) or {}).get(session) or [])
                for slot, members in (slots or {}).items():
                    members = members or []
                    if (d.weekday() == 2 and session == "下午"
                            and slot in (TREATMENT, BIOPSY) and members):
                        out.append(f"{iso} {session}：{slot} 週三下午應休診，"
                                   f"卻排了 {'、'.join(members)}")
                    if slot not in _SPECIAL_SLOTS and len(members) > cap:
                        out.append(f"{iso} {session} {slot} 診：{len(members)} 人"
                                   f"超過容量 {cap}")
                    if slot in closed and members:
                        out.append(f"{iso} {session}：{slot} 已停診，"
                                   f"卻仍排了 {'、'.join(members)}")
                    for c in members:
                        if c in leavers:
                            out.append(f"{iso} {session} {slot}：{c} 當日請假卻被排")
                        elif c not in valid:
                            out.append(f"{iso} {session} {slot}："
                                       f"{c} 不在當日 PGY 名單/梯次")
        return out

    def day_course_stats(self, ym: str,
                         day_slots_override: "dict | None" = None) -> dict:
        """[2026-07-23 使用者] 週期次數統計：PGY=本月、Clerk=整個兩週梯次（跨月自動把
        另一半月份的存檔 day_slots 合併進來，以梯次起訖裁切）。統計吃「排出來的結果」
        （含手動改過/鎖定的格），不是 solver 內部計數。

        day_slots_override: 用 preview 的 day_slots 取代【本月】內容（預覽報告用）；
        其他月份一律讀存檔。回：
          {"pgy": {"roster": [...], "stats": {code: {photo,photo_wed_pm,tx,biopsy,follow,rest}}},
           "batches": [{"id","start","end","members","stats"}]}
        """
        inp = self.build_day_input(ym)
        cur_slots = (day_slots_override if day_slots_override is not None
                     else (self.storage.load_month(ym).get("day_slots") or {}))
        # [codex P2] PGY=本月 → 以月份起訖裁切:set_day_slot 不強制 d∈ym,月檔可能殘留
        # 跨月 iso 鍵(如鎖定殘留),不裁切會灌進 PGY 月統計。
        y, m = int(ym[:4]), int(ym[5:7])
        m_first = date(y, m, 1)
        m_last = (date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)) \
            - timedelta(days=1)
        pgy_stats = person_course_stats(
            cur_slots, include={str(c) for c in inp.pgy_roster},
            start=m_first, end=m_last)
        batches_out = []
        for b in inp.clerk_batches:
            b_end = b.start_monday + timedelta(days=13)
            merged: dict = {}
            for ymm in sorted({f"{d.year:04d}-{d.month:02d}"
                               for d in (b.start_monday, b_end)}):
                slots = (cur_slots if ymm == ym
                         else (self.storage.load_month(ymm).get("day_slots") or {}))
                # [codex P2] 各月檔只採「屬於該月」的 iso 鍵:月檔可能殘留跨月鍵
                # (set_day_slot 不強制 d∈ym),不過濾會蓋掉另一個月檔的權威內容。
                merged.update({iso: v for iso, v in slots.items()
                               if iso[:7] == ymm})
            batches_out.append({
                "id": b.id, "start": b.start_monday.isoformat(),
                "end": b_end.isoformat(), "members": list(b.members),
                "stats": person_course_stats(
                    merged, include={str(c) for c in b.members},
                    start=b.start_monday, end=b_end)})
        return {"pgy": {"roster": list(inp.pgy_roster), "stats": pgy_stats},
                "batches": batches_out}

    def update_month(self, ym: str, mutator, *, retries: int = 4):
        """讀最新月檔 → 套上【這一個窄改動】→ CAS 寫回;被搶先就重讀重套。

        ★整份寫回一個舊快照會靜默吃掉他機的修改★(外審排班第 1 輪 P1-01):
        月檔一個檔案裝著 R/VS 值班、日排班、請假、指定、停診…… 跨機同步的
        背景 pull 會在「讀出來 → 改 → 存回去」中間把它換成他機的新版本,
        而整份 `save_month(舊快照)` 會把對方剛同步成功的欄位一起退回去 ——
        從 Git 看來那是 pull 之後產生的合法新變更,擋不住,也看不出來。
        所以窄改動一律走這裡:CAS 失敗就【重讀最新版、把同一個改動再套一次】,
        對方的欄位因此保留。

        `mutator(month)` 的回傳值原樣回傳給呼叫端。★mutator 必須可重跑★
        (它拿到的永遠是當下最新的月檔,可能被呼叫不只一次);會改到別的檔案
        或有其他副作用的動作,放在這個呼叫【之後】做,不要放進 mutator。
        """
        # ★窄改動的基底必須是可信的★(外審排班 RS-6):寬鬆載入對壞檔會回一份
        #   預設空月檔,拿它當基底把這一次的改動寫回去 = 整月的值班/請假/報告
        #   被清成只剩這一格(`_guard_overwrite` 只留一份 `.corrupt-` 備份就
        #   放行,擋不住)。`_update_canonical` 早就是這樣做的,月檔不能例外。
        last: "StaleRosterDataError | None" = None
        for _ in range(max(1, int(retries))):
            month, rev = self.storage.load_month_snapshot(ym)
            out = mutator(month)
            try:
                self.storage.save_month(ym, month, expected_revision=rev)
            except StaleRosterDataError as e:
                last = e
                logging.info("[roster] %s 存檔時發現盤上已更新 → 重讀後重套", ym)
                continue
            return out
        assert last is not None
        raise last

    def _update_canonical(self, name: str, save, mutator, *,
                          retries: int = 4):
        """讀最新的正典檔 → 套上【這一個窄改動】→ CAS 寫回;被搶先就重讀重套。

        ★`update_month` 的同一套道理,擴到月檔以外的正典檔★
        (外審排班第 2 輪 P1-01):設定頁的每一次編輯都是整份 read-modify-write,
        而背景 pull 會在中間換掉檔案 —— 他機剛新增的成員因此被靜默移除,
        接著 `_sync_ledger` 還會把那個人的點數/歷史當成「已離職」作廢。
        「存檔前再讀一次」不夠(那只是把窗口縮小);要的是【比對-交換】。

        `mutator(data)` 的回傳值原樣回傳。★mutator 必須可重跑★。
        """
        # ★窄改動的【基底】必須是可信的,而且要與 revision 是同一份位元組★
        #   `load_*` 對壞檔/鎖檔一律靜默回空,拿它當基底再寫回去,一次新增成員
        #   就會把整份設定清成只剩那一個人(2026-07-25 已經學過這條)。
        #   而「先嚴格檢查、再算 revision、再 load」是三次獨立的讀取 ——
        #   中間換入損壞內容時,revision 與空資料都取自那份壞的,CAS 兩邊對得上
        #   就放行(外審排班 RS-5 第 2 輪 P2)。故一律走 `canonical_snapshot`:
        #   讀一次位元組,嚴格解析它、也用它算 revision;壞檔直接拋、磁碟不動。
        last: "StaleRosterDataError | None" = None
        for _ in range(max(1, int(retries))):
            data, rev = self.storage.canonical_snapshot(name)
            out = mutator(data)
            try:
                save(data, rev)
            except StaleRosterDataError as e:
                last = e
                logging.info("[roster] %s 存檔時發現盤上已更新 → 重讀後重套",
                             name)
                continue
            return out
        assert last is not None
        raise last

    def change_members_and_sync_ledger(self, scope: str, mutator) -> None:
        """名單變更 + 帳本同步是★一件事★,整段在同一個臨界區內完成。

        (外審排班 RS-5 第 1 輪 P1-1)分開做的話會這樣壞:名單那一步已經走 CAS
        (他機新增的 F 因此保住了),但接著的帳本同步若拿【呼叫端事先算好的】
        舊 ids 去 `sync_members`,F 不在那份名單裡 → ★它的餘額與所有 history
        delta 被永久刪除★。所以 ids 必須從【剛剛寫成功的、受保護的最新
        config】重新推導,而且兩步之間不可以讓背景同步插進來。
        """
        with self.storage.write_barrier():
            self.update_config(mutator)
            cfg = self.storage.load_config()      # ★重讀最新版再推導名單★
            ids = [m.get("id") for m in (cfg.get(f"{scope}_members") or [])]
            self.update_ledger(lambda led: sync_members(led, scope, ids))

    def update_config(self, mutator, *, retries: int = 4):
        """對 config.json 做一個窄改動(名單/參數),CAS 保護。"""
        return self._update_canonical(
            "config.json",
            lambda d, rev: self.storage.save_config(d, expected_revision=rev),
            mutator, retries=retries)

    def update_ledger(self, mutator, *, retries: int = 4):
        return self._update_canonical(
            "ledger.json",
            lambda d, rev: self.storage.save_ledger(d, expected_revision=rev),
            mutator, retries=retries)

    def update_clerk_batches(self, mutator, *, retries: int = 4):
        return self._update_canonical(
            "clerk_batches.json",
            lambda d, rev: self.storage.save_clerk_batches(
                d, expected_revision=rev),
            mutator, retries=retries)

    def update_clinic_template(self, mutator, *, retries: int = 4):
        return self._update_canonical(
            "clinic_template.json",
            lambda d, rev: self.storage.save_clinic_template(
                d, expected_revision=rev),
            mutator, retries=retries)

    def update_biopsy_grid(self, mutator, *, retries: int = 4):
        return self._update_canonical(
            "biopsy_grid.json",
            lambda d, rev: self.storage.save_biopsy_grid(
                d, expected_revision=rev),
            mutator, retries=retries)

    def toggle_week_color(self, year: int, week: str) -> "str | None":
        """把某一週的手動覆蓋色切到下一個狀態(粉→綠→取消)。→ 新的值。

        ★只動這一週★(外審排班 RS-5 第 1 輪 P1-3):UI 原本是「讀整份覆蓋集 →
        改一格 → `replace=True` 整組寫回」,他機剛設的別週覆蓋會被抹掉;而且
        下一個狀態要用【當下最新的】值去算,不是畫面上的舊值。
        """
        out: dict = {}

        def _mut(cur):
            weeks = cur.setdefault("weeks", {})
            nxt = {None: "pink", "pink": "green", "green": None}.get(
                weeks.get(week))
            if nxt:
                weeks[week] = nxt
            else:
                weeks.pop(week, None)
            cur["year"] = int(year)
            cur["source"] = "manual"
            out["value"] = nxt

        self._update_canonical(
            "week_colors.json",
            lambda d, rev: self.storage.save_week_colors(
                int(year), d.get("weeks") or {}, source="manual",
                replace=True, expected_revision=rev),
            _mut)
        return out.get("value")

    def update_holiday_duty(self, mutator, *, retries: int = 4):
        return self._update_canonical(
            "holiday_duty.json",
            lambda d, rev: self.storage.save_holiday_duty(
                d, expected_revision=rev),
            mutator, retries=retries)

    def set_pgy_month_roster(self, ym: str, codes) -> None:
        def _mut(month):
            month["pgy_month_roster"] = [str(c) for c in codes]
        self.update_month(ym, _mut)

    def set_pgy_apply_pref(self, ym: str, codes) -> None:
        """[2026-07-23 使用者] 設定本月「Apply 本科」PGY（至多 2 位）：自動排班時，
        週二/週五早午的 101 診跟診在【座位次數平手時】優先排這些人（公平最優先，
        偏好只是最後的平手決勝）。"""
        codes = [str(c) for c in codes]
        if len(codes) > 2:
            raise ValueError("Apply 本科優先最多選 2 位")
        def _mut(month):
            old = month.get("pgy_apply_pref")
            month["pgy_apply_pref"] = codes
            self._audit(month, "pgy", f"{ym} apply_pref", old, codes, "manual")
        self.update_month(ym, _mut)

    def toggle_day_lock(self, ym: str, d: date, session: str) -> bool:
        """鎖定/解鎖某日某時段（鎖定後自動排班不重排該時段）。回傳新狀態。"""
        def _mut(month):
            if month.get("finalized"):
                raise FinalizedMonthError(f"{ym} 已定案（唯讀）")
            locks = month.setdefault("day_locks", {}).setdefault(
                d.isoformat(), {})
            # ★狀態由【當下最新的月檔】決定★:重試時他機可能已經改過鎖定,
            #   沿用第一次算出來的 new 會把對方的狀態原封蓋回去。
            turn_on = not locks.get(session)
            if turn_on:
                locks[session] = True
            else:
                locks.pop(session, None)
                if not locks:
                    month["day_locks"].pop(d.isoformat(), None)
            return turn_on
        return self.update_month(ym, _mut)

    def is_day_locked(self, ym: str, d: date, session: str) -> bool:
        month = self.storage.load_month(ym)
        return bool(((month.get("day_locks") or {}).get(d.isoformat())
                     or {}).get(session))

    def clear_unlocked_day(self, ym: str) -> None:
        """清除未鎖定的日排班時段（保留鎖定時段）。"""
        def _mut(month):
            if month.get("finalized"):
                raise FinalizedMonthError(f"{ym} 已定案（唯讀）")
            day_locks = month.get("day_locks") or {}
            kept: dict = {}
            for iso, sessions in (month.get("day_slots") or {}).items():
                for session, slots in sessions.items():
                    if (day_locks.get(iso) or {}).get(session):
                        kept.setdefault(iso, {})[session] = slots
            month["day_slots"] = kept
            month["day_report"] = ""   # 舊報告已與清除後不符 → 一併清掉，避免誤導
        self.update_month(ym, _mut)

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
                          sessions, closed: bool = True) -> dict:
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
        room = str(room)
        sessions = [s for s in (sessions or []) if s]
        # 以「模板原始開診」判斷該室哪些日/時段真的有開，只對那些寫 override，
        # 避免對本來就沒開這室的日子（週末/假日/非該診週幾/週三下午）塞垃圾。
        template = self.storage.load_clinic_template().get("template") or {}
        base = month_grid(ym, template, self.storage.holidays_set())

        def _mut(month) -> dict:
            if month.get("finalized"):
                raise FinalizedMonthError(
                    f"{ym} 已定案（唯讀）；解除定案後才能改門診")
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
                        continue                  # 該日該時段本來就沒開這室 → 跳過
                    sess = ov.setdefault(iso, {}).setdefault(session, {})
                    lst = sess.setdefault("closed_rooms", [])
                    if closed and room not in lst:
                        lst.append(room)
                    elif not closed and room in lst:
                        lst.remove(room)
                    if not lst:                   # 清理空殼，grid_overrides 不留垃圾
                        sess.pop("closed_rooms", None)
                    if not sess:
                        ov[iso].pop(session, None)
                    # 停診 → 清掉既有班表中該診間的人。未鎖定才動(尊重鎖定契約);
                    # 鎖定時段若正排著該診間的人,不無聲刪除 → 收集回報使用者
                    # 自行處理(RS-05)。
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
                # [RS-03] 有清掉指派 → 舊 day_report 已與現況不符,一併清空
                # 避免幽靈化。
                month["day_report"] = ""
            # [RS-05] 停診/恢復是影響班表的動作,留 audit 痕跡。
            self._audit(month, "day",
                        f"closure:{room} {start.isoformat()}~{end.isoformat()} "
                        f"{sorted(sessions)}",
                        None, "closed" if closed else "open", "closure")
            return {"cleared": cleared, "skipped_locked": skipped_locked}

        return self.update_month(ym, _mut)

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

    # ── 週六切片（R2/R3 輪排，2026-07-13）────────────────────────────────
    @staticmethod
    def _biopsy_overrides(month: dict) -> dict:
        """[2026-07-27 使用者] 月檔內「手動指定的週六切片人選」→ {date: mid}。
        壞鍵略過（與其他讀取容錯一致）。"""
        out: dict = {}
        for iso, mid in (month.get("biopsy_override") or {}).items():
            if not mid:
                continue
            try:
                out[date.fromisoformat(iso)] = str(mid)
            except (ValueError, TypeError):
                continue
        return out

    def _prev_month_friday_duty(self, year: int, month: int) -> dict:
        """月初 1 號是週六時,回 {上月最後一天(週五): 值班人};其餘情況回空 dict。

        只在真的需要時才去讀上月月檔(避免每次重排都多一次磁碟 IO);讀不到就回空 ——
        沒有上月資料時「週五連動」單純不生效,不可因此讓整個切片重排失敗。
        """
        first = date(year, month, 1)
        if first.weekday() != 5:            # 1 號不是週六 → 週五在本月,不必跨月
            return {}
        fri = first - timedelta(days=1)
        prev_ym = f"{fri.year:04d}-{fri.month:02d}"
        try:
            prev = self.storage.load_month(prev_ym)
        except Exception:
            logging.debug("[roster.service] 讀上月月檔失敗(週五連動略過)", exc_info=True)
            return {}
        cell = ((prev.get("r_duty") or {}).get(fri.isoformat()) or {})
        person = cell.get("person")
        return {fri: str(person)} if person else {}

    def _biopsy_compute(self, ym: str, duty_by_date: dict,
                        book: "dict | None" = None,
                        month: "dict | None" = None) -> tuple:
        """以指定值班表計算該月週六切片
        → (assign, notes, pair, counts_after, names)。

        counts 基底＝biopsy.json 回滾本月舊分錄後的累計（同月重排不重複累計；
        在副本上回滾，不動傳入的 book）。純計算，不寫任何檔。"""
        from cmuh_common.roster.saturday_biopsy import rollback_biopsy
        cfg = self.storage.load_config()
        members = [Member.from_dict(d) for d in (cfg.get("r_members") or [])]
        y, m = int(ym[:4]), int(ym[5:7])
        # ★[2026-08-02 補審] 跨月週五收攏在這裡,而不是各呼叫端自己補★
        #   放在 recompute 那邊的話,report 預覽(render_report)這條路徑就沒有,
        #   於是「月初 1 號是週六」的月份會出現【預覽的切片人選與定案後不同】。
        #   本函式是所有切片計算的唯一入口,補在這裡才不會有人漏掉。
        duty_by_date = dict(duty_by_date)
        duty_by_date.update(self._prev_month_friday_duty(y, m))
        # [2026-07-27] month 可由呼叫端傳入【記憶體中尚未存檔的月檔】——手動指定
        # 切片後若這裡自行重讀磁碟，會讀到舊的 override 而把指定吃掉。
        if month is None:
            month = self.storage.load_month(ym)
        leaves = _parse_date_map((month.get("leaves") or {}).get("r") or {})
        if book is None:
            book = self.storage.load_biopsy()
        base = {"counts": dict(book.get("counts") or {}),
                "history": [dict(e) for e in (book.get("history") or [])]}
        rollback_biopsy(base, ym)
        assign, notes = assign_saturday_biopsy(
            year=y, month=m, members=members, duty=duty_by_date,
            leaves=leaves, counts=base["counts"],
            last_person=last_assigned_before(book, ym),
            overrides=self._biopsy_overrides(month))
        pair, _ = biopsy_pair(members)
        counts_after = dict(base["counts"])
        for cell in assign.values():
            mid = cell["person"]
            counts_after[mid] = int(counts_after.get(mid, 0)) + 1
        names = {mm.id: (mm.name or mm.id) for mm in members}
        return assign, notes, pair, counts_after, names

    def recompute_saturday_biopsy(self, ym: str,
                                  month: "dict | None" = None) -> tuple:
        """依月檔現況（r_duty）重排週六切片並結算計數帳本。

        month 傳入 → 就地更新 month["saturday_biopsy"]、【不寫任何檔】，回
        (assign, notes, book, book_rev)——呼叫端 save_month 成功後再
        save_biopsy(book, expected_revision=book_rev)
        （月檔是 gate：定案擋下時計數帳本不得先行落地）。
        month=None → 自行 load；save_month 成功後 save_biopsy。

        ★整段在同一個臨界區內★(外審排班 RS-5 第 2 輪 P1-2):自行 load 的那條
        路要寫【月檔與切片帳本兩個檔】,兩者之間他機更新 biopsy.json 的話,
        CAS 正確擋下切片帳本、月檔卻已經落地 —— 兩個檔當場互相矛盾,而且
        沒有任何人會發現(之後的切片平衡全部以錯的次數為基礎)。
        傳入 month 的那條路由呼叫端持有同一個臨界區(RLock,可重入)。"""
        with self.storage.write_barrier():
            return self._recompute_saturday_biopsy_locked(ym, month)

    def _recompute_saturday_biopsy_locked(self, ym: str,
                                          month: "dict | None") -> tuple:
        own = month is None
        _own_rev = None
        if own:
            # ★編輯基底一律用嚴格快照★(外審排班 RS-6):寬鬆載入對壞檔回一份
            #   預設空月檔,而它的 revision 就取自那份壞內容 —— CAS 兩邊對得上
            #   就放行,整月被寫成只剩這次重排的結果。
            month, _own_rev = self.storage.load_month_snapshot(ym)
        # ★切片帳本也要 CAS★(外審排班 RS-5 第 1 輪 P1-2):兩台同時改 R 值班或
        #   手動指定切片時,月檔各自被 `update_month` 保住了,但最後的
        #   `save_biopsy` 沒有 revision —— 後寫的會整份蓋掉先寫的切片計數與
        #   結算歷史,之後的切片平衡全部以錯的次數為基礎。
        book_rev = self.storage.canonical_revision("biopsy.json")
        # ★[2026-08-02 補審 第1輪] 要拒絕就得在【改動 month 之前】拒絕★
        #   settle_biopsy 的守門原本要到函式尾端才拋,那時 month["saturday_biopsy"]
        #   與 report_r 都已經改過了;而呼叫端(set_cell / set_biopsy_person /
        #   clear_unlocked)一律把例外當成可略過並【照樣存檔】——於是月檔被改了、
        #   biopsy.json 的次數沒動,兩邊從此不一致,而且使用者完全看不到。
        book = self.storage.load_biopsy()
        if not biopsy_can_rollback(book, ym):
            raise ValueError(
                f"{ym} 早於切片帳本保留的最舊月份，無法安全重算切片次數"
                f"（重記會重複計入）。該月的切片人選維持原樣。")
        y, m = int(ym[:4]), int(ym[5:7])
        duty_by_date: dict = {}
        for iso, cell in (month.get("r_duty") or {}).items():
            p = (cell or {}).get("person")
            if not p:
                continue
            try:
                dt = date.fromisoformat(iso)
            except (ValueError, TypeError):
                continue
            if (dt.year, dt.month) == (y, m):
                duty_by_date[dt] = str(p)
        # ★[2026-08-02 補審 P2] 月初就是週六時,它的週五在【上個月】★
        #   本迴圈把非當月日期全部丟掉,於是正式服務路徑永遠看不到那個週五 ——
        #   週五連動在「月初 1 號是週六」的月份完全失效。
        #   (我原本的測試直接把 7/31 塞進純函式的 duty,沒走服務層,因此沒抓到。)
        assign, notes, pair, after, names = self._biopsy_compute(
            ym, duty_by_date, book, month=month)
        month["saturday_biopsy"] = {
            d.isoformat(): dict(cell) for d, cell in assign.items()}
        # [codex P2] 已存決策報告的[週六切片]段同步刷新——否則手改週六格/請假後,
        # 報告(與定案 PDF 讀的 report_r)仍印舊切片人選,與月檔/匯出不一致。
        rpt = month.get("report_r") or ""
        if rpt and pair:
            section = format_biopsy_section(assign, notes, after, pair, names)
            i = rpt.find("[週六切片]")
            base_rpt = rpt[:i].rstrip() if i >= 0 else rpt.rstrip()
            month["report_r"] = ((base_rpt + "\n\n" + section)
                                 if base_rpt else section)
        settle_biopsy(book, ym, assign)
        if own:
            # 自行 load 的那條路:寫回時 CAS —— 這份是【整份重算後的月檔】,
            # 沿用舊快照寫回會把他機在重算期間的修改一起退回去。
            self.storage.save_month(ym, month,
                                    expected_revision=_own_rev)
            self.storage.save_biopsy(book, expected_revision=book_rev)
        return assign, notes, book, book_rev

    def render_report(self, scope: str, ym: str, result: SolveResult) -> str:
        """以目前 storage 狀態重建 ctx，產生 result 的四段式決策報告（純字串）。
        R 排班另附「週六切片」預覽段（依 result 的值班連動＋次數平衡，未落地）。"""
        ctx = self.build_context(scope, ym)
        base = build_report(ctx, result, _SCOPE_LABEL.get(scope, scope))
        if scope == "r" and result.status == "ok":
            try:
                assign, notes, pair, after, names = self._biopsy_compute(
                    ym, dict(result.assignments))
                base += "\n\n" + format_biopsy_section(
                    assign, notes, after, pair, names)
            except Exception:
                logging.exception("[roster.service] 週六切片預覽段生成失敗（略過）")
        return base

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
        # ★整段在同一個臨界區內★(外審 RS-2 第 1 輪 P1 的同一個病灶):
        #   `build_context` 讀的是月檔【以外】的名單/假日/年度指定,驗證與
        #   寫入之間被背景 pull 換掉的話,`_result_stale_reason` 驗的是舊資料
        #   而月檔 CAS 又看不到那些檔 —— 兩道關卡一起失效。
        with self.storage.write_barrier():
            self._accept_solution_locked(scope, ym, result)

    def _accept_solution_locked(self, scope: str, ym: str,
                                result: SolveResult) -> None:
        """`accept_solution` 的本體。★呼叫端必須持有 `write_barrier`★"""
        # ★這條路徑【不重試】★(外審排班第 1 輪 P1-01):result 是照【當時的】
        #   名單/請假/指定/鎖定算出來的整批結果,還會連動結算計數帳本 ——
        #   盤上被他機換過之後重讀重套只會蓋掉對方,語意上該做的是拒絕重排。
        #   (下面的 `_result_stale_reason` 擋的是【輸入】變了;CAS 擋的是
        #   【月檔本身】被換過 —— 兩者不是同一件事:他機改的可能是日排班、
        #   停診、audit 這些不進 SolveContext 的欄位。)
        month, _month_rev = self.storage.load_month_snapshot(ym)
        if month.get("finalized"):
            raise FinalizedMonthError(f"{ym} 已定案（唯讀）；解除定案後才能套用排班")

        # result 必須仍符合「當前」輸入才落地：預覽後若 請假/指定/鎖定/名單/假日
        # 任一改動，舊 result 可能把請假者排上或違反新 directive，settle 出的帳本/
        # 報告就與實況脫節。以重建的 ctx 驗證，不符即拒絕、要求重排（寫入前）。
        ctx = self.build_context(scope, ym)
        # ★判準是【整份輸入的指紋】,不是一張手工白名單★(外審排班第 2 輪
        #   P1-04):`_result_stale_reason` 逐項列舉的那六七件事會腐爛 ——
        #   `fixed_weekday`(固定星期)與 `week_colors`(色塊連週)都是 CP-SAT 的
        #   ★硬約束★,卻從來沒被它看過;duty_min/max、帳本、上月尾端也一樣。
        #   預覽開著的期間他機改了其中任一項,舊解照樣落地成一份【違反目前
        #   硬限制】的班表,而且畫面上看不出來。指紋走 dataclass 的欄位本身,
        #   日後有人加一個新輸入欄位,它自動被涵蓋。
        if not result.input_fingerprint:
            raise ValueError(
                "排班結果沒有輸入指紋（可能來自舊版程式或未經求解器產生），"
                "無從確認它是否仍符合目前的名單/請假/固定星期/週色等設定，"
                "請重新排班")
        if result.input_fingerprint != rvs_input_fingerprint(ctx):
            # 白名單降級成★第二層診斷★:能講清楚是哪一項就講,講不出來也照樣
            # 擋下(守衛的判準是指紋,不是「我找不找得到理由」)。
            why = self._result_stale_reason(ctx, result) or "輸入設定已變動"
            raise ValueError(f"排班結果已過期（{why}），請重新排班")
        # 指紋相同 ⇒ 輸入沒變;這一層驗的是【結果與輸入自不自洽】(涵蓋日期、
        # 指定有沒有被採用、點數對不對……),那是另一件事,不可以一起省掉。
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
        report = build_report(ctx, result, _SCOPE_LABEL.get(scope, scope))

        # [週六切片 2026-07-13] R 排班落地 → 以最終月檔 duty（含保留的鎖定格）
        # 重排週六切片並附報告段；biopsy.json 於 save_month 成功後才寫。
        biopsy_book = None
        _bio_rev = None                        # 重排失敗時也要有值(見下方守衛)
        if scope == "r":
            try:
                # 先寫入本次 report(recompute 會在其上刷新/附加[週六切片]段)
                month["report_r"] = report
                assign, notes, biopsy_book, _bio_rev = (
                    self.recompute_saturday_biopsy(ym, month))
                report = month["report_r"]
            except Exception:
                biopsy_book = None
                logging.exception("[roster.service] 週六切片重排失敗"
                                  "（值班照常落地，切片請手動處理）")
        month[f"report_{scope}"] = report

        # 兩個檔都先預檢,讓最常見的失敗(壞檔/鎖檔)發生在任何寫入之前。
        month_path = self.storage._month_path(ym)
        self.storage.assert_readable(month_path)
        self.storage._guard_overwrite(str(month_path))

        # ★跨檔不可能真的原子,所以要選對【順序】與【可收斂性】★
        #   (外審排班 P2-01)舊寫法是「帳本先、月檔後」,中斷會留下
        #   「帳本已結算、月檔還是舊班表」—— 那個方向★沒有辦法自動收斂★:
        #   誰也說不出帳上多出來的那筆該不該退。
        #   反過來「月檔先、帳本後」中斷,留下的是「帳本還沒記上」——
        #   而帳本是【可以從月檔重算出來的衍生物】(`resettle_from_duty`),
        #   所以那個方向永遠救得回來。再配一筆意圖紀錄,下次開程式就自動
        #   收斂(見 `reconcile_pending_settles`),不必靠人記得。
        self.storage.mark_pending_settle(scope, ym)
        try:
            self.storage.save_month(ym, month, expected_revision=_month_rev)
            ledger = self.storage.load_ledger()
            settle_month(ledger, scope, ym, result.points_by_person)
            self.storage.save_ledger(ledger)
            if biopsy_book is not None:
                self.storage.save_biopsy(biopsy_book,
                                         expected_revision=_bio_rev)
        except Exception:
            # 意圖留著 → 下次開程式會用月檔把帳本重建到一致。
            raise
        else:
            self.storage.clear_pending_settle(scope, ym)

    # ── 手動編輯（每次立即存檔 + 審計）──────────────────────────────────
    def set_cell(self, scope: str, ym: str, d: date,
                 person: "str | None", via: str = "manual") -> list:
        """改格（person=None → 清空並移除該格）。回傳改後 quick_validate 警告
        （不阻止儲存，設計文件 §16.4）。"""
        iso = d.isoformat()
        # [週六切片] 手改 R 週六值班 → 值班連動可能改變,同批重排(月檔存檔成功
        # 才寫計數帳本;重排失敗不擋手動改格)。
        # ★[2026-08-02 補審 P2] 週五也要觸發★ 2026-07-27 起切片人選在「次數平手」時
        #   會參考【週五值班】(使用者需求:週六早上切片的人盡量也值週五)。只在週六
        #   觸發的話,改完週五之後月檔/biopsy.json/決策報告/匯出全都還是舊人選,
        #   而且畫面上看不出來 —— 它們與求解結果不一致卻沒有任何提示。
        #   ★月底的週五要重排【下個月】★:它的翌日(下月 1 號)若是週六,
        #   _biopsy_compute 會用到它(見 _prev_month_friday_duty)——只跳過不做,
        #   下月月檔/帳本/報告就會停在舊人選。我第一版只寫「不影響本月」就跳過,
        #   與同一批的跨月修正自相矛盾(補審第 2 輪抓到)。
        _holder: dict = {}
        _next_day = d + timedelta(days=1)
        _same_month_friday = d.weekday() == 4 and _next_day.month == d.month
        # ★週五的隔天【永遠】是週六 —— 要檢查的是跨月,不是星期幾。
        #   寫成 `_next_day.weekday() == 5` 會讓每一次月內的週五修改都額外重排並
        #   再存一次【本月】(_next_ym 就是本月)→ 重複快照與多餘 IO。★
        _cross_month_friday = d.weekday() == 4 and _next_day.month != d.month

        def _mut(month):
            _holder.clear()                    # ★所有早退之前★(見 clear_unlocked)
            duty = month.setdefault(f"{scope}_duty", {})
            old = duty.get(iso)
            old_person = old.get("person") if old else None
            if person is None:
                duty.pop(iso, None)
            else:
                locked = bool(old.get("locked")) if old else False
                duty[iso] = {"person": person, "locked": locked, "source": via}
            self._audit(month, scope, iso, old_person, person, via)
            # ★切片重排也要在 mutator 裡★:它吃的是【這一份】月檔的 r_duty ——
            #   放在外面就會拿第一次讀到的舊快照去算,重試之後月檔與切片帳本
            #   互相對不上(而且沒有人看得出來)。
            if scope == "r" and (d.weekday() == 5 or _same_month_friday):
                try:
                    (_a, _n, _holder["book"],
                     _holder["rev"]) = self.recompute_saturday_biopsy(
                        ym, month)
                except Exception:
                    logging.exception(
                        "[roster.service] set_cell 週六切片重排失敗（略過）")

        # ★月檔與切片帳本要在同一個臨界區內★(外審排班 RS-5 第 2 輪 P1-2):
        #   分開做的話,兩者之間他機更新了 biopsy.json → CAS 正確擋下切片帳本
        #   的寫入,但月檔的新值班/saturday_biopsy 早就落地了 —— 兩個檔當場
        #   互相矛盾,而且沒有任何人會發現(之後的切片平衡全部以錯的次數算)。
        #   臨界區內別人寫不進 biopsy.json,重排時取的 revision 到寫入為止
        #   都還有效,CAS 因此不會在這裡被觸發。
        with self.storage.write_barrier():
            self.update_month(ym, _mut)
            if _holder.get("book") is not None:
                self.storage.save_biopsy(
                    _holder["book"], expected_revision=_holder.get("rev"))
        # 月底週五(翌日是下月 1 號且為週六)→ 重排【下個月】。本月月檔已存好,
        # 這裡才動下月,兩者互不影響;下月沒有月檔就什麼都不做。
        if scope == "r" and _cross_month_friday:
            _next_ym = f"{_next_day.year:04d}-{_next_day.month:02d}"
            try:
                # 下月沒排過 → 不做(不可憑空生出一份月檔);
                # 已定案 → 唯讀,save_month 會丟 FinalizedMonthError,先跳過免噪音。
                if (self.storage.month_exists(_next_ym)
                        and not self.storage.load_month(_next_ym).get("finalized")):
                    self.recompute_saturday_biopsy(_next_ym)
            except Exception:
                logging.exception(
                    "[roster.service] set_cell 跨月週六切片重排失敗（略過）")
        return self.quick_validate(scope, ym)

    def set_biopsy_person(self, ym: str, d: date,
                          person: "str | None") -> list:
        """[2026-07-27 使用者] 右鍵強制指定某週六的切片人選（person=None → 清除
        指定、改回自動排）。回改後 quick_validate("r") 警告（不阻止儲存）。

        指定存在月檔 biopsy_override，之後任何重排（手改值班、請假變動、重跑自動
        排班）都會沿用——否則 set_cell 的連動重排會立刻把手動指定洗掉。
        非週六直接忽略（切片只在週六）。定案月由 save_month 擋下並拋例外。
        """
        if d.weekday() != 5:
            return self.quick_validate("r", ym)
        # ★[2026-08-02 補審 第2輪] 這條路徑仍會存下半套★
        #   原本先寫 biopsy_override 再呼叫重排,重排被擋下時例外被吞掉、
        #   override 卻照樣存進月檔 —— 那個指定【永遠不會生效】(該月已經不能重排),
        #   月檔於是留著一個與 saturday_biopsy 矛盾、又無人能調和的欄位。
        #   本函式的目的就是切片,重排做不到就整個拒絕,由 UI 跳訊息告訴使用者。
        if not biopsy_can_rollback(self.storage.load_biopsy(), ym):
            raise ValueError(
                f"{ym} 早於切片帳本保留的最舊月份，該月的切片已無法重算，"
                f"因此無法指定切片人選（指定了也不會生效）。"
                f"如需調整請直接編輯 biopsy.json。")
        iso = d.isoformat()
        _holder: dict = {}

        def _mut(month):
            _holder.clear()                    # ★所有早退之前★(見 clear_unlocked)
            ov = month.setdefault("biopsy_override", {})
            old = ov.get(iso)
            if person is None:
                ov.pop(iso, None)
            else:
                ov[iso] = str(person)
            if not ov:
                month.pop("biopsy_override", None)
            self._audit(month, "r", f"biopsy:{iso}", old, person, "manual")
            # ★這裡不可以吞例外★(外審排班 RS-5 第 2 輪 P1-1):本函式的目的
            #   就是切片,重排做不到就整個拒絕(上面的守門已經是這個結論)。
            #   吞掉的話 override 照樣存進月檔,而 saturday_biopsy / biopsy.json
            #   停在舊人選 —— 留下一個永遠不會生效、也沒有人看得出來的指定。
            #   ★解包的個數要跟著回傳值走★:少解一個會拋 ValueError,若又被
            #   這裡吞掉,「手動指定」就變成【必然】只寫半套(這一輪我自己
            #   把 3 改成 4 時漏掉這一處,外審當場抓到)。
            (_a, _n, _holder["book"],
             _holder["rev"]) = self.recompute_saturday_biopsy(ym, month)

        # ★月檔與切片帳本在同一個臨界區內★(見 set_cell 的同一條說明)
        with self.storage.write_barrier():
            self.update_month(ym, _mut)        # 定案 → 拋例外,帳本不落地
            if _holder.get("book") is not None:
                self.storage.save_biopsy(
                    _holder["book"], expected_revision=_holder.get("rev"))
        return self.quick_validate("r", ym)

    def clear_unlocked(self, scope: str, ym: str) -> None:
        """清除未鎖定的 R/VS 值班格（保留鎖定格），一次 load/save，並清舊決策報告。

        RF-20：取代 UI 逐格 set_cell（避免整月最多 31 次 load/save + 驗證 +
        GitSync commit 造成 UI 凍結與 commit 洪水）。
        """
        _holder: dict = {}

        def _mut(month) -> bool:
            # ★holder 一律在最前面清空★(外審排班 RS-1 第 1 輪 P1):試算那次
            #   算出來的切片帳本若留在 holder 裡,而正式那次因為他機已經清完
            #   而早退,最後就會把【試算時的】帳本寫進 biopsy.json —— 月檔是
            #   他機的新狀態、切片次數卻退回舊的,之後的平衡全部歪掉。
            #   清空要在【每一個早退之前】,不是在重排之前。
            _holder.clear()
            if month.get("finalized"):
                raise FinalizedMonthError(f"{ym} 已定案（唯讀）")
            duty = month.get(f"{scope}_duty") or {}
            # 與逐格迴圈語意等價：只清「有 person 且未鎖」的格，保留鎖定格與無 person 殘格。
            kept = {iso: c for iso, c in duty.items()
                    if c.get("locked") or not c.get("person")}
            if kept == duty:                   # 沒有未鎖已排格 → 不 save，免空 commit
                return False
            month[f"{scope}_duty"] = kept
            month[f"report_{scope}"] = ""      # 舊報告已與清除後不符 → 一併清掉
            self._audit(month, scope, "clear_unlocked", None, None, "clear")
            # [週六切片] R 值班清除 → 切片依殘餘(鎖定)值班+次數平衡重排,與月檔同批
            if scope == "r":
                try:
                    (_a, _n, _holder["book"],
                     _holder["rev"]) = self.recompute_saturday_biopsy(
                        ym, month)
                except Exception:
                    logging.exception(
                        "[roster.service] clear_unlocked 週六切片重排失敗（略過）")
            return True

        probe, _rev = self.storage.load_month_snapshot(ym)
        if not _mut(probe):                    # 試算:沒有東西可清就不要寫檔
            return
        # ★月檔與切片帳本在同一個臨界區內★(見 set_cell 的同一條說明)
        with self.storage.write_barrier():
            self.update_month(ym, _mut)
            if _holder.get("book") is not None:
                self.storage.save_biopsy(
                    _holder["book"], expected_revision=_holder.get("rev"))

    def toggle_lock(self, scope: str, ym: str, d: date) -> bool:
        """切換鎖定（空格不可鎖）。回傳切換後的鎖定狀態。"""
        iso = d.isoformat()
        _holder: dict = {}

        def _mut(month) -> bool:
            _holder.clear()                    # ★所有早退之前★(見 clear_unlocked)
            duty = month.setdefault(f"{scope}_duty", {})
            cell = duty.get(iso)
            if not cell or not cell.get("person"):
                _holder["empty"] = True
                return False
            cell["locked"] = not cell.get("locked", False)
            self._audit(month, scope, iso,
                        f"locked={not cell['locked']}",
                        f"locked={cell['locked']}", "lock")
            return cell["locked"]

        probe, _rev = self.storage.load_month_snapshot(ym)
        _mut(probe)
        if _holder.get("empty"):               # 空格不可鎖 → 不寫檔
            return False
        return self.update_month(ym, _mut)

    def set_leaves(self, scope: str, ym: str, member_id: str, dates) -> None:
        self._set_date_map(scope, ym, "leaves", member_id, dates)
        # [codex P2] R 請假變動影響週六切片（平衡候選排除/值班連動的請假優先）
        # → 同步重排＋刷新報告段；定案月 _set_date_map 已先拋,不會走到這裡。
        if scope == "r":
            try:
                self.recompute_saturday_biopsy(ym)
            except Exception:
                logging.exception(
                    "[roster.service] set_leaves 週六切片重排失敗（略過）")

    def set_must(self, scope: str, ym: str, member_id: str, dates) -> None:
        self._set_date_map(scope, ym, "must_duty", member_id, dates)

    def rename_member(self, scope: str, old_id: str, new_id: str) -> int:
        """把某 scope 成員的代號 old_id 連動改成 new_id（跨所有資料一次到位）。回傳異動處數。

        代號是帳本/值班/請假/切片計數的主鍵，單改名單會讓其餘資料仍指向舊鍵而斷鏈，故集中在本層
        一次改齊：config 名單、ledger（餘額+本 scope history deltas）、【所有月份】的 duty.person /
        leaves[scope] / must_duty[scope] / last_weekend[scope].person（含已定案月 → force）、
        holiday_duty[scope] 指定值、R 專屬 saturday_biopsy.person 與 biopsy.json（counts+history）。

        [codex] 交易式：先【全部載入】（各檔的 schema/讀取檢查在此就炸，尚未寫任何檔）＋守門，再於
        記憶體改好，最後逐檔寫；任一寫入失敗即【回滾】已寫的檔，不留半套改名。guard：new_id 去空白
        後不可空、不可撞現有成員，且不可已存在於任何「以 id 為鍵」的歷史資料（帳本餘額/分錄/切片
        計數/請假/指定）→ 避免蓋掉離職者的歷史紀錄。
        """
        # ★整段包在同一個臨界區內★(外審排班第 2 輪 P1-01):改名一次要動
        #   config + 帳本 + 假日指定 + 切片帳本 + 所有月份 —— Phase 1「全部
        #   載入」到 Phase 3「逐檔寫入」之間,背景 pull 隨時可以換掉其中任何
        #   一個檔,而只有月檔那幾筆有 CAS。臨界區讓「載入 → 改 → 逐檔寫 →
        #   需要時回滾」整段看見同一份盤面。
        with self.storage.write_barrier():
            return self._rename_member_locked(scope, old_id, new_id)

    def _rename_member_locked(self, scope: str, old_id: str,
                              new_id: str) -> int:
        """`rename_member` 的本體。★呼叫端必須持有 `write_barrier`★"""
        old_id = str(old_id)
        new_id = str(new_id).strip()
        if not new_id:
            raise ValueError("新代號不可空白")
        if new_id == old_id:
            return 0

        # ── Phase 1：全部載入（讀取/schema 錯誤在此就炸，尚未寫任何檔）＋守門 ──────────
        # [codex P1] 先嚴格預檢每個要改寫的檔：storage 一般 load_* 用的 _load_json 會把壞檔/鎖檔
        # 靜默當成空 dict，交易式改名若照寫就會用【空白覆蓋】壞檔而靜默清空帳本/月檔。壞檔 → 中止。
        yms = self.storage.iter_month_yms()
        preflight = ["config.json", "ledger.json", "holiday_duty.json"]
        if scope == "r":
            preflight.append("biopsy.json")
        for name in preflight:
            self.storage.assert_readable(name)
        for ym in yms:
            self.storage.assert_readable(self.storage._month_path(ym))

        cfg = self.storage.load_config()
        members = cfg.get(f"{scope}_members") or []
        ids = [str(m.get("id")) for m in members]
        if old_id not in ids:
            raise ValueError(f"{scope.upper()} 名單中找不到代號 {old_id}")
        if new_id in ids:
            raise ValueError(f"代號 {new_id} 已存在於 {scope.upper()} 名單，不可重複")
        ledger = self.storage.load_ledger()
        holiday = self.storage.load_holiday_duty()
        biopsy = self.storage.load_biopsy() if scope == "r" else None
        _loaded = {ym: self.storage.load_month_snapshot(ym)
                   for ym in yms}
        months = {ym: mr[0] for ym, mr in _loaded.items()}
        month_revs = {ym: mr[1] for ym, mr in _loaded.items()}

        # [codex] new_id 必須是「全新」代號：不可已出現在任何歷史資料——無論當【鍵】(帳本/切片
        # 計數/請假指定，覆蓋會蓋掉離職者紀錄) 或當【值】(值班/國定假日/last_weekend/週六切片/切片
        # 分錄的 person，混用會讓 old_id 與某離職者 new_id 的歷史混為一人無法辨識)。
        clashes = set()
        if new_id in (ledger.get(scope) or {}):
            clashes.add("帳本餘額")
        for e in (ledger.get("history") or []):
            if e.get("scope") == scope and new_id in (e.get("deltas") or {}):
                clashes.add("帳本分錄")
        if scope == "r":
            if new_id in ((biopsy or {}).get("counts") or {}):
                clashes.add("切片計數")
            for e in ((biopsy or {}).get("history") or []):
                if new_id in (e.get("assign") or {}).values():
                    clashes.add("切片分錄")
        if new_id in (holiday.get(scope) or {}).values():
            clashes.add("國定假日指定")
        for m in months.values():
            for mk in ("leaves", "must_duty"):
                if new_id in ((m.get(mk) or {}).get(scope) or {}):
                    clashes.add("請假/指定")
            for cell in (m.get(f"{scope}_duty") or {}).values():
                if cell.get("person") == new_id:
                    clashes.add("值班紀錄")
            if ((m.get("last_weekend") or {}).get(scope) or {}).get("person") == new_id:
                clashes.add("last_weekend")
            if scope == "r":
                for cell in (m.get("saturday_biopsy") or {}).values():
                    if cell.get("person") == new_id:
                        clashes.add("週六切片")
        if clashes:
            raise ValueError(
                f"代號 {new_id} 已出現在歷史資料（{'、'.join(sorted(clashes))}），"
                f"為免與離職者紀錄混同，請改用全新代號")

        # 回滾用原內容（deepcopy 必須在改動【之前】拍下）
        snap = {"config": copy.deepcopy(cfg), "ledger": copy.deepcopy(ledger),
                "holiday": copy.deepcopy(holiday),
                "biopsy": copy.deepcopy(biopsy) if biopsy is not None else None,
                "months": {ym: copy.deepcopy(m) for ym, m in months.items()}}

        # ── Phase 2：記憶體改好（收集待寫檔）──────────────────────────────────────
        changed = 0
        for m in members:                                   # 1) config 名單
            if str(m.get("id")) == old_id:
                m["id"] = new_id
                changed += 1
        book = ledger.setdefault(scope, {})                 # 2) ledger 餘額 + 本 scope deltas
        if old_id in book:
            book[new_id] = book.pop(old_id)
            changed += 1
        for e in (ledger.get("history") or []):
            if e.get("scope") == scope:
                deltas = e.get("deltas") or {}
                if old_id in deltas:
                    deltas[new_id] = deltas.pop(old_id)
                    changed += 1
        sub = holiday.get(scope) or {}                      # 5) holiday_duty[scope] 指定值
        holiday_changed = False
        for d, mid in list(sub.items()):
            if mid == old_id:
                sub[d] = new_id
                changed += 1
                holiday_changed = True
        biopsy_changed = False
        if scope == "r":                                    # 4) biopsy.json
            counts = biopsy.get("counts") or {}
            if old_id in counts:
                counts[new_id] = counts.pop(old_id)
                changed += 1
                biopsy_changed = True
            for e in (biopsy.get("history") or []):
                assign = e.get("assign") or {}
                for iso, mid in list(assign.items()):
                    if mid == old_id:
                        assign[iso] = new_id
                        changed += 1
                        biopsy_changed = True
        touched_months = []                                 # 3) 所有月份
        for ym, month in months.items():
            touched = False
            for cell in (month.get(f"{scope}_duty") or {}).values():
                if cell.get("person") == old_id:
                    cell["person"] = new_id
                    touched = True
                    changed += 1
            for mapkey in ("leaves", "must_duty"):
                sm = (month.get(mapkey) or {}).get(scope) or {}
                if old_id in sm:
                    sm[new_id] = sm.pop(old_id)
                    touched = True
                    changed += 1
            lw = (month.get("last_weekend") or {}).get(scope) or {}
            if lw.get("person") == old_id:
                lw["person"] = new_id
                touched = True
                changed += 1
            if scope == "r":
                for cell in (month.get("saturday_biopsy") or {}).values():
                    if cell.get("person") == old_id:
                        cell["person"] = new_id
                        touched = True
                        changed += 1
                # [2026-07-27] 手動指定的切片人選同樣是以代號為鍵 → 一起改名，
                # 否則改代號後指定指向不存在的人，重排時會被當「不在名單」丟掉。
                for iso, mid in list((month.get("biopsy_override")
                                      or {}).items()):
                    if mid == old_id:
                        month["biopsy_override"][iso] = new_id
                        touched = True
                        changed += 1
            if touched:
                touched_months.append(ym)

        # ── Phase 3：逐檔寫入；任一失敗即回滾已寫的檔（不留半套改名）──────────────────
        # 每一筆:(標籤, 新資料, 舊資料, 寫入函式, ★還原函式★)。
        # ★還原不可以帶 CAS★:回滾時盤上那一份【就是我們剛寫進去的】,
        #   拿原始 revision 去比一定不符 —— 那會讓回滾自己失敗、留下半套改名。
        _cfg = lambda d: self.storage.save_config(d)          # noqa: E731
        _led = lambda d: self.storage.save_ledger(d)          # noqa: E731
        writes = [("config", cfg, snap["config"], _cfg, _cfg),
                  ("ledger", ledger, snap["ledger"], _led, _led)]
        if holiday_changed:
            _hol = lambda d: self.storage.save_holiday_duty(d)  # noqa: E731
            writes.append(("holiday", holiday, snap["holiday"], _hol, _hol))
        if biopsy_changed:
            _bio = lambda d: self.storage.save_biopsy(d)      # noqa: E731
            writes.append(("biopsy", biopsy or {}, snap["biopsy"],
                           _bio, _bio))
        for ym in touched_months:
            writes.append((
                f"month:{ym}", months[ym], snap["months"][ym],
                (lambda y, rev: lambda d: self.storage.save_month(
                    y, d, force=True, expected_revision=rev))(
                        ym, month_revs.get(ym, "")),
                (lambda y: lambda d: self.storage.save_month(
                    y, d, force=True))(ym)))
        done = []
        try:
            for _label, new_data, old_data, save_fn, restore_fn in writes:
                save_fn(new_data)
                done.append((old_data, restore_fn))
        except Exception:
            logging.exception("[roster.service] 改名寫入失敗，回滾已寫的 %d 檔", len(done))
            for old_data, save_fn in reversed(done):
                try:
                    save_fn(old_data)
                except Exception:
                    logging.exception("[roster.service] 改名回滾失敗，資料可能半套，"
                                      "請從 .bak 快照人工還原")
            raise

        logging.info("[roster.service] 代號連動改名 %s/%s → %s（%d 處）",
                     scope, old_id, new_id, changed)
        return changed

    def reconcile_pending_settles(self) -> list:
        """開程式時把「沒確認完成的結算」用月檔重建到一致。→ 已收斂的清單。

        (外審排班 P2-01)`accept_solution` / `finalize` 要寫月檔與帳本兩個檔,
        中斷會留下不一致。順序刻意是「月檔先、帳本後」,所以中斷後帳本只會
        【落後】—— 用 `resettle_from_duty`(以月檔的實際排班重算)就能救回來。
        已定案的月份仍是唯讀,重算會被拒 → ★那一筆意圖留著並記 error★,
        不可以靜默清掉(清掉就等於宣稱已經一致了)。
        """
        out: list = []
        for item in self.storage.load_pending_settles():
            scope = str(item.get("scope") or "")
            ym = str(item.get("ym") or "")
            if not scope or not ym:
                self.storage.clear_pending_settle(scope, ym)
                continue
            try:
                # ★整段在臨界區內★(外審排班 RS-4 第 1 輪 P2):`resettle_from_duty`
                #   會「讀帳本 → 依月檔重算 → 寫回」,而開程式的當下 GitSync 正在
                #   做啟動 pull/補推 —— 中間被 merge 換掉帳本的話,寫回去的是手上
                #   那份舊的,他機剛同步進來的結算就靜默消失(而且我們接著還把意圖
                #   清掉,等於宣稱已經一致)。
                with self.storage.write_barrier():
                    self.resettle_from_duty(scope, ym)
                    self.storage.clear_pending_settle(scope, ym)
            except Exception:
                logging.exception(
                    "[roster.service] ★%s %s 的結算無法自動收斂★ 帳本可能仍與"
                    "月檔不一致,意圖紀錄保留,請人工確認", scope, ym)
                continue
            out.append((scope, ym))
            logging.warning("[roster.service] 上次未完成的結算已用月檔重建:"
                            "%s %s", scope, ym)
        return out

    def resettle_from_duty(self, scope: str, ym: str, *,
                           retries: int = 4) -> dict:
        """以目前月檔『實際排班』（含手動調整/換班）重算該 scope 帳本。

        自動回滾同月同 scope 舊分錄再重記 → 帳本永遠反映最終排班（accept 之後
        又手改的格也算進去）。回傳每人本月點數。

        名單清空時仍會 settle（points 空 → 回滾該月舊分錄、不留殘餘）。已定案
        月份唯讀，拒絕重算。

        ★整段在同一個臨界區內,而且月檔只讀一次★(外審排班 RS-6 / 第 2 輪
        P1-03):定案判斷、算點數用的 duty、建 context 用的月檔原本是三次
        獨立讀取 —— 中間被背景同步換掉的話,會用 A 版的班表算出帳本、卻對著
        B 版做決定,而兩邊都「看起來成功」。

        ★臨界區只鎖得住這一個行程★(RS-6 第 1 輪 P1):跨機的那一半靠 CAS ——
        所以這裡不是「鎖起來就沒事」,而是「整批用同一份月檔算完,再用它的
        revision 把月檔寫回去」;寫回時被搶先就★整批重來★(帳本的結算本身
        是冪等的:`settle_month` 會先回滾同月同 scope 的舊分錄)。
        """
        last: "StaleRosterDataError | None" = None
        with self.storage.write_barrier():
            for _ in range(max(1, int(retries))):
                # [2026-07-25 審查/codex] 來源月檔讀不到就不可以算:寬鬆載入回
                # 空會算出「全月 0 點」,settle_month 把真正的舊分錄回滾掉,而
                # save_ledger 寫的是另一個檔(守門看不到來源有問題)→ 帳本被清
                # 成零、UI 還報成功;定案判斷也會因為讀到 finalized=False 而
                # 失效。★嚴格快照把「確認讀得到」與「拿來用的內容」變成同一次
                # 讀取★
                month, rev = self.storage.load_month_snapshot(ym)
                try:
                    return self._resettle_locked(scope, ym, month, rev)[0]
                except StaleRosterDataError as e:
                    last = e
                    logging.info("[roster] %s 重算帳本時盤上已更新 → 整批重來",
                                 ym)
                    continue
        assert last is not None
        raise last

    def _resettle_locked(self, scope: str, ym: str, month: dict,
                         month_rev) -> "tuple[dict, str]":
        """→ (每人點數, ★算它用的那份 duty 的識別★)。呼叫端必須持有臨界區。

        回傳識別是給 `finalize` 用的:它要在標定案之前確認「被標成唯讀的
        那一份月檔」就是「剛剛拿來算帳本的那一份」。
        ★切片重排也用【這一份】月檔★:讓它自行 load 的話,它讀到的可能是
        他機剛寫進來的另一版 —— 帳本結算自 A 版、月檔與切片帳本卻存成 B 版,
        而整個操作還回報成功(RS-6 第 1 輪 P1)。
        """
        if month.get("finalized"):
            raise FinalizedMonthError(f"{ym} 已定案（唯讀）；解除定案後才能重算帳本")
        ctx = self.build_context(scope, ym, month=month)
        duty = (month.get(f"{scope}_duty") or {})
        points = {m.id: 0 for m in ctx.members}
        y, m = int(ym[:4]), int(ym[5:7])
        for iso, cell in duty.items():
            p = cell.get("person")
            if p not in points:
                continue
            try:
                dt = date.fromisoformat(iso)
            except (ValueError, TypeError):
                continue
            # [2026-07-25 審查/RP3-07] 非當月鍵不計入結算：跨機人工合併/外部編輯可能在
            # 月檔留下鄰月日期,算進去會虛增該人點數 → fair_share 與每人 delta 一起偏掉,
            # 錯誤帳本還會結轉到下個月的排班目標。build_export / recompute_saturday_biopsy
            # / day_course_stats 都有這道過濾,只有這條【真正寫進 ledger.json】的路徑漏了。
            if (dt.year, dt.month) != (y, m):
                logging.warning("[roster.service] %s 月檔含非當月鍵 %s，重算帳本時略過",
                                ym, iso)
                continue
            points[p] += day_point(dt, ctx.holidays, ctx.params)
        # [週六切片] 重算帳本＝以最終實排為準 → 切片同步重排(含 finalize 前重算)
        #   ★用手上這一份月檔重排,不讓它自己再讀一次★(見 docstring)。
        book = book_rev = None
        if scope == "r":
            try:
                _a, _n, book, book_rev = self.recompute_saturday_biopsy(
                    ym, month)
            except Exception:
                book = book_rev = None
                logging.exception("[roster.service] resettle 週六切片重排失敗（略過）")
        # ★寫入順序:月檔 → 帳本 → 切片帳本★(RS-4 定下的可收斂方向);
        #   月檔的 CAS 是這一批的閘門 —— 被搶先就在這裡失敗,呼叫端整批重來,
        #   帳本與切片帳本都還沒動。
        self.storage.mark_pending_settle(scope, ym)
        self.storage.save_month(ym, month, expected_revision=month_rev)
        # ★帳本也要 CAS★:他機剛結算完的別月分錄不可以被這份舊快照吃掉。
        self.update_ledger(lambda led: settle_month(led, scope, ym, points))
        if book is not None:
            self.storage.save_biopsy(book, expected_revision=book_rev)
        self.storage.clear_pending_settle(scope, ym)
        return points, _duty_digest(month, scope)

    def finalize(self, ym: str, on: bool) -> None:
        """定案/解除定案。解除需覆寫已定案月檔 → 一律 force=True。

        定案時：以最終（含手動調整/換班）的 R/VS 排班重算帳本，確保帳本＝實況。

        ★整段在同一個臨界區,而且「算帳本用的月檔」與「被標定案的月檔」要是
        同一份★(外審排班 RS-6 / 第 2 輪 P1-03):兩者分開讀的話,他機在中間
        存進來的班表會變成「帳本＝舊班表、定案的是新班表」—— 而定案之後是
        唯讀的,只能靠解除定案才救得回來。臨界區擋住背景同步,標定案之前再
        用 duty 的識別回頭確認一次(★守衛不能只靠推理★)。
        """
        with self.storage.write_barrier():
            self._finalize_locked(ym, on)

    def _finalize_locked(self, ym: str, on: bool) -> None:
        _digests: dict = {}
        _resettled: list = []
        if on:
            # ★先預檢月檔寫得下去,再動帳本★(第二輪外審)
            #   下面 _resettle_locked 會先寫 ledger.json,而最後的
            #   save_month(force=True) 現在可能因為備份失敗而拒寫 —— 那會留下
            #   「帳本已重算、月份沒定案」的半套,而 UI 只說「定案失敗」。
            self.storage.preflight_required_backup(
                str(self.storage._month_path(ym)))
            m0, _m0_rev = self.storage.load_month_snapshot(ym)
            hist = self.storage.load_ledger().get("history") or []
            settled = {h.get("scope") for h in hist if h.get("month") == ym}
            for scope in ("r", "vs"):
                # 有排班、或本月已有結算（可能被清空 → 需回滾）都要重算。
                # 重算失敗即讓例外上拋 → 中止定案（不留「已定案但帳本沒更新」的
                # 半套狀態）；UI 會攔截顯示錯誤並還原定案勾選。
                if m0.get(f"{scope}_duty") or scope in settled:
                    # ★意圖紀錄★(外審排班 P2-01):這裡帳本先寫、月檔(定案旗標)
                    #   後寫,中途中斷會留下「帳本已重算、月份沒定案」。定案本身
                    #   不是資料損壞,但帳本與月檔的一致性仍要能自動收斂 ——
                    #   留一筆意圖,下次開程式用月檔重建(見
                    #   `reconcile_pending_settles`)。
                    self.storage.mark_pending_settle(scope, ym)
                    # ★每個 scope 各自取最新快照★:前一個 scope 的重算會把月檔
                    #   寫回去(切片/報告),沿用 m0 的 revision 會被自己的寫入
                    #   擋下來。最後的識別比對仍會確認兩個 scope 的班表都沒變。
                    m, rev = self.storage.load_month_snapshot(ym)
                    _digests[scope] = self._resettle_locked(scope, ym, m, rev)[1]
                    _resettled.append(scope)
        # 重讀是必要的:上面的切片重排會把 saturday_biopsy/報告寫進月檔。
        month, _rev = self.storage.load_month_snapshot(ym)
        for scope, dig in _digests.items():
            if _duty_digest(month, scope) != dig:
                raise StaleRosterDataError(
                    f"{ym} 的 {scope.upper()} 班表在重算帳本之後又變動了，"
                    f"已中止定案（否則會定案在一份與帳本不符的班表上）。"
                    f"請重新整理後再試一次。")
        month["finalized"] = bool(on)
        self._audit(month, "-", ym, None, f"finalized={bool(on)}", "finalize")
        # 定案路徑上方已經 preflight_required_backup 過（快照就在那時留下的）。
        # 這裡若再要求一次，兩次之間檔案被鎖住就仍會留下「帳本已重算、月份沒定案」
        # 的半套 —— 預檢的意義就沒了（外審第 10 輪）。
        from cmuh_common.roster.storage import BEST_EFFORT_BACKUP
        # ★定案也要 CAS★:它把整份月檔寫回去(含 duty/day_slots/…),
        #   他機在這中間存進來的修改會被一起蓋掉,而定案的結果是唯讀 ——
        #   之後只能靠解除定案才救得回來。被搶先就拒絕,重來一次即可。
        self.storage.save_month(ym, month, force=True,
                                backup=BEST_EFFORT_BACKUP if on else None,
                                expected_revision=_rev)
        if on:
            # ★只清【自己真的重算過】的那幾個 scope★:無條件清掉會把別人
            #   (例如上一次中斷的 accept)留下的意圖一起抹掉 —— 那等於宣稱
            #   「已經一致了」,而我們並沒有為它做任何事。
            for scope in _resettled:
                self.storage.clear_pending_settle(scope, ym)

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
        if scope == "r":
            checks.extend(self._biopsy_checks(ctx, ym))
        return checks

    def _biopsy_checks(self, ctx: SolveContext, ym: str) -> list:
        """[週六切片] 驗證層安全網：缺 R2/R3 級提示；值班連動不符警告
        （改格/重排會自動重算,此處只補「外部途徑改壞」的把關）。"""
        out: list = []
        if not biopsy_can_rollback(self.storage.load_biopsy(), ym):
            # 不只是「不重算」——要讓使用者知道這個月的切片次數不再會自動維護。
            out.append(Precheck(
                "warn", "saturday_biopsy",
                f"{ym} 早於切片帳本保留的最舊月份，切片人選不會再自動重算"
                f"（改值班/請假都不會連動）；如需調整請直接編輯 biopsy.json。"))
        pair, notes = biopsy_pair(ctx.members)
        for n in notes:
            out.append(Precheck("info", "saturday_biopsy", n))
        if not pair:
            return out
        pair_ids = {m.id for m in pair}
        month = self.storage.load_month(ym)
        duty = month.get("r_duty") or {}
        for iso, cell in sorted((month.get("saturday_biopsy") or {}).items()):
            dp = (duty.get(iso) or {}).get("person")
            bp = (cell or {}).get("person")
            try:
                dd = date.fromisoformat(iso)
            except (ValueError, TypeError):
                continue
            # 值班連動不符(值班者未請假才要求連動 —— 請假優先於連動)
            if (dp in pair_ids and bp != dp
                    and dd not in (ctx.leaves.get(dp) or set())):
                out.append(Precheck(
                    "warn", "saturday_biopsy",
                    f"{dd.month}/{dd.day}(六) 值班 {dp} 為 R2/R3,切片應值班連動"
                    f"（目前={bp}）；改該週六格或重新排班會自動重算"))
            # [codex P2] 切片人選當日請假 → 警告(外部途徑改壞月檔的安全網)
            if bp and dd in (ctx.leaves.get(bp) or set()):
                out.append(Precheck(
                    "warn", "saturday_biopsy",
                    f"{dd.month}/{dd.day}(六) 切片 {bp} 當日請假,請重排或手動調整"))
        return out

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
            # [2026-07-27 使用者] 連休段可依使用者指定拆段（見 split_block_runs）
            # → 只要求「同一段內同一人」。舊版要求整個連休段同一人，使用者刻意把
            # 9/25-27 給 Z、9/28 給 K 時，求解成功卻永遠卡在「結果已過期」套用不了。
            for run in split_block_runs(
                    b.days,
                    {d: directives[d][0] for d in b.days if d in directives}):
                persons = {result.assignments.get(x) for x in run}
                if len(persons) > 1:
                    return (f"連休段 {run[0].month}/{run[0].day} 起"
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
        days = sorted(d.isoformat() for d in (dates or set()))

        def _mut(month):
            table = month.setdefault(key, {}).setdefault(scope, {})
            if days:
                table[str(member_id)] = days
            else:
                table.pop(str(member_id), None)
            self._audit(month, scope, f"{key}:{member_id}", None,
                        ",".join(days) or "（清空）", key)
        self.update_month(ym, _mut)

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
        directives, _ = collect_directives(ctx)
        for b in ctx.blocks:
            # [2026-07-27 使用者] 以「依指定拆出的段」為單位檢查——使用者刻意把
            # 連休段指定給不同人時，那是預期結果，不該每次都跳「成對被改破」。
            for run in split_block_runs(
                    b.days,
                    {d: directives[d][0] for d in b.days if d in directives}):
                persons = {assigned.get(d) for d in run}
                span = (f"{run[0].month}/{run[0].day}"
                        + (f"-{run[-1].day}" if len(run) > 1 else ""))
                if persons == {None}:
                    continue                  # 整段尚未排 → 不算「改破」
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
