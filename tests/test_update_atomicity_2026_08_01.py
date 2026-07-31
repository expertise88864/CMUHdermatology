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
    """
    app = tmp_path / "app"
    app.mkdir()
    targets = []
    entries = []
    for i in range(n_files):
        p = app / f"mod{i}.py"
        p.write_text(f"OLD-{i}", encoding="utf-8")
        targets.append(str(p))
        entries.append({"target": str(p), "existed_before": True})
    if new_file:
        p = app / "brand_new.py"      # 這一批才第一次出現的檔
        targets.append(str(p))
        entries.append({"target": str(p), "existed_before": False})

    for i in range(replaced):
        p = app / f"mod{i}.py"
        (app / f"mod{i}.py.bak").write_text(f"OLD-{i}", encoding="utf-8")
        p.write_text(f"NEW-{i}", encoding="utf-8")
    if new_file and replaced > 0:
        (app / "brand_new.py").write_text("NEW", encoding="utf-8")

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
    assert len(restored) == 3


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
