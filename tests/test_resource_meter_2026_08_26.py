# -*- coding: utf-8 -*-
"""Phase 0:主程式內建資源量測(使用者 2026-08-26 定案:自動量測、不另開視窗)。

規則與反例:
  1. 家族根的認定:cmdline 命中 token 的 python 行程;宿主自己不重複算;
  2. 子孫樹依 ppid 歸屬,★不搶已被認領的行程★(別的根不會被吞成子孫);
  3. 逐 exe 彙總(Chrome 全樹是一列,不是幾十列);
  4. ★fail-open★:行程表炸掉 → self 列照寫;寫檔炸掉 → 不拋;
  5. 舊月檔清理只認自己的檔名格式;
  6. 佈線:主程式啟動路徑真的 start 了(沒有呼叫端的量測器等於沒有量測);
  7. ★不另開視窗★:行程表的 PowerShell 一定帶 CREATE_NO_WINDOW。
"""
import ast
import inspect
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from cmuh_common import resource_meter as rm  # noqa: E402
from cmuh_common.resource_meter import (  # noqa: E402
    ResourceMeter, descendant_tree, family_roots,
)

# 假行程表:會診(pid 10)+systemftp 子(11);打卡(20)→chromedriver(21)→chrome×2;
# 宿主自己 pid 99 有一個子行程(100);一個不相干的 python(30)與記事本(40)。
TABLE = [
    {"pid": 10, "ppid": 1, "name": "python.exe",
     "cmd": r"C:\py\python.exe C:\app\consult_query.py"},
    {"pid": 11, "ppid": 10, "name": "systemftp.exe", "cmd": ""},
    {"pid": 20, "ppid": 1, "name": "python.exe",
     "cmd": r"C:\py\python.exe C:\app\autoclock.py"},
    {"pid": 21, "ppid": 20, "name": "chromedriver.exe", "cmd": ""},
    {"pid": 22, "ppid": 21, "name": "chrome.exe", "cmd": ""},
    {"pid": 23, "ppid": 22, "name": "chrome.exe", "cmd": ""},
    {"pid": 30, "ppid": 1, "name": "python.exe", "cmd": "python -m pip list"},
    {"pid": 40, "ppid": 1, "name": "notepad.exe", "cmd": "notepad"},
    {"pid": 100, "ppid": 99, "name": "chromedriver.exe", "cmd": ""},
]


def _fake_sampler(pid):
    return {"user": 1.0, "kernel": 0.5, "rss": 10.0, "handles": 7,
            "gdi": 0, "user_objs": 0}


def _meter(tmp_path, *, table=None, sampler=_fake_sampler, monkeypatch=None):
    m = ResourceMeter(str(tmp_path), "主程式", "test",
                      proc_table=(lambda: TABLE) if table is None else table,
                      pid_sampler=sampler)
    if monkeypatch is not None:
        monkeypatch.setattr(os, "getpid", lambda: 99)
    return m


def _rows(tmp_path):
    files = [f for f in os.listdir(tmp_path) if f.startswith("resource_meter_")]
    assert len(files) == 1
    with open(os.path.join(tmp_path, files[0]), encoding="utf-8") as f:
        return [ln.strip().split(",") for ln in f.readlines()[1:]]


# ══ 1~3. 家族認定/子孫樹/彙總 ═══════════════════════════════════════════
class TestTheFamilyModel:
    def test_roots_are_matched_by_cmdline_token(self):
        roots = family_roots(TABLE, 99)
        assert {(lb, p["pid"]) for lb, p in roots} == {
            ("會診查詢", 10), ("打卡", 20)}, "不相干的 python/記事本不可入列"

    def test_the_host_itself_is_not_a_root(self):
        table = TABLE + [{"pid": 99, "ppid": 1, "name": "python.exe",
                          "cmd": r"python C:\app\main.py"}]
        assert 99 not in {p["pid"] for _, p in family_roots(table, 99)}

    def test_descendants_follow_the_ppid_tree(self):
        claimed = {10, 20, 99}
        got = descendant_tree(TABLE, 20, claimed)
        assert {p["pid"] for p in got} == {21, 22, 23}, "chrome 全樹要跟著 chromedriver"

    def test_a_root_is_never_swallowed_as_a_descendant(self):
        """★反例只靠 claimed 分勝負★:把會診(10)掛成打卡(20)的子行程,
        它仍是獨立的根,不可被吞進打卡的子孫樹。"""
        table = [dict(p, ppid=(20 if p["pid"] == 10 else p["ppid"]))
                 for p in TABLE]
        claimed = {10, 20, 99}
        got = descendant_tree(table, 20, claimed)
        assert 10 not in {p["pid"] for p in got}
        assert 11 not in {p["pid"] for p in got}, "被認領根的子孫也不歸這棵"

    def test_children_are_aggregated_per_exe(self, tmp_path, monkeypatch):
        m = _meter(tmp_path, monkeypatch=monkeypatch)
        m.sample_once()
        rows = _rows(tmp_path)
        chrome = [r for r in rows if r[4] == "child" and r[5] == "chrome.exe"]
        assert len(chrome) == 1 and chrome[0][7] == "2", (
            "chrome 兩個行程要彙總成一列(n_procs=2)")
        host_child = [r for r in rows
                      if r[3] == "主程式" and r[4] == "child"]
        assert len(host_child) == 1 and host_child[0][5] == "chromedriver.exe", (
            "宿主自己的子行程(pid 100)也要量")

    def test_every_scope_kind_is_present(self, tmp_path, monkeypatch):
        m = _meter(tmp_path, monkeypatch=monkeypatch)
        m.sample_once()
        scopes = {r[4] for r in _rows(tmp_path)}
        assert {"system", "self", "proc", "child"} <= scopes


# ══ 4. fail-open ════════════════════════════════════════════════════════
class TestFailOpen:
    def test_a_broken_proc_table_still_writes_self(self, tmp_path):
        def _boom():
            raise RuntimeError("CIM 掛了")
        m = _meter(tmp_path, table=_boom)
        m.sample_once()
        rows = _rows(tmp_path)
        assert any(r[4] == "self" for r in rows), "行程表炸掉不可拖累 self 列"

    def test_an_unwritable_dir_does_not_raise(self, tmp_path):
        m = ResourceMeter(str(tmp_path / "沒有這個目錄" / "x"), "主程式",
                          "test", proc_table=lambda: [],
                          pid_sampler=_fake_sampler)
        m.sample_once()          # 不拋即過

    def test_a_failing_pid_sampler_skips_that_process_only(
            self, tmp_path, monkeypatch):
        m = _meter(tmp_path, monkeypatch=monkeypatch,
                   sampler=lambda pid: _fake_sampler(pid) if pid != 10 else None)
        m.sample_once()
        rows = _rows(tmp_path)
        assert not any(r[4] == "proc" and r[6] == "10" for r in rows)
        assert any(r[4] == "proc" and r[6] == "20" for r in rows), (
            "一個行程量不到不可拖累其他行程")


# ══ 5. 舊檔清理 ═════════════════════════════════════════════════════════
def test_prune_only_touches_its_own_old_files(tmp_path):
    from datetime import datetime
    old = tmp_path / "resource_meter_202601.csv"
    cur = tmp_path / "resource_meter_202608.csv"
    other = tmp_path / "resource_baseline_keep.csv"
    for f in (old, cur, other):
        f.write_text("x", encoding="utf-8")
    m = ResourceMeter(str(tmp_path), "主程式", "test",
                      proc_table=lambda: [], pid_sampler=_fake_sampler)
    m.prune_old_files(now=datetime(2026, 8, 26))
    assert not old.exists(), "62 天前的月檔要清"
    assert cur.exists() and other.exists(), "本月檔與別人的檔不可動"


# ══ 6~7. 佈線與「不另開視窗」════════════════════════════════════════════
def test_main_starts_the_meter_on_its_startup_path():
    """★沒有呼叫端的量測器等於沒有量測★(wired-up-or-it-does-not-exist)。"""
    p = os.path.join(os.path.dirname(__file__), "..", "src", "main.py")
    with open(p, encoding="utf-8") as f:
        src = f.read()
    tail = src[src.index('if __name__ == "__main__":'):]
    assert "ResourceMeter(" in tail
    # ★要指名量測器自己的 start★:`.start()` 在 __main__ 段落裡別的執行緒也有,
    #   籠統斷言會讓「建了量測器卻沒啟動」照樣綠(突變 #14 抓到)。
    assert "_resource_meter.start()" in tail
    i = tail.index("ResourceMeter(")
    assert "AutomationApp(main_root" in tail[:i], (
        "量測要在主程式初始化成功之後才啟動(初始化失敗的路徑不量)")


def test_the_proc_table_never_opens_a_window():
    """★使用者定案:不另開視窗★ —— PowerShell 一定帶 CREATE_NO_WINDOW。"""
    src = inspect.getsource(rm._default_proc_table)
    assert "_CREATE_NO_WINDOW" in src
    assert rm._CREATE_NO_WINDOW == 0x08000000
    tree = ast.parse(inspect.getsource(rm))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "attr", "") == "run"):
            kw = {k.arg for k in node.keywords}
            assert "creationflags" in kw, "subprocess.run 沒帶 creationflags"


def test_production_defaults_are_the_real_collectors():
    """注入點是測試用的;★生產預設必須是真的收集器★,不然整支是空殼。"""
    m = ResourceMeter(".", "x", "v")
    assert m._proc_table is rm._default_proc_table
    assert m._pid_sampler is rm._default_pid_sampler


def test_every_row_has_the_full_column_count(tmp_path, monkeypatch):
    """★欄位數用測的,不用數逗號的★:四種列(system/self/proc/child)都要
    剛好 19 欄,少一欄整份報告的欄位就全部錯位。"""
    m = _meter(tmp_path, monkeypatch=monkeypatch)
    m.sample_once()
    for r in _rows(tmp_path):
        assert len(r) == 19, (len(r), r)


@pytest.mark.parametrize("iso", ["2026-08-26T10:00:00"])
def test_the_csv_header_is_stable(tmp_path, iso):
    """報告腳本靠欄位順序;改欄位要 append 不重排(釘住 header)。"""
    m = ResourceMeter(str(tmp_path), "主程式", "test",
                      proc_table=lambda: [], pid_sampler=_fake_sampler)
    m.sample_once()
    files = [f for f in os.listdir(tmp_path) if f.startswith("resource_meter_")]
    with open(os.path.join(tmp_path, files[0]), encoding="utf-8") as f:
        header = f.readline().strip()
    assert header == ("ts,host,version,label,scope,exe,pid,n_procs,"
                      "cpu_user_s,cpu_kernel_s,rss_mb,handles,gdi,user_objs,"
                      "py_threads,sys_idle_s,sys_kernel_s,sys_user_s,"
                      "mem_load_pct")


def test_the_loop_samples_immediately_on_start(tmp_path, monkeypatch):
    """★開機先取一筆★:不然要等 5 分鐘才有第一筆,短命行程什麼都留不下。
    (stop 先 set → _loop 取完第一筆就返回,不會卡測試。)"""
    m = ResourceMeter(str(tmp_path), "主程式", "test",
                      proc_table=lambda: [], pid_sampler=_fake_sampler)
    m._stop.set()
    m._loop()
    assert any(f.startswith("resource_meter_") for f in os.listdir(tmp_path))


# ══ 外審 Phase0 R1 的三個修正 ═══════════════════════════════════════════
class TestTheRoundOneFindings:
    def test_the_production_launcher_command_line_is_recognized(self):
        """★生產的命令列不含英文模組名★(R1 P1):launcher 用 runpy 行程內
        執行,CommandLine 是 `pythonw.exe 中國醫皮膚科打卡程式.pyw`。
        我的第一版只在開發機形狀上測過 —— 又一次「沒用生產呼叫形狀」。"""
        table = [
            {"pid": 50, "ppid": 1, "name": "pythonw.exe",
             "cmd": r'"C:\py\pythonw.exe" "C:\app\中國醫皮膚科打卡程式.pyw"'},
            {"pid": 51, "ppid": 1, "name": "pythonw.exe",
             "cmd": r'"C:\py\pythonw.exe" "C:\app\中國醫皮膚科會診查詢程式.pyw"'},
            {"pid": 52, "ppid": 1, "name": "pythonw.exe",
             "cmd": r'"C:\py\pythonw.exe" "C:\app\中國醫皮膚科守護程式.pyw"'},
        ]
        got = {(lb, p["pid"]) for lb, p in family_roots(table, 99)}
        assert got == {("打卡", 50), ("會診查詢", 51), ("watchdog", 52)}, got

    def test_an_unknown_rss_does_not_poison_the_aggregate(
            self, tmp_path, monkeypatch):
        """★未知不是 0,也加不得★(R1 P2):讀不到記憶體的子行程,rss 記空欄,
        彙總只加得到的那些 —— 混著加會整段炸掉,當 0 加會低估。"""
        def _mixed(pid):
            d = _fake_sampler(pid)
            return dict(d, rss=("" if pid == 22 else 5.0))
        m = _meter(tmp_path, sampler=_mixed, monkeypatch=monkeypatch)
        m.sample_once()
        rows = _rows(tmp_path)
        chrome = [r for r in rows if r[4] == "child" and r[5] == "chrome.exe"]
        assert len(chrome) == 1 and chrome[0][7] == "2", chrome
        assert chrome[0][10] == "5.0", (
            f"未知 rss 要跳過、已知的照加: {chrome[0]}")

    def test_prune_keeps_a_month_whose_end_is_within_the_window(self, tmp_path):
        """★62 天以【月底】算★(R1 P2):8/03 時,6 月檔的月底資料才 34 天,
        整檔帶走等於把保留期砍半;5 月檔的月底已 64 天 → 才可以刪。
        ★反例只靠年齡基準(月初 vs 月底)分勝負★。"""
        from datetime import datetime
        june = tmp_path / "resource_meter_202606.csv"
        may = tmp_path / "resource_meter_202605.csv"
        for f in (june, may):
            f.write_text("x", encoding="utf-8")
        m = ResourceMeter(str(tmp_path), "主程式", "test",
                          proc_table=lambda: [], pid_sampler=_fake_sampler)
        m.prune_old_files(now=datetime(2026, 8, 3))
        assert june.exists(), "6 月檔的月底資料才 34 天,不可刪"
        assert not may.exists(), "5 月檔整個月都超過 62 天,要刪"

    def test_a_memory_read_failure_is_blank_not_zero(self):
        """★源碼釘住誠實出口★:K32GetProcessMemoryInfo 失敗的分支必須產出
        空字串(未知),不是 0.0。單元層無法注入 API 失敗,故以源碼守住 ——
        搬家/改寫時這條會逼人重新面對這個決定。"""
        src = inspect.getsource(rm._sample_handle)
        assert 'else "")' in src and "else 0.0" not in src

    def test_an_all_unknown_aggregate_is_blank_not_zero(
            self, tmp_path, monkeypatch):
        """★同一條誠實規則在彙總層也要成立★(R2):兩個子行程都量不到記憶體
        → 彙總欄是【空】—— 累加器初值 0.0 漏出去就是假資料。
        ★反例只靠「有沒有任何已知值」分勝負★(上一條測的是混合情境)。"""
        def _all_unknown(pid):
            return dict(_fake_sampler(pid), rss="")
        m = _meter(tmp_path, sampler=_all_unknown, monkeypatch=monkeypatch)
        m.sample_once()
        chrome = [r for r in _rows(tmp_path)
                  if r[4] == "child" and r[5] == "chrome.exe"]
        assert len(chrome) == 1 and chrome[0][10] == "", chrome
