# -*- coding: utf-8 -*-
"""跨機同步儲存層（設計文件 §15 / §8）：把 settings/roster/ 放 private git repo，
多台電腦以 git 同步排班資料。

策略：
- 開檔時 `git pull --ff-only`（只快進；有衝突/落後 → 警告，不自動 merge，交人工）。
- 每次存檔：**本地 commit（同步、快，不卡 UI）** + **背景去抖 push**（把連續多筆
  存檔合併成一次 push，避免每個欄位各推一次、也不讓網路 push 凍住 UI）。
- 目錄非 git repo（使用者未設定 private repo）→ 自動退化為純 RosterStorage，
  所有存取照常、完全不碰 git。

前提：使用者已在該目錄 `git init` 或 clone private repo，且設好 remote 與認證
（SSH key / 認證管理員）。本層只呼叫 git，不管理認證。
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time

from cmuh_common.roster.storage import RosterStorage


class GitSyncStorage(RosterStorage):
    def __init__(self, base_dir: str, remote_sync: bool = True,
                 push_debounce_sec: float = 3.0):
        super().__init__(base_dir)
        self._remote_sync = remote_sync
        self._debounce = push_debounce_sec
        self._git_ok = self._is_git_repo()
        self._push_lock = threading.Lock()
        self._push_timer: "threading.Timer | None" = None
        if self._git_ok and self._remote_sync:
            self._pull()

    # ── git 基礎 ─────────────────────────────────────────────────────────
    def _is_git_repo(self) -> bool:
        return os.path.isdir(os.path.join(self.base_dir, ".git"))

    def _git(self, *args, timeout: float = 30.0) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", "-C", self.base_dir, *args],
            capture_output=True, text=True, timeout=timeout, check=False)

    def _pull(self) -> None:
        try:
            r = self._git("pull", "--ff-only")
        except (OSError, subprocess.SubprocessError) as e:
            logging.warning("[roster.gitsync] pull 執行失敗（略過）：%s", e)
            return
        if r.returncode != 0:
            logging.warning(
                "[roster.gitsync] pull 未成功（可能離線/有衝突需人工處理，不自動"
                "合併）：%s", (r.stderr or r.stdout).strip())

    # ── 存檔攔截：本地 commit + 去抖 push ────────────────────────────────
    def _save(self, path: str, data: dict) -> None:
        super()._save(path, data)                # 先照常原子寫入
        if not (self._git_ok and self._remote_sync):
            return
        if self._commit(path):                   # commit 真的成功（或乾淨無變更）才推
            self._schedule_push()

    def _commit(self, path: str) -> bool:
        """本地 commit。回傳是否可繼續推送（成功 or 乾淨無變更＝True；真失敗＝False）。"""
        try:
            self._git("add", "-A")
            r = self._git("commit", "-m",
                          f"roster sync: {os.path.basename(path)} "
                          f"{time.strftime('%Y-%m-%d %H:%M:%S')}")
        except (OSError, subprocess.SubprocessError) as e:
            logging.warning("[roster.gitsync] 本地 commit 執行失敗：%s", e)
            return False
        if r.returncode == 0:
            return True
        out = f"{r.stdout}\n{r.stderr}".lower()
        if "nothing to commit" in out or "no changes added" in out:
            return True                          # 乾淨樹 → 仍可推（補推之前失敗的）
        logging.warning(
            "[roster.gitsync] commit 失敗（此機可能未設 git user.name/email，"
            "本次變更未同步）：%s", (r.stderr or r.stdout).strip())
        return False

    def _remote_name(self) -> "str | None":
        try:
            r = self._git("remote")
        except (OSError, subprocess.SubprocessError):
            return None
        remotes = [x for x in (r.stdout or "").split() if x]
        return "origin" if "origin" in remotes else (remotes[0] if remotes else None)

    def _schedule_push(self) -> None:
        with self._push_lock:
            if self._push_timer is not None:
                self._push_timer.cancel()
            self._push_timer = threading.Timer(self._debounce, self._push)
            self._push_timer.daemon = True
            self._push_timer.start()

    def _push(self) -> None:
        remote = self._remote_name()
        if not remote:
            logging.info("[roster.gitsync] 尚未設定 remote，略過 push")
            return
        try:
            # 明確 push 到 remote 的同名分支（HEAD）→ 不依賴 upstream 設定，
            # `git init`+add remote 未 set-upstream 的情況也能推。
            r = self._git("push", remote, "HEAD")
        except (OSError, subprocess.SubprocessError) as e:
            logging.warning("[roster.gitsync] push 執行失敗（略過，下次存檔再推）：%s", e)
            return
        if r.returncode != 0:
            logging.warning(
                "[roster.gitsync] push 未成功（可能離線/遠端較新需先 pull）：%s",
                (r.stderr or r.stdout).strip())

    def flush(self) -> None:
        """立即推送（取消去抖、同步 push）；關閉程式前可呼叫確保不漏推。"""
        if not (self._git_ok and self._remote_sync):
            return
        with self._push_lock:
            if self._push_timer is not None:
                self._push_timer.cancel()
                self._push_timer = None
        self._push()
