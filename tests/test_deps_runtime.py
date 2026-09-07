# -*- coding: utf-8 -*-
"""Dependency runtime cache and verification tests."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import deps_runtime as dr  # noqa: E402


def test_dependency_fingerprint_changes_with_interpreter(monkeypatch):
    required = [("psutil", "psutil")]

    monkeypatch.setattr(dr.sys, "executable", r"C:\Python312\pythonw.exe")
    first = dr._build_fingerprint(required)
    monkeypatch.setattr(dr.sys, "executable", r"C:\Python313\pythonw.exe")
    second = dr._build_fingerprint(required)

    assert first != second
    assert "exe:" in first


def test_dependency_fingerprint_changes_with_manifest_spec(monkeypatch):
    specs = {"demo": "demo>=1"}
    monkeypatch.setattr(dr, "_resolve_requirement_spec", lambda pkg: specs[pkg])
    first = dr._build_fingerprint([("demo", "json")])
    specs["demo"] = "demo>=2"
    second = dr._build_fingerprint([("demo", "json")])
    assert first != second


def test_find_missing_libs_reports_transitive_import_failure(monkeypatch):
    def fake_import(name):
        if name == "pyautogui":
            raise RuntimeError("broken transitive dependency")
        return object()

    monkeypatch.setattr(dr.importlib, "import_module", fake_import)
    monkeypatch.setattr(dr, "_distribution_satisfies", lambda _pkg: True)

    assert dr._find_missing_libs([
        ("psutil", "psutil"),
        ("pyautogui", "pyautogui"),
    ]) == [("pyautogui", "pyautogui")]


def test_find_missing_libs_reports_importable_version_mismatch(monkeypatch):
    real_import = dr.importlib.import_module
    monkeypatch.setattr(
        dr.importlib, "import_module",
        lambda name: object() if name == "json" else real_import(name),
    )
    monkeypatch.setattr(dr, "_resolve_requirement_spec", lambda _pkg: "demo>=2")
    monkeypatch.setattr(dr.importlib.metadata, "version", lambda _name: "1.0")
    assert dr._find_missing_libs([("demo", "json")]) == [("demo", "json")]


def test_a_version_mismatch_is_never_imported_first(monkeypatch):
    """★外審 r11 P2★:版本不符的套件★不可以先被 import★。
    先 import 的話它就進了 `sys.modules`,pip 就地升級檔案之後行程仍在用記憶體裡的舊
    實作,而安裝後的複驗看的是 distribution metadata(新的)→ 通過。於是「已修好」是
    假的,一路撐到下次重啟為止 —— 可能正是那個有漏洞的版本還在跑。"""
    imported = []
    real_import = dr.importlib.import_module
    monkeypatch.setattr(
        dr.importlib, "import_module",
        lambda name: (imported.append(name), real_import("json"))[1],
    )
    monkeypatch.setattr(dr, "_resolve_requirement_spec", lambda _pkg: "demo>=2")
    monkeypatch.setattr(dr.importlib.metadata, "version", lambda _name: "1.0")

    assert dr._find_missing_libs([("demo", "json")]) == [("demo", "json")]
    assert imported == [], "★版本不符卻已經 import 進來了★"


def test_nothing_is_imported_while_any_dependency_is_mismatched(monkeypatch):
    """★外審 r11 P2(第二回)★:逐個交錯檢查時,排在前面而版本合格的套件會被 import,
    它可能★transitively★載入排在後面、版本不合格的那個 —— 實例:`pyautogui` 排在
    `Pillow` 之前,import PyAutoGUI/PyScreeze 會把 PIL 一起載進來。等輪到檢查 Pillow
    時它早就在 sys.modules 裡,之後 pip 就地升級也換不掉記憶體中的舊實作。
    所以只要有任何一個不合格,★一個都不可以 import★。"""
    imported = []
    real_import = dr.importlib.import_module
    monkeypatch.setattr(
        dr.importlib, "import_module",
        lambda name: (imported.append(name), real_import("json"))[1],
    )
    specs = {"good": "good>=1", "bad": "bad>=2"}
    versions = {"good": "9.0", "bad": "1.0"}
    monkeypatch.setattr(dr, "_resolve_requirement_spec", lambda pkg: specs[pkg])
    monkeypatch.setattr(dr.importlib.metadata, "version", lambda name: versions[name])

    # good 合格且排在前面,bad 不合格排在後面(= pyautogui / Pillow 的形狀)
    out = dr._find_missing_libs([("good", "json"), ("bad", "json")])
    assert out == [("bad", "json")]
    assert imported == [], "★合格的那個被 import 了 → 它可能把不合格的一起載進來★"


def test_a_satisfied_dependency_is_still_imported(monkeypatch):
    """反例:版本符合就要照常 import(才驗得到 transitive 損壞)。"""
    imported = []
    real_import = dr.importlib.import_module
    monkeypatch.setattr(
        dr.importlib, "import_module",
        lambda name: (imported.append(name), real_import("json"))[1],
    )
    monkeypatch.setattr(dr, "_resolve_requirement_spec", lambda _pkg: "demo>=2")
    monkeypatch.setattr(dr.importlib.metadata, "version", lambda _name: "3.0")

    assert dr._find_missing_libs([("demo", "json")]) == []
    assert imported == ["json"]


def test_a_malformed_installed_version_is_treated_as_unsatisfied(monkeypatch):
    """契約:已安裝版本字串不是 PEP 440(legacy 版號,或被中斷的安裝留下的殘缺
    metadata)→ 當成不滿足,交給修復流程。
    ★這條不保證走到哪一條路★:目前的 packaging(26.3)對畸形字串是直接回 False;
    會拋例外的那條路由下面那條測試單獨量。"""
    monkeypatch.setattr(dr, "_resolve_requirement_spec", lambda _pkg: "demo>=2")
    monkeypatch.setattr(dr.importlib.metadata, "version",
                        lambda _name: "not-a-pep440-version")
    assert dr._distribution_satisfies("demo") is False


def test_a_version_comparison_that_raises_falls_through_to_repair(monkeypatch):
    """★外審 r11 P2★:比對已安裝版本時★拋例外★ → 要當成不滿足、交給修復流程,
    不可以讓整支程式在修復 UI 之前就掛掉(周圍每一個失敗都是這樣處理的)。

    ★不靠 packaging 的實際行為★:本機的 packaging 26.3 對畸形字串是回 False 而不是拋,
    所以「畸形字串」那條測試量不到這裡的 except —— 它是靠巧合通過的。這裡直接注入一個
    會拋的 specifier,量的才是這條規則本身。防禦仍然必要:requirements 允許
    packaging>=23.2(舊版對 InvalidVersion 是拋的),而 bootstrap 退路 `pip._vendor`
    的版本完全不受 requirements 控制。"""
    class _Specifier:
        def __bool__(self):
            return True

        def contains(self, *_a, **_k):
            raise ValueError("InvalidVersion: not-a-pep440-version")

    class _Requirement:
        name = "demo"
        marker = None
        specifier = _Specifier()

    monkeypatch.setattr(dr, "_requirement_class", lambda: (lambda _spec: _Requirement()))
    monkeypatch.setattr(dr, "_resolve_requirement_spec", lambda _pkg: "demo>=2")
    monkeypatch.setattr(dr.importlib.metadata, "version", lambda _name: "1.0")
    assert dr._distribution_satisfies("demo") is False


def test_distribution_ignores_an_inactive_environment_marker(monkeypatch):
    monkeypatch.setattr(
        dr, "_resolve_requirement_spec",
        lambda _pkg: "demo>=2; python_version < '1'",
    )

    def unexpected_version_lookup(_name):
        raise AssertionError("inactive dependency must not be looked up")

    monkeypatch.setattr(dr.importlib.metadata, "version", unexpected_version_lookup)
    assert dr._distribution_satisfies("demo") is True


def test_all_modules_discoverable_detects_removed_cached_dependency(monkeypatch):
    monkeypatch.setattr(
        dr.importlib.util,
        "find_spec",
        lambda name: None if name == "pyautogui" else object(),
    )

    assert dr._all_modules_discoverable([
        ("psutil", "psutil"),
        ("pyautogui", "pyautogui"),
    ]) is False


def test_all_modules_discoverable_accepts_present_dependencies(monkeypatch):
    monkeypatch.setattr(dr.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(dr, "_distribution_satisfies", lambda _pkg: True)

    assert dr._all_modules_discoverable([("psutil", "psutil")]) is True


def test_cached_dependency_rejects_version_mismatch(monkeypatch):
    monkeypatch.setattr(dr.importlib.util, "find_spec", lambda _name: object())
    monkeypatch.setattr(dr, "_distribution_satisfies", lambda _pkg: False)
    assert dr._all_modules_discoverable([("demo>=2", "json")]) is False


def test_packaging_bootstrap_dependency_is_added_once():
    assert dr._with_packaging_dependency([("requests", "requests")])[0] == (
        "packaging", "packaging")
    libs = [("packaging>=23", "packaging"), ("requests", "requests")]
    assert dr._with_packaging_dependency(libs) == libs


def test_dependency_installer_window_is_destroyed_when_mainloop_fails(
    tmp_path, monkeypatch
):
    from cmuh_common import deps_installer

    class FakeInstaller:
        destroyed = False

        def __init__(self, _required_libs, _missing_libs, *, repair_lock_fd):
            self.is_finished = False

        def mainloop(self):
            raise RuntimeError("tk failed")

        def destroy(self):
            FakeInstaller.destroyed = True

    monkeypatch.setattr(dr, "is_frozen", lambda: False)
    monkeypatch.setattr(dr, "get_app_dir", lambda: str(tmp_path))
    monkeypatch.setattr(
        dr,
        "_find_missing_libs",
        lambda _required_libs: [("pyautogui", "pyautogui")],
    )
    monkeypatch.setattr(deps_installer, "DependencyInstaller", FakeInstaller)

    with pytest.raises(RuntimeError, match="tk failed"):
        dr.ensure_dependencies([("pyautogui", "pyautogui")])

    assert FakeInstaller.destroyed is True


def test_dependency_installer_cancel_exits_nonzero(tmp_path, monkeypatch):
    from cmuh_common import deps_installer

    class FakeInstaller:
        def __init__(self, _required_libs, _missing_libs, *, repair_lock_fd):
            self.is_finished = False

        def mainloop(self):
            return None

        def destroy(self):
            return None

    monkeypatch.setattr(dr, "is_frozen", lambda: False)
    monkeypatch.setattr(dr, "get_app_dir", lambda: str(tmp_path))
    monkeypatch.setattr(
        dr,
        "_find_missing_libs",
        lambda _required_libs: [("pyautogui", "pyautogui")],
    )
    monkeypatch.setattr(deps_installer, "DependencyInstaller", FakeInstaller)

    with pytest.raises(SystemExit) as exc:
        dr.ensure_dependencies([("pyautogui", "pyautogui")])

    assert exc.value.code == 1


# ── [外審 r11 P1 第三回] 同一台機器只能有一個行程在修依賴 ──────────────────────
def _lock_env(monkeypatch, tmp_path, *, is_child):
    lock = tmp_path / "repair.lock"
    monkeypatch.setattr(dr, "_repair_lock_path", lambda: str(lock))
    monkeypatch.setattr(dr, "restart_handshake_active", lambda: is_child)
    monkeypatch.setattr(dr, "parent_understands_bootstrapping", lambda: False)
    monkeypatch.setattr(dr, "_build_fingerprint", lambda _libs: "fp")
    ran = []
    monkeypatch.setattr(dr, "_ensure_dependencies_locked",
                        lambda *a: ran.append(1))
    return lock, ran


def test_a_restart_child_exits_cleanly_when_another_process_is_repairing(
        monkeypatch, tmp_path):
    lock, ran = _lock_env(monkeypatch, tmp_path, is_child=True)
    holder = dr._acquire_repair_lock(str(lock))
    try:
        with pytest.raises(SystemExit) as excinfo:
            dr.ensure_dependencies(
                [("demo", "json")],
                deps_cache_filename=".no-such-cache",
                bootstrap=True,
            )
        assert excinfo.value.code == 0
        assert ran == []
        assert dr._acquire_repair_lock(str(lock)) is None
    finally:
        dr._release_repair_lock(holder, str(lock))


def test_runtime_dependency_check_does_not_reuse_bootstrap_exit(
        monkeypatch, tmp_path):
    lock, ran = _lock_env(monkeypatch, tmp_path, is_child=True)
    monkeypatch.setattr(dr, "parent_supports_repair_only", lambda: True)
    signals = []
    monkeypatch.setattr(dr, "restart_handshake_signal", signals.append)

    dr.ensure_dependencies(
        [("demo", "json")], deps_cache_filename=".no-such-cache")

    assert ran == [1]
    assert signals == []


def test_runtime_dependency_check_waits_for_repair_lock_despite_handshake(
        monkeypatch, tmp_path):
    lock, ran = _lock_env(monkeypatch, tmp_path, is_child=True)
    holder = dr._acquire_repair_lock(str(lock))
    monkeypatch.setattr(
        dr.time, "sleep", lambda _seconds: dr._release_repair_lock(holder, str(lock)))

    dr.ensure_dependencies(
        [("demo", "json")], deps_cache_filename=".no-such-cache")

    assert ran == [1]


def test_a_cold_start_waits_instead_of_exiting(monkeypatch, tmp_path):
    lock, ran = _lock_env(monkeypatch, tmp_path, is_child=False)
    holder = dr._acquire_repair_lock(str(lock))
    monkeypatch.setattr(dr.time, "sleep", lambda _s: dr._release_repair_lock(holder, str(lock)))
    dr.ensure_dependencies([("demo", "json")], deps_cache_filename=".no-such-cache")
    assert ran == [1]
    reacquired = dr._acquire_repair_lock(str(lock))
    assert reacquired is not None
    dr._release_repair_lock(reacquired, str(lock))


def test_an_unowned_marker_is_immediately_reusable(monkeypatch, tmp_path):
    # File age says nothing about ownership; even a fresh orphan is reusable.
    lock, ran = _lock_env(monkeypatch, tmp_path, is_child=True)
    lock.write_text("4242", encoding="utf-8")
    dr.ensure_dependencies([("demo", "json")], deps_cache_filename=".no-such-cache")
    assert ran == [1]


def test_the_lock_is_released_even_when_the_repair_raises(monkeypatch, tmp_path):
    lock, _ran = _lock_env(monkeypatch, tmp_path, is_child=False)
    def _boom(*_a):
        raise SystemExit(1)
    monkeypatch.setattr(dr, "_ensure_dependencies_locked", _boom)
    with pytest.raises(SystemExit):
        dr.ensure_dependencies([("demo", "json")], deps_cache_filename=".no-such-cache")
    reacquired = dr._acquire_repair_lock(str(lock))
    assert reacquired is not None
    dr._release_repair_lock(reacquired, str(lock))


def test_dependency_still_missing_after_install_exits_nonzero(
    tmp_path, monkeypatch
):
    from cmuh_common import deps_installer

    class FakeInstaller:
        def __init__(self, _required_libs, _missing_libs, *, repair_lock_fd):
            self.is_finished = True

        def mainloop(self):
            return None

        def destroy(self):
            return None

    monkeypatch.setattr(dr, "is_frozen", lambda: False)
    monkeypatch.setattr(dr, "get_app_dir", lambda: str(tmp_path))
    monkeypatch.setattr(
        dr,
        "_find_missing_libs",
        lambda _required_libs: [("pyautogui", "pyautogui")],
    )
    monkeypatch.setattr(deps_installer, "DependencyInstaller", FakeInstaller)

    with pytest.raises(SystemExit) as exc:
        dr.ensure_dependencies([("pyautogui", "pyautogui")])

    assert exc.value.code == 1
