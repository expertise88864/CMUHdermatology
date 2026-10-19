# -*- coding: utf-8 -*-
"""[2026-10-19 使用者] R 一線：每月 1–2 個週末、年度週末比例、年度週期重置。

使用者定案:
  * 排班週期 8 月～隔年 7 月;8 月起 R/VS 點數帳本、週六切片、VS 週末計數、
    R 年度週末全部重新計算(銜接規則照常跨月)。
  * 每人每月 1–2 個週末(週六所在月份計;春節等「不計入年度週末」不算;整月
    沒有可值週末的人不套用「至少 1 個」)。
  * 9–11 班與每月 1–2 個週末排不出來時【不自動放寬】,由使用者確認。
  * 年度週末比例 R1:R2:R3 = 3.6:3.3:3.1(可設定),嚴格版:只在一樣好的排法間挑。
"""
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster import cycle  # noqa: E402
from cmuh_common.roster import weekend_share as ws  # noqa: E402
from cmuh_common.roster.ledger import settle_month  # noqa: E402
from cmuh_common.roster.model import Member, SolveContext, build_duty_blocks  # noqa: E402
from cmuh_common.roster.saturday_biopsy import settle_biopsy  # noqa: E402
from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.storage import RosterStorage  # noqa: E402


# ─── 年度週期 ─────────────────────────────────────────────────────────────
class TestCycle:
    @pytest.mark.parametrize("ym, start", [
        ("2026-08", "2026-08"), ("2026-12", "2026-08"), ("2027-01", "2026-08"),
        ("2027-07", "2026-08"), ("2027-08", "2027-08")])
    def test_cycle_start(self, ym, start):
        assert cycle.cycle_start(ym) == start

    def test_cycle_months_and_full_year(self):
        assert cycle.cycle_months("2026-10") == ["2026-08", "2026-09", "2026-10"]
        full = cycle.full_cycle_months("2027-02")
        assert full[0] == "2026-08" and full[-1] == "2027-07" and len(full) == 12
        assert cycle.cycle_label("2027-02") == "2026/8–2027/7"

    def test_ledger_in_cycle_is_the_sum_of_this_cycles_entries(self):
        led: dict = {}
        settle_month(led, "r", "2026-07", {"A": 10, "B": 2})     # 上個週期
        settle_month(led, "r", "2026-08", {"A": 4, "B": 6})
        assert led["r"] == {"A": 3.0, "B": -3.0}                 # 累計檔照舊
        assert cycle.ledger_in_cycle(led, "r", "2026-09") == {"A": -1.0, "B": 1.0}
        assert cycle.ledger_in_cycle(led, "r", "2026-07") == {"A": 4.0, "B": -4.0}

    @staticmethod
    def _full_history(last="2026-08", scope="r"):
        """24 個月滿額的 history(=舊版可能已修剪過),最後一個月是 last。"""
        y, m = int(last[:4]), int(last[5:7])
        out = []
        for _ in range(24):
            out.append({"month": f"{y:04d}-{m:02d}", "scope": scope,
                        "deltas": {"A": 0.0, "B": 0.0}})
            m -= 1
            if m == 0:
                y, m = y - 1, 12
        out[0]["deltas"] = {"A": 1.0, "B": -1.0}        # 2026-08 本週期
        return out

    def test_a_book_trimmed_by_the_old_version_still_resets(self):
        """[外審 PR #9 P2-3] 舊版修剪 history 時沒留下被丟掉的總和:累計值裡有一塊
        history 已經找不到的舊餘額。history 已滿 24 個月 → 被丟掉的必是最舊、必在
        本週期之前 → 本週期只加總本週期的分錄,那塊不會帶進來。"""
        led = {"r": {"A": 7.0, "B": -7.0},             # +6/-6 屬於早已被修剪的月份
               "history": self._full_history()}
        assert cycle.ledger_in_cycle(led, "r", "2026-09") == {"A": 1.0, "B": -1.0}

    def test_a_manual_correction_in_a_short_history_is_kept(self):
        """直接改 ledger.json 是文件記載的應急手段;history 不足 24 個月 = 不可能被
        修剪過 → 史外餘額是使用者的修正,屬於本週期(不可以默默丟掉)。"""
        led = {"r": {"A": 7.0, "B": -7.0},
               "history": [{"month": "2026-08", "scope": "r",
                            "deltas": {"A": 1.0, "B": -1.0}}]}
        assert cycle.ledger_in_cycle(led, "r", "2026-09") == {"A": 7.0, "B": -7.0}

    def test_an_untracked_legacy_trim_stops_the_solve_instead_of_guessing(self, tmp_path):
        """[外審 PR #9 第 2 輪] 舊版修剪過(history 滿、沒有 trimmed 記錄)→ 分不出舊值
        與修正 → 求解路徑明講並停下。判定要在回滾之前做(回滾 8 月後只剩 23 個月)。"""
        st = RosterStorage(str(tmp_path))
        st.save_config({"r_members": [{"id": "A"}, {"id": "B"}], "vs_members": []})
        st.save_ledger({"r": {"A": 7.0, "B": -7.0}, "history": self._full_history()})
        svc = RosterService(st)
        with pytest.raises(cycle.UntrackedTrimError, match="trimmed"):
            svc.build_context("r", "2026-08", for_solve=True)
        # 顯示不擋(退回只加總本週期分錄)
        assert svc.build_context("r", "2026-09").ledger == {"A": 1.0, "B": -1.0}

    def test_settling_into_an_untracked_full_book_keeps_it_untracked(self, tmp_path):
        """[外審 PR #9 第 3 輪] 舊版修剪過的帳本再結算一個新月份(→ 再修剪一次),
        不可以因此被標成「已追蹤」—— 否則更早被丟掉的舊值會被當成本年度修正。"""
        led = {"r": {"A": 7.0, "B": -7.0}, "history": self._full_history()}
        settle_month(led, "r", "2026-09", {"A": 1, "B": 1})
        assert "trimmed" not in led
        with pytest.raises(cycle.UntrackedTrimError):
            cycle.ledger_in_cycle(led, "r", "2026-10", strict=True)
        book = {"counts": {"C": 10, "K": 2},
                "history": [{"month": f"{2024 + (i // 12):04d}-{i % 12 + 1:02d}",
                             "assign": {}} for i in range(7, 31)]}
        settle_biopsy(book, "2026-09", {"2026-09-05": "K"})
        assert "trimmed" not in book
        with pytest.raises(cycle.UntrackedTrimError):
            cycle.biopsy_in_cycle(book, "2026-10", strict=True)

    def test_a_manual_correction_survives_a_full_tracked_history(self):
        """[外審 PR #9 第 2 輪] history 剛好滿 24 個月不代表修正要丟掉:有 trimmed
        記錄(＝修剪都有記總和)時,史外餘額就是使用者的修正,保留。"""
        led = {"r": {"A": 7.0, "B": -7.0}, "history": self._full_history(),
               "trimmed": {}}
        assert cycle.ledger_in_cycle(led, "r", "2026-09", strict=True) == {
            "A": 7.0, "B": -7.0}

    def test_new_trimming_records_totals_and_they_stay_out_of_the_cycle(self):
        from cmuh_common.roster import ledger as L
        led: dict = {}
        months = [f"{2024 + (m - 1) // 12:04d}-{(m - 1) % 12 + 1:02d}"
                  for m in range(1, 30)]                    # 2024-01 … 2026-05
        for ym in months:
            settle_month(led, "r", ym, {"A": 2, "B": 0})     # 每月 A +1
        settle_month(led, "r", "2026-08", {"A": 0, "B": 2})  # 本週期 A -1
        assert len({e["month"] for e in led["history"]}) == L.HISTORY_KEEP_MONTHS
        assert led["trimmed"]["r"]["A"] > 0, "被修剪的分錄要留下總和"
        assert cycle.ledger_in_cycle(led, "r", "2026-09", strict=True) == {
            "A": -1.0, "B": 1.0}
        led["r"]["A"] += 0.5                                 # 之後的手動修正 → 保留
        assert cycle.ledger_in_cycle(led, "r", "2026-09", strict=True)["A"] == -0.5

    def test_biopsy_in_cycle(self):
        book: dict = {}
        settle_biopsy(book, "2026-07", {"2026-07-04": "C", "2026-07-11": "C"})
        settle_biopsy(book, "2026-08", {"2026-08-01": "K"})
        assert book["counts"] == {"C": 2, "K": 1}
        assert cycle.biopsy_in_cycle(book, "2026-09") == {"C": 0, "K": 1}
        hist = [{"month": f"{2024 + (i // 12):04d}-{i % 12 + 1:02d}", "assign": {}}
                for i in range(7, 31)]                     # 2024-08 … 2026-07,共 24 個月
        hist[-1] = {"month": "2026-08", "assign": {"2026-08-01": "K"}}
        legacy = {"counts": {"C": 10, "K": 2}, "history": hist}   # 舊版修剪過的檔
        assert cycle.biopsy_in_cycle(legacy, "2026-09") == {"C": 0, "K": 1}


# ─── 週末次數 ─────────────────────────────────────────────────────────────
class TestWeekendUnits:
    def test_counted_by_saturday_month_orphan_sunday_excluded(self):
        # 2026/11/1 是週日(其週六在 10 月) → 不算 11 月;11 月有 4 個週六
        blocks = build_duty_blocks(2026, 11, set())
        assert len(ws.counted_blocks(blocks, set())) == 4

    def test_a_long_holiday_block_counts_once_and_exempt_blocks_not_at_all(self):
        # 2026/9/25(五) 中秋 → 五六日同一塊,只算 1 次
        hol = {date(2026, 9, 25), date(2026, 9, 28)}
        blocks = build_duty_blocks(2026, 9, hol)
        assert len(ws.counted_blocks(blocks, set())) == 4
        assert len(ws.counted_blocks(blocks, {date(2026, 9, 25)})) == 3

    def test_month_units_from_real_aug_to_oct(self):
        """與使用者 8–10 月實際班表的手數一致:R1 4、R2 5、R3 5(10/31 算 10 月)。"""
        sats = {  # 週六 → 值班者(取自 115-08/09/10 月班表)
            "2026-08": {1: "C", 8: "K", 15: "C", 22: "Z", 29: "K"},
            "2026-09": {5: "Z", 12: "K", 19: "C", 26: "Z"},
            "2026-10": {3: "K", 10: "C", 17: "K", 24: "Z", 31: "C"},
        }
        total: dict = {}
        hol = {date(2026, 9, 25), date(2026, 9, 28)}
        for ym, by_day in sats.items():
            y, m = int(ym[:4]), int(ym[5:7])
            duty = {date(y, m, d): p for d, p in by_day.items()}
            for mid, n in ws.month_units(y, m, duty, hol, set()).items():
                total[mid] = total.get(mid, 0) + n
        assert total == {"Z": 4, "C": 5, "K": 5}

    def test_an_exemption_in_the_next_month_excludes_the_saturday_too(self):
        """[外審 PR #9 P2-5] 10/31(六) 的塊被裁成只有它自己;11/1(日) 標「不計入」
        時,那整個週末都不算 —— 不可以只看裁切後的 b.days。"""
        blocks = build_duty_blocks(2026, 10, set())
        exempt = {date(2026, 11, 1)}
        counted = ws.counted_blocks(blocks, exempt, set())
        assert all(b.saturday != date(2026, 10, 31) for b in counted)
        assert len(counted) == 4
        duty = {date(2026, 10, 31): "C"}
        assert ws.month_units(2026, 10, duty, set(), exempt) == {}

    def test_holidays_owned_by_the_earlier_weekend_do_not_exempt_the_next(self):
        """[外審 PR #9 第 2 輪] 2/8–12 連續假日已被 2/6 那個週末往後鏈走;2/8 標
        「不計入」只排除 2/6 那塊,2/13–14 是另一個週末,照常計入。"""
        hol = {date(2027, 2, d) for d in range(8, 13)}
        assert ws.weekend_chain(date(2027, 2, 13), hol) == [date(2027, 2, 13),
                                                            date(2027, 2, 14)]
        blocks = build_duty_blocks(2027, 2, hol)
        counted = ws.counted_blocks(blocks, {date(2027, 2, 8)}, hol)
        assert {b.saturday for b in counted} == {date(2027, 2, 13), date(2027, 2, 20),
                                                  date(2027, 2, 27)}

    def test_an_exemption_reached_through_a_chained_holiday(self):
        hol = {date(2026, 11, 2)}                          # 11/2(一) 國定假日
        blocks = build_duty_blocks(2026, 10, hol)
        assert any(b.saturday == date(2026, 10, 31)
                   for b in ws.counted_blocks(blocks, set(), hol))
        assert not any(b.saturday == date(2026, 10, 31)
                       for b in ws.counted_blocks(blocks, {date(2026, 11, 2)}, hol))

    def test_weights_and_targets(self):
        members = [Member("Z", "", "R1"), Member("C", "", "R2"),
                   Member("K", "", "R3"), Member("Y", "", "R4")]
        w = ws.member_weights(members, ws.level_weights({}))
        assert w["Z"] == 3.6 and w["C"] == 3.3 and w["K"] == 3.1
        assert w["Y"] == pytest.approx((3.6 + 3.3 + 3.1) / 3)   # 不偏任何一方
        three = {k: w[k] for k in ("Z", "C", "K")}
        t = ws.cumulative_targets({"Z": 4, "C": 5, "K": 5}, three, 4)
        assert sum(t.values()) == pytest.approx(18)
        assert t["Z"] > t["C"] > t["K"]
        # 53 個週末的整年目標約 19.1 / 17.5 / 16.4(使用者確認的數字)
        yt = ws.year_targets(53, three)
        assert [round(yt[k], 1) for k in ("Z", "C", "K")] == [19.1, 17.5, 16.4]

    def test_config_weights_are_used_and_bad_values_ignored(self):
        lw = ws.level_weights({"weekend_share": {"R1": 4, "r3": "2.5", "R2": "x",
                                                 "R4": -1}})
        assert lw["R1"] == 4.0 and lw["R3"] == 2.5 and lw["R2"] == 3.3
        assert "R4" not in lw


# ─── 求解器 ───────────────────────────────────────────────────────────────
ortools = pytest.importorskip("ortools", reason="ortools 未安裝（自動排班引擎）")
from cmuh_common.roster.calendar_colors import week_colors_for_year  # noqa: E402
from cmuh_common.roster.solve_rvs import solve_duty  # noqa: E402

W = {"Z": 3.6, "C": 3.3, "K": 3.1}


def _three():
    return [Member("Z", "賴", "R1", fixed_weekday=2),
            Member("C", "林", "R2", fixed_weekday=3),
            Member("K", "陳", "R3", fixed_weekday=1)]


def _nov(**kw):
    kw.setdefault("members", _three())
    kw.setdefault("week_colors", week_colors_for_year(2026))
    kw.setdefault("prev_last_weekend", (date(2026, 10, 31), "C"))
    return SolveContext(scope="r", year=2026, month=11, **kw).prepare()


def _weekends(ctx, res):
    blocks = ws.counted_blocks(ctx.blocks, ctx.weekend_exempt)
    out = {m.id: 0 for m in ctx.members}
    for b in blocks:
        out[res.assignments[b.saturday]] += 1
    return out


class TestMonthlyWeekendRange:
    def test_every_resident_gets_one_or_two_weekends(self):
        ctx = _nov()
        res = solve_duty(ctx)
        assert res.status == "ok"
        assert all(1 <= n <= 2 for n in _weekends(ctx, res).values())

    def test_someone_on_leave_for_every_weekend_is_exempt_from_the_minimum(self):
        leave = {date(2026, 11, d) for d in (7, 8, 14, 15, 21, 22, 28, 29)}
        ctx = _nov(leaves={"K": leave})
        res = solve_duty(ctx, allow_relax=("duty_range",))
        assert res.status == "ok", (res.status, res.diagnosis)
        wk = _weekends(ctx, res)
        assert wk["K"] == 0 and wk["Z"] == 2 and wk["C"] == 2
        assert any(c.rule_id == "weekend_range" and "不套用" in c.msg
                   for c in res.prechecks)

    def test_a_sunday_directive_for_someone_else_takes_the_whole_weekend(self):
        """[外審 PR #9 P2-1] 週日鎖給別人 → 同段綁同一人,週六也跟著走。K 每個週末
        都被週日的鎖定帶走 → K 沒有可值的週末 → 不套用「至少 1 個」。"""
        locks = {date(2026, 11, 8): "Z", date(2026, 11, 15): "C",
                 date(2026, 11, 22): "Z", date(2026, 11, 29): "C"}
        ctx = _nov(locks=locks)
        res = solve_duty(ctx, allow_relax=("duty_range",))
        assert res.status == "ok", (res.status, res.diagnosis)
        assert _weekends(ctx, res)["K"] == 0
        assert any(c.rule_id == "weekend_range" and "陳" in c.msg and "不套用" in c.msg
                   for c in res.prechecks)
        assert "weekend_range" not in res.relaxed

    def test_an_exempt_spring_festival_block_is_not_counted(self):
        """春節週末塊由年度指定給 R1,不佔他「每月最多 2 個」的名額。"""
        hol = {date(2027, 2, d) for d in (5, 8, 9, 10)}          # 示意:春節連假
        exempt = set(hol)
        annual = {d: "Z" for d in (date(2027, 2, 5), date(2027, 2, 6),
                                   date(2027, 2, 7))}
        ctx = SolveContext(scope="r", year=2027, month=2, members=_three(),
                           holidays=hol, annual_holiday=annual,
                           week_colors=week_colors_for_year(2027),
                           weekend_exempt=exempt).prepare()
        counted = ws.counted_blocks(ctx.blocks, ctx.weekend_exempt)
        assert all(date(2027, 2, 6) not in b.days for b in counted)
        res = solve_duty(ctx)
        assert res.status in ("ok", "need_confirm_relax"), res.diagnosis
        if res.status == "ok":
            assert all(1 <= n <= 2 for n in _weekends(ctx, res).values())

    def test_three_weekends_for_one_person_needs_confirmation(self):
        """5 個週末、2 人可值 → 有人要 3 個 → 不自動放寬。"""
        two = [Member("Z", "賴", "R1"), Member("C", "林", "R2")]
        ctx = SolveContext(scope="r", year=2026, month=8, members=two,
                           week_colors=week_colors_for_year(2026)).prepare()
        res = solve_duty(ctx)
        assert res.status == "need_confirm_relax"
        assert "weekend_range" in res.relax_proposal and not res.assignments


class TestYearlyShareSecondPass:
    def test_the_resident_furthest_behind_gets_the_extra_weekend(self):
        """真實 8–10 月累計 4/5/5 → 11 月的第二個週末給 R1。"""
        ctx = _nov(weekend_prior={"Z": 4, "C": 5, "K": 5}, weekend_weights=W)
        res = solve_duty(ctx)
        assert res.status == "ok"
        assert _weekends(ctx, res) == {"Z": 2, "C": 1, "K": 1}

    def test_the_share_term_actually_steers_the_choice(self):
        """R3 遠落後時改給 R3 —— 證明是比例在決定,不是巧合。"""
        ctx = _nov(weekend_prior={"Z": 6, "C": 6, "K": 2}, weekend_weights=W)
        res = solve_duty(ctx)
        assert res.status == "ok"
        assert _weekends(ctx, res)["K"] == 2

    def test_the_second_pass_never_costs_points(self):
        """嚴格版:有沒有年度比例,點數分配一樣公平(第一段目標值被固定住)。"""
        base = solve_duty(_nov())
        ctx = _nov(weekend_prior={"Z": 6, "C": 6, "K": 2}, weekend_weights=W)
        steered = solve_duty(ctx)

        def dev(r, c):
            return sum(abs(r.points_by_person[m.id] - r.targets[m.id])
                       for m in c.members)
        assert dev(steered, ctx) <= dev(base, _nov()) + 1e-9
        assert all(9 <= n <= 11 for n in steered.duty_counts.values())


# ─── 服務層:歷史月份、假日表、設定、VS/切片的週期重置 ─────────────────────
def _svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({
        "r_members": [{"id": "Z", "name": "賴", "level": "R1"},
                      {"id": "C", "name": "林", "level": "R2"},
                      {"id": "K", "name": "陳", "level": "R3"}],
        "vs_members": [{"id": "D", "name": "D"}, {"id": "L", "name": "L"}],
        "weekend_share": {"R1": 3.6, "R2": 3.3, "R3": 3.1}})
    return RosterService(st)


def _put_saturdays(svc, ym, by_day):
    y, m = int(ym[:4]), int(ym[5:7])

    def mut(month):
        duty = month.setdefault("r_duty", {})
        for d, p in by_day.items():
            for dd in (date(y, m, d), date(y, m, d).fromordinal(
                    date(y, m, d).toordinal() + 1)):
                if dd.month == m:
                    duty[dd.isoformat()] = {"person": p, "locked": False,
                                            "source": "manual"}
    svc.update_month(ym, mut)


class TestServiceWiring:
    def test_prior_counts_come_from_the_stored_months_of_this_cycle(self, tmp_path):
        svc = _svc(tmp_path)
        _put_saturdays(svc, "2026-07", {4: "Z", 11: "Z"})          # 上個週期:不算
        _put_saturdays(svc, "2026-08", {1: "C", 8: "K", 15: "C", 22: "Z", 29: "K"})
        _put_saturdays(svc, "2026-10", {31: "C"})
        ctx = svc.build_context("r", "2026-11")
        assert ctx.weekend_prior == {"C": 3, "K": 2, "Z": 1}
        assert ctx.weekend_weights == W
        # 權威路徑(求解)讀到同一批
        res = svc.run_solve("r", "2026-11")
        assert res.status in ("ok", "need_confirm_relax")

    def test_holiday_exemption_round_trips_and_feeds_the_context(self, tmp_path):
        svc = _svc(tmp_path)
        d = date(2027, 2, 5)

        def mut(tbl):
            tbl["r"][d] = "Z"
            tbl["weekend_exempt"] = {d}
        svc.update_holiday_duty(mut)
        assert svc.storage.load_holiday_duty()["weekend_exempt"] == {d}
        assert svc.build_context("r", "2027-02").weekend_exempt == {d}

    def test_a_malformed_exemption_list_fails_closed_on_the_solve_path(self, tmp_path):
        import json
        svc = _svc(tmp_path)
        with open(svc.storage._path("holiday_duty.json"), "w", encoding="utf-8") as f:
            json.dump({"r": {}, "vs": {}, "weekend_exempt": "2027-02-05"}, f)
        with pytest.raises(ValueError, match="weekend_exempt"):
            svc.run_solve("r", "2027-02")

    def test_vs_weekend_counts_do_not_carry_from_july_into_august(self, tmp_path):
        svc = _svc(tmp_path)

        def mut(month):
            month.setdefault("vs_duty", {})["2026-07-25"] = {
                "person": "D", "locked": False, "source": "manual"}
        svc.update_month("2026-07", mut)
        svc.update_month("2026-08", lambda m: None)
        svc.update_month("2026-09", lambda m: None)

        def mut8(month):
            month.setdefault("vs_duty", {})["2026-08-29"] = {
                "person": "D", "locked": False, "source": "manual"}
        svc.update_month("2026-08", mut8)
        assert svc.build_context("vs", "2026-08").prev_weekend_counts == {}
        assert svc.build_context("vs", "2026-09").prev_weekend_counts == {"D": 1}

    def test_biopsy_allocation_starts_each_cycle_from_zero(self, tmp_path):
        """[外審 PR #9 P2-2/3] 排切片用的次數也只看本週期(上個週期的分錄不算)。"""
        svc = _svc(tmp_path)
        june = {f"2026-06-{d:02d}": "C" for d in (6, 7, 13, 14, 20, 27)}
        june.update({"2026-06-21": "K", "2026-06-28": "K"})
        svc.storage.save_biopsy({"counts": {"C": 10, "K": 2}, "history": [
            {"month": "2026-07",
             "assign": {f"2026-07-{d:02d}": "C" for d in (4, 11, 18, 25)}},
            {"month": "2026-06", "assign": june}]})
        _put_saturdays(svc, "2026-08", {1: "Z", 8: "Z", 15: "Z", 22: "Z", 29: "Z"})
        _assign, _notes, _pair, after, _names = svc._biopsy_compute(
            "2026-08", {d: p for d, p in
                        ws.duty_map(svc.storage.load_month("2026-08"), "r").items()})
        assert after["C"] + after["K"] == 5             # 只有本月 5 個週六
        assert abs(after["C"] - after["K"]) <= 1, after

    def test_renaming_moves_the_trimmed_totals_too(self, tmp_path):
        """[外審 PR #9 第 1 輪 P2-4] 被修剪的舊分錄總和要跟著代號一起搬。"""
        svc = _svc(tmp_path)
        svc.storage.save_ledger({
            "r": {"Z": 6.0, "C": -6.0},
            "history": [{"month": "2026-08", "scope": "r", "deltas": {"Z": 1.0, "C": -1.0}}],
            "trimmed": {"r": {"Z": 5.0, "C": -5.0}}})
        svc.rename_member("r", "Z", "Z2")
        led = svc.storage.load_ledger()
        assert led["trimmed"]["r"] == {"Z2": 5.0, "C": -5.0}
        assert cycle.ledger_in_cycle(led, "r", "2026-09", strict=True)["Z2"] == 1.0

    def test_renaming_keeps_the_in_cycle_balance(self, tmp_path):
        """[外審 PR #9 P2-4] 改代號時累計值與 history 分錄一起搬 → 本週期餘額跟著人走
        (上個週期的部分也一起搬、一起被扣掉)。"""
        svc = _svc(tmp_path)
        svc.storage.save_ledger({
            "r": {"Z": 5.0, "C": -5.0},
            "history": [
                {"month": "2026-07", "scope": "r", "deltas": {"Z": 4.0, "C": -4.0}},
                {"month": "2026-08", "scope": "r", "deltas": {"Z": 1.0, "C": -1.0}}]})
        before = cycle.ledger_in_cycle(svc.storage.load_ledger(), "r", "2026-09")["Z"]
        svc.rename_member("r", "Z", "Z2")
        after = cycle.ledger_in_cycle(svc.storage.load_ledger(), "r", "2026-09")
        assert before == after["Z2"] == 1.0
        assert "Z" not in after

    def test_the_year_summary(self, tmp_path):
        svc = _svc(tmp_path)
        _put_saturdays(svc, "2026-08", {1: "C", 8: "K", 15: "C", 22: "Z", 29: "K"})
        ctx = svc.build_context("r", "2026-09")
        rows = svc.weekend_year_summary(ctx, {date(2026, 9, 5): "Z"})
        by = {r["id"]: r for r in rows}
        assert by["Z"]["done"] == 2 and by["C"]["done"] == 2 and by["K"]["done"] == 2
        assert by["Z"]["year"] > by["C"]["year"] > by["K"]["year"]
        assert sum(r["year"] for r in rows) == pytest.approx(53, abs=0.2)
