# -*- coding: utf-8 -*-
"""[批次RS-23 / 全審 2026-08-23(head f4c1a8c)一個 P1 + 四個 P2]

主題:**recovery metadata 自己也要是 transactional data**。

P1-01 意圖檔的「讀 → 改 → 寫」不是交易:嚴格讀取只保證讀到的那一份完整,
      擋不住【讀完之後、寫回之前】背景 Git merge 把他機的義務合併進來 ——
      這一次寫入就用手上那份舊清單整份覆蓋,對方的 recovery obligation
      ★合法地消失★(不是衝突、不是壞檔,git 看來只是正常的 post-merge commit)。
P2-01 改名把【已證實 stale】的結算降級成「無從查證」= 從「擋下求解」變成
      「只出警告」;改名並沒有提供任何新證據說它突然安全了。
P2-02 意圖記錄只驗到「是 dict」;而收斂端還會主動刪掉看不懂的義務。
P2-03 關聯不變量:同一 scope 的代號重複會改變 CP-SAT 的語意
      (`2*A + B == 1` → A 永遠不能值班);同一 (scope,月份) 兩筆結算分錄
      會讓回滾只拿掉一筆。
P2-04 改名是多檔交易,但回滾只存在記憶體裡 —— 斷電/被砍會留下一半舊一半新,
      而且沒有任何線索知道做到哪裡。
"""
import io
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.model import day_point                    # noqa: E402
from cmuh_common.roster.service import RosterService              # noqa: E402
from cmuh_common.roster.solve_rvs import (                        # noqa: E402
    SolveResult, rvs_input_fingerprint,
)
from cmuh_common.roster.storage import RosterStorage              # noqa: E402

OCT, NOV, SEP = "2026-10", "2026-11", "2026-09"


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


def _accept(svc, ym=OCT):
    ctx = svc.build_context("r", ym, for_solve=True)
    a = {d: "A" for d in ctx.days}
    for b in ctx.blocks:
        for x in b.days:
            a[x] = a[b.days[0]]
    pts = {m.id: 0 for m in ctx.members}
    for d, mid in a.items():
        pts[mid] += day_point(d, ctx.holidays, ctx.params)
    svc.accept_solution("r", ym, SolveResult(
        status="ok", scope="r", level_used=0, level_name="L0",
        assignments=a, points_by_person=pts,
        input_fingerprint=rvs_input_fingerprint(ctx),
        month_revision=svc.storage.load_month_snapshot(ym)[1]))


# ══ P1-01 意圖檔的讀改寫必須是一個交易 ═══════════════════════════════════
class TestThePendingFileIsWrittenTransactionally:
    def _merge_lands(self, svc, rec):
        """模擬背景 Git merge:★另一條執行緒★把他機的義務併進盤上的檔。

        真實的 GitSync 換工作樹內容時要先拿工作樹鎖(`_replace_tree`),而
        `write_barrier` 持的就是那把鎖 —— 所以「合併」只能在臨界區之外發生。
        """
        def _run():
            with svc.storage.write_barrier():
                cur = svc.storage.load_pending_settles()
                cur.append(rec)
                svc.storage._save(svc.storage._path("pending_settle.json"),
                                  {"pending": cur})

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        return t

    def test_a_merge_between_read_and_write_does_not_lose_the_other_debt(
            self, svc, monkeypatch):
        """★反例本體★:他機的 9 月 ledger 義務是「9 月還沒結算」的唯一證據。
        A 機正常記一筆義務 → 讀到空清單 → 背景 pull 把對方的義務合併進來 →
        A 用手上那份舊清單整份寫回去 = 對方的義務合法地消失(不是衝突、不是
        壞檔、CAS 也看不到),而 11 月從此可以拿錯的結轉排班。"""
        remote = {"scope": "r", "ym": SEP, "kind": "ledger", "ts": "x"}
        real = svc.storage._pending_for_write
        threads = []

        def _hook():
            out = real()
            monkeypatch.setattr(svc.storage, "_pending_for_write", real)
            t = self._merge_lands(svc, remote)   # ★讀完之後、寫回之前★
            threads.append(t)
            t.join(1.0)                          # 有臨界區的話它進不來
            return out

        monkeypatch.setattr(svc.storage, "_pending_for_write", _hook)
        svc.storage.mark_pending_settle("r", OCT, kind="biopsy")
        for t in threads:
            t.join(5.0)
        got = {(x["ym"], svc.storage.pending_kind(x))
               for x in svc.storage.load_pending_settles()}
        assert (SEP, "ledger") in got, f"★他機的義務被覆蓋掉了★ {got}"
        assert (OCT, "biopsy") in got, got

    def test_the_mutators_take_the_barrier(self):
        """★接上去了才存在★:讀改寫必須整段在臨界區內(背景 merge 要拿
        `_tree_lock` 才能換檔,而臨界區持著它)。"""
        import ast
        import inspect
        import textwrap
        for name in ("mark_pending_settle", "clear_pending_settle",
                     "retype_pending_settle", "mark_pending_grid_shift",
                     "clear_pending_grid_shift", "mark_pending_rename",
                     "clear_pending_rename"):
            fn = getattr(RosterStorage, name)
            tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
            withs = [n for n in ast.walk(tree) if isinstance(n, ast.With)]
            assert any(
                isinstance(item.context_expr, ast.Call)
                and getattr(item.context_expr.func, "attr", "")
                == "write_barrier"
                for w in withs for item in w.items), \
                f"★{name} 的讀改寫沒有包在 write_barrier 裡★"

    def test_a_reentrant_caller_still_works(self, svc):
        """呼叫端本來就在臨界區裡時不可以卡住(RLock 可重入)。"""
        with svc.storage.write_barrier():
            assert svc.storage.mark_pending_settle("r", OCT, kind="biopsy")
            svc.storage.clear_pending_settle("r", OCT, kind="biopsy")
        assert svc.storage.load_pending_settles() == []


# ══ P2-02 記錄層級的語意 ═════════════════════════════════════════════════
class TestAnUnreadableObligationIsNotNoObligation:
    def _write(self, svc, name, text):
        io.open(svc.storage._path(name), "w", encoding="utf-8").write(text)

    def test_an_empty_record_is_refused(self, svc):
        """`{}` 以前照樣通過「是 dict」那一關,而收斂端看到缺欄位就把它清掉。"""
        self._write(svc, "pending_settle.json", '{"pending": [{}]}')
        with pytest.raises(ValueError, match="看不懂"):
            svc.storage.load_pending_settles_strict()

    def test_a_blank_scope_is_refused(self, svc):
        self._write(svc, "pending_settle.json",
                    '{"pending": [{"scope": "", "ym": "", "kind": "all"}]}')
        with pytest.raises(ValueError, match="看不懂"):
            svc.storage.load_pending_settles_strict()

    def test_an_unknown_kind_is_refused(self, svc):
        self._write(svc, "pending_settle.json",
                    '{"pending": [{"scope": "r", "ym": "2026-10",'
                    ' "kind": "something"}]}')
        with pytest.raises(ValueError, match="看不懂"):
            svc.storage.load_pending_settles_strict()

    def test_a_legacy_record_without_a_kind_is_still_valid(self, svc):
        """★舊版沒有 kind★(一律當成 "all",見 `pending_kind`)—— 不可以擋。"""
        self._write(svc, "pending_settle.json",
                    '{"pending": [{"scope": "r", "ym": "2026-10"}]}')
        assert len(svc.storage.load_pending_settles_strict()) == 1

    def test_a_grid_shift_without_dates_is_refused(self, svc):
        self._write(svc, "pending_grid_shift.json",
                    '{"pending": [{"batch_id": "b1", "old_start": "壞",'
                    ' "new_start": "2026-10-12"}]}')
        with pytest.raises(ValueError, match="看不懂"):
            svc.storage.load_pending_grid_shifts_strict()

    def test_the_reconcile_never_deletes_what_it_cannot_read(self):
        """★收斂端不可以把「認不得」變成「沒有義務」★(最後一道)。"""
        import ast
        import inspect
        import textwrap
        src = textwrap.dedent(inspect.getsource(
            RosterService.reconcile_pending_grid_shifts))
        tree = ast.parse(src).body[0]
        for handler in [n for t in ast.walk(tree) if isinstance(t, ast.Try)
                        for n in t.handlers]:
            names = [c.func.attr for c in ast.walk(
                ast.Module(body=handler.body, type_ignores=[]))
                if isinstance(c, ast.Call)
                and isinstance(c.func, ast.Attribute)]
            assert "clear_pending_grid_shift" not in names, \
                "★看不懂的平移意圖被清掉了★"


# ══ P2-03 關聯不變量 ═════════════════════════════════════════════════════
class TestTheRelationalInvariants:
    def test_a_duplicate_member_id_is_refused(self, svc):
        """★重複的代號會改變 CP-SAT 的語意★:`{(日期, id): 變數}` 只會產生
        一顆變數,而 `AddExactlyOne` 把它枚舉兩次 → `2*A + B == 1` → A 從此
        不可能值班。★不可以靜默去重★(兩筆可能有不同 level/固定星期/姓名)。"""
        io.open(svc.storage._path("config.json"), "w",
                encoding="utf-8").write(
            '{"r_members": [{"id": "A", "level": "R2"},'
            ' {"id": "A", "level": "R3"}]}')
        with pytest.raises(ValueError, match="重複的代號"):
            svc.run_solve("r", OCT)

    def test_two_settlements_for_one_month_are_refused(self, svc):
        """回滾只會拿掉一筆,另一筆的差額會永遠留在餘額裡。"""
        io.open(svc.storage._path("ledger.json"), "w",
                encoding="utf-8").write(
            '{"r": {"A": 1.0}, "vs": {}, "history": ['
            '{"month": "2026-09", "scope": "r", "deltas": {"A": 1.0}},'
            '{"month": "2026-09", "scope": "r", "deltas": {"A": 2.0}}]}')
        with pytest.raises(ValueError, match="兩筆結算分錄"):
            svc.run_solve("r", OCT)

    def test_a_healthy_config_still_solves(self, svc):
        svc.run_solve("r", OCT)


# ══ P2-04 改名的交易意圖 ═════════════════════════════════════════════════
class TestRenameIsCrashDurable:
    def _boom_after(self, svc, monkeypatch, n):
        """讓第 n 個檔寫完之後整個行程「被砍」(BaseException,回滾接不到)。"""
        real = svc.storage.save_month
        state = {"n": 0}

        def _hook(ym, data, *a, **kw):
            state["n"] += 1
            if state["n"] > n:
                raise KeyboardInterrupt("被砍")
            return real(ym, data, *a, **kw)

        monkeypatch.setattr(svc.storage, "save_month", _hook)

    def test_a_kill_mid_transaction_leaves_a_durable_intent(self, svc,
                                                            monkeypatch):
        """★反例本體★:回滾只存在記憶體裡 —— 被砍之後盤上一半舊一半新,
        而下次開程式沒有任何線索知道那次改名做到哪裡。"""
        _accept(svc)
        svc.storage.save_month(NOV, {"r_duty": {
            "2026-11-03": {"person": "A", "locked": False, "source": "m"}}})
        self._boom_after(svc, monkeypatch, 0)
        with pytest.raises(KeyboardInterrupt):
            svc.rename_member("r", "A", "R1")
        pend = svc.storage.load_pending_renames()
        assert [(x["old_id"], x["new_id"]) for x in pend] == [("A", "R1")], \
            f"★半套改名沒有留下任何線索★ {pend}"

    def test_the_startup_finishes_it_forward(self, svc, monkeypatch):
        """★方向是往前做完★:回滾需要當初那份記憶體快照,重開之後沒有了;
        而「old_id → new_id」對每個檔都是冪等的。"""
        _accept(svc)
        svc.storage.save_month(NOV, {"r_duty": {
            "2026-11-03": {"person": "A", "locked": False, "source": "m"}}})
        self._boom_after(svc, monkeypatch, 0)
        with pytest.raises(KeyboardInterrupt):
            svc.rename_member("r", "A", "R1")
        monkeypatch.undo()
        assert svc.reconcile_pending_renames() == [("r", "A", "R1")]
        assert not svc.storage.load_pending_renames()
        ids = [m["id"] for m in svc.storage.load_config()["r_members"]]
        assert "R1" in ids and "A" not in ids
        cell = svc.storage.load_month(NOV)["r_duty"]["2026-11-03"]
        assert cell["person"] == "R1", "★月檔還停在舊代號(半套)★"

    def test_a_successful_rename_leaves_no_intent(self, svc):
        _accept(svc)
        svc.rename_member("r", "A", "R1")
        assert svc.storage.load_pending_renames() == []

    def test_a_rolled_back_rename_leaves_no_intent(self, svc, monkeypatch):
        """回滾成功＝盤上是完整的舊狀態 → 那筆意圖是誤報,要撤掉。"""
        _accept(svc)
        real = svc.storage.save_holiday_duty
        monkeypatch.setattr(
            svc.storage, "save_holiday_duty",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("寫不進去")))
        svc.storage.save_holiday_duty = real if False else \
            svc.storage.save_holiday_duty
        svc.storage.save_config({**svc.storage.load_config(),
                                 "r_members": [{"id": "A"}, {"id": "B"}]})
        st = svc.storage
        io.open(st._path("holiday_duty.json"), "w", encoding="utf-8").write(
            '{"r": {"2026-10-10": "A"}, "vs": {}}')
        with pytest.raises(RuntimeError):
            svc.rename_member("r", "A", "R1")
        assert svc.storage.load_pending_renames() == [], \
            "★回滾成功卻留下誤報的改名意圖★"

    def test_a_no_op_rename_records_nothing(self, svc):
        """同號、空白、撞名都不會動到任何檔 —— 不該留下改名意圖。"""
        assert svc.rename_member("r", "A", "A") == 0
        with pytest.raises(ValueError):
            svc.rename_member("r", "A", "B")
        assert svc.storage.load_pending_renames() == []

    # ── 續作的授權邊界(外審 Codex RS-23 P1-02)────────────────────────
    def _crash(self, svc, monkeypatch):
        _accept(svc)
        svc.storage.save_month(NOV, {"r_duty": {
            "2026-11-03": {"person": "A", "locked": False, "source": "m"}}})
        self._boom_after(svc, monkeypatch, 0)
        with pytest.raises(KeyboardInterrupt):
            svc.rename_member("r", "A", "R1")
        monkeypatch.undo()

    def test_it_refuses_when_both_ids_are_in_the_roster(self, svc,
                                                        monkeypatch):
        """★反例本體★:意圖說 A→R1,而名單同時有 A 與 R1。

        我們自己的交易不可能停在這裡(config 是第一個寫的檔,而且是整份原子
        寫入 —— 只會留下「只剩 R1」)。所以 R1 是【另一位合法成員】(他機新增
        /別的改名),照著續作會把 A 的餘額覆蓋到他身上,救不回來。
        """
        self._crash(svc, monkeypatch)
        cfg = svc.storage.load_config()          # 名單復原成「A 還在」+ 新人 R1
        cfg["r_members"] = [{"id": "A"}, {"id": "B"}, {"id": "R1"}]
        svc.storage.save_config(cfg)
        led = svc.storage.load_ledger()
        led["r"]["R1"] = 7.0                     # 那位合法成員的餘額
        svc.storage.save_ledger(led)
        assert svc.reconcile_pending_renames() == []
        assert svc.storage.load_ledger()["r"]["R1"] == 7.0,             "★把別人的餘額覆蓋掉了★"
        assert svc.storage.load_pending_renames(), "★意圖被清掉了★"

    def test_the_api_itself_refuses_that_shape(self, svc):
        """★守衛要在做事的那一層★:`resume=True` 是公開參數,收斂端的盤面
        判斷擋得住自己那條路,擋不住別的呼叫端(將來的 UI/腳本)。"""
        cfg = svc.storage.load_config()
        cfg["r_members"] = [{"id": "A"}, {"id": "B"}, {"id": "R1"}]
        svc.storage.save_config(cfg)
        led = svc.storage.load_ledger()
        led.setdefault("r", {})["R1"] = 7.0
        svc.storage.save_ledger(led)
        with pytest.raises(ValueError, match="拒絕自動續作"):
            svc.rename_member("r", "A", "R1", resume=True)
        assert svc.storage.load_ledger()["r"]["R1"] == 7.0

    # ── 名單的形狀是必要條件,不是充分條件(外審 Codex 第 2 輪)──────────
    def test_a_config_we_did_not_write_is_not_proof(self, svc, monkeypatch):
        """★反例本體★:他機可以獨立把 A 移除、加進一位【合法的】R1 ——
        那個盤面(名單只剩 R1、沒有 A)與我們的半套長得一模一樣。
        交易開始前的 revision 只證明得了「還沒開始」;要續作就得有
        「這份 config 是我們寫的」那個證據(寫完當下讀回來的 revision)。"""
        self._crash(svc, monkeypatch)
        cfg = svc.storage.load_config()          # 名單仍是「只剩 R1」…
        cfg["r_members"] = list(cfg["r_members"]) + [{"id": "C"}]  # …但被別人動過
        svc.storage.save_config(cfg)
        assert svc.reconcile_pending_renames() == []
        assert svc.storage.load_pending_renames(), "★意圖被清掉了★"
        assert svc.storage.load_month(NOV)["r_duty"]["2026-11-03"][
            "person"] == "A", "★在證明不了的盤面上動手了★"

    def test_the_proof_survives_a_second_crash(self, svc, monkeypatch):
        """★證據要撐得過再一次中斷★:續作會把同一份 config 再寫一次(內容
        一樣 → revision 一樣),所以第二次被砍之後照樣證明得了、收斂得完。"""
        self._crash(svc, monkeypatch)
        self._boom_after(svc, monkeypatch, 0)    # 續作時再被砍一次
        with pytest.raises(KeyboardInterrupt):   # 被砍就是被砍,不吞掉
            svc.reconcile_pending_renames()
        monkeypatch.undo()
        assert svc.reconcile_pending_renames() == [("r", "A", "R1")]
        assert not svc.storage.load_pending_renames()

    def test_a_file_holding_both_ids_blocks_the_resume(self, svc, monkeypatch):
        """★逐檔的不變量★:改名對每個檔都是整份 old→new 的原子寫入,所以
        合法的中間狀態每個檔只會全舊或全新。帳本同時有 A 與 R1(merge 把 A
        的餘額帶回來 / R1 本來就是別人)→ 續作會把兩個人的歷史混成一個人。"""
        self._crash(svc, monkeypatch)
        led = svc.storage.load_ledger()          # 帳本已改成 R1,merge 帶回 A
        led["r"]["A"] = 3.0
        svc.storage.save_ledger(led)
        assert svc.reconcile_pending_renames() == []
        assert svc.storage.load_ledger()["r"]["A"] == 3.0, "★被覆蓋掉了★"
        assert svc.storage.load_pending_renames(), "★意圖被清掉了★"

    def test_months_are_judged_one_file_at_a_time(self, svc, monkeypatch):
        """★判準要逐檔算★:改名是一個月檔一個月檔寫的,所以「一個月已經是
        新代號、另一個月還停在舊代號」正是半套的樣子。把所有月檔併在一起
        算的話,這個【該收斂】的情況會被誤判成「兩個 id 並存」而永遠擋住。"""
        _accept(svc)
        svc.storage.save_month(NOV, {"r_duty": {
            "2026-11-03": {"person": "A", "locked": False, "source": "m"}}})
        self._boom_after(svc, monkeypatch, 1)    # ★第一個月檔寫完才被砍★
        with pytest.raises(KeyboardInterrupt):
            svc.rename_member("r", "A", "R1")
        monkeypatch.undo()
        _by = {ym: svc.storage.load_month(ym) for ym in (OCT, NOV)}
        _who = {ym: {(c or {}).get("person")
                     for c in (m.get("r_duty") or {}).values()}
                for ym, m in _by.items()}
        assert {"A"} in _who.values() and {"R1"} in _who.values(),             f"前提:一個月新、一個月舊 {_who}"
        assert svc.reconcile_pending_renames() == [("r", "A", "R1")]
        for ym in (OCT, NOV):
            for cell in (svc.storage.load_month(ym).get("r_duty")
                         or {}).values():
                assert cell.get("person") == "R1"

    def test_a_crash_right_after_the_config_write_is_recoverable(
            self, svc, monkeypatch):
        """★證據不可以等到 config 寫完之後才補記★(外審 Codex 第 3 輪 P2)。

        斷電剛好落在「config 已寫、證據還沒落地」之間的話,盤上是真正的半套
        (config 新、其餘檔舊),而收斂端從此永遠證明不了、只能人工修 ——
        那正是這批要涵蓋的任意中斷窗口。所以意圖要在★寫第一個檔之前★就記下
        「我要把 config 寫成什麼樣子」。
        """
        _accept(svc)
        svc.storage.save_month(NOV, {"r_duty": {
            "2026-11-03": {"person": "A", "locked": False, "source": "m"}}})
        monkeypatch.setattr(          # config 寫完的【下一步】就被砍
            svc.storage, "save_ledger",
            lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt("被砍")))
        with pytest.raises(KeyboardInterrupt):
            svc.rename_member("r", "A", "R1")
        monkeypatch.undo()
        assert svc.reconcile_pending_renames() == [("r", "A", "R1")]
        assert not svc.storage.load_pending_renames()
        assert svc.storage.load_month(NOV)["r_duty"]["2026-11-03"][
            "person"] == "R1"

    def test_a_biopsy_override_collision_blocks_the_resume(self, svc,
                                                          monkeypatch):
        """★判準要涵蓋改名真的會改寫的每一個欄位★(外審 Codex 第 3 輪 P1):
        手動指定的切片人選也是以代號為值 —— 漏掉它就會把兩位醫師的週六切片
        指定混成一個人。"""
        self._crash(svc, monkeypatch)
        m = svc.storage.load_month(NOV)          # 該月檔還停在舊代號…
        m["biopsy_override"] = {"2026-11-07": "R1"}   # …merge 帶進別人的指定
        svc.storage.save_month(NOV, m)
        assert svc.reconcile_pending_renames() == []
        assert svc.storage.load_month(NOV)["biopsy_override"] == {
            "2026-11-07": "R1"}, "★別人的切片指定被混掉了★"
        assert svc.storage.load_pending_renames(), "★意圖被清掉了★"

    def test_a_normal_rename_refuses_that_collision_too(self, svc):
        """同一個欄位在【正常改名】的撞名掃描裡也不可以漏(離職者的指定)。"""
        _accept(svc)
        m = svc.storage.load_month(OCT)
        m["biopsy_override"] = {"2026-10-17": "R1"}   # R1＝某離職者的指定
        svc.storage.save_month(OCT, m, force=True)
        with pytest.raises(ValueError, match="切片指定"):
            svc.rename_member("r", "A", "R1")

    def test_it_refuses_intents_that_share_a_target(self, svc, monkeypatch):
        """A→R1 與 B→R1 共用目標:誰先跑決定誰的歷史被覆蓋,程式沒資格挑。"""
        self._crash(svc, monkeypatch)
        svc.storage.mark_pending_rename("r", "B", "R1")
        assert svc.reconcile_pending_renames() == []
        assert len(svc.storage.load_pending_renames()) == 2

    def test_it_refuses_a_chain_of_intents(self, svc, monkeypatch):
        """A→R1 與 R1→R2 首尾相接:順序不同結果不同。"""
        self._crash(svc, monkeypatch)
        svc.storage.mark_pending_rename("r", "R1", "R2")
        assert svc.reconcile_pending_renames() == []
        assert len(svc.storage.load_pending_renames()) == 2

    def test_an_intent_that_never_started_converges_to_the_old_state(
            self, svc, monkeypatch):
        """★收斂的兩個終點:全舊或全新★。意圖記下了但一個檔都還沒寫
        (config 的 revision 還是交易開始前那一個)→ 盤上就是完整的舊狀態,
        沒有東西要做完,那筆意圖是誤報。"""
        _accept(svc)
        real = svc.storage.save_config
        monkeypatch.setattr(
            svc.storage, "save_config",
            lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt("被砍")))
        with pytest.raises(KeyboardInterrupt):
            svc.rename_member("r", "A", "R1")
        monkeypatch.setattr(svc.storage, "save_config", real)
        assert svc.storage.load_pending_renames(), "前提:意圖已落地"
        assert svc.reconcile_pending_renames() == []
        assert not svc.storage.load_pending_renames()
        ids = [m["id"] for m in svc.storage.load_config()["r_members"]]
        assert "A" in ids and "R1" not in ids, "★不該把它做完★"

    def test_an_ambiguous_roster_is_not_finished_forward(self, svc,
                                                         monkeypatch):
        """★判準要能分開【處置不同】的狀況★:名單裡「只剩 old」既不是「還沒
        開始」(config 的 revision 已經變過),也不是我們的半套 —— 續作會跳過
        撞名守衛,而盤上已經有 R1 的痕跡(帳本鍵)。認不得就保留並講清楚。"""
        self._crash(svc, monkeypatch)            # 崩在寫完 config/帳本之後
        cfg = svc.storage.load_config()          # 有人把名單改回 A
        cfg["r_members"] = [{"id": "A"}, {"id": "B"}]
        svc.storage.save_config(cfg)
        assert svc.reconcile_pending_renames() == []
        assert svc.storage.load_pending_renames(), "★意圖被清掉了★"
        assert svc.storage.load_month(NOV)["r_duty"]["2026-11-03"][
            "person"] == "A", "★在看不懂的盤面上動手了★"

    def test_the_resume_is_wired_at_startup(self):
        import ast
        src = io.open(os.path.join(os.path.dirname(__file__), "..", "src",
                                   "scheduler.py"), encoding="utf-8").read()
        names = {n.func.attr for n in ast.walk(ast.parse(src))
                 if isinstance(n, ast.Call) and hasattr(n.func, "attr")}
        assert "reconcile_pending_renames" in names
