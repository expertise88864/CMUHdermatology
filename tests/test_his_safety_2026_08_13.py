# -*- coding: utf-8 -*-
"""[批次AD-5] HIS safety(外審 2026-08-12 P1-07 + P2-05)。

★P1-07★ 「我的會診清單」命令後挑視窗,兩個以上合格候選時舊版直接拿
`hits[0]` —— 沒有任何理由證明那是對的一張(HIS 重複處理命令、上一輪
form 轉場延遲、Delphi 同時建兩個同 class form)。★擷取錯的一張會診單
會被寄出去★,那不是 UI 小毛病。改成 `len(hits)==1` 才採認,多個=本輪
失敗重試(fail-closed)。

★P2-05★ `force_close_active()` 以前是「單連線設計」的全域斬殺;現在
回查的 Sent 查詢可能與指令/觸發掃描在不同執行緒上重疊 —— 指令掃描逾時
卻把健康的 Sent 查詢一起砍掉,收斂白跑一輪。改成 tag 分域。
"""
import ast
import importlib
import io
import os
import sys

import pytest

NL = chr(10)
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

cq = importlib.import_module("consult_query")
ir = importlib.import_module("cmuh_common.imap_reader")

SRC = io.open(os.path.join(REPO_ROOT, "src", "consult_query.py"),
              encoding="utf-8").read()


def _fn_node(name):
    for n in ast.walk(ast.parse(SRC)):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"找不到 {name}")


def _is_len_hits_cmp(test, op_type):
    """`len(hits) <op> 1` 的比較式。"""
    return (isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Call)
            and getattr(test.left.func, "id", "") == "len"
            and getattr(test.left.args[0], "id", "") == "hits"
            and len(test.ops) == 1 and isinstance(test.ops[0], op_type)
            and getattr(test.comparators[0], "value", None) == 1)


class TestMultipleCandidatesFailClosed:
    """★兩個以上合格候選 → 本輪不擷取★,且★第一眼的單一候選不算數★
    (外審 AD-5 第 1 輪 P1):A 先出現、B 下一個 UI dispatch 才出現 ——
    單看一個時間點的快照照樣擷取到錯的一張。要連續數次唯一才採認。"""

    @staticmethod
    def _drive(seq, **kw):
        """用預先寫好的觀察序列驅動 helper(耗盡後維持最後一格)。"""
        it = list(seq)

        def _poll():
            return it.pop(0) if len(it) > 1 else it[0]

        kw.setdefault("deadline_sec", 5.0)
        kw.setdefault("what", "會診單視窗")
        kw.setdefault("sleep_sec", 0.0)
        return cq._sole_stable_window(_poll, **kw)

    def test_a_stable_singleton_is_accepted(self):
        assert self._drive([[], [7], [7], [7]]) == 7

    def test_a_late_second_window_aborts(self):
        """★核心競態★ [] → [A] → [A,B]:B 晚一拍出現,不可以拿 A。

        ★match 要釘在「無法辨別」★:逾時也拋 RuntimeError —— 不釘訊息的話,
        把 >1 檢查整個拿掉照樣(以逾時)通過,量到的是別條規則。"""
        with pytest.raises(RuntimeError, match="無法辨別"):
            self._drive([[], [7], [7, 8]])

    def test_two_at_once_abort_immediately(self):
        with pytest.raises(RuntimeError, match="無法辨別"):
            self._drive([[7, 8]])

    def test_a_changed_candidate_restarts_the_count(self):
        """A 換成 B → 重新計數(不是把 A 的信用轉給 B)。

        ★反例要讓兩條路答案不同★:`[7],[7],[8],[7]…` —— 續數的話第三格
        就把 8 收下(streak 承襲自 7);重新計數的話 8 只有一次觀察,
        之後回到 7 連三次 → answer=7。殊途同歸的序列量不到這條規則。"""
        assert self._drive([[7], [7], [8], [7], [7], [7]]) == 7

    def test_a_vanishing_candidate_resets(self):
        assert self._drive([[7], [], [7], [7], [7]]) == 7

    def test_timeout_raises(self):
        with pytest.raises(RuntimeError, match="等不到"):
            self._drive([[]], deadline_sec=0.05)

    @pytest.mark.parametrize("fn", ["_query_cycle", "_run_with_sw_hide"])
    def test_both_sites_use_the_shared_rule(self, fn):
        """★兩邊各寫一套判準就會有一邊靜默失效★ —— 都要走同一個 helper。"""
        node = _fn_node(fn)
        calls = {m.func.id for m in ast.walk(node)
                 if isinstance(m, ast.Call) and isinstance(m.func, ast.Name)}
        assert "_sole_stable_window" in calls, (
            f"★{fn} 沒有走共用的穩定確認規則★")


class TestForceCloseIsScopedByTag:
    """★[P2-05] 指令掃描逾時不可以砍掉健康的 Sent 查詢★"""

    class _Sock:
        def __init__(self):
            self.dead = False

        def shutdown(self, *a):
            self.dead = True

        def close(self):
            self.dead = True

    class _Conn:
        """可雜湊的假連線(SimpleNamespace 不可雜湊,進不了 registry)。"""

        def __init__(self, sock):
            self.sock = sock

    def _conn(self):
        return self._Conn(self._Sock())

    @pytest.fixture(autouse=True)
    def _clean_registry(self):
        with ir._active_conn_lock:
            ir._active_conns.clear()
        yield
        with ir._active_conn_lock:
            ir._active_conns.clear()

    def test_a_tagged_close_spares_other_tags(self):
        cmd, sent = self._conn(), self._conn()
        ir._set_active(cmd, "commands")
        ir._set_active(sent, "sent")
        assert ir.force_close_active(tag="commands") is True
        assert cmd.sock.dead, "自己那條要砍"
        assert not sent.sock.dead, (
            "★指令掃描逾時把健康的 Sent 查詢一起砍掉★ 收斂白跑一輪")

    def test_clear_removes_only_the_tag(self):
        cmd, sent = self._conn(), self._conn()
        ir._set_active(cmd, "commands")
        ir._set_active(sent, "sent")
        ir.force_close_active(clear=True, tag="commands")
        with ir._active_conn_lock:
            assert cmd not in ir._active_conns, "放生的那條要移出 registry"
            assert sent in ir._active_conns, "別人的連線不可以被誤清"

    def test_no_tag_still_closes_everything(self):
        """self-watchdog 的「整個排程器卡死」情境本來就該全砍。"""
        a, b = self._conn(), self._conn()
        ir._set_active(a, "trigger")
        ir._set_active(b, "sent")
        ir.force_close_active()
        assert a.sock.dead and b.sock.dead

    def test_every_worker_passes_its_own_tag(self):
        """★接線★ 各 worker 的逾時路徑要砍【自己那種】,不是全域。"""
        for fn, tag in (("_run_imap_commands_with_timeout", "commands"),
                        ("_ack_command_mail", "ack"),
                        ("_run_imap_check_with_timeout", "trigger")):
            node = _fn_node(fn)
            seg = ast.get_source_segment(SRC, node) or ""
            assert f'force_close_active(tag="{tag}")' in seg, (
                f"★{fn} 的逾時斬殺沒有帶自己的 tag★ 會誤砍健康連線")


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
