# -*- coding: utf-8 -*-
"""R 一線值班的年度週末次數（2026-10-19 使用者定案）。

★一次怎麼算★
  一個週末塊（週六＋週日，含鏈進來的國定假日）算 1 次，以【週六所在月份】
  計（10/31 六 → 算 10 月；月初只有週日的孤兒塊不算，它屬上月那個週末）。
  連假塊即使很長也只算 1 次 —— 負擔已經反映在點數裡。
  年度假日表勾了「不計入年度週末」的日期（春節）所在的週末塊不計入：
  春節由 R1 前半、R2 後半指定值班，不參與週末分配。

★目標★
  每個 8 月～隔年 7 月的週期內，R1:R2:R3 的週末次數盡量依權重分配
  （預設 3.6 : 3.3 : 3.1，設定頁可改）：R1 最多、R2 次之、R3 最少，但不差太多。
  人數變動時一律按【當時在名單上的人】重新分配（離開者的次數不再計入）。

★它是最低優先★
  求解器先照所有規則與點數解出最佳班表，把那個目標值固定住，才在「一樣好」
  的排法之間挑週末次數最接近目標的那一個（嚴格版：不犧牲任何既有規則）。

本模組只有純函式，無檔案 IO。
"""
from __future__ import annotations

from datetime import date, timedelta

from cmuh_common.roster.model import build_duty_blocks

#: 預設權重（使用者定案 2026-10-19）。
DEFAULT_WEIGHTS = {"R1": 3.6, "R2": 3.3, "R3": 3.1}

#: config.json 的鍵：{"R1": 3.6, "R2": 3.3, "R3": 3.1}
CONFIG_KEY = "weekend_share"


def level_weights(cfg: dict) -> dict:
    """config → {級別: 權重}。缺鍵/壞值 → 該級用預設；全部壞掉 → 整份預設。"""
    raw = cfg.get(CONFIG_KEY)
    out = dict(DEFAULT_WEIGHTS)
    if isinstance(raw, dict):
        for lvl, w in raw.items():
            try:
                f = float(w)
            except (TypeError, ValueError):
                continue
            if f > 0:
                out[str(lvl).strip().upper()] = f
    return out


def member_weights(members, weights_by_level: dict) -> dict:
    """{member_id: 權重}。名單上的級別不在權重表裡 → 用權重表的平均值
    （例如有 R4 或級別空白時，不偏向任何一方）。"""
    if not members:
        return {}
    known = [w for w in weights_by_level.values() if w > 0]
    fallback = sum(known) / len(known) if known else 1.0
    return {m.id: float(weights_by_level.get((m.level or "").strip().upper(),
                                             fallback))
            for m in members}


def weekend_chain(saturday: date, holidays) -> list:
    """週六所在的【完整】週末連休段(不受月份裁切),★歸屬規則與月內建塊相同★。

    `build_duty_blocks` 只在本月內建塊,所以 10/31(六) 的塊只有它自己 —— 而
    11/1(日) 被標「不計入」時,那個週末整個都不該算(外審 PR #9 第 1 輪)。
    但不可以單純往前後鏈假日:週一～週五連續假日已被【前一個週末】往後鏈走時,
    後一個週末往前鏈不到它們(外審 PR #9 第 2 輪)。所以在週六前後三週的範圍內,
    照 build_duty_blocks 的同一套順序(週六由早到晚、先往後鏈再往前鏈、不吃已
    被佔用的日)重建,取包含這個週六的那一塊。"""
    hol = set(holidays or ())
    start = saturday - timedelta(days=21)
    end = saturday + timedelta(days=21)
    span = {start + timedelta(days=i) for i in range((end - start).days + 1)}
    claimed: set = set()
    d = start + timedelta(days=(5 - start.weekday()) % 7)      # 範圍內第一個週六
    while d <= end:
        block = [d]
        if d + timedelta(days=1) in span:
            block.append(d + timedelta(days=1))
        nxt = block[-1] + timedelta(days=1)
        while nxt in span and nxt in hol and nxt.weekday() < 5 and nxt not in claimed:
            block.append(nxt)
            nxt += timedelta(days=1)
        prv = d - timedelta(days=1)
        while prv in span and prv in hol and prv.weekday() < 5 and prv not in claimed:
            block.insert(0, prv)
            prv -= timedelta(days=1)
        claimed.update(block)
        if d == saturday:
            return block
        d += timedelta(days=7)
    return [saturday, saturday + timedelta(days=1)]


def counted_blocks(blocks, exempt, holidays=()) -> list:
    """會計入年度週末的週末塊：含本月週六、且整個週末連休段(含跨月的那一半)
    沒有任何一天被標「不計入」。"""
    exempt = set(exempt or ())
    if not exempt:
        return [b for b in blocks if b.saturday is not None]
    return [b for b in blocks
            if b.saturday is not None
            and not any(d in exempt for d in b.days)
            and not any(d in exempt for d in weekend_chain(b.saturday, holidays))]


def month_units(year: int, month: int, duty: dict, holidays, exempt) -> dict:
    """某月每人的週末次數 {member_id: 次}。duty: {date: member_id}。

    以【週六那天的人】為準：連休段被使用者指定拆給不同人時，值週六的人才是
    「值了這個週末」的人（與 WeekendCapRule、last_weekend 同一個判準）。"""
    out: dict = {}
    blocks = build_duty_blocks(year, month, set(holidays or ()))
    for b in counted_blocks(blocks, exempt, holidays):
        mid = duty.get(b.saturday)
        if mid:
            out[mid] = out.get(mid, 0) + 1
    return out


def duty_map(month_doc: dict, scope: str = "r") -> dict:
    """月檔 → {date: member_id}（壞日期/空格略過）。"""
    out: dict = {}
    for iso, cell in ((month_doc or {}).get(f"{scope}_duty") or {}).items():
        if not isinstance(cell, dict):
            continue
        person = str(cell.get("person") or "")
        if not person:
            continue
        try:
            out[date.fromisoformat(iso)] = person
        except (TypeError, ValueError):
            continue
    return out


def cumulative_targets(prior: dict, weights: dict, units_this_month: int) -> dict:
    """本月結束時，每人的累計週末目標 {member_id: 次(浮點)}。

    = (名單上每人本週期之前的次數總和 + 本月計入的週末數) × 權重占比。
    只看【目前名單】(weights 的鍵)：離開的人不再分配,新來的人從 0 起算。"""
    total_w = sum(weights.values())
    if total_w <= 0:
        return {}
    pool = sum(int(prior.get(mid, 0)) for mid in weights) + int(units_this_month)
    return {mid: pool * w / total_w for mid, w in weights.items()}


def year_targets(total_units: int, weights: dict) -> dict:
    """整個週期的週末目標 {member_id: 次(浮點)}（畫面/報告用）。"""
    total_w = sum(weights.values())
    if total_w <= 0:
        return {}
    return {mid: total_units * w / total_w for mid, w in weights.items()}


def cycle_year_units(cycle_yms, holidays, exempt) -> int:
    """整個週期會計入的週末塊總數（用年度假日表推算；尚未排班的月份也算）。"""
    total = 0
    for ym in cycle_yms:
        y, m = int(ym[:4]), int(ym[5:7])
        total += len(counted_blocks(build_duty_blocks(y, m, set(holidays or ())),
                                    exempt, holidays))
    return total


def year_summary(ctx, duty_this_month: dict) -> list:
    """R 年度週末統計（畫面/報告共用）→ [{id,name,level,done,to_date,year}]。

    done＝本週期之前＋本月(duty_this_month: {date: member_id})已值的週末；
    to_date＝到本月為止依權重應值的次數；year＝整個週期(8–7 月)的目標。
    全部只看【目前名單】,按當時在班的人重新分配。"""
    from cmuh_common.roster.cycle import full_cycle_months  # noqa: PLC0415
    weights = {m.id: float((ctx.weekend_weights or {}).get(m.id) or 0)
               for m in ctx.members}
    weights = {mid: w for mid, w in weights.items() if w > 0}
    if not weights:
        return []
    blocks = counted_blocks(ctx.blocks, ctx.weekend_exempt, ctx.holidays)
    this_month: dict = {}
    for b in blocks:
        mid = duty_this_month.get(b.saturday)
        if mid:
            this_month[mid] = this_month.get(mid, 0) + 1
    ym = f"{ctx.year:04d}-{ctx.month:02d}"
    to_date = cumulative_targets(ctx.weekend_prior, weights, len(blocks))
    year = year_targets(cycle_year_units(full_cycle_months(ym), ctx.holidays,
                                         ctx.weekend_exempt), weights)
    rows = []
    for m in ctx.members:
        if m.id not in weights:
            continue
        rows.append({
            "id": m.id, "name": m.name, "level": m.level,
            "done": int(ctx.weekend_prior.get(m.id, 0)) + this_month.get(m.id, 0),
            "to_date": round(to_date.get(m.id, 0.0), 1),
            "year": round(year.get(m.id, 0.0), 1)})
    return rows
