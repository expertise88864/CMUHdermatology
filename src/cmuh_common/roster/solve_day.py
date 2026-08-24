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
from dataclasses import dataclass, field
from datetime import date

from cmuh_common.roster.model import (
    STUDENT_SESSIONS, dedupe_codes, is_weekend,
)

PHOTO = "照光"        # 每時段必排 1 PGY（含週三下午），最優先
TREATMENT = "治療室"  # 每時段 1 PGY，但週三下午休診不排
BIOPSY = "切片室"
REST = "放假"
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


def _seat(ctx, pool, room, ck, prefer: frozenset = frozenset()):
    """依 ck(人)命名空間的座位公平計數輪選並就座。

    key＝(今日尚無工作, 座位次數, 非偏好者, 該房次數, 連排懲罰, 同伴次數,
    抖動, 代號)：
    [2026-07-27 使用者] 首鍵＝「今天還沒有任何工作的人最優先」——目標是每人每天
    至少有半天有事做,不要早上放假下午又放假。放在最前面才有效（放在次數之後等於
    幾乎不生效：早上放假的人座位次數本來就較低,原本就會被選,失效的正是「次數偏高
    但整天閒著」這個真正要救的情形）。不會破壞長期公平：早上放假者當日至多補到 1
    個位子,仍少於整天有課者的 2 個,座位次數自我校正（實測 spread 仍 ≤1）。
    早上時段人人皆「今日尚無工作」→ 此鍵在上午不生效,上午行為與舊版相同。
    次之跟診次數最少者優先（★公平＝使用者 2026-08-24 的主要要求★）；
    ★這一鍵刻意【只看跟診】★:切片室的次數由配額保證一樣多(見
    `BiopsyStep`),所以「跟診也一樣多」就等於「總量也一樣多」——
    把總量混進來反而會讓剛切完片的人被擠出座位,越補越不平。
    平手時 prefer 先上（Apply 本科 101 週二/五）；
    [2026-07-24] 再比「跟過這間診的次數」少者先、罰「上一次跟診就是這間」（反連排）；
    [2026-07-25 使用者] 再比「與本房已就座者共事過幾次」少者先——房多樣性只管
    「誰跟哪一間」,管不到「誰跟誰」,故仍可能固定同兩人成對（如 1、2 號總是一起）。
    最後決定性抖動打散。"""
    rk = str(room).strip()
    fc = ctx.fc
    seated = ctx.seat_ck.setdefault(room, [])   # [codex R1] 房內既有者的真實 ck

    def _key(p):
        k = ck(ctx, p)
        pair_cost = sum(fc.pair.get(_pair_key(k, q), 0) for q in seated)
        return (_idle_today(fc, k, ctx.d),   # ★今天還沒事做的人最優先（反整天放假）
                fc.seat.get(k, 0),
                0 if p in prefer else 1,
                fc.seat_room.get((k, rk), 0),
                1 if fc.last_seat_room.get(k) == rk else 0,
                pair_cost,
                _jitter(ctx.d, ctx.session, "seat", p), p)
    pick = min(pool, key=_key)
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
                _seat(ctx, ctx.pgy, r, _pgy_ck, prefer=ctx.room_pref(r))


class ClerkSeedStep(FillStep):
    def run(self, ctx, slots, log):
        for r in _room_order(ctx):
            if not ctx.clerk:
                break
            # [RS-15] 容量檢查:TwoPgySeatStep 可能已先坐了 PGY —— 本步原本
            # 假設自己是第一個入座者(房必空),盲塞會把容量 1 的房坐成 2 人。
            # 非兩位 PGY 月時房仍為空,此檢查恆真、行為不變。
            if len(ctx.room_slots[r]) < ctx.capacity:
                _seat(ctx, ctx.clerk, r, _clerk_ck)


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
                _seat(ctx, ctx.clerk, r, _clerk_ck)


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
                  biopsy_force=frozenset()) -> tuple:
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
        biopsy_force=frozenset(biopsy_force))
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
    biopsy_open: dict = field(default_factory=dict)   # {iso: {session: bool}}
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
    """
    return _solve_month_once(inp)[:3]


def _solve_month_once(inp: DaySolveInput) -> tuple:
    """單趟整月填充 → (day_slots, log, warnings, fc)。fc 供測試檢視公平計數。"""
    fc = FairCounters()
    day_slots: dict = {}
    log: list = []
    warnings: list = []
    pgy_leave = (inp.leaves.get("pgy") or {})
    clerk_leave = (inp.leaves.get("clerk") or {})
    # [RS-15] 判準=該月 PGY 名單恰 2 位(去重;不看當日可用人數 —— 請假造成
    # 的臨時 2 人不算,名單就是 2 人的月份整月一致啟用,行為可預期)。
    two_pgy = len({str(p) for p in inp.pgy_roster}) == 2

    # RF-09：先把上月跨月梯次的既存班表餵進 fc（只餵切片室與 clerk 座位/放假；跳過
    # 治療室與上月 PGY，避免污染本月 PGY 月度公平），讓「本梯未輪過切片」的判定與月底
    # missed 警告都以「整梯」而非「本月」為單位。
    for iso in sorted(inp.prior_sessions):
        try:
            d = date.fromisoformat(iso)
        except (ValueError, TypeError):
            continue
        batch = next((b for b in inp.clerk_batches if b.covers(d)), None)
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
        _cov = [b for b in inp.clerk_batches if b.covers(_d)]
        if not _cov:
            continue
        for _s in STUDENT_SESSIONS:
            if (inp.locked.get(_iso) or {}).get(_s) is not None:
                continue
            if _d.weekday() == WED and _s == "下午":
                continue
            if not (inp.biopsy_open.get(_iso) or {}).get(_s):
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
    _bio_slots: dict = {}               # {梯次: {(日期, 時段)}}
    _grid_days = {_d.isoformat() for _d in inp.grid}
    _grid_last = max(inp.grid) if inp.grid else None
    # ★這一梯【之後】還有沒有排得到的時段★(外審 Codex 配額版 R2):
    #   跨月梯次到了第二個月一定看得到上個月的日期,若拿「有沒有格網外的
    #   日期」當判準,最終那一次的差異就永遠不會被點名。要看的是【未來】。
    _batch_more: dict = {}
    for _iso, _sess in (inp.biopsy_open or {}).items():
        try:
            _bd = date.fromisoformat(_iso)
        except (ValueError, TypeError):
            continue
        if is_weekend(_bd) or _bd in (inp.holidays or set()):
            continue        # ★假日不會出現在任何月份的格網裡 → 不是可排的量★
        _bcov = [b for b in inp.clerk_batches if b.covers(_bd)]
        if not _bcov:
            continue
        for _s, _on in (_sess or {}).items():
            if not _on or _s not in STUDENT_SESSIONS:
                continue
            if _bd.weekday() == WED and _s == "下午":
                continue                # 週三下午恆關
            _bio_slots.setdefault(_bcov[0].id, set()).add((_iso, _s))
            if (_iso not in _grid_days and _grid_last is not None
                    and _bd > _grid_last):
                _batch_more[_bcov[0].id] = True
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
    #   ★鎖定時段一律先從分母拿掉★(外審 Codex 配額版 R2):它不重排,所以
    #   「本來標示開放」不代表排得到人 —— 只有那一格【確實指派給本梯成員】
    #   時才把它加回去(那個人真的會切一次)。空的鎖定格、或指派給未知/
    #   已換梯代號的鎖定格,都不是可分配的量。
    _ord: dict = {}                     # (iso, 時段) → 全月的先後順序
    for _i, _d in enumerate(sorted(inp.grid)):
        for _j, _s in enumerate(STUDENT_SESSIONS):
            _ord[(_d.isoformat(), _s)] = _i * 10 + _j
    for _iso, _sessions in (inp.locked or {}).items():
        try:
            _ld = date.fromisoformat(_iso)
        except (ValueError, TypeError):
            continue
        _lcov = [b for b in inp.clerk_batches if b.covers(_ld)]
        if not _lcov:
            continue
        if _iso not in _grid_days:
            # ★掉出格網的鎖定時段只原樣保留,不餵任何計數★(RF-02 的契約):
            #   那一天在這個月根本沒有開診(假日/週末)—— 主迴圈永遠不會處理它,
            #   算進配額分母就會把 cap 撐高成排不完的量。
            continue
        _lmembers = set(dedupe_codes(_lcov[0].members))
        for _s, _slots in (_sessions or {}).items():
            if _s not in STUDENT_SESSIONS:
                continue
            _bio_slots.setdefault(_lcov[0].id, set()).discard((_iso, _s))
            for _c in ((_slots or {}).get(BIOPSY) or []):
                if str(_c) not in _lmembers:
                    # ★未知/已換梯的代號不得污染公平計數★(RF-10 的契約;
                    #   `replay_counters` 也是這樣濾的)—— 算進分母會把配額
                    #   撐大,自動排班就會多排,反而讓現役成員的次數不一致。
                    continue
                _locked_bx.setdefault((_lcov[0].id, str(_c)), []).append(
                    _ord.get((_iso, _s), -1))
                _locked_n[_lcov[0].id] = _locked_n.get(_lcov[0].id, 0) + 1
                _bio_slots.setdefault(_lcov[0].id, set()).add((_iso, _s))

    solved_batch_ids: set = set()
    overlap_days: dict = {}               # {(勝者id, 敗者id): [最早重疊日, 最晚重疊日]}
    for d in sorted(inp.grid):
        if is_weekend(d):
            continue
        iso = d.isoformat()
        # RF-08：同日可能被多個梯次涵蓋（設定允許同週一多梯、或起始日打錯部分重疊）。
        # 維持與原 next() 相同的決定性勝者＝原始順序第一個；其餘梯次成員該日不排，
        # 累積重疊區間於迴圈後一次示警（點名被忽略的梯次與實際重疊日期）。
        covering_today = [b for b in inp.clerk_batches if b.covers(d)]
        batch = covering_today[0] if covering_today else None
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
            biopsy = bool((inp.biopsy_open.get(iso) or {}).get(session))
            # [RS-24] 配額:整梯開放時段數 ÷ 人數(取整)。★每個人切一樣多★,
            #   多出來的時段留空。開放數少於人數時取 1(排得到的人先輪,
            #   輪不到的由月底警告點名 —— 設計文件 C4 的語意)。
            #   ★跨月的梯次★:本月只看得到自己這半段的格網 → 把上月已經回放
            #   進 `biopsy_done` 的次數也算進總量,配額才不會被腰斬。
            _members = dedupe_codes(clerk_members)
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
                _later = [i2 for i2, s2, b2 in _bio_seq[_pos + 1:]
                          if b2 == batch_key]
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
                biopsy_quota_left=_quota, biopsy_force=_force)
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

    # 切片室輪不到：只對「本月確有工作日被排」的梯次示警（否則邊界梯次會誤報）
    for b in inp.clerk_batches:
        if b.id not in solved_batch_ids:
            continue
        counts = {c: fc.biopsy_done.get((b.id, c), 0) for c in sorted(b.members)}
        missed = [c for c, n in counts.items() if n == 0]
        if missed:
            warnings.append(f"切片室輪不到（梯次 {b.id}，本梯內未排到）："
                            + "、".join(missed))
        # [RS-24] ★配額制下「次數一樣」是要求 → 任何不一致都要點名★
        #   (外審 Codex 配額版 P2:門檻還停在舊 min-first 容許的 >1)。
        #   ★但「這一梯之後還排得到」時放寬★:跨月梯次在第一個月本來就只
        #   排得到一半,那時的差異不是異常(下個月會補齊)——那種情況維持舊的
        #   >1 門檻,免得每個月都跳一次噪音;到了最後一個月就要嚴格。
        elif counts and (
                max(counts.values()) - min(counts.values())
                > (1 if _batch_more.get(b.id) else 0)):
            warnings.append(
                f"切片室次數不均（梯次 {b.id}，同梯應盡量一致）："
                + "、".join(f"{c}×{n}" for c, n in counts.items())
                + " —— 多因請假/鎖定時段所致，可手動於月曆調整")
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
                    else:
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
