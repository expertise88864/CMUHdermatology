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
    TWO_PGY_PHOTO_ONLY, apply_locked_biopsy_adjustment,
    arbitration_order, batch_biopsy_slots, biopsy_quota_warnings,
    day_input_fingerprint, day_owner_batch,
    BIOPSY, PHOTO, REST, TREATMENT, DaySolveInput, month_solve_day,
    person_course_stats,
)
from cmuh_common.roster.report import (
    build_final_biopsy_state_report, build_final_day_state_report,
    build_final_state_report, build_report,
)
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
    StrictSources,
    next_ym,
    prev_ym,
)

#: ★權威路徑的輸入宣告★(外審 2026-08-22 P1-01/P1-02;見 storage.StrictSources)
#:   會【寫回去】的計算(求解/套用/結算/定案/切片重排/匯出)一律先把這些檔
#:   在同一個臨界區內嚴格讀完 —— 寬鬆載入把「暫時讀不到」變成合法的空值,
#:   而空值算得出一份看起來正常的班表/帳本/正式文件。
#:   ★宣告不足不會靜默退回寬鬆讀取,而是當場拋★(未宣告的來源存取即錯誤),
#:   所以這幾行是被測試釘住的事實,不是註解裡的宣稱。
SRC_RVS = ("config.json", "holiday_duty.json", "week_colors.json",
           "ledger.json")
SRC_BIOPSY = ("config.json", "biopsy.json")
SRC_DAY = ("config.json", "holiday_duty.json", "clinic_template.json",
           "clerk_batches.json", "biopsy_grid.json")
SRC_CLOSURE = ("clinic_template.json", "holiday_duty.json")
#: 結算/定案:重算帳本(RVS)之外還會重排週六切片(BIOPSY)。
SRC_SETTLE = tuple(sorted(set(SRC_RVS) | set(SRC_BIOPSY)))
#: 匯出:R/VS 與日排班必須來自【同一份】快照(否則正式文件是拼裝品)。
SRC_EXPORT = tuple(sorted(set(SRC_RVS) | set(SRC_DAY)))

class DayStructureError(RuntimeError):
    """日排班有★結構性錯誤★ → 拒絕定案(使用者 2026-08-25 定案)。

    ★只有定案會擋,手動編輯照樣存得下去★:編輯中的班表本來就會經過不完整的
    中間狀態(先把人搬走再搬回來),擋在那裡只會逼使用者繞路。而定案是
    【單向】的 —— 它重算帳本並讓月檔唯讀,帶著「切片室 3 個人」定案下去,
    只能靠解除定案才救得回來。
    """


#: 定案被擋時訊息裡最多列幾筆(其餘請看警告面板)。
_BAD_SHOWN = 20
_SCOPE_LABEL = {"r": "R 排班", "vs": "VS 排班"}
_SPECIAL_SLOTS = frozenset((PHOTO, TREATMENT, BIOPSY, REST))   # 非跟診房的特殊格


def _deltas_of(entry: dict) -> dict:
    """一筆結算分錄的差額(正規化到小數第 4 位;比對只有這一份實作)。"""
    return {str(k): round(float(v or 0.0), 4)
            for k, v in ((entry or {}).get("deltas") or {}).items()}


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


def _day_digest(month: dict) -> str:
    """日排班內容(day_slots)的識別 —— `day_report` 描述的就是它。"""
    canon = json.dumps(month.get("day_slots") or {},
                       ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


#: 決策報告與現況的關係(見 `report_state`)。
REPORT_FRESH = "fresh"
REPORT_STALE = "stale"
REPORT_UNVERIFIABLE = "unverifiable"

#: ★決策報告永遠要講的一句話★(外審 RS-19 R1-1):它描述的是【那一次自動
#: 求解】,而其中的點數/目標/帳本數字反映的是求解當時的設定。改點數規則、
#: 改國定假日、改名單、或別的月份結算過之後,這些數字就不再成立 —— 而值班格
#: 的識別看不見那些改動(見 `report_content_digest`)。無條件講,才不會有
#: 「查得到的那幾種原因講了、查不到的那幾種被當成沒事」的縫。
REPORT_HISTORICAL_NOTE = (
    "※ 本段是【當初自動求解】的紀錄:其中的點數、目標與帳本數字反映求解"
    "當時的設定(點數規則／國定假日／成員名單／帳本結轉)。"
    "最終班表與結算請以上方「最終班表」段為準。")

#: 另外【查得出來】的兩種原因。★處置不同,不可以壓成同一句★:
#:  stale 是「已知班表被改過」,unverifiable 是「舊版程式沒留識別,查不出來」。
REPORT_NOTE = {
    REPORT_STALE: (
        "⚠ 這是【初次自動求解】當時的紀錄。之後這個月的值班有過人工調整，"
        "本段不代表最終班表；最終班表與結算請看上方「最終班表」段。"),
    REPORT_UNVERIFIABLE: (
        "⚠ 這份報告由舊版程式產生，沒有留下可比對的識別，"
        "無法確認它是否仍與最終班表相符；最終班表與結算請看上方「最終班表」段。"),
}


def _month_duty(month: dict, scope: str, y: int, m: int) -> dict:
    """月檔的該 scope 值班 -> {date: member_id}(只取本月、真的有人的格)。

    ★非當月鍵一律不算★:跨機人工合併/外部編輯會在月檔留下鄰月日期,算進去
    就虛增那個人的班數與點數(`_resettle_locked` / `build_export` 都有這道
    過濾,留底文件不能是唯一的例外)。
    """
    out: dict = {}
    for iso, cell in (month.get(f"{scope}_duty") or {}).items():
        p = (cell or {}).get("person")
        if not p:
            continue
        try:
            dt = date.fromisoformat(iso)
        except (ValueError, TypeError):
            continue
        if (dt.year, dt.month) == (y, m):
            out[dt] = str(p)
    return out


def report_content_digest(month: dict, scope: str) -> str:
    """報告所描述的那份內容的識別(r/vs＝值班格;day＝日排班格)。

    ★它只證明【班表】沒被改過★(外審 RS-19 R1-1):`build_report` 的結算數字
    還依賴點數規則、國定假日、成員名單與帳本結轉 —— 只改設定、不動班表時
    這個識別仍然相符。那些輸入沒有被涵蓋,是因為要比對它們就得重建一次
    `for_solve` 的 context,而那條路對【分錄已被修剪的舊月份】會 fail-closed
    (RS-9 的教訓:單純想看一個舊月份的人會連視窗都打不開)。
    所以改成另一個方向:報告的結算★一律★標明是求解當時的數字
    (見 `REPORT_HISTORICAL_NOTE`),不去宣稱它現在還成立。
    """
    return _day_digest(month) if scope == "day" else _duty_digest(month, scope)


def report_key(scope: str) -> str:
    return "day_report" if scope == "day" else f"report_{scope}"


def report_digest_key(scope: str) -> str:
    return f"report_digest_{scope}"


def stamp_report_digest(month: dict, scope: str) -> None:
    """存報告時一併蓋上它所描述的那份內容的識別(見 `report_state`)。"""
    month[report_digest_key(scope)] = report_content_digest(month, scope)


def report_notice(month: dict, scope: str) -> str:
    """這份報告要附的說明(空＝沒有報告)。

    ★歷史性那一句無條件講★;班表另外被改過/無從查證時再加上那一句
    (見 `REPORT_HISTORICAL_NOTE` 與 `REPORT_NOTE`)。
    """
    state = report_state(month, scope)
    if not state:
        return ""
    extra = REPORT_NOTE.get(state, "")
    return REPORT_HISTORICAL_NOTE + (chr(10) + extra if extra else "")


def report_state(month: dict, scope: str) -> str:
    """這份已存的決策報告,還配不配得上月檔現在的班表?

    (外審 2026-08-22 P1-03)`build_report` 印的是【那一次自動求解】的具體
    班表與結算 —— 日期、誰值班、幾點、各人平日/假日班數、新帳本餘額。
    Auto Accept 之後只要有人手動換一天班,`set_cell` 會正確更新 duty、audit
    與切片,★卻不會動到那份報告★;定案時 `finalize` 用最新 duty 重算帳本,
    也不會重新產生報告。於是定案 PDF 裡的班表=舊版、帳本=新版,互相矛盾。

    ★判準用「內容的識別」而不是逐一在每個編輯路徑清報告★:清報告要窮舉
    所有會改到 duty 的路徑(現在有 set_cell/set_lock 之外還會再長出來),
    漏一條就又回到同一個缺陷;識別是【衍生的】,任何路徑改了 duty,下一次
    讀取自然就對不上。
    """
    if not (month.get(report_key(scope)) or ""):
        return ""
    dig = month.get(report_digest_key(scope))
    if not dig:
        # 舊版程式存的報告沒有識別 —— ★查不出來就不可以宣稱它是最終的★
        return REPORT_UNVERIFIABLE
    return (REPORT_FRESH if dig == report_content_digest(month, scope)
            else REPORT_STALE)


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


def _config_intent_digest(cfg: dict) -> str:
    """這一份設定內容的★語意識別★(與序列化格式無關)。

    用途:改名的意圖在★寫第一個檔之前★就記下「我要把 config 寫成什麼樣子」;
    復原時把盤上的 config 解析回來算同一個識別,相符就證明那一份是我們寫的
    (而不是他機獨立把 old 移除、加進一位同代號的合法成員 —— 那個盤面的
    「名單只剩 new」長得一模一樣)。

    ★不預測位元組★(外審 Codex 第 3 輪 P2):比對的兩邊都是【解析後的資料】,
    不依賴 writer 的縮排/編碼/鍵序;`schema_version` 由寫入路徑自己補,排除。
    """
    d = {k: v for k, v in (cfg or {}).items() if k != "schema_version"}
    blob = json.dumps(d, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _files_with_both_ids(scope: str, old_id: str, new_id: str, ledger: dict,
                         holiday: dict, biopsy, months: dict) -> list:
    """→ ★同一個檔案裡同時出現 old_id 與 new_id★ 的檔名清單。

    改名對每一個檔都是「整份把 old 換成 new」的原子寫入,所以未完成的交易
    留下的每一個檔只會是【全舊】或【全新】。★判準要逐檔算★:不同月檔一個
    全舊、一個全新是合法的中間狀態,把它們併在一起算會誤擋真正的續作。
    """
    def _led(who) -> bool:
        if who in (ledger.get(scope) or {}):
            return True
        return any(e.get("scope") == scope and who in (e.get("deltas") or {})
                   for e in (ledger.get("history") or []))

    def _hol(who) -> bool:
        return who in (holiday.get(scope) or {}).values()

    def _bio(who) -> bool:
        b = biopsy or {}
        if who in (b.get("counts") or {}):
            return True
        return any(who in (e.get("assign") or {}).values()
                   for e in (b.get("history") or []))

    def _mon(m: dict, who) -> bool:
        for mk in ("leaves", "must_duty"):
            if who in ((m.get(mk) or {}).get(scope) or {}):
                return True
        if any((c or {}).get("person") == who
               for c in (m.get(f"{scope}_duty") or {}).values()):
            return True
        if ((m.get("last_weekend") or {}).get(scope) or {}).get(
                "person") == who:
            return True
        if scope == "r" and who in (m.get("biopsy_override") or {}).values():
            # ★判準要涵蓋「改名真的會改寫」的每一個欄位★(外審 Codex 第 3 輪
            #   P1):手動指定的切片人選也是以代號為值,漏掉它就會把兩位醫師的
            #   週六切片指定混成一個人。
            return True
        return scope == "r" and any(
            (c or {}).get("person") == who
            for c in (m.get("saturday_biopsy") or {}).values())

    out = []
    if _led(old_id) and _led(new_id):
        out.append("ledger.json")
    if _hol(old_id) and _hol(new_id):
        out.append("holiday_duty.json")
    if scope == "r" and _bio(old_id) and _bio(new_id):
        out.append("biopsy.json")
    for ym, m in sorted(months.items()):
        if m is not None and _mon(m, old_id) and _mon(m, new_id):
            out.append(f"{ym} 月檔")
    return out


def _rename_intents_collide(rec: dict, all_recs: list) -> bool:
    """這一筆改名意圖有沒有跟別筆牽扯在一起(共用目標/首尾相接)。

    ★收斂順序不可以決定誰的歷史被誰覆蓋★:`A→B` 與 `C→B` 共用目標,
    先跑的那一筆會讓後跑的那一筆把它的餘額蓋掉;`A→B` 與 `B→C` 首尾相接,
    順序不同結果不同。兩種都不是程式有資格挑的 —— 保留並請人確認。
    """
    scope = str(rec.get("scope") or "")
    old_id, new_id = str(rec.get("old_id") or ""), str(rec.get("new_id") or "")
    for other in all_recs:
        if other is rec or str(other.get("scope") or "") != scope:
            continue
        o_old = str(other.get("old_id") or "")
        o_new = str(other.get("new_id") or "")
        if (o_new == new_id or o_old == old_id      # 共用目標/共用來源
                or o_new == old_id or o_old == new_id):   # 首尾相接
            return True
    return False


class RosterService:
    def __init__(self, storage: RosterStorage):
        self.storage = storage

    # ── 讀取組裝 ────────────────────────────────────────────────────────
    def _sources(self, ym: str, names, *, months=()) -> StrictSources:
        """這條權威路徑的輸入,一次嚴格讀完(本月 + 上月 + `months`)。

        ★呼叫端必須持有 `write_barrier`★:一次讀好幾個檔,臨界區外的話它們
        彼此就不是同一個時間點的內容 —— 而「整批一致」正是這個包裝的用意。
        ★上月一律納入★:跨月銜接(last_weekend)、連續值班尾端、跨月週五的
        切片連動、Clerk 跨月公平計數回放都要讀它,而它讀不到的後果與本月
        讀不到一樣是【安靜地少一段限制】。
        """
        # ★`months` 只給真的需要的路徑加★(外審 2026-08-24 P1-01):日排班的
        #   Clerk 梯次是兩週、可以跨月,所以它要多讀下個月;其他路徑不該因此
        #   多一個 fail-closed 的來源(下個月的月檔壞掉不該擋住本月結算)。
        return self.storage.strict_sources(
            names, (prev_ym(ym), ym) + tuple(months))

    def solver_ledger(self, scope: str, ym: str, *,
                      src: "StrictSources | None" = None) -> dict:
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
        led_all = (src.load_ledger() if src is not None
                   else copy.deepcopy(
                       self.storage.canonical_snapshot("ledger.json")[0]))
        # ★「本月之前」＝本月與【所有更晚的月份】都要回滾★(外審 RS-20 R1-2):
        #   先手動編輯 12 月一格(現在會立刻結算),再回頭自動排 11 月 ——
        #   只回滾 11 月的話,12 月的差額會被當成 11 月的 carry-in,公平目標
        #   整個歪掉。帳本是累計的,而「未來」不可能是「之前」的一部分。
        _later = sorted({str(e.get("month") or "")
                         for e in (led_all.get("history") or [])
                         if e.get("scope") == scope
                         and str(e.get("month") or "") >= ym})
        for _m in [ym, *[x for x in _later if x != ym]]:
            if not can_rollback(led_all, _m):
                # 分錄可能已被修剪 → 無從確認「本月之前」是多少。寧可擋下
                # 並說清楚(硬猜一個基準會讓之後每個月的公平目標都跟著錯)。
                raise ValueError(
                    f"{_m} 比帳本保留的最舊月份還早，無法確認 {ym} 結算之前的"
                    f"餘額，因此不能重新排班（重排會以錯誤的公平基準計算）。"
                    f"如確實需要，請直接調整 ledger.json。")
            rollback_month(led_all, scope, _m)
        # ★餘額 0 與「這個人還沒有分錄」對求解是同一件事★:回滾之後會留下
        #   一堆值為 0 的鍵,而原本那些鍵根本不存在 —— 兩者的求解結果完全
        #   相同,指紋卻不一樣(RS-7 的過期判準會因此誤報「輸入設定已變動」)。
        #   求解端一律 `ledger.get(mid, 0.0)`,所以這裡去掉 0 是等價的正規化。
        return {k: v for k, v in (led_all.get(scope) or {}).items()
                if round(float(v or 0.0), 4) != 0.0}

    def build_context(self, scope: str, ym: str, *,
                      month: "dict | None" = None,
                      for_solve: bool = False,
                      src: "StrictSources | None" = None) -> SolveContext:
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
        # ★`src` 給的是【權威輸入】(見 SRC_* 與 storage.StrictSources)★:
        #   有它就整批走嚴格快照,沒有它維持既有的寬鬆載入(顯示路徑)。
        #   換的是【來源物件】而不是逐個讀取點加 if —— 分岔一定會漏一個。
        st = src if src is not None else self.storage
        cfg = st.load_config()
        month = st.load_month(ym) if month is None else month
        y, m = int(ym[:4]), int(ym[5:7])

        members = [Member.from_dict(d)
                   for d in (cfg.get(f"{scope}_members") or [])]

        holiday_table = st.load_holiday_duty()
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
        ledger = (self.solver_ledger(scope, ym, src=src) if for_solve
                  else dict(st.load_ledger().get(scope) or {}))
        # 週色：決定性自動套色（依 115 行事曆 4 週交替邏輯，涵蓋跨年邊界的
        # y-1/y/y+1）為基底 → 使用者於設定頁的手動覆蓋優先蓋上。
        week_colors: dict = {}
        for yr in (y - 1, y, y + 1):
            week_colors.update(week_colors_for_year(yr))
        week_colors.update(st.load_week_colors())
        prev = st.prev_month_last_weekend(ym, scope)

        # [2026-07-13 連續值班] 上月最後 4 天已排值班 → prev_tail(連續值班軟限制
        # 的跨月常數;5 日窗最多需要往前看 4 天)。上月檔不存在時 load_month 回預設
        # (duty 空)→ prev_tail 空,規則自動退化成只看本月。
        prev_tail: dict = {}
        first = date(y, m, 1)
        # ★讀檔要在 try 之外★:`src` 那條路徑的讀取失敗代表【上月檔壞了/讀不到】,
        #   吞掉它就等於「連續值班限制安靜地只看本月」—— 正是這一批要消滅的
        #   fail-open。下面的 try 只負責容忍【內容裡的壞日期】。
        prev_duty = (st.load_month(prev_ym(ym)).get(f"{scope}_duty") or {})
        try:
            for k in range(1, 5):
                dd = first - timedelta(days=k)
                cell = prev_duty.get(dd.isoformat()) or {}
                if cell.get("person"):
                    prev_tail[dd] = str(cell["person"])
        except Exception:
            logging.exception("[roster.service] 上月值班尾端有壞資料（略過，"
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
        """`build_export` 的本體。★呼叫端必須持有 `write_barrier`★

        ★正式文件寧可匯不出來,也不可以少一半還說成功★(外審 2026-08-22
        P1-02):月檔/名單/假日/模板任一暫時讀不到時,寬鬆載入回空 —— xlsx/docx
        writer 相信 service 給的 payload,於是使用者拿到一份【格式正常、
        少班少姓名少日排班】的正式班表,而且看不出哪裡不對。
        """
        # ★匯出也會走 `build_day_input`(要開診格網)→ 它現在需要下個月★
        #   (RS-26:Clerk 梯次是兩週、可以跨月;宣告不足會當場拋,不會靜默
        #    退回寬鬆讀取 —— 那正是 `StrictSources` 的用意)。
        src = self._sources(ym, SRC_EXPORT, months=(next_ym(ym),))
        cfg = src.load_config()
        month = src.load_month(ym)
        y, m = int(ym[:4]), int(ym[5:7])
        holiday_table = src.load_holiday_duty()
        holidays = set(holiday_table["r"]) | set(holiday_table["vs"])
        ledger = src.load_ledger()

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
        # ★這裡原本把失敗吞掉照樣匯出★(外審 2026-08-22 P1-02):門診模板讀不到
        #   時 day_grid={} —— 匯出的月曆整片空白,而「真的沒有開診」與「剛好
        #   讀失敗」在文件上長得一模一樣。正式文件不接受這種 partial success。
        day_grid = self.build_day_input(ym, src=src).grid
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
    def build_day_input(self, ym: str, *,
                        src: "StrictSources | None" = None) -> DaySolveInput:
        """組裝 PGY/Clerk 日填充器輸入（開診格網 + 名單 + 切片開放 + 請假）。

        `src`＝權威輸入(見 `SRC_DAY`)。★會寫回去的路徑一律要帶★:門診模板
        讀不到 → 診間整批消失、Clerk 梯次讀不到 → 學生整批消失,而日填充器
        照樣算得完一份「合法」的班表,匯出的正式文件就少人少班。
        預覽(`run_day_solve`)刻意維持寬鬆 —— 它不寫任何檔,而套用時
        `_accept_day_locked` 會用權威輸入擋下來。
        """
        st = src if src is not None else self.storage
        cfg = st.load_config()
        month = st.load_month(ym)
        y, m = int(ym[:4]), int(ym[5:7])
        holidays = st.holidays_set()
        template = st.load_clinic_template().get("template") or {}

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
                               for x in st.load_clerk_batches())
                   if b is not None]
        covering = batches_covering(batches, y, m)     # 逐日在 solve 時再依 covers 分配
        # ★勝者判準要看得到鄰居★(外審 RS-26 R2 P2):RF-08 的「原始順序第一個」
        #   是【逐日】判定的,而配額的分母涵蓋整梯(含上個月那半段)——
        #   上個月某一天的勝者可能是一個【本月開始前就結束】的梯次。只送
        #   `covering` 的話,求解器看不到它就誤以為自己勝出,把那些永遠排不到
        #   的時段算進自己的配額。
        #   ★只加「與 active 梯次的整梯範圍重疊」的鄰居★:全部送進去會讓
        #   毫不相干的梯次也進指紋(改 12 月的梯次讓 8 月的預覽過期)。
        #   ★順序要照原始清單★:勝者判準本身就是「原始順序第一個」。
        #   ★不可以借用 `clerk_batches`★(外審 RS-26 R3):那個欄位有既有契約
        #   ——「涵蓋本月的梯次」,`day_course_stats`/側欄/報告都照它列人。
        #   把鄰居塞進去會讓本月的統計多出一個上個月就結束的梯次。
        _cov_ids = {b.id for b in covering}
        _spans = [(b.start_monday, b.start_monday + timedelta(days=13))
                  for b in covering]
        batch_order = [
            b for b in batches
            if b.id in _cov_ids
            or any(b.start_monday <= e and s0 <= b.start_monday +
                   timedelta(days=13) for s0, e in _spans)]
        # ★切片開放要保留「是哪一梯的」★(外審 2026-08-24 P2-01):壓平成全域
        #   map 之後,重疊梯次(RF-08 只採原始順序第一個)的敗者設定會污染勝者
        #   —— 勝者的 Clerk 會被叫去一個其實只替敗者開的切片室,或者反過來
        #   被敗者關掉。勝者政策要貫穿【所有】輸入維度,不只人員名單。
        bio_all = st.load_biopsy_grid()
        biopsy_open: dict = {}
        for b in covering:
            for iso, sess in (bio_all.get(b.id) or {}).items():
                try:                                   # 只採「該梯次確實涵蓋」的日期，
                    if not b.covers(date.fromisoformat(iso)):  # 忽略梯次外的過期/誤設
                        continue
                except (ValueError, TypeError):
                    continue
                biopsy_open.setdefault(b.id, {}).setdefault(iso, {}).update(sess)

        # ★請假的視野要跟梯次一樣長★(外審 2026-08-24 P1-01):配額的分母是
        #   【整梯】的開放時段,而「補不補得完」要看每個人哪幾天在 —— 下個月
        #   那半段的請假存在下個月的月檔裡。只讀本月的話,求解會在【存在可行
        #   解】時排出補不完的結果(跨月梯次的最後一格挑錯人),而套用時的過期
        #   閘門也看不到那筆請假(它根本不在這次求解的輸入裡)。
        _nxt = next_ym(ym)
        # ★沒有月檔 ≠ 沒有開診★:月檔只提供 override/請假,開診日由門診模板
        #   與年度假日決定 —— 讀不到就當「沒有 override」,不是「整月沒診」。
        #   (`load_month` 對不存在的月份回一份預設月檔,刻意如此。)
        _nxt_month = st.load_month(_nxt)
        leaves = {
            "pgy": _parse_date_map((month.get("leaves") or {}).get("pgy") or {}),
            "clerk": _parse_date_map((month.get("leaves") or {}).get("clerk") or {}),
        }
        for _c, _ds in _parse_date_map(
                (_nxt_month.get("leaves") or {}).get("clerk") or {}).items():
            leaves["clerk"].setdefault(_c, set()).update(_ds)
        # ★「那一天到底開不開診」也要看得到★:切片格網的 UI 允許勾選所有平日,
        #   而那一天可能是假日或整天停診 —— 那不是可分配的量,算進分母會撐出
        #   一個補不完的配額。
        #   ★三個月都要★(外審 RS-26 R1 P1):梯次是兩週,它可以【從上個月開始】
        #   也可以【延到下個月】—— 只放本月+下月的話,求解 9 月時 8/31 起的
        #   那一梯會被腰斬成只剩 9 月,配額算出來遠低於整梯的正解。
        course_days: set = {d.isoformat() for d in grid}
        for _om in (prev_ym(ym), _nxt):
            course_days |= {
                d.isoformat() for d in month_grid(
                    _om, template, holidays,
                    st.load_month(_om).get("grid_overrides") or {})}
        # 鎖定時段：以「目前 day_slots 內容」為鎖定值（自動排班時保留、只重排其餘）
        day_slots = month.get("day_slots") or {}
        locked: dict = {}
        for iso, sessions in (month.get("day_locks") or {}).items():
            for session, on in sessions.items():
                slots = (day_slots.get(iso) or {}).get(session)
                if on and slots is not None:
                    locked.setdefault(iso, {})[session] = slots

        # ★[RS-29] 相鄰月份【已經定下來、這次求解改不動】的時段★
        #   (全審 2026-08-24 P1-01)。RS-26 已讓配額的分母看得到整梯,但
        #   「那一格是不是已經有人」仍只看本月 —— 下個月已鎖定/已定案的切片
        #   會被當成還能自由分配的未來機會,於是本月挑錯人(存在可行解卻錯過)。
        #   兩種來源:
        #     ① 明確鎖定的時段(與本月 `locked` 同一個判準:鎖了而且有內容);
        #     ② ★已定案月份的【全部】day_slots★ —— 定案之後月檔唯讀,那些格
        #        本來就不可能被這次求解改動,語意上與鎖定完全相同。
        #   只放【相鄰月份】:本月是 `locked` 的地盤,兩邊都放會重複計數。
        course_fixed: dict = {}
        for _om in (prev_ym(ym), _nxt):
            _m = st.load_month(_om)
            _ds = _m.get("day_slots") or {}
            _final = bool(_m.get("finalized"))
            for _iso, _sess in _ds.items():
                if not isinstance(_sess, dict):
                    continue
                _locks = ((_m.get("day_locks") or {}).get(_iso) or {})
                for _s, _slots in _sess.items():
                    if _slots is None:
                        continue
                    if _final or _locks.get(_s):
                        course_fixed.setdefault(_iso, {})[_s] = _slots

        # RF-09：跨月梯次公平計數延續——對每個「起始日早於本月 1 號」的 covering 梯次，
        # 讀上月檔 day_slots 中該梯 covers 的時段，供 month_solve_day 先回放進 fc。
        prior_sessions: dict = {}
        prior_pgy: set = set()
        first = date(y, m, 1)
        cross = [b for b in covering if b.start_monday < first]
        if cross:
            prev = st.load_month(prev_ym(ym))
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
            clerk_batches=covering, batch_order=batch_order,
            biopsy_open=biopsy_open, leaves=leaves,
            # [RS-29] 相鄰月份的既定時段(鎖定/已定案)——只餵計數與可行性,
            #   不會被寫進本月結果(那是 `locked` 的語意)。
            course_fixed=course_fixed,
            # [RS-24] 整梯配額的分子要濾掉假日(切片格網含跨月日期,而那些
            #   日子在該月的 `month_grid` 裡可能根本不存在)。
            holidays=set(holidays),
            # [RS-26] 整梯真正開診的日子(本月 + 下個月的格網)——配額的分母與
            #   「之後還補不補得完」都用它,不再只憑切片格網的勾選。
            course_days=course_days,
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
        # ★驗證用的輸入也要是權威的★(外審 2026-08-22 P1-01):門診模板/梯次/
        #   名單任一讀不到時,寬鬆載入會回空 —— 求解那次若讀到同一個空狀態,
        #   兩邊指紋相等就放行,而那份班表少了整批診間或整批 Clerk。
        src = self._sources(ym, SRC_DAY, months=(next_ym(ym),))
        month, rev = src.month_snapshot(ym)
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
                    self.build_day_input(ym, src=src)):
                raise ValueError(
                    "排班結果已過期（名單／請假／Clerk 梯次／停診／門診模板等"
                    "輸入已變動），請重新排班")
        month["day_slots"] = self._overlay_locked_sessions(month, day_slots)
        month["day_report"] = report or ""      # 供「報告」鈕顯示落地當下的報告
        stamp_report_digest(month, "day")       # 見 accept_solution 的同一段
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

    @staticmethod
    def _biopsy_is_open(open_slots, order, d, session) -> bool:
        """這一天這個時段的切片室,對【當天做主的梯次】而言開不開放。

        ★向勝者梯次拿★(RF-08 / RS-26 P2-01):敗者梯次的開放設定不算數。
        沒有梯次做主 → 不開放(那天沒有 Clerk 上班,自然不會有切片)。

        ★吃的是「真的排得到的時段」而不是原始格網★(外審 RS-30 R1 P2):
        切片格網的 UI 允許勾選所有平日,而那一天可能是國定假日、週末、整日
        停診、或週三下午 —— 求解器早就用 `batch_biopsy_slots()` 把這些濾掉了,
        這裡若只看格網的勾選,手動與自動又會各說各話(而這一批的整個立論
        就是「手動路徑要知道自動排班知道的事」)。判準只留一份。
        """
        owner = day_owner_batch(order, d)
        if owner is None:
            return False
        return (d.isoformat(), session) in (open_slots.get(owner.id) or set())

    def validate_course_quota(self, ym: str, *, inp=None,
                              day_slots=None) -> tuple:
        """Clerk 切片配額的【現況】點名 → (警告清單, 該標紅的 {(梯次, 代號)})。

        ★求解當下說過的話,手改之後要有人再說一次★(RS-28,全審 P2-03):
        「切片室輪不到 / 次數不均」原本只在求解器裡算,而且只算【那一次求解的
        結果】。使用者手動於月曆調整之後(RS-24 的配額平均正是靠這些格),
        沒有任何地方再檢查一次 —— 報告是求解當下那一份,側欄的紅底又用著
        RS-24 之前的舊規則。判準與求解器共用 `biopsy_quota_warnings`。

        ★只點名「本月真的排到東西」的梯次★:邊界梯次(這個月一天都還沒排)
        本來就是 0 次,點名它只是噪音 —— 求解器用 `solved_batch_ids`
        表達同一件事,這裡的等價物是「本月的 day_slots 裡有它做主的日子」。
        """
        inp = self.build_day_input(ym) if inp is None else inp
        if day_slots is None:
            day_slots = self.storage.load_month(ym).get("day_slots") or {}
        order = arbitration_order(inp)
        _y, _m = int(ym[:4]), int(ym[5:7])
        active: set = set()
        for iso in day_slots or {}:
            try:
                d = date.fromisoformat(iso)
            except (ValueError, TypeError):
                continue
            if d not in inp.grid:
                # ★不在本月開診格網裡的鍵不算「本月排到了東西」★(外審 RS-28
                #   R1 P2):鎖定日事後變成假日/整日停診時,RF-02 會★原樣保留★
                #   那一格 —— 它在本月的鍵裡,卻不在格網裡。求解器的
                #   `solved_batch_ids` 是在迭代 `inp.grid` 時才加入梯次的,
                #   拿年月當等價物會把整梯點亮成「排過了」而誤報輪不到。
                #   ★這一個條件同時就是月份過濾★:`inp.grid` 只含本月日期,
                #   他月殘留鍵必然不在裡面。之前另外寫的 `(d.year, d.month)`
                #   檢查已被它完全涵蓋 —— 突變驗證抓到那行是死碼(反例被這裡
                #   先擋住),★量不到的守衛是死碼★,故刪除而不是留著誤導。
                continue
            owner = day_owner_batch(order, d)
            if owner is not None:
                active.add(owner.id)
        # ★不加「active 是空的就早退」★:`only_ids=active` 本來就把它們全部
        #   濾掉了,那個早退量不出任何差別 —— 量不到的守衛是死碼,留著只會
        #   讓人以為「不點名」是在那裡決定的(同 RS-27 拿掉的那一個)。
        counts = self._course_biopsy_counts(ym, order, inp.course_days)
        slots, more = batch_biopsy_slots(inp, order)
        # ★cap 的分母要先套鎖定調整★(外審 RS-28 R2 P2):與求解器同一份實作
        #   (鎖定格先拿掉、有效鎖定切片加回)—— 否則求解器合法排出的平均結果
        #   會被這裡誤報「超過配額」,反向也會漏掉等量的超額。
        apply_locked_biopsy_adjustment(inp, order, slots)
        caps = {b.id: max(1, len(slots.get(b.id, ())) // len(b.members))
                for b in inp.clerk_batches if b.members}
        return biopsy_quota_warnings(inp.clerk_batches, counts,
                                     batch_more=more, only_ids=active,
                                     caps=caps)

    def _course_biopsy_counts(self, ym: str, order, course_days=()) -> dict:
        """整梯的切片次數,★逐日以勝者梯次歸屬★ → {(梯次 id, 代號): 次數}。

        ★不可以拿 `day_course_stats` 的代號統計當梯次命名空間★
        (外審 RS-28 R1 P1):它只用「梯次日期範圍 + 代號」篩選,而 Clerk 代號
        ★跨梯會重用★ —— RS-26 就是為了這件事才把求解器的公平計數鍵改成
        `(梯次, 代號)`。兩梯部分重疊時,前一梯 C1 在重疊日的切片會被算進後一梯
        的 C1,於是後一梯明明全員都沒切,驗證器卻可能只標另一人、甚至不出聲。
        在 service 這一側再算一次代號統計,等於繞過那道隔離。

        ★各月檔只採本月鍵★:與報告/匯出/週期統計同一道過濾(見 RS-27)。
        """
        counts: dict = {}
        for m in (prev_ym(ym), ym, next_ym(ym)):
            _my, _mm = int(m[:4]), int(m[5:7])
            for iso, sessions in (self.storage.load_month(m)
                                  .get("day_slots") or {}).items():
                try:
                    d = date.fromisoformat(iso)
                except (ValueError, TypeError):
                    continue
                if (d.year, d.month) != (_my, _mm):
                    continue
                if course_days and iso not in course_days:
                    # ★掉出開診格網的內容不算數★(外審 RS-28 R1 P2):RF-02 會
                    #   原樣保留鎖定日事後變成假日/停診的那一格 —— 它不是這一梯
                    #   排得到的量,算進次數會讓配額判斷失真。
                    continue
                owner = day_owner_batch(order, d)
                if owner is None:
                    continue
                for _s, slots in (sessions or {}).items():
                    for c in ((slots or {}).get(BIOPSY) or []):
                        key = (owner.id, str(c))
                        counts[key] = counts.get(key, 0) + 1
        return counts

    def validate_day_structure(self, ym: str, *, day_slots=None,
                               inp=None) -> list:
        """★結構性錯誤★ —— 這張日排班在【規則上不可能成立】的地方。回訊息清單。

        與 `quick_validate_day` 的其他檢查刻意分開,因為兩者的處置不同
        (使用者 2026-08-25 定案):
          * 手動編輯:兩類都★只警告、不擋存檔★(設計 §16.4 的一貫作風);
          * 定案:★只擋這一類★。定案會重算帳本並讓月檔唯讀 —— 帶著
            「切片室 3 個人」定案下去,只能靠解除定案才救得回來。
            而「當日請假卻被排」「輪不到切片室」那些是【要知道但可能正確】
            的現況,擋了會讓月結卡死。

        檢的是四件事(每一條都出自設計文件,不是這裡發明的):
          1. 照光 = 0~1 位★本月 PGY★(P2:每時段恰 1 位 PGY,含週三下午);
          2. 治療室 = 0~1 位★本月 PGY★(P2;週三下午休診 → 0 位也合法,
             RS-15 兩位 PGY 月的二早/四下/五早同樣是 0 位);
          3. 切片室 = 0~1 位★當天勝者梯次的 Clerk★(C2 一個時段一個人;
             C3 有開才排 → 0 位合法。勝者判準與求解器共用
             `solve_day.day_owner_batch`);
          4. 同一個人同一時段只能有一個工作(放假也算矛盾),
             跟診房不得超過房容量。

        ★「幾位」與「誰」都要查★:只查人數的話,「照光排一位 Clerk」照樣過關
        —— 那一格的規則是「一位 PGY」,身分本來就是規則的一半。

        `day_slots` / `inp` 可由呼叫端帶入,讓定案能驗【它正要定案的那一份
        月檔快照】,而不是另外再讀一次(外審 RS-6 的同一個理由:兩次讀取
        之間的差異會讓「驗過的」與「存下去的」不是同一份)。
        """
        if day_slots is None:
            day_slots = self.storage.load_month(ym).get("day_slots") or {}
        if not day_slots:
            # ★沒有日排班就沒有結構可言 —— 而且不可以為了「檢查」去讀一份
            #   這條路本來不需要的權威輸入★:定案原本不碰門診模板/梯次/切片
            #   格網,多讀一次就是多一條與定案無關的失敗路徑(只有 R/VS 值班
            #   的月份會因為那些檔案的問題而定不了案)。
            return []
        inp = self.build_day_input(ym) if inp is None else inp
        out: list = []
        cap = inp.capacity
        pgy_set = {str(c) for c in inp.pgy_roster}
        order = arbitration_order(inp)
        _y, _m = int(ym[:4]), int(ym[5:7])
        for iso in sorted(day_slots or {}):
            try:
                d = date.fromisoformat(iso)
            except (ValueError, TypeError):
                continue
            if (d.year, d.month) != (_y, _m):
                # ★他月的殘留鍵不歸這個月管★(外審 RS-27 R1 P1):
                #   `set_day_slot` 不強制 `d ∈ ym`,月檔因此可能殘留他月的鍵
                #   (舊檔、手改 JSON、跨機合併)——報告、匯出、週期統計三處
                #   都各自把非本月鍵濾掉,這裡是同一道過濾。
                #   ★而且這裡是【閘門】★:本月的 UI 只列得出本月的日期,
                #   拿他月的殘留擋住定案,使用者在程式裡沒有任何辦法修好它
                #   —— 那就是一道沒有出口的閘門(只能手改 JSON)。
                #   它自己那個月份的驗證會看到它。
                continue
            owner = day_owner_batch(order, d)
            owner_members = {str(c) for c in (owner.members if owner else [])}
            for session, slots in sorted((day_slots.get(iso) or {}).items()):
                # ── 同一個人同一時段只能做一件事 ──────────────────────
                _where: dict = {}
                for slot, members in (slots or {}).items():
                    for c in (members or []):
                        _where.setdefault(str(c), []).append(str(slot))
                for c, places in sorted(_where.items()):
                    if len(places) > 1:
                        out.append(f"{iso} {session}:{c} 同時被排在 "
                                   f"{'、'.join(places)} —— 同一個人同一時段"
                                   f"只能做一件事,請確認名單是否有重複代號")
                for slot, members in sorted((slots or {}).items()):
                    members = [str(c) for c in (members or [])]
                    # ★空格不必特別短路★:下面每一條對空清單本來就是「沒問題」
                    #   (`> 1` 不成立、沒有 outsider、沒有超容量)。加一個
                    #   `if not members: continue` 量不出任何差別 —— 那種
                    #   守衛只會讓人以為「空格是在這裡被放行的」。
                    if slot in (PHOTO, TREATMENT):
                        if len(members) > 1:
                            out.append(
                                f"{iso} {session}:{slot} 排了 {len(members)} 位"
                                f"({'、'.join(members)})—— 每個時段只能有 1 位")
                        outsiders = [c for c in members if c not in pgy_set]
                        if outsiders:
                            out.append(
                                f"{iso} {session}:{slot} 排了 "
                                f"{'、'.join(outsiders)} —— 這一格只能排本月 PGY")
                    elif slot == BIOPSY:
                        if len(members) > 1:
                            out.append(
                                f"{iso} {session}:切片室排了 {len(members)} 位"
                                f"({'、'.join(members)})—— 一個切片時段一個人")
                        outsiders = [c for c in members
                                     if c not in owner_members]
                        if outsiders:
                            _who = (f"當天的梯次是「{owner.id}」"
                                    if owner else "當天沒有任何 Clerk 梯次")
                            out.append(
                                f"{iso} {session}:切片室排了 "
                                f"{'、'.join(outsiders)} —— {_who},"
                                f"切片室只能排該梯次的 Clerk")
                    elif slot not in _SPECIAL_SLOTS and len(members) > cap:
                        out.append(f"{iso} {session} {slot} 診:{len(members)} 人"
                                   f"超過容量 {cap}")
        return out

    def quick_validate_day(self, ym: str) -> list:
        """[RS-07] PGY/Clerk 日排班快速檢查（warn 不擋存，符合設計 §16.4）。回傳訊息清單：
        (a)請假者被排、(b)代號不在當日名單/梯次、(c)週三下午治療室/切片有人、
        (d)停診房仍有人(兜 RS-03/05 殘留)、
        (e)★合併後才成立的名單身分衝突★(外審 2026-08-22 P2-03:兩台各改
        一個檔,git 乾淨合併但結果違規)—— 它不屬於某一天,卻會讓每一天都
        少一個人,放在同一個警告面板使用者才有機會修。

        ★結構性錯誤(特別格的人數/身分、一人多工、房容量)在
        `validate_day_structure()`★(RS-27):同一份實作也給定案當閘門用
        —— 面板上看到的與擋定案的必須是同一套判準,不然使用者會遇到
        「面板沒說什麼,定案卻不讓過」。"""
        out: list = list(self.validate_roster_identity_invariants())
        month = self.storage.load_month(ym)
        day_slots = month.get("day_slots") or {}
        if not day_slots:
            return out
        inp = self.build_day_input(ym)
        pgy_set = {str(c) for c in inp.pgy_roster}
        _order = arbitration_order(inp)
        # [RS-30] RS-15 的判準與求解器同一條:該月 PGY 名單恰 2 位。
        _two_pgy = len({str(c) for c in inp.pgy_roster}) == 2
        # ★整梯真正排得到的切片時段★:算一次就好(逐格重算是 O(n²))。
        _open_slots, _ = batch_biopsy_slots(inp, _order)
        out.extend(self.validate_day_structure(ym, day_slots=day_slots,
                                               inp=inp))
        # [RS-28] 切片配額的現況點名(輪不到 / 次數不均)——手改過的格也算數。
        out.extend(self.validate_course_quota(ym, inp=inp,
                                              day_slots=day_slots)[0])
        closures = self.clinic_closures(ym)
        for iso in sorted(day_slots):
            try:
                d = date.fromisoformat(iso)
            except (ValueError, TypeError):
                continue
            # ★名單檢查也要套勝者判準★(全審 2026-08-24 P2-03):同一天被多梯
            #   涵蓋時只有勝者梯次排得到人(RF-08),敗者梯次的成員那天不上班
            #   —— 拿聯集當「合法名單」,手動把敗者梯次的 Clerk 排進跟診診間
            #   完全不會被點名(結構驗證只管特別格與容量)。判準與求解器、
            #   與「＋選人」候選清單共用同一個 `day_owner_batch`。
            _owner = day_owner_batch(_order, d)
            clerk_today = {str(c) for c in ((_owner.members or [])
                                            if _owner else ())}
            valid = pgy_set | clerk_today
            leavers = {mid for mid, ds in (inp.leaves.get("pgy") or {}).items()
                       if d in ds}
            leavers |= {mid for mid, ds in (inp.leaves.get("clerk") or {}).items()
                        if d in ds}
            for session, slots in (day_slots.get(iso) or {}).items():
                # 房號型別由 `clinic_closures` 統一正規化(規則只有一份)
                closed = set((closures.get(iso) or {}).get(session) or [])
                for slot, members in (slots or {}).items():
                    members = members or []
                    if (d.weekday() == 2 and session == "下午"
                            and slot in (TREATMENT, BIOPSY) and members):
                        out.append(f"{iso} {session}：{slot} 週三下午應休診，"
                                   f"卻排了 {'、'.join(members)}")
                    if slot in closed and members:
                        out.append(f"{iso} {session}：{slot} 已停診，"
                                   f"卻仍排了 {'、'.join(members)}")
                    # ★[RS-30 / 全審 P2-04] 手動路徑也要知道「這一格今天開不開」★
                    #   結構驗證刻意只管【幾位、什麼身分】(RS-27 的定義),
                    #   而自動排班還知道另外兩件事,手動編輯卻完全不知道:
                    #   ①切片室那一格今天有沒有開放(C3:開放格網是手動維護的);
                    #   ②RS-15 兩位 PGY 月的二早/四下/五早治療室不排。
                    #   依使用者 2026-08-25 的定案,這兩條★只警告、不擋存也不擋
                    #   定案★(它們是排班規則,不是「規則上不可能成立」的結構)。
                    if (slot == BIOPSY and members
                            and not self._biopsy_is_open(
                                _open_slots, _order, d, session)):
                        out.append(f"{iso} {session}：切片室今天沒有開放，"
                                   f"卻排了 {'、'.join(members)}"
                                   f"——請確認開放格網或改回")
                    if (slot == TREATMENT and members and _two_pgy
                            and (d.weekday(), session) in TWO_PGY_PHOTO_ONLY):
                        out.append(f"{iso} {session}：兩位 PGY 的月份這個時段"
                                   f"不排治療室（只排照光），卻排了 "
                                   f"{'、'.join(members)}")
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
    def settle_intent(self, scope: str, ym: str, kind: str = "all", *,
                      witness_ym: "str | None"):
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
        # ★意圖要在來源寫入之前記下,所以來源自己失敗時會留下一筆不存在的債★
        #   (外審 2026-08-22 P2)`witness_ym` 指的是【這個區塊會改到的那份
        #   來源月檔】:進場先量它的識別,例外時再量一次 —— 內容沒變就代表
        #   什麼都沒落地,那筆意圖是誤報,清掉它(否則它會擋住定案,而其實
        #   沒有任何東西需要收斂)。★量不到一律當作有債★(見 `_source_unchanged`)。
        #   `witness_ym=None` ＝【來源在進場之前就已經落地】(跨月週五那條),
        #   此時進場即負債,失敗一律保留。
        _w0 = (self.storage.month_revision(witness_ym)
               if witness_ym else None)
        state = RosterService._DerivedIntent()
        try:
            yield state
        except BaseException:
            if mine and self._source_unchanged(witness_ym, _w0):
                logging.info(
                    "[roster.service] %s %s 的來源沒有任何變動（%s）→ "
                    "撤掉這次記下的意圖", scope, ym, kind)
                self.storage.clear_pending_settle(scope, ym, kind)
            raise
        if mine and state.kept:
            logging.warning(
                "[roster.service] ★%s %s 的%s尚未重建成功★(%s)—— 意圖保留,"
                "下次開程式會用月檔收斂", scope, ym, kind, state.kept_reason)
            return
        if mine:
            self.storage.clear_pending_settle(scope, ym, kind)

    def _source_unchanged(self, witness_ym: "str | None", rev0) -> bool:
        """那一份來源月檔的內容,從進場到現在【證明得了】沒有變過嗎?

        ★量,不要推理★:靠「例外的型別看起來像是寫入前拋的」去猜,遲早會
        猜錯一個路徑,而猜錯的方向是【靜默丟掉一筆真的債】。
        量不到(檔案暫時讀不到)或本來就沒有 witness 一律回 False ——
        「查不出來」不可以被當成「沒事發生」;多留一筆意圖的代價只是下次
        開程式重算一次已經正確的衍生物。
        """
        if not witness_ym or rev0 is None:
            return False
        if not RosterStorage.revision_is_readable(rev0):
            return False
        now = self.storage.month_revision(witness_ym)
        return RosterStorage.revision_is_readable(now) and now == rev0

    @contextlib.contextmanager
    def biopsy_obligation(self, ym: str, what: str = "週六切片重排", *,
                          witness_ym: "str | None"):
        """「先改來源、再重建切片衍生物」的★唯一★包裝(外審 2026-08-22 P2-01)。

        用法:
            with self.biopsy_obligation(ym, witness_ym=ym) as ob:
                mutate_source()                # 請假/值班/梯次…
                try:
                    self.recompute_saturday_biopsy(ym)
                except Exception as e:
                    ob.keep(str(e))            # 來源已改、衍生物沒跟上

        ★不要靠每個呼叫端自己記得補意圖★:`set_leaves` 與跨月週五那兩條路
        原本只 log 就算了 —— 請假已經落地、`saturday_biopsy`/`biopsy.json`
        還停在舊狀態,而且沒有任何人負責收斂(使用者只看到「請假成功」)。
        ★重建成功時什麼都不留★:意圖只在真的失敗時保留。
        ★`witness_ym` 沒有預設值★(外審 2026-08-22 P2):它決定「來源自己
        失敗時要不要撤掉這筆意圖」,而兩種答案各自對得起某些呼叫端 ——
        給它預設值的話,寫錯的那一端會安靜地丟掉一筆真的債(跨月連動就是
        這種:來源是【本月】的週五值班,義務卻記在下個月頭上)。
        """
        with self.settle_intent("r", ym, kind="biopsy",
                                witness_ym=witness_ym) as state:
            try:
                yield state
            finally:
                if state.kept:
                    logging.warning(
                        "[roster.service] ★%s 的%s未完成★(%s)—— 意圖保留",
                        ym, what, state.kept_reason)

    def _biopsy_intent(self, scope: str, ym: str, *,
                       witness_ym: "str | None"):
        """R 才會寫切片帳本 → 只有 R 需要這一筆意圖(見 `settle_intent`)。

        ★種類是 biopsy★(外審次輪 P2-02):這條路只重建切片計數,帳本沒動 ——
        用 "all" 的話,它會涵蓋(並在成功時清掉)別人未完成的帳本義務。
        """
        if scope != "r":
            # ★仍然吐一個狀態物件★:呼叫端不必為了 VS 分岔寫 if,
            #   對它 .keep() 是無害的(沒有意圖可保留)。
            return contextlib.nullcontext(RosterService._DerivedIntent())
        return self.settle_intent("r", ym, kind="biopsy",
                                  witness_ym=witness_ym)

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
            # ★會整份寫回去的路徑也要驗內容★(外審 RS-20 R1-4):typed loader
            #   會把壞掉的項目先濾掉,接著這裡把「濾掉之後」的整份存回去 ——
            #   ★那是永久刪除★,而使用者只是在設定頁改了另一筆資料。
            data, rev = self.storage.canonical_snapshot(name, validate=True)
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
            month.pop(report_digest_key("day"), None)   # 識別跟著報告一起走
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
        # ★展開與寫入必須在同一個臨界區★(外審 2026-08-22 P1-04):停診存的是
        #   【展開後的每一天】,而「哪些天有開」是在這裡依當時的模板算的 ——
        #   算完到寫入之間背景 pull 把他機新增的場次(例如同期間多了週四上午
        #   101 診)合併進來的話,那些新場次不在 base 裡,不會被寫 closed_rooms,
        #   而 UI 回報「整段停診成功」。之後自動排班讀的是【現在的】模板 ——
        #   週四上午 101 是開的,學生就被排進去了。
        # ★而且要用權威輸入★:模板/假日表暫時讀不到時,寬鬆載入回空 ->
        #   base 全空 -> candidates==0 -> 使用者看到的是「模板上沒開診」,
        #   一個與事實無關卻很有說服力的錯誤訊息。
        with self.storage.write_barrier():
            src = self._sources(ym, SRC_CLOSURE)
            template = src.load_clinic_template().get("template") or {}
            base = month_grid(ym, template, src.holidays_set())
            return self._set_clinic_closed_locked(
                ym, room, start, end, sessions, closed, base)

    def _set_clinic_closed_locked(self, ym: str, room: str, start: date,
                                  end: date, sessions, closed: bool,
                                  base: dict) -> dict:
        """`set_clinic_closed` 的本體(展開結果 `base` 由呼叫端在臨界區內算好)。
        ★呼叫端必須持有 `write_barrier`★"""

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
                month.pop(report_digest_key("day"), None)
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
            # ★整批權威輸入一次嚴格讀完★(外審 2026-08-22 P1-01):假日表/名單/
            #   週色任一暫時讀不到時,寬鬆載入會把它變成合法的空值 —— solver
            #   按平日排、預覽完全正常,而套用時重建 context 若讀到同一個壞
            #   狀態,兩次指紋都是【同樣錯的空語意】,比對相等就放行。
            src = self._sources(ym, SRC_RVS)
            month, month_rev = src.month_snapshot(ym)
            ctx = self.build_context(scope, ym, month=month, for_solve=True,
                                     src=src)
            # ★結轉進來的帳本要與它所描述的那些月份一致★(外審 RS-20 P1-02)
            _stale, _unknown = self.stale_settlements(scope, ym, src=src)
            if _stale:
                raise ValueError(
                    f"{'、'.join(_stale)} 的帳本結算與該月實際排班不一致"
                    f"（班表改過之後沒有重算，或上一次結算沒有完成）；"
                    f"直接排 {ym} 會用錯誤的公平目標（帳本結轉是下個月的"
                    f"基準）。請先到那個月按「重算帳本」（或重開排班程式，"
                    f"開啟時會自動收斂），再回來排班。"
                    f"（若該月早於帳本保留期而無法重算，"
                    f"請直接調整 ledger.json 的餘額。）")
        res = solve_duty(ctx, allow_disable_color=allow_disable_color)
        if _unknown:
            # ★查不出來要講出來★:舊版程式記的分錄沒有識別,月檔被刪的也查不到。
            res.diagnosis = list(res.diagnosis) + [
                f"{'、'.join(_unknown)} 的帳本結算沒有可比對的識別"
                f"（舊版程式所記或月檔已不在），無法確認它是否仍與該月實際"
                f"排班一致；若那幾個月曾手動換班，請先按「重算帳本」。"]
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

    def _prev_month_friday_duty(self, year: int, month: int, *,
                                src: "StrictSources | None" = None) -> dict:
        """月初 1 號是週六時,回 {上月最後一天(週五): 值班人};其餘情況回空 dict。

        只在真的需要時才去讀上月月檔(避免每次重排都多一次磁碟 IO)。
        ★寬鬆路徑讀不到就回空★ —— 沒有上月資料時「週五連動」單純不生效,
        不可因此讓整個切片重排失敗;★權威路徑(`src`)不吞★:那條路要寫回
        biopsy.json,少一個週五連動就是把切片排給錯的人並記進計數帳本。
        """
        first = date(year, month, 1)
        if first.weekday() != 5:            # 1 號不是週六 → 週五在本月,不必跨月
            return {}
        fri = first - timedelta(days=1)
        pym = f"{fri.year:04d}-{fri.month:02d}"
        if src is not None:
            prev = src.load_month(pym)
        else:
            try:
                prev = self.storage.load_month(pym)
            except Exception:
                logging.debug("[roster.service] 讀上月月檔失敗(週五連動略過)",
                              exc_info=True)
                return {}
        cell = ((prev.get("r_duty") or {}).get(fri.isoformat()) or {})
        person = cell.get("person")
        return {fri: str(person)} if person else {}

    def _biopsy_compute(self, ym: str, duty_by_date: dict,
                        book: "dict | None" = None,
                        month: "dict | None" = None, *,
                        src: "StrictSources | None" = None) -> tuple:
        """以指定值班表計算該月週六切片
        → (assign, notes, pair, counts_after, names)。

        counts 基底＝biopsy.json 回滾本月舊分錄後的累計（同月重排不重複累計；
        在副本上回滾，不動傳入的 book）。純計算，不寫任何檔。"""
        from cmuh_common.roster.saturday_biopsy import rollback_biopsy
        st = src if src is not None else self.storage
        cfg = st.load_config()
        members = [Member.from_dict(d) for d in (cfg.get("r_members") or [])]
        y, m = int(ym[:4]), int(ym[5:7])
        # ★[2026-08-02 補審] 跨月週五收攏在這裡,而不是各呼叫端自己補★
        #   放在 recompute 那邊的話,report 預覽(render_report)這條路徑就沒有,
        #   於是「月初 1 號是週六」的月份會出現【預覽的切片人選與定案後不同】。
        #   本函式是所有切片計算的唯一入口,補在這裡才不會有人漏掉。
        duty_by_date = dict(duty_by_date)
        duty_by_date.update(self._prev_month_friday_duty(y, m, src=src))
        # [2026-07-27] month 可由呼叫端傳入【記憶體中尚未存檔的月檔】——手動指定
        # 切片後若這裡自行重讀磁碟，會讀到舊的 override 而把指定吃掉。
        if month is None:
            month = st.load_month(ym)
        leaves = _parse_date_map((month.get("leaves") or {}).get("r") or {})
        if book is None:
            book = st.load_biopsy()
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
                                  month: "dict | None" = None, *,
                                  src: "StrictSources | None" = None) -> tuple:
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
            return self._recompute_saturday_biopsy_locked(
                ym, month, src=src if src is not None
                else self._sources(ym, SRC_BIOPSY))

    def _recompute_saturday_biopsy_locked(
            self, ym: str, month: "dict | None", *,
            src: "StrictSources | None" = None) -> tuple:
        """★切片重排是【會寫回去】的計算,一律走權威輸入★(外審 P1-01):
        `config.json` 讀不到就沒有 R 成員 → 切片一個人都排不出來,而
        `settle_biopsy` 照樣把「本月沒有人切片」寫進計數帳本。"""
        own = month is None
        if src is None:                       # 呼叫端沒帶 → 自己宣告(仍在臨界區內)
            src = self._sources(ym, SRC_BIOPSY)
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
        book, book_rev = src.snapshot("biopsy.json")
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
            ym, duty_by_date, book, month=month, src=src)
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
            # 來源就是這一份月檔:save_month 沒成功 → 什麼都沒落地 → 沒有債。
            with self.settle_intent("r", ym, kind="biopsy", witness_ym=ym):
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
        # ★驗證用的輸入也必須是權威的★(外審 2026-08-22 P1-01):這裡重建
        #   context 是為了證明「舊結果仍符合現況」——若它讀到的是被寬鬆載入
        #   正規化成空值的壞檔,求解那次也讀到同一個空值時兩邊指紋相等,
        #   守衛就替一份【依錯誤輸入算出來的班表】背書並寫進帳本。
        src = self._sources(ym, SRC_SETTLE)
        month, _month_rev = src.month_snapshot(ym)
        if month.get("finalized"):
            raise FinalizedMonthError(f"{ym} 已定案（唯讀）；解除定案後才能套用排班")

        # result 必須仍符合「當前」輸入才落地：預覽後若 請假/指定/鎖定/名單/假日
        # 任一改動，舊 result 可能把請假者排上或違反新 directive，settle 出的帳本/
        # 報告就與實況脫節。以重建的 ctx 驗證，不符即拒絕、要求重排（寫入前）。
        ctx = self.build_context(scope, ym, for_solve=True,   # 與求解同一種
                                 src=src)
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
        # ★這一份只是【診斷用的快照】,不是任何人的輸入★(外審 RS-20 P1-01):
        #   下個月的跨月連休鏈改由上月 canonical duty 推導
        #   (`storage.last_weekend_of`)—— 手動換班之後這個欄位就過期了,
        #   當初正是它讓 11 月把跨月週日固定給「換班前」的那個人。
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
                    self.recompute_saturday_biopsy(ym, month, src=src))
                report = month["report_r"]
            except Exception as e:  # noqa: BLE001
                biopsy_book = None
                _bio_failed = str(e) or e.__class__.__name__
                logging.exception("[roster.service] 週六切片重排失敗"
                                  "（值班照常落地，切片留待收斂）")
        month[f"report_{scope}"] = report
        # ★報告要能證明自己描述的是哪一份班表★(外審 2026-08-22 P1-03):
        #   之後任何一次手動換班都會讓它對不上,定案 PDF 才不會把舊報告
        #   當成最終班表印出去(見 `report_state`)。
        stamp_report_digest(month, scope)

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
            ledger, _led_rev = src.snapshot("ledger.json")
            settle_month(ledger, scope, ym, result.points_by_person,
                         duty_digest=_duty_digest(month, scope))
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
        # ★帳本的意圖要包住【月檔的寫入】★(外審 RS-21 P2-01):在月檔落地
        #   之後才記,中間被砍就留下一個沒有分錄也沒有意圖的月份。
        #   witness=本月:月檔根本沒改成功時,那筆意圖是誤報,要撤掉。
        with (self.storage.write_barrier(),
              self.settle_intent(scope, ym, kind="ledger",
                                 witness_ym=ym) as _led,
              self._biopsy_intent(scope, ym, witness_ym=ym) as _intent):
            self.update_month(ym, _mut)
            if _holder.get("book") is not None:
                self.storage.save_biopsy(
                    _holder["book"], expected_revision=_holder.get("rev"))
            if _holder.get("failed"):
                _intent.keep(f"週六切片重排失敗:{_holder['failed']}")
            # ★換班＝帳本也變了★:不同步的話,下個月的公平目標會用換班前的
            #   結轉算(而使用者不會知道要按「重算帳本」)。
            self._converge_ledger_locked(scope, ym, _led)
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
                # ★這裡沒有 witness★:來源是【本月】的週五值班,而且它在
                #   進入這個區塊之前就已經存好了 —— 下月月檔沒變不代表沒有債,
                #   正好相反,那就是「下月的切片還沒跟上」本身。
                with self.biopsy_obligation(
                        _next_ym, "跨月週五連動的切片重排",
                        witness_ym=None) as ob:
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
        # witness＝這一份月檔:定案/被搶先而整個 update_month 失敗時,盤上什麼
        # 都沒動 → 那筆意圖是誤報,撤掉(外審 2026-08-22 P2)。
        with (self.storage.write_barrier(),
              self._biopsy_intent("r", ym, witness_ym=ym)):
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
            month.pop(report_digest_key(scope), None)   # 識別跟著報告一起走
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
        # ★清除也是手動改動班表★:帳本要跟著實排走,而且意圖要包住月檔的
        #   寫入(見 set_cell 的同一段)。
        with (self.storage.write_barrier(),
              self.settle_intent(scope, ym, kind="ledger",
                                 witness_ym=ym) as _led,
              self._biopsy_intent(scope, ym, witness_ym=ym) as _intent):
            self.update_month(ym, _mut)
            if _holder.get("book") is not None:
                self.storage.save_biopsy(
                    _holder["book"], expected_revision=_holder.get("rev"))
            if _holder.get("failed"):
                _intent.keep(f"週六切片重排失敗:{_holder['failed']}")
            self._converge_ledger_locked(scope, ym, _led)

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
        # 來源＝這個月的月檔(請假寫在裡面);請假本身被擋下時不留債。
        with self.biopsy_obligation(ym, "請假變動後的週六切片重排",
                                    witness_ym=ym) as ob:
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

    def rename_member(self, scope: str, old_id: str, new_id: str, *,
                      resume: bool = False) -> int:
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
            # ★多檔交易要有 durable 的意圖★(外審 RS-23 P2-04)——
            #   記在【前置檢查通過之後、開始寫檔之前】(見
            #   `_rename_member_locked`):同號的 no-op、空白代號、撞名等
            #   根本不會動到任何檔,替它們記一筆改名意圖是誤報。
            return self._rename_member_locked(scope, old_id, new_id,
                                              resume=resume)

    def _rename_never_started(self, rec: dict) -> bool:
        """這一筆改名意圖是不是【一個檔都還沒寫】就中斷了。

        config 是改名的第一個寫入,而正典檔是整份原子寫入 —— 所以
        「config.json 的 revision 還是交易開始前那一個」就證明後面每一個檔
        都還沒被動過。★沒有記 revision 的舊意圖★退回同一個道理的推理:
        名單裡還是 old_id、而 new_id 不在 —— 我們自己的交易不可能停在這裡
        (它第一步就會把名單換掉)。
        """
        scope = str(rec.get("scope") or "")
        pre = str(rec.get("config_rev") or "")
        cfg, rev = self.storage.canonical_snapshot("config.json")
        if pre:
            return pre == rev
        ids = {str(m.get("id")) for m in (cfg.get(f"{scope}_members") or [])}
        return (str(rec.get("old_id")) in ids
                and str(rec.get("new_id")) not in ids)

    def reconcile_pending_renames(self) -> list:
        """開程式時把「做到一半的改名」做完(finish-forward)。→ 收斂清單。

        ★方向是往前做完,不是回頭★:回滾需要當初那份記憶體快照,重開之後
        沒有了;而「把 old_id 改成 new_id」對每一個檔案都是冪等的 —— 已經
        改過的檔不含 old_id,再跑一次就是 0 個異動。所以往前做完是唯一
        deterministic 的收斂方向,也正是使用者本來要的結果。
        """
        out: list = []
        recs = self.storage.load_pending_renames_strict()
        for rec in recs:
            scope = str(rec.get("scope") or "")
            old_id = str(rec.get("old_id") or "")
            new_id = str(rec.get("new_id") or "")
            if _rename_intents_collide(rec, recs):
                # ★互相牽扯的意圖不可以自動續作★(外審 Codex RS-23 P1-02):
                #   A→B 與 C→B 共用目標、A→B 與 B→C 首尾相接 —— 收斂順序
                #   會決定誰的歷史被誰覆蓋,而程式沒有資格挑。
                logging.error(
                    "[roster.service] ★%s 的改名意圖互相牽扯(%s → %s)★ "
                    "保留,請人工確認 pending_rename.json", scope, old_id,
                    new_id)
                continue
            try:
                with self.storage.write_barrier():
                    if self._rename_never_started(rec):
                        # ★證明盤上是完整的舊狀態 → 收斂方向是「全舊」★:
                        #   交易一個檔都沒動,沒有東西要做完(這也是外審
                        #   允許的兩個終點之一:全舊或全新,不能半套)。
                        self.storage.clear_pending_rename(scope, old_id,
                                                          new_id)
                        logging.warning(
                            "[roster.service] 上次的改名(%s %s → %s)還沒動到"
                            "任何檔就中斷了 → 維持原狀", scope, old_id, new_id)
                        continue
                    proof = str(rec.get("config_digest_after") or "")
                    if proof != _config_intent_digest(
                            self.storage.canonical_snapshot("config.json")[0]):
                        # ★名單的形狀是必要條件、不是充分條件★(外審 Codex
                        #   第 2 輪 P1):他機可能獨立把 old 移除、加進一位
                        #   合法的同代號成員 —— 那個盤面與我們的半套長得
                        #   一模一樣。要有「這份 config 是我們寫的」的證據
                        #   (寫完當下讀回來的 revision)才准續作;沒有記到
                        #   (剛好崩在那兩個寫入之間)或後來被別人動過,一律
                        #   保留意圖交給人。
                        logging.error(
                            "[roster.service] ★%s 的改名(%s → %s)沒有可證明"
                            "的半套狀態★(config 不是這次交易寫的 revision)"
                            " —— 保留意圖,請人工確認 pending_rename.json",
                            scope, old_id, new_id)
                        continue
                    # (revision 相符就【蘊含】名單只剩 new_id —— 那份
                    #  config 就是我們寫出去的那一份。所以這裡不再另外檢查
                    #  名單形狀:一個永遠不可能成立的判斷等於沒有判斷,
                    #  它只會讓人以為多了一層保護。名單同時有 old/new 的
                    #  形狀由 `_rename_member_locked` 自己擋 —— 那一層才是
                    #  公開 API 的入口。)
                    self.rename_member(scope, old_id, new_id, resume=True)
            except Exception:
                logging.exception(
                    "[roster.service] ★%s 的改名(%s → %s)無法收斂★ "
                    "意圖保留,請人工確認", scope, old_id, new_id)
                continue
            out.append((scope, old_id, new_id))
            logging.warning("[roster.service] 上次未完成的改名已做完:"
                            "%s %s → %s", scope, old_id, new_id)
        return out

    def _rename_member_locked(self, scope: str, old_id: str,
                              new_id: str, *, resume: bool = False) -> int:
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
        if resume and old_id in ids and new_id in ids:
            # ★兩個都在＝這不是我們的半套改名★(外審 Codex RS-23 P1-02):
            #   config 是第一個被寫的檔,而它是整份原子寫入 —— 我們自己的
            #   交易只會留下「只剩 new_id」。兩個都在表示 new_id 是【另一位
            #   合法成員】(他機新增/別的改名),續作會把 old 的餘額、切片
            #   計數、請假鍵覆蓋到他身上,而那是救不回來的。
            raise ValueError(
                f"{scope.upper()} 名單同時有 {old_id} 與 {new_id} —— "
                f"這不是未完成的改名留下的狀態,拒絕自動續作。"
                f"請人工確認 pending_rename.json。")
        if old_id not in ids and not (resume and new_id in ids):
            # ★續作時名單可能已經改完了★(外審 RS-23 P2-04):config 是第一個
            #   寫的檔 —— 「名單只剩 new_id、其餘檔還停在 old_id」正是被砍之後
            #   的預期中間狀態,不可以在這裡中止,否則那次改名永遠做不完。
            raise ValueError(f"{scope.upper()} 名單中找不到代號 {old_id}")
        if new_id in ids and not resume:
            raise ValueError(f"代號 {new_id} 已存在於 {scope.upper()} 名單，不可重複")
        # ★改名要把這些檔整份寫回去 → 來源一律用嚴格快照★(外審次輪 P2-01):
        #   壞檔的寬鬆載入回空,改名「成功」之後帳本/切片計數就只剩這一次
        #   改寫的殘骸(回滾用的 snap 也是同一份空的,救不回來)。
        # 三份都會被整份寫回去 → 一律驗內容(見 `_update_canonical` 的說明)。
        ledger = self.storage.canonical_snapshot("ledger.json",
                                                 validate=True)[0]
        holiday = self.storage.canonical_snapshot("holiday_duty.json",
                                                  validate=True)[0]
        biopsy = (self.storage.canonical_snapshot("biopsy.json",
                                                  validate=True)[0]
                  if scope == "r" else None)
        _loaded = {ym: self.storage.load_month_snapshot(ym)
                   for ym in yms}
        months = {ym: mr[0] for ym, mr in _loaded.items()}
        month_revs = {ym: mr[1] for ym, mr in _loaded.items()}
        # ★改名之前先記下「這一筆本來是不是 fresh」★(外審 RS-22 P2-01):
        #   改名後無條件重算識別的話,一個【本來就對不上】的結算會被洗成
        #   fresh —— 而那正是閘門用來擋下「拿舊帳排下個月」的唯一證據。
        _was_fresh = {ym: (_duty_digest(m, scope)
                           if m is not None else "")
                      for ym, m in months.items()}
        _stale_before: list = []       # 改名前就已經對不上的月份(見下)

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
                # ★這一欄改名時會被改寫,撞名掃描就必須看它★(外審 Codex
                #   第 3 輪 P1):否則 new_id 已是某離職者的手動切片指定時,
                #   改名會把兩人的指定混為一人。
                if new_id in (m.get("biopsy_override") or {}).values():
                    clashes.add("切片指定")
        # ★續作時 new_id 一定已經出現在半套資料裡★(外審 RS-23 P2-04):
        #   那是【我們上一次自己寫進去的】,不是別人的歷史紀錄 —— 這幾道
        #   撞名守衛在收斂時要跳過,否則那次改名永遠做不完。
        if clashes and not resume:
            raise ValueError(
                f"代號 {new_id} 已出現在歷史資料（{'、'.join(sorted(clashes))}），"
                f"為免與離職者紀錄混同，請改用全新代號")

        if resume:
            _both = _files_with_both_ids(scope, old_id, new_id, ledger,
                                        holiday, biopsy, months)
            if _both:
                # ★逐檔的不變量★(外審 Codex RS-23 第 2 輪 P1):本交易對每一個
                #   檔都是「整份把 old 換成 new」的原子寫入 —— 所以合法的中間
                #   狀態裡,每一個檔要麼【全舊】、要麼【全新】。同一個檔同時
                #   出現兩者,就證明那個 new 的資料不是我們寫的(他機獨立新增
                #   的成員、或 merge 把舊的 old 帶回來),續作會把兩個人的歷史
                #   混成一個人 —— 而那救不回來。
                #   ★不可以只看名單★:名單的形狀(只剩 new)是必要條件,不是
                #   充分條件。
                raise ValueError(
                    f"{'、'.join(_both)} 同時有 {old_id} 與 {new_id} 的資料 ——"
                    f"這不是未完成的改名留下的狀態,拒絕自動續作。"
                    f"請人工確認 pending_rename.json 與這些檔案。")

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

        # ★新鮮度識別也是要跟著改名走的資料★(外審 RS-21 P1-01):
        #   `duty_digest` 記的是「這筆結算是照哪一份班表算的」,而改代號會
        #   【合法地】把班表裡的 person 全部換掉 —— 識別於是對不上,求解下一個
        #   月就被永久擋下,而帳本與班表其實完全一致。更糟的是改名連【已定案】
        #   的月份都會 force 改,而定案月不能重算帳本 → 使用者無路可走。
        #   ★只重算【手上真的有那份月檔】的★(`yms` 是全部月檔);月檔已經不在
        #   的話就把識別拿掉(退回「無從查證」)—— 不可以留一個已知是錯的識別。
        for e in (ledger.get("history") or []):
            if e.get("scope") != scope or "duty_digest" not in e:
                continue
            _hym = str(e.get("month") or "")
            if _hym not in months:
                e.pop("duty_digest", None)     # 月檔不在了 → 無從查證
            elif e.get("duty_digest") == _was_fresh.get(_hym):
                e["duty_digest"] = _duty_digest(months[_hym], scope)
            else:
                # ★本來就對不上 → 絕不洗成 fresh★(外審 RS-22 P2-01)。
                # ★也不可以降級成「無從查證」★(外審 RS-23 P2-01):那一級
                #   只會出警告、不擋求解 —— 而系統在改名【之前】已經證明過
                #   這筆結算與班表不一致,改名並沒有提供任何新證據說它突然
                #   安全了。「已知是錯的」不可以變成「不知道,但繼續」。
                #   證據改成一筆 durable 的義務(閘門照樣擋、收斂端也修得掉),
                #   而不是留一顆永遠不可能相符的識別(那會沒有出口:定案月
                #   連「重算帳本」都做不到)。
                logging.warning(
                    "[roster.service] %s 的帳本結算在改名之前就與班表對不上"
                    " → 轉成待收斂的帳本義務(開程式會自動重算)", _hym)
                e.pop("duty_digest", None)
                _stale_before.append(_hym)

        # ★證據要先落地★:這些月份的帳本在改名【之前】就與班表對不上,
        #   那是與這次改名無關的事實 —— 先記下義務,改名就算整批回滾也不會
        #   把證據弄丟(義務本身是冪等的)。
        for _hym in sorted(set(_stale_before)):
            self.storage.mark_pending_settle(scope, _hym, kind="ledger")

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
        # ★寫第一個檔之前先記意圖★(外審 RS-23 P2-04):回滾只存在記憶體
        #   裡 —— 斷電/被砍/BaseException 時盤上會留下「一半舊、一半新」,
        #   而改名連【已定案的月份】都會 force 改,影響範圍很大。有這一筆,
        #   下次開程式就知道要把它做完(`reconcile_pending_renames`)。
        #   ★連交易開始前的 config revision 一起記★(外審 Codex RS-23
        #   P1-02):config 是下面第一個被寫的檔 —— 收斂端看到它沒變就能
        #   【證明】這次交易一個檔都沒動,不必靠推理。
        _mine_rename = self.storage.mark_pending_rename(
            scope, old_id, new_id,
            config_rev=self.storage.canonical_snapshot("config.json")[1],
            config_digest=_config_intent_digest(cfg))
        done = []
        try:
            for _label, new_data, old_data, save_fn, restore_fn in writes:
                save_fn(new_data)
                done.append((old_data, restore_fn))
        except Exception:
            logging.exception("[roster.service] 改名寫入失敗，回滾已寫的 %d 檔", len(done))
            _rolled = True
            for old_data, save_fn in reversed(done):
                try:
                    save_fn(old_data)
                except Exception:
                    _rolled = False
                    logging.exception("[roster.service] 改名回滾失敗，資料可能半套，"
                                      "請從 .bak 快照人工還原")
            # ★回滾成功＝盤上是完整的舊狀態 → 那筆意圖是誤報,撤掉★;
            #   回滾自己失敗就留著(半套改名正需要下次開程式做完)。
            if _rolled and _mine_rename:
                self.storage.clear_pending_rename(scope, old_id, new_id)
            raise

        # ★全部寫完了 → 交易結束,不論那筆意圖是誰記的★:續作(resume)時
        #   `mark_` 會回 False(意圖是【上一次被砍的自己】記的),用「是不是
        #   我記的」當清除條件的話,收斂永遠清不掉它 —— 下次開程式又收斂一次,
        #   義務沒有出口。跑完整整一輪就代表盤上已經是完整的新狀態。
        #   (回滾那條路仍然只清自己記的:那裡盤上是舊狀態,別人的半套改名
        #   還需要那筆意圖。)
        self.storage.clear_pending_rename(scope, old_id, new_id)
        logging.info("[roster.service] 代號連動改名 %s/%s → %s（%d 處）",
                     scope, old_id, new_id, changed)
        return changed

    def migrate_legacy_ledger_digests(self) -> list:
        """升級前的舊分錄沒有識別 → ★只認證【證明得了】的那些★。→ 認證清單。

        (外審 RS-21 P2-05 / R1-1)RS-20 之後的結算都帶識別,但升級當下每一筆
        舊分錄都沒有,於是永遠停在「無從查證」。
        ★不可以自動重算歷史★:重算會用【現在的】點數規則、國定假日與成員
        名單 —— 而那些都是可以獨立修改的設定。改過點數之後重算一個舊月份,
        算出來的差額本來就與當初不同;新加入的成員甚至會被記上一筆他還沒到
        職那個月的負債。★接著那顆新識別還會把改寫後的結果認證成 fresh★。
        (我上一版就是這樣寫的,而且註解宣稱「本來就對的話結果一模一樣」——
        那句話在點數/名單/假日變過之後是假的。)

        所以這裡改成【只量、不改】:在副本上用現在的輸入結算一次,拿它的
        分錄與帳本裡記著的那一筆逐項比對 ——
          * 完全相同 → 那筆帳本與該月的班表確實一致(在現行規則下也成立)
            → 補上識別(★只寫識別,餘額一個字都不動★),之後的閘門就守得住它;
          * 不同     → 可能是舊版換班沒重算,也可能只是規則變過 —— ★分不出來
            就不要動★:留在「無從查證」並記一筆 warning,由使用者決定要不要
            到那個月按「重算帳本」(那是一個明確的、他知道自己在做什麼的決定)。
        """
        out: list = []
        try:
            led, rev = self.storage.canonical_snapshot("ledger.json",
                                                       validate=True)
        except Exception:
            logging.exception("[roster.service] 讀帳本失敗（略過舊分錄認證）")
            return out
        targets = sorted({(str(e.get("scope") or ""), str(e.get("month") or ""))
                          for e in (led.get("history") or [])
                          if isinstance(e, dict) and "duty_digest" not in e})
        proven: dict = {}
        # ★證明與蓋章要在同一個交易裡★(外審 RS-21 R2-1):每個月份各開一次
        #   臨界區的話,證明完到蓋章之間背景同步可以把那一筆分錄換成他機的
        #   版本 —— 蓋上去的就變成「用舊證據認證新內容」。整段一個臨界區,
        #   而且蓋章前再逐項比對一次分錄(下面的 `_stamp`)。
        with self.storage.write_barrier():
          for scope, ym in targets:
            if scope not in ("r", "vs") or not ym:
                continue
            if not self.storage.month_exists(ym):
                continue           # 月檔不在了 → 沒有真相可比,保持「無從查證」
            try:
                dig = self._prove_settlement_matches(scope, ym, led)
            except Exception:
                logging.warning(
                    "[roster.service] %s %s 的舊分錄無法查證（維持「無從查證」）",
                    scope, ym, exc_info=True)
                continue
            if dig:
                old = next((e for e in (led.get("history") or [])
                            if e.get("scope") == scope
                            and e.get("month") == ym), {})
                proven[(scope, ym)] = (dig, _deltas_of(old))
                out.append((scope, ym))
            else:
                logging.warning(
                    "[roster.service] ★%s %s 的帳本分錄與該月班表對不上★ ——"
                    "可能是舊版換班後沒有重算,也可能只是點數/名單/假日改過。"
                    "分不出來就不代為改寫:請到該月按「重算帳本」再決定。",
                    scope, ym)
          if not proven:
            return out

          # ★只補識別★:餘額與分錄一個字都不動;而且蓋章前再確認一次
          #   「這一筆還是我證明過的那一筆」(★量,不要推理★:臨界區擋得住
          #   背景同步,但這一句才是真正證明得了的部分)。
          def _stamp(cur):
            for e in (cur.get("history") or []):
                key = (str(e.get("scope") or ""), str(e.get("month") or ""))
                if key not in proven or "duty_digest" in e:
                    continue
                dig, deltas = proven[key]
                if _deltas_of(e) != deltas:
                    logging.warning(
                        "[roster.service] %s %s 的分錄在查證之後又變了 → "
                        "不蓋識別", key[0], key[1])
                    continue
                e["duty_digest"] = dig
            return cur

          try:
            self.update_ledger(_stamp)
          except Exception:
            logging.exception("[roster.service] 舊分錄補識別失敗（不擋開啟）")
            return []
        logging.warning("[roster.service] 已認證升級前的舊分錄:%s",
                        "、".join(f"{sc}/{ym}" for sc, ym in out))
        return out

    def _prove_settlement_matches(self, scope: str, ym: str,
                                  led: dict) -> str:
        """帳本裡那筆結算,與這個月的班表【現在】算起來一不一樣?
        一樣 → 回它的班表識別;不一樣 → 回 ""。★呼叫端持 `write_barrier`★

        ★在副本上算,磁碟一個字都不動★(外審 RS-21 R1-1)。
        """
        month, _rev = self.storage.load_month_snapshot(ym, validate=True)
        src = self._sources(ym, SRC_RVS)
        ctx = self.build_context(scope, ym, month=month, src=src)
        points = self._points_from_duty(month, scope, ctx, ym)
        probe = copy.deepcopy(led)
        settle_month(probe, scope, ym, points)
        fresh = next((e for e in reversed(probe.get("history") or [])
                      if e.get("scope") == scope and e.get("month") == ym), {})
        old = next((e for e in (led.get("history") or [])
                    if e.get("scope") == scope and e.get("month") == ym), {})
        return (_duty_digest(month, scope)
                if _deltas_of(old) == _deltas_of(fresh) else "")

    def migrate_legacy_clerk_batch_ids(self) -> list:
        """舊版寫出來的梯次沒有 id → 補上一個【確定性】的 id。→ 補過的清單。

        (外審 RS-21 P1-02)沒有 id 的梯次是舊版正式支援的形狀,但切片格網是
        以 batch id 當鍵的 —— 兩梯都是 "" 就會互相覆蓋。補 id 之後那些資料才
        各自分得開。
        ★不可以用隨機 UUID★:兩台電腦各自跑一次遷移會產生兩個不同的 id,
        git 合併之後就變成兩梯 —— 用從內容推導出來的穩定值,兩邊算出來的
        結果一模一樣(重複跑也是冪等的)。
        ★驗證不得依賴這次遷移★:他機可能還沒升級(見
        `validate_authoritative_shape`)。

        ★改主鍵就要改外鍵★(外審 RS-22 P1-01):`biopsy_grid.json` 是
        `{batch_id: {日期: {時段: bool}}}`,而 `build_day_input` 讀的正是
        `grid[batch.id]` —— 只改 `clerk_batches.json` 的話,原本設定好的整梯
        「切片室開放」在下一次自動排班就靜靜地不見了:JSON 正常、驗證正常、
        排班跑完也沒有例外,使用者只會發現切片班沒人排。
        ★順序是「先複製、再換、最後清」★(不是 move):
          * 中途斷在複製之後 → 梯次還是舊 id,舊鍵仍在,照樣讀得到;
          * 斷在換 id 之後   → 新 id 已經有對應的格網;
          * 任何一刻都不會出現「梯次指向一個不存在的格網」。
        ★多梯共用舊鍵時複製給每一梯★:舊資料本來就是大家讀同一份,無從得知
        當初想分給誰 —— 挑一梯等於替使用者決定。
        """
        fixed: list = []
        plan: dict = {}                # 舊鍵 → [新 id, ...]
        grid_now: dict = {}            # 規劃時要看得到現有的格網(見下)

        def _plan(batches):
            fixed.clear()              # ★每一輪重試都要從頭算★(CAS 會重跑)
            plan.clear()
            taken = {str(b.get("id") or "").strip()
                     for b in batches if str(b.get("id") or "").strip()}

            def _free(cand: str, src) -> bool:
                """這個新 id 真的可以用嗎?

                ★不是只看「有沒有梯次在用」★(外審 RS-22 R1-1):切片格網那一
                邊也可能已經有這個鍵 —— 刪掉梯次時格網不會跟著刪,所以盤上
                會有孤兒格網;巧合同名的 `legacy-<日期>` 也可能存在。
                沿用一個【內容不一樣】的目的地 = 這一梯換到別人的切片開放,
                而它自己原本那份接著被清理刪掉。內容相同才可以沿用
                (那是上一次跑到一半留下的同一份,重跑要冪等)。
                """
                if cand in taken:
                    return False
                cur = grid_now.get(cand)
                return cur is None or cur == src
            for b in batches:
                if str(b.get("id") or "").strip():
                    continue
                start = str(b.get("start_monday") or "").strip()
                if not start:
                    continue           # 連起始日都沒有 → 本來就用不了,不亂補
                # ★同一個起始日可以有兩梯(repo 明文保留這個舊案例)★
                #   (外審 RS-21 R1-2):只用起始日的話兩梯會拿到同一個 id,
                #   而那份檔接著就會被唯一性驗證擋下 —— 遷移自己造出一份
                #   排不了班的資料。用內容再區分,而且要避開已經用掉的 id。
                src = grid_now.get(str(b.get("id") or ""))
                cand = f"legacy-{start}"
                if not _free(cand, src):
                    seed = json.dumps(b.get("members") or [],
                                      ensure_ascii=False, sort_keys=True)
                    cand = (f"legacy-{start}-"
                            + hashlib.sha256(seed.encode("utf-8"))
                            .hexdigest()[:8])
                if not _free(cand, src):
                    # ★分不出來就不要動★:寧可留著沒有 id(仍然排得了班),
                    #   也不要寫出一份會被唯一性驗證擋下的檔。
                    logging.warning(
                        "[roster.service] 梯次(起始日 %s)無法產生唯一 id,"
                        "維持沒有 id", start)
                    continue
                taken.add(cand)
                plan.setdefault(str(b.get("id") or ""), []).append(cand)
                fixed.append(cand)
            return batches

        try:
            with self.storage.write_barrier():
                # 1) 先算出對應(不寫任何檔)。★規劃時就要看現有的格網★:
                #    新 id 若已經有一份【別的】格網,它就不是空位。
                grid_now = self.storage.load_biopsy_grid()
                self.update_clerk_batches(_plan)
                if not fixed:
                    return []
                # 2) ★先把格網複製到新鍵★(舊鍵留著 → 中斷也讀得到)
                self.update_biopsy_grid(
                    lambda g: self._copy_grid_keys(g, plan))
                # 3) 再把梯次換成新 id
                self.update_clerk_batches(lambda bs: self._apply_ids(bs, plan))
                # 4) 最後清掉沒有任何梯次在用的舊鍵
                self.update_biopsy_grid(
                    lambda g: self._drop_unused_grid_keys(g))
        except Exception:
            logging.exception("[roster.service] 舊梯次補 id 失敗（不擋開啟）")
            return []
        logging.warning("[roster.service] 已為舊版梯次補上穩定 id:%s",
                        "、".join(fixed))
        return fixed

    @staticmethod
    def _copy_grid_keys(grid: dict, plan: dict) -> dict:
        """把舊鍵的切片格網複製到每一個新 id(舊鍵保留)。"""
        for old_key, new_ids in plan.items():
            src = grid.get(old_key)
            if not src:
                continue
            for nid in new_ids:
                cur = grid.get(nid)
                # 規劃時已經確認過:目的地要嘛是空的,要嘛就是同一份
                # (上一次跑到一半留下的)。這裡再確認一次才寫 —— 兩者之間
                # 只有本行程持著臨界區,但「證明得了」比「推理得出」可靠。
                if cur is None or cur == src:
                    grid[nid] = copy.deepcopy(src)
        return grid

    @staticmethod
    def _apply_ids(batches: list, plan: dict) -> list:
        """把計畫好的新 id 套到還沒有 id 的梯次上(順序即配對順序)。"""
        pending = {k: list(v) for k, v in plan.items()}
        for b in batches:
            key = str(b.get("id") or "")
            if str(b.get("id") or "").strip():
                continue
            queue = pending.get(key) or []
            if queue:
                b["id"] = queue.pop(0)
        return batches

    def _drop_unused_grid_keys(self, grid: dict) -> dict:
        """清掉沒有任何梯次在用的舊鍵。★沒有梯次用它才清★:遷移不完全時
        (例如撞名而留著沒有 id 的那一梯)舊鍵還要繼續給它用。"""
        live = {str(b.get("id") or "")
                for b in self.storage.load_clerk_batches()}
        for key in [k for k in grid if k not in live and not str(k).strip()]:
            grid.pop(key, None)
        return grid

    def reconcile_pending_settles(self) -> list:
        """開程式時把「沒確認完成的結算」用月檔重建到一致。→ 已收斂的清單。

        (外審排班 P2-01)`accept_solution` / `finalize` 要寫月檔與帳本兩個檔,
        中斷會留下不一致。順序刻意是「月檔先、帳本後」,所以中斷後帳本只會
        【落後】—— 用 `resettle_from_duty`(以月檔的實際排班重算)就能救回來。
        已定案的月份仍是唯讀,重算會被拒 → ★那一筆意圖留著並記 error★,
        不可以靜默清掉(清掉就等於宣稱已經一致了)。
        """
        out: list = []
        for item in self.storage.load_pending_settles_strict():
            scope = str(item.get("scope") or "")
            ym = str(item.get("ym") or "")
            kind = self.storage.pending_kind(item)
            if not scope or not ym:
                # ★看不懂就保留★(外審 RS-23 P2-02):嚴格讀取現在會更早擋下,
                #   這裡是最後一道 —— 絕不把「認不得」變成「沒有義務」。
                logging.warning(
                    "[roster.service] ★看不懂的結算意圖(%r)★ 保留,"
                    "請人工確認 pending_settle.json", item)
                continue
            try:
                # ★整段在臨界區內★(外審排班 RS-4 第 1 輪 P2):`resettle_from_duty`
                #   會「讀帳本 → 依月檔重算 → 寫回」,而開程式的當下 GitSync 正在
                #   做啟動 pull/補推 —— 中間被 merge 換掉帳本的話,寫回去的是手上
                #   那份舊的,他機剛同步進來的結算就靜默消失(而且我們接著還把意圖
                #   清掉,等於宣稱已經一致)。
                with self.storage.write_barrier():
                    if kind == "ledger":
                        # ★只欠帳本就只補帳本★(外審 RS-20 R1-3):走
                        #   `resettle_from_duty` 會連切片一起重排並改寫月檔。
                        self._settle_ledger_only_locked(scope, ym)
                    elif kind == "biopsy":
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
        for rec in self.storage.load_pending_grid_shifts_strict():
            bid = str(rec.get("batch_id") or "")
            try:
                old_start = date.fromisoformat(str(rec.get("old_start")))
                new_start = date.fromisoformat(str(rec.get("new_start")))
            except (ValueError, TypeError):
                # ★看不懂就保留,不可以清掉★(外審 RS-23 P2-02):
                #   「認不得的義務」不等於「沒有義務」——(嚴格讀取現在會在
                #   更早的地方擋下這種記錄,這裡是最後一道)。
                logging.warning(
                    "[roster.service] ★看不懂的平移意圖(%r)★ 保留,"
                    "請人工確認 pending_grid_shift.json", rec)
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
        grid_all, rev = self.storage.canonical_snapshot("biopsy_grid.json",
                                                        validate=True)
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
        # ★重算帳本是最危險的那一條★(外審 2026-08-22 P1-01):`config.json`
        #   暫時讀不到時,寬鬆載入回空 → members=[] → points 全空,而
        #   `settle_month` 會先回滾本月舊分錄再記上這份空的 —— ★正式帳本被
        #   改寫,而畫面回報成功★。權威輸入在這裡當場拒絕,磁碟原封不動。
        #   ★每次呼叫各自宣告★:定案會對 r/vs 各跑一次,前一個 scope 的重算
        #   已經寫過月檔/帳本/切片帳本,沿用上一輪的快照會拿到過期的版本。
        src = self._sources(ym, SRC_SETTLE)
        ctx = self.build_context(scope, ym, month=month, src=src)
        points = self._points_from_duty(month, scope, ctx, ym)
        # [週六切片] 重算帳本＝以最終實排為準 → 切片同步重排(含 finalize 前重算)
        #   ★用手上這一份月檔重排,不讓它自己再讀一次★(見 docstring)。
        book = book_rev = None
        _bio_failed = ""
        if scope == "r":
            try:
                _a, _n, book, book_rev = self.recompute_saturday_biopsy(
                    ym, month, src=src)
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
        _dig = _duty_digest(month, scope)
        self.update_ledger(lambda led: settle_month(led, scope, ym, points,
                                                    duty_digest=_dig))
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

    def stale_settlements(self, scope: str, ym: str, *,
                          src: "StrictSources | None" = None) -> tuple:
        """比 `ym` 早、而且帳本裡那筆結算已經對不上該月班表的月份。

        → (已證實不一致, 無從查證)

        (外審 RS-20 P1-02)帳本餘額是【下個月公平目標的基準】:
          10 月 Auto Accept → 帳本 A +5 / B -5
          使用者手動把幾天換給 B(真值應變成 +2 / -2),但沒有按「重算帳本」
          → 11 月自動排班看到的仍是 +5 / -5 → ★公平目標本身就是錯的★
        RS-7 的全輸入指紋救不了這條:它只能證明「解與當時 persisted 的帳本
        一致」,證明不了「那份帳本與 10 月的實排一致」。

        ★已證實不一致 → 擋;查不出來 → 講出來但不擋★:舊版程式記的分錄沒有
        識別(升級當下每一筆都是這樣),月檔被刪掉的月份也查不到 —— 一律擋的話,
        使用者升級後連一個月都排不了,而那是個一定會發生的狀態。
        """
        led = src.load_ledger() if src is not None else self.storage.load_ledger()
        stale, unknown = set(), set()
        for e in (led.get("history") or []):
            if e.get("scope") != scope:
                continue
            m = str(e.get("month") or "")
            if not m or m >= ym:
                continue          # 本月的舊分錄求解時本來就會被回滾(RS-9)
            dig = e.get("duty_digest")
            if not dig or not self.storage.month_exists(m):
                unknown.add(m)
                continue
            try:
                # ★嚴格讀★:壞檔在這裡回一份「空班表」的話,它與任何識別都
                #   對不上 —— 那會把「讀不到」誤報成「被改過」,而兩者的處置
                #   完全不同(一個要修檔,一個要按重算帳本)。
                if _duty_digest(self.storage.load_month_snapshot(m)[0],
                                scope) != dig:
                    stale.add(m)
            except Exception:     # 壞檔/新版 schema → 查不出來,不是「沒問題」
                logging.warning("[roster.service] %s 的月檔讀不到,無法確認帳本"
                                "是否仍與它一致", m, exc_info=True)
                unknown.add(m)
        # ★還沒結算完成的月份也不可以當結轉★(外審 RS-20 R1-1):
        #   純手動排的月份根本還沒有分錄,識別比對看不見它 —— 那筆意圖是
        #   唯一的線索(切片義務不算:它不影響點數)。
        for x in self.storage.load_pending_settles_strict():
            if str(x.get("scope") or "") != scope:
                continue
            m = str(x.get("ym") or "")
            if m and m < ym and self.storage.pending_kind(x) in ("ledger",
                                                                 "all"):
                stale.add(m)
        return sorted(stale), sorted(unknown)

    def _converge_ledger_locked(self, scope: str, ym: str, intent) -> None:
        """手動換班之後,帳本要跟著實排走(外審 RS-20 P1-02)。

        ★呼叫端必須持有 `write_barrier` 與 kind="ledger" 的意圖★
        (外審 RS-21 P2-01):意圖必須在【改月檔之前】就 durable —— 否則
        「月檔已落地、行程在收斂之前被砍」會留下一個沒有分錄也沒有意圖的
        月份:識別比對看不到它(沒有分錄可比)、收斂也不知道要補,而下個月
        照樣排得出來、結轉整個消失。這正是切片義務早就在用的交易形狀。

        ★不可以擋住這次編輯★:使用者要的是「改一格就是改一格」。收斂失敗
        (例如該月早於帳本保留期)時把意圖留著並記 log —— 開程式會收斂,
        求解【下個月】之前的閘門也會擋下來並說清楚是哪一個月。
        ★也不可以只靠使用者記得按「重算帳本」★:那是一個隱形的維護義務,
        而它保護的是下個月的公平性。

        ★只做帳本,不走完整的 `resettle_from_duty`★:那條路會連切片一起重排、
        並且清掉 (scope, 月份) 的結算意圖 —— 而呼叫端剛剛才為切片留下一筆
        義務,也可能有【別人】未完成的義務在那裡(RS-10 的教訓)。
        """
        try:
            self._settle_ledger_only_locked(scope, ym)
        except Exception as e:  # noqa: BLE001
            intent.keep(str(e) or e.__class__.__name__)
            logging.exception(
                "[roster.service] ★%s %s 手動換班後帳本未能同步重算★ ——"
                "意圖保留,求解下個月之前也會被閘門擋下", scope, ym)

    def _settle_ledger_only_locked(self, scope: str, ym: str) -> None:
        """只把帳本結算到與這一份月檔的實排一致 —— ★不碰切片、不寫月檔★。

        ★手動編輯後的收斂與開程式的補救共用同一份實作★(外審 RS-20 R1-3):
        `resettle_from_duty` 會連切片一起重排並改寫月檔,拿它來補一筆
        「只欠帳本」的義務,可能意外改派週六切片(別的月份/次數已經變了)。
        ★呼叫端必須持有 `write_barrier`★
        """
        month, _rev = self.storage.load_month_snapshot(ym)
        # ★定案月不可以自動重算★(外審 Codex RS-23 P1-03):這裡雖然只寫帳本、
        #   不碰月檔,但它是用【現在】的名單/點數規則/國定假日重算的 ——
        #   `fair_share` 的分母是目前的成員數,定案之後才到職的人會被算進那個
        #   月的公平分母(還沒到職就先背一筆負債),點數規則改過的話差額也會
        #   被改寫,而餘額是之後每個月公平目標的基準。★班表凍結不等於結算
        #   語意凍結★。
        #   ——但也不可以像以前那樣「靜默 return」:收斂端會把那筆意圖清掉,
        #   等於替一份已知對不上的帳本宣稱一致。改成拋出,意圖留著、閘門照
        #   擋、訊息說得出下一步(與週六切片那條路同一個形狀)。
        if month.get("finalized"):
            raise FinalizedMonthError(
                f"{ym} 已定案,而它的帳本結算與班表對不上。"
                f"自動重算會用【現在】的名單與點數規則改寫那個月的歷史"
                f"(定案後才到職的人也會被算進公平分母),所以不做。"
                f"請先解除該月定案,程式會在下次啟動時自動收斂,再重新定案。")
        src = self._sources(ym, SRC_RVS)
        ctx = self.build_context(scope, ym, month=month, src=src)
        points = self._points_from_duty(month, scope, ctx, ym)
        dig = _duty_digest(month, scope)
        self.update_ledger(lambda led: settle_month(led, scope, ym, points,
                                                    duty_digest=dig))

    @staticmethod
    def _points_from_duty(month: dict, scope: str, ctx, ym: str) -> dict:
        """這一份月檔的實排 → {member_id: 本月點數}。

        ★非當月鍵一律不算★:跨機人工合併/外部編輯會在月檔留下鄰月日期,
        算進去就虛增那個人的點數 —— 而它是下個月公平目標的基準。
        ★只有一份實作★:重算帳本與手動編輯後的收斂共用(兩邊各寫一次的話,
        遲早只有一邊被修好,而它們算的是同一件事)。
        """
        y, m = int(ym[:4]), int(ym[5:7])
        points = {mm.id: 0 for mm in ctx.members}
        for iso, cell in (month.get(f"{scope}_duty") or {}).items():
            p = (cell or {}).get("person")
            if p not in points:
                continue
            try:
                dt = date.fromisoformat(iso)
            except (ValueError, TypeError):
                continue
            if (dt.year, dt.month) != (y, m):
                logging.warning(
                    "[roster.service] %s 月檔含非當月鍵 %s，結算時略過", ym, iso)
                continue
            points[p] += day_point(dt, ctx.holidays, ctx.params)
        return points

    def finalize(self, ym: str, on: bool) -> list:
        """定案/解除定案 → ★定案當下的留底段落★(解除定案回 [])。

        ★快照要在【定案的同一個臨界區】裡取★(外審 RS-19 R1-2):留底 PDF 是
        背景執行緒產生的,而它可能要先下載安裝 reportlab —— 等它回過頭來組
        內容時,帳本(全域累計,別的月份/別台電腦都會動)早就不是定案當下那一
        份了。月檔本身因為已定案而唯讀,班表不會變,★但餘額會★,於是一份
        寫著「定案當下的排班快照」的文件印著之後才發生的結算。
        解除定案不需要留底 → 回空 list。

        定案時：以最終（含手動調整/換班）的 R/VS 排班重算帳本，確保帳本＝實況。

        ★整段在同一個臨界區,而且「算帳本用的月檔」與「被標定案的月檔」要是
        同一份★(外審排班 RS-6 / 第 2 輪 P1-03):兩者分開讀的話,他機在中間
        存進來的班表會變成「帳本＝舊班表、定案的是新班表」—— 而定案之後是
        唯讀的,只能靠解除定案才救得回來。臨界區擋住背景同步,標定案之前再
        用 duty 的識別回頭確認一次(★守衛不能只靠推理★)。
        """
        with self.storage.write_barrier():
            return self._finalize_locked(ym, on)

    def _finalize_locked(self, ym: str, on: bool) -> list:
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
            # ★結構錯誤在【動任何東西之前】擋下來★(RS-27,全審 P1-02):
            #   定案是單向的(重算帳本 + 月檔唯讀),而手動編輯刻意不擋 ——
            #   所以「切片室 3 個人」「照光排了 Clerk」這種規則上不可能成立的
            #   班表,唯一還來得及攔的地方就是這裡。
            #   ★驗的是 `m0` —— 正要被定案的那一份★,不是另外再讀一次
            #   (兩次讀取之間的差異會讓「驗過的」與「定案的」不是同一份)。
            _bad = self.validate_day_structure(
                ym, day_slots=(m0.get("day_slots") or {}))
            if _bad:
                _more = (
                    f"\n…(還有 {len(_bad) - _BAD_SHOWN} 筆,見 PGY/Clerk 分頁右側的警告面板)"
                    if len(_bad) > _BAD_SHOWN else "")
                raise DayStructureError(
                    "日排班有結構性錯誤,先修好才能定案(手動編輯不擋,"
                    "但定案之後月檔唯讀、帳本也已重算):\n\n"
                    + "\n".join(f"• {m}" for m in _bad[:_BAD_SHOWN])
                    + _more)
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
            # ★這裡也要嚴格★(外審 RS-21 P2-02):讀不到就當成「沒有未完成
            #   的事」的話,定案閘門會在最需要它的時候放行。
            _left = [x for x in self.storage.load_pending_settles_strict()
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
        # ★留底段落要在【寫下定案旗標之前】組好★(外審 RS-19 R2-1):
        #   組不出來(某個正典檔讀不到)就讓它在這裡上拋 —— 月檔還沒被改成
        #   唯讀,整批中止是乾淨的。反過來的話,例外會讓 UI 說「定案失敗」
        #   並把勾選還原,而磁碟上其實已經定案 —— ★假失敗比原本的 bug 更糟★
        #   (使用者會再按一次,而第二次面對的是一個已經唯讀的月份)。
        #   用手上這一份月檔,不再讀一次(它就是即將被寫下去的那一份)。
        sections = (self.build_finalize_pdf_sections(ym, month=month)
                    if on else [])
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
        return sections

    # ── 定案 PDF 留底 ───────────────────────────────────────────────────
    def build_finalize_pdf_sections(self, ym: str, *,
                                    month: "dict | None" = None) -> list:
        """組裝定案 PDF 內容:封面 + ★最終班表(由正典狀態重建)★ + 決策報告。

        (外審 2026-08-22 P1-03)舊版直接印 `report_r`/`report_vs` —— 那是
        【初次自動求解】的紀錄,Auto Accept 之後手動換過班就與事實不符,
        而 `finalize` 只重算帳本、不重新產生報告。於是留底 PDF 會出現
        「班表=舊版、帳本=新版」的自相矛盾,★而且它看起來完全正常★。
        現在最終班表與結算一律從正典狀態(月檔 duty + 設定 + 帳本)重建;
        求解報告仍保留(它說明的是「當初為什麼這樣排」),但會標明它與
        最終班表的關係(見 `report_state`)。

        ★整段在同一個臨界區、而且用權威輸入★(P1-01/P1-02):留底文件不接受
        「某個檔剛好讀不到 → 少一段 → 照樣成功」。
        """
        with self.storage.write_barrier():
            src = self._sources(ym, SRC_RVS)
            # `month=` 傳入 → ★用呼叫端手上那一份★(定案時它就是即將寫下去的
            #   那一份;另外再讀一次就可能是兩個版本 —— RS-6 的同一條道理)。
            month = src.load_month(ym) if month is None else month
            cfg = src.load_config()
            holidays = src.holidays_set()
            ledger_all = src.load_ledger()
        y, m = int(ym[:4]), int(ym[5:7])
        params = RosterParams.from_config(cfg)
        sections = [(f"{roc(y)}年{m:02d}月 排班定案留底",
                     f"月份：{ym}\n產生時間：{_now()}\n"
                     "（本檔為定案當下的排班快照，供存證留底）")]
        for scope, label in (("r", "R 排班"), ("vs", "VS 排班")):
            members = [Member.from_dict(d)
                       for d in (cfg.get(f"{scope}_members") or [])]
            duty = _month_duty(month, scope, y, m)
            if not duty and not members:
                continue                       # 這個 scope 本月完全沒東西
            sections.append((f"{label}最終班表與結算", build_final_state_report(
                year=y, month=m, scope_label=label, members=members,
                duty=duty, holidays=holidays, params=params,
                ledger=dict(ledger_all.get(scope) or {}))))
        # ★PGY/Clerk 與週六切片也是正式排班內容★(外審 RS-20 P1-03):
        #   它們原本只出現在【當初求解】的報告裡 —— 手動改過、或整個月純手動
        #   排(根本沒有報告)的話,留底文件就少了那一段,而封面寫的是
        #   「定案當下的排班快照」。一律由月檔的正典欄位重建。
        _r_names = {mm.get("id"): (mm.get("name") or mm.get("id"))
                    for mm in (cfg.get("r_members") or [])}
        sections.append(("PGY・Clerk 最終日排班", build_final_day_state_report(
            year=y, month=m, day_slots=month.get("day_slots") or {})))
        sections.append(("週六切片最終名單", build_final_biopsy_state_report(
            year=y, month=m, names=_r_names,
            saturday_biopsy=month.get("saturday_biopsy") or {})))
        for scope, label in (("r", "R 排班決策報告"), ("vs", "VS 排班決策報告"),
                             ("day", "PGY / Clerk 日排班報告")):
            rpt = month.get(report_key(scope))
            if not rpt:
                continue
            # ★這一段是「當初為什麼這樣排」的診斷紀錄,不是最終班表★
            #   (見 `report_notice`:歷史性無條件講,查得出來的原因再加一句)
            note = report_notice(month, scope)
            sections.append((label,
                             (note + "\n" * 2 + rpt) if note else rpt))
        return sections

    def report_for_display(self, scope: str, ym: str) -> str:
        """給「報告」鈕看的文字＝報告內容 + ★它與現況的關係★。

        (外審 2026-08-22 P1-03)使用者看報告是為了知道「現在這個月是怎麼排的」,
        而 Auto Accept 之後手動換過班的話,這份文字講的是舊班表 —— 不講清楚
        的話,畫面上的月曆與報告內容互相矛盾,而且看不出誰才是對的。
        顯示路徑刻意用寬鬆載入(讀不到就顯示空,不該讓視窗開不起來)。
        """
        month = self.storage.load_month(ym)
        text = month.get(report_key(scope)) or ""
        if not text:
            return ""
        note = report_notice(month, scope)
        return (note + "\n\n" + text) if note else text

    def archive_finalize_pdf(self, ym: str, sections=None) -> str:
        """把該月定案排班報告輸出成 PDF 存到 <roster>/finalized/。回傳路徑。
        reportlab 未安裝 → RuntimeError（呼叫端 UI 負責 lazy 安裝後重試）。

        `sections`＝★定案當下就取好的那一份★(見 `finalize`)。沒帶的話才
        現場重組 —— 那是給「事後補印」用的,不是定案流程該走的路。"""
        from cmuh_common.roster import export_pdf
        y, m = int(ym[:4]), int(ym[5:7])
        out_dir = os.path.join(self.storage.base_dir, "finalized")
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(out_dir, f"{roc(y)}年{m:02d}月定案.pdf")
        export_pdf.export(path, (self.build_finalize_pdf_sections(ym)
                                 if sections is None else sections))
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
        checks.extend(self._carry_in_checks(scope, ym))
        return checks

    def _carry_in_checks(self, scope: str, ym: str) -> list:
        """結轉進來的帳本可不可信 —— ★在分頁的警告面板就要看得到★
        (外審 RS-22 P2-04)。

        升級前的舊分錄沒有識別。我們刻意★不擋★:擋了的話升級之後連一個月
        都排不了,而那個狀態每一台都會經過一次。但它會影響下個月的公平目標
        —— 所以至少要講在使用者看得到的地方,而不是等到按下自動排班之後、
        在報告的最底下才出現一行。
        """
        try:
            stale, unknown = self.stale_settlements(scope, ym)
        except Exception:                      # 顯示路徑不因此打不開
            logging.debug("[roster.service] 帳本新鮮度檢查失敗（略過）",
                          exc_info=True)
            return []
        out = []
        for m in stale:
            out.append(Precheck(
                "error", "LEDGER-STALE",
                f"{m} 的帳本結算與該月班表不一致 —— 請到該月按「重算帳本」"
                f"（否則本月的公平目標會用錯的結轉計算）"))
        for m in unknown:
            out.append(Precheck(
                "warn", "LEDGER-UNKNOWN",
                f"{m} 的帳本結算沒有可比對的識別（舊版程式所記，或月檔已不在），"
                f"無法確認它是否仍與該月實際排班一致；若那個月曾手動換班，"
                f"請先到該月按「重算帳本」"))
        return out

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
