# -*- coding: utf-8 -*-
"""批次 G：外部 code review 的三個 P2（2026-08-02）。

  P2-01 韌性層的時間差改用 monotonic  → `TestClocksThatCannotBeMovedBackwards`
  P2-04 prune_index 回傳型別         → `TestPruneResult`
  P2-03 明文 HTTP 來源不可信          → `TestUntrustedPlaintextSources`
"""
from __future__ import annotations

import ast
import inspect
import os
from datetime import datetime, timedelta

import pytest
from bs4 import BeautifulSoup

from cmuh_common import fetch_resilience as fr
from cmuh_common import patient_locator as pl
from cmuh_common import reg52_fetch as rf
from cmuh_common import reg52_parse as rp


@pytest.fixture(autouse=True)
def _clean():
    fr.reset_all()
    yield
    fr.reset_all()


# ── P2-01：牆上時鐘不可以決定退避／節流／TTL ──────────────────────────
class TestClocksThatCannotBeMovedBackwards:

    def test_no_wall_clock_left_in_the_resilience_layer(self):
        """★機械化地掃整個模組★ 只釘現在改到的那幾行，下次有人新增就漏了。

        用 AST 找 `time.time()` 呼叫（字串搜尋會被說明文字餵飽 —— 這份檔案的
        docstring 裡就寫著 `time.time()`）。
        """
        tree = ast.parse(inspect.getsource(fr))
        wall = [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "time"
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "time"]
        assert wall == [], f"第 {wall} 行還在用 time.time() 量經過時間"

    def test_the_backoff_survives_a_wall_clock_jump_backwards(self,
                                                              monkeypatch):
        """★這是 P2-01 真正的傷害★

        退避存的是絕對時間。牆上時鐘往回跳兩小時（NTP 校正、手動改時間、
        機器久沒開），那個來源就被自己的退避擋住兩小時 —— 而且沒有任何東西
        會提早解除它。
        """
        base = 5_000.0
        monkeypatch.setattr(fr.time, "monotonic", lambda: base)
        fr._source_backoff_fail("east:12345", 2, 90)
        assert fr._source_backoff_allow("east:12345")[0] is False

        # 牆上時鐘往回跳兩小時；monotonic 不受影響
        monkeypatch.setattr(fr.time, "time", lambda: 0.0)
        monkeypatch.setattr(fr.time, "monotonic", lambda: base + 3.0)

        allowed, remain = fr._source_backoff_allow("east:12345")
        assert allowed is True, "★時鐘往回跳把來源鎖住了★"
        assert remain == 0.0

    def test_the_ttl_cache_survives_a_wall_clock_jump_forwards(self,
                                                              monkeypatch):
        base = 5_000.0
        monkeypatch.setattr(fr.time, "monotonic", lambda: base)
        fr._cache_set("k", "v")

        monkeypatch.setattr(fr.time, "time", lambda: 1e12)   # 時鐘往前跳
        monkeypatch.setattr(fr.time, "monotonic", lambda: base + 1.0)
        assert fr._cache_get("k", 60) == "v", "牆上時鐘往前跳不該讓快取瞬間過期"

    def test_the_parse_cache_uses_the_same_clock(self, monkeypatch):
        base = 5_000.0
        monkeypatch.setattr(fr.time, "monotonic", lambda: base)
        fr._parse_cache_set("p", "<html>x</html>", {"a": 1})

        monkeypatch.setattr(fr.time, "monotonic",
                            lambda: base + fr.PARSE_CACHE_TTL_SECONDS + 1)
        assert fr._parse_cache_get("p", "<html>x</html>") is None

    def test_the_first_call_after_boot_is_not_throttled(self, monkeypatch):
        """★這是換基準時最容易踩到的坑★

        `_source_throttle_state.get(key, 0.0)` 只有在 `now` 是「一億七千萬」
        那種牆上時間時才成立。monotonic 在 Windows 上是【開機以來的秒數】——
        剛開機時只有幾十秒，`now - 0.0 < interval` 成真，於是每天早上第一次
        抓取會被自己的節流擋掉，而且完全沒有紀錄可查。
        """
        monkeypatch.setattr(fr.time, "monotonic", lambda: 12.0)   # 剛開機
        allowed, remain = fr._source_throttle_allow("east:dayoff", 600)
        assert allowed is True, "★開機後第一次就被自己的節流擋掉★"
        assert remain == 0.0

    def test_the_throttle_still_throttles(self, monkeypatch):
        """★空集合不算通過★ 證明上面那支不是因為節流整個失效才綠的。"""
        monkeypatch.setattr(fr.time, "monotonic", lambda: 12.0)
        assert fr._source_throttle_allow("east:dayoff", 600)[0] is True
        monkeypatch.setattr(fr.time, "monotonic", lambda: 300.0)
        allowed, remain = fr._source_throttle_allow("east:dayoff", 600)
        assert allowed is False
        assert remain == pytest.approx(312.0)

    def test_the_circuit_breaker_was_already_monotonic(self, monkeypatch):
        """對照組：熔斷器本來就用 monotonic，這一批沒有動它。"""
        base = 100.0
        monkeypatch.setattr(fr.time, "monotonic", lambda: base)
        for _ in range(fr._CIRCUIT_BREAKER_THRESHOLD):
            fr._circuit_record_fail("east")
        assert fr._circuit_is_tripped("east") is True
        monkeypatch.setattr(fr.time, "monotonic",
                            lambda: base + fr._CIRCUIT_BREAKER_RESET_SEC + 1)
        assert fr._circuit_is_tripped("east") is False


# ── P2-04：保留期失敗不可以長得像「沒事」 ────────────────────────────
class TestPruneResult:

    def _write(self, path, rows):
        import json
        path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8")

    def test_a_failure_is_distinguishable_from_nothing_to_do(self, tmp_path,
                                                             monkeypatch):
        """★這是 P2-04 的核心★

        原本兩者都回 `0`。這是一道保留期控制：默默失敗的意思是病人的病歷號
        留在磁碟上超過宣告的 30 天，而唯一的呼叫端只會當成「今天沒事」。
        """
        now = datetime.now()
        p = tmp_path / "idx.jsonl"
        self._write(p, [{"ts": (now - timedelta(days=99)).isoformat(),
                         "action": "舊", "chart_no": "24994923"}])

        monkeypatch.setattr(os, "replace",
                            lambda *_a: (_ for _ in ()).throw(OSError("鎖住")))
        failed = pl.prune_index(str(p), now=now)

        empty = tmp_path / "empty.jsonl"
        empty.write_text("", encoding="utf-8")
        nothing = pl.prune_index(str(empty), now=now)

        assert failed.status != nothing.status, (
            "★失敗與「沒事可做」還是分不開★")
        assert failed.ok is False
        assert nothing.ok is True

    def test_a_failure_says_why_without_leaking_a_path(self, tmp_path,
                                                       monkeypatch):
        """★reason 只放例外類別名★ 例外訊息可能含檔案路徑，路徑含使用者名稱。"""
        p = tmp_path / "idx.jsonl"
        self._write(p, [{"ts": "2020-01-01T00:00:00", "action": "舊"}])
        monkeypatch.setattr(
            os, "replace",
            lambda *_a: (_ for _ in ()).throw(
                OSError(r"C:\Users\SomeRealName\settings\idx.jsonl 被鎖住")))

        result = pl.prune_index(str(p))

        assert result.status == pl.PRUNE_FAILED
        assert result.reason == "OSError"
        assert "Users" not in result.describe()
        assert "SomeRealName" not in result.describe()

    def test_a_successful_prune_reports_both_counts(self, tmp_path):
        now = datetime.now()
        p = tmp_path / "idx.jsonl"
        self._write(p, [
            {"ts": (now - timedelta(days=99)).isoformat(), "action": "舊"},
            {"ts": (now - timedelta(days=1)).isoformat(), "action": "新"},
        ])
        result = pl.prune_index(str(p), now=now)
        assert (result.status, result.removed, result.kept) == (
            pl.PRUNE_OK, 1, 1)
        assert result.ok is True

    @pytest.mark.parametrize("status,expected", [
        (pl.PRUNE_OK, True),
        (pl.PRUNE_NOTHING_TO_DO, True),
        (pl.PRUNE_FAILED, False),
    ])
    def test_only_a_real_failure_is_not_ok(self, status, expected):
        assert pl.PruneResult(status).ok is expected

    def test_every_status_has_its_own_wording(self):
        said = {pl.PruneResult(s, removed=1, kept=2).describe()
                for s in (pl.PRUNE_OK, pl.PRUNE_NOTHING_TO_DO, pl.PRUNE_FAILED)}
        assert len(said) == 3

    def test_the_sweeper_reports_the_failure_instead_of_swallowing_it(self):
        """呼叫端要把失敗變成 `res.failed`，不是回 0 混進「沒事」。

        `retention.sweep` 的契約是「callable 回傳處理掉幾筆」，而它已經會把
        例外記進 `res.failed[label]` —— 所以 adapter 用【拋】來表示失敗。
        """
        import main
        src = inspect.getsource(main._prune_locator_index)
        assert ".ok" in src and "raise" in src, (
            "adapter 沒有把 prune 失敗轉成 sweep 看得懂的失敗")

    def test_the_sweeper_logs_a_failure_at_error_level(self):
        """★保留期沒跑完不可以只是 info★ 那會淹沒在正常的清掃紀錄裡。"""
        import main
        tree = ast.parse(inspect.getsource(main.run_retention_sweep))
        levels = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute)
                  and isinstance(n.func.value, ast.Name)
                  and n.func.value.id == "logging"}
        assert "error" in levels, "清掃失敗只會被記成 info/debug"

    def test_a_real_sweep_marks_the_failed_task(self, tmp_path, monkeypatch):
        """★真的跑一次 sweep★ 不只驗結構。"""
        from cmuh_common.retention import sweep

        def _boom():
            raise RuntimeError("★修剪失敗★OSError")

        res = sweep([], extra_tasks=[("定位索引", _boom)])
        assert res.failed.get("定位索引") == 1
        assert "刪不掉" in res.summary()


# ── P2-03：明文 HTTP 來源的內容不可信 ────────────────────────────────
class TestUntrustedPlaintextSources:

    def test_every_plaintext_url_is_declared_untrusted(self):
        """★守衛要掃「性質涵蓋的所有東西」，不是列舉當下那兩個★

        哪天有人再加一個 `http://` 來源而忘了寫進宣告，這裡要轉紅。
        """
        tree = ast.parse(inspect.getsource(rf))
        plaintext = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(
                    node.value, ast.Constant) and isinstance(
                    node.value.value, str):
                if node.value.value.startswith("http://"):
                    plaintext += [t.id for t in node.targets
                                  if isinstance(t, ast.Name)]
        assert plaintext, "找不到任何明文 URL（測試失效了）"
        declared = set(rf.PLAINTEXT_REG52_SOURCES)
        for name in plaintext:
            key = name.split("_")[0].lower()
            key = {"east": "east", "huisheng": "huisheng"}.get(key, key)
            assert any(d in name.lower() for d in declared), (
                f"{name} 是明文 HTTP，但沒有列進 PLAINTEXT_REG52_SOURCES")

    def test_the_https_sources_are_not_marked_plaintext(self):
        """★空集合不算通過★ 惠和與亞大是 https，不該被列進去。"""
        assert "huihe" not in rf.PLAINTEXT_REG52_SOURCES
        assert "auh" not in rf.PLAINTEXT_REG52_SOURCES
        assert rf.HUIHE_REG52_URL.startswith("https://")
        assert rf.AUH_REG52_BASE_URL.startswith("https://")

    def test_an_absurd_count_is_dropped_rather_than_believed(self):
        """★被塞進來的荒謬掛號數不可以變成假的止掛提醒★"""
        html = _branch_html(count="999999")
        parsed = rp.parse_east_fh1_schedule(BeautifulSoup(html, "lxml"))
        assert parsed == {}, "不合理的掛號數應該讓整格被略過"

    def test_an_absurd_count_is_not_silently_turned_into_zero(self):
        """★0 的意思是「這診沒人掛」—— 與「這格不可信」意義相反★"""
        html = _branch_html(count="999999")
        parsed = rp.parse_east_fh1_schedule(BeautifulSoup(html, "lxml"))
        counts = [a["count"] for slots in parsed.values() for a in slots]
        assert 0 not in counts

    def test_a_normal_count_still_gets_through(self):
        """★空集合不算通過★ 上面兩支不可以是因為整個解析壞掉才綠的。"""
        html = _branch_html(count="12")
        parsed = rp.parse_east_fh1_schedule(BeautifulSoup(html, "lxml"))
        counts = [a["count"] for slots in parsed.values() for a in slots]
        assert counts == [12]

    @pytest.mark.parametrize("digits,ok", [
        ("0", True), ("2000", True), ("2001", False), ("9999", False),
        ("10000", False),
    ])
    def test_the_bound_is_where_it_says_it_is(self, digits, ok):
        got = rp._bounded_count(digits, "測試")
        assert (got is not None) is ok

    def test_a_gigantic_number_does_not_blow_up_the_parser(self):
        """CPython 對超長數字字串的 int() 有上限（>4300 位丟 ValueError）。
        先看位數再轉，才不會在解析器裡炸掉。"""
        assert rp._bounded_count("9" * 100_000, "測試") is None

    def test_an_over_long_room_becomes_no_room_not_a_truncated_one(self):
        """★不截斷★ 截斷會生出一個看起來合理、實際是編造的診間號，
        而診間號會被印在止掛通知裡。"""
        long_room = "A" + "1" * 40
        assert rp._bounded_room(long_room + "診", "測試") == ""
        assert rp._bounded_room("A101診", "測試") == "A101診"

    def test_every_room_extraction_goes_through_the_bound(self):
        """★守衛要涵蓋所有取診間號的地方★ 三個解析器都要用同一道關卡。"""
        tree = ast.parse(inspect.getsource(rp))
        searches = [n for n in ast.walk(tree)
                    if isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr == "search"
                    and isinstance(n.func.value, ast.Name)
                    and n.func.value.id == "_RE_ROOM"]
        bounded = [n for n in ast.walk(tree)
                   if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Name)
                   and n.func.id == "_bounded_room"]
        assert searches, "找不到任何診間號解析（測試失效了）"
        assert len(bounded) >= len(searches), (
            f"有 {len(searches)} 處取診間號，只有 {len(bounded)} 處過關卡")

    def test_every_count_extraction_goes_through_the_bound(self):
        tree = ast.parse(inspect.getsource(rp))
        raw = [n.lineno for n in ast.walk(tree)
               if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Name) and n.func.id == "int"
               and n.args and isinstance(n.args[0], ast.Call)
               and isinstance(n.args[0].func, ast.Attribute)
               and n.args[0].func.attr == "group"]
        assert raw == [], (
            f"第 {raw} 行直接 int(match.group(...))，沒有過範圍檢查")

    def test_the_declaration_is_actually_read_by_production_code(self):
        """★[2026-08-02 外審第 2 輪 P2] 宣告了安全性質卻沒有對應行為 = 沒做★

        我第一版只加了一個常數和一段註解，生產程式碼【一行都沒有讀它】——
        只有註解與這份測試在引用。那是「宣稱要對得上實作」的反例。
        """
        import main
        assert "is_plaintext_source" in inspect.getsource(
            main.AutomationApp._dispatch_future_stop_alert_inner), (
            "止掛提醒沒有用到來源信任分類")

    def test_every_stop_alert_message_marks_untrusted_sources(self):
        """★[2026-08-02 外審第 3 輪 P2] 止掛提醒有【兩條】路徑★

        我第二版只標註了遠期那條，本週行事曆那條原封不動 —— 同一個偽造的數字
        換條路就又變回「看起來已驗證」。而我那支測試只看我改過的那一支，
        所以它綠得毫無意義。

        ★這支改成機械化地找【所有】建止掛訊息的地方★：任何組出含「目前掛號」
        字樣的 f-string 的函式，都必須在同一支函式裡問過 `is_plaintext_source`。
        以後再多一條路徑會在這裡轉紅。
        """
        import main

        class _Finder(ast.NodeVisitor):
            def __init__(self):
                self.stack = []
                self.hits = []

            def visit_FunctionDef(self, node):
                self.stack.append(node)
                self.generic_visit(node)
                self.stack.pop()

            visit_AsyncFunctionDef = visit_FunctionDef

            def visit_JoinedStr(self, node):
                text = "".join(v.value for v in node.values
                               if isinstance(v, ast.Constant)
                               and isinstance(v.value, str))
                if "目前掛號" in text and self.stack:
                    self.hits.append(self.stack[-1])   # 最內層的那個函式
                self.generic_visit(node)

        finder = _Finder()
        finder.visit(ast.parse(inspect.getsource(main)))
        assert finder.hits, "找不到任何止掛提醒訊息（測試失效了）"
        assert len({f.lineno for f in finder.hits}) >= 2, (
            "應該至少有兩條止掛提醒路徑（遠期掃描 + 本週行事曆）")

        missing = sorted({
            f"{fn.name}:{fn.lineno}" for fn in finder.hits
            if not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                       and n.func.id == "is_plaintext_source"
                       for n in ast.walk(fn))})
        assert missing == [], (
            f"這些地方建了止掛提醒訊息卻沒有標註不可信來源：{missing}")

    @pytest.mark.parametrize("branch,marked", [
        ("east", True), ("huisheng", True),
        ("huihe", False), ("auh", False), ("", False), (None, False),
    ])
    def test_only_plaintext_branches_are_classified_untrusted(self, branch,
                                                              marked):
        assert rf.is_plaintext_source(branch) is marked

    def test_a_plausible_forged_count_still_reaches_the_doctor_but_marked(self):
        """★這是 finding 的核心情境★

        攻擊者不需要塞 999999 —— 把東區某診改成 130 就能寄出一封與真的一模一樣
        的止掛提醒。範圍檢查對此無能為力，所以信裡必須說清楚來源不可驗證。

        ★標註而不是抑制★ 同時釘住「照樣寄」：抑制會讓分院的止掛提醒安靜消失。
        """
        html = _branch_html(count="130")
        parsed = rp.parse_east_fh1_schedule(BeautifulSoup(html, "lxml"))
        counts = [a["count"] for slots in parsed.values() for a in slots]
        assert counts == [130], "看起來合理的偽造值本來就擋不住 —— 它會照樣進來"

        assert rf.is_plaintext_source("east") is True
        assert "無法驗證" in rf.UNVERIFIED_TRANSPORT_NOTE

    def test_the_note_does_not_overstate_what_we_know(self):
        """★措辭鐵律★ 我們不知道「這筆被改過」，只知道「無從分辨」。"""
        note = rf.UNVERIFIED_TRANSPORT_NOTE
        for overclaim in ("已被竄改", "遭到攻擊", "確定"):
            assert overclaim not in note

    def test_the_threat_model_is_written_down_next_to_the_urls(self):
        """★宣稱要對得上實作★ 這個宣告是給下一個人看的，不能只有一個常數名。"""
        src = inspect.getsource(rf)
        head = src[:src.index("EAST_DISTRICT_REG52_URL")]
        for word in ("明文", "威脅模型", "reg52_parse"):
            assert word in head, f"威脅模型說明少了「{word}」"


def _branch_html(count: str) -> str:
    """一張最小的分院週表。"""
    return (
        "<html><body><table><tr>"
        "<td>上午</td>"
        "<td>(A101診)"
        '<div class="visitDate"><b>115/08/03</b> 已掛號：' + count + "</div>"
        "</td></tr></table></body></html>"
    )
