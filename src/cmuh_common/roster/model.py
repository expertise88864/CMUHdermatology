# -*- coding: utf-8 -*-
"""排班資料模型與月曆純計算（設計文件 §5 / §6）。

重點設計（皆為文件定案，勿在此重新發明）：
- 點數：平日 1、週六日 2、平日的國定假日 1。**假日撞週末以週末計 2 點**
  （年度指定表可能含週末日期如清明 4/4(六)，點數仍算週末 2 點）。
- 值班區塊（DutyBlock）：週六+週日必同一人；與週末**相鄰**的國定假日串進
  同一區塊（六日一三連休、五六日皆是），整塊同一人。平日孤立國定假日
  （不鄰週末）是單日，不成塊，由年度指定表指定。
- 跨月邊界：區塊只在本月內建構。月底是週六 → 下月 1 號(週日)由下月求解時
  以「上月最後週末值班人」固定（boundary fix）；月初是週日且上月末是週六
  → 本月 1 號固定為上月人選。色塊連週規則同樣要看上月最後一個週末。
- 週色 key：取該區塊「週六」所在 ISO 週（"2026-W31"）。孤兒週日區塊取該
  週日的 ISO 週（ISO 週一~週日，週日與其前一天週六同週 → 一致）。
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

# 時段常數（PGY/Clerk 用；R/VS 值班以「日」為單位）
SESSIONS = ("上午", "下午", "晚上")
STUDENT_SESSIONS = ("上午", "下午")

SCHEMA_VERSION = 1


# ─── 人員 ────────────────────────────────────────────────────────────────
@dataclass
class Member:
    """R 或 VS 成員。fixed_weekday 僅 R 使用（0=週一…6=週日；None=無）。"""
    id: str
    name: str
    level: str = ""                      # R1/R2/R3；VS 留空
    fixed_weekday: Optional[int] = None

    @staticmethod
    def from_dict(d: dict) -> "Member":
        fw = d.get("fixed_weekday")
        return Member(
            id=str(d.get("id", "")),
            name=str(d.get("name", "")),
            level=str(d.get("level", "")),
            fixed_weekday=int(fw) if fw is not None else None,
        )

    def to_dict(self) -> dict:
        out = {"id": self.id, "name": self.name, "level": self.level}
        if self.fixed_weekday is not None:
            out["fixed_weekday"] = int(self.fixed_weekday)
        return out


# ─── Clerk 梯次 ───────────────────────────────────────────────────────────
@dataclass
class ClerkBatch:
    """Clerk 兩週一梯次（起始必為週一，不綁月份，可跨月）。"""
    id: str
    start_monday: date
    members: list = field(default_factory=list)   # clerk 代號

    def covers(self, d: date) -> bool:
        return self.start_monday <= d < self.start_monday + timedelta(days=14)

    @staticmethod
    def from_dict(dd: dict) -> "ClerkBatch":
        return ClerkBatch(
            id=str(dd.get("id", "")),
            start_monday=date.fromisoformat(dd["start_monday"]),
            members=[str(x) for x in (dd.get("members") or [])])

    def to_dict(self) -> dict:
        return {"id": self.id, "start_monday": self.start_monday.isoformat(),
                "members": list(self.members)}


def batches_covering(batches: list, year: int, month: int) -> list:
    """回傳與該月任一天重疊的梯次（ClerkBatch 清單）。"""
    days = set(month_dates(year, month))
    return [b for b in batches if any(b.covers(d) for d in days)]


# ─── 參數 ────────────────────────────────────────────────────────────────
@dataclass
class RosterParams:
    """點數與範圍參數（config.json "points" / "duty_range_soft"）。"""
    weekday_point: int = 1
    weekend_point: int = 2
    holiday_point: int = 1        # 平日的國定假日
    duty_min: int = 9             # R 專用軟範圍（VS 不適用）
    duty_max: int = 11
    room_capacity: int = 2

    @staticmethod
    def from_config(cfg: dict) -> "RosterParams":
        pts = cfg.get("points") or {}
        rng = cfg.get("duty_range_soft") or [9, 11]
        return RosterParams(
            weekday_point=int(pts.get("weekday", 1)),
            weekend_point=int(pts.get("weekend", 2)),
            holiday_point=int(pts.get("national_holiday", 1)),
            duty_min=int(rng[0]), duty_max=int(rng[1]),
            room_capacity=int(cfg.get("room_capacity", 2)),
        )


# ─── 月曆工具 ─────────────────────────────────────────────────────────────
def roc(year: int) -> int:
    """西元 → 民國年（2026 → 115）。匯出檔名/表頭用。"""
    return year - 1911


def month_dates(year: int, month: int) -> list[date]:
    """該月全部日期（升冪）。"""
    _, last = calendar.monthrange(year, month)
    return [date(year, month, d) for d in range(1, last + 1)]


def is_weekend(d: date) -> bool:
    return d.weekday() >= 5  # 5=週六 6=週日


def week_matrix(year: int, month: int) -> list:
    """月曆週列矩陣（每列 7 格，週一起始；非本月格為 None）。UI 月曆與匯出共用。"""
    days = month_dates(year, month)
    lead = days[0].weekday()                 # 週一=0 → 月初前空格數
    cells: list = [None] * lead + list(days)
    while len(cells) % 7:
        cells.append(None)
    return [cells[i:i + 7] for i in range(0, len(cells), 7)]


def week_key(d: date) -> str:
    """ISO 週 key（"2026-W31"）。ISO 週為週一~週日 → 週六與其翌日週日同週。"""
    iso = d.isocalendar()
    return f"{iso[0]}-W{iso[1]:02d}"


def day_point(d: date, holidays: set, params: RosterParams) -> int:
    """單日點數。週末優先於假日（設計文件 §16 補遺前之定案：週末撞假日算 2）。"""
    if is_weekend(d):
        return params.weekend_point
    if d in holidays:
        return params.holiday_point
    return params.weekday_point


# ─── 值班區塊 ─────────────────────────────────────────────────────────────
@dataclass
class DutyBlock:
    """必須同一人連值的日期區塊（升冪、同月）。

    kind: "weekend"        正常含週六的區塊（可能鏈入相鄰假日）
          "weekend_orphan" 月初孤兒週日（其週六在上月；由 boundary fix 處理，
                           若上月資料缺失則獨立成塊並警告）
    """
    days: list = field(default_factory=list)
    kind: str = "weekend"

    @property
    def saturday(self) -> Optional[date]:
        for d in self.days:
            if d.weekday() == 5:
                return d
        return None

    def color_anchor(self) -> date:
        """週色查詢用日期：優先週六，孤兒塊用第一天（週日，ISO 同週）。"""
        return self.saturday or self.days[0]

    def points(self, holidays: set, params: RosterParams) -> int:
        return sum(day_point(d, holidays, params) for d in self.days)


def build_duty_blocks(year: int, month: int, holidays: set) -> list[DutyBlock]:
    """建構該月全部值班區塊（只在本月範圍內；跨月由 boundary fix 處理）。

    規則：每個本月週六起一塊 → 併入本月的翌日週日 → 往後鏈入連續國定假日
    （六日一…）→ 往前鏈入緊鄰週六的連續國定假日（…五六日）。
    月初 1 號若為週日（其週六在上月）→ 孤兒塊 weekend_orphan。
    """
    days = month_dates(year, month)
    in_month = set(days)
    blocks: list[DutyBlock] = []
    # [codex P2] 已被任一區塊佔用的日期。長連假(如春節)可能從月初孤兒週日一路
    # 鏈到下個週末：孤兒塊的前向鏈與週六塊的後向鏈都必須 (1)不跨進週末日
    # (2)不吃已被佔用的日 —— 否則兩塊重疊,上月人選會被錯誤地綁進下一個週末。
    claimed: set = set()

    first = days[0]
    if first.weekday() == 6:  # 月初孤兒週日
        b = DutyBlock(days=[first], kind="weekend_orphan")
        nxt = first + timedelta(days=1)
        while (nxt in in_month and nxt in holidays
               and not is_weekend(nxt)):        # 孤兒日後鏈假日(六日一跨月),不跨週末
            b.days.append(nxt)
            nxt += timedelta(days=1)
        blocks.append(b)
        claimed.update(b.days)

    for d in days:
        if d.weekday() != 5:
            continue
        b = DutyBlock(days=[d], kind="weekend")
        sun = d + timedelta(days=1)
        if sun in in_month:
            b.days.append(sun)
        # 往後鏈假日（例：週一國定假日 → 三連休三天同一人）
        nxt = b.days[-1] + timedelta(days=1)
        while (nxt in in_month and nxt in holidays
               and not is_weekend(nxt) and nxt not in claimed):
            b.days.append(nxt)
            nxt += timedelta(days=1)
        # 往前鏈假日（例：週五國定假日 → 五六日同一人）；不吃已被前塊佔用的日
        prv = d - timedelta(days=1)
        while (prv in in_month and prv in holidays
               and not is_weekend(prv) and prv not in claimed):
            b.days.insert(0, prv)
            prv -= timedelta(days=1)
        blocks.append(b)
        claimed.update(b.days)

    blocks.sort(key=lambda b: b.days[0])
    return blocks


def block_of_day(blocks: list[DutyBlock], d: date) -> Optional[DutyBlock]:
    for b in blocks:
        if d in b.days:
            return b
    return None


# ─── 求解輸入上下文 ───────────────────────────────────────────────────────
@dataclass
class SolveContext:
    """solve_rvs 的完整輸入。scope: "r" 或 "vs"。

    leaves / must_duty: {member_id: set[date]}（請假 / 一定要值班）
    annual_holiday: {date: member_id}（年度國定假日指定值班表，該 scope 的表）
    locks: {date: member_id}（鎖定格＝重排不動）
    ledger: {member_id: float}（正=多值了、目標調低；負=欠、目標調高）
    week_colors: {"2026-W31": "pink"/"green"}（缺週 → 保守視為同色並警告）
    prev_last_weekend: 上月最後週末 (saturday_date, member_id) 或 None
    boundary_fix: {date: member_id} 跨月固定（如月初孤兒週日=上月週六人選）
    """
    scope: str
    year: int
    month: int
    members: list = field(default_factory=list)          # list[Member]
    holidays: set = field(default_factory=set)            # set[date]
    leaves: dict = field(default_factory=dict)
    must_duty: dict = field(default_factory=dict)
    annual_holiday: dict = field(default_factory=dict)
    locks: dict = field(default_factory=dict)
    ledger: dict = field(default_factory=dict)
    week_colors: dict = field(default_factory=dict)
    prev_last_weekend: Optional[tuple] = None
    boundary_fix: dict = field(default_factory=dict)
    # [2026-07-13 連續值班] 上月「最後 4 天」的已排值班 {date: member_id}(缺月檔
    # 或未排=空)。連續值班軟限制需要看跨月尾端,否則上月底連休鏈接本月初的 4/5 連
    # 看不見。僅供軟規則當常數使用,不產生任何硬約束。
    prev_tail: dict = field(default_factory=dict)
    params: RosterParams = field(default_factory=RosterParams)

    # 建構後由 prepare() 填入
    days: list = field(default_factory=list)
    blocks: list = field(default_factory=list)

    def prepare(self) -> "SolveContext":
        self.days = month_dates(self.year, self.month)
        self.blocks = build_duty_blocks(self.year, self.month, self.holidays)
        return self

    def member_ids(self) -> list[str]:
        return [m.id for m in self.members]

    def member_by_id(self, mid: str) -> Optional[Member]:
        for m in self.members:
            if m.id == mid:
                return m
        return None

    def on_leave(self, mid: str, d: date) -> bool:
        return d in (self.leaves.get(mid) or set())

    def total_points(self) -> int:
        return sum(day_point(d, self.holidays, self.params) for d in self.days)

    def color_of_block(self, b: DutyBlock) -> Optional[str]:
        """區塊週色；未設定回 None（呼叫端保守視為同色 + 警告）。"""
        return self.week_colors.get(week_key(b.color_anchor()))
