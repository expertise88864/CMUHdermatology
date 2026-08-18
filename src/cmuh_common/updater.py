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
import json
import logging
import os
import re
import sys
import threading
import time
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
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
    # 這個檔【還沒被 replace 進去】時的暫存檔路徑（只有從交易日誌復原時才有值）。
    # 用途見 `_rollback_written_files`：分辨「.bak 不見」是「還沒換到它」還是
    # 「換過了但備份被刪掉」—— 兩者的正確處置完全相反。
    staged_path: str = ""


@dataclass
class RollbackOutcome:
    """回滾的結果。★用具名欄位而不是 tuple★

    2026-08-02：這裡本來是 3-tuple，這一批要再加一類（`terminal`）。上一批
    （reg52）就是因為改動一個多回傳值的函式、漏改其中一條 return 而讓呼叫端
    解包炸掉。多一個欄位不會讓任何呼叫端解包錯位，這個型別就是為此存在的。

    * `restored`  真的還原回舊版的檔（★誠實計數★，不含「崩潰時還沒輪到」的）
    * `unresolved` 這次沒還原成功、但【下次可能會成功】（防毒/權限暫時鎖住）
    * `terminal`  救不回來的（備份不存在、或日誌路徑落在程式目錄外）
    * `errors`    給人看的訊息，涵蓋上面兩類失敗
    """
    errors: list
    unresolved: list
    restored: list
    terminal: list


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


def _fsync_path(path: str) -> bool:
    """把 `path` 的內容刷到碟上。→ 成功與否（★不丟例外，由呼叫端決定處置★）。

    ★一定要用【可寫入】的 handle★（2026-08-03 外審第 4 輪，實測）：
    Windows 的 `os.fsync` 底層是 CRT 的 `_commit()`，而 `_commit()` 對唯讀
    file descriptor 一律回 `EBADF`。也就是說

        with open(p, "rb") as f: os.fsync(f.fileno())    # ← 在 Windows 永遠失敗

    這個寫法在本專案唯一會跑的平台上【百分之百是 no-op】，只是錯誤被吞掉、
    看起來像有做。實測：

        rb  → OSError [Errno 9] Bad file descriptor
        rb+ → OK

    所以 `_make_backup_atomically`（2026-08-01 起）宣稱的「fsync（斷電也要
    落到碟上）」其實從來沒有生效過 ——★宣稱要對得上實作★。
    """
    try:
        with open(path, "rb+") as f:
            os.fsync(f.fileno())
        return True
    except OSError:
        logging.error("[更新] fsync 失敗，內容不保證已落到碟上 [%s]", path,
                      exc_info=True)
        return False


def _restore_keeping_backup(backup: str, target: str) -> bool:
    """從備份還原，★但不要把備份用掉★（2026-08-03 外審 P2）。

    原本是 `os.replace(backup, target)` —— 一次搬移就把備份消耗掉了。
    可是「這一筆已經還原」要等 journal 的狀態落地才算數；若接著寫狀態與清日誌
    都失敗（防毒同時鎖住兩者），磁碟上就是「pending 而且沒有備份」，下一輪
    直接判成救不回來 ——★那正是這一批要消滅的假 terminal★。

    改成「複製到暫存 → 原子換名」：還原本身仍然是原子的，備份留到交易確實
    收乾淨之後才刪（見 `_drop_backups`）。中途死掉也沒關係 —— 備份還在，
    下一輪照著同一份再做一次，結果完全相同（冪等）。

    ★換名前要 fsync★：rename 是原子的沒錯，但那只保證【名字】的原子性，
    不保證暫存檔的【內容】已經落到碟上。換完名之後、快取還沒刷回去之前斷電，
    開機後看到的就是「正式檔已經改名成功、內容卻是半截」。

    → 回傳「這一筆算不算收工」（fsync 成功＝True）。

    ★fsync 失敗時仍然要換過去★（2026-08-03 外審第 4 輪，不採納原建議）：
    外審建議「fsync 失敗就不要換、也不要清備份」。不清備份是對的，但
    【不換】會把機器留在半新半舊的壞版本上，而換過去至少讓臨床程式跑在
    完好的舊版。兩者的斷電風險其實一樣 —— 因為 fsync 沒成功這一筆就【不
    算收工】：日誌與 `.bak` 都留著，下一輪重做一次（冪等）。也就是說

        換 + 留日誌/備份 → 斷電最壞是半截檔，下一輪修得回來；程式當下可用
        不換 + 留日誌/備份 → 斷電最壞是壞版本，下一輪修得回來；程式當下壞的

    後者沒有比較安全，只是比較晚可用。★可修復性來自留著日誌與備份，不是
    來自不動手★，所以照換，只是不報「已還原」。
    """
    tmp = target + ".restore.tmp"
    try:
        _copy_file_with_retry(backup, tmp)
        durable = _fsync_path(tmp)
        _replace_file_with_retry(tmp, target)
    except BaseException:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise
    return durable


def _drop_backups(targets) -> None:
    """交易確實收乾淨之後才清備份。刪不掉不致命（只是多留著）。"""
    for t in targets or []:
        try:
            bak = t + ".bak"
            if os.path.exists(bak):
                os.remove(bak)
        except OSError:
            logging.debug("[更新] 清除備份失敗 [%s]", t, exc_info=True)


def _replace_file_with_retry(src: str, dst: str) -> None:
    _file_op_with_retry(f"replace {src} -> {dst}", os.replace, src, dst)


def _copy_file_with_retry(src: str, dst: str) -> None:
    import shutil
    _file_op_with_retry(f"copy {src} -> {dst}", shutil.copy2, src, dst)


def _make_backup_atomically(target_path: str) -> None:
    """把 target 備份成 `target.bak`，而且【備份要嘛完整、要嘛不存在】。

    ★[2026-08-01 外審 P1] 不可以直接 copy 到 `.bak` 這個權威名字★
    交易日誌是在動第一個正式檔【之前】就落地的，所以復原程序看到 `.bak` 存在就會
    當它是可信的還原來源。而原本的做法是 `shutil.copy2(target, target + ".bak")`
    ——直接往那個權威名字寫。在這中間被砍（關機、斷電、watchdog 重啟）的話：
    正式檔還沒被動過、是完好的舊版，`.bak` 卻是【截斷的半個檔】；下次啟動的復原
    程序就會拿那個半截檔覆蓋掉完好的正式檔 —— **復原程序自己製造了損毀**。

    改成先寫 `.bak.tmp`、fsync（斷電也要落到碟上，不是只到 OS 快取），再用
    `os.replace` 原子換名。同磁碟的 rename 是原子的，所以 `.bak` 只會是
    「完整的舊版」或「還不存在」，不會是半截。
    """
    tmp_path = target_path + ".bak.tmp"
    try:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    except OSError:
        logging.debug("[更新] 清除殘留的 .bak.tmp 失敗 [%s]", tmp_path,
                      exc_info=True)
    _copy_file_with_retry(target_path, tmp_path)
    if not _fsync_path(tmp_path):
        # ★備份這一條可以【停下來】，而且應該停★（2026-08-03 外審第 4 輪）
        #   還原路徑不能停（停下來＝臨床程式留在壞版本上），但備份不同：
        #   做不出可信的備份就【不要更新】—— 更新是可以延後的，備份卻是等一下
        #   出事時唯一的退路。拿一份不保證完整的 .bak 去換掉正式檔，正是這個
        #   函式的 docstring 說要避免的「復原程序自己製造損毀」。
        #   丟出去 → Phase 2 中止 → 已寫的檔回滾 → 這一輪不更新，程式照跑舊版。
        with contextlib.suppress(OSError):
            os.remove(tmp_path)
        raise OSError(f"備份 fsync 失敗，不敢拿它換掉正式檔：{tmp_path}")
    _replace_file_with_retry(tmp_path, target_path + ".bak")


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


# ── 更新交易日誌（P1-08 2026-08-01）────────────────────────────────────────
# ★既有的兩階段寫入擋不住「行程在 Phase 2 中途死掉」★
#   `_commit_pending_writes` 已經做到「先把全部新內容寫成 .upd.tmp（含 fsync），
#   確定都寫得出來才開始 os.replace」。那擋掉了最常見的失敗（磁碟滿、防毒鎖檔）。
#   但 Phase 2 本身是【逐檔】replace 的 —— 行程在中途被砍（watchdog 重啟、使用者
#   關機、斷電、更新完自我重啟）時，磁碟上就是「一部分新、一部分舊」：
#     * `version.py` 已經是新版，它 import 的模組還是舊的 → 下次啟動 ImportError；
#     * 而且 SHA 比對會認為「版本已經是新的」→ 不再重抓 → **程式 brick**。
#   process 內的 rollback 幫不上忙 —— 那個 process 已經不在了。
#
#   所以在動第一個正式檔【之前】先落一份日誌：這一批要動哪些檔、哪些是新建的。
#   下次啟動看到日誌還在，就代表上次那批沒有走完 → 用 .bak 全部回滾。
#
# ★為什麼一律回滾，不試著「往前滾完」★
#   日誌不記「做到第幾個」（那要逐檔 fsync，代價高且仍有視窗）。回滾到一個【完整的
#   舊版本】永遠是安全的：更新下一輪會自己再來一次。往前滾則需要那些 .upd.tmp 還在、
#   內容還正確 —— 假設更多、錯了更慘。
JOURNAL_FILENAME = ".updater_commit.journal"
JOURNAL_SCHEMA = 1


def _journal_path(app_dir: str) -> str:
    return os.path.join(app_dir, JOURNAL_FILENAME)


def _write_commit_journal(app_dir: str, entries: list) -> bool:
    """在動第一個正式檔之前落日誌。回是否成功。

    ★寫不出日誌就【不要開始 commit】★ 沒有日誌的中途崩潰是不可復原的（沒人知道
    動過哪些檔）。寧可本輪不更新 —— 那只是「晚一點更新」，而 brick 是要人去現場的。

    ★[2026-08-01 外審第 2 輪 P1] 寫法本身也要原子★
    原本是直接 `open(path, "w")` —— 那會先把既有日誌【截斷】，失敗時的處置又是把它
    刪掉。`_rewrite_journal_for_retry` 正是拿這支去改寫既有日誌的，所以磁碟滿／IO
    錯誤時，它會把「原本那份完好的日誌」一起毀掉，然後留下一個半套的磁碟卻沒有任何
    交易標記 —— 跟它 docstring 宣稱的「失敗時保留原日誌」完全相反。
    改成寫 `.tmp` → fsync → 原子換名，失敗只清 tmp，【絕不】碰目的檔。
    """
    path = _journal_path(app_dir)
    tmp_path = path + ".tmp"
    try:
        payload = {
            "schema": JOURNAL_SCHEMA,
            "started": datetime.now().isoformat(timespec="seconds"),
            "pid": os.getpid(),
            # ★`staged` 是那個檔【還沒被 replace 進去】的暫存檔路徑★
            #   復原時用它分辨「.bak 不見」的兩種原因，見 `_rollback_written_files`。
            #   `os.replace(tmp, target)` 會把 tmp 消耗掉，所以 tmp 還在 ⇒ 還沒換過。
            "files": [{"target": t, "existed_before": bool(e),
                       "staged": s, "state": "pending"}
                      for t, e, s in (
                          tuple(x) + ("",) * (3 - len(tuple(x)))
                          for x in entries)],
        }
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())      # 斷電也要留得住，否則等於沒寫
        os.replace(tmp_path, path)    # 原子換名：目的檔要嘛舊的、要嘛完整的新的
        return True
    except Exception:
        logging.warning("[更新] 寫不出交易日誌 → 本輪不進行寫入（避免無法復原的中途崩潰）",
                        exc_info=True)
        # ★只清 tmp★ 目的檔可能是【還有用的舊日誌】（改寫重試清單時就是這樣），
        #   在這裡刪掉它等於把半套磁碟的唯一修復線索也一起丟了。
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return False


def _clear_commit_journal(app_dir: str) -> bool:
    """整批 commit 成功後清掉日誌。→ 日誌【確定不存在】才回 True。

    ★一定要在刪 .bak 之前★ 先刪 .bak 再刪日誌的話，中間崩潰會留下「日誌說要回滾、
    但備份已經沒了」的狀態。

    ★[2026-08-01 外審 P1] 必須回報成敗★ 原本吞掉例外又不回傳任何東西，呼叫端
    照樣往下刪 .bak —— 於是防毒/暫時鎖檔讓 `os.remove` 失敗時，會變成
    「日誌還在、備份只剩一部分」：下次啟動照日誌回滾，有備份的退回舊版、
    備份已被刪掉的留在新版 → 混版本。所謂「日誌先消失、才動備份」的不變量
    其實沒有被強制。現在刪完【回讀確認】，沒確認到就不准碰備份。
    """
    path = _journal_path(app_dir)
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        logging.warning("[更新] 清除交易日誌失敗 → 保留全套備份不刪", exc_info=True)
        return False
    # 回讀確認：remove 沒拋例外不代表檔案真的不在了（網路磁碟/防毒都可能）
    still_there = os.path.exists(path)
    if still_there:
        logging.warning("[更新] 交易日誌刪除後仍然存在 → 保留全套備份不刪")
    return not still_there


def recover_incomplete_update(app_dir: str = "") -> "list[str]":
    """啟動時呼叫：上一批更新若沒走完，用 .bak 把它整批回滾。→ 已還原的檔案清單。

    ★誠實邊界★ 這是在【應用程式自己的行程裡】跑的。如果上次的半套更新已經嚴重到
    連程式都啟動不了（例如 main.py 自己被換到一半），這段就不會被執行到 ——
    真要涵蓋那種情況需要一個獨立的啟動器。目前的實際涵蓋範圍是：
    「壞掉的是被 import 的模組、但進入點還跑得起來」，那也是最常見的形狀。
    watchdog 是另一個行程，它也會呼叫這裡（見 watchdog_runner），涵蓋面因此大一些。

    ★誠實邊界之二：這套保護【保護不到引進它自己的那一次更新】★
    （2026-08-01 外審指出，記錄下來而不是假裝修好了。）
    套用更新的是【已經載入記憶體】的那份 updater 程式碼。把 updater.py 覆蓋掉並
    不會改變正在跑的 `_commit_pending_writes`。所以 v2026.08.01.1 → .2 那一次
    ——也就是引進交易日誌的那次——是由【沒有日誌機制的舊 updater】執行的：
    它若在中途死掉，一樣會留下無法復原的混版本。要從第二次更新起才真的有保護。
    外審建議「拆成兩個 release 先鋪 updater 再鋪其餘」；那個窗口已經過去了
    （.2 早就上線，現在是 v2026.08.01.8），所以這裡不做假動作，只把限制寫明：
    ★以後凡是改動 commit/回滾流程，都要記得新邏輯是從【下一次】更新才生效。★
    要真正消除這個窗口，需要一個不在被替換檔案清單裡的獨立啟動器（尚未實作）。

    ★[2026-08-01 外審 P1] 復原必須拿【和寫入同一把】跨行程鎖★
    開機時 watchdog 幾乎同時拉起五支程式，每支都會跑到這裡。沒有鎖的話：
    A 正在 Phase 2 寫到一半（日誌已落地），B 啟動 → 看到日誌 → 判定「上次崩潰了」
    → 把 A 剛換好的檔回滾、對 A 還沒動到的檔報「找不到備份」，然後**把日誌清掉**；
    A 接著把剩下的檔換完 —— 結果是「第一個檔是舊的、其餘是新的」的混版本，
    而且日誌已經沒了，沒有任何人能修它。這正是這把鎖當初要防的事（見
    `_updater_write_lock`：五支程式同時啟動、.bak 互踩、混 commit）。
    拿不到鎖時【不做事】：那代表有人正在寫，不是「上次崩潰了」。

    ★誠實邊界（承上）★ 拿不到鎖就跳過，代表這一輪不復原；下次啟動再試。
    這是刻意的 —— 把「別人正在寫」誤判成「上次崩潰」比晚一輪復原危險得多。

    不拋例外：復原失敗只記 error，絕不讓它擋住程式啟動。
    """
    restored: list[str] = []
    try:
        app_dir = app_dir or get_app_dir()
        path = _journal_path(app_dir)
        # 先便宜地看一眼；沒有日誌就完全不必碰鎖（絕大多數啟動都走這條）
        if not os.path.exists(path):
            return restored
        with _updater_write_lock() as _acquired:
            if not _acquired:
                logging.info("[更新] 有另一支程式正在寫入更新 → 本輪不做復原"
                             "（正在寫 ≠ 上次崩潰），下次啟動再檢查")
                return restored
            return _recover_locked(app_dir, path, restored)
    except Exception:
        logging.error("[更新] 未完成更新的復原程序本身失敗（不影響啟動）",
                      exc_info=True)
    return restored


def _strict_parse_journal(payload):
    """→ (entries, 原因)。★與 bootstrap_recovery 共用同一支解析★

    借不到那支（模組不見／自己被更新換到一半）時【不要退回寬鬆解析】——
    那正是 drift 會重新長回來的地方。回 (None, 原因) 讓呼叫端什麼都不做。
    """
    try:
        import bootstrap_recovery
    except Exception:      # noqa: BLE001
        return None, "取不到 bootstrap_recovery 的嚴格解析（不改用寬鬆版）"
    try:
        return bootstrap_recovery.parse_journal(payload)
    except Exception as e:      # noqa: BLE001
        return None, f"解析交易日誌時例外：{type(e).__name__}"


def _mark_restored_in_journal(app_dir: str, files: list,
                              restored_targets: list) -> None:
    """把這一輪真的還原成功的項目標成 restored 並落地。

    ★與 bootstrap 共用同一套狀態語意★（見 `bootstrap_recovery` 的 STATE_*）。
    寫不下去只記一筆錯誤 —— 那時 `.bak` 仍在（bootstrap 的還原改成保留備份），
    下一輪照著同一份備份再做一次即可，結果相同。
    """
    if not restored_targets:
        return
    done = {os.path.normcase(t) for t in restored_targets}
    changed = False
    for item in files:
        if os.path.normcase(str(item.get("target") or "")) in done:
            if item.get("state") != "restored":
                item["state"] = "restored"
                changed = True
    if not changed:
        return
    try:
        import bootstrap_recovery
        if not bootstrap_recovery._rewrite_journal_states(app_dir, files, []):
            raise OSError("寫入交易日誌狀態失敗")
    except Exception as e:      # noqa: BLE001
        logging.error("[更新] 標記已還原項目失敗（%s）→ 保留備份，下一輪會再做"
                      "一次（結果相同）", e)


def _recover_locked(app_dir: str, path: str, restored: "list[str]") -> "list[str]":
    """`recover_incomplete_update` 的內層：呼叫時必須【已持有】更新寫入鎖。"""
    try:
        # 拿到鎖之後【再確認一次】：等鎖的期間，剛才那個寫入者可能已經正常結束
        # 並清掉日誌了 —— 那就沒有崩潰可復原。
        if not os.path.exists(path):
            return restored
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        # ★[2026-08-02 外審第 3 輪 P1-02] 不要維護兩套復原引擎★
        #   這裡原本是 `payload.get("files") or []`，而 bootstrap_recovery 那邊
        #   嚴格驗 schema 與欄位。drift 的後果很具體：bootstrap 看到結構壞掉的
        #   日誌會回 UNKNOWN 並【保留證據】；使用者選「仍要啟動」之後，main.py
        #   載入 updater，它把同一份 JSON 當成空陣列、判定「沒有東西要還原」→
        #   ★把那份唯一的證據刪掉★。改成共用同一支嚴格解析。
        files, why = _strict_parse_journal(payload)
        if files is None:
            # 看不懂就【什麼都不做】：不回滾、不清日誌，把證據留給人看。
            logging.error("[更新] ★交易日誌看不懂，本輪不做任何復原★（%s）"
                          "—— 保留原檔供追查", why)
            return restored
        logging.warning(
            "[更新] ★偵測到未完成的更新★（交易日誌 %s，%d 個檔，開始於 %s）→ 回滾到更新前的版本",
            os.path.basename(path), len(files), payload.get("started", "?"))
        # ★[2026-08-02 外審 P1-01] 日誌裡的絕對路徑不可以照單全收★
        #   正常情況下這個檔是自己寫的，但它就是磁碟上一個普通的 JSON。被改過
        #   （或被別的東西寫壞）而程式又以管理員身分執行時，「照著日誌把 A 換成 B」
        #   等於任意檔案覆寫／刪除。所以每一筆都要確認 target 在程式目錄底下。
        written = []
        rejected: list[str] = []
        skipped_done = 0
        for item in files:
            target = str(item.get("target") or "")
            if not target:
                continue
            if item.get("state") == "restored":
                # ★[2026-08-02 外審 P2-05] 上一輪已經還原完的不可以再判★
                #   它的 .bak 早被用掉了，再判就是假的 terminal。
                skipped_done += 1
                continue
            if not _is_inside_app_dir(app_dir, target):
                rejected.append(target)
                continue
            written.append(_WrittenFile(target,
                                        bool(item.get("existed_before")),
                                        str(item.get("staged") or "")))
        if rejected:
            logging.error("[更新] ★交易日誌裡有 %d 筆路徑不在程式目錄內，已拒絕還原★ "
                          "（日誌可能被竄改或寫壞）：%s",
                          len(rejected), "; ".join(rejected[:3]))
        if skipped_done:
            # 這代表上一輪其實還原成功了，只是清除／改寫日誌那一步沒做完。
            logging.info("[更新] 交易日誌裡有 %d 筆上一輪已還原完成 → 跳過"
                         "（不再判它們，避免誤判成「備份不見了」）", skipped_done)
        # reversed()：與 in-process 的回滾順序一致
        outcome = _rollback_written_files(written, from_journal=True)
        errors, unresolved, rolled_back = (
            outcome.errors, outcome.unresolved, outcome.restored)
        # ★[2026-08-03 外審 P2] 這一條路徑原本只【讀】state，不【寫】★
        #   watchdog 會直接呼叫 recover_incomplete_update()。它把某一筆還原成功、
        #   用掉了 .bak，接著 `_clear_commit_journal()` 因為日誌被鎖住而失敗 ——
        #   那一筆在磁碟上仍是 pending 而且沒有備份，下一輪就被判成 terminal，
        #   憑空生出一個 .failed.json。跳過既有的 restored 只解決了一半。
        #   ★在清日誌之前就把狀態寫下去★，清不掉也不會誤判。
        _mark_restored_in_journal(app_dir, files, rolled_back)
        # ★措辭鐵律★ 只報【真的動過】的檔：崩潰點之後那些根本沒被替換過的
        #   不算「已回滾」，說成回滾了是在誇大這支程式做過的事。
        restored.extend(rolled_back)
        if errors:
            logging.error("[更新] 回滾未完成的更新時有 %d 個錯誤：%s",
                          len(errors), "; ".join(errors[:3]))
        else:
            logging.warning("[更新] 已回滾 %d 個檔案到更新前的版本"
                            "（日誌共 %d 個，其餘在崩潰時還沒被替換過；"
                            "下一輪更新會重新套用）", len(rolled_back), len(written))
        # ★[2026-08-01 外審 P1] 只有【全部】還原成功才可以清掉日誌★
        #   原本無條件清。可是回滾也會失敗（防毒暫時鎖住某個檔最常見）——
        #   一清掉，磁碟上還是半新半舊，卻再也沒有任何標記讓下次啟動重試，
        #   等於把唯一的修復機會丟掉。改成把日誌【改寫成只剩沒還原成功的那幾個檔】：
        #   下次啟動會只重試它們，不會重覆回滾已經還原好的（那正是當初無條件清掉
        #   要避免的「每次啟動噴一批 error」）。
        #   ★[2026-08-02 外審 P1-01] 「救不回來」不等於「結案」★
        #   `terminal`（備份不見了／路徑被拒）的檔重試沒有意義，所以它不在
        #   `unresolved` 裡 —— 但原本的 else 分支會因此把日誌【刪掉】，磁碟上
        #   還是新舊混合，卻連「發生過什麼」的唯一證據也沒了。
        #
        #   ★[2026-08-02 外審第 2 輪 P2] 這兩件事必須能【同時】成立★
        #   我上一版寫成 if/elif：一批裡若既有救不回來的、又有防毒鎖住可重試的，
        #   `unresolved` 分支先跑並把日誌縮成只剩可重試的那幾個 —— terminal 的
        #   檔就此蒸發；等重試成功日誌被清掉，磁碟仍是混版而所有警示都沒了。
        #   改成先另存 terminal 標記（獨立的檔，不動日誌），再決定日誌怎麼處置。
        marker_ok = True
        if outcome.terminal or rejected:
            marker_ok = _archive_failed_journal(app_dir,
                                                outcome.terminal + rejected)
        if not marker_ok:
            # ★[2026-08-02 外審第 3 輪 P1] 標記寫不出來就【完全不要動日誌】★
            #   標記與日誌雙雙不在 = 下次啟動看到一片乾淨，混版被無聲放行。
            logging.error("[更新] 「無法修復」標記寫不出來 → 保留完整交易日誌")
        elif unresolved:
            _rewrite_journal_for_retry(app_dir, unresolved)
        else:
            # ★備份要留到日誌真的清掉之後★ 清不掉就留著：下一輪照同一份再做
            #   一次（冪等），而不是看到「pending 但沒有備份」而誤判成救不回來。
            if _clear_commit_journal(app_dir):
                _drop_backups(rolled_back)
    except Exception:
        logging.error("[更新] 未完成更新的復原程序本身失敗（不影響啟動）",
                      exc_info=True)
    return restored


FAILED_JOURNAL_SUFFIX = ".failed.json"


def _is_inside_app_dir(app_dir: str, path: str) -> bool:
    """`path` 是否落在 `app_dir` 底下（符號連結解析後）。判不出來一律回 False。

    ★查不到 ≠ 沒問題★ realpath/commonpath 會在跨磁碟機、路徑不存在等情況丟例外；
    那時我們無法證明它是安全的，就不要動它。
    """
    try:
        root = os.path.realpath(app_dir)
        return os.path.commonpath([root, os.path.realpath(path)]) == root
    except Exception:      # noqa: BLE001
        return False


def _archive_failed_journal(app_dir: str, terminal: "list[str]") -> bool:
    """把「救不回來」記成一個【獨立的標記檔】，★不動交易日誌本身★。

    ★[2026-08-02 外審第 2 輪 P2] 原本是把 journal 改名成 .failed.json★
    那讓「救不回來」與「可重試」互斥：同一批裡若兩者都有，一定會弄丟一邊。
    改成另存標記後，日誌可以繼續縮寫給重試用，兩種狀態同時留得下來。

    格式與 `bootstrap_recovery._record_terminal` 相同（那一支不能 import 這裡），
    由 `tests/test_review_batch_f_*.py` 的往返測試釘住。
    """
    path = _journal_path(app_dir) + FAILED_JOURNAL_SUFFIX
    known: list = []
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                known = (json.load(f) or {}).get("files") or []
    except Exception:      # noqa: BLE001  讀不到舊的就當沒有，不要因此不記
        known = []
    merged = list(known) + [t for t in terminal if t not in known]
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"schema": JOURNAL_SCHEMA, "files": merged}, f,
                      ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        logging.error("[更新] ★有 %d 個檔案永久無法還原★ 磁碟上可能是新舊混合的版本；"
                      "已記錄於 %s 供事後追查，臨床主程式啟動時會提醒：%s",
                      len(terminal), os.path.basename(path),
                      "; ".join(terminal[:3]))
        return True
    except Exception as e:      # noqa: BLE001
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        logging.error("[更新] 有 %d 個檔案永久無法還原，且標記檔寫入失敗（%s）"
                      "→ 呼叫端必須保留交易日誌", len(terminal), e)
        return False


def clear_failed_journal_marker(app_dir: str = "") -> bool:
    """一整輪更新【完全成功】之後，才可以把「救不回來」的標記清掉。

    ★[2026-08-02 外審第 2 輪 P1] 為什麼需要這一支★
    `.failed.json` 是給 `bootstrap_recovery` 看的長效標記：只要它還在，臨床主程式
    每次啟動都會問人。那個狀態必須有出口，否則使用者只能每天早上按「是」。

    出口就是「整棵樹被換成一致的新版」—— 一輪成功的更新會把 manifest 上每個檔
    對到期望的 SHA（相同的跳過、不同的重寫），跑完沒有錯誤就代表混版已經被蓋掉。
    ★只在那個時候清★ 不可以因為「這次沒偵測到問題」就清。
    """
    try:
        path = _journal_path(app_dir or get_app_dir()) + FAILED_JOURNAL_SUFFIX
        if not os.path.exists(path):
            return False
        os.remove(path)
        logging.warning("[更新] 已完成一輪完整更新 → 解除先前的「無法修復」標記（%s）",
                        os.path.basename(path))
        return True
    except Exception:      # noqa: BLE001
        logging.error("[更新] 解除「無法修復」標記失敗", exc_info=True)
        return False


def _rewrite_journal_for_retry(app_dir: str,
                               unresolved: "list[_WrittenFile]") -> None:
    """把日誌縮成「還沒還原成功」的那幾個檔，讓下次啟動只重試它們。

    寫不出來時【保留原本的日誌】—— 全部重試一次雖然會對已還原的檔噴 error，
    但總比完全沒有標記好（沒有標記就沒有人會再去修那個混版本）。
    ★這個保證是靠 `_write_commit_journal` 的「寫 tmp → 原子換名、失敗只清 tmp」★
    在它改成原子寫之前，這句話是假的：那時它會先截斷既有日誌、失敗又把它刪掉。
    """
    # staged 一律給 ""：能走到這裡的都是【確定換過】的檔，不需要那個判別依據。
    if _write_commit_journal(
            app_dir,
            [(w.target_path, w.existed_before, "") for w in unresolved]):
        logging.error("[更新] ★仍有 %d 個檔沒有還原成功★ 已把交易日誌縮寫成只剩它們，"
                      "下次啟動會再試一次", len(unresolved))
    else:
        logging.error("[更新] 仍有 %d 個檔沒有還原成功，且改寫交易日誌也失敗 → "
                      "保留原日誌（下次啟動會整批重試）", len(unresolved))


def _rollback_written_files(
        written_files: list[_WrittenFile],
        *, from_journal: bool = False,
) -> RollbackOutcome:
    """把已經換掉的檔還原回去。→ `RollbackOutcome`。

    ★[2026-08-01 外審 P1] 第二個回傳值是必要的★ 呼叫端要據此決定「交易日誌能不能
    清掉」：只要還有檔沒還原成功，日誌就得留著，否則那個混版本再也沒人會去修。

    ★`from_journal`：「沒有 .bak」有兩個成因，處置完全相反★
    （外審的建議沒有分這兩者；不分的話，其中一種會變成永遠停不下來的重試。）
      * 行程內回滾（False）：`written_files` 只放【確定已經換掉】的檔，備份一定
        做過了。這時 .bak 不見 = 真的出事 → 算錯誤。
      * 日誌復原（True）：日誌是在動第一個檔【之前】就落地的，列的是整批 staged
        檔案，所以裡面本來就有「崩潰時還沒輪到」的檔。分辨方式是那個檔的
        `.upd.tmp` 還在不在 —— `os.replace(tmp, target)` 會把 tmp 消耗掉：
          tmp 還在  ⇒ 還沒換過 ⇒ 它就是更新前的版本，沒有東西要還原（不是錯誤）
          tmp 不在  ⇒ 換過了，而備份卻消失了（防毒清掉／被手動刪）
                     ⇒ 這個檔【救不回來】，要大聲報錯

    ★可重試 vs 不可重試★ 只有【可能會好】的失敗才放進 `unresolved` 讓下次啟動
    再試（典型是防毒暫時鎖住檔案）。「備份根本不存在」重試一萬次也不會出現，
    留在日誌裡只會讓每次啟動噴同一批 error —— 那正是當初無條件清日誌想避免的事。
    """
    errors: list[str] = []
    unresolved: list[_WrittenFile] = []
    restored: list[str] = []
    terminal: list[str] = []
    for written in reversed(written_files):
        backup_path = written.target_path + ".bak"
        name = os.path.basename(written.target_path)
        # ★[2026-08-01 外審第 2 輪 P1] 「還沒被換過」的證據要【先】看，而且贏過 .bak★
        #   原本是先看 .bak 在不在，.bak 存在就直接拿它還原。可是 .bak 有可能是
        #   【上一批】留下來的陳舊備份（上次 commit 成功後清理 .bak 失敗就會這樣，
        #   而那個清理是靜默吞掉錯誤的）。這時如果本批在「複製到 .bak.tmp」的中途
        #   死掉，正式檔根本還沒被動過，卻會被那個【更舊的】陳舊備份蓋掉 —— 復原
        #   把使用者降版了。暫存檔還在就是「還沒 replace」的鐵證（os.replace 會把
        #   它吃掉），這個證據比 .bak 存不存在可靠，所以先判它。
        untouched = bool(written.staged_path) and os.path.exists(
            written.staged_path)
        if not written.existed_before and not os.path.exists(
                written.target_path):
            untouched = True          # 新增檔還沒被建出來 → 也是「還沒輪到」
        if from_journal and untouched:
            logging.info("[更新] %s 在崩潰時還沒被替換過（暫存檔仍在）→ "
                         "它就是更新前的版本，不需還原", name)
            continue
        if not os.path.exists(backup_path):
            if written.existed_before:
                msg = (f"找不到備份: {backup_path}"
                       "（這個檔已經被換成新版，而備份不見了 → 救不回來）")
                logging.error("更新回滾失敗 [%s]: %s", written.target_path, msg)
                errors.append(f"[rollback] {written.target_path}: {msg}")
                # ★刻意【不】放進 unresolved★ 備份不會自己長回來，重試沒有意義。
                #   但它要進 `terminal`：呼叫端得知道「有檔案永久回不去了」，
                #   才不會把交易日誌當成乾淨結案而刪掉（2026-08-02 外審 P1-01）。
                terminal.append(written.target_path)
                continue
        try:
            durable = True
            if written.existed_before:
                durable = _restore_keeping_backup(backup_path,
                                                  written.target_path)
            elif os.path.exists(written.target_path):
                _remove_file_with_retry(written.target_path)
            if durable:
                restored.append(written.target_path)
            else:
                # ★措辭鐵律★ 內容【已經】換回舊版了，說「回滾失敗」是誣賴它。
                #   缺的是耐久性保證，所以這一筆不算收工：不進 restored（於是
                #   不會被標成 restored、備份也不會被清），改進 unresolved 讓
                #   下一輪原封不動再做一次（冪等）。
                msg = ("內容已換回舊版，但 fsync 失敗 → 不保證已寫到碟上；"
                       "保留日誌與備份，下一輪重做一次")
                logging.error("更新回滾尚未收工 [%s]: %s",
                              written.target_path, msg)
                errors.append(f"[rollback] {written.target_path}: {msg}")
                unresolved.append(written)
        except Exception as e:
            logging.error("更新回滾失敗 [%s]: %s", written.target_path, e)
            errors.append(f"[rollback] {written.target_path}: {e}")
            unresolved.append(written)   # 這類（鎖住/權限）下次可能就成功了
    return RollbackOutcome(errors=errors, unresolved=unresolved,
                           restored=restored, terminal=terminal)


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
    # [IE-05 2026-07-12] manifest 為合法 JSON 但非 dict(如 list)時,下游 .get() 會在 try 外
    # 拋 AttributeError 逃逸;此處提早驗證,轉成受控錯誤訊息。
    if not isinstance(manifest, dict):
        raise ValueError("manifest.json 頂層非物件(dict)")
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
    # [IE-06 2026-07-12] manifest 條目缺 sha256 → 不可不校驗就寫入(資料落地 fail-closed)。與
    # IE-01 同模式:raise 讓整批不更新(現況 sync_manifest 一律產 sha,缺 sha=清單異常)。
    if not expected_sha:
        raise ValueError(f"[{key}] manifest 缺 sha256,拒絕不校驗寫入(整批暫不更新)")

    # ★比對的對象是【現在真的在跑的那一份】★(外審 L2 第 1 輪 P1-01):
    #   L2 之後 `<app>/src` 是回退點、不再更新 —— 拿它比會永遠判定落後。
    local_path = _local_read_path(app_dir, local_filename)

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


# 部署目標平台。抽成模組層常數(而不是每次現算 sys.platform)有兩個理由:
# 讓「Windows 上鎖機制壞掉」與「這不是 Windows」變成兩件可分辨的事,也讓測試
# 能夠明確驗證兩條分支 —— 見 _updater_write_lock 的 fail-closed 說明。
IS_WINDOWS = sys.platform.startswith("win")


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

    yield True=可寫(拿到鎖,或【確定不是 Windows】);False=逾時沒拿到、或 Windows 上鎖機制
    故障 → caller 本輪放棄。yield 在 try/finally(非 try/except)內,不吞 body 例外。

    ★[2026-07-30 第二輪外審 P1-06] Windows 上鎖機制故障必須 fail-closed★
    原本四條失敗路徑(取不到 app dir、import msvcrt 失敗、開鎖檔失敗、初始化鎖檔失敗)
    全都 `yield True` 照樣寫。但「拿不到鎖」與「鎖壞了」在後果上是同一件事 —— 而鎖壞掉
    最可能發生的時刻正是磁碟權限/防毒出問題的時候,也正是【最需要這把鎖】的時候。
    在那個瞬間退回無鎖,等於這把鎖只在不需要它的時候有效。
    多支程式同時寫同一批 src/cmuh_common/*.py 與同名 .bak 的後果是「回滾還原到錯版本、
    混 commit」—— 那比「本輪不更新、下次再說」嚴重得多,取捨很清楚。
    只有 `not IS_WINDOWS` 才維持放行:部署目標是 Windows,CI(Linux)與開發機不可被鎖死。
    """
    if not IS_WINDOWS:
        yield True                     # 確定不是 Windows → 不擋(刻意;部署目標是 Windows)
        return
    try:
        lock_path = os.path.join(get_app_dir(), ".updater_write.lock")
    except Exception:
        logging.warning("[更新] 取不到 app 目錄,無法建立更新鎖 → 本輪不寫入",
                        exc_info=True)
        yield False
        return
    try:
        import msvcrt
    except Exception:
        # 在 Windows 上 import msvcrt 失敗不是「這不是 Windows」,而是執行環境壞了。
        logging.warning("[更新] Windows 上無法 import msvcrt(執行環境異常)"
                        " → 本輪不寫入", exc_info=True)
        yield False
        return
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    except OSError:
        logging.warning("[更新] 開更新鎖檔失敗(可能被防毒/權限擋住)→ 本輪不寫入",
                        exc_info=True)
        yield False
        return
    try:
        # [codex P1] msvcrt.locking 從「目前檔位」鎖 nbytes。新建的 .updater_write.lock 是空檔;
        # 為跨 Windows/CRT 版本穩健(不倚賴「可鎖超過 EOF」的行為),先確保檔內至少 1 byte、再把
        # 檔位歸零,固定鎖 [0,1)。本機實測空檔可鎖,此步僅作保險與可攜性。
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")
        os.lseek(fd, 0, os.SEEK_SET)
    except OSError:
        logging.warning("[更新] 初始化鎖檔失敗(磁碟滿/權限?)→ 本輪不寫入",
                        exc_info=True)
        try:
            os.close(fd)
        except OSError:
            pass
        yield False
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

    # ★[P1-08 2026-08-01] 先復原上一批沒走完的更新★
    #   在【檢查/下載之前】做：否則這一輪會拿半套的磁碟狀態去比對版本，
    #   有可能因為 version.py 已經是新的而判定「不需要更新」→ 半套狀態就這樣留著。
    #   ★[2026-08-02 外審 P1-01] 復原沒收乾淨就【不要】再套用新的一批★
    #   原本無論復原結果如何都繼續往下跑。可是「上一批只回滾了一半」＋「這一批
    #   再寫進去」＝ 新版蓋在半舊的樹上，而 .bak 鏈已經斷了：出事就再也回不去。
    #   判斷依據刻意用【磁碟上還有沒有交易日誌】而不是回傳值 —— 那是真相本身，
    #   而且 `recover_incomplete_update` 內部已經吞掉所有例外、回傳值反映不了失敗。
    #   註：救不回來（terminal）的情況日誌會被改名成 .failed.json，這裡就看不到它，
    #   於是更新照常進行 —— 那是對的：整批換成新版正是修好混版最有效的辦法。
    try:
        recover_incomplete_update()
    except Exception:
        logging.error("[更新] 復原未完成更新失敗", exc_info=True)
    try:
        if os.path.exists(_journal_path(get_app_dir())):
            msg = ("[journal] 上一批更新尚未完全回滾（交易日誌還在）→ "
                   "本輪不套用新的更新，等它收乾淨再說")
            logging.error("[更新] ★%s★", msg)
            result.errors.append(msg)
            return result
    except Exception:
        logging.error("[更新] 檢查交易日誌是否殘留時失敗", exc_info=True)

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
        # ★這裡是「整棵樹一致」的證明★ 走到這代表 manifest 上【每一個】檔的
        #   磁碟 SHA 都對得上，而且沒有任何下載失敗。先前若留下「救不回來」的
        #   標記，到此可以解除 —— 混版已經不存在了。
        clear_failed_journal_marker(app_dir)
        # ★「磁碟已是最新」不等於「我正在跑的是最新」★(外審 L2 第 2 輪 P2)
        #   L2 之後 SHA 比對的對象是【指標指著的那一棵】:另一支程式可能在
        #   我開始檢查之前就裝好新版並切了指標,於是我這一輪零筆待寫、從這裡
        #   直接回 has_update=False —— 而下面那個「別人已更新 → 我要重啟」的
        #   判斷在持鎖分支裡,零筆待寫【永遠走不到】。結果是這支程式一直跑
        #   舊碼(六支程式各自更新、卻沒有一支真的換版),正是 L2 要解決的事。
        _inst_ver, _inst_status = _read_ondisk_app_version_ex(app_dir)
        # ★版本號相同也可能不是同一棵樹★(外審 L2 第 3 輪 P1):同版 SHA
        #   修復會把指標從 `versions/V` 切到 `V.r2`,兩邊 version.py 都是 V。
        if ((_inst_status == "ok" and _inst_ver
             and parse_version(_inst_ver) > parse_version(CURRENT_VERSION))
                or _installed_tree_differs_from_running(app_dir)):
            logging.info("[更新] 磁碟上是 v%s、本行程跑 v%s(來源樹 %s)→ "
                         "需重啟才會載到指標指著的那一棵", _inst_ver or "?",
                         CURRENT_VERSION, _running_src_dir())
            result.has_update = True
            result.updated_files.append(
                ("(另一程式已更新，本程式需重啟)", _inst_ver or CURRENT_VERSION))
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
        _disk_ver, _disk_status = _read_ondisk_app_version_ex(app_dir)
        if _disk_status == "error":
            # [2026-07-26 審查] 磁碟版本【暫時】讀不到(原檔完好、只是被鎖住)。
            # 此時無法確認「本批是不是比磁碟舊」→ 不可寫,否則會踩到降版/版本錯亂。
            # 放棄本批的成本只是晚一輪更新;寫下去的成本是把診間程式降版、或寫成半新半舊。
            logging.warning(
                "[更新] 取得鎖後讀不到磁碟版本(檔案仍在)→ 無法確認是否會降版,"
                "整批放棄,下一輪再更新")
            # [外審] 一定要記進 result.errors:呼叫端(main.py 的更新檢查)是靠這個欄位
            # 分辨「失敗」與「已是最新」的 —— 不記的話 UI 會顯示「所有程式皆為最新版本」,
            # 但其實是下載好的更新被丟掉了。與其他 fail-closed 路徑(取不到寫入鎖等)一致。
            result.errors.append(
                "讀不到磁碟上的版本檔(可能被防毒/備份鎖住)→ 為避免降版,本次更新未套用")
            return result
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
        # ★同一批內容別人已經裝好了 → 不要再造一棵一模一樣的變體樹★
        #   (外審 L2 第 2 輪 P1)我下載時磁碟還缺這些檔,但等我拿到鎖時
        #   另一支程式可能已經裝好【同版】並切了指標。此時再走一次版本化
        #   安裝,只會因為「完整目錄不可變」而開出 `V.r2`、把指標切到另一棵
        #   內容相同的樹 —— 純粹的邊際成本與擾動。這一輪什麼都不用寫,
        #   但如果磁碟上那一版比我正在跑的新,我要重啟才會載到它。
        if _installed_batch_is_current(app_dir, prepared_writes):
            logging.info("[更新] 取得鎖後發現本批內容【已經在磁碟上生效】"
                         "(另一程式剛裝好)→ 不重複安裝")
            # 同上:版本號一樣但不是同一棵樹(同版修復)也要重啟。
            if ((_disk_ver
                 and parse_version(_disk_ver) > parse_version(CURRENT_VERSION))
                    or _installed_tree_differs_from_running(app_dir)):
                result.has_update = True
                result.updated_files.append(
                    ("(另一程式已更新，本程式需重啟)",
                     _disk_ver or CURRENT_VERSION))
            return result
        return _commit_pending_writes(prepared_writes, result)


def _installed_tree_differs_from_running(app_dir: str) -> bool:
    """指標指著的那一棵,和我★現在真的在跑的★那一棵,是不是不同棵。

    ★版本號相同不代表是同一棵★(外審 L2 第 3 輪 P1):同版 SHA 修復會把
    指標從 `versions/V` 切到 `versions/V.r2` —— 兩邊的 `version.py` 都寫著
    V,只比版本號的話沒有人會要求重啟,那支程式就繼續跑在【損壞的】舊樹上
    (它正是因為損壞才被修的)。第一次切換也是同一回事:`<app>/src` 與
    `versions/V/src` 的內容可能一模一樣,但要跑到新樹上仍得重啟。

    ★重啟會解決它,不會變成迴圈★:自我重啟一律走固定的 launcher(批次X),
    launcher 重新解析指標 —— 重啟之後兩者必然一致。查不出來就回 False
    (不吵),那一輪仍有版本號比較可用。
    """
    try:
        inst = os.path.normcase(os.path.abspath(_installed_src_dir(app_dir)))
        run = os.path.normcase(os.path.abspath(_running_src_dir()))
    except Exception:                                   # noqa: BLE001
        logging.debug("[更新] 比對執行中/指標樹失敗", exc_info=True)
        return False
    return inst != run


def _installed_batch_is_current(app_dir: str, prepared_writes: list) -> bool:
    """這一批的每一個檔,磁碟上★現在會被載入的那一份★是否已經是這個內容。

    只有在「全部都已經相同」時才回 True —— 讀不到、比不出來、或有任何一個
    不同,都回 False(照舊安裝)。失效方向刻意選「多裝一次」:漏裝會讓這台
    機器停在舊版,而多裝一次只是多一棵版本目錄。
    """
    if not prepared_writes:
        return False
    try:
        for _key, local_filename, _nv, content, _t in prepared_writes:
            path = _local_read_path(app_dir, local_filename)
            if not os.path.exists(path):
                return False
            if _sha256_local_file(path) != _sha256_text(content):
                return False
    except Exception:                                   # noqa: BLE001
        logging.debug("[更新] 比對磁碟現況失敗 → 照常安裝", exc_info=True)
        return False
    return True


def _read_ondisk_app_version_ex(app_dir: str) -> tuple:
    """讀磁碟上 src/cmuh_common/version.py 的 CURRENT_VERSION,回 (version, status)。

    status 與本專案既有的 safe_load_json_ex 契約一致:
      "ok"          讀到且解析成功
      "missing"     檔案不存在(不完整的安裝)
      "unparsable"  檔案在但找不到 CURRENT_VERSION(內容損壞/寫到一半)
      "error"       OSError/PermissionError 等【暫時性】失敗 —— **原檔通常仍完好**

    [2026-07-26 審查 ★降版 / 版本錯亂★] 舊版三種失敗全部回 ''(與「沒有版本」無法區分),
    而呼叫端的降版守衛寫成 `if (_disk_ver and ...)` —— 於是防毒/備份軟體鎖住 version.py
    的那一瞬間,守衛【整個被跳過】:本批若是較舊的 manifest revision(CDN 舊清單),
    就會直接覆蓋磁碟上別的程式剛寫好的新版 = 降版 + 部分檔新部分檔舊,
    正是 _commit_pending_writes 的說明裡要防的 version skew。
    區分之後:missing/unparsable 代表磁碟上【沒有可信版本可比】,寫下去是修復(照舊放行);
    "error" 代表原檔完好只是讀不到 → 呼叫端必須放棄本批,下一輪再更新。
    """
    # ★讀【正在跑的那一棵樹】的 version.py★(外審 L2 第 1 輪 P1-03):
    #   降版守衛就是靠這個值判斷「本批會不會把磁碟降版」。L2 之後
    #   `<app>/src` 停在回退點,拿它比會允許把指標切回更舊的版本。
    vp = _local_read_path(app_dir, "src/cmuh_common/version.py")
    try:
        with open(vp, "r", encoding="utf-8") as f:
            content = f.read()
    except FileNotFoundError:
        return "", "missing"
    except UnicodeDecodeError:
        # [外審] 內容不是合法 UTF-8 = 檔案【損壞】(寫到一半/磁碟壞軌),不是「暫時讀不到」。
        # 歸到 error 會讓這台機器【永遠】不再更新(每輪都放棄)= brick;
        # 歸到 unparsable 才會走修復路徑,把完整的一批寫回去。
        logging.warning("[更新] 磁碟版本檔內容非合法 UTF-8(視為損壞,將由更新修復):%s", vp)
        return "", "unparsable"
    except OSError:
        logging.warning("[更新] 讀磁碟版本失敗(檔案仍在,可能被防毒/備份鎖住):%s",
                        vp, exc_info=True)
        return "", "error"
    except Exception:
        logging.warning("[更新] 讀磁碟版本發生非預期例外:%s", vp, exc_info=True)
        return "", "error"
    m = re.search(r'CURRENT_VERSION\s*=\s*["\']([\d.]+)["\']', content)
    return (m.group(1), "ok") if m else ("", "unparsable")


def _read_ondisk_app_version(app_dir: str) -> str:
    """相容用薄包裝(只取版本字串)。需要區分「讀不到」與「沒有版本」請用 _ex 版。"""
    return _read_ondisk_app_version_ex(app_dir)[0]


# ── 批次L・L2:版本化安裝目錄 + 原子切換 ──────────────────────────────────
#   設計見 `docs/批次L_版本化目錄與原子切換_設計_2026-08-03.md`。
#   L1(已上線)做的是【讀取】:`current.txt` 不存在就走 `<app>/src`。
#   L2 在這裡:`src/` 底下的檔改成★裝進一個全新的 versions/<V>/★,
#   驗完 SHA 才寫 `.complete`,最後把指標原子切過去。
#   ★六支 .pyw 與 version_pointer.py 不走這條路★(設計 §4):它們是
#   Task Scheduler 指的固定路徑,是「切版本救不回來」的唯一單點,
#   仍然就地更新(既有的交易日誌 + .bak 路徑)。
KEEP_VERSIONS = 3


def _load_version_pointer(app_dir: str):
    """把 app 根目錄的 `version_pointer.py` 載進來(單一事實來源)。→ 模組或 None。

    ★不重寫一份安全字元/目錄名的判準★:那會漂移。載不進來就回 None,
    呼叫端退回既有的就地更新 —— 行為與 L1 之前完全相同。
    """
    import importlib.util  # noqa: PLC0415
    path = os.path.join(app_dir, "version_pointer.py")
    try:
        os.lstat(path)
    except OSError:
        return None
    try:
        spec = importlib.util.spec_from_file_location(
            "_updater_version_pointer", path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # ★載得進來 ≠ 介面完整★(外審 L2 第 1 輪 P2):截斷或版本不相容的
        #   resolver 語法可能合法,少了某個函式/常數 —— 那時 AttributeError
        #   會從 `_commit_pending_writes` 逸出,而不是依約退化成就地更新。
        #   ★名字在 ≠ 叫得動★(外審 L2 第 2 輪 P2):`is_safe_version` 可能
        #   被寫成一個字串、或一呼叫就拋 —— 只驗 `hasattr` 的話那個例外
        #   照樣從 commit 路徑逸出。所以這裡驗到「真的能用」為止:
        #   函式要 callable、常數要非空字串,再★實際問一次判準★(安全的
        #   版本字串要說 True、跑出目錄的要說 False)。這一段就是那個
        #   「fail-to-in-place」的單一邊界 —— 任何例外都在這裡變成 None。
        for name in ("is_safe_version", "resolve_src", "is_complete"):
            if not callable(getattr(mod, name, None)):
                logging.warning("[更新] version_pointer 的 %s 不可呼叫 →"
                                " 這一輪走就地更新", name)
                return None
        for name in ("VERSIONS_DIRNAME", "COMPLETE_MARKER", "POINTER_NAME"):
            val = getattr(mod, name, None)
            if not isinstance(val, str) or not val:
                logging.warning("[更新] version_pointer 的 %s 不是有效字串 →"
                                " 這一輪走就地更新", name)
                return None
        if not (mod.is_safe_version("2026.01.01.1") is True
                and mod.is_safe_version("../跑出去") is False):
            logging.warning("[更新] version_pointer.is_safe_version 判準不對"
                            " → 這一輪走就地更新")
            return None
        # ★`resolve_src` 也要【實際解析一次】並驗回傳契約★(外審 L2 第 3 輪
        #   P2):它是整個機制的核心,而 callable 只說得出「叫得動」。
        #   這一次解析與 stub 開機時做的是同一件事 —— 它在這台機器上會不會
        #   拋、回傳的是不是 `Resolution(src_dir=…)`,現在就問得出來。
        #   壞掉時 stub 那一側會退回 `<app>/src`(它自己的 fallback),所以
        #   更新器也必須退回就地更新,兩側才會落在同一棵樹上。
        probe = mod.resolve_src(app_dir, "updater")
        probe_src = str(getattr(probe, "src_dir", "") or "")
        if not probe_src:
            logging.warning("[更新] version_pointer.resolve_src 的回傳不符契約"
                            "(沒有 src_dir)→ 這一輪走就地更新")
            return None
        return mod
    except Exception:                                   # noqa: BLE001
        logging.warning("[更新] 載入 version_pointer 失敗 → 這一輪走就地更新",
                        exc_info=True)
        return None


def _local_read_path(app_dir: str, local_filename: str) -> str:
    """這個 manifest 目標在磁碟上【現在真正被載入】的那一份在哪裡。

    ★L2 之後「跑的那一棵樹」會移動★(外審 L2 第 1 輪 P1-01/03):
    `<app>/src` 在 L2 期間刻意不再被覆蓋 —— 它是一鍵回退點,內容停在
    L2 上線那天。如果比對 SHA / 讀磁碟版本還看它:
      * 每一輪都判定「落後」→ 反覆重新下載、重新安裝;
      * 反過來,現行版本樹壞掉而舊副本剛好符合 SHA 時,更新器會宣稱
        「已是最新」而不去修復它;
      * 取得寫入鎖後的降版守衛也會拿一個【更舊】的版本去比,於是允許
        把 `current.txt` 從別的 process 剛切上去的新版切回舊版。
    所以:`src/` 底下的目標一律解析到 `_installed_src_dir()`(=指標指著的
    那一棵);根目錄那幾個固定檔(六支 .pyw、version_pointer.py、
    manifest.json)不受指標影響,仍在 `<app>`。
    """
    if _is_src_relative(local_filename):
        rel = str(local_filename).replace("\\", "/").split("/", 1)[1]
        return _resolve_target_path(_installed_src_dir(app_dir), rel)
    return _resolve_target_path(app_dir, local_filename)


def _installed_src_dir(app_dir: str) -> str:
    """這個 app 目錄★下次啟動會載入★的那一棵 src。

    問的是【版本指標】,不是「這個 process 現在跑哪一棵」:切換之後、重啟
    之前,兩者會不一樣,而更新器要維護的是【磁碟上會被載入的那一份】。
    指標不存在/壞掉/版本目錄不完整時 `resolve_src` 本來就回 `<app>/src`
    —— 過渡期與退化路徑因此自動維持舊行為(單一事實來源,不另寫判準)。
    """
    vp = _load_version_pointer(app_dir)
    if vp is None:
        return os.path.join(app_dir, "src")
    try:
        return vp.resolve_src(app_dir).src_dir
    except Exception:                                   # noqa: BLE001
        logging.warning("[更新] 解析版本指標失敗 → 讀取端退回 <app>/src",
                        exc_info=True)
        return os.path.join(app_dir, "src")


def _is_src_relative(local_filename: str) -> bool:
    """manifest 的目標是不是 `src/` 底下的檔(那些才進版本目錄)。"""
    parts = str(local_filename or "").replace("\\", "/").split("/")
    return len(parts) > 1 and parts[0] == "src"


def _running_src_dir() -> str:
    """這一支程式此刻真的在跑的那一棵 `src`(= 本模組的上上層)。

    ★用執行中的位置,不是 `<app>/src`★:上一版可能已經切到
    `versions/<V-1>/src`,新版本要以【現在跑的這一棵】為底,否則會拿一棵
    更舊的樹來疊(混版)。
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _is_reparse_point(path: str) -> bool:
    """這個目錄項目本身是不是 reparse point(junction/symlink)。查不動=是。

    ★junction 不是 symlink★:`os.path.islink` 對它回 False,要看
    `st_file_attributes` 的 FILE_ATTRIBUTE_REPARSE_POINT(與
    `version_pointer._tree_has_reparse_points` 同一套判準)。
    """
    import stat as _stat  # noqa: PLC0415
    flag = getattr(_stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError:
        return True                      # 查不動 → 當成不安全
    if not flag:
        return bool(os.path.islink(path))
    return bool(getattr(st, "st_file_attributes", 0) & flag)


def _safe_versions_root(app_dir: str, vp) -> str:
    """可以安全建立/列舉/刪除的 `versions` 根。→ 路徑;不安全回 ""。

    ★這裡會 rmtree,而主程式可能是提權執行的★(外審 L2 第 1 輪 P1-05):
    使用者可寫的 `<app>` 底下若被放成指向別處的 junction,安裝會寫到那個
    外部位置、而清舊版會遞迴刪除那裡的目錄。所以動它之前要確認:
    根目錄本身不是 reparse point,而且 realpath 仍落在 realpath(app_dir)
    之內。查不動一律當不安全(fail-closed) —— 這條路徑的代價是刪錯東西。
    """
    root = os.path.join(app_dir, vp.VERSIONS_DIRNAME)
    try:
        if os.path.exists(root):
            if _is_reparse_point(root):
                logging.error("[更新] ★versions 根是 reparse point★ 拒絕使用"
                              "(%s)", root)
                return ""
            real_root = os.path.realpath(root)
            real_app = os.path.realpath(app_dir)
            common = os.path.commonpath([real_app, real_root])
            if os.path.normcase(common) != os.path.normcase(real_app):
                logging.error("[更新] ★versions 根的實體位置跑出程式目錄★"
                              "(%s)", real_root)
                return ""
    except Exception:                                   # noqa: BLE001
        logging.error("[更新] 檢查 versions 根失敗 → 這一輪不做版本化安裝",
                      exc_info=True)
        return ""
    return root


def _strict_installed_src_dir(vp, app_dir: str) -> str:
    """問 resolver「這個 app 目錄下次會載入哪一棵」——★不吞任何例外★。

    (外審 L2 第 3/4 輪 P2)`_installed_src_dir` 刻意寬容:讀取端與 stub 的
    fallback 對齊,resolver 壞了就一起看 `<app>/src`。但★決定要不要版本化
    安裝★的時候寬容是錯的 —— 那會裝進版本目錄、切一個【stub 跟不動】的
    指標,而 stub 那側退回未更新的 `<app>/src`:更新於是靜默地永遠不生效。
    所以這條路徑用這個 strict 版本:拋錯、或回傳不符契約(`src_dir` 空/None
    —— 寬容版會把它變成字串 `"None"` 這種假路徑),一律讓例外往外傳,由
    commit 的單一 fail-to-in-place 邊界接住,整批明確走就地更新。
    """
    res = vp.resolve_src(app_dir)
    src = str(getattr(res, "src_dir", "") or "")
    if not src:
        raise ValueError("version_pointer.resolve_src 回傳不符契約"
                         "(沒有 src_dir)")
    return src


def _pick_version_dir(app_dir: str, version: str, vp,
                      installed_src: str = "") -> str:
    """這一次要裝進哪一個版本目錄名。→ 目錄名;找不到安全的回 ""。

    ★絕不重用「正在用」或「正在跑」的那一個★(外審 L2 第 1 輪 P1-02):
    同一個版本號也會需要重裝(SHA 修復),而安裝的第一步是把目標目錄整個
    刪掉重建 —— 若那正是現行版本樹,等於把自己的來源刪掉:安裝失敗,而
    `current.txt` 指著一個不存在的版本,現行行程之後的 lazy import 也會
    當場失敗。所以同名衝突時換一個不可變的識別碼(`V.r2`、`V.r3`…)。
    """
    root = os.path.join(app_dir, vp.VERSIONS_DIRNAME)
    # ★這裡【不】走會吞例外的 `_installed_src_dir`★(理由見
    #   `_strict_installed_src_dir`):呼叫端(commit 的保護區)通常已經解析
    #   過並把結果傳進來 —— 那樣「resolver 在保護區【裡面】被實際呼叫」在
    #   流程上就看得見,不必讀進這個函式才知道。沒傳的話在這裡 strict
    #   解析一次(同樣不吞例外)。
    inst_src = str(installed_src or "") or _strict_installed_src_dir(
        vp, app_dir)
    # ★誠實紀錄★:自從「完整目錄不可變」那條規則加進來(外審第 2 輪 P1),
    #   `inst_src` 這一半在版本目錄上其實已經被涵蓋了 —— 指標只會指向
    #   【完整】的版本目錄,而那些候選在下面就先被跳過了。留著是縱深防禦
    #   (萬一日後 resolver 的完整性判準改變),不是承重的那一層;
    #   ★承重的是這一句會【實際呼叫 resolver】★:它壞掉時例外要往外傳。
    busy = {os.path.normcase(os.path.abspath(inst_src)),
            os.path.normcase(os.path.abspath(_running_src_dir()))}
    for n in range(1, 20):
        name = version if n == 1 else f"{version}.r{n}"
        if not vp.is_safe_version(name):
            continue
        # ★【完整】的版本目錄一律不可變★(外審 L2 第 2 輪 P1):這台機器上
        #   有六支共用更新器的程式,寫入鎖只序列化「誰在部署」,不代表別人
        #   已經停止使用舊目錄。第三支程式可能正跑在 `versions/V` 上,而它
        #   既不是我的樹、也不是指標現在指的那一棵 —— 只避開這兩者的話,
        #   同版重裝就會把它腳下的來源樹 rmtree 掉。裝好且標了 `.complete`
        #   的目錄只能被讀,要重裝就換一個識別碼;沒有 `.complete` 的是
        #   半成品(指標不可能指過去、沒有人跑得起來),可以安全重建。
        if vp.is_complete(app_dir, name):
            continue
        cand_src = os.path.normcase(
            os.path.abspath(os.path.join(root, name, "src")))
        if cand_src in busy:
            continue
        return name
    logging.error("[更新] 找不到可用的版本目錄名(%s)→ 這一輪不做版本化安裝",
                  version)
    return ""


def _install_versioned_src(app_dir: str, version: str, src_writes: list,
                           vp, result: UpdateResult) -> bool:
    """把整棵 src 裝進 `versions/<V>/src`(全新目錄)。→ 成功嗎。

    ★更新器只下載【有變的】檔,所以版本目錄必須先以現行整棵樹為底再疊★
    —— 只放變更檔的話那個版本目錄根本跑不起來。
    裝完【逐檔回讀驗 SHA256】,全過才寫 `.complete`(最後一步)。
    失敗:整個版本目錄丟掉,`current.txt` 一個位元組都沒動。
    """
    import shutil  # noqa: PLC0415
    root = _safe_versions_root(app_dir, vp)
    if not root:
        result.errors.append("[versions] versions 根不安全(reparse/跑出程式"
                             "目錄)→ 這一批不寫入")
        return False
    vroot = os.path.join(root, version)
    base_src = _running_src_dir()
    try:
        if os.path.isdir(vroot):
            # 半成品(沒有 .complete)或重裝 —— 整個丟掉重來,不要疊。
            shutil.rmtree(vroot)
        os.makedirs(vroot, exist_ok=True)
        shutil.copytree(base_src, os.path.join(vroot, "src"),
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for key, local_filename, _new_ver, content, _target in src_writes:
            dst = _resolve_target_path(vroot, local_filename)
            os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
            with open(dst, "w", encoding="utf-8") as fh:
                fh.write(content)
                fh.flush()
                os.fsync(fh.fileno())
            if _sha256_local_file(dst) != _sha256_text(content):
                raise RuntimeError(f"[{key}] 版本目錄回讀 SHA 不符")
        # ★`.complete` 是最後一步★:沒有它的版本目錄一律是半成品,
        #   `version_pointer.is_complete()` 不會讓指標指過去。
        marker = os.path.join(vroot, vp.COMPLETE_MARKER)
        with open(marker, "w", encoding="utf-8") as fh:
            fh.write(f"{version}\n")
            fh.flush()
            os.fsync(fh.fileno())
        return True
    except Exception as e:                              # noqa: BLE001
        result.errors.append(f"[versions] 版本目錄安裝失敗: {e}")
        logging.warning("[更新] 版本目錄 %s 安裝失敗 → 整個丟掉,指標不動",
                        vroot, exc_info=True)
        try:
            if os.path.isdir(vroot):
                shutil.rmtree(vroot)
        except OSError:
            logging.debug("[更新] 清掉半成品版本目錄失敗", exc_info=True)
        return False


def _switch_version_pointer(app_dir: str, version: str, vp) -> bool:
    """★原子切換★:`current.txt` ← 版本字串(同磁碟 rename)。→ 切成功嗎。"""
    pointer = os.path.join(app_dir, vp.POINTER_NAME)
    tmp = pointer + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(f"{version}\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, pointer)
        _fsync_path(pointer)
        logging.info("[更新] 版本指標已切到 %s(下次啟動生效)", version)
        return True
    except Exception:                                   # noqa: BLE001
        logging.error("[更新] ★切換版本指標失敗★ 新版已裝好但沒有生效 ——"
                      "仍跑舊版(下一輪會再試)", exc_info=True)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def _prune_old_versions(app_dir: str, keep_version: str, vp) -> None:
    """只留最近 `KEEP_VERSIONS` 個版本目錄,★現用的那個永遠不刪★。"""
    import shutil  # noqa: PLC0415
    root = _safe_versions_root(app_dir, vp)
    if not root:
        return                       # 不安全就不要在那裡遞迴刪東西
    try:
        names = [n for n in os.listdir(root)
                 if os.path.isdir(os.path.join(root, n))]
    except OSError:
        return
    # 依 mtime 由新到舊(版本字串排序在跨年/補號時不可靠)
    def _mtime(n):
        try:
            return os.path.getmtime(os.path.join(root, n))
        except OSError:
            return 0.0
    names.sort(key=_mtime, reverse=True)
    keep = {str(keep_version)}
    # ★正在跑的、以及指標現在指著的那一棵,都不可以刪★:指標切過去之後
    #   現行行程仍然從【舊】那一棵 lazy import,一路用到重啟為止 ——
    #   把它刪掉不是「下次啟動起不來」,是【現在】就當掉。
    _root_nc = os.path.normcase(os.path.abspath(root))
    for _d in (_running_src_dir(), _installed_src_dir(app_dir)):
        _parent = os.path.dirname(os.path.abspath(_d))
        if os.path.normcase(os.path.dirname(_parent)) == _root_nc:
            keep.add(os.path.basename(_parent))
    for n in names[:KEEP_VERSIONS]:
        keep.add(n)
    for n in names:
        if n in keep:
            continue
        try:
            shutil.rmtree(os.path.join(root, n))
            logging.info("[更新] 清掉舊版本目錄 %s", n)
        except OSError:
            logging.debug("[更新] 清舊版本目錄失敗 %s", n, exc_info=True)


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

    # ── 批次L・L2:`src/` 底下的檔改走版本化目錄 ──────────────────────────
    #   ★順序刻意是「先裝版本目錄 → 再就地寫 stub → 最後才切指標」★
    #   * 版本目錄裝到一半失敗:整個丟掉,指標沒動 —— 磁碟上的正式檔一個
    #     位元組都沒被碰過(這正是版本化要換掉「就地覆蓋」的理由);
    #   * stub 就地寫失敗:走既有的 .bak 回滾,而指標【還沒切】—— 舊 stub
    #     配舊 src,仍然一致;
    #   * 指標切失敗:新版裝好了但沒生效,下一輪再切(仍跑舊版,不是壞版)。
    versioned_ok = False
    versioned_ver = ""
    vp_mod = None
    try:
        _app_dir_now = get_app_dir()
    except Exception:                                   # noqa: BLE001
        _app_dir_now = ""
    src_writes = [w for w in prepared_writes if _is_src_relative(w[1])]
    if src_writes and _app_dir_now:
        vp_mod = _load_version_pointer(_app_dir_now)
        _app_ver = str(result.manifest_app_version or "")
        # ★resolver 的實際呼叫全部收在這一個 fail-to-in-place 邊界裡★
        #   (外審 L2 第 2 輪 P2):`_load_version_pointer` 已經驗過介面,
        #   但「驗過的那一刻能用」不等於「每一次呼叫都不拋」(磁碟上的檔
        #   可能同時被換掉、實作可能對某些輸入炸掉)。契約是【完全退化成
        #   舊路徑】,所以這裡把例外一律翻譯成「這一輪不做版本化安裝」,
        #   而不是讓它從 commit 逸出、把整批更新變成不明失敗。
        try:
            if vp_mod is not None and _app_ver \
                    and vp_mod.is_safe_version(_app_ver):
                # ★resolver 就在這個保護區【裡面】被實際呼叫★:strict 版本
                #   會把「拋錯 / 回傳不符契約」直接送到下面那個 except,
                #   整批走就地更新(外審 L2 第 4 輪 P2)。
                _inst_src = _strict_installed_src_dir(vp_mod, _app_dir_now)
                # ★不可以裝進「現在正在用/正在跑/別人跑著」的那個目錄★:
                #   安裝第一步是把目標整個刪掉重建。
                versioned_ver = _pick_version_dir(
                    _app_dir_now, _app_ver, vp_mod, _inst_src)
        except Exception:                               # noqa: BLE001
            logging.warning("[更新] 版本指標判斷失敗 → 這一輪走就地更新",
                            exc_info=True)
            versioned_ver = ""
        if versioned_ver and vp_mod is not None:
            versioned_ok = _install_versioned_src(
                _app_dir_now, versioned_ver, src_writes, vp_mod, result)
            if not versioned_ok:
                # ★整批放棄,正式檔一個位元組都沒動★
                #   (承重的是上面那句 `result.errors.append` —— 既有的
                #    「有 error 就整批放棄」不變量本來就會擋住 Phase 1;
                #    這個 return 是把意圖寫明的第二層,不是唯一的防線。)
                return result
            # 這一批的 src 檔已經進了版本目錄 → 就地階段只處理其餘的檔
            # (六支 .pyw、version_pointer.py 這些「切版本救不回來」的)。
            prepared_writes = [w for w in prepared_writes
                               if not _is_src_relative(w[1])]
            for _k, _lf, _nv, _c, _t in src_writes:
                result.updated_files.append((_lf, _nv))
        else:
            logging.info("[更新] 版本化安裝條件不足(resolver=%s, 版本=%r)→"
                         " 這一輪走就地更新(行為與 L1 之前相同)",
                         "有" if vp_mod is not None else "無", _app_ver)

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

    # ★[P1-08 2026-08-01] 動第一個正式檔之前先落交易日誌★
    #   Phase 2 是逐檔 replace 的：行程在中途被砍（watchdog 重啟、關機、斷電）時，
    #   磁碟上會是「一部分新、一部分舊」，而 process 內的回滾幫不上忙（那個 process
    #   已經不在了）。日誌讓下次啟動知道「上次那批沒走完」→ 用 .bak 整批回滾。
    #   寫不出日誌就不要開始 —— 沒有日誌的中途崩潰是不可復原的。
    app_dir_for_journal = os.path.dirname(staged[0][1]) if staged else ""
    try:
        app_dir_for_journal = get_app_dir()
    except Exception:
        logging.debug("[更新] 取 app_dir 失敗，交易日誌改放第一個目標檔的目錄",
                      exc_info=True)
    if not _write_commit_journal(
            app_dir_for_journal,
            [(entry[1], entry[2], entry[0]) for entry in staged]):
        result.errors.append("[journal] 交易日誌寫入失敗，本批不寫入")
        for entry in staged:
            try:
                if os.path.exists(entry[0]):
                    os.remove(entry[0])
            except OSError:
                pass
        return result

    # Phase 2：逐檔 backup→replace（同磁碟 rename，幾乎不會失敗）
    written_files: list[_WrittenFile] = []
    for tmp_path, target_path, existed_before, key, local_filename, new_ver in staged:
        try:
            if existed_before:
                _make_backup_atomically(target_path)
            _replace_file_with_retry(tmp_path, target_path)
            result.updated_files.append((local_filename, new_ver))
            written_files.append(_WrittenFile(target_path, existed_before))
            logging.info("  ✅ 已更新 %s -> v%s", local_filename, new_ver)
        except Exception as e:
            result.errors.append(f"[{key}] 寫入失敗: {e}")
            break

    if result.errors:
        rb = _rollback_written_files(written_files)
        rollback_errors, unresolved = rb.errors, rb.unresolved
        result.errors.extend(rollback_errors)
        result.updated_files.clear()
        # 清掉任何殘留、尚未 replace 的 .upd.tmp
        for entry in staged:
            try:
                if os.path.exists(entry[0]):
                    os.remove(entry[0])
            except OSError:
                pass
        # ★[2026-08-02 外審第 3 輪 P2] terminal 必須在 unresolved 【之前】處理★
        #   上一版把 `if rb.terminal:` 放在 `if unresolved:` 後面，而那一支會
        #   return —— 兩種失敗同時發生時，terminal 分支永遠跑不到：日誌被縮成
        #   只剩可重試的檔，重試成功後日誌清掉，磁碟仍混版而所有痕跡都沒了。
        marker_ok = True
        if rb.terminal:
            marker_ok = _archive_failed_journal(app_dir_for_journal, rb.terminal)
        if not marker_ok:
            # ★[外審第 3 輪 P1] 標記沒寫成功就不准動日誌★
            #   標記與日誌都不在＝下次啟動一片乾淨，混版被無聲放行。
            logging.error("更新寫入失敗，且「無法修復」標記寫不出來 → "
                          "保留完整交易日誌作為唯一證據")
            return result
        if unresolved:
            # ★[2026-08-01 外審 P1] 回滾自己也會失敗（防毒暫時鎖住檔最常見）★
            #   原本無條件清日誌 —— 磁碟上還是半新半舊，卻再也沒有標記讓下次啟動
            #   重試，等於把唯一的修復機會丟掉。留下（縮寫成只剩沒還原成功的檔）。
            _rewrite_journal_for_retry(app_dir_for_journal, unresolved)
            logging.error("更新寫入失敗，且有 %d 個檔【沒有】回滾成功 → "
                          "已保留交易日誌，下次啟動會再試", len(unresolved))
            return result
        # 全部都回滾完了 → 日誌沒有存在的理由（留著會讓下次啟動再回滾一次，
        # 而那時 .bak 已經被 rollback 消耗掉 → 每次啟動噴一批 error）。
        _clear_commit_journal(app_dir_for_journal)
        # ★誠實計數★ 報 `restored`，不是整批的長度：崩潰點之後那些根本沒被
        #   替換過的檔不算「回滾了」。
        logging.warning("更新寫入失敗，已回滾 %d 個檔案（本批共 %d 個）",
                        len(rb.restored), len(written_files))
        return result

    # ★整批 replace 都成功了 → 先清日誌，再清 .bak★
    #   順序不可對調：先刪 .bak 再刪日誌的話，中間崩潰會留下「日誌說要回滾、
    #   但備份已經沒了」的狀態 —— 那比沒有日誌更糟。
    #   ★[2026-08-01 外審 P1] 而且要【確認】日誌真的不在了才准動備份★
    #   清不掉就整套備份留著：日誌還在＋備份完整 = 下次啟動能乾淨回滾（退回舊版，
    #   下一輪再更新）；日誌還在＋備份缺一半 = 混版本，沒人修得了。
    if not _clear_commit_journal(app_dir_for_journal):
        # ★[2026-08-01 外審第 2 輪 P1] 不可以就這樣回去★
        #   上一版只是「留著備份 + 記個 error 就 return」。但那樣 `has_update`
        #   不會被設起來 → 呼叫端不會要求重啟 → 行程繼續跑著【舊模組】，磁碟上卻
        #   已經是【整批新檔】：之後任何一個延遲 import 都會載到新版，變成同一個
        #   行程裡新舊混用。
        #   現在鎖還在手上、備份也還完整 —— 這是把它收乾淨最好的時機：整批回滾。
        #   回滾完磁碟＝舊版，跟記憶體裡的舊模組一致，連重啟都不需要。
        result.errors.append(
            "[journal] 交易日誌清不掉 → 整批回滾（磁碟與記憶體都保持在舊版）")
        _rb = _rollback_written_files(written_files)
        rb_errors, rb_unresolved = _rb.errors, _rb.unresolved
        result.errors.extend(rb_errors)
        result.updated_files.clear()
        if rb_unresolved:
            logging.error("[更新] 日誌清不掉且回滾也有 %d 個檔沒成功 —— "
                          "交易日誌留著，下次啟動會再試", len(rb_unresolved))
        elif _rb.terminal:
            # 同上：救不回來的檔不在 unresolved 裡，不可以當成乾淨結案。
            if _archive_failed_journal(app_dir_for_journal, _rb.terminal):
                _clear_commit_journal(app_dir_for_journal)
            else:
                logging.error("[更新] 「無法修復」標記寫不出來 → 保留交易日誌")
        else:
            # 回滾用掉了 .bak；再試一次清日誌。清不掉也不致命：下次啟動會照日誌
            # 重跑一次回滾，那時檔案已經是舊版、備份也沒了 → 噴一批 error 後把
            # 日誌清掉。吵，但磁碟本身是對的、而且會自己收斂。
            _clear_commit_journal(app_dir_for_journal)
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

    # ★整批 commit 成功 → 混版已經被整棵樹的新版蓋掉★
    #   這是「救不回來」標記的出口（見 `clear_failed_journal_marker`）：
    #   本批的每個檔都寫成了 manifest 上的 SHA，其餘的在下載階段就比對相符。
    clear_failed_journal_marker(app_dir_for_journal)

    # ── 批次L・L2:★最後一步才切指標★ ────────────────────────────────────
    #   走到這裡代表:版本目錄已裝好並驗過 SHA(含 `.complete`)、就地那幾個
    #   「切版本救不回來」的檔也已經成功寫入。切換是單一個 `os.replace`,
    #   同磁碟 rename 是原子的:成功 = 下次啟動整棵樹一起換過去;
    #   失敗 = 新版裝好但沒生效,仍跑舊版(下一輪再切),不是壞版。
    #   ★切不過去不算整批失敗★:磁碟上是【兩個都完整】的版本,只是指標
    #   還指著舊的 —— 這是安全的一邊,不需要回滾已經寫好的 stub。
    if versioned_ok and vp_mod is not None and _app_dir_now:
        if _switch_version_pointer(_app_dir_now, versioned_ver, vp_mod):
            _prune_old_versions(_app_dir_now, versioned_ver, vp_mod)
        else:
            result.errors.append(
                "[versions] 版本指標切換失敗(新版已裝好,這一輪仍跑舊版)")
            # ★指標沒切成功就【不可以】說「有更新、請重啟」★:呼叫端
            #   (打卡/會診/座標)只看 `need_restart_after_update`,不看
            #   errors —— 重啟後 stub 讀到的還是舊指標、跑的還是同一份
            #   程式碼;失敗若持續(指標被防毒鎖住)就成了重啟迴圈。
            #   這一輪確實沒有任何東西生效,所以據實回報「沒有更新」。
            result.updated_files.clear()

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
