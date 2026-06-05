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


def test_initialize_driver_retries_once_after_failure(monkeypatch):
    """第 1 次建立失敗（如 Chrome 更新後版本不合）→ 清快取 → 第 2 次重抓重試成功。"""
    import selenium.webdriver as real_webdriver
    import selenium.webdriver.chrome.service as svc_mod

    get_path_calls = []
    invalidated = []
    monkeypatch.setattr(
        ws, "get_chromedriver_path",
        lambda: get_path_calls.append(1) or "C:/cd/chromedriver.exe")
    monkeypatch.setattr(ws, "build_chrome_options", lambda headless=True: object())
    monkeypatch.setattr(
        ws, "_invalidate_chromedriver_cache", lambda: invalidated.append(1))

    class _FakeService:
        def __init__(self, path):
            self.path = path

        def stop(self):
            pass

    monkeypatch.setattr(svc_mod, "Service", _FakeService)

    sentinel_driver = object()
    chrome_calls = []

    def _fake_chrome(service, options):
        chrome_calls.append(1)
        if len(chrome_calls) == 1:
            raise RuntimeError("SessionNotCreated: chromedriver/chrome version mismatch")
        return sentinel_driver

    monkeypatch.setattr(real_webdriver, "Chrome", _fake_chrome)

    result = ws.initialize_driver(headless=True)

    assert result is sentinel_driver          # 第 2 次成功
    assert len(chrome_calls) == 2             # 重試了一次
    assert len(invalidated) == 1             # 第 1 次失敗清了快取
    assert len(get_path_calls) == 2          # 第 2 次重抓 driver 路徑


def test_initialize_driver_returns_none_after_two_failures(monkeypatch):
    """連兩次都失敗 → 回 None（不無限重試），且兩次都清快取。"""
    import selenium.webdriver as real_webdriver
    import selenium.webdriver.chrome.service as svc_mod

    invalidated = []
    monkeypatch.setattr(ws, "get_chromedriver_path", lambda: "C:/cd/chromedriver.exe")
    monkeypatch.setattr(ws, "build_chrome_options", lambda headless=True: object())
    monkeypatch.setattr(
        ws, "_invalidate_chromedriver_cache", lambda: invalidated.append(1))

    class _FakeService:
        def __init__(self, path):
            pass

        def stop(self):
            pass

    monkeypatch.setattr(svc_mod, "Service", _FakeService)

    def _always_fail(service, options):
        raise RuntimeError("boom")

    monkeypatch.setattr(real_webdriver, "Chrome", _always_fail)

    assert ws.initialize_driver(headless=True) is None
    assert len(invalidated) == 2
