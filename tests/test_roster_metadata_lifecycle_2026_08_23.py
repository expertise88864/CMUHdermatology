# -*- coding: utf-8 -*-
"""[批次RS-21 / 全審 2026-08-22(head ef41ebe)兩個 P1 + 四個 P2]

RS-20 建立了「衍生資料要帶新鮮度識別」的架構,這一批處理它自己的生命週期:
★freshness / recovery metadata 也是一等資料 —— 改名、升級、當機交易都要
一起維護它★。

P1-01 改代號會合法地換掉班表裡的 person → `duty_digest` 對不上 → 未來月份
      被永久擋下(而且已定案月無法重算,使用者無路可走)。
P1-02 舊版正式支援「沒有 id 的 Clerk 梯次」,新 validator 卻判它非法 ——
      升級之後日排班與匯出對一份舊版自己寫出來的檔整批失敗。
P2-01 帳本意圖在【月檔落地之後】才記:中間被砍就留下一個沒有分錄也沒有
      意圖的月份 —— 兩道判準都看不到它。
P2-02 `pending_settle.json` 已經是閘門的權威輸入,卻仍用寬鬆載入。
P2-03 語意驗證只做了一半(week_colors/ledger/biopsy/月檔的日期鍵)。
P2-05 升級前就已經 stale 的帳本不會自己好。
"""
import io
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.model import day_point                    # noqa: E402
from cmuh_common.roster.service import RosterService              # noqa: E402
from cmuh_common.roster.solve_rvs import (                        # noqa: E402
    SolveResult, rvs_input_fingerprint,
)
from cmuh_common.roster.storage import (                          # noqa: E402
    FinalizedMonthError, RosterStorage,
)

OCT, NOV = "2026-10", "2026-11"


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
        input_fingerprint=rvs_input_fingerprint(ctx),
        month_revision=svc.storage.load_month_snapshot(ym)[1])


def _accept_oct(svc, person="A"):
    svc.accept_solution("r", OCT, _result_for(svc, OCT, _cover(svc, OCT,
                                                               person)))


# ══ P1-01 改名要一併遷移新鮮度識別 ═══════════════════════════════════════
class TestRenameCarriesTheFreshnessIdentity:
    def test_a_rename_does_not_make_every_future_month_unschedulable(
            self, svc):
        """★反例本體★:改代號是完全正常的設定操作,它會【合法地】把班表裡的
        person 全部換掉 —— 識別於是對不上,而帳本與班表其實完全一致。
        舊寫法會從此擋住所有後續月份的自動排班。"""
        _accept_oct(svc)
        assert svc.stale_settlements("r", NOV) == ([], [])
        svc.rename_member("r", "A", "R1")
        assert svc.stale_settlements("r", NOV) == ([], []), \
            "★改個代號就把後面每個月都鎖死了★"
        svc.storage.save_month(NOV, {"r_duty": {}})
        svc.run_solve("r", NOV)              # 不得被擋

    def test_the_migrated_digest_still_detects_a_real_edit(self, svc):
        """★守衛不可以因為遷移而失效★:改名之後真的手改一格,還是要抓得到。"""
        _accept_oct(svc)
        svc.rename_member("r", "A", "R1")
        led = svc.storage.load_ledger()
        for e in led["history"]:
            e["duty_digest"] = "被改名之後又被人動過的班表"
        svc.storage.save_ledger(led)
        assert svc.stale_settlements("r", NOV)[0] == [OCT]

    def test_vs_follows_the_same_rule(self, svc):
        svc.set_cell("vs", OCT, date(2026, 10, 6), "D")
        assert svc.stale_settlements("vs", NOV) == ([], [])
        svc.rename_member("vs", "D", "V9")
        assert svc.stale_settlements("vs", NOV) == ([], [])

    def test_a_finalized_month_is_migrated_too(self, svc):
        """★最糟的組合★:改名連已定案的月份都會 force 改,而定案月不能重算
        帳本 —— 識別若沒跟著遷移,使用者連「去那個月按重算」都做不到。"""
        _accept_oct(svc)
        svc.finalize(OCT, True)
        svc.rename_member("r", "A", "R1")
        assert svc.stale_settlements("r", NOV) == ([], [])

    def test_a_history_month_with_no_file_becomes_unverifiable(self, svc):
        """月檔已經不在了 → 識別無從重算 → 退回「無從查證」,
        ★不可以留一個已知是錯的識別★。"""
        _accept_oct(svc)
        os.remove(svc.storage._month_path(OCT))
        svc.rename_member("r", "A", "R1")
        entry = [e for e in svc.storage.load_ledger()["history"]
                 if e["month"] == OCT][0]
        assert "duty_digest" not in entry
        assert svc.stale_settlements("r", NOV) == ([], [OCT])


# ══ P1-02 舊版沒有 id 的 Clerk 梯次 ══════════════════════════════════════
class TestLegacyClerkBatchesStillWork:
    def _legacy(self, svc):
        io.open(svc.storage._path("clerk_batches.json"), "w",
                encoding="utf-8").write(
            '{"batches": [{"start_monday": "2026-10-05",'
            ' "members": ["1", "2", "3"]}]}')

    def test_a_legacy_batch_without_an_id_does_not_break_the_day_solve(
            self, svc):
        """★反例本體★:這是舊版程式自己寫得出來的合法檔(`from_dict` 與
        `clerk_batch_key()` 都明文支援 id 缺失)—— 升級之後不可以整批失敗。"""
        self._legacy(svc)
        inp = svc.build_day_input(OCT)
        assert [b.members for b in inp.clerk_batches] == [["1", "2", "3"]]

    def test_a_legacy_batch_does_not_break_the_export(self, svc):
        self._legacy(svc)
        svc.build_export(OCT)

    def test_two_batches_sharing_an_id_are_still_refused(self, svc):
        """★要擋的是這個★:同一個 id 指到兩梯 → 切片格網互相覆蓋。"""
        io.open(svc.storage._path("clerk_batches.json"), "w",
                encoding="utf-8").write(
            '{"batches": [{"id": "b1", "start_monday": "2026-10-05",'
            ' "members": ["1"]}, {"id": "b1", "start_monday": "2026-10-19",'
            ' "members": ["2"]}]}')
        with pytest.raises(ValueError, match="重複"):
            svc.build_export(OCT)

    def test_the_migration_gives_them_a_deterministic_id(self, svc):
        """★不可以用隨機 UUID★:兩台各自跑遷移會產生兩個不同的 id,
        git 合併之後就變成兩梯。"""
        self._legacy(svc)
        assert svc.migrate_legacy_clerk_batch_ids() == ["legacy-2026-10-05"]
        first = svc.storage.load_clerk_batches()
        self._legacy(svc)                     # 另一台從同一份舊檔開始
        svc.migrate_legacy_clerk_batch_ids()
        assert svc.storage.load_clerk_batches() == first

    def test_two_legacy_batches_on_the_same_monday_get_distinct_ids(
            self, svc):
        """★同一個起始日可以有兩梯★(repo 明文保留這個舊案例)——只用起始日
        當 id 的話兩梯會拿到同一個,而那份檔接著就會被唯一性驗證擋下:
        遷移自己造出一份排不了班的資料(外審 RS-21 R1-2)。"""
        io.open(svc.storage._path("clerk_batches.json"), "w",
                encoding="utf-8").write(
            '{"batches": [{"start_monday": "2026-10-05", "members": ["1"]},'
            ' {"start_monday": "2026-10-05", "members": ["2"]}]}')
        svc.migrate_legacy_clerk_batch_ids()
        ids = [b["id"] for b in svc.storage.load_clerk_batches()]
        assert len(set(ids)) == 2, f"★兩梯拿到同一個 id★ {ids}"
        svc.build_export(OCT)                 # 遷移後的檔仍要通過權威驗證

    def test_an_unresolvable_collision_is_left_unmigrated(self, svc):
        """分不出來就不要動:寧可留著沒有 id(仍然排得了班),也不要寫出一份
        會被唯一性驗證擋下的檔。"""
        one = '{"start_monday": "2026-10-05", "members": ["1"]}'
        io.open(svc.storage._path("clerk_batches.json"), "w",
                encoding="utf-8").write(
            '{"batches": [' + ", ".join([one] * 3) + ']}')
        svc.migrate_legacy_clerk_batch_ids()
        ids = [str(b.get("id") or "") for b in svc.storage.load_clerk_batches()]
        assert len([x for x in ids if x]) == len(set(x for x in ids if x))
        svc.build_export(OCT)                 # ★檔案仍然可用★

    def test_the_migration_is_idempotent(self, svc):
        self._legacy(svc)
        svc.migrate_legacy_clerk_batch_ids()
        assert svc.migrate_legacy_clerk_batch_ids() == []

    def test_the_migration_leaves_real_ids_alone(self, svc):
        svc.storage.save_clerk_batches([{"id": "b1",
                                         "start_monday": "2026-10-05",
                                         "members": ["1"]}])
        assert svc.migrate_legacy_clerk_batch_ids() == []
        assert svc.storage.load_clerk_batches()[0]["id"] == "b1"


# ══ P2-01 意圖要包住月檔的寫入 ═══════════════════════════════════════════
class TestTheLedgerIntentCoversTheSourceWrite:
    def test_a_crash_after_the_month_lands_still_leaves_a_trace(self, svc,
                                                                monkeypatch):
        """★反例本體★:純手動排的第一格 —— 月檔落地之後、收斂之前被砍。
        沒有分錄可比識別、也沒有意圖,兩道判準都看不到那個月。"""
        monkeypatch.setattr(
            RosterService, "_settle_ledger_only_locked",
            lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt("被砍")))
        with pytest.raises(KeyboardInterrupt):
            svc.set_cell("r", OCT, date(2026, 10, 6), "A")
        assert svc.storage.load_month(OCT)["r_duty"], "前提不成立:月檔沒落地"
        kinds = [(x["ym"], svc.storage.pending_kind(x))
                 for x in svc.storage.load_pending_settles()]
        assert (OCT, "ledger") in kinds, f"★月檔改了卻沒有任何線索★ {kinds}"

    def test_a_refused_edit_leaves_no_ledger_debt(self, svc):
        """witness=本月:月檔根本沒改成功時,那筆意圖是誤報。"""
        svc.finalize(OCT, True)
        with pytest.raises(FinalizedMonthError):
            svc.set_cell("r", OCT, date(2026, 10, 6), "A")
        assert not svc.storage.load_pending_settles()

    def test_a_successful_edit_clears_it(self, svc):
        svc.set_cell("r", OCT, date(2026, 10, 6), "A")
        assert not svc.storage.load_pending_settles()

    def test_a_failed_settle_keeps_the_debt(self, svc, monkeypatch):
        monkeypatch.setattr(
            RosterService, "_settle_ledger_only_locked",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("結算壞了")))
        svc.set_cell("r", OCT, date(2026, 10, 6), "A")     # 編輯不被擋
        assert svc.storage.load_month(OCT)["r_duty"]
        assert [(x["ym"], svc.storage.pending_kind(x))
                for x in svc.storage.load_pending_settles()] == [(OCT,
                                                                  "ledger")]


# ══ P2-02 未完成的結算是閘門的權威輸入 ═══════════════════════════════════
class TestThePendingFileIsAuthoritative:
    def _corrupt(self, svc):
        io.open(svc.storage._path("pending_settle.json"), "w",
                encoding="utf-8").write("{壞掉的")

    def test_the_solve_gate_refuses_when_it_cannot_be_read(self, svc):
        """★反例本體★:讀不到被正規化成「沒有任何未完成的事」—— 閘門在最
        需要它的時候放行(這正是 RS-19 修過的形狀,只是換一個檔)。"""
        _accept_oct(svc)
        self._corrupt(svc)
        svc.storage.save_month(NOV, {"r_duty": {}})
        with pytest.raises(ValueError, match="pending_settle.json"):
            svc.run_solve("r", NOV)

    def test_the_finalize_gate_refuses_too(self, svc):
        """★空月份才量得到這一條★:有班表的月份會先 `mark_pending_settle`,
        而那次寫入會把壞檔備份後覆寫掉 —— 閘門讀到的就是一份新的好檔,
        勝負分不出來(第一版就是這樣)。"""
        self._corrupt(svc)
        with pytest.raises(ValueError, match="pending_settle.json"):
            svc.finalize(OCT, True)

    def test_the_display_loader_stays_lenient(self, svc):
        self._corrupt(svc)
        assert svc.storage.load_pending_settles() == []

    def test_a_wrong_root_is_not_read_as_no_debts(self, svc):
        """★形狀錯了也不可以正規化成「沒有未完成的事」★:壞 JSON 由嚴格快照
        擋下,而【合法 JSON、但 pending 不是清單】是另一條路。"""
        io.open(svc.storage._path("pending_settle.json"), "w",
                encoding="utf-8").write('{"pending": {"r": "2026-10"}}')
        with pytest.raises(ValueError, match="pending"):
            svc.storage.load_pending_settles_strict()

    def test_a_later_write_does_not_erase_an_unreadable_file(self, svc):
        """★反例本體★(外審 RS-21 R1-3):壞檔沒擋住開程式(刻意的),使用者
        接著手動改一格 —— `mark_pending_settle` 若用寬鬆讀取,就會把那份壞檔
        換成「只有我這一筆」;等這次操作成功、意圖被清掉,★先前所有未完成的
        義務就永久消失★,而之後的閘門看到一份健康的空檔案照樣放行。"""
        self._corrupt(svc)
        raw = io.open(svc.storage._path("pending_settle.json"),
                      encoding="utf-8").read()
        with pytest.raises(ValueError, match="pending_settle.json"):
            svc.set_cell("r", OCT, date(2026, 10, 6), "A")
        assert io.open(svc.storage._path("pending_settle.json"),
                       encoding="utf-8").read() == raw,             "★壞檔被換成只有這一筆的新檔★"

    def test_a_healthy_file_is_read_the_same_way(self, svc):
        svc.storage.mark_pending_settle("r", OCT, kind="biopsy")
        assert (svc.storage.load_pending_settles_strict()
                == svc.storage.load_pending_settles())


# ══ P2-03 語意驗證補齊 ═══════════════════════════════════════════════════
class TestTheRestOfTheSchemaIsCheckedToo:
    def _write(self, svc, name, text):
        io.open(svc.storage._path(name), "w", encoding="utf-8").write(text)

    def test_a_bad_leave_date_is_refused_on_the_authoritative_path(self, svc):
        """★這一條最痛★:`leaves` 少一天 = 請假的人被排上班,而它只是一個
        會被 warning 跳過的壞日期字串。"""
        io.open(svc.storage._month_path(OCT), "w", encoding="utf-8").write(
            '{"leaves": {"r": {"A": ["2026-10-10", "十月十二"]}}}')
        with pytest.raises(ValueError, match="2026-10.json"):
            svc.run_solve("r", OCT)

    def test_a_bad_closure_date_is_refused(self, svc):
        """`grid_overrides` 少一天 = 已經停診的診間又被排人。"""
        io.open(svc.storage._month_path(OCT), "w", encoding="utf-8").write(
            '{"grid_overrides": {"不是日期": {"上午": {"closed_rooms": ["101"]}}}}')
        with pytest.raises(ValueError, match="2026-10.json"):
            svc.build_export(OCT)

    def test_a_wrong_typed_week_colors_is_refused(self, svc):
        """色塊連週是 CP-SAT 的硬限制。"""
        self._write(svc, "week_colors.json", '{"weeks": []}')
        with pytest.raises(ValueError, match="week_colors.json"):
            svc.run_solve("r", OCT)

    def test_a_non_numeric_ledger_balance_is_refused(self, svc):
        self._write(svc, "ledger.json",
                    '{"r": {"A": "五點"}, "vs": {}, "history": []}')
        with pytest.raises(ValueError, match="ledger.json"):
            svc.run_solve("r", OCT)

    def test_a_non_numeric_biopsy_count_is_refused(self, svc):
        self._write(svc, "biopsy.json", '{"counts": {"A": "三次"}}')
        with pytest.raises(ValueError, match="biopsy.json"):
            svc.recompute_saturday_biopsy(OCT)

    def test_a_falsey_day_slot_cell_is_refused(self, svc):
        """★日期對、值卻是 []★(外審 RS-21 R1-4):下游一律 `or {}` —— 那一天的
        日排班就這樣從正式文件裡消失,而它是合法 JSON、日期也沒問題。"""
        io.open(svc.storage._month_path(OCT), "w", encoding="utf-8").write(
            '{"day_slots": {"2026-10-05": []}}')
        with pytest.raises(ValueError, match="2026-10.json"):
            svc.build_export(OCT)

    def test_a_falsey_biopsy_cell_is_refused(self, svc):
        io.open(svc.storage._month_path(OCT), "w", encoding="utf-8").write(
            '{"saturday_biopsy": {"2026-10-03": []}}')
        with pytest.raises(ValueError, match="2026-10.json"):
            svc.build_export(OCT)

    def test_a_wrong_typed_closure_room_list_is_refused(self, svc):
        io.open(svc.storage._month_path(OCT), "w", encoding="utf-8").write(
            '{"grid_overrides": {"2026-10-05": {"上午": '
            '{"closed_rooms": "101"}}}}')
        with pytest.raises(ValueError, match="2026-10.json"):
            svc.build_export(OCT)

    def test_a_falsey_day_lock_is_refused(self, svc):
        """★鎖定的意思正是「不要動它」★(外審 RS-21 R2-2):錯型別的 falsey 值
        會被靜靜當成「沒鎖」,那些格子就會被自動排班覆蓋掉。"""
        io.open(svc.storage._month_path(OCT), "w", encoding="utf-8").write(
            '{"day_locks": {"2026-10-05": {"上午": []}}}')
        with pytest.raises(ValueError, match="2026-10.json"):
            svc.build_export(OCT)

    def test_a_falsey_biopsy_person_is_refused(self, svc):
        io.open(svc.storage._month_path(OCT), "w", encoding="utf-8").write(
            '{"saturday_biopsy": {"2026-10-03": {"person": []}}}')
        with pytest.raises(ValueError, match="2026-10.json"):
            svc.build_export(OCT)

    def test_a_healthy_month_still_passes(self, svc):
        io.open(svc.storage._month_path(OCT), "w", encoding="utf-8").write(
            '{"day_slots": {"2026-10-05": {"上午": {"101": ["P1"]}}},'
            ' "day_locks": {"2026-10-05": {"上午": true}},'
            ' "saturday_biopsy": {"2026-10-03": {"person": "A"}},'
            ' "biopsy_override": {"2026-10-10": ""},'
            ' "grid_overrides": {"2026-10-05": {"上午": '
            '{"closed_rooms": ["101"]}}}}')
        svc.build_export(OCT)

    def test_the_display_path_still_opens(self, svc):
        """守衛不得讓「只是想看一眼」的人打不開視窗。"""
        io.open(svc.storage._month_path(OCT), "w", encoding="utf-8").write(
            '{"leaves": {"r": {"A": ["2026-10-10", "十月十二"]}}}')
        svc.build_context("r", OCT)
        svc.quick_validate("r", OCT)


# ══ P2-05 舊分錄:只認證證明得了的,★絕不改寫歷史★ ══════════════════════
class TestTheLegacyLedgerIsCertifiedNotRewritten:
    def _drop_digests(self, svc):
        led = svc.storage.load_ledger()
        for e in led["history"]:
            e.pop("duty_digest", None)        # 升級當下每一筆都是這樣
        svc.storage.save_ledger(led)

    def test_a_consistent_entry_gets_certified_without_touching_the_balance(
            self, svc):
        _accept_oct(svc)
        before = dict(svc.storage.load_ledger()["r"])
        self._drop_digests(svc)
        assert svc.stale_settlements("r", NOV) == ([], [OCT])
        assert svc.migrate_legacy_ledger_digests() == [("r", OCT)]
        assert dict(svc.storage.load_ledger()["r"]) == before, \
            "★認證不可以順手改餘額★"
        assert svc.stale_settlements("r", NOV) == ([], [])

    def test_a_finalized_month_is_certified_too(self, svc):
        """不做的話那些月份永遠停在「無從查證」(定案月不能重算帳本)。"""
        _accept_oct(svc)
        svc.finalize(OCT, True)
        self._drop_digests(svc)
        assert svc.migrate_legacy_ledger_digests() == [("r", OCT)]
        assert svc.stale_settlements("r", NOV) == ([], [])
        assert svc.storage.load_month(OCT)["finalized"] is True

    def test_a_changed_point_rule_is_never_rewritten(self, svc):
        """★反例本體★(外審 RS-21 R1-1):點數規則是可以獨立修改的設定 ——
        拿【現在的】規則去重算一個舊月份,算出來的差額本來就與當初不同,
        而新識別還會把改寫後的結果認證成 fresh。分不出「舊版沒重算」與
        「規則變過」時,★不代為改寫★。"""
        _accept_oct(svc)
        self._drop_digests(svc)
        before = dict(svc.storage.load_ledger()["r"])
        cfg = svc.storage.load_config()
        cfg["points"] = {"weekday": 5, "weekend": 9, "national_holiday": 5}
        svc.storage.save_config(cfg)
        assert svc.migrate_legacy_ledger_digests() == []
        assert dict(svc.storage.load_ledger()["r"]) == before, \
            "★用今天的點數規則改寫了歷史帳本★"
        assert svc.stale_settlements("r", NOV) == ([], [OCT]), \
            "分不出來就要留在「無從查證」"

    def test_a_new_member_does_not_get_a_debt_for_a_month_before_joining(
            self, svc):
        """同一條規則的另一面:名單也是可以改的 —— 新人不可以被記上一筆
        他還沒到職那個月的負債。"""
        _accept_oct(svc)
        self._drop_digests(svc)
        before = dict(svc.storage.load_ledger()["r"])
        svc.change_members_and_sync_ledger(
            "r", lambda cfg: cfg["r_members"].append({"id": "C",
                                                      "name": "新人"}))
        svc.migrate_legacy_ledger_digests()
        after = dict(svc.storage.load_ledger()["r"])
        assert after.get("C", 0.0) == 0.0, f"★新人被記上舊月份的負債★ {after}"
        for mid, v in before.items():
            assert after[mid] == v, "★舊分錄被今天的名單改寫了★"

    def test_a_really_stale_entry_stays_unverifiable(self, svc):
        """舊版換班沒重算的那一種也留在「無從查證」——★它與「規則變過」
        在資料上分不出來★,而代為改寫的風險比留著大。使用者到那個月按
        「重算帳本」是一個明確的決定。"""
        _accept_oct(svc)
        m = svc.storage.load_month(OCT)
        m["r_duty"]["2026-10-06"] = {"person": "B", "locked": False,
                                     "source": "manual"}
        svc.storage.save_month(OCT, m)        # 繞過現在的自動收斂
        self._drop_digests(svc)
        before = dict(svc.storage.load_ledger()["r"])
        assert svc.migrate_legacy_ledger_digests() == []
        assert dict(svc.storage.load_ledger()["r"]) == before
        assert svc.stale_settlements("r", NOV) == ([], [OCT])
        # 出口:使用者自己按「重算帳本」→ 收斂 + 補上識別
        svc.resettle_from_duty("r", OCT)
        assert svc.stale_settlements("r", NOV) == ([], [])

    def test_an_entry_that_changed_after_the_proof_is_not_stamped(
            self, svc, monkeypatch):
        """★證明與蓋章之間不可以換內容★(外審 RS-21 R2-1):背景同步把那一筆
        換成他機的版本之後才蓋章,等於「用舊證據認證新內容」。"""
        _accept_oct(svc)
        self._drop_digests(svc)
        real = svc._prove_settlement_matches

        def _hook(scope, ym, led):
            out = real(scope, ym, led)
            cur = svc.storage.load_ledger()       # 他機同步進來的另一版分錄
            for e in cur["history"]:
                e["deltas"] = {k: float(v) + 1.0
                               for k, v in (e.get("deltas") or {}).items()}
            svc.storage.save_ledger(cur)
            return out

        monkeypatch.setattr(svc, "_prove_settlement_matches", _hook)
        svc.migrate_legacy_ledger_digests()
        entry = [e for e in svc.storage.load_ledger()["history"]
                 if e["month"] == OCT][0]
        assert "duty_digest" not in entry, "★用舊證據認證了新內容★"

    def test_a_month_with_no_file_is_left_unverifiable(self, svc):
        _accept_oct(svc)
        self._drop_digests(svc)
        os.remove(svc.storage._month_path(OCT))
        assert svc.migrate_legacy_ledger_digests() == []
        assert svc.stale_settlements("r", NOV) == ([], [OCT])

    def test_it_is_idempotent(self, svc):
        _accept_oct(svc)
        assert svc.migrate_legacy_ledger_digests() == []


def test_both_migrations_run_at_startup():
    """★接上去了才存在★:遷移沒有被開程式流程呼叫的話,它只是一個函式。"""
    import ast
    src = io.open(os.path.join(os.path.dirname(__file__), "..", "src",
                               "scheduler.py"), encoding="utf-8").read()
    names = {n.func.attr for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and hasattr(n.func, "attr")}
    assert "migrate_legacy_clerk_batch_ids" in names
    assert "migrate_legacy_ledger_digests" in names
