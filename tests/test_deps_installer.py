# -*- coding: utf-8 -*-
"""deps_installer 的版本 spec 解析測試（不啟動 Tk UI）。

只測純函式：_normalize_pkg / _load_requirement_specs / _resolve_pip_spec。
這些保證 runtime fallback 安裝也吃 requirements.txt 的版本上限 pin。
"""
import os
import sys

import pytest

_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, os.path.abspath(_SRC))

import cmuh_common.deps_installer as di


@pytest.fixture
def fake_requirements(tmp_path, monkeypatch):
    """寫一個臨時 requirements.txt 並讓 deps_installer 讀它。"""
    req = tmp_path / "requirements.txt"
    req.write_text(
        "# comment line\n"
        "requests>=2.31.0,<3\n"
        "beautifulsoup4>=4.12.0,<5\n"
        "Pillow>=10.0.0,<12\n"
        "sv-ttk>=2.6.0,<3  # inline comment\n"
        "pywin32>=306\n"
        "keyboard>=0.13.5          # 0.x\n"
        "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(di, "get_app_dir", lambda: str(tmp_path))
    # 清快取，逼它重讀臨時檔
    monkeypatch.setattr(di, "_REQ_SPECS_CACHE", None, raising=False)
    return tmp_path


def test_normalize_pkg():
    assert di._normalize_pkg("Pillow") == "pillow"
    assert di._normalize_pkg("sv-ttk") == "sv-ttk"
    assert di._normalize_pkg("sv_ttk") == "sv-ttk"
    assert di._normalize_pkg("  beautifulsoup4 ") == "beautifulsoup4"


def test_load_requirement_specs_parses_lines(fake_requirements):
    specs = di._load_requirement_specs()
    assert specs["requests"] == "requests>=2.31.0,<3"
    assert specs["beautifulsoup4"] == "beautifulsoup4>=4.12.0,<5"
    assert specs["pillow"] == "Pillow>=10.0.0,<12"
    # 行內註解要被去掉
    assert specs["sv-ttk"] == "sv-ttk>=2.6.0,<3"
    assert specs["keyboard"] == "keyboard>=0.13.5"
    # 純註解行 / 空行不該進來
    assert "comment" not in specs


def test_resolve_pip_spec_uses_pinned_spec(fake_requirements):
    assert di._resolve_pip_spec("beautifulsoup4") == "beautifulsoup4>=4.12.0,<5"
    assert di._resolve_pip_spec("Pillow") == "Pillow>=10.0.0,<12"
    assert di._resolve_pip_spec("pywin32") == "pywin32>=306"
    # 連字號/底線視為相同
    assert di._resolve_pip_spec("sv_ttk") == "sv-ttk>=2.6.0,<3"


def test_resolve_pip_spec_fallback_bare_name(fake_requirements):
    # requirements.txt 沒列的套件 → 回裸名
    assert di._resolve_pip_spec("somethingelse") == "somethingelse"


def test_load_requirement_specs_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(di, "get_app_dir", lambda: str(tmp_path / "nonexistent"))
    monkeypatch.setattr(di, "_REQ_SPECS_CACHE", None, raising=False)
    specs = di._load_requirement_specs()
    assert specs == {}
    # 缺檔時仍回裸套件名，不爆
    assert di._resolve_pip_spec("requests") == "requests"


def test_pip_python_executable_prefers_console_sibling_for_pythonw(tmp_path):
    pythonw = tmp_path / "pythonw.exe"
    python = tmp_path / "python.exe"
    pythonw.write_bytes(b"")
    python.write_bytes(b"")

    assert di._pip_python_executable(str(pythonw)) == str(python)


def test_pip_python_executable_keeps_original_without_console_sibling(tmp_path):
    pythonw = tmp_path / "pythonw.exe"
    pythonw.write_bytes(b"")

    assert di._pip_python_executable(str(pythonw)) == str(pythonw)


def test_rotate_dependency_install_log_keeps_small_log(tmp_path):
    log_path = tmp_path / "dependency_install.log"
    log_path.write_text("small", encoding="utf-8")

    assert di._rotate_dependency_install_log(str(log_path), max_bytes=10) is False
    assert log_path.read_text(encoding="utf-8") == "small"
    assert not (tmp_path / "dependency_install.log.bak").exists()


def test_rotate_dependency_install_log_moves_large_log(tmp_path):
    log_path = tmp_path / "dependency_install.log"
    log_path.write_text("long log content", encoding="utf-8")

    assert di._rotate_dependency_install_log(str(log_path), max_bytes=5) is True
    assert not log_path.exists()
    assert (tmp_path / "dependency_install.log.bak").read_text(
        encoding="utf-8"
    ) == "long log content"


def test_run_on_ui_thread_ignores_callback_after_close(monkeypatch):
    installer = object.__new__(di.DependencyInstaller)
    installer._closing = False
    callbacks = []
    installer.after = lambda _delay, callback: callbacks.append(callback)

    monkeypatch.setattr(di.threading, "current_thread", lambda: object())
    monkeypatch.setattr(di.threading, "main_thread", lambda: object())

    called = []
    assert installer._run_on_ui_thread(lambda: called.append(True)) is True
    installer._closing = True
    callbacks[0]()

    assert called == []
