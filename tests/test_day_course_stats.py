# -*- coding: utf-8 -*-
"""PGY/Clerk 週期次數統計 + 月曆總覽（2026-07-23 使用者需求）。

  1. person_course_stats：photo/週三午照/tx/biopsy/跟診/放假 分類、include 過濾、
     起訖裁切、壞日期鍵略過（純函式）。
  2. service.day_course_stats：PGY=本月、Clerk=整梯（跨月合併另一半月份存檔）、
     preview override 蓋掉本月。
  3. format_course_stats：0 切片 Clerk 帶「未排切片」標記。
  4. 公平性釘位（使用者需求語意）：整月照光/治療室次數 spread ≤1、週三下午照光
     獨立 spread ≤1；切片室每人至少 1 次（時段足夠時）。
  5. _day_cell_text：月曆總覽單日格摘要（純函式）。
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.solve_day import (  # noqa: E402
    BIOPSY, PHOTO, REST, STAT_KEYS, TREATMENT, DaySolveInput, FairCounters,
    format_course_stats, month_solve_day, person_course_stats, solve_session,
)
from cmuh_common.roster.storage import RosterStorage  # noqa: E402
from cmuh_common.roster.ui.day_tab import _day_cell_text  # noqa: E402

WED_ISO = "2026-08-05"      # 2026/8/5 = 週三


# ─── person_course_stats（純函式）─────────────────────────────────────────
def _sample_slots():
    return {
        WED_ISO: {"上午": {PHOTO: ["A"], TREATMENT: ["B"], "101": ["C", "D"],
                          REST: ["E"]},
                  "下午": {PHOTO: ["B"], BIOPSY: ["K1"], "102": ["A"]}},
        "2026-08-06": {"上午": {PHOTO: ["C"], "101": ["A"]}},
        "bad-key": {"上午": {PHOTO: ["Z"]}},
    }


def test_stats_classification_and_wed_pm():
    st = person_course_stats(_sample_slots())
    assert st["A"] == {"photo": 1, "photo_wed_pm": 0, "tx": 0, "biopsy": 0,
                       "follow": 2, "rest": 0}
    # B：週三【下午】照光 → photo 與 photo_wed_pm 同時 +1；上午治療室 tx+1
    assert st["B"]["photo"] == 1 and st["B"]["photo_wed_pm"] == 1
    assert st["B"]["tx"] == 1
    assert st["K1"]["biopsy"] == 1
    assert st["E"]["rest"] == 1
    assert "Z" not in st, "壞日期鍵應整鍵略過"


def test_stats_include_filter_and_date_bounds():
    st = person_course_stats(_sample_slots(), include={"A"})
    assert set(st) == {"A"}
    st2 = person_course_stats(_sample_slots(), start=date(2026, 8, 6),
                              end=date(2026, 8, 6))
    assert "B" not in st2 and st2["C"]["photo"] == 1   # 只剩 8/6


def test_format_marks_clerk_without_biopsy():
    txt = format_course_stats(
        {}, ["P1"],
        [{"id": "b1", "start": "2026-08-03", "end": "2026-08-16",
          "members": ["K1", "K2"],
          "stats": {"K1": dict.fromkeys(STAT_KEYS, 0) | {"biopsy": 1}}}])
    assert "未排切片" in txt          # K2 全零 → 標記
    assert txt.count("未排切片") == 1  # K1 有排 → 不標


# ─── service.day_course_stats（跨月梯次合併 + override）───────────────────────
def _svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({"pgy_members": [{"id": "P1"}, {"id": "P2"}],
                    "points": {"weekday": 1, "weekend": 2}})
    # 梯次 2026-07-27(一)～08-09：跨 7/8 月
    st.save_clerk_batches([{"id": "bx", "start_monday": "2026-07-27",
                            "members": ["K1", "K2"]}])
    return RosterService(st), st


def test_day_course_stats_merges_cross_month_batch(tmp_path):
    svc, st = _svc(tmp_path)
    m7 = st.load_month("2026-07")
    m7["day_slots"] = {"2026-07-28": {"上午": {BIOPSY: ["K1"], "101": ["K2"]}}}
    st.save_month("2026-07", m7)
    m8 = st.load_month("2026-08")
    m8["day_slots"] = {"2026-08-04": {"上午": {BIOPSY: ["K2"], PHOTO: ["P1"]}}}
    st.save_month("2026-08", m8)

    data = svc.day_course_stats("2026-08")
    assert data["pgy"]["stats"]["P1"]["photo"] == 1
    b = next(x for x in data["batches"] if x["id"] == "bx")
    assert b["stats"]["K1"]["biopsy"] == 1, "上月(7/28)的切片要併進整梯統計"
    assert b["stats"]["K2"]["biopsy"] == 1 and b["stats"]["K2"]["follow"] == 1


def test_day_course_stats_pgy_bounded_to_month(tmp_path):
    """[codex P2] 月檔 day_slots 殘留跨月 iso 鍵（set_day_slot 不強制 d∈ym）→
    PGY 月統計要以月份起訖裁切，不得灌入他月次數。"""
    svc, st = _svc(tmp_path)
    m8 = st.load_month("2026-08")
    m8["day_slots"] = {"2026-08-04": {"上午": {PHOTO: ["P1"]}},
                       "2026-09-01": {"上午": {PHOTO: ["P1"]}}}   # 跨月殘留
    st.save_month("2026-08", m8)
    data = svc.day_course_stats("2026-08")
    assert data["pgy"]["stats"]["P1"]["photo"] == 1, "9/1 的殘留不得計入 8 月"


def test_day_course_stats_stale_cross_month_key_does_not_override(tmp_path):
    """[codex P2] 8 月檔殘留 7 月的 iso 鍵 → 不得蓋掉 7 月檔的權威內容（各月檔只採本月鍵）。"""
    svc, st = _svc(tmp_path)
    m7 = st.load_month("2026-07")
    m7["day_slots"] = {"2026-07-28": {"上午": {BIOPSY: ["K1"]}}}   # 權威:K1 切片
    st.save_month("2026-07", m7)
    m8 = st.load_month("2026-08")
    m8["day_slots"] = {"2026-07-28": {"上午": {BIOPSY: ["K2"]}}}   # 8月檔殘留假的 7/28
    st.save_month("2026-08", m8)
    data = svc.day_course_stats("2026-08")
    b = next(x for x in data["batches"] if x["id"] == "bx")
    assert b["stats"]["K1"]["biopsy"] == 1, "7/28 應以 7 月檔為權威"
    assert b["stats"].get("K2", {}).get("biopsy", 0) == 0, "殘留鍵不得蓋掉權威內容"


def test_day_course_stats_override_replaces_current_month(tmp_path):
    svc, st = _svc(tmp_path)
    m8 = st.load_month("2026-08")
    m8["day_slots"] = {"2026-08-04": {"上午": {PHOTO: ["P1"]}}}
    st.save_month("2026-08", m8)
    preview = {"2026-08-04": {"上午": {PHOTO: ["P2"], BIOPSY: ["K1"]}}}
    data = svc.day_course_stats("2026-08", day_slots_override=preview)
    assert "P1" not in data["pgy"]["stats"], "override 應完全取代本月存檔"
    assert data["pgy"]["stats"]["P2"]["photo"] == 1
    b = next(x for x in data["batches"] if x["id"] == "bx")
    assert b["stats"]["K1"]["biopsy"] == 1


def test_day_slots_with_locks_matches_accept(tmp_path):
    """[codex P2] 預覽統計的鎖定合併必須與 accept 落地內容一致：鎖定時段不在 preview
    （掉出格網）時，day_slots_with_locks 要把它補回來。"""
    svc, st = _svc(tmp_path)
    m8 = st.load_month("2026-08")
    m8["day_slots"] = {"2026-08-10": {"上午": {PHOTO: ["P1"]}}}
    m8["day_locks"] = {"2026-08-10": {"上午": True}}
    st.save_month("2026-08", m8)
    preview = {"2026-08-11": {"上午": {PHOTO: ["P2"]}}}      # preview 缺鎖定日
    merged = svc.day_slots_with_locks("2026-08", preview)
    assert merged["2026-08-10"]["上午"] == {PHOTO: ["P1"]}, "鎖定時段應補回"
    assert merged["2026-08-11"]["上午"] == {PHOTO: ["P2"]}
    # 與 accept 落地內容一致
    svc.accept_day_solution("2026-08", preview)
    assert st.load_month("2026-08")["day_slots"] == merged


# ─── 公平性釘位（使用者需求語意；底層機制既有,此處鎖 spread）────────────────────
def _spread(counts: dict, people) -> int:
    vals = [counts.get(p, 0) for p in people]
    return max(vals) - min(vals) if vals else 0


def test_month_photo_tx_and_wed_pm_spread_at_most_one():
    """整月照光/治療室每人次數盡量一致（spread≤1），週三下午照光另獨立平均（spread≤1）。"""
    pgy = ["P1", "P2", "P3"]
    grid = {}
    d = date(2026, 8, 3)                      # 週一起連續 4 週工作日
    while d <= date(2026, 8, 28):
        if d.weekday() < 5:
            grid[d] = {"上午": ["101"], "下午": [] if d.weekday() == 2 else ["101"]}
        d += timedelta(days=1)
    day_slots, _log, _warn = month_solve_day(DaySolveInput(
        ym="2026-08", grid=grid, pgy_roster=pgy))
    st = person_course_stats(day_slots, include=set(pgy))
    photo = {p: st.get(p, {}).get("photo", 0) for p in pgy}
    tx = {p: st.get(p, {}).get("tx", 0) for p in pgy}
    wed = {p: st.get(p, {}).get("photo_wed_pm", 0) for p in pgy}
    assert _spread(photo, pgy) <= 1, f"照光 spread>1: {photo}"
    assert _spread(tx, pgy) <= 1, f"治療室 spread>1: {tx}"
    assert _spread(wed, pgy) <= 1, f"週三下午照光 spread>1: {wed}"
    assert sum(wed.values()) == 4              # 8 月有 4 個週三下午


def test_batch_every_clerk_gets_biopsy_when_enough_sessions():
    """切片室開放時段 ≥ 梯次人數 → 每位 Clerk 至少輪到一次（不靠警告）。"""
    fc = FairCounters()
    clerks = ["K1", "K2", "K3"]
    d = date(2026, 8, 3)
    opened = 0
    while opened < 4:                          # 4 個開放時段 > 3 人
        if d.weekday() < 5 and not (d.weekday() == 2):
            solve_session(d, "上午", ["101"], [], list(clerks), True, fc,
                          batch_key="bx")
            opened += 1
        d += timedelta(days=1)
    assert all(fc.biopsy_done.get(("bx", c), 0) >= 1 for c in clerks), \
        f"每位 Clerk 至少一次切片: {fc.biopsy_done}"


# ─── 月曆總覽單日格（純函式）───────────────────────────────────────────────
def test_day_cell_text_summary():
    txt = _day_cell_text(date(2026, 8, 5), {
        "上午": {PHOTO: ["A"], TREATMENT: ["B"], "101": ["C", "D"], REST: ["E"]},
        "下午": {PHOTO: ["F"]},
    })
    assert txt.splitlines()[0].startswith("5")
    assert "照:A" in txt and "治:B" in txt
    assert "101:C、D" in txt
    assert "休:E" in txt
    assert "午 照:F" in txt


def test_day_cell_text_empty_sessions_omitted():
    txt = _day_cell_text(date(2026, 8, 5), {})
    assert txt.splitlines() == ["5（三）"]
