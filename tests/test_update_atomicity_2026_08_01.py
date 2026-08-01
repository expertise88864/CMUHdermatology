# -*- coding: utf-8 -*-
"""[2026-08-01 第二輪外審 P1-08] 更新崩潰的原子性 —— 交易日誌 + 啟動復原。

★既有的兩階段寫入擋不住「行程在 Phase 2 中途死掉」★
`_commit_pending_writes` 已經做到「先把全部新內容寫成 .upd.tmp（含 fsync），確定都
寫得出來才開始 os.replace」—— 那擋掉了最常見的失敗（磁碟滿、防毒鎖檔）。
但 Phase 2 本身是【逐檔】replace 的。行程在中途被砍（watchdog 重啟、關機、斷電、
更新完自我重啟）時，磁碟上就是「一部分新、一部分舊」：

  * `version.py` 已經是新版，它 import 的模組還是舊的 → 下次啟動 ImportError；
  * 而且 SHA/版本比對會認為「已經是新版」→ **不再重抓** → 程式 brick，要人去現場。

process 內的 rollback 完全幫不上忙 —— 那個 process 已經不在了。

這一檔測的就是「行程消失」這個 in-process 程式碼永遠處理不到的情境：
直接偽造出崩潰後的磁碟狀態（日誌在、.bak 在、檔案已被換掉一部分），
再問復原程序有沒有把它救回一個【完整的舊版本】。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import updater  # noqa: E402


def _crashed_state(tmp_path, n_files=3, replaced=2, new_file=False):
    """偽造「Phase 2 做到一半就斷電」的磁碟狀態。

    → (app_dir, [目標檔路徑…])
    前 `replaced` 個檔已經被換成新內容、且留有 .bak；其餘還是舊的。

    ★[2026-08-01] 還沒被替換的檔要留著它的 `.upd.tmp`★
    真正的 Phase 2 是「先把整批寫成 .upd.tmp，再逐檔 backup→replace」，而
    `os.replace(tmp, target)` 會把 tmp 消耗掉 —— 所以崩潰當下，還沒輪到的檔
    【一定】還留著自己的 .upd.tmp。原本這個 fixture 沒有做出這件事，偽造出一個
    真實流程不會產生的磁碟狀態，於是「還沒輪到」與「換過了但備份被刪」在測試裡
    長得一模一樣，兩者相反的處置也就無從分辨。
    """
    app = tmp_path / "app"
    app.mkdir()
    targets = []
    entries = []
    for i in range(n_files):
        p = app / f"mod{i}.py"
        p.write_text(f"OLD-{i}", encoding="utf-8")
        targets.append(str(p))
        entries.append({"target": str(p), "existed_before": True,
                        "staged": str(app / f".mod{i}.py.upd.tmp")})
    if new_file:
        p = app / "brand_new.py"      # 這一批才第一次出現的檔
        targets.append(str(p))
        entries.append({"target": str(p), "existed_before": False,
                        "staged": str(app / ".brand_new.py.upd.tmp")})

    # 整批都先 staged 過（Phase 1 的產物）
    for entry in entries:
        with open(entry["staged"], "w", encoding="utf-8") as f:
            f.write("NEW")

    for i in range(replaced):
        p = app / f"mod{i}.py"
        (app / f"mod{i}.py.bak").write_text(f"OLD-{i}", encoding="utf-8")
        p.write_text(f"NEW-{i}", encoding="utf-8")
        os.remove(app / f".mod{i}.py.upd.tmp")     # replace 把 tmp 吃掉了
    if new_file and replaced > 0:
        (app / "brand_new.py").write_text("NEW", encoding="utf-8")
        os.remove(app / ".brand_new.py.upd.tmp")

    (app / updater.JOURNAL_FILENAME).write_text(
        json.dumps({"schema": 1, "started": "2026-08-01T09:00:00",
                    "pid": 1234, "files": entries}, ensure_ascii=False),
        encoding="utf-8")
    return str(app), targets


# ─── ★核心★ 崩潰後回到一個完整的舊版本 ────────────────────────────────────
def test_a_half_written_update_is_rolled_back_on_startup(tmp_path):
    """★這是 P1-08 的整個重點★

    崩潰後磁碟是「mod0/mod1 新、mod2 舊」—— 那是一個**不存在過的版本組合**。
    復原之後三個檔必須全部回到舊版（完整、可啟動）。
    """
    app, targets = _crashed_state(tmp_path, n_files=3, replaced=2)
    assert [open(t, encoding="utf-8").read() for t in targets] == \
        ["NEW-0", "NEW-1", "OLD-2"], "測試前提：磁碟真的是半套狀態"

    restored = updater.recover_incomplete_update(app)

    assert [open(t, encoding="utf-8").read() for t in targets] == \
        ["OLD-0", "OLD-1", "OLD-2"], "必須整批回到更新前的版本"
    # ★措辭鐵律★ 只算【真的動過】的：mod2 崩潰時根本還沒被替換，把它算成
    #   「已回滾」是在誇大程式做過的事（日誌有 3 個檔，實際還原 2 個）。
    assert set(restored) == set(targets[:2])    # 回滾是反序走的，比集合


def test_a_file_created_by_the_batch_is_removed_on_rollback(tmp_path):
    """這一批才新建的檔沒有「舊版」可以還原 —— 要刪掉，不能留一個孤兒模組。"""
    app, _t = _crashed_state(tmp_path, n_files=2, replaced=2, new_file=True)
    new_file = os.path.join(app, "brand_new.py")
    assert os.path.exists(new_file)
    updater.recover_incomplete_update(app)
    assert not os.path.exists(new_file)


def test_the_journal_is_cleared_after_recovery(tmp_path):
    """★日誌一定要清掉★ 留著會讓每次啟動都重跑一次回滾，而第二次的 .bak 已經被
    第一次消耗掉 → 每次啟動噴一批 error，看起來像壞掉。"""
    app, _t = _crashed_state(tmp_path)
    updater.recover_incomplete_update(app)
    assert not os.path.exists(os.path.join(app, updater.JOURNAL_FILENAME))
    assert updater.recover_incomplete_update(app) == [], "第二次應該無事可做"


def test_no_journal_means_nothing_to_do(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    assert updater.recover_incomplete_update(str(app)) == []


def test_recovery_never_raises_even_with_a_corrupt_journal(tmp_path):
    """★復原程序不可以擋住程式啟動★ 日誌壞掉時只記 error。"""
    app = tmp_path / "app"
    app.mkdir()
    (app / updater.JOURNAL_FILENAME).write_text("{ 不是 JSON", encoding="utf-8")
    assert updater.recover_incomplete_update(str(app)) == []


def test_recovery_reports_but_survives_a_missing_backup(tmp_path, caplog):
    """.bak 不見了（被防毒清掉、被人手動刪）→ 那個檔救不回來，但其餘的要救，
    而且要留下 error —— 不可以安靜地留下半套狀態。"""
    import logging as _lg
    app, targets = _crashed_state(tmp_path, n_files=3, replaced=2)
    os.remove(os.path.join(app, "mod0.py.bak"))
    with caplog.at_level(_lg.ERROR):
        updater.recover_incomplete_update(app)
    assert open(targets[1], encoding="utf-8").read() == "OLD-1", "其餘的仍要救回"
    assert any("回滾" in r.getMessage() for r in caplog.records)


# ─── 日誌的寫入時機 ────────────────────────────────────────────────────────
def test_the_journal_is_written_before_the_first_replace():
    """★順序決定它有沒有用★ 日誌若在第一個 os.replace 之後才寫，
    正好在那個視窗崩潰就沒有任何紀錄 —— 等於沒做。"""
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(
        inspect.getsource(updater._commit_pending_writes)))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body.pop(0)                      # 去 docstring，避免自我命中
    code = ast.unparse(tree)
    assert code.index("_write_commit_journal") < code.index(
        "_replace_file_with_retry"), "日誌必須在第一個 replace 之前"


def test_the_journal_is_cleared_before_the_backups_are_deleted():
    """★順序不可對調★ 先刪 .bak 再刪日誌的話，中間崩潰會留下
    「日誌說要回滾、但備份已經沒了」—— 比沒有日誌更糟。"""
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(
        inspect.getsource(updater._commit_pending_writes)))
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body.pop(0)
    code = ast.unparse(tree)
    # `ast.unparse` 會把字串常數印成單引號 → 用 bak_path 這個變數名定位比較穩
    assert code.index("_clear_commit_journal") < code.rindex("bak_path"), \
        "清日誌必須在刪 .bak 之前"


def test_a_batch_is_abandoned_when_the_journal_cannot_be_written(tmp_path,
                                                                 monkeypatch):
    """★寫不出日誌就不要開始★ 沒有日誌的中途崩潰是不可復原的 ——
    寧可「晚一點更新」，那比 brick 一台診間電腦輕得多。"""
    app = tmp_path / "app"
    app.mkdir()
    target = app / "mod.py"
    target.write_text("OLD", encoding="utf-8")
    monkeypatch.setattr(updater, "get_app_dir", lambda: str(app))
    monkeypatch.setattr(updater, "_write_commit_journal",
                        lambda _d, _e: False)
    result = updater.UpdateResult()
    out = updater._commit_pending_writes(
        [("k", "mod.py", "1.0", "NEW", str(target))], result)
    assert out.errors and any("交易日誌" in e for e in out.errors)
    assert target.read_text(encoding="utf-8") == "OLD", "正式檔一個都不可以被動過"
    assert not list(app.glob("*.upd.tmp")), "暫存檔要清乾淨"


def test_a_successful_commit_leaves_no_journal_behind(tmp_path, monkeypatch):
    """整批成功之後不可以留下日誌 —— 否則下次啟動會把好好的更新回滾掉。"""
    app = tmp_path / "app"
    app.mkdir()
    target = app / "mod.py"
    target.write_text("OLD", encoding="utf-8")
    monkeypatch.setattr(updater, "get_app_dir", lambda: str(app))
    monkeypatch.setattr(updater, "_precompile_files", lambda _p: None)
    result = updater.UpdateResult()
    out = updater._commit_pending_writes(
        [("k", "mod.py", "1.0", "NEW", str(target))], result)
    assert not out.errors, out.errors
    assert target.read_text(encoding="utf-8") == "NEW"
    assert not os.path.exists(os.path.join(str(app), updater.JOURNAL_FILENAME))
    assert not os.path.exists(str(target) + ".bak"), "commit 後 .bak 要清掉"


def test_an_in_process_failure_also_clears_the_journal(tmp_path, monkeypatch):
    """process 內已經回滾完了 → 日誌沒有存在的理由。留著的話下次啟動會再回滾一次，
    而那時 .bak 已經被這次的回滾消耗掉 → 每次啟動噴一批 error。"""
    app = tmp_path / "app"
    app.mkdir()
    ok_file = app / "a.py"
    ok_file.write_text("OLD-A", encoding="utf-8")
    bad_file = app / "b.py"
    bad_file.write_text("OLD-B", encoding="utf-8")
    monkeypatch.setattr(updater, "get_app_dir", lambda: str(app))

    real_replace = updater._replace_file_with_retry
    calls = {"n": 0}

    def _flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:                 # 第二個檔 replace 失敗
            raise OSError("防毒鎖住了")
        return real_replace(src, dst)
    monkeypatch.setattr(updater, "_replace_file_with_retry", _flaky)

    result = updater.UpdateResult()
    out = updater._commit_pending_writes(
        [("k1", "a.py", "1.0", "NEW-A", str(ok_file)),
         ("k2", "b.py", "1.0", "NEW-B", str(bad_file))], result)
    assert out.errors
    assert ok_file.read_text(encoding="utf-8") == "OLD-A", "已寫的要回滾"
    assert not os.path.exists(os.path.join(str(app), updater.JOURNAL_FILENAME))


# ─── ★端到端：真的把行程砍掉，再讓下一次啟動救回來★ ──────────────────────
def test_a_killed_process_leaves_a_usable_journal_and_recovery_works(
        tmp_path, monkeypatch):
    """★這一支才是這整檔的驗收★

    上面那些測試都是【手工偽造】崩潰後的磁碟狀態，所以就算 `_write_commit_journal`
    根本沒寫東西，它們照樣全綠 —— 實測過：把它改成 no-op，15 支全過。
    那正是「檢查器會跳到綠燈」的形狀：測了復原，卻沒測「崩潰時真的留得下線索」。

    這裡改成走【真的 commit 路徑】，在 Phase 2 中途丟一個 `BaseException`
    （`except Exception` 接不到 → 就地離開，不會跑到 in-process 回滾）——
    那就是「行程被 watchdog 砍掉／斷電」在程式碼裡的樣子。
    然後才問：磁碟上留下的東西夠不夠讓下一次啟動救回來。
    """
    app = tmp_path / "app"
    app.mkdir()
    a, b = app / "a.py", app / "b.py"
    a.write_text("OLD-A", encoding="utf-8")
    b.write_text("OLD-B", encoding="utf-8")
    monkeypatch.setattr(updater, "get_app_dir", lambda: str(app))

    real_replace = updater._replace_file_with_retry
    calls = {"n": 0}

    def _die(src, dst):
        # ★只數「換到正式檔」那幾次★ 備份現在也是用 replace 原子換名的
        #   （.bak.tmp → .bak），不排除的話會在【還沒換掉 a】的時候就死，
        #   測的就不是「a 已新、b 還舊」那個半套狀態了。
        if str(dst).endswith(".bak"):
            return real_replace(src, dst)
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt("行程被砍")      # BaseException：不被接住
        return real_replace(src, dst)
    monkeypatch.setattr(updater, "_replace_file_with_retry", _die)

    with pytest.raises(KeyboardInterrupt):
        updater._commit_pending_writes(
            [("k1", "a.py", "1.0", "NEW-A", str(a)),
             ("k2", "b.py", "1.0", "NEW-B", str(b))], updater.UpdateResult())

    # 崩潰後的磁碟：a 已經換成新的、b 還是舊的 —— 一個不存在過的版本組合
    assert a.read_text(encoding="utf-8") == "NEW-A"
    assert b.read_text(encoding="utf-8") == "OLD-B"

    journal = app / updater.JOURNAL_FILENAME
    assert journal.exists(), \
        "★沒有日誌就救不回來★ 崩潰時磁碟上必須留下「這批動過哪些檔」"
    payload = json.loads(journal.read_text(encoding="utf-8"))
    assert [f["target"] for f in payload["files"]] == [str(a), str(b)]

    # 下一次啟動
    updater.recover_incomplete_update(str(app))
    assert a.read_text(encoding="utf-8") == "OLD-A"
    assert b.read_text(encoding="utf-8") == "OLD-B"
    assert not journal.exists()


# ─── 接線 ──────────────────────────────────────────────────────────────────
def test_check_and_update_recovers_before_it_compares_versions():
    """★順序★ 若先比版本再復原，半套狀態下 version.py 已經是新的 →
    可能判定「不需要更新」→ 半套就這樣留著。"""
    import inspect
    src = inspect.getsource(updater.check_and_update)
    assert "recover_incomplete_update()" in src
    assert src.index("recover_incomplete_update()") < src.index("_fetch_manifest")


def test_the_watchdog_also_recovers_on_startup():
    """★涵蓋面最大的復原點★ watchdog 是獨立行程：被更新弄壞的那支程式啟動不了時，
    它自己的復原路徑跑不到，而 watchdog 還活著。"""
    import io as _io
    path = os.path.join(os.path.dirname(__file__), "..", "src",
                        "watchdog_runner.py")
    src = _io.open(path, encoding="utf-8").read()
    assert "recover_incomplete_update" in src


@pytest.mark.parametrize("name", ["JOURNAL_FILENAME", "JOURNAL_SCHEMA"])
def test_the_journal_constants_are_public(name):
    """常數要是公開的：復原、測試、日後的排查工具都要指得到同一個檔名。"""
    assert hasattr(updater, name)


# ══════════════════════════════════════════════════════════════════════════
# [2026-08-01 外審] 五個 CONFIRMED P1 的回歸測試
# ══════════════════════════════════════════════════════════════════════════
def test_recovery_defers_while_another_process_is_writing(tmp_path,
                                                          monkeypatch, caplog):
    """★P1-1：復原會跟正在進行的 commit 撞在一起，撞出混版本★

    開機時 watchdog 幾乎同時拉起五支程式。A 正在 Phase 2 寫到一半(日誌已落地)，
    B 啟動 → 看到日誌 → 以為「上次崩潰了」→ 回滾 A 剛換好的檔、又把日誌清掉；
    A 接著把剩下的換完 → 「第一個舊、其餘新」的混版本，而且再也沒有日誌能修它。
    這正是 `_updater_write_lock` 當初要防的事，而復原當時沒有拿那把鎖。

    拿不到鎖時必須【什麼都不做】——「有人正在寫」不等於「上次崩潰了」。
    """
    import contextlib
    import logging as _lg

    app, targets = _crashed_state(tmp_path, n_files=3, replaced=2)

    @contextlib.contextmanager
    def _busy(timeout_sec=30.0):
        yield False                      # 模擬「另一支程式正在寫」
    monkeypatch.setattr(updater, "_updater_write_lock", _busy)

    with caplog.at_level(_lg.INFO):
        restored = updater.recover_incomplete_update(app)

    assert restored == [], "拿不到鎖就不可以動任何檔"
    assert [open(t, encoding="utf-8").read() for t in targets] == \
        ["NEW-0", "NEW-1", "OLD-2"], "★磁碟必須原封不動★ 不可以去回滾別人正在寫的批次"
    assert os.path.exists(os.path.join(app, updater.JOURNAL_FILENAME)), \
        "★更不可以把別人的交易日誌清掉★ 清掉就沒有人能修那個混版本了"


def test_recovery_takes_the_same_lock_the_writer_takes():
    """釘住「用的是同一把鎖」—— 換成別的鎖等於沒鎖。"""
    import ast
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(
        inspect.getsource(updater.recover_incomplete_update)))
    names = {n.func.id for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_updater_write_lock" in names, \
        "復原必須拿【和寫入同一把】跨行程鎖"


def test_a_torn_backup_cannot_overwrite_an_intact_file(tmp_path, monkeypatch):
    """★P1-2：復原程序自己製造損毀★

    備份原本是 `shutil.copy2(target, target + ".bak")` —— 直接往【權威名字】寫。
    日誌是在動第一個正式檔之前就落地的，所以復原看到 .bak 存在就當它可信。
    在 copy 中途斷電的話:正式檔還沒被動過、是完好的舊版，.bak 卻是半截的 ——
    下次啟動就拿半截檔覆蓋掉完好的正式檔。

    現在先寫 .bak.tmp、fsync、再原子換名，所以 .bak 只會是「完整舊版」或「不存在」。
    這支模擬「複製到一半就死」，然後驗證權威的 .bak 沒有被寫出半截。
    """
    target = tmp_path / "mod.py"
    target.write_text("COMPLETE-OLD-CONTENT", encoding="utf-8")

    real_copy = updater._copy_file_with_retry

    def _die_midway(src, dst):
        with open(dst, "w", encoding="utf-8") as f:
            f.write("TRUNC")             # 只寫了一部分
        raise KeyboardInterrupt("斷電")
    monkeypatch.setattr(updater, "_copy_file_with_retry", _die_midway)

    with pytest.raises(KeyboardInterrupt):
        updater._make_backup_atomically(str(target))

    bak = tmp_path / "mod.py.bak"
    assert not bak.exists(), \
        "★權威的 .bak 不可以出現半截內容★ 半截檔會在下次啟動被當成可信的還原來源"
    monkeypatch.setattr(updater, "_copy_file_with_retry", real_copy)
    updater._make_backup_atomically(str(target))
    assert bak.read_text(encoding="utf-8") == "COMPLETE-OLD-CONTENT"


def test_backup_is_published_by_rename_not_written_in_place():
    """釘住做法本身:不可以再直接 copy 到 `.bak`。"""
    import ast
    import inspect
    import textwrap
    src = ast.unparse(ast.parse(textwrap.dedent(
        inspect.getsource(updater._make_backup_atomically))))
    assert ".bak.tmp" in src and "_replace_file_with_retry" in src
    assert "fsync" in src, "沒 fsync 的話斷電後 .bak 可能根本沒落到碟上"


def test_once_mode_also_recovers(monkeypatch):
    """★P1-3：排程真正在跑的是 --once，而它從來不做復原★

    schtasks 每 2 分鐘跑 `watchdog_runner.py --once`；daemon 是選用的。
    復原原本只掛在 daemon 分支 —— 於是沒開 daemon 的機器(最需要靠排程自救的
    那些)根本走不到復原。而當時的測試只是 grep 原始碼裡有沒有那個字串,
    所以照樣綠。
    """
    import watchdog_runner
    called = []
    monkeypatch.setattr(watchdog_runner, "_recover_incomplete_update",
                        lambda: called.append("recover"))
    monkeypatch.setattr(watchdog_runner, "_setup_logging", lambda *a, **k: None)

    class _Core:
        @staticmethod
        def run_one_tick(mode="outer"):
            called.append("tick")
            return []
    # ★兩個都要換★ `from cmuh_common import watchdog_core` 在該模組【已經被別的
    #   測試 import 過】時，取的是套件物件上的屬性，不是 sys.modules —— 只換
    #   sys.modules 的話單獨跑會過、全套跑會抓到真的模組（測試互相污染）。
    import cmuh_common
    monkeypatch.setitem(sys.modules, "cmuh_common.watchdog_core", _Core)
    monkeypatch.setattr(cmuh_common, "watchdog_core", _Core, raising=False)

    assert watchdog_runner._run_once_via_core() == 0
    assert called == ["recover", "tick"], \
        "★復原要在 import/執行 watchdog_core 之前★ 那個模組正是可能被換到一半的東西"


def test_an_unclearable_journal_rolls_the_batch_back(tmp_path, monkeypatch):
    """★P1-4：日誌清不掉時，整批要收回去 —— 不可以就這樣回去★

    兩層問題：
      1. `_clear_commit_journal` 原本吞掉例外又不回報成敗，呼叫端照樣往下刪 .bak
         → 日誌還在、備份只剩一半 → 下次啟動回滾成混版本。
      2. （外審第 2 輪）只是「留著備份 + 記 error 就 return」也不夠：那樣
         `has_update` 不會被設起來 → 呼叫端不會重啟 → 行程繼續跑【舊模組】，
         磁碟上卻是【整批新檔】，之後任何一個延遲 import 都會載到新版 → 同一個
         行程裡新舊混用。

    此刻鎖還在手上、備份也還完整,是收乾淨最好的時機:整批回滾 → 磁碟＝舊版,
    跟記憶體裡的舊模組一致,連重啟都不需要。
    """
    app = tmp_path / "app"
    app.mkdir()
    a, b = app / "a.py", app / "b.py"
    a.write_text("OLD-A", encoding="utf-8")
    b.write_text("OLD-B", encoding="utf-8")
    monkeypatch.setattr(updater, "get_app_dir", lambda: str(app))
    monkeypatch.setattr(updater, "_clear_commit_journal", lambda _d: False)

    result = updater.UpdateResult()
    out = updater._commit_pending_writes(
        [("k1", "a.py", "1.0", "NEW-A", str(a)),
         ("k2", "b.py", "1.0", "NEW-B", str(b))], result)

    assert a.read_text(encoding="utf-8") == "OLD-A", "★磁碟要回到舊版★"
    assert b.read_text(encoding="utf-8") == "OLD-B"
    assert not out.has_update, \
        "沒有成功 commit 就不可以宣告有更新（那會觸發一次沒有意義的重啟）"
    assert not out.updated_files
    assert any("交易日誌" in e for e in result.errors), "而且要說出來"


def test_a_failed_rollback_keeps_the_journal_for_a_retry(tmp_path, monkeypatch):
    """★P1-5：回滾自己也會失敗，而失敗時日誌被清掉＝唯一的修復機會沒了★

    典型情境是防毒暫時鎖住某個檔。磁碟仍是半新半舊，卻再也沒有標記讓下次啟動重試。
    """
    app = tmp_path / "app"
    app.mkdir()
    a, b = app / "a.py", app / "b.py"
    a.write_text("OLD-A", encoding="utf-8")
    b.write_text("OLD-B", encoding="utf-8")
    monkeypatch.setattr(updater, "get_app_dir", lambda: str(app))

    real_replace = updater._replace_file_with_retry

    def _flaky(src, dst):
        # 往前寫與回滾都是 replace 到同一個 dst，靠 src 分辨：
        #   前進 = 從 .upd.tmp 搬進去；回滾 = 從 .bak 搬回來。
        if dst == str(b) and not str(src).endswith(".bak"):
            raise OSError("b 寫入失敗 → 觸發回滾")
        if dst == str(a) and str(src).endswith(".bak"):
            raise PermissionError("防毒鎖住 a → 回滾也失敗")
        return real_replace(src, dst)     # a 的前進、.bak 的搬移都照常
    monkeypatch.setattr(updater, "_replace_file_with_retry", _flaky)

    result = updater.UpdateResult()
    updater._commit_pending_writes(
        [("k1", "a.py", "1.0", "NEW-A", str(a)),
         ("k2", "b.py", "1.0", "NEW-B", str(b))], result)

    assert os.path.exists(os.path.join(str(app), updater.JOURNAL_FILENAME)), \
        "★回滾沒成功就要留下日誌★ 清掉的話下次啟動不知道還有半套狀態要修"


def test_a_permanently_unfixable_file_does_not_loop_forever(tmp_path):
    """★反方向:不可以無限重試★

    「備份根本不存在」重試一萬次也不會長回來。若把它也留在日誌裡，每次啟動都會
    噴同一批 error —— 那正是當初無條件清日誌想避免的事。所以只有【可能會好】的
    失敗(鎖住/權限)才留著重試。
    """
    app, targets = _crashed_state(tmp_path, n_files=2, replaced=2)
    os.remove(os.path.join(app, "mod0.py.bak"))     # 換過了、備份卻沒了

    updater.recover_incomplete_update(app)
    assert not os.path.exists(os.path.join(app, updater.JOURNAL_FILENAME)), \
        "救不回來的檔不可以讓日誌永遠留著"
    assert updater.recover_incomplete_update(app) == [], "第二次無事可做"


def test_a_file_not_yet_reached_is_not_reported_as_rolled_back(tmp_path,
                                                               caplog):
    """★分辨「還沒輪到」與「備份被刪」★ 兩者都是「沒有 .bak」，處置卻相反。

    分辨依據是那個檔的 `.upd.tmp` 還在不在 —— `os.replace(tmp, target)` 會把它
    吃掉，所以 tmp 還在就代表還沒換過(它本來就是舊版,沒事)。
    """
    import logging as _lg
    app, targets = _crashed_state(tmp_path, n_files=3, replaced=1)
    with caplog.at_level(_lg.ERROR):
        restored = updater.recover_incomplete_update(app)
    assert set(restored) == {targets[0]}
    assert not [r for r in caplog.records if r.levelno >= _lg.ERROR], \
        "還沒輪到的檔不是錯誤，不可以每次啟動噴 error(那會讓人以為壞掉)"


def test_a_crash_while_backing_up_cannot_destroy_the_intact_file(tmp_path,
                                                                 monkeypatch):
    """★P1-2 的【端到端】版:走真正的 commit 路徑★

    上面那兩支是直接呼叫 `_make_backup_atomically` / 讀它的原始碼 —— 突變驗證時
    發現:把 `_commit_pending_writes` 裡的呼叫【換回舊的直接 copy】，那兩支照樣全綠。
    測了那個函式本身，卻沒測「正式流程真的有用它」，等於守衛沒蓋到生產路徑。

    這支從 `_commit_pending_writes` 進去，在【備份複製到一半】時斷電:
      * 正式檔還沒被動過 → 它是完好的舊版
      * 舊做法:半截內容直接落在權威的 `.bak` 上
      * 下次啟動的復原程序把那個半截檔蓋回正式檔 → **復原自己製造了損毀**
    """
    app = tmp_path / "app"
    app.mkdir()
    target = app / "mod.py"
    target.write_text("COMPLETE-OLD-CONTENT", encoding="utf-8")
    monkeypatch.setattr(updater, "get_app_dir", lambda: str(app))

    def _die_midway(src, dst):
        with open(dst, "w", encoding="utf-8") as f:
            f.write("TRUNC")             # 備份只寫了一部分
        raise KeyboardInterrupt("斷電")
    monkeypatch.setattr(updater, "_copy_file_with_retry", _die_midway)

    with pytest.raises(KeyboardInterrupt):
        updater._commit_pending_writes(
            [("k", "mod.py", "1.0", "NEW", str(target))],
            updater.UpdateResult())

    assert target.read_text(encoding="utf-8") == "COMPLETE-OLD-CONTENT", \
        "測試前提:正式檔在備份階段還沒被動過"

    # 下一次啟動的復原
    updater.recover_incomplete_update(str(app))
    assert target.read_text(encoding="utf-8") == "COMPLETE-OLD-CONTENT", \
        ("★復原不可以把完好的檔換成半截備份★ "
         "備份必須先寫 .bak.tmp 再原子換名，權威的 .bak 才不會出現半截內容")


# ─── ★[2026-08-01 外審第 2 輪] 三個 CONFIRMED P1★ ────────────────────────
def test_a_stale_backup_cannot_downgrade_an_untouched_file(tmp_path):
    """★復原把使用者【降版】了★

    上一批 commit 成功後清 .bak 失敗(那個清理是靜默吞錯的)→ 磁碟上留著一份
    【更舊的】陳舊備份。本批在「複製到 .bak.tmp」的中途死掉:正式檔根本還沒被動過,
    但復原看到 .bak 存在就拿它還原 → 把好好的檔換成上上個版本。

    暫存檔還在就是「還沒 replace」的鐵證(os.replace 會把它吃掉),這個證據必須
    【贏過】.bak 存不存在。
    """
    app = tmp_path / "app"
    app.mkdir()
    target = app / "mod.py"
    target.write_text("CURRENT", encoding="utf-8")
    (app / "mod.py.bak").write_text("ANCIENT", encoding="utf-8")   # 上一批的殘留
    staged = app / ".mod.py.upd.tmp"
    staged.write_text("NEW", encoding="utf-8")                     # 還沒 replace
    (app / updater.JOURNAL_FILENAME).write_text(
        json.dumps({"schema": 1, "started": "2026-08-01T09:00:00", "pid": 1,
                    "files": [{"target": str(target), "existed_before": True,
                               "staged": str(staged)}]}, ensure_ascii=False),
        encoding="utf-8")

    updater.recover_incomplete_update(str(app))
    assert target.read_text(encoding="utf-8") == "CURRENT", \
        "★不可以拿上一批的陳舊備份蓋掉沒被動過的檔★ 那是把使用者降版"


def test_a_failed_journal_rewrite_keeps_the_original_journal(tmp_path,
                                                             monkeypatch):
    """★宣稱與實作要相符★

    `_rewrite_journal_for_retry` 的 docstring 寫「寫不出來時保留原本的日誌」,
    但它用的 `_write_commit_journal` 原本是 `open(path, "w")` —— 先【截斷】既有
    日誌,失敗的處置又是把它【刪掉】。於是磁碟滿/IO 錯誤時,那份還有用的原日誌
    連同半套磁碟的唯一線索一起沒了,跟宣稱完全相反。
    """
    app = tmp_path / "app"
    app.mkdir()
    journal = app / updater.JOURNAL_FILENAME
    journal.write_text('{"schema":1,"files":[{"target":"x"}]}', encoding="utf-8")
    original = journal.read_text(encoding="utf-8")

    real_open = open

    def _no_space(path, mode="r", *a, **k):
        if str(path).endswith(".journal.tmp"):
            raise OSError(28, "磁碟空間不足")
        return real_open(path, mode, *a, **k)
    monkeypatch.setattr("builtins.open", _no_space)

    assert updater._write_commit_journal(str(app), [("y", True, "")]) is False
    monkeypatch.undo()
    assert journal.exists(), "★原本那份日誌不可以被毀掉★"
    assert journal.read_text(encoding="utf-8") == original, "而且要原封不動"
    assert not (app / (updater.JOURNAL_FILENAME + ".tmp")).exists(), \
        "半截的 tmp 要清掉"


def test_the_journal_is_published_by_rename():
    """釘住原子寫法本身 —— 直接 open(path,'w') 會截斷既有日誌。"""
    import ast
    import inspect
    import textwrap
    src = ast.unparse(ast.parse(textwrap.dedent(
        inspect.getsource(updater._write_commit_journal))))
    assert "os.replace" in src, "日誌要用原子換名發布"
    assert "fsync" in src
