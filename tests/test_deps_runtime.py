# -*- coding: utf-8 -*-
"""Dependency runtime cache and verification tests."""
import os
import sys

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
