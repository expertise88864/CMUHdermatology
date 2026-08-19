# -*- coding: utf-8 -*-
"""[批次RS-8 / 排班審R2 P1-02 + P2-02] 對話框送回來的整份快照不是「意圖」。

視窗開著的期間他機同步進來的請假/名單/格子內容,會被那份【開窗當時的】
快照整包覆蓋 —— 而且是★合法地★覆蓋:月檔的 CAS 只看得到「整份月檔有沒有
被換過」,看不到「這個欄位的值是誰的意圖」。使用者按下確定時的意思是
「我加了這幾天、拿掉了這幾天」,不是「這個月只有這幾天」。
請假直接餵給求解器:★被吃掉的那一天,該休的人會被排班★。

鎖定/週色那一類「切換」也是同一個病灶的另一面:對【使用者看不到的最新值】
取反,他機剛鎖起來時使用者按「鎖定」反而幫忙解鎖,而畫面刷新後看起來正常。

★本檔刻意不走 tests/roster_edit_helpers.py★:那些輔助以「現在盤上的那一份」
當基準,基準永遠等於盤上,這裡的反例就什麼都量不到。
"""
import inspect
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.roster.service import (  # noqa: E402
    RosterService, changed_entries, merge_set_edit,
)
from cmuh_common.roster.storage import (  # noqa: E402
    RosterStorage, StaleRosterDataError,
)

YM = "2026-08"
D3, D5, D9 = date(2026, 8, 3), date(2026, 8, 5), date(2026, 8, 9)


@pytest.fixture()
def svc(tmp_path):
    st = RosterStorage(str(tmp_path))
    st.save_config({"r_members": [{"id": "A"}, {"id": "B"}],
                    "vs_members": [], "pgy_members": [{"id": "P1"}]})
    st.save_month(YM, {})
    return RosterService(st)


class TestTheOtherMachinesEditSurvivesTheDialog:

    def test_a_remote_leave_is_not_wiped_by_my_leave_edit(self, svc):
        """★核心反例★ 我開著視窗時只看到 8/3;他機替同一人加了 8/9。
        我加上 8/5 按確定 —— 8/9 必須還在(否則那天他會被排班)。"""
        svc.set_leaves("r", YM, "A", {D3}, baseline=set())
        baseline = {D3}                       # 我的視窗顯示的就是這一份
        svc.set_leaves("r", YM, "A", {D3, D9}, baseline={D3})   # 他機
        svc.set_leaves("r", YM, "A", {D3, D5}, baseline=baseline)
        assert set(svc.get_leaves("r", YM, "A")) == {D3, D5, D9}, \
            "★他機剛同步進來的請假被整份快照吃掉了★"

    def test_my_removal_still_removes(self, svc):
        """使用者看見 8/3 而且明確取消了它 → 就是要拿掉(不可以又被合併回來)。"""
        svc.set_leaves("r", YM, "A", {D3, D5}, baseline=set())
        svc.set_leaves("r", YM, "A", set(), baseline={D3, D5})
        assert set(svc.get_leaves("r", YM, "A")) == set()

    def test_must_duty_uses_the_same_rule(self, svc):
        svc.set_must("r", YM, "B", {D3}, baseline=set())
        svc.set_must("r", YM, "B", {D3, D9}, baseline={D3})     # 他機
        svc.set_must("r", YM, "B", {D3, D5}, baseline={D3})
        ctx = svc.build_context("r", YM)
        assert set(ctx.must_duty.get("B") or ()) == {D3, D5, D9}

    def test_a_remote_pgy_member_is_not_wiped(self, svc):
        """他機把 P9 加進當月 PGY → 我這邊的視窗看不到他,但不可以刪掉他
        (刪掉的話,他明天就不會出現在日排班的候選名單裡)。"""
        svc.set_pgy_month_roster(YM, ["P1"], baseline=[])
        svc.set_pgy_month_roster(YM, ["P1", "P9"], baseline=["P1"])  # 他機
        svc.set_pgy_month_roster(YM, ["P1", "P2"], baseline=["P1"])
        cur = svc.storage.load_month(YM)["pgy_month_roster"]
        assert set(cur) == {"P1", "P2", "P9"}, cur

    def test_the_apply_pref_limit_is_rechecked_after_the_merge(self, svc):
        """兩邊各自合法、合起來超過 2 位 → ★明確拒絕★,不可以自己挑掉一個。"""
        svc.set_pgy_apply_pref(YM, ["P1"], baseline=[])
        svc.set_pgy_apply_pref(YM, ["P1", "P8"], baseline=["P1"])   # 他機
        with pytest.raises(ValueError, match="超過 2 位"):
            svc.set_pgy_apply_pref(YM, ["P1", "P2"], baseline=["P1"])
        assert set(svc.storage.load_month(YM)["pgy_apply_pref"]) == {"P1", "P8"}


class TestTheDayEditDialogOnlyWritesWhatWasTouched:

    def _slots(self, svc, session="上午"):
        return (((svc.storage.load_month(YM).get("day_slots") or {})
                 .get(D3.isoformat()) or {}).get(session)) or {}

    def test_an_untouched_slot_does_not_revert_a_remote_change(self, svc):
        """★輸入框裡顯示的是開窗當時的值★:原封送回去 = 把他機的修改退回。"""
        svc.set_day_session(YM, D3, "上午", {"101": ["P1"], "照光": ["P2"]},
                            baseline={})
        opened = {"101": ["P1"], "照光": ["P2"]}       # 我的視窗看到的
        svc.set_day_session(YM, D3, "上午", {"101": ["P9"]},
                            baseline={"101": ["P1"]})  # 他機改了 101
        # 我只動了「照光」,101 那一格連碰都沒碰 → 不可以把 P9 蓋回 P1
        svc.set_day_session(YM, D3, "上午",
                            {"101": ["P1"], "照光": ["P3"]}, baseline=opened)
        cur = self._slots(svc)
        assert cur["101"] == ["P9"], f"★沒碰過的格子把他機的修改退回了★ {cur}"
        assert cur["照光"] == ["P3"]

    def test_a_touched_slot_that_also_changed_remotely_is_refused(self, svc):
        """兩個互相衝突的意圖 → 程式沒有立場替使用者選一個。"""
        svc.set_day_session(YM, D3, "上午", {"101": ["P1"]}, baseline={})
        svc.set_day_session(YM, D3, "上午", {"101": ["P9"]},
                            baseline={"101": ["P1"]})              # 他機
        with pytest.raises(StaleRosterDataError, match="衝突"):
            svc.set_day_session(YM, D3, "上午", {"101": ["P2"]},
                                baseline={"101": ["P1"]})
        assert self._slots(svc)["101"] == ["P9"], "★被拒絕就不可以留下半套★"

    def test_a_no_op_save_still_writes_nothing(self, svc):
        svc.set_day_session(YM, D3, "上午", {"101": ["P1"]}, baseline={})
        n = svc.set_day_session(YM, D3, "上午", {"101": ["P1"]},
                                baseline={"101": ["P1"]})
        assert n == 0


class TestTheIntentIsAbsoluteNotRelative:
    """★送「想要的狀態」,不是「反過來」★(P2-02)"""

    def test_locking_a_cell_someone_else_just_locked_keeps_it_locked(
            self, svc):
        svc.storage.save_month(YM, {"r_duty": {
            D3.isoformat(): {"person": "A", "locked": False}}})
        # 使用者看到「未鎖定」→ 按下「鎖定」;他機在這中間已經先鎖了
        svc.set_lock("r", YM, D3, True)        # 他機
        assert svc.set_lock("r", YM, D3, True) is True
        cell = svc.storage.load_month(YM)["r_duty"][D3.isoformat()]
        assert cell["locked"] is True, "★按「鎖定」反而把它解鎖了★"

    def test_locking_an_empty_cell_is_still_refused(self, svc):
        svc.storage.save_month(YM, {"r_duty": {}})
        assert svc.set_lock("r", YM, D3, True) is False

    def test_day_lock_takes_the_desired_state(self, svc):
        svc.set_day_lock(YM, D3, "上午", True)    # 他機
        assert svc.set_day_lock(YM, D3, "上午", True) is True
        assert svc.is_day_locked(YM, D3, "上午") is True

    def test_week_color_takes_the_desired_color(self, svc):
        svc.set_week_color(2026, "2026-W33", "green")     # 他機
        # 使用者看到的是「無覆蓋」→ 循環的下一個是 pink,他要的就是 pink
        assert svc.set_week_color(2026, "2026-W33", "pink") == "pink"
        assert svc.storage.load_week_colors()["2026-W33"] == "pink"

    def test_an_unknown_color_is_refused(self, svc):
        with pytest.raises(ValueError):
            svc.set_week_color(2026, "2026-W33", "紫")


class TestTheMergeRuleItself:

    def test_only_the_users_own_adds_and_removes_are_applied(self):
        assert merge_set_edit({1, 2, 9}, {1, 2}, {1, 3}) == {1, 3, 9}
        assert merge_set_edit({1, 2}, {1, 2}, {1, 2}) == {1, 2}
        assert merge_set_edit(set(), set(), {5}) == {5}

    def test_untouched_entries_are_not_written_back(self):
        got = changed_entries({"a": ["x"], "b": ["y"]},
                              {"a": ["x"], "b": ["z"]})
        assert got == {"b": ["z"]}
        assert changed_entries({"a": []}, {"a": None}) == {}


class TestNoCallerCanSkipTheBaseline:
    """★機械化守衛★:漏一個呼叫端,那個欄位就等於沒有這個保護。"""

    OPS = ("set_leaves", "set_must", "set_day_session",
           "set_pgy_month_roster", "set_pgy_apply_pref")

    def test_the_service_ops_require_a_baseline(self):
        for name in self.OPS:
            sig = inspect.signature(getattr(RosterService, name))
            p = sig.parameters.get("baseline")
            assert p is not None, f"★{name} 沒有 baseline 參數★"
            assert p.kind is inspect.Parameter.KEYWORD_ONLY
            assert p.default is inspect.Parameter.empty, \
                f"★{name} 的 baseline 有預設值 → 呼叫端可以不寫,守衛形同虛設★"

    def test_the_ui_passes_a_baseline_everywhere(self):
        """★用 AST 逐個呼叫看,不要用字串視窗★:兩個呼叫寫在一起時,
        「往後看幾百個字有沒有 baseline=」會撈到隔壁那一個的 —— 少帶基準的
        那一個因此照樣綠(第一版就是這樣,突變沒轉紅才發現)。"""
        import ast

        from cmuh_common.roster.ui import day_tab, duty, settings
        bad = []
        for mod in (day_tab, duty, settings):
            tree = ast.parse(inspect.getsource(mod))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and node.func.attr in self.OPS):
                    continue
                if not any(k.arg == "baseline" for k in node.keywords):
                    bad.append(f"{mod.__name__}:{node.lineno} "
                               f"{node.func.attr}")
        assert not bad, f"★這些呼叫沒有帶基準★ {bad}"


    def test_no_toggle_style_api_is_left(self):
        for name in ("toggle_lock", "toggle_day_lock", "toggle_week_color"):
            assert not hasattr(RosterService, name), \
                f"★{name} 還在 —— 取反的語意會把他機的狀態翻掉★"
        from cmuh_common.roster.ui import day_tab, duty, settings
        for mod in (day_tab, duty, settings):
            src = inspect.getsource(mod)
            assert "service.toggle_" not in src, mod.__name__


# ══ 第 2 輪外審 ═════════════════════════════════════════════════════════
class TestTheInheritedRosterIsAlsoACurrentValue:
    """★沒有月度覆蓋 ≠ 沒有「目前的名單」★(RS-8 第 1 輪 P1)

    `pgy_month_roster` 是 `None` 時的語意是「沿用 config 的 PGY 名單」——
    對話框顯示的正是它。把 `None` 當成空集合來合併,他機剛加進 config 的人
    就會在這次存檔變成一份【明確排除他】的月度覆蓋:他從此不在日排班的候選
    名單裡,而畫面上完全看不出來。
    """

    def test_a_config_member_added_while_the_dialog_was_open_survives(
            self, svc):
        # 對話框開著時顯示的是 config 的 [P1](還沒有月度覆蓋)
        baseline = ["P1"]
        svc.update_config(lambda cfg: cfg["pgy_members"].append({"id": "P9"}))
        svc.set_pgy_month_roster(YM, ["P1", "P2"], baseline=baseline)
        cur = svc.storage.load_month(YM)["pgy_month_roster"]
        assert set(cur) == {"P1", "P2", "P9"}, \
            f"★他機剛加進 config 的人被這次存檔明確排除掉了★ {cur}"

    def test_a_removal_still_wins_over_the_inherited_list(self, svc):
        """使用者看見 P1 而且刪掉它 → 就是要刪(繼承來的名單不可以把它加回來)。"""
        svc.update_config(lambda cfg: cfg["pgy_members"].append({"id": "P3"}))
        svc.set_pgy_month_roster(YM, ["P3"], baseline=["P1", "P3"])
        assert svc.storage.load_month(YM)["pgy_month_roster"] == ["P3"]


class TestTheUiTakesTheIntentFromWhatWasShown:
    """★不可以把同一個缺陷往上搬一層★(RS-8 第 2 輪 P2)

    服務層改成「送想要的狀態」之後,UI 若在按下去的當下【再讀一次磁碟】取反,
    結果完全一樣:他機剛鎖起來時,使用者按下標著「鎖定」的動作照樣把它解開。
    意圖必須取自【畫面上顯示的那一份】。
    """

    def test_the_intent_arrives_as_a_parameter_and_is_never_rebound(self):
        """★判準是「意圖從哪裡來」,不是「有沒有讀磁碟」★

        `_set_lock_session` 仍然要讀月檔 —— 但那是為了「空時段不可鎖」這個
        前置條件,不是推導目標狀態。所以守衛看兩件事:目標是參數傳進來的、
        而且函式內不會把它重新綁定成某個讀取結果;另外不准出現「這個東西
        現在是不是鎖著/什麼顏色」那類讀取(那正是取反用的來源)。
        """
        import ast

        from cmuh_common.roster.ui import day_tab, duty, settings
        STATE_READS = ("is_day_locked", "load_week_colors_raw",
                       "load_week_colors")
        for mod, fname, param in ((duty, "_set_lock", "want"),
                                  (day_tab, "_set_lock_session", "want"),
                                  (settings, "_week_color_cycle", None)):
            fn = None
            for node in ast.walk(ast.parse(inspect.getsource(mod))):
                if isinstance(node, ast.FunctionDef) and node.name == fname:
                    fn = node
            assert fn is not None, f"{mod.__name__}.{fname} 不見了"
            if param:
                names = [a.arg for a in fn.args.args]
                assert param in names,                     f"★{mod.__name__}.{fname} 的目標狀態不是參數傳進來的★"
                for sub in ast.walk(fn):
                    if (isinstance(sub, ast.Assign)
                            and any(isinstance(t, ast.Name) and t.id == param
                                    for t in sub.targets)):
                        assert isinstance(sub.value, ast.Call) and (
                            getattr(sub.value.func, "id", "") == "bool"), (
                            f"★{mod.__name__}.{fname} 把目標狀態重新綁定成"
                            f"別的東西了★")
            for sub in ast.walk(fn):
                if (isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Attribute)):
                    assert sub.func.attr not in STATE_READS, (
                        f"★{mod.__name__}.{fname} 又去讀了一次它自己的狀態"
                        f"({sub.func.attr}) —— 意圖要取自畫面★")


    def test_the_duty_menu_uses_the_rendered_lock_state(self):
        src = inspect.getsource(
            __import__("cmuh_common.roster.ui.duty", fromlist=["x"]))
        i_shown = src.index("self._shown_locked[(iso, scope)] = locked")
        i_menu = src.index("shown = bool(self._shown_locked.get(")
        assert i_shown < i_menu
        assert "self._set_lock(d, scope, not shown)" in src

    def test_the_day_menu_label_and_action_come_from_one_read(self):
        from cmuh_common.roster.ui import day_tab
        src = inspect.getsource(day_tab)
        i = src.index("locked = self.service.is_day_locked(self.app.ym, d, session)")
        seg = src[i:i + 400]
        assert "w=not locked" in seg, \
            "★選單標籤與送出的目標要出自同一次判讀★"

    def test_the_week_color_uses_the_rendered_snapshot(self):
        from cmuh_common.roster.ui import settings
        src = inspect.getsource(settings)
        assert "self._wc_shown = dict(manual)" in src
        assert "WEEK_COLOR_CYCLE.get(self._wc_shown.get(wk))" in src


# ══ 第 3 輪(使用者轉來的完整 review:P1-01-C 與 P2-01)════════════════════
class TestTheClerkBatchDialogIsAlsoADelta:
    """★對話框回的是整份預填紀錄,不是意圖★(外審 P1-01-C)

    A 只改成員、B 同時改起始日 → 整包 update 回去會把起始日改回舊值,
    而起始日決定「這個梯次存在於哪些日期」:求解候選人、切片格網覆蓋範圍、
    跨月統計全部跟著錯,畫面上卻看不出來。
    """

    def _seed(self, svc):
        svc.storage.save_clerk_batches([{"id": "b1",
                                         "start_monday": "2026-08-03",
                                         "members": ["C1"]}])
        return {"id": "b1", "start_monday": "2026-08-03", "members": ["C1"]}

    def _batch(self, svc):
        return svc.storage.load_clerk_batches()[0]

    def test_an_untouched_field_does_not_revert_a_remote_change(self, svc):
        before = self._seed(svc)
        # 他機把起始日往後移了一週(我的視窗看不到)
        svc.update_clerk_batches(
            lambda bs: bs[0].update({"start_monday": "2026-08-10"}))
        # 我只改成員 —— 起始日的輸入框還是開窗當時的舊值
        svc.update_clerk_batch_fields(
            "b1", before, {"id": "b1", "start_monday": "2026-08-03",
                           "members": ["C1", "C2"]})
        got = self._batch(svc)
        assert got["start_monday"] == "2026-08-10",             f"★他機剛改的起始日被沒動過的舊值還原了★ {got}"
        assert got["members"] == ["C1", "C2"]

    def test_a_field_changed_on_both_sides_is_refused(self, svc):
        before = self._seed(svc)
        svc.update_clerk_batches(
            lambda bs: bs[0].update({"start_monday": "2026-08-10"}))
        with pytest.raises(StaleRosterDataError, match="也被另一台"):
            svc.update_clerk_batch_fields(
                "b1", before, {"id": "b1", "start_monday": "2026-08-17",
                               "members": ["C1"]})
        assert self._batch(svc)["start_monday"] == "2026-08-10",             "★被拒絕就不可以留下半套★"

    def test_a_batch_deleted_elsewhere_is_reported(self, svc):
        before = self._seed(svc)
        svc.update_clerk_batches(lambda bs: bs.clear())
        with pytest.raises(StaleRosterDataError, match="已不在清單"):
            svc.update_clerk_batch_fields("b1", before, {"members": ["C9"]})

    def test_an_unchanged_dialog_writes_nothing(self, svc):
        before = self._seed(svc)
        assert svc.update_clerk_batch_fields("b1", before, dict(before)) == {}

    def test_the_ui_delegates_instead_of_writing_the_whole_record(self):
        from cmuh_common.roster.ui import settings as mod
        src = inspect.getsource(mod.SettingsTab._batch_edit)
        assert "before = dict(cur)" in src, "★要留下開窗當時的那一份當基準★"
        assert "update_clerk_batch_fields(" in src
        assert "b.update(dlg.result)" not in src, "★又整包蓋回去了★"



class TestARenameRefreshesTheRevisionToo:
    """★內容與 revision 要一起換成同一次讀到的那一份★(外審 P2-01)

    只重讀內容而 `_cfg_rev` 停在改名之前的版本 → 接下來套用姓名/級職/固定
    星期的存檔一定被自己的改名判成過期,而 UI 會說「設定已被另一台電腦更新」。
    """

    def test_member_edit_refreshes_content_and_revision_together(self):
        from cmuh_common.roster.ui import settings as mod
        src = inspect.getsource(mod.SettingsTab._member_edit)
        i_ren = src.index("self.service.rename_member(")
        after = src[i_ren:]
        assert "self._cfg = self.service.storage.load_config()" not in after, \
            "★改名後只重讀內容、沒有換 revision★"
        assert after.count("self._refresh_cfg_from_disk()") >= 2

    def test_the_refresh_takes_both_from_one_read(self):
        from cmuh_common.roster.ui import settings as mod
        src = inspect.getsource(mod.SettingsTab._refresh_cfg_from_disk)
        assert "assert_readable" in src
        assert "self._cfg_rev = " in src
        assert "self._cfg = " in src
