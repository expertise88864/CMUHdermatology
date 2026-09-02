# -*- coding: utf-8 -*-
"""[2026-09-02 使用者] 拖班的診間,「目前這一節」也要看得到。

★使用者實機★(9/2 週三晚上):101 與 102 都在看診,但 101 下午還沒看完 ——
浮動視窗只看得到 101,★完全沒有 102 診晚上★。

★根因★:`_overrun_effective_tc()` 會把「更早時段今天看過診、還沒判定關診」
的診間整格拉回那一節。它是【每一格都套用】的:
  * 101 下午還在拖 → 101 那格被釘在下午 → 101 晚上永遠沒人查;
  * 102 若下午也有診而沒被判定關診 → 102 那格同樣被拉回下午 →
    下午沒東西 → 卡片被 `should_show_room` 隱藏 → 102 診晚上整個消失。

★使用者定案:兩節都要★。做法是為拖班的診間多輪一次目前這一節,結果只多一張
浮動卡 —— ★不佔五格中的任何一格★(使用者原本提議「從後方如 105 診暫時取代」,
不佔格比取代更好:105 不必被犧牲),★也不寫 tracker★(關診偵測的狀態機只由
主路徑維護,不被這筆額外查詢污染)。
"""
import ast
import inspect
import os
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main as m  # noqa: E402
from cmuh_common import floating_clinic  # noqa: E402


class _Var:
    def __init__(self, v):
        self._v = v

    def get(self):
        return self._v


class _Host:
    """只帶被測方法需要的東西(真的 AutomationApp 要 Tk/Win32)。"""

    def __init__(self, rooms=("101", "102", "103", "104", "105")):
        self.clinic_room_vars = [_Var(r) for r in rooms]
        self._floating_status_by_room = {}
        self._floating_extra_status_by_room = {}
        self._floating_error_streak = {}
        self.fetched = []
        self._fake_results = {}

    def fetch_clinic_light_status(self, room, time_code=None):
        self.fetched.append((str(room), str(time_code)))
        return self._fake_results.get((str(room), str(time_code)), {})


for _name in ("_collect_widget_room_status", "_poll_overrun_current_sessions",
              "_build_floating_status"):
    setattr(_Host, _name, m.AutomationApp.__dict__[_name])


def _rs(room, slot, doctor="王醫師"):
    return floating_clinic.RoomStatus(room=room, slot=slot, doctor=doctor,
                                      light="1", fetched=True)


# ══ 額外那一節真的被查了 ═════════════════════════════════════════════════
class TestTheOverrunRoomsCurrentSessionIsPolled:
    def test_it_polls_the_current_session_of_an_overrunning_room(self,
                                                                 monkeypatch):
        h = _Host()
        monkeypatch.setattr(m.time, "sleep", lambda _s: None)
        h._fake_results[("101", "3")] = {"reg64_time_code": "3",
                                         "doc_name": "王醫師", "light": "5"}
        h._poll_overrun_current_sessions([("101", "3")])
        assert h.fetched == [("101", "3")], h.fetched
        ex = h._floating_extra_status_by_room.get("101")
        assert ex is not None and ex.slot and ex.doctor == "王醫師", ex

    def test_nothing_is_polled_when_no_room_overruns(self, monkeypatch):
        """★不可以每輪都多打★:沒有診間在拖班時,一次額外請求都不該送出。"""
        h = _Host()
        monkeypatch.setattr(m.time, "sleep", lambda _s: None)
        h._poll_overrun_current_sessions([])
        assert h.fetched == []
        assert h._floating_extra_status_by_room == {}

    def test_a_room_that_stopped_overrunning_loses_its_extra_card(
            self, monkeypatch):
        """★殭屍卡★:不再拖班之後,那張額外卡要跟著消失 ——
        留著就是一張永遠不會更新的卡。"""
        h = _Host()
        monkeypatch.setattr(m.time, "sleep", lambda _s: None)
        h._floating_extra_status_by_room = {"101": _rs("101", "晚上")}
        h._poll_overrun_current_sessions([])
        assert h._floating_extra_status_by_room == {}

    def test_a_failed_extra_fetch_does_not_raise(self, monkeypatch):
        """額外查詢是附加價值,壞掉不可以影響主路徑。"""
        h = _Host()
        monkeypatch.setattr(m.time, "sleep", lambda _s: None)

        def _boom(room, time_code=None):
            raise RuntimeError("reg64 掛了")
        h.fetch_clinic_light_status = _boom
        h._poll_overrun_current_sessions([("101", "3")])   # 不可拋
        assert h._floating_extra_status_by_room == {}


# ══ 浮動視窗看得到兩張,而且沒有人被擠掉 ══════════════════════════════════
class TestBothSessionsAppearWithoutDisplacingAnyone:
    def test_the_users_actual_case(self):
        """★使用者實機那一幕★:101 下午(拖班)+ 101 晚上 + 102 晚上,
        而且 103/104/105 一格都沒被吃掉。"""
        h = _Host()
        h._floating_status_by_room = {
            "101": _rs("101", "下午"),
            "102": _rs("102", "晚上", "李醫師"),
        }
        h._floating_extra_status_by_room = {"101": _rs("101", "晚上")}
        got = [(r.room, r.slot) for r in h._collect_widget_room_status()]
        assert ("101", "下午") in got
        assert ("101", "晚上") in got, "★拖班診間目前那一節仍然看不到★"
        assert ("102", "晚上") in got, "★102 診晚上被擠掉了★"

    def test_the_extra_card_sits_next_to_its_own_room(self):
        """額外那張要緊接在它自己那一格後面(不是跑到列表最後)。"""
        h = _Host()
        h._floating_status_by_room = {"101": _rs("101", "下午"),
                                      "102": _rs("102", "晚上")}
        h._floating_extra_status_by_room = {"101": _rs("101", "晚上")}
        got = [(r.room, r.slot) for r in h._collect_widget_room_status()]
        assert got.index(("101", "晚上")) == got.index(("101", "下午")) + 1
        assert got.index(("102", "晚上")) > got.index(("101", "晚上"))

    def test_no_slot_is_taken_over(self):
        """★不佔格★:五格的診間全部都還在(105 不必被犧牲)。"""
        h = _Host()
        for r in ("101", "102", "103", "104", "105"):
            h._floating_status_by_room[r] = _rs(r, "晚上")
        h._floating_extra_status_by_room = {"101": _rs("101", "下午")}
        got = [r.room for r in h._collect_widget_room_status()]
        for r in ("101", "102", "103", "104", "105"):
            assert r in got, r

    def test_the_same_room_and_session_is_not_drawn_twice(self):
        """★同一格診間被設兩次時不要畫出兩張一樣的卡★
        (額外那張若剛好與主卡同一節,也只留一張)。"""
        h = _Host(rooms=("101", "101", "", "", ""))
        h._floating_status_by_room = {"101": _rs("101", "下午")}
        h._floating_extra_status_by_room = {"101": _rs("101", "下午")}
        got = [(r.room, r.slot) for r in h._collect_widget_room_status()]
        assert got == [("101", "下午")], got

    def test_without_any_extra_the_list_is_unchanged(self):
        """★對照組★:沒有拖班時,清單與改動前逐格一致。"""
        h = _Host()
        for r in ("101", "102", "103", "104", "105"):
            h._floating_status_by_room[r] = _rs(r, "晚上")
        got = [r.room for r in h._collect_widget_room_status()]
        assert got == ["101", "102", "103", "104", "105"], got


# ══ 接線:主迴圈要真的把拖班診間記下來並去查 ══════════════════════════════
def test_the_polling_loop_records_and_polls_overrun_rooms():
    """★沒有呼叫端 = 這個功能不存在★"""
    src = textwrap.dedent(
        inspect.getsource(m.AutomationApp._update_clinic_lights_loop_body))
    tree = ast.parse(src)
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "_poll_overrun_current_sessions" in called, (
        "★主迴圈沒有查拖班診間目前那一節★")
    assert "overrun_rooms.append" in src, "★沒有把被拉回的診間記下來★"
    # 記錄的條件必須是「有效時段 != 原本要的時段」(= 真的被拉回了)
    assert 'if str(tc_effective) != str(tc_requested):' in src, src[:0] or (
        "★記錄條件不是『被拉回』→ 會對沒拖班的診間也多打一次★")


def test_the_extra_poll_runs_after_the_main_rows_are_dispatched():
    """★正事優先 —— 而且是【處理並派送】完之後★

    ★[外審第 1 輪 P2-1] 這條測試自己也更正過★:第一版只驗「排在
    `packed_rows.append` 之後」,而我當時正是把它放在【抓完、還沒處理】的位置
    —— 一次慢的額外請求(最壞 10 秒 HTTP 逾時)會讓已經抓回來的五格主資料
    整整晚那麼久才上畫面。判準要問的是「主結果派送出去了沒」,
    不是「抓完了沒」。
    """
    src = inspect.getsource(m.AutomationApp._update_clinic_lights_loop_body)
    i_extra = src.index("_poll_overrun_current_sessions")
    assert src.index("for pack in packed_rows:") < i_extra, (
        "★額外查詢排在主結果的處理迴圈之前 → 會拖慢表格的五格★")
    # 主路徑把 UI 更新排進主緒的那些 root.after 也必須都在它之前
    assert src.index("update_single_clinic_ui_error") < i_extra, (
        "★主路徑的 UI 派送還沒排,就先去做附加查詢★")


def test_the_extra_entry_does_not_write_trackers():
    """★不寫 tracker★:關診偵測的狀態機只由主路徑那一節維護。
    這一筆額外查詢若也去寫,拖班那一節的關診判定會被污染。"""
    src = inspect.getsource(m.AutomationApp._poll_overrun_current_sessions)
    for forbidden in ("clinic_trackers", "_tracker_lock",
                      "update_single_clinic_ui", "clinic_ui_elements"):
        assert forbidden not in src, "★額外查詢碰了 " + forbidden + "★"


class TestTheExtraQueryMustNotHealTheErrorStreak:
    """★[外審第 1 輪 P2-2] 連續錯誤計數是【以診間為單位】的★

    拖班時同一個診間有兩筆查詢(被釘住的那一節 + 此刻這一節)。
    額外那一筆若也把計數歸零,就會變成:
      主路徑那一節每輪錯一次 → 計數加到 1 → 額外那一筆成功 → 歸零
      → ★永遠到不了隱藏門檻★,一張過期/無效的卡一直留在畫面上。
    額外那一筆的連線健康,不代表被釘住那一節的健康。
    """

    def test_a_successful_extra_query_does_not_reset_the_streak(self,
                                                                monkeypatch):
        h = _Host()
        monkeypatch.setattr(m.time, "sleep", lambda _s: None)
        h._floating_error_streak["101"] = 1          # 主路徑上一輪錯過一次
        h._fake_results[("101", "3")] = {"reg64_time_code": "3",
                                         "doc_name": "王醫師", "light": "5"}
        h._poll_overrun_current_sessions([("101", "3")])
        assert h._floating_error_streak.get("101") == 1, (
            "★額外查詢把主路徑累積的連續錯誤計數洗掉了 →"
            " 過期的卡永遠不會被隱藏★")

    def test_the_primary_path_still_resets_it(self):
        """★對照組★:主路徑成功時仍然要歸零(否則暫時性瞬斷之後
        有診的診間會被先前累積的次數誤判成「沒診」而隱藏)。"""
        h = _Host()
        h._floating_error_streak["101"] = 1
        h._floating_status_by_room = {}
        _capture = m.AutomationApp.__dict__["_capture_floating_status"]
        _capture(h, 0, {"reg64_time_code": "2", "doc_name": "王醫師",
                        "light": "3"}, {})
        assert h._floating_error_streak.get("101") == 0
        assert h._floating_status_by_room.get("101") is not None

    def test_the_builder_defaults_to_not_resetting(self):
        """★預設不歸零★:日後有人加第三個呼叫端時,不必記得傳旗標
        也不會意外把計數洗掉(失效方向要安全)。"""
        sig = inspect.signature(m.AutomationApp._build_floating_status)
        assert sig.parameters["reset_error_streak"].default is False


@pytest.mark.parametrize("attr", ["_floating_extra_status_by_room"])
def test_the_extra_dict_is_initialised(attr):
    """UI 還沒跑過任何一輪時也不可以 AttributeError。"""
    src = inspect.getsource(m.AutomationApp.__init__)
    assert "self." + attr + " = {}" in src
