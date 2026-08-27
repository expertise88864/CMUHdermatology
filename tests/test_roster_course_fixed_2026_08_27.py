# -*- coding: utf-8 -*-
"""批次 RS-29:相鄰月份的既定時段要進求解輸入(全審 2026-08-24 的 P1-01)。

★RS-26 讓配額的【分母】看得到整梯,但「那一格是不是已經有人」仍只看本月★
於是下個月已鎖定/已定案的切片被當成還能自由分配的未來機會 —— 本月因此挑錯人,
而正解明明存在。外審給的決定性反例就是下面第一條。

第二個缺口同樣要命:那些既定時段★沒有進求解輸入★,所以也不在指紋裡 ——
「8 月預覽 → 9 月加鎖 → 套用」兩次指紋相同,舊結果被照單全收。
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cmuh_common.roster.model import ClerkBatch  # noqa: E402
from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.solve_day import (  # noqa: E402
    BIOPSY, DaySolveInput, day_input_fingerprint, month_solve_day,
)
from cmuh_common.roster.storage import RosterStorage  # noqa: E402

AUG, SEP = "2026-08", "2026-09"
MON = date(2026, 8, 31)          # 週一;梯次 8/31～9/13(跨月)
SEP1 = date(2026, 9, 1)


def _svc(tmp_path) -> RosterService:
    """整梯只開兩個切片時段:8/31 早、9/01 早 → 兩人各該切 1 次。"""
    st = RosterStorage(str(tmp_path))
    st.save_config({"pgy_members": [{"id": "P1"}], "r_members": [],
                    "vs_members": [], "room_capacity": 2})
    st.save_clinic_template({"template": {
        str(w): {"上午": [{"room": "101"}], "下午": []} for w in range(5)}})
    st.save_clerk_batches([{"id": "b1", "start_monday": MON.isoformat(),
                            "members": ["C1", "C2"]}])
    st.save_biopsy_grid({"b1": {MON.isoformat(): {"上午": True},
                                SEP1.isoformat(): {"上午": True}}})
    st.save_month(AUG, {})
    st.save_month(SEP, {})
    return RosterService(st)


def _sep_fixed(svc, *, locked=True, finalized=False, slots=None):
    """把 9/01 早的切片設成【已經定下來】(鎖定或已定案)。"""
    m = {"day_slots": {SEP1.isoformat(): {
        "上午": (slots if slots is not None else {BIOPSY: ["C1"]})}}}
    if locked:
        m["day_locks"] = {SEP1.isoformat(): {"上午": True}}
    if finalized:
        m["finalized"] = True
    svc.storage.save_month(SEP, m)


def _biopsy_on(day_slots, d) -> list:
    return (((day_slots.get(d.isoformat()) or {}).get("上午")
             or {}).get(BIOPSY) or [])


# ══ ① 外審的決定性反例 ═══════════════════════════════════════════════════
class TestNextMonthFixedBiopsyIsNotAFreeFutureSlot:
    def test_the_only_remaining_slot_goes_to_whoever_still_needs_it(
            self, tmp_path):
        """★整梯兩格兩人 → 各 1 次;9/01 早已鎖給 C1 → 8/31 只能是 C2。★

        求解器看不到那個鎖定時,它以為 9/01 還能給 C1 或 C2,8/31 兩人各方面
        都相同 → 掉到抖動決勝(crc32 讓 C1 勝出)→ C1 切兩次、C2 掛零,
        而 1/1 的可行解明明存在。

        ★反例只靠「9/01 已經有人」分勝負★:開放時段、人數、請假全都沒動,
        只是那一格被鎖定了。
        """
        svc = _svc(tmp_path)
        _sep_fixed(svc)
        day_slots, _log, _w = month_solve_day(svc.build_day_input(AUG))
        assert _biopsy_on(day_slots, MON) == ["C2"], (
            f"8/31 挑錯人(9/01 已經是 C1 了): {_biopsy_on(day_slots, MON)}")

    def test_a_finalized_next_month_counts_the_same(self, tmp_path):
        """★已定案的月份【整份】都改不動★:月檔唯讀,那些格與鎖定同義。
        ★反例只靠 finalized 分勝負★ —— 這一次沒有 day_locks。"""
        svc = _svc(tmp_path)
        _sep_fixed(svc, locked=False, finalized=True)
        day_slots, _log, _w = month_solve_day(svc.build_day_input(AUG))
        assert _biopsy_on(day_slots, MON) == ["C2"]

    def test_an_unlocked_next_month_slot_is_still_free(self, tmp_path):
        """★反例的對照組★:同樣的內容,但沒鎖也沒定案 → 那一格仍可重排,
        8/31 不受限制(證明上面兩條量到的是「已經定下來」,不是「有東西」)。"""
        svc = _svc(tmp_path)
        _sep_fixed(svc, locked=False, finalized=False)
        inp = svc.build_day_input(AUG)
        assert inp.course_fixed == {}, inp.course_fixed


# ══ ② 過期閘門:預覽期間下個月被改動 ═════════════════════════════════════
class TestTheStaleGateSeesTheNextMonth:
    def test_adding_a_september_lock_changes_the_fingerprint(self, tmp_path):
        """★沒進求解輸入的東西不會進指紋★:8 月預覽 → 9 月加鎖 → 套用,
        舊結果會被照單全收(請假/停診那些都擋得到,唯獨這個漏)。"""
        svc = _svc(tmp_path)
        before = day_input_fingerprint(svc.build_day_input(AUG))
        _sep_fixed(svc)
        after = day_input_fingerprint(svc.build_day_input(AUG))
        assert before != after, "下個月加了鎖定,指紋卻沒變"

    def test_finalizing_september_changes_the_fingerprint(self, tmp_path):
        svc = _svc(tmp_path)
        _sep_fixed(svc, locked=False, finalized=False)
        before = day_input_fingerprint(svc.build_day_input(AUG))
        _sep_fixed(svc, locked=False, finalized=True)
        assert day_input_fingerprint(svc.build_day_input(AUG)) != before


# ══ ③ 不得錯算配額的三種既定時段 ═════════════════════════════════════════
class TestFixedSlotsThatAreNotAssignable:
    """外審點名的第四條:未來的鎖定格若是【空的 / 未知代號 / 敗者梯次】,
    都不是可分配的量 —— 算進分母會把配額撐大,自動排班就會多排。"""

    @staticmethod
    def _solve(tmp_path, slots):
        svc = _svc(tmp_path)
        _sep_fixed(svc, slots=slots)
        return month_solve_day(svc.build_day_input(AUG))[0]

    def test_a_fixed_but_empty_slot_shrinks_the_denominator(self, tmp_path):
        """9/01 鎖成【沒有切片】→ 整梯真正排得到的只剩 8/31 一格,
        配額 max(1, 1//2)=1;那一格給誰都行,但★不可以有人切兩次★。"""
        got = self._solve(tmp_path, {})
        assert len(_biopsy_on(got, MON)) <= 1

    def test_a_fixed_slot_for_an_unknown_code_does_not_count(self, tmp_path):
        """★未知/已換梯的代號不得污染分母★(RF-10):9/01 鎖給了一個不在本梯
        名單的代號 —— 那一格沒有本梯的人會去切,不可以算進本梯的分母。

        ★反例要讓分母差異跨過整數除法的門檻★:5 格 2 人 → 配額 2(用完就留空,
        RS-24);錯把第 6 格算進來 → 配額 3 → 那一格就被填掉。
        我第一版用「2 格 vs 1 格」,兩邊 `max(1, n//2)` 都是 1,量不到(突變假綠燈)。
        """
        got = _solve_five_slot_batch(tmp_path, fixed={BIOPSY: ["ZZ"]})
        assert got == 4, f"未知代號那一格被算進分母了(填了 {got} 格)"

    def test_a_fixed_slot_for_a_member_does_count(self, tmp_path):
        """★對照組★:同一格改成本梯的 C1 → 它算數(分母 6、配額 3)。
        與上一條只差代號在不在名單裡。"""
        assert _solve_five_slot_batch(tmp_path, fixed={BIOPSY: ["C1"]}) == 5

    def test_a_valid_fixed_code_pre_deducts_that_person(self, tmp_path):
        """★對照組★:同一格改成本梯的 C1 → 它算數,C1 被預扣 → 8/31 給 C2。
        (與上一條只差代號在不在名單裡。)"""
        got = self._solve(tmp_path, {BIOPSY: ["C1"]})
        assert _biopsy_on(got, MON) == ["C2"]


# ══ ④ 本月的鎖定不可以被重複吸收 ═════════════════════════════════════════
def test_a_current_month_key_is_not_absorbed_twice(tmp_path):
    """★`course_fixed` 只管相鄰月份★:本月是 `locked` 的地盤。同一格被兩邊
    都吸收的話,`_locked_n`/預扣都會多算一次 —— 直接餵一個本月的鍵給
    求解器,結果必須與沒餵一樣。"""
    svc = _svc(tmp_path)
    base = svc.build_day_input(AUG)
    poisoned = DaySolveInput(
        **{f: getattr(base, f) for f in base.__dataclass_fields__
           if f != "course_fixed"},
        course_fixed={MON.isoformat(): {"上午": {BIOPSY: ["C1"]}}})
    assert (month_solve_day(poisoned)[0] == month_solve_day(base)[0])


# ══ ⑤ 求解器層的直接反例(不經 service)═══════════════════════════════════
def test_the_solver_treats_course_fixed_as_taken(tmp_path):
    """★同一組輸入、只多一個 `course_fixed`★ —— 直接餵求解器,
    排除 service 那一層的任何影響。"""
    grid = {MON: {"上午": ["101"], "下午": []}}
    common = dict(
        ym=AUG, grid=grid, pgy_roster=["P1"],
        clerk_batches=[ClerkBatch("b1", MON, ["C1", "C2"])],
        biopsy_open={"b1": {MON.isoformat(): {"上午": True},
                            SEP1.isoformat(): {"上午": True}}},
        course_days={MON.isoformat(), SEP1.isoformat()})
    free = month_solve_day(DaySolveInput(**common))[0]
    fixed = month_solve_day(DaySolveInput(
        **common,
        course_fixed={SEP1.isoformat(): {"上午": {BIOPSY: ["C1"]}}}))[0]
    assert _biopsy_on(fixed, MON) == ["C2"]
    assert _biopsy_on(free, MON) != _biopsy_on(fixed, MON), (
        "沒有 course_fixed 時本來就會挑 C2 的話,這條反例量不到規則")


def test_a_fixed_entry_on_a_closed_day_has_no_effect_at_all(tmp_path):
    """★既定時段也要通過「那一天真的有開診」★:相鄰月份那一天若不在
    `course_days` 裡(週末/假日/整日停診),它不是可排的量。

    ★反例的性質是「完全沒有影響」★:同一組輸入,只差有沒有那一筆 —— 結果
    必須一模一樣。我第一版只斷言「8/31 還是排了 1 格」,而錯把它算進來之後
    配額仍是 1(整數除法),只是 C1 被預扣 → 那條斷言量不到(突變假綠燈)。
    """
    svc = _svc(tmp_path)
    off = date(2026, 9, 5)                       # 週六:永遠不在格網裡
    svc.storage.save_month(SEP, {
        "day_slots": {off.isoformat(): {"上午": {BIOPSY: ["C1"]}}},
        "day_locks": {off.isoformat(): {"上午": True}}})
    inp = svc.build_day_input(AUG)
    assert off.isoformat() in inp.course_fixed      # service 有交出去
    assert off.isoformat() not in inp.course_days   # 但那天沒開診
    with_entry = month_solve_day(inp)[0]
    clean = DaySolveInput(
        **{f: getattr(inp, f) for f in inp.__dataclass_fields__
           if f != "course_fixed"}, course_fixed={})
    assert with_entry == month_solve_day(clean)[0], (
        "不開診那一天的既定時段影響了結果")


def _solve_five_slot_batch(tmp_path, *, fixed):
    """8/24 起的梯次:8 月開 5 個切片時段 + 9/01 一個【既定】時段。
    → 回傳 8 月實際被填掉的切片格數。"""
    start = date(2026, 8, 24)                    # 週一;梯次 8/24～9/06
    aug_days = [start + timedelta(days=i) for i in range(5)]   # 一~五
    st = RosterStorage(str(tmp_path))
    st.save_config({"pgy_members": [{"id": "P1"}], "r_members": [],
                    "vs_members": [], "room_capacity": 2})
    st.save_clinic_template({"template": {
        str(w): {"上午": [{"room": "101"}], "下午": []} for w in range(5)}})
    st.save_clerk_batches([{"id": "b1", "start_monday": start.isoformat(),
                            "members": ["C1", "C2"]}])
    grid = {d.isoformat(): {"上午": True} for d in aug_days}
    grid[SEP1.isoformat()] = {"上午": True}
    st.save_biopsy_grid({"b1": grid})
    st.save_month(AUG, {})
    st.save_month(SEP, {"day_slots": {SEP1.isoformat(): {"上午": fixed}},
                        "day_locks": {SEP1.isoformat(): {"上午": True}}})
    day_slots, _log, _w = month_solve_day(RosterService(st).build_day_input(AUG))
    return sum(1 for d in aug_days if _biopsy_on(day_slots, d))


def test_a_fixed_next_month_slot_is_not_free_capacity_for_someone_else(
        tmp_path):
    """★既定時段不是「之後還排得到」的機會★ —— 這一條要靠【可行性】分勝負。

    前面那條主反例其實是被「預扣」決定的(C1 配額歸零),就算把既定時段仍
    當成自由容量也照樣過關(突變假綠燈)。要量到 `_locked_keys`,必須讓
    ★別人的最後機會★落在那一格上:

      三格三人(8/31、9/01、9/02),配額各 1;9/01 已鎖給 C1;
      C3 在 9/02 請假 → C3 只剩 8/31 與 9/01,而 9/01 已經有人。
      → 今天(8/31)不排 C3 就永遠排不到,必須強制。
      若把 9/01 當成還空著,求解器以為 C3 之後還有機會 → 今天落到抖動決勝。
    """
    start = date(2026, 8, 31)
    sep1, sep2 = date(2026, 9, 1), date(2026, 9, 2)
    st = RosterStorage(str(tmp_path))
    st.save_config({"pgy_members": [{"id": "P1"}], "r_members": [],
                    "vs_members": [], "room_capacity": 2})
    st.save_clinic_template({"template": {
        str(w): {"上午": [{"room": "101"}], "下午": []} for w in range(5)}})
    st.save_clerk_batches([{"id": "b1", "start_monday": start.isoformat(),
                            "members": ["C1", "C2", "C3"]}])
    st.save_biopsy_grid({"b1": {d.isoformat(): {"上午": True}
                                for d in (start, sep1, sep2)}})
    st.save_month(AUG, {})
    st.save_month(SEP, {
        "day_slots": {sep1.isoformat(): {"上午": {BIOPSY: ["C1"]}}},
        "day_locks": {sep1.isoformat(): {"上午": True}},
        "leaves": {"clerk": {"C3": [sep2.isoformat()]}}})
    day_slots, _log, _w = month_solve_day(RosterService(st).build_day_input(AUG))
    assert _biopsy_on(day_slots, start) == ["C3"], (
        f"9/01 被當成 C3 之後還排得到的機會了: {_biopsy_on(day_slots, start)}")


def test_a_previous_month_fixed_slot_is_not_deducted_twice(tmp_path):
    """★上個月的既定時段不可以被扣兩次★(外審 RS-29 R1 P1)。

    跨月梯次的上月時段已經由 `prior_sessions` 回放進 `fc.biopsy_done`(RF-09);
    配額是 `cap - biopsy_done - 未來的鎖定預留`。我把相鄰月份【一律】標成
    `_FUTURE_POS`,於是同一次切片既算「已完成」又算「預留」——
    那個人本月的配額憑空少一次。(`o > _now` 這個判準本來就是為了不重複扣。)

    梯次 7/27~8/09、C1/C2;整梯四格(7/28、8/03、8/04、8/05)→ 配額各 2。
    7/28 已鎖給 C1 → C1 本月還該切 1 次、C2 該切 2 次 → 8 月三格全滿。
    重複扣的話 C1 配額變 0 → 只剩 C2 能排、又被 cap 2 夾住 → 只填 2 格。

    ★反例只靠「上月 vs 下月」分勝負★:同樣是 `course_fixed` 裡的一格,
    只是它落在本月【之前】。
    """
    start = date(2026, 7, 27)                    # 週一;梯次 7/27～8/09
    jul_slot = date(2026, 7, 28)
    aug_days = [date(2026, 8, 3), date(2026, 8, 4), date(2026, 8, 5)]
    st = RosterStorage(str(tmp_path))
    st.save_config({"pgy_members": [{"id": "P1"}], "r_members": [],
                    "vs_members": [], "room_capacity": 2})
    st.save_clinic_template({"template": {
        str(w): {"上午": [{"room": "101"}], "下午": []} for w in range(5)}})
    st.save_clerk_batches([{"id": "b1", "start_monday": start.isoformat(),
                            "members": ["C1", "C2"]}])
    st.save_biopsy_grid({"b1": {d.isoformat(): {"上午": True}
                                for d in [jul_slot] + aug_days}})
    st.save_month("2026-07", {
        "day_slots": {jul_slot.isoformat(): {"上午": {BIOPSY: ["C1"]}}},
        "day_locks": {jul_slot.isoformat(): {"上午": True}}})
    st.save_month(AUG, {})
    day_slots, _log, _w = month_solve_day(RosterService(st).build_day_input(AUG))
    got = [c for d in aug_days for c in _biopsy_on(day_slots, d)]
    assert len(got) == 3, f"上月那一格被扣了兩次 → 8 月少排: {got}"
    assert got.count("C1") == 1 and got.count("C2") == 2, got


def test_a_previous_month_slot_locked_empty_shrinks_the_denominator(tmp_path):
    """★上月的既定時段仍要參與分母調整★:那一格在格網裡是開放的,卻被鎖成
    【沒有切片】—— 沒有人會在那裡切,不可以算進整梯的分母。

    ★反例要讓分母差異跨過整數除法門檻★:6 格 2 人 → 配額 3(8 月五格全填);
    正確地把那一格拿掉 → 5 格 → 配額 2 → 8 月只填 4 格,留一格空
    (配額用完就留空,RS-24)。
    (前一條「不可重複扣」量的是【位置】;這一條量的是【有沒有被吸收】——
     兩者是不同的規則,各自要有反例。)
    """
    start = date(2026, 7, 27)                    # 梯次 7/27～8/09
    jul_slot = date(2026, 7, 28)
    aug_days = [date(2026, 8, 3) + timedelta(days=i) for i in range(5)]
    st = RosterStorage(str(tmp_path))
    st.save_config({"pgy_members": [{"id": "P1"}], "r_members": [],
                    "vs_members": [], "room_capacity": 2})
    st.save_clinic_template({"template": {
        str(w): {"上午": [{"room": "101"}], "下午": []} for w in range(5)}})
    st.save_clerk_batches([{"id": "b1", "start_monday": start.isoformat(),
                            "members": ["C1", "C2"]}])
    st.save_biopsy_grid({"b1": {d.isoformat(): {"上午": True}
                                for d in [jul_slot] + aug_days}})
    st.save_month("2026-07", {
        "day_slots": {jul_slot.isoformat(): {"上午": {}}},      # 鎖成空的
        "day_locks": {jul_slot.isoformat(): {"上午": True}}})
    st.save_month(AUG, {})
    day_slots, _log, _w = month_solve_day(RosterService(st).build_day_input(AUG))
    filled = sum(1 for d in aug_days if _biopsy_on(day_slots, d))
    assert filled == 4, f"上月那個空鎖定格被算進分母了(8 月填了 {filled} 格)"
