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


def _run(args: list, timeout: float = 20.0):
    """跑一個 schtasks 指令 → (returncode, stdout)。失敗回 (None, "")。"""
    try:
        cp = subprocess.run(args, capture_output=True, timeout=timeout,
                            encoding="utf-8", errors="replace",
                            creationflags=_CREATE_NO_WINDOW, check=False)
        return cp.returncode, (cp.stdout or "")
    except Exception:                                   # noqa: BLE001
        logging.debug("[watchdog-task] 指令失敗:%s", args[:2], exc_info=True)
        return None, ""


def query_action(task_name: str = TASK_NAME):
    """→ (目前的 action 字串 或 None, reason)。

    ★「查不到」與「沒有這個 task」是兩件事★
    查不到就當成「已經是對的」，等於在不知道的情況下宣稱沒問題。
    """
    rc, out = _run(["schtasks", "/Query", "/TN", task_name, "/FO", "LIST", "/V"])
    if rc is None:
        return None, UNREADABLE
    if rc != 0:
        # schtasks 對「找不到」與「權限不足」都回非 0；用輸出內容分不出來，
        # 所以這裡只在【明確查得到但沒有 action】時才說 NO_TASK。
        return None, (NO_TASK if not out.strip() else UNREADABLE)
    for line in out.splitlines():
        low = line.strip().lower()
        if low.startswith("task to run:") or low.startswith("工作要執行:"):
            return line.split(":", 1)[1].strip(), ""
    return None, UNREADABLE


def action_is_legacy(action: str) -> bool:
    """這個 action 是不是直接跑 `src\\watchdog_runner.py`（沒經過 launcher）。"""
    text = str(action or "")
    if not text:
        return False
    if LAUNCHER_NAME in text:
        return False            # 已經走 launcher
    return LEGACY_MARKER in text


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
    if not action_is_legacy(action):
        return OK_ALREADY
    launcher = os.path.join(app_dir, LAUNCHER_NAME)
    if not os.path.isfile(launcher):
        logging.error("[watchdog-task] 找不到 %s → 不改寫（改了會變成排程指向"
                      "不存在的檔，比現況更糟）", launcher)
        return FAILED
    rc, _out = _run(["schtasks", "/Change", "/TN", task_name,
                     "/TR", desired_action(app_dir, pythonw)])
    if rc != 0:
        logging.error("[watchdog-task] 改寫排程失敗(rc=%s)→ ★watchdog 仍然跑"
                      "舊版 src★", rc)
        return FAILED
    after, _r = query_action(task_name)
    if after is None or action_is_legacy(after):
        logging.error("[watchdog-task] 改寫後回讀仍是舊的(%s)→ ★沒有真的生效★",
                      after)
        return FAILED
    logging.warning("[watchdog-task] 排程已改走固定啟動器:%s", after)
    return MIGRATED
