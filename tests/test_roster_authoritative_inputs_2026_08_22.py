# -*- coding: utf-8 -*-
"""[批次RS-19 / 全審 2026-08-22 四個 P1] 權威輸入、正式文件、報告新鮮度、停診。

P1-01 會【寫回去】的計算不得吃寬鬆載入的空值:`holiday_duty.json` 讀不到就
      沒有國定假日、`config.json` 讀不到就沒有成員 —— solver 照樣算得完,
      `settle_month` 照樣把那份空的寫進正式帳本。★指紋擋不住★:套用時重建
      context 若讀到同一個壞狀態,兩次指紋是【同樣錯的空語意】,比對相等。
P1-02 正式文件(匯出 / 定案留底 PDF)不接受 partial success:少班少人的
      xlsx 與「真的沒有排班」在文件上長得一模一樣。
P1-03 Auto Accept 之後手動換班 → `report_r` 沒有失效 → 定案 PDF 印的是
      舊班表,而帳本已依新班表重算(同一份文件自相矛盾)。
P1-04 停診把「哪幾天有開」在臨界區外展開:背景 pull 進來的新場次不在展開
      結果裡 → 那些場次沒被停診,而 UI 回報「整段停診成功」。
P2    義務在來源寫入之前記下 → 來源自己被拒時會留下一筆其實不存在的債。
"""
import ast
import contextlib
import inspect
import io
import os
import sys
import textwrap
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster import service as svc_mod                 # noqa: E402
from cmuh_common.roster.model import day_point                    # noqa: E402
from cmuh_common.roster.report import build_final_state_report    # noqa: E402
from cmuh_common.roster.service import (                          # noqa: E402
    REPORT_FRESH, REPORT_STALE, REPORT_UNVERIFIABLE, RosterService,
    report_state,
)
from cmuh_common.roster.solve_rvs import (                        # noqa: E402
    SolveResult, rvs_input_fingerprint,
)
from cmuh_common.roster.storage import (                          # noqa: E402
    FinalizedMonthError, RosterStorage,
)

YM = "2026-08"          # 2026/8/1 = 週六
SRC_DIR = os.path.join(os.path.dirname(__file__), "..", "src")


@pytest.fixture()
def svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({
        "r_members": [{"id": "A", "name": "名A"}, {"id": "B", "name": "名B"}],
        "vs_members": [{"id": "D", "name": "D醫師"}],
        "pgy_members": [{"id": "P1"}, {"id": "P2"}],
        "points": {"weekday": 1, "weekend": 2, "national_holiday": 1},
    })
    st.save_month(YM, {"r_duty": {}})
    st.save_clinic_template({"template": {
        "1": {"上午": [{"room": "101", "doctor": "甲"}]}}})
    st.save_clerk_batches([{"id": "b1", "start_monday": "2026-08-03",
                            "members": ["C1"]}])
    return RosterService(st)


def _corrupt(svc, name: str) -> None:
    """把某個正典檔弄成壞的(人工編輯/同步中斷/防毒鎖住之後的實際樣子)。"""
    io.open(svc.storage._path(name), "w", encoding="utf-8").write("{壞掉的")


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


# ══ P1-01 權威輸入 ══════════════════════════════════════════════════════
class TestTheFingerprintCannotSeeThis:
    def test_a_corrupt_holiday_table_looks_exactly_like_an_empty_one(
            self, svc):
        """★這就是為什麼指紋不夠★:壞掉的假日表被寬鬆載入正規化成「沒有
        任何國定假日」—— 與一份真的空表【逐位元組等價】。求解與套用讀到
        同一個壞狀態時,兩次指紋當然相等,守衛於是替一份按平日排的班表背書。
        """
        svc.storage.save_holiday_duty({"r": {date(2026, 8, 10): "A"},
                                       "vs": {}})
        good = rvs_input_fingerprint(svc.build_context("r", YM,
                                                       for_solve=True))
        _corrupt(svc, "holiday_duty.json")
        broken = rvs_input_fingerprint(svc.build_context("r", YM,
                                                         for_solve=True))
        svc.storage.save_holiday_duty({"r": {}, "vs": {}})
        empty = rvs_input_fingerprint(svc.build_context("r", YM,
                                                        for_solve=True))
        assert broken == empty, "前提不成立:壞檔與空表在寬鬆載入下不等價"
        assert broken != good, "前提不成立:假日表根本沒進指紋"

    def test_the_solve_refuses_instead_of_pretending_they_are_workdays(
            self, svc):
        _corrupt(svc, "holiday_duty.json")
        with pytest.raises(ValueError, match="holiday_duty.json"):
            svc.run_solve("r", YM)

    def test_an_unreadable_previous_month_stops_the_solve(self, svc):
        """★上月讀不到＝安靜地少一段限制★:跨月銜接(last_weekend)、連續值班
        的尾端、跨月週五的切片連動都來自它。少了它,求解照樣算得完,而算出來
        的班表可能與上月最後一個週末撞在一起 —— 畫面上完全看不出來。"""
        io.open(svc.storage._month_path("2026-07"), "w",
                encoding="utf-8").write("{壞")
        with pytest.raises(ValueError, match="2026-07.json"):
            svc.run_solve("r", YM)

    def test_accept_refuses_when_an_input_became_unreadable(self, svc):
        """★套用時的重建也要是權威的★:預覽時好好的,按下套用前那一刻檔案
        壞了 —— 舊寫法會用「空的名單/假日」重建 ctx 去驗一份舊結果。"""
        res = _result_for(svc, YM, _cover(svc, YM, "A"))
        _corrupt(svc, "config.json")
        with pytest.raises(ValueError, match="config.json"):
            svc.accept_solution("r", YM, res)
        assert svc.storage.load_ledger()["r"] == {}, "★被拒就不可以落地★"


class TestTheLedgerIsNotRewrittenFromAnEmptyRoster:
    def _accepted(self, svc):
        svc.accept_solution("r", YM, _result_for(svc, YM, _cover(svc, YM)))
        return dict(svc.storage.load_ledger()["r"])

    def test_resettle_refuses_when_the_roster_is_unreadable(self, svc):
        """★反例本體★:`config.json` 暫時讀不到 → members=[] → points 全空,
        而 `settle_month` 會【先回滾本月舊分錄】再記上那份空的 ——
        正式帳本被改寫成 0,畫面還回報「已重算」。"""
        before = self._accepted(svc)
        assert before, "前提不成立:帳本本來就是空的,量不到覆寫"
        _corrupt(svc, "config.json")
        with pytest.raises(ValueError, match="config.json"):
            svc.resettle_from_duty("r", YM)
        svc.storage.save_config({
            "r_members": [{"id": "A", "name": "名A"}, {"id": "B"}],
            "points": {"weekday": 1, "weekend": 2, "national_holiday": 1}})
        assert dict(svc.storage.load_ledger()["r"]) == before, \
            "★帳本被一份【從讀不到的檔推導出來的空名單】改寫了★"

    def test_a_readable_roster_still_resettles(self, svc):
        """守衛不得因為嚴格化而讓正常路徑失效。"""
        self._accepted(svc)
        pts = svc.resettle_from_duty("r", YM)
        assert pts.get("A", 0) > 0


class TestTheDeclarationIsTheContract:
    def test_an_undeclared_source_fails_loudly(self, svc):
        """★宣告不足要當場失敗,不可以靜默退回寬鬆讀取★ —— 否則「這條路徑
        吃哪些檔」就只是註解裡的宣稱。"""
        src = svc.storage.strict_sources(("config.json",), (YM,))
        assert src.load_config()["r_members"]
        with pytest.raises(KeyError, match="宣告"):
            src.load_holiday_duty()
        with pytest.raises(KeyError, match="宣告"):
            src.load_month("2026-09")

    def test_the_shapes_are_copies(self, svc):
        """★每次存取回深拷貝★:呼叫端會就地改帳本(`settle_biopsy` 就是),
        共用同一個物件的話,「這次讀到的輸入」會被上一次的計算改掉。"""
        src = svc.storage.strict_sources(("ledger.json",), (YM,))
        src.load_ledger()["r"]["A"] = 99
        assert src.load_ledger()["r"] == {}

    def test_the_version_and_the_content_come_from_one_read(self, svc):
        shape, rev = src_snap = svc.storage.strict_sources(
            ("ledger.json",), ()).snapshot("ledger.json")
        assert isinstance(shape, dict) and rev == \
            svc.storage.canonical_revision("ledger.json")
        assert len(src_snap) == 2


class TestDisplayPathsStayLenient:
    """★不可以為了嚴格化而讓「只是想看一眼」的人打不開視窗★(回歸)。"""

    def test_a_corrupt_file_does_not_break_the_display_context(self, svc):
        _corrupt(svc, "week_colors.json")
        svc.build_context("r", YM)            # 顯示用 → 不得拋
        svc.quick_validate("r", YM)

    def test_the_day_preview_stays_lenient(self, svc):
        _corrupt(svc, "clinic_template.json")
        svc.run_day_solve(YM)                 # 預覽不寫檔 → 不得拋

    def test_but_applying_that_preview_is_refused(self, svc):
        """預覽寬鬆、套用嚴格 —— 分界就在「會不會寫回去」。"""
        res = svc.run_day_solve(YM)
        _corrupt(svc, "clerk_batches.json")
        with pytest.raises(ValueError, match="clerk_batches.json"):
            svc.accept_day_solution(YM, res.day_slots, "", expect=res)



@contextlib.contextmanager
def _no_lenient_reads(st):
    """★權威路徑不得有【任何一次】寬鬆的磁碟讀取★

    這是「builder 真的用了權威輸入」的判準。用壞檔量不到它:嚴格讀取發生在
    `_sources()` 建立包裝的那一刻,所以 builder 就算忽略 `src` 自己去讀,
    壞檔照樣會在更早的地方被擋下 —— 兩種寫法都紅,分不出勝負。
    (帶 `_parsed=` 的呼叫是快照自己在做正規化,不是去讀盤。)
    """
    names = ("load_config", "load_ledger", "load_biopsy", "load_holiday_duty",
             "load_clinic_template", "load_clerk_batches", "load_biopsy_grid",
             "load_week_colors", "load_week_colors_raw", "holidays_set",
             "load_month", "load_month_with_revision",
             "prev_month_last_weekend")
    seen: list = []
    orig = {n: getattr(st, n) for n in names}

    def _wrap(n, f):
        def w(*a, **kw):
            if kw.get("_parsed") is None:
                seen.append(n)
            return f(*a, **kw)
        return w

    for n in names:
        setattr(st, n, _wrap(n, orig[n]))
    try:
        yield seen
    finally:
        for n in names:
            setattr(st, n, orig[n])


class TestTheAuthoritativePathsReadOnlyThroughTheBundle:
    """★宣告要有牙齒★:builder 若忽略 `src` 自己讀盤,「這條路徑吃哪些檔」
    就又變回一句宣稱 —— 而未宣告的那些檔會靜靜地退回寬鬆載入。"""

    def test_the_solve_reads_nothing_leniently(self, svc):
        with _no_lenient_reads(svc.storage) as seen:
            svc.run_solve("r", YM)
        assert not seen, f"★求解仍在寬鬆讀盤★ {seen}"

    def test_accept_reads_nothing_leniently(self, svc):
        res = _result_for(svc, YM, _cover(svc, YM))
        with _no_lenient_reads(svc.storage) as seen:
            svc.accept_solution("r", YM, res)
        assert not seen, f"★套用仍在寬鬆讀盤★ {seen}"

    def test_resettle_reads_nothing_leniently(self, svc):
        svc.accept_solution("r", YM, _result_for(svc, YM, _cover(svc, YM)))
        with _no_lenient_reads(svc.storage) as seen:
            svc.resettle_from_duty("r", YM)
        assert not seen, f"★重算帳本仍在寬鬆讀盤★ {seen}"

    def test_the_day_apply_reads_nothing_leniently(self, svc):
        res = svc.run_day_solve(YM)
        with _no_lenient_reads(svc.storage) as seen:
            svc.accept_day_solution(YM, res.day_slots, "", expect=res)
        assert not seen, f"★日排班套用仍在寬鬆讀盤★ {seen}"

    def test_the_export_reads_nothing_leniently(self, svc):
        with _no_lenient_reads(svc.storage) as seen:
            svc.build_export(YM)
        assert not seen, f"★匯出仍在寬鬆讀盤★ {seen}"

    def test_the_archive_reads_nothing_leniently(self, svc):
        with _no_lenient_reads(svc.storage) as seen:
            svc.build_finalize_pdf_sections(YM)
        assert not seen, f"★定案留底仍在寬鬆讀盤★ {seen}"

    def test_the_closure_reads_nothing_leniently(self, svc):
        with _no_lenient_reads(svc.storage) as seen:
            svc.set_clinic_closed(YM, "101", date(2026, 8, 3),
                                  date(2026, 8, 31), ["上午"])
        assert not seen, f"★停診仍在寬鬆讀盤★ {seen}"

    def test_the_biopsy_recompute_reads_nothing_leniently(self, svc):
        svc.set_cell("r", YM, date(2026, 8, 1), "A")
        with _no_lenient_reads(svc.storage) as seen:
            svc.recompute_saturday_biopsy(YM)
        assert not seen, f"★切片重排仍在寬鬆讀盤★ {seen}"

    def test_the_builder_uses_the_snapshot_not_the_disk(self, svc):
        """同一件事的另一面:包裝建立之後盤上再變,權威計算仍用【當時那一份】
        —— 這才是「整批一致」的意思。"""
        src = svc.storage.strict_sources(svc_mod.SRC_RVS,
                                         ("2026-07", YM))
        svc.storage.save_config({
            "r_members": [{"id": "X"}],
            "points": {"weekday": 1, "weekend": 2, "national_holiday": 1}})
        ctx = svc.build_context("r", YM, src=src)
        assert [m.id for m in ctx.members] == ["A", "B"]


# ══ P1-02 正式文件不接受 partial success ═════════════════════════════════
class TestTheOfficialDocumentIsAllOrNothing:
    def test_the_export_refuses_an_unreadable_month(self, svc):
        with pytest.raises(ValueError, match="2026-08.json"):
            _corrupt_month(svc)
            svc.build_export(YM)

    def test_the_export_refuses_an_unreadable_template(self, svc):
        """★這一條原本被吞掉★:`build_day_input` 失敗 → day_grid={} →
        月曆整片空白,而「真的沒有開診」與「剛好讀失敗」在文件上一模一樣。"""
        _corrupt(svc, "clinic_template.json")
        with pytest.raises(ValueError, match="clinic_template.json"):
            svc.build_export(YM)

    def test_the_export_does_not_swallow_a_day_input_failure(
            self, svc, monkeypatch):
        """★這一條要用包裝【之外】的失敗才量得到★:宣告的檔壞掉時,
        `_sources()` 在更早的地方就擋下了 —— 那是另一條規則的功勞。
        這裡量的是「組日排班時出事,還照樣把半份文件交出去」。"""
        monkeypatch.setattr(svc, "build_day_input",
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("格網組不起來")))
        with pytest.raises(RuntimeError, match="格網組不起來"):
            svc.build_export(YM)

    def test_a_healthy_month_still_exports(self, svc):
        data = svc.build_export(YM)
        assert data["ym"] == YM and "day_grid" in data

    def test_the_finalize_archive_refuses_too(self, svc):
        _corrupt(svc, "config.json")
        with pytest.raises(ValueError, match="config.json"):
            svc.build_finalize_pdf_sections(YM)

    def test_the_export_button_turns_the_refusal_into_words(self):
        """★fail-closed 要有人話★:UI 若不接住,使用者按下匯出只會得到一個
        沒有訊息的 Tk traceback(而且看起來像當掉)。"""
        for rel in ("ui/duty.py", "ui/day_tab.py"):
            path = os.path.join(SRC_DIR, "cmuh_common", "roster", rel)
            tree = ast.parse(io.open(path, encoding="utf-8").read())
            guarded = set()
            for t in ast.walk(tree):
                if not isinstance(t, ast.Try) or not t.handlers:
                    continue
                for n in ast.walk(ast.Module(body=t.body, type_ignores=[])):
                    if (isinstance(n, ast.Call)
                            and getattr(n.func, "attr", "") == "build_export"):
                        guarded.add(id(n))
            calls = [n for n in ast.walk(tree)
                     if isinstance(n, ast.Call)
                     and getattr(n.func, "attr", "") == "build_export"]
            assert calls, f"{rel} 找不到匯出呼叫(判準失效)"
            assert all(id(c) in guarded for c in calls), \
                f"★{rel} 的匯出沒有接住讀取失敗★"


def _corrupt_month(svc) -> None:
    io.open(svc.storage._month_path(YM), "w", encoding="utf-8").write("{壞")


# ══ P1-03 報告的新鮮度 ══════════════════════════════════════════════════
class TestTheReportKnowsWhichRosterItDescribes:
    def _accept(self, svc):
        svc.accept_solution("r", YM, _result_for(svc, YM, _cover(svc, YM)))

    def test_a_fresh_report_says_so(self, svc):
        self._accept(svc)
        assert report_state(svc.storage.load_month(YM), "r") == REPORT_FRESH

    def test_one_manual_swap_makes_it_stale(self, svc):
        """★反例本體★:Auto Accept → 發現一天要換班 → `set_cell` 正確改了
        duty/audit/切片,★卻沒有動報告★;定案時帳本用新 duty 重算,報告仍是
        舊的 —— 留底 PDF 於是「班表=舊、帳本=新」。"""
        self._accept(svc)
        svc.set_cell("r", YM, date(2026, 8, 5), "B")
        assert report_state(svc.storage.load_month(YM), "r") == REPORT_STALE

    def test_a_legacy_report_is_unverifiable_not_fresh(self, svc):
        """★查不出來不可以說成沒事★:舊版程式存的報告沒有識別。"""
        m = svc.storage.load_month(YM)
        m["report_r"] = "舊版報告"
        svc.storage.save_month(YM, m)
        assert report_state(svc.storage.load_month(YM), "r") \
            == REPORT_UNVERIFIABLE

    def test_no_report_is_not_a_state(self, svc):
        assert report_state(svc.storage.load_month(YM), "r") == ""

    def test_the_day_report_follows_the_same_rule(self, svc):
        res = svc.run_day_solve(YM)
        svc.accept_day_solution(YM, res.day_slots, "日報告", expect=res)
        assert report_state(svc.storage.load_month(YM), "day") == REPORT_FRESH
        svc.set_day_slot(YM, date(2026, 8, 4), "上午", "101", ["P1"])
        assert report_state(svc.storage.load_month(YM), "day") == REPORT_STALE

    def test_clearing_the_report_takes_its_digest_with_it(self, svc):
        self._accept(svc)
        svc.clear_unlocked("r", YM)
        m = svc.storage.load_month(YM)
        assert not m.get("report_r") and "report_digest_r" not in m


class TestTheArchiveCarriesTheFinalState:
    def test_the_pdf_shows_the_person_who_actually_works_that_day(self, svc):
        """定案留底要印【最終班表】,不是初次求解的紀錄。"""
        svc.accept_solution("r", YM, _result_for(svc, YM, _cover(svc, YM)))
        svc.set_cell("r", YM, date(2026, 8, 5), "B")
        secs = svc.build_finalize_pdf_sections(YM)
        final = [b for t, b in secs if "最終班表" in t]
        assert final, "★沒有由正典狀態重建的最終班表段★"
        body = final[0]
        i = body.index("[最終班表]")
        line = [x for x in body[i:].splitlines() if "8/5" in x]
        assert line and "名B" in line[0], f"最終班表仍印著舊人選:{line}"

    def test_the_solver_report_is_labelled_when_it_no_longer_matches(
            self, svc):
        svc.accept_solution("r", YM, _result_for(svc, YM, _cover(svc, YM)))
        svc.set_cell("r", YM, date(2026, 8, 5), "B")
        secs = dict(svc.build_finalize_pdf_sections(YM))
        rpt = secs["R 排班決策報告"]
        assert "不代表最終班表" in rpt, "★過期的報告沒有被標示★"

    def test_a_fresh_report_gets_no_scary_note(self, svc):
        svc.accept_solution("r", YM, _result_for(svc, YM, _cover(svc, YM)))
        secs = dict(svc.build_finalize_pdf_sections(YM))
        assert "不代表最終班表" not in secs["R 排班決策報告"]

    def test_someone_off_the_roster_is_still_listed(self, svc):
        """★換班給已離開名單的人時,靜靜略過會讓留底文件少一天班★"""
        txt = build_final_state_report(
            year=2026, month=8, scope_label="R 排班",
            members=[], duty={date(2026, 8, 5): "離職者"}, holidays=set(),
            params=svc.build_context("r", YM).params, ledger={})
        assert "已不在目前名單" in txt
        # ★勝負要在【結算】那一段分★:班表那一行是另一條規則(逐日列出)——
        #   只看整份字串的話,把結算漏掉的突變照樣是綠的。
        assert "離職者" in txt.split("[結算]")[1]

    def test_the_display_note_is_wired(self, svc):
        svc.accept_solution("r", YM, _result_for(svc, YM, _cover(svc, YM)))
        svc.set_cell("r", YM, date(2026, 8, 5), "B")
        assert "不代表最終班表" in svc.report_for_display("r", YM)


# ══ P1-04 停診:展開與寫入同一個臨界區 ═══════════════════════════════════
class TestTheClosureExpansionIsInsideTheBarrier:
    def test_the_template_is_read_while_the_barrier_is_held(self, tmp_path):
        """★反例本體★:展開「哪幾天有開 101」是在臨界區外做的話,背景 pull
        可以在展開之後、寫入之前把他機新增的場次合併進來 —— 那些日子不會被
        寫進 closed_rooms,而 UI 回報「整段停診成功」。之後自動排班讀的是
        【現在的】模板,學生就被排進那個「已經停診」的診間。
        """
        depth = {"now": 0, "when_read": None}
        st = RosterStorage(str(tmp_path))
        st.save_config({"r_members": [], "vs_members": [], "pgy_members": []})
        st.save_month(YM, {})
        st.save_clinic_template({"template": {
            "1": {"上午": [{"room": "101", "doctor": "甲"}]}}})
        real_barrier, real_sources = st.write_barrier, st.strict_sources

        import contextlib

        @contextlib.contextmanager
        def _barrier():
            depth["now"] += 1
            try:
                with real_barrier():
                    yield
            finally:
                depth["now"] -= 1

        def _sources(*a, **kw):
            depth["when_read"] = depth["now"]
            return real_sources(*a, **kw)

        st.write_barrier = _barrier                          # type: ignore
        st.strict_sources = _sources                         # type: ignore
        RosterService(st).set_clinic_closed(
            YM, "101", date(2026, 8, 3), date(2026, 8, 31), ["上午"])
        assert depth["when_read"], \
            "★門診模板是在臨界區【外面】展開的(展開到寫入之間可被換檔)★"

    def test_a_corrupt_template_is_not_reported_as_no_clinic(self, svc):
        """★訊息要分得出處置不同的原因★:「模板上沒開診」要去改模板/日期,
        「模板讀不到」要去修檔案 —— 壓成同一句會讓人查錯方向。"""
        _corrupt(svc, "clinic_template.json")
        with pytest.raises(ValueError, match="clinic_template.json") as e:
            svc.set_clinic_closed(YM, "101", date(2026, 8, 3),
                                  date(2026, 8, 31), ["上午"])
        assert "沒有開診" not in str(e.value)

    def test_closing_still_works(self, svc):
        out = svc.set_clinic_closed(YM, "101", date(2026, 8, 3),
                                    date(2026, 8, 31), ["上午"])
        assert out["changed"] > 0
        # 模板的鍵是 weekday(週一=0)→ "1" 是週二;2026-08-04 才是週二。
        assert svc.clinic_closures(YM)["2026-08-04"]["上午"] == ["101"]


# ══ P2 意圖:來源沒改到就沒有債 ═══════════════════════════════════════════
class TestARefusedEditLeavesNoDebt:
    def test_a_finalized_month_refuses_the_leave_and_records_nothing(
            self, svc):
        """★反例本體★:義務必須在來源寫入【之前】記下(否則中途斷電就沒有
        線索),於是來源自己被拒時會留下一筆其實不存在的債 —— 它會擋住定案,
        而根本沒有東西需要收斂。"""
        svc.finalize(YM, True)
        with pytest.raises(FinalizedMonthError):
            svc.set_leaves("r", YM, "A", {date(2026, 8, 8)}, baseline=set())
        assert not svc.storage.load_pending_settles(), \
            "★留下了一筆不存在的債★"

    def test_a_real_failure_still_leaves_the_debt(self, svc, monkeypatch):
        """反向:來源真的改了、衍生物沒跟上 → 債要留著(不可以一起清掉)。"""
        monkeypatch.setattr(svc, "recompute_saturday_biopsy",
                            lambda *a, **k: (_ for _ in ()).throw(
                                RuntimeError("切片重排壞了")))
        svc.set_leaves("r", YM, "A", {date(2026, 8, 8)}, baseline=set())
        assert [x["ym"] for x in svc.storage.load_pending_settles()] == [YM]

    def test_the_witness_has_no_default(self):
        """★沒有預設值★:兩種答案各自對得起某些呼叫端 —— 給預設值的話,
        寫錯的那一端會安靜地丟掉一筆真的債。"""
        for fn in (RosterService.settle_intent, RosterService.biopsy_obligation,
                   RosterService._biopsy_intent):
            sig = inspect.signature(getattr(fn, "__wrapped__", fn))
            p = sig.parameters["witness_ym"]
            assert p.kind is inspect.Parameter.KEYWORD_ONLY
            assert p.default is inspect.Parameter.empty, fn

    def test_the_cross_month_obligation_does_not_need_its_month_to_change(
            self, svc, monkeypatch):
        """★跨月那條沒有 witness★:來源是【本月】的週五值班,在進入區塊之前
        就已經存好了 —— 下月月檔沒變不代表沒有債,那正好就是債本身。
        (2026 年只有 7/31 是「月底週五且翌日 1 號是週六」。)"""
        ym = "2026-07"
        svc.storage.save_month(ym, {"r_duty": {}})
        real = svc.recompute_saturday_biopsy

        def _only_next_fails(y, month=None, **kw):
            if y == YM:
                raise RuntimeError("下個月的切片重排壞了")
            return real(y, month, **kw)
        monkeypatch.setattr(svc, "recompute_saturday_biopsy", _only_next_fails)
        svc.set_cell("r", ym, date(2026, 7, 31), "A")
        assert YM in [x["ym"] for x in svc.storage.load_pending_settles()]

    def test_an_unmeasurable_witness_keeps_the_debt(self, svc, monkeypatch):
        """★量不到不可以當成沒事★:witness 讀不到時保留意圖(代價只是下次
        開程式多重算一次已經正確的衍生物)。"""
        monkeypatch.setattr(svc.storage, "month_revision",
                            lambda _ym: "<unreadable>")
        with pytest.raises(RuntimeError):
            with svc.settle_intent("r", YM, kind="biopsy", witness_ym=YM):
                raise RuntimeError("來源爆了")
        assert svc.storage.load_pending_settles()

    def test_the_clear_is_still_not_unconditional(self):
        """★守衛的守衛★:那個 except 分支必須是【有條件的】—— 無條件清掉
        等於回到「失敗時替它宣稱已經一致」。"""
        fn = ast.parse(textwrap.dedent(inspect.getsource(
            RosterService.settle_intent.__wrapped__))).body[0]
        handlers = [h for t in ast.walk(fn) if isinstance(t, ast.Try)
                    for h in t.handlers]
        assert handlers, "找不到那個 except(判準失效)"
        for h in handlers:
            clears = [c for c in ast.walk(ast.Module(body=h.body,
                                                     type_ignores=[]))
                      if isinstance(c, ast.Call)
                      and getattr(c.func, "attr", "") == "clear_pending_settle"]
            for c in clears:
                assert any(isinstance(n, ast.If) and c in list(ast.walk(n))
                           for n in ast.walk(ast.Module(body=h.body,
                                                        type_ignores=[]))), \
                    "★例外路徑無條件清掉意圖★"
        assert not [t for t in ast.walk(fn)
                    if isinstance(t, ast.Try) and t.finalbody], \
            "★清除不可以放在 finally★"


# ══ 來源宣告本身要被用到(不是註解裡的宣稱)═════════════════════════════
def test_every_authoritative_entry_point_declares_its_sources():
    """★這幾條路徑都必須拿權威輸入★ —— 少一條就是少一個 fail-open。"""
    tree = ast.parse(io.open(os.path.join(
        SRC_DIR, "cmuh_common", "roster", "service.py"),
        encoding="utf-8").read()).body
    cls = [n for n in tree if isinstance(n, ast.ClassDef)
           and n.name == "RosterService"][0]
    need = {"run_solve", "_accept_solution_locked", "_resettle_locked",
            "_accept_day_locked", "_build_export_locked",
            "build_finalize_pdf_sections", "_recompute_saturday_biopsy_locked",
            "set_clinic_closed"}
    seen = set()
    for fn in [n for n in cls.body if isinstance(n, ast.FunctionDef)]:
        if any(isinstance(c, ast.Call)
               and getattr(c.func, "attr", "") == "_sources"
               for c in ast.walk(fn)):
            seen.add(fn.name)
    assert need <= seen, f"★這些權威路徑沒有宣告來源★ {sorted(need - seen)}"


def test_the_source_sets_are_registered_canonical_files():
    """宣告的檔名必須真的是正典檔(打錯字會在執行時才炸,而且只在那條路徑)。"""
    for names in (svc_mod.SRC_RVS, svc_mod.SRC_DAY, svc_mod.SRC_BIOPSY,
                  svc_mod.SRC_CLOSURE, svc_mod.SRC_SETTLE, svc_mod.SRC_EXPORT):
        for n in names:
            assert n in RosterStorage.CANONICAL_FILES, n


# ══ 外審 R1 的三條修正 ═══════════════════════════════════════════════════
class TestTheReportNeverClaimsItsNumbersStillHold:
    """★識別只證明【班表】沒被改過★(外審 RS-19 R1-1):報告的結算數字還依賴
    點數規則/國定假日/名單/帳本結轉 —— 只改設定、不動班表時識別仍相符,
    舊寫法於是把它當成「fresh」原樣印出,而上方的最終班表用的是新規則。"""

    def _accept(self, svc):
        svc.accept_solution("r", YM, _result_for(svc, YM, _cover(svc, YM)))

    def test_a_fresh_report_still_says_its_numbers_are_historical(self, svc):
        self._accept(svc)
        secs = dict(svc.build_finalize_pdf_sections(YM))
        rpt = secs["R 排班決策報告"]
        assert "當初【自動求解】的紀錄" in rpt or "當初" in rpt
        assert "求解當時的設定" in rpt, "★沒有講清楚那些數字是歷史的★"
        assert "不代表最終班表" not in rpt, "班表沒被改過就不該說被改過"

    def test_changing_the_points_does_not_leave_the_old_numbers_unlabelled(
            self, svc):
        """★反例本體★:只改點數規則、一格都沒動 —— 值班格的識別完全相符。"""
        self._accept(svc)
        before = dict(svc.build_finalize_pdf_sections(YM))
        cfg = svc.storage.load_config()
        cfg["points"] = {"weekday": 5, "weekend": 9, "national_holiday": 5}
        svc.storage.save_config(cfg)
        after = dict(svc.build_finalize_pdf_sections(YM))
        assert report_state(svc.storage.load_month(YM), "r") == REPORT_FRESH, \
            "前提不成立:改點數也動到了值班格的識別,量不到這條規則"
        assert before["R 排班最終班表與結算"] != after["R 排班最終班表與結算"], \
            "前提不成立:最終班表根本沒跟著點數規則走"
        assert "求解當時的設定" in after["R 排班決策報告"], \
            "★舊的點數/帳本數字被當成現在仍成立的★"

    def test_the_display_says_the_same_thing(self, svc):
        self._accept(svc)
        assert "求解當時的設定" in svc.report_for_display("r", YM)

    def test_a_changed_roster_still_gets_its_own_sentence(self, svc):
        """兩種原因的處置不同 → 兩句話都要在(不可以壓成一句)。"""
        self._accept(svc)
        svc.set_cell("r", YM, date(2026, 8, 5), "B")
        rpt = dict(svc.build_finalize_pdf_sections(YM))["R 排班決策報告"]
        assert "求解當時的設定" in rpt and "不代表最終班表" in rpt


class TestTheArchiveSnapshotIsTakenAtFinalizeTime:
    """★留底快照要在定案的同一個臨界區裡取★(外審 RS-19 R1-2):產生 PDF 的
    背景工作可能要先下載安裝 reportlab —— 期間帳本(全域累計)會被別的月份/
    別台電腦改掉,而月檔因為已定案而唯讀,班表不會變、★餘額會★。"""

    def test_finalize_hands_back_the_sections(self, svc):
        svc.accept_solution("r", YM, _result_for(svc, YM, _cover(svc, YM)))
        secs = svc.finalize(YM, True)
        assert any("最終班表" in t for t, _b in secs)

    def test_a_later_ledger_change_does_not_leak_into_the_snapshot(self, svc):
        """定案之後別的月份結算 → 帳本餘額變了。定案當下取的那份不受影響。"""
        svc.accept_solution("r", YM, _result_for(svc, YM, _cover(svc, YM)))
        secs = dict(svc.finalize(YM, True))
        led = svc.storage.load_ledger()
        led["r"]["A"] = float(led["r"].get("A", 0.0)) + 99.0   # 別月結算
        svc.storage.save_ledger(led)
        later = dict(svc.build_finalize_pdf_sections(YM))
        assert secs["R 排班最終班表與結算"] \
            != later["R 排班最終班表與結算"], \
            "前提不成立:帳本餘額根本沒印在留底文件上"
        assert "+99" not in secs["R 排班最終班表與結算"] \
            and "99.0" not in secs["R 排班最終班表與結算"]

    def test_the_finalize_button_hands_the_snapshot_to_the_worker(self):
        """★接上去了才存在★:UI 若不把它傳下去,背景工作還是會自己重組。"""
        for rel in ("ui/duty.py", "ui/day_tab.py"):
            src = io.open(os.path.join(SRC_DIR, "cmuh_common", "roster", rel),
                          encoding="utf-8").read()
            tree = ast.parse(src)
            calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                     and getattr(n.func, "id", "") == "archive_finalize_pdf_async"]
            assert calls, f"{rel} 找不到留底呼叫(判準失效)"
            for c in calls:
                assert len(c.args) >= 4, f"★{rel} 沒有把定案當下的快照傳下去★"

    def test_the_archive_writes_the_handed_over_snapshot(self, svc,
                                                        monkeypatch):
        """交下來的那一份就是要寫進 PDF 的那一份 —— 現場再重組一次的話,
        前面在臨界區裡取快照的功夫就白費了。"""
        from cmuh_common.roster import export_pdf
        seen = {}
        monkeypatch.setattr(export_pdf, "export",
                            lambda path, secs: seen.setdefault("secs", secs))
        svc.archive_finalize_pdf(YM, [("標題", "內容")])
        assert seen.get("secs") == [("標題", "內容")],             "★沒有用交下來的那一份,現場又重組了★"

    def test_a_failed_snapshot_does_not_leave_the_month_finalized(self, svc):
        """★假失敗比原本的 bug 更糟★(外審 RS-19 R2-1):留底段落組不出來時
        例外會上拋,UI 說「定案失敗」並把勾選還原 —— 而磁碟上其實已經定案。
        使用者於是再按一次,面對的卻是一個已經唯讀的月份。
        (空月份不必重算帳本,所以壞掉的 config 不會在更早的地方擋下來。)"""
        _corrupt(svc, "config.json")
        with pytest.raises(ValueError, match="config.json"):
            svc.finalize(YM, True)
        assert not svc.storage.load_month(YM).get("finalized"),             "★回報失敗,月檔卻已經被標成定案(唯讀)★"

    def test_unfinalizing_needs_no_snapshot(self, svc):
        svc.accept_solution("r", YM, _result_for(svc, YM, _cover(svc, YM)))
        svc.finalize(YM, True)
        assert svc.finalize(YM, False) == []
