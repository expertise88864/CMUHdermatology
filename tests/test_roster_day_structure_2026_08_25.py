# -*- coding: utf-8 -*-
"""批次 RS-27:日排班的結構驗證(全審 2026-08-24 的 P1-02)。

手動編輯是「一次覆寫整個時段」的自由文字(頓號分隔代號),所以它可以造出
規則上不可能成立的班表 —— 而原本的六道檢查(請假、名單、週三下午、容量、
停診、一人多工)全部放行,一路通到定案與匯出。

★使用者 2026-08-25 定案★:
  * 手動編輯【只警告不擋存】(維持設計 §16.4 的一貫作風);
  * 定案【只擋結構錯誤】—— 定案是單向的(重算帳本 + 月檔唯讀),
    而「當日請假卻被排」那種是【要知道但可能正確】的現況,擋了會讓月結卡死。

規則出自設計文件,不是這裡發明的:
  P2 照光/治療室 = 每時段恰 1 位 PGY;C2 切片室 = 一個時段一個人(Clerk);
  RF-08 同日多梯重疊時,勝者＝原始順序第一個。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cmuh_common.roster.service import (  # noqa: E402
    DayStructureError, RosterService,
)
from cmuh_common.roster.solve_day import (  # noqa: E402
    BIOPSY, PHOTO, REST, TREATMENT,
)
from cmuh_common.roster.storage import RosterStorage  # noqa: E402

YM = "2026-08"
MON = "2026-08-03"          # 週一
WED = "2026-08-05"          # 週三


def _svc(tmp_path, *, day_slots=None, batches=None, month=None):
    """房容量 2、週一~五早上開 101/102;梯次 b1(C1/C2)自 8/03 起。"""
    st = RosterStorage(str(tmp_path))
    st.save_config({"pgy_members": [{"id": "P1"}, {"id": "P2"}],
                    "r_members": [], "vs_members": [], "room_capacity": 2})
    st.save_clinic_template({"template": {
        str(w): {"上午": [{"room": "101"}, {"room": "102"}], "下午": []}
        for w in range(5)}})
    st.save_clerk_batches(batches or [
        {"id": "b1", "start_monday": MON, "members": ["C1", "C2"]}])
    st.save_month(YM, dict(month or {}, day_slots=day_slots or {}))
    return RosterService(st)


def _slots(**kw):
    return {MON: {"上午": dict(kw)}}


# ══ 1. 照光/治療室:0~1 位、而且是本月 PGY ═══════════════════════════════
class TestThePgyOnlyRooms:
    @pytest.mark.parametrize("slot", [PHOTO, TREATMENT])
    def test_one_pgy_is_fine(self, tmp_path, slot):
        svc = _svc(tmp_path, day_slots=_slots(**{slot: ["P1"]}))
        assert svc.validate_day_structure(YM) == []

    @pytest.mark.parametrize("slot", [PHOTO, TREATMENT])
    def test_two_people_is_a_structural_error(self, tmp_path, slot):
        """★一間房同一個時段只有一個人用得到★(設計 P2:每時段恰 1 位)。

        ★反例只靠人數分勝負★:兩位都是【本月 PGY】,身分那一條擋不住它。
        """
        svc = _svc(tmp_path, day_slots=_slots(**{slot: ["P1", "P2"]}))
        msgs = svc.validate_day_structure(YM)
        assert any("只能有 1 位" in m and slot in m for m in msgs), msgs

    @pytest.mark.parametrize("slot", [PHOTO, TREATMENT])
    def test_a_clerk_in_a_pgy_room_is_a_structural_error(self, tmp_path, slot):
        """★「幾位」與「誰」都要查★:只查人數的話,照光排一位 Clerk 照樣過關
        —— 那一格的規則是「一位 PGY」,身分本來就是規則的一半。

        ★反例只靠身分分勝負★:只有【一個】人,人數那一條擋不住它。
        """
        svc = _svc(tmp_path, day_slots=_slots(**{slot: ["C1"]}))
        msgs = svc.validate_day_structure(YM)
        assert any("只能排本月 PGY" in m and slot in m for m in msgs), msgs

    def test_an_empty_room_is_not_an_error(self, tmp_path):
        """★0 位是合法的,不是「恰 1 位」★:週三下午治療室休診、RS-15 兩位
        PGY 月的二早/四下/五早、全員請假(設計 P6「不硬塞」)、切片室那一格
        今天沒開(C3)—— 都會留空。照字面把 P2 的「恰 1 位」寫成
        `len(members) != 1` 就會把這些正常月份全部判成結構錯誤,再也定不了案。

        ★反例裡真的有空格★:治療室與切片室都給空清單,這條規則才量得到。
        """
        svc = _svc(tmp_path, day_slots={
            WED: {"下午": {PHOTO: ["P1"], TREATMENT: [], BIOPSY: []}}})
        assert svc.validate_day_structure(YM) == []


# ══ 2. 切片室:0~1 位、而且是【當天勝者梯次】的 Clerk ═══════════════════
class TestTheBiopsyRoom:
    def test_one_clerk_from_the_batch_is_fine(self, tmp_path):
        svc = _svc(tmp_path, day_slots=_slots(**{BIOPSY: ["C1"]}))
        assert svc.validate_day_structure(YM) == []

    def test_two_clerks_is_a_structural_error(self, tmp_path):
        """C2:一個切片時段一個人。★反例只靠人數★ —— 兩位都在梯次名單裡。"""
        svc = _svc(tmp_path, day_slots=_slots(**{BIOPSY: ["C1", "C2"]}))
        msgs = svc.validate_day_structure(YM)
        assert any("一個切片時段一個人" in m for m in msgs), msgs

    def test_a_pgy_in_the_biopsy_room_is_a_structural_error(self, tmp_path):
        """★反例只靠身分★ —— 只有一個人,而且他是合法的本月 PGY。"""
        svc = _svc(tmp_path, day_slots=_slots(**{BIOPSY: ["P1"]}))
        msgs = svc.validate_day_structure(YM)
        assert any("切片室只能排該梯次的 Clerk" in m for m in msgs), msgs

    def test_the_loser_batchs_clerk_cannot_take_the_biopsy_room(self, tmp_path):
        """★勝者判準與求解器共用一份★(RF-08):同日被兩梯涵蓋時,只有原始
        順序第一個排得到人 —— 敗者梯次的 Clerk 那天根本不上班,把他排進
        切片室是結構錯誤。

        ★反例只靠「哪一梯做主」分勝負★:D1 確實是一位 Clerk、只有一個人,
        人數與「是不是 Clerk」兩條都擋不住它。
        """
        svc = _svc(tmp_path, day_slots=_slots(**{BIOPSY: ["D1"]}), batches=[
            {"id": "b1", "start_monday": MON, "members": ["C1", "C2"]},
            {"id": "b2", "start_monday": MON, "members": ["D1", "D2"]}])
        msgs = svc.validate_day_structure(YM)
        assert any("切片室只能排該梯次的 Clerk" in m and "b1" in m
                   for m in msgs), msgs

    def test_the_winner_batchs_clerk_is_accepted(self, tmp_path):
        """★同一組輸入、只換人★:證明上一條量到的是勝者判準,不是「有兩梯
        就一律報錯」。"""
        svc = _svc(tmp_path, day_slots=_slots(**{BIOPSY: ["C1"]}), batches=[
            {"id": "b1", "start_monday": MON, "members": ["C1", "C2"]},
            {"id": "b2", "start_monday": MON, "members": ["D1", "D2"]}])
        assert svc.validate_day_structure(YM) == []


# ══ 3. 一人多工 / 房容量(既有規則,改由同一份實作提供)═══════════════════
class TestTheSharedChecksMovedIn:
    def test_one_person_two_jobs(self, tmp_path):
        svc = _svc(tmp_path,
                   day_slots=_slots(**{PHOTO: ["P1"], TREATMENT: ["P1"]}))
        msgs = svc.validate_day_structure(YM)
        assert any("同時被排在" in m for m in msgs), msgs

    def test_resting_while_working_is_still_a_contradiction(self, tmp_path):
        """放假格可以多人,但「又放假又有工作」一樣是矛盾。"""
        svc = _svc(tmp_path,
                   day_slots=_slots(**{PHOTO: ["P1"], REST: ["P1", "P2"]}))
        msgs = svc.validate_day_structure(YM)
        assert any("同時被排在" in m and "P1" in m for m in msgs), msgs
        assert not any("P2" in m for m in msgs), "只放假的人被誤報了"

    def test_room_over_capacity(self, tmp_path):
        svc = _svc(tmp_path, day_slots=_slots(**{"101": ["P1", "P2", "C1"]}))
        msgs = svc.validate_day_structure(YM)
        assert any("超過容量" in m for m in msgs), msgs

    def test_the_warning_panel_shows_them_too(self, tmp_path):
        """★面板上看到的與擋定案的必須是同一套判準★:不然使用者會遇到
        「面板沒說什麼,定案卻不讓過」。"""
        svc = _svc(tmp_path, day_slots=_slots(**{BIOPSY: ["C1", "C2"]}))
        assert any("一個切片時段一個人" in m
                   for m in svc.quick_validate_day(YM))


# ══ 4. 定案閘門 ═════════════════════════════════════════════════════════
class TestFinalizeIsGated:
    def test_a_structural_error_blocks_finalize(self, tmp_path):
        svc = _svc(tmp_path, day_slots=_slots(**{BIOPSY: ["C1", "C2"]}))
        with pytest.raises(DayStructureError) as ei:
            svc.finalize(YM, True)
        assert "一個切片時段一個人" in str(ei.value)
        assert not svc.storage.load_month(YM).get("finalized"), "竟然定案了"

    def test_a_clean_month_still_finalizes(self, tmp_path):
        """★閘門不可以把好月份也擋掉★ —— 與上一條只差切片室的人數。"""
        svc = _svc(tmp_path, day_slots=_slots(**{BIOPSY: ["C1"]}))
        svc.finalize(YM, True)
        assert svc.storage.load_month(YM).get("finalized")

    def test_a_non_structural_warning_does_not_block(self, tmp_path):
        """★只擋結構錯誤★(使用者定案):當日請假卻被排是【要知道但可能
        正確】的現況(換班、補班),擋了會讓月結卡死。"""
        svc = _svc(tmp_path, day_slots=_slots(**{PHOTO: ["P1"]}),
                   month={"leaves": {"pgy": {"P1": [MON]}}})
        assert any("請假" in m for m in svc.quick_validate_day(YM))
        svc.finalize(YM, True)
        assert svc.storage.load_month(YM).get("finalized")

    def test_unfinalizing_is_never_blocked(self, tmp_path):
        """★出口不可以被閘門鎖死★:已經定案的月份若被判定有結構錯誤,
        解除定案正是唯一的修法 —— 擋住它等於把使用者關在裡面。"""
        svc = _svc(tmp_path, day_slots=_slots(**{BIOPSY: ["C1"]}))
        svc.finalize(YM, True)
        m = svc.storage.load_month(YM)
        m["day_slots"][MON]["上午"][BIOPSY] = ["C1", "C2"]
        svc.storage.save_month(YM, m, force=True)
        svc.finalize(YM, False)
        assert not svc.storage.load_month(YM).get("finalized")

    def test_a_month_without_day_slots_does_not_read_day_sources(
            self, tmp_path, monkeypatch):
        """★新增的守衛不可以自己帶來新的失效模式★:定案本來不碰門診模板/
        梯次/切片格網。只有 R/VS 值班的月份不該因為那些檔案而定不了案。"""
        svc = _svc(tmp_path, day_slots={})

        def _boom(*a, **k):
            raise AssertionError("沒有日排班還去建求解輸入")
        monkeypatch.setattr(svc, "build_day_input", _boom)
        assert svc.validate_day_structure(YM) == []
        svc.finalize(YM, True)
        assert svc.storage.load_month(YM).get("finalized")


# ══ 5. 外審第 1 輪 P1:他月的殘留鍵不歸這個月管 ═══════════════════════════
class TestCrossMonthResidueIsNotThisMonthsProblem:
    """★閘門不可以沒有出口★(外審 RS-27 R1 P1)。

    `set_day_slot` 不強制 `d ∈ ym`,所以月檔可能殘留他月的 iso 鍵(舊檔、
    手改 JSON、跨機合併)。報告、匯出、週期統計三處都各自把非本月鍵濾掉
    —— 而本月的 UI 也只列得出本月的日期。拿他月的殘留擋住定案,使用者在
    程式裡沒有任何辦法修好它,只能手改 JSON。
    """

    RESIDUE = {"2026-09-01": {"上午": {BIOPSY: ["C1", "C2"]}}}

    def test_it_does_not_produce_a_structural_error(self, tmp_path):
        svc = _svc(tmp_path, day_slots=dict(
            _slots(**{BIOPSY: ["C1"]}), **self.RESIDUE))
        assert svc.validate_day_structure(YM) == []

    def test_it_does_not_block_finalize(self, tmp_path):
        svc = _svc(tmp_path, day_slots=dict(
            _slots(**{BIOPSY: ["C1"]}), **self.RESIDUE))
        svc.finalize(YM, True)
        assert svc.storage.load_month(YM).get("finalized")

    def test_the_same_defect_inside_the_month_still_blocks(self, tmp_path):
        """★反例只靠「是不是本月」分勝負★:一模一樣的違規內容,只是把日期
        從 9/01 換成 8/03 —— 這一次必須擋。"""
        svc = _svc(tmp_path, day_slots={
            MON: {"上午": {BIOPSY: ["C1", "C2"]}}})
        with pytest.raises(DayStructureError):
            svc.finalize(YM, True)
