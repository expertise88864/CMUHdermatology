# -*- coding: utf-8 -*-
"""跨機同步儲存層（設計文件 §15 / §8）：把 settings/roster/ 放 private git repo，
多台電腦以 git 同步排班資料。

策略：
- 開檔時 `git pull --ff-only`（只快進；有衝突/落後 → 警告，不自動 merge，交人工）。
- 每次存檔：**本地 commit（同步、快，不卡 UI）** + **背景去抖 push**（把連續多筆
  存檔合併成一次 push，避免每個欄位各推一次、也不讓網路 push 凍住 UI）。
- **推前先同步**（pull-before-push）：push 前先 fetch + ff-only merge；分歧時試
  `git pull --rebase`（兩台改不同檔可全自動復原），rebase 失敗（同檔衝突）→
  `rebase --abort` 並回報 diverged 狀態，絕不自動 merge JSON、交人工。
- **週期性 pull**：長駐時每 `pull_interval_sec` 秒背景 fetch + ff-only，抓另一台
  的變更；HEAD 有變即透過 `on_remote_change` 通知 UI 重繪。
- 同步狀態透過 `on_sync_state(state, detail)` 回報（state ∈ ok/offline/diverged/
  error）。**callback 會在背景 thread 執行**，UI 端需自行 marshal 回主執行緒
  （並吞掉 mainloop 結束後的 TclError）。
- 目錄非 git repo（使用者未設定 private repo）→ 自動退化為純 RosterStorage，
  所有存取照常、完全不碰 git。

git 併發：所有會動到 working tree / index / refs 的操作（commit、pull、push、
週期 pull）一律持 `self._git_lock`（RLock），避免背景 push 與 UI 存檔 commit、
或兩個 push 互撞 .git/index.lock 或把 rebase/merge 中間態 commit 出去。

前提：使用者已在該目錄 `git init` 或 clone private repo，且設好 remote 與認證
（SSH key / 認證管理員）。本層只呼叫 git，不管理認證。

註：本層會在首次啟動時建立 `.gitignore`（排除 *.bak-* / *.corrupt-* / *.tmp
快照與暫存檔）。若既有 repo 在此版本前已誤把 *.bak-* commit 進歷史，需人工
一次性 `git rm --cached -- '*.bak-*'` 清除，本層不自動處理。
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
                 push_debounce_sec: float = 3.0,
                 on_sync_state=None, on_remote_change=None,
                 pull_interval_sec: float = 300.0):
        super().__init__(base_dir)
        self._remote_sync = remote_sync
        self._debounce = push_debounce_sec
        self._on_sync_state = on_sync_state
        self._on_remote_change = on_remote_change
        self._pull_interval = pull_interval_sec
        self._git_ok = self._is_git_repo()
        # [RP3-02] 讓退化(未啟用 git 同步)路徑可察——否則診間電腦若非 repo,
        # 只會靜默改用純本機儲存,沒人知道跨機同步其實沒在運作。
        logging.info("[roster.gitsync] git 同步：%s（repo=%s、remote_sync=%s、%s）",
                     "啟用" if (self._git_ok and self._remote_sync) else "未啟用",
                     self._git_ok, self._remote_sync, self.base_dir)
        self.sync_state = "ok"
        self._push_lock = threading.Lock()        # 只管 _push_timer 欄位
        self._git_lock = threading.RLock()        # 所有 git working-tree/refs 操作
        self._push_timer: "threading.Timer | None" = None
        self._stop_evt = threading.Event()
        self._pull_thread: "threading.Thread | None" = None
        if self._git_ok and self._remote_sync:
            # 先 pull：若 clone 的 repo 已含（他機提交過的）.gitignore，_ensure_gitignore
            # 會偵測到而跳過，避免本機留下未追蹤的 .gitignore 撞掉之後的 ff-only merge。
            self._pull()
            self._ensure_gitignore()
            if self._pull_interval and self._pull_interval > 0:
                self._pull_thread = threading.Thread(
                    target=self._pull_loop, name="roster-git-pull", daemon=True)
                self._pull_thread.start()

    # ── git 基礎 ─────────────────────────────────────────────────────────
    def _is_git_repo(self) -> bool:
        # [RP3-02] worktree/submodule 的 .git 是「檔案」(gitdir 指標)不是目錄,
        # 用 exists 才不會把它們誤判成非 repo 而靜默停用同步。
        return os.path.exists(os.path.join(self.base_dir, ".git"))

    def _git(self, *args, timeout: float = 30.0) -> subprocess.CompletedProcess:
        # encoding='utf-8'（不用 text=True 的 locale 預設）：cp950/big5 中文 Windows
        # （診間電腦）上，git 輸出的 UTF-8 中文（commit 訊息/分支/路徑）才不會
        # UnicodeDecodeError 炸掉背景 push 執行緒。errors='replace' 再兜底。
        # LC_ALL=C 讓 git 訊息維持英文（'nothing to commit' 判斷穩定）；
        # GIT_TERMINAL_PROMPT=0 讓無 console 時 git 不卡等認證輸入直接失敗走離線。
        # creationflags=CREATE_NO_WINDOW：pythonw(.pyw)無 console 環境下不閃黑窗
        # （getattr 在非 Windows 回 0，POSIX 的 creationflags=0 為合法預設）。
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
        return subprocess.run(
            ["git", "-C", self.base_dir, *args],
            capture_output=True, encoding="utf-8", errors="replace",
            timeout=timeout, check=False, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    # 需被 git 忽略的檔類：
    # *.bak-*：storage 月檔快照；*.corrupt-*：壞檔備份（防禦性）；
    # *.tmp：atomic_io 暫存檔（*.{name}.XXXX.tmp 亦被 * 涵蓋）；
    # finalized/：定案 PDF 留底（二進位、可由已同步的月檔重生 → 純本機不進 git，
    #   避免 repo 二進位膨脹，也免「PDF 直寫後遲遲沒 commit/推」的同步時序問題）。
    _GITIGNORE_LINES = ("*.bak-*", "*.corrupt-*", "*.tmp", "finalized/")

    def _ensure_gitignore(self) -> None:
        """確保 .gitignore 含必要規則（保留使用者既有內容，只補缺的標準行）。"""
        p = os.path.join(self.base_dir, ".gitignore")
        try:
            existing = ""
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    existing = f.read()
            have = set(existing.splitlines())
            missing = [ln for ln in self._GITIGNORE_LINES if ln not in have]
            if not missing:
                return
            with open(p, "a" if existing else "w", encoding="utf-8") as f:
                if existing and not existing.endswith("\n"):
                    f.write("\n")
                f.write("\n".join(missing) + "\n")
        except OSError as e:
            logging.warning("[roster.gitsync] 寫入 .gitignore 失敗（略過）：%s", e)

    def _set_state(self, state: str, detail: str = "") -> None:
        """更新同步狀態並通知 callback（callback 在呼叫端 thread 執行）。"""
        self.sync_state = state
        if state == "ok":
            logging.info("[roster.gitsync] 同步狀態：ok")
        else:
            logging.warning("[roster.gitsync] 同步狀態：%s（%s）", state, detail)
        cb = self._on_sync_state
        if cb is not None:
            try:
                cb(state, detail)
            except Exception:
                logging.debug("[roster.gitsync] on_sync_state callback 失敗",
                              exc_info=True)

    def _current_branch(self) -> "str | None":
        try:
            r = self._git("rev-parse", "--abbrev-ref", "HEAD")
        except (OSError, subprocess.SubprocessError):
            return None
        b = (r.stdout or "").strip()
        if r.returncode != 0 or not b or b == "HEAD":   # detached / 解析失敗
            return None
        return b

    def _rev_parse(self, ref: str) -> "str | None":
        try:
            r = self._git("rev-parse", ref)
        except (OSError, subprocess.SubprocessError):
            return None
        return (r.stdout or "").strip() if r.returncode == 0 else None

    def _pull(self) -> None:
        with self._git_lock:
            try:
                # [RP3-13] 限時 8s——啟動時 _pull 阻塞 UI,遠端不通/慢時原本可卡到
                # git 內建逾時(最長 ~30s);超時就以本機資料開檔,別讓開程式空等。
                r = self._git("pull", "--ff-only", timeout=8.0)
            except subprocess.TimeoutExpired as e:
                logging.warning("[roster.gitsync] pull 逾時（>8s），以本機資料開啟：%s", e)
                self._set_state("offline", "pull 逾時，以本機資料開啟")
                return
            except (OSError, subprocess.SubprocessError) as e:
                logging.warning("[roster.gitsync] pull 執行失敗（略過）：%s", e)
                self._set_state("offline", str(e))
                return
            if r.returncode != 0:
                detail = (r.stderr or r.stdout).strip()
                logging.warning(
                    "[roster.gitsync] pull 未成功（可能離線/有衝突需人工處理，不自動"
                    "合併）：%s", detail)
                # 開檔 pull 失敗：保守回報 offline（真正的分歧由 push 路徑偵測並升級
                # 為 diverged，避免啟動即彈嚇人的衝突視窗）。
                self._set_state("offline", detail)
            else:
                self._set_state("ok")

    # ── 週期性 pull（抓另一台的變更）────────────────────────────────────
    def _pull_loop(self) -> None:
        while not self._stop_evt.wait(self._pull_interval):
            try:
                self._periodic_pull()
            except Exception:
                logging.debug("[roster.gitsync] 週期 pull 失敗", exc_info=True)

    def _periodic_pull(self) -> None:
        """背景 fetch + ff-only；HEAD 有變 → 通知 on_remote_change。"""
        changed = False
        with self._git_lock:
            remote = self._remote_name()
            branch = self._current_branch()
            if not remote or not branch:
                return
            before = self._rev_parse("HEAD")
            f = self._git("fetch", remote, branch)
            if f.returncode != 0:
                self._set_state("offline", (f.stderr or f.stdout).strip())
                return
            m = self._git("merge", "--ff-only", "FETCH_HEAD")
            if m.returncode == 0:
                self._set_state("ok")
                after = self._rev_parse("HEAD")
                changed = bool(before and after and before != after)
            # ff-only 失敗＝本機領先或分歧 → 留給 push 路徑處理，不在此升級狀態
        if changed and self._on_remote_change is not None:
            try:
                self._on_remote_change()
            except Exception:
                logging.debug("[roster.gitsync] on_remote_change callback 失敗",
                              exc_info=True)

    # ── 存檔攔截：本地 commit + 去抖 push ────────────────────────────────
    def _save(self, path: str, data: dict) -> None:
        super()._save(path, data)                # 先照常原子寫入（write-through，不卡）
        if not (self._git_ok and self._remote_sync):
            return
        # 拿不到 git 鎖＝背景正在 push/pull：檔案已寫盤，這次先略過 commit，
        # 下次存檔的 add -A 會補收；仍排一次 push 以免變更留在本機。
        if not self._git_lock.acquire(timeout=3.0):
            logging.warning(
                "[roster.gitsync] git 忙碌中，本次存檔延後 commit（檔案已寫盤，"
                "下次存檔補收）：%s", os.path.basename(path))
            self._schedule_push()
            return
        try:
            if self._commit(os.path.basename(path)):
                self._schedule_push()
        finally:
            self._git_lock.release()

    def _commit(self, label: str) -> bool:
        """本地 commit（呼叫端須持 _git_lock）。

        回傳是否可繼續推送（成功 or 乾淨無變更＝True；真失敗＝False）。
        """
        try:
            self._git("add", "-A")
            r = self._git("commit", "-m",
                          f"roster sync: {label} "
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
        """推前先同步（fetch + ff-only；分歧試 rebase）再 push。全程持 _git_lock。"""
        with self._git_lock:
            # 先補收：_save 因鎖逾時略過 commit 時只排了 push，這裡（鎖已空出）把那筆
            # 已寫盤但未 commit 的變更補 commit 進來，避免「存檔成功卻遲遲沒推、他機看到
            # 舊資料」直到下次存檔或關程式才補上。乾淨樹＝nothing-to-commit（no-op）。
            self._commit("背景補收")
            remote = self._remote_name()
            if not remote:
                logging.info("[roster.gitsync] 尚未設定 remote，略過 push")
                return
            branch = self._current_branch()
            if not branch:
                self._set_state("error", "detached HEAD，無法同步")
                return
            # 先確認遠端是否已有此分支：不存在＝首推（無需 fetch，直接 push 建立）；
            # ls-remote 失敗＝連不到遠端（離線）。
            ls = self._git("ls-remote", "--heads", remote, branch)
            if ls.returncode != 0:
                self._set_state("offline", (ls.stderr or ls.stdout).strip())
                return
            if (ls.stdout or "").strip():
                f = self._git("fetch", remote, branch)
                if f.returncode != 0:
                    self._set_state("offline", (f.stderr or f.stdout).strip())
                    return
                m = self._git("merge", "--ff-only", "FETCH_HEAD")
                if m.returncode != 0:
                    # 分歧：先試 rebase（兩台改不同檔可自動復原）
                    rb = self._git("pull", "--rebase", remote, branch)
                    if rb.returncode != 0:
                        self._git("rebase", "--abort")   # 同檔衝突 → 交人工
                        self._set_state("diverged", (rb.stderr or rb.stdout).strip())
                        return
            try:
                p = self._git("push", remote, "HEAD")
            except (OSError, subprocess.SubprocessError) as e:
                logging.warning(
                    "[roster.gitsync] push 執行失敗（略過，下次存檔再推）：%s", e)
                self._set_state("offline", str(e))
                return
            if p.returncode != 0:
                detail = (p.stderr or p.stdout).strip()
                logging.warning(
                    "[roster.gitsync] push 未成功（可能離線/遠端較新需先 pull）：%s",
                    detail)
                self._set_state("offline", detail)
                return
            self._set_state("ok")

    def flush(self) -> None:
        """立即推送（取消去抖、同步 push）；關閉程式前呼叫確保不漏推。

        先做一次 catch-up commit（補收 _save 因鎖逾時略過的變更），再 push。
        """
        if not (self._git_ok and self._remote_sync):
            return
        self._stop_evt.set()                     # 收掉週期 pull 執行緒
        with self._push_lock:
            if self._push_timer is not None:
                self._push_timer.cancel()
                self._push_timer = None
        with self._git_lock:
            self._commit("關閉前同步")           # 補收未 commit 的變更
            self._push()
