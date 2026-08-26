# -*- coding: utf-8 -*-
"""批次 RS-28(前半):切片配額的【現況】點名(全審 2026-08-24 的 P2-03)。

★求解當下說過的話,手改之後要有人再說一次★
「切片室輪不到 / 次數不均」原本只長在求解器裡,而且只算【那一次求解的結果】。
RS-24 的配額平均正是靠月曆上那些格,使用者手改之後沒有任何地方再檢查一次
—— 報告是求解當下那一份,側欄的紅底又還用著 RS-24 之前的舊規則
(「至少跟過一次切片室」)。
"""
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.solve_day import (  # noqa: E402
    BIOPSY, biopsy_quota_warnings,
)
from cmuh_common.roster.storage import RosterStorage  # noqa: E402

YM = "2026-08"
MON = date(2026, 8, 3)          # 週一,梯次 b1 = 8/03～8/16


class _B:
    """最小的梯次替身(只需要 id / members)。"""

    def __init__(self, bid, members):
        self.id, self.members = bid, members


def _svc(tmp_path, *, day_slots=None, opens=4, start=None, bid="b1"):
    st = RosterStorage(str(tmp_path))
    st.save_config({"pgy_members": [{"id": "P1"}], "r_members": [],
                    "vs_members": [], "room_capacity": 2})
    st.save_clinic_template({"template": {
        str(w): {"上午": [{"room": "101"}], "下午": []} for w in range(5)}})
    _s = start or MON
    st.save_clerk_batches([{"id": bid, "start_monday": _s.isoformat(),
                            "members": ["C1", "C2"]}])
    st.save_biopsy_grid({bid: {
        (_s + timedelta(days=i)).isoformat(): {"上午": True}
        for i in range(opens)}})
    st.save_month(YM, {"day_slots": day_slots or {}})
    return RosterService(st)


def _bio(**by_day):
    return {(MON + timedelta(days=int(i))).isoformat():
            {"上午": {BIOPSY: [c]}} for i, c in by_day.items()}


# ══ 1. 共用判準本身 ═════════════════════════════════════════════════════
class TestTheSharedQuotaRule:
    BATCH = [_B("b1", ["C1", "C2"])]

    def test_equal_counts_are_silent(self):
        out, flagged = biopsy_quota_warnings(
            self.BATCH, {("b1", "C1"): 2, ("b1", "C2"): 2})
        assert (out, flagged) == ([], set())

    def test_a_one_off_difference_is_reported(self):
        """★配額制下「次數一樣」是要求★(RS-24):差一次也要點名。"""
        out, flagged = biopsy_quota_warnings(
            self.BATCH, {("b1", "C1"): 2, ("b1", "C2"): 1})
        assert any("次數不均" in m for m in out), out
        assert flagged == {("b1", "C2")}, "紅底要標在比較少的那個人身上"

    def test_a_batch_with_more_ahead_is_lenient(self):
        """★跨月梯次的第一個月本來就只排得到一半★ —— 那時的差異不是異常,
        維持 >1 的門檻,免得每個月都跳一次噪音。"""
        out, flagged = biopsy_quota_warnings(
            self.BATCH, {("b1", "C1"): 2, ("b1", "C2"): 1},
            batch_more={"b1"})
        assert (out, flagged) == ([], set())
        out2, _ = biopsy_quota_warnings(
            self.BATCH, {("b1", "C1"): 3, ("b1", "C2"): 1},
            batch_more={"b1"})
        assert any("次數不均" in m for m in out2), "放寬不等於不看"

    def test_nobody_scheduled_is_reported_as_missed(self):
        out, flagged = biopsy_quota_warnings(
            self.BATCH, {("b1", "C1"): 2, ("b1", "C2"): 0})
        assert any("輪不到" in m and "C2" in m for m in out), out
        assert flagged == {("b1", "C2")}

    def test_missed_wins_over_uneven(self):
        """★一個人都沒輪到時只說「輪不到」★:兩句話同時跳只會互相稀釋,
        而且處置一樣(去補他一次)。"""
        out, _ = biopsy_quota_warnings(
            self.BATCH, {("b1", "C1"): 3, ("b1", "C2"): 0})
        assert len(out) == 1 and "輪不到" in out[0]

    def test_only_ids_limits_the_batches(self):
        """邊界梯次(這個月一天都還沒排)本來就是 0 次,點名它只是噪音。"""
        out, _ = biopsy_quota_warnings(
            self.BATCH, {}, only_ids=set())
        assert out == []


# ══ 2. service 的現況檢查 ═══════════════════════════════════════════════
class TestTheCurrentStateIsChecked:
    def test_a_manual_edit_that_breaks_the_quota_is_reported(self, tmp_path):
        """★這一批的核心★:手改之後沒有再求解,而現況已經不平均了。"""
        svc = _svc(tmp_path, day_slots=_bio(**{"0": "C1", "1": "C1",
                                               "2": "C1", "3": "C2"}))
        out, flagged = svc.validate_course_quota(YM)
        assert any("次數不均" in m for m in out), out
        assert flagged == {("b1", "C2")}
        assert any("次數不均" in m for m in svc.quick_validate_day(YM))

    def test_an_even_manual_result_is_silent(self, tmp_path):
        """★反例只靠「均不均」分勝負★ —— 同樣是手改、同樣四格。"""
        svc = _svc(tmp_path, day_slots=_bio(**{"0": "C1", "1": "C1",
                                               "2": "C2", "3": "C2"}))
        assert svc.validate_course_quota(YM) == ([], set())

    def test_a_month_with_nothing_scheduled_is_silent(self, tmp_path):
        """★沒排到東西的月份不點名★:否則每開一個新月份就跳兩句「輪不到」。"""
        svc = _svc(tmp_path, day_slots={})
        assert svc.validate_course_quota(YM) == ([], set())

    def test_cross_month_residue_does_not_activate_a_batch(self, tmp_path):
        """他月的殘留鍵不算「本月排到了東西」(同 RS-27 的過濾)。

        ★反例要讓那個殘留鍵【真的屬於某一梯】★:梯次 b2 從 8/31 起(涵蓋
        9/01),所以少了月份過濾的話,9/01 那個殘留鍵會把 b2 點亮成「本月有
        排到東西」,接著兩位 Clerk 就被判成「輪不到」——而 8 月根本還沒排。
        """
        svc = _svc(tmp_path, bid="b2", start=date(2026, 8, 31),
                   day_slots={"2026-09-01": {"上午": {BIOPSY: ["C1"]}}})
        assert svc.validate_course_quota(YM) == ([], set())


# ══ 3. 求解器與現況檢查共用同一份判準 ═══════════════════════════════════
def test_the_solver_uses_the_same_function():
    """★沒有呼叫端的共用函式等於沒有共用★:求解器的月底點名必須是它。"""
    import ast
    import inspect
    from cmuh_common.roster import solve_day
    tree = ast.parse(inspect.getsource(solve_day))
    users = {n.name for n in ast.walk(tree)
             if isinstance(n, ast.FunctionDef)
             and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                     and c.func.id == "biopsy_quota_warnings"
                     for c in ast.walk(n))}
    assert "_solve_month_once" in users, "求解器沒有走共用判準"


@pytest.mark.parametrize("bad", ["至少跟過一次切片室", "尚未排到"])
def test_the_sidebar_text_is_not_the_pre_quota_rule(bad):
    """★側欄那段字是 RS-24 之前的規則★:配額平均之後,「有排到就好」不再成立。"""
    p = os.path.join(os.path.dirname(__file__), "..", "src", "cmuh_common",
                     "roster", "ui", "day_tab.py")
    with open(p, encoding="utf-8") as f:
        src = f.read()
    body = src[src.index("def _build_side"):src.index("def refresh")]
    body = "\n".join(ln.split("#")[0] for ln in body.splitlines())
    assert bad not in body, f"側欄還在說「{bad}」"


# ══ 4. 外審第 1 輪的三個 CONFIRMED ══════════════════════════════════════
class TestTheRoundOneFindings:
    def test_counts_are_attributed_to_the_winner_batch(self, tmp_path):
        """★不可以拿代號統計當梯次命名空間★(R1 P1):Clerk 代號跨梯會重用,
        而重疊日只有勝者梯次排得到人。

        b1(8/03~8/16)與 b2(8/10~8/23)部分重疊,兩梯都有一位「C1」。
        8/10 由 b1 勝 —— 那一次切片是 b1 的 C1 切的。b2 自己的日子一格都沒排,
        所以 b2 的 C1 與 C3 ★都★該被點名「輪不到」。用日期範圍+代號去數的話,
        b1 那一次會被算進 b2 的 C1,於是只點名 C3。

        ★反例只靠歸屬分勝負★:人數、身分、開放時段全都合法。
        """
        st = RosterStorage(str(tmp_path))
        st.save_config({"pgy_members": [{"id": "P1"}], "r_members": [],
                        "vs_members": [], "room_capacity": 2})
        st.save_clinic_template({"template": {
            str(w): {"上午": [{"room": "101"}], "下午": []} for w in range(5)}})
        st.save_clerk_batches([
            {"id": "b1", "start_monday": MON.isoformat(),
             "members": ["C1", "C2"]},
            {"id": "b2", "start_monday": (MON + timedelta(days=7)).isoformat(),
             "members": ["C1", "C3"]},
        ])
        _ovl = (MON + timedelta(days=7)).isoformat()        # 8/10,b1 勝
        st.save_biopsy_grid({"b1": {_ovl: {"上午": True}},
                             "b2": {(MON + timedelta(days=14)).isoformat():
                                    {"上午": True}}})
        st.save_month(YM, {"day_slots": {
            _ovl: {"上午": {BIOPSY: ["C1"]}},
            (MON + timedelta(days=14)).isoformat(): {"上午": {"101": ["C1"]}},
        }})
        out, _flag = RosterService(st).validate_course_quota(YM)
        _b2 = [m for m in out if "b2" in m]
        assert _b2, out
        assert "C1" in _b2[0] and "C3" in _b2[0], (
            f"b1 的切片被算進 b2 的同名 C1 了: {_b2}")

    def test_over_quota_is_reported_even_when_everyone_is_equal(self, tmp_path):
        """★「次數相同」不足以證明沒有超過配額★(R1 P2):只開兩格、兩個人 →
        配額各 1 次;手改成各 2 次的話全距是 0,舊判準完全靜默,
        而 RS-24 明定配額用完的時段要留空。"""
        svc = _svc(tmp_path, opens=2,
                   day_slots=_bio(**{"0": "C1", "1": "C2",
                                     "2": "C1", "3": "C2"}))
        out, _ = svc.validate_course_quota(YM)
        assert any("超過配額" in m for m in out), out

    def test_exactly_on_quota_is_silent(self, tmp_path):
        """★反例只靠「有沒有超過」分勝負★ —— 同樣兩格兩人,剛好各 1 次。"""
        svc = _svc(tmp_path, opens=2,
                   day_slots=_bio(**{"0": "C1", "1": "C2"}))
        assert svc.validate_course_quota(YM) == ([], set())

    def test_a_key_outside_the_clinic_grid_does_not_activate(self, tmp_path):
        """★掉出開診格網的同月鍵不算「排到了東西」★(R1 P2):鎖定日事後變成
        假日/整日停診時 RF-02 會原樣保留那一格。求解器的 `solved_batch_ids`
        是在迭代 `inp.grid` 時才加入梯次的 —— 只看年月並不等價。

        用週六(8/08):它在本月、屬於 b1,但永遠不在開診格網裡。
        """
        svc = _svc(tmp_path, day_slots={
            (MON + timedelta(days=5)).isoformat(): {"上午": {BIOPSY: ["C1"]}}})
        assert svc.validate_course_quota(YM) == ([], set())

    def test_a_duplicate_residue_in_another_months_file_is_not_double_counted(
            self, tmp_path):
        """★各月檔只採本月鍵★在【計數層】也要成立(RS-27 的同一份契約):
        7 月檔殘留一個 8/03 的鍵、內容與 8 月檔的權威 8/03 相同(跨機合併的
        典型殘留)。計數層少了月份過濾就會把同一次切片算兩次 → C1 被誤報
        「超過配額 / 次數不均」,而現況其實完全平均。

        ★反例只靠計數層的過濾分勝負★:active 判斷(`d in inp.grid`)只看
        本月檔的鍵,7 月檔的殘留根本不經過它。
        """
        svc = _svc(tmp_path, opens=2,
                   day_slots=_bio(**{"0": "C1", "1": "C2"}))
        svc.storage.save_month("2026-07", {"day_slots": {
            MON.isoformat(): {"上午": {BIOPSY: ["C1"]}}}})
        assert svc.validate_course_quota(YM) == ([], set())


# ══ 5. 外審第 2 輪:cap 的分母要套鎖定調整(與求解器同一份)═══════════════
class TestLockedSlotsAdjustTheCap:
    """★兩邊必須用同一個「有效時段集合」★(外審 RS-28 R2 P2):求解器的分母
    會被鎖定調整(鎖定格先拿掉、有效鎖定切片加回),service 用原始盤點的話,
    求解器合法排出的平均結果會被誤報「超過配額」,反向也會漏掉等量超額。"""

    def _locked_svc(self, tmp_path, *, day_slots, day_locks):
        svc = _svc(tmp_path, opens=4, day_slots=day_slots)
        m = svc.storage.load_month(YM)
        m["day_locks"] = day_locks
        svc.storage.save_month(YM, m)
        return svc

    def test_a_valid_locked_biopsy_slot_raises_the_cap(self, tmp_path):
        """4 個開放格 + 2 個有效鎖定切片 → 分母 6、cap 3;兩人各 3 次是
        ★求解器自己就會排出來★的平均結果,不得誤報。
        (用原始盤點的話 cap=4//2=2 → 兩人都被誤報超過配額。)"""
        d5, d6 = "2026-08-10", "2026-08-11"          # 週一/週二,b1 涵蓋
        svc = self._locked_svc(
            tmp_path,
            day_slots=dict(
                _bio(**{"0": "C1", "1": "C2", "2": "C2", "3": "C2"}),
                **{d5: {"上午": {BIOPSY: ["C1"]}},
                   d6: {"上午": {BIOPSY: ["C1"]}}}),
            day_locks={d5: {"上午": True}, d6: {"上午": True}})
        assert svc.validate_course_quota(YM) == ([], set())

    def test_a_locked_empty_session_shrinks_the_cap(self, tmp_path):
        """反向:原本開放的格被鎖成【沒有切片】→ 它不是可分配的量,分母 3、
        cap 1 —— C1 的 2 次是超額,必須點名。(原始盤點 cap=2 → 靜默漏報。)

        ★反例只靠 discard 那一條分勝負★:有效鎖定切片的加回在這裡不發生
        (鎖定內容沒有切片室)。
        """
        d4 = (MON + timedelta(days=3)).isoformat()   # 8/06,開放格之一
        svc = self._locked_svc(
            tmp_path,
            day_slots=dict(
                _bio(**{"0": "C1", "1": "C1", "2": "C2"}),
                **{d4: {"上午": {}}}),
            day_locks={d4: {"上午": True}})
        out, _ = svc.validate_course_quota(YM)
        assert any("超過配額" in m and "C1" in m for m in out), out
