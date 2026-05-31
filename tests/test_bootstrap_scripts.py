# -*- coding: utf-8 -*-
"""Deployment bootstrap script regression checks."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_root_python_setup_stays_ascii_and_verifies_imports():
    setup_path = ROOT / "安裝Python.bat"
    setup_bytes = setup_path.read_bytes()
    setup_text = setup_bytes.decode("ascii")

    assert all(byte < 128 for byte in setup_bytes)
    assert "scripts\\verify_dependencies.py" in setup_text
    assert "settings\\python_setup.log" in setup_text
    assert "--prefer-binary" in setup_text
    assert "2>&1" in setup_text


def test_deploy_installer_verifies_imports_and_fails_closed():
    src = (ROOT / "deploy" / "installer.bat").read_text(encoding="utf-8")

    assert "scripts\\verify_dependencies.py" in src
    assert "settings\\python_setup.log" in src
    assert "--prefer-binary" in src
    assert "if errorlevel 1" in src


def test_manifest_sync_includes_dependency_bootstrap_files():
    src = (ROOT / "scripts" / "sync_manifest.py").read_text(encoding="utf-8")

    assert '"安裝Python.bat"' in src
    assert '"deploy/installer.bat"' in src
    assert '"scripts/verify_dependencies.py"' in src
