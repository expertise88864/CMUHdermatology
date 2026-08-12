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
POINTER_MALFORMED = "pointer_malformed"    # 讀得出來,但不是「恰好一行」
UNSAFE_VERSION = "unsafe_version"
VERSION_MISSING = "version_missing"
INCOMPLETE = "incomplete"
ESCAPES_VERSIONS = "escapes_versions_dir"  # 實體位置跑出 versions/ 外(junction)

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
    """→ (版本字串 或 None, reason)。★「沒有指標」與「讀不出來」是兩件事★

    ★[外審 2026-08-12 P2-02] 格式是【恰好一個邏輯行】★
    這個檔是原子版本選擇器 —— 寫入端一次只會寫一行。多出來的任何非空白
    內容(`V2\\nTHIS_FILE_IS_CORRUPTED`)都代表寫入被打斷或被別的東西動過:
    「第一行剛好還像版本號」不能當成沒事,那正是壞了一半的樣子。
    同理,檔案大到超過讀取上限(指標不該有這種大小)也一律當壞掉。
    用 utf-8-sig:有些編輯器會補 BOM,BOM 不該讓指標失效。
    """
    path = os.path.join(app_dir, POINTER_NAME)
    try:
        with open(path, encoding="utf-8-sig") as fh:
            raw = fh.read(_MAX_VERSION_LEN * 4)
            beyond_cap = fh.read(1)
    except FileNotFoundError:
        return None, NO_POINTER
    except OSError:
        # 存在但打不開（權限、防毒鎖住、磁碟錯誤）—— 這【不是】「沒有指標」。
        return None, POINTER_UNREADABLE
    except Exception:                                   # noqa: BLE001
        return None, POINTER_UNREADABLE
    if beyond_cap:
        return None, POINTER_MALFORMED
    lines = raw.splitlines()
    version = lines[0].strip() if lines else ""
    if not version:
        return None, POINTER_UNREADABLE
    if any(ln.strip() for ln in lines[1:]):
        return None, POINTER_MALFORMED
    if not is_safe_version(version):
        return None, UNSAFE_VERSION
    return version, PINNED


def version_src_dir(app_dir: str, version: str) -> str:
    return os.path.join(app_dir, VERSIONS_DIRNAME, version, "src")


def is_complete(app_dir: str, version: str) -> bool:
    """裝到一半的版本目錄絕對不可以被指到。

    ★用 `isfile` 不用 `exists`★(外審 2026-08-12 P2-01):`.complete` 是
    部署流程最後一步【寫的檔案】—— 同名的目錄不是部署流程留的,是別的
    東西弄出來的,不能當成「裝完了」的證明。
    """
    marker = os.path.join(app_dir, VERSIONS_DIRNAME, version, COMPLETE_MARKER)
    try:
        return os.path.isfile(marker)
    except Exception:                                   # noqa: BLE001
        return False


def _stays_inside_versions(app_dir: str, src: str) -> bool:
    """★實體位置必須留在 `<app>/versions/` 底下★(外審 2026-08-12 P2-01)

    版本字串的字元白名單擋得掉 `..` 與磁碟機代號,但擋不掉【junction /
    符號連結】:`versions/V2` 若是指到別處的 junction,字串層看起來完全
    合法,實際載入的卻是任意目錄。realpath 會把連結展開 —— 展開後不在
    versions/ 底下就拒絕。分不出來(不同磁碟機、realpath 失敗)也拒絕。

    ★而且要看整棵樹,不是只看 `src` 那一層★(外審 AD-1 第 1 輪 P2)
    只 realpath 頂端目錄的話,`src/cmuh_common` 或入口檔本身是指到外面的
    連結時照樣逸出。政策從「展開後留在裡面」收緊成
    ★版本樹內部不允許任何 reparse point★ —— 我們自己的部署流程只會
    複製檔案,永遠不會建連結;樹裡出現連結本身就是「不是部署流程放的」
    的證據,不必分辨它指到哪裡。掃不動(權限、AV 鎖住)也拒絕:
    這條路的回退是大聲走 `<app>/src`,不是擋住開機。
    """
    try:
        root = os.path.normcase(
            os.path.realpath(os.path.join(app_dir, VERSIONS_DIRNAME)))
        real = os.path.normcase(os.path.realpath(src))
        if os.path.commonpath([root, real]) != root or real == root:
            return False
        version_dir = os.path.dirname(src)      # <app>/versions/<version>
        return not _tree_has_reparse_points(version_dir)
    except Exception:                                   # noqa: BLE001
        return False


def _tree_has_reparse_points(top: str) -> bool:
    """整棵樹(含 `top` 自己)有沒有任何 reparse point。查不動=有(fail-closed)。

    `entry.is_symlink()` 看不到 junction(那是目錄的 reparse point,不是
    symlink),所以用 `st_file_attributes` 的 FILE_ATTRIBUTE_REPARSE_POINT ——
    junction、symlink、mount point 一網打盡。非 Windows 退回 lstat/islink。
    """
    import stat as _stat
    attr = getattr(_stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)

    def _is_reparse(path: str) -> bool:
        st = os.lstat(path)
        if attr and getattr(st, "st_file_attributes", 0) & attr:
            return True
        return _stat.S_ISLNK(st.st_mode)

    # ★[外審 AD-1 第 2 輪 P2] os.walk 預設會【靜默跳過】列舉失敗的子樹★
    #   沒給 onerror 的話,一個拒絕列目錄的子樹就讓掃描「沒看到=沒有」——
    #   守衛自己 no-op 掉了。onerror 一律 raise,由呼叫端的 except 收成
    #   「查不動=拒絕」(大聲回退 <app>/src,不擋開機)。
    def _refuse_to_skip(err):
        raise err

    if _is_reparse(top):
        return True
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False,
                                                onerror=_refuse_to_skip):
        for name in dirnames + filenames:
            if _is_reparse(os.path.join(dirpath, name)):
                return True
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
    if not _stays_inside_versions(app_dir, src):
        _note(app_dir, program_name, ESCAPES_VERSIONS, version)
        return Resolution(_legacy_src(app_dir), "", ESCAPES_VERSIONS)
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
