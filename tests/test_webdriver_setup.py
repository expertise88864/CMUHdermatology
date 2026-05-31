# -*- coding: utf-8 -*-
"""clock.webdriver_setup helpers."""
import builtins
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from clock import webdriver_setup as ws  # noqa: E402


def test_detect_chrome_version_powershell_uses_no_window(monkeypatch):
    calls = []

    def fake_check_output(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return "123.0.6312.86\n"

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "winreg":
            raise ImportError("winreg unavailable in test")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(ws.os, "name", "nt")
    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(ws.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(ws.subprocess, "check_output", fake_check_output)

    assert ws._detect_chrome_major_version() == "123"
    assert calls
    assert calls[0][1]["creationflags"] == 0x08000000


def test_write_disk_cache_uses_shared_atomic_json_writer(monkeypatch, tmp_path):
    calls = []
    cache_path = tmp_path / "chromedriver_path.json"
    monkeypatch.setattr(ws, "_path_cache_file", cache_path)
    monkeypatch.setattr(
        ws,
        "atomic_write_json",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    ws._write_disk_cache("C:/driver/chromedriver.exe", "123")

    assert calls == [(
        (str(cache_path), {
            "path": "C:/driver/chromedriver.exe",
            "chrome_major": "123",
        }),
        {"indent": 2},
    )]
