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
TOO_CLOSE_DAYS = 2          # < 此值 → 警告終止
SAME_DOSE_DAYS = 7          # = 此值 → 保持 dose
DECAY_DOSE_UPPER = 14       # 8-14 → ×0.75 floor 10
LONG_GAP_DOSE = 250         # > 14 → 固定 250
DECAY_FACTOR = 0.75


# ─── Action enum ────────────────────────────────────────────────────────
class UvbAction:
    """F2/F3 對應的 UVB 處理動作 (字串 enum)。"""
    NO_UVB_LINE = "no_uvb_line"          # 處置內沒 UVB 行 → fallback 跑原 51019 流程
    PARSE_FAIL = "parse_fail"            # 有 UVB 但格式怪解析失敗 → fallback
    TOO_CLOSE = "too_close"              # 0-1 天 → 警告終止 (不跑 51019)
    UPDATED = "updated"                   # 正常更新


# ─── Parse result ───────────────────────────────────────────────────────
@dataclass
class UvbLineInfo:
    """parse 出來的單行 UVB 結構。"""
    full_match: str       # 完整原始行內容 (含 line ending 之前的文字)
    dose: int             # 原劑量
    count: int            # 原 (N) 次數
    last_date: date       # 原日期
    increase: int         # increase 後的數字
    max_dose: int         # MAX 後的數字
    span: tuple[int, int] # 在 source text 中的 (start, end) char offset


# 主要 regex — 寬鬆: 忽略空白、大小寫、月日零填充
# 範例: "UVB 520mj/cm2 (11) on (2026/05/26), increase 30mj/cm2 if no erythema, MAX:800 mj/cm2, W2, W5M"
_UVB_LINE_RE = re.compile(
    r"UVB\s*"
    r"(?P<dose>\d+)\s*(?:mj/cm2)?\s*"          # 劑量 (可省 mj/cm2)
    r"\(\s*(?P<count>\d+)\s*\)\s*"             # (count)
    r"on\s*"
    r"\(\s*(?P<y>\d{4})/(?P<m>\d{1,2})/(?P<d>\d{1,2})\s*\)"  # (yyyy/mm/dd)
    r"[^A-Za-z]*increase\s*"                   # 跳過任意非字母字元到 increase
    r"(?P<increase>\d+)"                       # increase 後數字
    r".*?"                                      # 跳到 MAX (non-greedy)
    r"MAX\s*:?\s*"                             # MAX 可有可無 ":"
    r"(?P<max>\d+)",                           # MAX 後數字
    flags=re.IGNORECASE | re.DOTALL,
)


def parse_uvb_line(text: str) -> Optional[UvbLineInfo]:
    """從 text 中找第一個 UVB 行，回 UvbLineInfo 或 None。

    回 None 的情況：
      - text 沒含 UVB 字串
      - 含 UVB 但 format 不對 (parse_fail)
    """
    if "UVB" not in text and "uvb" not in text.lower():
        return None
    m = _UVB_LINE_RE.search(text)
    if not m:
        return None
    try:
        return UvbLineInfo(
            full_match=m.group(0),
            dose=int(m.group("dose")),
            count=int(m.group("count")),
            last_date=date(int(m.group("y")), int(m.group("m")), int(m.group("d"))),
            increase=int(m.group("increase")),
            max_dose=int(m.group("max")),
            span=(m.start(), m.end()),
        )
    except (ValueError, KeyError):
        return None


# ─── 劑量計算 ────────────────────────────────────────────────────────────
def compute_new_dose(*, dose: int, increase: int, max_dose: int,
                     days_diff: int) -> Optional[int]:
    """依天數差算新劑量。

    回 None 表示「太密集 (0-1 天)」 — caller 該跳警告。
    其他天數差一定有 int 回值。
    """
    if days_diff < TOO_CLOSE_DAYS:
        return None
    if days_diff < SAME_DOSE_DAYS:           # 2-6 天 → +increase, cap MAX
        return min(dose + increase, max_dose)
    if days_diff == SAME_DOSE_DAYS:           # 7 天剛好 → 保持
        return dose
    if days_diff <= DECAY_DOSE_UPPER:         # 8-14 天 → ×0.75 floor 10
        decayed = dose * DECAY_FACTOR
        return int(math.floor(decayed / 10) * 10)
    return LONG_GAP_DOSE                      # > 14 天 → 250


# ─── 寫回行內容 ──────────────────────────────────────────────────────────
def format_uvb_line(original: UvbLineInfo, *, new_dose: int, new_count: int,
                    today: date) -> str:
    """產生新的 UVB 行內容，shape 維持跟 original 一樣。

    替換 dose / count / date 三個值，其餘 (mj/cm2 / on / increase X / MAX:Y /
    W2 / W5M 等後綴) 全部保留。
    """
    # 從 original.full_match 抓出原本的 3 個欄位字串位置 → 用 str.replace 替換
    # 不用 regex 替換是因為要保持其他空白格式
    src = original.full_match

    # 1. 替換 dose：找原 dose 數字第一次出現 (在 UVB 之後)
    #    使用 regex 因為要對齊「UVB 520」這個 pattern，不能誤改 "(11)" 的 11
    src = re.sub(
        r"(UVB\s*)" + str(original.dose) + r"(\s*(?:mj/cm2)?)",
        lambda mo: f"{mo.group(1)}{new_dose}{mo.group(2)}",
        src,
        count=1,
        flags=re.IGNORECASE,
    )

    # 2. 替換 count: (N) → (N+1)
    src = re.sub(
        r"\(\s*" + str(original.count) + r"\s*\)",
        f"({new_count})",
        src,
        count=1,
    )

    # 3. 替換日期 — 用零填充格式 YYYY/MM/DD
    today_str = f"{today.year}/{today.month:02d}/{today.day:02d}"
    # 原日期可能 (2026/5/26) 或 (2026/05/26)，都改成 zero-padded
    old_date_re = (rf"\(\s*{original.last_date.year}/"
                   rf"{original.last_date.month:01d}\D?{original.last_date.month:02d}*/"
                   rf"{original.last_date.day:01d}\D?{original.last_date.day:02d}*\s*\)")
    # 簡化版：直接 match (YYYY/m/d) 或 (YYYY/mm/dd) 都接受
    simple_re = (rf"\(\s*{original.last_date.year}"
                 rf"/0?{original.last_date.month}"
                 rf"/0?{original.last_date.day}\s*\)")
    src = re.sub(simple_re, f"({today_str})", src, count=1)

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


def update_uvb_in_text(text: str, today: Optional[date] = None) -> UvbUpdateResult:
    """主入口：給整段「處置」text，回更新後 text + 動作類型。

    today=None 用今天日期；測試時傳 fixed date 方便 reproducible。
    """
    if today is None:
        today = date.today()

    parsed = parse_uvb_line(text)
    if parsed is None:
        # 沒含 UVB or parse 失敗
        if "UVB" in text.upper():
            return UvbUpdateResult(action=UvbAction.PARSE_FAIL)
        return UvbUpdateResult(action=UvbAction.NO_UVB_LINE)

    days_diff = (today - parsed.last_date).days

    if days_diff < TOO_CLOSE_DAYS:
        return UvbUpdateResult(
            action=UvbAction.TOO_CLOSE,
            last_date=parsed.last_date,
            days_diff=days_diff,
            parsed=parsed,
        )

    new_dose = compute_new_dose(
        dose=parsed.dose, increase=parsed.increase,
        max_dose=parsed.max_dose, days_diff=days_diff,
    )
    assert new_dose is not None  # days_diff >= 2 已過 too-close 檢查

    new_count = parsed.count + 1
    new_line = format_uvb_line(parsed, new_dose=new_dose, new_count=new_count,
                               today=today)
    new_text = text[:parsed.span[0]] + new_line + text[parsed.span[1]:]

    return UvbUpdateResult(
        action=UvbAction.UPDATED,
        new_text=new_text,
        new_dose=new_dose,
        new_count=new_count,
        last_date=parsed.last_date,
        days_diff=days_diff,
        parsed=parsed,
    )
