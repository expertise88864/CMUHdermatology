# -*- coding: utf-8 -*-
"""[批次RS-16 / 全審次輪 P2 群] 衍生物的義務、身分與可信來源。

五條(全部是「看起來成功、實際不一致」的形狀):
  P2-01 會被寫回去的東西,來源一律嚴格快照(壞檔不得變成一份空的漂亮基準)
  P2-02 意圖代表「衍生物還沒重建成功」,不是「第二次寫檔還沒做完」
  P2-03 週六切片的手動指定/「本週不切片」是 R 求解的輸入(週五連動要認它)
  P2-04 門診模板刪除以【身分】為準,不可用畫面上的行號
  P2-05 梯次移動與格網平移是跨檔的兩步,中斷要留得下線索且收斂前先核對
"""
import io
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.model import Member, SolveContext      # noqa: E402
from cmuh_common.roster.rules import FridayBiopsyLinkRule      # noqa: E402
from cmuh_common.roster.service import RosterService           # noqa: E402
from cmuh_common.roster.storage import (                       # noqa: E402
    RosterStorage, StaleRosterDataError,
)

YM = "2026-08"


@pytest.fixture()
def svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({
        "r_members": [{"id": "A", "name": "甲", "level": "R2"},
                      {"id": "B", "name": "乙", "level": "R3"}],
        "vs_members": [], "pgy_members": [],
        "points": {"weekday": 1, "weekend": 2, "national_holiday": 1},
    })
    st.save_month(YM, {"r_duty": {}})
    return RosterService(st)


def _corrupt(st, name):
    io.open(os.path.join(st.base_dir, name), "w", encoding="utf-8").write(
        "{壞掉的 JSON")


# ══ P2-01 壞檔不得變成一份空的漂亮基準 ═══════════════════════════════════
class TestWriteIntentPathsUseAStrictSource:
    def test_the_solver_baseline_fails_closed_on_a_corrupt_ledger(self, svc):
        """★求解基準是「會被寫回去的東西」的來源★:寬鬆載入回空帳本 →
        預覽用「大家都是 0」算出一份看起來很正常的班表,套用時就把那份空的
        當基準寫回去。壞檔要在求解之前就明講。"""
        _corrupt(svc.storage, "ledger.json")
        with pytest.raises(ValueError):
            svc.build_context("r", YM, for_solve=True)

    def test_a_corrupt_ledger_cannot_be_reset_by_accept(self, svc):
        """accept 的帳本來源同理:壞檔不得被「幾乎只剩本月」的新帳蓋掉。"""
        pytest.importorskip("ortools")
        res = svc.run_solve("r", YM)
        assert res.status == "ok", res.diagnosis
        _corrupt(svc.storage, "ledger.json")
        with pytest.raises(ValueError):
            svc.accept_solution("r", YM, res)
        raw = io.open(os.path.join(svc.storage.base_dir, "ledger.json"),
                      encoding="utf-8").read()
        assert raw.startswith("{壞掉"), "★壞帳本被覆寫掉了★"

    def test_accept_refuses_when_the_ledger_moved_under_it(self, svc,
                                                           monkeypatch):
        """★accept 的帳本寫入要帶 CAS★:沒有 `expected_revision` 的話,他機
        剛結算的【別月】分錄會被這份舊快照整份蓋掉。

        ★反例要孤立這一條★:直接把帳本弄壞是量不到的 —— `solver_ledger`
        (求解基準)早一步就 fail closed 了,綠燈其實來自另一條規則。
        這裡讓快照回一個【對不上盤面】的 revision:唯有「寫回時比對」才擋得住。
        """
        pytest.importorskip("ortools")
        res = svc.run_solve("r", YM)
        assert res.status == "ok", res.diagnosis
        real = svc.storage.canonical_snapshot
        monkeypatch.setattr(
            svc.storage, "canonical_snapshot",
            lambda name, *a, **k: ((real(name, *a, **k)[0], "他機改過了")
                                   if name == "ledger.json"
                                   else real(name, *a, **k)))
        with pytest.raises(StaleRosterDataError):
            svc.accept_solution("r", YM, res)

    @staticmethod
    def _storage_calls(fn) -> list:
        """→ 這個函式裡對 `self.storage.X(...)` 的呼叫名稱清單。

        ★用 AST,不用字串比對★:第一版寫成 `"load_biopsy()" not in src`,
        結果被我自己【解釋這件事的註解】判成違規(註解裡本來就要提到舊寫法)。
        守衛要看的是程式碼真的呼叫了什麼。
        """
        import ast
        import inspect
        import textwrap
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        out = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)):
                continue
            v = node.func.value
            if isinstance(v, ast.Attribute) and v.attr == "storage":
                out.append(node.func.attr)
        return out

    def test_the_biopsy_book_is_read_once(self):
        """版本與內容同源:`canonical_revision` + `load_biopsy` 是兩次讀取,
        中間被換入壞內容時兩邊都取自那份壞的,CAS 對得上就放行。"""
        import ast
        import inspect
        import textwrap
        # (RS-19)這一份改由權威輸入 `StrictSources.snapshot("biopsy.json")`
        # 提供 —— 仍是【讀一次位元組、版本與內容同源】,只是來源物件換了。
        # 「兩次讀取」的那個形狀照樣不准出現。
        fn = RosterService._recompute_saturday_biopsy_locked
        calls = self._storage_calls(fn)
        names = [n.func.attr for n in ast.walk(
            ast.parse(textwrap.dedent(inspect.getsource(fn))))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        assert "snapshot" in names or "canonical_snapshot" in calls
        assert "load_biopsy" not in names and "canonical_revision" not in names

    def test_rename_reads_its_sources_strictly(self):
        """改名會把帳本/切片計數整份寫回去 → 來源不可以是寬鬆載入。"""
        calls = self._storage_calls(RosterService._rename_member_locked)
        assert calls.count("canonical_snapshot") >= 2, calls
        assert "load_ledger" not in calls and "load_biopsy" not in calls


# ══ P2-02 意圖＝衍生物的義務 ═════════════════════════════════════════════
class TestTheIntentMeansTheDerivedStateIsRebuilt:
    def _boom_recompute(self, svc, monkeypatch):
        def _boom(*_a, **_k):
            raise RuntimeError("切片重排壞了")
        monkeypatch.setattr(svc, "recompute_saturday_biopsy", _boom)

    def test_set_cell_keeps_the_intent_when_the_recompute_fails(
            self, svc, monkeypatch):
        """★反例本體★:使用者要的是「重排失敗不擋手動改格」,但月檔已經換成
        新班表、biopsy.json 停在舊人選 —— 那個不一致要有人負責收斂。"""
        self._boom_recompute(svc, monkeypatch)
        svc.set_cell("r", YM, date(2026, 8, 8), "A")     # 8/8 是週六
        assert svc.storage.load_month(YM)["r_duty"], "手動改格不該被擋"
        pend = [(x["scope"], x["ym"], svc.storage.pending_kind(x))
                for x in svc.storage.load_pending_settles()]
        assert pend == [("r", YM, "biopsy")], f"★意圖被清掉了★ {pend}"

    def test_a_successful_edit_leaves_no_intent(self, svc):
        svc.set_cell("r", YM, date(2026, 8, 8), "A")
        assert not svc.storage.load_pending_settles()

    def test_clear_unlocked_keeps_the_intent_too(self, svc, monkeypatch):
        svc.set_cell("r", YM, date(2026, 8, 8), "A")
        self._boom_recompute(svc, monkeypatch)
        svc.clear_unlocked("r", YM)
        assert [svc.storage.pending_kind(x)
                for x in svc.storage.load_pending_settles()] == ["biopsy"]

    def test_the_biopsy_obligation_does_not_swallow_the_ledger_one(self, svc):
        """★種類化的重點★:別人未完成的帳本義務("all")不可以被切片這條路
        清掉 —— 清掉等於替帳本宣稱「已經一致」。"""
        svc.storage.mark_pending_settle("r", YM, kind="all")
        svc.set_cell("r", YM, date(2026, 8, 8), "A")     # 成功,但義務不是它的
        assert [svc.storage.pending_kind(x)
                for x in svc.storage.load_pending_settles()] == ["all"]

    def test_two_kinds_of_obligation_coexist(self, svc):
        """★不同種類是不同的義務,不可以被摺成一筆★:摺起來之後,先記的那個
        種類會讓後來的另一種永遠登記不上(而它真的還沒重建)。"""
        assert svc.storage.mark_pending_settle("r", YM, kind="ledger") is True
        assert svc.storage.mark_pending_settle("r", YM, kind="biopsy") is True
        kinds = sorted(svc.storage.pending_kind(x)
                       for x in svc.storage.load_pending_settles())
        assert kinds == ["biopsy", "ledger"], kinds
        # 同一種再記一次仍是冪等(義務是別人的 → 回 False,不得重複)
        assert svc.storage.mark_pending_settle("r", YM, kind="biopsy") is False
        svc.storage.clear_pending_settle("r", YM, kind="biopsy")
        assert [svc.storage.pending_kind(x)
                for x in svc.storage.load_pending_settles()] == ["ledger"]

    def test_a_biopsy_only_obligation_does_not_rewrite_the_ledger(
            self, svc, monkeypatch):
        """★只欠切片就只重建切片★:整份重算會連帳本一起改寫 —— 那不是它的
        義務,而帳本是下個月公平目標的基準。"""
        svc.set_cell("r", YM, date(2026, 8, 8), "A")
        svc.update_ledger(lambda led: led.setdefault("r", {}).update({"A": 9.0}))
        svc.storage.mark_pending_settle("r", YM, kind="biopsy")
        svc.reconcile_pending_settles()
        assert svc.storage.load_ledger()["r"]["A"] == 9.0,             "★只欠切片,帳本卻被整份重算改寫了★"

    def test_the_kept_intent_reconciles_narrowly(self, svc, monkeypatch):
        """★手動改格留下的意圖,種類必須是 biopsy★:記成 "all" 的話,下次開
        程式的收斂會走整份重算 —— 連帶把帳本改寫掉,而帳本根本沒有壞。
        (反例走【真正的生產路徑】產生意圖,不是手工 mark:那樣量不到
        `_biopsy_intent` 用的是哪一種。)"""
        # ★班表要不對稱★:兩人點數相同的話,整份重算算出來的 delta 是 0,
        #   帳本被改寫也看不出來 —— 勝負靠巧合的反例什麼都量不到。
        for d in (3, 4, 5):                              # A 多值三個平日
            svc.set_cell("r", YM, date(2026, 8, d), "A")
        self._boom_recompute(svc, monkeypatch)
        svc.set_cell("r", YM, date(2026, 8, 8), "B")     # 意圖由這條路留下
        # ★基準取【收斂之前】的帳本★(RS-20 P1-02 之後,手動改格本身就會把
        #   帳本結算到與實排一致 —— 事先手工塞一個值再比對,量到的是那次
        #   合法的結算,不是收斂有沒有多改)。
        before = dict(svc.storage.load_ledger()["r"])
        assert len(set(before.values())) > 1,             "反例要不對稱,否則整份重算改寫帳本也看不出來"
        monkeypatch.undo()
        svc.reconcile_pending_settles()
        assert dict(svc.storage.load_ledger()["r"]) == before,             "★只欠切片,收斂卻整份重算而改寫了帳本★"
        assert not svc.storage.load_pending_settles()

    def test_accept_downgrades_the_obligation_instead_of_erasing_it(
            self, svc, monkeypatch):
        """★外審 RS-16 R1-1★:accept 進場先記了 "all";切片重建失敗時
        「先 mark(biopsy) 再 clear(all)」是行不通的 —— mark 會判定「已有一筆
        all 涵蓋你」而回 False,接著 clear 把 all 拿掉,義務整個消失。
        降級必須是【一次寫入】的原地改型。"""
        pytest.importorskip("ortools")
        res = svc.run_solve("r", YM)
        assert res.status == "ok", res.diagnosis
        self._boom_recompute(svc, monkeypatch)
        svc.accept_solution("r", YM, res)                # 值班照常落地
        assert svc.storage.load_month(YM)["r_duty"]
        assert svc.storage.load_ledger()["r"], "帳本這一半確實重建好了"
        pend = [(x["scope"], x["ym"], svc.storage.pending_kind(x))
                for x in svc.storage.load_pending_settles()]
        assert pend == [("r", YM, "biopsy")], f"★義務消失了★ {pend}"

    def test_resettle_downgrades_the_same_way(self, svc, monkeypatch):
        svc.set_cell("r", YM, date(2026, 8, 3), "A")
        self._boom_recompute(svc, monkeypatch)
        svc.resettle_from_duty("r", YM)
        assert [svc.storage.pending_kind(x)
                for x in svc.storage.load_pending_settles()] == ["biopsy"]

    def test_a_successful_accept_leaves_nothing_behind(self, svc):
        pytest.importorskip("ortools")
        svc.accept_solution("r", YM, svc.run_solve("r", YM))
        assert not svc.storage.load_pending_settles()

    def test_reconcile_rebuilds_only_what_is_owed(self, svc, monkeypatch):
        """只欠切片時不必(也不該)整份重算帳本:收斂後意圖清掉、切片重建。"""
        self._boom_recompute(svc, monkeypatch)
        svc.set_cell("r", YM, date(2026, 8, 8), "A")
        monkeypatch.undo()
        assert svc.reconcile_pending_settles() == [("r", YM)]
        assert not svc.storage.load_pending_settles()
        assert svc.storage.load_month(YM).get("saturday_biopsy") is not None


# ══ P2-03 週五連動要認得手動指定/不切片 ══════════════════════════════════
class _FakeModel:
    def __init__(self):
        self.bools = []

    def NewBoolVar(self, name):      # noqa: N802
        self.bools.append(name)
        return ("bool", name)

    def Add(self, *_a, **_k):        # noqa: N802
        return None


class _FakeMc:
    def __init__(self, ctx):
        self.model = _FakeModel()
        self.x = {(d, m.id): ("x", d, m.id)
                  for d in ctx.days for m in ctx.members}


def _ctx_with_override(ov):
    ctx = SolveContext(
        scope="r", year=2026, month=8,
        members=[Member(id="A", name="甲", level="R2"),
                 Member(id="B", name="乙", level="R3")],
        biopsy_override=ov)
    ctx.prepare()
    return ctx


class TestTheFridayLinkKnowsTheOverride:
    SAT = date(2026, 8, 8)
    FRI = date(2026, 8, 7)

    def _terms(self, ov):
        ctx = _ctx_with_override(ov)
        mc = _FakeMc(ctx)
        return FridayBiopsyLinkRule().objective_terms(mc, ctx), mc

    def test_a_skipped_saturday_gets_no_reward(self):
        """「本週不切片」→ 沒有切片者可連動,不得為了不存在的切片調週五。"""
        terms, mc = self._terms({self.SAT: ""})
        assert not [n for n in mc.model.bools
                    if n.endswith(self.SAT.isoformat())], mc.model.bools
        assert all(self.SAT.isoformat() not in str(t) for t, _w in terms)
        assert all(str(self.FRI) not in str(t) for t, _w in terms)

    def test_a_manual_person_is_rewarded_directly_on_friday(self):
        """手動指定 → 切片者已固定,獎勵的必須是【他】值週五,
        而不是「誰值週六誰就連動」。"""
        terms, _mc = self._terms({self.SAT: "A"})
        got = [t for t, _w in terms if t == ("x", self.FRI, "A")]
        assert got, terms
        assert not any(t == ("x", self.FRI, "B") for t, _w in terms), \
            "★獎勵給了沒有要切片的人★"

    def test_an_unlisted_override_falls_back_to_duty_linkage(self):
        """名單外代號會被 `assign_saturday_biopsy` 忽略改自動排 → 規則也要
        退回值班連動(兩邊語意必須一致)。"""
        terms, mc = self._terms({self.SAT: "ZZ"})
        assert [n for n in mc.model.bools if self.SAT.isoformat() in n]
        assert terms

    def test_no_override_keeps_the_old_behaviour(self):
        terms, mc = self._terms({})
        assert [n for n in mc.model.bools if self.SAT.isoformat() in n]
        assert terms

    def test_the_override_is_part_of_the_solver_input_fingerprint(self, svc):
        """★成為輸入就要進指紋★:否則預覽期間他機改了指定,舊解照樣落地。"""
        from cmuh_common.roster.solve_rvs import rvs_input_fingerprint
        before = rvs_input_fingerprint(
            svc.build_context("r", YM, for_solve=True))
        svc.set_biopsy_person(YM, date(2026, 8, 8), "")   # 本週不切片
        after = rvs_input_fingerprint(
            svc.build_context("r", YM, for_solve=True))
        assert before != after, "★手動指定改了,求解輸入的指紋卻沒變★"


# ══ P2-04 模板刪除用身分,不用行號 ════════════════════════════════════════
class TestTemplateDeletionUsesIdentity:
    def test_a_remote_insert_does_not_shift_my_delete(self, svc):
        """★反例本體★:畫面是 [乙, 甲],使用者選乙;他機在前面插入 X →
        最新是 [X, 乙, 甲],依 index 刪掉的是別人。"""
        b_id = svc.add_clinic_template_entry(0, "上午", "102", "乙醫師")
        svc.add_clinic_template_entry(0, "上午", "101", "甲醫師")
        svc.update_clinic_template(
            lambda d: d["template"]["0"]["上午"].insert(
                0, {"id": "x9", "room": "199", "doctor": "他機"}))
        svc.delete_clinic_template_entry(0, "上午", entry_id=b_id)
        rooms = [e.get("room") for e in
                 svc.storage.load_clinic_template()["template"]["0"]["上午"]]
        assert rooms == ["199", "101"], f"★刪錯人★ {rooms}"

    def test_a_legacy_entry_without_id_is_found_by_content(self, svc):
        svc.update_clinic_template(
            lambda d: d.setdefault("template", {}).setdefault("0", {})
            .setdefault("上午", []).append({"room": "103", "doctor": "丙"}))
        svc.delete_clinic_template_entry(
            0, "上午", entry_id="",
            identity=RosterService.clinic_template_identity(
                {"room": "103", "doctor": "丙"}))
        assert not (svc.storage.load_clinic_template()["template"]["0"]
                    ["上午"])

    def test_an_already_deleted_entry_is_reported_not_guessed(self, svc):
        eid = svc.add_clinic_template_entry(0, "上午", "101")
        svc.delete_clinic_template_entry(0, "上午", entry_id=eid)
        with pytest.raises(StaleRosterDataError, match="已經不在清單"):
            svc.delete_clinic_template_entry(0, "上午", entry_id=eid)

    def test_new_entries_carry_a_stable_id(self, svc):
        eid = svc.add_clinic_template_entry(0, "下午", "105")
        rows = svc.storage.load_clinic_template()["template"]["0"]["下午"]
        assert eid and rows[0]["id"] == eid

    def test_the_ui_hands_over_an_identity_not_a_row_number(self):
        """★UI 不得再用 index 當持久身分★(守衛錨在性質上)。"""
        import inspect
        from cmuh_common.roster.ui import settings as s
        src = inspect.getsource(s.SettingsTab._template_del)
        assert "delete_clinic_template_entry" in src
        assert "pop(int(" not in src and "int(idx)" not in src


# ══ P2-05 梯次移動 → 格網平移的跨檔意圖 ══════════════════════════════════
class TestTheGridShiftHasAnIntent:
    @pytest.fixture()
    def bsvc(self, tmp_path):
        st = RosterStorage(str(tmp_path))
        st.save_config({"r_members": [], "vs_members": []})
        st.save_clerk_batches([{"id": "b1", "start_monday": "2026-08-03"}])
        st.save_biopsy_grid({"b1": {"2026-08-03": {"上午": True}}})
        return RosterService(st)

    def _move(self, bsvc, new_start="2026-08-10"):
        before = {"id": "b1", "start_monday": "2026-08-03"}
        return bsvc.update_clerk_batch_fields(
            "b1", before, dict(before, start_monday=new_start))

    def test_a_crash_before_the_shift_leaves_an_intent(self, bsvc, monkeypatch):
        monkeypatch.setattr(bsvc, "_shift_biopsy_grid",
                            lambda *_a, **_k: (_ for _ in ()).throw(
                                OSError("這一刻不給寫")))
        with pytest.raises(OSError):
            self._move(bsvc)
        pend = bsvc.storage.load_pending_grid_shifts()
        assert [(x["batch_id"], x["old_start"], x["new_start"])
                for x in pend] == [("b1", "2026-08-03", "2026-08-10")], pend

    def test_the_next_start_finishes_the_shift(self, bsvc, monkeypatch):
        monkeypatch.setattr(bsvc, "_shift_biopsy_grid",
                            lambda *_a, **_k: (_ for _ in ()).throw(
                                OSError("x")))
        with pytest.raises(OSError):
            self._move(bsvc)
        monkeypatch.undo()
        assert bsvc.reconcile_pending_grid_shifts() == ["b1"]
        assert bsvc.storage.load_biopsy_grid()["b1"] == {
            "2026-08-10": {"上午": True}}
        assert not bsvc.storage.load_pending_grid_shifts()

    def test_a_successful_move_leaves_no_intent(self, bsvc):
        self._move(bsvc)
        assert not bsvc.storage.load_pending_grid_shifts()

    def test_reconcile_verifies_the_batch_before_moving_anything(self, bsvc):
        """★不可盲信意圖★:意圖是在梯次寫入【之前】記的,那次寫入仍可能失敗。
        梯次還在舊日期 → 格網本來就該留在舊窗,什麼都不要動。"""
        bsvc.storage.mark_pending_grid_shift("b1", "2026-08-03", "2026-08-10")
        assert bsvc.reconcile_pending_grid_shifts() == ["b1"]
        assert bsvc.storage.load_biopsy_grid()["b1"] == {
            "2026-08-03": {"上午": True}}, "★依著沒發生過的移動硬搬了★"

    def test_an_unrecognisable_grid_is_kept_not_guessed(self, bsvc):
        bsvc.storage.save_clerk_batches(
            [{"id": "b1", "start_monday": "2026-08-10"}])
        bsvc.storage.save_biopsy_grid({"b1": {"2026-09-21": {"上午": True}}})
        bsvc.storage.mark_pending_grid_shift("b1", "2026-08-03", "2026-08-10")
        assert bsvc.reconcile_pending_grid_shifts() == []
        assert bsvc.storage.load_pending_grid_shifts(), "★收斂不了卻清掉意圖★"
        assert bsvc.storage.load_biopsy_grid()["b1"] == {
            "2026-09-21": {"上午": True}}, "★看不懂還硬搬★"

    def test_an_overlapping_window_is_decided_by_identity(self, bsvc,
                                                          monkeypatch):
        """★外審 RS-16 R1-2★:位移 7 天、格網只剩 8/10 那一格 —— 它同時落在
        舊窗(8/03~8/16)與新窗(8/10~8/23)。只看落點會判成「已經搬過了」而
        清掉意圖,切片室的開放週從此掛在錯的一週。正解是 8/17。"""
        bsvc.storage.save_biopsy_grid({"b1": {"2026-08-10": {"上午": True}}})
        monkeypatch.setattr(bsvc, "_shift_biopsy_grid",
                            lambda *_a, **_k: (_ for _ in ()).throw(
                                OSError("x")))
        with pytest.raises(OSError):
            self._move(bsvc)
        monkeypatch.undo()
        assert bsvc.reconcile_pending_grid_shifts() == ["b1"]
        assert bsvc.storage.load_biopsy_grid()["b1"] == {
            "2026-08-17": {"上午": True}}, "★重疊視窗被誤判成已完成★"

    def test_a_legacy_intent_without_identity_keeps_an_ambiguous_grid(
            self, bsvc):
        """舊版意圖沒有平移前的身分 → 重疊時★寧可留著讓人確認★,不猜。"""
        bsvc.storage.save_clerk_batches(
            [{"id": "b1", "start_monday": "2026-08-10"}])
        bsvc.storage.save_biopsy_grid({"b1": {"2026-08-10": {"上午": True}}})
        bsvc.storage.mark_pending_grid_shift("b1", "2026-08-03", "2026-08-10")
        assert bsvc.reconcile_pending_grid_shifts() == []
        assert bsvc.storage.load_pending_grid_shifts()
        assert bsvc.storage.load_biopsy_grid()["b1"] == {
            "2026-08-10": {"上午": True}}, "★歧義時還是搬了★"

    def test_a_crash_after_the_shift_is_recognised_as_done(self, bsvc,
                                                          monkeypatch):
        """★另一個崩潰視窗★(外審 RS-16 R2):格網搬好了、意圖還沒清掉就斷電。
        收斂要認得「這就是搬過去的樣子」→ 清掉意圖;認不出來的話,那一梯的
        起始日從此再也改不動(每次都被「上一次還沒收斂」擋下)。"""
        real_clear = bsvc.storage.clear_pending_grid_shift
        monkeypatch.setattr(bsvc.storage, "clear_pending_grid_shift",
                            lambda *_a, **_k: (_ for _ in ()).throw(
                                OSError("清意圖時斷電")))
        with pytest.raises(OSError):
            self._move(bsvc)
        monkeypatch.setattr(bsvc.storage, "clear_pending_grid_shift",
                            real_clear)
        assert bsvc.storage.load_biopsy_grid()["b1"] == {
            "2026-08-10": {"上午": True}}, "格網其實已經搬好了"
        assert bsvc.reconcile_pending_grid_shifts() == ["b1"]
        assert not bsvc.storage.load_pending_grid_shifts()
        assert bsvc.storage.load_biopsy_grid()["b1"] == {
            "2026-08-10": {"上午": True}}, "★又搬了一次★"

    def test_a_hand_edited_grid_is_not_guessed(self, bsvc, monkeypatch):
        """平移前後的樣子都對不上(有人手工編過)→ 保留意圖、不動格網。"""
        monkeypatch.setattr(bsvc, "_shift_biopsy_grid",
                            lambda *_a, **_k: (_ for _ in ()).throw(
                                OSError("x")))
        with pytest.raises(OSError):
            self._move(bsvc)
        monkeypatch.undo()
        bsvc.update_biopsy_grid(                       # 有人又改了格子
            lambda g: g["b1"].__setitem__("2026-08-05", {"下午": True}))
        assert bsvc.reconcile_pending_grid_shifts() == []
        assert bsvc.storage.load_pending_grid_shifts()

    def test_a_second_move_while_one_is_pending_is_refused(self, bsvc):
        """兩次平移疊起來之後,收斂端再也分不出格網停在哪一段 → 明確拒絕。"""
        bsvc.storage.mark_pending_grid_shift("b1", "2026-08-03", "2026-08-10")
        with pytest.raises(StaleRosterDataError, match="還沒有收斂完成"):
            self._move(bsvc, "2026-08-17")

    def test_the_reconcile_is_wired_up_at_startup(self):
        import inspect

        import scheduler
        src = inspect.getsource(scheduler.ScheduleApp.__init__)
        assert "reconcile_pending_grid_shifts()" in src, \
            "★沒有呼叫端＝這個機制不存在★"
