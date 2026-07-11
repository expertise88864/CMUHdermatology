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
import contextlib
import hashlib
import logging
import os
import re
import threading
import time
import concurrent.futures
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
# [stability r4] 整批下載的牆鐘總時限：UPDATE_TIMEOUT 只限「單次連線」，整批沒有封頂。
# 持續網路劣化下，77 個檔 × 單檔最壞 ~49s ÷ 8 worker ≈ 數百秒會卡住背景 worker。設一個
# 寬鬆上限把最壞情況封頂，又不致誤殺「慢但會成功」的首次完整下載(整批超時即整批不寫，
# 與既有『任一失敗則整批不寫』不變量一致，不會造成半套寫入)。
_DOWNLOAD_BATCH_DEADLINE_SEC = 300  # 5 分鐘
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

    # 【穩定性 2026-05-21】SHA mismatch backoff：先前該 key hash 對不上，且還在 backoff 中
    now_ts = time.time()
    until = _sha_mismatch_until.get(key, 0.0)
    if now_ts < until:
        # [IE-01 2026-07-10] backoff 中【不可】return None(=「不需更新」)—— 這個檔其實落後、只是
        # 暫時抓不到,當它不需更新的話其餘檔照寫 → 磁碟混版本(cmuh_common 五程式共用,一支混版
        # 拖垮全部,還可能 ImportError crash-loop)。改 raise 讓整批 fail-closed(caller「任一失敗
        # 整批不寫」),backoff 過後自然重試補齊。「寧可不更也不能壞」。
        raise ValueError(
            f"[{key}] 仍在下載失敗 backoff 中（剩 {until - now_ts:.0f}s）—"
            f"整批暫不更新以避免混版本")

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


@contextlib.contextmanager
def _updater_write_lock(timeout_sec: float = 30.0):
    """[IE-02 2026-07-10 + codex] 跨行程 + 跨 session 的「更新寫入」鎖。開機時 watchdog 幾乎同時拉起
    五支程式,每支啟動都背景 check_and_update、全部寫同一批 src/cmuh_common/*.py 與同名 .bak →
    .bak 互踩、回滾還原到錯版本、混 commit。用鎖讓寫檔階段序列化。

    刻意用 msvcrt.locking 對 app_dir 下 .updater_write.lock 的位元組上【OS 級鎖】,而非:
      - Local\\ named mutex —— 只在同一 Windows session 內有效(互動使用者 vs schtasks session 0 /
        多 RDP session 各自一把,不互斥);
      - 手動 O_CREAT|O_EXCL 鎖檔 + stale 判斷 —— [codex] 有 stale 回收的 remove race(兩行程同時
        判 stale、一個刪+建新鎖,另一個 remove 把新鎖刪掉 → 併發寫)。
    msvcrt.locking 是 OS 鎖:持有者行程結束/crash 時 Windows【自動釋放】,不需手動判 stale,徹底
    避開該 race;鎖檔路徑固定 → 跨 session 共享;不需 Global\\ 的 SeCreateGlobalPrivilege。

    yield True=可寫(拿到鎖,或非 Windows/鎖機制故障退回無鎖);False=逾時沒拿到 → caller 本輪放棄。
    yield 在 try/finally(非 try/except)內,不吞 body 例外。"""
    try:
        lock_path = os.path.join(get_app_dir(), ".updater_write.lock")
    except Exception:
        yield True
        return
    try:
        import msvcrt
    except Exception:
        yield True                     # 非 Windows / 無 msvcrt → 不擋(部署目標是 Windows)
        return
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    except OSError:
        logging.debug("[更新] 開更新鎖檔失敗,退回無鎖", exc_info=True)
        yield True
        return
    try:
        # [codex P1] msvcrt.locking 從「目前檔位」鎖 nbytes。新建的 .updater_write.lock 是空檔;
        # 為跨 Windows/CRT 版本穩健(不倚賴「可鎖超過 EOF」的行為),先確保檔內至少 1 byte、再把
        # 檔位歸零,固定鎖 [0,1)。本機實測空檔可鎖,此步僅作保險與可攜性。
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError:
        logging.debug("[更新] 初始化鎖檔失敗,退回無鎖", exc_info=True)
        try:
            os.close(fd)
        except OSError:
            pass
        yield True
        return
    acquired = False
    deadline = time.time() + timeout_sec
    while True:
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)   # non-blocking 取 1 byte 獨佔鎖
            acquired = True
            break
        except OSError:                # 別的行程持有中
            if time.time() >= deadline:
                break
            time.sleep(0.2)
    try:
        yield acquired                 # yield 在 try/finally 內,body 例外照常往上、鎖照樣釋放
    finally:
        if acquired:
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        try:
            os.close(fd)               # 關 fd 一併釋放鎖;不刪鎖檔(靠 OS 鎖不靠檔案存在)
        except OSError:
            pass


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
    # [stability r4] 不用 `with ThreadPoolExecutor`：其 __exit__ 會 shutdown(wait=True)
    # join 全部 worker，會讓下面 as_completed 的總時限失效(殘留 worker 仍各自跑到 request
    # 逾時)。改為手動管理，超時時 shutdown(wait=False, cancel_futures=True) 立即返回。
    executor = ThreadPoolExecutor(max_workers=max_workers)
    try:
        futures = {executor.submit(_download_one, fe, app_dir): fe for fe in file_entries}
        try:
            for fut in as_completed(futures, timeout=_DOWNLOAD_BATCH_DEADLINE_SEC):
                fe = futures[fut]
                try:
                    ret = fut.result()
                    if ret is not None:
                        pending_writes.append(ret)
                except Exception as e:
                    err_msg = f"[{fe.get('key', '?')}] {e}"
                    logging.error(err_msg)
                    result.errors.append(err_msg)
        except concurrent.futures.TimeoutError:
            err_msg = (f"下載批次超過 {_DOWNLOAD_BATCH_DEADLINE_SEC:.0f}s 總時限，"
                       f"放棄本次更新（不寫入任何檔）")
            logging.warning(err_msg)
            result.errors.append(err_msg)
    finally:
        # wait=False：不等殘留 worker(否則總時限失效)；它們各自帶 request timeout 會自行結束。
        executor.shutdown(wait=False, cancel_futures=True)

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

    # [IE-02 2026-07-10] 進入寫檔階段前取【跨行程更新鎖】:五程式開機幾乎同時 check_and_update、
    #  全部寫同一批 src/cmuh_common/*.py 與同名 .bak → .bak 互踩/回滾還原到錯版本/混 commit。
    #  序列化寫檔階段,同一時間只有一支在寫。拿不到鎖 → 本輪放棄(fail-closed)。
    with _updater_write_lock() as _acquired:
        if not _acquired:
            logging.info("[更新] 另一程式正在寫入更新,本輪放棄(避免混版本),下輪再試")
            result.errors.append("另一程式正在寫入更新,本輪放棄")
            return result
        # [IE-04 2026-07-10] 進 Phase 1 前【再查一次】suspend:上面只在函式開頭查一次,但下載最長
        #  5 分鐘,期間 watchdog 若偵測 crash-loop 寫下抑制旗標,不重查會照樣把(很可能肇事的)新版
        #  寫進磁碟。有旗標 → 整批放棄。
        _susp = get_auto_update_suspend_until()
        if _susp:
            logging.warning("[更新] 寫入前發現 auto-update 已被抑制,整批放棄(不寫任何檔)")
            result.suspended_until = _susp
            return result
        # [codex P2 2026-07-10] 取得鎖後【再驗一次磁碟版本】:併發下另一支程式可能在我下載期間已寫入
        #  【更新】的版本(鎖保證那是完整一批)。若我下載的是較舊 manifest revision,拿到鎖後不可用
        #  過時的 prepared_writes 覆蓋 → 降版 + 假的「已更新/需重啟」。
        #  只在磁碟版本【嚴格大於】本批 manifest 版本時放棄(=別人已寫更新版,寫下去會降版)。
        #  磁碟版本【等於】本批時【仍要寫】—— 那是同版的 SHA 修復(某檔損壞/缺漏),prepared_writes
        #  帶著修復內容,不可因 app 版號相同就當「已是最新」而丟棄(否則同版損壞永遠修不回)。
        #
        # [codex P2 round3] 有人問:同 app_version 但「不同 commit」的 hotfix 併發下,舊 revision 會不會
        #  在此把新 revision 覆蓋(同版回滾)？在本專案【不會發生】,且無法用 commit sha 更好地防:
        #   (1) push_helper 每次 push 都必 bump app_version(YYYY.MM.DD.serial 單調遞增、每 push 唯一);
        #       故「同 app_version」恆指【同一份已發佈 revision】= 修復,不存在內容不同的競爭 revision。
        #   (2) 就算真要比 revision 新舊,git commit sha 是【無序】的 —— 只能判斷「不同」,判斷不了「誰較新」。
        #       能單調排序 revision 的只有 app_version 本身(見(1)),所以拿 _remote_commit_sha 反而更弱。
        #  因此「等於就寫(修復)」在此是正確且無回滾風險的;真正的降版由上面的嚴格 > 擋掉。
        _disk_ver = _read_ondisk_app_version(app_dir)
        if (_disk_ver and result.manifest_app_version
                and parse_version(_disk_ver)
                > parse_version(result.manifest_app_version)):
            logging.info(
                "[更新] 取得鎖後發現磁碟版本 v%s 已【新於】本批 v%s(另一程式已寫更新版),整批放棄避免降版",
                _disk_ver, result.manifest_app_version)
            # [codex P2] 別的程式已把磁碟更新到 v_disk。若 v_disk 比「本行程啟動時載入的執行版本
            #  CURRENT_VERSION」還新,代表我正在跑舊碼、磁碟已是新碼:之後任何 lazy import 會抓到新檔
            #  → 版本錯亂(正是本批要防的 skew)。標記需重啟,讓 caller 重啟本行程、乾淨載入磁碟新版。
            #  (本行程沒寫任何檔,updated_files 放一筆說明用合成項目,好讓 main.py 的重啟提示有內容。)
            if parse_version(_disk_ver) > parse_version(CURRENT_VERSION):
                result.has_update = True
                result.updated_files.append(("(另一程式已更新，本程式需重啟)", _disk_ver))
            return result
        return _commit_pending_writes(prepared_writes, result)


def _read_ondisk_app_version(app_dir: str) -> str:
    """讀磁碟上 src/cmuh_common/version.py 的 CURRENT_VERSION(可能已被另一程式更新到最新);讀不到
    回 ''。用於取得更新寫入鎖【之後】的降版防護(module 級 import 的 CURRENT_VERSION 是啟動當下的,
    可能過時)。[codex P2]"""
    try:
        vp = os.path.join(app_dir, "src", "cmuh_common", "version.py")
        with open(vp, "r", encoding="utf-8") as f:
            m = re.search(r'CURRENT_VERSION\s*=\s*["\']([\d.]+)["\']', f.read())
        return m.group(1) if m else ""
    except Exception:
        return ""


def _commit_pending_writes(prepared_writes: list, result: UpdateResult) -> UpdateResult:
    """[IE-02/IE-04 抽出] 兩階段批次寫入。由 check_and_update 在【持有跨行程更新鎖、且再次確認未被
    suspend】之後呼叫。

    [stability] 盡量逼近「全有或全無」,避免部分檔新、部分檔舊的版本錯亂(version skew,例如
    version.py 已新但它 import 的模組還舊 → 下次啟動 ImportError 又因 SHA 短路不再重抓 → 程式 brick):
      Phase 1:每個檔的新內容先寫到各自的 .upd.tmp(含 fsync)。任一失敗 → 清掉所有 .upd.tmp、整批
              放棄(此時磁碟上的正式檔完全沒被動過)。
      Phase 2:逐檔 backup(.bak)→os.replace(同磁碟 rename,幾乎不會失敗)。萬一中途失敗,從 .bak 回滾。
    比原本逐檔 atomic_write_text 更安全:把最可能失敗的「寫內容/fsync」(磁碟滿、AV 鎖檔)全部擋在任何
    os.replace 之前。"""
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

    # [stability r4] 整批已成功 commit、不再需要回滾 → 清掉本批建立的 .bak 備份，
    # 避免無人值守長跑下程式目錄持續堆積過時的 .py.bak。務必放在上面的 rollback
    # early-return 之後(走到這裡代表已 commit)，否則會破壞失敗路徑的回滾能力。
    for written in written_files:
        if not written.existed_before:
            continue  # 新建檔沒有對應 .bak
        bak_path = written.target_path + ".bak"
        try:
            if os.path.exists(bak_path):
                os.remove(bak_path)
        except OSError:
            logging.debug("清除更新備份檔失敗 [%s]", bak_path, exc_info=True)

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
