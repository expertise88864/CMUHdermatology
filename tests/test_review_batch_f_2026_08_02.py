# -*- coding: utf-8 -*-
"""批次 F：啟動前的獨立更新復原（2026-08-02 外部 code review P1-01）。

外審的四個子問題，各自對應下面的測試群：
  (a) 復原跑在 main.py 匯入【之後】      → `TestRecoveryRunsBeforeAnythingElse`
  (b) 復原失敗只 logging.debug、照樣更新 → `TestCheckAndUpdateStopsOnUnrecovered`
  (c) 救不回來時把交易日誌【刪掉】       → `TestTerminalFailureKeepsEvidence`
  (d) 日誌裡的路徑沒有重新驗證           → `TestJournalPathsAreNotTrusted`
"""
from __future__ import annotations

import ast
import io
import json
import os
import sys

import pytest

import bootstrap_recovery as br
from cmuh_common import updater

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

LAUNCHERS = [
    "中國醫皮膚科主程式.pyw",
    "中國醫皮膚科守護程式.pyw",
    "中國醫皮膚科打卡程式.pyw",
    "中國醫皮膚科排班程式.pyw",
    "中國醫皮膚科會診查詢程式.pyw",
    "中國醫皮膚科點座標偵測程式.pyw",
]

CLINICAL_LAUNCHER = LAUNCHERS[0]


def _read_launcher(name):
    with io.open(os.path.join(REPO_ROOT, name), encoding="utf-8") as f:
        return f.read()


def _seed_journal(app_dir, entries):
    """用【updater 自己的寫入函式】造日誌 —— 不要在測試裡手刻 JSON。

    手刻的話，updater 哪天改了欄位名，這裡的測試還是綠的，而正式路徑已經對不上。
    """
    assert updater._write_commit_journal(str(app_dir), entries) is True
    return os.path.join(str(app_dir), br.JOURNAL_FILENAME)


# ── (a) 復原必須跑在任何 import 之前 ──────────────────────────────────
class TestRecoveryRunsBeforeAnythingElse:

    @pytest.mark.parametrize("name", LAUNCHERS)
    def test_every_launcher_recovers_before_it_runs_or_imports_anything(
            self, name):
        """★這是這一批的核心不變量★

        復原的價值完全來自「時機」：晚一步，半套更新的模組就已經在記憶體裡了。
        所以這裡比的是【行號】——復原呼叫必須排在 `runpy.run_path` 與任何
        `cmuh_common` 匯入之前。
        """
        tree = ast.parse(_read_launcher(name))
        recover_lines, guarded_lines = [], []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name) and \
                        func.id == "_recover_incomplete_update":
                    recover_lines.append(node.lineno)
                elif isinstance(func, ast.Attribute) and \
                        func.attr == "run_path":
                    guarded_lines.append(node.lineno)
            elif isinstance(node, ast.ImportFrom) and \
                    (node.module or "").startswith("cmuh_common"):
                guarded_lines.append(node.lineno)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith("cmuh_common"):
                        guarded_lines.append(node.lineno)

        assert recover_lines, f"{name} 沒有呼叫 _recover_incomplete_update"
        # 定義處也叫這個名字，但那是 FunctionDef 不是 Call；這裡只會抓到呼叫。
        assert guarded_lines, f"{name} 找不到 run_path/cmuh_common 匯入（測試失效了）"
        assert min(recover_lines) < min(guarded_lines), (
            f"{name}：復原（第 {min(recover_lines)} 行）必須排在"
            f"第 {min(guarded_lines)} 行之前")

    def test_the_clinical_launcher_can_actually_refuse_to_start(self):
        """主程式的 `run_path` 必須在「復原結果」的 if 底下 —— 不是只呼叫一下。

        只呼叫不看回傳值，就等於「復原沒完成照樣啟動」，那正是要修的東西。
        """
        tree = ast.parse(_read_launcher(CLINICAL_LAUNCHER))
        guard = None
        for node in ast.walk(tree):
            if isinstance(node, ast.If):
                calls = [n for n in ast.walk(node.test)
                         if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Name)
                         and n.func.id == "_recover_incomplete_update"]
                if calls:
                    guard = node
                    break
        assert guard is not None, "主程式沒有把啟動包在復原結果的判斷底下"
        inside = {n.lineno for n in ast.walk(guard)
                  if isinstance(n, ast.Call)
                  and isinstance(n.func, ast.Attribute)
                  and n.func.attr == "run_path"}
        outside = {n.lineno for n in ast.walk(tree)
                   if isinstance(n, ast.Call)
                   and isinstance(n.func, ast.Attribute)
                   and n.func.attr == "run_path"} - inside
        assert inside, "run_path 不在復原判斷的分支內"
        assert not outside, f"還有 run_path 在復原判斷之外：第 {sorted(outside)} 行"

    def test_only_the_clinical_launcher_is_allowed_to_block_startup(self):
        """★其餘五支刻意【不擋】★ 它們無人看顧，跳窗只會讓行程卡死。

        這個測試把那個取捨釘住：哪天有人順手把 confirm_start_despite 加到
        守護程式或打卡程式，會在這裡轉紅並被迫重新想一次。
        """
        for name in LAUNCHERS[1:]:
            assert "confirm_start_despite" not in _read_launcher(name), (
                f"{name} 是無人看顧的程式，不可以跳窗擋啟動")
        assert "confirm_start_despite" in _read_launcher(CLINICAL_LAUNCHER)

    def test_bootstrap_recovery_imports_nothing_from_the_app(self):
        """★它不可以 import cmuh_common★ 那正是可能壞掉的東西。

        含函式內的延遲匯入 —— `ast.walk` 會走進去。
        """
        with io.open(os.path.join(REPO_ROOT, "src", "bootstrap_recovery.py"),
                     encoding="utf-8") as f:
            tree = ast.parse(f.read())
        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if (node.module or "").split(".")[0] in ("cmuh_common", "src"):
                    bad.append(node.module)
            elif isinstance(node, ast.Import):
                bad += [a.name for a in node.names
                        if a.name.split(".")[0] in ("cmuh_common", "src")]
        assert bad == [], f"bootstrap_recovery 匯入了應用程式模組：{bad}"


# ── 契約：兩邊對日誌的認知必須一致 ────────────────────────────────────
class TestJournalContractStaysInSyncWithUpdater:

    def test_both_modules_agree_on_the_journal_filename(self):
        assert br.JOURNAL_FILENAME == updater.JOURNAL_FILENAME
        assert br.FAILED_JOURNAL_SUFFIX == updater.FAILED_JOURNAL_SUFFIX

    def test_bootstrap_can_read_a_journal_the_updater_actually_wrote(
            self, tmp_path):
        """★往返測試，不是字串比對★

        重複實作最怕的是「欄位名改了、只有一邊跟上」。這裡讓 updater 寫、
        bootstrap 讀，欄位一改就會有一邊對不上。
        """
        target = tmp_path / "mod.py"
        target.write_text("new", encoding="utf-8")
        (tmp_path / "mod.py.bak").write_text("old", encoding="utf-8")
        _seed_journal(tmp_path, [(str(target), True, "")])

        result = br.recover_before_start(str(tmp_path))

        assert result.status == br.RECOVERED
        assert target.read_text(encoding="utf-8") == "old"


# ── 復原本身的行為 ────────────────────────────────────────────────────
class TestRecoverBeforeStart:

    def test_no_journal_means_clean_and_touches_nothing(self, tmp_path):
        result = br.recover_before_start(str(tmp_path))
        assert result.status == br.CLEAN
        assert result.safe_to_start is True
        # 沒有日誌就完全不該碰鎖檔（絕大多數啟動走這條）
        assert not (tmp_path / br.LOCK_FILENAME).exists()

    def test_a_finished_rollback_clears_the_journal(self, tmp_path):
        target = tmp_path / "a.py"
        target.write_text("new", encoding="utf-8")
        (tmp_path / "a.py.bak").write_text("old", encoding="utf-8")
        journal = _seed_journal(tmp_path, [(str(target), True, "")])

        result = br.recover_before_start(str(tmp_path))

        assert result.status == br.RECOVERED
        assert result.restored == ["a.py"]
        assert not os.path.exists(journal)

    def test_a_new_file_that_was_added_gets_removed(self, tmp_path):
        added = tmp_path / "brand_new.py"
        added.write_text("added by the update", encoding="utf-8")
        _seed_journal(tmp_path, [(str(added), False, "")])

        result = br.recover_before_start(str(tmp_path))

        assert result.status == br.RECOVERED
        assert not added.exists()

    def test_a_file_that_was_never_replaced_is_not_counted_as_restored(
            self, tmp_path):
        """★誠實計數★ 崩潰時還沒輪到的檔不算「已回滾」。

        `os.replace(tmp, target)` 會把 tmp 吃掉，所以 tmp 還在＝還沒換過。
        """
        target = tmp_path / "later.py"
        target.write_text("still the old one", encoding="utf-8")
        staged = tmp_path / "later.py.upd.tmp"
        staged.write_text("the new one, not applied yet", encoding="utf-8")
        _seed_journal(tmp_path, [(str(target), True, str(staged))])

        result = br.recover_before_start(str(tmp_path))

        assert result.status == br.RECOVERED
        assert result.restored == []          # ★不是 ["later.py"]★
        assert target.read_text(encoding="utf-8") == "still the old one"

    def test_a_missing_backup_is_terminal_and_keeps_the_evidence(
            self, tmp_path):
        target = tmp_path / "gone.py"
        target.write_text("new version, backup vanished", encoding="utf-8")
        journal = _seed_journal(tmp_path, [(str(target), True, "")])

        result = br.recover_before_start(str(tmp_path))

        assert result.status == br.TERMINAL_FAILURE
        assert result.safe_to_start is False
        assert not os.path.exists(journal), "日誌應該改名，不是留在原處"
        assert os.path.exists(journal + br.FAILED_JOURNAL_SUFFIX), (
            "★救不回來時必須保留證據★")

    def test_a_locked_file_is_retryable_and_the_journal_survives(
            self, tmp_path, monkeypatch):
        target = tmp_path / "locked.py"
        target.write_text("new", encoding="utf-8")
        (tmp_path / "locked.py.bak").write_text("old", encoding="utf-8")
        journal = _seed_journal(tmp_path, [(str(target), True, "")])

        def _boom(src, dst):
            raise OSError("[WinError 5] 存取被拒（防毒鎖住）")

        monkeypatch.setattr(br.os, "replace", _boom)
        result = br.recover_before_start(str(tmp_path))

        assert result.status == br.RETRYABLE_FAILURE
        assert result.unresolved == ["locked.py"]
        assert os.path.exists(journal), "可重試的失敗必須留著日誌讓下次再試"

    def test_a_corrupt_journal_is_unknown_not_clean(self, tmp_path):
        """★查不到 ≠ 沒問題★ 讀不懂日誌不可以當成沒事。"""
        (tmp_path / br.JOURNAL_FILENAME).write_text("{not json",
                                                    encoding="utf-8")
        result = br.recover_before_start(str(tmp_path))
        assert result.status == br.UNKNOWN
        assert result.safe_to_start is False

    def test_a_busy_lock_is_unknown_rather_than_a_rollback(
            self, tmp_path, monkeypatch):
        """「另一支正在寫」≠「上次崩潰」—— 不可以把人家寫到一半的批次回滾掉。"""
        target = tmp_path / "b.py"
        target.write_text("new", encoding="utf-8")
        (tmp_path / "b.py.bak").write_text("old", encoding="utf-8")
        _seed_journal(tmp_path, [(str(target), True, "")])

        import contextlib

        @contextlib.contextmanager
        def _busy(app_dir, timeout_sec=10.0):
            yield False

        monkeypatch.setattr(br, "_write_lock", _busy)
        result = br.recover_before_start(str(tmp_path))

        assert result.status == br.UNKNOWN
        assert target.read_text(encoding="utf-8") == "new", "不可以動別人的批次"

    def test_recovery_never_raises_even_when_everything_breaks(
            self, tmp_path, monkeypatch):
        _seed_journal(tmp_path, [(str(tmp_path / "x.py"), True, "")])

        def _explode(*a, **k):
            raise RuntimeError("開檔炸了")

        monkeypatch.setattr(br, "open", _explode, raising=False)
        result = br.recover_before_start(str(tmp_path))   # 不可以拋出去
        assert result.status == br.UNKNOWN

    @pytest.mark.parametrize("status,expected", [
        (br.CLEAN, True),
        (br.RECOVERED, True),
        (br.RETRYABLE_FAILURE, False),
        (br.TERMINAL_FAILURE, False),
        (br.UNKNOWN, False),
    ])
    def test_only_clean_and_recovered_are_safe_to_start(self, status,
                                                        expected):
        assert br.RecoveryResult(status).safe_to_start is expected

    def test_every_status_has_its_own_wording(self):
        """★措辭鐵律★ 五種狀態不可以共用一句話（尤其不能把 UNKNOWN 說成沒事）。"""
        said = {br.RecoveryResult(s).describe()
                for s in (br.CLEAN, br.RECOVERED, br.RETRYABLE_FAILURE,
                          br.TERMINAL_FAILURE, br.UNKNOWN)}
        assert len(said) == 5


# ── (d) 日誌裡的路徑不可信 ────────────────────────────────────────────
class TestJournalPathsAreNotTrusted:

    def test_bootstrap_refuses_a_target_outside_the_app_dir(self, tmp_path):
        """★這支程式常以管理員身分執行★

        日誌是磁碟上一個普通 JSON。若照單全收，改掉它就等於任意檔案刪除。
        """
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        outsider = tmp_path / "important.txt"
        outsider.write_text("使用者的東西", encoding="utf-8")
        _seed_journal(app_dir, [(str(outsider), False, "")])

        result = br.recover_before_start(str(app_dir))

        assert outsider.exists(), "★程式目錄外的檔案被刪掉了★"
        assert outsider.read_text(encoding="utf-8") == "使用者的東西"
        assert result.status == br.TERMINAL_FAILURE

    def test_updater_also_refuses_a_target_outside_the_app_dir(self, tmp_path):
        """同一個攻擊面在 updater 的復原路徑上也要堵住（它跑在長命的行程裡）。"""
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        outsider = tmp_path / "outside.py"
        outsider.write_text("原本的內容", encoding="utf-8")
        _seed_journal(app_dir, [(str(outsider), False, "")])

        updater.recover_incomplete_update(str(app_dir))

        assert outsider.exists()
        assert outsider.read_text(encoding="utf-8") == "原本的內容"

    @pytest.mark.parametrize("relative", ["..", os.path.join("..", "..")])
    def test_path_traversal_in_the_journal_is_rejected(self, tmp_path,
                                                       relative):
        app_dir = tmp_path / "app"
        app_dir.mkdir()
        escaped = os.path.join(str(app_dir), relative, "escaped.py")
        assert br._inside(str(app_dir), escaped) is False

    def test_a_path_inside_the_app_dir_is_accepted(self, tmp_path):
        """★空集合不算通過★ 上面那些拒絕測試，要有這一支證明它不是全拒。"""
        assert br._inside(str(tmp_path),
                          os.path.join(str(tmp_path), "src", "main.py")) is True


# ── (c) updater：救不回來時保留證據 ──────────────────────────────────
class TestTerminalFailureKeepsEvidence:

    def test_updater_archives_rather_than_deletes_when_unrecoverable(
            self, tmp_path):
        target = tmp_path / "lost.py"
        target.write_text("new version, no backup", encoding="utf-8")
        journal = _seed_journal(tmp_path, [(str(target), True, "")])

        updater.recover_incomplete_update(str(tmp_path))

        assert not os.path.exists(journal)
        assert os.path.exists(journal + updater.FAILED_JOURNAL_SUFFIX), (
            "★原本會直接刪掉：磁碟仍是混版，證據卻沒了★")

    def test_a_clean_rollback_still_clears_the_journal(self, tmp_path):
        """對照組：正常結案時日誌要被【清掉】，不可以留下 .failed.json。"""
        target = tmp_path / "ok.py"
        target.write_text("new", encoding="utf-8")
        (tmp_path / "ok.py.bak").write_text("old", encoding="utf-8")
        journal = _seed_journal(tmp_path, [(str(target), True, "")])

        updater.recover_incomplete_update(str(tmp_path))

        assert not os.path.exists(journal)
        assert not os.path.exists(journal + updater.FAILED_JOURNAL_SUFFIX)

    def test_rollback_outcome_separates_retryable_from_terminal(self,
                                                               tmp_path):
        retryable = tmp_path / "r.py"
        retryable.write_text("new", encoding="utf-8")
        (tmp_path / "r.py.bak").write_text("old", encoding="utf-8")
        doomed = tmp_path / "d.py"
        doomed.write_text("new", encoding="utf-8")   # 沒有 .bak

        out = updater._rollback_written_files(
            [updater._WrittenFile(str(retryable), True),
             updater._WrittenFile(str(doomed), True)],
            from_journal=True)

        assert out.restored == [str(retryable)]
        assert out.terminal == [str(doomed)]
        assert out.unresolved == []      # ★不可重試的不准放進重試佇列★


# ── (b) 復原沒收乾淨就不要再套新的一批 ────────────────────────────────
class TestCheckAndUpdateStopsOnUnrecovered:

    def test_update_round_aborts_while_a_journal_remains(self, tmp_path,
                                                         monkeypatch):
        """★新版蓋在半舊的樹上，.bak 鏈就斷了 —— 出事再也回不去★"""
        monkeypatch.setattr(updater, "get_app_dir", lambda: str(tmp_path))
        monkeypatch.setattr(updater, "recover_incomplete_update",
                            lambda *a, **k: [])
        (tmp_path / updater.JOURNAL_FILENAME).write_text("{}", encoding="utf-8")

        def _should_not_run(*a, **k):
            raise AssertionError("★復原沒完成卻仍然去抓遠端版本★")

        monkeypatch.setattr(updater, "_fetch_remote_manifest", _should_not_run,
                            raising=False)

        result = updater.check_and_update(write_files=False)

        assert result.errors
        assert any("交易日誌" in e for e in result.errors)

    def test_the_abort_uses_the_disk_not_the_return_value(self):
        """判斷依據必須是「日誌還在不在」。

        `recover_incomplete_update` 內部吞掉所有例外、回傳的是【還原了哪些檔】——
        全部失敗時它回空 list，跟「本來就沒事」長得一模一樣。拿它當判準等於沒判。
        """
        src = _code_of(updater.check_and_update)
        assert "_journal_path" in src, "沒有去看磁碟上的日誌"


def _code_of(func):
    """取函式原始碼，★把 docstring 拿掉★

    否則「檢查程式碼有沒有寫某個字」的測試會被自己的說明文字餵飽而永遠通過。
    """
    import inspect
    import textwrap
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    node = tree.body[0]
    body = node.body[1:] if (
        isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)) else node.body
    return "\n".join(ast.unparse(n) for n in body)


# ── 給人看的那個視窗 ──────────────────────────────────────────────────
class TestTheDialog:

    def _fake_user32(self, monkeypatch, return_value, captured):
        class _MB:
            def MessageBoxW(self, hwnd, text, title, flags):
                captured.append((text, title, flags))
                return return_value

        class _Windll:
            user32 = _MB()

        import ctypes
        monkeypatch.setattr(ctypes, "windll", _Windll(), raising=False)

    def test_yes_means_start_anyway(self, monkeypatch):
        captured = []
        self._fake_user32(monkeypatch, 6, captured)      # IDYES
        result = br.RecoveryResult(br.TERMINAL_FAILURE, unresolved=["a.py"])
        assert br.confirm_start_despite(result, "主程式") is True

    def test_anything_other_than_yes_means_do_not_start(self, monkeypatch):
        for code in (7, 2, 0):        # IDNO / IDCANCEL / 叫不出視窗
            captured = []
            self._fake_user32(monkeypatch, code, captured)
            result = br.RecoveryResult(br.TERMINAL_FAILURE)
            assert br.confirm_start_despite(result, "主程式") is False

    def test_the_default_button_is_do_not_start(self, monkeypatch):
        """★驚慌之下連按 Enter 不可以就這樣帶著混版進診間★"""
        captured = []
        self._fake_user32(monkeypatch, 7, captured)
        br.confirm_start_despite(br.RecoveryResult(br.TERMINAL_FAILURE), "主程式")
        _text, _title, flags = captured[0]
        assert flags & 0x100, "沒有 MB_DEFBUTTON2 → 預設鈕會停在「是」"
        assert flags & 0x04, "不是 Yes/No 對話框"

    def test_the_dialog_says_what_actually_happened(self, monkeypatch):
        captured = []
        self._fake_user32(monkeypatch, 7, captured)
        br.confirm_start_despite(
            br.RecoveryResult(br.RETRYABLE_FAILURE, unresolved=["mod_x.py"]),
            "主程式")
        text = captured[0][0]
        assert "mod_x.py" in text, "沒有告訴使用者是哪個檔"
        assert "主程式" in text

    def test_no_dialog_means_no_start(self, monkeypatch):
        """★問不到人就不要啟動★ MessageBox 叫不出來時不可以預設放行。"""
        import ctypes

        class _Broken:
            @property
            def user32(self):
                raise OSError("沒有 GUI")

        monkeypatch.setattr(ctypes, "windll", _Broken(), raising=False)
        assert br.confirm_start_despite(
            br.RecoveryResult(br.UNKNOWN), "主程式") is False


class TestTheRecoveryLog:

    def test_a_clean_startup_writes_nothing(self, tmp_path):
        br.recover_and_report(str(tmp_path), "主程式")
        assert not (tmp_path / br.RECOVERY_LOG).exists(), (
            "每次啟動都寫一行會把 log 灌爆，也讓真的有事那次淹沒在裡面")

    def test_a_failure_is_written_down_with_the_file_names(self, tmp_path):
        target = tmp_path / "lost.py"
        target.write_text("new", encoding="utf-8")
        _seed_journal(tmp_path, [(str(target), True, "")])

        result = br.recover_and_report(str(tmp_path), "主程式")

        assert result.status == br.TERMINAL_FAILURE
        written = (tmp_path / br.RECOVERY_LOG).read_text(encoding="utf-8")
        assert "lost.py" in written
        assert "主程式" in written

    def test_an_unwritable_log_does_not_break_startup(self, tmp_path,
                                                      monkeypatch):
        _seed_journal(tmp_path, [(str(tmp_path / "z.py"), True, "")])
        real_open = io.open

        def _no_log(path, *a, **k):
            if str(path).endswith(br.RECOVERY_LOG):
                raise OSError("磁碟滿了")
            return real_open(path, *a, **k)

        monkeypatch.setattr(br, "open", _no_log, raising=False)
        result = br.recover_and_report(str(tmp_path), "主程式")
        assert result.status in (br.TERMINAL_FAILURE, br.RETRYABLE_FAILURE)


class TestTheStandaloneEntryPoint:

    def test_it_exits_nonzero_when_recovery_did_not_finish(self, tmp_path,
                                                           monkeypatch,
                                                           capsys):
        monkeypatch.setattr(
            br, "recover_before_start",
            lambda app_dir: br.RecoveryResult(br.TERMINAL_FAILURE,
                                              errors=["備份不見了"]))
        assert br.main() == 1
        out = capsys.readouterr().out
        assert br.TERMINAL_FAILURE in out
        assert "備份不見了" in out

    def test_it_exits_zero_when_there_is_nothing_to_do(self, tmp_path,
                                                       monkeypatch, capsys):
        monkeypatch.setattr(br, "recover_before_start",
                            lambda app_dir: br.RecoveryResult(br.CLEAN))
        assert br.main() == 0


@pytest.mark.skipif(os.name != "nt", reason="msvcrt 只有 Windows 有")
class TestTheLockIsTheSameOneTheUpdaterUses:

    def test_bootstrap_waits_for_a_lock_the_updater_is_holding(self):
        """兩邊必須是【同一把鎖】—— 開機時五支程式幾乎同時起來。

        用 updater 的 context manager 持鎖，再讓 bootstrap 去搶：搶不到才證明
        它們鎖的是同一個檔的同一個位元組。

        ★不要在這裡 monkeypatch `paths.get_app_dir`★ 第一版這樣寫，測試轉紅，
        而紅的原因不是產品壞掉：conftest 用「穩定函式 ＋ 可變 holder」換掉了
        `get_app_dir`，updater 早已 by-name 綁到那個穩定函式上，改
        `paths.get_app_dir` 動不到它。於是 updater 鎖 conftest 的目錄、
        bootstrap 鎖 tmp_path —— 兩個不同的檔，當然不會互相擋。
        改成兩邊都用 `updater.get_app_dir()`（本測試專屬的暫存目錄）。
        """
        app_dir = updater.get_app_dir()
        with updater._updater_write_lock() as held:
            assert held is True
            with br._write_lock(app_dir, timeout_sec=0.5) as got:
                assert got is False, "★bootstrap 沒有被擋住 → 兩把不同的鎖★"

    def test_the_lock_is_available_when_nobody_holds_it(self):
        """★空集合不算通過★ 證明上面那支不是因為永遠拿不到鎖才綠的。"""
        with br._write_lock(updater.get_app_dir(), timeout_sec=0.5) as got:
            assert got is True


def test_bootstrap_recovery_is_shipped_by_the_updater():
    """它必須進更新清單，否則診間永遠拿不到這支程式。"""
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
    try:
        import sync_manifest
    finally:
        sys.path.pop(0)
    entries = sync_manifest.collect_entries("0.0.0")
    shipped = {e["local_filename"] for e in entries}
    assert "src/bootstrap_recovery.py" in shipped
    for name in LAUNCHERS:
        assert name in shipped, f"{name} 沒有進更新清單"


def test_the_journal_schema_written_is_the_one_bootstrap_reads(tmp_path):
    """欄位層級的釘子：updater 寫出來的每一筆都必須有 bootstrap 讀的三個鍵。"""
    journal = _seed_journal(tmp_path, [("C:\\app\\a.py", True, "C:\\app\\a.tmp")])
    with io.open(journal, encoding="utf-8") as f:
        payload = json.load(f)
    for entry in payload["files"]:
        assert set(entry) >= {"target", "existed_before", "staged"}
