# -*- coding: utf-8 -*-
"""[批次RS-20 / 全審 2026-08-22(head 507d3b8)三個 P1 + 兩個 P2]

同一條架構原則的四個切面:★只有 canonical schedule 是真相;凡是從它推導得出
的東西,要嘛在使用前重新推導,要嘛帶新鮮度識別★。

P1-01 `last_weekend` 是 Auto Accept 當下寫下的快取,手動換班不會更新它 ——
      而它是【下個月跨月連休的硬約束】。10/31 換給 B 之後,11/1 仍被固定給 A。
P1-02 手動換班不會更新帳本,而帳本結轉是【下個月公平目標的基準】;
      正常操作序列(Auto→Accept→換班→切下月→Auto)就會用到換班前的舊帳。
P1-03 定案留底只重建了 R/VS 的最終班表 —— PGY/Clerk 與週六切片仍只靠
      「當初求解」的報告,純手動排的月份因此完全沒有那兩段。
P2-01 `StrictSources` 擋得住「讀不到/壞 JSON」,擋不住【合法 JSON 但內容會被
      typed loader 靜靜濾掉】(壞日期鍵、非物件的梯次項目…)。
P2-02 GitSync 量髒污用的是「commit 開始時列的 pathspec」,那一刻還不存在的
      新月檔因此量不到 → 剛標記的未 commit 狀態被清掉。
"""
import io
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.model import day_point                    # noqa: E402
from cmuh_common.roster.report import (                           # noqa: E402
    build_final_biopsy_state_report, build_final_day_state_report,
)
from cmuh_common.roster.service import RosterService              # noqa: E402
from cmuh_common.roster.solve_rvs import (                        # noqa: E402
    SolveResult, apply_boundary_from_prev, rvs_input_fingerprint,
)
from cmuh_common.roster.storage import (                          # noqa: E402
    RosterStorage, last_weekend_of,
)

# 2026-10-31 = 週六、2026-11-01 = 週日 → 跨月連休鏈的典型月份
OCT, NOV = "2026-10", "2026-11"
SAT = date(2026, 10, 31)


@pytest.fixture()
def svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({
        "r_members": [{"id": "A", "name": "名A"}, {"id": "B", "name": "名B"}],
        "vs_members": [{"id": "D", "name": "D醫師"}],
        "points": {"weekday": 1, "weekend": 2, "national_holiday": 1},
    })
    st.save_month(OCT, {"r_duty": {}})
    return RosterService(st)


def _cover(svc, ym, person="A"):
    ctx = svc.build_context("r", ym)
    a = {d: person for d in ctx.days}
    for b in ctx.blocks:
        for x in b.days:
            a[x] = a[b.days[0]]
    return a


def _result_for(svc, ym, assignments):
    ctx = svc.build_context("r", ym, for_solve=True)
    pts = {m.id: 0 for m in ctx.members}
    for d, mid in assignments.items():
        if mid in pts:
            pts[mid] += day_point(d, ctx.holidays, ctx.params)
    return SolveResult(
        status="ok", scope="r", level_used=0, level_name="L0",
        assignments=dict(assignments), points_by_person=pts,
        last_weekend={"saturday": SAT.isoformat(),
                      "person": assignments.get(SAT, "")},
        input_fingerprint=rvs_input_fingerprint(ctx),
        month_revision=svc.storage.load_month_snapshot(ym)[1])


def _accept_oct(svc):
    svc.accept_solution("r", OCT, _result_for(svc, OCT, _cover(svc, OCT, "A")))


# ══ P1-01 跨月銜接由 canonical duty 推導 ═════════════════════════════════
class TestTheCrossMonthBoundaryFollowsTheRealRoster:
    def test_a_manual_swap_on_the_last_saturday_moves_the_boundary(self, svc):
        """★反例本體★:10/31 自動排給 A → 套用 → 手動換成 B。
        11 月的跨月連休鏈(11/1 週日)必須跟著變成 B —— 舊寫法讀的是
        `last_weekend` 快取(還寫著 A),於是 solver 把 11/1 硬性固定給 A,
        而 10 月最後那個週六實際上是 B 值的。"""
        _accept_oct(svc)
        assert svc.storage.load_month(OCT)["r_duty"][SAT.isoformat()][
            "person"] == "A"
        svc.set_cell("r", OCT, SAT, "B")
        ctx = svc.build_context("r", NOV)
        apply_boundary_from_prev(ctx)
        assert ctx.prev_last_weekend == (SAT, "B")
        assert ctx.boundary_fix.get(date(2026, 11, 1)) == "B", \
            f"★跨月連休仍固定給換班前的人★ {ctx.boundary_fix}"

    def test_the_stale_cache_is_ignored_even_when_it_disagrees(self, svc):
        """快取與實排不一致時,★實排贏★(快取只是診斷用的紀錄)。"""
        _accept_oct(svc)
        svc.set_cell("r", OCT, SAT, "B")
        m = svc.storage.load_month(OCT)
        assert (m.get("last_weekend") or {}).get("r", {}).get("person") == "A"
        assert last_weekend_of(m, "r", OCT) == (SAT, "B")

    def test_clearing_the_month_removes_the_boundary(self, svc):
        """清掉 10 月的班之後,11 月不可以還被固定給原來那個人。"""
        _accept_oct(svc)
        svc.clear_unlocked("r", OCT)
        ctx = svc.build_context("r", NOV)
        apply_boundary_from_prev(ctx)
        assert ctx.prev_last_weekend is None
        assert not ctx.boundary_fix

    def test_a_purely_manual_month_still_provides_the_boundary(self, svc):
        """★從來沒按過 Auto/Accept 的月份也要能銜接★:快取根本不存在,
        而 canonical duty 有 —— 舊寫法在這種月份完全沒有跨月限制。"""
        svc.set_cell("r", OCT, SAT, "B")
        m = svc.storage.load_month(OCT)
        assert "last_weekend" not in m or not m["last_weekend"]
        ctx = svc.build_context("r", NOV)
        apply_boundary_from_prev(ctx)
        assert ctx.prev_last_weekend == (SAT, "B")

    def test_the_last_saturday_is_the_calendar_one_not_the_last_filled(
            self, svc):
        """★判準是「這個月的最後一個週六」★:改成「往前找到有人的那一個」
        會把連休鏈接到兩週前的週末,在下個月固定給一個根本不相鄰的人。"""
        svc.set_cell("r", OCT, date(2026, 10, 24), "B")   # 前一個週六有人
        assert last_weekend_of(svc.storage.load_month(OCT), "r", OCT) is None

    def test_vs_follows_the_same_rule(self, svc):
        svc.set_cell("vs", OCT, SAT, "D")
        assert last_weekend_of(svc.storage.load_month(OCT), "vs", OCT) \
            == (SAT, "D")


# ══ P1-02 帳本結轉的新鮮度 ═══════════════════════════════════════════════
class TestTheCarryInLedgerMatchesTheRealRoster:
    def test_a_manual_swap_updates_the_ledger(self, svc):
        """★反例本體★:Auto→Accept→手動換班→(沒按重算帳本)→切下月→Auto。
        全部都是正常 UI 操作,而舊寫法讓 11 月用換班前的結轉算公平目標。"""
        _accept_oct(svc)
        before = dict(svc.storage.load_ledger()["r"])
        svc.set_cell("r", OCT, date(2026, 10, 6), "B")     # 週二換給 B
        after = dict(svc.storage.load_ledger()["r"])
        assert after != before, "★換了班,帳本卻還停在換班前★"
        assert after.get("B", 0) > before.get("B", 0)

    def test_the_gate_refuses_when_the_ledger_could_not_follow(self, svc,
                                                               monkeypatch):
        """收斂失敗(例如該月早於帳本保留期)時不擋這次編輯,但★下個月的
        求解要擋下來並說清楚是哪一個月★ —— 不可以靜靜地用舊帳排班。"""
        _accept_oct(svc)
        monkeypatch.setattr(
            RosterService, "_settle_ledger_only_locked",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("收斂壞了")))
        svc.set_cell("r", OCT, date(2026, 10, 6), "B")
        stale, _unknown = svc.stale_settlements("r", NOV)
        assert stale == [OCT]
        with pytest.raises(ValueError, match="2026-10"):
            svc.run_solve("r", NOV)

    def test_a_fresh_ledger_does_not_block_anything(self, svc):
        _accept_oct(svc)
        svc.set_cell("r", OCT, date(2026, 10, 6), "B")
        assert svc.stale_settlements("r", NOV) == ([], [])

    def test_the_current_month_is_not_checked(self, svc):
        """本月的舊分錄求解時本來就會被回滾(RS-9)—— 擋它是誤報。"""
        _accept_oct(svc)
        led = svc.storage.load_ledger()
        for e in led["history"]:
            e.pop("duty_digest", None)
            e["duty_digest"] = "對不上的識別"
        svc.storage.save_ledger(led)
        stale, _ = svc.stale_settlements("r", OCT)
        assert stale == []

    def test_a_legacy_entry_without_a_digest_warns_instead_of_blocking(
            self, svc):
        """★升級當下每一筆舊分錄都沒有識別★ —— 一律擋的話,使用者升級後
        連一個月都排不了。查不出來要講出來,但不擋。"""
        _accept_oct(svc)
        led = svc.storage.load_ledger()
        for e in led["history"]:
            e.pop("duty_digest", None)
        svc.storage.save_ledger(led)
        stale, unknown = svc.stale_settlements("r", NOV)
        assert stale == [] and unknown == [OCT]
        res = svc.run_solve("r", NOV)
        assert any("沒有可比對的識別" in s for s in res.diagnosis), res.diagnosis

    def test_the_settlement_records_which_roster_it_came_from(self, svc):
        _accept_oct(svc)
        led = svc.storage.load_ledger()
        entry = [e for e in led["history"] if e["month"] == OCT][0]
        assert entry.get("duty_digest"), "★結算沒有記下它是照哪一份班表算的★"


# ══ P1-03 留底文件要有 PGY/Clerk 與切片的最終狀態 ════════════════════════
class TestTheArchiveCoversEveryRoster:
    def _month_with_day_slots(self, svc):
        m = svc.storage.load_month(OCT)
        m["day_slots"] = {"2026-10-05": {"上午": {"101": ["P1"],
                                                  "照光": ["P2"]}}}
        m["saturday_biopsy"] = {SAT.isoformat(): {"person": "A",
                                                  "reason": "手動"}}
        svc.storage.save_month(OCT, m)

    def test_a_purely_manual_day_roster_still_reaches_the_pdf(self, svc):
        """★整月純手動排的話根本沒有 day_report★ —— 舊寫法的留底文件因此
        完全沒有日排班,而封面寫著「定案當下的排班快照」。"""
        self._month_with_day_slots(svc)
        secs = dict(svc.build_finalize_pdf_sections(OCT))
        assert "PGY・Clerk 最終日排班" in secs
        assert "P1" in secs["PGY・Clerk 最終日排班"]
        assert "照光:P2" in secs["PGY・Clerk 最終日排班"]

    def test_the_saturday_biopsy_reaches_the_pdf_without_a_report(self, svc):
        self._month_with_day_slots(svc)
        secs = dict(svc.build_finalize_pdf_sections(OCT))
        assert "週六切片最終名單" in secs
        assert "名A" in secs["週六切片最終名單"]

    def test_an_empty_month_says_so_instead_of_going_missing(self, svc):
        secs = dict(svc.build_finalize_pdf_sections(OCT))
        assert "沒有任何日排班紀錄" in secs["PGY・Clerk 最終日排班"]
        assert "沒有排週六切片" in secs["週六切片最終名單"]

    def test_a_manual_edit_after_accept_shows_the_new_person(self, svc):
        """★最終狀態要跟著 day_slots 走★(不是跟著當初的報告)。"""
        m = svc.storage.load_month(OCT)
        m["day_slots"] = {"2026-10-05": {"上午": {"101": ["舊的人"]}}}
        m["day_report"] = "當初的日排班報告"
        svc.storage.save_month(OCT, m)
        svc.set_day_slot(OCT, date(2026, 10, 5), "上午", "101", ["新的人"])
        body = dict(svc.build_finalize_pdf_sections(OCT))["PGY・Clerk 最終日排班"]
        assert "新的人" in body and "舊的人" not in body

    def test_the_renderers_skip_other_months_keys(self):
        """非當月鍵不列(與結算/匯出同一道過濾)。"""
        out = build_final_day_state_report(
            year=2026, month=10,
            day_slots={"2026-09-30": {"上午": {"101": ["X"]}}})
        assert "X" not in out
        out2 = build_final_biopsy_state_report(
            year=2026, month=10, names={},
            saturday_biopsy={"2026-09-26": {"person": "X"}})
        assert "X" not in out2


# ══ P2-01 內容也要嚴格(合法 JSON 但會被靜靜濾掉的形狀)═══════════════════
class TestTheContentIsCheckedNotJustTheJson:
    def _write(self, svc, name, text):
        io.open(svc.storage._path(name), "w", encoding="utf-8").write(text)

    def test_a_bad_holiday_key_is_refused_on_the_authoritative_path(self, svc):
        """★反例本體★:合法 JSON,`_strict_snapshot` 放行,而 typed loader
        只記 warning 就跳過那一天 —— 整年國定假日可以只剩幾天,solver 完全
        看不出來。"""
        self._write(svc, "holiday_duty.json",
                    '{"r": {"2026-10-10": "A", "十月十日": "A"}, "vs": {}}')
        with pytest.raises(ValueError, match="holiday_duty.json"):
            svc.run_solve("r", OCT)

    def test_the_display_path_still_tolerates_it(self, svc):
        """顯示不受影響(讀不到/壞內容就顯示少一點,不該讓視窗打不開)。"""
        self._write(svc, "holiday_duty.json",
                    '{"r": {"2026-10-10": "A", "十月十日": "A"}, "vs": {}}')
        svc.build_context("r", OCT)          # 不得拋
        assert svc.storage.holidays_set() == {date(2026, 10, 10)}

    def test_a_batch_that_is_not_an_object_is_refused(self, svc):
        self._write(svc, "clerk_batches.json", '{"batches": ["壞掉的"]}')
        with pytest.raises(ValueError, match="clerk_batches.json"):
            svc.build_export(OCT)

    def test_a_member_without_an_id_is_refused(self, svc):
        self._write(svc, "config.json", '{"r_members": [{"name": "沒有代號"}]}')
        with pytest.raises(ValueError, match="config.json"):
            svc.run_solve("r", OCT)

    def test_a_healthy_file_passes(self, svc):
        svc.run_solve("r", OCT)              # 正常資料不得被新守衛擋下


# ══ 外審 R1 的六條修正 ═══════════════════════════════════════════════════
class TestAMonthThatWasNeverSettledIsAlsoStale:
    """★純手動排的月份根本還沒有分錄★(外審 RS-20 R1-1):識別比對只走
    history,查不到它 —— 收斂若在讀檔/建 context 就失敗,那個月的結轉會整個
    消失,而下個月照樣排得出來。意圖是這種「從未結算過」的唯一線索。"""

    def test_a_failed_first_settlement_still_blocks_the_next_month(
            self, svc, monkeypatch):
        monkeypatch.setattr(
            RosterService, "_settle_ledger_only_locked",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("結算壞了")))
        svc.set_cell("r", OCT, date(2026, 10, 6), "A")   # 純手動的第一格
        assert not [e for e in svc.storage.load_ledger().get("history") or []
                    if e.get("month") == OCT], "前提不成立:已經有分錄了"
        assert svc.stale_settlements("r", NOV)[0] == [OCT], \
            "★從未結算過的月份完全看不到★"
        with pytest.raises(ValueError, match="2026-10"):
            svc.run_solve("r", NOV)

    def test_the_intent_is_recorded_before_anything_can_fail(self, svc,
                                                             monkeypatch):
        monkeypatch.setattr(
            RosterService, "_settle_ledger_only_locked",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("結算壞了")))
        svc.set_cell("r", OCT, date(2026, 10, 6), "A")
        kinds = [(x["ym"], svc.storage.pending_kind(x))
                 for x in svc.storage.load_pending_settles()]
        assert (OCT, "ledger") in kinds, kinds

    def test_a_biopsy_only_obligation_does_not_block_the_next_month(
            self, svc):
        """★切片義務不影響點數★ —— 拿它擋求解是誤報。"""
        svc.storage.mark_pending_settle("r", OCT, kind="biopsy")
        assert svc.stale_settlements("r", NOV)[0] == []

    def test_a_successful_edit_leaves_no_blocking_intent(self, svc):
        svc.set_cell("r", OCT, date(2026, 10, 6), "A")
        assert svc.stale_settlements("r", NOV) == ([], [])


class TestTheSolveBaselineExcludesTheFuture:
    """★「本月之前」不可以包含未來★(外審 RS-20 R1-2):先手動編輯 12 月
    一格(現在會立刻結算),再回頭自動排 11 月 —— 舊寫法只回滾 11 月,
    12 月的差額就被當成 11 月的 carry-in。"""

    def test_a_future_months_settlement_is_not_carried_in(self, svc):
        svc.storage.save_month("2026-12", {"r_duty": {}})
        svc.set_cell("r", "2026-12", date(2026, 12, 1), "A")   # 12 月手動一格
        assert [e for e in svc.storage.load_ledger()["history"]
                if e["month"] == "2026-12"], "前提不成立:12 月沒有結算分錄"
        base = svc.solver_ledger("r", NOV)
        assert base == {}, f"★未來月份的差額被當成 11 月的結轉★ {base}"

    def test_an_earlier_month_is_still_carried_in(self, svc):
        """守衛不得把【真正該結轉的】那些月份也回滾掉。"""
        _accept_oct(svc)
        base = svc.solver_ledger("r", NOV)
        assert base and any(abs(v) > 0 for v in base.values()), base


class TestTheLedgerOnlyRecoveryStaysNarrow:
    """★只欠帳本就只補帳本★(外審 RS-20 R1-3):走完整的 resettle 會連切片
    一起重排並改寫月檔 —— 別的月份/次數已經變了的話,可能意外改派切片。"""

    def test_reconcile_does_not_touch_the_biopsy_state(self, svc,
                                                       monkeypatch):
        _accept_oct(svc)
        svc.storage.mark_pending_settle("r", OCT, kind="ledger")
        before = dict(svc.storage.load_month(OCT).get("saturday_biopsy") or {})
        called: list = []
        monkeypatch.setattr(svc, "recompute_saturday_biopsy",
                            lambda *a, **k: called.append(1) or (None, [], {}, ""))
        svc.reconcile_pending_settles()
        assert not called, "★只欠帳本卻去重排了切片★"
        assert dict(svc.storage.load_month(OCT).get("saturday_biopsy") or {}) \
            == before
        assert not svc.storage.load_pending_settles()


class TestTheReadModifyWritePathsValidateToo:
    def test_a_settings_edit_does_not_silently_delete_a_bad_entry(self, svc):
        """★反例本體★(外審 RS-20 R1-4):壞掉的項目被 typed loader 濾掉,
        接著整份寫回去 = ★永久刪除★,而使用者只是在設定頁改了另一筆資料。"""
        io.open(svc.storage._path("clerk_batches.json"), "w",
                encoding="utf-8").write(
            '{"batches": [{"id": "b1", "start_monday": "2026-10-05",'
            ' "members": ["C1"]}, "壞掉的一筆"]}')
        with pytest.raises(ValueError, match="clerk_batches.json"):
            svc.update_clerk_batches(lambda bs: bs)
        raw = io.open(svc.storage._path("clerk_batches.json"),
                      encoding="utf-8").read()
        assert "壞掉的一筆" in raw, "★那一筆被靜靜刪掉了★"

    def test_a_healthy_settings_edit_still_works(self, svc):
        svc.update_clerk_batches(
            lambda bs: bs.append({"id": "b9", "start_monday": "2026-10-05",
                                  "members": ["C9"]}) or bs)
        assert [b["id"] for b in svc.storage.load_clerk_batches()] == ["b9"]


class TestAFalseyWrongContainerDoesNotSlipThrough:
    """★`or {}` 會把【錯型別但 falsey】的值正規化成合法的空值★
    (外審 RS-20 R1-5):`{"r": []}` 於是變成一張空的假日表,而每一道守衛
    都放行 —— 整年國定假日就這樣消失。"""

    def test_a_list_where_the_holiday_table_should_be_is_refused(self, svc):
        io.open(svc.storage._path("holiday_duty.json"), "w",
                encoding="utf-8").write('{"r": [], "vs": {}}')
        with pytest.raises(ValueError, match="holiday_duty.json"):
            svc.run_solve("r", OCT)

    def test_a_dict_where_the_members_should_be_is_refused(self, svc):
        io.open(svc.storage._path("clerk_batches.json"), "w",
                encoding="utf-8").write(
            '{"batches": [{"id": "b1", "start_monday": "2026-10-05",'
            ' "members": {}}]}')
        with pytest.raises(ValueError, match="clerk_batches.json"):
            svc.build_export(OCT)

    def test_an_absent_members_field_is_still_fine(self, svc):
        """守衛不得把【合法的缺欄位】也擋掉(members 可以不寫)。"""
        io.open(svc.storage._path("clerk_batches.json"), "w",
                encoding="utf-8").write(
            '{"batches": [{"id": "b1", "start_monday": "2026-10-05"}]}')
        svc.build_export(OCT)
