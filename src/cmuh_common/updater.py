# -*- coding: utf-8 -*-
"""線上更新模組（GitHub raw + manifest.json）。

雙軌行為：
  - .pyw 模式：實際下載並覆寫對應 .py 檔，更新後由呼叫端決定是否重啟。
  - .exe 模式：只比對 app_version；發現新版時跳通知請使用者去 GitHub release
              下載新 exe，不嘗試覆寫（Windows 鎖檔）。

流程（搬自原主程式 check_and_update line 8600-8704，URL 改 GitHub raw）：
  1. fetch manifest.json from GitHub raw（含 cache-buster）
  2. 比對每個檔案的 version（tuple 比較）
  3. 平行下載新版（ThreadPoolExecutor）
  4. SHA256 校驗 + 全部成功才寫入（任一失敗則整批不寫，保持本地一致性）
  5. 失敗時保留本地舊版，記 log，不阻擋啟動
"""
import hashlib
import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests

from cmuh_common.atomic_io import atomic_write_text
from cmuh_common.paths import get_app_dir, is_frozen, restart_self
from cmuh_common.version import CURRENT_VERSION, parse_version

# === GitHub repo 設定 ===
GITHUB_OWNER = "expertise88864"
GITHUB_REPO = "CMUHdermatology"
GITHUB_BRANCH = "main"
RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/{GITHUB_BRANCH}"
MANIFEST_URL = f"{RAW_BASE}/manifest.json"
RELEASE_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"

UPDATE_TIMEOUT = 15
MANIFEST_TIMEOUT = 8


@dataclass
class UpdateResult:
    """更新結果。供 UI 層判斷是否提示重啟。"""
    checked: bool = False
    has_update: bool = False
    updated_files: list = field(default_factory=list)  # [(local_filename, new_version), ...]
    errors: list = field(default_factory=list)
    manifest_app_version: str = ""
    is_frozen: bool = False
    release_url: str = RELEASE_URL


def _fetch_manifest(timeout: float = MANIFEST_TIMEOUT) -> dict:
    """取 manifest.json，加 cache-buster。"""
    url = f"{MANIFEST_URL}?t={int(time.time())}"
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = 'utf-8'
    return resp.json()


def _read_local_version(local_path: str) -> str:
    """讀本地檔案 CURRENT_VERSION（搬自原 get_local_version line 1050-1063）。"""
    if not os.path.exists(local_path):
        return "0.0.0"
    try:
        with open(local_path, 'r', encoding='utf-8') as f:
            content = f.read()
        m = re.search(r'CURRENT_VERSION\s*=\s*["\']([\d.]+)["\']', content)
        return m.group(1) if m else "0.0.0"
    except Exception:
        return "0.0.0"


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode('utf-8')).hexdigest()


def _download_one(file_entry: dict, app_dir: str) -> Optional[tuple]:
    """下載單一檔案；如本地已是最新或內容版本未較新則回 None。

    回傳 (key, local_filename, new_version, content) 或 None。
    """
    key = file_entry["key"]
    remote_path = file_entry["remote_path"]
    local_filename = file_entry["local_filename"]
    expected_version = file_entry.get("version", "0.0.0")
    expected_sha = (file_entry.get("sha256") or "").lower().strip()

    local_path = os.path.join(app_dir, local_filename)
    local_ver = _read_local_version(local_path)

    # 本地已是最新就跳過
    if parse_version(local_ver) >= parse_version(expected_version):
        return None

    url = f"{RAW_BASE}/{remote_path}?t={int(time.time())}"
    logging.info("  [%s] 偵測到新版（v%s -> v%s），下載中...", key, local_ver, expected_version)

    resp = requests.get(url, timeout=UPDATE_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = 'utf-8'
    content = resp.text

    # SHA256 校驗（manifest 有指定才驗）
    if expected_sha:
        actual_sha = _sha256_text(content)
        if actual_sha != expected_sha:
            raise ValueError(f"[{key}] SHA256 不符（預期 {expected_sha[:12]}.. 實際 {actual_sha[:12]}..）")

    # 雙重驗證：檔案內 CURRENT_VERSION 必須符合 manifest（避免 raw cache 拿到舊版）
    m = re.search(r'CURRENT_VERSION\s*=\s*["\']([\d.]+)["\']', content)
    if m:
        actual_version = m.group(1)
        if parse_version(actual_version) <= parse_version(local_ver):
            logging.info("  [%s] 下載內容版本 v%s 並未較新，跳過", key, actual_version)
            return None
    else:
        actual_version = expected_version  # 子模組可能沒有頂層宣告，採 manifest 版本

    return (key, local_filename, actual_version, content)


def check_and_update(
    progress_callback: Optional[Callable[[str, str], None]] = None,
    write_files: Optional[bool] = None,
) -> UpdateResult:
    """執行更新檢查。

    Args:
        progress_callback: 可選回呼，簽名 (stage, info) -> None
        write_files: None=自動依 is_frozen 判斷; True=強制寫; False=只查不寫
    """
    result = UpdateResult(is_frozen=is_frozen())

    def _progress(stage: str, info: str = "") -> None:
        if progress_callback:
            try:
                progress_callback(stage, info)
            except Exception:
                logging.debug("progress_callback 例外", exc_info=True)

    _progress("fetching_manifest")
    try:
        manifest = _fetch_manifest()
    except Exception as e:
        logging.error("取得 manifest.json 失敗: %s", e)
        result.errors.append(f"無法連線 GitHub: {e}")
        return result

    result.checked = True
    result.manifest_app_version = manifest.get("app_version", "")

    # 預設：.exe 不寫；.pyw 寫
    if write_files is None:
        write_files = not is_frozen()

    # === .exe 模式（或被指定為唯讀檢查）===
    if not write_files:
        local_app_ver = CURRENT_VERSION
        if parse_version(result.manifest_app_version) > parse_version(local_app_ver):
            logging.info(
                "[更新檢查] 偵測到新版 v%s（本地 v%s），請至 %s 下載",
                result.manifest_app_version, local_app_ver, RELEASE_URL,
            )
            result.has_update = True
        else:
            logging.info("[更新檢查] 已是最新版 v%s", local_app_ver)
        return result

    # === .pyw 模式：實際下載 ===
    app_dir = get_app_dir()
    file_entries = manifest.get("files", [])
    if not file_entries:
        logging.warning("manifest.json 沒有 files 欄位")
        return result

    _progress("downloading")
    pending_writes = []

    max_workers = max(1, min(8, len(file_entries)))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(_download_one, fe, app_dir): fe for fe in file_entries}
        for fut in as_completed(futures):
            fe = futures[fut]
            try:
                ret = fut.result()
                if ret is not None:
                    pending_writes.append(ret)
            except Exception as e:
                err_msg = f"[{fe.get('key', '?')}] {e}"
                logging.error(err_msg)
                result.errors.append(err_msg)

    # 任一失敗則整批不寫
    if result.errors:
        logging.warning("部分檔案下載失敗（%d 個），本次不更新任何檔案", len(result.errors))
        return result

    if not pending_writes:
        logging.info("[更新檢查] 所有檔案皆為最新")
        return result

    _progress("writing")
    for key, local_filename, new_ver, content in pending_writes:
        target_path = os.path.join(app_dir, local_filename)
        if atomic_write_text(target_path, content):
            result.updated_files.append((local_filename, new_ver))
            logging.info("  ✅ 已更新 %s -> v%s", local_filename, new_ver)
        else:
            result.errors.append(f"[{key}] 寫入失敗")

    result.has_update = len(result.updated_files) > 0
    return result


def need_restart_after_update(result: UpdateResult) -> bool:
    """更新後是否需重啟（.pyw 模式且有檔案被覆寫）。"""
    return (not result.is_frozen) and result.has_update


def perform_restart() -> None:
    """重啟自己（呼叫 paths.restart_self）。"""
    restart_self()
