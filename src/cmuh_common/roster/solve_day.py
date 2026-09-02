# -*- coding: utf-8 -*-
"""PGY/Clerk 逐時段填充器（設計文件 §3.6；純函式、決定性）。

每時段輸入：跟診診間(房號升冪)、可用 PGY、可用 Clerk、診間容量、切片室是否開。
八步驟（各為一個可替換 FillStep，順序 = PIPELINE）：
  1 照光Step     ← 1 位 PGY（**每個時段一律要 1 位**，含週三下午；最優先；照光總次數
                  最少者，週三下午另計 photo_wed_pm 公平）
  2 治療室Step   ← 1 位 PGY（**週三下午休診不排**；其餘時段皆排；治療室總次數最少者；
                  [RS-15] 兩位 PGY 月的二早/四下/五早也不排 → 該位改優先跟診）
  3 切片室Step   ← 1 位 Clerk（僅切片室開；[RS-24] ★配額平均★：整梯的開放
                  時段數 ÷ 人數（取整數）＝每人該切幾次，誰去則由「目前跟診
                  次數最多者」決定；同日早午不連切）
  3.5 TwoPgySeat [RS-15] 兩位 PGY 月:照光/治療室之外仍空著的 PGY 先入座
                  （優先權>Clerk;非兩位 PGY 月為 no-op）
  4 ClerkSeed    每個開診診間各放 1 位 Clerk（房序=決定性洗牌、就座公平輪轉）
  5 PgyMix       逐欄補 PGY（先補到「有 1 人的診間」形成 1C+1P；無 Clerk 月直接填診）
  6 ClerkOverflow 剩 Clerk 補進剩餘容量
  [2026-07-24 使用者] 跟診房多樣性：就座輪選在「總次數/Apply偏好」之後加比
  「跟過這間診的次數」(少者先)與「上次就是這間」懲罰(反連排)；診間處理順序改
  決定性抖動洗牌(原固定房號升冪→人少於房時永遠只填低房號,學生從跟不到 103/105)
  ——被填的房與 1C+1P 配對組合逐日變化,一起跟診的人自然錯開。
  7 RestStep     還沒位子 → 放假（★純殘量,不做平均;理由見下★）

★[RS-24 使用者 2026-08-24] 切片室＝整梯配額平均;誰去則由跟診次數決定★
  同學反應:切片室次數一樣,跟診次數卻有人多有人少。
  ★根因★:舊版切片室的輪選【只看本梯切片次數】,而「那一個時段的座位夠不夠
  坐所有人」是浮動的 —— 座位多於人時,被抽去切片的人淨損失一次跟診;座位少於
  人時他本來就會放假(淨損失 0)。所以「切片次數一樣」完全不保證「跟診次數
  一樣」,差別在你是在寬鬆的時段還是擠的時段被抽走,而那筆帳沒有人記。
  (不是跨月份:RF-09 早就把同一梯次上個月的既存班表回放進公平計數,
   `replay_counters` 連跟診座位都回放。)
  使用者定案的規則:
    1. ★配額★:先算這一梯的切片開放時段數,除以梯次人數取整數 = 每人該切
       幾次(例:16 個時段 5 個人 → 每人 3 次)。多出來的時段留空 ——
       ★寧可空著也不讓某個人比別人多切★。
       (開放時段數少於人數時配額會是 0 → 改取 1,讓排得到的人先輪到,
        輪不到的由月底警告點名 —— 設計文件 C4 本來就是這個語意。)
    2. ★誰去★:配額還沒用完的人裡面,挑【目前跟診次數最多】的那一位。
       這就是把「被抽去切片」的成本記到帳上:誰跟診多,誰去切片,
       兩邊的次數一起被拉平。
    3. 其餘的人照舊進跟診座位(就座輪選的公平鍵仍是跟診次數),
       都排不進去的才放假(★放假純殘量,不做平均;理由見下★)。
  ★請假會讓配額排不完★:所以「還剩幾個時段、每個人還缺幾次、他哪幾天在」
  要一起看 —— 判準是【放掉這一格之後,剩下的配額與剩下的時段還配得起來嗎】
  (最大匹配;見 `_biopsy_forced_today`)。單看「剩餘時段數 ≥ 待補次數」或
  「他自己還有沒有機會」都會漏掉兩個人共用同一個瓶頸時段的情況。

★放假次數【不】做平均，而且做不到★（2026-08-03 使用者實測提問後更正）
  本行原本寫「放假次數輪平均」—— 那是【錯的】：`fc.rest` / `fc.last_rest`
  從頭到尾只被 RestStep 寫入，三支選人 key（照光、治療室、就座）都沒有讀它。
  更重要的是，就算補上也達不到目的，因為：

      放假 = 可排時段 − 工作時段

  每個時段的工作名額是固定的（照光 1、治療室 1、診間 = 房數 × 容量），
  而照光/治療室/跟診三者各自都已按次數平均。於是【工作次數平均】與
  【放假次數平均】在「每人可排時段不同」時是互斥的：工作攤平之後，
  放假必然跟著可排時段走 —— 在場越久的人，剩下沒位子的時段就越多。

  實例（2026-08，21 個平日 = 42 個半天）：
      A 照光11+治療9+跟診16+放假6 = 42 ← 全月零請假，滿額在場
      B 11+10+15+4 = 40    C 10+10+14+4 = 38    D 10+9+13+4 = 36
  A 的放假比別人多 2，正是因為他比 D 多在 6 個半天，而多出來的名額有限。
  要讓放假數字齊頭，就得把工作從 A 挪給請假多的人 —— 那會破壞照光/治療的
  平均（使用者要的正是那一項）。★所以這是刻意不做，不是漏做。★

先照光、再治療室，兩者各消耗 1 位 PGY，剩餘 PGY 才與 Clerk 一起進診間。
決定性鐵律：一切輪選用 key=(次數, 決定性抖動, 代號) 取最小；抖動＝crc32(日期|時段|
用途|代號)——同輸入恆同結果（可重跑重現），但逐日/逐時段變化 → 平手時打散，不會
鎖死「同人固定同時段」的節拍（見 _jitter）。
不硬塞：照光/治療室無 PGY → 記警告，不填（貪婪填充器無法硬性保證滿足，缺人時
以警告呈現）；切片室當日全體已切/全請假 → 靜默留空（同日不重複是規則，非異常）
——整梯輪不到或次數不均由月底警告點名。
"""
from __future__ import annotations

import zlib
from calendar import monthrange
from dataclasses import dataclass, field
from datetime import date, timedelta

from cmuh_common.roster.model import (
    CLERK_COURSE_DAYS, STUDENT_SESSIONS, dedupe_codes, is_weekend,
)

PHOTO = "照光"        # 每時段必排 1 PGY（含週三下午），最優先
TREATMENT = "治療室"  # 每時段 1 PGY，但週三下午休診不排
BIOPSY = "切片室"
REST = "放假"


def is_follow_slot(slot) -> bool:
    """這一格算不算「跟診」——★特別格以外的一律是★（房號可為任意字串）。

    ★只留一份定義★：週期統計（`person_course_stats`）與 service 的整梯跟診
    次數（RS-33）都走它。兩邊各寫一次 `not in (...)` 的話，日後新增一種特別格
    （例如把「會議」獨立出來）只會改到其中一邊，而那種漂移沒有任何東西會叫。
    """
    return slot not in (PHOTO, TREATMENT, BIOPSY, REST)

#: [RS-29] 相鄰月份既定時段的時序位置 —— 比本月任何一格都晚。
#:   本月的位置是 `列序*10 + 時段序`,一個月最多 31*10+1;取一個遠大於它的
#:   常數即可,不必也不該去算「本月有幾天」(那會隨月份長度變動)。
_FUTURE_POS = 10 ** 9
WED = 2

# [2026-07-23 使用者] 「Apply 本科」PGY 優先偏好：勾選的 PGY（至多 2 位）在
# 週二/週五（weekday 1/4）早午的 101 診跟診「優先安排」。公平最優先——偏好只做
# 座位輪選的【平手決勝】（排在座位次數之後、抖動之前），次數落後者永遠先補，
# 整月跟診次數 spread ≤1 性質不變；請假者本來就不在候選內。
APPLY_PREF_ROOM = "101"
APPLY_PREF_WEEKDAYS = (1, 4)          # 週二、週五（早午時段皆適用）

# [2026-08-21 使用者·RS-15] 該月恰有【兩位】PGY 時的特別規則:照光/治療室
# 每時段各吃 1 位,兩人整月互卡、完全跟不到診(像打雜的)。故:照光仍然
# 每時段必排(最優先、性質不變);但下列時段【治療室不排】,被釋出的那位
# PGY 優先入座跟診,而且優先權>Clerk(TwoPgySeatStep,座位不足時 Clerk
# 讓位)。★僅恰為 2 位時啟用★:1 位月釋出治療室也沒有第二人可跟診,
# 3 位以上月本來就輪得開 —— 判準是【該月 PGY 名單】,不是當日可用人數
# (請假造成的臨時 2 人不算)。
TWO_PGY_PHOTO_ONLY = ((1, "上午"), (3, "下午"), (4, "上午"))  # 二早/四下/五早

# ── [RS-33 2026-09-02 使用者] Clerk 整梯跟診時段的目標區間 ──────────────────
# 使用者原話:「兩週 Course 內總計週一至週五早上下午共 10 個時段、2 週共 20 個,
#   其中 2 個時段是開會(週三下午),切片室每人只需要 1-3 個時段,因此讓跟診時段
#   控制在每個人總共 7-11 個左右(盡量滿足,若無法也沒關係,例如遇到假日/連假可能
#   無法滿足;先滿足其他 Clerk 排班規定之後,讓控制跟診時段的條件順位在後方一點)」
#
# ★兩端的性質完全不同,實作方式也不同★
#   上限(11):★可執行★ —— 座位可以【留空】。到達上限的 Clerk 不再進入候選,
#     那一格就空著(他去放假)。這是這條規則唯一會改變排班結果的地方。
#   下限(7):★不可執行★ —— 座位本來就是能填就填(`_seat` 是 min-first,
#     次數少的人永遠先坐)。填不到 7 只可能是「整梯根本沒有那麼多可坐的時段」
#     (假日/連假/診間少/請假),那不是排班演算法能生出來的東西。
#     所以下限做成★月底點名★:講出「誰只跟到幾次」,不假裝排得出來。
#
# ★為什麼上限用「留空」而不是排序鍵★:`_seat` 的第一鍵已經是跟診次數
#   (RS-25,min-first),所以把「超過上限」放進排序鍵永遠不會生效 ——
#   次數最多的人本來就排在最後,只有在【其他人都一樣多】時才會被選到,
#   而那正是要擋下來的情況。要讓總量停在 11,唯一的辦法是那一格不坐人。
#
# ★「順位在後方」怎麼落實★:這個上限★只影響就座★,而且是在照光/治療室/
#   切片室(含配額與期限)全部排完之後才輪到的步驟。換句話說,其他 Clerk 規則
#   要用到的人力一個都不會被它擋掉;它只決定「剩下的座位要不要再塞人」。
CLERK_SEAT_TARGET_MIN = 7
CLERK_SEAT_TARGET_MAX = 11


def _jitter(d: date, session: str, purpose: str, code: str) -> int:
    """[2026-07-23 使用者] 決定性抖動，取代舊的「最久沒輪到(LRU)」平手決勝。

    LRU 在「每天早/午兩時段」的固定節拍下會形成穩定輪轉週期 → 鎖死成固定配對
    （實測：2 位 PGY 時 A 永遠早上照光、B 永遠下午）。改用 crc32(日期|時段|用途|代號)：
    主鍵仍是「次數」→ 整月每人次數照樣平均（spread ≤1 性質不變）；平手時逐日/逐時段
    亂序打散，誰排早誰排午不再固定。非真亂數（不用 random/時鐘），同輸入恆同結果，
    決定性鐵律不破（重排/回放/測試皆可重現）。"""
    return zlib.crc32(f"{d.isoformat()}|{session}|{purpose}|{code}"
                      .encode("utf-8"))


def _idle_today(fc: "FairCounters", k, d: date) -> int:
    """[2026-07-27 使用者] 0＝今天還沒有任何工作（優先給事做）、1＝今天已有工作。

    早上時段人人皆 0（本日尚未開排）→ 此項不生效，等同舊行為；下午才真正分道，
    讓「早上放假的人」優先補上位子，避免整天放假。純函式。"""
    return 0 if fc.worked_day.get(k) != d else 1


def _follow_week_key(d: date, code) -> tuple:
    """[RS-31] 週別跟診計數的鍵:(ISO年, ISO週, 代號)。二早/四下/五早都在
    週一～週五內,ISO 週(一～日)剛好把「同一週」框住。跨月交界週與 PGY 其他
    公平計數同一個邊界 —— 只看本月(RF-09 刻意不回放上月 PGY)。"""
    wk = d.isocalendar()
    return (wk[0], wk[1], code)


def _pick(ctx: "SessionCtx", cands: list, count_map: dict, purpose: str):
    """公平輪選：次數最少 → 決定性抖動 → 代號字典序（決定性；見 _jitter）。"""
    return min(cands, key=lambda p: (count_map.get(p, 0),
                                     _jitter(ctx.d, ctx.session, purpose, p), p))


@dataclass
class FairCounters:
    photo_total: dict = field(default_factory=dict)  # PGY 照光總次數
    photo_wed_pm: dict = field(default_factory=dict)  # PGY 週三下午照光次數
    tx_total: dict = field(default_factory=dict)     # PGY 治療室總次數
    rest: dict = field(default_factory=dict)         # 放假次數（PGY+Clerk）
    biopsy_done: dict = field(default_factory=dict)  # Clerk 本梯切片次數
    seat: dict = field(default_factory=dict)         # 診間就座次數（公平輪轉）
    # [2026-07-24 使用者] 跟診房多樣性：每人×每房次數（盡量輪過各診,不固定跟同房）
    seat_room: dict = field(default_factory=dict)    # {(ck, 房): 次數}
    last_seat_room: dict = field(default_factory=dict)  # {ck: 房} 上次跟的房(反連排)
    # [2026-07-25 使用者] 同伴多樣性：兩人同一診間共事次數（避免「1、2 號永遠一起
    # 跟 101」——房多樣性只管「誰跟哪一間」,管不到「誰跟誰」）。鍵=兩人 ck 排序後的
    # tuple,故與人員命名空間一致（Clerk 跨梯不互相繼承）。
    pair: dict = field(default_factory=dict)         # {(ck_a, ck_b): 共事次數}
    # [2026-07-27 使用者] 反「整天放假」：記錄每人最近一次【有工作】的日期
    # （照光/治療室/切片室/跟診皆算）。下午輪選時「今天還沒有任何工作」者優先，
    # 讓每人每天盡量至少有半天有事做,而不是早上放假下午又放假。
    worked_day: dict = field(default_factory=dict)   # {ck: 最近有工作的日期}
    # [RS-31 2026-08-27 使用者] 兩位 PGY 月:二早/四下/五早跟診的【週別】計數
    # {(ISO年, ISO週, 代號): 次數}。PGY 反映這三個時段會「這週全是 A、下週全是
    # B」—— 照光挑人只看 photo_total,週內落點是奇偶性的副作用。仿週三下午
    # photo_wed_pm 的既有模式:這些時段的照光輪選先看「這週誰跟診多」,多者去
    # 照光、跟診輪給另一位 → 同一週內兩人都跟得到診。★只在兩位 PGY 月讀取★;
    # 回放(鎖定時段)不分模式一律記錄,非兩位 PGY 月寫了也沒有人讀。
    two_pgy_follow_week: dict = field(default_factory=dict)
    # last_*：最近一次輪到日期。[2026-07-23] 輪選 key 已改用 _jitter 平手決勝（LRU 會
    # 鎖死固定配對），這些欄位保留作紀錄/回放資料，不再參與輪選。
    last_photo: dict = field(default_factory=dict)
    last_tx: dict = field(default_factory=dict)
    last_rest: dict = field(default_factory=dict)
    last_biopsy: dict = field(default_factory=dict)
    last_seat: dict = field(default_factory=dict)


@dataclass
class SessionCtx:
    d: date
    session: str
    rooms: list                       # 跟診房（升冪）
    pgy: list                         # 可用 PGY（步驟會消耗）
    clerk: list                       # 可用 Clerk
    biopsy_open: bool
    capacity: int
    fc: FairCounters
    room_slots: dict = field(default_factory=dict)
    # [codex R1] 房內每位已就座者的【真實 ck】(角色感知)。同伴計數不可用「本次候選池
    # 的 ck resolver」去推斷房內既有者——PgyMixStep 進場時房裡坐的是 Clerk,用 _pgy_ck
    # 推會標成 ("pgy", 代號),與 replay_counters 的 ("clerk", 梯次, 代號) 永遠對不上
    # (代號跨梯重用時還會誤繼承別梯的配對史)。
    seat_ck: dict = field(default_factory=dict)
    batch_key: str = ""               # 切片輪替以「梯次」為單位（代號跨梯會重用）
    apply_pref: frozenset = frozenset()   # Apply 本科 PGY（101 診週二/週五平手優先）
    two_pgy_mode: bool = False        # [RS-15] 該月 PGY 名單恰 2 位
    # [RS-24] 切片室配額:{代號: 還缺幾次}。★None = 不設上限★(直接呼叫
    #   `solve_session` 的呼叫端算不出配額 —— 它要看整梯的開放時段數與名單)。
    biopsy_quota_left: "dict | None" = None
    # 今天不補就補不完的人(請假造成的瓶頸;見 `_biopsy_forced_today`)。
    biopsy_force: frozenset = frozenset()
    # [RS-33] {代號: 這一格【之後】還有幾次鎖定的跟診}。跟診上限要連它一起算
    #   —— 那幾次已經指派給他了,只是主迴圈還沒走到。★None = 沒有預留★
    #   (直接呼叫 `solve_session` 的呼叫端看不到整月的鎖定表)。
    seat_reserved: "dict | None" = None
    # [RS-34] {代號: 這一梯每人該跟幾次}。★None = 只受 7-11 的上限管★
    #   (第一趟求解、以及尚未走完的梯次都是 None)。
    seat_cap: "dict | None" = None


    @property
    def wed_pm(self) -> bool:
        return self.d.weekday() == WED and self.session == "下午"

    @property
    def two_pgy_photo_only(self) -> bool:
        """[RS-15] 兩位 PGY 月的「只排照光」時段(二早/四下/五早)。"""
        return (self.two_pgy_mode
                and (self.d.weekday(), self.session) in TWO_PGY_PHOTO_ONLY)

    def room_pref(self, room) -> frozenset:
        """該房此時段的「平手優先」集合：僅 101 診且週二/週五時＝apply_pref，其餘空。"""
        if (str(room).strip() == APPLY_PREF_ROOM
                and self.d.weekday() in APPLY_PREF_WEEKDAYS):
            return self.apply_pref
        return frozenset()


class FillStep:
    def run(self, ctx: SessionCtx, slots: dict, log: list) -> None:  # noqa: ARG002
        raise NotImplementedError


class PhotoStep(FillStep):
    """照光：每個時段（含週三下午）一律排 1 位 PGY，最優先。"""

    def run(self, ctx, slots, log):
        if not ctx.pgy:
            log.append(f"⚠ {ctx.session} 照光無 PGY 可排（全請假？）")
            return
        fc = ctx.fc
        if ctx.wed_pm:                          # 週三下午：先比 photo_wed_pm 再比總次數
            pick = min(ctx.pgy, key=lambda p: (
                fc.photo_wed_pm.get(p, 0), fc.photo_total.get(p, 0),
                _jitter(ctx.d, ctx.session, "photo", p), p))
        elif ctx.two_pgy_photo_only:
            # [RS-31 2026-08-27 使用者] 二早/四下/五早的跟診要【同一週內】輪替
            # (不能這週全是 A、下週全是 B):這週已跟診多的先去照光,跟診讓給
            # 另一位。photo_total 降為第二鍵 —— 與週三下午 photo_wed_pm 同一個
            # 模式;偏差由其後時段的 min-first 自行收斂(兩人月每時段照光必有
            # 一人,差距至多暫時 +1 就被拉回)。
            pick = min(ctx.pgy, key=lambda p: (
                -fc.two_pgy_follow_week.get(_follow_week_key(ctx.d, p), 0),
                fc.photo_total.get(p, 0),
                _jitter(ctx.d, ctx.session, "photo", p), p))
        else:
            pick = _pick(ctx, ctx.pgy, fc.photo_total, "photo")
        ctx.pgy.remove(pick)
        slots[PHOTO] = [pick]
        fc.photo_total[pick] = fc.photo_total.get(pick, 0) + 1
        if ctx.wed_pm:
            fc.photo_wed_pm[pick] = fc.photo_wed_pm.get(pick, 0) + 1
        fc.last_photo[pick] = ctx.d
        fc.worked_day[_pgy_ck(ctx, pick)] = ctx.d      # 反整天放假：今日已有工作
        log.append(f"{ctx.session} 照光 ← PGY {pick}"
                   + ("（週三下午）" if ctx.wed_pm else ""))


class TreatmentStep(FillStep):
    """治療室：除週三下午（休診）外，每個時段排 1 位 PGY（照光之後、進診間之前）。"""

    def run(self, ctx, slots, log):
        if ctx.wed_pm:                          # 週三下午治療室休診（照光另開）
            return
        if ctx.two_pgy_photo_only:              # [RS-15] 兩位 PGY 月:只排照光
            log.append(f"{ctx.session} 治療室不排（兩位 PGY 月，"
                       f"另一位優先跟診）")
            return
        if not ctx.pgy:
            log.append(f"⚠ {ctx.session} 治療室無 PGY 可排（全請假？）")
            return
        fc = ctx.fc
        pick = _pick(ctx, ctx.pgy, fc.tx_total, "tx")
        ctx.pgy.remove(pick)
        slots[TREATMENT] = [pick]
        fc.tx_total[pick] = fc.tx_total.get(pick, 0) + 1
        fc.last_tx[pick] = ctx.d
        fc.worked_day[_pgy_ck(ctx, pick)] = ctx.d      # 反整天放假：今日已有工作
        log.append(f"{ctx.session} 治療室 ← PGY {pick}")


def _max_pairs(people: list, slots: list, free) -> int:
    """人 × 時段的最大匹配數(每人至多一個時段、每個時段至多一個人)。

    ★用途★:切片室的「每人兩週至少一次」要判斷的是
    【現在放掉這一格,剩下的人與剩下的時段還配得起來嗎】——
    「剩餘時段數 ≥ 待補人數」與「他自己還有沒有機會」都只是這件事的
    必要條件,兩者都成立仍可能漏人(兩個人共用同一個瓶頸時段)。
    規模很小(十幾人 × 幾十個時段),簡單的增廣路徑就夠,而且是決定性的。
    `free(person, slot)` = 那個人那天在不在。
    """
    match: dict = {}                    # 時段索引 → 已配到的人

    def _same_day_taken(p, s) -> bool:
        """★同一天只能切一次★:正式排班用 `last_biopsy` 擋(見 `_biopsy_cands`)
        —— 判斷「還配不配得完」時若允許同一人吃掉同一天的早+午,就會高估可行性
        而放掉今天該補的人(外審 Codex RS-24 配額版 P2)。
        (`slots` 是日期字串,同一天的早/午是【同一個標籤】出現兩次。)"""
        return any(q == p and slots[j] == s for j, q in match.items())

    def _aug(p, seen: set) -> bool:
        for i, s in enumerate(slots):
            if i in seen or not free(p, s) or _same_day_taken(p, s):
                continue
            seen.add(i)
            if i not in match or _aug(match[i], seen):
                match[i] = p
                return True
        return False

    return sum(1 for p in people if _aug(p, set()))


def _biopsy_forced_today(todo: list, later: list, today_ok: list,
                         free) -> frozenset:
    """→ ★今天非補不可的人★(空集合 = 今天怎麼挑都還補得完)。

    `todo`   ★還缺的次數★——每個人重複出現「他還缺幾次」次(配額制);
    `later`  這一格【之後】還解得到的切片時段;
    `today_ok` 今天在、今天排得到的人;`free(人, 時段)` 那天在不在。

    判準:放掉這一格之後配不齊(`最大匹配 < 還缺的總次數`)→ 今天就得補。
    要補誰:挑「補了他一次之後其餘的仍配得齊」的候選 —— 否則只是把補不到的
    人換一個而已。全都配不齊(本來就排不完)→ 今天在的都算候選,至少補一個,
    剩下的由月底「切片室輪不到/次數不均」點名。
    """
    if not todo or not today_ok:
        return frozenset()
    if _max_pairs(list(todo), list(later), free) >= len(todo):
        return frozenset()              # 放掉這一格也還補得完
    good = []
    for c in today_ok:
        rest = list(todo)
        if c in rest:
            rest.remove(c)              # ★只拿掉一次★(配額可能不只一次)
        if _max_pairs(rest, list(later), free) >= len(todo) - 1:
            good.append(c)
    return frozenset(good or today_ok)


def _biopsy_cands(ctx) -> list:
    """今天還能進切片室的 Clerk(★同日早+午不得同一人★)。"""
    return [c for c in ctx.clerk
            if ctx.fc.last_biopsy.get((ctx.batch_key, c)) != ctx.d]


def _take_biopsy(ctx, slots, log, pick, why: str) -> None:
    fc = ctx.fc
    ctx.clerk.remove(pick)
    slots[BIOPSY] = [pick]
    fc.biopsy_done[(ctx.batch_key, pick)] = (
        fc.biopsy_done.get((ctx.batch_key, pick), 0) + 1)
    fc.last_biopsy[(ctx.batch_key, pick)] = ctx.d
    fc.worked_day[_clerk_ck(ctx, pick)] = ctx.d    # 反整天放假:今日已有工作
    log.append(f"{ctx.session} 切片室 ← Clerk {pick}（{why}）")


class BiopsyStep(FillStep):
    """[RS-24] 切片室:★配額平均、由跟診次數最多者去★。

    * 配額(`biopsy_quota_left`)由整月填充算:整梯的開放時段數 ÷ 人數(取整),
      所以每個人切一樣多次;多出來的時段留空,★寧可空著也不讓誰多切★。
    * 選人:配額還沒用完的人裡面挑【目前跟診次數最多】的 —— 那正是同學抱怨的
      那筆帳:誰跟診多,誰去切片,兩邊一起拉平。舊版挑的是「切片次數最少者」,
      它管得住切片、管不住跟診。
    * 請假造成的瓶頸由 `biopsy_force` 覆蓋(見 `_biopsy_forced_today`):
      「今天不補他就補不完了」的人優先,不然配額會排不完。
    * 直接呼叫 `solve_session` 而沒帶配額的呼叫端 → ★不設上限★
      (維持「切片室開著就有人」的舊行為,測試與工具腳本才不必知道配額)。
    """
    def run(self, ctx, slots, log):
        # 週三下午切片室硬性關閉（C3 定案）→ 即使手動格網誤設為開，也不排。
        if not ctx.biopsy_open or ctx.wed_pm:
            return
        fc = ctx.fc
        quota = ctx.biopsy_quota_left
        cands = [c for c in _biopsy_cands(ctx)
                 if quota is None or quota.get(c, 0) > 0]
        if not cands:
            return
        # ★瓶頸優先★:今天不補他就補不完(請假把他的機會吃掉了)。
        forced = [c for c in cands if c in ctx.biopsy_force]
        if forced:
            cands = forced
        pick = min(cands, key=lambda c: (
            -fc.seat.get(_clerk_ck(ctx, c), 0),      # ★跟診最多的人先去切片★
            -(quota.get(c, 0) if quota else 0),      # 還缺得多的先補
            _idle_today(fc, _clerk_ck(ctx, c), ctx.d),
            _jitter(ctx.d, ctx.session, "biopsy", c), c))
        _take_biopsy(ctx, slots, log, pick,
                     "今天不排就補不完" if forced else "配額輪替")


def _pgy_ck(ctx, p):
    return ("pgy", p)                    # PGY 代號整月穩定 → 全月共用


def _clerk_ck(ctx, c):
    return ("clerk", ctx.batch_key, c)   # Clerk 代號跨梯會重用 → 依梯次命名空間


def _pair_key(a, b) -> tuple:
    """同伴計數的對稱鍵（兩人 ck 排序）——(A,B) 與 (B,A) 視為同一組。"""
    sa, sb = str(a), str(b)
    return (sa, sb) if sa <= sb else (sb, sa)


def _clerk_seat_eligible(ctx, pool):
    """還沒到跟診上限的 Clerk(RS-33)。全部到頂 → 回空清單 = 這一格留空。

    ★只用在 Clerk 的就座步驟★:PGY 不受此限(他們的公平是月度、而且照光/
    治療室本來就吃掉大半時段)。判準讀的是 `fc.seat`,那份計數以
    `("clerk", 梯次, 代號)` 為鍵、且跨月回放(見 `_clerk_ck` 與
    `replay_counters`)—— 也就是★整梯的累計★,正是使用者要控制的那個數。

    ★已完成 + 尚未走到的鎖定★(外審 RS-33 R1 P2):較晚日期鎖定的跟診要走到
    那天才由 `replay_counters` 入帳 —— 只看 `fc.seat` 的話,較早的日子會把他
    排到上限,鎖定那一次再加上去就是 12 次。`ctx.seat_reserved` 是那些「已經
    指派給他、只是還沒跑到」的次數(含相鄰月份的既定時段)。
    """
    res = ctx.seat_reserved or {}
    caps = ctx.seat_cap or {}

    def _level(c):
        return ctx.fc.seat.get(_clerk_ck(ctx, c), 0) + res.get(c, 0)

    # ★試過但撤掉的作法:進度鎖★(RS-34)。少掉的那幾個座位總得有人放假,
    #   而貪婪求解會把它們全部丟在最後幾格 —— 最早跟滿的人於是在最後一天
    #   整天沒事做。想用「誰都不可以跑在最少的人前面」把留空分散開,
    #   ★實測代價太大★:那條規則在「這一節坐得下的人多於還在最低階的人」
    #   時就整個位子空掉,1 間診 3 個人從 10/10/10 掉到 ★8/8/8(浪費 10 個
    #   座位)★,而且整天放假照樣發生。留這段註解是為了下次不要再走一遍。
    return [c for c in pool
            if _level(c) < min(CLERK_SEAT_TARGET_MAX,
                               caps.get(c, CLERK_SEAT_TARGET_MAX))]


def _seat(ctx, pool, room, ck, prefer: frozenset = frozenset(),
          eligible=None):
    """依 ck(人)命名空間的座位公平計數輪選並就座。

    key＝(★座位次數★, 今日尚無工作, 非偏好者, 該房次數, 連排懲罰, 同伴次數,
    抖動, 代號)：
    ★[RS-25 2026-08-24 使用者] 跟診次數是【第一】鍵★ ——「跟診次數不能有人兩週
    跟了 7 次、有人跟了 10 次,也要盡量每人差 ≤1」。這條決定★推翻了 2026-07-27
    「反整天放假放最前面」在兩者衝突時的優先權★:那一版會讓【跟診次數偏高但今天
    還沒事做】的人壓過【次數較少但早上有工作】的人 —— 那正是 7 vs 10 的來源
    (當時那條測試自己的例子就是 9 次 vs 3 次)。
    ★這一鍵刻意【只看跟診】★:切片室的次數由配額保證一樣多(見 `BiopsyStep`),
    所以「跟診也一樣多」就等於「總量也一樣多」—— 把總量混進來反而會讓剛切完片
    的人被擠出座位,越補越不平。
    「反整天放假」(2026-07-27)降為★平手時的決勝★:次數一樣時仍優先給今天還沒
    事做的人,所以絕大多數時段行為不變。★實測代價★(兩週、切片室全開):
    5 人 2/3 房、4 人 1 房 ×1、5 人早 3 房午 1 房 → 跟診次數與整天放假人次
    完全相同;只有「3 人搶 1 個座位」那種極擠的情境多 1 人次整天放假,
    換到的是跟診全距從 2 收斂到 1。
    (早上時段人人皆「今日尚無工作」→ 那一鍵在上午本來就不生效。)
    平手時 prefer 先上（Apply 本科 101 週二/五）；
    [2026-07-24] 再比「跟過這間診的次數」少者先、罰「上一次跟診就是這間」（反連排）；
    [2026-07-25 使用者] 再比「與本房已就座者共事過幾次」少者先——房多樣性只管
    「誰跟哪一間」,管不到「誰跟誰」,故仍可能固定同兩人成對（如 1、2 號總是一起）。
    最後決定性抖動打散。

    `eligible`:限定只能從這些人裡挑(RS-33 的跟診上限用它把「已到頂」的人
    排除)。★一個都不剩就回 None 且【不動 pool】★ —— 呼叫端據此讓那一格
    留空;回傳值必須被檢查,否則 `while` 迴圈會空轉。"""
    rk = str(room).strip()
    fc = ctx.fc
    seated = ctx.seat_ck.setdefault(room, [])   # [codex R1] 房內既有者的真實 ck

    def _key(p):
        k = ck(ctx, p)
        pair_cost = sum(fc.pair.get(_pair_key(k, q), 0) for q in seated)
        return (fc.seat.get(k, 0),          # ★跟診次數最少者永遠先★(RS-25)
                _idle_today(fc, k, ctx.d),  # 平手時給今天還沒事做的人(反整天放假)
                0 if p in prefer else 1,
                fc.seat_room.get((k, rk), 0),
                1 if fc.last_seat_room.get(k) == rk else 0,
                pair_cost,
                _jitter(ctx.d, ctx.session, "seat", p), p)
    candidates = pool if eligible is None else [p for p in pool if p in eligible]
    if not candidates:
        return None                        # RS-33:沒有人還能坐 → 這一格留空
    pick = min(candidates, key=_key)
    pool.remove(pick)
    k = ck(ctx, pick)
    for q in seated:                       # 與本房已就座者各記一次共事
        pk = _pair_key(k, q)
        fc.pair[pk] = fc.pair.get(pk, 0) + 1
    ctx.room_slots[room].append(pick)
    seated.append(k)                       # 保存真實 ck 供後續同房者比對
    fc.seat[k] = fc.seat.get(k, 0) + 1
    fc.seat_room[(k, rk)] = fc.seat_room.get((k, rk), 0) + 1
    fc.last_seat_room[k] = rk
    fc.last_seat[k] = ctx.d
    fc.worked_day[k] = ctx.d               # 反整天放假：今日已有工作
    return pick


def _room_order(ctx, pref_first: bool = False) -> list:
    """[2026-07-24 使用者] 診間處理順序＝決定性抖動洗牌（逐日/逐時段變化）。

    原固定房號升冪：學生少於診間數時永遠只填低房號（都跟 101/102,從輪不到
    103/105）,且 1C+1P 配對房固定 → 改洗牌後被填的房與配對組合天天不同。
    pref_first：Apply 本科生效日（週二/五且有勾選者）把 101 提到最前,
    偏好者的平手決勝不會先被洗到前面的別房消耗掉。"""
    rooms = sorted(ctx.rooms, key=lambda r: (
        _jitter(ctx.d, ctx.session, "roomorder", str(r)), str(r)))
    if (pref_first and ctx.apply_pref
            and ctx.d.weekday() in APPLY_PREF_WEEKDAYS):
        rooms.sort(key=lambda r: 0 if str(r).strip() == APPLY_PREF_ROOM else 1)
    return rooms


class TwoPgySeatStep(FillStep):
    """[RS-15] 兩位 PGY 月:照光/治療室之外仍空著的 PGY 先入座(優先權>Clerk)。

    必須放在 ClerkSeedStep ★之前★才成立:座位=房數×容量,容量 1 或房少時
    Clerk 先坐滿,PgyMixStep 就沒有位子可補 —— 使用者定案 PGY 跟診優先權
    >Clerk。非兩位 PGY 月完全不動(既有 1C+1P 混搭順序照舊);兩位 PGY 月的
    其他時段照光+治療室已把兩人占滿 → 此步自然無事可做,實際只在「只排照光」
    時段(二早/四下/五早)與治療室本就休診的時段釋出人力時生效。
    座位輪選走同一個 `_seat`(公平/房多樣性/同伴多樣性/Apply 偏好全沿用)。
    """

    def run(self, ctx, slots, log):
        if not ctx.two_pgy_mode:
            return
        for r in _room_order(ctx, pref_first=True):
            if not ctx.pgy:
                return
            if len(ctx.room_slots[r]) < ctx.capacity:
                pick = _seat(ctx, ctx.pgy, r, _pgy_ck, prefer=ctx.room_pref(r))
                # [RS-31] 週別跟診計數:只記【真的坐進去】的那一次,而且只記
                # 二早/四下/五早(週三下午治療室休診釋出的入座不在輪替規則內)。
                if ctx.two_pgy_photo_only:
                    k = _follow_week_key(ctx.d, pick)
                    ctx.fc.two_pgy_follow_week[k] = \
                        ctx.fc.two_pgy_follow_week.get(k, 0) + 1


class ClerkSeedStep(FillStep):
    def run(self, ctx, slots, log):
        # [RS-33] 到達整梯跟診上限的人不再入座 → 那一格留空(他去放假)。
        #   ★每一格重算★:同一時段內前面幾間坐掉之後,有人可能剛好到頂。
        for r in _room_order(ctx):
            if not ctx.clerk:
                break
            # [RS-15] 容量檢查:TwoPgySeatStep 可能已先坐了 PGY —— 本步原本
            # 假設自己是第一個入座者(房必空),盲塞會把容量 1 的房坐成 2 人。
            # 非兩位 PGY 月時房仍為空,此檢查恆真、行為不變。
            if len(ctx.room_slots[r]) < ctx.capacity:
                _seat(ctx, ctx.clerk, r, _clerk_ck,
                      eligible=_clerk_seat_eligible(ctx, ctx.clerk))


class PgyMixStep(FillStep):
    def run(self, ctx, slots, log):
        rooms = _room_order(ctx, pref_first=True)
        # (a) 優先補「已坐 1 位 Clerk」的診間第 2 位 → 形成 1C+1P 混搭
        #     （Clerk 少於診間數時，先配對再說，不先去佔空房）
        for r in rooms:
            if not ctx.pgy:
                return
            if len(ctx.room_slots[r]) == 1 < ctx.capacity:
                _seat(ctx, ctx.pgy, r, _pgy_ck, prefer=ctx.room_pref(r))
        # (b) 再填空診間的第 1、2 位（PGY 只優先到第 2 位；第 3 位起留給 Clerk
        #     overflow — 見 §3.6 步驟 4/5）。無 Clerk 月即由此直填診間。
        for slot in range(min(ctx.capacity, 2)):
            for r in rooms:
                if not ctx.pgy:
                    return
                if len(ctx.room_slots[r]) == slot:
                    _seat(ctx, ctx.pgy, r, _pgy_ck, prefer=ctx.room_pref(r))


class ClerkOverflowStep(FillStep):
    def run(self, ctx, slots, log):
        for r in _room_order(ctx):
            while len(ctx.room_slots[r]) < ctx.capacity and ctx.clerk:
                # [RS-33] ★回傳 None 一定要 break★:`_seat` 沒挑到人時
                #   刻意不動 pool(那一格留空),不 break 的話這個 while
                #   會空轉到掛。
                if _seat(ctx, ctx.clerk, r, _clerk_ck,
                         eligible=_clerk_seat_eligible(ctx, ctx.clerk)) is None:
                    break


class RestStep(FillStep):
    def run(self, ctx, slots, log):
        rest_people = sorted(ctx.pgy + ctx.clerk)
        if not rest_people:
            return
        for p in ctx.pgy:                              # 放假計數同樣分命名空間
            k = _pgy_ck(ctx, p)
            ctx.fc.rest[k] = ctx.fc.rest.get(k, 0) + 1
            ctx.fc.last_rest[k] = ctx.d
        for c in ctx.clerk:
            k = _clerk_ck(ctx, c)
            ctx.fc.rest[k] = ctx.fc.rest.get(k, 0) + 1
            ctx.fc.last_rest[k] = ctx.d
        slots[REST] = rest_people
        log.append(f"{ctx.session} 放假：{'、'.join(rest_people)}")


PIPELINE = [PhotoStep(), TreatmentStep(), BiopsyStep(), TwoPgySeatStep(),
            ClerkSeedStep(), PgyMixStep(), ClerkOverflowStep(), RestStep()]


def solve_session(d: date, session: str, rooms: list, pgy_avail: list,
                  clerk_avail: list, biopsy_open: bool, fc: FairCounters,
                  capacity: int = 2, pipeline=None, batch_key: str = "",
                  apply_pref=frozenset(), two_pgy_mode: bool = False,
                  biopsy_quota_left=None,
                  biopsy_force=frozenset(), seat_reserved=None,
                  seat_cap=None) -> tuple:
    """單一時段填充 → (slots, log)。slots: {房/治療室/切片室/放假: [代號,...]}。"""
    # ★同一個人不可以在池子裡出現兩次★(外審 2026-08-21 P1-01):每一步都是
    #   「選一個 → remove 一個 occurrence」,重複的代號因此可以在同一時段被
    #   排進兩個工作(照光+治療室、切片+跟診)。寫入邊界與 build_day_input
    #   都已經擋,這裡是【求解器自己的】最後一道 —— 任何呼叫端(含測試)
    #   都不該有辦法用重複名單解出班表。
    _pgy = dedupe_codes(pgy_avail)
    _clerk = dedupe_codes(clerk_avail)
    _log_pre: list = []
    if len(_pgy) != len(list(pgy_avail)) or len(_clerk) != len(list(clerk_avail)):
        _log_pre.append(
            f"⚠ {session} 可用名單有重複代號 → 已去重"
            f"(請修正 PGY 名單/梯次成員)")
    # ★跨池重疊也是同一件事★(外審 RS-17 R1-1):同一個代號同時出現在 PGY
    #   名單與 Clerk 梯次時,逐池去重各自都「合法」—— 照光排 PGY A、切片排
    #   Clerk A,存檔與匯出裡就是同一個代號出現在兩格,誰也分不出那是兩個人
    #   還是一個人被排了兩件事。★留 PGY、把他從 Clerk 池移除★:照光/治療室
    #   是每時段硬性需求,切片/跟診可略;並且明講是誰,不能只寫進 log
    #   (使用者看得到的只有 warnings 面板)。
    _cross = [c for c in _clerk if c in set(_pgy)]
    if _cross:
        _clerk = [c for c in _clerk if c not in set(_cross)]
        _log_pre.append(
            f"⚠ {session} 代號 {'、'.join(_cross)} 同時在 PGY 名單與 Clerk "
            f"梯次中 → 本時段只當 PGY 排(請修正其中一邊的名單)")
    ctx = SessionCtx(
        d=d, session=session, rooms=sorted(rooms),
        pgy=sorted(_pgy),
        clerk=sorted(_clerk),
        biopsy_open=biopsy_open, capacity=capacity, fc=fc,
        room_slots={r: [] for r in sorted(rooms)}, batch_key=batch_key,
        apply_pref=frozenset(apply_pref), two_pgy_mode=two_pgy_mode,
        biopsy_quota_left=(dict(biopsy_quota_left)
                           if biopsy_quota_left is not None else None),
        biopsy_force=frozenset(biopsy_force),
        seat_reserved=(dict(seat_reserved)
                       if seat_reserved is not None else None),
        seat_cap=dict(seat_cap) if seat_cap is not None else None)
    slots: dict = {}
    log: list = list(_log_pre)             # 名單問題要進使用者看得到的警告
    for step in (pipeline or PIPELINE):
        step.run(ctx, slots, log)
    for r in ctx.rooms:                          # 房間格（含空房不輸出）
        if ctx.room_slots[r]:
            slots[r] = ctx.room_slots[r]
    return slots, log


@dataclass
class DaySolveInput:
    ym: str
    grid: dict                    # {date: {session: [rooms]}}（clinic_grid.month_grid）
    pgy_roster: list              # 該月 PGY 代號
    clerk_batches: list = field(default_factory=list)  # ClerkBatch 樣（.covers/.members/.id）
    # ★只給 RF-08 仲裁用的完整梯次順序★(外審 RS-26 R3):`clerk_batches` 的
    #   契約是「涵蓋本月的梯次」(統計/側欄/報告照它列人),不可以為了仲裁把
    #   鄰居塞進去。而勝者是【逐日】判定的,配額的定義域又是整梯 —— 上個月
    #   某一天的勝者可能是一個本月開始前就結束的梯次。空 = 退回
    #   `clerk_batches`(直接呼叫求解器的測試/工具)。
    batch_order: list = field(default_factory=list)
    # ★依梯次★(外審 2026-08-24 P2-01):壓平成全域 map 的話,重疊梯次的敗者
    #   設定會污染勝者(RF-08 只採原始順序第一個)。{batch_id: {iso: {時段: bool}}}
    biopsy_open: dict = field(default_factory=dict)
    leaves: dict = field(default_factory=dict)        # {"pgy":{c:set},"clerk":{c:set}}
    capacity: int = 2
    locked: dict = field(default_factory=dict)        # {iso: {session: slots}} 鎖定不重排
    # RF-09 跨月梯次延續：上月屬某跨月梯次的既存 day_slots（只餵切片/clerk 公平計數）
    prior_sessions: dict = field(default_factory=dict)  # {iso: {session: slots}}
    prior_pgy: set = field(default_factory=set)          # 上月 PGY 代號（從 replay 剔除）
    # [RS-24] 年度國定假日(整梯配額的分子要濾掉那些日子 —— 切片格網的 UI
    #   允許勾選所有平日,不會因為後來變成假日而自動取消;而那一天在該月的
    #   `month_grid` 裡根本不存在,永遠排不到)。
    holidays: set = field(default_factory=set)
    # [RS-26] 整梯真正開診的日子(本月 + 下個月的格網;`build_day_input` 算)。
    #   空集合 = 呼叫端沒算 → 退回「只用假日/週末過濾」的舊行為。
    course_days: set = field(default_factory=set)
    # [RS-34] 整梯★真的有跟診診間★的日子。與 `course_days` ★刻意分開★
    #   (外審 RS-34 R2 P1):`month_grid` 對每個非假日平日都會寫入一個鍵,
    #   ★即使上午/下午的診間清單都是空的★ —— 所以「在 `course_days` 裡」
    #   只代表那天不是週末/國定假日,不代表跟診排得到人。
    #   ★不可以直接把 `course_days` 收窄★:它同時是切片配額的分母,而切片室
    #   與跟診診間是分開的兩件事(全院跟診停診那天切片室仍可能開)。
    course_clinic_days: set = field(default_factory=set)
    # ★[RS-29] 相鄰月份【已經定下來、這次求解改不動】的時段★
    #   (全審 2026-08-24 P1-01)。形狀同 `locked`:{iso: {session: {格: [代號]}}}。
    #   RS-26 讓配額的分母看得到整梯(下個月的開放時段也算),但「那一格是不是
    #   已經有人」仍只看本月 —— 於是下個月已鎖定/已定案的切片會被當成
    #   【還能自由分配的未來機會】:兩人各該切一次、9/01 早已鎖給 C1 時,
    #   求解器以為 9/01 還能給 C2,8/31 就掉到抖動決勝而挑了 C1 → C1 切兩次、
    #   C2 掛零,而 1/1 的可行解明明存在。
    #   ★與 `locked` 分開兩個欄位★:`locked` 的語意是「本月原樣輸出」,
    #   把鄰月的東西混進去會讓那些時段被寫進本月的結果。這裡只餵計數與可行性。
    #   進了 dataclass 就自動進指紋 → 預覽期間有人改動下個月的鎖定/定案,
    #   套用時會被判過期(全審點名的第二個缺口)。
    course_fixed: dict = field(default_factory=dict)
    apply_pref: set = field(default_factory=set)  # Apply 本科 PGY（101 週二/五平手優先）


def day_input_fingerprint(inp: "DaySolveInput") -> str:
    """這一次求解【吃到的全部輸入】的識別(見 `roster.fingerprint`)。

    ★逐欄列舉會腐爛,所以走 dataclass 的欄位本身★(外審排班第 1 輪 P1-02):
    PGY/Clerk 的求解結果在預覽視窗裡可能停留很久,期間他機同步進來的請假、
    Clerk 梯次、停診、門診模板、PGY 名單、已鎖定時段…… 任何一項變了,舊解
    再套用下去就是把最新狀態整批蓋掉(請假的人又被排上、剛停診的診間又有人)。
    ★正規化的實作只有一份★(`roster.fingerprint`):R/VS 那一側用的是同一個
    函式 —— 兩邊各寫一套的話,遲早只有一邊被修好。
    """
    from cmuh_common.roster.fingerprint import input_fingerprint  # noqa: PLC0415
    return input_fingerprint(inp)


def _avail(roster: list, leave_map: dict, d: date) -> list:
    return sorted(p for p in roster if d not in (leave_map.get(p) or set()))


def replay_counters(fc: FairCounters, d: date, session: str, slots: dict,
                    batch_key: str, pgy_set: set, clerk_set: set) -> None:
    """把「已鎖定/既存」時段結果餵進公平計數，讓後續未鎖時段對齊（不重新分配）。
    以名單分類 PGY/Clerk 命名空間（座位/放假）；治療室→tx、切片室→biopsy。"""
    wed_pm = (d.weekday() == WED and session == "下午")
    # 照光/治療室 key 是裸代號、PGY 代號整月穩定，stale key 不污染現役者 → 不過濾。
    for p in slots.get(PHOTO, []):
        fc.photo_total[p] = fc.photo_total.get(p, 0) + 1
        if wed_pm:
            fc.photo_wed_pm[p] = fc.photo_wed_pm.get(p, 0) + 1
        fc.last_photo[p] = d
    for p in slots.get(TREATMENT, []):
        fc.tx_total[p] = fc.tx_total.get(p, 0) + 1
        fc.last_tx[p] = d
    for c in slots.get(BIOPSY, []):
        if c not in clerk_set:            # RF-10：已換梯/非名單代號不污染切片命名空間
            continue
        k = (batch_key, c)
        fc.biopsy_done[k] = fc.biopsy_done.get(k, 0) + 1
        fc.last_biopsy[k] = d

    def _ck(p):
        return ("pgy", p) if p in pgy_set else ("clerk", batch_key, p)
    _room_slots = {s: ps for s, ps in slots.items()
                   if s not in (PHOTO, TREATMENT, BIOPSY, REST)}
    _working = {p for ps in _room_slots.values() for p in ps}
    _working |= {p for s in (PHOTO, TREATMENT, BIOPSY) for p in slots.get(s, [])}
    # [2026-07-27 使用者] 反整天放假的「今日已有工作」也要回放：早上鎖定/既存格
    # 裡有工作的人，下午自動排班時不得再被當成「今天閒著」而優先補位。
    for p in _working:
        if p in pgy_set or p in clerk_set:
            fc.worked_day[_ck(p)] = d
    for slot, people in slots.items():
        if slot in (PHOTO, TREATMENT, BIOPSY):
            continue
        for p in people:
            if p not in pgy_set and p not in clerk_set:
                continue                  # RF-10：未知代號不計座位/放假（不誤繼承）
            k = _ck(p)
            if slot == REST:
                fc.rest[k] = fc.rest.get(k, 0) + 1
                fc.last_rest[k] = d
            else:                         # 跟診：連同房多樣性計數一起回放
                fc.seat[k] = fc.seat.get(k, 0) + 1
                fc.last_seat[k] = d
                rk = str(slot).strip()
                fc.seat_room[(k, rk)] = fc.seat_room.get((k, rk), 0) + 1
                fc.last_seat_room[k] = rk
                # [RS-31] `two_pgy_follow_week` ★刻意不在這裡記★:本月鎖定
                # 時段的跟診已在主迴圈【之前】整月預掃入帳(未來的鎖定要能
                # 影響更早的自動時段 —— 外審 R1 P2),在這裡再記就是重複;
                # RF-09 上月回放則刻意不入帳(PGY 公平是月度的)。在這裡
                # 補記會把鎖定跟診算兩次、平手決勝權被偷走 —— 有測試釘著。
    # [2026-07-25] 同伴計數同樣回放：鎖定/跨月既存格若不算共事次數,後續未鎖時段
    # 會以為這兩人沒配過而繼續把他們湊在一起。同房內兩兩各記一次。
    # （獨立一圈跑,不可併進上面的 for——併進去會逐房重跑而重複計數。）
    for slot, people in slots.items():
        if slot in (PHOTO, TREATMENT, BIOPSY, REST):
            continue
        known = [p for p in people if p in pgy_set or p in clerk_set]
        for i, a in enumerate(known):
            for b in known[i + 1:]:
                pk = _pair_key(_ck(a), _ck(b))
                fc.pair[pk] = fc.pair.get(pk, 0) + 1


def _warn_locked_content(warnings: list, d: date, session: str, locked_slots: dict,
                         pgy_set: set, clerk_set: set,
                         pgy_leave: dict, clerk_leave: dict) -> None:
    """RF-10：鎖定內容原樣保留，但檢核當日請假者 / 非名單代號並人話警告（不改內容）。"""
    # ★鎖定內容也要檢查「同一個人同時段兩件事」★(外審 RS-17 R1-2):
    #   鎖定時段原樣保留、不重排 —— 手動編輯或人工改 JSON 造出的雙排會原封
    #   不動地落地,而預覽/報告完全不提。使用者按下套用之前就該看到。
    _where: dict = {}
    for slot_name, people in (locked_slots or {}).items():
        for p in (people or []):
            _where.setdefault(str(p), []).append(str(slot_name))
    for p, places in sorted(_where.items()):
        if len(places) > 1:
            warnings.append(
                f"{d.month}/{d.day} {session} 🔒鎖定時段內 {p} 同時被排在 "
                f"{'、'.join(places)}——同一個人同一時段只能做一件事,請解鎖修正")
    warned_leave: set = set()
    for slot_name, people in locked_slots.items():
        for p in people:
            if p not in pgy_set and p not in clerk_set:
                warnings.append(f"{d.month}/{d.day} {session} 🔒鎖定時段內 {p} "
                                f"不在本月 PGY 名單/當日梯次——請確認")
                continue
            if slot_name == REST or p in warned_leave:  # 放假不算衝突；同人只警告一次
                continue
            leave_set = pgy_leave.get(p) if p in pgy_set else clerk_leave.get(p)
            if leave_set and d in leave_set:
                warned_leave.add(p)
                warnings.append(f"{d.month}/{d.day} {session} 🔒鎖定時段內 {p} "
                                f"當日已請假，仍照鎖定排入——請確認或解鎖重排")


def arbitration_order(inp: DaySolveInput) -> list:
    """仲裁用的梯次順序(原始順序)。

    優先用 `batch_order` —— 它含【只供仲裁的鄰居梯次】(RS-26:上個月某一天的
    勝者可能是一個本月開始前就結束的梯次)。沒有就退回 `clerk_batches`
    (直接呼叫求解器的測試/工具)。
    """
    return list(inp.batch_order or inp.clerk_batches)


def batches_on_day(order, d) -> list:
    """涵蓋這一天的梯次,★維持原始順序★(勝者判準就是靠這個順序)。"""
    return [b for b in order if b.covers(d)]


def day_owner_batch(order, d):
    """RF-08:這一天由哪一梯做主 → 該梯次(沒有涵蓋這一天的就 None)。

    ★判準只留一份★:求解器的每一個仲裁點、以及 service 的日排班結構驗證,
    都問這同一個函式。同日可能被多個梯次涵蓋(設定允許同週一多梯、或起始日
    打錯而部分重疊),勝者＝原始順序第一個 —— 這是刻意的決定性規則,
    其餘梯次的成員該日不排。
    """
    covering = batches_on_day(order, d)
    return covering[0] if covering else None


def batch_biopsy_slots(inp: DaySolveInput, order=None) -> tuple:
    """整梯【真的排得到】的切片時段 → ({梯次: {(iso, 時段)}}, 之後還有的梯次 id)。

    ★這是配額的分母,也是「這一梯排完了沒」的依據★(RS-28):求解器拿它算
    每人該切幾次;service 的現況檢查拿第二個回傳值決定要用哪一種門檻。
    ★判準只留一份★ —— 兩邊不可以各自數一次「開放了幾個時段」。

    盤點時逐條套用的排除規則(每一條都有它自己的外審來歷):
      * 只算【這一梯自己涵蓋】的日期;
      * ★分母也要套 RF-08 的勝者判準★(外審 RS-26 R1 P2):重疊日只有原始
        順序第一個梯次排得到人 —— 敗者在那些日子的開放是它【永遠排不到】的量,
        算進它自己的分母會把配額撐大;
      * 週末/國定假日不在任何月份的格網裡,不是可排的量;
      * ★「那一天到底開不開診」要用該月的格網★(外審 2026-08-24 P1-01):
        切片格網的 UI 允許勾選所有平日,而下個月那一天可能整天停診;
        `course_days` 是整梯真正開診的日子(呼叫端沒算就退回舊行為);
      * 週三下午恆關;
      * ★用不重複的 (日期, 時段) 集合★:既在開放格網又在鎖定表裡不可算兩次。

    第二個回傳值是★這一梯【之後】還有沒有排得到的時段★(外審 Codex 配額版 R2):
    跨月梯次到了第二個月一定看得到上個月的日期,若拿「有沒有格網外的日期」
    當判準,最終那一次的差異就永遠不會被點名 —— 要看的是【未來】。
    """
    order = arbitration_order(inp) if order is None else order
    slots: dict = {}
    more: set = set()
    grid_days = {d.isoformat() for d in inp.grid}
    grid_last = max(inp.grid) if inp.grid else None
    for bid, days in (inp.biopsy_open or {}).items():
        bat = next((b for b in inp.clerk_batches if b.id == bid), None)
        if bat is None:
            continue
        for iso, sess in (days or {}).items():
            try:
                bd = date.fromisoformat(iso)
            except (ValueError, TypeError):
                continue
            if not bat.covers(bd):
                continue
            own = day_owner_batch(order, bd)
            if own is None or own.id != bid:
                continue
            if is_weekend(bd) or bd in (inp.holidays or set()):
                continue
            if inp.course_days and iso not in inp.course_days:
                continue
            for s, on in (sess or {}).items():
                if not on or s not in STUDENT_SESSIONS:
                    continue
                if bd.weekday() == WED and s == "下午":
                    continue
                slots.setdefault(bid, set()).add((iso, s))
                if (iso not in grid_days and grid_last is not None
                        and bd > grid_last):
                    more.add(bid)
    return slots, more


def apply_locked_adjustments(inp: DaySolveInput, order,
                             bio_slots: dict) -> tuple:
    """鎖定/既定時段的吸收(就地修改 bio_slots)→
    (locked_keys, locked_bx, locked_n, ord_map, locked_seat)。

    ★改名(RS-33)★:這一趟不只調整切片分母了 —— 它同時盤點【鎖定的跟診】
    位置給跟診上限預留用。兩者的規則(格網邊界、勝者梯次、RF-10 未知代號、
    相鄰月份的時序位置)★逐條相同★,所以走同一趟吸收;各自寫一份的話,
    日後只會有一邊跟著邊界規則走。

    ★solver 與 service 的現況檢查共用這一份★(外審 RS-28 R2 P2):cap 的分母
    在求解器裡會被這裡調整(鎖定格先拿掉、有效鎖定切片加回、未知代號不加回),
    service 若用原始盤點算 cap,求解器合法排出的平均結果會被誤報「超過配額」,
    反向(鎖定成空白的開放格)也會漏掉等量的超額 —— 兩邊必須用同一個
    「有效時段集合」。service 只需要調整後的 `bio_slots`,其餘三個回傳值是
    求解器要的(鎖定預扣/順序)。

    (內文自求解器逐字搬移;開頭別名只是接參數,勿「整理」成改名 ——
     ★byte-identical 的搬移之外不該有第二種變更★。)
    """
    _order, _bio_slots = order, bio_slots
    _grid_days = {_d.isoformat() for _d in inp.grid}
    # ★鎖定時段已經指派的切片要算數★(外審 Codex RS-24 P2):它不在 `_bio_seq`
    #   裡(鎖定時段不重排),但那個人【確實會】切到 —— 不排除的話,期限會把
    #   一個已經有著落的人再補一次,還可能因此擠掉真正沒輪到的人。
    #   (掃整份鎖定表就夠:落在過去的鎖定時段會由 `replay_counters` 計入
    #    `biopsy_done`,兩條路都會把他當成已經輪過。)
    #   ★鍵要含梯次★(外審 Codex 第 2 輪 P2):Clerk 代號是依梯次命名空間的
    #   (`_clerk_ck`)—— 只存代號的話,別梯的同一個代號會讓這一梯的人被當成
    #   「已經有著落」而永遠不補。勝者梯次的判準與主迴圈一致(RF-08)。
    #   ★是「已經占掉一次配額」不是「整個豁免」★:配額制下鎖定時段那一次
    #   也算他的次數,所以要從他的配額扣一次(而不是把人整個排除);分母也要
    #   把這些時段算進去(它們確實有人切)。
    _locked_bx: dict = {}               # {(梯次, 代號): [(順序, 日期)…]}
    _locked_n: dict = {}                # {梯次: 鎖定時段的切片總數}
    #   ★鎖定時段【不是】還能分給別人的容量★(外審 RS-26 R1 P2):它算進配額
    #   分母(那一格確實有人切),但可行性匹配若把它當成「之後還排得到」的
    #   機會,就會虛構出一個不存在的未來 → 今天挑錯人。
    _locked_keys: dict = {}             # {梯次: {(日期, 時段)}}
    #   ★鎖定時段一律先從分母拿掉★(外審 Codex 配額版 R2):它不重排,所以
    #   「本來標示開放」不代表排得到人 —— 只有那一格【確實指派給本梯成員】
    #   時才把它加回去(那個人真的會切一次)。空的鎖定格、或指派給未知/
    #   已換梯代號的鎖定格,都不是可分配的量。
    #   ★[RS-33] 鎖定的跟診位置★:與 `_locked_bx` 同一個道理 —— 主迴圈是
    #   時序的,較晚日期鎖定的跟診要走到那天才由 `replay_counters` 計入
    #   `fc.seat`。不先預留的話,較早的日子會把他自動排到上限,鎖定那一次再
    #   加上去 = 整梯 12 次:★自動排班自己突破了它宣稱的上限★,而事後的
    #   點名還會把它歸因成「手動調整」(外審 RS-33 R1 P2)。
    _locked_seat: dict = {}             # {(梯次, 代號): [位置…]}
    _ord: dict = {}                     # (iso, 時段) → 全月的先後順序
    for _i, _d in enumerate(sorted(inp.grid)):
        for _j, _s in enumerate(STUDENT_SESSIONS):
            _ord[(_d.isoformat(), _s)] = _i * 10 + _j
    def _absorb(table, open_days, pos_of):
        """把一張「已經定下來」的表吸收進分母/預扣/佔位。

        `open_days` = 那些日期裡【真的有開診】的集合;`pos_of` 給這一格在
        時序上的位置(本月用 `_ord`,相鄰月份一律排在本月之後)。
        本月與相鄰月份的規則完全一樣,所以只留一份實作。
        """
        for _iso, _sessions in (table or {}).items():
            try:
                _ld = date.fromisoformat(_iso)
            except (ValueError, TypeError):
                continue
            _lcov = batches_on_day(_order, _ld)
            if not _lcov:
                continue
            if _iso not in open_days:
                # ★掉出格網的鎖定時段只原樣保留,不餵任何計數★(RF-02 的契約):
                #   那一天在這個月根本沒有開診(假日/週末)—— 主迴圈永遠不會處理它,
                #   算進配額分母就會把 cap 撐高成排不完的量。
                continue
            _lmembers = set(dedupe_codes(_lcov[0].members))
            for _s, _slots in (_sessions or {}).items():
                if _s not in STUDENT_SESSIONS:
                    continue
                _locked_keys.setdefault(_lcov[0].id, set()).add((_iso, _s))
                _bio_slots.setdefault(_lcov[0].id, set()).discard((_iso, _s))
                for _c in ((_slots or {}).get(BIOPSY) or []):
                    if str(_c) not in _lmembers:
                        # ★未知/已換梯的代號不得污染公平計數★(RF-10 的契約;
                        #   `replay_counters` 也是這樣濾的)—— 算進分母會把配額
                        #   撐大,自動排班就會多排,反而讓現役成員的次數不一致。
                        continue
                    _locked_bx.setdefault((_lcov[0].id, str(_c)), []).append(
                        pos_of(_iso, _s))
                    _locked_n[_lcov[0].id] = _locked_n.get(_lcov[0].id, 0) + 1
                    _bio_slots.setdefault(_lcov[0].id, set()).add((_iso, _s))
                for _slot, _people in (_slots or {}).items():
                    if not is_follow_slot(_slot):
                        continue          # 照光/治療室/切片/放假不是跟診
                    for _c in (_people or []):
                        # ★這裡刻意不濾梯次成員★(不同於上面的切片):切片的
                        #   盤點還餵分母 `_locked_n`/`_bio_slots`,未知代號會
                        #   把配額撐大;跟診的盤點只餵【逐位成員】的預留表
                        #   (主迴圈是 `for c in _members` 建的),非成員的鍵
                        #   永遠沒有人去讀它。★量不到的守衛是死碼★
                        #   (同 RS-28 拿掉的 `(d.year, d.month)`)。
                        _locked_seat.setdefault(
                            (_lcov[0].id, str(_c)), []).append(
                                pos_of(_iso, _s))

    _absorb(inp.locked, _grid_days, lambda i, s: _ord.get((i, s), -1))
    # ★[RS-29] 相鄰月份的既定時段★:規則與本月相同,只有兩處不一樣 ——
    #   ① 開診與否要看整梯的 `course_days`(本月的格網當然不含下個月的日子);
    #   ② 時序位置一律排在本月所有時段【之後】。位置是拿來判斷
    #      「這一次鎖定還沒跑到 → 要先預留他的配額」的(見主迴圈的 `o > _now`),
    #      下個月的時段永遠在未來 —— 沿用 `_ord.get(..., -1)` 的話會被當成
    #      「早就跑過了」而不預扣,C1 的配額就白白多出一次。
    #   ★本月的日期不走這裡★:那是 `locked` 的地盤,兩邊都吸收會重複計數。
    #   ★上個月的既定時段位置要算「過去」★(外審 RS-29 R1 P1):跨月梯次的
    #   上月時段已經由 `prior_sessions` 回放進 `fc.biopsy_done`(RF-09),
    #   而配額是 `cap - biopsy_done - 未來的鎖定預留` —— 把上月也標成未來,
    #   同一次切片會被【扣兩次】(既算已完成、又算預留),那個人本月的配額
    #   憑空少一次。`o > _now` 這個判準本來就是為了不重複扣才存在的。
    _month_start = f"{inp.ym}-01"
    _absorb({i: s for i, s in (inp.course_fixed or {}).items()
             if i not in _grid_days},
            set(inp.course_days or ()),
            lambda i, s: (-1 if i < _month_start else _FUTURE_POS))
    return _locked_keys, _locked_bx, _locked_n, _ord, _locked_seat


def clerk_schedulable_days(inp, b) -> set:
    """這一梯【真的排得到班】的日子(RS-34)。

    ★不可以拿「起始日起 14 個日曆日」當定義域★(外審 RS-34 R1 P1-1):
    週末、國定假日、整日停診的那幾天根本不進求解器 —— 拿它們判斷「這個人
    整梯有沒有請假」的話,★在週末勾一天假就會把他判成非全勤★,配額的分母
    因此變了、整份班表跟著變。

    ★而且「在格網裡」不等於「有診可跟」★(外審 RS-34 R2 P1):`month_grid`
    對每個非假日平日都會寫入一個鍵,★即使上午/下午的診間清單都是空的★
    (全日停診就是這個形狀)。所以要看的是【那天到底有沒有跟診診間】——
    `course_clinic_days` 就是為此存在的(三個月都算,跨月梯次才對得起來);
    呼叫端沒算就退回本月的格網,直接看它自己的診間清單。
    """
    open_days = set(inp.course_clinic_days or ())
    out = set()
    for i in range(CLERK_COURSE_DAYS):
        d = b.start_monday + timedelta(days=i)
        if is_weekend(d) or d in (inp.holidays or set()):
            continue
        sessions = inp.grid.get(d)
        if sessions is not None:
            # ★本月的格網最權威★:它直接說得出那天有沒有診間。
            if any(sessions.values()):
                out.add(d)
            continue
        if open_days:
            if d.isoformat() in open_days:
                out.add(d)
        elif inp.course_days and d.isoformat() in inp.course_days:
            out.add(d)          # 舊呼叫端只算得出 `course_days` → 退而求其次
    return out


def clerk_full_attendance(inp, b, clerk_leave) -> list:
    """整梯【全勤】的成員(RS-34)——配額的分母只算他們。

    有人請假時,用「總數 ÷ 全員」會把沒請假的人一起拉下來 —— 那是拿別人的
    跟診機會去換一個補不回來的一致。請假者不設下限、也不拉低別人。
    """
    days = clerk_schedulable_days(inp, b)
    return [c for c in dedupe_codes(b.members)
            if not ((clerk_leave.get(c) or set()) & days)]


def clerk_seat_uneven_warnings(batches, counts, *, only_ids=None) -> list:
    """[RS-34] 整梯跟診次數★沒能拉成一致★的點名 → 人話警告清單。

    `counts` = {(梯次 id, 代號): 跟診次數}(與 `clerk_seat_band_warnings`
    同一個形狀,求解器餵公平計數、service 餵存檔的現況)。

    ★什麼時候會出現★:自動排班會一直重排到一致為止,所以它出現代表這一次
    求解★改不動★那個差距 —— 鎖定格、上個月已經排好的班、或月曆上的手動
    調整。訊息因此要講「哪幾位、幾次」與「為什麼補不掉」,而不是叫使用者
    再按一次自動排班(按幾次都一樣)。
    """
    out: list = []
    for b in batches:
        if only_ids is not None and b.id not in only_ids:
            continue
        c2 = {c: int(counts.get((b.id, c), 0)) for c in sorted(b.members)}
        if not c2 or max(c2.values()) == min(c2.values()):
            continue
        out.append(
            f"跟診次數不一致（梯次 {b.id}）："
            + "、".join(f"{c}×{n}" for c, n in c2.items())
            + " —— 自動排班已排到它做得到的最平，剩下的差距來自鎖定時段、"
              "上個月已排好的班或手動調整，請自行於月曆調整")
    return out


def clerk_batches_ended_by(batches, cutoff: date) -> set:
    """整梯【已經走完】(最後一天不晚於 `cutoff`)的梯次 id。

    給跟診區間點名的「偏少」那一半當閘門用 —— 梯次跨月時,排完第一個月
    ★必然★只有半數時段,那時候喊「跟診偏少」是100% 的誤報,而會固定誤報的
    警告使用者只會學會無視它。
    """
    out = set()
    for b in batches:
        try:
            end = b.start_monday + timedelta(days=CLERK_COURSE_DAYS - 1)
        except (TypeError, AttributeError):   # 壞資料:不擋,只是不點名
            continue
        if end <= cutoff:
            out.add(b.id)
    return out


def clerk_seat_band_warnings(batches, counts, *, only_ids=None,
                             ended_ids=None) -> list:
    """[RS-33] Clerk 整梯跟診次數落在目標區間外的點名 → 人話警告清單。

    `counts` = {(梯次 id, 代號): 跟診次數}。與 `biopsy_quota_warnings` 同一個
    形狀,而且要餵★同一種來源★:求解器餵自己的公平計數、service 餵存檔的現況
    —— 不然「排出來當下沒說話,手改之後也沒人說話」。

    ★只點名、不強求★(使用者 2026-09-02 定案):
    * ★低於下限★:整梯可坐的時段本來就可能不夠(假日/連假/診間少/請假),
      那不是排班演算法生得出來的東西 —— 所以這裡★只講事實★
      「他整梯只跟到 N 次」,不寫「應該要有 7 次」那種做不到的話。
    * ★高於上限★:求解器不會排出來(到頂就留空),所以它一旦出現,
      代表是【手動改的】—— 講清楚是哪幾位,由使用者決定要不要改回。

    `ended_ids`:整梯已經走完的梯次(見 `clerk_batches_ended_by`)。
    ★只擋「偏少」那一半★ —— 兩邊的性質不同:
      * 次數只會往上加,所以「已經超過上限」在梯次中途就已經是定局,
        提早講反而更有用(還來得及手動改回去);
      * 「不足下限」在梯次還沒走完時★必然★成立(跨月梯次排完第一個月時
        更是保證不足),那不是事實而是還沒排到 —— 會固定誤報的警告,
        使用者只會學會無視它。
    ★已知限度★:判準是「梯次最後一天不晚於這個月底」,不是「那些日子真的都
    存過檔」。上個月的班表從來沒排過的話,回放/掃描都取不到那半個梯次的次數,
    這裡仍會說偏少 —— 一個月一個月排下來不會遇到,但值得知道。
    """
    out: list = []
    for b in batches:
        if only_ids is not None and b.id not in only_ids:
            continue
        c2 = {c: int(counts.get((b.id, c), 0)) for c in sorted(b.members)}
        if not c2:
            continue
        low = ([(c, n) for c, n in c2.items() if n < CLERK_SEAT_TARGET_MIN]
               if ended_ids is None or b.id in ended_ids else [])
        if low:
            out.append(
                f"跟診時段偏少（梯次 {b.id}，目標每人 {CLERK_SEAT_TARGET_MIN}"
                f"-{CLERK_SEAT_TARGET_MAX} 個時段）："
                + "、".join(f"{c}×{n}" for c, n in low)
                + " —— 多因假日/連假、診間或人力不足所致，排班無法自行補足")
        high = [(c, n) for c, n in c2.items() if n > CLERK_SEAT_TARGET_MAX]
        if high:
            out.append(
                f"跟診時段偏多（梯次 {b.id}，目標每人上限"
                f" {CLERK_SEAT_TARGET_MAX} 個時段）："
                + "、".join(f"{c}×{n}" for c, n in high)
                + " —— 自動排班到上限就會留空，超出多為手動調整，請確認")
    return out


def biopsy_quota_warnings(batches, counts, *, batch_more=(),
                          only_ids=None, caps=None) -> tuple:
    """切片室配額的點名 → (人話警告清單, 該被標紅的 {(梯次, 代號)})。

    `counts` = {(梯次 id, 代號): 次數}。求解器餵它自己的公平計數;
    service 餵【現況】(存檔的 day_slots,含手動改過的格)——
    ★同一份判準★,不然「排出來當下說平均、手改之後沒人再說話」。

    * ★超過配額★:`caps` 給了這一梯的每人上限就檢查它 —— ★「次數相同」
      不足以證明沒有超過★(外審 RS-28 R1 P2):手動編輯視窗固定提供切片欄位,
      寫入端也不要求該時段在 `biopsy_open` 裡,兩人各自在非開放時段多排一次
      就是 2/2、全距 0、全部靜默,而 RS-24 明定配額用完要留空。
      (求解器本來就不會超額,所以它傳不傳 `caps` 都一樣。)
    * 一個人都沒輪到 → 點名(設計文件 C4);
    * 否則★配額制下「次數一樣」是要求,任何不一致都要點名★(RS-24)——
      但這一梯之後還排得到時放寬成 >1,免得跨月梯次的第一個月每次都跳噪音。
    """
    out: list = []
    flagged: set = set()
    for b in batches:
        if only_ids is not None and b.id not in only_ids:
            continue
        c2 = {c: int(counts.get((b.id, c), 0)) for c in sorted(b.members)}
        if not c2:
            continue
        _cap = (caps or {}).get(b.id)
        if _cap is not None:
            over = [(c, n) for c, n in c2.items() if n > _cap]
            if over:
                out.append(
                    f"切片室超過配額（梯次 {b.id}，每人上限 {_cap} 次）："
                    + "、".join(f"{c}×{n}" for c, n in over)
                    + " —— 配額用完的時段應留空，請於月曆改回")
        missed = [c for c, n in c2.items() if n == 0]
        if missed:
            out.append(f"切片室輪不到（梯次 {b.id}，本梯內未排到）："
                       + "、".join(missed))
            flagged |= {(b.id, c) for c in missed}
        elif (max(c2.values()) - min(c2.values())
                > (1 if b.id in batch_more else 0)):
            out.append(
                f"切片室次數不均（梯次 {b.id}，同梯應盡量一致）："
                + "、".join(f"{c}×{n}" for c, n in c2.items())
                + " —— 多因請假/鎖定時段所致，可手動於月曆調整")
            _top = max(c2.values())
            flagged |= {(b.id, c) for c, n in c2.items() if n < _top}
    return out, flagged


def month_solve_day(inp: DaySolveInput) -> tuple:
    """整月逐（工作日×早/午）填充 → (day_slots, log, warnings)。

    day_slots: {iso: {session: {slot: [代號]}}}；warnings: 人話警告清單。
    - 治療室每個非假日工作日每時段都需 1 PGY(週三下午休診除外;
      [RS-15] 兩位 PGY 月的二早/四下/五早亦不排,該位改優先跟診)。
    - Clerk 逐日只取「當日所屬兩週梯次」的成員（跨梯不互相借人）。

    ★[使用者定案 2026-08-02] 週三下午照光的「補半天假」機制已整個取消★
    原本：週三下午場次無法整除 PGY 人數時，多值的人記一筆欠假，之後在有空檔的時段
    優先放假抵銷；為了讓月底才超額的人也能用月初的空檔補到，還跑了兩趟求解。
    取消的理由：配額＝場次÷人數取整，**PGY 人數多於週三下午場次時配額是 0**
    （例：4 個週三、5 位 PGY——很常見），於是每個值到一次的人都被記欠假，
    月底警告近乎每月都跳，成了噪音。使用者決定整個拿掉。
    保留下來的是「週三下午照光次數盡量平均」（PhotoStep 以 photo_wed_pm 為主鍵）
    與月底的次數統計（person_course_stats 的 photo_wed_pm）——要不要給半天假，
    由使用者看統計自行斟酌、手動於月曆安排。

    ★[RS-34 使用者 2026-09-02] Clerk 跟診次數要【完全一致】★
    「雖然限制 7-11 班,但是每個人都要平均一致,例如全部人都是 9 班、
     全部人都是 8 班等等」。RS-25 的「次數最少者先坐」只保證★全距 ≤1★,
    而且切片室配額用完之後,「今天誰還能坐診」就不再由跟診次數決定 ——
    實測 1 間診 3 個人會跑出 9/10/11(★全距 2★,拿掉 7-11 的上限也一樣,
    不是上限造成的)。要真的一致,得先知道整梯到底有幾個座位可坐,
    而那個數★預測不出來★:它等於「房數×容量 − 那一節 PGY 佔掉的」,
    PGY 佔幾個又由照光/治療室/RS-15 兩位 PGY 月的規則決定。
    ★所以用量的,不用推的★:第一趟照常排(只受 7-11 管)→ 數出每人實際
    坐了幾次 → 算出「每人該坐幾次」→ 第二趟以它為硬上限重排,多出來的
    座位★留空★。這與切片室 RS-24 的配額是同一個作法、同一句理由
    (「多出來的時段留空,寧可空著也不讓誰多」)。
    (兩趟求解在這個函式裡也有前例 —— 見上面那段被取消的補半天假機制。)
    """
    # ★第一趟在迴圈外★:它一定會跑,所以「最好的那一趟」從第一趟起就有值
    #   —— 不必用 `best is None` 這種型別上證不出來的寫法。
    caps: dict = {}
    day_slots, log, warnings, fc = _solve_month_once(inp)
    best_cost = _clerk_equal_cost(inp, fc)
    best_out = (day_slots, log, warnings, fc)
    for _pass in range(1, CLERK_EQUALIZE_MAX_PASSES):
        nxt = _clerk_equal_seat_caps(inp, fc, caps)
        if nxt is None:               # 已經一致 → 收工
            return day_slots, log, warnings
        if nxt == caps:
            # ★再排一趟也不會變★(外審 RS-34 R1 P1-2):全勤者已經一致,
            #   多出來的次數來自這一次求解★改不動★的東西(鎖定格、上個月的
            #   班)。繼續壓只會把全勤者一起拉低,而且永遠追不上 —— 停手,
            #   由下面的點名據實說明。
            break
        caps = nxt
        day_slots, log, warnings, fc = _solve_month_once(inp, seat_cap=caps)
        cost = _clerk_equal_cost(inp, fc)
        if cost < best_cost:
            best_cost, best_out = cost, (day_slots, log, warnings, fc)
    # ★收斂不了就交出最好的那一趟★:硬要再壓只會愈壓愈少,而排班本身仍然
    #   合法(次數不一致由月底點名說明)。★不可以交出「最後一趟」★——
    #   它的上限是被硬降下來的,不保證比先前好。
    #   (真的收斂到一致時,那一趟的代價必然最小 → `best_out` 就是它;
    #    而且更早的一趟若已經一致,上面那個 `return` 早就走掉了。)
    _ds, _log, _warn, _fc = best_out
    _y, _m = int(inp.ym[:4]), int(inp.ym[5:7])
    return _ds, _log, list(_warn) + clerk_seat_uneven_warnings(
        inp.clerk_batches,
        {(b, c): n for k, n in _fc.seat.items()
         if isinstance(k, tuple) and len(k) == 3 and k[0] == "clerk"
         for _ns, b, c in (k,)},
        only_ids=clerk_batches_ended_by(
            inp.clerk_batches, date(_y, _m, monthrange(_y, _m)[1])))


#: [RS-34] 求解最多跑幾趟(第一趟量、其餘收斂)。每趟約 2ms,上限只是止損。
CLERK_EQUALIZE_MAX_PASSES = 6


def _clerk_equal_cost(inp: DaySolveInput, fc: FairCounters) -> tuple:
    """這一趟的好壞 →(★不一致的總量★, 少排的座位數)。越小越好。

    第一鍵是使用者要的東西(全距);第二鍵讓「一樣一致」的兩趟裡,
    ★留空比較少★的那一趟勝出 —— 一致不該用浪費跟診機會去換更多。
    """
    spread = seats = 0
    for b in inp.clerk_batches:
        members = dedupe_codes(b.members)
        if not members:
            continue
        ns = [fc.seat.get(("clerk", b.id, c), 0) for c in members]
        spread += max(ns) - min(ns)
        seats -= sum(ns)
    return (spread, seats)


def _clerk_equal_seat_caps(inp: DaySolveInput, fc: FairCounters,
                           caps: dict) -> "dict | None":
    """[RS-34] 這一趟的結果 → 下一趟的 {(梯次, 代號): 該跟幾次}。
    ★回 None = 已經一致,不必再排★。

    ★只管【已經走完】的梯次★(`clerk_batches_ended_by`):跨月梯次在第一個
    月只排得到一半,那時候的總數不是整梯的總數,拿它算配額會把人壓到一半。
    走完的那個月,`fc.seat` 已經含上個月回放進來的次數(RF-09)= 整梯總數。

    ★分母只算【整梯全勤】的人★:有人請假時,用「總數 ÷ 全員」會把沒請假的
    人一起拉下來 —— 那是拿別人的跟診機會去換一個補不回來的一致。所以配額
    由全勤者的平均決定,請假者★不會被拉高也不會拉低別人★(他本來就落後,
    上限對他不生效),差額由月底的點名說明。全員都請過假 → 退回全員平均。

    ★第一趟用平均、之後用最小值★:平均是「理論上該有幾次」,但貪婪求解不
    保證每個人都搆得到(實測 1 間診 3 個人:平均 10,排出來是 9/10/10)。
    搆不到就往下收斂到「大家都真的做得到」的那一階 —— 而且★每一趟至少要
    降 1★,否則同一個數字會反覆重排而不收斂。

    ★已經套上的上限要留著★:某一梯這一趟拉齊了,下一趟不能把它的上限拿掉
    —— 一拿掉就退回原樣,永遠收斂不了。
    """
    _y, _m = int(inp.ym[:4]), int(inp.ym[5:7])
    ended = clerk_batches_ended_by(
        inp.clerk_batches, date(_y, _m, monthrange(_y, _m)[1]))
    clerk_leave = (inp.leaves.get("clerk") or {})
    out: dict = dict(caps)
    changed = False
    for b in inp.clerk_batches:
        if b.id not in ended:
            continue
        members = dedupe_codes(b.members)
        if not members:
            continue
        counts = {c: fc.seat.get(("clerk", b.id, c), 0) for c in members}
        base = clerk_full_attendance(inp, b, clerk_leave) or members
        base_ns = [counts[c] for c in base]
        level = min(base_ns)
        if max(base_ns) == level and max(counts.values()) <= level:
            continue                  # 真的一致了(請假者落後不算)
        prev = out.get((b.id, base[0]))
        # ★每一趟至少降 1★(`prev - 1`),所以這個迴圈一定會停:同一個數字
        #   反覆重排只會得到同一個結果。
        #   (原本這裡還多寫了一個「target >= prev 就強制降」的保險 ——
        #    突變驗證抓到它★永遠不會成立★:`min(…, prev - 1)` 已經保證了。
        #    ★量不到的守衛是死碼★,刪掉而不是留著誤導。)
        if max(base_ns) == level:
            # ★全勤者已經一致,是【請假者反而比較多】★(外審 RS-34 R1 P1-2):
            #   鎖定格與跨月回放都會把次數加進 `fc.seat`,所以這是可達狀態。
            #   目標就是全勤者這個水準 —— ★不可以再往下壓全勤者★
            #   (那是拿他們的跟診機會去追一個可能追不到的數)。
            #   下一趟若還是同一組上限,`month_solve_day` 會判定「再排也不會變」
            #   而停手並點名 —— 既有的鎖定/上個月的班表這一次求解改不動。
            target = level
        elif prev is None:
            target = sum(base_ns) // len(base_ns)
        else:
            target = min(min(base_ns), prev - 1)
        if target < 0:
            continue
        out.update({(b.id, c): target for c in members})
        changed = True
    return out if changed else None


def _solve_month_once(inp: DaySolveInput, seat_cap=None) -> tuple:
    """單趟整月填充 → (day_slots, log, warnings, fc)。fc 供測試檢視公平計數。

    `seat_cap` = {(梯次, 代號): 該跟幾次}(RS-34 第二趟才有;None = 只受
    7-11 的上限管)。"""
    # ★仲裁只有一份順序★:★凡是要問「這一天由哪一梯做主」的地方都用它★
    #   (主迴圈、可排時段序列、配額分母、鎖定掃描、RF-09 上月回放…)——
    #   免得「哪些梯次參與仲裁」在不同地方各有一套。
    #   ☆這裡刻意不寫「共 N 個地方」☆:數字會隨程式演進而錯,性質不會
    #   (外審 RS-26 R4 就是漏掉了第五個)。
    #   ★勝者判準本身在 `day_owner_batch()`★(RS-27):service 的結構驗證要問
    #   同一個問題(切片室那一格該是誰),兩邊共用一份實作。
    _order = arbitration_order(inp)
    fc = FairCounters()
    day_slots: dict = {}
    log: list = []
    warnings: list = []
    pgy_leave = (inp.leaves.get("pgy") or {})
    clerk_leave = (inp.leaves.get("clerk") or {})
    # [RS-15] 判準=該月 PGY 名單恰 2 位(去重;不看當日可用人數 —— 請假造成
    # 的臨時 2 人不算,名單就是 2 人的月份整月一致啟用,行為可預期)。
    two_pgy = len({str(p) for p in inp.pgy_roster}) == 2

    # ★[RS-31 外審 R1 P2] 本月鎖定時段的三時段跟診要【先】入帳★:主迴圈是
    # 時序的,鎖定時段走到那天才回放 —— 週四/週五鎖了 P1 跟診、週二自動排班
    # 時看不到的話,照光仍會抽走 P2、P1 連跟三場。與切片室「還沒跑到的鎖定
    # 時段要先預留」(`_locked_bx`)同一個道理。★這裡是鎖定跟診的唯一寫入點★
    # (`replay_counters` 刻意不記,否則重複);RF-02 掉出格網的鎖定本來就
    # 不餵計數 → 只掃格網內的日子,邊界一致。不分兩位 PGY 模式一律預掃
    # (讀取端只在兩位 PGY 月看)。
    _pgy_codes = set(inp.pgy_roster)
    for _d3 in sorted(inp.grid):
        if is_weekend(_d3):
            continue
        _iso3 = _d3.isoformat()
        for _s3 in STUDENT_SESSIONS:
            _ls3 = (inp.locked.get(_iso3) or {}).get(_s3)
            if _ls3 is None or (_d3.weekday(), _s3) not in TWO_PGY_PHOTO_ONLY:
                continue
            for _slot3, _ppl3 in _ls3.items():
                if _slot3 in (PHOTO, TREATMENT, BIOPSY, REST):
                    continue
                for _p3 in (_ppl3 or []):
                    if _p3 in _pgy_codes:
                        _wk3 = _follow_week_key(_d3, _p3)
                        fc.two_pgy_follow_week[_wk3] = \
                            fc.two_pgy_follow_week.get(_wk3, 0) + 1

    # RF-09：先把上月跨月梯次的既存班表餵進 fc（只餵切片室與 clerk 座位/放假；跳過
    # 治療室與上月 PGY，避免污染本月 PGY 月度公平），讓「本梯未輪過切片」的判定與月底
    # missed 警告都以「整梯」而非「本月」為單位。
    for iso in sorted(inp.prior_sessions):
        try:
            d = date.fromisoformat(iso)
        except (ValueError, TypeError):
            continue
        # ★回放的梯次也要由完整的勝者順序決定★(外審 RS-26 R4):那一天的
        #   勝者可能是【只供仲裁的鄰居梯次】—— 而 Clerk 代號跨梯會重用,
        #   把屬於 b0/C1 的既存切片記進 b1/C1 的話,本月會多扣他一次配額
        #   (公平計數刻意用 `(梯次, 代號)` 隔開,選錯梯次等於繞過那道隔離)。
        #   勝者是鄰居時就用【它自己】的命名空間回放:本月不排它,計數互不干擾。
        batch = day_owner_batch(_order, d)
        if batch is None:
            continue
        members = set(batch.members)
        sessions = inp.prior_sessions[iso]
        for session in STUDENT_SESSIONS:
            slots = sessions.get(session)
            if not slots:
                continue
            filtered = {}
            for slot_name, people in slots.items():
                if slot_name in (PHOTO, TREATMENT):   # 照光/治療室屬 PGY 月度公平→不跨月餵
                    continue
                keep = [p for p in people
                        if p in members and p not in inp.prior_pgy]
                if keep:
                    filtered[slot_name] = keep
            if filtered:
                replay_counters(fc, d, session, filtered, batch.id,
                                pgy_set=set(), clerk_set=members)

    # [RS-24] ★期限★:每一個「還解得到的」切片開放時段,在它自己的梯次裡
    #   後面(含自己)還剩幾個。鎖定時段不算(它不重排,補不了配額)、週三下午
    #   不算(恆關)。★勝者梯次的判準要與主迴圈一致★(RF-08 取原始順序第一個),
    #   否則算的是另一梯的剩餘量。
    #   ★跨月的梯次只看得到本月的格網★ → 剩餘量被低估,期限會提早觸發
    #   (寧可早一點補到切片,也不要整梯有人沒輪到);下個月那半段會看到
    #   回放進來的 `biopsy_done`,不會重複強制。
    _bio_seq: list = []
    for _d in sorted(inp.grid):
        if is_weekend(_d):
            continue
        _iso = _d.isoformat()
        _cov = batches_on_day(_order, _d)
        if not _cov:
            continue
        for _s in STUDENT_SESSIONS:
            if (inp.locked.get(_iso) or {}).get(_s) is not None:
                continue
            if _d.weekday() == WED and _s == "下午":
                continue
            if not ((inp.biopsy_open.get(_cov[0].id) or {}).get(_iso)
                    or {}).get(_s):
                continue
            _bio_seq.append((_iso, _s, _cov[0].id))
    _bio_pos: dict = {}
    for _i, (_iso, _s, _bid) in enumerate(_bio_seq):
        _bio_pos[(_iso, _s)] = _i
    # ★配額的分母是【整梯】的開放時段數,不是本月看得到的那幾個★
    #   (外審 Codex RS-24 配額版 P1):梯次跨月時,兩個月各看到 8 個時段、
    #   5 個人 —— 各自算 `8//5=1` 的話整梯只有 2 次,而整梯的正解是
    #   `16//5=3`,除法的餘數被吃掉了。`biopsy_open` 是【依梯次】載入的
    #   (`build_day_input`),本來就含跨月日期,所以這裡算得出整梯的量。
    #   ★用不重複的 (日期, 時段) 集合★:鎖定時段既在開放格網裡、又在鎖定
    #   表裡的話不可以算兩次。
    _bio_slots, _batch_more = batch_biopsy_slots(inp, _order)
    _grid_days = {_d.isoformat() for _d in inp.grid}
    (_locked_keys, _locked_bx, _locked_n,
     _ord, _locked_seat) = apply_locked_adjustments(
        inp, _order, _bio_slots)

    solved_batch_ids: set = set()
    overlap_days: dict = {}               # {(勝者id, 敗者id): [最早重疊日, 最晚重疊日]}
    for d in sorted(inp.grid):
        if is_weekend(d):
            continue
        iso = d.isoformat()
        # RF-08：同日可能被多個梯次涵蓋（設定允許同週一多梯、或起始日打錯部分重疊）。
        # 維持與原 next() 相同的決定性勝者＝原始順序第一個；其餘梯次成員該日不排，
        # 累積重疊區間於迴圈後一次示警（點名被忽略的梯次與實際重疊日期）。
        covering_today = batches_on_day(_order, d)
        batch = day_owner_batch(_order, d)
        for loser in covering_today[1:]:
            rng = overlap_days.setdefault((batch.id, loser.id), [d, d])
            rng[0], rng[1] = min(rng[0], d), max(rng[1], d)
        clerk_members = batch.members if batch else []
        batch_key = batch.id if batch else ""
        if batch:
            solved_batch_ids.add(batch.id)
        pgy_set, clerk_set = set(inp.pgy_roster), set(clerk_members)
        for session in STUDENT_SESSIONS:
            locked_slots = (inp.locked.get(iso) or {}).get(session)
            if locked_slots is not None:          # 鎖定時段：保留原樣、只餵進計數
                day_slots.setdefault(iso, {})[session] = locked_slots
                _warn_locked_content(warnings, d, session, locked_slots,
                                     pgy_set, clerk_set, pgy_leave, clerk_leave)
                replay_counters(fc, d, session, locked_slots, batch_key,
                                pgy_set, clerk_set)
                log.append(f"{d.month}/{d.day}({'一二三四五六日'[d.weekday()]}) "
                           f"{session} 🔒鎖定（不重排）")
                continue
            rooms = (inp.grid.get(d) or {}).get(session) or []
            # 週三下午跟診關閉但治療室照開 → 該時段仍需跑（rooms 為 []）
            # ★向勝者梯次拿它自己的切片開放★(外審 2026-08-24 P2-01)
            biopsy = bool(((inp.biopsy_open.get(batch_key) or {}).get(iso)
                           or {}).get(session))
            # [RS-24] 配額:整梯開放時段數 ÷ 人數(取整)。★每個人切一樣多★,
            #   多出來的時段留空。開放數少於人數時取 1(排得到的人先輪,
            #   輪不到的由月底警告點名 —— 設計文件 C4 的語意)。
            #   ★跨月的梯次★:本月只看得到自己這半段的格網 → 把上月已經回放
            #   進 `biopsy_done` 的次數也算進總量,配額才不會被腰斬。
            _members = dedupe_codes(clerk_members)
            # [RS-33] ★這一格【之後】還有幾次鎖定的跟診★ —— 與切片配額的
            #   `o > _now` 完全同一個判準(上/本月已跑過的那些已經在
            #   `fc.seat` 裡,再預留一次就會扣兩次)。
            _seat_now = _ord.get((iso, session), -1)
            _seat_res = {
                c: sum(1 for o in _locked_seat.get((batch_key, c), [])
                       if o > _seat_now)
                for c in _members} if _members else None
            # [RS-34] 第二趟才有的「每人該跟幾次」。
            _seat_cap_now = ({c: seat_cap[(batch_key, c)] for c in _members
                              if (batch_key, c) in seat_cap}
                             if seat_cap else None)
            _quota: "dict | None" = None
            _force: frozenset = frozenset()
            _pos = _bio_pos.get((iso, session))
            if _members and _pos is not None:
                _cap = max(1, len(_bio_slots.get(batch_key, ()))
                           // len(_members))
                _now = _ord.get((iso, session), -1)
                # ★還沒跑到的鎖定時段要先預留★:那一次已經指派給他了,
                #   跑到那天 `replay_counters` 才會計入 —— 現在不先扣的話
                #   會多補他一次(已經跑過的則已在 `biopsy_done` 裡,不重複扣)。
                _quota = {
                    c: max(0, _cap - fc.biopsy_done.get((batch_key, c), 0)
                           - sum(1 for o in _locked_bx.get((batch_key, c), [])
                                 if o > _now))
                    for c in _members}
                # ★還缺的次數★(每人重複出現他還缺的次數)——請假會讓某些人的
                #   機會被吃掉,判準是「放掉這一格之後還配不配得完」。
                _todo = [c for c in _members for _ in range(_quota.get(c, 0))]
                # ★可行性的視野要跟配額的視野一樣長★(外審 2026-08-24 P1-01):
                #   只看本月剩下的時段,跨月梯次在本月最後一格會誤判成「誰去
                #   都行」—— 而下個月那一格他可能請假。用整梯的時段集合,
                #   取【這一格之後】的部分(含下個月),請假由 `free` 判斷。
                _now_key = (iso, STUDENT_SESSIONS.index(session)
                            if session in STUDENT_SESSIONS else 0)
                #   ★鎖定時段要扣掉★:它在分母裡(有人切),但不是還能分給
                #   別人的機會 —— 當成未來容量會高估可行性、今天挑錯人。
                _free_slots = (_bio_slots.get(batch_key, set())
                               - _locked_keys.get(batch_key, set()))
                _later = [i2 for i2, s2 in sorted(_free_slots)
                          if (i2, STUDENT_SESSIONS.index(s2)
                              if s2 in STUDENT_SESSIONS else 0) > _now_key]
                _force = _biopsy_forced_today(
                    _todo, _later,
                    # ★今天排得到他嗎★要跟正式排班同一個條件
                    #   (`_biopsy_cands`:同日早+午不得同一人)。
                    # (誠實標註:這一條的突變不會讓任何測試變紅 —— `BiopsyStep`
                    #  取的是 `_biopsy_cands ∩ biopsy_force`,已經濾掉今天切過
                    #  的人。留著是為了讓「非補不可的人」這個集合本身是對的,
                    #  不是為了防漏;不硬湊一個假的反例來讓它看起來被測到。)
                    [c for c in _quota
                     if _quota[c] > 0
                     and d not in (clerk_leave.get(c) or set())
                     and fc.last_biopsy.get((batch_key, c)) != d],
                    lambda c, i2: (date.fromisoformat(i2)
                                   not in (clerk_leave.get(c) or set())))
            slots, slog = solve_session(
                d, session, rooms,
                _avail(inp.pgy_roster, pgy_leave, d),
                _avail(clerk_members, clerk_leave, d),
                biopsy, fc, inp.capacity, batch_key=batch_key,
                apply_pref=frozenset(inp.apply_pref), two_pgy_mode=two_pgy,
                biopsy_quota_left=_quota, biopsy_force=_force,
                seat_reserved=_seat_res, seat_cap=_seat_cap_now)
            day_slots.setdefault(iso, {})[session] = slots
            log.append(f"{d.month}/{d.day}({'一二三四五六日'[d.weekday()]}) "
                       + "；".join(slog))
            warnings.extend(f"{d.month}/{d.day} {ln.lstrip('⚠ ')}"
                            for ln in slog if ln.startswith("⚠"))

    # RF-02：鎖定時段的日期若事後掉出開診格網（假日/週末），主迴圈迭代不到 → 在此
    # 一律原樣補回輸出並人話警告，絕不因格網變動而無聲刪除鎖定內容（不餵計數：該日
    # 實際休診，餵計數會扭曲治療室/放假公平輪轉）。
    for iso, sessions in sorted(inp.locked.items()):
        for session, slots in sessions.items():
            if session not in day_slots.get(iso, {}):
                day_slots.setdefault(iso, {})[session] = slots
                warnings.append(f"{iso} {session} 🔒鎖定時段不在本月開診格網"
                                f"（假日/週末？），已原樣保留，請確認是否解鎖")
                log.append(f"{iso} {session} 🔒鎖定時段不在開診格網，原樣保留")

    # RF-08：梯次重疊 → 點名被忽略的梯次與實際重疊日期（協助定位打錯的起始日）。
    for (win, lose), (d1, d2) in sorted(overlap_days.items()):
        warnings.append(
            f"梯次重疊：{d1.isoformat()}～{d2.isoformat()} 由梯次 {win} 與 {lose} "
            f"同時涵蓋，重疊日只採 {win}，{lose} 成員該期間不會被排班——請修正梯次起始日")

    # 切片室配額點名:只對「本月確有工作日被排」的梯次示警(否則邊界梯次會誤報)。
    # ★判準與 service 的現況檢查共用★(RS-28)—— 見 `biopsy_quota_warnings`。
    _bw, _ = biopsy_quota_warnings(
        inp.clerk_batches, fc.biopsy_done, batch_more=_batch_more,
        only_ids=solved_batch_ids)
    warnings.extend(_bw)

    # [RS-33] 跟診時段落在目標區間外的點名(下限只講事實、不假裝補得出來)。
    #   `fc.seat` 的鍵是 `("clerk"|"pgy", …)`,只取 Clerk 那一半並攤平成
    #   `(梯次, 代號)` —— 與 `biopsy_quota_warnings` 同一個 counts 形狀。
    _y, _m = int(inp.ym[:4]), int(inp.ym[5:7])
    warnings.extend(clerk_seat_band_warnings(
        inp.clerk_batches,
        {(b, c): n for k, n in fc.seat.items()
         if isinstance(k, tuple) and len(k) == 3 and k[0] == "clerk"
         for _ns, b, c in (k,)},
        only_ids=solved_batch_ids,
        # ★「偏少」只在整梯走完之後才成立★:`fc.seat` 這時含上個月回放進來的
        #   同梯次次數(RF-09),所以梯次結束於本月 = 手上就是整梯的總數。
        ended_ids=clerk_batches_ended_by(
            inp.clerk_batches,
            date(_y, _m, monthrange(_y, _m)[1]))))
    return day_slots, log, warnings, fc


# ─── 週期統計（2026-07-23 使用者需求）────────────────────────────────────────
# 排班排出來之後，統計整個 course（PGY=月、Clerk=兩週梯次）每人的各類次數，
# 給 UI 側欄/報告呈現，讓「照光/治療室盡量一致、週三下午照光獨立平均、Clerk 至少
# 跟過一次切片」可被使用者直接驗證。純函式：吃 day_slots 形狀資料（含手動改過的格）。
STAT_KEYS = ("photo", "photo_wed_pm", "tx", "biopsy", "follow", "rest")


def person_course_stats(sessions_by_iso: dict, include=None,
                        start: "date | None" = None,
                        end: "date | None" = None) -> dict:
    """統計每人次數 → {code: {photo, photo_wed_pm, tx, biopsy, follow, rest}}。

    sessions_by_iso: {iso: {session: {slot: [codes]}}}（月檔 day_slots 或 preview）。
    include: 只統計這些代號（None=全部）；start/end: 只統計此日期範圍（含端點，
    Clerk 梯次跨月時由呼叫端把兩個月的 day_slots 合併餵入並以梯次起訖裁切）。
    slot 分類：照光/治療室/切片室/放假為特殊格；其餘一律視為「跟診」（房號可為任意字串）。
    壞日期鍵略過（與 storage 讀取容錯一致）。
    """
    out: dict = {}

    def bump(code, key):
        if include is not None and code not in include:
            return
        st = out.setdefault(code, dict.fromkeys(STAT_KEYS, 0))
        st[key] += 1

    for iso in sorted(sessions_by_iso or {}):
        try:
            d = date.fromisoformat(iso)
        except (ValueError, TypeError):
            continue
        if (start and d < start) or (end and d > end):
            continue
        for session, slots in (sessions_by_iso[iso] or {}).items():
            wed_pm = (d.weekday() == WED and session == "下午")
            for slot, people in (slots or {}).items():
                for p in people or []:
                    if slot == PHOTO:
                        bump(p, "photo")
                        if wed_pm:
                            bump(p, "photo_wed_pm")
                    elif slot == TREATMENT:
                        bump(p, "tx")
                    elif slot == BIOPSY:
                        bump(p, "biopsy")
                    elif slot == REST:
                        bump(p, "rest")
                    elif is_follow_slot(slot):   # ★與 service 共用同一份定義★
                        bump(p, "follow")
    return out


def format_course_stats(pgy_stats: dict, pgy_roster: list,
                        batch_stats: list) -> str:
    """把週期統計排成 monospace 文字段（給決策報告/預覽）。

    pgy_stats: person_course_stats 結果；pgy_roster: 本月 PGY 代號（沒排到也列 0）。
    batch_stats: [{"id","start","end","members","stats"}]（每梯一筆，跨月已合併）。
    """
    lines = ["【週期次數統計】",
             "  PGY（本月）：  照光  週三午照  治療室  跟診  放假"]
    for c in sorted({*pgy_roster, *pgy_stats}):
        st = pgy_stats.get(c) or dict.fromkeys(STAT_KEYS, 0)
        lines.append(f"    {c:<8s}  {st['photo']:>3d}  {st['photo_wed_pm']:>6d}"
                     f"  {st['tx']:>5d}  {st['follow']:>3d}  {st['rest']:>3d}")
    for b in batch_stats:
        lines.append(f"  Clerk 梯次 {b['id']}（{b['start']}～{b['end']}）："
                     f"切片  跟診  放假")
        for c in sorted({*b.get("members", []), *b["stats"]}):
            st = b["stats"].get(c) or dict.fromkeys(STAT_KEYS, 0)
            mark = "  ⚠未排切片" if st["biopsy"] == 0 else ""
            lines.append(f"    {c:<8s}  {st['biopsy']:>3d}  {st['follow']:>3d}"
                         f"  {st['rest']:>3d}{mark}")
    if not batch_stats:
        lines.append("  Clerk：本月無梯次")
    return "\n".join(lines)
