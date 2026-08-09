# -*- coding: utf-8 -*-
"""[批次 L・L1] 版本化安裝目錄的【讀取】能力。

設計見 `docs/批次L_版本化目錄與原子切換_設計_2026-08-03.md`。
L1 只做讀取：`current.txt` 不存在時走現行的 `<app>\\src`，**行為與今天完全相同**。
更新流程完全不動（那是 L2）。

★這支檔案的位置與相依是刻意的★
* 放在 **app 根目錄**，不在 `src/` 底下 —— 因為它要回答的正是「該載入哪一棵
  `src`」。放進 `src/` 就是雞生蛋:要先知道版本才 import 得到它。
* **只用標準庫**，不 import 任何本專案模組（與 `bootstrap_recovery` 同一條規矩）。
* 六支 `.pyw` 與這支檔案是唯一「切版本救不回來」的檔 —— 它們就地更新。
  所以這裡的每一個失敗路徑都必須有出口,不可以讓六支程式一起起不來。

★三種情況要分清楚,不可以摺成一種★
| 情況 | 意思 | 該怎麼辦 |
|---|---|---|
| 沒有 `current.txt` | **過渡期的正常狀態**(L1/L2 都還沒切) | 安靜走 `<app>\\src` |
| 有指標但讀不出來／不安全 | 指標壞了 | 走 `<app>\\src`,**大聲留紀錄** |
| 有指標但版本目錄不存在／沒有 `.complete` | 半成品或被清掉了 | 同上 |

把後兩種當成第一種,就是「安靜地跑舊版,讓人以為更新成功了」——
那正是這一整批要消滅的東西。
"""
from __future__ import annotations

import datetime
import os
from collections import namedtuple

POINTER_NAME = "current.txt"
VERSIONS_DIRNAME = "versions"
COMPLETE_MARKER = ".complete"
LOG_NAME = "version_pointer.log"

# 版本字串只允許這些字元（版本號長成 `2026.08.09.3`）。
# ★這是路徑穿越防線★ `current.txt` 的內容會被拼進路徑;`..`、`/`、`\`、磁碟機
#   代號都必須擋掉,否則一個被竄改（或寫壞）的指標可以指到任意目錄。
_SAFE_CHARS = set("0123456789abcdefghijklmnopqrstuvwxyz"
                  "ABCDEFGHIJKLMNOPQRSTUVWXYZ._-")
_MAX_VERSION_LEN = 64

#: `reason` 的封閉集合 —— 呼叫端可以據此分流,不必比對字串長相。
PINNED = "pinned"                      # 真的用了版本化目錄
NO_POINTER = "no_pointer"              # ★過渡期的正常狀態★
POINTER_UNREADABLE = "pointer_unreadable"
UNSAFE_VERSION = "unsafe_version"
VERSION_MISSING = "version_missing"
INCOMPLETE = "incomplete"

#: 只有這一個 reason 是「本來就預期會發生」的,其餘都要留紀錄。
EXPECTED_REASONS = frozenset({PINNED, NO_POINTER})

Resolution = namedtuple("Resolution", "src_dir version reason")


def _legacy_src(app_dir: str) -> str:
    return os.path.join(app_dir, "src")


def is_safe_version(text) -> bool:
    """這個字串可不可以安全地拼進路徑。"""
    s = str(text or "").strip()
    if not s or len(s) > _MAX_VERSION_LEN:
        return False
    if s in (".", ".."):
        return False
    return all(ch in _SAFE_CHARS for ch in s)


def read_pointer(app_dir: str):
    """→ (版本字串 或 None, reason)。★「沒有指標」與「讀不出來」是兩件事★"""
    path = os.path.join(app_dir, POINTER_NAME)
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read(_MAX_VERSION_LEN * 4)
    except FileNotFoundError:
        return None, NO_POINTER
    except OSError:
        # 存在但打不開（權限、防毒鎖住、磁碟錯誤）—— 這【不是】「沒有指標」。
        return None, POINTER_UNREADABLE
    except Exception:                                   # noqa: BLE001
        return None, POINTER_UNREADABLE
    version = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    if not version:
        return None, POINTER_UNREADABLE
    if not is_safe_version(version):
        return None, UNSAFE_VERSION
    return version, PINNED


def version_src_dir(app_dir: str, version: str) -> str:
    return os.path.join(app_dir, VERSIONS_DIRNAME, version, "src")


def is_complete(app_dir: str, version: str) -> bool:
    """裝到一半的版本目錄絕對不可以被指到。"""
    marker = os.path.join(app_dir, VERSIONS_DIRNAME, version, COMPLETE_MARKER)
    try:
        return os.path.exists(marker)
    except Exception:                                   # noqa: BLE001
        return False


def resolve_src(app_dir: str, program_name: str = "") -> Resolution:
    """決定這一次要載入哪一棵 `src`。**任何失敗都回退到 `<app>\\src`,不丟例外。**

    回傳的 `reason` 是封閉集合;`reason not in EXPECTED_REASONS` 代表
    「指標存在但用不了」—— 那一定會留下紀錄（見 `_note`）。
    """
    app_dir = os.path.abspath(app_dir)
    version, reason = read_pointer(app_dir)
    if version is None:
        if reason != NO_POINTER:
            _note(app_dir, program_name, reason, "")
        return Resolution(_legacy_src(app_dir), "", reason)
    src = version_src_dir(app_dir, version)
    if not is_complete(app_dir, version):
        _note(app_dir, program_name, INCOMPLETE, version)
        return Resolution(_legacy_src(app_dir), "", INCOMPLETE)
    try:
        ok = os.path.isdir(src)
    except Exception:                                   # noqa: BLE001
        ok = False
    if not ok:
        _note(app_dir, program_name, VERSION_MISSING, version)
        return Resolution(_legacy_src(app_dir), "", VERSION_MISSING)
    return Resolution(src, version, PINNED)


def _note(app_dir: str, program_name: str, reason: str, version: str) -> None:
    """把「指標存在但用不了」寫進 log。★best-effort,但絕不可以安靜★

    寫不進去也不能讓六支程式起不來 —— 所以這裡吞例外;
    但一般情況下這個檔案就是唯一會說「你以為在跑新版,其實在跑舊版」的地方。
    """
    try:
        stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with open(os.path.join(app_dir, LOG_NAME), "a", encoding="utf-8") as fh:
            fh.write("%s [%s] current.txt 指到的版本用不了（%s%s）→ "
                     "★退回舊的 <app>\\src★\n"
                     % (stamp, program_name or "?", reason,
                        ("：" + version) if version else ""))
    except Exception:                                   # noqa: BLE001
        pass
