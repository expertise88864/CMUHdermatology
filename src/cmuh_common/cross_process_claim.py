# -*- coding: utf-8 -*-
"""跨行程的獨佔宣告(claim)——★不依賴 Windows mutex API★。

為什麼需要它(外審 R3-P2-04 R2 P1):打卡的「先查刷卡表、沒紀錄才打」是典型的
check-then-act —— 查完到真的點下去之間還有 1~5 秒的隨機延遲與數次頁面操作,
中間★沒有任何跨行程的序列化★。兩份 autoclock 同時跑時,兩邊都會讀到「尚無
紀錄」而各打一次。而且★重讀刷卡表擋不住★:`get_current_swipe_info` 讀的是
目前這個瀏覽器的 DOM,別的行程打的卡不會出現在裡面(打完自己那一次之所以
驗得到,是因為自己的 postback 讓頁面重繪了)。

這不是假想:repo 內的「清理重複打卡程式.ps1」「診斷打卡重複執行.ps1」就是
session 0 雙開造成重複打卡之後留下的現場工具(見 `watchdog_core.start_program`
的註解)。單例 mutex 是 `Local\\` 的、擋不住跨 session,而 mutex API 本身壞掉時
更是連查都查不出來。

★設計上的三個硬要求★
1. ★不可以永久卡住★:擁有者死掉(當機、被 kill)之後,下一個要來的人必須
   接得走。所以紀錄裡存 PID + 行程建立時間(排除 PID 重用),查得出「已經
   不在了」就接手;另外還有一個 TTL,擋住「還活著但整個卡死」的擁有者。
2. ★接手本身要防競爭★:兩個人同時發現擁有者已死時,只能有一個接手成功 ——
   寫進暫存檔再 `os.replace`,然後★回頭讀一次確認裡面是自己★。
3. ★這一層壞掉不可以讓打卡停擺★:建不了目錄、讀不了檔之類的意外一律
   fail-open(照常打卡)。它是額外的一道防線,不是打卡的前提條件 ——
   反過來會變成「檔案系統一有問題就整天不打卡」,那比重複打卡更難發現。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from contextlib import contextmanager

_log = logging.getLogger(__name__)

#: ★行程內的那一半互斥★:作業系統的檔案鎖是【以行程為單位】的 —— 同一個
#: 行程裡的兩個執行緒各自開 fd 去鎖同一段,兩邊都會成功(行程不會擋自己)。
#: 打卡程式本身是多執行緒的(排程執行緒 + UI),所以行程內也要有一份。
#: 逐 key 一把,不同帳號/不同打卡窗仍然可以平行跑。
_local_locks_guard = threading.Lock()
_local_locks: dict = {}


def _local_lock(key: str) -> threading.Lock:
    with _local_locks_guard:
        lk = _local_locks.get(key)
        if lk is None:
            lk = _local_locks[key] = threading.Lock()
        return lk

#: 擁有者還活著、但整個卡死時,最多讓別人等這麼久(秒)。
#: 打卡一次(登入→讀表→延遲→點擊→驗證)實測遠短於此;取寬鬆值是因為
#: ★誤判「擁有者不見了」的代價是重複打卡★,而多等一會兒只是晚打。
DEFAULT_TTL_SEC = 300.0


def _claims_dir() -> str:
    from cmuh_common.paths import get_settings_dir  # noqa: PLC0415
    return os.path.join(get_settings_dir(), "claims")


def _claim_path(key: str) -> str:
    # key 會含帳號/時段,不可以直接當檔名(路徑字元、長度、大小寫)。
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return os.path.join(_claims_dir(), f"{digest}.json")


def _own_create_time():
    try:
        import psutil  # noqa: PLC0415
        return float(psutil.Process(os.getpid()).create_time())
    except Exception:
        return None


def _record(key: str) -> dict:
    return {"key": key, "pid": os.getpid(),
            "create_time": _own_create_time(), "ts": time.time()}


def _owner_gone(rec: dict, ttl_sec: float) -> bool:
    """擁有者是不是★確定不在了★(或已經超過 TTL)。

    ★查不出來一律當成「還在」★:接手的代價是重複打卡,而多等一輪只是晚打
    —— 兩邊不對稱,所以未知要倒向保守那一邊。
    """
    try:
        ts = float(rec.get("ts") or 0.0)
    except (TypeError, ValueError):
        ts = 0.0
    if ts and (time.time() - ts) > ttl_sec:
        return True                    # 卡死的擁有者:TTL 是唯一的出口
    pid = rec.get("pid")
    if not isinstance(pid, int) or pid <= 0:
        return False                   # 壞紀錄:查不出來 → 當成還在
    if pid == os.getpid():
        return True                    # 自己上次留下來的(同一支重跑)
    try:
        import psutil  # noqa: PLC0415
        proc = psutil.Process(pid)
        ct = rec.get("create_time")
        if ct is None:
            return False               # 舊格式:無從排除 PID 重用 → 當成還在
        return abs(float(proc.create_time()) - float(ct)) > 1.0
    except Exception as exc:           # NoSuchProcess / psutil 不可用
        try:
            import psutil  # noqa: PLC0415
            if isinstance(exc, psutil.NoSuchProcess):
                return True            # ★確定不在了★
        except Exception:
            pass
        return False                   # 其它(權限/psutil 缺)→ 當成還在


def _read_claim(path: str):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except FileNotFoundError:
        return None
    except Exception:
        return {}                      # 壞檔:當成「有人拿著但查不出是誰」


def _lock_path(path: str) -> str:
    """★永遠不會被刪掉的鎖檔★ —— 這是互斥的根據。

    (外審 R3-P2-04 R4 P1)只靠「rename 走 + 建回來」不夠:B 把 A 的宣告搬走
    去驗身分的那一瞬間,★原路徑不存在★,第三個人 C 就能用 `O_EXCL` 建起來,
    於是 A 與 C 同時打卡。要真正互斥,所有取得者都必須先拿到★同一個、
    而且不會暫時消失★的東西 —— 作業系統層級的檔案鎖。
    """
    return f"{path}.lock"


@contextmanager
def _os_file_lock(path: str, *, deadline_sec: float = 5.0):
    """對 `path` 取得 OS 層級的獨佔鎖 → yield True/False(逾時)。

    ★只在【判定與寫入】這幾毫秒內持有★,不是整個打卡期間 —— 長時間的互斥由
    宣告紀錄本身負責(那才是可以跨越當機的東西)。
    ★行程死掉時作業系統會自動釋放★,所以鎖本身不會把人卡死。
    逾時代表有人卡在那幾毫秒裡 —— 極不正常,回 False 讓這一輪略過
    (打卡是每分鐘 re-fire 的,★略過一輪不等於漏打★)。
    """
    fd = None
    locked = False
    try:
        fd = os.open(path, os.O_CREAT | os.O_RDWR)
        deadline = time.monotonic() + max(0.0, deadline_sec)
        while True:
            try:
                _lock_fd(fd)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    _log.error("[claim] 取鎖逾時(%s)→ 本輪略過", path)
                    yield False
                    return
                time.sleep(0.05)
        yield True
    except Exception:
        # 拿不到鎖檔本身(權限/磁碟)→ 少一道防線,但不可以讓打卡停擺。
        _log.warning("[claim] 鎖檔無法使用 → 照常執行(少一道防線)",
                     exc_info=True)
        yield True
    finally:
        if fd is not None:
            try:
                if locked:
                    _unlock_fd(fd)
            finally:
                try:
                    os.close(fd)
                except OSError:
                    pass


def _lock_fd(fd: int) -> None:
    """非阻塞地鎖住一個 byte。拿不到就拋 OSError(由上面重試)。

    ★只做 Windows★:這一整個 repo 是 Windows 專用的(到處都是
    `ctypes.windll`、`msvcrt`、schtasks),`msvcrt` 一定在。原本還寫了一段
    `fcntl` 的後備 —— 在這裡★永遠跑不到★,而且 pyright 在 Windows 上根本
    不認得 `fcntl`(平白多五筆型別債)。量不到的分支是死碼,刪掉。
    """
    import msvcrt  # noqa: PLC0415
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)


def _unlock_fd(fd: int) -> None:
    import msvcrt  # noqa: PLC0415
    os.lseek(fd, 0, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def _write_record(path: str, rec: dict) -> None:
    """★在鎖裡面寫★ —— 不需要 CAS,互斥由鎖保證。"""
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(rec, fh, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _acquire(path: str, rec: dict, ttl_sec: float) -> bool:
    """在鎖裡面做判定與寫入 → 是否拿到宣告。"""
    cur = _read_claim(path)
    if cur is not None and not _owner_gone(cur, ttl_sec):
        _log.warning("[claim] 已由 PID=%s 持有 → 本次略過", cur.get("pid"))
        return False
    if cur is not None:
        _log.warning("[claim] 前一位持有者(PID=%s)已不在或逾時 → 接手",
                     cur.get("pid"))
    _write_record(path, rec)
    return True


@contextmanager
def exclusive_claim(key: str, *, ttl_sec: float = DEFAULT_TTL_SEC):
    """跨行程獨佔 `key` → yield True(拿到)/ False(別人正拿著且還活著)。

    ★互斥靠的是【永遠不會消失的鎖檔】★(見 `_lock_path`):判定與寫入都在鎖
    裡面做,所以不存在「宣告檔暫時不見、第三個人趁空建立」的窗。宣告紀錄
    本身負責跨越當機(擁有者死掉 → 下一個人在鎖裡看到、接手)。

    任何意外一律 yield True(見模組說明的第 3 點)。
    """
    path = ""
    mine = False
    try:
        os.makedirs(_claims_dir(), exist_ok=True)
        path = _claim_path(key)
        rec = _record(key)
    except Exception:
        _log.warning("[claim] %s 取得失敗 → 照常執行(少一道防線)", key,
                     exc_info=True)
        yield True
        return

    local = _local_lock(key)
    if not local.acquire(blocking=False):
        _log.warning("[claim] %s 已由本行程的另一個執行緒持有 → 本次略過", key)
        yield False
        return
    try:
        try:
            with _os_file_lock(_lock_path(path)) as got_lock:
                if not got_lock:
                    yield False
                    return
                mine = _acquire(path, rec, ttl_sec)
        except Exception:
            _log.warning("[claim] %s 取得失敗 → 照常執行(少一道防線)", key,
                         exc_info=True)
            yield True
            return
        if not mine:
            yield False
            return
        yield True
    finally:
        try:
            if mine:
                with _os_file_lock(_lock_path(path)) as got_lock:
                    if got_lock:
                        cur = _read_claim(path)
                        if cur and cur.get("pid") == os.getpid():
                            os.unlink(path)
        except Exception:
            _log.debug("[claim] %s 釋放失敗(TTL 會兜底)", key, exc_info=True)
        finally:
            local.release()
