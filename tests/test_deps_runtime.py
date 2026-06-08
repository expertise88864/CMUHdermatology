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


def test_find_missing_libs_reports_transitive_import_failure(monkeypatch):
    def fake_import(name):
        if name == "pyautogui":
            raise RuntimeError("broken transitive dependency")
        return object()

    monkeypatch.setattr(dr.importlib, "import_module", fake_import)

    assert dr._find_missing_libs([
        ("psutil", "psutil"),
        ("pyautogui", "pyautogui"),
    ]) == [("pyautogui", "pyautogui")]


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

    assert dr._all_modules_discoverable([("psutil", "psutil")]) is True


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
