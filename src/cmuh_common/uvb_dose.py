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
from datetime import date
from typing import Optional


# ─── Constants ──────────────────────────────────────────────────────────
# [2026-06-02] 至少間隔一天；同日或隔天重複照光都警告:
#   days_diff = 0-1                   → 警告終止
#   days_diff = 2-6                   → +increase (cap MAX)
#   days_diff = 7 (剛好)               → 保持 dose
#   days_diff = 8-14 (含14)           → × 0.75, floor 10, 最低 250
#   days_diff = 15-21 (含21)          → × 0.5, floor 10, 最低 250
#   days_diff > 21                    → 固定 250
TOO_CLOSE_DAYS = 2            # days_diff < 此值 → 警告終止 (同日或隔天)
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
# [v20.14 2026-05-26] 病歷可能有好幾個月前甚至好幾年前的照光紀錄，user 不希望
# 程式直接拿舊紀錄套日期/decay 自動更新。距上次 > 此值 → CONFIRM_NEEDED 跳
# Yes/No 給醫師決定是否要按舊紀錄繼續更新。
STALE_DAYS = 30            # 超過 30 天 → 跳 Yes/No 確認


# ─── Action enum ────────────────────────────────────────────────────────
class UvbAction:
    """F2/F3 對應的 UVB 處理動作 (字串 enum)。

    [v20.5 2026-05-26] 為了「確保資訊正確」，每種 uncertain case 都拿到一個
    明確的 action，caller (main.py) 依此決定要警告終止或繼續。
    現在只有 UPDATED 才會繼續走 51019，其他全部都要 stop+prompt。
    """
    NO_UVB_LINE = "no_uvb_line"          # 處置內沒 UVB 行 → 警告 (F2/F3 不該沒 UVB)
    PARSE_FAIL = "parse_fail"            # 有 UVB 但格式怪 → 警告
    TOO_CLOSE = "too_close"              # 同日或隔天重複照光 → 警告
    SANITY_FAIL = "sanity_fail"          # parse 出來的值超出合理範圍 → 警告
    CONFIRM_NEEDED = "confirm_needed"    # [v20.12] dose 超過 MAX_DOSE → Yes/No 確認
    UPDATED = "updated"                  # 正常更新 (唯一繼續走 51019 的 case)
    # [v20.17] 處置有 UVB+dose 但缺 MAX/increase (e.g. "keep UVB 850 mj/cm2") —
    # 不修改處置但繼續執行 51019+療程 (像 F1 lenient mode 的 NO_UVB_LINE 行為)
    SILENT_SKIP = "silent_skip"


# ─── Parse result ───────────────────────────────────────────────────────
@dataclass
class UvbLineInfo:
    """parse 出來的單行 UVB 結構。

    [v20.7] count 變 Optional — 處置可能沒寫 (N) 次數欄位 (醫師選擇不記)。
    沒 count → 不更新 count，dose/date 仍要更新。
    [v20.16] last_date / increase 也變 Optional — 病歷可能是第一次照光，沒
    日期或 increase 字眼。partial parse 可吐沒 last_date 的 info 給 caller 跳
    Yes/No「當作第一次照光」確認。
    """
    full_match: str       # 完整原始行內容 (含 line ending 之前的文字)
    dose: int             # 原劑量
    count: Optional[int]  # 原 (N) 次數，None=處置沒寫
    last_date: Optional[date] = None  # 原日期，None=處置沒寫 (第一次照光)
    increase: Optional[int] = None    # increase 後的數字，None=處置沒寫
    max_dose: int = 0     # MAX 後的數字
    span: tuple = (0, 0)  # 在 source text 中的 (start, end) char offset
    # [v20.15] 原始 date 字串 (e.g. "(115/05/24)" / "(1150524)" / "2026/5/24")
    # — format_uvb_line 寫回時用同樣 format 取代
    date_text: str = ""
    # [v20.15] keyword 文字 ("UVB" 或 "Phototherapy") — format_uvb_line 用
    keyword_text: str = "UVB"


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

# [v20.15 2026-05-26] 也接受 "Phototherapy" 當 keyword (劉香君實機 case)
# [v20.18 2026-05-27] 也接受 "UV" 簡寫當 keyword (陳冠廷實機 case "uv 1150mj")
# 注意 alternation 順序: UVB|UV 必須長的先 — 否則 "UVB" 會被 UV 部分匹配
# [2026-06-01] 分隔符也接受逗號:「phototherapy, 950mj」這類關鍵字與劑量數字間
# 夾逗號的自由寫法(曾大鈞實機 case)。原本只允許冒號/空白,逗號會讓 dose 解析失敗。
_UVB_DOSE_RE = re.compile(
    r"(UVB|Phototherapy|UV)(?:\s*[:：,，]?\s*"
    r"|\s+[^\r\n,，]{0,40}?\bdose\s*[:：]?\s*)(\d+)",
    re.IGNORECASE)
# [v20.11] 接受帶 paren 跟不帶 paren 兩種:
#   (2026/05/24) — group 1-3
#    2026/05/24  — group 4-6
# [v20.15] 新增民國年支援:
#   (115/05/24)   — group 7-9   (ROC 3-digit year + slash/dash)
#   (1150524)     — group 10-12 (ROC 7-digit concatenated YYYMMDD)
# year 100-150 視為民國年 (對應 AD 2011-2061)
_UVB_DATE_RE = re.compile(
    r"[\(（]\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s*[\)）]"     # AD paren
    r"|"
    r"\b(\d{4})[-/](\d{1,2})[-/](\d{1,2})\b"                    # AD bare
    r"|"
    r"[\(（]\s*(\d{3})[-/](\d{1,2})[-/](\d{1,2})\s*[\)）]"     # ROC paren slash
    r"|"
    r"[\(（]\s*(\d{3})(\d{2})(\d{2})\s*[\)）]"                  # ROC concat
)
# count: \(\s*\d+\s*\) — 任何 paren 內純數字。
# 為了不抓到日期 (年是 4 位)，caller 會先 mask date span 再 search。
# 大於 MAX_COUNT 的會在 sanity check 時擋下，這裡先放寬接受任意位數。
# [v20.7] 也排除「年」可能性 — 4 位數字當 count 機率極低，先排除避免誤抓
_UVB_COUNT_RE = re.compile(r"[\(（]\s*(\d+)\s*[\)）]")
# increase / increased / add / 每次加 / 增加 (case-insensitive)
# [v20.16] 也接受常見打字錯誤 "incrase" / "incraese" (張耀銘實機 case)
# [v20.17] 接受 "in crease" 中間有空格 (張智宇實機 case)
# [2026-06-02] 兩種寫法都接受：
#   (a) 關鍵字在前、數字在後：increase/add/adding/added/每次加 … N
#       add 改 add(?:ing|ed|s)? 以接受「adding 100」(陳珮淇實機 case，原本
#       "add" 後接 "ing" 就比不到數字 → inc=None → parse_fail)。
#   (b) 數字在前、"each (time)"/"每次" 在後的自由寫法、無 add/increase 關鍵字：
#       「50 each time」「100 mj each」「30 每次」(周宗翰實機 case)。
#       數字落在 group(2)，caller 用 group(1) or group(2) 取值。
_UVB_INCREASE_RE = re.compile(
    r"(?:(?:in\s*cr(?:e?a?|a?e?)se[d]?|add(?:ing|ed|s)?)(?:\s+by)?"
    r"|每次增加|每次加|增加|加)\s*[:：]?\s*(\d+)"
    r"|"
    r"(\d+)\s*(?:mj(?:/cm2)?)?\s*(?:each(?:\s+time)?|每次)",
    re.IGNORECASE)
# [v20.8] MAX 接受多種同義表達:
#   MAX:N / MAX N / MAX at N / MAX dose: N / fix N / fixed at N / fixed to N / 固定 N
# \bfix(?:ed)? 確保 word boundary 避免抓到 "prefix"/"fixing" 等
# [v20.15] 新增 "MAX dose" 寫法 (鄧仲強實機 case: "MAX dose: 1200mj/cm2")
# [v20.17] 新增 "MAX UVB / MAX Phototherapy" 寫法 (黃冠輝實機 case:
#   "max UVB 1800 mj/cm2")
# [2026-06-01] 新增 "upper limit" / "上限" 同義(曾大鈞實機 case:
#   "...Add 50mj each time, upper limit: 950mj")。
_UVB_MAX_RE = re.compile(
    r"(?:MAX(?:\s+(?:dose|UVB|Phototherapy))?(?:\s+(?:at|to))?\s*[:：]?\s*"
    r"|\bfix(?:ed)?(?:\s+(?:at|to))?\s*[:：]?\s*"
    r"|upper\s*limit(?:\s+(?:at|to))?\s*[:：]?\s*"
    r"|(?:each\s+time\s+)?(?:till|until)\s*[:：]?\s*"
    r"|maintain\s+dose\s+at\s*[:：]?\s*"
    r"|最大(?:劑量|剂量)?\s*[:：]?\s*"
    r"|上限(?:在|為)?\s*[:：]?\s*"
    r"|固定(?:在|為)?\s*[:：]?\s*)(\d+)",
    re.IGNORECASE,
)

# [v20.12 2026-05-26] 同日期 triplet 偵測 — 用於更新非 UVB 關鍵字 (e.g. excimer
# light) 但日期相同的 (count) ... (date) 三元組。
# 例如:
#   excimer light (25) 1000mJ for nape on (2026/5/25)
#   1500mj/cm2 (44) on (2026/5/25) add 50 each time, fixed at 1500
# 中間限制無括號避免跨越獨立 segment。
_TRIPLET_RE = re.compile(
    r"[\(（]\s*(\d{1,3})\s*[\)）]"                  # (count)
    r"([^()（）\r\n]{0,120}?)"                       # 中間 (same line only)
    r"[\(（]\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})\s*[\)）]" # (YYYY/MM/DD)
)
# Triplet 周圍需要的 UVB-相關標記 (避免誤動其他內容如「(10) days for ...」)
# 「\d\s*mj」要求數字接 mJ/mj (劑量單位)，比單純 "mj" 更安全
_UVB_MARKER_RE = re.compile(
    r"(?:uvb|excimer|\d\s*mj|phototherapy|photo\s*therapy)",
    re.IGNORECASE,
)
_EXCIMER_MARKER_RE = re.compile(r"\bexcimer(?:\s+light)?\b", re.IGNORECASE)
_EXCIMER_DOSE_RE = re.compile(r"(\d+)\s*(mj(?:/cm2)?)", re.IGNORECASE)


def _date_text(dt: date, sep: str = "/") -> str:
    """格式化日期並保留原本分隔符，sep 非 '-' 時預設用 '/'。"""
    sep = "-" if sep == "-" else "/"
    return f"{dt.year}{sep}{dt.month:02d}{sep}{dt.day:02d}"


def _has_maintain_dose(text: str) -> bool:
    return bool(re.search(r"\bmaintain(?:\s+the)?\s+dose\b(?!\s+at\s*\d)", text,
                          re.IGNORECASE))


def _replace_uvb_dose(src: str, old_dose: int, new_dose: int) -> str:
    """Replace a structured UVB dose while preserving its original prefix."""
    match = _UVB_DOSE_RE.search(src)
    if match is None:
        return src
    try:
        if int(match.group(2)) != old_dose:
            return src
    except (TypeError, ValueError):
        return src
    return src[:match.start(2)] + str(new_dose) + src[match.end(2):]


def _resolve_date_match(date_m) -> Optional[tuple]:
    """[v20.15] _UVB_DATE_RE 有 4 個 alternative，回 (year_ad, month, day) 或 None。

    Group layout:
        1-3:  AD paren  (\\d{4}/\\d{1,2}/\\d{1,2})
        4-6:  AD bare    \\d{4}/\\d{1,2}/\\d{1,2}
        7-9:  ROC paren slash  (\\d{3}/\\d{1,2}/\\d{1,2})    民國年 + 1911
        10-12: ROC concat       (\\d{3}\\d{2}\\d{2})         民國年 + 1911
    """
    g = date_m.groups()
    # AD slashed (paren or bare)
    if g[0]:    # AD paren
        y, m, d = int(g[0]), int(g[1]), int(g[2])
    elif g[3]:  # AD bare
        y, m, d = int(g[3]), int(g[4]), int(g[5])
    elif g[6]:  # ROC paren slash (3-digit)
        roc_y = int(g[6])
        if not (60 <= roc_y <= 200):  # 民國 60-200 = AD 1971-2111
            return None
        y, m, d = roc_y + 1911, int(g[7]), int(g[8])
    elif g[9]:  # ROC concat 7-digit
        roc_y = int(g[9])
        if not (60 <= roc_y <= 200):
            return None
        y, m, d = roc_y + 1911, int(g[10]), int(g[11])
    else:
        return None
    return y, m, d


def parse_uvb_line(text: str) -> Optional[UvbLineInfo]:
    """從 text 中找第一個 UVB 行，回 UvbLineInfo 或 None。

    [v20.6] 獨立 field 解析 — 順序不限、容忍中文夾雜、increase/add 等同義。

    解析步驟：
      1. UVB 起點 (或 Phototherapy): `(UVB|Phototherapy)\\s*:?\\s*(\\d+)`
      2. MAX 終點 (or fixed/固定): UVB 之後第一個 MAX/fix 找到 max_dose
      3. segment = text[line_start:max_end]   ← [v20.15] 擴到行首吸收日期可能
         在 UVB 前的 case (e.g. "(2026/05/24) UVB 850 ...")
      4. 在 segment 內各別找:
         - date: AD or 民國年 (paren / slashed / concat)
         - count: 不重疊 date 的 `[\\(（]\\s*\\d+\\s*[\\)）]`
         - increase: `(increase[d]?|add|每次加|增加|加)\\s*\\d+`
      5. 任一 field 缺 → 回 None (parse_fail)

    [v20.15] 新增:
      - keyword 接受 "Phototherapy" (劉香君實機 case)
      - 民國年 date format (115/05/24, 1150524 — 詹晟凱/陳文海實機 case)
      - segment 擴到行首吸收 date 在 UVB 前的 case (楊亮筠實機 case)
      - "MAX dose: N" 寫法 (鄧仲強實機 case)
    """
    lower = text.lower()
    # [v20.18] 也接受 "uv" 簡寫 (但需 word boundary 避免誤抓 uveitis/UVA 等)
    if ("uvb" not in lower and "phototherapy" not in lower
            and not re.search(r"\buv\b", lower)):
        return None

    # 1. UVB / Phototherapy dose
    dose_m = _UVB_DOSE_RE.search(text)
    if not dose_m:
        return None
    try:
        dose = int(dose_m.group(2))
    except ValueError:
        return None
    keyword_text = dose_m.group(1)  # "UVB" or "Phototherapy"
    dose_start = dose_m.start()

    # 2. MAX (從 UVB 之後找)
    max_m = _UVB_MAX_RE.search(text, dose_start)
    if not max_m:
        return None
    try:
        max_dose = int(max_m.group(1))
    except ValueError:
        return None
    max_end = max_m.end()

    # [v20.15] segment 擴到 line_start (UVB 所在行的開頭) — 日期可能寫在
    # UVB 之前 (e.g. "(2026/05/24) UVB 850 ...")
    line_start = text.rfind("\n", 0, dose_start) + 1  # 0 if not found
    segment = text[line_start:max_end]

    # 3. Date (segment 內第一個 YYYY/MM/DD or 民國 YYY/MM/DD or YYYMMDD)
    date_m = _UVB_DATE_RE.search(segment)
    if not date_m:
        return None
    ymd = _resolve_date_match(date_m)
    if ymd is None:
        return None
    try:
        last_date = date(*ymd)
    except (ValueError, TypeError):
        return None
    date_text = date_m.group(0)

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
        if _has_maintain_dose(segment):
            increase = 0
        else:
            return None
    else:
        try:
            # group(1)=關鍵字在前的數字；group(2)="N each time" 數字在前的數字
            increase = int(inc_m.group(1) or inc_m.group(2))
        except (ValueError, TypeError):
            return None

    return UvbLineInfo(
        full_match=segment,
        dose=dose,
        count=count,
        last_date=last_date,
        increase=increase,
        max_dose=max_dose,
        span=(line_start, max_end),
        date_text=date_text,
        keyword_text=keyword_text,
    )


def parse_uvb_partial(text: str) -> Optional[UvbLineInfo]:
    """[v20.16] 寬鬆 parse — 只需要 dose + max，date/count/increase 可缺。

    使用情境：strict parse_uvb_line 失敗時，caller 用這個試試是不是「第一次
    照光」case (有 UVB+劑量+MAX 但沒 (N)/date/increase)。
    回 UvbLineInfo 時 last_date/increase 可能是 None。
    """
    lower = text.lower()
    # [v20.18] 也接受 "uv" 簡寫 (但需 word boundary 避免誤抓 uveitis/UVA 等)
    if ("uvb" not in lower and "phototherapy" not in lower
            and not re.search(r"\buv\b", lower)):
        return None

    dose_m = _UVB_DOSE_RE.search(text)
    if not dose_m:
        return None
    try:
        dose = int(dose_m.group(2))
    except ValueError:
        return None
    keyword_text = dose_m.group(1)
    dose_start = dose_m.start()

    max_m = _UVB_MAX_RE.search(text, dose_start)
    if not max_m:
        return None
    try:
        max_dose = int(max_m.group(1))
    except ValueError:
        return None
    max_end = max_m.end()

    line_start = text.rfind("\n", 0, dose_start) + 1
    segment = text[line_start:max_end]

    # Optional date
    last_date: Optional[date] = None
    date_text = ""
    date_m = _UVB_DATE_RE.search(segment)
    if date_m:
        ymd = _resolve_date_match(date_m)
        if ymd is not None:
            try:
                last_date = date(*ymd)
                date_text = date_m.group(0)
            except (ValueError, TypeError):
                last_date = None
                date_text = ""

    # Optional count (mask date span if any)
    if date_m:
        seg_masked = (segment[:date_m.start()]
                      + " " * (date_m.end() - date_m.start())
                      + segment[date_m.end():])
    else:
        seg_masked = segment
    count_m = _UVB_COUNT_RE.search(seg_masked)
    count: Optional[int] = None
    if count_m:
        try:
            count = int(count_m.group(1))
        except ValueError:
            count = None

    # Optional increase
    inc_m = _UVB_INCREASE_RE.search(segment)
    increase: Optional[int] = None
    if inc_m:
        try:
            increase = int(inc_m.group(1) or inc_m.group(2))
        except (ValueError, TypeError):
            increase = None

    return UvbLineInfo(
        full_match=segment,
        dose=dose,
        count=count,
        last_date=last_date,
        increase=increase,
        max_dose=max_dose,
        span=(line_start, max_end),
        date_text=date_text,
        keyword_text=keyword_text,
    )


# ─── 劑量計算 ────────────────────────────────────────────────────────────
def _floor_to_10(value: float) -> int:
    """下十推到 10 倍數: 432→430, 435→430, 437.5→430"""
    return int(math.floor(value / 10) * 10)


def compute_new_dose(*, dose: int, increase: int, max_dose: int,
                     days_diff: int) -> Optional[int]:
    """[v20.10] 依天數差算新劑量。

    days_diff < 2 (即同日或隔天) → 回 None (caller 跳警告)
    其他天數差一定有 int 回值。所有 decay 結果不低於 MIN_DECAY_DOSE (250)。
    """
    if days_diff < TOO_CLOSE_DAYS:                  # 同日或隔天 → 太密集
        return None
    if days_diff < SAME_DOSE_DAYS:                  # 2-6 天 → +increase, cap MAX
        return min(dose + increase, max_dose)
    if days_diff == SAME_DOSE_DAYS:                 # 7 天剛好 → 保持
        return dose
    # [safety] decay/long-gap 結果一律夾在 [.., dose, max_dose]：MIN_DECAY_DOSE(250)
    # 下限在 dose 或 max_dose 低於 250 時，會讓「衰退」反而回傳高於當前劑量/超過
    # 上限的值（照光過量風險）。min(.., dose, max_dose) 確保衰退永遠不增量、不超
    # 上限。正常情況(max_dose=800、dose≥250)此夾值不改變結果。
    if days_diff <= DECAY_75_UPPER:                 # 8-14 天 → ×0.75
        decayed = _floor_to_10(dose * DECAY_75_FACTOR)
        return min(max(decayed, MIN_DECAY_DOSE), dose, max_dose)
    if days_diff <= DECAY_50_UPPER:                 # 15-21 天 → ×0.5
        decayed = _floor_to_10(dose * DECAY_50_FACTOR)
        return min(max(decayed, MIN_DECAY_DOSE), dose, max_dose)
    return min(LONG_GAP_DOSE, dose, max_dose)       # > 21 天 → 250(夾上限/當前)


# ─── 寫回行內容 ──────────────────────────────────────────────────────────
def _today_in_format(today: date, sample_text: str) -> str:
    """[v20.15] 把 today 格式化成跟 sample_text 一樣的 format。

    sample_text 是 _UVB_DATE_RE 的原 match (e.g. "(115/05/24)" / "(1150524)" /
    "(2026/05/24)" / "2026/5/24")。

    支援格式:
      - AD slashed: 2026/5/24 → 2026/05/26
      - AD paren: (2026/5/24) → (2026/05/26)
      - ROC slashed: (115/05/24) → (115/05/26)
      - ROC concat 7-digit: (1150524) → (1150526)
      - 全形 paren: （115/05/24）→（115/05/26）
    """
    stripped = sample_text.strip()
    has_paren_full = stripped.startswith("（")
    has_paren_half = stripped.startswith("(")
    inner = stripped.strip("()（）").strip()
    sep = "-" if "-" in inner else "/"

    if "-" not in inner and "/" not in inner:
        # ROC concat 7-digit YYYMMDD
        roc_y = today.year - 1911
        body = f"{roc_y:03d}{today.month:02d}{today.day:02d}"
    else:
        year_part = inner.split(sep)[0]
        if len(year_part) == 3:
            # ROC slashed (3-digit year)
            roc_y = today.year - 1911
            body = f"{roc_y:03d}{sep}{today.month:02d}{sep}{today.day:02d}"
        else:
            # AD slashed (4-digit year)
            body = f"{today.year}{sep}{today.month:02d}{sep}{today.day:02d}"

    if has_paren_full:
        return f"（{body}）"
    if has_paren_half:
        return f"({body})"
    return body


def format_uvb_line(original: UvbLineInfo, *, new_dose: int,
                    new_count: Optional[int], today: date) -> str:
    """產生新的 UVB 行內容，shape 維持跟 original 一樣。

    替換 dose / count / date 三個值，其餘 (mj/cm2 / on / increase X / MAX:Y /
    W2 / W5M 等後綴) 全部保留。

    [v20.7] new_count=None → 不替換 count (處置原本就沒寫 (N))。
    [v20.15] 支援 Phototherapy keyword + ROC 民國日期格式 (slashed/concat)
    + date 可能在 UVB 之前 (full_match 已擴到 line_start)。
    """
    # 從 original.full_match 抓出原本的 3 個欄位字串位置 → 用 str.replace 替換
    # 不用 regex 替換是因為要保持其他空白格式
    src = original.full_match

    # 1. 替換 dose：找原 dose 數字第一次出現 (在 UVB 之後)
    #    使用 regex 因為要對齊「UVB 520」這個 pattern，不能誤改 "(11)" 的 11
    #    [v20.2] 允許「UVB:」冒號 — 跟 parse regex 一致
    #    [v20.18] 接受 "UV" 簡寫 — 跟 _UVB_DOSE_RE 一致
    src = _replace_uvb_dose(src, original.dose, new_dose)

    # 2. 替換 count: (N) → (N+1) — 僅當原本有 count 且傳入 new_count
    if original.count is not None and new_count is not None:
        src = re.sub(
            r"([\(\uFF08]\s*)" + str(original.count) + r"(\s*[\)\uFF09])",
            lambda mo: f"{mo.group(1)}{new_count}{mo.group(2)}",
            src,
            count=1,
        )

    # 3. 替換日期 — [v20.15] 用 original.date_text 找原樣字串，對應同 format
    # 寫回 (支援 AD slashed / ROC slashed / ROC concat 三種格式)
    if original.date_text:
        new_date_text = _today_in_format(today, original.date_text)
        idx = src.find(original.date_text)
        if idx >= 0:
            src = (src[:idx] + new_date_text
                   + src[idx + len(original.date_text):])
        else:
            src = src.replace(original.date_text, new_date_text, 1)
        return src

    # Fallback (老舊 caller 沒填 date_text) — 用零填充 AD format
    old_y = original.last_date.year
    old_m = original.last_date.month
    old_d = original.last_date.day
    with_paren_re = (
        rf"([\(（]\s*){old_y}([/-])0?{old_m}([/-])0?{old_d}"
        rf"(\s*[\)）])"
    )
    if re.search(with_paren_re, src):
        src = re.sub(
            with_paren_re,
            lambda mo: (f"{mo.group(1)}"
                        f"{_date_text(today, mo.group(2))}"
                        f"{mo.group(4)}"),
            src,
            count=1,
        )
    else:
        bare_re = rf"\b{old_y}([/-])0?{old_m}([/-])0?{old_d}\b"
        src = re.sub(
            bare_re,
            lambda mo: _date_text(today, mo.group(1)),
            src,
            count=1,
        )

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


def _update_excimer_lines(text: str, today: date) -> tuple[str, int, Optional[dict]]:
    """Update structured excimer lines independently from UVB lines."""
    lines = text.splitlines(keepends=True)
    updated = 0
    first_update: Optional[dict] = None

    for index, line in enumerate(lines):
        marker = _EXCIMER_MARKER_RE.search(line)
        if marker is None:
            continue
        date_m = _UVB_DATE_RE.search(line, marker.end())
        max_m = _UVB_MAX_RE.search(line, marker.end())
        dose_m = _EXCIMER_DOSE_RE.search(line, marker.end())
        inc_m = _UVB_INCREASE_RE.search(line, marker.end())
        if (date_m is None or max_m is None or dose_m is None
                or dose_m.start() > date_m.start()):
            continue

        ymd = _resolve_date_match(date_m)
        if ymd is None:
            continue
        try:
            last_date = date(*ymd)
            dose = int(dose_m.group(1))
            max_dose = int(max_m.group(1))
            increase = int(inc_m.group(1) or inc_m.group(2)) if inc_m else 0
        except (TypeError, ValueError):
            continue

        masked = (line[:date_m.start()]
                  + " " * (date_m.end() - date_m.start())
                  + line[date_m.end():])
        count_m = _UVB_COUNT_RE.search(masked, marker.end(), date_m.start())
        count = int(count_m.group(1)) if count_m else None
        days_diff = (today - last_date).days

        if (dose < MIN_DOSE or max_dose < MIN_DOSE
                or increase < 0 or increase > 200
                or (count is not None and not (1 <= count <= MAX_COUNT))
                or days_diff < TOO_CLOSE_DAYS or days_diff > STALE_DAYS):
            continue

        new_dose = compute_new_dose(
            dose=dose, increase=increase, max_dose=max_dose,
            days_diff=days_diff)
        if new_dose is None:
            continue
        if _has_maintain_dose(line):
            new_dose = dose
        new_count = count + 1 if count is not None else None

        edits = [
            (dose_m.span(1), str(new_dose)),
            (date_m.span(), _today_in_format(today, date_m.group(0))),
        ]
        if count_m is not None and new_count is not None:
            edits.append((count_m.span(1), str(new_count)))
        new_line = line
        for (start, end), replacement in sorted(edits, reverse=True):
            new_line = new_line[:start] + replacement + new_line[end:]

        lines[index] = new_line
        updated += 1
        if first_update is None:
            first_update = {
                "dose": new_dose,
                "count": new_count,
                "last_date": last_date,
                "days_diff": days_diff,
            }

    return "".join(lines), updated, first_update


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
            r"([\(\uFF08]\s*)" + str(old_count) + r"(\s*[\)\uFF09])",
            lambda mo: f"{mo.group(1)}{old_count + 1}{mo.group(2)}",
            rep,
            count=1,
        )
        rep = re.sub(
            rf"([\(\uFF08]\s*){seg_date.year}([/-])0?{seg_date.month}([/-])0?{seg_date.day}(\s*[\)\uFF09])",
            lambda mo: f"{mo.group(1)}{_date_text(today, mo.group(2))}{mo.group(4)}",
            rep,
            count=1,
        )
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


def _first_time_update(parsed: UvbLineInfo, today: date,
                        uvb_lines: int) -> UvbUpdateResult:
    """[v20.16] 處置 UVB 行沒日期 → 當作第一次照光記錄:
      - [v20.17] dose 套用 +increase 公式 (treat as 2-6 days, min cap MAX)
      - 只更新原句已經存在的欄位
      - 原本沒有 count 或 date 時，不自行補寫

    v20.17 起此 path 是 silent — 不再需要 Yes/No 確認。
    """
    # 若有 increase → 套用 +increase 公式 (尊重原 MAX); 否則保持原 dose
    if parsed.increase is not None and parsed.max_dose:
        new_dose = min(parsed.dose + parsed.increase, parsed.max_dose)
    else:
        new_dose = parsed.dose
    new_count = parsed.count + 1 if parsed.count is not None else None
    src = parsed.full_match

    # 1. 替換 dose: UVB:OLD → UVB:NEW
    # [v20.18] 接受 "UV" 簡寫 keyword
    src = _replace_uvb_dose(src, parsed.dose, new_dose)

    # 2. 原句有 count 才更新 count；沒有 date 就保持沒有 date。
    if parsed.count is not None and new_count is not None:
        src = re.sub(
            r"([\(（]\s*)" + str(parsed.count) + r"(\s*[\)）])",
            lambda mo: f"{mo.group(1)}{new_count}{mo.group(2)}",
            src, count=1,
        )

    return UvbUpdateResult(
        action=UvbAction.UPDATED,
        new_text=src,  # caller 會把 segment 接回完整 text
        new_dose=new_dose,
        new_count=new_count,
        last_date=None,
        days_diff=None,
        parsed=parsed,
        uvb_line_count=uvb_lines,
    )


def update_uvb_in_text(text: str, today: Optional[date] = None,
                       skip_dose_sanity: bool = False,
                       skip_stale_check: bool = False) -> UvbUpdateResult:
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

    [v20.14 2026-05-26] 病歷可能有舊紀錄 (好幾個月/年前)，user 不希望直接
    用舊紀錄套日期/decay 更新。distance > STALE_DAYS (30) 不在 skip 模式 →
    回 CONFIRM_NEEDED 給 caller 跳 Yes/No: Yes 重 call 帶 skip_stale_check=True
    才繼續更新；No 直接終止不修改處置。

    [v20.16→v20.17 2026-05-26] 處置 UVB 行可能沒日期 (第一次照光紀錄)，strict
    parse_uvb_line 會 fail。strict fail 後試 parse_uvb_partial，若 last_date is None
    (無日期) → 直接走 silent first-time (_first_time_update)：跳過 days_diff/decay，
    只更新原句已有欄位、不跳對話框。
    (註：v20.16 曾用 CONFIRM_NEEDED + treat_as_first_time 參數重 call，v20.17 改為
    silent first-time 後該參數已無作用，[stability r4] 已移除以免誤導未來 caller。)
    """
    if today is None:
        today = date.today()

    uvb_lines = _count_uvb_lines(text)

    parsed = parse_uvb_line(text)
    if parsed is None:
        # excimer / excimer light 本身也是照光，不要求同時出現 UVB。
        if not re.search(r"(?:UVB|Phototherapy|\bUV\b)", text,
                         re.IGNORECASE):
            excimer_text, excimer_count, excimer_first = _update_excimer_lines(
                text, today)
            if excimer_count and excimer_first:
                return UvbUpdateResult(
                    action=UvbAction.UPDATED,
                    new_text=excimer_text,
                    new_dose=excimer_first["dose"],
                    new_count=excimer_first["count"],
                    last_date=excimer_first["last_date"],
                    days_diff=excimer_first["days_diff"],
                    uvb_line_count=excimer_count,
                    additional_triplets_updated=max(0, excimer_count - 1),
                )
        # Strict parse 失敗 — 試 partial parse 看是不是「沒日期」case
        partial = parse_uvb_partial(text)
        if partial is None:
            # 連結構化 dose+max 都找不到。再細分:
            # - 有「UVB:」或「UVB：」結構 (with garbage 中文 etc.) → PARSE_FAIL
            #   (e.g. 廖三發「UVB:已打折 1000」中文夾在冒號後)
            # - [v20.17] 有 UVB/Phototherapy + 數字 但缺 MAX (e.g. 圖三梁雯琳
            #   `keep UVB 850 mj/cm2`) → SILENT_SKIP (不修改處置但繼續執行
            #   51019+療程)
            # - 連 UVB/Phototherapy + 數字 結構都沒有 → NO_UVB_LINE
            #   (例如「keep phototherapy on both lower limbs to 680」這種
            #   一般描述語句，不是結構化處置)
            # [v20.18] 加 UV 同樣判斷 (但 UV: 較罕見)
            if re.search(r"(?:UVB|Phototherapy|UV)\s*[:：]",
                         text, re.IGNORECASE):
                return UvbUpdateResult(action=UvbAction.PARSE_FAIL,
                                       uvb_line_count=uvb_lines)
            if _UVB_DOSE_RE.search(text):
                # 有 UVB+數字但結構不完整 (缺 MAX) → silent skip
                return UvbUpdateResult(action=UvbAction.SILENT_SKIP,
                                       uvb_line_count=uvb_lines)
            return UvbUpdateResult(action=UvbAction.NO_UVB_LINE,
                                   uvb_line_count=uvb_lines)
        # Partial 抓到 dose+max
        if partial.last_date is None:
            # [v20.17] 沒 date → 直接 silent first-time 更新，不跳對話框
            # (user request: "不用跳出是否新增日期 直接修改劑量")
            # 註：first-time 刻意只受 phrase 內「本地 max」約束(_first_time_update
            # 已 min(dose+increase, 本地max) 夾住)，不套用全域 MAX_DOSE 上限確認
            # ——醫師當下親手寫的 dose+max 視為可信(見 test_silent_first_time_*)。
            result = _first_time_update(partial, today, uvb_lines)
            # 接回原文
            full_new = (text[:partial.span[0]]
                        + result.new_text
                        + text[partial.span[1]:])
            result.new_text = full_new
            return result
        # partial 有 date 但少 increase 之類 — fall through to PARSE_FAIL
        # (theoretically 應該被 strict parse_uvb_line 抓到才對)
        return UvbUpdateResult(action=UvbAction.PARSE_FAIL,
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
    if ((parsed.increase <= 0 and not _has_maintain_dose(parsed.full_match))
            or parsed.increase > 200):
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

    # [v20.14 2026-05-26] 距上次 > 30 天 → 病歷可能是舊紀錄，跳 Yes/No 確認
    # caller 按 Yes 後以 skip_stale_check=True 重 call 繼續走 decay 計算
    if days_diff > STALE_DAYS and not skip_stale_check:
        return UvbUpdateResult(
            action=UvbAction.CONFIRM_NEEDED,
            confirm_reason=(
                f"上次照光日期 {parsed.last_date.strftime('%Y/%m/%d')} "
                f"距今 {days_diff} 天 (超過 {STALE_DAYS} 天) — "
                f"病歷可能是舊紀錄，請確認是否真要按舊紀錄繼續更新"),
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
    assert new_dose is not None  # 已通過 too-close 檢查

    # [v20.15] 處置含 "maintain" 字眼 → 醫師意圖維持原劑量，覆蓋 compute 結果
    # 只動 count + date，dose 保持 parsed.dose 不增不減
    if _has_maintain_dose(parsed.full_match):
        new_dose = parsed.dose

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
        if ((next_uvb.increase <= 0
             and not _has_maintain_dose(next_uvb.full_match))
                or next_uvb.increase > 200):
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

    # ─── Step C: 同行照光 continuation triplet 更新 ─────────────────────
    # 找剩下沒被 step A/B 更新的同行 (count) ... (date) 三元組:
    #   - 同行繼續的 UVB segment (e.g. `/ new for ... 1500mj/cm2 (44) on (date)`)
    #   - excimer / excimer light 由 Step D 依自己的欄位獨立更新
    # Step A 已經把第一行 triplet 的 date 改成 today，所以這裡掃描 working text
    # 時，第一行的 triplet 不會匹配 parsed.last_date，自動跳過。
    # 只更新 count + date，不動 segment 內 dose (continuation 通常 fixed at MAX
    # 不會變；若要 dose decay，後續再加)。
    triplet_edits = []
    for m in _TRIPLET_RE.finditer(working):
        try:
            seg_date = date(int(m.group(3)), int(m.group(4)),
                            int(m.group(5)))
        except (ValueError, TypeError):
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
        line_text = working[line_start:line_end]
        excimer_marker = _EXCIMER_MARKER_RE.search(line_text)
        if (excimer_marker is not None
                and line_start + excimer_marker.start() < m.start()):
            continue
        if not _UVB_MARKER_RE.search(line_text):
            continue
        # Same-line continuation segments are phototherapy too. They may carry
        # their own date, so update them independently when a dose precedes
        # the triplet. Other different-date triplets remain uncertain.
        dose_prefix = working[max(line_start, m.start() - 32):m.start()]
        continuation_m = re.search(
            r"(\d+)\s*mj(?:/cm2)?\s*$", dose_prefix, re.IGNORECASE)
        if seg_date != parsed.last_date:
            # Cross-date continuation updates are safe without recalculating
            # dose only when that segment is already capped at this line's MAX.
            if (continuation_m is None
                    or int(continuation_m.group(1)) != parsed.max_dose):
                continue
        seg_days_diff = (today - seg_date).days
        if seg_days_diff < TOO_CLOSE_DAYS or seg_days_diff > MAX_GAP_DAYS:
            continue
        # 構造該 triplet 替換內容: count→count+1, date→today
        seg_text = m.group(0)
        seg_text = re.sub(
            r"([\(\uFF08]\s*)" + str(old_count) + r"(\s*[\)\uFF09])",
            lambda mo: f"{mo.group(1)}{old_count + 1}{mo.group(2)}",
            seg_text,
            count=1,
        )
        seg_text = re.sub(
            rf"([\(\uFF08]\s*){seg_date.year}([/-])0?{seg_date.month}([/-])0?{seg_date.day}(\s*[\)\uFF09])",
            lambda mo: f"{mo.group(1)}{_date_text(today, mo.group(2))}{mo.group(4)}",
            seg_text,
            count=1,
        )
        triplet_edits.append((m.span(), seg_text))

    triplet_count = 0
    for span, replacement in reversed(triplet_edits):
        working = working[:span[0]] + replacement + working[span[1]:]
        triplet_count += 1

    # ─── Step D: excimer / excimer light 各自依自己的欄位更新 ───────────
    working, excimer_count, _ = _update_excimer_lines(working, today)
    triplet_count += excimer_count

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
