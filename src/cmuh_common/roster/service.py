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

import contextlib
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
from cmuh_common.roster.ledger import (
    can_rollback, rollback_month, settle_month, sync_members,
)
from cmuh_common.roster.model import (
    ClerkBatch, Member, RosterParams, SolveContext, batches_covering, day_point,
    dedupe_codes, duplicated_codes, roc,
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


def merge_set_edit(current, baseline, edited) -> set:
    """把使用者【真的做過的】增減套到盤上最新的集合上(3-way)。

    ★對話框送回來的整份勾選不是「使用者的意圖」★(外審排班第 2 輪 P1-02):
    視窗開著的期間他機同步進來的請假/名單變動,會被那份開窗當時的快照整包
    覆蓋掉 —— 而且是【合法地】覆蓋:月檔的 CAS 只看得到「整份月檔有沒有被
    換過」,看不到「這個欄位的值是誰的意圖」。使用者按下確定時的意思是
    「我加了這幾天、拿掉了這幾天」,不是「這個月只有這幾天」。

    衝突在集合上不會發生(逐元素獨立):使用者沒碰過的元素保留盤上的;
    使用者拿掉的就拿掉(他看見它、而且明確取消了它)。
    """
    cur, base, new = set(current or ()), set(baseline or ()), set(edited or ())
    return (cur - (base - new)) | (new - base)


def changed_entries(baseline: dict, edited: dict) -> dict:
    """→ 使用者【真的改過】的那幾格(值與 baseline 不同的)。

    沒改過的格子不可以寫回去:輸入框裡顯示的是開窗當時的值,原封送回等於
    把他機在這段期間的修改靜默退回舊值。
    """
    base = dict(baseline or {})
    out = {}
    for k, v in (edited or {}).items():
        if (base.get(k) or None) != (v or None):
            out[k] = v
    return out


def field_changed(new, old) -> bool:
    """這個欄位是不是真的被使用者改動過。

    對話框對「沒填」一律回空字串或 None,而原紀錄可能根本沒有那個鍵 ——
    兩邊都算「沒填」,不可視為一次變更(否則每次按確定都會塞一堆空欄位進去,
    而且會被誤判成與他機衝突)。
    """
    if new in ("", None) and old in ("", None):
        return False
    return new != old


def _grid_keys_digest(grid: dict) -> str:
    """一份切片格網的★鍵集身分★(日期集合的雜湊)。

    (外審 RS-16 R1-2)平移意圖要能回答「這份格網搬過了沒有」。位移 < 14 天時
    新舊視窗重疊,只看日期落點分不出來 —— 記下平移【之前】的鍵集,收斂時比對
    「現況 == 平移前」還是「現況 == 平移前搬過去的樣子」,兩者都不是就不猜。
    """
    return hashlib.sha256(
        "|".join(sorted(str(k) for k in (grid or {}))).encode("utf-8")
    ).hexdigest()[:16]


def _shifted_keys_digest(grid: dict, back_delta: int) -> str:
    """把現況的鍵【往回】搬 back_delta 天之後的身分(壞日期 → 空字串)。

    用來回答「現況是不是【平移前那一份】搬過去的樣子」:把現況往回搬,
    再與記下來的平移前身分比對。
    """
    keys = []
    for iso in (grid or {}):
        try:
            keys.append((date.fromisoformat(str(iso))
                         + timedelta(days=back_delta)).isoformat())
        except (ValueError, TypeError):
            return ""
    return _grid_keys_digest(dict.fromkeys(keys))


def assert_unique_codes(codes, label: str) -> list:
    """名單寫入前的唯一性守門 → 正規化後的名單;有重複就★明確拒絕★。

    (外審 2026-08-21 P1-01)日填充器把 list 的每一個 occurrence 當成一個人:
    `PhotoStep` 只 remove 掉一個 occurrence,同一個代號還留在池子裡,下一步
    就能再選到他 —— 產出「同一人同一時段照光又在治療室」這種【物理上不可能
    執行】的班表,而請假/名單/容量三道檢查全部合法,一路通到定案與匯出。
    ★正常 UI 就打得出來★(輸入「P1、P1」),所以不能只靠 assert:要在存檔的
    邊界擋下並說人話。
    """
    dup = duplicated_codes(codes)
    if dup:
        raise ValueError(
            f"{label}有重複的代號:{'、'.join(dup)}。"
            f"同一個人不能在名單裡出現兩次(排班會把他當成兩個人,"
            f"排出同一時段既照光又在治療室的班表)。請移除重複項目後再存檔。")
    return [str(c) for c in (codes or [])]


def assert_no_cross_roster(codes, others, label: str, other_label: str) -> None:
    """PGY 與 Clerk 的代號★不可交集★(外審 RS-17 R1-1)。

    逐邊唯一還不夠:同一個代號同時出現在 PGY 名單與 Clerk 梯次時,兩邊各自
    都「合法」—— 照光排 PGY A、切片排 Clerk A,存檔與匯出裡就是同一個代號
    出現在同一時段的兩格,誰也分不出那是兩個人還是一個人被排了兩件事。
    """
    clash = [c for c in dedupe_codes(codes) if str(c) in {str(o) for o in
                                                         (others or [])}]
    if clash:
        raise ValueError(
            f"{label}的代號 {'、'.join(clash)} 已經出現在{other_label}。"
            f"同一個代號不能兩邊都有(排班會把他同時排進兩種工作,"
            f"而且存檔之後分不出是誰)。請改掉其中一邊的代號。")


def clerk_batch_key(b: dict) -> str:
    """梯次的查找鍵(id 缺失的舊資料退回 start_monday,UI 與服務層共用一份)。"""
    return str((b or {}).get("id") or (b or {}).get("start_monday") or "")


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
    def solver_ledger(self, scope: str, ym: str) -> dict:
        """求解要看的是【本月結算之前】的餘額。→ {member_id: 餘額}

        (外審排班 RS-9 / 新一輪 P1-02)`ledger.py` 的契約寫得很清楚:正值＝
        之前多值、目標調低,而且「同月重排 = 先 rollback 該月舊分錄再重記」。
        直接讀當下的帳本的話,同一個月按第二次「自動排班」時,solver 看到的是
        【本月第一次班表造成的暫時差額】(例如 A +5 / B -5)—— 它會刻意排出
        一份反向傾斜的新班表去補償;而接受時 `settle_month` 又會把第一次那筆
        rollback 掉,最後反而留下一筆方向相反的欠帳。
        ★只在記憶體裡回滾,磁碟不動★:這裡是求解的輸入,不是結算。
        """
        # ★求解的公平基準也是「會被寫回去的東西」的來源★(外審次輪 P2-01):
        #   寬鬆載入對壞檔回一份空帳本 —— 於是預覽會用「大家都是 0」算出一份
        #   看起來很正常的班表,使用者按下套用之後 `settle_month` 就把那份空的
        #   當成基準寫回去。嚴格快照讓壞檔在【求解之前】就明講,fail closed。
        led_all = copy.deepcopy(
            self.storage.canonical_snapshot("ledger.json")[0])
        if not can_rollback(led_all, ym):
            # 該月分錄可能已被修剪 → 無從確認「本月之前」是多少。寧可擋下
            # 並說清楚(硬猜一個基準會讓之後每個月的公平目標都跟著錯)。
            raise ValueError(
                f"{ym} 比帳本保留的最舊月份還早，無法確認本月結算之前的餘額，"
                f"因此不能重新排班（重排會以錯誤的公平基準計算）。"
                f"如確實需要，請直接調整 ledger.json。")
        rollback_month(led_all, scope, ym)
        # ★餘額 0 與「這個人還沒有分錄」對求解是同一件事★:回滾之後會留下
        #   一堆值為 0 的鍵,而原本那些鍵根本不存在 —— 兩者的求解結果完全
        #   相同,指紋卻不一樣(RS-7 的過期判準會因此誤報「輸入設定已變動」)。
        #   求解端一律 `ledger.get(mid, 0.0)`,所以這裡去掉 0 是等價的正規化。
        return {k: v for k, v in (led_all.get(scope) or {}).items()
                if round(float(v or 0.0), 4) != 0.0}

    def build_context(self, scope: str, ym: str, *,
                      month: "dict | None" = None,
                      for_solve: bool = False) -> SolveContext:
        """讀 config/ledger/holiday_duty/week_colors/month 檔 → 已 prepare 且已套
        跨月銜接（boundary_fix）的 SolveContext。

        boundary_fix 在此就補（不只 solve_duty 內）→ 求解/驗證/過期檢查看到的
        directive 一致；solve_duty 會再冪等呼叫一次，無害。

        `month=` 傳入已載入的月檔 → ★用【呼叫端手上那一份】,不另外再讀一次★
        (外審排班 RS-6):重算帳本時「算點數用的 duty」與「被標定案的月檔」
        必須是同一份,分開讀就可能是兩個版本。

        `for_solve=True` → 帳本改用★本月結算之前★的餘額(見 `solver_ledger`)。
        ★只有求解那條路要這樣★(外審排班 RS-9 第 1 輪):同一個 context 也
        餵給顯示(結算面板)、報告與驗證 —— 那些地方要的是【帳本現在的實際
        餘額】。一律回滾的話,使用者套用排班之後打開分頁只會看到 0/0,
        而設定頁直接讀帳本又顯示 +5/-5,兩邊自相矛盾;更糟的是,那個
        「分錄可能已被修剪」的 fail-closed 會讓【單純想看一個舊月份】的人
        連分頁都打不開。"""
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

        # [外審次輪 P2-03] 週六切片的手動指定/「本週不切片」是 R 求解的輸入
        #   (FridayBiopsyLinkRule 要看它);VS 沒有切片,給空的。
        biopsy_override = (self._biopsy_overrides(month) if scope == "r"
                           else {})
        ledger = (self.solver_ledger(scope, ym) if for_solve
                  else dict(self.storage.load_ledger().get(scope) or {}))
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
            prev_tail=prev_tail, biopsy_override=biopsy_override,
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
        # ★不可以把重複的名單原樣送進 solver★(外審 2026-08-21 P1-01):
        #   寫入端已經擋下,但外部工具/人工合併/舊檔仍可能留著重複代號。
        _dup = duplicated_codes(pgy_roster)
        if _dup:
            logging.warning(
                "[roster.service] %s 的 PGY 名單有重複代號 %s → 已去重"
                "(請到設定頁或當月人員修正)", ym, "、".join(_dup))
            pgy_roster = dedupe_codes(pgy_roster)

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

    def set_day_session(self, ym: str, d: date, session: str, slots: dict, *,
                        baseline: dict) -> int:
        """[RS-06] 改某(日,時段)的格（slots＝{slot: [人]}；空清單→移除該格）。
        一次 load、逐格 diff 才記 audit、一次 save。回傳實際變動的格數。

        ★只寫使用者【真的改過】的格★(外審排班第 2 輪 P1-02):`baseline` 是
        開窗時各格的內容。沒改過的格子原封送回去,等於把他機在這段期間的
        修改靜默退回舊值(輸入框裡顯示的本來就是開窗當時的值)。
        改過的格子若在盤上也被別人改過 → ★明確拒絕★:那一格有兩個互相衝突
        的意圖,程式沒有立場替使用者選一個。
        """
        wanted = changed_entries(baseline, slots)

        def _mut(month):
            sess = (month.setdefault("day_slots", {})
                    .setdefault(d.isoformat(), {}).setdefault(session, {}))
            changed = 0
            for slot, people in wanted.items():
                old = sess.get(slot)
                base = (baseline or {}).get(slot)
                if (old or None) != (base or None):
                    shown = "、".join(old or []) or "（空）"
                    raise StaleRosterDataError(
                        f"{d.month}/{d.day} {session} 的「{slot}」已被其他電腦"
                        f"改成 {shown}，與你這次的修改衝突。"
                        f"請重新整理後再改一次。")
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
        (d)房容量超標、(e)停診房仍有人（兜 RS-03/05 殘留）、
        (f)★合併後才成立的名單身分衝突★(外審 2026-08-22 P2-03:兩台各改
        一個檔,git 乾淨合併但結果違規)—— 它不屬於某一天,卻會讓每一天都
        少一個人,放在同一個警告面板使用者才有機會修。"""
        out: list = list(self.validate_roster_identity_invariants())
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
                # ★同一個人同一時段只能有一個工作★(外審 2026-08-21 P1-01):
                #   名單重複、手改 JSON、手動編輯時段都可能造出「照光又在
                #   治療室」這種物理上做不到的班表 —— 而請假/名單/容量三道
                #   檢查全部合法,不點名的話它會一路通到定案與匯出。
                #   放假格不算工作,但「又放假又有工作」同樣是矛盾 → 一起看。
                _where: dict = {}
                for slot, members in (slots or {}).items():
                    for c in (members or []):
                        _where.setdefault(str(c), []).append(str(slot))
                for c, places in sorted(_where.items()):
                    if len(places) > 1:
                        out.append(f"{iso} {session}:{c} 同時被排在 "
                                   f"{'、'.join(places)} —— 同一個人同一時段"
                                   f"只能做一件事,請確認名單是否有重複代號")
                # 房號型別由 `clinic_closures` 統一正規化(規則只有一份)
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

    class _DerivedIntent:
        """意圖的當次狀態:★呼叫端要能說「這個衍生物其實沒重建成功」★。

        (外審次輪 P2-02)`set_cell` / `resettle` 這些路徑把重排的例外接住並
        照樣存月檔(使用者需求:重排失敗不擋手動改格)—— 於是 context manager
        看到的是正常返回,意圖被清掉,而 `biopsy.json` 根本沒跟著新的班表走。
        意圖代表的是★「衍生物還沒重建成功」★,不是「第二次寫檔還沒做完」。
        """

        def __init__(self):
            self.kept_reason = ""

        def keep(self, reason: str) -> None:
            """衍生物沒重建成功 → 意圖留著,下次開程式收斂。"""
            self.kept_reason = str(reason or "衍生物未重建")

        @property
        def kept(self) -> bool:
            return bool(self.kept_reason)

    @contextlib.contextmanager
    def settle_intent(self, scope: str, ym: str, kind: str = "all"):
        """★兩個檔的寫入之間斷電/當掉,要留得下線索★(外審排班 RS-10 / P2-02)

        月檔與 `biopsy.json`(或帳本)是兩個檔。同一個 `write_barrier` 擋得住
        背景 Git merge 與其他執行緒,★擋不住行程被砍、停電、或第二次寫入的
        I/O 失敗★ —— 那會留下「月檔已換成新的 saturday_biopsy、biopsy.json
        還是舊的」,而且沒有任何紀錄。
        意圖只記 (scope, 月份):帳本與切片計數都是【可以從月檔重算出來的
        衍生物】,下次開程式的 `reconcile_pending_settles` 會用月檔把它們
        重建到一致(收斂不了就保留意圖並告警,不可以靜默清掉)。

        ★離開時只有在沒有例外的情況下才清★:所以這裡刻意不寫 try/finally。
        ★而且只清掉【這一次記下的】那一筆★(外審排班 RS-10 第 1 輪 P1):
        `mark_pending_settle` 是冪等的 —— 已經有一筆的話它不會再記,而那一筆
        屬於【另一個還沒完成的操作】(例如上一次 accept 寫完月檔、帳本卻寫失敗)。
        這條手動路徑★只重算切片,不會重算帳本★,把別人的意圖一併清掉等於替它
        宣稱「已經一致了」,開程式時的收斂從此不會再跑,而帳本就一直錯下去
        (它還是下個月公平目標的基準)。
        """
        mine = self.storage.mark_pending_settle(scope, ym, kind)
        state = RosterService._DerivedIntent()
        yield state
        if mine and state.kept:
            logging.warning(
                "[roster.service] ★%s %s 的%s尚未重建成功★(%s)—— 意圖保留,"
                "下次開程式會用月檔收斂", scope, ym, kind, state.kept_reason)
            return
        if mine:
            self.storage.clear_pending_settle(scope, ym, kind)

    @contextlib.contextmanager
    def biopsy_obligation(self, ym: str, what: str = "週六切片重排"):
        """「先改來源、再重建切片衍生物」的★唯一★包裝(外審 2026-08-22 P2-01)。

        用法:
            with self.biopsy_obligation(ym) as ob:
                mutate_source()                # 請假/值班/梯次…
                try:
                    self.recompute_saturday_biopsy(ym)
                except Exception as e:
                    ob.keep(str(e))            # 來源已改、衍生物沒跟上

        ★不要靠每個呼叫端自己記得補意圖★:`set_leaves` 與跨月週五那兩條路
        原本只 log 就算了 —— 請假已經落地、`saturday_biopsy`/`biopsy.json`
        還停在舊狀態,而且沒有任何人負責收斂(使用者只看到「請假成功」)。
        ★重建成功時什麼都不留★:意圖只在真的失敗時保留。
        """
        with self.settle_intent("r", ym, kind="biopsy") as state:
            try:
                yield state
            finally:
                if state.kept:
                    logging.warning(
                        "[roster.service] ★%s 的%s未完成★(%s)—— 意圖保留",
                        ym, what, state.kept_reason)

    def _biopsy_intent(self, scope: str, ym: str):
        """R 才會寫切片帳本 → 只有 R 需要這一筆意圖(見 `settle_intent`)。

        ★種類是 biopsy★(外審次輪 P2-02):這條路只重建切片計數,帳本沒動 ——
        用 "all" 的話,它會涵蓋(並在成功時清掉)別人未完成的帳本義務。
        """
        if scope != "r":
            # ★仍然吐一個狀態物件★:呼叫端不必為了 VS 分岔寫 if,
            #   對它 .keep() 是無害的(沒有意圖可保留)。
            return contextlib.nullcontext(RosterService._DerivedIntent())
        return self.settle_intent("r", ym, kind="biopsy")

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

    def set_pgy_default_members(self, codes: list, *, baseline) -> list:
        """儲存設定頁的 PGY 預設代號 —— 只套【相對 baseline 的增刪】。→ 存後名單。

        (RS-14,全審次輪 P1-02-C)文字欄是開窗/重載時預填的整串;整份覆寫的
        話,欄位開著的期間他機加進 config 的人會被這次存檔【明確排除】——
        從此不在候選名單,而畫面看不出來。集合語意與 `set_leaves` 同規:
        增與刪都是使用者的意圖,合併到【現在的】名單上。
        ★純調順序要尊重★:集合沒變且盤上沒被動過 → 照使用者排的順序整份採用
        (set delta 看不見順序,這裡是唯一能保住它的分支)。
        """
        codes = assert_unique_codes(codes, "PGY 預設代號")
        base = [str(c) for c in (baseline or [])]
        out: dict = {}

        def _mut(cfg):
            cur = [str(m.get("id")) for m in (cfg.get("pgy_members") or [])]
            by_id = {str(m.get("id")): m
                     for m in (cfg.get("pgy_members") or [])}
            if cur == base:
                merged = list(dict.fromkeys(codes))
            else:
                removes = {c for c in base if c not in codes}
                adds = [c for c in codes if c not in base]
                merged = [c for c in cur if c not in removes]
                merged += [c for c in adds if c not in merged]
            cfg["pgy_members"] = [by_id.get(c, {"id": c}) for c in merged]
            out["merged"] = merged
        with self.storage.write_barrier():     # 見 set_pgy_month_roster 的說明
            assert_no_cross_roster(
                codes, self._clerk_codes_where_default_applies(),
                "PGY 預設代號", "會用到這份預設名單的 Clerk 梯次成員")
            self.update_config(_mut)
        return out.get("merged") or []

    def update_ledger(self, mutator, *, retries: int = 4):
        return self._update_canonical(
            "ledger.json",
            lambda d, rev: self.storage.save_ledger(d, expected_revision=rev),
            mutator, retries=retries)

    def update_clerk_batch_fields(self, key: str, before: dict,
                                  edited: dict) -> dict:
        """把使用者【真的改過的】梯次欄位套到最新的那一筆上。→ 實際套用的欄位。

        ★對話框回的是整份預填紀錄,不是意圖★(外審排班 RS-8 第 1 輪 P1-01-C):
        整包寫回去的話,開窗期間他機改的欄位會被使用者沒動過的舊值悄悄還原 ——
        起始日被改回去就等於改變「這個梯次存在於哪些日期」:求解候選人、切片
        格網覆蓋範圍、跨月統計全部跟著錯,而畫面上看不出來。
        使用者真的改過、而盤上也被改過的欄位 → ★明確拒絕★(不猜誰贏)。
        """
        edits = {k: v for k, v in (edited or {}).items()
                 if field_changed(v, (before or {}).get(k))}
        if not edits:
            return {}
        if "members" in edits:                 # 外審 2026-08-21 P1-01
            assert_unique_codes(edits["members"], "這個梯次的成員")
        _batch_id: dict = {}

        def _mut(bs):
            for b in bs:
                if clerk_batch_key(b) != key:
                    continue
                clash = [k for k in edits
                         if field_changed(b.get(k), (before or {}).get(k))]
                if clash:
                    raise StaleRosterDataError(
                        f"這個梯次的「{'、'.join(clash)}」在你編輯期間也被另一台"
                        f"電腦改過，為避免把對方的修改改回去，這次變更已中止。"
                        f"請重新整理後再改一次。")
                b.update(edits)
                _batch_id["id"] = b.get("id")
                return
            raise StaleRosterDataError(
                f"這個梯次（{key}）已不在清單中（可能已在另一台電腦刪除），"
                f"本次變更未套用。")

        # ★兩個正典檔的寫入包在同一個 write_barrier 內★(deep R1-2):
        #   梯次檔落地與格網平移之間若讓背景 pull 插進來合併一份更新的
        #   batch+grid,平移就會拿【舊的位移量】去搬【新的格網】。barrier
        #   擋住同步換檔;★行程被砍/停電擋不住 → 留一筆意圖★(外審次輪
        #   P2-05):中斷會留下「梯次已是新日期、格網還在舊日期」,而落在
        #   涵蓋範圍外的格子會被 `build_day_input` 直接忽略(切片室看起來
        #   整梯沒開),沒有任何紀錄。
        with self.storage.write_barrier():
            # ★跨池檢查要看【成員】也要看【日期】★(外審 RS-17 R3):
            #   時間範圍變成不變量的一部分之後,「成員沒動、只把梯次搬到
            #   另一個月」同樣能造出重疊 —— 守衛原本掛在 members 底下,
            #   那條路直接繞過去,落地成一份有效衝突(solver 之後只會把那位
            #   Clerk 丟掉並警告,等於他整梯沒班)。
            #   成員取【盤上最新的那一份】(這次沒改就是它),日期取這次要
            #   落地的值;整段在臨界區內,與稍後的 CAS 寫入看同一個盤面。
            if "members" in edits or "start_monday" in edits:
                _cur_b = next((b for b in self.storage.load_clerk_batches()
                               if clerk_batch_key(b) == key), None) or before
                _eff_members = edits.get(
                    "members", (_cur_b or {}).get("members") or [])
                _eff_start = edits.get(
                    "start_monday", (_cur_b or {}).get("start_monday"))
                assert_no_cross_roster(
                    _eff_members, self._pgy_codes_during_batch(_eff_start),
                    "這個梯次的成員", "這一梯期間的 PGY 名單")
            _shift = None
            if "start_monday" in edits:
                # 格網以【梯次 id】為鍵 → 意圖也要用 id(舊資料沒有 id 就沒有
                # 格網,自然沒有這個義務)。id 在臨界區內從盤上取,與待會兒
                # 真的被平移的那一梯必然是同一個。
                _cur = next((b for b in self.storage.load_clerk_batches()
                             if clerk_batch_key(b) == key), None)
                _bid = str((_cur or {}).get("id") or "")
                if _bid:
                    _shift = (_bid, str((before or {}).get("start_monday")
                                        or ""), str(edits["start_monday"]))
                    _pre = _grid_keys_digest(
                        self.storage.load_biopsy_grid().get(_bid) or {})
                    if not self.storage.mark_pending_grid_shift(*_shift,
                                                                _pre):
                        raise StaleRosterDataError(
                            "這個梯次上一次的起始日變更還沒有收斂完成（切片"
                            "格網可能仍停在更早的日期）。請重新啟動排班程式"
                            "讓它自動修復之後，再改一次起始日。")
            try:
                self.update_clerk_batches(_mut)
            except Exception:
                # 梯次沒改成 → 這筆意圖對應的移動從未發生,留著只會讓收斂端
                # 多做一次「核對後什麼都不做」。清掉是安全的:格網也沒動過。
                if _shift is not None:
                    self.storage.clear_pending_grid_shift(_shift[0])
                raise
            # ★起始日改了 → 切片格網在【同一個服務呼叫】內跟著平移★(RS-14):
            #   原本由 UI 在存檔後另外呼叫,而且 payload 是開窗時讀的舊格網。
            #   old 取 before 的值 —— 上面的 clash 守衛已保證盤上此欄與
            #   before 一致,才輪得到這次 edits 落地。
            if _shift is not None:
                self._shift_biopsy_grid(_shift[0], _shift[1], _shift[2])
                self.storage.clear_pending_grid_shift(_shift[0])
        return edits

    def validate_roster_identity_invariants(self) -> list:
        """→ 目前資料裡★已經存在★的名單身分衝突(人話清單;空＝沒問題)。

        (外審 2026-08-22 P2-03)寫入邊界只擋得住【這一台】的編輯:兩台分別
        改 `months/YYYY-MM.json` 與 `clerk_batches.json` 時,git 會乾淨地把
        兩邊合起來 —— 誰都沒有違規,合併後的結果卻違反不變量。跨檔交易做
        不到,所以改成「事後一定看得到」:開程式時記一筆、日排班分頁的警告
        面板也顯示(solver 端仍有 fail-safe,不會真的雙排)。
        """
        out: list = []
        for b in self.storage.load_clerk_batches():
            bid = str((b or {}).get("id")
                      or (b or {}).get("start_monday") or "?")
            members = [str(c) for c in ((b or {}).get("members") or [])]
            dup = duplicated_codes(members)
            if dup:
                out.append(f"Clerk 梯次 {bid} 的成員有重複代號:"
                           f"{'、'.join(dup)} —— 請到設定頁修正")
            during = set(self._pgy_codes_during_batch(
                (b or {}).get("start_monday")))
            clash = [c for c in dedupe_codes(members) if c in during]
            if clash:
                out.append(
                    f"代號 {'、'.join(clash)} 同時是 Clerk 梯次 {bid} 的成員與"
                    f"該期間的 PGY —— 排班時只會當 PGY 排,請修正其中一邊")
        for ym in self.storage.iter_month_yms():
            try:
                cur = self.storage.load_month(ym).get("pgy_month_roster")
            except Exception:                  # 壞月檔另有守衛,不在這裡吵
                continue
            dup = duplicated_codes([str(c) for c in (cur or [])])
            if dup:
                out.append(f"{ym} 的當月 PGY 名單有重複代號:"
                           f"{'、'.join(dup)} —— 請到 PGY 分頁修正")
        cfg_dup = duplicated_codes(
            [str(m.get("id"))
             for m in (self.storage.load_config().get("pgy_members") or [])])
        if cfg_dup:
            out.append(f"PGY 預設代號有重複:{'、'.join(cfg_dup)}"
                       f" —— 請到設定頁修正")
        return out

    # ── 跨池檢查的【時間範圍】(外審 RS-17 R2)────────────────────────────
    #   ★同一個代號在不同時間屬於不同人是正常的★:七月的 PGY 代號 A 之後
    #   變成八月梯次的 Clerk A —— 兩者從來不會在同一個時段同時在場,擋掉它
    #   等於禁止合法的重複使用(repo 自己的 `prior_pgy` 契約就是為了這件事
    #   而存在)。所以只比對【時間上真的重疊】的那一段。
    @staticmethod
    def _months_of_batch(start_monday) -> list:
        """一梯(起始日起 14 天)涵蓋到的月份 'YYYY-MM'(可能跨月)。"""
        try:
            d0 = date.fromisoformat(str(start_monday))
        except (ValueError, TypeError):
            return []
        out = []
        for i in range(14):
            d = d0 + timedelta(days=i)
            ym = f"{d.year:04d}-{d.month:02d}"
            if ym not in out:
                out.append(ym)
        return out

    def _effective_pgy_codes(self, ym: str) -> list:
        """某個月【實際會用到】的 PGY 名單:月度覆蓋優先,否則 config 預設。"""
        try:
            cur = self.storage.load_month(ym).get("pgy_month_roster")
        except Exception:                # 壞月檔不該擋住名單編輯
            cur = None
        if cur is None:
            cur = [str(m.get("id"))
                   for m in (self.storage.load_config().get("pgy_members")
                             or [])]
        return dedupe_codes([str(c) for c in (cur or [])])

    def _pgy_codes_during_batch(self, start_monday) -> list:
        """這一梯涵蓋期間會在場的 PGY 代號(逐月取實際名單後聯集)。"""
        out: list = []
        for ym in self._months_of_batch(start_monday):
            out += self._effective_pgy_codes(ym)
        return dedupe_codes(out)

    def _clerk_codes_in_month(self, ym: str) -> list:
        """涵蓋到這個月的梯次成員(只有這些人會與該月 PGY 同時在場)。"""
        out: list = []
        for b in self.storage.load_clerk_batches():
            if ym in self._months_of_batch((b or {}).get("start_monday")):
                out += [str(c) for c in ((b or {}).get("members") or [])]
        return dedupe_codes(out)

    def _clerk_codes_where_default_applies(self) -> list:
        """會與【config 預設 PGY 名單】碰頭的梯次成員。

        預設名單適用於「沒有月度覆蓋」的月份 → 只要某一梯涵蓋到的月份裡有
        任何一個沒有覆蓋,那一梯的成員就會與預設名單同時在場。
        """
        out: list = []
        for b in self.storage.load_clerk_batches():
            months = self._months_of_batch((b or {}).get("start_monday"))
            uses_default = False
            for ym in months:
                try:
                    if self.storage.load_month(ym).get("pgy_month_roster")                             is None:
                        uses_default = True
                        break
                except Exception:
                    uses_default = True   # 讀不到 → 保守視為會用到預設
                    break
            if uses_default:
                out += [str(c) for c in ((b or {}).get("members") or [])]
        return dedupe_codes(out)

    def add_clerk_batch(self, batch: dict) -> None:
        """新增一梯(唯一性在這裡守門;整份寫回仍走 narrow mutator)。"""
        members = assert_unique_codes((batch or {}).get("members") or [],
                                      "這個梯次的成員")
        with self.storage.write_barrier():     # 見 set_pgy_month_roster 的說明
            assert_no_cross_roster(
                members, self._pgy_codes_during_batch(
                    (batch or {}).get("start_monday")),
                "這個梯次的成員", "這一梯期間的 PGY 名單")
            self.update_clerk_batches(lambda bs: bs.append(dict(batch)))

    def update_clerk_batches(self, mutator, *, retries: int = 4):
        return self._update_canonical(
            "clerk_batches.json",
            lambda d, rev: self.storage.save_clerk_batches(
                d, expected_revision=rev),
            mutator, retries=retries)

    @staticmethod
    def clinic_template_identity(entry: dict) -> tuple:
        """一筆門診模板列的【內容身分】(舊資料沒有 id 時的比對依據)。"""
        e = entry or {}
        return (str(e.get("room") or ""), str(e.get("doctor") or ""),
                bool(e.get("is_self_paid")))

    def add_clinic_template_entry(self, wd, session: str, room: str,
                                  doctor: str = "",
                                  is_self_paid: bool = False) -> str:
        """新增一筆門診模板列 → 這一筆的 ★穩定 id★。

        (外審次輪 P2-04)刪除原本用「畫面上的第幾列」當持久身分 —— 他機在前面
        插入一筆之後,同一個 index 指到的是別人。id 由這裡產生並存進檔案,
        之後的刪除/比對都認它。
        """
        import uuid  # noqa: PLC0415
        entry: dict = {"id": uuid.uuid4().hex[:12], "room": str(room),
                       "doctor": str(doctor or "")}
        if is_self_paid:
            entry["is_self_paid"] = True

        def _mut(data):
            (data.setdefault("template", {}).setdefault(str(wd), {})
             .setdefault(str(session), []).append(entry))
        self.update_clinic_template(_mut)
        return entry["id"]

    def delete_clinic_template_entry(self, wd, session: str, *,
                                     entry_id: str = "",
                                     identity: "tuple | None" = None) -> bool:
        """刪掉【指定的那一筆】門診模板列。→ 是否真的刪到。

        身分優先序(外審次輪 P2-04):
          1. `entry_id`(本版新增的列都有)——他機怎麼增刪都指得準。
          2. 舊資料沒有 id → 用★完整內容身分★(診間/醫師/自費)比對;
             內容相同的兩列在排班上完全等價,刪哪一筆結果一樣,故取第一筆。
        兩者都找不到 → `StaleRosterDataError`(可能已被他機刪掉),
        ★絕不退回用 index 猜★。
        """
        hit = {"ok": False}

        def _mut(data):
            lst = ((data.get("template") or {}).get(str(wd)) or {}).get(
                str(session)) or []
            idx = -1
            if entry_id:
                idx = next((i for i, e in enumerate(lst)
                            if str((e or {}).get("id") or "") == str(entry_id)),
                           -1)
            if idx < 0 and identity is not None:
                idx = next((i for i, e in enumerate(lst)
                            if not (e or {}).get("id")
                            and self.clinic_template_identity(e)
                            == tuple(identity)), -1)
            if idx < 0:
                raise StaleRosterDataError(
                    "這一列門診模板已經不在清單中（可能已在另一台電腦刪除或"
                    "修改），本次刪除未套用。請重新整理後再試一次。")
            lst.pop(idx)
            hit["ok"] = True
        self.update_clinic_template(_mut)
        return hit["ok"]

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

    def set_biopsy_cells(self, batch_id: str, edited: dict, *,
                         baseline, batch_start) -> dict:
        """把切片格網對話框裡【使用者真的改過的】格子套到最新格網。→ 套用的 delta。

        edited/baseline 形狀:{(iso, session): bool}(對話框可見的格子);
        batch_start=開窗當時的梯次起始日(這些格子被顯示時所依據的身分)。
        ★整梯替換不是意圖★(RS-14,全審次輪 P1-02-A):對話框開著的期間他機改
        的同梯其他格,會被開窗快照整包蓋回去 —— 而且是合法地蓋(mutation 在
        最新版正典檔上執行,但 payload 是開窗時算好的舊整梯)。
        使用者改過、而盤上也被他機改過的格子 → ★明確拒絕★(與 Clerk 欄位
        delta 同規,不猜誰贏);沒動過的格子一律以盤上現值為準、絕不寫。

        ★格子的絕對日期只有相對起始日才有意義★(deep R1-1):他機改了起始日
        (格網已整組平移)或刪了梯次之後,舊日期的 delta 寫下去是一格
        「窗外孤兒」—— `build_day_input` 只看目前 14 天覆蓋範圍,直接忽略它,
        而畫面回報成功。梯次身分驗證與格網寫入包在同一個 write_barrier 內
        (背景同步不得在兩讀之間換檔)。
        """
        delta = {k: bool(v) for k, v in (edited or {}).items()
                 if bool(v) != bool((baseline or {}).get(k))}
        if not delta:
            return {}

        def _mut(g_all):
            g = {iso: dict(sess)
                 for iso, sess in (g_all.get(batch_id) or {}).items()}
            clash = [f"{iso} {sess}" for (iso, sess) in delta
                     if bool((g.get(iso) or {}).get(sess))
                     != bool((baseline or {}).get((iso, sess)))]
            if clash:
                raise StaleRosterDataError(
                    f"這一梯的「{'、'.join(sorted(clash))}」在你編輯期間也被"
                    f"另一台電腦改過，為避免把對方的修改改回去，這次變更已"
                    f"中止。請重新開啟視窗後再改一次。")
            for (iso, sess), want in sorted(delta.items()):
                if want:
                    g.setdefault(iso, {})[sess] = True
                else:
                    day = g.get(iso)
                    if day is not None:
                        day.pop(sess, None)
                        if not day:
                            g.pop(iso, None)
            g_all[batch_id] = g

        with self.storage.write_barrier():
            cur = next((b for b in self.storage.load_clerk_batches()
                        if str(b.get("id")) == str(batch_id)), None)
            if cur is None:
                raise StaleRosterDataError(
                    f"這個梯次（{batch_id}）已不在清單中（可能已在另一台電腦"
                    f"刪除），切片格網的這次變更未套用。")
            if str(cur.get("start_monday") or "") != str(batch_start or ""):
                raise StaleRosterDataError(
                    "這個梯次的起始日在你編輯期間被另一台電腦改過（切片格網"
                    "已整組平移到新日期）。為避免把格子寫到已失效的舊日期，"
                    "這次變更已中止，請重新開啟視窗後再改一次。")
            self.update_biopsy_grid(_mut)
        return delta

    def _shift_biopsy_grid(self, batch_id, old_start, new_start) -> None:
        """梯次起始日改了 → 切片格網整組平移相同天數。

        ★平移量算在 mutator 裡、以【最新】格網為底★(RS-14,全審次輪
        P1-02-B):原本 UI 先讀一份格網、算好 shifted 整包,再丟進 CAS ——
        CAS 看到的是最新版,payload 卻是開窗時那份,他機剛改的格子照樣被吃。
        已知殘留(外審同輪判 P2):梯次檔與格網檔是兩個正典檔,兩寫之間
        crash 仍會留下「起始日已改、格網未移」的半套 —— 待後續批次補
        transaction intent。
        """
        if not batch_id or not old_start or not new_start:
            return
        try:
            delta = (date.fromisoformat(str(new_start))
                     - date.fromisoformat(str(old_start))).days
        except (ValueError, TypeError):
            return
        if delta == 0:
            return

        def _mut(g_all):
            g = g_all.get(batch_id)
            if not g:
                return
            shifted: dict = {}
            for iso, sess in g.items():
                try:
                    nd = date.fromisoformat(iso) + timedelta(days=delta)
                except (ValueError, TypeError):
                    continue
                shifted[nd.isoformat()] = sess
            g_all[batch_id] = shifted
        self.update_biopsy_grid(_mut)

    #: 週色手動覆蓋的循環順序(UI 依畫面上的值算下一個,再送過來)
    WEEK_COLOR_CYCLE = {None: "pink", "pink": "green", "green": None}

    def set_week_color(self, year: int, week: str,
                       color: "str | None") -> "str | None":
        """把某一週的手動覆蓋色設成 color(None＝移除覆蓋、回歸自動色)。→ 新的值。

        ★只動這一週★(外審排班 RS-5 第 1 輪 P1-3):UI 原本是「讀整份覆蓋集 →
        改一格 → `replace=True` 整組寫回」,他機剛設的別週覆蓋會被抹掉。
        ★而且送的是【想要的顏色】,不是「切到下一個」★(第 2 輪 P2-02):
        使用者看著粉色雙擊,他要的就是綠色;由程式對【他看不到的最新值】取
        下一個狀態,會跳到一個他沒有要求的顏色。下一個狀態由 UI 依畫面上的
        值算好(`WEEK_COLOR_CYCLE`)再送過來。
        """
        if color not in (None, "pink", "green"):
            raise ValueError(f"週色只能是 pink/green/None，收到 {color!r}")
        out: dict = {}

        def _mut(cur):
            weeks = cur.setdefault("weeks", {})
            if color:
                weeks[week] = color
            else:
                weeks.pop(week, None)
            cur["year"] = int(year)
            cur["source"] = "manual"
            out["value"] = color

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

    def set_pgy_month_roster(self, ym: str, codes, *, baseline) -> None:
        """`baseline`＝開窗時畫面上的那一份(必填)。★整份覆蓋會吃掉他機剛加
        的人★:那個人明天就不會出現在日排班的候選名單裡,而畫面上看不出來。"""
        base = [str(c) for c in (baseline or [])]
        edited = assert_unique_codes(codes, "當月 PGY 人員")

        def _mut(month):
            cur = month.get("pgy_month_roster")
            if cur is None:
                # ★沒有月度覆蓋 ≠ 沒有「目前的名單」★(外審排班 RS-8 第 1 輪
                #   P1):`None` 的語意是「沿用 config 的 PGY 名單」(對話框顯示
                #   的也正是它)。當成空集合的話,他機剛加進 config 的人會在這次
                #   存檔變成一份【明確排除他】的月度覆蓋 —— 他從此不在候選名單
                #   裡,而畫面上完全看不出來。
                cfg = self.storage.load_config()
                cur = [str(m.get("id"))
                       for m in (cfg.get("pgy_members") or [])]
            keep = merge_set_edit([str(c) for c in cur], base, edited)
            # 順序:使用者這一份的順序優先,他機新增的接在後面(去重)
            merged = [c for c in edited if c in keep]
            merged += [str(c) for c in cur
                       if str(c) in keep and str(c) not in merged]
            month["pgy_month_roster"] = merged
        # ★config 與月檔的讀寫要在同一個臨界區★:兩者之間背景同步換掉 config
        #   的話,合併用的「目前名單」與寫進去的月檔不是同一個盤面。
        #   ★跨池檢查也要在裡面★(外審 2026-08-22 P2-03):在外面查的話,
        #   背景 pull 可以在「查完 Clerk 名單」與「寫進月檔」之間把他機新增
        #   的 Clerk 拉進來,寫出一個當場就違反不變量的月檔。
        with self.storage.write_barrier():
            assert_no_cross_roster(
                edited, self._clerk_codes_in_month(ym),
                "當月 PGY 人員", f"涵蓋 {ym} 的 Clerk 梯次成員")
            self.update_month(ym, _mut)

    def set_pgy_apply_pref(self, ym: str, codes, *, baseline) -> None:
        """[2026-07-23 使用者] 設定本月「Apply 本科」PGY（至多 2 位）：自動排班時，
        週二/週五早午的 101 診跟診在【座位次數平手時】優先排這些人（公平最優先，
        偏好只是最後的平手決勝）。"""
        codes = [str(c) for c in codes]
        if len(codes) > 2:
            raise ValueError("Apply 本科優先最多選 2 位")
        base = [str(c) for c in (baseline or [])]

        def _mut(month):
            old = month.get("pgy_apply_pref")
            merged = merge_set_edit([str(c) for c in (old or [])], base, codes)
            # ★合併之後要重新驗一次上限★:他機也剛加了一位的話,兩邊各自
            #   合法、合起來就超過 2 位 —— 那時要明確拒絕,不可以自己挑掉一個。
            if len(merged) > 2:
                raise ValueError(
                    f"其他電腦已經把 Apply 本科設成 {sorted(old or [])}，"
                    f"與你這次的選擇合併後超過 2 位。請重新整理後再選一次。")
            month["pgy_apply_pref"] = [c for c in codes if c in merged] + [
                c for c in sorted(merged) if c not in codes]
            self._audit(month, "pgy", f"{ym} apply_pref", old,
                        month["pgy_apply_pref"], "manual")
        self.update_month(ym, _mut)

    def set_day_lock(self, ym: str, d: date, session: str, on: bool) -> bool:
        """把某(日,時段)設成鎖定/解鎖(鎖定後自動排班不重排該時段)。回傳新狀態。

        ★送的是【想要的狀態】,不是「反過來」★(見 `set_lock` 的同一條說明)。
        """
        want = bool(on)

        def _mut(month):
            if month.get("finalized"):
                raise FinalizedMonthError(f"{ym} 已定案（唯讀）")
            locks = month.setdefault("day_locks", {}).setdefault(
                d.isoformat(), {})
            if want:
                locks[session] = True
            else:
                locks.pop(session, None)
                if not locks:
                    month["day_locks"].pop(d.isoformat(), None)
            return want
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
                # ★回傳一律字串★(外審 RS-11 第 1 輪 P2):人工編輯的數字形狀
                #   會讓停診對話框的 "、".join(rooms) 直接 TypeError,連窗都開
                #   不起來。
                closed = sorted({str(r) for r in
                                 ((sov or {}).get("closed_rooms") or [])})
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
            candidates = 0              # 模板上該室有開的 (日,時段) 數
            changed = 0                 # 真的改了停診狀態的 (日,時段) 數
            for d, day in base.items():
                if d < start or d > end:
                    continue
                iso = d.isoformat()
                for session in sessions:
                    if room not in (day.get(session) or []):
                        continue                  # 該日該時段本來就沒開這室 → 跳過
                    candidates += 1
                    sess = ov.setdefault(iso, {}).setdefault(session, {})
                    # ★既有清單先正規化★(外審 RS-11 第 1 輪 P1):人工編輯過的
                    #   月檔可能存數字 [101] —— 不正規化的話,恢復比不中而說
                    #   「沒有停診紀錄」;先停再恢復更慘:清單變 [101, "101"],
                    #   恢復只移掉字串那個,★回報成功、診間卻仍然停診★。
                    lst = sorted({str(r) for r in
                                  (sess.get("closed_rooms") or [])})
                    if closed and room not in lst:
                        lst.append(room)
                        lst.sort()
                        changed += 1
                    elif not closed and room in lst:
                        lst.remove(room)
                        changed += 1
                    sess["closed_rooms"] = lst
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
            # ★一天都沒動到就不可以「看起來成功」★(2026-08-20 使用者回報:
            #   停診按了沒反應、沒有紀錄、恢復也一樣)。原本這裡什麼都不說,
            #   audit 照記、對話框照關 —— 使用者無從分辨「成功」與「整段被
            #   跳過」。而且★訊息要分得出處置不同的原因★:「模板上沒開」要去
            #   改模板/日期;「已經是這個狀態」則什麼都不用做。
            #   拋例外＝中止 update_month 的存檔,不留誤導的 audit。
            if candidates == 0:
                raise ValueError(
                    f"依門診週模板，{room} 在 {start.isoformat()}～"
                    f"{end.isoformat()} 的選定時段沒有開診，"
                    f"沒有可{'停診' if closed else '恢復'}的日子。"
                    f"請確認診間、日期範圍與時段（週三下午一律不開診）。")
            if changed == 0:
                raise ValueError(
                    f"{room} 在選定範圍內已全部是停診狀態，無需再停。"
                    if closed else
                    f"選定範圍內沒有 {room} 的停診紀錄，無需恢復。")
            if cleared:
                # [RS-03] 有清掉指派 → 舊 day_report 已與現況不符,一併清空
                # 避免幽靈化。
                month["day_report"] = ""
            # [RS-05] 停診/恢復是影響班表的動作,留 audit 痕跡。
            self._audit(month, "day",
                        f"closure:{room} {start.isoformat()}~{end.isoformat()} "
                        f"{sorted(sessions)}",
                        None, "closed" if closed else "open", "closure")
            return {"cleared": cleared, "skipped_locked": skipped_locked,
                    "changed": changed}

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
        # ★求解與套用時的重建必須是【同一種】context★:指紋比對只在兩邊
        #   看到同一份輸入時才有意義(見 `solver_ledger`)。
        # ★月檔身分在【求解當下】捕捉,且與其餘輸入同一個臨界區讀★(RS-13,
        #   全審次輪 P1-01):revision 等到套用才讀的話,讀到的是「按下套用
        #   那一刻」的版本 —— 預覽開著的期間他機改的【未鎖定值班格】不是
        #   SolveContext 的輸入,指紋看不見它,而套用會整份重建 `{scope}_duty`
        #   把它退回舊解,CAS 還會判定一切正常。快照與 config/帳本/假日/週色
        #   包在同一個 write_barrier 內讀,擋住讀到一半被換檔;CP-SAT 在
        #   barrier 外跑(可能數十秒,不可扣住所有寫入)。
        with self.storage.write_barrier():
            month, month_rev = self.storage.load_month_snapshot(ym)
            ctx = self.build_context(scope, ym, month=month, for_solve=True)
        res = solve_duty(ctx, allow_disable_color=allow_disable_color)
        res.month_revision = month_rev
        return res

    # ── 週六切片（R2/R3 輪排，2026-07-13）────────────────────────────────
    @staticmethod
    def _biopsy_overrides(month: dict) -> dict:
        """[2026-07-27 使用者] 月檔內「手動指定的週六切片人選」→ {date: mid}。
        壞鍵略過（與其他讀取容錯一致）。"""
        out: dict = {}
        for iso, mid in (month.get("biopsy_override") or {}).items():
            if mid is None:
                continue
            try:
                # ★"" 是「這個週六不切片」的哨兵,要保留★(RS-12)——
                #   用 truthiness 過濾會把它跟 None 一起丟掉,不切片的指定
                #   就永遠到不了排程函式。
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
        # ★讀一次位元組:版本與內容同源★(外審次輪 P2-01):
        #   `canonical_revision` 之後再 `load_biopsy()` 是兩次獨立讀取,中間
        #   被換入壞內容時兩邊都取自那份壞的,CAS 對得上就放行(RS-5 第 2 輪
        #   已在別處修過同一個形狀,這一條漏了)。
        book, book_rev = self.storage.canonical_snapshot("biopsy.json")
        # ★[2026-08-02 補審 第1輪] 要拒絕就得在【改動 month 之前】拒絕★
        #   settle_biopsy 的守門原本要到函式尾端才拋,那時 month["saturday_biopsy"]
        #   與 report_r 都已經改過了;而呼叫端(set_cell / set_biopsy_person /
        #   clear_unlocked)一律把例外當成可略過並【照樣存檔】——於是月檔被改了、
        #   biopsy.json 的次數沒動,兩邊從此不一致,而且使用者完全看不到。
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
            # ★兩個檔之間留一筆意圖★(RS-10):中間被砍掉的話,月檔已經是新的
            #   saturday_biopsy、biopsy.json 還是舊的 —— 而這條路的呼叫端
            #   (請假變動、重新結算、跨月連動)還會把例外整個吞掉,不留紀錄
            #   就等於那個不一致永遠留在磁碟上。
            with self.settle_intent("r", ym, kind="biopsy"):
                self.storage.save_month(ym, month,
                                        expected_revision=_own_rev)
                self.storage.save_biopsy(book, expected_revision=book_rev)
        return assign, notes, book, book_rev

    def render_report(self, scope: str, ym: str, result: SolveResult) -> str:
        """以目前 storage 狀態重建 ctx，產生 result 的四段式決策報告（純字串）。
        R 排班另附「週六切片」預覽段（依 result 的值班連動＋次數平衡，未落地）。

        ★報告描述的是【這一份 result】,所以要用它的基準★(外審排班 RS-9
        第 2 輪 P1):報告的「新帳本」是 `ctx.ledger + (點數 - 公平份額)`。
        同月第二次求解時,求解看的是本月結算之前的餘額,報告若用顯示端的
        帳本(已含第一次結算),印出來的結轉與新帳本就與接受後的實際結果
        對不上 —— 而接受時 `settle_month` 會先把第一次那筆回滾掉。
        (已存進月檔的舊報告是當時的字串,直接讀月檔即可,不受此影響。)
        """
        ctx = self.build_context(scope, ym, for_solve=True)
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
        ctx = self.build_context(scope, ym, for_solve=True)   # 與求解同一種
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

        # ★第三層:月檔本身的身分★(RS-13,全審次輪 P1-01)。上面兩層看的都
        #   是 SolveContext 的輸入;「未鎖定格現在排誰」不是輸入 —— 他機在
        #   預覽期間手動改的未鎖值班格,指紋看不見,而下面整份重建
        #   `{scope}_duty` 會把它退回舊解。判準=【求解當下】的月檔 revision
        #   (`run_solve` 捕捉);保守政策:他 scope/日排班等無關變動也拒 ——
        #   寧可要求重排,不猜「哪些月檔欄位無害」。
        #   ★None 才是「沒標記」;空字串是「求解時月檔還不存在」的合法身分★
        if result.month_revision is None:
            raise ValueError(
                "排班結果沒有月檔版本標記（可能來自舊版程式或未經求解器產生），"
                "無從確認預覽期間月檔是否被其他電腦修改過，請重新排班")
        if result.month_revision != _month_rev:
            raise ValueError(
                "預覽期間這個月的月檔已被修改（可能是另一台電腦同步了值班／"
                "日排班等變動）。為避免把那些修改蓋回舊的排班結果，本次未套用，"
                "請重新排班")

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
        _bio_failed = ""
        if scope == "r":
            try:
                # 先寫入本次 report(recompute 會在其上刷新/附加[週六切片]段)
                month["report_r"] = report
                assign, notes, biopsy_book, _bio_rev = (
                    self.recompute_saturday_biopsy(ym, month))
                report = month["report_r"]
            except Exception as e:  # noqa: BLE001
                biopsy_book = None
                _bio_failed = str(e) or e.__class__.__name__
                logging.exception("[roster.service] 週六切片重排失敗"
                                  "（值班照常落地，切片留待收斂）")
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
            # ★帳本的編輯基底要嚴格、寫回要 CAS★(外審次輪 P2-01):
            #   寬鬆載入對壞檔回一份空帳本,`settle_month` 在它上面記本月,
            #   `_guard_overwrite` 只替壞檔留 `.corrupt-` 備份然後放行 ——
            #   結果是「幾乎只剩本月」的新帳本蓋掉整份餘額與歷史,而且畫面
            #   回報成功。無 revision 的寫入同樣會吃掉他機剛結算的別月分錄。
            ledger, _led_rev = self.storage.canonical_snapshot("ledger.json")
            settle_month(ledger, scope, ym, result.points_by_person)
            self.storage.save_ledger(ledger, expected_revision=_led_rev)
            if biopsy_book is not None:
                self.storage.save_biopsy(biopsy_book,
                                         expected_revision=_bio_rev)
        except Exception:
            # 意圖留著 → 下次開程式會用月檔把帳本重建到一致。
            raise
        else:
            # ★只清掉【真的重建好了】的那部分義務★(外審次輪 P2-02):
            #   切片重排失敗時月檔已經是新班表、biopsy.json 還是舊人選 ——
            #   先記下只剩切片的那一筆(順序:先記後清,中途斷電也不會兩頭落空),
            #   再清掉涵蓋全部的 "all"。
            if _bio_failed:
                # ★原子降級,不可以「先記後清」★(外審 RS-16 R1-1):既有的
                #   "all" 涵蓋 "biopsy",mark 會判定「別人已經扛著了」而回
                #   False —— 新的那筆沒記上,接著 clear 又把 all 拿掉,義務
                #   整個消失(而 biopsy.json 確實還是舊的)。
                self.storage.retype_pending_settle(scope, ym, "all", "biopsy")
                logging.warning(
                    "[roster.service] ★%s %s 的切片帳本尚未重建★(%s)——"
                    "意圖保留,下次開程式會用月檔收斂", scope, ym, _bio_failed)
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
                # ★每一輪重試都要從頭算★(外審次輪 P2-02 順帶):CAS 被搶先時
                #   `_mut` 會重跑,這一輪重排失敗卻留著上一輪的 book,存下去
                #   的就是【依另一版月檔算出來的】切片帳本。
                _holder.pop("book", None)
                _holder.pop("rev", None)
                _holder["failed"] = ""
                try:
                    (_a, _n, _holder["book"],
                     _holder["rev"]) = self.recompute_saturday_biopsy(
                        ym, month)
                except Exception as e:  # noqa: BLE001
                    # ★衍生物沒重建成功 → 意圖必須留著★(外審次輪 P2-02):
                    #   使用者要的是「重排失敗不擋手動改格」,但月檔已經換成
                    #   新的值班而 biopsy.json 停在舊人選 —— 那個不一致要有
                    #   人負責收斂,而負責的正是這一筆意圖。
                    _holder["failed"] = str(e) or e.__class__.__name__
                    logging.exception(
                        "[roster.service] set_cell 週六切片重排失敗（值班照常"
                        "落地,切片留待收斂）")

        # ★只有 R 會寫切片帳本★(外審排班 RS-10 第 1 輪 P1):VS 改格不碰
        #   biopsy.json,記一筆 "r" 的意圖只會讓開程式時去重算一個沒有壞掉的
        #   東西;更重要的是,意圖的鍵是 (scope, 月份),張冠李戴會讓「誰的義務」
        #   說不清楚。
        # ★月檔與切片帳本要在同一個臨界區內★(外審排班 RS-5 第 2 輪 P1-2):
        #   分開做的話,兩者之間他機更新了 biopsy.json → CAS 正確擋下切片帳本
        #   的寫入,但月檔的新值班/saturday_biopsy 早就落地了 —— 兩個檔當場
        #   互相矛盾,而且沒有任何人會發現(之後的切片平衡全部以錯的次數算)。
        #   臨界區內別人寫不進 biopsy.json,重排時取的 revision 到寫入為止
        #   都還有效,CAS 因此不會在這裡被觸發。
        with (self.storage.write_barrier(),
              self._biopsy_intent(scope, ym) as _intent):
            self.update_month(ym, _mut)
            if _holder.get("book") is not None:
                self.storage.save_biopsy(
                    _holder["book"], expected_revision=_holder.get("rev"))
            if _holder.get("failed"):
                _intent.keep(f"週六切片重排失敗:{_holder['failed']}")
        # 月底週五(翌日是下月 1 號且為週六)→ 重排【下個月】。本月月檔已存好,
        # 這裡才動下月,兩者互不影響;下月沒有月檔就什麼都不做。
        if scope == "r" and _cross_month_friday:
            _next_ym = f"{_next_day.year:04d}-{_next_day.month:02d}"
            # 下月沒排過 → 不做(不可憑空生出一份月檔,也沒有東西會不一致)。
            if self.storage.month_exists(_next_ym):
                # ★跨月這條同樣要留義務★(外審 2026-08-22 P2-01):本月的週五
                #   值班已經改了,下月月初那個週六的切片人選卻可能沒跟上。
                # ★下月已定案 → 更要留義務,不可以靜默略過★(外審 RS-18 R1-1):
                #   定案只是「那份月檔唯讀」,不是「它已經與新的週五值班一致」。
                #   略過的話,下月的 saturday_biopsy/報告/切片計數會繼續反映
                #   舊的週五值班,而且完全沒有紀錄 —— 正好違反這一批要建立的
                #   「定案＝資料一致」契約。留著義務,收斂端會請使用者先解除定案。
                with self.biopsy_obligation(
                        _next_ym, "跨月週五連動的切片重排") as ob:
                    # ★這個分支只是【診斷】★:走 else 讓它自己拋
                    #   FinalizedMonthError 的話,義務照樣會留下來(例外路徑
                    #   本來就不清)—— 差別在 log 是一句話還是一整串 traceback。
                    #   誠實標註:它的突變不會轉紅,因為它不決定義務的存廢。
                    if self.storage.load_month(_next_ym).get("finalized"):
                        ob.keep(f"{_next_ym} 已定案（唯讀），無法重建切片；"
                                f"請先解除該月定案")
                        logging.warning(
                            "[roster.service] %s 已定案 → 跨月切片重排留待"
                            "解除定案後收斂", _next_ym)
                    else:
                        try:
                            self.recompute_saturday_biopsy(_next_ym)
                        except Exception as e:  # noqa: BLE001
                            ob.keep(str(e) or e.__class__.__name__)
                            logging.exception(
                                "[roster.service] set_cell 跨月週六切片重排"
                                "失敗（留待收斂）")
        return self.quick_validate(scope, ym)

    def set_biopsy_person(self, ym: str, d: date,
                          person: "str | None") -> list:
        """[2026-07-27 使用者] 右鍵強制指定某週六的切片人選（person=None → 清除
        指定、改回自動排；★person="" → 這個週六不切片★[2026-08-20 使用者:
        不是每個週六早上都要切片]——該週不排人、不累計次數、不影響輪替）。
        回改後 quick_validate("r") 警告（不阻止儲存）。

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
        # 種類=biopsy:這條路只重建切片計數,不碰點數帳本(外審次輪 P2-02)
        with self.storage.write_barrier(), self._biopsy_intent("r", ym):
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
                _holder.pop("book", None)      # 重試時不可沿用上一輪的結果
                _holder.pop("rev", None)
                _holder["failed"] = ""
                try:
                    (_a, _n, _holder["book"],
                     _holder["rev"]) = self.recompute_saturday_biopsy(
                        ym, month)
                except Exception as e:  # noqa: BLE001
                    # 衍生物沒重建成功 → 意圖留著(見 set_cell 的同一條說明)
                    _holder["failed"] = str(e) or e.__class__.__name__
                    logging.exception(
                        "[roster.service] clear_unlocked 週六切片重排失敗"
                        "（清除照常落地,切片留待收斂）")
            return True

        probe, _rev = self.storage.load_month_snapshot(ym)
        if not _mut(probe):                    # 試算:沒有東西可清就不要寫檔
            return
        # ★月檔與切片帳本在同一個臨界區內★(見 set_cell 的同一條說明);
        #   ★而且兩個檔之間要留一筆意圖★(RS-10):臨界區擋不住行程被砍。
        with (self.storage.write_barrier(),
              self._biopsy_intent(scope, ym) as _intent):
            self.update_month(ym, _mut)
            if _holder.get("book") is not None:
                self.storage.save_biopsy(
                    _holder["book"], expected_revision=_holder.get("rev"))
            if _holder.get("failed"):
                _intent.keep(f"週六切片重排失敗:{_holder['failed']}")

    def set_lock(self, scope: str, ym: str, d: date, locked: bool) -> bool:
        """把某格設成「鎖定/未鎖定」(空格不可鎖)。回傳設定後的狀態。

        ★送的是【想要的狀態】,不是「反過來」★(外審排班第 2 輪 P2-02):
        取反的話,他機剛把這一格鎖起來時,使用者按下畫面上寫著「鎖定」的動作
        反而幫忙解鎖 —— 而且畫面刷新後看起來一切正常。使用者的意圖是絕對的
        (「我要它鎖住」),不是相對於某個他早就看不到的舊值。
        """
        iso = d.isoformat()
        want = bool(locked)
        _holder: dict = {}

        def _mut(month) -> bool:
            _holder.clear()                    # ★所有早退之前★(見 clear_unlocked)
            duty = month.setdefault(f"{scope}_duty", {})
            cell = duty.get(iso)
            if not cell or not cell.get("person"):
                _holder["empty"] = True
                return False
            if bool(cell.get("locked", False)) == want:
                _holder["noop"] = True         # 盤上已經是這個狀態 → 不寫檔
                return want
            self._audit(month, scope, iso,
                        f"locked={cell.get('locked', False)}",
                        f"locked={want}", "lock")
            cell["locked"] = want
            return want

        probe, _rev = self.storage.load_month_snapshot(ym)
        _mut(probe)
        if _holder.get("empty"):               # 空格不可鎖 → 不寫檔
            return False
        if _holder.get("noop"):                # 已經是想要的狀態 → 不寫檔
            return want
        return self.update_month(ym, _mut)

    def set_leaves(self, scope: str, ym: str, member_id: str, dates, *,
                   baseline) -> None:
        """`baseline`＝開窗時的那一份(必填,見 `_set_date_map`)。"""
        if scope != "r":
            self._set_date_map(scope, ym, "leaves", member_id, dates, baseline)
            return
        # [codex P2] R 請假變動影響週六切片（平衡候選排除/值班連動的請假優先）
        # → 同步重排＋刷新報告段；定案月 _set_date_map 已先拋,不會走到這裡。
        # ★來源已改、衍生物沒跟上,就要留下義務★(外審 2026-08-22 P2-01):
        #   原本只 log —— 請假成功落地、切片停在舊人選,沒有人會去收斂。
        with self.biopsy_obligation(ym, "請假變動後的週六切片重排") as ob:
            self._set_date_map(scope, ym, "leaves", member_id, dates, baseline)
            try:
                self.recompute_saturday_biopsy(ym)
            except Exception as e:  # noqa: BLE001
                ob.keep(str(e) or e.__class__.__name__)
                logging.exception(
                    "[roster.service] set_leaves 週六切片重排失敗"
                    "（請假照常落地,切片留待收斂）")

    def set_must(self, scope: str, ym: str, member_id: str, dates, *,
                 baseline) -> None:
        self._set_date_map(scope, ym, "must_duty", member_id, dates, baseline)

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
        # ★改名要把這些檔整份寫回去 → 來源一律用嚴格快照★(外審次輪 P2-01):
        #   壞檔的寬鬆載入回空,改名「成功」之後帳本/切片計數就只剩這一次
        #   改寫的殘骸(回滾用的 snap 也是同一份空的,救不回來)。
        ledger = self.storage.canonical_snapshot("ledger.json")[0]
        holiday = self.storage.load_holiday_duty()
        biopsy = (self.storage.canonical_snapshot("biopsy.json")[0]
                  if scope == "r" else None)
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
            kind = self.storage.pending_kind(item)
            if not scope or not ym:
                self.storage.clear_pending_settle(scope, ym, kind)
                continue
            try:
                # ★整段在臨界區內★(外審排班 RS-4 第 1 輪 P2):`resettle_from_duty`
                #   會「讀帳本 → 依月檔重算 → 寫回」,而開程式的當下 GitSync 正在
                #   做啟動 pull/補推 —— 中間被 merge 換掉帳本的話,寫回去的是手上
                #   那份舊的,他機剛同步進來的結算就靜默消失(而且我們接著還把意圖
                #   清掉,等於宣稱已經一致)。
                with self.storage.write_barrier():
                    if kind == "biopsy":
                        # ★只欠切片★:帳本已經是對的,`resettle_from_duty` 會
                        #   連帶重寫它 —— 沒有必要,而且已定案的月份會被它擋下
                        #   (切片其實還重建得回來)。只重建欠的那一個。
                        self._reconcile_biopsy_only(ym)
                    else:
                        self.resettle_from_duty(scope, ym)
                    self.storage.clear_pending_settle(scope, ym, kind)
            except Exception:
                logging.exception(
                    "[roster.service] ★%s %s 的結算無法自動收斂★ 帳本可能仍與"
                    "月檔不一致,意圖紀錄保留,請人工確認", scope, ym)
                continue
            out.append((scope, ym))
            logging.warning("[roster.service] 上次未完成的%s已用月檔重建:"
                            "%s %s", kind, scope, ym)
        return out

    def _reconcile_biopsy_only(self, ym: str) -> None:
        """只把切片計數帳本重建到與月檔一致(外審次輪 P2-02)。

        `recompute_saturday_biopsy(ym)` 自行 load 的那條路會寫月檔+切片帳本,
        兩者同批、同臨界區,而且它自己也留意圖 —— 收斂失敗時那一筆會接手。

        ★已定案的月份收斂不了,而且要說得出下一步★(外審 2026-08-22 P2-02):
        重建要寫月檔,而定案月唯讀。新的定案閘門已經不允許帶著未完成的義務
        定案,但舊資料仍可能是這個狀態 —— 明講「請先解除定案」,不要只留下
        一句泛用的例外訊息(意圖保留,不會被清掉)。
        """
        if self.storage.load_month(ym).get("finalized"):
            raise FinalizedMonthError(
                f"{ym} 已定案（唯讀），無法自動重建週六切片資料。"
                f"請先解除該月定案,程式會在下次啟動時自動收斂。")
        self.recompute_saturday_biopsy(ym)

    def reconcile_pending_grid_shifts(self) -> list:
        """開程式時收斂「梯次已移動、切片格網還沒跟著平移」(外審次輪 P2-05)。

        ★平移不是冪等的,所以要先看格網現在對齊哪一邊★:
          * 全部落在【新】起始日的 14 天窗 → 已經完成 → 清掉意圖。
          * 全部落在【舊】的窗 → 套用位移 → 存檔 → 清掉意圖。
          * 兩者都不是(有人手工編過、或跨過兩次搬移)→ ★保留意圖並告警★,
            不猜、也不硬搬(搬錯會把格子丟到誰也用不到的日期)。
        """
        out: list = []
        for rec in self.storage.load_pending_grid_shifts():
            bid = str(rec.get("batch_id") or "")
            try:
                old_start = date.fromisoformat(str(rec.get("old_start")))
                new_start = date.fromisoformat(str(rec.get("new_start")))
            except (ValueError, TypeError):
                logging.warning("[roster.service] 壞掉的平移意圖(%r)→ 清掉", rec)
                self.storage.clear_pending_grid_shift(bid)
                continue
            try:
                with self.storage.write_barrier():
                    done = self._reconcile_one_grid_shift(
                        bid, old_start, new_start,
                        str(rec.get("pre_digest") or ""))
            except Exception:
                logging.exception(
                    "[roster.service] ★梯次 %s 的切片格網平移無法收斂★"
                    "意圖保留,請人工確認", bid)
                continue
            if done:
                out.append(bid)
        return out

    def _reconcile_one_grid_shift(self, bid: str, old_start: date,
                                  new_start: date,
                                  pre_digest: str = "") -> bool:
        """→ 這一筆是否已收斂(清掉意圖)。呼叫端須持有臨界區。

        ★先看梯次【現在】的起始日,不可盲信意圖★:意圖是在梯次寫入之前記下
        的,那次寫入仍可能被 CAS 擋下或整個失敗 —— 照著意圖硬搬會把格網移到
        一個根本沒有發生過的日期。
        """
        cur = next((b for b in self.storage.load_clerk_batches()
                    if str(b.get("id")) == str(bid)), None)
        if cur is None:                        # 梯次已被刪 → 沒有義務
            self.storage.clear_pending_grid_shift(bid)
            return True
        cur_start = str(cur.get("start_monday") or "")
        if cur_start == old_start.isoformat():
            # 移動從未落地 → 格網本來就該留在舊窗,什麼都不要做
            self.storage.clear_pending_grid_shift(bid)
            return True
        if cur_start != new_start.isoformat():
            logging.warning(
                "[roster.service] ★梯次 %s 的起始日(%s)既不是意圖的舊值(%s)"
                "也不是新值(%s)★ 不猜測、不搬動,意圖保留請人工確認",
                bid, cur_start, old_start.isoformat(), new_start.isoformat())
            return False
        grid_all, rev = self.storage.canonical_snapshot("biopsy_grid.json")
        g = grid_all.get(bid) or {}
        if not g:                              # 沒有格網 → 沒有義務
            self.storage.clear_pending_grid_shift(bid)
            return True
        days = []
        for iso in g:
            try:
                days.append(date.fromisoformat(str(iso)))
            except (ValueError, TypeError):
                logging.warning("[roster.service] 梯次 %s 的格網有壞日期 %r →"
                                "無法判斷平移狀態,意圖保留", bid, iso)
                return False
        delta = (new_start - old_start).days
        cur_digest = _grid_keys_digest(g)

        # ★先用【平移前那份格網的身分】判定★(外審 RS-16 R1-2):位移 < 14 天
        #   時新舊視窗會重疊 —— 8/3→8/10、格網只剩 8/10 那一格的話,兩個視窗
        #   都符合,只看日期落點會把「還沒搬」誤判成「搬過了」而清掉意圖,
        #   切片室的開放週從此掛在錯的一週。
        if pre_digest:
            if cur_digest == pre_digest:       # 與平移前一模一樣 → 還沒搬
                pass                           # ↓ 往下走平移
            elif _shifted_keys_digest(g, -delta) == pre_digest:
                # 把現況往回搬,若與【平移前的身分】相同 → 這一份就是
                # 「把平移前那份搬過去」的樣子 → 已經完成
                # ★不可以拿現況跟「現況往回搬」比★(外審 RS-16 R2):位移不是
                #   0 的話那兩者永遠不同,於是搬好之後才斷電的那個視窗會被判成
                #   「有人手工編過」,意圖永遠留著,那一梯的起始日再也改不動。
                self.storage.clear_pending_grid_shift(bid)
                return True
            else:
                logging.warning(
                    "[roster.service] ★梯次 %s 的切片格網與平移前後的樣子都對"
                    "不上★(可能有人手工編過)不猜測、不搬動,意圖保留請人工確認",
                    bid)
                return False
        else:
            # 舊版意圖沒有身分 → 退回視窗規則,並★把重疊當成歧義★(寧可留著
            # 讓人確認,也不要搬到錯的一週)。
            def _within(start):
                return all(0 <= (d - start).days <= 13 for d in days)

            in_old, in_new = _within(old_start), _within(new_start)
            if in_old and in_new:
                logging.warning(
                    "[roster.service] ★梯次 %s 的格網同時落在新舊視窗(位移 %+d"
                    " 天)★ 舊版意圖沒有平移前的身分可比對,不猜測,意圖保留",
                    bid, delta)
                return False
            if in_new:
                self.storage.clear_pending_grid_shift(bid)
                return True
            if not in_old:
                logging.warning(
                    "[roster.service] ★梯次 %s 的切片格網日期既不在舊窗(%s)也"
                    "不在新窗(%s)★ 不猜測、不搬動,意圖保留請人工確認",
                    bid, old_start.isoformat(), new_start.isoformat())
                return False
        shifted = {}
        for iso, sess in g.items():
            nd = date.fromisoformat(str(iso)) + timedelta(days=delta)
            shifted[nd.isoformat()] = sess
        grid_all[bid] = shifted
        self.storage.save_biopsy_grid(grid_all, expected_revision=rev)
        self.storage.clear_pending_grid_shift(bid)
        logging.warning("[roster.service] 梯次 %s 的切片格網已補平移 %+d 天",
                        bid, delta)
        return True

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
        _bio_failed = ""
        if scope == "r":
            try:
                _a, _n, book, book_rev = self.recompute_saturday_biopsy(
                    ym, month)
            except Exception as e:  # noqa: BLE001
                book = book_rev = None
                _bio_failed = str(e) or e.__class__.__name__
                logging.exception("[roster.service] resettle 週六切片重排失敗"
                                  "（帳本照常重算,切片留待收斂）")
        # ★寫入順序:月檔 → 帳本 → 切片帳本★(RS-4 定下的可收斂方向);
        #   月檔的 CAS 是這一批的閘門 —— 被搶先就在這裡失敗,呼叫端整批重來,
        #   帳本與切片帳本都還沒動。
        self.storage.mark_pending_settle(scope, ym)
        self.storage.save_month(ym, month, expected_revision=month_rev)
        # ★帳本也要 CAS★:他機剛結算完的別月分錄不可以被這份舊快照吃掉。
        self.update_ledger(lambda led: settle_month(led, scope, ym, points))
        if book is not None:
            self.storage.save_biopsy(book, expected_revision=book_rev)
        if _bio_failed:                        # 見 accept 的同一條說明
            self.storage.retype_pending_settle(scope, ym, "all", "biopsy")
            logging.warning(
                "[roster.service] ★%s %s 的切片帳本尚未重建★(%s)—— 意圖保留",
                scope, ym, _bio_failed)
        else:
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
            # ★這個判斷會決定「要不要回滾本月舊分錄」→ 也算寫入路徑★
            #   (外審次輪 P2-01):壞帳本的寬鬆載入回空 → settled 是空集合 →
            #   已結算但本月沒排班的 scope 不會被重算,舊分錄就永遠留著。
            hist = (self.storage.canonical_snapshot("ledger.json")[0]
                    .get("history") or [])
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
        # ★定案的語意是「所有正典/衍生資料都已經一致」★(外審 2026-08-22
        #   P2-02):切片重建失敗時 `_resettle_locked` 會把義務降級成 biopsy
        #   並繼續 —— 若就這樣定案,月檔從此唯讀,而收斂端要寫月檔才能重建
        #   切片(`recompute_saturday_biopsy` 會被 finalized 守衛擋下)→
        #   那筆義務永遠留著、也永遠做不完。所以定案前先要求它清零。
        if on:
            _left = [x for x in self.storage.load_pending_settles()
                     if str(x.get("ym")) == ym]
            if _left:
                _kinds = "、".join(sorted({self.storage.pending_kind(x)
                                          for x in _left}))
                raise StaleRosterDataError(
                    f"{ym} 還有尚未重建完成的資料({_kinds}），不能定案。"
                    f"定案之後月檔唯讀,那些資料就再也補不回來了。"
                    f"請重新開啟排班程式讓它自動收斂(或先處理錯誤訊息裡的"
                    f"檔案問題)之後再定案。")
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
    def _set_date_map(self, scope, ym, key, member_id, dates, baseline) -> None:
        """把「這幾天加上去、那幾天拿掉」套到盤上最新的表上(不是整份覆蓋)。

        `baseline`＝開窗時畫面上顯示的那一份 —— ★它是判斷【使用者做了什麼】
        的依據★(外審排班第 2 輪 P1-02)。合併在 mutator 裡做:`update_month`
        重試時會重讀月檔,合併必須對著【那一次】的內容重算。
        """
        base = {d.isoformat() for d in (baseline or set())}
        edited = {d.isoformat() for d in (dates or set())}

        def _mut(month):
            table = month.setdefault(key, {}).setdefault(scope, {})
            cur = set(table.get(str(member_id)) or ())
            merged = merge_set_edit(cur, base, edited)
            days = sorted(merged)
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
