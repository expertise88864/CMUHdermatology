# -*- coding: utf-8 -*-
"""[批次 Y] 排程版 watchdog 的入口必須是固定的 `.pyw`，不是 `src\\watchdog_runner.py`。

★問題（外審 2026-08-09 P1-01）★
`安裝開機自動啟動.ps1` 以前把每 2 分鐘的 task 註冊成：

    pythonw.exe "<app>\\src\\watchdog_runner.py" --once

那是**直接執行 src 底下那一支**，不經過根目錄的 `.pyw` 啟動器。於是：

* `current.txt` 切到新版之後，主程式／打卡／會診都跑新版，
  **只有 watchdog 每兩分鐘永遠執行 `<app>\\src` 的舊版**；
* 新版的修正、設定 migration、process identity／recovery policy 全部不生效；
* `CMUH_APP_DIR` / `CMUH_LAUNCHER` 根本沒有被設過（那是 launcher 才會設的）。

**最後一道復原防線自己停在舊版**，而且沒有任何地方會說出來。

★而且改 installer 不夠★
已經部署的電腦上，舊的 task 不會自己更新 —— 沒有人會再跑一次安裝腳本。
所以要在執行期偵測並改寫，**改寫後回讀確認**，失敗要講出來（不可以假成功）。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import xml.etree.ElementTree as ET

TASK_NAME = "CMUH皮膚科守護程式_每2分鐘"
LAUNCHER_NAME = "中國醫皮膚科守護程式.pyw"
LEGACY_MARKER = "watchdog_runner.py"

#: 回傳值（封閉集合 —— 呼叫端可以分流，不必比對字串長相）
OK_ALREADY = "already_launcher"     # 本來就對，什麼都沒做
MIGRATED = "migrated"               # 改寫成功且回讀確認
NO_TASK = "no_task"                 # 這台機器沒裝這個排程（正常）
UNREADABLE = "unreadable"           # 查不到現況 → ★不可以當成「已經是對的」★
FAILED = "failed"                   # 改寫失敗或回讀不符

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

#: schtasks 回「找不到這個工作」時訊息裡會有的字樣(中英文)。
#: ★只有這些才算 NO_TASK★ 其餘非零離開碼一律是 UNREADABLE。
_NOT_FOUND_HINTS = (
    "cannot find the file specified",
    "does not exist",
    "the system cannot find",
    "找不到",
    "不存在",
)


def _decode_console(data: bytes) -> str:
    """把 schtasks 的輸出解成文字。★不可以寫死 UTF-8★

    ★[外審第 2 輪 #1] 這是實機量到的,不是推理★
    在本機(繁中 Windows、GetACP/GetOEMCP 都是 936)實測:

    * `schtasks /Query /XML` 導到 pipe 時輸出的是【單位元組的 cp936】,
      **不是** BOM 標示的 UTF-16(雖然 XML 宣告寫著 `encoding="UTF-16"`);
    * 用 `encoding="utf-8", errors="replace"` 解,含中文的欄位會整片變成
      U+FFFD。而我們真正的排程叫 `CMUH皮膚科守護程式_每2分鐘`、
      launcher 是 `中國醫皮膚科守護程式.pyw` —— **每一個字都是中文**。

    後果不是「偶爾解錯」:action 永遠對不上 `desired_action()`,
    於是每兩分鐘改寫一次排程、回讀又永遠不符 → 每輪 FAILED + 告警,
    而舊排程一次都遷移不成功。

    另一些 Windows 版本／語系【確實】會吐 UTF-16 BOM,所以兩種都要吃。
    """
    if not data:
        return ""
    if data[:2] in (b'\xff\xfe', b'\xfe\xff'):
        return data.decode("utf-16", "replace")
    for enc in _decode_candidates():
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    # 全都不行 → 至少不要拋例外(呼叫端會判成 UNREADABLE,那是誠實的)
    return data.decode("utf-8", "replace")


def _decode_candidates():
    """依序嘗試的編碼。UTF-8 先試(嚴格),失敗才退回主機的 code page。"""
    out = ["utf-8-sig", "utf-8"]
    try:
        import ctypes
        for cp in (ctypes.windll.kernel32.GetOEMCP(),      # type: ignore[attr-defined]
                   ctypes.windll.kernel32.GetACP()):       # type: ignore[attr-defined]
            name = "cp%d" % int(cp)
            if name not in out:
                out.append(name)
    except Exception:                               # noqa: BLE001
        pass
    import locale
    try:
        pref = locale.getpreferredencoding(False)
        if pref and pref.lower() not in out:
            out.append(pref)
    except Exception:                               # noqa: BLE001
        pass
    return out


def _run(args: list, timeout: float = 20.0):
    """跑一個 schtasks 指令 → (returncode, stdout, stderr)。失敗回 (None, "", "")。

    ★[外審 P1-03] stderr 一定要帶回來★
    第一版 capture 了 stderr 卻不回傳,而 `query_action()` 在
    「非零 rc + stdout 為空」時判成 `NO_TASK`。schtasks 把錯誤寫在 stderr
    (權限不足、排程服務不可用、參數錯誤)時,stdout 正好是空的 ——
    於是【查詢失敗】被判成【這台沒有 task,正常,不用處理】。
    那正好推翻這個模組自己宣稱的「查不到 ≠ 沒有 task」。

    ★[外審第 2 輪 #1] 用 bytes 收、自己解碼★ 見 `_decode_console()`。
    """
    try:
        cp = subprocess.run(args, capture_output=True, timeout=timeout,
                            creationflags=_CREATE_NO_WINDOW, check=False)
        return (cp.returncode, _decode_console(cp.stdout or b''),
                _decode_console(cp.stderr or b''))
    except Exception:                                   # noqa: BLE001
        logging.debug("[watchdog-task] 指令失敗:%s", args[:2], exc_info=True)
        return None, "", ""


def query_action(task_name: str = TASK_NAME):
    """→ (目前的 action 字串 或 None, reason)。

    ★[外審 P1-03] 用 `/XML`,不要解析 `/FO LIST /V`★
    後者是【給人看的在地化輸出】:欄位名稱會隨系統語言變(`Task To Run:`
    /`工作要執行:`/…),編碼也隨 OEM code page 變。拿它當機器契約,
    換一台語系不同的電腦就對不上 → 回 UNREADABLE → 舊 task 繼續指著
    legacy source。XML 的 `<Command>` / `<Arguments>` 才是穩定契約。

    ★「查不到」與「沒有 task」是兩件事★
    查不到就當成「已經是對的」,等於在不知道的情況下宣稱沒問題。
    """
    rc, out, err = _run(["schtasks", "/Query", "/TN", task_name, "/XML"])
    if rc is None:
        return None, UNREADABLE
    if rc != 0:
        # ★只有【明確說找不到】才是 NO_TASK★ 其餘一律 UNREADABLE。
        #   權限不足、排程服務不可用的訊息寫在 stderr,而 stdout 是空的
        #   —— 用「stdout 空不空」分辨,會把失敗判成「沒有 task」。
        blob = (out + " " + err).lower()
        if any(k in blob for k in _NOT_FOUND_HINTS):
            return None, NO_TASK
        logging.warning("[watchdog-task] 查詢排程失敗(rc=%s):%s",
                        rc, (err or out).strip()[:200])
        return None, UNREADABLE
    try:
        root = ET.fromstring(_strip_xml_declaration(out))
    except ET.ParseError:
        logging.warning("[watchdog-task] 排程 XML 解析失敗 → 查不出來")
        return None, UNREADABLE
    except ValueError:
        # 「Unicode strings with encoding declaration are not supported」
        logging.warning("[watchdog-task] 排程 XML 帶編碼宣告 → 查不出來")
        return None, UNREADABLE
    # Task Scheduler 的 XML 有 namespace;用 local-name 比對免得綁死版本。
    cmd = args = None
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]
        if tag == "Command" and cmd is None:
            cmd = (el.text or "").strip()
        elif tag == "Arguments" and args is None:
            args = (el.text or "").strip()
    if not cmd:
        return None, UNREADABLE
    # ★[外審第 2 輪 #4] Command 要重新加引號★
    #   XML 的 `<Command>` 是【結構化欄位】,本身不含命令列引號。直接
    #   `cmd + " " + args` 串起來的話,Python 裝在
    #   `C:\Program Files\...\pythonw.exe` 的機器上,`_norm_action()` 會把
    #   執行檔拆成兩個 token,永遠對不上 `desired_action()` 的 quoted 形式
    #   —— 正確的排程每兩分鐘被重寫一次,而且回讀永遠判失敗。
    return _join_action(cmd, args or ""), ""



def _strip_xml_declaration(text: str) -> str:
    """拿掉 `<?xml ... ?>`。

    schtasks 的宣告寫著 `encoding="UTF-16"`,但我們手上已經是 str 了。
    某些 Python 版本會對「帶編碼宣告的 str」直接拋 ValueError;
    宣告對我們也沒有任何用處(解碼在 `_decode_console` 就做完了)。
    """
    t = (text or "").lstrip()
    if t.startswith("<?xml"):
        end = t.find("?>")
        if end >= 0:
            return t[end + 2:].lstrip()
    return t


def _join_action(command: str, arguments: str) -> str:
    """把結構化的 Command/Arguments 併成一條可比對的命令列。"""
    c = str(command or "").strip().strip(chr(34))
    a = str(arguments or "").strip()
    if not c:
        return ""
    return ("%s%s%s %s" % (chr(34), c, chr(34), a)).strip()


def action_is_legacy(action: str) -> bool:
    """這個 action 是不是直接跑 `src\\watchdog_runner.py`（沒經過 launcher）。"""
    text = str(action or "")
    if not text:
        return False
    if LAUNCHER_NAME in text:
        return False            # 已經走 launcher
    return LEGACY_MARKER in text


def _norm_action(text: str) -> str:
    """把 action 正規化成可比對的形狀(去引號、收斂空白、小寫、正規化路徑)。"""
    import shlex
    raw = str(text or "").strip()
    if not raw:
        return ""
    try:
        parts = shlex.split(raw, posix=False)
    except ValueError:
        parts = raw.split()
    out = []
    for p in parts:
        p = p.strip().strip(chr(34))
        if not p:
            continue
        if os.sep in p or "/" in p:
            try:
                p = os.path.normcase(os.path.normpath(p))
            except (OSError, ValueError):
                pass
        out.append(p.lower())
    return " ".join(out)


def _same_action(a, b) -> bool:
    """兩個 action 是不是同一件事(引號、斜線、大小寫不算差別)。"""
    na, nb = _norm_action(a), _norm_action(b)
    return bool(na) and na == nb


def desired_action(app_dir: str, pythonw: str = "") -> str:
    exe = pythonw or sys.executable
    return '"%s" "%s" --once' % (exe, os.path.join(app_dir, LAUNCHER_NAME))


def migrate_if_legacy(app_dir: str, task_name: str = TASK_NAME,
                      pythonw: str = "") -> str:
    """把舊的排程改寫成走固定 launcher。→ 回傳封閉集合裡的一個值。

    ★改完一定要回讀★ `schtasks /Change` 回 0 不代表真的寫進去了
    （權限不足、被群組原則擋、名稱大小寫不符都遇過）。
    這個專案反覆踩到的坑就是「下達了就當成生效」。
    """
    action, reason = query_action(task_name)
    if action is None:
        return reason or UNREADABLE
    if _same_action(action, desired_action(app_dir, pythonw)):
        return OK_ALREADY
    if not action_is_legacy(action):
        # 不是 legacy、也不是我們要的 —— 例如指到錯的根、缺 --once、
        # 或被人改成別的東西。★不可以當成「已經是對的」★。
        logging.warning("[watchdog-task] 排程 action 既不是舊的也不是預期的"
                        "(%s)→ 改寫成預期值", action)
    launcher = os.path.join(app_dir, LAUNCHER_NAME)
    if not os.path.isfile(launcher):
        logging.error("[watchdog-task] 找不到 %s → 不改寫（改了會變成排程指向"
                      "不存在的檔，比現況更糟）", launcher)
        return FAILED
    want = desired_action(app_dir, pythonw)
    rc, _out, err = _run(["schtasks", "/Change", "/TN", task_name,
                          "/TR", want])
    if rc != 0:
        logging.error("[watchdog-task] 改寫排程失敗(rc=%s,%s)→ ★watchdog 仍然跑"
                      "舊版 src★", rc, (err or "").strip()[:200])
        return FAILED
    after, _r = query_action(task_name)
    # ★[外審 P1-03] 要【精確比對】,不是「只要不含 watchdog_runner 就算過」★
    #   否則「指到錯的根」「缺 --once」「換成別的執行檔」全都會被當成成功。
    if after is None or not _same_action(after, want):
        logging.error("[watchdog-task] 改寫後回讀不符 → ★沒有真的生效★"
                      "(想要:%s / 實際:%s)", want, after)
        return FAILED
    logging.warning("[watchdog-task] 排程已改走固定啟動器:%s", after)
    return MIGRATED
