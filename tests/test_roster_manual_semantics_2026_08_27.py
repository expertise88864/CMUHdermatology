# -*- coding: utf-8 -*-
"""批次 RS-30:手動路徑要知道自動排班知道的事(全審 2026-08-24 的 P2-03/P2-04)。

RS-27 的結構驗證刻意只管【幾位、什麼身分】,而自動排班還知道另外三件事,
手動編輯完全不知道:
  P2-03 同一天被多梯涵蓋時,只有【原始順序第一個】那一梯排得到人(RF-08)
        —— 而「＋選人」的候選清單與 `quick_validate_day` 的名單檢查都在做
        covering 梯次的★聯集★,敗者梯次的 Clerk 可以被選進跟診診間、
        而且一句警告都不會有。
  P2-04 ①切片室那一格今天有沒有開放(開放格網是手動維護的);
        ②RS-15 兩位 PGY 月的二早/四下/五早治療室不排。

依使用者 2026-08-25 定案,這些★只警告、不擋存也不擋定案★
(它們是排班規則,不是「規則上不可能成立」的結構)。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.solve_day import BIOPSY, TREATMENT  # noqa: E402
from cmuh_common.roster.storage import RosterStorage  # noqa: E402

YM = "2026-08"
MON = date(2026, 8, 3)           # 週一
TUE = date(2026, 8, 4)           # 週二(RS-15 的「二早」)


def _svc(tmp_path, *, pgy=("P1", "P2", "P3"), batches=None, opens=None,
         day_slots=None):
    st = RosterStorage(str(tmp_path))
    st.save_config({"pgy_members": [{"id": c} for c in pgy],
                    "r_members": [], "vs_members": [], "room_capacity": 2})
    st.save_clinic_template({"template": {
        str(w): {"上午": [{"room": "101"}], "下午": [{"room": "101"}]}
        for w in range(5)}})
    st.save_clerk_batches(batches or [
        {"id": "b1", "start_monday": MON.isoformat(), "members": ["C1", "C2"]}])
    st.save_biopsy_grid(opens if opens is not None else
                        {"b1": {MON.isoformat(): {"上午": True}}})
    st.save_month(YM, {"day_slots": day_slots or {}})
    return RosterService(st)


_TWO_BATCHES = [
    {"id": "b1", "start_monday": MON.isoformat(), "members": ["C1", "C2"]},
    {"id": "b2", "start_monday": MON.isoformat(), "members": ["D1", "D2"]},
]


# ══ P2-03 勝者判準要貫穿手動路徑 ═════════════════════════════════════════
class TestTheManualPathUsesTheWinnerBatch:
    def test_a_loser_batch_clerk_in_a_clinic_room_is_reported(self, tmp_path):
        """★敗者梯次的 Clerk 那天根本不上班★:自動排班只排 b1,而手動把 b2 的
        D1 排進 101 診 —— 結構驗證只管特別格與容量,所以完全不會被點名。

        ★反例只靠勝者判準分勝負★:D1 是一位合法的 Clerk、人數沒超過容量、
        也沒請假 —— 差別只在他那天不歸這一梯做主。
        """
        svc = _svc(tmp_path, batches=_TWO_BATCHES, day_slots={
            MON.isoformat(): {"上午": {"101": ["D1"]}}})
        assert any("D1" in m and "不在當日 PGY 名單/梯次" in m
                   for m in svc.quick_validate_day(YM))

    def test_the_winner_batch_clerk_is_accepted(self, tmp_path):
        """★對照組★:同樣的位置換成勝者梯次的 C1 → 不可以說他不在名單。

        (斷言只看【這一條規則】的那句話:同一份面板還會有 RS-28 的配額點名
         「切片室輪不到」—— 那是另一條規則,拿它來當這裡的訊號會誤判。)
        """
        svc = _svc(tmp_path, batches=_TWO_BATCHES, day_slots={
            MON.isoformat(): {"上午": {"101": ["C1"]}}})
        assert not [m for m in svc.quick_validate_day(YM)
                    if "不在當日 PGY 名單/梯次" in m]

    def test_the_candidate_list_offers_only_the_winner_batch(self, tmp_path):
        """★「＋選人」不可以邀請使用者去排一個排不到的人★:候選清單與
        名單檢查必須同源,否則 UI 讓你選、面板再罵你。"""
        import ast
        import inspect
        p = os.path.join(os.path.dirname(__file__), "..", "src", "cmuh_common",
                         "roster", "ui", "day_tab.py")
        with open(p, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "_load_candidates")
        calls = {c.func.id for c in ast.walk(fn)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}
        assert "day_owner_batch" in calls, "候選清單沒有走勝者判準"
        src = inspect.getsource  # noqa: F841 - 只是讓意圖明顯
        assert not [n for n in ast.walk(fn)
                    if isinstance(n, ast.Attribute) and n.attr == "covers"], (
            "候選清單還在對 covering 梯次做聯集")


# ══ P2-04 切片室今天開不開 ═══════════════════════════════════════════════
class TestABiopsyOnAClosedSessionIsReported:
    def test_a_biopsy_on_a_session_that_is_not_open(self, tmp_path):
        """開放格網只勾了【上午】,手動把 C1 排進【下午】的切片室 ——
        自動排班永遠不會這樣排,而結構驗證看不出來(人數 1、身分是勝者
        梯次的 Clerk,兩條都合法)。"""
        svc = _svc(tmp_path, day_slots={
            MON.isoformat(): {"下午": {BIOPSY: ["C1"]}}})
        assert any("切片室今天沒有開放" in m for m in svc.quick_validate_day(YM))

    def test_an_open_session_is_silent(self, tmp_path):
        """★對照組★:同一個人、同一梯,排在有開放的上午 → 不該有這句。"""
        svc = _svc(tmp_path, day_slots={
            MON.isoformat(): {"上午": {BIOPSY: ["C1"]}}})
        assert not any("切片室今天沒有開放" in m
                       for m in svc.quick_validate_day(YM))

    def test_the_openness_comes_from_the_winner_batch(self, tmp_path):
        """★開放與否向勝者梯次拿★(RF-08 / RS-26 P2-01):b2 是敗者,
        它把下午勾開不算數 —— 勝者 b1 沒開,就是沒開。"""
        svc = _svc(tmp_path, batches=_TWO_BATCHES,
                   opens={"b1": {MON.isoformat(): {"上午": True}},
                          "b2": {MON.isoformat(): {"下午": True}}},
                   day_slots={MON.isoformat(): {"下午": {BIOPSY: ["C1"]}}})
        assert any("切片室今天沒有開放" in m for m in svc.quick_validate_day(YM))

    def test_a_day_with_no_batch_is_not_open(self, tmp_path):
        """沒有梯次做主的日子不可能有切片(那天沒有 Clerk 上班)。"""
        far = date(2026, 8, 31)          # b1 是 8/03~8/16,涵蓋不到
        svc = _svc(tmp_path, day_slots={
            far.isoformat(): {"上午": {BIOPSY: ["C1"]}}})
        assert any("切片室今天沒有開放" in m for m in svc.quick_validate_day(YM))


    def test_a_holiday_is_not_open_even_if_the_grid_says_so(self, tmp_path):
        """★吃「真的排得到的時段」而不是原始格網★(外審 RS-30 R1 P2)。

        切片格網的 UI 允許勾選所有平日,而那一天後來被設成國定假日 ——
        求解器早就用 `batch_biopsy_slots()` 把假日濾掉了(那天根本不在格網裡),
        手動路徑若只看格網的勾選就會說「有開放」,兩邊又各說各話。

        ★反例只靠「有效 vs 原始」分勝負★:格網明明是 True、梯次也是勝者、
        人也是那一梯的 Clerk —— 差別只在那一天是假日。
        """
        hol = date(2026, 8, 5)           # 週三;設成國定假日
        svc = _svc(tmp_path,
                   opens={"b1": {hol.isoformat(): {"上午": True}}},
                   day_slots={hol.isoformat(): {"上午": {BIOPSY: ["C1"]}}})
        # ★用生產的呼叫形狀★:`save_holiday_duty` 吃的是
        #   {"r": {日期: 值班者}, "vs": {...}}(scope 在外層)。
        #   我第一版寫成 {日期: {...}} → 假日根本沒進去,測試紅得莫名其妙。
        svc.storage.save_holiday_duty({"r": {hol.isoformat(): "R1"}, "vs": {}})
        assert any("切片室今天沒有開放" in m
                   for m in svc.quick_validate_day(YM))


# ══ P2-04 RS-15 兩位 PGY 月的治療室 ══════════════════════════════════════
class TestTheTwoPgyTreatmentSessions:
    def test_a_treatment_on_a_photo_only_session_is_reported(self, tmp_path):
        """★兩位 PGY 月的二早/四下/五早只排照光★(RS-15):自動排班知道,
        手動編輯不知道 —— 而 `quick_validate_day` 原本只有週三下午的特例。"""
        svc = _svc(tmp_path, pgy=("P1", "P2"), day_slots={
            TUE.isoformat(): {"上午": {TREATMENT: ["P1"]}}})
        assert any("不排治療室" in m for m in svc.quick_validate_day(YM))

    def test_three_pgy_is_silent(self, tmp_path):
        """★反例只靠「恰 2 位」分勝負★:同一天同一格,名單三個人 →
        RS-15 根本不適用,不可以誤報。"""
        svc = _svc(tmp_path, pgy=("P1", "P2", "P3"), day_slots={
            TUE.isoformat(): {"上午": {TREATMENT: ["P1"]}}})
        assert not any("不排治療室" in m for m in svc.quick_validate_day(YM))

    def test_a_normal_session_is_silent(self, tmp_path):
        """★反例只靠「是不是那三個時段」分勝負★:同樣兩位 PGY,
        週一上午不在 RS-15 的清單裡 → 照排不誤。"""
        svc = _svc(tmp_path, pgy=("P1", "P2"), day_slots={
            MON.isoformat(): {"上午": {TREATMENT: ["P1"]}}})
        assert not any("不排治療室" in m for m in svc.quick_validate_day(YM))


# ══ 使用者定案:這些只警告,不擋存也不擋定案 ═══════════════════════════════
def test_these_warnings_never_block_finalize(tmp_path):
    """★使用者 2026-08-25 定案:定案只擋【結構錯誤】★ —— 這一批加的是
    排班規則的語意警告,不可以升級成定案閘門(那會讓月結卡死)。"""
    svc = _svc(tmp_path, pgy=("P1", "P2"), day_slots={
        MON.isoformat(): {"下午": {BIOPSY: ["C1"]}},
        TUE.isoformat(): {"上午": {TREATMENT: ["P1"]}}})
    warns = svc.quick_validate_day(YM)
    assert any("切片室今天沒有開放" in m for m in warns)
    assert any("不排治療室" in m for m in warns)
    assert svc.validate_day_structure(YM) == [], "這些不是結構錯誤"
    svc.finalize(YM, True)
    assert svc.storage.load_month(YM).get("finalized")

