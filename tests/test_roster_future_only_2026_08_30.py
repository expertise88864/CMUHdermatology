# -*- coding: utf-8 -*-
"""[批次RS-32a / 2026-08-30 使用者] 自動排班只排【明天起】(PGY/Clerk 日排班)。

「排班只針對今天以後(不含今天):按下自動排班時,今天(含)之前自動鎖定
 (但使用者沒按畫面上的鎖定的話仍可手動編輯);只安排明天開始的排班,
 但要參考今天(含)以前的資料來做未來排班。」

實作:`build_day_input(today=...)` 把格網內 d ≤ today 的每個時段以【目前
day_slots 內容】併入 `locked`(含空時段)。`locked` 的既有語意一次給齊三件事:
原樣保留不重排、回放進公平計數(照光/跟診/切片/RS-31 週別跟診)、
配額分母把過去沒排到的開放時段拿掉。★只在求解器輸入層,不寫 `day_locks`★
—— UI 的可編輯性不變。`today=None`(預設)= 行為不變;真時鐘只在 UI 按鈕
進入(`date.today()`),service/測試一律注入固定日期 —— 它進了輸入就進了
指紋,跨日之後舊預覽會被 stale 閘門擋下。
"""
import ast
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.solve_day import (  # noqa: E402
    BIOPSY, PHOTO, _jitter, day_input_fingerprint, month_solve_day,
)
from cmuh_common.roster.storage import RosterStorage  # noqa: E402

YM = "2026-08"
TUE = date(2026, 8, 25)
WED = date(2026, 8, 26)          # ← 注入的「今天」
THU = date(2026, 8, 27)
FRI = date(2026, 8, 28)


def _svc(tmp_path, *, pgy=("P1", "P2"), day_slots=None, day_locks=None,
         opens=None):
    st = RosterStorage(str(tmp_path))
    st.save_config({"pgy_members": [{"id": c} for c in pgy],
                    "r_members": [], "vs_members": [], "room_capacity": 2})
    st.save_clinic_template({"template": {
        str(w): {"上午": [{"room": "101"}], "下午": [{"room": "101"}]}
        for w in range(5)}})
    st.save_clerk_batches([{"id": "b1", "start_monday": "2026-08-17",
                            "members": ["C1", "C2"]}])
    st.save_biopsy_grid(opens or {})
    m = {"day_slots": day_slots or {}}
    if day_locks:
        m["day_locks"] = day_locks
    st.save_month(YM, m)
    return RosterService(st)


# ══ ① 輸入層:今天(含)以前 → 併入 locked ═════════════════════════════════
class TestPastDaysBecomeLockedInput:
    def test_every_past_session_is_locked_with_its_current_content(
            self, tmp_path):
        svc = _svc(tmp_path, day_slots={
            TUE.isoformat(): {"上午": {PHOTO: ["P1"]}}})
        inp = svc.build_day_input(YM, today=WED)
        assert inp.locked[TUE.isoformat()]["上午"] == {PHOTO: ["P1"]}
        # ★空時段也要鎖(鎖成 {})★:過去沒排到的就是沒排到
        assert inp.locked[TUE.isoformat()]["下午"] == {}
        assert inp.locked[WED.isoformat()]["上午"] == {}      # 今天(含)
        assert THU.isoformat() not in inp.locked              # 明天起可排

    def test_a_user_lock_on_a_future_day_is_untouched(self, tmp_path):
        """使用者自己鎖的未來時段照舊(兩種鎖並存)。"""
        svc = _svc(tmp_path,
                   day_slots={THU.isoformat(): {"上午": {PHOTO: ["P2"]}}},
                   day_locks={THU.isoformat(): {"上午": True}})
        inp = svc.build_day_input(YM, today=WED)
        assert inp.locked[THU.isoformat()]["上午"] == {PHOTO: ["P2"]}
        assert "下午" not in inp.locked.get(THU.isoformat(), {})

    def test_no_today_means_no_change(self, tmp_path):
        """★today=None(預設)= 行為不變★:locked 只有使用者的鎖。"""
        svc = _svc(tmp_path,
                   day_slots={TUE.isoformat(): {"上午": {PHOTO: ["P1"]}}},
                   day_locks={TUE.isoformat(): {"上午": True}})
        inp = svc.build_day_input(YM)
        assert inp.locked == {TUE.isoformat(): {"上午": {PHOTO: ["P1"]}}}

    def test_the_cutoff_enters_the_fingerprint(self, tmp_path):
        """★時鐘進了輸入就要進指紋★:同一份資料、不同的 today → 不同指紋
        (跨日之後舊預覽必須被 stale 閘門擋下)。"""
        svc = _svc(tmp_path)
        f1 = day_input_fingerprint(svc.build_day_input(YM, today=WED))
        f2 = day_input_fingerprint(svc.build_day_input(YM, today=THU))
        assert f1 != f2

    def test_past_content_enters_the_fingerprint_too(self, tmp_path):
        """過去的內容也是輸入:預覽開著時有人手動改了昨天 → 指紋要變。"""
        svc = _svc(tmp_path)
        f1 = day_input_fingerprint(svc.build_day_input(YM, today=WED))
        svc.storage.save_month(YM, {"day_slots": {
            TUE.isoformat(): {"上午": {PHOTO: ["P9"]}}}})
        f2 = day_input_fingerprint(svc.build_day_input(YM, today=WED))
        assert f1 != f2


# ══ ② 求解層:保留過去、只排未來、參考過去 ═══════════════════════════════
class TestTheSolverKeepsThePastAndSchedulesTheFuture:
    def test_past_days_are_kept_verbatim_and_future_gets_scheduled(
            self, tmp_path):
        weird = {PHOTO: ["P1"], "101": ["P1"]}    # 同一人兩件事:自動排班絕不會
        svc = _svc(tmp_path, day_slots={TUE.isoformat(): {"上午": weird}})
        ds, _l, _w = month_solve_day(svc.build_day_input(YM, today=WED))
        assert ds[TUE.isoformat()]["上午"] == weird, "★過去沒有被原樣保留★"
        assert ds[WED.isoformat()]["上午"] == {}, "★今天(含)不可被排★"
        assert ds[THU.isoformat()]["上午"].get(PHOTO), "★明天起要照排★"

    def test_the_future_is_steered_by_the_past(self, tmp_path):
        """★「要參考今天(含)以前的資料」★:過去四個時段照光全是 P2 →
        明天第一個時段的照光必須是 P1(次數平衡看得到過去)。

        ★反例的鑑別力★:8/27 上午的抖動偏向 P2 —— 不併過去的話,求解器
        從頭排、兩人平手,抖動會選 P2;併了才會是 P1(測試內驗明)。
        """
        assert (_jitter(THU, "上午", "photo", "P2")
                < _jitter(THU, "上午", "photo", "P1")), "反例失去鑑別力"
        svc = _svc(tmp_path, day_slots={
            TUE.isoformat(): {"上午": {PHOTO: ["P2"]}, "下午": {PHOTO: ["P2"]}},
            WED.isoformat(): {"上午": {PHOTO: ["P2"]}, "下午": {PHOTO: ["P2"]}},
        })
        ds, _l, _w = month_solve_day(svc.build_day_input(YM, today=WED))
        assert ds[THU.isoformat()]["上午"][PHOTO] == ["P1"], (
            ds[THU.isoformat()]["上午"])

    def test_a_missed_past_biopsy_slot_is_not_backfilled(self, tmp_path):
        """過去開了切片但沒排到 → ★不可以回頭補★;未來的照排。
        (對照組:不帶 today 時兩格都排。)"""
        opens = {"b1": {TUE.isoformat(): {"上午": True},
                        FRI.isoformat(): {"上午": True}}}
        svc = _svc(tmp_path, opens=opens)
        ds, _l, _w = month_solve_day(svc.build_day_input(YM, today=WED))
        assert BIOPSY not in ds[TUE.isoformat()]["上午"], "★過去被回頭補了★"
        assert len(ds[FRI.isoformat()]["上午"].get(BIOPSY) or []) == 1
        ds2, _l2, _w2 = month_solve_day(svc.build_day_input(YM))
        assert (ds2[TUE.isoformat()]["上午"].get(BIOPSY)
                and ds2[FRI.isoformat()]["上午"].get(BIOPSY)), "對照組失效"

    def test_past_biopsy_content_pre_deducts_that_clerk(self, tmp_path):
        """過去 C1 已切過 → 配額看得到 → 未來那格輪給 C2。"""
        opens = {"b1": {TUE.isoformat(): {"上午": True},
                        FRI.isoformat(): {"上午": True}}}
        svc = _svc(tmp_path, opens=opens, day_slots={
            TUE.isoformat(): {"上午": {BIOPSY: ["C1"]}}})
        ds, _l, _w = month_solve_day(svc.build_day_input(YM, today=WED))
        assert ds[FRI.isoformat()]["上午"][BIOPSY] == ["C2"], (
            ds[FRI.isoformat()]["上午"])

    def test_a_past_trio_follow_feeds_the_weekly_rotation(self, tmp_path):
        """[RS-31 銜接] 過去的二早跟診(P1)要進週別計數 → 週四午的三時段
        照光必是 P1、跟診輪給 P2(預掃讀的就是 locked,自動接上)。"""
        svc = _svc(tmp_path, day_slots={
            TUE.isoformat(): {"上午": {PHOTO: ["P2"], "101": ["P1"]}}})
        ds, _l, _w = month_solve_day(svc.build_day_input(YM, today=WED))
        thu_pm = ds[THU.isoformat()]["下午"]
        assert thu_pm[PHOTO] == ["P1"], thu_pm
        # 房內還會有 Clerk(容量 2)—— 斷言只看 P2 有沒有入座
        assert "P2" in (thu_pm.get("101") or []), thu_pm

    def test_a_wholly_past_month_is_returned_verbatim(self, tmp_path):
        """整個月都在過去 → 自動排班等於 no-op(現況原樣回傳,不加不減)。"""
        existing = {date(2026, 8, 5).isoformat(): {"上午": {PHOTO: ["P9"]}}}
        svc = _svc(tmp_path, day_slots=existing)
        ds, _l, _w = month_solve_day(
            svc.build_day_input(YM, today=date(2026, 9, 15)))
        for iso, sessions in ds.items():
            for session, slots in sessions.items():
                assert slots == (existing.get(iso, {}).get(session) or {}), (
                    iso, session, slots)


# ══ ③ 套用閘門:跨日的舊預覽要被擋 ═══════════════════════════════════════
class TestApplyingAStalePreviewIsRejected:
    def test_a_preview_from_yesterday_cannot_be_applied_today(self, tmp_path):
        import pytest
        svc = _svc(tmp_path)
        res = svc.run_day_solve(YM, today=WED)
        with pytest.raises(ValueError, match="過期"):
            svc.accept_day_solution(YM, res.day_slots, expect=res, today=THU)

    def test_the_same_day_applies_cleanly(self, tmp_path):
        svc = _svc(tmp_path)
        res = svc.run_day_solve(YM, today=WED)
        svc.accept_day_solution(YM, res.day_slots, expect=res, today=WED)
        saved = svc.storage.load_month(YM)["day_slots"]
        assert saved[THU.isoformat()]["上午"].get(PHOTO), "套用後未來有排"
        # ★今天(含)以前經過 run_day_solve → accept 全程保持原樣(這裡是空)★
        #   —— run_day_solve 沒把 today 傳下去的話,今天就會被排到。
        assert saved[WED.isoformat()]["上午"] == {}, saved[WED.isoformat()]
        # ★不寫 day_locks★:自動保留是求解器層的,不影響 UI 可編輯性
        assert not svc.storage.load_month(YM).get("day_locks"), (
            "★自動保留被寫成了使用者鎖定★")


# ══ ④ 佈線:真時鐘只在 UI 進入,而且兩個呼叫點都要帶 ═════════════════════
def test_the_ui_passes_today_at_both_call_sites():
    """★沒有呼叫端的功能等於不存在★:UI 的自動排班與套用都要帶 today
    (漏了套用那一端的話,跨日的舊預覽會直接落地)。"""
    p = os.path.join(os.path.dirname(__file__), "..", "src", "cmuh_common",
                     "roster", "ui", "day_tab.py")
    with open(p, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    for wanted in ("run_day_solve", "accept_day_solution"):
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == wanted]
        assert calls, f"找不到 {wanted} 的呼叫(測試失效)"
        for c in calls:
            assert any(k.arg == "today" for k in c.keywords), (
                f"★UI 的 {wanted} 沒帶 today★")
