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

        def __init__(self, _required_libs, _missing_libs):
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
        def __init__(self, _required_libs, _missing_libs):
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


def test_dependency_still_missing_after_install_exits_nonzero(
    tmp_path, monkeypatch
):
    from cmuh_common import deps_installer

    class FakeInstaller:
        def __init__(self, _required_libs, _missing_libs):
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
