# -*- coding: utf-8 -*-
"""WebDriver 初始化 + ChromeDriver 路徑【磁碟】快取。

【強化】原打卡程式只快取在記憶體（line 91-99 的 _cached_chromedriver_path）。
重啟後又要 ChromeDriverManager().install() 一次（網路慢時數秒）。
本模組改寫到 settings/chromedriver_path.json，含 chrome 主版本，版本變了才重抓。
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import threading
from pathlib import Path

from cmuh_common.paths import get_settings_dir

_path_cache_lock = threading.Lock()
_path_cache_file = Path(get_settings_dir()) / "chromedriver_path.json"


def _detect_chrome_major_version() -> str | None:
    """從 Windows 登錄檔或 chrome.exe 取主版本號。失敗回 None（不阻擋啟動）。"""
    if os.name != 'nt':
        return None
    try:
        import winreg  # type: ignore[import-not-found]
        for key_path in (
            r"Software\Google\Chrome\BLBeacon",
            r"Software\Wow6432Node\Google\Chrome\BLBeacon",
        ):
            for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(hive, key_path) as k:
                        ver, _ = winreg.QueryValueEx(k, "version")
                        return str(ver).split('.')[0]
                except OSError:
                    continue
    except Exception:
        logging.debug("讀 Chrome 版本（登錄檔）失敗", exc_info=True)
    # 退而求其次：試 powershell 抓 chrome.exe 版本
    try:
        out = subprocess.check_output(
            ["powershell", "-NoProfile", "-Command",
             "(Get-Item (Get-Command chrome -ErrorAction SilentlyContinue).Source).VersionInfo.FileVersion"],
            stderr=subprocess.DEVNULL, timeout=5, text=True,
        )
        m = re.match(r"(\d+)", out.strip())
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _read_disk_cache() -> tuple[str, str] | None:
    """回傳 (chromedriver_path, chrome_major) 或 None。"""
    try:
        if not _path_cache_file.is_file():
            return None
        data = json.loads(_path_cache_file.read_text(encoding='utf-8'))
        path = data.get('path')
        ver = data.get('chrome_major')
        if path and isinstance(path, str) and os.path.isfile(path):
            return path, str(ver or "")
    except Exception:
        logging.debug("讀 chromedriver 磁碟快取失敗", exc_info=True)
    return None


def _write_disk_cache(path: str, chrome_major: str | None) -> None:
    try:
        _path_cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = _path_cache_file.with_suffix('.json.tmp')
        tmp.write_text(
            json.dumps({"path": path, "chrome_major": chrome_major or ""}, ensure_ascii=False),
            encoding='utf-8',
        )
        os.replace(tmp, _path_cache_file)
    except Exception:
        logging.debug("寫 chromedriver 磁碟快取失敗", exc_info=True)


_memory_cache: str | None = None


def get_chromedriver_path() -> str:
    """回傳可用的 chromedriver.exe 絕對路徑。

    優先順序：記憶體快取 → 磁碟快取（chrome 版本相符）→ ChromeDriverManager().install()。
    """
    global _memory_cache
    with _path_cache_lock:
        if _memory_cache and os.path.isfile(_memory_cache):
            return _memory_cache

        chrome_major = _detect_chrome_major_version()
        cached = _read_disk_cache()
        if cached:
            cached_path, cached_major = cached
            if not chrome_major or cached_major == chrome_major:
                _memory_cache = cached_path
                return cached_path
            logging.info("Chrome 版本由 v%s 變為 v%s，重抓 chromedriver",
                         cached_major, chrome_major)

        # 真正下載
        from webdriver_manager.chrome import ChromeDriverManager  # type: ignore[import-not-found]
        path = ChromeDriverManager().install()
        _memory_cache = path
        _write_disk_cache(path, chrome_major)
        return path


def build_chrome_options(headless: bool = True):
    """回傳 selenium Options。集中所有效能與隱私旗標。"""
    from selenium.webdriver.chrome.options import Options  # type: ignore[import-not-found]

    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    for arg in (
        "--disable-gpu", "--window-size=1280,800", "--no-sandbox",
        "--disable-dev-shm-usage", "--disable-extensions",
        "--dns-prefetch-disable", "--log-level=3",
        "--disable-images", "--blink-settings=imagesEnabled=false",
        "--disable-notifications", "--disable-popup-blocking",
        "--disable-infobars", "--disable-background-networking",
        "--disable-sync",
    ):
        opts.add_argument(arg)
    opts.add_experimental_option("excludeSwitches", ["enable-logging"])
    opts.page_load_strategy = "eager"
    return opts


def _invalidate_chromedriver_cache() -> None:
    """【穩定性 2026-05-21】清掉記憶體 + 磁碟快取。
    用於 webdriver.Chrome() 失敗時（例：AV 隔離了 chromedriver.exe），
    下次 get_chromedriver_path() 會走 ChromeDriverManager 重抓最新版。"""
    global _memory_cache
    with _path_cache_lock:
        _memory_cache = None
        try:
            if _path_cache_file.exists():
                _path_cache_file.unlink()
        except Exception:
            logging.debug("刪 chromedriver 磁碟快取失敗", exc_info=True)


def initialize_driver(headless: bool = True):
    """初始化 selenium Chrome WebDriver。失敗回 None（呼叫端自行處理）。

    【穩定性 2026-05-21】失敗時清快取，避免 AV 隔離 chromedriver 後永久 fail。
    """
    try:
        from selenium import webdriver  # type: ignore[import-not-found]
        from selenium.webdriver.chrome.service import Service  # type: ignore[import-not-found]

        logging.info("初始化 WebDriver (Headless=%s)...", headless)
        return webdriver.Chrome(
            service=Service(get_chromedriver_path()),
            options=build_chrome_options(headless),
        )
    except Exception as e:
        logging.exception("初始化 WebDriver 失敗: %s", e)
        _invalidate_chromedriver_cache()
        return None
