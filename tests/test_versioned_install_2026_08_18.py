# -*- coding: utf-8 -*-
"""[批次L・L2] 更新裝進 `versions/<V>/` 並原子切換 `current.txt`。

設計見 `docs/批次L_版本化目錄與原子切換_設計_2026-08-03.md`。L1(已上線)
做的是【讀取】;L2 是【寫入】那一半:

* `src/` 底下的檔改成裝進一個★全新的版本目錄★(先以【現在真的在跑的】
  那一棵樹為底,再疊上這一批變更的檔 —— 更新器只下載有變的檔,只放變更檔
  的版本目錄根本跑不起來);
* 逐檔回讀驗 SHA256,全過才寫 `.complete`(最後一步);
* ★最後才切指標★,而且切換是單一個 `os.replace`。

失敗的三種長相各自安全:版本目錄裝到一半→整個丟掉、指標沒動;stub 就地
寫失敗→既有 .bak 回滾、指標還沒切;指標切失敗→新版裝好但沒生效(仍跑
舊版,不是壞版)。

★六支 .pyw 與 version_pointer.py 不走這條路★(設計 §4):它們是
Task Scheduler 指的固定路徑,是「切版本救不回來」的唯一單點。
"""
import hashlib
import importlib
import os
import sys

import pytest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

up = importlib.import_module("cmuh_common.updater")
VP_PATH = os.path.join(REPO_ROOT, "version_pointer.py")

V = "2026.08.19.1"


@pytest.fixture()
def app(tmp_path, monkeypatch):
    """一個假的 app 目錄:根目錄有 version_pointer.py 與六支 stub 的替身,
    另有一棵「正在跑的」src。"""
    root = tmp_path / "app"
    (root / "src" / "cmuh_common").mkdir(parents=True)
    (root / "src" / "cmuh_common" / "a.py").write_text("舊 A\n",
                                                       encoding="utf-8")
    (root / "src" / "b.py").write_text("舊 B\n", encoding="utf-8")
    (root / "src" / "__pycache__").mkdir()
    (root / "src" / "__pycache__" / "junk.pyc").write_bytes(b"\x00")
    (root / "中國醫皮膚科主程式.pyw").write_text("舊 stub\n", encoding="utf-8")
    # 真正的 version_pointer.py(單一事實來源,不另寫一份判準)
    (root / "version_pointer.py").write_text(
        open(VP_PATH, encoding="utf-8").read(), encoding="utf-8")
    monkeypatch.setattr(up, "get_app_dir", lambda: str(root))
    monkeypatch.setattr(up, "_running_src_dir", lambda: str(root / "src"))
    return root


def _writes(*pairs):
    """組出 prepared_writes:(key, local_filename, new_ver, content, target)。"""
    out = []
    for local, content in pairs:
        out.append((local, local, V, content, ""))
    return out


def _result(app_version=V):
    r = up.UpdateResult()
    r.manifest_app_version = app_version
    return r


def _install(app, writes, result=None):
    res = result or _result()
    vp = up._load_version_pointer(str(app))
    ok = up._install_versioned_src(str(app), V, writes, vp, res)
    return ok, vp, res


class TestTheVersionDirectoryIsAWholeTree:

    def test_it_copies_the_running_tree_and_overlays_the_changes(self, app):
        """★整棵樹★:更新器只下載有變的檔 —— 版本目錄要先以現行樹為底。"""
        ok, _vp, _res = _install(app, _writes(("src/b.py", "新 B\n")))
        assert ok
        vsrc = app / "versions" / V / "src"
        assert (vsrc / "b.py").read_text(encoding="utf-8") == "新 B\n"
        assert (vsrc / "cmuh_common" / "a.py").read_text(
            encoding="utf-8") == "舊 A\n", \
            "★只放變更檔★ 那個版本目錄根本跑不起來"
        assert not (vsrc / "__pycache__").exists(), "不要把 pyc 一起搬過去"

    def test_the_complete_marker_is_written_last(self, app):
        """`.complete` 是最後一步 —— 沒有它的版本目錄一律是半成品。"""
        ok, vp, _res = _install(app, _writes(("src/b.py", "新 B\n")))
        assert ok and vp.is_complete(str(app), V)
        marker = app / "versions" / V / vp.COMPLETE_MARKER
        assert marker.read_text(encoding="utf-8").strip() == V

    def test_a_half_built_directory_is_thrown_away(self, app, monkeypatch):
        """裝到一半失敗 → ★整個版本目錄丟掉、指標不動★(正式檔沒被碰過)。"""
        bad = _writes(("src/b.py", "新 B\n"))
        real = up._sha256_local_file
        monkeypatch.setattr(up, "_sha256_local_file",
                            lambda p: "不是我要的 SHA")
        ok, vp, res = _install(app, bad)
        monkeypatch.setattr(up, "_sha256_local_file", real)
        assert not ok and res.errors
        assert not (app / "versions" / V).exists(), "半成品要清掉,不可以留著"
        assert not (app / vp.POINTER_NAME).exists(), "指標一個位元組都不該動"

    def test_a_stale_version_directory_is_rebuilt_not_merged(self, app):
        """同版本號重裝:整個丟掉重來,不可以疊在半成品上(混版)。"""
        stale = app / "versions" / V / "src"
        stale.mkdir(parents=True)
        (stale / "殘留.py").write_text("上一次裝到一半\n", encoding="utf-8")
        ok, _vp, _res = _install(app, _writes(("src/b.py", "新 B\n")))
        assert ok
        assert not (app / "versions" / V / "src" / "殘留.py").exists()


class TestTheSwitchIsTheLastStep:

    def test_the_pointer_moves_only_after_a_complete_install(self, app):
        ok, vp, _res = _install(app, _writes(("src/b.py", "新 B\n")))
        assert ok
        assert not (app / vp.POINTER_NAME).exists(), "裝好還沒切 → 仍跑舊版"
        assert up._switch_version_pointer(str(app), V, vp)
        assert (app / vp.POINTER_NAME).read_text(
            encoding="utf-8").strip() == V
        res = vp.resolve_src(str(app))
        assert res.version == V and res.reason == vp.PINNED
        assert res.src_dir == str(app / "versions" / V / "src")

    def test_a_failed_switch_leaves_the_old_version_running(self, app,
                                                            monkeypatch):
        """切不過去 = 新版裝好但沒生效(仍跑舊版)—— 不是壞版,下一輪再切。"""
        ok, vp, _res = _install(app, _writes(("src/b.py", "新 B\n")))
        assert ok

        def _boom(*_a, **_k):
            raise OSError("磁碟這一刻不給換")
        monkeypatch.setattr(up.os, "replace", _boom)
        assert up._switch_version_pointer(str(app), V, vp) is False
        monkeypatch.undo()
        assert not (app / vp.POINTER_NAME).exists()
        assert vp.resolve_src(str(app)).reason == vp.NO_POINTER, \
            "★沒有指標是過渡期的正常狀態★ 仍安靜走 <app>/src"
        assert not (app / (vp.POINTER_NAME + ".tmp")).exists(), "暫存要清掉"


class TestTheWholeCommitPath:

    def test_a_successful_commit_installs_switches_and_keeps_stubs(self, app):
        """★端到端★ 一次成功的 commit:src 進版本目錄、stub 就地換、
        指標最後才切 —— 三件事都要真的發生。"""
        writes = [
            ("src/b.py", "src/b.py", V, "新 B\n", str(app / "src" / "b.py")),
            ("中國醫皮膚科主程式.pyw", "中國醫皮膚科主程式.pyw", V, "新 stub\n",
             str(app / "中國醫皮膚科主程式.pyw")),
        ]
        res = up._commit_pending_writes(writes, _result())
        assert not res.errors, res.errors
        vp = up._load_version_pointer(str(app))
        # ① src 進了版本目錄,★沒有★就地覆蓋 <app>/src(L2 期間它是回退用的)
        assert (app / "versions" / V / "src" / "b.py").read_text(
            encoding="utf-8") == "新 B\n"
        assert (app / "src" / "b.py").read_text(encoding="utf-8") == "舊 B\n", \
            "★src 又被就地覆蓋一次★ 那就沒有一鍵回退了"
        # ② stub 就地換掉(它是「切版本救不回來」的檔)
        assert (app / "中國醫皮膚科主程式.pyw").read_text(
            encoding="utf-8") == "新 stub\n"
        # ③ 指標切過去了,而且解析得到那棵新的樹
        assert (app / vp.POINTER_NAME).read_text(
            encoding="utf-8").strip() == V
        assert vp.resolve_src(str(app)).src_dir == \
            str(app / "versions" / V / "src")
        assert res.has_update and len(res.updated_files) == 2

    def test_a_failed_versioned_install_touches_nothing(self, app,
                                                        monkeypatch):
        """版本目錄裝失敗 → ★整批放棄★:stub 不動、指標不動、src 不動。"""
        monkeypatch.setattr(up, "_sha256_local_file", lambda p: "壞掉的 SHA")
        writes = [
            ("src/b.py", "src/b.py", V, "新 B\n", str(app / "src" / "b.py")),
            ("中國醫皮膚科主程式.pyw", "中國醫皮膚科主程式.pyw", V, "新 stub\n",
             str(app / "中國醫皮膚科主程式.pyw")),
        ]
        res = up._commit_pending_writes(writes, _result())
        assert res.errors and not res.has_update
        assert (app / "中國醫皮膚科主程式.pyw").read_text(
            encoding="utf-8") == "舊 stub\n", "★stub 已經被換掉了★"
        assert not (app / "current.txt").exists()
        assert not (app / "versions" / V).exists()


class TestOldVersionsArePruned:

    def test_it_keeps_the_recent_ones_and_never_the_current(self, app):
        vroot = app / "versions"
        made = []
        for i in range(6):
            name = f"2026.08.1{i}.1"
            (vroot / name / "src").mkdir(parents=True)
            os.utime(vroot / name, (1000 + i, 1000 + i))
            made.append(name)
        vp = up._load_version_pointer(str(app))
        # 現用的是【最舊】那一個 —— 它永遠不可以被清掉
        up._prune_old_versions(str(app), made[0], vp)
        left = {p.name for p in vroot.iterdir()}
        assert made[0] in left, "★把正在用的版本刪了★ 下次啟動就起不來"
        assert len(left) == up.KEEP_VERSIONS + 1
        assert made[-1] in left and made[-2] in left, "最近的要留著(回退用)"


class TestOnlySrcGoesIntoTheVersionDirectory:

    def test_the_stubs_stay_in_place(self):
        """六支 .pyw 與 version_pointer.py 是「切版本救不回來」的單點,
        必須留在就地更新那條路(有交易日誌 + .bak)。"""
        assert up._is_src_relative("src/cmuh_common/updater.py")
        assert up._is_src_relative("src\\cmuh_common\\updater.py")
        assert not up._is_src_relative("中國醫皮膚科主程式.pyw")
        assert not up._is_src_relative("version_pointer.py")
        assert not up._is_src_relative("manifest.json")
        assert not up._is_src_relative("src")          # 目錄本身不是檔

    def test_the_commit_path_splits_them(self):
        import inspect
        src = inspect.getsource(up._commit_pending_writes)
        i = src.index("_install_versioned_src(")
        j = src.index("_switch_version_pointer(")
        # ★錨在【程式碼】上,不是 docstring 裡同名的字★:docstring 也寫著
        #   「Phase 2」,拿它當錨會量到一個假的順序。
        k = src.index("_make_backup_atomically(")
        assert i < k < j, \
            "★順序必須是「裝版本目錄 → 就地寫 stub → 最後切指標」★"
        assert "not _is_src_relative(w[1])" in src, \
            "★src 的檔進了版本目錄之後就不該再就地覆蓋一次★"


class TestItDegradesToTheOldBehaviour:

    def test_no_resolver_means_in_place_updates(self, app):
        """`version_pointer.py` 還沒送到(或壞了)→ 完全走舊路徑。"""
        (app / "version_pointer.py").unlink()
        assert up._load_version_pointer(str(app)) is None

    def test_an_unsafe_version_string_is_refused(self, app):
        """版本字串是要被拼進路徑的 —— 不安全就不做版本化(不是照做)。"""
        vp = up._load_version_pointer(str(app))
        assert vp is not None
        assert not vp.is_safe_version("../跑出去")
        assert not vp.is_safe_version("")

    def test_the_sha_is_verified_by_reading_the_file_back(self, app):
        """驗證是【回讀磁碟】,不是比對記憶體裡的字串。

        ★用生產的那一對雜湊函式★:`_sha256_local_file` 會把 CRLF 正規化成
        LF(與 `sync_manifest` 同一套演算法)—— 直接拿 `content.encode()`
        比對會在 Windows 上因為換行轉換而永遠不相等,那量到的是測試自己的
        假設,不是被測的性質。
        """
        content = "新 B\n"
        ok, _vp, _res = _install(app, _writes(("src/b.py", content)))
        assert ok
        path = str(app / "versions" / V / "src" / "b.py")
        assert up._sha256_local_file(path) == up._sha256_text(content)
        raw = open(path, "rb").read().replace(b"\r\n", b"\n")
        assert hashlib.sha256(raw).hexdigest() == up._sha256_text(content), \
            "回讀的內容要真的是這一批下載到的內容"


def _make_current(app, version, monkeypatch, *, running=True):
    """把 `versions/<version>` 做成【已安裝且正在跑】的那一棵。"""
    vsrc = app / "versions" / version / "src"
    vsrc.mkdir(parents=True)
    (vsrc / "b.py").write_text("現行 B\n", encoding="utf-8")
    (vsrc / "cmuh_common").mkdir()
    (vsrc / "cmuh_common" / "a.py").write_text("現行 A\n", encoding="utf-8")
    (app / "versions" / version / ".complete").write_text("", encoding="utf-8")
    (app / "current.txt").write_text(version + "\n", encoding="utf-8")
    if running:
        monkeypatch.setattr(up, "_running_src_dir", lambda: str(vsrc))
    return vsrc


class TestTheReadsFollowThePointerToo:
    """L2 之後 `<app>/src` 停在回退點、不再被就地更新 —— 所有「磁碟上現在
    是什麼」的讀取都必須問【指標指著的那一棵】,否則:①SHA 比對永遠判定
    落後(每輪重抓同樣的檔);②降版守衛拿舊版本號比,會放行把指標切回更舊
    的版本。"""

    def test_the_sha_shortcut_compares_the_installed_tree(self, app,
                                                          monkeypatch):
        vsrc = _make_current(app, V, monkeypatch)
        (vsrc / "b.py").write_text("已經是最新\n", encoding="utf-8")
        entry = {
            "key": "b", "remote_path": "src/b.py", "local_filename": "src/b.py",
            "version": "2099.01.01.1",
            "sha256": up._sha256_text("已經是最新\n"),
        }

        def _boom(*_a, **_k):
            raise AssertionError("★又去下載了★ 表示比對的是回退點那一棵")

        monkeypatch.setattr(up.requests, "get", _boom)
        assert up._download_one(entry, str(app)) is None

    def test_the_downgrade_guard_reads_the_installed_tree(self, app,
                                                          monkeypatch):
        (app / "src" / "cmuh_common" / "version.py").write_text(
            'CURRENT_VERSION = "2026.01.01.1"\n', encoding="utf-8")
        vsrc = _make_current(app, V, monkeypatch)
        (vsrc / "cmuh_common" / "version.py").write_text(
            'CURRENT_VERSION = "2026.08.19.1"\n', encoding="utf-8")
        assert up._read_ondisk_app_version_ex(str(app)) == (V, "ok")

    def test_it_falls_back_to_the_legacy_tree_without_a_pointer(self, app):
        assert up._installed_src_dir(str(app)) == str(app / "src")
        assert up._local_read_path(str(app), "src/b.py") == \
            str(app / "src" / "b.py")
        assert up._local_read_path(str(app), "中國醫皮膚科主程式.pyw") == \
            str(app / "中國醫皮膚科主程式.pyw"), "根目錄的檔不受指標影響"


class TestTheRunningTreeIsNeverTheInstallTarget:
    """★同一個版本號會需要重裝★(SHA 對不上、上一輪半途失敗),而安裝的
    第一步是把目標目錄整個刪掉重建 —— 若那正是現行/正在跑的那一棵,等於
    把自己的來源刪掉:安裝當場失敗,`current.txt` 指著一個不存在的版本,
    現行行程之後的 lazy import 也會炸。所以要換一個不可變的識別碼。"""

    def test_a_same_version_reinstall_does_not_touch_the_running_tree(
            self, app, monkeypatch):
        vsrc = _make_current(app, V, monkeypatch)
        writes = [("src/b.py", "src/b.py", V, "新 B\n",
                   str(app / "src" / "b.py"))]
        res = up._commit_pending_writes(writes, _result())
        assert not res.errors, res.errors
        assert (vsrc / "b.py").read_text(encoding="utf-8") == "現行 B\n", \
            "★把正在跑的那一棵刪掉重建了★"
        assert (app / "current.txt").read_text(encoding="utf-8").strip() == \
            V + ".r2"
        assert (app / "versions" / (V + ".r2") / "src" / "b.py").read_text(
            encoding="utf-8") == "新 B\n"
        # 而且是以【現在真的在跑的】那一棵為底,不是 <app>/src
        assert (app / "versions" / (V + ".r2") / "src" / "cmuh_common"
                / "a.py").read_text(encoding="utf-8") == "現行 A\n"

    def test_the_running_tree_is_skipped_even_if_the_pointer_says_otherwise(
            self, app, monkeypatch):
        """指標與現實不一致時(剛切完還沒重啟)兩棵都要避開。"""
        _make_current(app, V, monkeypatch, running=False)
        other = app / "versions" / (V + ".r2") / "src"
        other.mkdir(parents=True)
        monkeypatch.setattr(up, "_running_src_dir", lambda: str(other))
        vp = up._load_version_pointer(str(app))
        assert up._pick_version_dir(str(app), V, vp) == V + ".r3"

    def test_a_version_nobody_is_using_keeps_its_own_name(self, app):
        vp = up._load_version_pointer(str(app))
        assert up._pick_version_dir(str(app), V, vp) == V

    def test_calling_it_without_the_resolved_path_still_probes_strictly(
            self, app):
        """沒把已解析的路徑傳進來時,這個函式自己要 strict 解析一次 ——
        ★不可以無聲跳過★(那樣忙碌集合會是錯的,而呼叫端毫不知情)。"""

        class _Broken:
            VERSIONS_DIRNAME = "versions"
            COMPLETE_MARKER = ".complete"

            def is_safe_version(self, text):
                return True

            def is_complete(self, app_dir, version):
                return False

            def resolve_src(self, app_dir, program_name=""):
                raise OSError("這台機器上解析不了")

        with pytest.raises(OSError):
            up._pick_version_dir(str(app), V, _Broken())

    def test_the_prune_never_deletes_the_running_tree(self, app, monkeypatch):
        """指標切過去之後,現行行程仍從【舊】那一棵 lazy import ——
        把它清掉不是「下次啟動起不來」,是【現在】就當掉。"""
        vroot = app / "versions"
        running = "2026.08.10.1"
        for i in range(6):
            name = f"2026.08.1{i}.1"
            (vroot / name / "src").mkdir(parents=True)
            os.utime(vroot / name, (1000 + i, 1000 + i))   # running 是最舊的
        monkeypatch.setattr(up, "_running_src_dir",
                            lambda: str(vroot / running / "src"))
        vp = up._load_version_pointer(str(app))
        up._prune_old_versions(str(app), "2026.08.15.1", vp)
        assert (vroot / running).exists(), "★把正在跑的那一棵刪掉了★"


class TestACompleteVersionDirectoryIsImmutable:
    """★這台機器上有六支共用更新器的程式★:寫入鎖只序列化「誰在部署」,
    不代表別人已經停止使用舊目錄。第三支程式可能正跑在 `versions/V` 上,
    而它既不是我的樹、也不是指標指的那一棵 —— 同版重裝若選中它,rmtree
    會把它腳下的來源刪掉(之後任何 lazy import 都當場失敗)。"""

    def test_a_complete_directory_is_never_reused_even_if_nobody_here_uses_it(
            self, app):
        vsrc = app / "versions" / V / "src"
        vsrc.mkdir(parents=True)
        (vsrc / "b.py").write_text("第三支程式正在跑這個\n", encoding="utf-8")
        (app / "versions" / V / ".complete").write_text(V, encoding="utf-8")
        vp = up._load_version_pointer(str(app))
        # 指標不存在、`_running_src_dir` 是 <app>/src —— 兩個「忙碌」判準
        # 都不會涵蓋這一棵,唯一擋得住的是「完整目錄不可變」。
        assert up._pick_version_dir(str(app), V, vp) == V + ".r2"
        writes = [("src/b.py", "src/b.py", V, "新 B\n",
                   str(app / "src" / "b.py"))]
        res = up._commit_pending_writes(writes, _result())
        assert not res.errors, res.errors
        assert (vsrc / "b.py").read_text(
            encoding="utf-8") == "第三支程式正在跑這個\n", \
            "★把別的程式正在用的完整版本目錄刪掉重建了★"

    def test_a_half_built_directory_can_still_be_rebuilt(self, app):
        """沒有 `.complete` 的是半成品:指標不可能指過去,沒有人跑得起來
        —— 它必須可以被重建,否則每次失敗都會多留一個垃圾識別碼。"""
        (app / "versions" / V / "src").mkdir(parents=True)
        vp = up._load_version_pointer(str(app))
        assert up._pick_version_dir(str(app), V, vp) == V


class TestAnAlreadyAppliedBatchIsNotInstalledTwice:
    """我下載時磁碟還缺這些檔,等我拿到鎖時另一支程式可能已經裝好【同一批】
    並切了指標。此時再走一次版本化安裝,只會因為「完整目錄不可變」而開出
    `V.r2`、把指標切到一棵內容相同的樹。"""

    def test_it_reports_the_batch_as_already_on_disk(self, app, monkeypatch):
        vsrc = _make_current(app, V, monkeypatch)
        (vsrc / "b.py").write_text("新 B\n", encoding="utf-8")
        writes = [("b", "src/b.py", V, "新 B\n", str(app / "src" / "b.py"))]
        assert up._installed_batch_is_current(str(app), writes) is True

    def test_one_different_file_is_enough_to_install(self, app, monkeypatch):
        vsrc = _make_current(app, V, monkeypatch)
        (vsrc / "b.py").write_text("新 B\n", encoding="utf-8")
        writes = [
            ("b", "src/b.py", V, "新 B\n", ""),
            ("a", "src/cmuh_common/a.py", V, "還沒有的內容\n", ""),
        ]
        assert up._installed_batch_is_current(str(app), writes) is False

    def test_an_empty_batch_is_not_current(self, app):
        assert up._installed_batch_is_current(str(app), []) is False

    def test_the_locked_branch_checks_before_committing(self):
        """★錨在程式碼上★:這個檢查必須發生在 `_commit_pending_writes`
        之前,否則它擋不掉任何一次重複安裝。"""
        import inspect
        src = inspect.getsource(up.check_and_update)
        i = src.index("_installed_batch_is_current(")
        j = src.index("return _commit_pending_writes(")
        assert i < j


class TestAFailedSwitchDoesNotAskForARestart:
    """呼叫端只看 `need_restart_after_update`,不看 `errors` —— 指標沒切
    成功就重啟,起來讀到的還是舊指標、跑的是同一份程式碼;失敗若持續
    (指標被防毒鎖住)就成了重啟迴圈。"""

    def test_it_reports_no_update_when_the_pointer_did_not_move(
            self, app, monkeypatch):
        monkeypatch.setattr(up, "_switch_version_pointer",
                            lambda *_a, **_k: False)
        writes = [("src/b.py", "src/b.py", V, "新 B\n",
                   str(app / "src" / "b.py"))]
        res = up._commit_pending_writes(writes, _result())
        assert res.errors, "切換失敗要留下紀錄"
        assert not res.has_update
        assert not up.need_restart_after_update(res), \
            "★指標沒動卻要求重啟★ 重啟後跑的是同一份程式碼 → 迴圈"

    def test_a_successful_switch_still_asks_for_the_restart(self, app):
        writes = [("src/b.py", "src/b.py", V, "新 B\n",
                   str(app / "src" / "b.py"))]
        res = up._commit_pending_writes(writes, _result())
        assert res.has_update and up.need_restart_after_update(res)


class TestTheVersionsRootIsContained:
    """這條路徑會 `rmtree`,而主程式可能是提權執行的:`<app>` 底下若被
    放成指向別處的 junction,安裝會寫到那個外部位置、清舊版會遞迴刪除
    那裡的東西。動它之前要先確認它還在程式目錄裡。"""

    def test_a_reparse_point_root_is_refused(self, app, monkeypatch):
        (app / "versions").mkdir()
        monkeypatch.setattr(up, "_is_reparse_point", lambda p: True)
        vp = up._load_version_pointer(str(app))
        assert up._safe_versions_root(str(app), vp) == ""

    def test_a_root_whose_real_path_escaped_is_refused(self, app, monkeypatch,
                                                       tmp_path):
        (app / "versions").mkdir()
        outside = tmp_path / "別的地方"
        outside.mkdir()
        real = os.path.realpath

        def fake_realpath(p):
            if os.path.normcase(str(p)) == os.path.normcase(
                    str(app / "versions")):
                return str(outside)
            return real(p)

        monkeypatch.setattr(os.path, "realpath", fake_realpath)
        vp = up._load_version_pointer(str(app))
        assert up._safe_versions_root(str(app), vp) == ""

    def test_the_install_gives_up_instead_of_writing_outside(self, app,
                                                            monkeypatch):
        (app / "versions").mkdir()
        monkeypatch.setattr(up, "_is_reparse_point", lambda p: True)
        ok, _vp, res = _install(app, _writes(("src/b.py", "新 B\n")))
        assert not ok and res.errors
        assert not (app / "versions" / V).exists()

    def test_the_prune_deletes_nothing_when_the_root_is_unsafe(
            self, app, monkeypatch):
        vroot = app / "versions"
        for i in range(6):
            (vroot / f"2026.08.1{i}.1" / "src").mkdir(parents=True)
        monkeypatch.setattr(up, "_is_reparse_point", lambda p: True)
        vp = up._load_version_pointer(str(app))
        up._prune_old_versions(str(app), "2026.08.10.1", vp)
        assert len(list(vroot.iterdir())) == 6, \
            "★在一個不安全的根底下遞迴刪除★"

    def test_an_unreadable_root_is_refused(self, app, monkeypatch):
        """查不動就當不安全(fail-closed)—— 這條路徑的代價是刪錯東西。"""
        (app / "versions").mkdir()

        def _boom(_p):
            raise OSError("這一刻問不到實體路徑")

        monkeypatch.setattr(os.path, "realpath", _boom)
        vp = up._load_version_pointer(str(app))
        assert up._safe_versions_root(str(app), vp) == ""

    def test_a_normal_root_is_accepted(self, app):
        vp = up._load_version_pointer(str(app))
        assert up._safe_versions_root(str(app), vp) == str(app / "versions")


class TestDiskIsCurrentButIAmNot:
    """★「磁碟已是最新」不等於「我正在跑的是最新」★:L2 之後 SHA 比對的
    對象是指標指著的那一棵。另一支程式裝好新版並切了指標之後,我這一輪
    會零筆待寫 —— 而「別人已更新 → 我要重啟」的判斷在持鎖分支裡,零筆
    待寫【永遠走不到】。那樣六支程式各自檢查更新,卻沒有一支真的換版。"""

    def _arrange(self, app, monkeypatch, installed_version):
        vsrc = _make_current(app, V, monkeypatch)
        (vsrc / "cmuh_common").mkdir(exist_ok=True)
        (vsrc / "cmuh_common" / "version.py").write_text(
            f'CURRENT_VERSION = "{installed_version}"\n', encoding="utf-8")
        monkeypatch.setattr(up, "is_frozen", lambda: False)
        monkeypatch.setattr(up, "_fetch_manifest", lambda: {
            "app_version": "2999.01.01.1",
            "_remote_commit_sha": "a" * 40,
            "files": [{"key": "b", "remote_path": "src/b.py",
                       "local_filename": "src/b.py", "version": "2999.01.01.1",
                       "sha256": up._sha256_text("新 B\n")}],
        })
        monkeypatch.setattr(up, "_download_one", lambda *_a, **_k: None)

    def test_a_newer_installed_tree_asks_for_a_restart(self, app, monkeypatch):
        self._arrange(app, monkeypatch, "2999.01.01.1")
        res = up.check_and_update(write_files=True)
        assert not res.errors, res.errors
        assert res.has_update and up.need_restart_after_update(res), \
            "★磁碟已切到新版、我還跑舊版,卻不要求重啟★ 這支程式會一直跑舊碼"

    def test_the_same_version_asks_for_nothing(self, app, monkeypatch):
        self._arrange(app, monkeypatch, up.CURRENT_VERSION)
        res = up.check_and_update(write_files=True)
        assert not res.has_update and not res.updated_files

    def test_a_same_version_repair_still_asks_for_a_restart(self, app,
                                                            monkeypatch):
        """★版本號相同不代表是同一棵樹★:同版 SHA 修復把指標從
        `versions/V` 切到 `V.r2` —— 兩邊 `version.py` 都寫著同一個版本,
        只比版本號的話沒人會重啟,那支程式就一直跑在【損壞的】舊樹上
        (它正是因為損壞才被修的)。"""
        self._arrange(app, monkeypatch, up.CURRENT_VERSION)   # 指標 → V
        repaired = app / "versions" / (V + ".r2") / "src"
        (repaired / "cmuh_common").mkdir(parents=True)
        (repaired / "cmuh_common" / "version.py").write_text(
            f'CURRENT_VERSION = "{up.CURRENT_VERSION}"\n', encoding="utf-8")
        (app / "versions" / (V + ".r2") / ".complete").write_text(
            V + ".r2", encoding="utf-8")
        (app / "current.txt").write_text(V + ".r2\n", encoding="utf-8")
        # 我還跑在舊的(損壞的)那一棵上
        monkeypatch.setattr(up, "_running_src_dir",
                            lambda: str(app / "versions" / V / "src"))
        res = up.check_and_update(write_files=True)
        assert res.has_update and up.need_restart_after_update(res), \
            "★同版修復切了指標,舊行程卻不重啟★ 它會一直跑在損壞的樹上"

    def test_the_same_tree_is_not_reported_as_stale(self, app, monkeypatch):
        _make_current(app, V, monkeypatch)
        assert up._installed_tree_differs_from_running(str(app)) is False


class TestTheResolverInterfaceIsChecked:
    """載得進來 ≠ 介面完整:截斷或版本不相容的 `version_pointer.py` 語法
    可能合法卻少了某個函式 —— 那時 AttributeError 會從更新流程逸出,而不是
    依約退化成就地更新。"""

    def test_a_name_that_is_not_callable_is_refused(self, app):
        """★名字在 ≠ 叫得動★:`hasattr` 對一個字串也會說有。"""
        (app / "version_pointer.py").write_text(
            "VERSIONS_DIRNAME = 'versions'\n"
            "COMPLETE_MARKER = '.complete'\n"
            "POINTER_NAME = 'current.txt'\n"
            "is_safe_version = '我是字串不是函式'\n"
            "def resolve_src(app_dir, program_name=''):\n    return None\n"
            "def is_complete(app_dir, version):\n    return False\n",
            encoding="utf-8")
        assert up._load_version_pointer(str(app)) is None

    def test_a_resolve_src_that_is_not_callable_is_refused(self, app):
        """★這一條只有 callable 檢查擋得住★:`is_safe_version` 自己完全
        正常(行為探測會過),壞掉的是後面才會被呼叫到的 `resolve_src` ——
        那時例外會從讀取端/安裝端逸出,而不是退化成就地更新。"""
        (app / "version_pointer.py").write_text(
            "VERSIONS_DIRNAME = 'versions'\n"
            "COMPLETE_MARKER = '.complete'\n"
            "POINTER_NAME = 'current.txt'\n"
            "def is_safe_version(text):\n"
            "    import re\n"
            "    return bool(re.fullmatch(r'[0-9A-Za-z._-]+', str(text)))\n"
            "resolve_src = '我是字串不是函式'\n"
            "def is_complete(app_dir, version):\n    return False\n",
            encoding="utf-8")
        assert up._load_version_pointer(str(app)) is None

    def test_a_resolve_src_that_raises_is_refused(self, app):
        """`resolve_src` 是整個機制的核心,callable 只說得出「叫得動」——
        要★實際解析一次★(stub 開機時做的就是這件事)。"""
        (app / "version_pointer.py").write_text(
            "VERSIONS_DIRNAME = 'versions'\n"
            "COMPLETE_MARKER = '.complete'\n"
            "POINTER_NAME = 'current.txt'\n"
            "def is_safe_version(text):\n"
            "    import re\n"
            "    return bool(re.fullmatch(r'[0-9A-Za-z._-]+', str(text)))\n"
            "def resolve_src(app_dir, program_name=''):\n"
            "    raise OSError('這台機器上解析不了')\n"
            "def is_complete(app_dir, version):\n    return False\n",
            encoding="utf-8")
        assert up._load_version_pointer(str(app)) is None

    def test_a_resolve_src_that_breaks_the_contract_is_refused(self, app):
        (app / "version_pointer.py").write_text(
            "VERSIONS_DIRNAME = 'versions'\n"
            "COMPLETE_MARKER = '.complete'\n"
            "POINTER_NAME = 'current.txt'\n"
            "def is_safe_version(text):\n"
            "    import re\n"
            "    return bool(re.fullmatch(r'[0-9A-Za-z._-]+', str(text)))\n"
            "def resolve_src(app_dir, program_name=''):\n"
            "    return None\n"          # 沒有 src_dir
            "def is_complete(app_dir, version):\n    return False\n",
            encoding="utf-8")
        assert up._load_version_pointer(str(app)) is None

    def test_a_resolve_src_breaking_later_forces_the_in_place_path(
            self, app, monkeypatch):
        """★這一條只有「決定版本化時不吞 resolve_src 的例外」擋得住★:
        介面全對、`is_safe_version` 正常,壞的是 `resolve_src` 本身。
        吞掉的話會裝進版本目錄、切一個【stub 跟不動】的指標 —— stub 那側
        退回未更新的 `<app>/src`,更新於是靜默地永遠不生效。"""
        vp = up._load_version_pointer(str(app))

        class _Broken:
            VERSIONS_DIRNAME = "versions"
            COMPLETE_MARKER = ".complete"
            POINTER_NAME = "current.txt"

            def is_safe_version(self, text):
                return True

            def is_complete(self, app_dir, version):
                return False

            def resolve_src(self, app_dir, program_name=""):
                raise OSError("這台機器上解析不了")

        monkeypatch.setattr(up, "_load_version_pointer", lambda _ad: _Broken())
        writes = [("src/b.py", "src/b.py", V, "新 B\n",
                   str(app / "src" / "b.py"))]
        res = up._commit_pending_writes(writes, _result())
        assert not res.errors, res.errors
        assert (app / "src" / "b.py").read_text(
            encoding="utf-8") == "新 B\n", "退化路徑就是就地更新"
        assert not (app / vp.POINTER_NAME).exists(), \
            "★切了一個沒有人跟得動的指標★"
        assert not (app / "versions" / V).exists()

    def test_an_empty_src_dir_at_commit_time_forces_the_in_place_path(
            self, app, monkeypatch):
        """★寬容版會把 `None` 變成字串 "None" 這種假路徑★:strict 版本要
        認出「回傳不符契約」也是失敗,而不是拿假路徑去比對忙碌狀態。"""
        vp = up._load_version_pointer(str(app))

        class _Empty:
            VERSIONS_DIRNAME = "versions"
            COMPLETE_MARKER = ".complete"
            POINTER_NAME = "current.txt"

            def is_safe_version(self, text):
                return True

            def is_complete(self, app_dir, version):
                return False

            def resolve_src(self, app_dir, program_name=""):
                class _R:
                    src_dir = None
                return _R()

        monkeypatch.setattr(up, "_load_version_pointer", lambda _ad: _Empty())
        writes = [("src/b.py", "src/b.py", V, "新 B\n",
                   str(app / "src" / "b.py"))]
        res = up._commit_pending_writes(writes, _result())
        assert not res.errors, res.errors
        assert (app / "src" / "b.py").read_text(
            encoding="utf-8") == "新 B\n", "退化路徑就是就地更新"
        assert not (app / vp.POINTER_NAME).exists()
        assert not (app / "versions" / V).exists()

    def test_the_commit_boundary_calls_the_strict_resolver_itself(self):
        """★「resolver 在保護區裡被實際呼叫」要在流程上看得見★:靜態讀者
        (含外部審查)不該需要讀進 `_pick_version_dir` 才知道這件事。"""
        import inspect
        src = inspect.getsource(up._commit_pending_writes)
        # ★錨在【那一個】保護區上★:函式裡有好幾個 try(第一個是
        #   `get_app_dir()`)—— 從決定版本化的那一句往回找它所屬的 try。
        k = src.index("vp_mod.is_safe_version(")
        i = src.rindex("try:", 0, k)
        j = src.index("except Exception:", k)
        guarded = src[i:j]
        assert "_strict_installed_src_dir(" in guarded
        assert "_installed_src_dir(" not in guarded.replace(
            "_strict_installed_src_dir(", ""), \
            "★保護區裡不可以用會吞例外的那一版★"

    def test_a_resolver_that_raises_is_refused(self, app):
        (app / "version_pointer.py").write_text(
            "VERSIONS_DIRNAME = 'versions'\n"
            "COMPLETE_MARKER = '.complete'\n"
            "POINTER_NAME = 'current.txt'\n"
            "def is_safe_version(text):\n"
            "    raise RuntimeError('這個實作壞了')\n"
            "def resolve_src(app_dir, program_name=''):\n    return None\n"
            "def is_complete(app_dir, version):\n    return False\n",
            encoding="utf-8")
        assert up._load_version_pointer(str(app)) is None

    def test_a_resolver_whose_answers_are_wrong_is_refused(self, app):
        """判準本身錯掉(什麼都說安全)→ 版本字串會被拼進路徑,不可以用。"""
        (app / "version_pointer.py").write_text(
            "VERSIONS_DIRNAME = 'versions'\n"
            "COMPLETE_MARKER = '.complete'\n"
            "POINTER_NAME = 'current.txt'\n"
            "def is_safe_version(text):\n    return True\n"
            "def resolve_src(app_dir, program_name=''):\n    return None\n"
            "def is_complete(app_dir, version):\n    return False\n",
            encoding="utf-8")
        assert up._load_version_pointer(str(app)) is None

    def test_a_constant_of_the_wrong_type_is_refused(self, app):
        (app / "version_pointer.py").write_text(
            "VERSIONS_DIRNAME = None\n"
            "COMPLETE_MARKER = '.complete'\n"
            "POINTER_NAME = 'current.txt'\n"
            "def is_safe_version(text):\n"
            "    return text == '2026.01.01.1'\n"
            "def resolve_src(app_dir, program_name=''):\n    return None\n"
            "def is_complete(app_dir, version):\n    return False\n",
            encoding="utf-8")
        assert up._load_version_pointer(str(app)) is None

    def test_a_resolver_that_breaks_later_still_degrades_to_in_place(
            self, app, monkeypatch):
        """★驗過的那一刻能用,不代表每一次呼叫都不拋★:契約是【完全退化成
        舊路徑】,所以例外不可以從 commit 逸出、把整批更新變成不明失敗。"""
        vp = up._load_version_pointer(str(app))

        def _boom(_ad):
            class _Broken:
                def __getattr__(self, _name):
                    raise RuntimeError("resolver 這一刻壞了")
            return _Broken()

        monkeypatch.setattr(up, "_load_version_pointer", _boom)
        writes = [("src/b.py", "src/b.py", V, "新 B\n",
                   str(app / "src" / "b.py"))]
        res = up._commit_pending_writes(writes, _result())
        assert not res.errors, res.errors
        assert (app / "src" / "b.py").read_text(
            encoding="utf-8") == "新 B\n", "退化路徑就是就地更新"
        assert not (app / vp.POINTER_NAME).exists()

    def test_a_truncated_resolver_degrades_to_in_place(self, app):
        (app / "version_pointer.py").write_text(
            "VERSIONS_DIRNAME = 'versions'\n"
            "COMPLETE_MARKER = '.complete'\n"
            "POINTER_NAME = 'current.txt'\n"
            "def resolve_src(app_dir, program_name=''):\n"
            "    raise SystemError('不會被呼叫到')\n",   # 少了 is_safe_version
            encoding="utf-8")
        assert up._load_version_pointer(str(app)) is None

    def test_the_commit_path_survives_a_truncated_resolver(self, app):
        (app / "version_pointer.py").write_text(
            "POINTER_NAME = 'current.txt'\n", encoding="utf-8")
        writes = [("src/b.py", "src/b.py", V, "新 B\n",
                   str(app / "src" / "b.py"))]
        res = up._commit_pending_writes(writes, _result())
        assert not res.errors, res.errors
        assert (app / "src" / "b.py").read_text(
            encoding="utf-8") == "新 B\n", "退化路徑就是就地更新"
        assert not (app / "current.txt").exists()
