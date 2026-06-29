# -*- coding: utf-8 -*-
"""UVB / excimer 黃金樣本回歸測試(golden samples)。

把【真實病歷格式的輸入 + 今天日期 → 預期 detect 分流 / action / 劑量 / 寫回內容】固定成一張表,
涵蓋:(1) compute_new_dose 的天數規則(遞增/保持/衰減/長間隔/太近/過舊);(2) 本院實機分流案例
(陳韻璇 / 林怡君 / 簡子泰 等)與後續 Codex 審查補強的邊角(1-2 月舊 UVB、日期寫在關鍵字前)。

用途:照光劑量算錯=醫療事故、分流錯=健保/自費帳錯。任何改動(尤其把劑量規則外部化成 JSON、或動
detect_phototherapy_kind)後跑這張表,確保「修一個 case 不會弄壞另一個」。新案例請往 GOLDEN 加一列。

每列斷言:detect == kind、action 一致、(可選)new_dose 一致、contains 子字串都在 new_text、preserves
子字串(舊行/別欄位)原封保留在 new_text。
"""
import os
import sys
from dataclasses import dataclass
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.uvb_dose import (  # noqa: E402
    UvbAction,
    detect_phototherapy_kind,
    update_uvb_in_text,
)


@dataclass
class Golden:
    name: str                       # 案例名(實機病人或情境)
    text: str                       # 處置欄原文
    today: date                     # 處理當天
    detect: str                     # 預期 detect_phototherapy_kind
    action: str                     # 預期 UvbAction
    new_dose: "int | None" = None   # 預期新劑量(None=不檢查,如 too_close/confirm)
    contains: tuple = ()            # 這些子字串必須出現在寫回後的 new_text
    preserves: tuple = ()           # 這些(舊行/別欄位)必須原封保留在 new_text


_T = date(2026, 6, 24)

GOLDEN = [
    # ── compute_new_dose 天數規則(單一 UVB 行,detect=uvb)──────────────────
    Golden("UVB_遞增_2天", "UVB: 500 mj/cm2 (5) on (2026/06/22), increase 30, MAX:800",
           _T, "uvb", UvbAction.UPDATED, 530,
           contains=("530 mj/cm2 (6) on (2026/06/24)",)),
    Golden("UVB_保持_7天", "UVB: 500 mj/cm2 (5) on (2026/06/17), increase 30, MAX:800",
           _T, "uvb", UvbAction.UPDATED, 500,
           contains=("500 mj/cm2 (6) on (2026/06/24)",)),
    Golden("UVB_衰減075_12天", "UVB: 500 mj/cm2 (5) on (2026/06/12), increase 30, MAX:800",
           _T, "uvb", UvbAction.UPDATED, 370,
           contains=("370 mj/cm2 (6) on (2026/06/24)",)),
    Golden("UVB_衰減05_19天", "UVB: 500 mj/cm2 (5) on (2026/06/05), increase 30, MAX:800",
           _T, "uvb", UvbAction.UPDATED, 250),
    Golden("UVB_長間隔_25天", "UVB: 500 mj/cm2 (5) on (2026/05/30), increase 30, MAX:800",
           _T, "uvb", UvbAction.UPDATED, 250),
    Golden("UVB_太近_1天", "UVB: 500 mj/cm2 (5) on (2026/06/23), increase 30, MAX:800",
           _T, "uvb", UvbAction.TOO_CLOSE),
    Golden("UVB_過舊_45天", "UVB: 500 mj/cm2 (5) on (2026/05/10), increase 30, MAX:800",
           _T, "uvb", UvbAction.CONFIRM_NEEDED),

    # ── 簡子泰:關鍵字後接中文描述 + 冒號劑量(有 mj)──────────────────────
    Golden("簡子泰_中文desc冒號劑量",
           "UVB局部臉和後背: 440 mj/cm2(9) on (2026/6/24) add 30 each time, fixed at 1000mj/cm2",
           date(2026, 6, 26), "uvb", UvbAction.UPDATED, 470,
           contains=("470 mj/cm2(10) on (2026/06/26)",)),

    # ── 純 excimer(detect=pure_excimer,身份 01)─────────────────────────
    Golden("excimer_正常更新",
           "excimer light 700 mj/cm2 (138) on (2026/06/20), increase 50, max 1500",
           _T, "pure_excimer", UvbAction.UPDATED, 750,
           contains=("750 mj/cm2 (139) on (2026/06/24)",)),

    # ── 陳韻璇:病史只「提到」UVB(無劑量)+ excimer → pure_excimer,不卡住 ──
    Golden("陳韻璇_病史bareUVB加excimer",
           "Previous tx: UVB at singapore\n"
           "excimer light 700 mj/cm2 (138) on (2026/06/20), increase 50, max 1500",
           _T, "pure_excimer", UvbAction.UPDATED, 750,
           contains=("750 mj/cm2 (139) on (2026/06/24)",),
           preserves=("Previous tx: UVB at singapore",)),

    # ── 林怡君:近期 excimer + 一年多前舊 UVB → 更新 excimer、保留舊 UVB ─────
    Golden("林怡君_近期excimer加很舊UVB",
           "excimer light 700 mj/cm2 (138) on (2026/06/20), increase 50, max 1500\n"
           "UVB: 1500 mj/cm2 (232) on (2024/11/12)",
           _T, "pure_excimer", UvbAction.UPDATED, 750,
           contains=("750 mj/cm2 (139) on (2026/06/24)",),
           preserves=("UVB: 1500 mj/cm2 (232) on (2024/11/12)",)),

    # ── Codex r8:1-2 個月舊 UVB + 近期 excimer → 讓位給 excimer ───────────
    Golden("r8_45天舊UVB加近期excimer",
           "excimer light 700 mj/cm2 (138) on (2026/06/25), increase 50, max 1500\n"
           "UVB: 1500 mj/cm2 (232) on (2026/05/15), increase 50, max 2000",
           date(2026, 6, 29), "pure_excimer", UvbAction.UPDATED, 750,
           contains=("750 mj/cm2 (139) on (2026/06/29)",),
           preserves=("UVB: 1500 mj/cm2 (232) on (2026/05/15)",)),

    # ── Codex r9:日期寫在 UVB 關鍵字前(楊亮筠格式)+ 近期 excimer ─────────
    Golden("r9_日期在關鍵字前的舊UVB",
           "(2026/05/15) UVB 500 mj/cm2 increase 30 max 900\n"
           "excimer light 700 mj/cm2 (8) on (2026/06/25), increase 50, max 1500",
           date(2026, 6, 29), "pure_excimer", UvbAction.UPDATED, 750,
           contains=("750 mj/cm2 (9) on (2026/06/29)",),
           preserves=("(2026/05/15) UVB 500 mj/cm2",)),

    # ── 近期 UVB + 近期 excimer 並存(同日)→ uvb(合併治療,健保不漏 key)──
    Golden("近期UVB加近期excimer並存",
           "UVB: 500 mj/cm2 (5) on (2026/06/22) increase 30 max 800\n"
           "excimer light 700 mj/cm2 (8) on (2026/06/22)",
           _T, "uvb", UvbAction.UPDATED, 530,
           contains=("530 mj/cm2 (6) on (2026/06/24)",),
           preserves=("excimer light 700 mj/cm2 (8) on (2026/06/22)",)),
]


@pytest.mark.parametrize("g", GOLDEN, ids=[g.name for g in GOLDEN])
def test_uvb_golden_sample(g: Golden):
    assert detect_phototherapy_kind(g.text, g.today) == g.detect, "分流(detect)不符"
    r = update_uvb_in_text(g.text, g.today)
    assert r.action == g.action, f"action 不符:{r.action}"
    if g.new_dose is not None:
        assert r.new_dose == g.new_dose, f"new_dose 不符:{r.new_dose}"
    nt = r.new_text or ""
    for sub in g.contains:
        assert sub in nt, f"new_text 缺少預期內容:{sub!r}\n實際:{nt!r}"
    for sub in g.preserves:
        assert sub in nt, f"未保留原本應原封不動的內容:{sub!r}\n實際:{nt!r}"
