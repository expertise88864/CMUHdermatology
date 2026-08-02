# -*- coding: utf-8 -*-
"""批次 J（外部 review P1-03 / P1-04）。

P1-03 休診表沒有語意驗證 → 壞頁覆蓋好資料 →★停診被顯示成正常門診★
P1-04 任一來源退回 stale 仍宣告 is_live_final=True →★用 15 分鐘前的掛號數寄止掛信★
"""
from __future__ import annotations

import ast
import inspect
import textwrap

import pytest

from cmuh_common import reg52_contract as rc


def _code_of(func) -> str:
    """取原始碼並剝掉 docstring —— 掃原始碼的斷言不可以被說明文字餵飽。"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    node = tree.body[0]
    body = node.body[1:] if (
        node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)) else node.body
    return "\n".join(ast.unparse(n) for n in body)


_FILLER = "<p>" + "x" * 700 + "</p>"


def _page(inner: str) -> str:
    return f"<html><body>{inner}{_FILLER}</body></html>"


# ── P1-03：休診表的語意契約 ─────────────────────────────────────────────
class TestDayoffContract:

    def test_a_page_with_a_dayoff_table_is_valid(self):
        html = _page('<table id="dayoff"><tr><td>115/08/03</td></tr></table>')
        assert rc.classify_dayoff_html(html).ok is True

    def test_an_east_style_three_column_table_is_valid(self):
        """東區 fh1 的休診表是 width=300 三欄小表（解析器的退路認得它）。"""
        html = _page('<table width="300">'
                     "<tr><th>日期</th><th>診別</th><th>說明</th></tr>"
                     "<tr><td>115/08/03</td><td>上午</td><td>休診</td></tr>"
                     "</table>")
        assert rc.classify_dayoff_html(html).ok is True

    def test_a_doctor_with_no_dayoffs_is_still_valid(self):
        """★這一條最重要★

        多數醫師多數時候【就是沒有休診】。把「沒有休診」判成無效的話，
        休診來源會一路退避到停更 —— 後果與壞頁覆蓋一模一樣
        （停診被顯示成正常門診），只是換個方向。
        """
        html = _page('<table class="schedule"><tr><td>x</td></tr></table>')
        assert rc.classify_dayoff_html(html).ok is True

    @pytest.mark.parametrize("body,why", [
        ("<html><body>系統維護中</body></html>", "維護頁（太短）"),
        (_page("<h1>請先登入</h1><form><input name=pw></form>"), "登入頁"),
        (_page("<div>改版空殼</div>"), "未知版面"),
    ])
    def test_a_page_that_is_not_reg52_is_semantically_invalid(self, body, why):
        got = rc.classify_dayoff_html(body)
        assert got.ok is False, why
        assert got.usable_html == "", "無效的內容不可以交出去"

    def test_a_suspiciously_short_page_is_rejected_even_with_the_right_tag(
            self):
        """★突變驗證抓到：長度門檻本來沒有被獨立驗到★

        原本的「維護頁」案例沒有 dayoff 表也沒有 reg52 版面，所以拿掉長度檢查
        之後照樣被結構檢查擋下 —— 那個門檻等於沒測到。
        真正只有它擋得住的是「短、但帶著正確標籤」的頁面。
        """
        tiny = '<html><body><table id="dayoff"></table></body></html>'
        assert len(tiny) < rc.MIN_PAGE_CHARS
        got = rc.classify_dayoff_html(tiny)
        assert got.ok is False
        assert got.reason == "page_too_short"

    def test_an_invalid_outcome_never_carries_the_html(self):
        """語意無效的結果不可以把頁面內容帶出去（它會流向 log／快取判斷）。"""
        got = rc.classify_dayoff_html(_page("<div>病人 24994923</div>"))
        assert got.ok is False
        assert got.html == ""
        assert got.usable_html == ""

    def test_describe_would_not_leak_even_if_html_were_set(self):
        """★用偽造的不一致物件釘住★

        正常路徑上無效結果的 `html` 一定是空的，所以「describe 印出 html」
        這個突變在真實輸入下觀察不到。這裡直接造一個不該存在的物件，
        確保 `describe()` 本身就不碰內容。
        """
        forged = rc.FetchOutcome(rc.SEMANTIC_INVALID,
                                 html="<div>病人 24994923 王小明</div>",
                                 reason="missing_reg52_layout", length=99)
        said = forged.describe()
        assert "24994923" not in said and "王小明" not in said

    def test_the_reason_never_carries_page_text(self):
        """維護頁可能夾帶任何東西，而 log 是會被整包交給開發者的。"""
        html = _page("<div>病人 24994923 王小明 請洽櫃檯</div>")
        said = rc.classify_dayoff_html(html).describe()
        assert "24994923" not in said and "王小明" not in said

    def test_the_layout_check_is_shared_with_the_parser(self):
        """判準要與解析器認得的東西一致，不可以另立一套。"""
        code = _code_of(rc.classify_dayoff_html)
        assert "_has_reg52_skeleton" in code
        assert "_has_east_style_dayoff_table" in code
        # `ast.unparse` 會把雙引號正規化成單引號 —— 斷言不可以綁死引號樣式。
        assert "table#dayoff" in code


class TestDayoffIsClassifiedBeforeItIsTrusted:

    def test_both_fetch_paths_classify_before_caching(self):
        """★機械化地掃★ 每一個寫 dayoff 快取的地方都要先分類過。

        以後新增第三條抓取路徑會在這裡轉紅。
        """
        import main
        src = inspect.getsource(main.check_appointment_count)
        tree = ast.parse(textwrap.dedent(src))

        class _Finder(ast.NodeVisitor):
            def __init__(self):
                self.stack = []
                self.writers = []

            def visit_FunctionDef(self, node):
                self.stack.append(node)
                self.generic_visit(node)
                self.stack.pop()

            def visit_Call(self, node):
                if (isinstance(node.func, ast.Name)
                        and node.func.id == "_cache_set"
                        and node.args
                        and isinstance(node.args[0], ast.Name)
                        and node.args[0].id == "dayoff_cache_key"):
                    self.writers.append(self.stack[-1] if self.stack else None)
                self.generic_visit(node)

        finder = _Finder()
        finder.visit(tree)
        assert finder.writers, "找不到任何 dayoff 快取寫入（測試失效了）"
        for owner in finder.writers:
            scope = owner if owner is not None else tree
            assert any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                       and n.func.id == "_classify_dayoff_html"
                       for n in ast.walk(scope)), (
                "有一條 dayoff 抓取路徑沒有先驗語意就寫快取")

    def test_an_invalid_dayoff_page_does_not_clear_the_backoff(self):
        """語意失敗要記退避，不可以像成功一樣清掉它。"""
        import main
        src = inspect.getsource(main.check_appointment_count)
        assert src.count("_classify_dayoff_html") >= 2
        # 每一處分類失敗的分支都要呼叫 _source_backoff_fail
        assert src.count("休診表回應不是 reg52 頁") == 2


# ── P1-04：stale 不得取得寄信資格 ───────────────────────────────────────
class TestStaleDataDoesNotEarnAlertEligibility:

    def test_the_helper_records_the_degraded_source(self):
        import main
        seen: set = set()
        calls = []

        def _fake_cache_get(key, ttl, evict_expired=True):
            calls.append((key, ttl))
            return "<html>舊的</html>"

        real = main._cache_get
        main._cache_get = _fake_cache_get
        try:
            got = main._reg52_stale_fallback(("dayoff_html", "D1"), "dayoff",
                                             seen)
        finally:
            main._cache_get = real

        assert got == "<html>舊的</html>"
        assert seen == {"dayoff"}
        assert calls and calls[0][1] == main.REG52_STALE_CACHE_SECONDS

    def test_nothing_in_the_stale_cache_is_not_a_degradation(self):
        """★空集合不算通過★ 沒有舊資料可退時，不該被記成降級。"""
        import main
        seen: set = set()
        real = main._cache_get
        main._cache_get = lambda *a, **k: None
        try:
            got = main._reg52_stale_fallback(("main_html", "D1"), "main", seen)
        finally:
            main._cache_get = real
        assert got is None
        assert seen == set()

    def test_a_prefetched_candidate_that_is_never_used_is_not_a_degradation(
            self):
        """★[2026-08-02 外審第 2 輪 P1] 我上一版把「先備好」當成「用了」★

        外院來源的流程是「先把 stale 備援拿在手上，再去即時抓」。我原本在
        【抓之前】就標記降級 —— 即使整輪都是新鮮資料，這位醫師仍會以
        is_live_final=False 送出，掃描把他移出資格名單，★該寄的止掛提醒不會寄★。
        對外院醫師隔 10 分鐘手動刷新一次就會踩到。
        """
        import main
        seen: set = set()
        real = main._cache_get
        main._cache_get = lambda *a, **k: "<html>舊的備援</html>"
        try:
            got = main._reg52_stale_candidate(("east_html", "D1"))
        finally:
            main._cache_get = real
        assert got == "<html>舊的備援</html>", "備援還是要拿得到"
        assert seen == set(), "只是備援候選，不該記成降級"

    def test_the_prefetch_site_uses_the_candidate_helper(self):
        """結構釘子：排進 external_jobs 的那一處必須用 candidate（不標記）。"""
        import main
        code = _code_of(main.check_appointment_count)
        tree = ast.parse(code)
        appends = [n for n in ast.walk(tree)
                   if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "append"
                   and isinstance(n.func.value, ast.Name)
                   and n.func.value.id == "external_jobs"]
        assert appends, "找不到 external_jobs.append（測試失效了）"
        assert "_reg52_stale_candidate" in code, (
            "備援預取沒有改用 candidate helper")

    def test_the_job_runner_marks_only_when_it_actually_falls_back(self):
        """★標記要發生在「確實採用 stale」的那一刻★"""
        import main
        code = _code_of(main.check_appointment_count)
        tree = ast.parse(code)
        runner = next((n for n in ast.walk(tree)
                       if isinstance(n, ast.FunctionDef)
                       and n.name == "_run_external_job"), None)
        assert runner is not None, "找不到 _run_external_job（測試失效了）"
        # 標記必須在 `elif stale_html:` 那一支裡，不可以在函式一開頭
        marks = [n for n in ast.walk(runner)
                 if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "add"
                 and isinstance(n.func.value, ast.Name)
                 and n.func.value.id == "degraded_sources"]
        assert len(marks) == 1, "採用 stale 時應該剛好標記一次"
        in_fallback = [n for n in ast.walk(runner) if isinstance(n, ast.If)
                       and any(m in ast.walk(n) for m in marks)
                       and any(isinstance(x, ast.Name) and x.id == "stale_html"
                               for x in ast.walk(n))]
        assert in_fallback, "標記不在「改吃備援」那一支裡"

    def test_every_stale_fallback_goes_through_the_helper(self):
        """★機械化地掃★ 不可以有人繞過 helper 直接讀 stale 快取。

        繞過就等於「這一輪用了 15 分鐘前的資料」沒有被記錄，
        而 `is_live_final` 就會又變回那個假的 True。
        """
        import main
        src = inspect.getsource(main.check_appointment_count)
        tree = ast.parse(textwrap.dedent(src))
        raw = [n.lineno for n in ast.walk(tree)
               if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
               and n.func.id == "_cache_get"
               and any(isinstance(a, ast.Name)
                       and a.id == "REG52_STALE_CACHE_SECONDS"
                       for a in n.args)]
        assert raw == [], (
            f"第 {raw} 行直接讀 stale 快取，沒有記錄來源降級")

    def test_the_emit_is_gated_on_freshness_not_hardcoded_true(self):
        """★宣稱要對得上實際知道的事★

        原本無條件 `is_live_final=True`，而那是止掛提醒可以寄信的資格。
        """
        import main
        code = _code_of(main.check_appointment_count)
        assert "is_live_final=live_final" in code, (
            "還是把 is_live_final 寫死成 True")
        assert "live_final = not degraded_sources" in code

    def test_the_degraded_set_is_created_once_per_doctor(self):
        """每位醫師一份 —— 不可以跨醫師累積（會把別人的降級算到這位頭上）。"""
        import main
        code = _code_of(main.check_appointment_count)
        assert code.count("degraded_sources: set = set()") == 1

    def test_a_degraded_round_still_shows_the_data(self):
        """★降級不是隱藏★ 畫面照常顯示，只是不取得寄信資格。

        （抑制顯示會讓醫師看不到號碼 —— 那比顯示舊號碼嚴重。）
        """
        import main
        code = _code_of(main.check_appointment_count)
        assert "UiClinicDataMessage" in code, "找不到 emit（測試失效了）"
        # ★突變驗證抓到：逐行找「同一行同時有 if 與 return」擋不住兩行的寫法★
        tree = ast.parse(code)
        bail_outs = [n for n in ast.walk(tree) if isinstance(n, ast.If)
                     and any(isinstance(x, ast.Name)
                             and x.id == "degraded_sources"
                             for x in ast.walk(n.test))
                     and any(isinstance(x, ast.Return) and x.value is None
                             for x in n.body)]
        assert bail_outs == [], (
            "降級時整批不送 → 醫師看不到號碼，那比看到舊號碼嚴重")
