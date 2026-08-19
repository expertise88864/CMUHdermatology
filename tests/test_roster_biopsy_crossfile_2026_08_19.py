# -*- coding: utf-8 -*-
"""[批次RS-5 / 排班審R2 第 2 輪] 月檔與切片帳本是【兩個檔】。

第 1 輪替切片帳本補上 CAS 之後,冒出兩個新問題 —— 兩個都是「修正本身要
放進整體看」的例子:

  P1-1 `recompute_saturday_biopsy` 改成回傳四個值,`set_biopsy_person` 那一處
       忘了跟著改 → 每一次手動指定切片人選都拋 ValueError,又剛好被它自己的
       `except Exception` 吞掉:月檔存下了 `biopsy_override`,而 saturday_biopsy
       與 biopsy.json 停在舊人選。★那個指定永遠不會生效,也沒有人看得出來。★
  P1-2 月檔先寫、切片帳本後寫,兩者之間他機更新 biopsy.json 的話,CAS 正確
       擋下帳本、月檔卻已經落地 —— 兩個檔當場互相矛盾。CAS 把「靜默覆蓋」
       換成了「半套寫入」,不放進整體看就會以為問題解決了。
"""
import ast
import inspect
import os
import sys
import threading
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.service import RosterService  # noqa: E402
from cmuh_common.roster.storage import RosterStorage  # noqa: E402

SAT = date(2026, 8, 8)                      # 2026-08-01 起每個週六
SAT_ISO = SAT.isoformat()


@pytest.fixture()
def svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({"r_members": [{"id": "r1", "name": "甲", "level": "R1"},
                                  {"id": "r2", "name": "乙", "level": "R2"},
                                  {"id": "r3", "name": "丙", "level": "R3"}]})
    st.save_month("2026-08", {"r_duty": {
        SAT_ISO: {"person": "r1", "locked": False, "source": "test"}}})
    return RosterService(st)


def _person(st, iso=SAT_ISO):
    cell = (st.load_month("2026-08").get("saturday_biopsy") or {}).get(iso)
    return (cell or {}).get("person")


class TestAManualPickActuallyLands:

    def test_the_month_and_the_book_both_get_the_pick(self, svc):
        """★整條路徑真的跑一次★:少解一個回傳值就會拋 ValueError 而被吞掉,
        單看「save_biopsy 有沒有帶 revision」的守衛量不到 —— 那一行根本不會
        被執行到。"""
        svc.set_biopsy_person("2026-08", SAT, "r3")
        assert _person(svc.storage) == "r3", \
            "★月檔的 saturday_biopsy 沒有跟著手動指定改★"
        book = svc.storage.load_biopsy()
        assert book["counts"].get("r3"), "★切片帳本沒有記到這次指定★"
        assert any(h.get("month") == "2026-08" for h in book["history"])

    def test_a_failed_recompute_refuses_the_whole_thing(self, svc,
                                                        monkeypatch):
        """★重排做不到就整個拒絕★:留下 override 而 saturday_biopsy 沒動,
        等於存了一個永遠不會生效的指定(本函式的既有契約)。"""
        monkeypatch.setattr(
            svc, "recompute_saturday_biopsy",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("排不出來")))
        with pytest.raises(RuntimeError):
            svc.set_biopsy_person("2026-08", SAT, "r3")
        month = svc.storage.load_month("2026-08")
        assert not month.get("biopsy_override"), \
            "★重排失敗,月檔卻留下了指定★"

    def test_every_unpack_matches_the_return_arity(self):
        """★機械化★:回傳值個數變了,漏改的那一處要當場紅,不能靠人眼。"""
        src = inspect.getsource(
            sys.modules["cmuh_common.roster.service"])
        want = len(inspect.getsource(
            RosterService._recompute_saturday_biopsy_locked
        ).rsplit("return ", 1)[1].split(","))
        bad = []
        for node in ast.walk(ast.parse(src)):
            if not isinstance(node, ast.Assign):
                continue
            call = node.value
            if not (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and call.func.attr == "recompute_saturday_biopsy"):
                continue
            for tgt in node.targets:
                if isinstance(tgt, ast.Tuple) and len(tgt.elts) != want:
                    bad.append((node.lineno, len(tgt.elts)))
        assert not bad, f"★解包個數與回傳值({want})不符★ {bad}"


class TestTheMonthAndTheBookAreWrittenTogether:
    """他機在「月檔已寫、切片帳本還沒寫」的空隙更新 biopsy.json。"""

    def _competitor(self, st, started, held):
        def _run():
            started.set()
            with st.write_barrier():           # 他機/他緒也走同一個臨界區
                held.append(True)
                book = st.load_biopsy()
                book["counts"]["r9"] = 99
                st.save_biopsy(book)
        return _run

    def test_set_cell_does_not_leave_a_half_write(self, svc):
        st = svc.storage
        started, held, spawned = threading.Event(), [], []
        real = st.save_month

        def _hooked(ym, month, **kw):
            out = real(ym, month, **kw)
            t = threading.Thread(
                target=self._competitor(st, started, held), daemon=True)
            t.start()
            assert started.wait(timeout=5)
            t.join(timeout=0.6)                # ★這一刻正是那個空隙★
            spawned.append(t)
            return out

        st.save_month = _hooked                # type: ignore
        try:
            svc.set_cell("r", "2026-08", SAT, "r2")   # 不得拋 Stale
        finally:
            st.save_month = real               # type: ignore
        spawned[-1].join(timeout=10)
        book = st.load_biopsy()
        assert book["counts"].get("r9") == 99, "對方最後要寫得進來"
        assert any(h.get("month") == "2026-08" for h in book["history"]), \
            "★月檔改了,切片帳本卻沒有跟上 —— 兩個檔從此不一致★"
        assert _person(st), "月檔的 saturday_biopsy 應已重排"

    def test_the_self_loading_path_is_also_one_critical_section(self, svc):
        """★同一條規則要涵蓋【所有】會寫這兩個檔的路徑★

        `recompute_saturday_biopsy(ym)`(自行 load 的那條)一樣是「先寫月檔、
        再寫切片帳本」,而它的呼叫端(請假變動、重新結算)還把例外整個吞掉 ——
        不一致會安靜地留在磁碟上。只修呼叫端那三處等於漏掉這一條。
        """
        st = svc.storage
        started, held, once = threading.Event(), [], []
        real = st.save_month

        def _hooked(ym, month, **kw):
            out = real(ym, month, **kw)
            # ★對抗要落在【重排那一次】存檔之後★:請假本身也會存一次月檔,
            #   在那一次放對方進來的話,重排是在對方寫完之後才取 revision ——
            #   有沒有臨界區都不會出事,反例量不到任何東西。
            if "saturday_biopsy" in month and not once:
                once.append(True)
                t = threading.Thread(
                    target=self._competitor(st, started, held), daemon=True)
                t.start()
                assert started.wait(timeout=5)
                t.join(timeout=0.6)
                once.append(t)
            return out

        st.save_month = _hooked                # type: ignore
        try:
            svc.set_leaves("r", "2026-08", "r2", [date(2026, 8, 10)])
        finally:
            st.save_month = real               # type: ignore
        once[-1].join(timeout=10)
        book = st.load_biopsy()
        assert book["counts"].get("r9") == 99
        assert any(h.get("month") == "2026-08" for h in book["history"]), \
            "★月檔重排了,切片帳本卻被 CAS 擋下而沒有跟上(例外還被吞掉)★"
