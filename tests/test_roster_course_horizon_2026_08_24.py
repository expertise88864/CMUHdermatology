# -*- coding: utf-8 -*-
"""[批次RS-26 / 全審 2026-08-24 P1-01 + P2-01]

★最佳化的視野是兩週,限制的視野卻只有本月★

RS-24 讓切片配額用【整梯】的開放時段當分母(跨月梯次也算得到下個月那半段),
但「誰哪幾天在」「那一天到底開不開診」卻只讀本月的月檔 —— 於是:

1. 求解會在【存在可行解】的情況下排出「下個月那個人請假、配額補不完」的結果
   (跨月梯次的最後一格挑錯人);
2. 套用時的過期閘門也看不到下個月的請假 —— 它根本不在這次求解的輸入裡,
   所以不會被判定成過期(違反「所有 solver 輸入都要進指紋」這條架構原則)。

P2-01:切片開放本來先被壓平成全域 map 才選勝者梯次 —— 重疊梯次(RF-08 只採
原始順序第一個)的敗者設定會污染勝者。勝者政策要貫穿【所有】輸入維度。
"""
import os
import sys
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.model import ClerkBatch                  # noqa: E402
from cmuh_common.roster.service import RosterService             # noqa: E402
from cmuh_common.roster.solve_day import (                       # noqa: E402
    BIOPSY, DaySolveInput, month_solve_day,
)
from cmuh_common.roster.storage import RosterStorage             # noqa: E402

AUG, SEP = "2026-08", "2026-09"
MON = date(2026, 8, 31)         # 週一,梯次 8/31～9/13(跨月)


def _svc(tmp_path) -> RosterService:
    """8/31 起的跨月梯次;整梯只開兩個切片時段:8/31 早、9/01 早。"""
    st = RosterStorage(str(tmp_path))
    st.save_config({"pgy_members": [{"id": "P1"}],
                    "r_members": [], "vs_members": []})
    st.save_clinic_template({"template": {                # 週一~五早上都有 101
        str(w): {"上午": [{"room": "101"}], "下午": []} for w in range(5)}})
    st.save_clerk_batches([{"id": "b1", "start_monday": MON.isoformat(),
                            "members": ["C1", "C2"]}])
    st.save_biopsy_grid({"b1": {
        MON.isoformat(): {"上午": True},
        (MON + timedelta(days=1)).isoformat(): {"上午": True}}})
    st.save_month(AUG, {})
    st.save_month(SEP, {})
    return RosterService(st)


# ══ P1-01 求解:下個月的請假要進得來 ══════════════════════════════════════
class TestTheCourseHorizon:
    def test_next_month_leave_reaches_the_solver(self, tmp_path):
        """★接上去了才存在★:9/01 的請假存在【9 月】的月檔裡,而排 8 月時
        它必須進得到求解輸入(否則配額的可行性判斷是瞎的)。"""
        svc = _svc(tmp_path)
        svc.storage.save_month(SEP, {"leaves": {"clerk": {
            "C2": [(MON + timedelta(days=1)).isoformat()]}}})
        inp = svc.build_day_input(AUG)
        assert (MON + timedelta(days=1)) in (inp.leaves["clerk"].get("C2")
                                             or set())

    def test_august_must_leave_the_last_slot_to_whoever_can_still_take_it(
            self, tmp_path):
        """★反例本體★(外審 2026-08-24 P1-01):整梯 2 個切片時段、2 個人 →
        配額各 1 次。C2 在 9/01 請假,所以唯一的可行解是【8/31 給 C2】。

        只看本月的話,8/31 是本月最後一個(也是唯一一個)切片時段,兩個人在
        求解眼中「誰去都行」→ 抖動選了 C1 → 9 月 C2 補不到,而月底只會出現
        一句「切片室輪不到」的警告。
        """
        svc = _svc(tmp_path)
        svc.storage.save_month(SEP, {"leaves": {"clerk": {
            "C2": [(MON + timedelta(days=1)).isoformat()]}}})
        res = svc.run_day_solve(AUG)
        got = ((res.day_slots.get(MON.isoformat()) or {})
               .get("上午") or {}).get(BIOPSY)
        assert got == ["C2"], f"8/31 應該留給之後補不到的 C2: {got}"

    def test_without_the_leave_either_one_may_take_it(self, tmp_path):
        """沒有那筆請假時本來就兩個人都可以 —— 這條確認上面那個反例是
        【被規則決定】的,不是被別的東西鎖死。"""
        svc = _svc(tmp_path)
        res = svc.run_day_solve(AUG)
        got = ((res.day_slots.get(MON.isoformat()) or {})
               .get("上午") or {}).get(BIOPSY)
        assert got in (["C1"], ["C2"]), got

    def test_a_next_month_closure_is_not_counted_as_capacity(self, tmp_path):
        """★下個月那一天到底開不開診★也要看得到:9/01 整天停診時,整梯真正
        可排的只有 8/31 一格 → 配額 `1//2 = 0 → 取 1`,只有一個人排得到。
        (算進去的話配額仍是 1,但「之後還補得完」的判斷會是假的。)"""
        svc = _svc(tmp_path)
        nxt = (MON + timedelta(days=1)).isoformat()
        svc.storage.save_month(SEP, {"grid_overrides": {
            nxt: {"上午": {"closed_rooms": ["101"]}}}})
        inp = svc.build_day_input(AUG)
        assert nxt in inp.course_days, "停診【房間】不代表那天沒開診"
        svc.storage.save_holiday_duty({"r": {nxt: "X"}, "vs": {}})
        inp2 = svc.build_day_input(AUG)
        assert nxt not in inp2.course_days, "假日不該算成可排的量"


class TestTheCourseStartsBeforeThisMonth:
    """★鏡像方向★(外審 RS-26 R1 P1):梯次也可以【從上個月開始】——
    求解 9 月時,8 月那半段的時段一樣是整梯配額的一部分。"""

    def test_last_months_half_still_counts_in_the_quota(self, tmp_path):
        svc = _svc(tmp_path)
        inp = svc.build_day_input(SEP)
        assert MON.isoformat() in inp.course_days, (
            "上個月那半段被腰斬了 → 配額分母只剩 9 月")

    def test_a_month_with_no_file_still_has_clinic_days(self, tmp_path):
        """★沒有月檔 ≠ 沒有開診★:開診日由門診模板與年度假日決定,
        月檔只提供 override/請假 —— 讀不到就當「沒有 override」。"""
        svc = _svc(tmp_path)
        oct_ = "2026-10"
        assert not svc.storage.month_exists(oct_)
        inp = svc.build_day_input(SEP)
        assert any(iso.startswith(oct_) for iso in inp.course_days)


# ══ P1-01 套用:下個月的請假要讓舊預覽過期 ════════════════════════════════
class TestTheStaleGateSeesTheCourse:
    def test_a_next_month_leave_makes_the_preview_stale(self, tmp_path):
        """★所有 solver 輸入都要進指紋★:8 月預覽開著時,他機在 9 月加了一筆
        請假 —— 那筆請假會改變 8 月該把切片給誰,所以舊預覽必須被判過期。"""
        svc = _svc(tmp_path)
        res = svc.run_day_solve(AUG)
        svc.storage.save_month(SEP, {"leaves": {"clerk": {
            "C2": [(MON + timedelta(days=1)).isoformat()]}}})
        with pytest.raises(ValueError, match="過期"):
            svc.accept_day_solution(AUG, res.day_slots, "", expect=res)


# ══ 鎖定時段不是「還能分給別人」的未來容量 ════════════════════════════════
class TestALockedSlotIsNotFutureCapacity:
    def test_it_does_not_fabricate_a_chance_for_someone_else(self):
        """★反例本體★(外審 RS-26 R1 P2):未來的鎖定切片已經指定給 "K1",
        它算進配額分母(那一格確實有人切),但★不是還能分給別人的機會★。

        4 個開放時段、3 個人 → 配額各 1 次;"K1" 由週二的鎖定格補滿。
        "1" 只有週一、週二在(週三、週四請假)—— 而週二那格是鎖定的,
        所以他唯一的機會就是【今天(週一)】。
        可行性匹配若把鎖定格當成未來容量,就會虛構出「"1" 週二還排得到」→
        今天不強制 → 落到抖動(這一天偏好 "6")→ "1" 整梯輪不到。
        ★兩個候選的配額一樣★(都還缺 1),所以這條測試量的是可行性判準本身,
        不是「還缺得多的先補」那一鍵。
        """
        d1 = date(2026, 8, 3)                       # 一、二、三、四(早診)
        days = [d1 + timedelta(days=i) for i in range(4)]
        grid = {d: {"上午": ["101"], "下午": []} for d in days}
        bat = ClerkBatch("b1", d1, ["6", "1", "K1"])
        day_slots, _log, _w = month_solve_day(DaySolveInput(
            ym=AUG, grid=grid, pgy_roster=["P1"], clerk_batches=[bat],
            biopsy_open={"b1": {d.isoformat(): {"上午": True} for d in days}},
            locked={days[1].isoformat(): {"上午": {BIOPSY: ["K1"]}}},
            leaves={"clerk": {"1": {days[2], days[3]}}}))
        got: dict = {}
        for d in days:
            for c in ((day_slots.get(d.isoformat()) or {}).get("上午")
                      or {}).get(BIOPSY) or []:
                got[c] = got.get(c, 0) + 1
        assert got.get("1") == 1, (
            f"鎖定格被當成「之後還排得到」的機會 → 今天挑錯人: {got}")


# ══ P2-01 重疊梯次:切片開放要向勝者梯次拿 ════════════════════════════════
class TestOverlappingBatchesKeepTheirOwnOpenings:
    def _grid(self):
        g = {}
        d = MON
        while d < MON + timedelta(days=5):
            g[d] = {"上午": ["101"], "下午": []}
            d += timedelta(days=1)
        return g

    def test_the_loser_cannot_open_the_winners_biopsy_room(self):
        """★反例本體★:b1 是勝者(原始順序第一個)、它那天沒開切片室;
        b2 是敗者、它開了。壓平成全域 map 的話,b1 的 Clerk 會被叫去一個
        其實只替 b2 開的切片室。"""
        b1 = ClerkBatch("b1", MON, ["C1", "C2"])
        b2 = ClerkBatch("b2", MON, ["D1", "D2"])
        day_slots, _log, _w = month_solve_day(DaySolveInput(
            ym=AUG, grid=self._grid(), pgy_roster=["P1"],
            clerk_batches=[b1, b2],
            biopsy_open={"b2": {MON.isoformat(): {"上午": True}}}))
        assert BIOPSY not in (day_slots[MON.isoformat()]["上午"] or {})

    def test_the_losers_own_quota_excludes_days_it_can_never_be_scheduled(
            self):
        """★分母也要套勝者判準★(外審 RS-26 R1 P2):敗者在重疊日的開放是它
        【永遠排不到】的量 —— 算進它自己的分母會把配額撐大,之後真正輪到它
        當勝者的那幾格就會被填成不一致。

        b1(8/31 起)與 b2(9/7 起)★部分重疊★:9/7~9/13 由 b1 勝。
        b2 有 2 人;重疊日開了 3 個切片(它永遠排不到)、自己的日子開了 3 個。
        分母若含重疊日 → `6//2 = 3` → 自己那 3 格全被填成 2/1;
        正解分母是 3 → 配額 `3//2 = 1` → 兩人各 1 次,剩一格留空。
        """
        b1 = ClerkBatch("b1", MON, ["C1", "C2"])            # 8/31~9/13
        b2 = ClerkBatch("b2", MON + timedelta(days=7),      # 9/7~9/20
                        ["D1", "D2"])
        overlap = [MON + timedelta(days=i) for i in (7, 8, 9)]
        own = [MON + timedelta(days=i) for i in (14, 15, 16)]
        grid = {d: {"上午": ["101"], "下午": []} for d in overlap + own}
        day_slots, _log, _w = month_solve_day(DaySolveInput(
            ym="2026-09", grid=grid, pgy_roster=["P1"],
            clerk_batches=[b1, b2],
            biopsy_open={
                "b1": {d.isoformat(): {"上午": True} for d in overlap},
                "b2": {d.isoformat(): {"上午": True}
                       for d in overlap + own}}))
        got: dict = {}
        for d in own:
            for c in ((day_slots.get(d.isoformat()) or {}).get("上午")
                      or {}).get(BIOPSY) or []:
                got[c] = got.get(c, 0) + 1
        assert sorted(got.items()) == [("D1", 1), ("D2", 1)], (
            f"敗者把重疊日算進自己的配額了: {got}")
        assert sum(got.values()) == 2, f"多出來的那一格應留空: {got}"

    def test_a_neighbour_batch_that_ended_before_this_month_still_wins(
            self, tmp_path):
        """★反例本體★(外審 RS-26 R2 P2):RF-08 的勝者是【逐日】判定的,
        而 `batches_covering` 只給「涵蓋本月」的梯次 —— 上個月某一天的勝者
        可能是一個【本月開始前就結束】的梯次(b0)。求解器看不到它,就會誤以為
        自己(b1)勝出,把那些永遠排不到的時段算進自己的配額分母。

        b0(8/17~8/30,原始順序在前)與 b1(8/24~9/6)在 8/24~8/30 重疊;
        求解 9 月時 b0 根本不涵蓋 9 月。b1 在重疊日有 3 個開放(排不到)、
        在 9 月有 3 個開放 → 正解分母 3、配額 1;分母若含重疊日 → 配額 2。
        """
        st = RosterStorage(str(tmp_path))
        st.save_config({"pgy_members": [{"id": "P1"}],
                        "r_members": [], "vs_members": []})
        st.save_clinic_template({"template": {
            str(w): {"上午": [{"room": "101"}], "下午": []} for w in range(5)}})
        b0_start, b1_start = date(2026, 8, 17), date(2026, 8, 24)
        st.save_clerk_batches([
            {"id": "b0", "start_monday": b0_start.isoformat(),
             "members": ["E1", "E2"]},
            {"id": "b1", "start_monday": b1_start.isoformat(),
             "members": ["F1", "F2"]}])
        overlap = [b1_start + timedelta(days=i) for i in (0, 1, 2)]
        own = [date(2026, 9, 1), date(2026, 9, 2), date(2026, 9, 3)]
        st.save_biopsy_grid({"b1": {d.isoformat(): {"上午": True}
                                    for d in overlap + own}})
        st.save_month("2026-08", {})
        st.save_month("2026-09", {})
        svc = RosterService(st)
        res = svc.run_day_solve("2026-09")
        got: dict = {}
        for d in own:
            for c in ((res.day_slots.get(d.isoformat()) or {}).get("上午")
                      or {}).get(BIOPSY) or []:
                got[c] = got.get(c, 0) + 1
        assert sorted(got.items()) == [("F1", 1), ("F2", 1)], (
            f"上個月的鄰居梯次沒進勝者判準 → 配額被撐大: {got}")
        assert sum(got.values()) == 2, f"多出來的那一格應留空: {got}"
        # ★仲裁用的順序不可以借用 `clerk_batches`★(外審 RS-26 R3):
        #   那個欄位的契約是「涵蓋本月的梯次」,統計/側欄/報告都照它列人 ——
        #   把鄰居塞進去,本月的側欄就會多出一個上個月就結束的梯次與它的成員。
        inp = svc.build_day_input("2026-09")
        assert [b.id for b in inp.clerk_batches] == ["b1"], (
            f"本月的梯次清單被仲裁用的鄰居污染了: "
            f"{[b.id for b in inp.clerk_batches]}")
        assert [b.id for b in inp.batch_order] == ["b0", "b1"], (
            "仲裁用的順序要含鄰居,而且照原始清單順序")
        stats = svc.day_course_stats("2026-09")
        assert not ({"E1", "E2"} & set(stats)), (
            f"本月統計多出上個月就結束的梯次成員: {sorted(stats)}")

    def test_the_prior_month_replay_uses_the_real_winner(self, tmp_path):
        """★反例本體★(外審 RS-26 R4):RF-09 回放也要由完整的勝者順序決定。

        重疊日的勝者是鄰居 b0,而 ★Clerk 代號跨梯會重用★ —— 上月那天排的
        其實是 b0 的 C1。若回放時把它記進 b1/C1,本月就會多扣 b1 的 C1 一次
        配額(公平計數刻意用 (梯次, 代號) 隔開,選錯梯次等於繞過那道隔離)。
        """
        st = RosterStorage(str(tmp_path))
        st.save_config({"pgy_members": [{"id": "P1"}],
                        "r_members": [], "vs_members": []})
        st.save_clinic_template({"template": {
            str(w): {"上午": [{"room": "101"}], "下午": []} for w in range(5)}})
        b0_start, b1_start = date(2026, 8, 17), date(2026, 8, 24)
        st.save_clerk_batches([
            {"id": "b0", "start_monday": b0_start.isoformat(),
             "members": ["C1", "E2"]},          # ★兩梯共用代號 C1★
            {"id": "b1", "start_monday": b1_start.isoformat(),
             "members": ["C1", "F2"]}])
        own = [date(2026, 9, 1), date(2026, 9, 2)]
        st.save_biopsy_grid({"b1": {d.isoformat(): {"上午": True}
                                    for d in own}})
        # 上月重疊日(勝者是 b0)那一天,C1 已經切過一次 —— 那是 b0 的 C1
        overlap_day = b1_start + timedelta(days=1)
        st.save_month("2026-08", {"day_slots": {
            overlap_day.isoformat(): {"上午": {BIOPSY: ["C1"]}}}})
        st.save_month("2026-09", {})
        svc = RosterService(st)
        res = svc.run_day_solve("2026-09")
        got: dict = {}
        for d in own:
            for c in ((res.day_slots.get(d.isoformat()) or {}).get("上午")
                      or {}).get(BIOPSY) or []:
                got[c] = got.get(c, 0) + 1
        assert sorted(got.items()) == [("C1", 1), ("F2", 1)], (
            f"上月屬於 b0 的切片被記到 b1 的同名 Clerk 身上: {got}")

    def test_the_loser_cannot_close_the_winners_biopsy_room(self):
        """反方向:敗者沒開,不可以把勝者的開放關掉。"""
        b1 = ClerkBatch("b1", MON, ["C1", "C2"])
        b2 = ClerkBatch("b2", MON, ["D1", "D2"])
        day_slots, _log, _w = month_solve_day(DaySolveInput(
            ym=AUG, grid=self._grid(), pgy_roster=["P1"],
            clerk_batches=[b1, b2],
            biopsy_open={"b1": {MON.isoformat(): {"上午": True}},
                         "b2": {MON.isoformat(): {"上午": False}}}))
        got = (day_slots[MON.isoformat()]["上午"] or {}).get(BIOPSY)
        assert got and got[0] in ("C1", "C2"), got
