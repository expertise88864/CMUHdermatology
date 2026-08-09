# -*- coding: utf-8 -*-
"""[批次 L・L1] 版本化目錄的【讀取】能力。

設計見 `docs/批次L_版本化目錄與原子切換_設計_2026-08-03.md`。
L1 的驗收條件就一句：**`current.txt` 不存在時，行為與今天完全相同。**

★三種情況不可以摺成一種★
| 情況 | 意思 | 該怎麼辦 |
|---|---|---|
| 沒有 `current.txt` | 過渡期的**正常**狀態 | 安靜走 `<app>/src` |
| 有指標但讀不出來／不安全 | 指標壞了 | 走 `<app>/src`，**留紀錄** |
| 版本目錄不存在／沒有 `.complete` | 半成品或被清掉 | 同上 |

把後兩種當成第一種，就是「安靜地跑舊版、讓人以為更新成功了」。
"""
import glob
import importlib.util
import os
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

_spec = importlib.util.spec_from_file_location(
    "_vp_under_test", os.path.join(REPO_ROOT, "version_pointer.py"))
vp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(vp)


def _app(tmp_path, version=None, *, complete=True, make_src=True,
         pointer_text=None):
    (tmp_path / "src").mkdir()
    if version:
        d = tmp_path / "versions" / version
        d.mkdir(parents=True)
        if make_src:
            (d / "src").mkdir()
        if complete:
            (d / ".complete").write_text("", encoding="utf-8")
    if pointer_text is not None:
        (tmp_path / "current.txt").write_text(pointer_text, encoding="utf-8")
    elif version:
        (tmp_path / "current.txt").write_text(version, encoding="utf-8")
    return str(tmp_path)


def _log(app):
    p = os.path.join(app, vp.LOG_NAME)
    return open(p, encoding="utf-8").read() if os.path.exists(p) else ""


# ── L1 的驗收條件 ─────────────────────────────────────────────────────────
def test_no_pointer_behaves_exactly_like_today(tmp_path):
    """★L1 的全部★ 沒有 current.txt → 走 `<app>/src`，而且【不吵】。"""
    app = _app(tmp_path)
    r = vp.resolve_src(app, "測試")
    assert r.src_dir == os.path.join(app, "src")
    assert r.reason == vp.NO_POINTER
    assert _log(app) == "", "過渡期的正常狀態不該留下警告紀錄（久了就沒人看）"


def test_a_valid_pointer_is_used(tmp_path):
    app = _app(tmp_path, "2026.08.09.9")
    r = vp.resolve_src(app, "測試")
    assert r.src_dir == os.path.join(app, "versions", "2026.08.09.9", "src")
    assert (r.version, r.reason) == ("2026.08.09.9", vp.PINNED)
    assert _log(app) == ""


# ── 「指標存在但用不了」的三種，全部要回退【而且留紀錄】───────────────────
@pytest.mark.parametrize("kw,expect", [
    (dict(version="2026.08.09.9", complete=False), vp.INCOMPLETE),
    (dict(version="2026.08.09.9", make_src=False), vp.VERSION_MISSING),
    (dict(pointer_text="  \n"), vp.POINTER_UNREADABLE),
    (dict(pointer_text="../../evil"), vp.UNSAFE_VERSION),
    (dict(pointer_text="C:\\windows"), vp.UNSAFE_VERSION),
])
def test_a_broken_pointer_falls_back_loudly(tmp_path, kw, expect):
    app = _app(tmp_path, **kw)
    r = vp.resolve_src(app, "測試")
    assert r.src_dir == os.path.join(app, "src"), "沒有回退到舊的 src"
    assert r.reason == expect
    assert "退回舊的" in _log(app), (
        f"★安靜地跑舊版★（{expect}）—— 人會以為更新成功了")


def test_an_incomplete_version_is_never_used_even_if_src_exists(tmp_path):
    """★裝到一半的版本目錄絕對不可以被指到★（`.complete` 是唯一判準）"""
    app = _app(tmp_path, "2026.08.09.9", complete=False)
    assert os.path.isdir(os.path.join(app, "versions", "2026.08.09.9", "src"))
    assert vp.resolve_src(app, "測試").reason == vp.INCOMPLETE


# ── 路徑穿越 ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("bad", [
    "..", ".", "../x", "a/b", "a\\b", "C:\\x", "x" * 65, "", "  ",
    "ver;rm", "ver name", "ver\x00",
])
def test_unsafe_versions_are_rejected(bad):
    assert vp.is_safe_version(bad) is False, bad


@pytest.mark.parametrize("ok", ["2026.08.09.3", "1.0", "a-b_c", "V2"])
def test_safe_versions_are_accepted(ok):
    assert vp.is_safe_version(ok) is True, ok


def test_the_reason_set_is_closed():
    """`reason` 是封閉集合 —— 呼叫端才能分流，不必比對字串長相。"""
    known = {vp.PINNED, vp.NO_POINTER, vp.POINTER_UNREADABLE,
             vp.UNSAFE_VERSION, vp.VERSION_MISSING, vp.INCOMPLETE}
    assert vp.EXPECTED_REASONS <= known
    assert vp.EXPECTED_REASONS == {vp.PINNED, vp.NO_POINTER}, (
        "只有『用了版本目錄』與『過渡期沒有指標』是預期狀態")


# ── 這支自己不可以擋住開機 ────────────────────────────────────────────────
def test_it_never_raises_even_when_everything_is_broken(tmp_path, monkeypatch):
    """★六支程式的共同單點★ 任何失敗都要有出口。"""
    app = _app(tmp_path, "2026.08.09.9")

    def _boom(*a, **k):
        raise OSError("磁碟壞了")

    monkeypatch.setattr(vp.os.path, "isdir", _boom)
    monkeypatch.setattr(vp.os.path, "exists", _boom)
    r = vp.resolve_src(app, "測試")
    assert r.src_dir == os.path.join(app, "src")


def test_a_log_that_cannot_be_written_does_not_break_startup(tmp_path,
                                                             monkeypatch):
    app = _app(tmp_path, pointer_text="../../evil")
    monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(
        OSError("唯讀")))
    r = vp.resolve_src(app, "測試")          # 不可以往上拋
    assert r.reason in (vp.POINTER_UNREADABLE, vp.UNSAFE_VERSION)


def test_it_imports_nothing_from_the_project():
    """★不可以 import 專案模組★ 它要回答的正是「該載入哪一棵 src」。"""
    import ast
    with open(os.path.join(REPO_ROOT, "version_pointer.py"),
              encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.add(n.module.split(".")[0])
    assert mods <= {"__future__", "datetime", "os", "collections"}, (
        f"引入了非標準庫或專案模組：{sorted(mods)}")


# ── 六支 stub ────────────────────────────────────────────────────────────
_STUBS = sorted(glob.glob(os.path.join(REPO_ROOT, "*.pyw")))


def test_there_are_six_stubs():
    assert len(_STUBS) == 6, f"啟動器數量變了：{[os.path.basename(p) for p in _STUBS]}"


def _stub_block(path):
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    # ★區塊從 `_note_resolver_failure` 開始★（外審 P1-03 之後 resolver 會呼叫它）
    #   只從 `_resolve_src` 開始擷取的話，exec 這段會 NameError —— 測到的是
    #   我的擷取範圍，不是被測的行為。
    start = src.index("def _note_resolver_failure(")
    end = src.index("_SRC = _resolve_src()", start)
    return src[start:end]


@pytest.mark.parametrize("path", _STUBS, ids=lambda p: os.path.basename(p))
def test_every_stub_resolves_through_the_pointer(path):
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    assert "_SRC = _resolve_src()" in src, "這支 stub 沒有接上版本解析"
    assert "version_pointer.py" in src


def test_all_six_resolver_blocks_are_byte_identical():
    """★六份複製品必須逐字相同★

    這一段是六支程式的共同單點，而且是「切版本救不回來」的檔。分岔的樣子是
    「有兩支程式跑舊版、四支跑新版」，而且沒有任何地方會說出來。
    """
    blocks = {os.path.basename(p): _stub_block(p) for p in _STUBS}
    uniq = set(blocks.values())
    assert len(uniq) == 1, (
        "六支 stub 的解析區塊不一致：" + ", ".join(sorted(blocks)))


@pytest.mark.parametrize("path", _STUBS, ids=lambda p: os.path.basename(p))
def test_a_stub_falls_back_when_the_pointer_module_is_missing(path, tmp_path):
    """★半套更新也要開得起來★

    stub 與 `version_pointer.py` 都是就地更新的檔，一次更新可能只換到一半。
    那時仍然要走 `<app>/src`，不可以讓六支程式一起起不來。
    """
    src = _stub_block(path)
    ns = {"os": os, "_HERE": str(tmp_path), "_PROGRAM": "t"}
    exec(src + "\n_SRC = _resolve_src()\n", ns)      # noqa: S102
    assert ns["_SRC"] == os.path.join(str(tmp_path), "src")


def test_the_stub_does_not_put_the_app_root_on_sys_path():
    """★根目錄不可以進 sys.path★ 它會永久參與所有 import 解析。

    ★用 AST，不要掃字串★ 這個區塊的 docstring 裡本來就寫著 `sys.path`
    （在解釋為什麼不用它）—— 掃字串會紅在自己的散文上。第一版就是這樣。
    """
    import ast
    tree = ast.parse(_stub_block(_STUBS[0]))
    touched = [n for n in ast.walk(tree)
               if isinstance(n, ast.Attribute) and n.attr == "path"
               and isinstance(n.value, ast.Name) and n.value.id == "sys"]
    assert touched == [], "解析區塊動了 sys.path —— 請用 spec_from_file_location"
    calls = {n.func.attr for n in ast.walk(tree)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "spec_from_file_location" in calls


def test_the_pointer_module_is_shipped_to_the_clinic():
    """★接上去了才算數★

    `version_pointer.py` 若不在 manifest 的 extras 裡，它永遠不會送到診間 ——
    六支 stub 每一次都走 fallback，這個功能等於不存在，而且**沒有任何地方會
    說出來**（fallback 本來就是安靜的正常狀態）。
    """
    import ast
    with open(os.path.join(REPO_ROOT, "scripts", "sync_manifest.py"),
              encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    listed = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign)
                and any(getattr(t, "id", "") == "extra_files"
                        for t in node.targets)
                and isinstance(node.value, ast.List)):
            listed = {e.value for e in node.value.elts
                      if isinstance(e, ast.Constant)}
    assert listed, "找不到 extra_files 清單（測試自己失效了）"
    assert "version_pointer.py" in listed, (
        "★version_pointer.py 沒有被送到診間★ —— stub 會永遠走 fallback")
    for stub in _STUBS:
        assert os.path.basename(stub) in listed, (
            f"{os.path.basename(stub)} 不在 extras —— 新的 stub 不會送出去")


# ── 外審 P1：版本化之後路徑會整個歪掉 ─────────────────────────────────────
# `runpy.run_path` **會**把 `sys.argv[0]` 換成被執行的那支源碼（CPython 的
# `_ModifiedArgv0`）。我原本以為它不會 —— 實測推翻了我的假設，兩個 P1 都成立：
#   * `get_app_dir()` 的「src 的父層」推導 → `<app>/versions/<V>`，
#     settings/log/assets 全部跑進版本目錄。★切一次版＝所有設定都不見了★
#   * `restart_self()` 直接重跑 `versions/<V1>/src/...` → 永遠不再讀 current.txt。
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))
from cmuh_common import paths as cpaths  # noqa: E402


@pytest.fixture
def versioned(tmp_path, monkeypatch, real_get_app_dir):
    """模擬「已經在跑版本化目錄」的狀態。

    ★要用 `real_get_app_dir`★ conftest 有一個 autouse 夾具把 `get_app_dir()`
    整個導向 per-test 的 `tmp/_cmuh_app`（為了測試隔離）。不拿正版函式的話，
    這幾條測到的是那個假的，而外審指出的正是「真正的推導邏輯會歪掉」——
    量錯對象的測試通過與否都不代表任何事。
    """
    app = tmp_path
    entry = app / "versions" / "2026.08.09.9" / "src" / "main.py"
    entry.parent.mkdir(parents=True)
    entry.write_text("", encoding="utf-8")
    # ★要長得像真正的 src★ `_looks_like_src_dir` 認的是
    #   `cmuh_common/version.py`。少了它，述詞不成立、走的是另一條分支 ——
    #   量到的就不是外審指出的那個推導。
    (entry.parent / "cmuh_common").mkdir()
    (entry.parent / "cmuh_common" / "version.py").write_text("", encoding="utf-8")
    (app / "src").mkdir()
    launcher = app / "中國醫皮膚科主程式.pyw"
    launcher.write_text("", encoding="utf-8")
    monkeypatch.setattr(cpaths.sys, "argv", [str(entry)])
    monkeypatch.setattr(cpaths, "is_frozen", lambda: False)
    monkeypatch.delenv(cpaths.APP_DIR_ENV, raising=False)
    monkeypatch.delenv(cpaths.LAUNCHER_ENV, raising=False)
    return app, entry, launcher, real_get_app_dir


def test_without_the_pin_the_app_root_is_wrong(versioned, monkeypatch):
    """★先證明這個缺陷是真的★（否則下面那條測的是一個不存在的問題）"""
    app, _entry, _l, real = versioned
    assert real() == str(app / "versions" / "2026.08.09.9"), (
        "推導出來的根目錄竟然是對的 —— 這條測試的前提要重新檢查")


def test_the_pinned_app_dir_wins(versioned, monkeypatch):
    app, _entry, _l, real = versioned
    monkeypatch.setenv(cpaths.APP_DIR_ENV, str(app))
    assert real() == str(app)


def test_settings_stay_outside_the_version_directory(versioned, monkeypatch):
    """★設計明訂 settings 不隨版本走★ 切版本不可以把設定弄丟。"""
    app, _entry, _l, real = versioned
    monkeypatch.setenv(cpaths.APP_DIR_ENV, str(app))
    d = os.path.join(real(), "settings")
    assert "versions" not in d, f"設定跑進版本目錄了：{d}"
    assert d.startswith(str(app))


def test_a_bogus_pin_does_not_break_everything(versioned, monkeypatch):
    """★壞掉的環境變數不可以讓程式找不到設定★ 只信真的存在的目錄。"""
    app, _entry, _l, real = versioned
    monkeypatch.setenv(cpaths.APP_DIR_ENV, str(app / "nope"))
    assert real() == str(app / "versions" / "2026.08.09.9")
    monkeypatch.setenv(cpaths.APP_DIR_ENV, "   ")
    assert real() == str(app / "versions" / "2026.08.09.9")


def test_restart_goes_through_the_fixed_launcher(versioned, monkeypatch):
    """★重啟要重新讀 current.txt★ 否則更新後永遠停在舊版。

    ★測 `build_restart_command`，不要呼叫 `restart_self()`★
    後者結尾會 `os._exit()` —— 在測試裡呼叫它會【殺掉整個 pytest 行程】
    （我第一版就是這樣寫的：輸出停在 `FF.F` 之後什麼都沒有）。
    """
    app, entry, launcher, _r = versioned
    # ★[外審 P2] 兩個值是【一組】的★ 這條測試原本只設 LAUNCHER_ENV，
    #   把「沒有可信的根也接受 launcher」這個缺陷【釘成了通過條件】——
    #   守衛後來改成 fail-closed 時，紅的是這條測試而不是程式。
    monkeypatch.setenv(cpaths.APP_DIR_ENV, str(app))
    monkeypatch.setenv(cpaths.LAUNCHER_ENV, str(launcher))
    cmd = cpaths.build_restart_command()
    assert str(launcher) in cmd, f"重啟走的不是固定啟動器：{cmd}"
    assert str(entry) not in cmd, "★重啟仍指向舊版本的源碼★"


def test_restart_falls_back_when_the_launcher_is_unknown(versioned):
    """★沒有釘住就照舊★（過渡期／直接跑 src 的情境不可以壞掉）"""
    _app, entry, _l, _r = versioned
    assert str(entry) in cpaths.build_restart_command()


def test_a_bogus_launcher_pin_falls_back(versioned, monkeypatch):
    """釘到一個不存在的檔 → 照舊，不可以組出一條跑不起來的命令。"""
    app, entry, _l, _r = versioned
    monkeypatch.setenv(cpaths.LAUNCHER_ENV, str(app / "nope.pyw"))
    assert str(entry) in cpaths.build_restart_command()


def test_restart_self_uses_the_shared_builder():
    """★抽出來之後要真的被用★ 否則測的是一條沒人走的路。"""
    import ast
    import inspect
    import textwrap
    src = textwrap.dedent(inspect.getsource(cpaths.restart_self))
    names = {n.func.id for n in ast.walk(ast.parse(src))
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "build_restart_command" in names, (
        "restart_self 沒有走共用的組指令函式 —— 兩邊會漂")


def test_every_stub_pins_both_values():
    """★六支都要釘★ 漏一支＝那支程式的設定會跑進版本目錄。"""
    import ast
    for path in _STUBS:
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        assigned = set()
        for n in ast.walk(tree):
            if (isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store)
                    and isinstance(n.slice, ast.Constant)):
                assigned.add(n.slice.value)
        assert {"CMUH_APP_DIR", "CMUH_LAUNCHER"} <= assigned, (
            f"{os.path.basename(path)} 沒有把根目錄/啟動器釘進環境：{sorted(assigned)}")


def test_the_env_names_match_between_stub_and_paths():
    """字串常數兩邊要一致 —— 打錯字的樣子是「釘了但沒人讀」。"""
    with open(_STUBS[0], encoding="utf-8") as fh:
        src = fh.read()
    assert cpaths.APP_DIR_ENV in src and cpaths.LAUNCHER_ENV in src


# ── 外審第 2 輪 P1：watchdog 兩支也要走釘住的根目錄 ───────────────────────
# 版本化之後它們的 `__file__` 在 `<app>/versions/<V>/src/...`，推出來的根是
# `<app>/versions/<V>` —— watchdog 會讀到【另一份】設定與鎖，而且到版本目錄底下
# 找六支 `.pyw`（那裡沒有）。★後果是 watchdog 再也救不回任何一支臨床程式★，
# 而它正是最後一道防線。
def test_pinned_app_dir_only_trusts_a_real_directory(tmp_path, monkeypatch):
    monkeypatch.delenv(cpaths.APP_DIR_ENV, raising=False)
    assert cpaths.pinned_app_dir() == ""
    monkeypatch.setenv(cpaths.APP_DIR_ENV, str(tmp_path / "nope"))
    assert cpaths.pinned_app_dir() == "", "釘到不存在的目錄竟然被採用"
    monkeypatch.setenv(cpaths.APP_DIR_ENV, "   ")
    assert cpaths.pinned_app_dir() == ""
    monkeypatch.setenv(cpaths.APP_DIR_ENV, str(tmp_path))
    assert cpaths.pinned_app_dir() == str(tmp_path)


def test_watchdog_core_derives_root_from_the_pin():
    """★watchdog_core 的 `_ROOT` 必須走 `pinned_app_dir()`★"""
    import ast
    import inspect
    from cmuh_common import watchdog_core
    tree = ast.parse(inspect.getsource(watchdog_core))
    root_assign = None
    for n in ast.walk(tree):
        if (isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == "_ROOT" for t in n.targets)):
            root_assign = ast.dump(n.value)
    assert root_assign, "找不到 _ROOT 的指派（測試自己失效了）"
    assert "pinned_app_dir" in root_assign, (
        f"watchdog_core 仍然只用 __file__ 推根目錄：{root_assign}")


def test_watchdog_runner_uses_the_same_env_name():
    """★字串常數兩邊要一致★ 打錯字的樣子是「釘了但沒人讀」。

    `watchdog_runner` 跑在 `cmuh_common` 可 import 之前，只能寫死字串 ——
    所以這裡逐字比對它與 `paths.APP_DIR_ENV`。
    """
    import ast
    with open(os.path.join(REPO_ROOT, "src", "watchdog_runner.py"),
              encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)
    root_assign = None
    for n in ast.walk(tree):
        if (isinstance(n, ast.Assign)
                and any(getattr(t, "id", "") == "_ROOT" for t in n.targets)):
            root_assign = ast.dump(n.value)
    assert root_assign and "_PINNED_ROOT" in root_assign, (
        f"watchdog_runner 仍然只用 __file__ 推根目錄：{root_assign}")
    consts = {n.value for n in ast.walk(tree)
              if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert cpaths.APP_DIR_ENV in consts, (
        f"環境變數名稱與 paths.APP_DIR_ENV（{cpaths.APP_DIR_ENV}）不一致")


def test_watchdog_root_lands_on_the_app_dir(tmp_path, monkeypatch):
    """★端對端★ 釘住之後，重新載入的 watchdog_core 根目錄要是 `<app>`。"""
    import importlib
    monkeypatch.setenv(cpaths.APP_DIR_ENV, str(tmp_path))
    from cmuh_common import watchdog_core
    reloaded = importlib.reload(watchdog_core)
    try:
        assert str(reloaded._ROOT) == str(tmp_path)
        assert str(reloaded.SETTINGS_DIR) == os.path.join(str(tmp_path),
                                                          "settings")
        assert "versions" not in str(reloaded.CONFIG_PATH)
    finally:
        monkeypatch.delenv(cpaths.APP_DIR_ENV, raising=False)
        importlib.reload(watchdog_core)


# ── 外審 P1-03：resolver 自己壞掉不可以是靜默的 ───────────────────────────
def _run_stub_block(path, app_dir):
    src = _stub_block(path)
    ns = {"os": os, "_HERE": str(app_dir), "_PROGRAM": "t"}
    exec(src + chr(10) + "_SRC = _resolve_src()" + chr(10), ns)   # noqa: S102
    return ns["_SRC"]


@pytest.mark.parametrize("path", _STUBS, ids=lambda p: os.path.basename(p))
def test_a_missing_pointer_module_stays_quiet(path, tmp_path):
    """★過渡期的正常狀態★ 這個檔還沒送到這台機器 → 不留警告紀錄。"""
    (tmp_path / "src").mkdir()
    assert _run_stub_block(path, tmp_path) == os.path.join(str(tmp_path), "src")
    assert not os.path.exists(os.path.join(str(tmp_path), vp.LOG_NAME)), (
        "檔案還沒送到就吵，久了 log 就沒人看")


@pytest.mark.parametrize("path", _STUBS, ids=lambda p: os.path.basename(p))
def test_a_broken_pointer_module_is_never_silent(path, tmp_path):
    """★核心（外審 P1-03）★ resolver 自己壞掉 → 一定要留下紀錄。

    我在 `version_pointer.py` 的文件裡寫著「『沒有指標』與『指標壞了』不可以
    摺成一種」，然後在 stub 這一層把「resolver 載不進來」摺進了那個安靜的
    正常狀態 —— **實際在跑舊版，而且沒有任何地方說得出來**，
    人看到版本號沒變只會以為更新還沒下來。宣稱與實作不符，而且是我自己的原則。
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "version_pointer.py").write_text(
        "def resolve_src(  # 括號沒關 → 語法錯", encoding="utf-8")
    assert _run_stub_block(path, tmp_path) == os.path.join(str(tmp_path), "src")
    log = os.path.join(str(tmp_path), vp.LOG_NAME)
    assert os.path.exists(log), "★resolver 壞掉卻一個字都沒留★"
    text = open(log, encoding="utf-8").read()
    assert "載入失敗" in text and "不是】新版本" in text, text


@pytest.mark.parametrize("path", _STUBS, ids=lambda p: os.path.basename(p))
def test_an_unwritable_log_still_starts(path, tmp_path):
    """留不下紀錄也不能擋住開機（六支程式的共同單點）。

    ★用真實的檔案系統條件，不要攔 `builtins.open`★
    攔 `open` 會連 `exec_module` 讀原始碼那一步一起弄壞 —— 那樣測到的是
    「import 機制被我打壞了」，不是「log 寫不進去」。這裡把 log 的路徑做成
    一個【目錄】，開檔寫入必然失敗，而其餘一切照常。
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "version_pointer.py").write_text("raise RuntimeError()",
                                                 encoding="utf-8")
    (tmp_path / vp.LOG_NAME).mkdir()          # 同名目錄 → 寫入必然失敗
    assert _run_stub_block(path, tmp_path) == os.path.join(str(tmp_path), "src")


def test_a_launcher_without_a_trusted_root_is_rejected(versioned, monkeypatch):
    """★[外審 P2] 守衛不可以 no-op★

    `if root and ...` 在沒有（或無效的）`CMUH_APP_DIR` 時會把整個 containment
    檢查跳過，於是【任何存在的檔】都被接受 —— 繼承來的陳舊值就能讓我們去
    重啟別的程式，而 UAC 那條路還會把它提權。
    """
    app, entry, launcher, _r = versioned
    monkeypatch.setenv(cpaths.LAUNCHER_ENV, str(launcher))
    monkeypatch.delenv(cpaths.APP_DIR_ENV, raising=False)
    assert cpaths.pinned_launcher() == "", "沒有可信的根卻接受了 launcher"
    assert str(entry) in cpaths.build_restart_command(), "應該退回照舊行為"
    monkeypatch.setenv(cpaths.APP_DIR_ENV, str(app / "不存在"))
    assert cpaths.pinned_launcher() == "", "無效的根卻接受了 launcher"


@pytest.mark.parametrize("path", _STUBS, ids=lambda p: os.path.basename(p))
def test_a_resolver_raising_filenotfound_is_not_silent(path, tmp_path):
    """★[外審 P1] 用 `except FileNotFoundError` 當「還沒送到」的判準是錯的★

    resolver 存在、但執行時自己去開別的檔失敗，丟的也是 FileNotFoundError。
    那樣一個【壞掉的】resolver 會被當成【還沒送到】，靜默跑舊版。
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "version_pointer.py").write_text(
        "open('絕對不存在的檔案.txt')", encoding="utf-8")
    assert _run_stub_block(path, tmp_path) == os.path.join(str(tmp_path), "src")
    log = os.path.join(str(tmp_path), vp.LOG_NAME)
    assert os.path.exists(log), (
        "★resolver 壞掉卻被當成『還沒送到』而靜默★")
    assert "FileNotFoundError" in open(log, encoding="utf-8").read()


@pytest.mark.parametrize("path", _STUBS, ids=lambda p: os.path.basename(p))
def test_a_directory_named_like_the_resolver_is_never_silent(path, tmp_path):
    """★[外審第 2 輪] `isfile()` 把【部署失敗的痕跡】當成【還沒送到】★

    同名目錄、壞掉的連結 —— `isfile()` 一律回 False。那些正是部署失敗留下的
    東西，卻被摺進「這個檔還沒送到」那個安靜的正常狀態。
    **又一次把看得見的壞變成安靜的壞**，而且是我上一輪的修法造成的。
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "version_pointer.py").mkdir()      # ← 同名目錄
    assert _run_stub_block(path, tmp_path) == os.path.join(str(tmp_path), "src")
    log = os.path.join(str(tmp_path), vp.LOG_NAME)
    assert os.path.exists(log), (
        "★同名目錄（部署失敗的痕跡）被當成『還沒送到』而靜默★")


def test_the_existence_probe_does_not_follow_links():
    """★[外審第 3 輪] `os.stat()` 會跟隨符號連結★

    壞掉的連結照樣丟 `FileNotFoundError` → 又被當成「還沒送到」而靜默，
    而註解才剛把壞連結歸類成【部署失敗的痕跡】。宣稱與實作不符。

    ★這裡用結構性斷言，不做真的 symlink★ Windows 建 symlink 需要權限，
    在沒有權限的機器上那條測試會 skip —— 一條會 skip 的測試守不住任何東西。
    `lstat` vs `stat` 是這個修正的全部內容，直接釘住它。
    """
    import ast
    for path in _STUBS:
        tree = ast.parse(_stub_block(path))
        probes = {n.func.attr for n in ast.walk(tree)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                  and n.func.attr in ("stat", "lstat")}
        assert "lstat" in probes, (
            f"{os.path.basename(path)} 的存在性探測沒有用 lstat")
        assert "stat" not in probes, (
            f"{os.path.basename(path)} 仍在用會跟隨連結的 stat")
