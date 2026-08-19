# -*- coding: utf-8 -*-
"""[批次RS-1] 跨機同步後,舊的記憶體快照不可以整份覆寫月檔。

排班程式的月檔是【一個檔案裝一整個月】:R/VS 值班、日排班、請假、指定、
停診、audit 全在裡面。GitSync 的背景 pull 會在使用者「讀出來 → 改 → 存回去」
的中間把它換成他機的新版本 —— 整份 `save_month(舊快照)` 於是把對方剛同步
成功的欄位一起退回舊值。★從 Git 看來那是 pull 之後產生的合法新變更★,
不是衝突,所以 Git 幫不上忙,使用者也看不出來。

本檔用一個 barrier 把那一瞬間釘住:在 `load_month*` 與 `save_month` 之間
【真的】插入一次他機寫入,再看結果。
"""
import threading
from datetime import date

import pytest

from cmuh_common.roster.service import RosterService
from cmuh_common.roster.storage import (
    RosterStorage,
    StaleRosterDataError,
    _file_revision,
)


@pytest.fixture()
def st(tmp_path):
    s = RosterStorage(str(tmp_path / "roster"))
    s.save_config({"r_members": [{"id": "K"}, {"id": "C"}],
                   "vs_members": [], "pgy_members": [], "clerk_members": []})
    return s


def _remote_write(st, ym, mutate):
    """模擬【他機的變更被 pull 進來】:直接改盤上的那一份。"""
    month = st.load_month(ym)
    mutate(month)
    st.save_month(ym, month)


class TestTheOtherMachinesChangesSurvive:
    """★核心性質★:我改我的欄位,不可以動到對方剛同步進來的欄位。"""

    def test_a_remote_pull_between_load_and_save_is_not_clobbered(self, st):
        ym = "2026-09"
        st.save_month(ym, {"r_duty": {}, "day_slots": {}})
        svc = RosterService(st)

        fired = []
        real = st.load_month_snapshot

        def _load_then_remote_write(y):
            out = real(y)
            if not fired:                      # 只插一次,不然重試會無限落空
                fired.append(True)
                # ★他機在這一瞬間把【日排班】同步進來★
                _remote_write(st, ym, lambda m: m.setdefault(
                    "day_slots", {}).setdefault("2026-09-03", {}).update(
                        {"上午": {"101": ["PGY1"]}}))
            return out

        st.load_month_snapshot = _load_then_remote_write        # type: ignore
        try:
            svc.set_cell("r", ym, date(2026, 9, 1), "K")
        finally:
            st.load_month_snapshot = real                       # type: ignore

        after = st.load_month(ym)
        assert after["r_duty"]["2026-09-01"]["person"] == "K", \
            "我自己的修改要留著"
        assert after["day_slots"]["2026-09-03"]["上午"]["101"] == ["PGY1"], \
            "★他機剛同步進來的日排班被整份寫回的舊快照吃掉了★"
        assert fired, "測試沒有真的插入他機寫入 —— 這條反例什麼都沒量到"

    def test_two_machines_editing_different_scopes_both_survive(self, st):
        """A 改 R 值班、B 改 VS 值班 —— 兩邊都要在。"""
        ym = "2026-09"
        st.save_month(ym, {"r_duty": {}, "vs_duty": {}})
        svc = RosterService(st)
        fired = []
        real = st.load_month_snapshot

        def _hook(y):
            out = real(y)
            if not fired:
                fired.append(True)
                _remote_write(st, ym, lambda m: m.setdefault(
                    "vs_duty", {}).update(
                        {"2026-09-05": {"person": "VS1", "locked": False}}))
            return out

        st.load_month_snapshot = _hook                          # type: ignore
        try:
            svc.set_cell("r", ym, date(2026, 9, 1), "K")
        finally:
            st.load_month_snapshot = real                       # type: ignore
        after = st.load_month(ym)
        assert after["r_duty"]["2026-09-01"]["person"] == "K"
        assert after["vs_duty"]["2026-09-05"]["person"] == "VS1"

    def test_the_retry_re_applies_the_same_narrow_change(self, st):
        """重試是【重讀最新版 + 重套同一個窄改動】,不是把舊快照再寫一次。"""
        ym = "2026-09"
        st.save_month(ym, {"leaves": {}})
        svc = RosterService(st)
        fired = []
        real = st.load_month_snapshot

        def _hook(y):
            out = real(y)
            if not fired:
                fired.append(True)
                _remote_write(st, ym, lambda m: m.setdefault(
                    "leaves", {}).setdefault("pgy", {}).update(
                        {"P9": ["2026-09-20"]}))
            return out

        st.load_month_snapshot = _hook                          # type: ignore
        try:
            svc.set_leaves("r", ym, "K", {date(2026, 9, 10)})
        finally:
            st.load_month_snapshot = real                       # type: ignore
        after = st.load_month(ym)
        assert after["leaves"]["r"]["K"] == ["2026-09-10"]
        assert after["leaves"]["pgy"]["P9"] == ["2026-09-20"], \
            "★他機的請假被吃掉★"


class TestTheCasItself:

    def test_a_stale_revision_is_refused(self, st):
        ym = "2026-09"
        st.save_month(ym, {"r_duty": {}})
        month, rev = st.load_month_with_revision(ym)
        _remote_write(st, ym, lambda m: m.update({"note": "他機改過了"}))
        with pytest.raises(StaleRosterDataError):
            st.save_month(ym, month, expected_revision=rev)

    def test_the_matching_revision_is_accepted(self, st):
        ym = "2026-09"
        st.save_month(ym, {"r_duty": {}})
        month, rev = st.load_month_with_revision(ym)
        month["r_duty"]["2026-09-01"] = {"person": "K"}
        st.save_month(ym, month, expected_revision=rev)         # 不該拋
        assert st.load_month(ym)["r_duty"]["2026-09-01"]["person"] == "K"

    def test_a_first_time_file_expects_the_empty_revision(self, st):
        """★"" 是有意義的期望值(這一份還不存在)★ —— 不可以用 None 當哨兵:
        「檔案不存在」與「沒有帶期望值」是兩件事。"""
        ym = "2026-10"
        month, rev = st.load_month_with_revision(ym)
        assert rev == ""
        st.save_month(ym, month, expected_revision="")          # 首次建檔
        with pytest.raises(StaleRosterDataError):
            st.save_month(ym, month, expected_revision="")      # 已經有了

    def test_the_revision_matches_the_content_that_was_parsed(self, st):
        ym = "2026-09"
        st.save_month(ym, {"r_duty": {"2026-09-01": {"person": "K"}}})
        month, rev = st.load_month_with_revision(ym)
        assert rev == _file_revision(st._month_path(ym))
        assert month["r_duty"]["2026-09-01"]["person"] == "K"

    def test_a_pull_landing_between_two_reads_cannot_forge_a_match(
            self, st, monkeypatch):
        """★位元組只讀一次★:算 revision 與解析內容若分兩次讀,他機的變更
        剛好落在兩次之間,就會得到「配不上手中內容」的 revision —— CAS 於是
        拿【新版本的】識別放行【舊版本的】內容,把對方的修改整份覆蓋掉。
        (安靜的環境下兩次讀當然一樣;要量到這條規則,反例必須真的在中間寫。)
        """
        import json as _json

        import cmuh_common.roster.storage as mod
        ym = "2026-09"
        path = st._month_path(ym)
        st.save_month(ym, {"r_duty": {"2026-09-01": {"person": "OLD"}}})
        newer = {"schema_version": 1, "month": ym,
                 "r_duty": {"2026-09-02": {"person": "他機剛同步進來"}}}

        real = mod._read_bytes
        seen: list = []

        def _hook(p):
            out = real(p)
            if str(p) == path:
                seen.append(p)
                if len(seen) == 1:               # 第一次讀完 → 他機的變更落地
                    with open(path, "w", encoding="utf-8") as f:
                        _json.dump(newer, f)
            return out

        monkeypatch.setattr(mod, "_read_bytes", _hook)
        month, rev = st.load_month_with_revision(ym)
        monkeypatch.setattr(mod, "_read_bytes", real)

        with pytest.raises(StaleRosterDataError):
            st.save_month(ym, month, expected_revision=rev)
        after = st.load_month(ym)
        assert after["r_duty"] == {
            "2026-09-02": {"person": "他機剛同步進來"}}, \
            "★他機的內容被一份舊快照覆蓋了(revision 與內容不是同一份)★"


class TestThePrecomputedPathsRefuseInsteadOfRetrying:
    """帶著【預先算好的整批結果】的路徑不可以重試 —— 重讀之後把同一批舊結果
    再套一次,只是把對方的修改換個方式蓋掉。"""

    def test_accept_day_solution_refuses_a_stale_month(self, st):
        ym = "2026-09"
        st.save_month(ym, {"day_slots": {}})
        svc = RosterService(st)
        month, rev = st.load_month_with_revision(ym)
        assert rev
        _remote_write(st, ym, lambda m: m.setdefault("day_slots", {}).update(
            {"2026-09-03": {"上午": {"101": ["PGY9"]}}}))

        real = st.load_month_snapshot
        st.load_month_snapshot = lambda y: (month, rev)          # type: ignore
        try:
            with pytest.raises(StaleRosterDataError):
                svc.accept_day_solution(
                    ym, {"2026-09-04": {"上午": {"101": ["PGY1"]}}})
        finally:
            st.load_month_snapshot = real                        # type: ignore
        after = st.load_month(ym)
        assert after["day_slots"] == {
            "2026-09-03": {"上午": {"101": ["PGY9"]}}}, \
            "★舊的求解結果整批蓋掉了他機的日排班★"


class TestTheProbeResultNeverLeaksIntoTheRealRun:
    """試算(probe)只是為了決定「要不要寫檔」。它算出來的副產物若留在 holder
    裡,而正式那一次因為他機已經做完同一件事而早退,就會把【試算時的】結果
    寫出去 —— 月檔是他機的新狀態、切片帳本卻退回舊的。"""

    def test_a_no_op_real_run_does_not_write_the_probes_biopsy_book(
            self, st, monkeypatch):
        ym = "2026-09"
        st.save_month(ym, {"r_duty": {
            "2026-09-05": {"person": "K", "locked": False}}})
        svc = RosterService(st)

        books: list = []
        monkeypatch.setattr(st, "save_biopsy",
                            lambda book: books.append(book))
        real = st.load_month_snapshot
        fired = []

        def _hook(y):
            out = real(y)
            if not fired:                      # 試算讀完 → 他機先清光了
                fired.append(True)
                _remote_write(st, ym, lambda m: m.update({"r_duty": {}}))
            return out

        st.load_month_snapshot = _hook                          # type: ignore
        try:
            svc.clear_unlocked("r", ym)
        finally:
            st.load_month_snapshot = real                       # type: ignore
        assert not books, \
            "★正式那次什麼都沒清,卻把試算時算出來的切片帳本寫出去了★"

    def test_every_mutator_clears_its_holder_first(self):
        """★機械化守衛★:這是一整類缺陷,不是單一位置 —— 只要 `_mut` 會把
        東西放進 `_holder`,清空就必須是它的第一個動作(在所有早退之前)。"""
        import ast
        import inspect

        from cmuh_common.roster import service as mod
        tree = ast.parse(inspect.getsource(mod))
        checked = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name != "_mut":
                continue
            names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            if "_holder" not in names:
                continue
            checked += 1
            first = node.body[0]
            src = ast.dump(first)
            assert "_holder" in src and "clear" in src, \
                (f"{node.lineno} 行的 _mut 會用 _holder,但第一個動作不是"
                 f"清空 —— 早退時會把上一次(試算)的結果帶出去")
        assert checked >= 3, f"守衛只掃到 {checked} 個 —— 是不是換寫法了?"


class TestUnreadableIsNotAbsent:
    """★「這一刻讀不到」與「還沒有這一份」處置不同,就不可以壓成同一格★
    防毒/同步軟體鎖住檔案時把它當成「不存在」,CAS 會拿 "" 比 "" 而通過 ——
    於是一份【從讀不到的檔推導出來的空月檔】被整份寫回去。"""

    def test_an_unreadable_file_refuses_the_write_with_its_own_reason(
            self, st, monkeypatch):
        ym = "2026-09"
        st.save_month(ym, {"r_duty": {"2026-09-01": {"person": "K"}}})
        month, rev = st.load_month_with_revision(ym)

        import cmuh_common.roster.storage as mod

        # 「讀不到」在這一層的表示法就是 None(見 `_read_bytes` 的契約);
        # 下一條測試證明真的 OSError 會被轉成這個 None。
        monkeypatch.setattr(mod, "_read_bytes", lambda path: None)
        with pytest.raises(ValueError) as ei:
            st.save_month(ym, month, expected_revision=rev)
        assert "無法讀取" in str(ei.value), \
            "★訊息要說出真正的原因★ 這不是「被別人搶先」,重讀一次沒有用"
        assert not isinstance(ei.value, StaleRosterDataError)

    def test_a_real_os_error_becomes_the_unreadable_marker(self, st):
        """★這條把「表示法」接回【真的】OSError★:上一條用 None 當替身,
        若 `_read_bytes` 其實會把 OSError 轉成 b"",那個替身就測不到東西。"""
        import cmuh_common.roster.storage as mod
        # 開一個「目錄」——在 Windows 是 PermissionError、在 POSIX 是
        # IsADirectoryError,兩者都是 OSError(不是 FileNotFoundError)。
        assert mod._read_bytes(st.months_dir) is None
        assert mod._file_revision(st.months_dir) == mod._UNREADABLE_REV
        assert mod._read_bytes(st._month_path("2099-01")) == b"",             "缺檔仍要回 b\"\"(=還沒有這一份),與讀不到分開"

    def test_an_unreadable_load_cannot_be_written_back(self, st, monkeypatch):
        """讀的時候就讀不到 → 手上那份是空的,絕不可以拿它覆蓋好資料。"""
        ym = "2026-09"
        st.save_month(ym, {"r_duty": {"2026-09-01": {"person": "K"}}})

        import cmuh_common.roster.storage as mod
        real = mod._read_bytes
        monkeypatch.setattr(mod, "_read_bytes",
                            lambda p: None if p.endswith(".json") else real(p))
        month, rev = st.load_month_with_revision(ym)
        assert month.get("r_duty") == {}, "讀不到 → 手上是空的(既有契約)"
        monkeypatch.setattr(mod, "_read_bytes", real)
        with pytest.raises(ValueError):
            st.save_month(ym, month, expected_revision=rev)
        assert st.load_month(ym)["r_duty"]["2026-09-01"]["person"] == "K", \
            "★好資料被空月檔覆蓋了★"


class TestCompareAndWriteAreOneCriticalSection:
    """CAS 不是原子的就不是 CAS:兩條執行緒可以同時讀到同一個 revision、
    同時通過比對,然後後寫的把先寫的整份蓋掉。"""

    def test_two_writers_cannot_both_pass_the_same_comparison(
            self, st, monkeypatch):
        ym = "2026-09"
        st.save_month(ym, {"r_duty": {}})
        svc = RosterService(st)

        import cmuh_common.roster.storage as mod
        real_rev = mod._file_revision
        gate = threading.Barrier(2)
        used = []

        def _rev_with_barrier(path):
            out = real_rev(path)
            if path.endswith("2026-09.json") and len(used) < 2:
                used.append(path)
                try:
                    # 有鎖時第二條 thread 根本進不來 → 逾時後照常走(序列化);
                    # 沒鎖時兩條同時停在這裡 → 兩邊都拿到【同一個】revision。
                    gate.wait(timeout=1.5)
                except threading.BrokenBarrierError:
                    pass
            return out

        monkeypatch.setattr(mod, "_file_revision", _rev_with_barrier)
        errs: list = []

        def _worker(day: int):
            try:
                svc.set_cell("r", ym, date(2026, 9, day), f"P{day}")
            except Exception as e:                       # noqa: BLE001
                errs.append(e)

        ts = [threading.Thread(target=_worker, args=(d,)) for d in (1, 2)]
        for t in ts:
            t.start()
        for t in ts:
            t.join(timeout=30)
        assert not errs, errs
        duty = st.load_month(ym)["r_duty"]
        assert "2026-09-01" in duty and "2026-09-02" in duty, \
            f"★兩個寫入者同時通過了同一次比對★ {sorted(duty)}"


class TestConcurrentWritersDoNotLoseUpdates:
    """兩條 thread 同時對同一個月檔做窄改動 —— 兩邊都要留下。
    (同機雙視窗由單例互斥擋住,但跨機是同一個失效模式,執行緒是可測的形狀。)"""

    def test_parallel_narrow_edits_all_land(self, st):
        ym = "2026-09"
        st.save_month(ym, {"r_duty": {}})
        svc = RosterService(st)
        start = threading.Barrier(4)
        errs: list = []

        def _worker(day: int):
            try:
                start.wait(timeout=10)
                svc.set_cell("r", ym, date(2026, 9, day), f"P{day}")
            except Exception as e:                       # noqa: BLE001
                errs.append(e)

        threads = [threading.Thread(target=_worker, args=(d,))
                   for d in (1, 2, 3, 4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        assert not errs, errs
        duty = st.load_month(ym)["r_duty"]
        for d in (1, 2, 3, 4):
            iso = f"2026-09-0{d}"
            assert iso in duty, f"★{iso} 的修改在併發下不見了★ {sorted(duty)}"


class TestTheDaySolveResultCannotBeAppliedStale:
    """[批次RS-2 / 排班審 P1-02] PGY/Clerk 的求解結果沒有 stale gate 時,預覽
    視窗開著的期間他機同步進來的變更會被舊解整批蓋掉:請假的人又被排上、
    剛加入的 Clerk 消失、剛停診的診間又有人、他機手動調整的未鎖時段被洗掉。
    R/VS 那一側早就會「套用前重建 context 再驗」——這裡補上同一個模式。"""

    def _svc(self, st):
        st.save_clinic_template({"template": {
            "一": {"上午": ["101"], "下午": ["101"]},
            "二": {"上午": ["101"], "下午": ["101"]},
            "三": {"上午": ["101"], "下午": []},
            "四": {"上午": ["101"], "下午": ["101"]},
            "五": {"上午": ["101"], "下午": ["101"]},
        }})
        st.save_config({"r_members": [], "vs_members": [],
                        "pgy_members": [{"id": "P1"}, {"id": "P2"}],
                        "clerk_members": [{"id": "C1"}]})
        return RosterService(st)

    def _solved(self, svc, ym="2026-09"):
        svc.storage.save_month(ym, {"pgy_month_roster": ["P1", "P2"]})
        return svc.run_day_solve(ym)

    def test_a_remote_leave_change_rejects_the_apply(self, st):
        svc = self._svc(st)
        res = self._solved(svc)
        svc.set_leaves("pgy", "2026-09", "P1", {date(2026, 9, 3)})
        with pytest.raises(ValueError, match="已過期"):
            svc.accept_day_solution("2026-09", res.day_slots, expect=res)

    def test_a_remote_pgy_roster_change_rejects_the_apply(self, st):
        svc = self._svc(st)
        res = self._solved(svc)
        svc.set_pgy_month_roster("2026-09", ["P1"])
        with pytest.raises(ValueError, match="已過期"):
            svc.accept_day_solution("2026-09", res.day_slots, expect=res)

    def test_a_remote_clinic_closure_rejects_the_apply(self, st):
        svc = self._svc(st)
        res = self._solved(svc)
        svc.set_clinic_closed("2026-09", "101", date(2026, 9, 7),
                              date(2026, 9, 7), ["上午"])
        with pytest.raises(ValueError, match="已過期"):
            svc.accept_day_solution("2026-09", res.day_slots, expect=res)

    def test_a_remote_clerk_batch_change_rejects_the_apply(self, st):
        svc = self._svc(st)
        res = self._solved(svc)
        st.save_clerk_batches([{"id": "B1", "start_monday": "2026-08-31",
                                "members": ["C1"]}])
        with pytest.raises(ValueError, match="已過期"):
            svc.accept_day_solution("2026-09", res.day_slots, expect=res)

    def test_a_remote_unlocked_day_slot_change_rejects_the_apply(self, st):
        """他機【手動】調整了未鎖定的時段 —— 舊解會把它洗掉。
        (這一項不在 solver 輸入的指紋裡,是靠月檔 revision 擋下來的。)"""
        svc = self._svc(st)
        res = self._solved(svc)
        svc.set_day_slot("2026-09", date(2026, 9, 10), "上午", "101", ["P2"])
        with pytest.raises(ValueError, match="已過期"):
            svc.accept_day_solution("2026-09", res.day_slots, expect=res)

    def test_an_unchanged_state_still_applies(self, st):
        """★反方向★:什麼都沒變就要套得下去 —— 否則這個閘門等於停用排班。"""
        svc = self._svc(st)
        res = self._solved(svc)
        svc.accept_day_solution("2026-09", res.day_slots, expect=res)
        assert st.load_month("2026-09")["day_slots"] == res.day_slots

    def test_the_ui_passes_the_result_to_the_gate(self):
        """★沒有呼叫端就等於沒有這個閘門★:UI 的「套用」必須把 result 傳進去
        (只在 service 有參數、UI 不傳,是最容易靜默退化的形狀)。"""
        import inspect

        from cmuh_common.roster.ui import day_tab
        src = inspect.getsource(day_tab.DayScheduleTab)
        i = src.index("accept_day_solution(")
        assert "expect=" in src[i:i + 200], \
            "★UI 套用時沒有把求解結果交給 stale gate★"


class TestTheValidationAndTheWriteAreOneCriticalSection:
    """[RS-2 第 1 輪 P1] 月檔的 CAS 只看得到月檔;而「求解結果配不配得上現況」
    還要看月檔【以外】的檔案(名單/模板/Clerk 梯次/假日)。驗證與寫入之間若讓
    背景同步插進來合併那些檔,指紋比的是合併前的資料、月檔 revision 又沒變 ——
    兩道關卡都通過,舊解照樣落地。"""

    def test_an_external_file_cannot_change_between_check_and_write(self, st):
        """★用真的執行緒量★:臨界區持有期間,他機的外部檔更新必須進不來。"""
        done: list = []
        started = threading.Event()

        def _other_machine():
            started.set()
            st.save_clerk_batches([{"id": "B9", "start_monday": "2026-08-31",
                                    "members": ["C9"]}])
            done.append(True)

        with st.write_barrier():
            t = threading.Thread(target=_other_machine)
            t.start()
            assert started.wait(timeout=5)
            t.join(timeout=0.6)
            assert not done, \
                "★臨界區內,月檔以外的輸入仍然被換掉了(驗證與寫入之間有縫)★"
        t.join(timeout=10)
        assert done, "臨界區結束後,對方的寫入要能完成(不是被永久擋住)"

    def test_both_accept_paths_run_inside_the_barrier(self):
        """★結構守衛★:兩條 accept 都必須把「驗證+寫入」放進臨界區 ——
        只在其中一條做,另一條就是同一個缺陷的複製品。"""
        import inspect

        from cmuh_common.roster.service import RosterService
        for name in ("accept_day_solution", "accept_solution"):
            src = inspect.getsource(getattr(RosterService, name))
            assert "self.storage.write_barrier()" in src, \
                f"★{name} 沒有在臨界區內驗證+寫入★"

    def test_the_git_commit_happens_after_the_barrier_is_released(
            self, tmp_path):
        """★鎖序不可以反過來★:臨界區持有工作樹鎖,若在裡面去拿 git 鎖,就會
        與正在 pull 的背景執行緒(先 git 鎖、再工作樹鎖)互相等待 —— 死鎖。

        ★量的是行為,不是原始碼裡有沒有那個字串★:條件被改成 `if False:` 時
        字串照樣還在,那種守衛什麼都保證不了(第一版就是這樣寫的)。
        """
        import subprocess

        from cmuh_common.roster.gitsync_storage import GitSyncStorage
        work = tmp_path / "work"
        subprocess.run(["git", "init", str(work)], capture_output=True,
                       check=True)
        for k, v in (("user.email", "t@t"), ("user.name", "tester")):
            subprocess.run(["git", "-C", str(work), "config", k, v],
                           capture_output=True, check=True)
        gst = GitSyncStorage(str(work), pull_interval_sec=0)

        real_lock = gst._git_lock
        state = {"in_barrier": False, "bad": False}

        class _Watch:
            def acquire(self, *a, **kw):
                if state["in_barrier"]:
                    state["bad"] = True
                return real_lock.acquire(*a, **kw)

            def release(self):
                return real_lock.release()

            def __enter__(self):
                self.acquire()
                return self

            def __exit__(self, *a):
                self.release()

        gst._git_lock = _Watch()                     # type: ignore
        try:
            with gst.write_barrier():
                state["in_barrier"] = True
                gst.save_config({"r_members": [{"id": "X"}]})
                state["in_barrier"] = False
            assert not state["bad"], \
                "★臨界區內就去拿 git 鎖 —— 與 pull 反序,會死鎖★"
        finally:
            gst._git_lock = real_lock                # type: ignore
        # 離開臨界區之後,那一筆變更仍要真的進 git(不可以被吞掉)
        log = subprocess.run(["git", "-C", str(work), "log", "--name-only",
                              "--pretty=format:"], capture_output=True,
                             text=True)
        assert "config.json" in (log.stdout or ""), \
            "★延後的 commit 沒有補上 —— 變更留在本機、永遠不會同步出去★"
