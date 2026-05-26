# -*- coding: utf-8 -*-
"""UVB 自動調整劑量邏輯。

F2/F3 熱鍵觸發時：
  1. 讀 主窗「處置」TMemo 文字
  2. 找第一行含 UVB 的內容 (最上面)
  3. parse: dose / count / last_date / increase / max
  4. 依「今天 vs last_date」天數差套用劑量調整規則
  5. 覆蓋寫回該行 (count+1, date→today, dose 依規則)

【規則】依「今天 − last_date」天數差：
    0-1 天 → 太密集，跳警告終止 (F2/F3 不繼續跑 51019)
    2-6 天 → dose + increase, cap MAX
    = 7 天 → 保持 dose 不變
    8-14 天 → dose × 0.75, floor 到 10 的倍數 (435→430, 432→430)
    > 14 天 → 固定 250

【格式範例】
    UVB 520mj/cm2  (11) on  (2026/05/26)  , increase 30mj/cm2 if no erythema , MAX:800 mj/cm2 , W2, W5M

【容錯】
    - dose / increase / max 可能無單位「mj/cm2」(只看數字)
    - 日期 (YYYY/MM/DD) 月日可有/無零填充
    - 多餘空白忽略
    - MAX 後面可能有 ":" 或沒有
    - W2 / W5M 等後綴一律保留不動
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional


# ─── Constants ──────────────────────────────────────────────────────────
# [v20.10 2026-05-26] 劑量規則修正 (user 確認 1 天差也要警告):
#   days_diff = 0 (同日) / 1 (昨日)  → 警告終止 (至少要間隔 1 天 = ≥ 2 天)
#   days_diff = 2-6                   → +increase (cap MAX)
#   days_diff = 7 (剛好)               → 保持 dose
#   days_diff = 8-14 (含14)           → × 0.75, floor 10, 最低 250
#   days_diff = 15-21 (含21)          → × 0.5, floor 10, 最低 250
#   days_diff > 21                    → 固定 250
TOO_CLOSE_DAYS = 2            # days_diff < 此值 → 警告終止 (即 0 或 1 天)
SAME_DOSE_DAYS = 7            # = 此值 → 保持
DECAY_75_UPPER = 14           # 8-14 → ×0.75
DECAY_50_UPPER = 21           # 15-21 → ×0.5
LONG_GAP_DOSE = 250           # > 21 → 250
MIN_DECAY_DOSE = 250          # decay 結果不低於此值 (floor)

DECAY_75_FACTOR = 0.75
DECAY_50_FACTOR = 0.5

# ─── Sanity bounds (拒絕 parse 出來明顯異常的值) ────────────────────────
# [v20.5 2026-05-26] 為了「確保資訊正確」，parse 出來的值若超出合理範圍 →
# 直接 sanity_fail，給 caller 跳警告終止，不嘗試自動計算。
MIN_DOSE = 50              # UVB 劑量正常 200-1500 mj/cm2，給寬點 50 為下限
MAX_DOSE = 1500            # [v20.12] 上限改回 1500，超過跳 Yes/No 確認
MAX_COUNT = 999            # 治療次數不該超過 999 (~5 年週週照)
MAX_GAP_DAYS = 730         # 距上次照光超過 2 年 → 異常 (病歷可能跑掉)


# ─── Action enum ────────────────────────────────────────────────────────
class UvbAction:
    """F2/F3 對應的 UVB 處理動作 (字串 enum)。

    [v20.5 2026-05-26] 為了「確保資訊正確」，每種 uncertain case 都拿到一個
    明確的 action，caller (main.py) 依此決定要警告終止或繼續。
    現在只有 UPDATED 才會繼續走 51019，其他全部都要 stop+prompt。
    """
    NO_UVB_LINE = "no_uvb_line"          # 處置內沒 UVB 行 → 警告 (F2/F3 不該沒 UVB)
    PARSE_FAIL = "parse_fail"            # 有 UVB 但格式怪 → 警告
    TOO_CLOSE = "too_close"              # 0-1 天 → 警告
    SANITY_FAIL = "sanity_fail"          # parse 出來的值超出合理範圍 → 警告
    CONFIRM_NEEDED = "confirm_needed"    # [v20.12] dose 超過 MAX_DOSE → Yes/No 確認
    UPDATED = "updated"                   # 正常更新 (唯一繼續走 51019 的 case)


# ─── Parse result ───────────────────────────────────────────────────────
@dataclass
class UvbLineInfo:
    """parse 出來的單行 UVB 結構。

    [v20.7] count 變 Optional — 處置可能沒寫 (N) 次數欄位 (醫師選擇不記)。
    沒 count → 不更新 count，dose/date 仍要更新。
    """
    full_match: str       # 完整原始行內容 (含 line ending 之前的文字)
    dose: int             # 原劑量
    count: Optional[int]  # 原 (N) 次數，None=處置沒寫
    last_date: date       # 原日期
    increase: int         # increase 後的數字
    max_dose: int         # MAX 後的數字
    span: tuple[int, int] # 在 source text 中的 (start, end) char offset


# [v20.6 2026-05-26] 從「整段 regex」改成「獨立 field 解析」
# 原本 _UVB_LINE_RE 一條 regex 要求 dose/count/on(date)/increase/MAX 順序固定，
# 但實機 data 順序千變萬化:
#   Case 1: "UVB 1000 on (date), (count), increase N"  ← date 先於 count
#   Case 2: "UVB: 1200 mj/cm2已打折(137) on (date)"   ← 數字後夾中文再 (count)
#   Case 3: "UVB: 950 (39) on (date) add 50 each"     ← 用 add 取代 increase
# 一條 regex 撐不下這些變異，改成：
#   1. 找 UVB 起點 → 找 MAX:N 終點 → segment 範圍
#   2. 在 segment 內**各別**找 dose / date / count / increase
#   3. 各 field 順序不限，缺任一 field → parse_fail

_UVB_DOSE_RE = re.compile(r"UVB\s*[:：]?\s*(\d+)", re.IGNORECASE)
# [v20.11] 接受帶 paren 跟不帶 paren 兩種:
#   (2026/05/24) — group 1-3
#    2026/05/24  — group 4-6
_UVB_DATE_RE = re.compile(
    r"\(\s*(\d{4})/(\d{1,2})/(\d{1,2})\s*\)"
    r"|"
    r"\b(\d{4})/(\d{1,2})/(\d{1,2})\b"
)
# count: \(\s*\d+\s*\) — 任何 paren 內純數字。
# 為了不抓到日期 (年是 4 位)，caller 會先 mask date span 再 search。
# 大於 MAX_COUNT 的會在 sanity check 時擋下，這裡先放寬接受任意位數。
# [v20.7] 也排除「年」可能性 — 4 位數字當 count 機率極低，先排除避免誤抓
_UVB_COUNT_RE = re.compile(r"\(\s*(\d+)\s*\)")
# increase / increased / add (case-insensitive)
_UVB_INCREASE_RE = re.compile(
    r"(?:increase[d]?|add)\s*(\d+)", re.IGNORECASE)
# [v20.8] MAX 接受多種同義表達:
#   MAX:N / MAX N / fix N / fixed N / fix at N / fixed at N
# \bfix(?:ed)? 確保 word boundary 避免抓到 "prefix"/"fixing" 等
_UVB_MAX_RE = re.compile(
    r"(?:MAX\s*[:：]?\s*|\bfix(?:ed)?(?:\s+at)?\s+)(\d+)",
    re.IGNORECASE,
)

# [v20.12 2026-05-26] 同日期 triplet 偵測 — 用於更新非 UVB 關鍵字 (e.g. excimer
# light) 但日期相同的 (count) ... (date) 三元組。
# 例如:
#   excimer light (25) 1000mJ for nape on (2026/5/25)
#   1500mj/cm2 (44) on (2026/5/25) add 50 each time, fixed at 1500
# 中間限制無括號避免跨越獨立 segment。
_TRIPLET_RE = re.compile(
    r"\(\s*(\d{1,3})\s*\)"                         # (count)
    r"([^()]{0,120}?)"                              # 中間 (no parens)
    r"\(\s*(\d{4})/(\d{1,2})/(\d{1,2})\s*\)"        # (YYYY/MM/DD)
)
# Triplet 周圍需要的 UVB-相關標記 (避免誤動其他內容如「(10) days for ...」)
# 「\d\s*mj」要求數字接 mJ/mj (劑量單位)，比單純 "mj" 更安全
_UVB_MARKER_RE = re.compile(
    r"(?:uvb|excimer|\d\s*mj|phototherapy|photo\s*therapy)",
    re.IGNORECASE,
)


def parse_uvb_line(text: str) -> Optional[UvbLineInfo]:
    """從 text 中找第一個 UVB 行，回 UvbLineInfo 或 None。

    [v20.6] 獨立 field 解析 — 順序不限、容忍中文夾雜、increase/add 等同義。

    解析步驟：
      1. UVB 起點: `UVB\\s*:?\\s*(\\d+)` 找到 dose
      2. MAX 終點: `MAX\\s*:?\\s*(\\d+)` 找到 max_dose (UVB 後第一個 MAX)
      3. segment = text[uvb_start:max_end]
      4. 在 segment 內各別找:
         - date: `\\(\\s*\\d{4}/\\d{1,2}/\\d{1,2}\\s*\\)`
         - count: 不重疊 date 的 `\\(\\s*\\d{1,3}\\s*\\)`
         - increase: `(increase[d]?|add)\\s*\\d+`
      5. 任一 field 缺 → 回 None (parse_fail)
    """
    if "uvb" not in text.lower():
        return None

    # 1. UVB dose
    dose_m = _UVB_DOSE_RE.search(text)
    if not dose_m:
        return None
    try:
        dose = int(dose_m.group(1))
    except ValueError:
        return None
    start = dose_m.start()

    # 2. MAX (從 UVB 之後找)
    max_m = _UVB_MAX_RE.search(text, start)
    if not max_m:
        return None
    try:
        max_dose = int(max_m.group(1))
    except ValueError:
        return None
    end = max_m.end()
    segment = text[start:end]
    # 相對 segment 的 span
    rel_start = 0
    rel_end = end - start

    # 3. Date (segment 內第一個 YYYY/MM/DD，paren 可省)
    date_m = _UVB_DATE_RE.search(segment)
    if not date_m:
        return None
    # [v20.11] regex 有兩組: 1-3 (帶 paren) / 4-6 (不帶 paren)
    y = date_m.group(1) or date_m.group(4)
    m = date_m.group(2) or date_m.group(5)
    d = date_m.group(3) or date_m.group(6)
    try:
        last_date = date(int(y), int(m), int(d))
    except (ValueError, TypeError):
        return None

    # 4. Count (segment 內第一個數字 paren，排除 date 範圍)
    # [v20.7] count 變 Optional — 沒 (N) 處置仍可更新 dose/date
    seg_masked = (
        segment[:date_m.start()]
        + " " * (date_m.end() - date_m.start())
        + segment[date_m.end():]
    )
    count_m = _UVB_COUNT_RE.search(seg_masked)
    count: Optional[int] = None
    if count_m:
        try:
            count = int(count_m.group(1))
        except ValueError:
            count = None

    # 5. Increase / add
    inc_m = _UVB_INCREASE_RE.search(segment)
    if not inc_m:
        return None
    try:
        increase = int(inc_m.group(1))
    except ValueError:
        return None

    return UvbLineInfo(
        full_match=segment,
        dose=dose,
        count=count,
        last_date=last_date,
        increase=increase,
        max_dose=max_dose,
        span=(start, end),
    )


# ─── 劑量計算 ────────────────────────────────────────────────────────────
def _floor_to_10(value: float) -> int:
    """下十推到 10 倍數: 432→430, 435→430, 437.5→430"""
    return int(math.floor(value / 10) * 10)


def compute_new_dose(*, dose: int, increase: int, max_dose: int,
                     days_diff: int) -> Optional[int]:
    """[v20.10] 依天數差算新劑量。

    days_diff < 2 (即 0 同日 或 1 昨日) → 回 None (caller 跳警告)
    其他天數差一定有 int 回值。所有 decay 結果不低於 MIN_DECAY_DOSE (250)。
    """
    if days_diff < TOO_CLOSE_DAYS:                  # 0 或 1 天 → 太密集
        return None
    if days_diff < SAME_DOSE_DAYS:                  # 2-6 天 → +increase, cap MAX
        return min(dose + increase, max_dose)
    if days_diff == SAME_DOSE_DAYS:                 # 7 天剛好 → 保持
        return dose
    if days_diff <= DECAY_75_UPPER:                 # 8-14 天 → ×0.75
        decayed = _floor_to_10(dose * DECAY_75_FACTOR)
        return max(decayed, MIN_DECAY_DOSE)
    if days_diff <= DECAY_50_UPPER:                 # 15-21 天 → ×0.5
        decayed = _floor_to_10(dose * DECAY_50_FACTOR)
        return max(decayed, MIN_DECAY_DOSE)
    return LONG_GAP_DOSE                            # > 21 天 → 250


# ─── 寫回行內容 ──────────────────────────────────────────────────────────
def format_uvb_line(original: UvbLineInfo, *, new_dose: int,
                    new_count: Optional[int], today: date) -> str:
    """產生新的 UVB 行內容，shape 維持跟 original 一樣。

    替換 dose / count / date 三個值，其餘 (mj/cm2 / on / increase X / MAX:Y /
    W2 / W5M 等後綴) 全部保留。

    [v20.7] new_count=None → 不替換 count (處置原本就沒寫 (N))。
    """
    # 從 original.full_match 抓出原本的 3 個欄位字串位置 → 用 str.replace 替換
    # 不用 regex 替換是因為要保持其他空白格式
    src = original.full_match

    # 1. 替換 dose：找原 dose 數字第一次出現 (在 UVB 之後)
    #    使用 regex 因為要對齊「UVB 520」這個 pattern，不能誤改 "(11)" 的 11
    #    [v20.2] 允許「UVB:」冒號 — 跟 parse regex 一致
    src = re.sub(
        r"(UVB\s*[:：]?\s*)" + str(original.dose) + r"(\s*(?:mj/cm2)?)",
        lambda mo: f"{mo.group(1)}{new_dose}{mo.group(2)}",
        src,
        count=1,
        flags=re.IGNORECASE,
    )

    # 2. 替換 count: (N) → (N+1) — 僅當原本有 count 且傳入 new_count
    if original.count is not None and new_count is not None:
        src = re.sub(
            r"\(\s*" + str(original.count) + r"\s*\)",
            f"({new_count})",
            src,
            count=1,
        )

    # 3. 替換日期 — 用零填充格式 YYYY/MM/DD
    # [v20.11] 原日期可能帶或不帶 paren — 偵測原本格式 → 同樣格式寫回
    today_str = f"{today.year}/{today.month:02d}/{today.day:02d}"
    old_y = original.last_date.year
    old_m = original.last_date.month
    old_d = original.last_date.day
    with_paren_re = rf"\(\s*{old_y}/0?{old_m}/0?{old_d}\s*\)"
    if re.search(with_paren_re, src):
        # 帶 paren: (2026/5/24) → (2026/05/26)
        src = re.sub(with_paren_re, f"({today_str})", src, count=1)
    else:
        # 不帶 paren: 2026/5/24 → 2026/05/26 (注意 word boundary 避免誤改其他數字)
        bare_re = rf"\b{old_y}/0?{old_m}/0?{old_d}\b"
        src = re.sub(bare_re, today_str, src, count=1)

    return src


# ─── 主入口 ──────────────────────────────────────────────────────────────
@dataclass
class UvbUpdateResult:
    """處理結果，給 caller 決定後續流程。"""
    action: str                          # UvbAction.*
    new_text: Optional[str] = None       # action=UPDATED 時的整段處置新 text
    new_dose: Optional[int] = None       # 新劑量
    new_count: Optional[int] = None      # 新次數
    last_date: Optional[date] = None     # 原日期 (給警告 dialog 顯示)
    days_diff: Optional[int] = None      # 天數差
    parsed: Optional[UvbLineInfo] = None # 原 parse 結果 (debug 用)
    sanity_reason: Optional[str] = None  # action=SANITY_FAIL 時的失敗原因 (給警告顯示)
    confirm_reason: Optional[str] = None # [v20.12] action=CONFIRM_NEEDED 時的提問內容
    uvb_line_count: int = 0              # 處置內有幾行 UVB (≥2 給 info log)
    additional_lines_updated: int = 0    # [v20.11] 同日期額外更新的 UVB 行數
    additional_triplets_updated: int = 0 # [v20.12] 同日期非 UVB 關鍵字 triplet 額外更新數
    # [v20.13] 偵測到的「不確定 / 需要醫師確認」其他 triplet (e.g. line 1 是
    # excimer 但日期跟第一行 UVB 不同)。caller 應該跳 Yes/No 詢問是否套用。
    uncertain_other_triplets: Optional[list] = None


def _count_uvb_lines(text: str) -> int:
    """數處置 text 內有幾行 UVB (粗略 — 每行算一次)。"""
    return sum(1 for ln in text.splitlines() if "uvb" in ln.lower())


def _detect_uncertain_triplets(text: str, today: date,
                                max_days_ago: int = 365) -> list:
    """[v20.13] 偵測 text 中「看起來像 UVB/excimer 但日期不同於今天」的 triplet。

    使用情境：update_uvb_in_text 第一行 UVB 已更新，同日期 triplet 也更新後，
    剩下的 (count) ... (date) 若有 UVB-marker 又日期合理 (近 1 年內)，
    視為「不確定該不該更新」，caller 應該跳 Yes/No 詢問醫師。

    Returns list of dicts:
        [{'line': str, 'count': int, 'date': date, 'days_ago': int,
          'span': (start, end), 'original_seg': str, 'replacement': str}, ...]
        replacement 內含 count+1, date→today (供 caller Yes 時 apply)。
    """
    out = []
    today_str = f"{today.year}/{today.month:02d}/{today.day:02d}"
    for m in _TRIPLET_RE.finditer(text):
        try:
            seg_date = date(int(m.group(3)), int(m.group(4)),
                            int(m.group(5)))
        except (ValueError, TypeError):
            continue
        # 今天的 date (已經被 step A/B/C 更新) 跳過
        if seg_date == today:
            continue
        # 未來日期跳過 (異常)
        if seg_date > today:
            continue
        # 太舊跳過 (> 1 年 — 歷史紀錄)
        days_ago = (today - seg_date).days
        if days_ago > max_days_ago:
            continue
        # marker 必須同行 (不跨 newline) — 避免誤抓不相關內容
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        if line_end == -1:
            line_end = len(text)
        line_text = text[line_start:line_end]
        if not _UVB_MARKER_RE.search(line_text):
            continue
        try:
            old_count = int(m.group(1))
        except ValueError:
            continue
        if not (1 <= old_count <= MAX_COUNT):
            continue
        # 構造 "Yes 時" 套用的新 segment: count+1, date→today
        rep = m.group(0)
        rep = re.sub(
            r"\(\s*" + str(old_count) + r"\s*\)",
            f"({old_count + 1})", rep, count=1)
        rep = re.sub(
            rf"\(\s*{seg_date.year}/0?{seg_date.month}/0?{seg_date.day}\s*\)",
            f"({today_str})", rep, count=1)
        out.append({
            'line': line_text.strip(),
            'count': old_count,
            'date': seg_date,
            'days_ago': days_ago,
            'span': m.span(),
            'original_seg': m.group(0),
            'replacement': rep,
        })
    return out


def apply_uncertain_updates(text: str, triplets: list) -> str:
    """[v20.13] 將 _detect_uncertain_triplets 偵測到的 triplet 套用 (count+1,
    date→today)，回新 text。

    end-to-start 套用避免 offset 失效。
    """
    if not triplets:
        return text
    out = text
    # span 從後往前套
    for t in sorted(triplets, key=lambda x: x['span'][0], reverse=True):
        s, e = t['span']
        out = out[:s] + t['replacement'] + out[e:]
    return out


def update_uvb_in_text(text: str, today: Optional[date] = None,
                       skip_dose_sanity: bool = False) -> UvbUpdateResult:
    """主入口：給整段「處置」text，回更新後 text + 動作類型。

    today=None 用今天日期；測試時傳 fixed date 方便 reproducible。

    [v20.5 2026-05-26] 加 sanity check —「確保資訊正確，不確定就停下來」：
      - parse 後驗證 dose/count/max/days_diff 都在合理範圍
      - 寫回後 round-trip verify (重新 parse 新 text → 預期值是否一致)
      - 任一不符 → 回 SANITY_FAIL 給 caller 警告

    [v20.12 2026-05-26] dose 上限改回 1500, 但 dose 或 MAX 超過 1500 改成回
    CONFIRM_NEEDED — caller 跳 Yes/No dialog，按 Yes 後以 skip_dose_sanity=True
    重 call 跳過上限檢查繼續執行。
    新增同日期 triplet 偵測 — 處置內非 UVB-關鍵字 (如 excimer light) 但 (count)
    on (date) 日期跟第一行 UVB 相同的，count+1, date→today 一併更新。
    """
    if today is None:
        today = date.today()

    uvb_lines = _count_uvb_lines(text)

    parsed = parse_uvb_line(text)
    if parsed is None:
        # 沒含 UVB or parse 失敗
        if "UVB" in text.upper():
            return UvbUpdateResult(action=UvbAction.PARSE_FAIL,
                                   uvb_line_count=uvb_lines)
        return UvbUpdateResult(action=UvbAction.NO_UVB_LINE,
                               uvb_line_count=uvb_lines)

    # ─── CONFIRM_NEEDED check (v20.12) ──────────────────────────────────
    # 原劑量或 MAX 超過建議上限 → caller 跳 Yes/No 確認
    # caller 按 Yes 後 skip_dose_sanity=True 重 call → 走下面的 sanity (略過上限)
    if not skip_dose_sanity:
        if parsed.dose > MAX_DOSE or parsed.max_dose > MAX_DOSE:
            return UvbUpdateResult(
                action=UvbAction.CONFIRM_NEEDED,
                confirm_reason=(
                    f"原劑量 {parsed.dose} mj/cm2 或 MAX {parsed.max_dose} "
                    f"超過建議上限 {MAX_DOSE} mj/cm2"),
                last_date=parsed.last_date,
                parsed=parsed, uvb_line_count=uvb_lines,
            )

    # ─── Sanity checks on parsed values ─────────────────────────────────
    # 下限永遠檢查；上限只在非 skip 模式才當作異常
    if parsed.dose < MIN_DOSE:
        return UvbUpdateResult(
            action=UvbAction.SANITY_FAIL,
            sanity_reason=(f"原劑量 {parsed.dose} 低於下限 {MIN_DOSE}"),
            parsed=parsed, uvb_line_count=uvb_lines,
        )
    if parsed.max_dose < MIN_DOSE:
        return UvbUpdateResult(
            action=UvbAction.SANITY_FAIL,
            sanity_reason=(f"MAX {parsed.max_dose} 低於下限 {MIN_DOSE}"),
            parsed=parsed, uvb_line_count=uvb_lines,
        )
    # count sanity (count 可能 None — 處置沒寫，跳過 sanity)
    if parsed.count is not None and (
            parsed.count <= 0 or parsed.count > MAX_COUNT):
        return UvbUpdateResult(
            action=UvbAction.SANITY_FAIL,
            sanity_reason=f"次數 ({parsed.count}) 異常 [1-{MAX_COUNT}]",
            parsed=parsed, uvb_line_count=uvb_lines,
        )
    if parsed.increase <= 0 or parsed.increase > 200:
        return UvbUpdateResult(
            action=UvbAction.SANITY_FAIL,
            sanity_reason=f"increase ({parsed.increase}) 異常 [1-200]",
            parsed=parsed, uvb_line_count=uvb_lines,
        )

    days_diff = (today - parsed.last_date).days

    # 日期在未來 → 病歷有問題
    if days_diff < 0:
        return UvbUpdateResult(
            action=UvbAction.SANITY_FAIL,
            sanity_reason=(f"上次照光日期 ({parsed.last_date}) 在未來，"
                          f"病歷可能有誤"),
            parsed=parsed, uvb_line_count=uvb_lines,
        )
    # 距上次超過 2 年 → 異常 (病歷可能跑錯病人)
    if days_diff > MAX_GAP_DAYS:
        return UvbUpdateResult(
            action=UvbAction.SANITY_FAIL,
            sanity_reason=(f"距上次照光 {days_diff} 天 (>{MAX_GAP_DAYS}天)，"
                          f"異常請確認"),
            last_date=parsed.last_date, days_diff=days_diff,
            parsed=parsed, uvb_line_count=uvb_lines,
        )

    if days_diff < TOO_CLOSE_DAYS:
        return UvbUpdateResult(
            action=UvbAction.TOO_CLOSE,
            last_date=parsed.last_date,
            days_diff=days_diff,
            parsed=parsed, uvb_line_count=uvb_lines,
        )

    new_dose = compute_new_dose(
        dose=parsed.dose, increase=parsed.increase,
        max_dose=parsed.max_dose, days_diff=days_diff,
    )
    assert new_dose is not None  # days_diff >= 2 已過 too-close 檢查

    # 新 dose sanity 再檢一次 (理論上 compute_new_dose 不會吐出怪值，這層保險)
    if new_dose < MIN_DOSE:
        return UvbUpdateResult(
            action=UvbAction.SANITY_FAIL,
            sanity_reason=f"計算出新劑量 {new_dose} 低於下限",
            parsed=parsed, days_diff=days_diff, uvb_line_count=uvb_lines,
        )
    if not skip_dose_sanity and new_dose > MAX_DOSE:
        return UvbUpdateResult(
            action=UvbAction.SANITY_FAIL,
            sanity_reason=f"計算出新劑量 {new_dose} 超過上限 {MAX_DOSE}",
            parsed=parsed, days_diff=days_diff, uvb_line_count=uvb_lines,
        )

    # [v20.7] count optional — 處置沒寫 (N) 就不更新 count，dose/date 仍更新
    new_count: Optional[int] = None
    if parsed.count is not None:
        new_count = parsed.count + 1
    new_line = format_uvb_line(parsed, new_dose=new_dose, new_count=new_count,
                               today=today)

    # ─── Step A: 套用第一行 UVB segment 替換 ─────────────────────────────
    # 後續 step B/C 都在這個 working text 上操作，避免 offset 失效。
    working = text[:parsed.span[0]] + new_line + text[parsed.span[1]:]
    first_end = parsed.span[0] + len(new_line)

    # ─── Step B: v20.11 同日期 UVB-關鍵字多行更新 ───────────────────────
    # 處置可能有多行 UVB (e.g. 不同部位獨立記)，全部 last_date 跟第一行一致的
    # 都套用 format_uvb_line (dose+count+date 全部更新)，不同日期就停。
    uvb_additional = 0
    cursor = first_end
    while True:
        rest = working[cursor:]
        next_uvb = parse_uvb_line(rest)
        if next_uvb is None:
            break
        if next_uvb.last_date != parsed.last_date:
            # 不同日期 (通常是更早的歷史紀錄) → 不動，停止
            break
        # 各別 sanity (additional segment 也要過 dose 上下限)
        if (next_uvb.dose < MIN_DOSE or next_uvb.max_dose < MIN_DOSE):
            break
        if not skip_dose_sanity and (next_uvb.dose > MAX_DOSE
                                     or next_uvb.max_dose > MAX_DOSE):
            break
        if next_uvb.increase <= 0 or next_uvb.increase > 200:
            break
        # 同日期 — 用該行自己的 dose/increase/MAX 算
        next_new_dose = compute_new_dose(
            dose=next_uvb.dose, increase=next_uvb.increase,
            max_dose=next_uvb.max_dose, days_diff=days_diff,
        )
        if next_new_dose is None or next_new_dose < MIN_DOSE:
            break
        if not skip_dose_sanity and next_new_dose > MAX_DOSE:
            break
        next_new_count = (next_uvb.count + 1
                          if next_uvb.count is not None else None)
        next_new_line = format_uvb_line(
            next_uvb, new_dose=next_new_dose, new_count=next_new_count,
            today=today)
        abs_start = cursor + next_uvb.span[0]
        abs_end = cursor + next_uvb.span[1]
        working = working[:abs_start] + next_new_line + working[abs_end:]
        uvb_additional += 1
        cursor = abs_start + len(next_new_line)

    # ─── Step C: v20.12 同日期 triplet 更新 ──────────────────────────────
    # 找剩下沒被 step A/B 更新的 (count) ... (parsed.last_date) 三元組:
    #   - 同行繼續的 UVB segment (e.g. `/ new for ... 1500mj/cm2 (44) on (date)`)
    #   - 不同光照設備 (e.g. `excimer light (25) 1000mJ ... on (date)`)
    # Step A 已經把第一行 triplet 的 date 改成 today，所以這裡掃描 working text
    # 時，第一行的 triplet 不會匹配 parsed.last_date，自動跳過。
    # 只更新 count + date，不動 segment 內 dose (continuation 通常 fixed at MAX
    # 不會變；若要 dose decay，後續再加)。
    today_str = f"{today.year}/{today.month:02d}/{today.day:02d}"
    triplet_edits = []
    for m in _TRIPLET_RE.finditer(working):
        try:
            seg_date = date(int(m.group(3)), int(m.group(4)),
                            int(m.group(5)))
        except (ValueError, TypeError):
            continue
        if seg_date != parsed.last_date:
            continue
        try:
            old_count = int(m.group(1))
        except ValueError:
            continue
        if not (1 <= old_count <= MAX_COUNT):
            continue
        # 要求 same line 內有 UVB-相關標記 (uvb/excimer/數字mj/phototherapy)
        # 不跨換行 — 避免誤動其他「(N) ... (date)」內容 (e.g. 上一行才是 UVB
        # 行，下一行的「(5) days post op」不該被算 UVB 同類項)
        line_start = working.rfind("\n", 0, m.start()) + 1   # 0 if not found
        line_end = working.find("\n", m.end())
        if line_end == -1:
            line_end = len(working)
        if not _UVB_MARKER_RE.search(working[line_start:line_end]):
            continue
        # 構造該 triplet 替換內容: count→count+1, date→today
        seg_text = m.group(0)
        seg_text = re.sub(
            r"\(\s*" + str(old_count) + r"\s*\)",
            f"({old_count + 1})", seg_text, count=1,
        )
        seg_text = re.sub(
            rf"\(\s*{seg_date.year}/0?{seg_date.month}/0?{seg_date.day}\s*\)",
            f"({today_str})", seg_text, count=1,
        )
        triplet_edits.append((m.span(), seg_text))

    triplet_count = 0
    for span, replacement in reversed(triplet_edits):
        working = working[:span[0]] + replacement + working[span[1]:]
        triplet_count += 1

    new_text = working

    # ─── Round-trip verify: 重新 parse 新 text 確認結果一致 ─────────────
    # 防 format_uvb_line 因為奇怪格式沒替換成功，dose/count/date 跟預期不符
    verify = parse_uvb_line(new_text)
    if verify is None:
        return UvbUpdateResult(
            action=UvbAction.SANITY_FAIL,
            sanity_reason="寫回後重新 parse 失敗 (格式可能損毀)",
            parsed=parsed, days_diff=days_diff, uvb_line_count=uvb_lines,
        )
    # dose / date 一定要對；count 若有更新也要對 (none → none, 有 → 數字符)
    dose_ok = verify.dose == new_dose
    date_ok = verify.last_date == today
    count_ok = verify.count == new_count  # 若兩邊都 None 也算 ok
    if not (dose_ok and date_ok and count_ok):
        return UvbUpdateResult(
            action=UvbAction.SANITY_FAIL,
            sanity_reason=(f"寫回後 round-trip verify 失敗: "
                          f"預期 dose={new_dose}/count={new_count}/date={today}, "
                          f"實際 dose={verify.dose}/count={verify.count}/"
                          f"date={verify.last_date}"),
            parsed=parsed, days_diff=days_diff, uvb_line_count=uvb_lines,
        )

    # ─── v20.13 偵測「不確定其他 triplet」(日期不同) ─────────────────────
    # 例：line 1 是 excimer (37) (2026/5/22), line 2 是 UVB ... (2026/5/24)
    # Step A 處理 line 2 (UVB)，Step C 因日期不同沒動 line 1。
    # 但醫師可能希望 line 1 也一起更新 — 跳 Yes/No 給醫師決定。
    uncertain_others = _detect_uncertain_triplets(new_text, today)

    return UvbUpdateResult(
        action=UvbAction.UPDATED,
        new_text=new_text,
        new_dose=new_dose,
        new_count=new_count,
        last_date=parsed.last_date,
        days_diff=days_diff,
        parsed=parsed,
        uvb_line_count=uvb_lines,
        additional_lines_updated=uvb_additional,
        additional_triplets_updated=triplet_count,
        uncertain_other_triplets=uncertain_others if uncertain_others else None,
    )
