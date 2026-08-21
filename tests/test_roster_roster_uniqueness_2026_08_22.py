# -*- coding: utf-8 -*-
"""[批次RS-17 / 外審 2026-08-21 P1-01] 名單的成員必須唯一。

日填充器把 list 的每一個 occurrence 當成一個人:每一步都是「選一個 →
remove 一個 occurrence」,重複的代號因此還留在池子裡,下一步可以再選到他。
結果是★物理上不可能執行★的班表(同一人同一時段照光又在治療室、或切片
又在跟診),而請假/名單/容量三道檢查全部合法 —— 0 warning 一路通到定案
與匯出。而且★正常 UI 就打得出來★(「當月 PGY 人員」輸入「P1、P1」)。

三層一起做(任一層單獨都不夠):
  1 寫入邊界明確拒絕(人話訊息;不是只有 assert)
  2 solver 邊界防禦(舊檔/外部工具/人工合併留下的重複不得原樣進 solver)
  3 驗證器點名(手改 JSON、手動編輯時段造出的同時段多工作)
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.model import (                        # noqa: E402
    ClerkBatch, dedupe_codes, duplicated_codes,
)
from cmuh_common.roster.service import RosterService          # noqa: E402
from cmuh_common.roster.solve_day import (                    # noqa: E402
    BIOPSY, PHOTO, REST, TREATMENT, FairCounters, month_solve_day,
    solve_session,
)
from cmuh_common.roster.storage import RosterStorage          # noqa: E402

YM = "2026-08"


@pytest.fixture()
def svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({"r_members": [], "vs_members": [],
                    "pgy_members": [{"id": "P1"}, {"id": "P2"}]})
    st.save_month(YM, {})
    st.save_clerk_batches([{"id": "b1", "start_monday": "2026-08-03",
                            "members": ["C1", "C2"]}])
    return RosterService(st)


# ══ 純函式 ═══════════════════════════════════════════════════════════════
def test_the_predicate_names_every_repeat_once():
    assert duplicated_codes(["A", "B", "A", "C", "A", "B"]) == ["A", "B"]
    assert duplicated_codes(["A", "B"]) == []
    assert duplicated_codes([]) == []
    assert dedupe_codes(["B", "A", "B"]) == ["B", "A"]      # 保序


# ══ 層 1:寫入邊界拒絕(人話) ═══════════════════════════════════════════
class TestTheWriteBoundaryRefuses:
    def test_the_month_roster_refuses_duplicates(self, svc):
        with pytest.raises(ValueError, match="重複的代號"):
            svc.set_pgy_month_roster(YM, ["P1", "P1"], baseline=[])
        assert svc.storage.load_month(YM).get("pgy_month_roster") is None, \
            "★被拒就不可以留下任何一半★"

    def test_a_duplicate_hidden_among_others_is_still_refused(self, svc):
        with pytest.raises(ValueError, match="P1"):
            svc.set_pgy_month_roster(YM, ["P1", "P2", "P1"], baseline=[])

    def test_the_message_says_which_code_and_why(self, svc):
        with pytest.raises(ValueError) as e:
            svc.set_pgy_month_roster(YM, ["P9", "P9"], baseline=[])
        msg = str(e.value)
        assert "P9" in msg and "重複" in msg
        assert "照光" in msg and "治療室" in msg, "要說清楚後果,不是只說不行"

    def test_the_pgy_defaults_refuse_duplicates(self, svc):
        with pytest.raises(ValueError, match="重複的代號"):
            svc.set_pgy_default_members(["P1", "P1"], baseline=["P1"])
        assert [m["id"] for m in svc.storage.load_config()["pgy_members"]] \
            == ["P1", "P2"]

    def test_a_clerk_batch_refuses_duplicate_members(self, svc):
        with pytest.raises(ValueError, match="重複的代號"):
            svc.add_clerk_batch({"id": "b2", "start_monday": "2026-08-17",
                                 "members": ["C3", "C3"]})
        assert [b["id"] for b in svc.storage.load_clerk_batches()] == ["b1"]

    def test_editing_a_batch_into_duplicates_is_refused(self, svc):
        before = {"id": "b1", "start_monday": "2026-08-03",
                  "members": ["C1", "C2"]}
        with pytest.raises(ValueError, match="重複的代號"):
            svc.update_clerk_batch_fields(
                "b1", before, dict(before, members=["C1", "C1"]))
        assert svc.storage.load_clerk_batches()[0]["members"] == ["C1", "C2"]

    def test_a_clean_list_still_saves(self, svc):
        svc.set_pgy_month_roster(YM, ["P1", "P2"], baseline=[])
        assert svc.storage.load_month(YM)["pgy_month_roster"] == ["P1", "P2"]


# ══ 層 2:solver 邊界防禦(舊檔照樣救得回來) ══════════════════════════
class TestTheSolverBoundaryNormalises:
    def test_a_legacy_batch_with_duplicates_is_deduped(self):
        b = ClerkBatch.from_dict({"id": "b9", "start_monday": "2026-08-03",
                                  "members": ["C1", "C1", "C2"]})
        assert b is not None and b.members == ["C1", "C2"]

    def test_build_day_input_dedupes_a_hand_edited_month(self, svc):
        svc.storage.save_month(YM, {"pgy_month_roster": ["P1", "P1", "P2"]})
        inp = svc.build_day_input(YM)
        assert inp.pgy_roster == ["P1", "P2"]

    def test_the_session_solver_never_double_books_a_duplicate(self):
        """★最後一道★:任何呼叫端(含測試)都不該有辦法用重複名單解出
        「同一人同時段兩件事」的班表。"""
        fc = FairCounters()
        slots, _log = solve_session(
            date(2026, 8, 4), "上午", ["101"],
            pgy_avail=["P1", "P1"], clerk_avail=["C1", "C1"],
            biopsy_open=True, fc=fc, capacity=2)
        where: dict = {}
        for slot, people in slots.items():
            for p in (people or []):
                where.setdefault(p, []).append(slot)
        assert not {p: w for p, w in where.items() if len(w) > 1}, slots
        assert slots[PHOTO] == ["P1"] and TREATMENT not in slots
        assert slots[BIOPSY] == ["C1"]

    def test_the_duplicate_does_not_inflate_the_counters(self):
        fc = FairCounters()
        solve_session(date(2026, 8, 4), "上午", ["101"],
                      pgy_avail=["P1", "P1"], clerk_avail=[],
                      biopsy_open=False, fc=fc, capacity=2)
        assert fc.photo_total == {"P1": 1}
        assert not fc.tx_total, "去重後沒有第二個人可排治療室"


# ══ 跨池:PGY 與 Clerk 的代號不可交集(外審 RS-17 R1-1)═══════════════
class TestTheTwoRostersAreDisjoint:
    def test_a_pgy_code_already_used_by_a_clerk_is_refused(self, svc):
        with pytest.raises(ValueError, match="已經出現在"):
            svc.set_pgy_month_roster(YM, ["P1", "C1"], baseline=[])

    def test_a_clerk_batch_reusing_a_pgy_code_is_refused(self, svc):
        with pytest.raises(ValueError, match="已經出現在"):
            svc.add_clerk_batch({"id": "b2", "start_monday": "2026-08-17",
                                 "members": ["P1"]})

    def test_editing_a_batch_into_a_pgy_code_is_refused(self, svc):
        before = {"id": "b1", "start_monday": "2026-08-03",
                  "members": ["C1", "C2"]}
        with pytest.raises(ValueError, match="已經出現在"):
            svc.update_clerk_batch_fields(
                "b1", before, dict(before, members=["C1", "P2"]))

    def test_a_code_reused_in_another_month_is_allowed(self, svc):
        """★同一個代號在不同時間屬於不同人是正常的★(外審 RS-17 R2):
        七月的 PGY 代號 A,八月才變成某一梯的 Clerk A —— 兩者從不同時在場。
        擋掉它等於禁止合法的重複使用(repo 的 `prior_pgy` 契約就是為此存在)。
        """
        svc.storage.save_month("2026-07", {"pgy_month_roster": ["A"]})
        svc.storage.save_month(YM, {"pgy_month_roster": ["P1", "P2"]})
        svc.add_clerk_batch({"id": "b8", "start_monday": "2026-08-17",
                             "members": ["A"]})          # 八月梯次,不該被擋
        assert any(b["id"] == "b8"
                   for b in svc.storage.load_clerk_batches())

    def test_a_code_active_in_the_same_month_is_still_refused(self, svc):
        """時間真的重疊時仍要擋:同一梯涵蓋的月份裡,PGY 名單有同一個代號。"""
        svc.storage.save_month(YM, {"pgy_month_roster": ["P1", "A"]})
        with pytest.raises(ValueError, match="已經出現在"):
            svc.add_clerk_batch({"id": "b7", "start_monday": "2026-08-17",
                                 "members": ["A"]})

    def test_a_batch_spanning_two_months_checks_both(self, svc):
        """跨月的梯次要與【兩個月】的名單都比對(只看起始月會漏)。"""
        svc.storage.save_month(YM, {"pgy_month_roster": ["P1"]})
        svc.storage.save_month("2026-09", {"pgy_month_roster": ["Z"]})
        with pytest.raises(ValueError, match="Z"):
            svc.add_clerk_batch({"id": "b6", "start_monday": "2026-08-31",
                                 "members": ["Z"]})

    def test_moving_a_batch_into_a_conflicting_month_is_refused(self, svc):
        """★只改起始日也會造出重疊★(外審 RS-17 R3):七月的梯次成員 A 本來
        沒衝突,把它整梯搬到八月(八月 PGY 名單有 A)—— 成員一個字都沒動,
        守衛若掛在 members 底下就整條繞過去。"""
        svc.storage.save_month("2026-07", {"pgy_month_roster": ["P9"]})
        svc.storage.save_month(YM, {"pgy_month_roster": ["A"]})
        svc.storage.save_clerk_batches(
            [{"id": "bx", "start_monday": "2026-07-06", "members": ["A"]}])
        before = {"id": "bx", "start_monday": "2026-07-06", "members": ["A"]}
        with pytest.raises(ValueError, match="已經出現在"):
            svc.update_clerk_batch_fields(
                "bx", before, dict(before, start_monday="2026-08-17"))
        assert svc.storage.load_clerk_batches()[0]["start_monday"]             == "2026-07-06", "★被拒卻已經搬過去了★"

    def test_moving_a_batch_to_a_clean_month_still_works(self, svc):
        svc.storage.save_month("2026-07", {"pgy_month_roster": ["P9"]})
        svc.storage.save_month(YM, {"pgy_month_roster": ["P1", "P2"]})
        svc.storage.save_clerk_batches(
            [{"id": "bx", "start_monday": "2026-07-06", "members": ["A"]}])
        before = {"id": "bx", "start_monday": "2026-07-06", "members": ["A"]}
        svc.update_clerk_batch_fields(
            "bx", before, dict(before, start_monday="2026-08-17"))
        assert svc.storage.load_clerk_batches()[0]["start_monday"]             == "2026-08-17"

    def test_the_solver_never_books_the_same_code_twice(self):
        """★逐池去重不夠★:同一個代號在兩個池子裡各自都「唯一」——
        照光排 PGY A、切片排 Clerk A,存檔裡就是同一個代號出現在兩格。"""
        fc = FairCounters()
        slots, log = solve_session(
            date(2026, 8, 4), "上午", ["101"],
            pgy_avail=["A"], clerk_avail=["A"],
            biopsy_open=True, fc=fc, capacity=2)
        where: dict = {}
        for slot, people in slots.items():
            for p in (people or []):
                where.setdefault(p, []).append(slot)
        assert not {p: w for p, w in where.items() if len(w) > 1}, slots
        assert slots[PHOTO] == ["A"], slots
        assert BIOPSY not in slots, "A 只能當 PGY 排"

    def test_the_cross_pool_problem_reaches_the_user(self):
        """★只寫進 log 等於沒說★:月層的警告是從 `⚠` 開頭的 log 行收集的。"""
        fc = FairCounters()
        _slots, log = solve_session(
            date(2026, 8, 4), "上午", ["101"],
            pgy_avail=["A"], clerk_avail=["A"],
            biopsy_open=True, fc=fc, capacity=2)
        assert any(ln.startswith("⚠") and "A" in ln for ln in log), log

    def test_a_locked_session_collision_is_warned_before_applying(self, svc):
        """鎖定時段原樣保留 → 手動造出的雙排要在【預覽】就說,不能等落地。"""
        svc.storage.save_month(YM, {
            "pgy_month_roster": ["P1", "P2"],
            "day_locks": {"2026-08-04": {"上午": True}},
            "day_slots": {"2026-08-04": {"上午": {PHOTO: ["P1"],
                                                  TREATMENT: ["P1"]}}}})
        _slots, _log, warns = month_solve_day(svc.build_day_input(YM))
        assert any("同時被排在" in w and "P1" in w for w in warns), warns


# ══ 層 3:驗證器點名(手改 JSON / 手動編輯) ══════════════════════════
class TestTheValidatorNamesTheCollision:
    def _month(self, svc, slots):
        svc.storage.save_month(YM, {"day_slots": {"2026-08-04": {"上午": slots}},
                                    "pgy_month_roster": ["P1", "P2"]})

    def test_photo_and_treatment_by_the_same_person_is_reported(self, svc):
        self._month(svc, {PHOTO: ["P1"], TREATMENT: ["P1"]})
        msgs = svc.quick_validate_day(YM)
        assert any("同時被排在" in m and "P1" in m for m in msgs), msgs

    def test_a_clerk_in_biopsy_and_a_room_is_reported(self, svc):
        svc.storage.save_clerk_batches([{"id": "b1",
                                         "start_monday": "2026-08-03",
                                         "members": ["C1"]}])
        self._month(svc, {BIOPSY: ["C1"], "101": ["C1"]})
        assert any("同時被排在" in m and "C1" in m
                   for m in svc.quick_validate_day(YM))

    def test_working_and_resting_at_once_is_reported(self, svc):
        self._month(svc, {PHOTO: ["P1"], REST: ["P1"]})
        assert any("同時被排在" in m for m in svc.quick_validate_day(YM))

    def test_a_clean_day_says_nothing(self, svc):
        self._month(svc, {PHOTO: ["P1"], TREATMENT: ["P2"]})
        assert not [m for m in svc.quick_validate_day(YM)
                    if "同時被排在" in m]
