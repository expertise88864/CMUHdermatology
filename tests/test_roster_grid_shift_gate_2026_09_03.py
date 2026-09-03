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


# ══ 權威邊界不只定案(外審第七輪 P2)══════════════════════════════════════
class TestTheAuthoritativeBoundary:
    """★「有未收斂的持久義務」≠「可以產生權威結果」★

    第一版只擋了定案。但 `accept_day_solution` 會把班表寫進 canonical 月檔、
    `build_export` 會產出★要發出去的正式 Word/Excel★ —— 兩者都已經越過權威
    邊界。切片格網停在改起始日之前的那一週時,那一梯的切片格子落在梯次涵蓋
    範圍外而被忽略(切片室整梯等於沒開),而畫面上看不出來。
    """

    def _stuck(self, tmp_path):
        """格網對不上新舊任何一邊 → 收斂會拒絕搬動、意圖留著。"""
        svc = _svc(tmp_path, grid_days=[date(2026, 9, 21)])
        svc.storage.save_month(YM, {"day_slots": {
            MON.isoformat(): {"上午": {"101": ["C1"]}}}})
        return svc

    def test_accept_is_blocked(self, tmpdir_path):
        """★套用會寫進 canonical 月檔★ —— 定案不是唯一該擋的地方。"""
        svc = self._stuck(tmpdir_path)
        before = svc.storage.load_month(YM).get("day_slots")
        with pytest.raises(PendingGridShiftError) as e:
            svc.accept_day_solution(YM, {"2026-09-08": {
                "上午": {"101": ["C2"]}}})
        assert "b1" in str(e.value)
        assert svc.storage.load_month(YM).get("day_slots") == before, \
            "★被擋下來卻已經寫進去了★"

    def test_export_is_blocked(self, tmpdir_path):
        """★匯出的是要發出去的正式文件★:印出去就收不回來了。"""
        svc = self._stuck(tmpdir_path)
        with pytest.raises(PendingGridShiftError):
            svc.build_export(YM)

    def test_preview_is_not_blocked_but_says_why(self, tmpdir_path):
        """★預覽不擋★(它沒有副作用,擋了只會讓人連畫面都打不開)——
        但★要把原因講在警告裡★,否則使用者會遇到「排得出來卻套不下去」
        而不知道去修什麼。"""
        svc = self._stuck(tmpdir_path)
        res = svc.run_day_solve(YM)
        assert res.day_slots is not None, "預覽不該被擋"
        assert any("切片格網" in w for w in res.warnings), res.warnings

    def test_they_pass_once_it_is_reconcilable(self, tmpdir_path):
        """★出口在這三條路上都要通★:格網還在舊窗 → 進臨界區之前的收斂會
        把它搬好 → 套用/匯出都做得下去(不必重開程式)。"""
        svc = _svc(tmpdir_path, grid_days=[OLD])
        svc.storage.save_month(YM, {"day_slots": {
            MON.isoformat(): {"上午": {"101": ["C1"]}}}})
        svc.accept_day_solution(YM, {MON.isoformat(): {
            "上午": {"101": ["C2"]}}})
        assert svc.build_export(YM) is not None
        assert svc.storage.load_pending_grid_shifts() == []

    def test_the_gate_runs_outside_the_write_barrier(self, tmpdir_path):
        """★收斂要在臨界區【外面】做★:`reconcile_…` 自己要拿臨界區,
        在裡面呼叫就是巢狀(基底層是 RLock 會過,GitSync 那層不保證)。
        判準:閘門的呼叫要排在 `write_barrier()` 之前。"""
        import ast
        import inspect
        import textwrap
        for fn in (RosterService.accept_day_solution,
                   RosterService.build_export):
            src = textwrap.dedent(inspect.getsource(fn))
            names = [n.func.attr for n in ast.walk(ast.parse(src))
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute)
                     and n.func.attr in ("_guard_grid_shifts",
                                         "write_barrier")]
            assert names[:2] == ["_guard_grid_shifts", "write_barrier"], \
                f"{fn.__name__}: {names}"


class TestTheRaceBetweenTheGateAndTheBarrier:
    """★閘門在鎖外、動作在鎖內 = check-then-act★(外審第七輪 R1 P2)。

    背景 pull 或另一支執行緒可以在那個縫裡寫進一筆未收斂的平移意圖 ——
    而 pending 的寫入用的正是同一把 barrier,所以那是真的會發生的交錯。
    定案那條路早就在臨界區裡面再驗一次;這一批把 accept/export 補齊。

    反例的做法:把 `write_barrier` 換成一個★進入時才寫進 pending★的替身
    (那正是「鎖外檢查通過之後、鎖內動作之前」那一刻)。
    """

    def _svc_ok(self, tmp_path):
        """一開始是乾淨的(鎖外那一道會放行)。"""
        svc = _svc(tmp_path, pending=False, grid_days=[MON])
        svc.storage.save_month(YM, {"day_slots": {
            MON.isoformat(): {"上午": {"101": ["C1"]}}}})
        return svc

    def _inject_on_enter(self, svc, monkeypatch):
        from contextlib import contextmanager
        real = svc.storage.write_barrier
        path = os.path.join(svc.storage.base_dir, "pending_grid_shift.json")

        @contextmanager
        def _barrier():
            with real():
                # ★就在這一刻★:鎖外的檢查已經過了,鎖內的動作還沒開始。
                svc.storage.save_biopsy_grid(
                    {"b1": {"2026-09-21": {"上午": True}}})
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump({"pending": [{
                        "batch_id": "b1", "old_start": OLD.isoformat(),
                        "new_start": MON.isoformat(), "pre_digest": ""}]}, fh)
                yield
        monkeypatch.setattr(svc.storage, "write_barrier", _barrier)

    def test_accept_still_refuses(self, tmpdir_path, monkeypatch):
        svc = self._svc_ok(tmpdir_path)
        before = svc.storage.load_month(YM).get("day_slots")
        self._inject_on_enter(svc, monkeypatch)
        with pytest.raises(PendingGridShiftError):
            svc.accept_day_solution(YM, {"2026-09-08": {
                "上午": {"101": ["C2"]}}})
        assert svc.storage.load_month(YM).get("day_slots") == before, \
            "★縫裡插進來的 pending 沒擋住,班表已經寫下去了★"

    def test_export_still_refuses(self, tmpdir_path, monkeypatch):
        svc = self._svc_ok(tmpdir_path)
        self._inject_on_enter(svc, monkeypatch)
        with pytest.raises(PendingGridShiftError):
            svc.build_export(YM)

    def test_the_inner_check_does_not_reconcile(self, tmpdir_path):
        """★鎖內那一道只能純檢查★:呼叫端已經持鎖,而收斂自己也要拿。"""
        import ast
        import inspect
        import textwrap
        src = textwrap.dedent(inspect.getsource(
            RosterService._guard_grid_shifts_locked))
        called = {n.func.attr for n in ast.walk(ast.parse(src))
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute)}
        assert "pending_grid_shift_blockers" in called, called
        assert "require_grid_shifts_reconciled" not in called, \
            "★鎖內不可以呼叫會拿鎖的收斂★"

    def test_both_layers_are_wired(self):
        """★兩道都要在★:鎖外那一道是【出口】(順便收斂),鎖內那一道才是
        擋得住競態的閘門 —— 少任何一道都是這一輪 finding 的形狀。"""
        import ast
        import inspect
        import textwrap
        pairs = ((RosterService.build_export,
                  RosterService._build_export_locked),
                 (RosterService.accept_day_solution,
                  RosterService._accept_day_locked))
        for outer, inner in pairs:
            for fn, want in ((outer, "_guard_grid_shifts"),
                             (inner, "_guard_grid_shifts_locked")):
                src = textwrap.dedent(inspect.getsource(fn))
                called = {n.func.attr for n in ast.walk(ast.parse(src))
                          if isinstance(n, ast.Call)
                          and isinstance(n.func, ast.Attribute)}
                assert want in called, f"{fn.__name__} 少了 {want}"
