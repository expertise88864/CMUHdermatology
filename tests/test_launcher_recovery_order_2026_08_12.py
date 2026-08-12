# -*- coding: utf-8 -*-
"""[批次AD-1] L2 結案:復原先於版本解析 + 嚴格指標 + containment。

★[外審 2026-08-12 P1-01]★ 舊順序是「先解析 → 再復原 → 跑解析時選到的那棵」。
復原收的是上一批沒走完的更新殘局 —— 包括 version_pointer.py / current.txt
本身。復原把指標修好了,這一次卻仍跑修復【前】選到的 <app>/src ——
★復原成功 ≠ 本次啟動用的是復原後的狀態★。

★[P2-02]★ `current.txt` 是原子版本選擇器:格式必須是【恰好一個邏輯行】。
`V2\\nTHIS_FILE_IS_CORRUPTED` 的第一行剛好還像版本號,不能當成沒事 ——
那正是寫壞了一半的樣子。

★[P2-01]★ 版本字串白名單擋得掉 `..`,擋不掉 junction:`versions/V2` 指到
別處的話,字串層完全合法,實際載入的卻是任意目錄。realpath 展開後必須
留在 versions/ 底下。`.complete` 也必須是【檔案】(部署流程寫的是檔)。
"""
import glob
import io
import os
import shutil
import subprocess
import sys

import pytest

NL = chr(10)
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, REPO_ROOT)

import version_pointer as vp  # noqa: E402

_STUBS = sorted(glob.glob(os.path.join(REPO_ROOT, "*.pyw")))


# ── ① 順序:六支 stub 的復原一定要排在解析之前 ───────────────────────────
@pytest.mark.parametrize("path", _STUBS, ids=lambda p: os.path.basename(p))
def test_recovery_runs_before_the_version_is_resolved(path):
    src = io.open(path, encoding="utf-8").read()
    call = src.rindex("_recover_incomplete_update()")   # 最後一次出現=呼叫點
    resolve = src.index("_SRC = _resolve_src()")
    assert call < resolve, (
        "★復原成功 ≠ 本次啟動用的是復原後的狀態★ 解析要排在復原之後")


@pytest.mark.parametrize("path", _STUBS, ids=lambda p: os.path.basename(p))
def test_the_env_pins_still_follow_the_resolution(path):
    """環境釘選與 sys.path 要跟著解析走(在它之後、在 runpy 之前)。"""
    src = io.open(path, encoding="utf-8").read()
    resolve = src.index("_SRC = _resolve_src()")
    assert resolve < src.index('os.environ["CMUH_APP_DIR"]')
    assert resolve < src.index("sys.path.insert(0, _SRC)")
    assert src.index("sys.path.insert(0, _SRC)") < src.index("runpy.run_path(")


# ── ② 端到端:復原修好指標之後,【同一次啟動】就要跑到 V2 ────────────────
class TestRecoveryRepairsThePointerAndTheSameRunUsesIt:
    """外審指定的整合情境:

        resolver 初始損壞
        current.txt → V2
        recovery 修復 resolver
        → 同一次 launcher invocation 必須真正執行 V2
    """

    @staticmethod
    def _build_app(tmp_path, *, recovery_repairs: bool):
        app = tmp_path
        # 損壞的 resolver:載入(exec_module)會炸 → stub 走 fallback
        (app / "version_pointer.py").write_text(
            'raise RuntimeError("上一批更新只寫到一半")', encoding="utf-8")
        (app / "current.txt").write_text("V2", encoding="utf-8")
        # V2 樹(完整):跑到就寫下記號
        v2 = app / "versions" / "V2" / "src"
        v2.mkdir(parents=True)
        (app / "versions" / "V2" / ".complete").write_text("", encoding="utf-8")
        marker = app / "ran.txt"
        (v2 / "scheduler.py").write_text(
            "open(r'%s','w').write('V2')" % marker, encoding="utf-8")
        # 舊樹:跑到也寫下記號
        legacy = app / "src"
        legacy.mkdir()
        (legacy / "scheduler.py").write_text(
            "open(r'%s','w').write('legacy')" % marker, encoding="utf-8")
        # 假的復原模組:把 resolver 修好(用 repo 裡真的那一份)
        good = io.open(os.path.join(REPO_ROOT, "version_pointer.py"),
                       encoding="utf-8").read()
        (legacy / "bootstrap_recovery.py").write_text(
            "import io" + NL
            + "def recover_and_report(app_dir, name):" + NL
            + ("    io.open(app_dir + r'\\version_pointer.py', 'w',"
               " encoding='utf-8').write(%r)" % good
               if recovery_repairs else "    pass") + NL
            + "    class _R:" + NL
            + "        safe_to_start = True" + NL
            + "    return _R()" + NL, encoding="utf-8")
        # 用排班 stub(復原是裸呼叫、結尾直接 runpy scheduler.py)
        shutil.copyfile(
            os.path.join(REPO_ROOT, "中國醫皮膚科排班程式.pyw"),
            app / "stub.pyw")
        return app, marker

    @staticmethod
    def _run(app):
        cp = subprocess.run(
            [sys.executable, str(app / "stub.pyw")],
            capture_output=True, text=True, timeout=120,
            encoding="utf-8", errors="replace", cwd=str(app))
        assert cp.returncode == 0, (cp.stdout, cp.stderr)

    def test_the_same_invocation_runs_the_repaired_version(self, tmp_path):
        app, marker = self._build_app(tmp_path, recovery_repairs=True)
        self._run(app)
        assert marker.read_text(encoding="utf-8") == "V2", (
            "★復原修好了指標,這一次卻仍跑修復前選到的 <app>/src★")

    def test_an_unrepaired_pointer_still_falls_back(self, tmp_path):
        """復原沒修 resolver 時照舊走 <app>/src —— 回退路徑不能被這一改弄壞。"""
        app, marker = self._build_app(tmp_path, recovery_repairs=False)
        self._run(app)
        assert marker.read_text(encoding="utf-8") == "legacy"


# ── ③ 指標解析:恰好一個邏輯行 ──────────────────────────────────────────
class TestThePointerMustBeExactlyOneLine:
    @staticmethod
    def _app(tmp_path, pointer_bytes):
        (tmp_path / "current.txt").write_bytes(pointer_bytes)
        v = tmp_path / "versions" / "V2" / "src"
        v.mkdir(parents=True)
        (tmp_path / "versions" / "V2" / ".complete").write_text(
            "", encoding="utf-8")
        (tmp_path / "src").mkdir()
        return str(tmp_path)

    def test_trailing_garbage_is_rejected(self, tmp_path):
        app = self._app(tmp_path, b"V2\nTHIS_FILE_IS_CORRUPTED\n")
        r = vp.resolve_src(app, "測試")
        assert r.reason == vp.POINTER_MALFORMED
        assert r.src_dir == os.path.join(app, "src")

    def test_trailing_whitespace_lines_are_fine(self, tmp_path):
        app = self._app(tmp_path, b"V2\n\n  \n")
        assert vp.resolve_src(app, "測試").reason == vp.PINNED

    def test_a_bom_does_not_break_the_pointer(self, tmp_path):
        app = self._app(tmp_path, b"\xef\xbb\xbfV2\n")
        assert vp.resolve_src(app, "測試").reason == vp.PINNED, (
            "有些編輯器會補 BOM —— BOM 不該讓指標失效")

    def test_an_oversized_pointer_is_rejected(self, tmp_path):
        app = self._app(tmp_path, b"V2\n" + b" " * 4096)
        assert vp.resolve_src(app, "測試").reason == vp.POINTER_MALFORMED, (
            "指標不該有這種大小 —— 大檔就是壞掉的樣子")


# ── ④ containment:junction / 同名目錄 ───────────────────────────────────
class TestTheVersionTreeMustPhysicallyStayInsideVersions:
    def test_a_complete_marker_that_is_a_directory_is_rejected(self, tmp_path):
        (tmp_path / "current.txt").write_text("V2", encoding="utf-8")
        v = tmp_path / "versions" / "V2"
        (v / "src").mkdir(parents=True)
        (v / ".complete").mkdir()          # 目錄,不是部署流程寫的檔案
        (tmp_path / "src").mkdir()
        assert vp.resolve_src(str(tmp_path), "測試").reason == vp.INCOMPLETE

    @pytest.mark.skipif(sys.platform != "win32", reason="junction 是 Windows 概念")
    def test_a_junction_escaping_versions_is_rejected(self, tmp_path):
        """`versions/V2` 是指到外面的 junction → 字串層合法,實體位置不合法。"""
        outside = tmp_path / "outside"
        (outside / "src").mkdir(parents=True)
        (outside / ".complete").write_text("", encoding="utf-8")
        (tmp_path / "versions").mkdir()
        (tmp_path / "src").mkdir()
        (tmp_path / "current.txt").write_text("V2", encoding="utf-8")
        link = tmp_path / "versions" / "V2"
        cp = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            capture_output=True, text=True, timeout=30)
        if cp.returncode != 0:
            pytest.skip("這台建不了 junction:%s" % cp.stderr.strip())
        r = vp.resolve_src(str(tmp_path), "測試")
        assert r.reason == vp.ESCAPES_VERSIONS, (
            "★junction 讓 current.txt 指到任意目錄★")
        assert r.src_dir == os.path.join(str(tmp_path), "src")

    @pytest.mark.skipif(sys.platform != "win32", reason="junction 是 Windows 概念")
    def test_a_junction_inside_the_tree_is_also_rejected(self, tmp_path):
        """★[外審 AD-1 第 1 輪 P2]★ 只 realpath 頂端目錄擋不住內層逸出:
        `src/cmuh_common` 是指到外面的 junction 時,src 本身完全正常。
        政策=版本樹內部不允許任何 reparse point(部署流程只複製檔案,
        樹裡出現連結本身就是「不是部署流程放的」的證據)。
        """
        outside = tmp_path / "outside_pkg"
        outside.mkdir()
        (tmp_path / "current.txt").write_text("V2", encoding="utf-8")
        v = tmp_path / "versions" / "V2"
        (v / "src").mkdir(parents=True)
        (v / ".complete").write_text("", encoding="utf-8")
        (tmp_path / "src").mkdir()
        cp = subprocess.run(
            ["cmd", "/c", "mklink", "/J",
             str(v / "src" / "cmuh_common"), str(outside)],
            capture_output=True, text=True, timeout=30)
        if cp.returncode != 0:
            pytest.skip("這台建不了 junction:%s" % cp.stderr.strip())
        r = vp.resolve_src(str(tmp_path), "測試")
        assert r.reason == vp.ESCAPES_VERSIONS, (
            "★內層 junction 把 import 導向 versions/ 外★")
        assert r.src_dir == os.path.join(str(tmp_path), "src")

    def test_a_tree_that_cannot_be_enumerated_is_rejected(
            self, tmp_path, monkeypatch):
        """★[外審 AD-1 第 2 輪 P2]★ os.walk 預設【靜默跳過】列舉失敗的子樹。

        一個拒絕列目錄的子樹(ACL、防毒鎖住)會讓掃描「沒看到=沒有」——
        藏在裡面的 reparse point 根本沒被檢查,守衛自己 no-op 掉了。
        查不動必須=拒絕(大聲回退 <app>/src),不是當成乾淨。
        """
        (tmp_path / "current.txt").write_text("V2", encoding="utf-8")
        v = tmp_path / "versions" / "V2"
        (v / "src").mkdir(parents=True)
        (v / ".complete").write_text("", encoding="utf-8")
        (tmp_path / "src").mkdir()

        def _walk_that_hits_an_acl_wall(top, followlinks=False, onerror=None):
            # 模擬 os.walk 的契約:有 onerror 就回報錯誤,沒有就靜默跳過
            if onerror is not None:
                onerror(OSError("存取被拒(ACL)"))
            return iter(())

        monkeypatch.setattr(vp.os, "walk", _walk_that_hits_an_acl_wall)
        r = vp.resolve_src(str(tmp_path), "測試")
        assert r.reason == vp.ESCAPES_VERSIONS, (
            "★查不動被當成乾淨★ 沒掃到的子樹裡什麼都可能有")
        assert r.src_dir == os.path.join(str(tmp_path), "src")

    def test_a_normal_version_still_resolves(self, tmp_path):
        """containment 不可以把正常版本誤殺(它是新加的守衛,守衛要能過好人)。"""
        (tmp_path / "current.txt").write_text("V2", encoding="utf-8")
        v = tmp_path / "versions" / "V2"
        (v / "src").mkdir(parents=True)
        (v / ".complete").write_text("", encoding="utf-8")
        (tmp_path / "src").mkdir()
        r = vp.resolve_src(str(tmp_path), "測試")
        assert r.reason == vp.PINNED
        assert r.src_dir == os.path.join(str(tmp_path), "versions", "V2", "src")


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
