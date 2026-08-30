# -*- coding: utf-8 -*-
"""[外審第二輪 R2-P2-02/03/06] 保留期的執行者、靜默失敗、與 durable 寫入。

R2-P2-02 ★誰產生敏感資料,誰就要自己執行保留期★
  會診截圖(7 天)與打卡除錯檔(3 天)是由【另外兩支獨立程式】產生的,而全域清掃
  只由主程式在啟動時與每日固定時間跑。watchdog 允許「這台只跑會診+打卡、主程式
  很少開」的合法部署(治療室共用電腦正是這樣)。
  而兩支程式原本的 TTL 清掃★只在產生新資料時★被呼叫(存新截圖/存新除錯檔那一刻)
  —— 事件一停就再也不跑。兩者合起來:宣告的保留期不再是保證。

R2-P2-03 ★連年齡都讀不到的檔會被靜默跳過★
  `except OSError: continue` —— 不進 failed、不進 oldest、不進摘要。於是
  「磁碟上還躺著一個含 PHI 的檔」與「沒有過期檔案」長得一模一樣。
  ★無法確認檔案年齡 ≠ 檔案在保留期內★。

R2-P2-06 ★原子 ≠ durable★
  `patient_locator._atomic_write_rows()` 只有 `os.replace`,沒有 fsync ——
  而它是「同一天第二位 mismatch」唯一查得出是哪位病人的線索。
"""
import ast
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import retention as R  # noqa: E402
from cmuh_common.retention import (  # noqa: E402
    RetentionRule, start_background_sweeper, sweep,
)

SRC = os.path.join(os.path.dirname(__file__), "..", "src")


def _rule(d, days=3.0, label="測試"):
    return RetentionRule(label=label, directory=str(d), patterns=("*.png",),
                         retain_days=days)


def _boom_stat(monkeypatch, name: str):
    """讓★這個檔★的 stat 失敗(生產的失敗形狀:ACL / 防毒 / 暫時性 IO)。"""
    real = os.stat

    def _spy(path, *a, **k):
        if str(path).endswith(name):
            raise OSError("模擬:ACL/防毒讓 stat 失敗")
        return real(path, *a, **k)
    monkeypatch.setattr(os, "stat", _spy)


def _aged(d, name, days_old):
    p = d / name
    p.write_bytes(b"x")
    t = time.time() - days_old * 86400.0
    os.utime(p, (t, t))
    return p


# ══ R2-P2-03:讀不到年齡不可以靜默跳過 ═══════════════════════════════════
class TestAnUnreadableAgeIsNotSilence:
    def test_it_is_reported_instead_of_skipped(self, tmp_path, monkeypatch):
        """★核心★:狀態讀不到 → 摘要要說出來,而且不算 clean。

        ★量生產的呼叫形狀★(外審 R1-2):第一版我 patch 的是
        `os.path.getmtime` —— 但生產路徑上★先跑的是 `os.path.isfile()`★,
        它自己也做一次 stat 並把 OSError 吞成 False,於是真實的 ACL/防毒情境
        會在到達被測程式碼【之前】就被靜默丟掉。現在整條路徑只做一次
        `os.stat`,測試也就 patch 它。
        """
        _aged(tmp_path, "a.png", 99)
        _boom_stat(monkeypatch, "a.png")
        res = sweep([_rule(tmp_path)])
        assert res.stat_failed == {"測試": 1}, res
        assert not res.clean, res
        assert "年齡讀不到" in res.summary(), res.summary()

    def test_the_file_is_still_on_disk(self, tmp_path, monkeypatch):
        """★這才是重點★:報告要 degraded,是因為那個含 PHI 的檔還在磁碟上。"""
        _aged(tmp_path, "a.png", 99)
        _boom_stat(monkeypatch, "a.png")
        sweep([_rule(tmp_path)])
        # ★用 listdir 而不是 p.exists()★:後者也會 stat,會撞上我們自己的樁。
        assert "a.png" in os.listdir(tmp_path), "★前提:它確實留在磁碟上★"

    def test_no_silent_prefilter_swallows_the_failure(self, tmp_path,
                                                      monkeypatch):
        """★外審 R1-2 的反例★:stat 一開始就失敗(而不是只有取 mtime 那一步),
        整個檔仍然必須被算進 `stat_failed` —— 不可以在列舉階段就被
        `os.path.isfile()` 之類的「順手判斷」吞掉。"""
        _aged(tmp_path, "a.png", 99)
        calls = []
        real = os.stat

        def _spy(path, *a, **k):
            calls.append(str(path))
            if str(path).endswith("a.png"):
                raise OSError("模擬:連 stat 都失敗")
            return real(path, *a, **k)
        monkeypatch.setattr(os, "stat", _spy)
        monkeypatch.setattr(os.path, "isfile",
                            lambda _p: (_ for _ in ()).throw(
                                AssertionError("★不可以再有靜默的預先過濾★")))
        res = sweep([_rule(tmp_path)])
        assert res.stat_failed == {"測試": 1}, (res, calls)
        assert not res.clean, res

    def test_a_directory_matching_the_pattern_is_not_a_failure(self, tmp_path):
        """★不可矯枉過正★:樣式撞到目錄時,那不是「讀不到狀態」——
        stat 成功、只是不是普通檔,要靜靜略過(否則永遠 degraded)。

        ★目錄要是【過期】的才分得出勝負★:新的目錄根本走不到刪除那一步,
        少了 S_ISREG 守衛也不會有任何差別 —— 我第一版就是這樣,突變假綠燈。
        過期的話,沒有守衛就會對目錄呼叫 `os.remove` → OSError → 記成刪不掉。
        """
        d = tmp_path / "sub.png"
        d.mkdir()
        t = time.time() - 99 * 86400.0
        os.utime(d, (t, t))
        res = sweep([_rule(tmp_path)])
        assert res.clean and res.stat_failed == {} and res.failed == {}, res

    def test_a_healthy_sweep_is_clean(self, tmp_path):
        """★對照組★:一切正常 → clean,摘要不得出現那句話(不可誤報)。"""
        _aged(tmp_path, "old.png", 99)
        _aged(tmp_path, "new.png", 0.1)
        res = sweep([_rule(tmp_path)])
        assert res.clean and res.stat_failed == {}, res
        assert "年齡讀不到" not in res.summary(), res.summary()
        assert res.deleted == {"測試": 1}, res

    def test_a_delete_failure_still_counts_separately(self, tmp_path,
                                                      monkeypatch):
        """★兩種失敗要分得開★:刪不掉 vs 年齡讀不到 —— 處置不同
        (前者是鎖住/權限,後者連「該不該刪」都還不知道)。"""
        _aged(tmp_path, "a.png", 99)
        monkeypatch.setattr(
            os, "remove",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("鎖住")))
        res = sweep([_rule(tmp_path)])
        assert res.failed == {"測試": 1} and res.stat_failed == {}, res


# ══ R2-P2-02:產生者自己跑,不依賴事件發生 ═══════════════════════════════
class TestTheProducerSweepsOnItsOwn:
    def test_it_sweeps_immediately_at_startup(self, tmp_path):
        """★啟動就掃一次★:不必等到下一次產生資料,也不必等主程式開機。"""
        old = _aged(tmp_path, "old.png", 99)
        done = []
        t = start_background_sweeper([_rule(tmp_path)],
                                     _sleep=lambda _s: done.append(1) or
                                     time.sleep(3600))
        for _ in range(200):                    # 等第一輪掃完
            if done:
                break
            time.sleep(0.01)
        assert done, "★啟動後沒有立刻掃★"
        assert not old.exists(), "★過期檔沒有被清掉★"
        assert t is not None and t.daemon, "要是 daemon 緒(不可擋住關程式)"

    def test_it_keeps_sweeping_without_any_new_event(self, tmp_path):
        """★核心★:第二輪清掃不需要任何新資料產生 ——
        原本的形狀是「只有存新截圖時才清」,事件一停就再也不跑。"""
        rounds = []

        def _sleep(_s):
            rounds.append(1)
            if len(rounds) >= 2:
                time.sleep(3600)                # 量到兩輪就停住

        _aged(tmp_path, "a.png", 99)
        start_background_sweeper([_rule(tmp_path)], _sleep=_sleep)
        for _ in range(300):
            if len(rounds) >= 2:
                break
            time.sleep(0.01)
        assert len(rounds) >= 2, f"★只跑了 {len(rounds)} 輪(沒有週期性)★"

    def test_a_broken_sweep_never_escapes(self, tmp_path, monkeypatch):
        """★絕不影響臨床流程★:清掃自己炸了也不可以把例外丟出來。"""
        monkeypatch.setattr(
            R, "sweep",
            lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("boom")))
        done = []
        start_background_sweeper([_rule(tmp_path)],
                                 _sleep=lambda _s: done.append(1) or
                                 time.sleep(3600))
        for _ in range(200):
            if done:
                break
            time.sleep(0.01)
        assert done, "★例外逃出來把清掃緒殺掉了★"


# ══ 佈線:沒有呼叫端的函式等於不存在 ═════════════════════════════════════
def _calls_in(path: str, func: str) -> bool:
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read())
    return any(isinstance(n, ast.Call)
               and ((isinstance(n.func, ast.Name) and n.func.id == func)
                    or (isinstance(n.func, ast.Attribute) and n.func.attr == func))
               for n in ast.walk(tree))


class TestBothProducersAreWiredUp:
    def test_autoclock_starts_its_own_sweeper(self):
        assert _calls_in(os.path.join(SRC, "autoclock.py"),
                         "start_background_sweeper"), \
            "★打卡沒有自己執行保留期★(除錯檔含帳號與完整畫面)"

    def test_consult_query_starts_its_own_sweeper(self):
        assert _calls_in(os.path.join(SRC, "consult_query.py"),
                         "start_background_sweeper"), \
            "★會診查詢沒有自己執行保留期★(截圖含整份病人清單)"

    def test_the_main_program_keeps_the_global_sweep(self):
        """主程式的全域清掃★保留為冗餘★ —— 不可以因為產生者自己會掃就拿掉
        (它掃的還有設定備份、restart_err 等別人不管的東西)。

        ★判準要照生產的形狀★:主程式是把函式【當參考】交給排程器
        (`("retention-sweep-startup", run_retention_sweep)`),不是直接呼叫 ——
        我第一版只找 Call,於是這條測試紅在一個沒有缺陷的地方。
        """
        with open(os.path.join(SRC, "main.py"), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        assert any(isinstance(n, ast.Name) and n.id == "run_retention_sweep"
                   and isinstance(n.ctx, ast.Load) for n in ast.walk(tree)),             "★主程式的全域清掃被拿掉了★"


# ══ R2-P2-06:原子 ≠ durable ═════════════════════════════════════════════
def test_the_locator_index_is_fsynced_before_the_rename(tmp_path,
                                                        monkeypatch):
    """★回傳成功 = 已落盤★:`os.replace` 只保證檔名切換的原子性。
    量的是「rename 之前有沒有對【那個 fd】fsync」。"""
    from cmuh_common import patient_locator as pl
    order = []
    real_fsync, real_replace = os.fsync, os.replace
    monkeypatch.setattr(os, "fsync",
                        lambda fd: (order.append("fsync"), real_fsync(fd))[1])
    monkeypatch.setattr(os, "replace",
                        lambda a, b: (order.append("replace"),
                                      real_replace(a, b))[1])
    pl._atomic_write_rows(str(tmp_path / "idx.jsonl"), [{"a": 1}])
    assert order == ["fsync", "replace"], order
