# -*- coding: utf-8 -*-
"""[批次RS-6 / 排班審R2 P1-03] 「算帳本用的月檔」與「被標定案的月檔」

`finalize` 先用月檔重算帳本、再把月檔標成唯讀,兩步之間各自重讀一次。
他機在中間存進來的班表變動 → ★帳本＝A 版班表、被定案的是 B 版★,而定案
之後是唯讀的,只能靠解除定案才救得回來。`resettle_from_duty` 自己也一樣:
定案判斷、算點數的 duty、建 context 的月檔是三次獨立讀取。

同批把月檔的【編輯基底】補成嚴格的:寬鬆載入對壞檔回一份預設空月檔,
`update_month` 拿它當基底寫回去 = 整月被清成只剩這一次的改動
(2026-07-25 的教訓;`_update_canonical` 已經有這道防護,月檔漏了)。
"""
import inspect
import os
import sys
import threading
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.service import RosterService, _duty_digest  # noqa: E402
from cmuh_common.roster.storage import (  # noqa: E402
    RosterStorage, StaleRosterDataError,
)

YM = "2026-08"
D1, D2 = date(2026, 8, 3), date(2026, 8, 4)


def _cell(p):
    return {"person": p, "locked": False, "source": "test"}


@pytest.fixture()
def svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({"r_members": [{"id": "r1", "name": "甲", "level": "R1"},
                                  {"id": "r2", "name": "乙", "level": "R2"}],
                    "vs_members": []})
    st.save_month(YM, {"r_duty": {D1.isoformat(): _cell("r1"),
                                  D2.isoformat(): _cell("r2")}})
    return RosterService(st)


def _points_of(st, scope=("r",)):
    hist = st.load_ledger().get("history") or []
    out: dict = {}
    for h in hist:
        if h.get("month") == YM and h.get("scope") in scope:
            for k, v in (h.get("deltas") or {}).items():
                out[k] = out.get(k, 0) + v
    return out


class TestTheLedgerAndTheFinalizedMonthAgree:

    def test_a_duty_change_between_the_two_steps_aborts_the_finalize(
            self, svc, monkeypatch):
        """★守衛要真的擋下來★:重算之後班表又變了,就不可以把它定案 ——
        定案是唯讀的,錯了只能解除定案才救得回來。"""
        real = svc._resettle_locked

        def _hook(scope, ym, month, *a, **kw):
            out = real(scope, ym, month, *a, **kw)
            if scope == "r":                 # 重算完之後,盤上的班表被換掉
                m = svc.storage.load_month(ym)
                m["r_duty"][D2.isoformat()] = _cell("r1")
                svc.storage.save_month(ym, m)
            return out

        monkeypatch.setattr(svc, "_resettle_locked", _hook)
        with pytest.raises(StaleRosterDataError):
            svc.finalize(YM, True)
        assert not svc.storage.load_month(YM).get("finalized"), \
            "★中止定案就不可以留下 finalized=True★"

    def test_a_competing_write_is_serialized_not_aborted(self, svc):
        """★臨界區在前、守衛在後,兩個都要有★

        守衛只是「最後一道確認」——沒有臨界區的話,他機的正常編輯會讓每一次
        定案都被擋下(把資料錯誤換成使用者永遠定不了案,那是另一種壞掉)。
        有臨界區時對方被序列化:定案照樣成功,對方的編輯事後才落地。
        """
        st = svc.storage
        started, spawned, outcome = threading.Event(), [], []
        real = svc._resettle_locked

        def _other(ym):
            started.set()
            with st.write_barrier():           # 他機/他緒的正常編輯
                m = st.load_month(ym)
                m["r_duty"][D2.isoformat()] = _cell("r1")
                try:
                    st.save_month(ym, m)
                    outcome.append("wrote")
                except Exception:              # 已定案 → 唯讀,這才是對的
                    outcome.append("refused")

        def _spawn(scope, ym, month, *a, **kw):
            out = real(scope, ym, month, *a, **kw)
            if scope == "r" and not spawned:
                t = threading.Thread(target=_other, args=(ym,), daemon=True)
                spawned.append(t)
                t.start()
                assert started.wait(timeout=5)
                t.join(timeout=0.6)            # ★這一刻正是那個空隙★
            return out

        svc._resettle_locked = _spawn          # type: ignore
        try:
            svc.finalize(YM, True)             # 不得被守衛擋下
        finally:
            svc._resettle_locked = real        # type: ignore
        spawned[0].join(timeout=10)
        assert svc.storage.load_month(YM)["finalized"] is True
        assert outcome == ["refused"],             f"對方應在定案【之後】才動得了月檔(而該月已唯讀) {outcome}"

    def test_a_normal_finalize_still_works(self, svc):
        svc.finalize(YM, True)
        month = svc.storage.load_month(YM)
        assert month["finalized"] is True
        assert any(h.get("month") == YM
                   for h in svc.storage.load_ledger()["history"]), (
            "帳本要有本月的結算")
        assert not svc.storage.load_pending_settles(), "意圖要被清掉"

    def test_the_digest_only_tracks_what_the_points_depend_on(self, svc):
        """鎖定/來源不影響點數 → 不該讓它們把定案擋下來(誤報也是缺陷)。"""
        m = svc.storage.load_month(YM)
        base = _duty_digest(m, "r")
        m["r_duty"][D1.isoformat()]["locked"] = True
        m["r_duty"][D1.isoformat()]["source"] = "manual"
        assert _duty_digest(m, "r") == base
        m["r_duty"][D1.isoformat()]["person"] = "r2"
        assert _duty_digest(m, "r") != base, "★換人一定要看得出來★"


class TestResettleReadsTheMonthOnce:

    def test_it_holds_the_barrier_and_reads_one_snapshot(self):
        src = inspect.getsource(RosterService.resettle_from_duty)
        assert "write_barrier()" in src
        assert "load_month_snapshot(" in src
        assert src.count("load_month") == 1, \
            "★月檔只讀一次★（定案判斷/算點數/建 context 都用同一份）"
        body = inspect.getsource(RosterService._resettle_locked)
        assert "self.storage.load_month" not in body, \
            "★重算的本體不可以自己再讀一次月檔★"
        assert "month=month" in body, "build_context 要用傳進來的那一份"

    def test_the_ledger_write_goes_through_the_cas(self, svc):
        """他機剛結算的【別月】分錄不可以被這次重算的舊快照吃掉。"""
        st = svc.storage
        fired: list = []
        real = st.canonical_snapshot

        def _hook(name, **kw):        # ★形狀要跟生產一樣★(現在會帶 validate=)
            out = real(name, **kw)
            if name == "ledger.json" and not fired:
                fired.append(True)
                led = st.load_ledger()
                led.setdefault("history", []).append(
                    {"month": "2026-07", "scope": "r", "deltas": {"r1": 3}})
                st.save_ledger(led)
            return out

        st.canonical_snapshot = _hook                        # type: ignore
        try:
            svc.resettle_from_duty("r", YM)
        finally:
            st.canonical_snapshot = real                     # type: ignore
        assert fired
        hist = st.load_ledger().get("history") or []
        assert any(h.get("month") == "2026-07" for h in hist), \
            "★他機的上月結算被這次重算整份蓋掉了★"
        assert any(h.get("month") == YM for h in hist)

    def test_nobody_can_write_while_a_resettle_runs(self, svc):
        """真的用執行緒量:重算進行中,別人寫不進正典檔。"""
        done: list = []
        started = threading.Event()

        def _other():
            started.set()
            with svc.storage.write_barrier():
                done.append(True)

        with svc.storage.write_barrier():
            t = threading.Thread(target=_other, daemon=True)
            t.start()
            assert started.wait(timeout=5)
            t.join(timeout=0.5)
            assert not done, "★臨界區內別人插了進來★"
        t.join(timeout=10)
        assert done


class TestTheEditBaseMustBeTrustworthy:

    def test_a_corrupt_month_aborts_the_edit(self, svc):
        """★整月被清成只剩這一格★是比陳舊覆蓋更慘的失敗。"""
        path = svc.storage._month_path(YM)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ 這不是 JSON")
        before = open(path, encoding="utf-8").read()
        with pytest.raises(ValueError):
            svc.set_cell("r", YM, D1, "r2")
        assert open(path, encoding="utf-8").read() == before, \
            "★壞檔時磁碟必須原封不動★"

    def test_the_snapshot_refuses_an_unreadable_month(self, svc, monkeypatch):
        """★『這一刻讀不到』不可以摺進『還沒有這一份』★

        摺進去的話 revision 會變成 ""(=還沒有這一份),CAS 就有機會拿 "" 比 ""
        而放行一份【從讀不到的檔推導出來的空月檔】。
        ★這一條要在它自己的層級量★:走 `set_cell` 的話,`_guard_overwrite`
        會先擋下來(它對鎖檔另有一道防線)—— 那個反例量到的是別條規則,
        把這一條刪掉照樣綠。
        """
        from cmuh_common.roster import storage as mod
        real = mod._read_bytes
        monkeypatch.setattr(
            mod, "_read_bytes",
            lambda p: None if str(p).endswith(f"{YM}.json") else real(p))
        # ★訊息要分得出【處置不同】的原因★:「被鎖住,等一下再試」與「這個檔
        #   是空的,去確認內容」的下一步完全不同。加了空檔那條規則之後,兩條
        #   對同一個輸入都會拋 —— 只驗型別的話,把這一條刪掉照樣綠。
        with pytest.raises(ValueError, match="暫時無法讀取"):
            svc.storage.load_month_snapshot(YM)

    def test_a_locked_month_aborts_the_edit(self, svc, monkeypatch):
        """月檔被鎖住 → 整次編輯中止、磁碟原封不動(多道防線共同保證:
        嚴格快照在最前面擋、`_guard_overwrite` 在寫入前再擋一次)。"""
        from cmuh_common.roster import storage as mod
        real = mod._read_bytes
        monkeypatch.setattr(
            mod, "_read_bytes",
            lambda p: None if str(p).endswith(f"{YM}.json") else real(p))
        with pytest.raises(ValueError):
            svc.set_cell("r", YM, D1, "r2")
        monkeypatch.undo()
        assert svc.storage.load_month(YM)["r_duty"][D1.isoformat()][
            "person"] == "r1", "★月檔內容必須原封不動★"

    def test_a_new_month_can_still_be_created(self, svc):
        svc.set_cell("r", "2026-09", date(2026, 9, 1), "r1")
        assert svc.storage.month_exists("2026-09")


# ══ 第 2 輪外審 ═════════════════════════════════════════════════════════
class TestTheCrossMachineHalfIsTheCas:
    """★臨界區只鎖得住這一個行程★(外審 RS-6 第 1 輪 P1)

    `write_barrier` 是 `threading.RLock` —— 另一台電腦(或另一個 storage
    實例)照樣寫得進來。所以「整批用同一份月檔算」還不夠,那一份的 revision
    還要當成寫回時的閘門:被搶先就整批重來,而不是把算好的東西照樣存下去。
    原本 `resettle_from_duty` 在結算完帳本之後才呼叫【自行 load】的切片重排,
    它讀到的是他機剛寫進來的另一版 → ★帳本結算自 A 版、月檔與切片帳本存成
    B 版,而整個操作還回報成功★。
    """

    def test_a_month_swapped_mid_flight_makes_the_whole_batch_redo(self, svc):
        st = svc.storage
        fired: list = []
        real = st.load_month_snapshot

        def _hook(ym, **kw):      # ★形狀要跟生產一樣★(現在會帶 validate=)
            out = real(ym, **kw)
            if not fired:                      # 讀完之後,他機換上另一版班表
                fired.append(True)
                m = st.load_month(ym)
                m["r_duty"][D2.isoformat()] = _cell("r1")   # r2 → r1
                st.save_month(ym, m)
            return out

        st.load_month_snapshot = _hook                       # type: ignore
        try:
            svc.resettle_from_duty("r", YM)
        finally:
            st.load_month_snapshot = real                    # type: ignore
        assert fired
        month = st.load_month(YM)
        # ★帳本要與【最後留在磁碟上的】月檔相符★
        duty = {d: c["person"] for d, c in month["r_duty"].items()}
        assert duty == {D1.isoformat(): "r1", D2.isoformat(): "r1"}, duty
        deltas = {}
        for h in st.load_ledger()["history"]:
            if h.get("month") == YM and h.get("scope") == "r":
                deltas = h.get("deltas") or {}
        assert deltas.get("r2") == pytest.approx(-1.0), (
            f"★帳本結算自另一版班表(r2 已經不值班了)★ {deltas}")

    def test_the_batch_is_gated_by_the_month_revision(self):
        src = inspect.getsource(RosterService._resettle_locked)
        i_book = src.index("recompute_saturday_biopsy(")
        i_month = src.index("save_month(")
        i_led = src.index("update_ledger(")
        assert src.index("ym, month", i_book) > i_book, \
            "★切片重排要用手上這一份月檔(不可以讓它自己再讀一次)★"
        assert i_month < i_led, "★月檔先寫(可收斂方向),它的 CAS 就是閘門★"
        assert "expected_revision=month_rev" in src

    def test_an_empty_month_file_is_not_a_missing_one(self, svc):
        """★存在但 0 位元組 ≠ 還沒有這一份★:兩者的 revision 都是 "",
        當成「首次建檔」的話 CAS 兩邊都對得上,窄改動就寫成一份只有這次
        改動的月檔,而使用者永遠不會知道原本有東西。"""
        path = svc.storage._month_path(YM)
        with open(path, "w", encoding="utf-8"):
            pass
        assert os.path.getsize(path) == 0
        with pytest.raises(ValueError):
            svc.storage.load_month_snapshot(YM)
        with pytest.raises(ValueError):
            svc.set_cell("r", YM, D1, "r2")
        assert os.path.getsize(path) == 0, "★磁碟必須原封不動★"

    def test_an_empty_canonical_file_is_not_a_missing_one(self, svc):
        path = svc.storage._path("config.json")
        with open(path, "w", encoding="utf-8"):
            pass
        with pytest.raises(ValueError):
            svc.storage.canonical_snapshot("config.json")

    def test_every_write_path_uses_the_strict_snapshot(self):
        """★機械化★:只修 resettle 一處等於漏掉同一類的其他入口。
        會【寫回月檔】的路徑都不可以用寬鬆載入當基底。"""
        import cmuh_common.roster.service as mod
        src = inspect.getsource(mod)
        loose = [i + 1 for i, ln in enumerate(src.splitlines())
                 if "load_month_with_revision(" in ln]
        assert len(loose) == 1, (
            f"★寬鬆載入只允許留在【不寫檔】的求解預覽那一處★ {loose}")
        fn = inspect.getsource(RosterService.run_day_solve)
        assert "load_month_with_revision(" in fn, \
            "唯一那一處應該是 run_day_solve(它不寫任何檔,見該處說明)"
