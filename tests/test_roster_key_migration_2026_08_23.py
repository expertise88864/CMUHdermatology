# -*- coding: utf-8 -*-
"""[批次RS-22 / 全審 2026-08-23(head 211749f)一個 P1 + 四個 P2]

P1-01 遷移改了【主鍵】(Clerk 梯次的 id),卻沒有跟著遷【外鍵】——
      `biopsy_grid.json` 是 `{batch_id: {日期: {時段: bool}}}`,而
      `build_day_input` 讀的正是 `grid[batch.id]`。升級之後那整梯的
      「切片室開放」就靜靜地不見了(JSON 正常、驗證正常、排班也跑得完)。
P2-01 改名之後無條件重算結算識別 → 一個【本來就對不上】的結算被洗成 fresh,
      而那正是閘門用來擋下「拿舊帳排下個月」的唯一證據。
P2-02 葉節點的排班語意:`"locked": []` 被當成沒鎖、切片格網的時段值不是
      bool 就等於沒開、週色拼錯就變成「不同色」而放行連值。
P2-03 `pending_grid_shift.json` 與 `pending_settle.json` 同型,卻還是寬鬆讀。
P2-04 「無從查證」只出現在求解報告裡 —— 要在分頁的警告面板就看得到。
"""
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.model import day_point                    # noqa: E402
from cmuh_common.roster.service import RosterService              # noqa: E402
from cmuh_common.roster.solve_rvs import (                        # noqa: E402
    SolveResult, rvs_input_fingerprint,
)
from cmuh_common.roster.storage import RosterStorage              # noqa: E402

OCT, NOV = "2026-10", "2026-11"
MON = "2026-10-05"          # 週一


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


def _legacy_batch(svc, members=("C1", "C2"), start=MON, n=1):
    """舊版程式寫得出來的形狀:沒有 id 的梯次。"""
    one = ('{"start_monday": "%s", "members": %s}'
           % (start, "[" + ", ".join(f'"{m}"' for m in members) + "]"))
    io.open(svc.storage._path("clerk_batches.json"), "w",
            encoding="utf-8").write(
        '{"batches": [' + ", ".join([one] * n) + ']}')


def _legacy_grid(svc, key="", iso="2026-10-06"):
    """舊版的切片格網:以那時候的 batch id(空字串)為鍵。"""
    io.open(svc.storage._path("biopsy_grid.json"), "w",
            encoding="utf-8").write(
        '{"grid": {"%s": {"%s": {"上午": true}}}}' % (key, iso))


def _biopsy_open(svc, ym=OCT):
    return svc.build_day_input(ym).biopsy_open


# ══ P1-01 改主鍵就要改外鍵 ═══════════════════════════════════════════════
class TestTheGridFollowsTheBatchId:
    def test_the_biopsy_grid_survives_the_id_migration(self, svc):
        """★反例本體★:升級前 `grid[""]` 讀得到,遷移把 batch id 換成
        `legacy-…` 之後 `grid.get("legacy-…")` 是 None —— 整梯的切片室開放
        就這樣從排班輸入裡消失,而且沒有任何錯誤訊息。"""
        _legacy_batch(svc)
        _legacy_grid(svc)
        before = _biopsy_open(svc)
        assert before, "前提不成立:遷移前就讀不到切片格網"
        svc.migrate_legacy_clerk_batch_ids()
        assert _biopsy_open(svc) == before, "★整梯的切片室開放不見了★"

    def test_every_batch_sharing_the_old_key_keeps_the_grid(self, svc):
        """★多梯共用舊鍵時複製給每一梯★:舊資料本來就是大家讀同一份,
        無從得知當初想分給誰 —— 挑一梯等於替使用者決定。"""
        _legacy_batch(svc, members=("C1",), n=1)
        io.open(svc.storage._path("clerk_batches.json"), "w",
                encoding="utf-8").write(
            '{"batches": [{"start_monday": "2026-10-05", "members": ["C1"]},'
            ' {"start_monday": "2026-10-05", "members": ["C2"]}]}')
        _legacy_grid(svc)
        svc.migrate_legacy_clerk_batch_ids()
        grid = svc.storage.load_biopsy_grid()
        ids = [b["id"] for b in svc.storage.load_clerk_batches()]
        assert len(ids) == 2 and all(grid.get(i) for i in ids), grid

    def test_the_old_key_is_cleaned_up(self, svc):
        _legacy_batch(svc)
        _legacy_grid(svc)
        svc.migrate_legacy_clerk_batch_ids()
        assert "" not in svc.storage.load_biopsy_grid()

    def test_an_unmigrated_batch_keeps_its_old_key(self, svc):
        """★遷移不完全時舊鍵還要留著★:撞名而留著沒有 id 的那一梯仍然要
        讀得到它的格網。"""
        one = '{"start_monday": "2026-10-05", "members": ["C1"]}'
        io.open(svc.storage._path("clerk_batches.json"), "w",
                encoding="utf-8").write(
            '{"batches": [' + ", ".join([one] * 3) + ']}')
        _legacy_grid(svc)
        svc.migrate_legacy_clerk_batch_ids()
        assert "" in svc.storage.load_biopsy_grid(), \
            "★還有梯次在用舊鍵,不可以清掉★"
        assert _biopsy_open(svc)

    def test_the_migration_is_idempotent(self, svc):
        _legacy_batch(svc)
        _legacy_grid(svc)
        svc.migrate_legacy_clerk_batch_ids()
        grid = svc.storage.load_biopsy_grid()
        assert svc.migrate_legacy_clerk_batch_ids() == []
        assert svc.storage.load_biopsy_grid() == grid

    def test_a_destination_holding_other_content_is_not_reused(self, svc):
        """★目的地已經有【別的內容】時,那個 id 不是空位★(外審 RS-22 R1-1):
        刪掉梯次時格網不會跟著刪(孤兒格網),巧合同名的鍵也可能存在 ——
        沿用它 = 這一梯換到別人的切片開放,而它自己原本那份接著被清理刪掉。
        ★判準看【排班輸入】★,不是看那個鍵還在不在(我第一版的測試只斷言
        目的地沒被蓋掉,那條在有缺陷的實作下照樣是綠的)。"""
        _legacy_batch(svc)
        io.open(svc.storage._path("biopsy_grid.json"), "w",
                encoding="utf-8").write(
            '{"grid": {"": {"2026-10-06": {"上午": true}},'
            ' "legacy-2026-10-05": {"2026-10-13": {"下午": true}}}}')
        before = _biopsy_open(svc)
        svc.migrate_legacy_clerk_batch_ids()
        assert _biopsy_open(svc) == before, "★這一梯換到別人的切片開放了★"
        assert svc.storage.load_biopsy_grid()["legacy-2026-10-05"] == {
            "2026-10-13": {"下午": True}}, "別人的格網也不可以被蓋掉"

    def test_a_resumed_migration_reuses_the_identical_copy(self, svc):
        """內容相同才可以沿用(上一次跑到一半留下的同一份)—— 重跑要冪等。"""
        _legacy_batch(svc)
        io.open(svc.storage._path("biopsy_grid.json"), "w",
                encoding="utf-8").write(
            '{"grid": {"": {"2026-10-06": {"上午": true}},'
            ' "legacy-2026-10-05": {"2026-10-06": {"上午": true}}}}')
        svc.migrate_legacy_clerk_batch_ids()
        assert [b["id"] for b in svc.storage.load_clerk_batches()] == [
            "legacy-2026-10-05"]

    def test_a_batch_without_a_grid_does_not_inherit_an_orphan(self, svc):
        """沒有格網的梯次更不可以憑空繼承一份孤兒格網。"""
        _legacy_batch(svc)
        io.open(svc.storage._path("biopsy_grid.json"), "w",
                encoding="utf-8").write(
            '{"grid": {"legacy-2026-10-05": {"2026-10-13": {"下午": true}}}}')
        svc.migrate_legacy_clerk_batch_ids()
        assert _biopsy_open(svc) == {}, "★憑空繼承了孤兒格網★"

    def test_a_batch_that_already_has_an_id_is_untouched(self, svc):
        svc.storage.save_clerk_batches([{"id": "b1", "start_monday": MON,
                                         "members": ["C1"]}])
        svc.storage.save_biopsy_grid({"b1": {"2026-10-06": {"上午": True}}})
        assert svc.migrate_legacy_clerk_batch_ids() == []
        assert svc.storage.load_biopsy_grid()["b1"]


# ══ P2-01 改名不可以把「本來就 stale」的洗成 fresh ═══════════════════════
class TestRenameDoesNotLaunderAStaleSettlement:
    def _accept(self, svc):
        ctx = svc.build_context("r", OCT, for_solve=True)
        a = {d: "A" for d in ctx.days}
        for b in ctx.blocks:
            for x in b.days:
                a[x] = a[b.days[0]]
        pts = {m.id: 0 for m in ctx.members}
        for d, mid in a.items():
            pts[mid] += day_point(d, ctx.holidays, ctx.params)
        svc.accept_solution("r", OCT, SolveResult(
            status="ok", scope="r", level_used=0, level_name="L0",
            assignments=a, points_by_person=pts,
            input_fingerprint=rvs_input_fingerprint(ctx),
            month_revision=svc.storage.load_month_snapshot(OCT)[1]))

    def test_a_pre_existing_mismatch_is_not_washed_away(self, svc):
        """★反例本體★:改名前那筆結算就已經對不上(人工合併/舊版狀態),
        改名之後無條件重算識別 = 把唯一的證據抹掉,下個月照樣用舊帳排。"""
        self._accept(svc)
        m = svc.storage.load_month(OCT)          # 繞過自動收斂,造出 stale
        m["r_duty"]["2026-10-06"] = {"person": "B", "locked": False,
                                     "source": "manual"}
        svc.storage.save_month(OCT, m)
        assert svc.stale_settlements("r", NOV)[0] == [OCT], "前提不成立"
        svc.rename_member("r", "A", "R1")
        stale, unknown = svc.stale_settlements("r", NOV)
        assert stale == [] and unknown == [OCT], \
            f"★本來對不上的結算被改名洗成 fresh★ {stale} {unknown}"

    def test_a_fresh_settlement_is_still_migrated(self, svc):
        """守衛不得因此讓正常的改名又回到「永久擋住下個月」。"""
        self._accept(svc)
        svc.rename_member("r", "A", "R1")
        assert svc.stale_settlements("r", NOV) == ([], [])


# ══ P2-02 葉節點的排班語意 ═══════════════════════════════════════════════
class TestTheLeafTypesThatDecideScheduling:
    def _month(self, svc, body):
        io.open(svc.storage._month_path(OCT), "w",
                encoding="utf-8").write(body)

    def test_a_falsey_locked_flag_is_refused(self, svc):
        """★鎖定的意思正是「不要動它」★:`[]` 被當成沒鎖,那一格就會被自動
        排班覆蓋掉。"""
        self._month(svc, '{"r_duty": {"2026-10-06": {"person": "A",'
                         ' "locked": []}}}')
        with pytest.raises(ValueError, match="2026-10.json"):
            svc.run_solve("r", OCT)

    def test_a_non_string_person_is_refused(self, svc):
        self._month(svc, '{"r_duty": {"2026-10-06": {"person": []}}}')
        with pytest.raises(ValueError, match="2026-10.json"):
            svc.run_solve("r", OCT)

    def test_a_falsey_finalized_flag_is_refused(self, svc):
        """定案＝唯讀。錯型別的 falsey 值會讓那份月檔又可以被整份覆寫。"""
        self._month(svc, '{"finalized": [], "r_duty": {}}')
        with pytest.raises(ValueError, match="2026-10.json"):
            svc.run_solve("r", OCT)

    def test_a_non_boolean_biopsy_session_is_refused(self, svc):
        """切片格網的時段值是 bool —— falsey 的錯型別＝整梯切片班消失。"""
        svc.storage.save_clerk_batches([{"id": "b1", "start_monday": MON,
                                         "members": ["C1"]}])
        io.open(svc.storage._path("biopsy_grid.json"), "w",
                encoding="utf-8").write(
            '{"grid": {"b1": {"2026-10-06": {"上午": []}}}}')
        with pytest.raises(ValueError, match="biopsy_grid.json"):
            svc.build_export(OCT)

    def test_a_misspelled_week_colour_is_refused(self, svc):
        """★色塊連週是比對兩週的字串相不相等★:拼錯的 "gren" 會被當成
        「不同色」,本該被禁止的連值兩個週末就放行了。"""
        io.open(svc.storage._path("week_colors.json"), "w",
                encoding="utf-8").write(
            '{"year": 2026, "weeks": {"2026-W41": "gren"}}')
        with pytest.raises(ValueError, match="week_colors.json"):
            svc.run_solve("r", OCT)

    def test_the_legal_colours_still_pass(self, svc):
        io.open(svc.storage._path("week_colors.json"), "w",
                encoding="utf-8").write(
            '{"year": 2026, "weeks": {"2026-W41": "pink",'
            ' "2026-W42": "green"}}')
        svc.run_solve("r", OCT)


# ══ P2-03 平移意圖也是權威輸入 ═══════════════════════════════════════════
class TestThePendingGridShiftIsAuthoritative:
    def _corrupt(self, svc):
        io.open(svc.storage._path("pending_grid_shift.json"), "w",
                encoding="utf-8").write("{壞掉的")

    def test_the_reconcile_refuses_instead_of_seeing_nothing(self, svc):
        """★與 pending_settle 同型★:梯次起始日已落地、格網還沒平移時,
        這份檔是唯一的線索 —— 讀不到卻回「沒有待辦」的話,收斂就不會跑。"""
        self._corrupt(svc)
        with pytest.raises(ValueError, match="pending_grid_shift.json"):
            svc.reconcile_pending_grid_shifts()

    def test_a_later_write_does_not_erase_it(self, svc):
        """壞檔沒擋住開程式 → 使用者接著移動另一梯 → 寬鬆讀取會把它換成
        「只有我這一筆」,舊義務就永久消失。"""
        self._corrupt(svc)
        raw = io.open(svc.storage._path("pending_grid_shift.json"),
                      encoding="utf-8").read()
        with pytest.raises(ValueError, match="pending_grid_shift.json"):
            svc.storage.mark_pending_grid_shift("b1", MON, "2026-10-12", {})
        assert io.open(svc.storage._path("pending_grid_shift.json"),
                       encoding="utf-8").read() == raw

    def test_a_wrong_root_is_not_read_as_no_debts(self, svc):
        io.open(svc.storage._path("pending_grid_shift.json"), "w",
                encoding="utf-8").write('{"pending": {"b1": "x"}}')
        with pytest.raises(ValueError, match="pending"):
            svc.storage.load_pending_grid_shifts_strict()

    def test_the_display_loader_stays_lenient(self, svc):
        self._corrupt(svc)
        assert svc.storage.load_pending_grid_shifts() == []

    def test_an_explicit_null_is_not_read_as_no_debts(self, svc):
        """★「沒有這個鍵」與「這個鍵是 null」是兩件事★(外審 RS-22 R1-2):
        前者是「還沒有任何待辦」,後者是讀不懂 —— 都當成空清單的話,下一次
        寫入就會把認不得的義務整份覆寫掉。"""
        io.open(svc.storage._path("pending_grid_shift.json"), "w",
                encoding="utf-8").write('{"pending": null}')
        with pytest.raises(ValueError, match="pending"):
            svc.storage.load_pending_grid_shifts_strict()

    def test_a_malformed_element_is_not_filtered_away(self, svc):
        io.open(svc.storage._path("pending_grid_shift.json"), "w",
                encoding="utf-8").write('{"pending": ["壞掉的一筆"]}')
        with pytest.raises(ValueError, match="不是物件"):
            svc.storage.load_pending_grid_shifts_strict()

    def test_the_settle_file_follows_the_same_rules(self, svc):
        """同一套規則,兩份意圖檔共用(修性質,不是修某一個實例)。"""
        io.open(svc.storage._path("pending_settle.json"), "w",
                encoding="utf-8").write('{"pending": ["壞掉的一筆"]}')
        with pytest.raises(ValueError, match="不是物件"):
            svc.storage.load_pending_settles_strict()

    def test_a_file_without_the_key_is_still_empty(self, svc):
        io.open(svc.storage._path("pending_grid_shift.json"), "w",
                encoding="utf-8").write('{}')
        assert svc.storage.load_pending_grid_shifts_strict() == []


# ══ P2-04 「查不出來」要在畫面上看得見 ═══════════════════════════════════
class TestTheCarryInWarningIsVisibleWithoutSolving:
    def test_an_unverifiable_month_shows_up_in_the_panel(self, svc):
        """★不必等到按下自動排班★:升級後的舊分錄沒有識別,而它會影響本月
        的公平目標 —— 分頁的警告面板就要講。"""
        ctx = svc.build_context("r", OCT, for_solve=True)
        a = {d: "A" for d in ctx.days}
        for b in ctx.blocks:
            for x in b.days:
                a[x] = a[b.days[0]]
        pts = {m.id: 0 for m in ctx.members}
        for d, mid in a.items():
            pts[mid] += day_point(d, ctx.holidays, ctx.params)
        svc.accept_solution("r", OCT, SolveResult(
            status="ok", scope="r", level_used=0, level_name="L0",
            assignments=a, points_by_person=pts,
            input_fingerprint=rvs_input_fingerprint(ctx),
            month_revision=svc.storage.load_month_snapshot(OCT)[1]))
        led = svc.storage.load_ledger()
        for e in led["history"]:
            e.pop("duty_digest", None)          # 升級當下的樣子
        svc.storage.save_ledger(led)
        svc.storage.save_month(NOV, {"r_duty": {}})
        msgs = [c.msg for c in svc.quick_validate("r", NOV)]
        assert any(OCT in m and "識別" in m for m in msgs), msgs

    def test_a_healthy_month_says_nothing_extra(self, svc):
        svc.storage.save_month(NOV, {"r_duty": {}})
        msgs = [c.msg for c in svc.quick_validate("r", NOV)]
        assert not any("重算帳本" in m for m in msgs), msgs
