# -*- coding: utf-8 -*-
"""Shared desktop helper launcher tests."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import process_launch  # noqa: E402


def test_resolve_app_script_returns_absolute_path(tmp_path):
    launcher = tmp_path / "helper.pyw"
    launcher.write_text("# launcher\n", encoding="utf-8")

    assert process_launch.resolve_app_script(
        "helper.pyw", app_dir=str(tmp_path)
    ) == str(launcher.resolve())


@pytest.mark.parametrize("script_name", ["", ".", "../outside.pyw"])
def test_resolve_app_script_rejects_unsafe_targets(tmp_path, script_name):
    with pytest.raises(ValueError):
        process_launch.resolve_app_script(script_name, app_dir=str(tmp_path))


def test_launch_app_script_uses_absolute_path_and_stable_cwd(tmp_path, monkeypatch):
    launcher = tmp_path / "helper.pyw"
    launcher.write_text("# launcher\n", encoding="utf-8")
    calls = []

    def fake_popen(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return object()

    monkeypatch.setattr(process_launch.subprocess, "Popen", fake_popen)

    result = process_launch.launch_app_script(
        "helper.pyw",
        executable="pythonw.exe",
        app_dir=str(tmp_path),
    )

    assert result is not None
    assert calls == [(
        ["pythonw.exe", str(launcher.resolve())],
        {
            "cwd": str(tmp_path.resolve()),
            "creationflags": getattr(process_launch.subprocess, "CREATE_NO_WINDOW", 0),
            "close_fds": True,
        },
    )]


def test_launch_python_script_adds_args_and_detached_flags(tmp_path, monkeypatch):
    launcher = tmp_path / "helper.pyw"
    launcher.write_text("# launcher\n", encoding="utf-8")
    calls = []

    monkeypatch.setattr(
        process_launch.subprocess,
        "Popen",
        lambda cmd, **kwargs: calls.append((cmd, kwargs)) or object(),
    )
    monkeypatch.setattr(process_launch.os, "name", "nt")
    monkeypatch.setattr(process_launch.subprocess, "DETACHED_PROCESS", 0x08, raising=False)
    monkeypatch.setattr(
        process_launch.subprocess,
        "CREATE_NEW_PROCESS_GROUP",
        0x200,
        raising=False,
    )

    process_launch.launch_python_script(
        str(launcher),
        args=["--configure"],
        executable="pythonw.exe",
        cwd=str(tmp_path),
        detached=True,
    )

    assert calls[0][0] == ["pythonw.exe", str(launcher.resolve()), "--configure"]
    assert calls[0][1]["cwd"] == str(tmp_path.resolve())
    assert calls[0][1]["creationflags"] & 0x08
    assert calls[0][1]["creationflags"] & 0x200
    assert calls[0][1]["close_fds"] is True
