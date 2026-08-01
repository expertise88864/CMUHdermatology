# -*- coding: utf-8 -*-
"""[2026-08-01 外部 code review 批次D / P1-05] F9/F10 送鍵序列改成可回讀。

★問題★
`send_key_to_window()` 忽略每一次 `PostMessageW` 的回傳值，而且回 `None`。
F9/F10 選片語就是靠這個序列把 grid 的選取列從第 0 列移到第 N 列：

    _send_key_to_window(grid, VK_DOWN, count=row_idx)
    logging.info("grid 已 VK_DOWN %d 次 → row %d", row_idx, row_idx)   # ← 無條件
    _click_button_by_text(popup, "帶回")                                # ← 照樣按

少送一次就選到【上一列】的片語，然後照樣「帶回」並在 Round 4 送出同意書 ——
那是寫錯病歷等級的後果，而且 log 還一路說「→ row N」。

★這一刀不做的事（誠實邊界）★
`TStringAlignGrid` 是 Delphi 的 owner-draw 格線，沒有標準訊息可以讀回「現在選第幾列」。
所以我們無法驗證「真的移到第 N 列」，只能驗證「N 次方向鍵都送出去了」。
措辭因此也跟著改：log 只講送出的事實，不宣稱 grid 在第幾列。
"""
import ast
import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import his_window as hw  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class _Poster:
    """假的 user32：可以指定第 N 次 PostMessageW 開始失敗。"""

    def __init__(self, fail_at=None, fail_kind="down"):
        self.calls = []
        self.fail_at = fail_at        # 第幾次（1-based）失敗
        self.fail_kind = fail_kind    # "down" / "up"

    def PostMessageW(self, hwnd, msg, wparam, lparam):
        WM_KEYDOWN = 0x0100
        kind = "down" if msg == WM_KEYDOWN else "up"
        self.calls.append(kind)
        n = sum(1 for c in self.calls if c == kind)
        if self.fail_at is not None and kind == self.fail_kind \
                and n >= self.fail_at:
            return 0
        return 1


def test_a_complete_sequence_reports_complete(monkeypatch):
    p = _Poster()
    monkeypatch.setattr(hw, "_user32", lambda: p)
    got = hw.send_key_to_window(1234, 0x28, count=3, interval=0)
    assert got.complete is True
    assert (got.requested, got.keydown_posted, got.keyup_posted) == (3, 3, 3)


def test_a_failed_keydown_stops_the_sequence(monkeypatch):
    """★一送失敗就停下來★ 繼續送只會讓選取列停在誰也不知道的位置。"""
    p = _Poster(fail_at=2, fail_kind="down")
    monkeypatch.setattr(hw, "_user32", lambda: p)
    got = hw.send_key_to_window(1234, 0x28, count=5, interval=0)
    assert got.complete is False
    assert got.keydown_posted == 1, "第 2 次失敗 → 只成功送出 1 次"
    assert p.calls.count("down") == 2, "失敗那一次之後不可以再送"


def test_a_failed_keyup_also_fails_the_sequence(monkeypatch):
    """★成對才算數★ KEYDOWN 送到但 KEYUP 沒送到，選取列一樣可能沒動。"""
    p = _Poster(fail_at=1, fail_kind="up")
    monkeypatch.setattr(hw, "_user32", lambda: p)
    got = hw.send_key_to_window(1234, 0x28, count=3, interval=0)
    assert got.complete is False
    assert got.keydown_posted == 1 and got.keyup_posted == 0


def test_all_keydowns_but_a_missing_last_keyup_is_not_complete(monkeypatch):
    """★這才是真正釘住「成對」的那一支★

    突變驗證抓到：把 `complete` 改成只看 keydown 時，上面那支照樣綠 ——
    因為 KEYUP 在【第一次】就失敗，keydown 也才送出 1 次（≠ requested）。
    要分辨兩種寫法，必須造出「keydown 全部送到、只有最後一個 keyup 沒送到」：
    那時只看 keydown 會判成功，而選取列其實可能停在上一列。
    """
    p = _Poster(fail_at=3, fail_kind="up")     # 第 3 次 KEYUP 失敗
    monkeypatch.setattr(hw, "_user32", lambda: p)
    got = hw.send_key_to_window(1234, 0x28, count=3, interval=0)
    assert got.keydown_posted == 3 == got.requested, "前提：keydown 全部送到"
    assert got.keyup_posted == 2, "前提：最後一個 keyup 沒送到"
    assert got.complete is False, "★只看 keydown 會誤判成功★"


def test_zero_count_is_trivially_complete(monkeypatch):
    """row 0 不需要送鍵 —— 那不是失敗。"""
    p = _Poster()
    monkeypatch.setattr(hw, "_user32", lambda: p)
    got = hw.send_key_to_window(1234, 0x28, count=0, interval=0)
    assert got.complete is True and p.calls == []


def test_the_result_describes_itself_without_claiming_a_row():
    """★措辭鐵律★ 這個結果物件只知道「送出了幾次」，不知道 grid 在第幾列。"""
    got = hw.KeySequenceResult(requested=3, keydown_posted=1, keyup_posted=1)
    text = got.describe()
    assert "3" in text and "1" in text
    assert "row" not in text.lower() and "列" not in text


# ─── 呼叫端：送不完就不可以按「帶回」 ────────────────────────────────────
def _main_func(name):
    tree = ast.parse(io.open(os.path.join(REPO_ROOT, "src", "main.py"),
                             encoding="utf-8").read())
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == name)


def _code_of(name) -> str:
    """剝掉 docstring 的原始碼（docstring 會引用不該出現的字串而自我命中）。"""
    fn = _main_func(name)
    stripped = ast.parse(ast.unparse(fn)).body[0]
    body = getattr(stripped, "body", [])
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        stripped.body = body[1:] or [ast.Pass()]
    return ast.unparse(stripped)


def _phrase_func_name() -> str:
    """找出送 VK_DOWN 並按「帶回」的那一支（名字含中文，別寫死）。"""
    tree = ast.parse(io.open(os.path.join(REPO_ROOT, "src", "main.py"),
                             encoding="utf-8").read())
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        src = ast.unparse(node)
        if "_send_key_to_window(" in src and "帶回" in src:
            return node.name
    raise AssertionError("找不到送 VK_DOWN 又按「帶回」的函式")


def test_an_incomplete_sequence_never_clicks_the_confirm_button():
    """★核心★ 送不完就不可以按「帶回」—— 現在 grid 停在哪一列是未知的。

    用 AST 檢查：`complete` 的失敗分支裡必須有 `return`，而且那個 return
    要在 `帶回` 出現【之前】。
    """
    name = _phrase_func_name()
    fn = _main_func(name)
    src = _code_of(name)
    i_guard = src.index("seq.complete")
    i_click = src.index("帶回")
    assert i_guard < i_click, "先檢查送鍵結果，才可以按「帶回」"

    # 失敗分支要真的 return False
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        test_src = ast.unparse(node.test)
        if "seq.complete" not in test_src:
            continue
        returns = [n for n in ast.walk(node) if isinstance(n, ast.Return)]
        assert returns, "送鍵不完整時必須直接 return，不可以往下走"
        return
    raise AssertionError("找不到 seq.complete 的守衛分支")


def test_an_incomplete_sequence_is_audited_as_send_failed():
    """要留下紀錄：這一次沒有完成，而且是送鍵階段失敗的。"""
    src = _code_of(_phrase_func_name())
    i_guard = src.index("seq.complete")
    tail = src[i_guard:]
    assert "_record_his_action" in tail
    assert "send_failed" in tail
    assert "_LEDGER_FAILED" in tail


def test_the_log_does_not_claim_the_grid_moved():
    """★措辭鐵律★ 選取列讀不回來，所以不可以宣稱「→ row N」。

    原本無條件 log「grid 已 VK_DOWN N 次 → row N」——那個箭頭是在陳述
    一件我們【驗證不了】的事，而它正是後續「帶回錯片語」時最誤導人的線索。
    """
    src = _code_of(_phrase_func_name())
    assert "→ row" not in src, "不可以宣稱 grid 移到了第幾列"
    assert "無法回讀" in src, "要講明選取列讀不回來"


def test_send_key_returns_a_result_not_none():
    """釘住介面本身：回 None 的話呼叫端只能假設成功。"""
    import inspect
    sig = inspect.signature(hw.send_key_to_window)
    assert sig.return_annotation is not None
    assert "KeySequenceResult" in str(sig.return_annotation)


def test_post_message_is_declared_before_being_trusted():
    """★開始信任回傳值之前要先宣告簽名★（批次C 學到的：不宣告會讀錯）。"""
    import ast as _ast
    import inspect
    import textwrap
    src = _ast.unparse(_ast.parse(textwrap.dedent(
        inspect.getsource(hw._user32))))
    assert "PostMessageW.argtypes" in src
    assert "PostMessageW.restype" in src
