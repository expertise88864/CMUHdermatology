# -*- coding: utf-8 -*-
"""[2026-08-02 補審第 2 輪] 週五↔週六切片連動的【服務層接線】。

兩條 finding 都不是純函式錯,而是「純函式新依賴了週五值班,但呼叫端沒跟上」:
  P2-1 set_cell() 只在改到【週六】時重排切片 → 改完週五之後,月檔/biopsy.json/
       決策報告/匯出全都還是舊人選,而且畫面上看不出來。
  P2-2 recompute_saturday_biopsy() 把非當月日期全部丟掉 → 月初 1 號就是週六時,
       它的週五在【上個月】,正式路徑永遠看不到 → 那些月份連動完全失效。

★我原本的測試直接把 7/31 塞進純函式的 duty,沒走服務層,所以給了假信心。★
本檔一律走 RosterService。
"""
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.storage import RosterStorage  # noqa: E402

R1, R2, R3 = "r1", "r2", "r3"


def _svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({"r_members": [
        {"id": R1, "name": "甲", "level": "R1"},
        {"id": R2, "name": "乙", "level": "R2"},
        {"id": R3, "name": "丙", "level": "R3"}]})
    return RosterService(st), st


def _month_with_duty(duty: dict) -> dict:
    return {"r_duty": {d.isoformat(): {"person": p, "locked": False,
                                       "source": "test"}
                       for d, p in duty.items()}}


def _biopsy_person(month: dict, d: date):
    cell = (month.get("saturday_biopsy") or {}).get(d.isoformat()) or {}
    return cell.get("person")


# ─── P2-2:月初就是週六 → 週五在上個月 ─────────────────────────────────────
def test_first_of_month_saturday_sees_previous_month_friday(tmp_path):
    """2026-08-01 是週六,它的週五 7/31 在上個月月檔裡。
    週六值班是 R1(不走值班連動)、R2/R3 次數平手 → 應由週五連動決定 = R3。"""
    svc, st = _svc(tmp_path)
    st.save_month("2026-07", _month_with_duty({date(2026, 7, 31): R3}))
    st.save_month("2026-08", _month_with_duty({date(2026, 8, 1): R1}))
    assign, _notes, _book = svc.recompute_saturday_biopsy("2026-08")
    cell = assign[date(2026, 8, 1)]
    assert cell["person"] == R3, "應由上月最後一天(週五)的值班人決定"
    assert "週五連動" in cell["reason"]


def test_missing_previous_month_file_does_not_break_recompute(tmp_path):
    """沒有上月資料 → 連動單純不生效,不可讓整個切片重排失敗。"""
    svc, st = _svc(tmp_path)
    st.save_month("2026-08", _month_with_duty({date(2026, 8, 1): R1}))
    assign, _notes, _book = svc.recompute_saturday_biopsy("2026-08")
    assert assign[date(2026, 8, 1)]["person"] in (R2, R3)


def test_non_saturday_first_day_does_not_read_previous_month(tmp_path, monkeypatch):
    """1 號不是週六 → 週五在本月,不該多做一次跨月讀檔。"""
    svc, st = _svc(tmp_path)
    st.save_month("2026-09", _month_with_duty({date(2026, 9, 5): R1}))
    assert svc._prev_month_friday_duty(2026, 9) == {}


# ─── P2-1:改到週五也要重排 ────────────────────────────────────────────────
# 2026/08 的週六:1、8、15、22、29。週五連動只在【次數平手】時決勝,
# 所以要挑一個真的平手的週六:讓 8/1 給 R2、8/8 給 R3(皆走值班連動)→
# 到 8/15 時兩人各 1 次、平手,此時 8/15 值班給 R1(不連動)→ 由週五 8/14 決定。
# (我第一版挑 8/8,那時 8/1 已先取走一次 → 其實是次數決定的,不是週五。)
def _tied_at_1508(friday_person: str) -> dict:
    return _month_with_duty({
        date(2026, 8, 1): R2, date(2026, 8, 8): R3,
        date(2026, 8, 15): R1, date(2026, 8, 14): friday_person})


def test_editing_a_friday_recomputes_saturday_biopsy(tmp_path):
    """★核心★ 手改週五值班 → 切片人選必須跟著重算並【存進月檔】。"""
    svc, st = _svc(tmp_path)
    st.save_month("2026-08", _tied_at_1508(R2))
    svc.recompute_saturday_biopsy("2026-08")
    sat = date(2026, 8, 15)
    assert _biopsy_person(st.load_month("2026-08"), sat) == R2,         "初始:次數平手 + 週五是 R2 → 連動選 R2"

    svc.set_cell("r", "2026-08", date(2026, 8, 14), R3, via="test")
    assert _biopsy_person(st.load_month("2026-08"), sat) == R3,         "改完週五之後,月檔裡的切片人選必須跟著變"


def test_editing_a_friday_also_updates_the_counter_book(tmp_path):
    """帳本也要同步 —— 否則後續月份的次數平衡會用到過期的累計。"""
    svc, st = _svc(tmp_path)
    st.save_month("2026-08", _tied_at_1508(R2))
    svc.recompute_saturday_biopsy("2026-08")
    svc.set_cell("r", "2026-08", date(2026, 8, 14), R3, via="test")
    book = st.load_biopsy()
    entry = [e for e in (book.get("history") or []) if e.get("month") == "2026-08"]
    assert entry, "帳本要有本月分錄"
    assert entry[0]["assign"].get("2026-08-15") == R3, "帳本要記到改後的人選"


def test_month_end_friday_recomputes_the_next_month(tmp_path):
    """★[補審第 2 輪第 2 次] 我上一版把過期行為寫進測試釘死了★

    我原本寫「月底的週五翌日已跨月,不影響本月,不必重排」—— 但同一批的另一個
    修正正好讓那個週五【影響下個月】(月初 1 號是週六時會去讀它)。兩者自相矛盾:
    改 7/31 之後,2026-08 的切片人選/帳本/報告全都停在舊值。
    """
    svc, st = _svc(tmp_path)
    st.save_month("2026-07", _month_with_duty({date(2026, 7, 31): R2}))
    st.save_month("2026-08", _month_with_duty({date(2026, 8, 1): R1}))
    svc.recompute_saturday_biopsy("2026-08")
    sat = date(2026, 8, 1)
    assert _biopsy_person(st.load_month("2026-08"), sat) == R2

    svc.set_cell("r", "2026-07", date(2026, 7, 31), R3, via="test")
    assert _biopsy_person(st.load_month("2026-08"), sat) == R3,         "改完月底週五之後,下個月的切片人選必須跟著變"


def test_month_end_friday_without_next_month_file_is_safe(tmp_path):
    """下個月還沒排過 → 不可憑空生出一份月檔。"""
    svc, st = _svc(tmp_path)
    st.save_month("2026-07", _month_with_duty({date(2026, 7, 31): R2}))
    svc.set_cell("r", "2026-07", date(2026, 7, 31), R3, via="test")
    assert not st.month_exists("2026-08")


def test_month_end_friday_skips_a_finalized_next_month(tmp_path):
    """下個月已定案(唯讀)→ 跳過,不可丟 FinalizedMonthError 汙染 log。"""
    svc, st = _svc(tmp_path)
    st.save_month("2026-07", _month_with_duty({date(2026, 7, 31): R2}))
    m8 = _month_with_duty({date(2026, 8, 1): R1})
    m8["finalized"] = True
    st.save_month("2026-08", m8)
    svc.set_cell("r", "2026-07", date(2026, 7, 31), R3, via="test")
    assert st.load_month("2026-08").get("finalized") is True


def test_report_preview_matches_what_gets_persisted(tmp_path):
    """★預覽與定案不可不一致★ render_report 走 _biopsy_compute,跨月週五必須收攏
    在那裡(而不是各呼叫端自己補),否則「月初 1 號是週六」的月份會預覽到別人。"""
    src = open(os.path.join(os.path.dirname(__file__), '..', 'src',
                            'cmuh_common', 'roster', 'service.py'),
               encoding='utf-8').read()
    i = src.index("def _biopsy_compute(")
    j = src.index("def recompute_saturday_biopsy(", i)
    assert "_prev_month_friday_duty(y, m)" in src[i:j],         "跨月週五要收在唯一入口 _biopsy_compute"
    i2 = src.index("def recompute_saturday_biopsy(")
    j2 = src.index("def render_report(", i2)
    assert "_prev_month_friday_duty" not in src[i2:j2],         "不可再由呼叫端各自補一次(那正是預覽會不一致的原因)"


def test_editing_a_saturday_still_recomputes(tmp_path):
    """既有行為不可退化。"""
    svc, st = _svc(tmp_path)
    st.save_month("2026-08", _month_with_duty({date(2026, 8, 8): R1}))
    svc.recompute_saturday_biopsy("2026-08")
    svc.set_cell("r", "2026-08", date(2026, 8, 8), R2, via="test")
    month = st.load_month("2026-08")
    assert _biopsy_person(month, date(2026, 8, 8)) == R2, "值班連動:週六值班者切片"
    assert json.dumps(month, ensure_ascii=False)


def test_in_month_friday_does_not_trigger_a_second_save(tmp_path, monkeypatch):
    """★週五的隔天永遠是週六★ 跨月判斷必須看【月份】,不是星期幾。
    寫成 `next.weekday() == 5` 會讓每一次月內週五修改都額外重排並再存一次本月
    → 重複快照與多餘 IO(補審第 2 輪第 3 次抓到)。"""
    svc, st = _svc(tmp_path)
    st.save_month("2026-08", _tied_at_1508(R2))
    svc.recompute_saturday_biopsy("2026-08")
    calls = []
    real = svc.recompute_saturday_biopsy
    monkeypatch.setattr(svc, "recompute_saturday_biopsy",
                        lambda ym, month=None: calls.append(ym) or real(ym, month))
    svc.set_cell("r", "2026-08", date(2026, 8, 14), R3, via="test")
    assert calls == ["2026-08"], f"月內週五只該重排一次,實際 {calls}"


def test_cross_month_friday_triggers_exactly_one_extra_recompute(tmp_path, monkeypatch):
    svc, st = _svc(tmp_path)
    st.save_month("2026-07", _month_with_duty({date(2026, 7, 31): R2}))
    st.save_month("2026-08", _month_with_duty({date(2026, 8, 1): R1}))
    calls = []
    real = svc.recompute_saturday_biopsy
    monkeypatch.setattr(svc, "recompute_saturday_biopsy",
                        lambda ym, month=None: calls.append(ym) or real(ym, month))
    svc.set_cell("r", "2026-07", date(2026, 7, 31), R3, via="test")
    assert calls == ["2026-08"], f"7/31 是月底週五 → 只重排下個月,實際 {calls}"
