# -*- coding: utf-8 -*-
"""批次 K2（外部 review P2-05）：交易日誌的每筆狀態機。

【問題】
回滾會把 `.bak` 用掉（`os.replace(bak, target)`）。若接著「清除／改寫日誌」
那一步失敗（權限、防毒），殘留的日誌下一次啟動會看到：
    existed_before=True、而 .bak 不見了 → 判成「救不回來」
那是★假的 terminal★，而且它會讓整組程式從此每次啟動都要人工覆寫。

【修法】狀態要被【記下來】，不可以從「.bak 還在不在」反推。
每還原一筆就把 `state` 改成 restored 並原子落地；下次啟動跳過它們。
"""
from __future__ import annotations

import json
import os

import pytest

import bootstrap_recovery as br
from cmuh_common import updater


def _seed(app_dir, entries):
    assert updater._write_commit_journal(str(app_dir), entries) is True
    return os.path.join(str(app_dir), br.JOURNAL_FILENAME)


def _read_journal(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


class TestTheJournalRecordsWhatWasDone:

    def test_a_fresh_journal_starts_as_pending(self, tmp_path):
        journal = _seed(tmp_path, [(str(tmp_path / "a.py"), True, "")])
        payload = _read_journal(journal)
        assert all(e["state"] == br.STATE_PENDING for e in payload["files"])

    def test_each_restore_is_recorded_immediately(self, tmp_path):
        """★每還原一筆就落地一次★ 中途斷電時下一次啟動才知道做到哪。"""
        a, b = tmp_path / "a.py", tmp_path / "b.py"
        for f in (a, b):
            f.write_text("新版", encoding="utf-8")
            (tmp_path / (f.name + ".bak")).write_text("舊版", encoding="utf-8")
        _seed(tmp_path, [(str(a), True, ""), (str(b), True, "")])

        seen = []
        real = br._atomic_write_json

        def _spy(path, payload):
            if str(path).endswith(br.JOURNAL_FILENAME):
                seen.append([e.get("state") for e in payload["files"]])
            return real(path, payload)

        br._atomic_write_json = _spy
        try:
            result = br.recover_before_start(str(tmp_path))
        finally:
            br._atomic_write_json = real

        assert result.status == br.RECOVERED
        assert len(seen) >= 2, "應該每還原一筆就寫一次狀態"
        assert seen[0].count(br.STATE_RESTORED) == 1, "第一次落地只該有一筆完成"


class TestAFailedCleanupDoesNotBecomeAFakeTerminal:

    def test_the_next_boot_skips_what_was_already_restored(self, tmp_path):
        """★這是 finding 的核心★

        第一輪還原成功但清不掉日誌 → 第二輪不可以把那幾筆判成
        「備份不見了 → 救不回來」。
        """
        target = tmp_path / "a.py"
        target.write_text("新版", encoding="utf-8")
        (tmp_path / "a.py.bak").write_text("舊版", encoding="utf-8")
        journal = _seed(tmp_path, [(str(target), True, "")])

        real_remove = os.remove
        os.remove = lambda p: (_ for _ in ()).throw(OSError("清不掉"))
        try:
            first = br.recover_before_start(str(tmp_path))
        finally:
            os.remove = real_remove

        assert first.status == br.RECOVERED
        assert target.read_text(encoding="utf-8") == "舊版"
        assert os.path.exists(journal), "前提：日誌沒被清掉"
        assert _read_journal(journal)["files"][0]["state"] == br.STATE_RESTORED

        second = br.recover_before_start(str(tmp_path))

        assert second.status != br.TERMINAL_FAILURE, (
            f"★假的 terminal★（{second.describe()}）")
        assert second.safe_to_start is True
        assert not os.path.exists(journal), "第二輪應該把日誌收乾淨"

    def test_the_second_round_does_not_claim_it_restored_anything(self,
                                                                  tmp_path):
        """★措辭鐵律★ 第二輪什麼都沒動，不可以說自己還原了檔案。"""
        target = tmp_path / "a.py"
        target.write_text("新版", encoding="utf-8")
        (tmp_path / "a.py.bak").write_text("舊版", encoding="utf-8")
        _seed(tmp_path, [(str(target), True, "")])

        real_remove = os.remove
        os.remove = lambda p: (_ for _ in ()).throw(OSError("清不掉"))
        try:
            br.recover_before_start(str(tmp_path))
        finally:
            os.remove = real_remove

        second = br.recover_before_start(str(tmp_path))
        assert second.restored == []

    def test_the_updater_also_skips_restored_entries(self, tmp_path):
        """兩條復原路徑都要跳過 —— 否則使用者選「仍要啟動」之後 updater 又
        會把它們判成救不回來。"""
        target = tmp_path / "a.py"
        target.write_text("舊版", encoding="utf-8")   # 已經還原過了，沒有 .bak
        journal = os.path.join(str(tmp_path), updater.JOURNAL_FILENAME)
        with open(journal, "w", encoding="utf-8") as f:
            json.dump({"schema": 1,
                       "files": [{"target": str(target), "existed_before": True,
                                  "staged": "", "state": "restored"}]}, f)

        updater.recover_incomplete_update(str(tmp_path))

        marker = journal + updater.FAILED_JOURNAL_SUFFIX
        assert not os.path.exists(marker), (
            "★已還原的項目被判成「備份不見了」★")


class TestTheBackupSurvivesUntilTheStateIsDurable:
    """[2026-08-03 外審 P2] 還原不可以在狀態落地之前就把備份用掉。"""

    def test_a_failed_state_write_leaves_the_backup_for_a_retry(self,
                                                                tmp_path,
                                                                monkeypatch):
        """★這是原問題的最後一個入口★

        `os.replace(bak, target)` 會消耗備份。若接著寫狀態與清日誌【都】失敗，
        磁碟上就是「pending 而且沒有備份」→ 下次啟動又是假的 terminal。
        """
        target = tmp_path / "a.py"
        target.write_text("新版", encoding="utf-8")
        backup = tmp_path / "a.py.bak"
        backup.write_text("舊版", encoding="utf-8")
        _seed(tmp_path, [(str(target), True, "")])

        # 狀態寫不進去（防毒鎖住 journal），清日誌也失敗
        monkeypatch.setattr(br, "_atomic_write_json", lambda *a, **k: False)
        monkeypatch.setattr(
            os, "remove",
            lambda p: (_ for _ in ()).throw(OSError("清不掉")))
        try:
            br.recover_before_start(str(tmp_path))
        finally:
            monkeypatch.undo()

        assert target.read_text(encoding="utf-8") == "舊版", "還原本身要成功"
        assert backup.exists(), (
            "★備份被用掉了 → 下次啟動會判成救不回來（假 terminal）★")

        # 下一輪：備份還在 → 照著再做一次，結果相同，不是 terminal
        second = br.recover_before_start(str(tmp_path))
        assert second.status != br.TERMINAL_FAILURE, (
            f"★假的 terminal★（{second.describe()}）")
        assert target.read_text(encoding="utf-8") == "舊版"

    def test_a_successful_state_write_drops_the_backup(self, tmp_path):
        """★空集合不算通過★ 狀態落地之後備份就該清掉，不要長期堆積。"""
        target = tmp_path / "a.py"
        target.write_text("新版", encoding="utf-8")
        backup = tmp_path / "a.py.bak"
        backup.write_text("舊版", encoding="utf-8")
        _seed(tmp_path, [(str(target), True, "")])

        result = br.recover_before_start(str(tmp_path))

        assert result.status == br.RECOVERED
        assert not backup.exists(), "狀態已落地，備份應該清掉"
        assert not (tmp_path / "a.py.restore.tmp").exists(), "暫存檔沒清乾淨"

    def test_the_updater_path_also_records_what_it_restored(self, tmp_path,
                                                            monkeypatch):
        """[2026-08-03 外審 P2] watchdog 直接走 updater 這條，原本只讀不寫 state。

        還原成功、但清日誌失敗 → 那一筆仍是 pending 且沒有備份 → 下一輪假 terminal。
        """
        target = tmp_path / "a.py"
        target.write_text("新版", encoding="utf-8")
        (tmp_path / "a.py.bak").write_text("舊版", encoding="utf-8")
        journal = _seed(tmp_path, [(str(target), True, "")])

        monkeypatch.setattr(updater, "_clear_commit_journal",
                            lambda app_dir: False)
        updater.recover_incomplete_update(str(tmp_path))
        monkeypatch.undo()

        assert os.path.exists(journal), "前提：日誌沒被清掉"
        entry = _read_journal(journal)["files"][0]
        assert entry["state"] == "restored", (
            "★updater 還原了卻沒有記下來 → 下一輪會判成救不回來★")


class TestTheUpdaterPathKeepsTheSameInvariant:
    """[2026-08-03 外審第 2 輪 P2] updater 有【自己的】回滾，也不可以吃掉備份。"""

    def test_the_updater_restore_does_not_consume_the_backup(self, tmp_path,
                                                             monkeypatch):
        """還原成功但清日誌失敗 → 備份必須還在，下一輪才救得回來。"""
        target = tmp_path / "a.py"
        target.write_text("新版", encoding="utf-8")
        backup = tmp_path / "a.py.bak"
        backup.write_text("舊版", encoding="utf-8")
        journal = _seed(tmp_path, [(str(target), True, "")])

        monkeypatch.setattr(updater, "_clear_commit_journal",
                            lambda app_dir: False)
        # 狀態也寫不進去 → 只剩「備份還在」這一條救命索
        monkeypatch.setattr(updater, "_mark_restored_in_journal",
                            lambda *a, **k: None)
        updater.recover_incomplete_update(str(tmp_path))
        monkeypatch.undo()

        assert target.read_text(encoding="utf-8") == "舊版", "還原本身要成功"
        assert backup.exists(), (
            "★備份被 os.replace 吃掉了 → 下一輪會判成救不回來★")
        assert os.path.exists(journal)
        assert not (tmp_path / "a.py.restore.tmp").exists(), "暫存檔沒清乾淨"

        # 下一輪：備份還在 → 再做一次，不是 terminal
        updater.recover_incomplete_update(str(tmp_path))
        assert not os.path.exists(
            journal + updater.FAILED_JOURNAL_SUFFIX), "★假的 terminal★"

    def test_the_backup_is_dropped_once_the_journal_is_cleared(self, tmp_path):
        """★空集合不算通過★ 交易收乾淨之後備份就該清掉，不要長期堆積。"""
        target = tmp_path / "a.py"
        target.write_text("新版", encoding="utf-8")
        backup = tmp_path / "a.py.bak"
        backup.write_text("舊版", encoding="utf-8")
        journal = _seed(tmp_path, [(str(target), True, "")])

        updater.recover_incomplete_update(str(tmp_path))

        assert not os.path.exists(journal), "日誌應該被清掉"
        assert not backup.exists(), "日誌清掉了，備份也該清"
        assert target.read_text(encoding="utf-8") == "舊版"


class TestStateIsNotInferredFromBackups:

    def test_a_pending_entry_without_a_backup_is_still_terminal(self,
                                                                tmp_path):
        """★空集合不算通過★ 真正沒還原過、又沒有備份的，仍然要判 terminal。"""
        target = tmp_path / "a.py"
        target.write_text("新版、沒有備份", encoding="utf-8")
        _seed(tmp_path, [(str(target), True, "")])

        result = br.recover_before_start(str(tmp_path))

        assert result.status == br.TERMINAL_FAILURE

    @pytest.mark.parametrize("state", ["done", "", "RESTORED", 1, None])
    def test_an_unrecognised_state_is_not_guessed(self, tmp_path, state):
        """不認得的狀態不要硬解（同 schema 版本的理由）。"""
        payload = {"schema": br.JOURNAL_SCHEMA,
                   "files": [{"target": "C:\\app\\a.py",
                              "existed_before": True, "state": state}]}
        files, why = br.parse_journal(payload)
        assert files is None, f"state={state!r} 應該判為看不懂"
        assert why

    def test_a_journal_without_state_still_works(self, tmp_path):
        """★向後相容★ 舊版寫的日誌沒有 state 欄位 —— 視為 pending。

        （這是不 bump schema 的理由：更新過程中留下的舊日誌仍要救得回來。）
        """
        target = tmp_path / "a.py"
        target.write_text("新版", encoding="utf-8")
        (tmp_path / "a.py.bak").write_text("舊版", encoding="utf-8")
        journal = os.path.join(str(tmp_path), br.JOURNAL_FILENAME)
        with open(journal, "w", encoding="utf-8") as f:
            json.dump({"schema": 1,
                       "files": [{"target": str(target),
                                  "existed_before": True, "staged": ""}]}, f)

        result = br.recover_before_start(str(tmp_path))

        assert result.status == br.RECOVERED
        assert target.read_text(encoding="utf-8") == "舊版"


class TestTheRestoreIsDurableNotJustAtomic:
    """[2026-08-03 外審第 3 輪 P2] 換名前要 fsync。

    `os.replace` 是原子的，但那只保證【名字】的原子性。暫存檔的內容還躺在
    OS 快取裡就換名、然後斷電的話，開機後看到的是「正式檔已經是新名字、
    內容卻是半截」——★而此時 journal 與 .bak 都已經被清掉★，連重試的機會
    都沒有。`_make_backup_atomically` 一直都有 fsync，這兩條還原路徑漏掉了。

    ★判準是【順序】不是【有沒有呼叫】★：先換名再 fsync 等於沒防到。

    ★而且要看它有沒有【成功】★（2026-08-03 外審第 4 輪）：第一版只記錄
    「fsync 被呼叫過」，而呼叫端把 OSError 吞掉了 —— 於是在 Windows 上
    `open(p, "rb")` + `os.fsync` 每一次都拋 EBADF、每一次都被吞、測試每一次
    都綠。★測到的是「有沒有呼叫」，不是「有沒有耐久性」★。
    """

    def _record_order(self, monkeypatch, replace_owner, replace_attr):
        order = []
        real_fsync = os.fsync
        real_replace = getattr(replace_owner, replace_attr)

        def _fsync(fd):
            real_fsync(fd)          # ★先真的做★ 失敗就讓它炸，不要記成成功
            order.append("fsync")

        def _replace(src, dst):
            order.append("replace")
            return real_replace(src, dst)

        monkeypatch.setattr(os, "fsync", _fsync)
        monkeypatch.setattr(replace_owner, replace_attr, _replace)
        return order

    def test_the_updater_restore_fsyncs_before_it_renames(self, tmp_path,
                                                          monkeypatch):
        target = tmp_path / "a.py"
        target.write_text("新版", encoding="utf-8")
        backup = tmp_path / "a.py.bak"
        backup.write_text("舊版", encoding="utf-8")

        order = self._record_order(monkeypatch, updater,
                                   "_replace_file_with_retry")
        updater._restore_keeping_backup(str(backup), str(target))
        monkeypatch.undo()

        assert order == ["fsync", "replace"], (
            f"還原沒有先把內容刷到碟上再換名：{order}")
        assert target.read_text(encoding="utf-8") == "舊版"

    def test_the_bootstrap_restore_fsyncs_before_it_renames(self, tmp_path,
                                                            monkeypatch):
        target = tmp_path / "a.py"
        target.write_text("新版", encoding="utf-8")
        backup = tmp_path / "a.py.bak"
        backup.write_text("舊版", encoding="utf-8")

        order = self._record_order(monkeypatch, os, "replace")
        errors = []
        outcome = br._rollback_one(
            str(tmp_path),
            {"target": str(target), "existed_before": True, "staged": ""},
            errors)
        monkeypatch.undo()

        assert outcome == "restored", f"還原本身就沒成功：{outcome} / {errors}"
        assert order == ["fsync", "replace"], (
            f"還原沒有先把內容刷到碟上再換名：{order}")
        assert target.read_text(encoding="utf-8") == "舊版"

    def test_a_read_only_handle_cannot_fsync_at_all_on_windows(self, tmp_path):
        """★這一條是上面那個假綠燈的根因，釘住它★

        不是「理論上比較好」——是 `open(p, "rb")` + `os.fsync` 在 Windows
        【一定失敗】。判準取自實機行為，不是推理。
        """
        p = tmp_path / "x.bin"
        p.write_bytes(b"A" * 64)
        if os.name != "nt":
            pytest.skip("只有 Windows 的 _commit() 會拒絕唯讀 fd")
        with pytest.raises(OSError), open(p, "rb") as f:
            os.fsync(f.fileno())
        with open(p, "rb+") as f:          # 可寫入的 handle 才做得到
            os.fsync(f.fileno())


class TestWhenFsyncFailsTheEntryIsNotFinished:
    """[2026-08-03 外審第 4 輪] fsync 失敗 → 換過去，但【不算收工】。

    外審建議「fsync 失敗就不要換」。不採納後半段：不換會把臨床程式留在半新
    半舊的壞版本上，而斷電風險一樣得靠「留著日誌與備份」來收 —— 所以照換，
    只是不報已還原（備份不清、日誌不清），下一輪原封不動再做一次。
    """

    def test_the_updater_replaces_but_keeps_the_backup(self, tmp_path,
                                                       monkeypatch):
        target = tmp_path / "a.py"
        target.write_text("新版", encoding="utf-8")
        backup = tmp_path / "a.py.bak"
        backup.write_text("舊版", encoding="utf-8")

        monkeypatch.setattr(updater, "_fsync_path", lambda p: False)
        durable = updater._restore_keeping_backup(str(backup), str(target))
        monkeypatch.undo()

        assert durable is False, "fsync 失敗卻回報收工了"
        assert target.read_text(encoding="utf-8") == "舊版", (
            "★不換並沒有比較安全★ 內容仍然要換回舊版")
        assert backup.exists(), "★留著備份才有下一輪★"
        assert not (tmp_path / "a.py.restore.tmp").exists()

    def test_the_updater_does_not_report_it_as_rolled_back(self, tmp_path,
                                                           monkeypatch):
        """不算收工 → 不進 restored（否則會被標成已還原、備份被清掉）。"""
        target = tmp_path / "a.py"
        target.write_text("新版", encoding="utf-8")
        backup = tmp_path / "a.py.bak"
        backup.write_text("舊版", encoding="utf-8")
        journal = _seed(tmp_path, [(str(target), True, "")])

        monkeypatch.setattr(updater, "_fsync_path", lambda p: False)
        monkeypatch.setattr(updater, "get_app_dir", lambda: str(tmp_path))
        restored = updater.recover_incomplete_update(str(tmp_path))
        monkeypatch.undo()

        assert restored == [], "fsync 沒成功不可以說「已回滾」"
        assert backup.exists(), "★備份被清掉＝下一輪救不回來★"
        assert os.path.exists(journal), "日誌要留著才會有下一輪"
        entry = _read_journal(journal)["files"][0]
        assert entry.get("state", br.STATE_PENDING) == br.STATE_PENDING, (
            f"還沒收工卻被標成已還原：{entry.get('state')!r}")

    def test_the_bootstrap_reports_retryable_not_restored(self, tmp_path,
                                                          monkeypatch):
        target = tmp_path / "a.py"
        target.write_text("新版", encoding="utf-8")
        backup = tmp_path / "a.py.bak"
        backup.write_text("舊版", encoding="utf-8")

        monkeypatch.setattr(br, "_fsync_path", lambda p, errors: False)
        errors = []
        outcome = br._rollback_one(
            str(tmp_path),
            {"target": str(target), "existed_before": True, "staged": ""},
            errors)
        monkeypatch.undo()

        assert outcome == "retryable", f"應該留給下一輪重做：{outcome}"
        assert target.read_text(encoding="utf-8") == "舊版", "內容仍要換回去"
        assert backup.exists(), "★留著備份才有下一輪★"

    def test_a_backup_that_cannot_be_fsynced_aborts_the_update(self, tmp_path,
                                                               monkeypatch):
        """★備份這一條相反：做不出可信的備份就不要更新★

        更新可以延後，備份卻是等一下出事時唯一的退路。拿一份不保證完整的
        `.bak` 去換掉正式檔，正是 `_make_backup_atomically` 要避免的事。
        """
        target = tmp_path / "a.py"
        target.write_text("目前版本", encoding="utf-8")

        monkeypatch.setattr(updater, "_fsync_path", lambda p: False)
        with pytest.raises(OSError):
            updater._make_backup_atomically(str(target))
        monkeypatch.undo()

        assert target.read_text(encoding="utf-8") == "目前版本", "正式檔沒被動"
        assert not (tmp_path / "a.py.bak").exists(), (
            "★不可以留下一份不保證完整的備份★ 它會被下一輪當成可信的還原來源")
        assert not (tmp_path / "a.py.bak.tmp").exists(), "暫存檔沒清乾淨"


def test_no_fsync_anywhere_in_src_uses_a_read_only_handle():
    """★守衛要涵蓋【這個性質涉及的所有檔】，不是我這次改到的兩個★

    這次的根因是「`open(p, "rb")` + `os.fsync` 在 Windows 是 no-op」。同樣的
    寫法在 src 底下任何地方都一樣壞，而且它不會噴錯 —— 只會安靜地沒有耐久性。
    所以用 AST 掃全部原始碼：凡是 `with open(...) as f:` 區塊裡出現
    `os.fsync(f.fileno())`，那個 open 的 mode 就必須可寫入。

    ★空集合不算通過★：掃不到任何 fsync 代表守衛失效（檔案搬家、寫法改變），
    那要當成失敗，不是「沒問題」。
    """
    import ast
    import pathlib

    src_root = pathlib.Path(__file__).resolve().parent.parent / "src"
    assert src_root.is_dir(), f"找不到 src（守衛失效）：{src_root}"

    checked, offenders = [], []
    for path in sorted(src_root.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError) as e:      # pragma: no cover
            offenders.append(f"{path.name}：讀不到／解析不了（{e}）")
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.With):
                continue
            for item in node.items:
                call = item.context_expr
                if not (isinstance(call, ast.Call)
                        and isinstance(call.func, ast.Name)
                        and call.func.id == "open"):
                    continue
                var = item.optional_vars
                if not isinstance(var, ast.Name):
                    continue
                # 這個 with 區塊裡有沒有 os.fsync(<var>.fileno())？
                syncs = [n for n in ast.walk(node)
                         if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Attribute)
                         and n.func.attr == "fsync"
                         and len(n.args) == 1
                         and isinstance(n.args[0], ast.Call)
                         and isinstance(n.args[0].func, ast.Attribute)
                         and n.args[0].func.attr == "fileno"
                         and isinstance(n.args[0].func.value, ast.Name)
                         and n.args[0].func.value.id == var.id]
                if not syncs:
                    continue
                mode = "r"
                if len(call.args) >= 2 and isinstance(call.args[1],
                                                      ast.Constant):
                    mode = str(call.args[1].value)
                for kw in call.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode = str(kw.value.value)
                where = f"{path.name}:{syncs[0].lineno}"
                checked.append(where)
                if not any(c in mode for c in "wa+x"):
                    offenders.append(f"{where} 用 mode={mode!r} 開檔")

    assert checked, "★掃不到任何 fsync★ 守衛失效了（不是「沒問題」）"
    assert offenders == [], (
        "唯讀 handle 的 os.fsync 在 Windows 一律回 EBADF＝沒有耐久性："
        + "；".join(offenders))
