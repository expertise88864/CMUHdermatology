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
