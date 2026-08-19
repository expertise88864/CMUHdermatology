# -*- coding: utf-8 -*-
"""[批次RS-5 / 排班審R2 P1-01] CAS 不能只做在月檔。

`config.json`、`ledger.json`、假日指定、模板、Clerk 梯次…… 每一個都是
「讀整份 → 改 → 寫整份回去」,而背景 pull 會在中間把檔案換成他機的新版本。
設定頁雖然已經「存檔前先與磁碟對齊」,★但對齊與寫回之間仍不是原子的★——
他機剛新增的成員因此被靜默移除,接著 `_sync_ledger` 還會把那個人的餘額與
歷史當成「已離職」作廢。
"""
import contextlib
import inspect
import threading

import pytest

from cmuh_common.roster.service import RosterService
from cmuh_common.roster.storage import RosterStorage, StaleRosterDataError


@pytest.fixture()
def svc(tmp_path):
    st = RosterStorage(str(tmp_path / "roster"))
    st.save_config({"r_members": [{"id": "K"}], "vs_members": [],
                    "pgy_members": [], "clerk_members": []})
    st.save_ledger({"r": {"K": 0.0}, "vs": {}, "history": []})
    return RosterService(st)


@contextlib.contextmanager
def _remote_write_after_read(st, name, remote):
    """★他機的寫入要落在【讀完之後、寫回之前】★

    落在讀之前的話,mutator 本來就會看到它 —— 那個反例分不出「有沒有 CAS」。
    注入點是生產程式碼真正的讀取點(`canonical_snapshot`),不是某個載入器的
    名字;否則實作換一種讀法,反例就悄悄失去對抗性。只注入一次,免得重試時
    又寫一次而永遠追不上。
    """
    fired: list = []
    real = st.canonical_snapshot

    def _hook(n):
        out = real(n)
        if n == name and not fired:
            fired.append(True)
            remote()
        return out

    st.canonical_snapshot = _hook                            # type: ignore
    try:
        yield
    finally:
        st.canonical_snapshot = real                         # type: ignore
    assert fired, "★他機的寫入根本沒有發生 —— 這個反例沒有對抗性★"


def _add_member(st, mid):
    def _do():
        cfg = st.load_config()
        cfg["r_members"].append({"id": mid})
        st.save_config(cfg)
    return _do


class TestTheOtherMachinesEditSurvives:

    def test_a_remote_member_is_not_wiped_by_my_add(self, svc):
        """A 新增 C、B 新增 F —— 兩個人都要在。"""
        st = svc.storage
        with _remote_write_after_read(st, "config.json",
                                      _add_member(st, "F")):
            svc.update_config(
                lambda cfg: cfg["r_members"].append({"id": "C"}))
        ids = [m["id"] for m in st.load_config()["r_members"]]
        assert set(ids) == {"K", "C", "F"}, (
            f"★他機新增的成員被整份 config 覆寫掉了★ {ids}")

    def test_a_remote_ledger_entry_is_not_wiped(self, svc):
        st = svc.storage

        def _remote():
            led = st.load_ledger()
            led["r"]["他機剛結算的人"] = 3.0
            st.save_ledger(led)

        with _remote_write_after_read(st, "ledger.json", _remote):
            svc.update_ledger(lambda led: led["r"].update({"K": 1.0}))
        led = st.load_ledger()
        assert led["r"]["K"] == 1.0
        assert led["r"]["他機剛結算的人"] == 3.0, "★他機的結算被吃掉★"


class TestTheCasCoversEveryCanonicalFile:

    def test_every_canonical_saver_accepts_expected_revision(self):
        """★漏一個就等於那個檔沒有保護★:用登記表逐一檢查,不是挑幾個看。"""
        savers = {
            "config.json": RosterStorage.save_config,
            "ledger.json": RosterStorage.save_ledger,
            "biopsy.json": RosterStorage.save_biopsy,
            "week_colors.json": RosterStorage.save_week_colors,
            "holiday_duty.json": RosterStorage.save_holiday_duty,
            "clinic_template.json": RosterStorage.save_clinic_template,
            "clerk_batches.json": RosterStorage.save_clerk_batches,
            "biopsy_grid.json": RosterStorage.save_biopsy_grid,
        }
        assert set(savers) == set(RosterStorage.CANONICAL_FILES), \
            "★登記表與這裡的清單不一致 —— 新增正典檔要兩邊都補★"
        for name, fn in savers.items():
            params = inspect.signature(fn).parameters
            assert "expected_revision" in params, \
                f"★{name} 的 save 沒有 CAS 參數★"

    def test_a_stale_revision_is_refused(self, svc):
        st = svc.storage
        rev = st.canonical_revision("config.json")
        cfg = st.load_config()
        st.save_config({**cfg, "note": "他機改過了"})       # 盤上前進一版
        with pytest.raises(StaleRosterDataError):
            st.save_config(cfg, expected_revision=rev)

    def test_an_unregistered_file_cannot_ask_for_a_revision(self, svc):
        with pytest.raises(KeyError):
            svc.storage.canonical_revision("months/2026-09.json")


class TestTheBaseMustBeTrustworthy:

    def test_a_corrupt_file_aborts_the_edit(self, svc, tmp_path):
        """★2026-07-25 的教訓不可以因為改成 mutator 而重新引進★:
        `load_*` 對壞檔靜默回空,拿它當基底再寫回去,一次新增成員就把整份
        設定清成只剩那一個人。"""
        path = svc.storage._path("config.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ 這不是 JSON")
        before = open(path, encoding="utf-8").read()
        with pytest.raises(ValueError):
            svc.update_config(lambda cfg: cfg.setdefault(
                "r_members", []).append({"id": "C"}))
        assert open(path, encoding="utf-8").read() == before, \
            "★壞檔時磁碟必須原封不動★"


class TestMultiFileOperationsAreOneCriticalSection:

    def test_rename_member_holds_the_barrier(self):
        src = inspect.getsource(RosterService.rename_member)
        assert "self.storage.write_barrier()" in src, \
            "★改名要動 config+帳本+假日+切片+所有月份,必須整段在臨界區內★"
        assert "_rename_member_locked" in src

    def test_nobody_can_write_while_a_rename_runs(self, svc):
        """真的用執行緒量:改名進行中,他機/他緒的正典檔寫入進不來。"""
        done: list = []
        started = threading.Event()

        def _other():
            started.set()
            svc.storage.save_clerk_batches([{"id": "b9",
                                             "start_monday": "2026-08-31"}])
            done.append(True)

        with svc.storage.write_barrier():
            t = threading.Thread(target=_other)
            t.start()
            assert started.wait(timeout=5)
            t.join(timeout=0.6)
            assert not done, "★改名進行中,別人的寫入插了進來★"
        t.join(timeout=10)
        assert done, "臨界區結束後對方要能完成(不是被永久擋住)"


class TestTheSettingsTabUsesTheMutatorApi:
    """★沒有呼叫端就等於沒有這個保護★:service 有 mutator API 而 UI 仍整份
    寫回,缺陷原封不動。"""

    def test_member_changes_go_through_the_guarded_service_op(self):
        """★名單變更與帳本同步是一件事★:分開做的話,帳本那一步會拿呼叫端
        事先算好的舊名單去 `sync_members` —— 他機剛新增的成員因此被當成離職者,
        餘額與所有 history delta 被永久刪除。"""
        from cmuh_common.roster.ui import settings as mod
        for name in ("_member_add", "_member_del"):
            src = inspect.getsource(getattr(mod.SettingsTab, name))
            assert "change_members_and_sync_ledger(" in src, \
                f"★{name} 沒有走受臨界區保護的名單+帳本操作★"
            assert "storage.save_config(" not in src, f"★{name} 還在整份寫回★"

    def test_the_service_op_derives_ids_from_the_fresh_config(self):
        src = inspect.getsource(RosterService.change_members_and_sync_ledger)
        assert "self.storage.write_barrier()" in src
        i_cfg = src.index("cfg = self.storage.load_config()")
        i_ids = src.index("ids = ")
        i_sync = src.index("sync_members(")
        assert i_cfg < i_ids < i_sync, \
            "★ids 必須由【寫成功之後重讀的】config 推導★"

    def test_a_remotely_added_member_keeps_its_ledger(self, svc):
        """★真的跑破壞性的 `sync_members`★(不是良性的 dict.update):
        他機新增 F 並結算之後,本機新增 C 不可以把 F 的餘額刪掉。"""
        st = svc.storage

        def _remote():
            _add_member(st, "F")()
            led = st.load_ledger()
            led["r"]["F"] = 5.0
            st.save_ledger(led)

        with _remote_write_after_read(st, "config.json", _remote):
            svc.change_members_and_sync_ledger(
                "r", lambda cfg: cfg["r_members"].append({"id": "C"}))
        led = st.load_ledger()
        assert led["r"].get("F") == 5.0, \
            "★他機新增的成員被當成離職者,帳本餘額被刪掉★"
        assert "C" in led["r"], "自己新增的人要補 0"


class TestNoCanonicalWriteEscapesTheCas:
    """★機械化守衛★:這是一整類缺陷,不是幾個位置。UI 只要還有任何一處直接
    `storage.save_<正典檔>(...)` 而不帶 `expected_revision`,那個檔就等於沒有
    跨機保護 —— 而且新增的編輯路徑很容易又漏掉。"""

    def test_the_settings_tab_has_no_unguarded_canonical_write(self):
        import re

        from cmuh_common.roster.ui import settings as mod
        src = inspect.getsource(mod)
        names = "|".join(n[:-5] for n in RosterStorage.CANONICAL_FILES)
        bad = []
        for m in re.finditer(r"storage\.save_(" + names + r")\(", src):
            # 取這一個呼叫到收尾括號為止的片段(夠涵蓋多行參數)
            seg = src[m.start():m.start() + 400]
            if "expected_revision" not in seg.split("\n\n")[0]:
                bad.append(src[:m.start()].count("\n") + 1)
        assert not bad, \
            f"★settings.py 這幾行仍是沒有 CAS 的整份寫回★ 行號 {bad}"

    def test_the_biopsy_book_is_written_with_a_revision(self):
        """切片帳本的正式寫入(手改值班/清除/手動指定/套用排班)都要帶 revision。

        ★唯一的例外是改名的【回滾】★:回滾時盤上那一份就是我們剛寫進去的,
        拿原始 revision 去比一定不符 —— 帶 CAS 會讓回滾自己失敗、留下半套改名
        (月檔那一側早就是這個結論)。改名整段在臨界區內,由另一條測試釘住。
        """
        src = inspect.getsource(RosterService)
        rollback = inspect.getsource(RosterService._rename_member_locked)
        lines = src.splitlines()
        for i, line in enumerate(lines, 1):
            if "storage.save_biopsy(" not in line:
                continue
            if line.strip() in rollback:            # 改名的回滾路徑,見上
                continue
            seg = "\n".join(lines[i - 1:i + 2])
            assert "expected_revision" in seg, \
                f"★service.py 第 {i} 行的切片帳本寫入沒有 CAS★"


# ══ 第 2 輪外審 ═════════════════════════════════════════════════════════
class TestTheBaseAndTheRevisionAreTheSameBytes:
    """★「先嚴格檢查、再算 revision、再 load」是三次獨立的讀取★

    (外審排班 RS-5 第 2 輪 P2)三次之間任何一次換入損壞內容,寬鬆 `load_*`
    回空、revision 又取自那份壞的 —— CAS 兩邊對得上就放行,一次編輯把整份
    設定改成只剩這一筆。每輪重做檢查、讀完再比一次 revision 都封不住:
    它們各自又是一次新的讀取。
    """

    def test_the_snapshot_reads_the_file_exactly_once(self, svc, monkeypatch):
        """★這就是那個性質本身★:多讀一次就多一個可以被換掉的窗口。"""
        from cmuh_common.roster import storage as mod
        reads: list = []
        real = mod._read_bytes
        monkeypatch.setattr(
            mod, "_read_bytes",
            lambda path: (reads.append(path), real(path))[1])
        svc.storage.canonical_snapshot("config.json")
        mine = [r for r in reads if r.endswith("config.json")]
        assert len(mine) == 1, f"★config.json 被讀了 {len(mine)} 次★"

    def test_a_corrupt_snapshot_raises_instead_of_returning_empty(self, svc):
        path = svc.storage._path("config.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ 這不是 JSON")
        with pytest.raises(ValueError):
            svc.storage.canonical_snapshot("config.json")

    def test_content_swapped_in_mid_edit_never_becomes_an_empty_overwrite(
            self, svc, monkeypatch):
        """★真正的反例★:第一次讀到好的、之後每一次讀都讀到壞的。

        舊寫法(檢查/revision/load 各讀一次)會在第二輪把 revision 與空資料
        都取自那份壞內容 → CAS 對得上 → 把整份名單寫成只剩新增的那一位。
        磁碟上的檔案自始至終是好的,所以 `_guard_overwrite` 也擋不住。
        """
        from cmuh_common.roster import storage as mod
        real = mod._read_bytes
        seen: list = []

        def _flaky(path):
            out = real(path)
            if not str(path).endswith("config.json"):
                return out
            seen.append(path)
            return out if len(seen) == 1 else b"{ ***"

        monkeypatch.setattr(mod, "_read_bytes", _flaky)
        with pytest.raises((ValueError, StaleRosterDataError)):
            svc.update_config(
                lambda cfg: cfg["r_members"].append({"id": "C"}))
        monkeypatch.undo()
        ids = [m["id"] for m in svc.storage.load_config()["r_members"]]
        assert "K" in ids, f"★原本的名單被空資料覆寫掉了★ {ids}"

    def test_every_canonical_file_has_a_snapshot(self, svc):
        """★漏一個就等於那個檔沒有這個保護★——逐一真的呼叫,不是看清單。"""
        for name in RosterStorage.CANONICAL_FILES:
            data, rev = svc.storage.canonical_snapshot(name)
            assert data is not None
            assert rev == svc.storage.canonical_revision(name)
