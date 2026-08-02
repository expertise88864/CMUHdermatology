# -*- coding: utf-8 -*-
"""批次 H（外部 review P1-05）：黑幕的三個 fail-open。

黑幕的用途是「遮住螢幕上的病歷」。這三個洞的共同形狀是
**在無法證明蓋住的情況下宣告蓋住了**，而其中兩個還會順手放行 F1-F12 ——
熱鍵於是在使用者看不見的畫面上對 HIS 動作。

  A. `show()` 在全覆蓋驗證【之前】就 `return True`（只要 `active` 為真）
  B. `coverage_state()` 用【建立當下】的快照回答「現在」
  C. `_force_close_survivors()` 取不到 user32 時把「查不出來」報成「都收乾淨了」
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from cmuh_common import screen_blackout as sb


def _code_of(func) -> str:
    """取函式原始碼，★把註解與 docstring 都剝掉★

    第一版直接對 `inspect.getsource` 的字串做 `not in` 斷言，結果命中的是我
    自己寫在註解裡的 `fully_verified` / `monitor_rects()` —— 掃原始碼的測試
    被自己的說明文字餵飽，這輪已經重複踩到第 7 次。
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    node = tree.body[0]
    body = node.body[1:] if (
        node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)) else node.body
    return "\n".join(ast.unparse(n) for n in body)


class _Idle:
    def __init__(self, value=0.0):
        self.value = value

    def __call__(self):
        return self.value


class _Busy:
    def __init__(self, value=False):
        self.value = value

    def __call__(self):
        return self.value


# ── A：已經有黑幕時不可以無條件宣告成功 ──────────────────────────────────
class TestShowDoesNotTrustAnExistingBlackout:

    def test_a_partial_blackout_is_rebuilt_instead_of_reported_as_success(
            self, tk_root):
        """★這是 finding A★

        `active` 是 `any(...)`：殘留一片、螢幕排列改變、視窗被移走 —— 都算 True。
        原本這時 `show()` 直接回 True，等於「醫師以為蓋住了，其實一半的病歷
        還亮著」，而且完全不重新檢查。
        """
        screens = [(0, 0, 400, 300)]
        bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(),
                               busy_fn=_Busy(), rects_fn=lambda: list(screens))
        try:
            assert bo.show() is True
            assert bo.coverage_state() == sb.COVERAGE_FULLY_VISIBLE

            # 多了一台螢幕 → 現有黑幕不再蓋滿
            screens.append((400, 0, 400, 300))
            assert bo.coverage_state() == sb.COVERAGE_PARTIAL

            # ★再按一次「黑幕」必須重建、而且真的蓋滿兩台★
            assert bo.show() is True
            assert bo.coverage_state() == sb.COVERAGE_FULLY_VISIBLE
            assert bo.panel_count == 2
        finally:
            bo.hide()

    def test_a_fully_covering_blackout_is_not_needlessly_rebuilt(self, tk_root):
        """★空集合不算通過★ 已經蓋滿就不要拆掉重建（重建會閃、也有風險）。"""
        bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(),
                               busy_fn=_Busy(),
                               rects_fn=lambda: [(0, 0, 400, 300)])
        try:
            assert bo.show() is True
            first = list(bo._hwnds)
            assert bo.show() is True
            assert list(bo._hwnds) == first, "已經蓋滿卻被拆掉重建了"
        finally:
            bo.hide()

    def test_it_refuses_to_rebuild_over_a_blackout_that_would_not_close(
            self, tk_root, monkeypatch):
        """★[2026-08-02 外審 P1] 我修 A 的時候自己造出來的洞★

        `_create()` 會把 `_hwnds` 整條換成新的一批。若拆除失敗、舊的那片仍然
        看得見，重建就讓它從此沒有人追蹤 —— 之後任何一次成功的 teardown 都會
        放行 F1-F12，而畫面上還蓋著那片舊黑幕。
        """
        screens = [(0, 0, 400, 300)]
        bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(),
                               busy_fn=_Busy(), rects_fn=lambda: list(screens))
        try:
            assert bo.show() is True
            old = list(bo._hwnds)
            # 拆不掉：強制關閉一律回報「還卡著」
            monkeypatch.setattr(sb.ScreenBlackout, "_force_close_survivors",
                                staticmethod(lambda hwnds: list(hwnds)))
            screens.append((400, 0, 400, 300))     # 讓覆蓋變成 PARTIAL

            assert bo.show() is False, "★拆不掉卻照樣重建★"
            assert list(bo._hwnds) == old, (
                "★舊的 HWND 被新的蓋掉了 → 那片黑幕從此沒人追蹤★")
        finally:
            monkeypatch.undo()
            bo.hide()

    def test_survivors_block_a_rebuild_even_when_active_is_false(self,
                                                                 tk_root):
        """★判準必須是 `_hwnds` 而不是 `active`★

        Tk 視窗已經 destroy、只剩 Win32 survivor 時 `active` 是 False —— 會整個
        跳過「已有黑幕」那段而直接走到 `_create()`，同樣蓋掉 survivor。
        """
        bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(),
                               busy_fn=_Busy(),
                               rects_fn=lambda: [(0, 0, 400, 300)])
        bo._hwnds = (0xDEAD,)          # 沒有 Tk 視窗，只剩追蹤中的 HWND
        assert bo.active is False
        assert bo.show() is False
        assert bo._hwnds == (0xDEAD,), "survivor 被蓋掉了"

    def test_the_early_return_is_gated_on_coverage_not_on_active(self):
        """結構釘子：`show()` 裡那條捷徑必須問過 `coverage_state`。"""
        src = textwrap.dedent(inspect.getsource(sb.ScreenBlackout.show))
        tree = ast.parse(src)
        guard = next((n for n in ast.walk(tree) if isinstance(n, ast.If)
                      and isinstance(n.test, ast.Attribute)
                      and n.test.attr == "active"), None)
        assert guard is not None, "找不到 `if self.active:` 那條捷徑（測試失效了）"
        calls = {n.func.attr for n in ast.walk(guard)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert "coverage_state" in calls, (
            "★`active` 是 any(...)，不可以拿它當「蓋滿了」★")


# ── B：coverage_state 要看現在，不是看建立當下 ───────────────────────────
class TestCoverageStateReflectsNow:

    def test_a_panel_that_was_moved_away_is_no_longer_fully_visible(self,
                                                                    tk_root):
        """★這是 finding B★

        `p.fully_verified` 是建立那一刻量的。視窗後來被移動/resize 之後它仍是
        True —— 於是畫面已經露出來了，狀態卻還說「完全蓋住」。
        """
        bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(),
                               busy_fn=_Busy(),
                               rects_fn=lambda: [(0, 0, 400, 300)])
        try:
            assert bo.show() is True
            assert bo.coverage_state() == sb.COVERAGE_FULLY_VISIBLE

            # 把那一片挪走（模擬視窗管理員/使用者拖動）
            hwnd = bo._hwnds[0]
            u = sb._u32()
            HWND_TOPMOST = -1
            SWP_NOACTIVATE = 0x0010
            u.SetWindowPos(hwnd, HWND_TOPMOST, 50, 50, 100, 100,
                           SWP_NOACTIVATE)
            tk_root.update()

            assert bo.coverage_state() == sb.COVERAGE_PARTIAL, (
                "★視窗被挪走了，狀態卻還說完全蓋住★")
        finally:
            bo.hide()

    def test_it_does_not_read_the_creation_time_snapshot(self):
        """結構釘子：不可以再用 `fully_verified` 當「現在」的判準。"""
        code = _code_of(sb.ScreenBlackout.coverage_state)
        assert "fully_verified" not in code, (
            "★那是建立當下的快照，回答不了「現在蓋成什麼樣」★")
        assert "_window_rect" in code, "沒有當場回讀每一片的位置"

    def test_an_unreadable_rect_is_unknown_not_fully_visible(self, tk_root,
                                                             monkeypatch):
        """★查不到 ≠ 沒事★ 量不到位置就不可以宣稱蓋滿。"""
        bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(),
                               busy_fn=_Busy(),
                               rects_fn=lambda: [(0, 0, 400, 300)])
        try:
            assert bo.show() is True
            monkeypatch.setattr(sb.ScreenBlackout, "_window_rect",
                                staticmethod(lambda hwnd: None))
            assert bo.coverage_state() == sb.COVERAGE_UNKNOWN
        finally:
            monkeypatch.undo()
            bo.hide()

    def test_it_uses_the_injected_rects_source(self):
        """★用 `self._rects_fn`，不是模組級 `monitor_rects()`★

        `_create` 就是用它決定要蓋哪些矩形。第一版我寫成模組級函式，
        既有測試（注入 500x400 假螢幕）立刻轉紅 —— 比對的是另一組螢幕。
        """
        code = _code_of(sb.ScreenBlackout.coverage_state)
        assert "self._rects_fn" in code
        assert "monitor_rects()" not in code


# ── C：查不到 user32 不等於黑幕收乾淨了 ─────────────────────────────────
class TestSurvivorsWhenWin32IsUnavailable:

    def test_an_unavailable_user32_reports_everything_as_stuck(self,
                                                               monkeypatch):
        """★這是三個裡最危險的★

        原本回 `[]`（＝都收乾淨了）。呼叫端據此清空 `_hwnds`、開放 F1-F12、
        甚至發喚醒 token —— 而螢幕上可能仍蓋著全黑的畫面。
        """
        monkeypatch.setattr(sb, "_u32",
                            lambda: (_ for _ in ()).throw(OSError("掛了")))
        hwnds = [111, 222, 333]
        assert sb.ScreenBlackout._force_close_survivors(hwnds) == hwnds

    def test_no_hwnds_is_still_nothing_to_do(self, monkeypatch):
        """★空集合不算通過★ 本來就沒有視窗時不該憑空生出 survivor。"""
        monkeypatch.setattr(sb, "_u32",
                            lambda: (_ for _ in ()).throw(OSError("掛了")))
        assert sb.ScreenBlackout._force_close_survivors([]) == []

    def test_the_hwnds_are_kept_so_the_state_is_not_falsely_clean(
            self, tk_root, monkeypatch):
        """teardown 時查不到 user32 → 不可以把狀態標記成「已收乾淨」。

        ★我原本在這裡斷言「閘門必須繼續擋」，那是【錯的】★
        `active_from_any_thread()` 在 Win32 查詢失敗時是**刻意回 False**
        （放行熱鍵）——它的 docstring 明寫理由：一次 Win32 失敗若造成
        F1-F12 永久鎖死，比「黑幕期間漏擋一次」嚴重得多。那是既有的、
        有意識的取捨，不是這批要改的東西。

        所以這個 fix 真正保住的是別的東西：`_hwnds` 不被清空。
        —— 狀態不會被誤標成乾淨，Win32 一恢復，閘門就能正確認出殘留的黑幕。
        """
        bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(),
                               busy_fn=_Busy(),
                               rects_fn=lambda: [(0, 0, 400, 300)])
        assert bo.show() is True
        alive_before = list(bo._hwnds)
        monkeypatch.setattr(sb, "_u32",
                            lambda: (_ for _ in ()).throw(OSError("掛了")))
        try:
            bo.hide()
            assert list(bo._hwnds) == alive_before, (
                "★HWND 被清空了 → 狀態被誤標成已收乾淨★")
        finally:
            monkeypatch.undo()
        # ★Win32 恢復之後，狀態要收斂到「事實」★
        #   這個情境裡 Tk 的 destroy 其實成功了（壞掉的只是我們的【驗證】），
        #   所以視窗真的不在 —— 正確答案就是 HIDDEN。
        #   ★不要斷言 True★：我第一版寫了「閘門要認出殘留視窗」，但這個測試
        #   根本沒有製造出殘留視窗，那是在斷言一個不存在的情境。
        assert bo.coverage_state() == sb.COVERAGE_HIDDEN
        bo.hide()

    def test_a_stuck_teardown_does_not_issue_a_wake_token(self, tk_root,
                                                          monkeypatch):
        """黑幕還在 → 沒有「剛剛消失」的競態，不該發 token
        （閘門靠 `active_from_any_thread()` 本來就還擋著）。"""
        bo = sb.ScreenBlackout(tk_root, idle_seconds_fn=_Idle(),
                               busy_fn=_Busy(),
                               rects_fn=lambda: [(0, 0, 400, 300)])
        assert bo.show() is True
        monkeypatch.setattr(sb, "_u32",
                            lambda: (_ for _ in ()).throw(OSError("掛了")))
        try:
            bo.hide()
            assert bo.consume_wake_gate() is False
        finally:
            monkeypatch.undo()
            bo.hide()


@pytest.mark.parametrize("name", ["show", "coverage_state"])
def test_the_two_answers_are_not_conflated(name):
    """`active`（保守的熱鍵閘門）與「蓋成什麼樣」是兩個問題。

    `active` 必須維持 `any(...)`（只要還有一片就擋熱鍵），而 `show()` 的成功
    判準與 `coverage_state()` 都不可以用它。
    """
    src = textwrap.dedent(inspect.getsource(getattr(sb.ScreenBlackout, name)))
    tree = ast.parse(src)
    returns_active = [n for n in ast.walk(tree) if isinstance(n, ast.Return)
                      and isinstance(n.value, ast.Attribute)
                      and n.value.attr == "active"]
    assert returns_active == [], f"{name} 直接把 `active` 當答案"
