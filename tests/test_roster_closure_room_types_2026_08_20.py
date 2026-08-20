# -*- coding: utf-8 -*-
"""[批次RS-11 / 2026-08-20 使用者回報] 停診按了沒反應、沒有紀錄、恢復也一樣。

根因是型別縫隙:UI 建的模板房號是字串,但多機人工改 JSON 是設計內流程,
編輯器/人手很容易把 "101" 存成數字 101。下拉選單顯示的是 str(room),
`month_grid` 回傳的卻是原始型別 —— `"101" not in [101]` 讓停診迴圈把
★每一天★都當成「本來就沒開這室」跳過:不寫、不報錯、audit 照記、
對話框照關。使用者無從分辨「成功」與「整段被跳過」。

兩件事一起修:
  ① 房號在載入邊界(`_template_rooms`)一律轉字串,override 的比對也正規化;
  ② 停診一天都沒動到 → ★明講原因★(模板沒開 vs 已是該狀態,處置不同),
     拋例外中止存檔,不留誤導的 audit;成功則一律回報做了幾個時段。
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.clinic_grid import month_grid  # noqa: E402
from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.storage import RosterStorage  # noqa: E402

YM = "2026-09"
MON1, MON30 = date(2026, 9, 1), date(2026, 9, 30)


def _svc(tmp_path, room):
    """room 可傳 int 或 str —— 兩種形狀都必須能停診。"""
    st = RosterStorage(str(tmp_path))
    st.save_clinic_template({"template": {
        "0": {"上午": [{"room": room, "doctor": "甲"}]}}})
    return RosterService(st)


class TestIntRoomsCloseJustLikeStrRooms:

    @pytest.mark.parametrize("room", [101, "101"])
    def test_close_and_restore_round_trip(self, tmp_path, room):
        """★使用者回報的原始情境★:數字房號的模板,停診/恢復都沒反應。"""
        svc = _svc(tmp_path, room)
        r = svc.set_clinic_closed(YM, "101", MON1, MON30,
                                  ["上午", "下午"], closed=True)
        assert r["changed"] > 0
        closures = svc.clinic_closures(YM)
        assert closures, f"★停診沒有寫進月檔★ room={room!r}"
        assert all(v == {"上午": ["101"]} for v in closures.values())
        r2 = svc.set_clinic_closed(YM, "101", MON1, MON30,
                                   ["上午", "下午"], closed=False)
        assert r2["changed"] == r["changed"]
        assert svc.clinic_closures(YM) == {}, "★恢復開診沒有生效★"

    def test_the_grid_excludes_the_closed_int_room(self, tmp_path):
        svc = _svc(tmp_path, 101)
        svc.set_clinic_closed(YM, "101", MON1, MON30, ["上午"], closed=True)
        grid = svc.build_day_input(YM).grid
        assert "101" not in (grid[date(2026, 9, 7)]["上午"] or []), \
            "★停診寫進去了,格網卻照樣開診★"

    def test_hand_edited_int_closed_rooms_still_apply(self, tmp_path):
        """人工編輯過的月檔把 closed_rooms 存成數字 → 格網一樣要排除。"""
        tpl = {"0": {"上午": [{"room": "101", "doctor": "甲"}]}}
        grid = month_grid(YM, tpl, set(),
                          overrides={"2026-09-07": {"上午":
                                                    {"closed_rooms": [101]}}})
        assert grid[date(2026, 9, 7)]["上午"] == [], \
            "★數字形狀的停診紀錄被靜默忽略★"

    def test_added_rooms_are_normalized_too(self, tmp_path):
        tpl = {"0": {"上午": [{"room": "101", "doctor": "甲"}]}}
        grid = month_grid(YM, tpl, set(),
                          overrides={"2026-09-07": {"上午":
                                                    {"added_rooms": [102]}}})
        assert grid[date(2026, 9, 7)]["上午"] == ["101", "102"]


class TestANoOpClosureSpeaksUp:
    """★「成功」與「整段被跳過」必須分得出來★"""

    def test_a_room_the_template_never_opens_is_refused_with_the_cause(
            self, tmp_path):
        svc = _svc(tmp_path, "101")
        before = svc.storage.load_month(YM).get("audit") or []
        with pytest.raises(ValueError, match="沒有開診"):
            svc.set_clinic_closed(YM, "999", MON1, MON30, ["上午"],
                                  closed=True)
        after = svc.storage.load_month(YM).get("audit") or []
        assert after == before, "★整段被跳過,卻留下了誤導的 audit★"

    def test_closing_an_already_closed_range_says_so(self, tmp_path):
        svc = _svc(tmp_path, "101")
        svc.set_clinic_closed(YM, "101", MON1, MON30, ["上午"], closed=True)
        with pytest.raises(ValueError, match="已全部是停診"):
            svc.set_clinic_closed(YM, "101", MON1, MON30, ["上午"],
                                  closed=True)

    def test_restoring_a_never_closed_range_says_so(self, tmp_path):
        svc = _svc(tmp_path, "101")
        with pytest.raises(ValueError, match="沒有.*停診紀錄"):
            svc.set_clinic_closed(YM, "101", MON1, MON30, ["上午"],
                                  closed=False)

    def test_a_successful_close_reports_how_many(self, tmp_path):
        svc = _svc(tmp_path, "101")
        r = svc.set_clinic_closed(YM, "101", MON1, MON30, ["上午"],
                                  closed=True)
        assert r["changed"] == 4                    # 2026-09 有四個週一

    def test_the_validator_sees_hand_edited_int_closures(self, tmp_path):
        """`quick_validate_day` 的「已停診卻仍排了人」檢查:人工編輯的月檔把
        closed_rooms 存成數字時,warning 一樣要出得來(day_slots 的鍵經 JSON
        寫回一定是字串,不正規化就永遠比不中 → 這條檢查形同不存在)。"""
        svc = _svc(tmp_path, "101")
        st = svc.storage
        month = st.load_month(YM)
        month["day_slots"] = {"2026-09-07": {"上午": {"101": ["P1"]}}}
        month["grid_overrides"] = {"2026-09-07": {"上午":
                                                  {"closed_rooms": [101]}}}
        month["pgy_month_roster"] = ["P1"]
        st.save_month(YM, month)
        warns = svc.quick_validate_day(YM)
        assert any("已停診" in w for w in warns), \
            f"★數字形狀的停診紀錄讓這條檢查靜默失效★ {warns}"


# ══ 第 2 輪外審 ═════════════════════════════════════════════════════════
class TestPersistedIntClosuresAreFirstClass:
    """★人工編輯留下的 [101] 也是既有狀態★(外審 RS-11 第 1 輪)

    只正規化 `month_grid` 的讀取是不夠的:`set_clinic_closed` 對既有清單的
    membership 仍是型別敏感的 —— 恢復比不中而說「沒有停診紀錄」;先停再恢復
    更慘:清單變 [101, "101"],恢復只移掉字串那個,★回報成功、診間卻仍然
    停診★。而 `clinic_closures()` 回傳原始型別,會讓停診對話框的
    `"、".join(rooms)` 直接 TypeError,連窗都開不起來。
    """

    def _seed_int_closure(self, tmp_path):
        svc = _svc(tmp_path, "101")
        st = svc.storage
        month = st.load_month(YM)
        month["grid_overrides"] = {"2026-09-07": {"上午":
                                                  {"closed_rooms": [101]}}}
        st.save_month(YM, month)
        return svc

    def test_restore_works_on_a_persisted_int_closure(self, tmp_path):
        svc = self._seed_int_closure(tmp_path)
        r = svc.set_clinic_closed(YM, "101", date(2026, 9, 7),
                                  date(2026, 9, 7), ["上午"], closed=False)
        assert r["changed"] == 1
        assert svc.clinic_closures(YM) == {}, \
            "★恢復回報成功,診間卻仍然停診★"
        grid = svc.build_day_input(YM).grid
        assert "101" in grid[date(2026, 9, 7)]["上午"]

    def test_closing_over_an_int_closure_does_not_duplicate(self, tmp_path):
        svc = self._seed_int_closure(tmp_path)
        with pytest.raises(ValueError, match="已全部是停診"):
            svc.set_clinic_closed(YM, "101", date(2026, 9, 7),
                                  date(2026, 9, 7), ["上午"], closed=True)
        month = svc.storage.load_month(YM)
        lst = month["grid_overrides"]["2026-09-07"]["上午"]["closed_rooms"]
        assert lst == [101], f"★不該長出 [101, '101'] 這種雙形狀★ {lst}"

    def test_clinic_closures_returns_strings(self, tmp_path):
        svc = self._seed_int_closure(tmp_path)
        rooms = svc.clinic_closures(YM)["2026-09-07"]["上午"]
        assert rooms == ["101"]
        assert all(isinstance(r, str) for r in rooms), \
            "★數字混進來會讓停診對話框的 join 直接 TypeError★"
        "、".join(rooms)                        # 對話框摘要做的事,不得拋
