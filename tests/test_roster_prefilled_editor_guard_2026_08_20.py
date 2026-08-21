# -*- coding: utf-8 -*-
"""[批次RS-14 / 全審次輪 P1-02] prefilled editor 的 delta 不變量 + 架構性守衛。

RS-8 修對了觀念(對話框顯示的整份資料 ≠ 使用者的修改),但守衛是一張
【五個已知 API 的名單】—— 名單外的 prefilled editor(切片格網對話框、
Clerk 起始日連動平移、PGY 預設代號欄)於是原封不動。名單會腐爛;
守衛要改成「★找出 UI 對 service 的所有寫入端,每一個都必須被分類★」:
新的寫入端沒分類就先紅,分類錯了(說是 delta 卻沒帶 baseline)也紅。
"""
import ast
import inspect
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.service import RosterService                # noqa: E402
from cmuh_common.roster.storage import (                            # noqa: E402
    RosterStorage, StaleRosterDataError,
)

YM = "2026-08"


@pytest.fixture()
def svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({"r_members": [{"id": "A"}, {"id": "B"}],
                    "vs_members": [],
                    "pgy_members": [{"id": "P1"}, {"id": "P2"}]})
    st.save_clerk_batches([{"id": "b1", "start_monday": "2026-08-03",
                            "names": "甲乙"}])
    st.save_biopsy_grid({"b1": {"2026-08-03": {"上午": True},
                                "2026-08-04": {"上午": True}}})
    return RosterService(st)


# ══ A. 切片格網對話框:整梯替換 → cell delta ═══════════════════════════════
class TestBiopsyGridCellsAreAnIntent:
    BASE = {("2026-08-03", "上午"): True, ("2026-08-03", "下午"): False,
            ("2026-08-04", "上午"): True, ("2026-08-04", "下午"): False}

    def test_a_remote_cell_edit_survives_my_unrelated_edit(self, svc):
        """★反例本體★:A 開著對話框(快照=BASE),B 關掉 8/4 上午;
        A 只加 8/3 下午 —— 舊寫法整梯替換會把 8/4 上午改回開。"""
        svc.set_biopsy_cells("b1", {("2026-08-04", "上午"): False},
                             baseline={("2026-08-04", "上午"): True},
                             batch_start="2026-08-03")  # B 機
        edited = {**self.BASE, ("2026-08-03", "下午"): True}           # A 的畫面
        svc.set_biopsy_cells("b1", edited, baseline=self.BASE,
                             batch_start="2026-08-03")
        g = svc.storage.load_biopsy_grid()["b1"]
        assert g.get("2026-08-03") == {"上午": True, "下午": True}
        assert not (g.get("2026-08-04") or {}).get("上午"), \
            "★B 關掉的格子被 A 的開窗快照蓋回去了★"

    def test_a_cell_changed_on_both_sides_is_refused(self, svc):
        svc.set_biopsy_cells("b1", {("2026-08-04", "上午"): False},
                             baseline={("2026-08-04", "上午"): True},
                             batch_start="2026-08-03")  # B 機
        edited = {**self.BASE, ("2026-08-04", "上午"): False}          # A 也改它
        with pytest.raises(StaleRosterDataError, match="也被另一台"):
            svc.set_biopsy_cells("b1", edited, baseline=self.BASE,
                             batch_start="2026-08-03")

    def test_an_unchanged_dialog_writes_nothing(self, svc):
        rev = svc.storage.canonical_revision("biopsy_grid.json")
        assert svc.set_biopsy_cells("b1", dict(self.BASE),
                                    baseline=dict(self.BASE),
                                    batch_start="2026-08-03") == {}
        assert svc.storage.canonical_revision("biopsy_grid.json") == rev

    def test_clearing_the_last_session_removes_the_day_key(self, svc):
        svc.set_biopsy_cells("b1", {("2026-08-04", "上午"): False},
                             baseline={("2026-08-04", "上午"): True},
                             batch_start="2026-08-03")
        assert "2026-08-04" not in svc.storage.load_biopsy_grid()["b1"], \
            "清到空的日子不留空殼(與 override 清除同規)"


# ══ B. Clerk 起始日 → 格網平移(服務層交易,吃最新格網)═══════════════════
class TestTheGridShiftFollowsTheLatestGrid:
    def test_the_shift_is_computed_from_the_latest_grid(self, svc):
        """A 開編輯窗後,B 加開 8/5 上午;A 改起始日 +7 天 —— 平移必須帶著
        B 的新格子走,不能用 A 開窗時讀的舊格網整包蓋回。"""
        before = {"id": "b1", "start_monday": "2026-08-03", "names": "甲乙"}
        svc.update_biopsy_grid(                                     # B 機
            lambda g: g["b1"].__setitem__("2026-08-05", {"上午": True}))
        svc.update_clerk_batch_fields(
            "b1", before, dict(before, start_monday="2026-08-10"))
        g = svc.storage.load_biopsy_grid()["b1"]
        assert g == {"2026-08-10": {"上午": True},
                     "2026-08-11": {"上午": True},
                     "2026-08-12": {"上午": True}}, \
            f"★平移用了開窗時的舊格網(B 的 8/5 沒跟著移)★ {g}"

    def test_a_non_start_edit_leaves_the_grid_alone(self, svc):
        before = {"id": "b1", "start_monday": "2026-08-03", "names": "甲乙"}
        g0 = svc.storage.load_biopsy_grid()["b1"]
        svc.update_clerk_batch_fields("b1", before,
                                      dict(before, names="丙丁"))
        assert svc.storage.load_biopsy_grid()["b1"] == g0

    def test_the_move_and_the_shift_share_one_barrier(self, svc, monkeypatch):
        """★兩個正典檔要在同一個 write_barrier 裡寫★(deep R1-2):中間讓
        背景 pull 換檔,平移會拿舊位移量搬新格網。以進出計數驗證兩個
        save 都發生在同一個最外層臨界區內。"""
        import contextlib
        depth = {"d": 0}
        seen: list = []
        real_wb = svc.storage.write_barrier

        @contextlib.contextmanager
        def wb():
            depth["d"] += 1
            try:
                with real_wb():
                    yield
            finally:
                depth["d"] -= 1
        monkeypatch.setattr(svc.storage, "write_barrier", wb)
        real_scb = svc.storage.save_clerk_batches
        real_sbg = svc.storage.save_biopsy_grid
        monkeypatch.setattr(
            svc.storage, "save_clerk_batches",
            lambda *a, **k: (seen.append(("batches", depth["d"])),
                             real_scb(*a, **k))[1])
        monkeypatch.setattr(
            svc.storage, "save_biopsy_grid",
            lambda *a, **k: (seen.append(("grid", depth["d"])),
                             real_sbg(*a, **k))[1])
        before = {"id": "b1", "start_monday": "2026-08-03", "names": "甲乙"}
        svc.update_clerk_batch_fields(
            "b1", before, dict(before, start_monday="2026-08-10"))
        assert [n for n, _ in seen] == ["batches", "grid"], seen
        assert all(d >= 1 for _, d in seen), \
            f"★有寫入發生在臨界區之外★ {seen}"


# ══ B2. 格子的日期身分=梯次起始日(deep R1-1)═════════════════════════════
class TestTheCellsAreBoundToTheStartDate:
    def test_a_moved_batch_invalidates_the_dialog_cells(self, svc):
        """A 開窗(起始日 8/3)後,B 把梯次移到 8/10(格網已整組平移)。
        A 存舊日期的格子 —— 舊絕對日期在新格網裡是「窗外孤兒」,
        `build_day_input` 直接忽略它而畫面回報成功(假成功)。必須拒絕。"""
        before = {"id": "b1", "start_monday": "2026-08-03", "names": "甲乙"}
        svc.update_clerk_batch_fields(                       # B 機移梯
            "b1", before, dict(before, start_monday="2026-08-10"))
        g0 = svc.storage.load_biopsy_grid()["b1"]
        with pytest.raises(StaleRosterDataError, match="起始日.*改過"):
            svc.set_biopsy_cells("b1", {("2026-08-03", "下午"): True},
                                 baseline={("2026-08-03", "下午"): False},
                                 batch_start="2026-08-03")
        assert svc.storage.load_biopsy_grid()["b1"] == g0, \
            "★被拒就不可以留下任何孤兒格★"

    def test_a_deleted_batch_invalidates_the_dialog_cells(self, svc):
        svc.update_clerk_batches(lambda bs: bs.clear())      # B 機刪梯
        with pytest.raises(StaleRosterDataError, match="已不在清單中"):
            svc.set_biopsy_cells("b1", {("2026-08-03", "下午"): True},
                                 baseline={("2026-08-03", "下午"): False},
                                 batch_start="2026-08-03")


# ══ C. PGY 預設代號:整串覆寫 → set delta ═════════════════════════════════
class TestPgyDefaultsAreASetDelta:
    def test_a_remote_add_survives_my_add(self, svc):
        svc.set_pgy_default_members(["P1", "P2", "P4"],
                                    baseline=["P1", "P2"])           # B 機加 P4
        svc.set_pgy_default_members(["P1", "P2", "P3"],
                                    baseline=["P1", "P2"])           # A 加 P3
        ids = [m["id"] for m in
               svc.storage.load_config()["pgy_members"]]
        assert ids == ["P1", "P2", "P4", "P3"], \
            f"★B 剛加的 P4 被 A 的整串舊值明確排除了★ {ids}"

    def test_my_removal_still_removes(self, svc):
        svc.set_pgy_default_members(["P2"], baseline=["P1", "P2"])
        ids = [m["id"] for m in svc.storage.load_config()["pgy_members"]]
        assert ids == ["P2"]

    def test_a_pure_reorder_is_honored_when_nothing_changed_remotely(self, svc):
        """set delta 看不見順序 —— 盤上沒被動過時,唯一能保住使用者排序的
        分支就是「整份照打的順序採用」。"""
        svc.set_pgy_default_members(["P2", "P1"], baseline=["P1", "P2"])
        ids = [m["id"] for m in svc.storage.load_config()["pgy_members"]]
        assert ids == ["P2", "P1"]

    def test_extra_member_fields_survive_the_merge(self, svc):
        svc.update_config(lambda c: c["pgy_members"].__setitem__(
            0, {"id": "P1", "note": "x"}))
        svc.set_pgy_default_members(["P1", "P2", "P3"],
                                    baseline=["P1", "P2"])
        assert svc.storage.load_config()["pgy_members"][0] == \
            {"id": "P1", "note": "x"}, "保留的成員不得被重建成只剩 id"


# ══ D. 架構性守衛:所有 UI 寫入端都必須被分類 ═════════════════════════════
_WRITER_PREFIXES = ("set_", "update_", "save_", "accept_", "clear_",
                    "finalize", "unfinalize", "resettle", "rename_",
                    "change_", "archive_", "mark_", "delete_", "remove_",
                    "move_", "seed_", "reconcile_", "shift_")

#: UI 允許呼叫的 service 寫入端 → 它的併發政策。★沒分類的寫入端先紅★。
#: 政策的意義(審這張表時逐一對照實作):
#:   delta            送「baseline + 使用者改過的部分」,同格衝突明確拒絕
#:   delta-fields     送「before + edited」欄位 delta(update_clerk_batch_fields)
#:   desired-state    送「想要的最終值」,服務層窄寫單一目標(格/鎖/週色…)
#:   identity-guarded 整批落地,但以 input_fingerprint + month_revision 驗身分
#:   narrow-mutator   CAS mutator 在最新版上做單一元素增刪;★不得整份替換
#:                    既有 subtree★(那是 delta 的工作)
#:   create-if-absent mutator 用 setdefault:只在還沒有時建立,絕不覆蓋
#:   explicit-command 使用者明確下令的整區操作(UI 有確認對話框)
#:   stable-identity  以【那一筆的身分】(穩定 id;舊資料退回完整內容)增刪,
#:                    ★不得用畫面上的行號/index 當持久身分★(外審次輪 P2-04)
#:   artifact-export  產出衍生檔(PDF/快照),不觸碰正典資料
_CLASSIFIED = {
    "set_leaves": "delta", "set_must": "delta", "set_day_session": "delta",
    "set_pgy_month_roster": "delta", "set_pgy_apply_pref": "delta",
    "set_biopsy_cells": "delta", "set_pgy_default_members": "delta",
    "update_clerk_batch_fields": "delta-fields",
    # [外審次輪 P2-04] 門診模板改用穩定身分:新增由服務層配 id、刪除以
    # id(舊資料退回完整內容)比對 —— UI 不再交出「畫面上的第幾列」。
    "add_clinic_template_entry": "narrow-mutator",
    "delete_clinic_template_entry": "stable-identity",
    "set_cell": "desired-state", "set_lock": "desired-state",
    "set_day_lock": "desired-state", "set_week_color": "desired-state",
    "set_biopsy_person": "desired-state", "set_clinic_closed": "desired-state",
    "accept_solution": "identity-guarded",
    "accept_day_solution": "identity-guarded",
    "change_members_and_sync_ledger": "narrow-mutator",
    "update_clerk_batches": "narrow-mutator",
    "update_clinic_template": "narrow-mutator",
    "update_holiday_duty": "narrow-mutator",
    "update_ledger": "narrow-mutator",
    "update_biopsy_grid": "create-if-absent",
    "clear_unlocked": "explicit-command",
    "clear_unlocked_day": "explicit-command",
    "finalize": "explicit-command",
    "resettle_from_duty": "explicit-command",
    "rename_member": "explicit-command",
    "archive_finalize_pdf": "artifact-export",
}


def _ui_modules():
    from cmuh_common.roster.ui import common, day_tab, duty, settings
    return (common, day_tab, duty, settings)


def _service_calls():
    """→ [(module_name, lineno, method_name, Call node)]:UI 對 service 的呼叫。"""
    out = []
    for mod in _ui_modules():
        tree = ast.parse(inspect.getsource(mod))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)):
                continue
            v = node.func.value
            if (isinstance(v, ast.Attribute) and v.attr == "service") or \
                    (isinstance(v, ast.Name) and v.id == "service"):
                out.append((mod.__name__, node.lineno, node.func.attr, node))
    return out


def _storage_calls():
    out = []
    for mod in _ui_modules():
        tree = ast.parse(inspect.getsource(mod))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)):
                continue
            v = node.func.value
            if isinstance(v, ast.Attribute) and v.attr == "storage":
                out.append((mod.__name__, node.lineno, node.func.attr, node))
    return out


class TestEveryUiWriterIsClassified:
    def test_no_unclassified_writer(self):
        """★新的寫入端要先分類才能上★ —— RS-8 的名單漏掉切片格網/PGY 欄位,
        就是因為守衛只認名單、不認「所有寫入端」。"""
        bad = [f"{m}:{ln} {name}"
               for m, ln, name, _ in _service_calls()
               if name.startswith(_WRITER_PREFIXES)
               and name not in _CLASSIFIED]
        assert not bad, (
            f"★這些 UI 寫入端還沒被分類★ {bad}\n"
            f"請在 _CLASSIFIED 加上它與它的併發政策(delta / desired-state /"
            f" narrow-mutator …),並確認實作真的符合那個政策。")

    def test_every_classified_writer_still_exists(self):
        """反向:分類表不得留幽靈(改名/刪除後表要跟著動,否則表會腐爛)。"""
        for name in _CLASSIFIED:
            assert hasattr(RosterService, name), f"★{name} 已不存在★"

    def test_delta_writers_always_carry_a_baseline(self):
        bad = []
        for m, ln, name, node in _service_calls():
            if _CLASSIFIED.get(name) != "delta":
                continue
            if not any(k.arg == "baseline" for k in node.keywords):
                bad.append(f"{m}:{ln} {name}")
        assert not bad, f"★delta 寫入端沒帶 baseline★ {bad}"

    def test_delta_field_writers_carry_before_and_edited(self):
        for m, ln, name, node in _service_calls():
            if _CLASSIFIED.get(name) != "delta-fields":
                continue
            assert len(node.args) + len(node.keywords) >= 3, \
                f"{m}:{ln} {name} 沒有送 before+edited"

    def test_create_if_absent_writers_use_setdefault(self):
        """update_biopsy_grid 在 UI 僅剩「建新梯種子」一處:mutator 必須是
        setdefault(只在還沒有時建立)。整份 __setitem__ 會把先建好的蓋掉。"""
        sites = [(m, ln, node) for m, ln, name, node in _service_calls()
                 if name == "update_biopsy_grid"]
        assert len(sites) == 1, (
            f"UI 直呼 update_biopsy_grid 的位置變了:{[(m, ln) for m, ln, _ in sites]}"
            f" —— 新增位置請改用 set_biopsy_cells(delta)或說明政策")
        _, _, node = sites[0]
        lam = node.args[0]
        assert isinstance(lam, ast.Lambda) and \
            isinstance(lam.body, ast.Call) and \
            isinstance(lam.body.func, ast.Attribute) and \
            lam.body.func.attr == "setdefault", \
            "★種子寫入不是 setdefault —— 兩機同時建梯會互相覆蓋★"

    def test_ui_never_writes_storage_directly(self):
        """storage 的寫入必須經過 service(CAS/臨界區/意圖都在那一層)。
        唯一的既存例外:settings 的 `_save_cfg`(整份 config 的 CAS 存檔,
        refresh-before-edit;RS-5 已把 revision 與內容綁在同一次讀)。
        ★這個例外只准縮小,不准長大★。"""
        writes = [(m, ln, name) for m, ln, name, _ in _storage_calls()
                  if name.startswith(("save_", "update_", "delete_",
                                      "remove_", "mark_", "write_"))]
        legacy = [(m, ln, name) for m, ln, name in writes
                  if name == "save_config"
                  and m.endswith("settings")]
        assert writes == legacy and len(legacy) <= 1, (
            f"★UI 繞過 service 直接寫 storage★ {writes}\n"
            f"(既存例外僅 settings._save_cfg 的 save_config 一處)")


# ══ E. 起始日平移已收進服務層(UI 不再自己算 payload)═════════════════════
def test_the_ui_no_longer_shifts_the_grid_itself():
    from cmuh_common.roster.ui import settings as s
    src = inspect.getsource(s)
    assert "_shift_biopsy_grid" not in src, \
        "★UI 又長出自己的平移(stale payload 的病灶)★"
    assert "update_clerk_batch_fields" in src


def test_the_service_shift_is_wired_into_the_field_update(tmp_path):
    """`update_clerk_batch_fields` 改 start_monday 就要順帶平移 —— 分開呼叫
    的話,呼叫端漏第二步,格網就永遠停在舊日期。"""
    st = RosterStorage(str(tmp_path))
    st.save_config({"r_members": [], "vs_members": []})
    st.save_clerk_batches([{"id": "b9", "start_monday": "2026-08-03"}])
    st.save_biopsy_grid({"b9": {"2026-08-03": {"上午": True}}})
    svc = RosterService(st)
    before = {"id": "b9", "start_monday": "2026-08-03"}
    svc.update_clerk_batch_fields("b9", before,
                                  dict(before, start_monday="2026-08-17"))
    assert svc.storage.load_biopsy_grid()["b9"] == \
        {"2026-08-17": {"上午": True}}
    assert date.fromisoformat(
        svc.storage.load_clerk_batches()[0]["start_monday"]).isoformat() \
        == "2026-08-17"
