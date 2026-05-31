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

# 【穩定性 2026-05-21】SHA256 mismatch backoff：CDN 拿到舊版時，本機記憶體
# 標記該 key 在 N 秒內不再重抓，避免每次 check 都死循環打 GitHub。
_SHA_MISMATCH_BACKOFF_SEC = 3600  # 1 小時
_sha_mismatch_until: dict = {}    # key -> next allowed timestamp


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


@dataclass(frozen=True)
class _WrittenFile:
    target_path: str
    existed_before: bool


def _resolve_target_path(app_dir: str, local_filename: str) -> str:
    """Resolve a manifest target while keeping writes inside the app directory."""
    if not isinstance(local_filename, str) or not local_filename.strip():
        raise ValueError("更新路徑不得為空")
    if os.path.isabs(local_filename):
        raise ValueError(f"更新路徑必須為相對路徑: {local_filename}")
    app_root = os.path.realpath(os.path.abspath(app_dir))
    target_path = os.path.abspath(os.path.join(app_root, local_filename))
    target_real_path = os.path.realpath(target_path)
    try:
        common_path = os.path.commonpath([app_root, target_real_path])
    except ValueError as e:
        raise ValueError(f"更新路徑無效: {local_filename}") from e
    if os.path.normcase(target_real_path) == os.path.normcase(app_root):
        raise ValueError(f"更新路徑不得指向程式目錄: {local_filename}")
    if os.path.normcase(common_path) != os.path.normcase(app_root):
        raise ValueError(f"更新路徑超出程式目錄: {local_filename}")
    return target_path


def _rollback_written_files(written_files: list[_WrittenFile]) -> list[str]:
    """Restore files already replaced when a later write in the batch fails."""
    errors = []
    for written in reversed(written_files):
        try:
            if written.existed_before:
                backup_path = written.target_path + ".bak"
                if not os.path.exists(backup_path):
                    raise FileNotFoundError(f"找不到備份: {backup_path}")
                os.replace(backup_path, written.target_path)
            elif os.path.exists(written.target_path):
                os.remove(written.target_path)
        except Exception as e:
            logging.error("更新回滾失敗 [%s]: %s", written.target_path, e)
            errors.append(f"[rollback] {written.target_path}: {e}")
    return errors


def _fetch_manifest(timeout: float = MANIFEST_TIMEOUT) -> dict:
    """取 manifest.json，加 cache-buster（每小時換一次，享受 GitHub CDN edge cache）。"""
    # 【效能 2026-05-21】每小時 bucket：同一小時內所有人共用 CDN cache（省 100-300ms）
    url = f"{MANIFEST_URL}?t={int(time.time() // 3600)}"
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


def _sha256_local_file(local_path: str) -> str:
    """計算本地檔 SHA256（與 sync_manifest 一致的 LF normalize 演算法）。"""
    try:
        with open(local_path, 'rb') as f:
            content = f.read()
        content = content.replace(b'\r\n', b'\n')
        return hashlib.sha256(content).hexdigest()
    except Exception:
        return ""


def _download_one(file_entry: dict, app_dir: str) -> Optional[tuple]:
    """下載單一檔案；如本地已是最新或內容版本未較新則回 None。

    【O1 優化】先做 SHA256 比對，若本地檔已等於 manifest 預期 hash 就跳過下載。
    這修正了原本「子模組沒有 CURRENT_VERSION 字樣，永遠被誤判為 v0.0.0
    需更新」的 bug — 原本每次啟動都重抓 21 個檔。

    回傳 (key, local_filename, new_version, content) 或 None。
    """
    key = file_entry["key"]
    remote_path = file_entry["remote_path"]
    local_filename = file_entry["local_filename"]
    expected_version = file_entry.get("version", "0.0.0")
    expected_sha = (file_entry.get("sha256") or "").lower().strip()

    local_path = _resolve_target_path(app_dir, local_filename)

    # [O1] SHA256 短路：本地內容已是 manifest 期望版 → 直接跳過（最常見路徑）
    if expected_sha and os.path.exists(local_path):
        local_sha = _sha256_local_file(local_path)
        if local_sha == expected_sha:
            return None

    # 【穩定性 2026-05-21】SHA mismatch backoff：先前該 key hash 對不上，且還在 backoff 中 → skip
    now_ts = time.time()
    until = _sha_mismatch_until.get(key, 0.0)
    if now_ts < until:
        logging.debug("[%s] SHA mismatch backoff 中（剩 %.0fs），跳過", key, until - now_ts)
        return None

    local_ver = _read_local_version(local_path)

    # 版本比對（仍保留作為次要判斷；若本地檔含 CURRENT_VERSION 字樣才有意義）
    if parse_version(local_ver) >= parse_version(expected_version):
        # 此分支：本地版本足夠新，但 hash 不符（可能行尾差異或檔案被改過）→ 仍重下載
        if expected_sha and os.path.exists(local_path):
            logging.info("  [%s] 版本 v%s 已新但 SHA256 不符，重新下載", key, local_ver)
        else:
            return None

    # cache-buster 也改每小時，享受 CDN
    url = f"{RAW_BASE}/{remote_path}?t={int(time.time() // 3600)}"
    logging.info("  [%s] 偵測到新版（v%s -> v%s），下載中...", key, local_ver, expected_version)

    resp = requests.get(url, timeout=UPDATE_TIMEOUT)
    resp.raise_for_status()
    resp.encoding = 'utf-8'
    content = resp.text

    # SHA256 校驗（manifest 有指定才驗）
    if expected_sha:
        actual_sha = _sha256_text(content)
        if actual_sha != expected_sha:
            # 【穩定性 2026-05-21】記下 backoff，避免下次 check 又重抓爛 CDN
            _sha_mismatch_until[key] = now_ts + _SHA_MISMATCH_BACKOFF_SEC
            raise ValueError(
                f"[{key}] SHA256 不符（預期 {expected_sha[:12]}.. 實際 {actual_sha[:12]}..）"
                f" — 暫停 1 小時"
            )

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
    prepared_writes = []
    seen_targets = set()
    for key, local_filename, new_ver, content in sorted(
        pending_writes, key=lambda item: item[1]
    ):
        try:
            target_path = _resolve_target_path(app_dir, local_filename)
        except ValueError as e:
            result.errors.append(f"[{key}] {e}")
            continue
        normalized_target = os.path.normcase(target_path)
        if normalized_target in seen_targets:
            result.errors.append(f"[{key}] 更新清單重複目標: {local_filename}")
            continue
        seen_targets.add(normalized_target)
        prepared_writes.append((key, local_filename, new_ver, content, target_path))

    if result.errors:
        logging.warning("更新清單驗證失敗，取消整批寫入")
        return result

    written_files: list[_WrittenFile] = []
    for key, local_filename, new_ver, content, target_path in prepared_writes:
        existed_before = os.path.exists(target_path)
        if atomic_write_text(target_path, content):
            result.updated_files.append((local_filename, new_ver))
            written_files.append(_WrittenFile(target_path, existed_before))
            logging.info("  ✅ 已更新 %s -> v%s", local_filename, new_ver)
        else:
            result.errors.append(f"[{key}] 寫入失敗")
            break

    if result.errors:
        rollback_errors = _rollback_written_files(written_files)
        result.errors.extend(rollback_errors)
        result.updated_files.clear()
        logging.warning("更新寫入失敗，已回滾 %d 個檔案", len(written_files))
        return result

    # [O8] 預編譯 .pyc：剛覆寫的 .py 立即 compile，省下次 import 時的 byte-compile 開銷
    if written_files:
        _precompile_files([written.target_path for written in written_files])

    result.has_update = len(result.updated_files) > 0
    return result


def _precompile_files(paths: list) -> None:
    """[O8] 對剛覆寫的 .py 檔做 byte-compile，產生 __pycache__/*.pyc。"""
    try:
        import py_compile
        compiled = 0
        for p in paths:
            if not p.endswith('.py'):
                continue
            try:
                py_compile.compile(p, doraise=True, quiet=1)
                compiled += 1
            except Exception:
                logging.debug("py_compile 失敗 [%s]", p, exc_info=True)
        if compiled:
            logging.info("[O8] 已預編譯 %d 個 .py 為 .pyc", compiled)
    except Exception:
        logging.debug("_precompile_files 例外", exc_info=True)


def need_restart_after_update(result: UpdateResult) -> bool:
    """更新後是否需重啟（.pyw 模式且有檔案被覆寫）。"""
    return (not result.is_frozen) and result.has_update


def perform_restart() -> None:
    """重啟自己（呼叫 paths.restart_self）。"""
    restart_self()
