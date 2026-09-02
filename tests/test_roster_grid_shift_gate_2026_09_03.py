# -*- coding: utf-8 -*-
"""[R6-P2-01] 切片格網的平移意圖沒收斂 → 求解/接受/匯出/定案全都照跑。

外審第六輪:`reconcile_pending_grid_shifts()` 在開程式時跑,而且是
★「不擋開啟」★的 —— 讀不到檔、或格網被人手工編過而對不上新舊任何一邊時,
意圖會留著,然後那一梯的切片格子★落在梯次涵蓋範圍外而被直接忽略★:
切片室整梯看起來沒開,而畫面上完全看不出來。

★這道閘門的形狀(與 RS-27 同一個作風)★
* 面板:提醒(手動編輯不擋 —— 編輯中的班表本來就會經過中間狀態);
* 定案:★擋★(單向操作:重算帳本 + 月檔唯讀,帶著錯的班表定案下去只能
  靠解除定案才救得回來)。

★出口★(這個 repo 造過一次沒有出口的閘門,不要再造第二次):
`require_grid_shifts_reconciled()` 與 `finalize()` 都會★先自己收斂一次★ ——
使用者把起始日或格網改對之後,不必重開程式就能過關。
"""
import json
import os
import sys
import tempfile
from datetime import date, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.service import (  # noqa: E402
    PendingGridShiftError, RosterService,
)
from cmuh_common.roster.storage import RosterStorage  # noqa: E402

YM = "2026-09"
MON = date(2026, 9, 7)          # 週一
OLD = date(2026, 8, 31)         # 「改起始日之前」的那一週


def _svc(tmp_path, *, start=MON, grid_days=None, pending=True,
         old_start=OLD, new_start=MON, corrupt=False):
    st = RosterStorage(str(tmp_path))
    st.save_config({"pgy_members": [{"id": "P1"}], "r_members": [],
                    "vs_members": [], "room_capacity": 2})
    st.save_clinic_template({"template": {
        str(w): {"上午": [{"room": "101"}], "下午": [{"room": "101"}]}
        for w in range(5)}})
    st.save_clerk_batches([{"id": "b1", "start_monday": start.isoformat(),
                            "members": ["C1", "C2"]}])
    days = grid_days if grid_days is not None else [OLD]
    st.save_biopsy_grid({"b1": {d.isoformat(): {"上午": True} for d in days}})
    st.save_month(YM, {})
    path = os.path.join(str(tmp_path), "pending_grid_shift.json")
    if corrupt:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("{ 這不是 JSON")
    elif pending:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"pending": [{"batch_id": "b1",
                                    "old_start": old_start.isoformat(),
                                    "new_start": new_start.isoformat(),
                                    "pre_digest": ""}]}, fh)
    return RosterService(st)


@pytest.fixture
def tmpdir_path():
    return tempfile.mkdtemp()


# ══ 純檢查 ════════════════════════════════════════════════════════════════
class TestTheBlockerCheck:
    def test_an_unreconciled_shift_blocks(self, tmpdir_path):
        """★格網對不上新舊任何一邊★(有人手工編過)→ 收斂會拒絕搬動、
        意圖留著 —— 這正是要擋的狀態。"""
        svc = _svc(tmpdir_path, grid_days=[date(2026, 9, 21)])
        out = svc.pending_grid_shift_blockers(YM)
        assert out and "b1" in out[0], out
        assert "切片室整梯等於沒開" in out[0], out[0]

    def test_it_says_how_to_get_out(self):
        """★訊息要能行動★:使用者按幾次自動排班都一樣,要講他該去改什麼。"""
        svc = _svc(tempfile.mkdtemp(), grid_days=[date(2026, 9, 21)])
        out = svc.pending_grid_shift_blockers(YM)
        assert "起始日" in out[0] and "切片格網" in out[0], out[0]
        assert "不必重開程式" in out[0], out[0]

    def test_no_pending_means_no_blocker(self, tmpdir_path):
        svc = _svc(tmpdir_path, pending=False)
        assert svc.pending_grid_shift_blockers(YM) == []

    def test_an_unreadable_file_does_not_pass(self, tmpdir_path):
        """★讀不到就不放行★:壞掉的意圖檔【證明不了】沒有待辦的平移,
        而這道閘門要擋的正是「看不出來的錯」。把未知當成沒事,等於閘門
        在最需要它的時候自己消失。"""
        svc = _svc(tmpdir_path, corrupt=True)
        out = svc.pending_grid_shift_blockers(YM)
        assert out and "無法確認" in out[0], out

    def test_another_months_batch_is_not_my_problem(self, tmpdir_path):
        """★只擋本月會被影響到的梯次★:別的月份的待辦不該卡住這個月
        (那會變成一道全域的、關不掉的閘門)。"""
        svc = _svc(tmpdir_path, start=date(2026, 12, 7),
                   grid_days=[date(2026, 12, 7)],
                   old_start=date(2026, 11, 30), new_start=date(2026, 12, 7))
        assert svc.pending_grid_shift_blockers(YM) == []

    def test_the_pure_check_does_not_reconcile(self, tmpdir_path):
        """★純檢查不可以有副作用★:定案是在臨界區【裡面】呼叫它的,
        而收斂自己要拿臨界區。"""
        svc = _svc(tmpdir_path, grid_days=[OLD])   # 這一筆其實收斂得掉
        before = svc.storage.load_pending_grid_shifts()
        svc.pending_grid_shift_blockers(YM)
        assert svc.storage.load_pending_grid_shifts() == before


# ══ 出口:先收斂一次 ═══════════════════════════════════════════════════════
class TestTheGateHasAnExit:
    def test_a_resolvable_shift_is_reconciled_and_passes(self, tmpdir_path):
        """★格網還停在舊窗★ → 收斂會把它搬過去 → 閘門放行。
        使用者不必重開程式(開程式時的收斂是一次性的)。"""
        svc = _svc(tmpdir_path, grid_days=[OLD])
        assert svc.require_grid_shifts_reconciled(YM) == []
        assert svc.storage.load_pending_grid_shifts() == []
        moved = svc.storage.load_biopsy_grid().get("b1") or {}
        assert (OLD + timedelta(days=7)).isoformat() in moved, moved

    def test_a_shift_that_never_landed_is_cleared(self, tmpdir_path):
        """梯次起始日根本沒改成新值 → 沒有義務 → 放行。"""
        svc = _svc(tmpdir_path, start=OLD, grid_days=[OLD])
        assert svc.require_grid_shifts_reconciled(YM) == []

    def test_a_genuinely_stuck_one_still_blocks(self, tmpdir_path):
        """★對照組★:出口不是「一律放行」。"""
        svc = _svc(tmpdir_path, grid_days=[date(2026, 9, 21)])
        assert svc.require_grid_shifts_reconciled(YM)

    def test_a_reconcile_crash_does_not_swallow_the_check(self, tmpdir_path,
                                                          monkeypatch):
        """收斂自己爆掉時,★判定仍要跑★(而且仍然擋)——
        「收斂失敗」不可以變成「沒問題」。"""
        svc = _svc(tmpdir_path, grid_days=[date(2026, 9, 21)])
        monkeypatch.setattr(
            svc, "reconcile_pending_grid_shifts",
            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        assert svc.require_grid_shifts_reconciled(YM)


# ══ 接線 ══════════════════════════════════════════════════════════════════
class TestItIsWiredUp:
    def test_the_warning_panel_shows_it(self, tmpdir_path):
        svc = _svc(tmpdir_path, grid_days=[date(2026, 9, 21)])
        svc.storage.save_month(YM, {"day_slots": {
            MON.isoformat(): {"上午": {"101": ["C1"]}}}})
        assert any("切片格網" in m for m in svc.quick_validate_day(YM)), \
            "警告面板沒有帶出平移未收斂"

    def test_the_panel_shows_it_before_anything_is_scheduled(self,
                                                             tmpdir_path):
        """★外審 R1 P2★:`quick_validate_day` 在「本月還沒排班」時會提早
        return —— 而使用者最常正是在那個時候才發現定不了案。面板若是空的,
        就★原封不動地還原了這道閘門要修的那個「看不出來」★。"""
        svc = _svc(tmpdir_path, grid_days=[date(2026, 9, 21)])
        svc.storage.save_month(YM, {})            # 一格都還沒排
        assert not (svc.storage.load_month(YM).get("day_slots")), "前提"
        assert any("切片格網" in m for m in svc.quick_validate_day(YM)), \
            "★還沒排班時面板是空的,但定案已經被擋★"

    def test_it_is_not_reported_twice(self, tmpdir_path):
        """排過班的月份不可以講兩次(移到前面之後,原本那一處要拿掉)。"""
        svc = _svc(tmpdir_path, grid_days=[date(2026, 9, 21)])
        svc.storage.save_month(YM, {"day_slots": {
            MON.isoformat(): {"上午": {"101": ["C1"]}}}})
        hits = [m for m in svc.quick_validate_day(YM) if "切片格網" in m]
        assert len(hits) == 1, hits

    def test_finalize_is_blocked(self, tmpdir_path):
        svc = _svc(tmpdir_path, grid_days=[date(2026, 9, 21)])
        svc.storage.save_month(YM, {"day_slots": {
            MON.isoformat(): {"上午": {"101": ["C1"]}}}})
        with pytest.raises(PendingGridShiftError) as e:
            svc.finalize(YM, True)
        assert "b1" in str(e.value)
        assert not (svc.storage.load_month(YM).get("finalized")), \
            "★被擋下來卻仍然定案了★"

    def test_finalize_passes_once_it_is_reconcilable(self, tmpdir_path):
        """★出口在定案這條路上也要通★:格網還在舊窗 → 定案前的收斂會把它
        搬好 → 定案得下去(不必重開程式)。"""
        svc = _svc(tmpdir_path, grid_days=[OLD])
        svc.storage.save_month(YM, {"day_slots": {
            MON.isoformat(): {"上午": {"101": ["C1"]}}}})
        svc.finalize(YM, True)
        assert svc.storage.load_month(YM).get("finalized") is True

    def test_unfinalizing_is_never_blocked(self, tmpdir_path):
        """★解除定案是【救回來】的那條路★ —— 不可以被這道閘門擋住,
        否則使用者會被鎖在定案狀態裡。"""
        svc = _svc(tmpdir_path, grid_days=[OLD])
        svc.storage.save_month(YM, {"day_slots": {
            MON.isoformat(): {"上午": {"101": ["C1"]}}}})
        svc.finalize(YM, True)
        # 事後才卡住(有人手工編了格網)
        svc.storage.save_biopsy_grid({"b1": {"2026-09-21": {"上午": True}}})
        with open(os.path.join(svc.storage.base_dir,
                               "pending_grid_shift.json"),
                  "w", encoding="utf-8") as fh:
            json.dump({"pending": [{"batch_id": "b1",
                                    "old_start": OLD.isoformat(),
                                    "new_start": MON.isoformat(),
                                    "pre_digest": ""}]}, fh)
        svc.finalize(YM, False)              # 不可以拋
        assert svc.storage.load_month(YM).get("finalized") is not True
