# -*- coding: utf-8 -*-
"""[穩定性總體檢 批次SD] 外部第二意見 #7/#8/#10。

#7  main 退出路徑呼叫 `logging.shutdown()`：它對每個 handler 拿 lock 再
    close —— 背景緒正持有 handler lock 時無限期等。視窗已消失、單例已
    釋放，舊 process 卻變成看不見的殭屍；使用者重開會兩行程並存，
    「關掉重開」這條最重要的人工恢復路徑反而失效。
#8  `window_icon` 每次套用載 2 個 owned HICON、加兩次 redo = 每開一個
    視窗洩 6 個 USER 物件，從不 DestroyIcon。
#10 `clinic_stats_history.json` 無保留期且每次更新整檔重寫 ——
    常駐數月後每次診次更新線性變慢。
"""
import ast
import datetime
import io
import os
import sys

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from cmuh_common import clinic_history as ch  # noqa: E402
from cmuh_common import window_icon as wi  # noqa: E402


# ══ #7 退出路徑 ═══════════════════════════════════════════════════════════
class TestExitDoesNotCallLoggingShutdown:
    def test_shutdown_app_never_calls_logging_shutdown(self):
        """★logging.shutdown() 會等 handler lock★ 只能非阻塞 flush。"""
        text = io.open(os.path.join(REPO_ROOT, "src", "main.py"),
                       encoding="utf-8").read()
        tree = ast.parse(text)
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "shutdown_app":
                seg = ast.get_source_segment(text, n) or ""
                code = "\n".join(ln.split("#")[0] for ln in seg.splitlines())
                assert "logging.shutdown()" not in code, (
                    "★退出路徑仍呼叫 logging.shutdown → handler lock 被占住"
                    "時變成看不見的殭屍 process★")
                assert "acquire(blocking=False)" in code, (
                    "非阻塞 flush 不見了(閃退前至少要試著把 log 排出去)")
                return
        raise AssertionError("找不到 shutdown_app(守衛自己失效了)")

    def test_the_flush_releases_what_it_acquires(self):
        text = io.open(os.path.join(REPO_ROOT, "src", "main.py"),
                       encoding="utf-8").read()
        tree = ast.parse(text)
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name == "shutdown_app":
                seg = ast.get_source_segment(text, n) or ""
                i = seg.index("acquire(blocking=False)")
                assert "release()" in seg[i:i + 900], "拿了 lock 沒還"
                return
        raise AssertionError("找不到 shutdown_app")


# ══ #8 HICON ══════════════════════════════════════════════════════════════
class TestIconHandlesAreReleased:
    def setup_method(self):
        with wi._owned_icons_lock:
            wi._owned_icons.clear()

    teardown_method = setup_method

    def test_a_reapply_destroys_the_previous_pair(self, monkeypatch):
        """★同一個 hwnd 重複套用(含 redo)不可以累積 handle★"""
        destroyed = []

        def _fake_destroy(*hs):
            destroyed.extend(h for h in hs if h)

        monkeypatch.setattr(wi, "_destroy_icons", _fake_destroy)
        wi._remember_owned(100, 11, 12)
        wi._remember_owned(100, 21, 22)      # redo:換新的一對
        assert destroyed == [11, 12], (
            f"★上一輪的 handle 沒被釋放:{destroyed}★")
        with wi._owned_icons_lock:
            assert wi._owned_icons[100] == (21, 22)

    def test_different_windows_do_not_release_each_other(self, monkeypatch):
        destroyed = []
        monkeypatch.setattr(wi, "_destroy_icons",
                            lambda *hs: destroyed.extend(hs))
        wi._remember_owned(100, 11, 12)
        wi._remember_owned(200, 21, 22)
        assert not destroyed, "別的視窗的 handle 被錯放"

    def test_the_registry_is_bounded(self, monkeypatch):
        """★登記表自己不可以變成洩漏源★ 淘汰時要連 handle 一起釋放。"""
        destroyed = []
        monkeypatch.setattr(wi, "_destroy_icons",
                            lambda *hs: destroyed.extend(h for h in hs if h))
        for i in range(wi._OWNED_ICONS_MAX + 10):
            wi._remember_owned(1000 + i, i * 2 + 1, i * 2 + 2)
        with wi._owned_icons_lock:
            assert len(wi._owned_icons) <= wi._OWNED_ICONS_MAX
        assert destroyed, "淘汰的視窗 handle 沒被釋放"

    def test_the_apply_path_registers_what_it_loads(self):
        """★接線★ 載入點要走登記(不登記=修了等於沒修)。"""
        text = io.open(os.path.join(
            REPO_ROOT, "src", "cmuh_common", "window_icon.py"),
            encoding="utf-8").read()
        tree = ast.parse(text)
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and \
                    n.name == "_apply_windows_wm_seticon_from_ico":
                seg = ast.get_source_segment(text, n) or ""
                assert "_remember_owned(" in seg, (
                    "★套用路徑沒登記 handle → 洩漏照舊★")
                return
        raise AssertionError("找不到套用函式")


# ══ #10 clinic history 保留期 ═════════════════════════════════════════════
class TestHistoryRetention:
    TODAY = datetime.date(2026, 8, 10)

    def _row(self, d):
        return {"date": d, "doc_name": "X", "completed_count": 1}

    def test_old_rows_are_pruned(self):
        rows = [self._row("2020/01/01"), self._row("2026/08/09")]
        out = ch.prune_history_rows(rows, today=self.TODAY)
        assert [r["date"] for r in out] == ["2026/08/09"], (
            "★保留期沒生效 → 檔案無上限成長★")

    def test_rows_inside_the_window_survive(self):
        d = (self.TODAY - datetime.timedelta(days=ch.HISTORY_RETAIN_DAYS - 1)
             ).strftime("%Y/%m/%d")
        out = ch.prune_history_rows([self._row(d)], today=self.TODAY)
        assert out, "還在保留期內的列被丟掉"

    def test_unparseable_dates_are_kept(self):
        """★看不懂不丟★ 丟掉=安靜地改變歷史統計;保留只是幾列垃圾。

        ★十個字元的【無效】日期是關鍵案例★(外審 SD 第1輪抓到)
        第一版判準是 `len(d) == 10 and d < cutoff` —— `"2020-01-01"`
        長度剛好 10、字串序也小於斜線格式的 cutoff(`-` < `/`),
        於是被靜靜刪掉,正好違反這個函式自己的契約。
        測試資料一定要含這種「長度對、格式錯」的列。
        """
        rows = [
            self._row(""),
            self._row("垃圾"),
            {"doc_name": "Y"},
            self._row("2020-01-01"),      # ★舊格式:長度 10 但解析不出來★
            self._row("2020.01.01"),      # ★同上,另一種分隔★
            self._row("01/01/2020"),      # ★長度 10、順序不同★
            self._row("2020/13/45"),      # ★格式對但不是合法日期★
        ]
        out = ch.prune_history_rows(rows, today=self.TODAY)
        assert len(out) == len(rows), (
            "★看不懂的日期被刪了 → 安靜地改變歷史統計★:"
            + str([r.get("date") for r in rows
                   if r not in out]))

    def test_the_save_path_actually_prunes(self):
        """★接線★ `_save_clinic_session_stat` 寫回前要套保留期。"""
        text = io.open(os.path.join(REPO_ROOT, "src", "main.py"),
                       encoding="utf-8").read()
        tree = ast.parse(text)
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and \
                    n.name == "_save_clinic_session_stat":
                seg = ast.get_source_segment(text, n) or ""
                i_prune = seg.index("_prune_history_rows")
                i_write = seg.index("_atomic_write_json")
                assert i_prune < i_write, "保留期套在寫回之後(等於沒套)"
                return
        raise AssertionError("找不到 _save_clinic_session_stat")

    def test_the_retention_is_generous(self):
        """730 天遠大於目前產品年齡 —— 上線當下必須是零行為差異。"""
        assert ch.HISTORY_RETAIN_DAYS >= 365
