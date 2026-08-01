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

# ★分界是「這支程式會不會對 HIS 送輸入」，不是「有沒有人看著」★
#   會送輸入的，復原沒完成就不進應用程式（主程式問人，其餘無聲退出）。
#   不送輸入的照常啟動 —— 守護程式尤其不可以擋，它是唯一會反覆重試復原的。
HIS_INPUT_LAUNCHERS = [
    "中國醫皮膚科打卡程式.pyw",        # 點打卡按鈕、輸入帳密
    "中國醫皮膚科會診查詢程式.pyw",    # PostMessage 點 systemftp 欄位/表格
]
NON_HIS_LAUNCHERS = [
    "中國醫皮膚科守護程式.pyw",
    "中國醫皮膚科排班程式.pyw",
    "中國醫皮膚科點座標偵測程式.pyw",
]


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

    def test_only_the_clinical_launcher_pops_a_window(self):
        """★只有主程式跳窗★ 其餘五支無人看顧，MessageBox 會讓行程卡死。"""
        for name in LAUNCHERS[1:]:
            assert "confirm_start_despite" not in _read_launcher(name), (
                f"{name} 是無人看顧的程式，不可以跳窗擋啟動")
        assert "confirm_start_despite" in _read_launcher(CLINICAL_LAUNCHER)

    @pytest.mark.parametrize("name", HIS_INPUT_LAUNCHERS)
    def test_programs_that_type_into_his_refuse_to_start_when_unsafe(
            self, name):
        """★[2026-08-02 外審第 2 輪 P1] 不跳窗 ≠ 照樣啟動★

        我第一版把「無人看顧」當成「必須 fail-open」，那是兩件事：
        不能【跳窗】成立，不能【退出】不成立 —— 排程會再叫一次，而混版模組
        一旦載進記憶體就收不回來。

        判準：`safe_to_start` 的結果必須真的用來決定要不要繼續，
        而且要在 `run_path` 之前就有一條退出路徑。
        """
        src = _read_launcher(name)
        assert "safe_to_start" in src, f"{name} 丟棄了復原結果"
        tree = ast.parse(src)

        # ★退出必須掛在【復原結果】那個判斷底下★
        #   第一版只比「最早的 SystemExit 行號 < 最早的 run_path 行號」，
        #   而打卡程式本來就有一個單例檢查的 `raise SystemExit(0)` 排在前面 ——
        #   把復原那段整個刪掉，測試照樣綠。這是「拿別人的證據當自己的」。
        guards = [n for n in ast.walk(tree) if isinstance(n, ast.If)
                  and any(isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                          and c.func.id == "_recover_incomplete_update"
                          for c in ast.walk(n.test))]
        assert guards, f"{name}：沒有把退出掛在復原結果的判斷上"
        exits = [n for g in guards for n in ast.walk(g)
                 if isinstance(n, ast.Raise) and isinstance(n.exc, ast.Call)
                 and isinstance(n.exc.func, ast.Name)
                 and n.exc.func.id == "SystemExit"]
        assert exits, f"{name}：復原不安全時沒有退出"
        runs = [n.lineno for n in ast.walk(tree)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "run_path"]
        assert runs and min(e.lineno for e in exits) < min(runs), (
            f"{name}：退出沒有排在載入應用程式之前")

    @pytest.mark.parametrize("name", NON_HIS_LAUNCHERS)
    def test_programs_that_never_touch_his_still_start(self, name):
        """★空集合不算通過★ 上面那條規則不可以無聲擴散到所有程式。

        守護程式尤其不可以擋：它是無人看顧下【唯一】會反覆重試復原的東西，
        擋掉它等於把自我修復的路砍斷（見該檔說明）。
        """
        src = _read_launcher(name)
        assert "safe_to_start" not in src, (
            f"{name} 不碰 HIS，不應該因復原失敗而拒絕啟動")

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
    """★比對【已提交的】manifest.json，不是「重新產生會包含」★

    [2026-08-02 外審第 2 輪] 第一版只呼叫 `collect_entries()`，那只證明
    「如果重新產生就會有」。實際送到診間的是 repo 裡那份 manifest.json ——
    新模組沒同步進去的話，updater 根本不知道有這個檔，修好的東西一輩子
    到不了診間。這支測試比對兩邊的檔案清單。
    """
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
    try:
        import sync_manifest
    finally:
        sys.path.pop(0)
    expected = {e["local_filename"] for e in sync_manifest.collect_entries("0")}
    with io.open(os.path.join(REPO_ROOT, "manifest.json"),
                 encoding="utf-8") as f:
        committed = {e["local_filename"] for e in json.load(f)["files"]}

    assert "src/bootstrap_recovery.py" in committed, (
        "★新模組沒進 manifest.json → 診間收不到★"
        "（修法：python scripts/sync_manifest.py <version>）")
    for name in LAUNCHERS:
        assert name in committed, f"{name} 沒有進更新清單"
    assert expected == committed, (
        f"manifest.json 與 sync_manifest 產生的清單不一致，"
        f"少了 {sorted(expected - committed)}，多了 {sorted(committed - expected)}")


def test_the_manifest_version_matches_the_code_version():
    """★manifest 的 app_version 必須等於磁碟上的 CURRENT_VERSION★

    [2026-08-02 外審第 2 輪 P1] updater 有一條明寫的假設：「同一個 app_version
    恆指同一份已發佈 revision」——它靠這個才敢在版號相同時照 SHA 修復
    （見 `check_and_update` 裡 codex P2 round3 那段註解）。若 manifest 內容變了
    卻沿用舊版號，另一支拿到舊快取 manifest 的行程就會把新版「修復」成舊版。

    ★這一支【只釘相等】，證明不了「不同 revision 用不同版號」★
    真正保證那件事的是 `push_helper` 的步驟順序，由
    `test_the_push_flow_is_what_guarantees_a_new_version_per_revision` 釘住。
    兩支要一起看才完整：這裡防「手動改了 manifest 卻沒同步版號」，那裡防
    「有人把 bump 或 sync 從發布流程拿掉」。
    """
    from cmuh_common.version import CURRENT_VERSION
    with io.open(os.path.join(REPO_ROOT, "manifest.json"),
                 encoding="utf-8") as f:
        manifest = json.load(f)
    assert manifest["app_version"] == CURRENT_VERSION, (
        f"manifest.json 是 v{manifest['app_version']}，"
        f"程式是 v{CURRENT_VERSION} —— 重新跑 sync_manifest")


def test_the_push_flow_is_what_guarantees_a_new_version_per_revision():
    """★updater 的版本契約靠這個順序成立，不是靠好習慣★

    `check_and_update` 明寫依賴「每個不同 revision 必 bump app_version」才敢在
    版號相同時照 SHA 修復。保證它的是 `push_helper.main()` 的順序：

        bump_version → sync_manifest(新版號) → git add → 驗 staged SHA → commit → push

    少了 bump，同一個版號會對應到兩份內容不同的 manifest；少了 sync_manifest，
    版號動了而 SHA 是舊的。這支測試釘住那個順序 —— ★不比對 git 歷史★，
    因為 CI 是 shallow clone，`HEAD~1` 不一定在。
    """
    import inspect
    sys.path.insert(0, os.path.join(REPO_ROOT, "scripts"))
    try:
        import push_helper
    finally:
        sys.path.pop(0)
    src = inspect.getsource(push_helper.main)
    order = []
    for name in ("step3_bump_version", "step4_sync_manifest", "step5_stage",
                 "verify_staged_manifest_hashes", "step5_commit", "step6_push"):
        assert f"{name}(" in src, f"發布流程少了 {name}"
        order.append(src.index(f"{name}("))
    assert order == sorted(order), (
        f"發布步驟順序被改動了：{order}（版號必須在產生 manifest 之前 bump）")


class TestTheTerminalMarkerSurvivesRestarts:
    """[2026-08-02 外審第 2 輪 P1] terminal 原本只擋第一次啟動。"""

    def test_an_old_marker_does_not_shadow_a_new_recoverable_journal(
            self, tmp_path):
        """★[2026-08-02 外審第 2 輪 P1] 這是我上一輪改出來的退步★

        第一版看到 `.failed.json` 就直接 return，連鎖都不拿 —— 於是「上次有個
        救不回來的檔」會讓「這次剛斷電、還救得回來的那一批」完全不被處理。
        使用者按「是」啟動之後，載到的是這次中斷造成的【新】混版模組。
        """
        (tmp_path / (br.JOURNAL_FILENAME + br.FAILED_JOURNAL_SUFFIX)).write_text(
            '{"schema": 1, "files": ["old.py"]}', encoding="utf-8")
        target = tmp_path / "fresh.py"
        target.write_text("新版（這次斷電留下的）", encoding="utf-8")
        (tmp_path / "fresh.py.bak").write_text("舊版", encoding="utf-8")
        _seed_journal(tmp_path, [(str(target), True, "")])

        result = br.recover_before_start(str(tmp_path))

        assert target.read_text(encoding="utf-8") == "舊版", (
            "★舊標記讓這次可復原的交易完全沒被處理★")
        assert result.restored == ["fresh.py"]
        # 舊標記還在 → 結論仍然是不可啟動
        assert result.status == br.TERMINAL_FAILURE
        assert result.safe_to_start is False

    def test_a_retryable_file_and_a_doomed_file_both_leave_a_trace(
            self, tmp_path):
        """★[2026-08-02 外審第 2 輪 P2] 兩種失敗必須能同時存在★

        一個檔備份不見了（救不回來），另一個被防毒鎖住（下次可能會好）。
        原本的 if/elif 會弄丟其中一邊：日誌縮寫給重試用之後，救不回來的那個
        就此蒸發 —— 重試成功、日誌被清，磁碟仍混版而所有警示都沒了。
        """
        doomed = tmp_path / "doomed.py"
        doomed.write_text("新版，備份不見了", encoding="utf-8")
        locked = tmp_path / "locked.py"
        locked.write_text("新版", encoding="utf-8")
        (tmp_path / "locked.py.bak").write_text("舊版", encoding="utf-8")
        _seed_journal(tmp_path, [(str(doomed), True, ""),
                                 (str(locked), True, "")])

        real_replace = os.replace

        def _fail_only_the_locked_one(src, dst):
            if str(dst).endswith("locked.py"):
                raise OSError("[WinError 5] 防毒鎖住")
            return real_replace(src, dst)

        import unittest.mock as mock
        with mock.patch.object(br.os, "replace", _fail_only_the_locked_one):
            result = br.recover_before_start(str(tmp_path))

        marker = tmp_path / (br.JOURNAL_FILENAME + br.FAILED_JOURNAL_SUFFIX)
        assert marker.exists(), "★救不回來的那個沒有留下任何痕跡★"
        assert "doomed.py" in marker.read_text(encoding="utf-8")

        journal = tmp_path / br.JOURNAL_FILENAME
        assert journal.exists(), "★可重試的那個沒有留下來給下次試★"
        kept = json.loads(journal.read_text(encoding="utf-8"))
        assert [os.path.basename(e["target"]) for e in kept["files"]] == \
            ["locked.py"], "日誌應該只剩下可重試的那個"
        assert result.safe_to_start is False

    def test_a_restored_file_does_not_come_back_as_doomed_next_boot(
            self, tmp_path):
        """★不縮寫日誌會讓成功的復原在下次開機變成永久警示★

        已還原的檔，它的 .bak 已被 os.replace 吃掉；整份日誌留到下次啟動，
        那些檔就會因為「找不到備份」被判成救不回來。
        """
        ok = tmp_path / "ok.py"
        ok.write_text("新版", encoding="utf-8")
        (tmp_path / "ok.py.bak").write_text("舊版", encoding="utf-8")
        locked = tmp_path / "locked.py"
        locked.write_text("新版", encoding="utf-8")
        (tmp_path / "locked.py.bak").write_text("舊版", encoding="utf-8")
        _seed_journal(tmp_path, [(str(ok), True, ""), (str(locked), True, "")])

        real_replace = os.replace
        state = {"fail": True}

        def _fail_the_locked_one(src, dst):
            if state["fail"] and str(dst).endswith("locked.py"):
                raise OSError("[WinError 5] 防毒鎖住")
            return real_replace(src, dst)

        import unittest.mock as mock
        with mock.patch.object(br.os, "replace", _fail_the_locked_one):
            first = br.recover_before_start(str(tmp_path))
        assert first.status == br.RETRYABLE_FAILURE

        state["fail"] = False                    # 防毒放手了
        second = br.recover_before_start(str(tmp_path))

        assert second.status == br.RECOVERED, (
            f"第二次應該乾淨收尾，卻是 {second.status}（{second.errors}）")
        assert not (tmp_path / (br.JOURNAL_FILENAME
                                + br.FAILED_JOURNAL_SUFFIX)).exists()
        assert locked.read_text(encoding="utf-8") == "舊版"

    def test_a_marker_that_could_not_be_written_keeps_the_whole_journal(
            self, tmp_path, monkeypatch):
        """★[2026-08-02 外審第 3 輪 P1] 標記寫失敗仍然清掉日誌 = 兩頭落空★

        當次還是回 terminal（使用者被擋住），但下一次啟動什麼都找不到 →
        CLEAN → 混版磁碟被無聲放行。
        """
        doomed = tmp_path / "doomed.py"
        doomed.write_text("新版，備份不見了", encoding="utf-8")
        _seed_journal(tmp_path, [(str(doomed), True, "")])

        monkeypatch.setattr(br, "_atomic_write_json",
                            lambda path, payload: False)
        first = br.recover_before_start(str(tmp_path))
        assert first.status == br.TERMINAL_FAILURE

        monkeypatch.undo()
        second = br.recover_before_start(str(tmp_path))
        assert second.status != br.CLEAN, (
            "★第二次啟動一片乾淨 —— 磁碟仍是混版卻放行了★")
        assert second.safe_to_start is False

    def test_the_updater_also_keeps_the_journal_when_the_marker_fails(
            self, tmp_path, monkeypatch):
        doomed = tmp_path / "doomed.py"
        doomed.write_text("新版，備份不見了", encoding="utf-8")
        journal = _seed_journal(tmp_path, [(str(doomed), True, "")])

        monkeypatch.setattr(updater, "_archive_failed_journal",
                            lambda app_dir, terminal: False)
        updater.recover_incomplete_update(str(tmp_path))

        assert os.path.exists(journal), (
            "★標記寫不出來，日誌又被清掉 = 沒有任何痕跡★")

    def test_a_marker_created_while_waiting_for_the_lock_is_noticed(
            self, tmp_path, monkeypatch):
        """★[2026-08-02 外審第 3 輪 P1] 鎖外的快照會過期★

        開機時五支程式同時起來。A 先拿到鎖、判定 terminal、寫標記並清掉日誌；
        B 在鎖外看到的 `had_marker` 還是 False。B 拿到鎖後若只重看日誌，
        就會回 CLEAN —— 在混版狀態下放行。
        """
        target = tmp_path / "x.py"
        target.write_text("新版", encoding="utf-8")
        journal_path = _seed_journal(tmp_path, [(str(target), True, "")])

        import contextlib
        marker = tmp_path / (br.JOURNAL_FILENAME + br.FAILED_JOURNAL_SUFFIX)

        @contextlib.contextmanager
        def _lock_that_takes_a_while(app_dir, timeout_sec=10.0):
            # 模擬「等鎖的期間，另一支程式把事情做完了」
            marker.write_text('{"schema": 1, "files": ["x.py"]}',
                              encoding="utf-8")
            os.remove(journal_path)
            yield True

        monkeypatch.setattr(br, "_write_lock", _lock_that_takes_a_while)
        result = br.recover_before_start(str(tmp_path))

        assert result.status == br.TERMINAL_FAILURE, (
            f"★拿到鎖後沒有重讀標記，回了 {result.status}★")
        assert result.safe_to_start is False

    def test_a_second_startup_still_reports_terminal(self, tmp_path):
        """★這是那個 finding 的核心★

        改名成 .failed.json 之後就沒有人再看它 → 第二次啟動找不到 journal
        → CLEAN → 磁碟仍是混版卻【無聲啟動】。整個機制只生效一次。
        """
        target = tmp_path / "lost.py"
        target.write_text("new, backup gone", encoding="utf-8")
        _seed_journal(tmp_path, [(str(target), True, "")])

        first = br.recover_before_start(str(tmp_path))
        assert first.status == br.TERMINAL_FAILURE

        second = br.recover_before_start(str(tmp_path))
        assert second.status == br.TERMINAL_FAILURE, "★第二次就放行了★"
        assert second.safe_to_start is False

    def test_the_marker_does_not_claim_a_file_count_it_did_not_measure(
            self, tmp_path):
        """★措辭鐵律★ 第二次啟動沒有再數檔案，就不可以說「0 個檔案」。"""
        (tmp_path / (br.JOURNAL_FILENAME + br.FAILED_JOURNAL_SUFFIX)).write_text(
            "{}", encoding="utf-8")
        said = br.recover_before_start(str(tmp_path)).describe()
        assert "0 個檔案" not in said

    def test_a_complete_update_round_lifts_the_marker(self, tmp_path):
        """出口：整棵樹被換成一致的新版之後才解除。"""
        marker = (tmp_path / (updater.JOURNAL_FILENAME
                              + updater.FAILED_JOURNAL_SUFFIX))
        marker.write_text("{}", encoding="utf-8")

        assert updater.clear_failed_journal_marker(str(tmp_path)) is True
        assert not marker.exists()
        assert br.recover_before_start(str(tmp_path)).status == br.CLEAN

    def test_nothing_to_lift_is_not_an_error(self, tmp_path):
        assert updater.clear_failed_journal_marker(str(tmp_path)) is False

    def test_the_updater_only_lifts_it_after_proving_consistency(self):
        """★不可以因為「這次沒偵測到問題」就清掉標記★

        兩個解除點都必須是「manifest 每個檔的 SHA 都對得上」的路徑：
        整批 commit 成功，或下載階段發現所有檔案皆為最新。
        """
        # ★排除它自己★ `def clear_failed_journal_marker(` 也含這個字串；
        #   不排除的話這支測試會被自己的定義餵飽（同一輪內第 N 次踩到
        #   「掃原始碼的測試自己命中自己」）。
        callers = [name for name, obj in vars(updater).items()
                   if callable(obj) and getattr(obj, "__module__", "")
                   == updater.__name__
                   and name != "clear_failed_journal_marker"
                   and "clear_failed_journal_marker(" in _safe_code(obj)]
        assert set(callers) == {"check_and_update", "_commit_pending_writes"}, (
            f"解除標記的地方變了：{sorted(callers)}")


def _body_without_docstring(node):
    """把 FunctionDef 的 docstring 拿掉再 unparse。

    ★又是同一個坑★ 說明文字裡提到 `bootstrap_recovery`，掃原始碼的斷言就會
    命中自己的註解而永遠通過（或像這次一樣永遠失敗）。
    """
    body = node.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return "\n".join(ast.unparse(n) for n in body)


def _safe_code(obj):
    import inspect
    try:
        return inspect.getsource(obj)
    except Exception:      # noqa: BLE001  內建/C 函式沒有原始碼
        return ""


class TestACorruptJournalIsNeverTreatedAsSuccess:
    """[2026-08-02 外審第 2 輪 P2] `payload.get("files") or []` 的洞。"""

    @pytest.mark.parametrize("payload,why", [
        ({}, "空物件"),
        ({"schema": 1}, "沒有 files"),
        ({"schema": 1, "files": []}, "files 是空的"),
        ({"schema": 99, "files": [{"target": "x", "existed_before": True}]},
         "schema 版本不認得"),
        ({"schema": 1, "files": [{"existed_before": True}]}, "項目缺 target"),
        ({"schema": 1, "files": [{"target": "x"}]}, "項目缺 existed_before"),
        ({"schema": 1, "files": "not a list"}, "files 型別錯"),
        ([1, 2, 3], "payload 根本不是物件"),
    ])
    def test_an_unreadable_journal_is_unknown_and_is_kept(self, tmp_path,
                                                          payload, why):
        journal = tmp_path / br.JOURNAL_FILENAME
        journal.write_text(json.dumps(payload), encoding="utf-8")

        result = br.recover_before_start(str(tmp_path))

        assert result.status == br.UNKNOWN, f"{why} 卻被當成可以啟動"
        assert result.safe_to_start is False
        assert journal.exists(), "★看不懂的日誌是唯一證據，不可以刪★"

    def test_a_well_formed_journal_is_still_accepted(self, tmp_path):
        """★空集合不算通過★ 證明上面不是「一律拒絕」才綠的。"""
        target = tmp_path / "ok.py"
        target.write_text("new", encoding="utf-8")
        (tmp_path / "ok.py.bak").write_text("old", encoding="utf-8")
        _seed_journal(tmp_path, [(str(target), True, "")])
        assert br.recover_before_start(str(tmp_path)).status == br.RECOVERED

    def test_both_modules_agree_on_the_schema_number(self):
        assert br.JOURNAL_SCHEMA == updater.JOURNAL_SCHEMA


class TestEveryRollbackConsumerHandlesTerminal:
    """[2026-08-02 外審第 2 輪 P2] 另外兩個呼叫端仍只看 unresolved。"""

    def test_a_failed_commit_whose_backup_vanished_keeps_the_evidence(
            self, tmp_path, monkeypatch):
        """★真的跑一次 commit 失敗 → 回滾 → 備份不見★

        第一版這裡是「數 `_clear_commit_journal(` 與 `.terminal` 的出現次數」，
        而突變 `if rb.terminal:` → `if False:` 之後兩個字串都還在，測試照樣綠。
        ★數字串不等於驗行為★ 改成把那條路徑實際走一遍。
        """
        first, second = tmp_path / "a.py", tmp_path / "b.py"
        first.write_text("舊 A", encoding="utf-8")
        second.write_text("舊 B", encoding="utf-8")

        # 不做備份 → 第一個檔換掉之後就沒有 .bak 可以還原（＝救不回來）
        monkeypatch.setattr(updater, "_make_backup_atomically", lambda p: None)

        calls = []

        def _replace_then_fail(src_path, dst_path):
            calls.append(dst_path)
            if len(calls) == 1:
                os.replace(src_path, dst_path)
                return
            raise OSError("[WinError 5] 第二個檔寫不進去")

        monkeypatch.setattr(updater, "_replace_file_with_retry",
                            _replace_then_fail)

        result = updater._commit_pending_writes(
            [("k1", "a.py", "1", "新 A", str(first)),
             ("k2", "b.py", "1", "新 B", str(second))],
            updater.UpdateResult(is_frozen=False))

        assert result.errors
        # 交易日誌放在 get_app_dir()（不是目標檔隔壁）—— 見 updater 裡
        # `app_dir_for_journal` 的取得方式。第一版這裡用 tmp_path，測試紅
        # 了但紅的不是產品。
        journal = os.path.join(updater.get_app_dir(),
                               updater.JOURNAL_FILENAME)
        assert not os.path.exists(journal), "日誌應該改名，不是留著重試"
        assert os.path.exists(journal + updater.FAILED_JOURNAL_SUFFIX), (
            "★備份不見了卻被當成乾淨結案：磁碟仍是混版，證據卻沒了★")
        assert result.updated_files == []

    def test_a_clean_rollback_after_a_failed_commit_leaves_no_marker(
            self, tmp_path, monkeypatch):
        """★空集合不算通過★ 有備份可還原時不可以留下「救不回來」標記。"""
        first, second = tmp_path / "a.py", tmp_path / "b.py"
        first.write_text("舊 A", encoding="utf-8")
        second.write_text("舊 B", encoding="utf-8")

        calls = []
        real_replace = updater._replace_file_with_retry

        def _replace_then_fail(src_path, dst_path):
            calls.append(dst_path)
            if len(calls) == 1:
                return real_replace(src_path, dst_path)
            raise OSError("[WinError 5] 第二個檔寫不進去")

        monkeypatch.setattr(updater, "_replace_file_with_retry",
                            _replace_then_fail)

        updater._commit_pending_writes(
            [("k1", "a.py", "1", "新 A", str(first)),
             ("k2", "b.py", "1", "新 B", str(second))],
            updater.UpdateResult(is_frozen=False))

        journal = os.path.join(updater.get_app_dir(),
                               updater.JOURNAL_FILENAME)
        assert not os.path.exists(journal + updater.FAILED_JOURNAL_SUFFIX)
        assert first.read_text(encoding="utf-8") == "舊 A", "沒有回滾成功"

    def test_updater_keeps_both_traces_when_both_kinds_of_failure_happen(
            self, tmp_path, monkeypatch):
        """updater 的日誌復原路徑也要能同時留下兩種痕跡（外審第 2 輪 P2）。"""
        doomed = tmp_path / "doomed.py"
        doomed.write_text("新版，備份不見了", encoding="utf-8")
        locked = tmp_path / "locked.py"
        locked.write_text("新版", encoding="utf-8")
        (tmp_path / "locked.py.bak").write_text("舊版", encoding="utf-8")
        _seed_journal(tmp_path, [(str(doomed), True, ""),
                                 (str(locked), True, "")])

        def _fail_the_locked_one(src, dst):
            if str(dst).endswith("locked.py"):
                raise OSError("[WinError 5] 防毒鎖住")
            return os.replace(src, dst)

        monkeypatch.setattr(updater, "_replace_file_with_retry",
                            _fail_the_locked_one)
        updater.recover_incomplete_update(str(tmp_path))

        journal = os.path.join(str(tmp_path), updater.JOURNAL_FILENAME)
        marker = journal + updater.FAILED_JOURNAL_SUFFIX
        assert os.path.exists(marker), "救不回來的那個沒有留下痕跡"
        assert "doomed.py" in io.open(marker, encoding="utf-8").read()
        assert os.path.exists(journal), "可重試的那個沒有留給下次"
        with io.open(journal, encoding="utf-8") as f:
            kept = json.load(f)
        assert [os.path.basename(e["target"]) for e in kept["files"]] == \
            ["locked.py"]

    def test_the_two_modules_write_the_same_marker_format(self, tmp_path):
        """★重複實作的第二個釘子★ updater 寫的標記，bootstrap 要讀得懂。"""
        updater._archive_failed_journal(str(tmp_path), ["a.py"])
        result = br.recover_before_start(str(tmp_path))
        assert result.status == br.TERMINAL_FAILURE
        assert result.safe_to_start is False

    def test_the_recovery_path_archives_instead_of_clearing(self, tmp_path):
        """對照：日誌復原那條路徑已經會保留證據（上面 TestTerminalFailure…）。"""
        target = tmp_path / "x.py"
        target.write_text("new", encoding="utf-8")
        _seed_journal(tmp_path, [(str(target), True, "")])
        updater.recover_incomplete_update(str(tmp_path))
        assert os.path.exists(os.path.join(
            str(tmp_path),
            updater.JOURNAL_FILENAME + updater.FAILED_JOURNAL_SUFFIX))


class TestTheLauncherWithoutTheRecoveryModule:
    """[2026-08-02 外審第 2 輪 P1] import 失敗時原本無條件放行。"""

    def test_it_asks_instead_of_assuming(self):
        src = _read_launcher(CLINICAL_LAUNCHER)
        tree = ast.parse(src)
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "_recover_incomplete_update")
        handlers = [h for h in ast.walk(func) if isinstance(h, ast.ExceptHandler)]
        assert handlers, "找不到 import 失敗的處理（測試失效了）"
        returns = [ast.unparse(n.value) for h in handlers
                   for n in ast.walk(h) if isinstance(n, ast.Return) and n.value]
        assert returns, "import 失敗那條路徑沒有回傳值"
        assert not any(r == "True" for r in returns), (
            "★載不進復原模組時無條件放行★ 那正是混版機率最高的時候")

    def test_the_fallback_dialog_is_self_contained(self):
        """它必須自給自足 —— 走到那裡就是因為 bootstrap_recovery 不能用。"""
        tree = ast.parse(_read_launcher(CLINICAL_LAUNCHER))
        func = next(n for n in ast.walk(tree)
                    if isinstance(n, ast.FunctionDef)
                    and n.name == "_ask_without_the_recovery_module")

        # ★用結構判，不要用字串★ 視窗文字裡【本來就會】出現
        #   「bootstrap_recovery.py」——那是要講給使用者聽的檔名，不是依賴。
        #   （字串斷言在這裡是誤報；同理 `0x100` 經 unparse 會變成 `256`。）
        imported = [a.name for n in ast.walk(func)
                    if isinstance(n, ast.Import) for a in n.names]
        imported += [n.module or "" for n in ast.walk(func)
                     if isinstance(n, ast.ImportFrom)]
        used = [n.value.id for n in ast.walk(func)
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)]
        assert "bootstrap_recovery" not in imported + used, (
            "後備視窗不可以依賴那個載不進來的模組")
        assert imported == ["ctypes"], f"只應該用 ctypes，卻用了 {imported}"

        body = _body_without_docstring(func)
        assert "MessageBoxW" in body
        flags = next(n for n in ast.walk(func)
                     if isinstance(n, ast.Call)
                     and isinstance(n.func, ast.Attribute)
                     and n.func.attr == "MessageBoxW").args[3]
        assert eval(ast.unparse(flags)) & 0x100, (   # noqa: S307  常數運算式
            "沒有 MB_DEFBUTTON2 → 預設鈕會停在「是」")


def test_the_journal_schema_written_is_the_one_bootstrap_reads(tmp_path):
    """欄位層級的釘子：updater 寫出來的每一筆都必須有 bootstrap 讀的三個鍵。"""
    journal = _seed_journal(tmp_path, [("C:\\app\\a.py", True, "C:\\app\\a.tmp")])
    with io.open(journal, encoding="utf-8") as f:
        payload = json.load(f)
    for entry in payload["files"]:
        assert set(entry) >= {"target", "existed_before", "staged"}
