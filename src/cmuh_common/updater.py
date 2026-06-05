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
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional

import requests

from cmuh_common.paths import get_app_dir, is_frozen, restart_self
from cmuh_common.update_policy import get_auto_update_suspend_until
from cmuh_common.version import CURRENT_VERSION, parse_version

# === GitHub repo 設定 ===
GITHUB_OWNER = "expertise88864"
GITHUB_REPO = "CMUHdermatology"
GITHUB_BRANCH = "main"
RAW_BASE = f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}/{GITHUB_BRANCH}"
MANIFEST_URL = f"{RAW_BASE}/manifest.json"
API_REF_URL = (
    f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
    f"/git/ref/heads/{GITHUB_BRANCH}"
)
RELEASE_URL = f"https://github.com/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"

UPDATE_TIMEOUT = 15
MANIFEST_TIMEOUT = 8

# 【穩定性 2026-06-03 fix①】單檔下載失敗（連線中斷 / SHA 不符）時，先對「同一個檔」
# 重試數次（CDN 舊版通常幾分鐘內就同步好），連續多次才真的判失敗。釘 commit SHA 後
# SHA 幾乎不會再不符；保留重試是為了擋短暫網路 / CDN 抖動。
_DOWNLOAD_ATTEMPTS = 3            # 單一檔最多嘗試次數
_DOWNLOAD_RETRY_DELAY_SEC = 2.0   # 每次重試間隔（秒）
# 連續重試仍失敗才進 backoff，且只鎖較短時間（取代舊版「一次 SHA 不符就鎖 1 小時」）。
_DOWNLOAD_FAIL_BACKOFF_SEC = 600  # 10 分鐘
_sha_mismatch_until: dict = {}    # key -> next allowed timestamp（記憶體，重啟即清）

# 【穩定性 2026-06-03 fix②】commit SHA 快取。
# GitHub ref API 未授權限流為每 IP 60 次/時；醫院多台電腦共用對外 NAT IP 很容易撞 403。
# 一旦 403 退回 branch 路徑（/main/file）會抓到 CDN 舊版 → SHA 對不上 → 下載失敗。
# 解法：把上次「成功」解析到的 commit SHA 快取在記憶體 + 磁碟，403 時沿用它釘住下載
#（釘 commit = 內容不可變、不會拿到舊版）。代價只是該輪可能看不到更新的版本，不會壞，
# 下次 API 通了就會拿到新 SHA。
_COMMIT_SHA_RE = re.compile(r"[0-9a-f]{40}")
_commit_sha_lock = threading.Lock()
_commit_sha_cache = ""            # 本 process 記憶體快取（最近一次成功解析到的 commit）
_commit_sha_from_cache = False    # 最近一次 _resolve_commit_sha 是否沿用舊快取


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
    suspended_until: float = 0.0


@dataclass(frozen=True)
class _WrittenFile:
    target_path: str
    existed_before: bool


_FILE_OP_RETRY_DELAYS_SEC = (0.05, 0.15, 0.35)


def _file_op_with_retry(label: str, func, *args):
    """Retry short-lived Windows file locks during update writes/rollback."""
    last_exc = None
    total_attempts = len(_FILE_OP_RETRY_DELAYS_SEC) + 1
    for attempt in range(total_attempts):
        try:
            return func(*args)
        except OSError as e:
            last_exc = e
            if attempt >= len(_FILE_OP_RETRY_DELAYS_SEC):
                break
            delay = _FILE_OP_RETRY_DELAYS_SEC[attempt]
            logging.debug(
                "[update] %s failed (%s), retry %d/%d in %.2fs",
                label, e, attempt + 2, total_attempts, delay,
            )
            time.sleep(delay)
    raise last_exc


def _replace_file_with_retry(src: str, dst: str) -> None:
    _file_op_with_retry(f"replace {src} -> {dst}", os.replace, src, dst)


def _copy_file_with_retry(src: str, dst: str) -> None:
    import shutil
    _file_op_with_retry(f"copy {src} -> {dst}", shutil.copy2, src, dst)


def _remove_file_with_retry(path: str) -> None:
    _file_op_with_retry(f"remove {path}", os.remove, path)


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
                _replace_file_with_retry(backup_path, written.target_path)
            elif os.path.exists(written.target_path):
                _remove_file_with_retry(written.target_path)
        except Exception as e:
            logging.error("更新回滾失敗 [%s]: %s", written.target_path, e)
            errors.append(f"[rollback] {written.target_path}: {e}")
    return errors


def _commit_sha_cache_path() -> str:
    from cmuh_common.paths import get_settings_dir
    return os.path.join(get_settings_dir(), "last_commit_sha.txt")


def _load_cached_commit_sha() -> str:
    """回上次成功解析的 commit SHA（記憶體優先，否則讀磁碟）。沒有則回 ''。"""
    global _commit_sha_cache
    with _commit_sha_lock:
        if _commit_sha_cache:
            return _commit_sha_cache
    try:
        with open(_commit_sha_cache_path(), "r", encoding="utf-8") as f:
            sha = f.read(64).strip().lower()
    except Exception:
        return ""
    if _COMMIT_SHA_RE.fullmatch(sha):
        with _commit_sha_lock:
            if not _commit_sha_cache:
                _commit_sha_cache = sha
        return sha
    return ""


def _save_cached_commit_sha(sha: str) -> None:
    """成功解析 commit 後寫回快取（記憶體 + 磁碟）。SHA 沒變則不寫磁碟。"""
    global _commit_sha_cache
    sha = (sha or "").strip().lower()
    if not _COMMIT_SHA_RE.fullmatch(sha):
        return
    with _commit_sha_lock:
        if sha == _commit_sha_cache:
            return
        _commit_sha_cache = sha
    try:
        from cmuh_common.atomic_io import atomic_write_text
        atomic_write_text(_commit_sha_cache_path(), sha + "\n")
    except Exception:
        logging.debug("[update] 寫入 commit SHA 快取失敗", exc_info=True)


def _resolve_commit_sha(timeout: float) -> str:
    """解析 main 最新 commit SHA。

    - 成功 → 更新快取並回新 SHA。
    - 失敗（403 限流 / 連線中斷）→ 沿用上次成功的快取 SHA（釘住下載、避免 branch 舊版）。
    - 連快取都沒有 → 回 ''（呼叫端最後才退回 branch 路徑）。
    """
    global _commit_sha_from_cache
    _commit_sha_from_cache = False
    try:
        ref_url = f"{API_REF_URL}?t={time.time_ns()}"
        ref_resp = requests.get(
            ref_url,
            timeout=timeout,
            headers={"User-Agent": "CMUH-Dermatology-Updater"},
        )
        ref_resp.raise_for_status()
        sha = str(ref_resp.json()["object"]["sha"]).strip().lower()
        if not _COMMIT_SHA_RE.fullmatch(sha):
            raise ValueError("GitHub ref API 回傳非預期 commit SHA")
        _save_cached_commit_sha(sha)
        return sha
    except Exception as e:
        cached = _load_cached_commit_sha()
        if cached:
            _commit_sha_from_cache = True
            logging.warning(
                "取得 GitHub commit SHA 失敗（%s），沿用上次成功的 commit %s.. 釘住下載",
                e, cached[:12],
            )
            return cached
        logging.warning(
            "取得 GitHub commit SHA 失敗且無快取，改用 branch fallback: %s", e
        )
        return ""


def _fetch_manifest(timeout: float = MANIFEST_TIMEOUT) -> dict:
    """取 manifest。優先用「API 當下解析到的『新』commit」釘住下載：同一次更新所有檔
    來自同一 commit、避開 Raw 分支短暫舊清單。

    【2026-06-05 修正】若 commit SHA 是「API 失敗後沿用的舊磁碟快取」，**不可**拿它去
    釘 manifest —— 否則一旦 api.github.com 長期不可達（醫院防火牆常擋 api.github.com
    卻放行 raw.githubusercontent.com），機器會被永遠釘在那個舊 commit、再也更新不過去。
    此時改走 branch 最新版（搭配 cache-buster），以「一定拿得到最新」為優先（branch CDN
    至多短暫舊，下次排程檢查即修正），徹底避免「永久卡在舊版」。
    """
    commit_sha = _resolve_commit_sha(timeout)
    # 只有「API 當下成功取得的新 commit」才用來釘；舊快取(_commit_sha_from_cache=True)走 branch。
    pinned_sha = "" if _commit_sha_from_cache else commit_sha
    remote_ref = pinned_sha or GITHUB_BRANCH
    url = (
        f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}"
        f"/{remote_ref}/manifest.json?v={remote_ref}&t={time.time_ns()}"
    )
    resp = requests.get(url, timeout=timeout)
    resp.raise_for_status()
    resp.encoding = 'utf-8'
    manifest = resp.json()
    if pinned_sha:
        manifest["_remote_commit_sha"] = pinned_sha
        manifest["_remote_commit_sha_from_cache"] = False
    return manifest


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


def _download_verified(key: str, base_url: str, expected_sha: str) -> Optional[str]:
    """下載並驗 SHA256；遇連線錯誤或 SHA 不符（多半是 CDN 舊版）時對「同一個檔」重試。

    第一次用乾淨網址（保留多台電腦共用 CDN 快取的好處）；重試時才加 nanotime
    旁路掉可能的舊快取。成功回 content；嘗試 _DOWNLOAD_ATTEMPTS 次仍失敗回 None。
    """
    last_err = ""
    for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
        # 第一次保持原網址讓多台電腦共用 CDN 快取；重試才打 nanotime 防快取
        url = base_url if attempt == 1 else f"{base_url}&t={time.time_ns()}"
        try:
            resp = requests.get(url, timeout=UPDATE_TIMEOUT)
            resp.raise_for_status()
            resp.encoding = 'utf-8'
            content = resp.text
        except Exception as e:
            last_err = f"連線錯誤: {e}"
        else:
            if not expected_sha:
                return content
            actual_sha = _sha256_text(content)
            if actual_sha == expected_sha:
                return content
            last_err = (
                f"SHA256 不符（預期 {expected_sha[:12]}.. 實際 {actual_sha[:12]}..）"
            )
        if attempt < _DOWNLOAD_ATTEMPTS:
            logging.info(
                "  [%s] 下載第 %d/%d 次失敗（%s），%.0fs 後重試",
                key, attempt, _DOWNLOAD_ATTEMPTS, last_err, _DOWNLOAD_RETRY_DELAY_SEC,
            )
            time.sleep(_DOWNLOAD_RETRY_DELAY_SEC)
    logging.warning(
        "  [%s] 下載重試 %d 次仍失敗：%s", key, _DOWNLOAD_ATTEMPTS, last_err
    )
    return None


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

    # 內容 hash 改變時網址也會改變；相同內容仍可共用 CDN cache。
    cache_key = expected_sha or expected_version
    remote_ref = file_entry.get("_remote_commit_sha") or GITHUB_BRANCH
    remote_base = (
        f"https://raw.githubusercontent.com/{GITHUB_OWNER}/{GITHUB_REPO}"
        f"/{remote_ref}"
    )
    base_url = f"{remote_base}/{remote_path}?v={cache_key}"
    logging.info("  [%s] 偵測到新版（v%s -> v%s），下載中...", key, local_ver, expected_version)

    # 【fix①】單檔重試：CDN 舊版 / 短暫連線抖動通常幾分鐘內自癒，連續多次才判失敗。
    content = _download_verified(key, base_url, expected_sha)
    if content is None:
        # 重試多次仍失敗 → 短期 backoff（取代舊版「一次就鎖 1 小時」），
        # 避免狂打 GitHub；CDN 同步好後很快就能再抓。
        _sha_mismatch_until[key] = now_ts + _DOWNLOAD_FAIL_BACKOFF_SEC
        raise ValueError(
            f"[{key}] 下載重試 {_DOWNLOAD_ATTEMPTS} 次仍失敗"
            f"（連線錯誤或 SHA256 不符）— 暫停 {int(_DOWNLOAD_FAIL_BACKOFF_SEC // 60)} 分鐘"
        )
    # 下載成功 → 清掉先前可能殘留的 backoff 標記
    _sha_mismatch_until.pop(key, None)

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

    # Crash-loop protection applies only to deployments that write in place.
    # Frozen builds may still perform a read-only version check.
    if write_files is None:
        write_files = not result.is_frozen
    if write_files:
        result.suspended_until = get_auto_update_suspend_until()
        if result.suspended_until:
            logging.warning(
                "[update-policy] auto-update suspended until %s",
                time.strftime("%Y-%m-%d %H:%M:%S",
                              time.localtime(result.suspended_until)),
            )
            return result

    _progress("fetching_manifest")
    try:
        manifest = _fetch_manifest()
    except Exception as e:
        logging.error("取得 manifest.json 失敗: %s", e)
        result.errors.append(f"無法連線 GitHub: {e}")
        return result

    result.checked = True
    result.manifest_app_version = manifest.get("app_version", "")

    if (write_files
            and parse_version(result.manifest_app_version)
            < parse_version(CURRENT_VERSION)):
        logging.warning(
            "[更新檢查] 遠端 manifest v%s 低於本機 v%s，拒絕降版",
            result.manifest_app_version, CURRENT_VERSION,
        )
        return result

    if (write_files
            and manifest.get("_remote_commit_sha_from_cache")
            and parse_version(result.manifest_app_version)
            <= parse_version(CURRENT_VERSION)):
        logging.warning(
            "[更新檢查] GitHub API 失敗且只取得 cached commit %s..；"
            "manifest v%s 未高於本機 v%s，拒絕寫檔以避免舊快取覆蓋新版",
            str(manifest.get("_remote_commit_sha", ""))[:12],
            result.manifest_app_version, CURRENT_VERSION,
        )
        return result

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
    remote_commit_sha = manifest.get("_remote_commit_sha", "")
    file_entries = [
        {**entry, "_remote_commit_sha": remote_commit_sha}
        for entry in manifest.get("files", [])
    ]
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

    # [stability] 兩階段批次寫入，盡量逼近「全有或全無」，避免部分檔新、部分檔
    # 舊的版本錯亂(version skew，例如 version.py 已新但它 import 的模組還舊 →
    # 下次啟動 ImportError 又因 SHA 短路不再重抓 → 程式 brick)：
    #   Phase 1：每個檔的新內容先寫到各自的 .upd.tmp（含 fsync）。任一失敗 → 清掉
    #            所有 .upd.tmp、整批放棄（此時磁碟上的正式檔完全沒被動過）。
    #   Phase 2：逐檔 backup(.bak)→os.replace（同磁碟 rename，幾乎不會失敗）。萬一
    #            中途失敗，從 .bak 回滾已替換的檔。
    # 比原本逐檔 atomic_write_text 更安全：把最可能失敗的「寫內容/fsync」(磁碟滿、
    # AV 鎖檔)全部擋在任何 os.replace 之前。
    import tempfile

    staged: list = []  # (tmp, target, existed_before, key, local_filename, new_ver)
    for key, local_filename, new_ver, content, target_path in prepared_writes:
        try:
            target_dir = os.path.dirname(target_path) or "."
            os.makedirs(target_dir, exist_ok=True)
            fd, tmp_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(target_path)}.",
                suffix=".upd.tmp", dir=target_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            staged.append((tmp_path, target_path, os.path.exists(target_path),
                           key, local_filename, new_ver))
        except Exception as e:
            result.errors.append(f"[{key}] 暫存寫入失敗: {e}")
            break

    if result.errors:
        # Phase 1 失敗：清掉所有 .upd.tmp，正式檔一個都沒動
        for entry in staged:
            try:
                if os.path.exists(entry[0]):
                    os.remove(entry[0])
            except OSError:
                logging.debug("移除暫存檔失敗 [%s]", entry[0], exc_info=True)
        logging.warning("更新暫存階段失敗，整批不寫入（正式檔未變動）")
        return result

    # Phase 2：逐檔 backup→replace（同磁碟 rename，幾乎不會失敗）
    written_files: list[_WrittenFile] = []
    for tmp_path, target_path, existed_before, key, local_filename, new_ver in staged:
        try:
            if existed_before:
                _copy_file_with_retry(target_path, target_path + ".bak")
            _replace_file_with_retry(tmp_path, target_path)
            result.updated_files.append((local_filename, new_ver))
            written_files.append(_WrittenFile(target_path, existed_before))
            logging.info("  ✅ 已更新 %s -> v%s", local_filename, new_ver)
        except Exception as e:
            result.errors.append(f"[{key}] 寫入失敗: {e}")
            break

    if result.errors:
        rollback_errors = _rollback_written_files(written_files)
        result.errors.extend(rollback_errors)
        result.updated_files.clear()
        # 清掉任何殘留、尚未 replace 的 .upd.tmp
        for entry in staged:
            try:
                if os.path.exists(entry[0]):
                    os.remove(entry[0])
            except OSError:
                pass
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
