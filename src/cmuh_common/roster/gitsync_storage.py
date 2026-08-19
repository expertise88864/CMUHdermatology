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

import contextlib
import logging
import os
import subprocess
import threading
import time

from cmuh_common.roster.storage import RosterStorage


def _is_month_filename(fn: str) -> bool:
    """月檔的正典檔名：YYYY-MM.json（與 RosterStorage.iter_month_yms 同一套判定）。

    刻意不接受任何其他 .json —— 人工解衝突時很容易在 months/ 底下留下
    「2026-08-rescue.json」「2026-08 - 複製.json」之類的副本，那些不該被同步出去。
    """
    if not fn.endswith(".json"):
        return False
    stem = fn[:-5]
    return (len(stem) == 7 and stem[4] == "-"
            and stem[:4].isdigit() and stem[5:].isdigit())


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
        # .gitignore 就緒與否 —— 未就緒即不 commit（P2-05 fail-closed）。
        # 非 git repo / 未設 remote 時不會走到 commit，維持 True 不影響純本機模式。
        self._gitignore_ok = True
        self._push_lock = threading.Lock()        # 只管 _push_timer 欄位
        self._git_lock = threading.RLock()        # 所有 git working-tree/refs 操作
        # ★工作樹【內容】的鎖★(外審排班第 1 輪 P1-01):存檔的
        #   「比對 revision → 寫入」必須與 merge/rebase 互斥,否則
        #   CAS 通過之後、寫入之前,背景 pull 還是能把檔案換掉。
        #   ★刻意不是 `_git_lock`★:那把鎖也涵蓋 fetch/push 這些網路
        #   動作(可能 30 秒),存檔不該被網路卡住 —— 那正是現行
        #   「拿不到鎖就先寫盤、延後 commit」設計要避免的事。
        #   鎖序固定:pull/push 先 `_git_lock` 再 `_tree_lock`;存檔只在
        #   寫入時持 `_tree_lock`,放掉之後才去拿 `_git_lock` commit。
        self._tree_lock = threading.RLock()
        # 臨界區內延後的 commit(每執行緒各自一份;見 `write_barrier`)
        self._local = threading.local()
        self._push_timer: "threading.Timer | None" = None
        self._stop_evt = threading.Event()
        self._pull_thread: "threading.Thread | None" = None
        if self._git_ok and self._remote_sync:
            # 先 pull：若 clone 的 repo 已含（他機提交過的）.gitignore，_ensure_gitignore
            # 會偵測到而跳過，避免本機留下未追蹤的 .gitignore 撞掉之後的 ff-only merge。
            self._pull()
            self._gitignore_ok = self._ensure_gitignore()
            # ★接手上一代沒推出去的 commit★(外審排班 P2-02):更新重啟時,
            #   舊 generation 只把變更 commit 到本機就把 mutex 交出來(它不能
            #   在新一代已經啟動的情況下還去碰同一個 .git —— index.lock/
            #   pull failed 都是這樣來的)。那些 commit 由這裡補推;沒有本機
            #   領先時 `_schedule_push` 推出去的是 no-op,成本可以忽略。
            self._schedule_push()
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

    # ★[2026-08-02 第二輪外審 P2-05] 只 stage 這些「正典資料檔」★
    #   原本 commit 用 `git add -A`，等於「working tree 裡沒被忽略的東西全都推出去」，
    #   而防線只有一個會寫失敗的 .gitignore。快照/壞檔備份/暫存/定案 PDF、以及
    #   使用者為了解衝突手工留下的救援副本，都會被推進 private repo。
    #   改成白名單之後，「哪些檔要跨機同步」是一份明確的清單，不是「除了忽略的以外
    #   全部」——新增資料檔要同步就必須來這裡加一行（漏加會被測試抓到）。
    _SYNC_FILES = ("config.json", "ledger.json", "biopsy.json",
                   "week_colors.json", "holiday_duty.json",
                   "clinic_template.json", "clerk_batches.json",
                   "biopsy_grid.json", ".gitignore")
    _SYNC_DIRS = ("months",)          # months/YYYY-MM.json（含刪除）

    def _is_canonical_path(self, rel: str) -> bool:
        """ls-files 吐回來的相對路徑是不是白名單認可的正典資料檔。"""
        rel = rel.replace("\\", "/")
        if rel in self._SYNC_FILES:
            return True
        head, _, tail = rel.partition("/")
        return head in self._SYNC_DIRS and "/" not in tail             and _is_month_filename(tail)

    def _ensure_gitignore(self) -> bool:
        """確保 .gitignore 含必要規則（保留使用者既有內容，只補缺的標準行）。

        回傳是否確實就緒。★寫不成功就不可以繼續同步★（見 _SYNC_FILES 的說明）：
        白名單已經是主要防線，但 .gitignore 仍是「使用者手工放進資料夾的東西不會
        被 git status 一直吵」的那一層，而且它本身就是要同步的檔之一。
        """
        p = os.path.join(self.base_dir, ".gitignore")
        try:
            existing = ""
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    existing = f.read()
            have = set(existing.splitlines())
            missing = [ln for ln in self._GITIGNORE_LINES if ln not in have]
            if missing:
                with open(p, "a" if existing else "w", encoding="utf-8") as f:
                    if existing and not existing.endswith("\n"):
                        f.write("\n")
                    f.write("\n".join(missing) + "\n")
        except OSError as e:
            logging.warning("[roster.gitsync] 寫入 .gitignore 失敗：%s", e)
            self._set_state("error", f".gitignore 無法建立/更新：{e}")
            return False
        self._ensure_local_exclude()
        return True

    def _gitignore_ready(self) -> bool:
        """.gitignore 是否就緒 —— 未就緒時【當場重試一次】。

        ★[外審第 11 輪] 不可只在 __init__ 判定一次★ 開程式當下若剛好被防毒/備份
        軟體鎖住幾秒,原本就會讓整個行程永久停止 commit 與 push —— 使用者得重開
        程式才會恢復,而畫面只說「.gitignore 未就緒」。暫時性失敗就該能自己好起來。
        """
        if self._gitignore_ok:
            return True
        self._gitignore_ok = self._ensure_gitignore()
        if self._gitignore_ok:
            logging.info("[roster.gitsync] .gitignore 已可寫入 → 恢復同步")
            self._set_state("ok")
        return self._gitignore_ok

    def _ensure_local_exclude(self) -> None:
        """第二層：.git/info/exclude（純本機、不會被同步，他機改不到也刪不掉）。

        .gitignore 是【會被同步的檔】——他機若把它改壞、或合併時弄丟，本機就沒有
        忽略規則了。exclude 補這一刀，寫不進去只記 debug（白名單才是主要防線）。
        """
        try:
            d = os.path.join(self.base_dir, ".git", "info")
            if not os.path.isdir(d):
                return
            p = os.path.join(d, "exclude")
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
        except OSError:
            logging.debug("[roster.gitsync] 寫入 .git/info/exclude 失敗（略過）",
                          exc_info=True)

    def _set_state(self, state: str, detail: str = "") -> None:
        """更新同步狀態並通知 callback（callback 在呼叫端 thread 執行）。

        ★[外審第 4 輪] `.gitignore` 未就緒時不得回報 ok★
        fail-closed 只擋住了 commit，但一次成功的【週期性 pull】會把狀態設回
        "ok" —— 底部狀態列顯示「已同步」，而本機的每一次存檔其實都被拒絕 commit，
        什麼都沒推出去。使用者看到綠燈、資料卻停在本機，正是最糟的那種失效。
        故把守衛放在這個唯一的狀態出口，而不是逐一修每個呼叫端。
        """
        if state == "ok" and not getattr(self, "_gitignore_ok", True):
            state, detail = "error", detail or ".gitignore 未就緒，本機變更不會同步"
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
            with self._tree_lock:               # 換工作樹內容 → 與存檔互斥
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
    @contextlib.contextmanager
    def write_barrier(self):
        """基底的臨界區 + 工作樹鎖;★commit 延到離開臨界區之後★。

        鎖序是固定的:pull/push 先 `_git_lock` 再 `_tree_lock`。若在持有
        `_tree_lock` 的期間去拿 `_git_lock`(commit 會做的事),就會與正在
        pull 的背景執行緒互相等待 —— 死鎖。所以臨界區內的存檔只寫盤並把
        檔名記下來,等鎖放掉之後再一次 commit + 排 push。
        """
        pending = getattr(self._local, "deferred", None)
        outermost = pending is None
        if outermost:
            self._local.deferred = []
        try:
            with self._tree_lock:
                with super().write_barrier():
                    yield
        finally:
            if outermost:
                names = self._local.deferred or []
                self._local.deferred = None
                if names and self._git_ok and self._remote_sync:
                    # 已經離開 `_tree_lock` → 這裡拿 `_git_lock` 不會反序。
                    if self._git_lock.acquire(timeout=3.0):
                        try:
                            if self._commit("、".join(sorted(set(names))[:5])):
                                self._schedule_push()
                        finally:
                            self._git_lock.release()
                    else:
                        logging.warning(
                            "[roster.gitsync] git 忙碌中，臨界區的變更延後 "
                            "commit（檔案已寫盤，下次存檔補收）")
                        self._schedule_push()

    def _save(self, path: str, data: dict, **kw) -> None:
        # **kw 透傳（目前是 backup=REQUIRE_BACKUP/BEST_EFFORT）——本層只加 git
        # commit/push，不干涉基底層的寫入政策；基底若拒寫會直接拋，這裡不會走到。
        # ★CAS 與寫入同在工作樹鎖內★:比對「盤上還是不是我讀到的那一份」
        #   之後,背景 merge 不可以插進來把檔案換掉再讓我覆寫。
        with self._tree_lock:
            super()._save(path, data, **kw)      # 原子寫入（write-through，不卡網路）
        if not (self._git_ok and self._remote_sync):
            return
        pending = getattr(self._local, "deferred", None)
        if pending is not None:
            # 在 `write_barrier` 的臨界區內:此刻拿 `_git_lock` 會與 pull 反序
            # → 死鎖。記下來,等臨界區結束再 commit。
            pending.append(os.path.basename(path))
            return
        # 拿不到 git 鎖＝背景正在 push/pull：檔案已寫盤，這次先略過 commit，
        # 下次存檔的白名單 add 會補收；仍排一次 push 以免變更留在本機。
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
        if not self._gitignore_ready():
            # fail-closed：沒有忽略規則就不 commit（見 _ensure_gitignore）。
            logging.warning("[roster.gitsync] .gitignore 未就緒 → 本次不 commit")
            return False
        try:
            # ★只收白名單★ `-A <pathspec>` 讓【刪除】也傳得出去（否則他機永遠
            #   留著已被刪掉的月份）；新檔要先 add，path-limited commit 才認得。
            paths = [n for n in self._SYNC_FILES
                     if os.path.exists(os.path.join(self.base_dir, n))]
            # ★逐檔列舉,不用目錄當 pathspec★ 空的 months/ 會讓 git 回
            #   "pathspec 'months' did not match any file(s) known to git"。
            # ★而且只收【正典檔名】YYYY-MM.json★（外審第 3 輪）：光看副檔名
            #   .json 會把 months/2026-08-rescue.json、「2026-08 - 複製.json」
            #   這類人工救援副本一起收進去 —— 白名單就又漏了。
            for dname in self._SYNC_DIRS:
                dpath = os.path.join(self.base_dir, dname)
                if not os.path.isdir(dpath):
                    continue
                for fn in sorted(os.listdir(dpath)):
                    if _is_month_filename(fn):
                        paths.append(f"{dname}/{fn}")
            # 已被追蹤但檔案已刪 → pathspec 仍要帶上，才能收到這筆刪除。
            # 同樣要過濾：ls-files 會把【已經被追蹤的】非正典檔也吐回來
            #（例如舊版 add -A 時期誤收進 repo 的救援副本），不濾就等於自動續命。
            ls = self._git("ls-files", "--", *self._SYNC_FILES, *self._SYNC_DIRS)
            if ls.returncode != 0:
                logging.warning("[roster.gitsync] ls-files 失敗，本次不 commit：%s",
                                (ls.stderr or ls.stdout).strip())
                return False
            for ln in (ls.stdout or "").splitlines():
                ln = ln.strip()
                if ln and ln not in paths and self._is_canonical_path(ln):
                    paths.append(ln)
            if not paths:
                return True                      # 沒有任何正典資料 → 無事可推
            a = self._git("add", "-A", "--", *paths)
            if a.returncode != 0:
                logging.warning("[roster.gitsync] add 失敗，本次不 commit：%s",
                                (a.stderr or a.stdout).strip())
                return False
            # ★[外審] commit 也要限定 pathspec★ 白名單只約束 add 是不夠的：
            #   `git commit` 提交的是【整個 index】，使用者自己 `git add` 過的
            #   救援副本、或先前中斷留在 index 的東西，照樣會被一起推出去。
            #   path-limited commit 只收這幾條路徑的工作區狀態，也不會動到
            #   使用者自己 stage 的內容（不搶、不丟）。
            r = self._git("commit", "-m",
                          f"roster sync: {label} "
                          f"{time.strftime('%Y-%m-%d %H:%M:%S')}",
                          "--", *paths)
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

    def _outgoing_non_canonical(self, remote: str, branch: str) -> list:
        """本次 push 會【新發佈出去】的路徑裡，有哪些不是白名單認可的正典資料檔。

        只看 `<remote>/<branch>..HEAD` 這個範圍：遠端既有歷史裡的髒東西不由本層
        自動處理（那需要人工 `git rm --cached`，見模組 docstring），但也不能因此
        把同步整個鎖死 —— 只擋「這一次會由我們推出去」的部分。
        遠端分支還不存在（首推）→ 拿 HEAD 的整棵樹比對。
        判定失敗（git 出錯）一律回空：這是縱深防禦，不該因為它自己壞掉而擋住同步。
        """
        try:
            # ★用 log 逐 commit 掃,不是 diff 兩端★(外審第 12 輪)
            #   淨差會漏掉「某個未推 commit 加了檔、後面另一個未推 commit 又刪掉」:
            #   `diff base..HEAD` 什麼都看不到,但 push 會把【兩個 commit 都】送出去,
            #   那個檔就永遠留在 git 歷史裡,任何拿得到 repo 的人都翻得出來。
            # ★--diff-filter=ACMR:排除【刪除】★(外審第 8 輪)
            #   否則使用者照我們自己給的指示做 `git rm --cached` 之後,那筆刪除
            #   又被判成一條非正典路徑 → 永遠推不出去,修復被自己擋死。
            r = self._git("log", "--format=", "--name-only",
                          "--diff-filter=ACMR", f"{remote}/{branch}..HEAD")
            if r.returncode != 0:
                # 遠端分支還不存在＝首推:整條歷史都會被發佈,就整條掃。
                r = self._git("log", "--format=", "--name-only",
                              "--diff-filter=ACMR", "HEAD")
                if r.returncode != 0:
                    return []
            return sorted({ln.strip() for ln in (r.stdout or "").splitlines()
                           if ln.strip() and not self._is_canonical_path(ln.strip())})
        except (OSError, subprocess.SubprocessError):
            logging.debug("[roster.gitsync] 待推路徑檢查失敗（略過）", exc_info=True)
            return []

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

    def _push(self, *, notify_remote_change: bool = True) -> None:
        """推前先同步（fetch + ff-only；分歧試 rebase）再 push。全程持 _git_lock。

        ★[2026-08-02 review] 推前同步若真的拉進他機的變更,必須通知 UI★
        RF-01 的修法只在【週期性 pull】那條路徑呼叫 on_remote_change,但推前同步
        (ff-only merge 或 rebase)同樣會改變盤上的資料。漏掉通知的後果不是「晚一點
        才更新」而是【永久看不到】:週期 pull 是比對 HEAD 前後有沒有變,而這裡已經
        合併過了 → 下一輪週期 pull 是 no-op → 那次變更的通知永遠不會發出。
        實際情境:B 機 14:00 存檔 → 3 秒後去抖 push → 推前同步把 A 機 13:50 的修改
        合併進來 → push 成功 → **B 的畫面仍是合併前的內容**,而且再也不會自己更新。
        使用者於是對著舊資料繼續改班 —— 正是 RF-01 要消除的那個失效模式,
        從 RF-01 自己新增的這條路徑漏出來。

        notify_remote_change:關閉前的 flush() 傳 False —— 那時 mainloop 即將結束,
        通知只會撞上 TclError,沒有意義。
        """
        changed = False
        try:
            changed = self._push_locked_body()
        finally:
            # ★[2026-08-02 補審] 通知必須無條件送出★
            #   合併成功但接著 push 失敗(離線)時,早退的 return 會跳過通知 ——
            #   而他機的資料【已經在盤上】,週期 pull 之後也是 no-op,
            #   於是永遠不通知。使用者繼續對著舊畫面編輯並覆蓋掉剛拉進來的變更。
            #   這與本次修的原始 finding 是同一個機制,只是深一層。
            # ★[2026-08-02 補審] 用回傳值,不可存在實例上★
            #   去抖 timer 與 flush() 可能同時跑(RF-06 就是這件事);
            #   _git_lock 在讀旗標之前就已釋放,第二個 push 會把旗標覆寫掉,
            #   第一個的通知就此消失 —— 又回到那個「永遠看不到」的缺陷。
            if (changed and notify_remote_change
                    and self._on_remote_change is not None):
                logging.info("[roster.gitsync] 推前同步拉進他機變更 → 通知 UI 重繪")
                try:
                    self._on_remote_change()
                except Exception:
                    logging.debug("[roster.gitsync] on_remote_change callback 失敗",
                                  exc_info=True)

    def _push_locked_body(self) -> bool:
        """_push 的實作本體(持鎖)。回傳「推前同步是否拉進了他機的變更」。

        抽出來有兩個理由:讓通知能放在 _push 的 finally 裡(不必在每一個 return 前
        重複一次),以及讓那個旗標是【每次呼叫各自的區域值】而不是實例狀態。"""
        changed = False
        if not self._gitignore_ready():
            # ★[外審第 6 輪] fail-closed 不能只擋 commit★
            #   `_push_locked_body` 原本無視 `_commit()` 的失敗照樣 push —— 而本機
            #   可能還躺著【舊版 `git add -A` 時期】收進來的未推 commit（裡面就有
            #   救援副本、快照、定案 PDF）。沒有忽略規則時連推都不該推。
            self._set_state("error", ".gitignore 未就緒，暫停同步（本機變更留在本機）")
            return False
        with self._git_lock:
            # 先補收：_save 因鎖逾時略過 commit 時只排了 push，這裡（鎖已空出）把那筆
            # 已寫盤但未 commit 的變更補 commit 進來，避免「存檔成功卻遲遲沒推、他機看到
            # 舊資料」直到下次存檔或關程式才補上。乾淨樹＝nothing-to-commit（no-op）。
            self._commit("背景補收")
            remote = self._remote_name()
            if not remote:
                logging.info("[roster.gitsync] 尚未設定 remote，略過 push")
                return changed
            branch = self._current_branch()
            if not branch:
                self._set_state("error", "detached HEAD，無法同步")
                return changed
            # 先確認遠端是否已有此分支：不存在＝首推（無需 fetch，直接 push 建立）；
            # ls-remote 失敗＝連不到遠端（離線）。
            ls = self._git("ls-remote", "--heads", remote, branch)
            if ls.returncode != 0:
                self._set_state("offline", (ls.stderr or ls.stdout).strip())
                return False
            if (ls.stdout or "").strip():
                # 在 fetch/merge/rebase 【之前】取 HEAD —— 要放在上面的
                # _commit("背景補收") 之後,否則本機自己的 commit 會被誤判成他機變更。
                before = self._rev_parse("HEAD")
                f = self._git("fetch", remote, branch)
                if f.returncode != 0:
                    self._set_state("offline", (f.stderr or f.stdout).strip())
                    return False
                with self._tree_lock:           # 換工作樹內容 → 與存檔互斥
                    m = self._git("merge", "--ff-only", "FETCH_HEAD")
                    if m.returncode != 0:
                        # 分歧：先試 rebase（兩台改不同檔可自動復原）
                        rb = self._git("pull", "--rebase", remote, branch)
                    else:
                        rb = None
                if m.returncode != 0:
                    if rb is not None and rb.returncode != 0:
                        with self._tree_lock:
                            self._git("rebase", "--abort")   # 同檔衝突 → 交人工
                        self._set_state("diverged", (rb.stderr or rb.stdout).strip())
                        return False
                after = self._rev_parse("HEAD")
                changed = bool(before and after and before != after)
            stray = self._outgoing_non_canonical(remote, branch)
            if stray:
                # ★[外審第 7 輪] 白名單只管【新的】commit★
                #   升級前若本機躺著舊版 `add -A` 造出、還沒推出去的 commit
                #   （最典型是人工解衝突留下的 months/2026-08-rescue.json），
                #   push 會把整條 HEAD 祖先一起發佈 —— 白名單等於沒擋到。
                #   只看【本次會新推出去的範圍】：遠端既有的歷史髒東西不由我們
                #   自動處理（那要人工 git rm --cached），但也不能因此把同步鎖死。
                self._set_state(
                    "error",
                    "本機有尚未推出的 commit 曾加入不該同步的檔案："
                    + "、".join(stray[:5])
                    + ("…" if len(stray) > 5 else "")
                    + "。（即使之後又刪掉，推出去仍會留在 git 歷史裡。）"
                    "請於 settings/roster 以 git log 確認後，用 git rm --cached "
                    "或整理未推的 commit，再重試。")
                return changed
            try:
                p = self._git("push", remote, "HEAD")
            except (OSError, subprocess.SubprocessError) as e:
                logging.warning(
                    "[roster.gitsync] push 執行失敗（略過，下次存檔再推）：%s", e)
                self._set_state("offline", str(e))
                return changed
            if p.returncode != 0:
                detail = (p.stderr or p.stdout).strip()
                logging.warning(
                    "[roster.gitsync] push 未成功（可能離線/遠端較新需先 pull）：%s",
                    detail)
                self._set_state("offline", detail)
                return changed
            self._set_state("ok")
            return changed
    def quiesce_local(self) -> None:
        """停掉背景同步並把本機變更 commit 完 —— ★完全不碰網路★。

        (外審排班 P2-02)更新重啟的交棒:舊 generation 若照舊先放 mutex 再
        `flush()`,新一代會在 1.5 秒後拿到 mutex 並開始 startup pull,而舊的
        還在 fetch/merge/push 同一個 `.git`(push 可能等 30 秒)—— 兩代同時碰
        working tree/index/HEAD,最典型的症狀是 index.lock 與 pull failed。
        所以交棒順序改成:★先 quiesce(本機 commit 一定要做完)→ 放 mutex →
        剩下的 push 交給新一代開機時補★。
        """
        if not self._git_ok:
            return
        self._stop_evt.set()                     # 收掉週期 pull 執行緒
        with self._push_lock:
            if self._push_timer is not None:
                self._push_timer.cancel()
                self._push_timer = None
        t = self._pull_thread
        if t is not None and t.is_alive():
            t.join(timeout=5.0)                  # 等它跑完手上那一輪再動 git
        with self._git_lock:
            self._commit("關閉前本機同步")

    def resume_sync(self) -> None:
        """把 `quiesce_local()` 停掉的背景同步收回來。

        (外審排班 RS-3 第 1 輪 P1)交棒是「先收斂本機,再確認接班」——
        ★接班沒成功時舊行程會繼續活著★(`restart_self` 確認新行程早夭就保留
        舊的),那時如果不把週期 pull 收回來,這台機器從此不再自動同步,而且
        使用者完全看不出來(只會覺得「別台的班表怎麼都沒進來」)。
        """
        if not (self._git_ok and self._remote_sync):
            return
        if not self._stop_evt.is_set():
            return                               # 沒有停過 → 不重複起執行緒
        self._stop_evt.clear()
        if self._pull_interval and self._pull_interval > 0:
            t = threading.Thread(target=self._pull_loop, daemon=True,
                                 name="roster-git-pull")
            self._pull_thread = t
            t.start()
        self._schedule_push()                    # 補推 quiesce 時已 commit 的

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
            self._push(notify_remote_change=False)   # mainloop 即將結束,通知無意義
