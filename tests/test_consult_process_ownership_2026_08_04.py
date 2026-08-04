# -*- coding: utf-8 -*-
"""只能終止自己開的 systemftp（2026-08-04 外審 P1-01）。

【問題】
`our_pids` 是用【全機 PID 差集】算出來的：

    our_pids = (_systemftp_pids() - before) | {our_pid}

冷啟動要等最多 120 秒。醫師在這段期間手動打開住院系統，他那個行程就會落進差集，
之後 teardown 對整個集合送 `WM_CLOSE` —— 等於替醫師關掉他自己開的 HIS。

而 `_terminate_session_process` 的 docstring 寫著「不可能誤殺使用者的醫囑系統」。
那句話只對後半段（`TerminateProcess(hproc)`，靠 handle）成立；前面那行
`close_pids(our_pids)` 吃的正是差集。★宣稱要對得上實作★

【修法】分清楚兩件事：
  * 「找視窗的候選集」可以寬鬆 —— 找錯只是找不到，不造成傷害
  * 「可以終止的集合」必須封閉 —— 弄錯就是關掉別人的程式

所以終止端改用 `_verified_owned_pids(root, candidates)`：只留 root 自己與可驗證
的後代。驗不出後代時只認 root（它是我們 spawn 出來的，身分由建立行為保證）。
"""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402


def _fake_psutil(monkeypatch, tree):
    """tree: {pid: [child_pid, ...]}（recursive 的結果直接給）。

    ★存在的 pid = keys ∪ 所有子代★ 子代不必自己也是 key，否則
    `_P(101)` 會拋例外，整個 children() 走進 except，測試就變成在測
    「查不到 root」那條路（第一版就是這樣紅的）。
    """
    alive = set(tree) | {c for kids in tree.values() for c in kids}

    class _P:
        def __init__(self, pid):
            if pid not in alive:
                raise LookupError(f"no such process {pid}")
            self.pid = pid

        def children(self, recursive=False):
            return [type(self)(c) for c in tree.get(self.pid, [])]

    mod = types.ModuleType("psutil")
    mod.Process = _P
    monkeypatch.setitem(sys.modules, "psutil", mod)


class TestOnlyOurOwnProcessesMayBeTerminated:

    def test_a_doctors_process_in_the_diff_is_not_terminated(self,
                                                             monkeypatch):
        """★這就是災情★ 醫師在冷啟動期間開的住院系統不可以被關掉。"""
        _fake_psutil(monkeypatch, {100: [101]})
        # 差集把醫師的 555 也算進來了
        keep = cq._verified_owned_pids(100, {100, 101, 555})

        assert 555 not in keep, "★醫師自己開的 systemftp 會被關掉★"
        assert keep == {100, 101}

    def test_our_own_children_are_still_terminated(self, monkeypatch):
        """★反方向:不可以變成什麼都不關★ 自己的後代照樣要收掉。

        否則每一輪都留下孤兒 systemftp，很快就撞到院方「最多兩個」的上限。
        """
        _fake_psutil(monkeypatch, {100: [101, 102]})
        keep = cq._verified_owned_pids(100, {100, 101, 102})
        assert keep == {100, 101, 102}

    def test_the_root_is_always_kept_even_if_it_already_exited(self,
                                                              monkeypatch):
        """root 已結束 → 列舉不到後代，但 root 本身仍要收（handle 還在）。"""
        _fake_psutil(monkeypatch, {})          # 連 root 都查不到
        keep = cq._verified_owned_pids(100, {100, 101, 555})

        assert keep == {100}, (
            "驗不出後代時只認 root —— 寧可留孤兒，不可誤關別人的")

    def test_psutil_missing_falls_back_to_root_only(self, monkeypatch):
        """psutil 不可用 → 只認 root（保守），不可以退回「整個差集都關」。"""
        monkeypatch.setitem(sys.modules, "psutil", None)
        keep = cq._verified_owned_pids(100, {100, 101, 555})
        assert keep == {100}

    def test_the_dropped_pids_are_logged_as_evidence(self, monkeypatch,
                                                     caplog):
        """★實機證據★ 差集裡有別人的行程時要留下痕跡，否則沒人知道發生過。"""
        import logging as _lg
        _fake_psutil(monkeypatch, {100: []})
        with caplog.at_level(_lg.WARNING):
            cq._verified_owned_pids(100, {100, 555, 556})

        msgs = " ".join(r.getMessage() for r in caplog.records)
        assert "555" in msgs and "556" in msgs, f"沒留下證據：{msgs}"

    def test_nothing_is_logged_when_everything_is_ours(self, monkeypatch,
                                                       caplog):
        """全部都是自己的 → 不要製造雜訊（否則警告會被當成背景音）。"""
        import logging as _lg
        _fake_psutil(monkeypatch, {100: [101]})
        with caplog.at_level(_lg.WARNING):
            cq._verified_owned_pids(100, {100, 101})
        assert caplog.records == []


class TestTheSwHideFallbackKeepsItsSpawnHandle:
    """[2026-08-04 外審 P1-01] 那條路原本把 Popen 物件整個丟掉。

    沒有 handle 就沒有任何【直接的】所有權事實，只能靠全機差集反推 —— 這與
    2026-07-27 事故的教訓同一條：spawn 子行程要留 handle。
    """

    def test_the_popen_object_is_retained(self):
        import ast
        import inspect
        import textwrap

        src = textwrap.dedent(inspect.getsource(cq._run_with_sw_hide))
        tree = ast.parse(src)
        # 找 subprocess.Popen(...) 這個呼叫，它的結果必須被指派給某個名字
        assigned = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            v = node.value
            if (isinstance(v, ast.Call) and isinstance(v.func, ast.Attribute)
                    and v.func.attr == "Popen"):
                assigned = True
        assert assigned, (
            "★Popen 的結果沒有被留住★ 這條路就沒有所有權可言，只能靠全機差集")

    def test_cleanup_is_scoped_to_the_spawn_root(self):
        """收尾要把 root_pid 傳進去，否則驗證等於沒接上。"""
        import ast
        import inspect
        import textwrap

        src = textwrap.dedent(inspect.getsource(cq._run_with_sw_hide))
        tree = ast.parse(src)
        ok = False
        for node in ast.walk(tree):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id == "_cleanup_pids_excluding_borrowed"):
                ok = any(kw.arg == "root_pid" for kw in node.keywords)
        assert ok, "收尾沒有指定 root_pid → 所有權驗證沒有生效"


class TestTheBorrowedFlagIsNotEnoughOnItsOwn:
    """`before` 快照只認得「更早以前」，認不得「剛剛才開」。"""

    def test_a_process_opened_during_cold_start_is_still_excluded(
            self, monkeypatch):
        """醫師在等登入視窗那 120 秒內【新開】的 → 不在 before 裡，
        borrowed=False，舊邏輯會把它關掉。有 root_pid 才擋得住。"""
        _fake_psutil(monkeypatch, {100: [101]})
        before = {9}                       # 更早以前就存在的
        our = {100, 101, 777}              # 777 = 醫師在冷啟動期間新開的

        keep = cq._cleanup_pids_excluding_borrowed(
            our, before, borrowed=False, root_pid=100)

        assert 777 not in keep, (
            "★borrowed 只擋得住『啟動前就存在』，擋不住『剛剛才開』★")
        assert keep == {100, 101}

    def test_without_a_root_the_old_behaviour_is_kept(self):
        """沒有 root_pid（呼叫端還沒留住 handle）→ 維持舊行為，不擅自改變。"""
        keep = cq._cleanup_pids_excluding_borrowed(
            {100, 777}, {9}, borrowed=False)
        assert keep == {100, 777}

    def test_borrowed_still_excludes_pre_existing(self, monkeypatch):
        """既有行為不可以壞掉：borrowed 仍要排除啟動前就存在的。"""
        _fake_psutil(monkeypatch, {100: []})
        keep = cq._cleanup_pids_excluding_borrowed(
            {100, 9}, {9}, borrowed=True, root_pid=100)
        assert 9 not in keep


def test_the_root_is_added_even_when_it_is_not_in_the_candidate_set(
        monkeypatch):
    """★`keep.add(root_pid)` 要真的有作用★（突變驗證抓到）

    先前每一支測試的候選集都【已經含有】root，所以把那一行刪掉也不會有人發現。
    候選集不含 root 的情況是真的：`our_pids` 來自視窗 PID 與差集，若 root 只是
    個啟動器、視窗屬於後代，root 可能根本不在裡面 —— 但 handle 還握在我們手上，
    它必須被收掉。
    """
    _fake_psutil(monkeypatch, {100: [101]})
    keep = cq._verified_owned_pids(100, {101, 555})

    assert 100 in keep, "★root 沒被收 → handle 還在卻留下一個活著的行程★"
    assert keep == {100, 101}


def test_terminate_actually_uses_the_ownership_check():
    """★接線本身也要被測到★（突變驗證抓到）

    上面每一支都直接呼叫 `_verified_owned_pids`，所以就算
    `_terminate_session_process` 改回 `close_pids(sess.our_pids)`，它們照樣全綠
    —— 而那正是 bug 還在的樣子。終止那段沒辦法在測試裡跑（會真的關行程、動
    Win32 handle），所以用 AST 檢查 `close_pids` 的引數確實經過驗證。
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(cq._terminate_session_process)))
    guarded = False
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "close_pids"):
            arg = node.args[0] if node.args else None
            guarded = (isinstance(arg, ast.Call)
                       and isinstance(arg.func, ast.Name)
                       and arg.func.id == "_verified_owned_pids")
    assert guarded, (
        "close_pids 拿到的不是驗證過的集合 → 仍可能關掉醫師的住院系統")
