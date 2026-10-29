# -*- coding: utf-8 -*-
"""[2026-10-29 使用者定案] UVB 與 Excimer 並存時跳視窗讓醫師選。

實機:處置同一欄有 '左眼皮內側*1 excimer 540 mj/cm2 on (…)' 與
'start UVB 局部臉(…): 630mj/cm2 …',F2 自己判成 UVB 改了 630→660,醫師要的是 excimer。
同一欄與分屬不同欄位兩種並存都改成:是=自費 Excimer、否=健保 UVB、取消=停止。
"""
import ctypes
from datetime import date

import pytest

from cmuh_common import uvb_dose as u

TODAY = date(2026, 10, 29)
CRLF = "\r\n"
EXC = "左眼皮內側*1 excimer 540 mj/cm2 on (2026/10/27) add 40 each, MAX 800"
HELD = "脖子*1 excimer 1240 mj/cm2, add 10 each, MAX 1600 -> hold due to improved"
UVB = "start UVB 局部臉( 左眼皮內側/右太陽穴): 630mj/cm2, add 30 mj/cm2, MAX: 1000  mj/cm2"
MIXED = CRLF.join([EXC, "OMP on (2026/03/30) ", HELD,
                   "pic on (2024/10/24) (2024/12/24) (2025/4/14) ...", UVB])
UVB_1026 = "UVB 500 mj/cm2 (5) on (2026/10/26) add 30 each time, MAX 1000"
UVB_1027 = "UVB 500 mj/cm2 (5) on (2026/10/27) add 30 each time, MAX 1000"


class TestNeedsRouteChoice:
    @pytest.mark.parametrize("text, want", [
        (MIXED, True),
        (EXC + CRLF + UVB, True),
        # 同日搭配(同次兩種都照)→ 沿用 UVB 流程,不問
        (UVB_1027 + CRLF + "excimer light (25) 1000mJ for nape on (2026/10/27)", False),
        # excimer 一個多月沒照 → 不算進行中
        (UVB_1027 + CRLF + "excimer 700 mj/cm2 on (2026/9/10) add 30 each, MAX 900", False),
        # 病史提到 excimer
        (UVB_1027 + CRLF + "previous excimer 700 mj/cm2 add 30 each, MAX 900", False),
        # [外審 PR #14 第 4 輪] 病史標題中間夾泛稱 phototherapy
        (UVB_1027 + CRLF + "Previous phototherapy: excimer 700 mj/cm2 add 30 each, MAX 900", False),
        (HELD + CRLF + UVB, False),            # 只有暫停中的 excimer
        (EXC, False),                          # 沒有 UVB
        (UVB, False),
        # [外審 PR #14 第 1 輪] 同一行先 UVB 後 excimer:只看 excimer 那一段自己的日期
        (UVB_1026 + "; excimer 540 mj/cm2 on (2026/10/27) add 40 each, MAX 800", True),
        (UVB_1027 + "; excimer 540 mj/cm2 on (2026/10/27) add 40 each, MAX 800", False),
        # [外審 PR #14 第 2 輪] 複合名稱 'excimer phototherapy' 不可被 phototherapy 切斷
        (UVB_1026 + CRLF + "excimer phototherapy 540 mj/cm2 on (2026/10/27) add 40 each, MAX 800",
         True),
        # [外審 PR #14 第 3 輪] 但後面另起的 Phototherapy 醫令仍是另一段:它的日期不可算給 excimer
        (UVB_1026 + CRLF + "excimer 540 mj/cm2 add 40 each, MAX 800; "
         "Phototherapy 600 mj/cm2 (7) on (2026/10/26) add 30 each time, MAX 1000", True),
    ])
    def test_cases(self, text, want):
        assert u.needs_route_choice(text, TODAY) is want


class TestForcedRoutes:
    def test_excimer_route_changes_only_excimer_lines(self):
        r = u.update_uvb_in_text(MIXED, TODAY, route="excimer")
        assert r.action == u.UvbAction.UPDATED and r.new_dose == 580
        new = r.new_text.split(CRLF)
        old = MIXED.split(CRLF)
        assert new[0] == EXC.replace("540", "580").replace("2026/10/27", "2026/10/29")
        assert new[1:] == old[1:], "UVB 行、暫停行、其他紀錄都不可以動"

    def test_default_route_is_unchanged(self):
        r = u.update_uvb_in_text(MIXED, TODAY)
        assert r.action == u.UvbAction.UPDATED and r.new_dose == 660   # 既有 UVB 路徑

    def test_no_updatable_excimer_never_falls_back_to_uvb(self):
        r = u.update_uvb_in_text(HELD + CRLF + UVB, TODAY, route="excimer")
        assert r.action == u.UvbAction.NO_UVB_LINE and r.new_text is None

    def test_a_uvb_line_right_below_excimer_is_not_a_continuation(self):
        text = EXC + CRLF + UVB
        r = u.update_uvb_in_text(text, TODAY, route="excimer")
        assert r.new_text.split(CRLF)[1] == UVB

    def test_unknown_route_is_rejected(self):
        with pytest.raises(ValueError):
            u.update_uvb_in_text(MIXED, TODAY, route="laser")

    def test_choosing_uvb_does_not_also_advance_a_separately_dated_excimer(self):
        """[外審 PR #14 第 1 輪] UVB 路徑的 Step D 會順手改各自有日期的 excimer。"""
        exc = "excimer 540 mj/cm2 on (2026/10/27) add 40 each, MAX 800"
        text = UVB_1026 + CRLF + exc
        assert u.needs_route_choice(text, TODAY)
        r = u.update_uvb_in_text(text, TODAY, route="uvb")
        new = r.new_text.split(CRLF)
        assert "530 mj/cm2 (6) on (2026/10/29)" in new[0]
        assert new[1] == exc, "選 UVB 時 excimer 不可動"
        assert "580 mj/cm2" in u.update_uvb_in_text(text, TODAY).new_text   # 預設路徑維持既有行為

    def test_uvb_route_filters_each_excimer_segment_by_its_own_date(self):
        """[外審 PR #14 第 2 輪] 同一行另一段 excimer 剛好與 UVB 同日,不可替別段背書。"""
        two = ("excimer 540 mj/cm2 on (2026/10/27) add 40 each, MAX 800, "
               "excimer 700 mj/cm2 on (2026/10/26) add 30 each, MAX 900")
        text = UVB_1026 + CRLF + two
        assert u.needs_route_choice(text, TODAY)
        r = u.update_uvb_in_text(text, TODAY, route="uvb")
        assert r.new_text.split(CRLF)[1] == two

    def test_uvb_route_reaches_later_lines_after_an_undated_driver(self):
        """[外審 PR #14 第 2 輪] 無日期驅動行之後的多行處理也要帶著『只做 UVB』。"""
        first = "UVB 局部臉 630mj/cm2, add 30 mj/cm2, MAX 1000"
        second = (UVB_1026 + ", excimer 540 mj/cm2 on (2026/10/27) add 40 each, MAX 800")
        text = first + CRLF + second
        r = u.update_uvb_in_text(text, TODAY, route="uvb")
        assert r.action == u.UvbAction.UPDATED
        new_second = r.new_text.split(CRLF)[1]
        assert "excimer 540 mj/cm2 on (2026/10/27)" in new_second, "excimer 段不可動"
        assert "excimer 580" in u.update_uvb_in_text(text, TODAY).new_text   # 預設路徑維持既有行為

    def test_same_date_excimer_is_still_updated_with_uvb_route(self):
        """同日搭配(同次兩種都照)選 UVB 時仍照既有 Step D 一起更新;另有日期的不動。"""
        same = "excimer 540 mj/cm2 on (2026/10/27) add 40 each, MAX 800"
        other = "excimer 700 mj/cm2 on (2026/10/26) add 30 each, MAX 900"
        text = CRLF.join([UVB_1027, same, other])
        r = u.update_uvb_in_text(text, TODAY, route="uvb")
        new = r.new_text.split(CRLF)
        assert "530 mj/cm2 (6) on (2026/10/29)" in new[0]
        assert "580 mj/cm2 on (2026/10/29)" in new[1]
        assert new[2] == other


# ─── 主程式整條流程 ───────────────────────────────────────────────────────────
@pytest.fixture
def his(monkeypatch):
    import main

    class FixedDate(date):
        @classmethod
        def today(cls):
            return TODAY

    state = {"memos": {}, "writes": [], "asked": [], "answer": 2, "warnings": []}
    monkeypatch.setattr(main, "date", FixedDate)
    monkeypatch.setattr(u, "date", FixedDate)
    monkeypatch.setattr(main, "_collect_phototherapy_memos",
                        lambda _h: (list(state["memos"].items()), True))
    monkeypatch.setattr(main, "check_stop", lambda: None)
    monkeypatch.setattr(main, "_record_his_action", lambda *a, **k: None)
    monkeypatch.setattr(main, "_find_hospital_main_window", lambda: 10)
    monkeypatch.setattr(main, "_show_uvb_warning",
                        lambda _h, title, *a, **k: state["warnings"].append(title))

    def message_box(_hwnd, text, title, flags):
        state["asked"].append((title, text, flags))
        return state["answer"]
    monkeypatch.setattr(ctypes.windll.user32, "MessageBoxW", message_box)

    class MemoPort:
        def find_main_window(self):
            return 10

        def locate_phototherapy(self, hwnd):
            return main._resolve_phototherapy_disposition(hwnd)

        def read_memo(self, hwnd):
            return state["memos"][hwnd]

        def write_memo(self, hwnd, text):
            state["writes"].append(hwnd)
            state["memos"][hwnd] = text
            return True

    state["port"] = MemoPort()
    main._reset_photo_route_choice()
    yield main, state
    main._reset_photo_route_choice()


def test_same_field_resolves_as_ambiguous_until_the_doctor_chooses(his):
    main, state = his
    state["memos"] = {20: MIXED}
    assert main._resolve_phototherapy_disposition(10) == (20, "ambiguous")
    state["answer"] = 6
    assert main._ask_photo_route("F2") == "excimer"
    assert main._resolve_phototherapy_disposition(10) == (20, "pure_excimer")
    _title, text, flags = state["asked"][0]
    assert "同一欄" in text and "是」＝自費 Excimer" in text and "否」＝健保 UVB" in text
    assert flags & 0x0F == 0x3 and flags & 0xF00 == 0x200, "是/否/取消,預設取消"


def test_f2_choosing_excimer_updates_only_the_excimer_line(his):
    main, state = his
    state["memos"] = {20: MIXED}
    state["answer"] = 6
    res = main._update_uvb_dose_core("F2", strict=True, memo_port=state["port"])
    assert res == main._F23_PURE_EXCIMER, state["warnings"]
    assert len(state["asked"]) == 1, "只問一次(之後的重新分流照選擇)"
    new = state["memos"][20].split(CRLF)
    assert "580 mj/cm2 on (2026/10/29)" in new[0]
    assert new[2] == HELD and new[4] == UVB


def test_f2_choosing_uvb_keeps_the_uvb_route(his):
    main, state = his
    state["memos"] = {20: MIXED}
    state["answer"] = 7
    assert main._update_uvb_dose_core("F2", strict=True, memo_port=state["port"]) is True
    new = state["memos"][20].split(CRLF)
    assert "660mj/cm2" in new[4] and new[0] == EXC


def test_f2_choosing_uvb_leaves_a_separately_dated_excimer_alone(his):
    """[外審 PR #14 第 1 輪] 選 UVB 後主程式也要把『只做 UVB』帶進劑量計算。"""
    main, state = his
    exc = "excimer 540 mj/cm2 on (2026/10/27) add 40 each, MAX 800"
    state["memos"] = {20: UVB_1026 + CRLF + exc}
    state["answer"] = 7
    assert main._update_uvb_dose_core("F2", strict=True, memo_port=state["port"]) is True
    new = state["memos"][20].split(CRLF)
    assert "530 mj/cm2 (6)" in new[0] and new[1] == exc


def test_f2_cancel_stops_without_writing(his):
    main, state = his
    state["memos"] = {20: MIXED}
    state["answer"] = 2
    assert main._update_uvb_dose_core("F2", strict=True, memo_port=state["port"]) is False
    assert state["writes"] == [] and state["memos"][20] == MIXED


def test_separate_fields_also_ask_and_follow_the_choice(his):
    main, state = his
    uvb_only = "UVB 500 mj/cm2 (5) on (2026/10/27) add 30 each time, MAX 1000"
    state["memos"] = {20: uvb_only, 30: EXC}
    state["answer"] = 6
    res = main._update_uvb_dose_core("F3", strict=True, memo_port=state["port"])
    assert res == main._F23_PURE_EXCIMER, state["warnings"]
    assert "不同欄位" in state["asked"][0][1]
    assert state["writes"] == [30] and state["memos"][20] == uvb_only


def test_f1_asks_before_keying_and_does_not_ask_again(his, monkeypatch):
    main, state = his
    state["memos"] = {20: MIXED}
    keyed = []
    monkeypatch.setattr(main, "_script_code_input_adaptive",
                        lambda code, **k: keyed.append(code) or True)
    state["answer"] = 7                                   # UVB
    real_core = main._update_uvb_dose_core
    monkeypatch.setattr(main, "_update_uvb_dose_core",
                        lambda label, **k: real_core(label, memo_port=state["port"], **k))
    assert main.script_F1_adaptive() is True, state["warnings"]
    assert keyed == ["51019"] and len(state["asked"]) == 1
    assert "660mj/cm2" in state["memos"][20]


def test_f1_excimer_choice_goes_to_the_pure_excimer_path(his, monkeypatch):
    main, state = his
    state["memos"] = {20: MIXED}
    calls = []
    monkeypatch.setattr(main, "_f1_pure_excimer", lambda label="F1": calls.append(label) or True)
    monkeypatch.setattr(main, "_script_code_input_adaptive",
                        lambda *a, **k: pytest.fail("選 Excimer 不可 key 51019"))
    state["answer"] = 6
    assert main.script_F1_adaptive() is True
    assert calls == ["F1"] and "1850159" in state["asked"][0][1]


def test_f1_cancel_keys_nothing(his, monkeypatch):
    main, state = his
    state["memos"] = {20: MIXED}
    monkeypatch.setattr(main, "_script_code_input_adaptive",
                        lambda *a, **k: pytest.fail("取消不可 key 任何醫令"))
    state["answer"] = 2
    assert main.script_F1_adaptive() is False


def test_a_new_hotkey_run_forgets_the_previous_choice(his):
    main, state = his
    state["memos"] = {20: MIXED}
    main._resolve_phototherapy_disposition(10)
    state["answer"] = 6
    main._ask_photo_route("F2")
    main._reset_photo_route_choice()
    assert main._resolve_phototherapy_disposition(10) == (20, "ambiguous")


def test_an_answer_from_a_cancelled_dialog_is_not_recorded(his, monkeypatch):
    """[外審 PR #14 第 1 輪] F12 取消後才按下的答案不可蓋掉新流程的選擇。"""
    main, state = his
    state["memos"] = {20: MIXED}
    main._resolve_phototherapy_disposition(10)
    state["answer"] = 6

    def cancelled():
        raise main.SubsystemInterrupted()
    monkeypatch.setattr(main, "check_stop", cancelled)
    with pytest.raises(main.SubsystemInterrupted):
        main._ask_photo_route("F2")
    assert main._photo_route_choice is None


def test_the_choice_survives_the_excimer_write_until_identity_is_checked(his):
    """[外審 PR #14 第 1 輪] 寫入後兩行同日 → 不再「需要選擇」,身份目標核對仍要認得那一欄。"""
    main, state = his
    uvb_today = "UVB 500 mj/cm2 (5) on (2026/10/29) add 30 each time, MAX 1000"
    state["memos"] = {20: EXC + CRLF + uvb_today}
    state["answer"] = 6
    anchor_out = []
    res = main._update_uvb_dose_core("F2", strict=True, memo_port=state["port"],
                                     identity_anchor_out=anchor_out)
    assert res == main._F23_PURE_EXCIMER, state["warnings"]
    assert not u.needs_route_choice(state["memos"][20], TODAY), "寫入後兩行同日"
    assert main._excimer_identity_target_current(anchor_out[0], memo_port=state["port"])
