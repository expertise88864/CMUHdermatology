# -*- coding: utf-8 -*-
"""[批次X] 任何「再開一次自己」都必須走固定的 `.pyw` launcher。

★共同病灶★ `runpy.run_path` 會把 `sys.argv[0]` 換成被執行的那支源碼
（CPython 的 `_ModifiedArgv0`）。版本化之後那是 `versions/<V1>/src/xxx.py`。
任何拿它去重新啟動的地方都會**鎖在舊版本，而且不再讀 `current.txt`**：

| 路徑 | 後果 |
| --- | --- |
| `restart_self()` | 更新後的重啟永遠停在舊版（L1 已修） |
| `run_as_admin()` | UAC 提權後跑舊版，且不重跑開機復原 |
| 托盤「設定」 | **舊版 UI 寫新版 settings** —— 舊預設值覆寫新欄位 |

所以三者共用同一個真相來源 `paths.self_entry_path()`。

★[外審 P2-02] 環境變數不可以只驗「存在」★
它是會被繼承的。只驗存在的話，一個指向別處的值就能讓我們去重啟另一支程式。
`pinned_launcher()` 要求它**就在釘住的 app 根目錄第一層**。
"""
import os
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from cmuh_common import paths as cpaths  # noqa: E402
from cmuh_common import platform_win as pw  # noqa: E402


@pytest.fixture
def versioned(tmp_path, monkeypatch):
    entry = tmp_path / "versions" / "V1" / "src" / "main.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("", encoding="utf-8")
    launcher = tmp_path / "中國醫皮膚科主程式.pyw"
    launcher.write_text("", encoding="utf-8")
    monkeypatch.setattr(cpaths.sys, "argv", [str(entry)])
    monkeypatch.setattr(cpaths, "is_frozen", lambda: False)
    monkeypatch.setenv(cpaths.APP_DIR_ENV, str(tmp_path))
    monkeypatch.setenv(cpaths.LAUNCHER_ENV, str(launcher))
    return tmp_path, entry, launcher


# ── pinned_launcher 的驗證強度 ────────────────────────────────────────────
def test_a_valid_launcher_is_accepted(versioned):
    _app, _entry, launcher = versioned
    assert cpaths.pinned_launcher() == os.path.realpath(str(launcher))


def test_a_launcher_outside_the_app_root_is_rejected(versioned, monkeypatch,
                                                     tmp_path_factory):
    """★核心（外審 P2-02）★ 只驗存在會讓我們去重啟【別的程式】。"""
    other = tmp_path_factory.mktemp("elsewhere") / "壞的.pyw"
    other.write_text("", encoding="utf-8")
    monkeypatch.setenv(cpaths.LAUNCHER_ENV, str(other))
    assert cpaths.pinned_launcher() == "", (
        "app 根目錄以外的檔竟然被當成我們的 launcher")


def test_a_launcher_nested_below_the_root_is_rejected(versioned, monkeypatch):
    """必須在【第一層】—— 六支 launcher 真正的位置。"""
    app, _entry, _l = versioned
    nested = app / "versions" / "V1" / "假的.pyw"
    nested.write_text("", encoding="utf-8")
    monkeypatch.setenv(cpaths.LAUNCHER_ENV, str(nested))
    assert cpaths.pinned_launcher() == ""


@pytest.mark.parametrize("value", ["", "   ", "C:\\nope\\nope.pyw"])
def test_a_bogus_launcher_is_rejected(versioned, monkeypatch, value):
    monkeypatch.setenv(cpaths.LAUNCHER_ENV, value)
    assert cpaths.pinned_launcher() == ""


# ── self_entry_path 是單一真相來源 ────────────────────────────────────────
def test_self_entry_prefers_the_launcher(versioned):
    _app, entry, launcher = versioned
    got = cpaths.self_entry_path()
    assert got == os.path.realpath(str(launcher))
    assert str(entry) != got, "★仍然指向舊版本的源碼★"


def test_self_entry_falls_back_without_a_pin(versioned, monkeypatch):
    """★沒釘住就照舊★ 過渡期／直接跑 src 不可以壞掉。"""
    _app, entry, _l = versioned
    monkeypatch.delenv(cpaths.LAUNCHER_ENV, raising=False)
    assert cpaths.self_entry_path() == str(entry)


def test_restart_goes_through_self_entry(versioned):
    _app, entry, launcher = versioned
    cmd = cpaths.build_restart_command()
    assert os.path.realpath(str(launcher)) in cmd
    assert str(entry) not in cmd


# ── UAC 提權 ─────────────────────────────────────────────────────────────
def test_uac_relaunch_uses_the_launcher(versioned):
    """★外審 P1-02★ 提權後不可以還在跑 V1。"""
    _app, entry, launcher = versioned
    params = pw._admin_relaunch_params([str(entry), "--background"],
                                       frozen=False)
    assert os.path.realpath(str(launcher)) in params
    assert str(entry) not in params
    assert "--background" in params, "後面的引數不可以掉"


def test_uac_relaunch_keeps_explicit_argv_without_a_pin(versioned,
                                                        monkeypatch):
    """★沒釘住就不可以覆寫呼叫端指定的 argv★

    我第一版無條件替換 `args[0]`，把既有測試（明確傳 argv 驗引號處理）弄紅了
    —— 那不是測試的問題，是我改壞了語意。
    """
    monkeypatch.delenv(cpaths.LAUNCHER_ENV, raising=False)
    params = pw._admin_relaunch_params(["C:\\a b\\main.pyw", "--x"],
                                       frozen=False)
    assert "C:\\a b\\main.pyw" in params


def test_uac_relaunch_is_unchanged_when_frozen(versioned):
    """frozen 模式本來就丟掉 argv[0]（見 `_admin_relaunch_params` 的說明）。"""
    _app, entry, launcher = versioned
    params = pw._admin_relaunch_params([str(entry), "--x"], frozen=True)
    assert str(entry) not in params and os.path.basename(
        str(launcher)) not in params


# ── 逐一盤點：不可以再有人直接用 sys.argv[0] 重啟 ─────────────────────────
def test_no_self_spawn_site_uses_raw_argv0():
    """★逐一盤點★ 只修看得到的那幾處，下一個新增的地方又會踩同一個坑。

    掃 `src/` 裡所有把 `sys.argv[0]` 當成「要執行的檔」傳出去的呼叫
    （`launch_python_script` / `Popen` / `os.exec*`）。
    允許清單只有 `paths.self_entry_path()` 自己 —— 它就是那個出口。
    """
    import ast
    import glob
    bad = []
    for path in glob.glob(os.path.join(REPO_ROOT, "src", "**", "*.py"),
                          recursive=True):
        with open(path, encoding="utf-8") as fh:
            src = fh.read()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None) or getattr(
                node.func, "attr", None)
            if name not in ("launch_python_script", "Popen", "execv", "execl"):
                continue
            for arg in node.args:
                dumped = ast.dump(arg)
                if "'argv'" in dumped and "'sys'" in dumped:
                    bad.append(f"{os.path.relpath(path, REPO_ROOT)}:"
                               f"{node.lineno} {name}")
    assert bad == [], (
        "這些地方仍然直接拿 sys.argv[0] 去啟動自己（版本化後會鎖在舊版）："
        + ", ".join(bad))


def test_tray_configure_uses_self_entry():
    """托盤『設定』：舊版 UI 寫新版 settings 是最難查的那種壞。"""
    import ast
    import inspect
    import textwrap
    import importlib
    cq = importlib.import_module("consult_query")
    src = textwrap.dedent(inspect.getsource(cq._tray_configure))
    names = {n.func.id for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "self_entry_path" in names, (
        "托盤設定沒有走固定 launcher —— 會用舊版 UI 寫新版 settings")


# ══ 外審 2026-08-09 P2-02：位置對 ≠ 身分對 ═══════════════════════════════
class TestLauncherAllowlist:
    """`CMUH_LAUNCHER` 是【會被繼承】的環境變數，而它的值會被拿去【執行】。

    只驗「存在 + 在 app 根第一層」的話，app 根裡任何一個檔都能通過 ——
    更新器剛下載的檔、被放進來的 `.pyw`、`manifest.json` 都算。
    而 `build_restart_command()` 會執行它，UAC 那條路還會提權。
    """

    @staticmethod
    def _root(tmp_path, monkeypatch):
        root = tmp_path / "app"
        root.mkdir()
        (root / "src").mkdir()
        (root / "src" / "cmuh_common").mkdir()
        (root / "src" / "cmuh_common" / "version.py").write_text(
            "CURRENT_VERSION='0'", encoding="utf-8")
        monkeypatch.setenv(cpaths.APP_DIR_ENV, str(root))
        return root

    def test_a_real_launcher_is_accepted(self, tmp_path, monkeypatch):
        root = self._root(tmp_path, monkeypatch)
        good = root / cpaths.LAUNCHER_NAMES[0]
        good.write_text("", encoding="utf-8")
        monkeypatch.setenv(cpaths.LAUNCHER_ENV, str(good))
        assert cpaths.pinned_launcher() == os.path.realpath(str(good))

    @pytest.mark.parametrize("name", [
        "隨便一支.pyw", "manifest.json", "更新暫存.exe", "main.py",
    ])
    def test_another_file_in_the_same_folder_is_rejected(self, tmp_path,
                                                         monkeypatch, name):
        """★核心★ 這些都在 app 根第一層、都真的存在 —— 位置檢查全部放行。"""
        root = self._root(tmp_path, monkeypatch)
        bogus = root / name
        bogus.write_text("", encoding="utf-8")
        monkeypatch.setenv(cpaths.LAUNCHER_ENV, str(bogus))
        assert cpaths.pinned_launcher() == "", (
            f"★{name} 通過了 → 我們會去執行(甚至提權執行)它★")

    def test_every_shipped_launcher_is_on_the_allowlist(self):
        """★白名單漂掉就等於全部拒絕★ 要跟 repo 裡真的有的那幾支對得上。"""
        import glob
        shipped = {os.path.basename(p)
                   for p in glob.glob(os.path.join(REPO_ROOT, "*.pyw"))}
        assert shipped, "找不到任何 .pyw（測試自己失效了）"
        assert shipped == set(cpaths.LAUNCHER_NAMES), (
            "白名單與實際出貨的啟動器不一致："
            f"少了 {sorted(shipped - set(cpaths.LAUNCHER_NAMES))}，"
            f"多了 {sorted(set(cpaths.LAUNCHER_NAMES) - shipped)}")
