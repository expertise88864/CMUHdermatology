# -*- coding: utf-8 -*-
"""路徑與重啟工具。同時支援 .pyw（Python 直跑）與 .exe（PyInstaller 打包）兩種模式。

關鍵概念：
- get_app_dir()：回傳「使用者看得到的程式目錄」（即 settings/、assets/、log 的父層）。
- restart_self()：雙軌重啟邏輯。

【修正 2026.05.04】get_app_dir 智能化偵測，避免 settings/ 分裂：
  原本若使用 `pythonw src/main.py` 啟動，sys.argv[0] = src/main.py，
  app_dir 會回 src/，settings/ 跑去 src/settings/，與雙擊 root launcher 的
  app_dir = repo root 不一致，造成 settings/ 分裂。

  本版改為：若 sys.argv[0] 落在「含有 cmuh_common 的目錄」內，
  自動往上一層（取 src/ 的父層即 repo root），保證 settings/ 永遠在 repo root。
"""
import os
import sys


#: ★[批次L L1] 由六支 `.pyw` 啟動器釘住的固定值★（見 `get_app_dir` / `restart_self`）
#:   啟動器是固定路徑、切版本救不回來的檔;它們知道真正的 app 根目錄與自己的路徑。
APP_DIR_ENV = "CMUH_APP_DIR"
LAUNCHER_ENV = "CMUH_LAUNCHER"


def pinned_app_dir() -> str:
    """啟動器釘住的固定 app 根目錄;沒釘住／釘到不存在的目錄 → 回空字串。

    ★單一讀取點★ `get_app_dir()`、watchdog 兩支都走這裡 —— 驗證邏輯只寫一次,
    不會有「某一處忘了驗存在」那種漂移。
    """
    import os as _os
    v = _os.environ.get(APP_DIR_ENV, "").strip()
    if not v:
        return ""
    v = _os.path.abspath(v)
    return v if _os.path.isdir(v) else ""


def is_frozen() -> bool:
    """是否在 PyInstaller 打包後的 .exe 模式下執行。"""
    return getattr(sys, 'frozen', False)


def _looks_like_src_dir(d: str) -> bool:
    """判斷目錄 d 是否為 src/（即包含 cmuh_common/ 子套件的目錄）。"""
    try:
        return os.path.isdir(os.path.join(d, 'cmuh_common')) and \
               os.path.isfile(os.path.join(d, 'cmuh_common', 'version.py'))
    except OSError:
        return False


def get_app_dir() -> str:
    """回傳程式所在目錄（settings/、assets/、log 的父層）。

    - .exe 模式：sys.executable 所在目錄
    - .pyw 模式：
        * 若 sys.argv[0] 在 src/ 內（直接跑 src/main.py 等）→ 回 src/ 的父層
        * 否則（雙擊 root launcher）→ 回 launcher 所在目錄
    """
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))

    # ★[批次L L1 外審 P1] 固定的 app 根目錄由【啟動器】釘住★
    #   `runpy.run_path` 會把 `sys.argv[0]` 換成被執行的那支檔（CPython 的
    #   `_ModifiedArgv0`）。版本化之後那是 `<app>/versions/<V>/src/main.py`,
    #   於是下面「src 的父層」推出來的是 `<app>/versions/<V>` —— settings、
    #   log、assets 全部會跑到版本目錄底下，而設計明訂 settings 不隨版本走。
    #   後果:切一次版就等於「所有設定都不見了」（帳密、門檻、已寄紀錄）。
    #   六支 `.pyw` 是固定路徑，它們知道真正的根目錄 → 由它們釘進環境變數;
    #   子行程繼承得到（watchdog 啟動的那些也算）。
    #   ★只信「真的存在的目錄」★ 讀不到就往下走既有的推導，不可以因為一個
    #   壞掉的環境變數讓整支程式找不到設定。
    pinned = pinned_app_dir()
    if pinned:
        return pinned
    main_script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else __file__
    script_dir = os.path.dirname(main_script)

    # 智能偵測：若 script_dir 看起來是 src/，回上一層 repo root
    if _looks_like_src_dir(script_dir):
        parent = os.path.dirname(script_dir)
        if parent and parent != script_dir:
            return parent
    return script_dir


def get_settings_dir() -> str:
    """設定/快取目錄（自動建立）。對應原主程式 SETTINGS_DIR。"""
    d = os.path.join(get_app_dir(), 'settings')
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def get_conf_path(filename: str) -> str:
    """回傳設定檔完整路徑。"""
    return os.path.join(get_settings_dir(), filename)


def get_assets_dir() -> str:
    """靜態資源目錄。.exe 模式優先回 _MEIPASS/assets，否則回 app_dir/assets。"""
    if is_frozen() and hasattr(sys, '_MEIPASS'):
        bundled = os.path.join(sys._MEIPASS, 'assets')  # type: ignore[attr-defined]
        if os.path.isdir(bundled):
            return bundled
    return os.path.join(get_app_dir(), 'assets')


def get_bundled_asset(relative_path: str) -> str:
    """取得內嵌靜態資源（圖示、音效等）。"""
    return os.path.join(get_assets_dir(), relative_path)


def get_log_path(filename: str = 'app.log') -> str:
    """log 檔路徑，預設放在 app_dir 直接層。"""
    return os.path.join(get_app_dir(), filename)


# spawn 後確認新行程沒有「起來就馬上死」的輪詢窗 = POLLS × INTERVAL ≈ 0.6 秒。
# [2026-07-26] 提成常數是因為呼叫端(main._restart_app)把【釋放 mutex】延後到 on_confirmed:
# 新行程的 ensure_single_instance 對 ALREADY_EXISTS 只重試 retry_sec(預設 1.5 秒),
# 本輪詢窗必須明顯小於它,新行程才一定還在重試窗內就等到 mutex。有測試釘住這個關係。
_SPAWN_ALIVE_POLLS = 6
_SPAWN_ALIVE_INTERVAL_SEC = 0.1


# restart_self 的回傳值(成功時本行程直接退出,故只有失敗路徑會回傳)。
# 呼叫端據此決定要不要對使用者示警 —— 「照設計自行結束」不該被說成「無法啟動」。
SPAWN_FAILED = "spawn_failed"                    # Popen 本身失敗
SPAWN_CHILD_CRASHED = "child_crashed"            # 子行程早夭且非正常結束
SPAWN_CHILD_EXITED_ORDERLY = "child_exited_orderly"   # exit=0 且無 stderr
_NO_STDERR_MARKERS = ("(子行程沒有留下任何 stderr)", "(未能建立 stderr 暫存檔)",
                      "(讀不到 stderr 暫存檔)", "")
# 崩潰的痕跡。★不可用「stderr 是空的」當判準★:autoclock 的 _setup_clock_logging()
# 會把 StreamHandler 接到 stderr,而「=== autoclock vX 啟動 ===」在設定閘門【之前】
# 就寫出去了 —— 於是 tail 永遠不是空的,orderly 永遠不成立,整個判定形同虛設。
# (這正是本判定第一版的錯誤;外審抓到。)改為看有沒有 Python 例外的痕跡。
_CRASH_MARKERS = ("Traceback (most recent call last)", "SyntaxError",
                  "ImportError", "ModuleNotFoundError", "Fatal Python error")


# ─── [第九輪 §4] restart 兩階段 READY 交握 ────────────────────────────────────
# 子行程要「完整就緒」必須先拿到單例 mutex,mutex 在父行程手上 —— 父行程若等子行程 READY
# 才放 mutex 就死鎖。今天的作法(0.6 秒還活著就放 mutex、子行程重試 1.5 秒)避開了死鎖,
# 代價是「活著 ≠ 就緒」:子行程 0.8 秒後死在 config/UI 初始化,父行程已拆光 → 零個可用
# instance。改成兩階段:
#   PRE-READY(子行程 import/設定都過、★即將★搶 mutex)→ 父行程拔熱鍵、放 mutex;
#   READY(子行程拿到 mutex + 核心初始化完成)→ 父行程做慢的拆解並退出;
#   子行程在兩者之間死掉 → 父行程★復原★(重取 mutex、重掛熱鍵),不再零 instance。
# ★降版也要能重啟★:舊版子行程永遠不寫交握檔 → PRE-READY 的觸發是 min(檔案出現, 0.6s 還
# 活著);之後從未看到檔案且活著 → 寬限 3s 後視為舊版子行程、照今天的行為確認退出。
# 傳輸:父行程 Popen(env=CMUH_RESTART_HANDSHAKE=<tmp 檔>);子行程原子寫入階段字串;父行程
# 每 0.1s 讀。單一寫者,不需要鎖;冷啟動沒有 env → signal 是 no-op。
RESTART_HANDSHAKE_ENV = "CMUH_RESTART_HANDSHAKE"
HANDSHAKE_WAITING_MUTEX = "waiting_mutex"
HANDSHAKE_READY = "ready"
HANDSHAKE_LEGACY_GRACE_SEC = 3.0      # 從未看到交握檔 → 視為舊版子行程的寬限
HANDSHAKE_READY_TIMEOUT_SEC = 30.0    # 看過交握檔但遲遲不 READY 的上限(它已持有 mutex,是存活者)
HANDSHAKE_MUTEX_RETRY_SEC = 10.0      # 交握存在時子行程搶 mutex 的重試窗(父行程是反應式放 mutex)
HANDOVER_CONFIRMED = "handover_confirmed"
SPAWN_CHILD_DIED_AFTER_HANDOVER = "child_died_after_handover"   # 放了 mutex 之後才死;已復原
SPAWN_RECOVERY_FAILED = "recovery_failed"    # 交棒後早夭且★復原失敗★(拿不回 mutex);呼叫端已安全退場

# [外審 r1 P1-1] ★交握路徑只屬於直接子行程★:latch 進模組變數後立刻從 os.environ 拿掉,
# 免得子行程再起的孫行程(例如主程式的 inner watchdog 拉起打卡)繼承同一個路徑、冒充 READY。
# 訊號也綁 PID(`<state> <pid>`),父行程只認直接子行程的 PID —— 兩層防護,前者減少洩漏,
# 後者是硬保證。
_HANDSHAKE_PATH: list = [None]      # [path or ""]:None = 還沒 latch


def _latch_handshake_path() -> str:
    import os as _os
    if _HANDSHAKE_PATH[0] is None:
        _HANDSHAKE_PATH[0] = _os.environ.pop(RESTART_HANDSHAKE_ENV, "").strip()
    return _HANDSHAKE_PATH[0]


def restart_handshake_active() -> bool:
    """本行程是不是由 `restart_self` 帶交握起來的子行程(啟動時 env 有交握檔路徑)。"""
    return bool(_latch_handshake_path())


def mutex_retry_sec() -> float:
    """子行程搶單例 mutex 的重試窗:交握存在時放大(父行程看到 PRE-READY 才放 mutex,
    不再靠常數對齊);冷啟動維持 1.5s(雙開情境最多多等 1.5s 才提示)。"""
    return HANDSHAKE_MUTEX_RETRY_SEC if restart_handshake_active() else 1.5


def restart_handshake_signal(state: str) -> bool:
    """子行程向父行程回報階段(HANDSHAKE_WAITING_MUTEX / HANDSHAKE_READY)。
    沒有交握(冷啟動)→ no-op 回 False。原子寫入(tmp + replace),父行程不會讀到半截。
    內容是 `<state> <pid>`:父行程只認直接子行程的 PID(孫行程冒充不了)。"""
    import os as _os
    path = _latch_handshake_path()
    if not path:
        return False
    try:
        tmp = f"{path}.{_os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(f"{state} {_os.getpid()}")
        _os.replace(tmp, path)
        return True
    except OSError:
        import logging as _logging
        _logging.debug("[restart_handshake] 寫入 %s 失敗", state, exc_info=True)
        return False


def read_handshake(path: str, expect_pid=None):
    """父行程讀交握檔;不存在/讀不到/內容不認得/★PID 不是直接子行程★ → None。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            s = f.read().strip()
    except OSError:
        return None
    parts = s.split()
    if not parts or parts[0] not in (HANDSHAKE_WAITING_MUTEX, HANDSHAKE_READY):
        return None
    if expect_pid is not None:
        if len(parts) < 2 or not parts[1].isdigit() or int(parts[1]) != int(expect_pid):
            return None
    return parts[0]


def wait_for_handover(proc, handshake_path: str, *, on_preready=None, on_confirmed=None,
                      on_recover=None, stderr_tail=lambda: "", now=None, sleep=None,
                      poll_interval: float = _SPAWN_ALIVE_INTERVAL_SEC) -> str:
    """父行程的交棒判定(純函式,可測)。回:
      HANDOVER_CONFIRMED              → 呼叫端退出本行程
      SPAWN_CHILD_CRASHED / SPAWN_CHILD_EXITED_ORDERLY → 早夭(PRE-READY 之前),本行程原封不動
      SPAWN_CHILD_DIED_AFTER_HANDOVER → 放了 mutex 之後才死;on_recover 已呼叫,本行程繼續
    只傳 on_confirmed(舊呼叫端)→ 在 PRE-READY 時刻呼叫一次 on_confirmed 並立即確認,
    之後不再呼叫(與今天的時序相同)。"""
    import logging as _logging
    import time as _time
    now = now or _time.monotonic
    sleep = sleep or _time.sleep
    legacy_caller = on_preready is None
    t0 = now()
    alive_grace = _SPAWN_ALIVE_POLLS * _SPAWN_ALIVE_INTERVAL_SEC      # 0.6s(今天的存活確認)
    preready_done = False
    saw_file = False
    expect_pid = getattr(proc, "pid", None)          # ★只認直接子行程的 PID★
    while True:
        sleep(poll_interval)
        rc = proc.poll()
        state = read_handshake(handshake_path, expect_pid)
        if state is not None:
            saw_file = True
        elapsed = now() - t0
        if rc is not None:
            if not preready_done:
                return classify_child_exit(rc, stderr_tail())
            _logging.error(
                "[restart_self] 新行程在交棒後才結束 (exit=%s) → 本行程嘗試復原(重取 mutex、"
                "重掛熱鍵)\n--- 新行程 stderr ---\n%s\n--- stderr 結束 ---",
                rc, stderr_tail())
            # [外審 r1 P1-2] 復原是否成立要★明講★:on_recover 回 True 才算拿回單例所有權;
            # 回 False / 拋例外 = 拿不回(別人搶到了 / mutex API 壞了)→ 呼叫端的 callback
            # 已自行安全退場(不可以繼續當一個沒守衛的 instance),這裡回 RECOVERY_FAILED。
            recovered = False
            if on_recover is not None:
                try:
                    recovered = bool(on_recover())
                except Exception:
                    _logging.exception("[restart_self] on_recover 復原失敗")
            return SPAWN_CHILD_DIED_AFTER_HANDOVER if recovered else SPAWN_RECOVERY_FAILED
        if not preready_done and (state is not None or elapsed >= alive_grace):
            preready_done = True
            cb = on_confirmed if legacy_caller else on_preready
            if cb is not None:
                try:
                    cb()
                except Exception:
                    _logging.exception("[restart_self] on_preready 收尾失敗（仍照常繼續）")
            if legacy_caller:
                return HANDOVER_CONFIRMED          # 舊呼叫端:與今天相同,確認即退出
        if preready_done:
            confirm = False
            if state == HANDSHAKE_READY:
                confirm = True
            elif not saw_file and elapsed >= alive_grace + HANDSHAKE_LEGACY_GRACE_SEC:
                _logging.info("[restart_self] 新行程沒有交握(舊版?)但存活 %.1fs → 照舊確認接手",
                              elapsed)
                confirm = True
            elif saw_file and elapsed >= HANDSHAKE_READY_TIMEOUT_SEC:
                _logging.warning("[restart_self] 新行程 %.0fs 仍未 READY 但存活且已持有 mutex"
                                 " → 視為接手成功", elapsed)
                confirm = True
            if confirm:
                if on_confirmed is not None:
                    try:
                        on_confirmed()
                    except Exception:
                        _logging.exception("[restart_self] on_confirmed 收尾失敗（仍照常退出）")
                return HANDOVER_CONFIRMED


def classify_child_exit(rc, stderr_tail: str) -> str:
    """把「子行程早夭」分類成 orderly / crashed。純函式,好測。

    orderly = 結束碼 0 且看不到 Python 例外的痕跡 → 新行程是【自己決定】結束的
    (例如本機沒有該程式的設定檔、單例已被別人持有)。那不是「新版本無法啟動」。
    """
    try:
        code = int(rc)
    except (TypeError, ValueError):
        return SPAWN_CHILD_CRASHED
    text = stderr_tail or ""
    if any(marker in text for marker in _CRASH_MARKERS):
        return SPAWN_CHILD_CRASHED
    return SPAWN_CHILD_EXITED_ORDERLY if code == 0 else SPAWN_CHILD_CRASHED

RESTART_ERR_GLOB = "cmuh_restart_*.err"
RESTART_ERR_KEEP_SEC = 86400        # 保留一天,足夠事後查一次早夭原因


def sweep_old_restart_err_files(tmpdir: str,
                                keep_sec: int = RESTART_ERR_KEEP_SEC,
                                now: float | None = None) -> int:
    """清掉上次重啟留下的子行程 stderr 暫存檔。回傳刪除數。絕不拋。

    ★[2026-08-02 補審] 為什麼清理只能在「下次 spawn」做★
    成功重啟時,子行程會【持有那個 handle 直到它自己結束】,父行程刪不掉
    (Windows 不允許刪除他人開啟中的檔)。不在下次 spawn 清掃的話,每一次成功
    重啟都會在 %TEMP% 永久留下一個檔 —— 更新/閒置重啟每天都會發生。

    只掃自己的命名樣式、只刪超過 keep_sec 的,刪不掉(還被開著/沒權限)就略過,
    絕不因為清理失敗而影響重啟本身。
    """
    import glob
    import time as _t
    removed = 0
    cutoff = (now if now is not None else _t.time()) - keep_sec
    try:
        candidates = glob.glob(os.path.join(tmpdir, RESTART_ERR_GLOB))
    except Exception:
        return 0
    for old in candidates:
        try:
            if os.path.getmtime(old) < cutoff:
                os.remove(old)
                removed += 1
        except OSError:
            continue
    return removed


#: ★六支 launcher 的封閉集合★(外審 2026-08-09 P2-02)
#   `CMUH_LAUNCHER` 是【會被子行程繼承】的環境變數。只驗「檔案存在 + 在 app
#   根目錄第一層」的話,app 根目錄裡【任何一個檔】都能通過 —— 包含更新器
#   剛下載的檔、被放進來的 `.pyw`、甚至 `manifest.json`。而 `self_entry_path()`
#   的結果會被 `build_restart_command()` 拿去執行,UAC 那條路還會把它提權。
#   位置對不代表身分對:再加一層名字的白名單。
LAUNCHER_NAMES = (
    "中國醫皮膚科主程式.pyw",
    "中國醫皮膚科守護程式.pyw",
    "中國醫皮膚科打卡程式.pyw",
    "中國醫皮膚科排班程式.pyw",
    "中國醫皮膚科會診查詢程式.pyw",
    "中國醫皮膚科點座標偵測程式.pyw",
)


def pinned_launcher() -> str:
    """啟動器釘住的固定 `.pyw`;沒釘住／檔案不在／不在白名單 → 空字串。

    ★[外審 P2-02] 不可以只驗『檔案存在』★
    環境變數是會被繼承的。只驗存在的話,一個指向別處的值就能讓我們去重啟
    另一支程式。所以要求它【就在釘住的 app 根目錄底下、而且是第一層】——
    那正是六支 launcher 真正的位置 —— 而且【檔名要在封閉集合裡】。
    """
    import os as _os
    v = _os.environ.get(LAUNCHER_ENV, "").strip()
    if not v:
        return ""
    try:
        v = _os.path.realpath(v)
        if not _os.path.isfile(v):
            return ""
        # ★[外審 P2] `if root and ...` 會讓守衛 no-op★
        #   沒有(或無效的)`CMUH_APP_DIR` 時,整個 containment 檢查被跳過,
        #   於是【任何存在的檔】都被接受 —— 繼承來的陳舊值就能讓我們
        #   去重啟別的程式,而 UAC 那條路還會把它提權。
        #   兩個值是【一組】的:沒有可信的根,就沒有可信的 launcher。
        root = pinned_app_dir()
        if not root:
            return ""
        if _os.path.dirname(v) != _os.path.realpath(root):
            return ""
        # ★位置對 ≠ 身分對★(外審 2026-08-09 P2-02)
        #   app 根目錄裡不是只有那六支;更新器會在那裡放檔案。
        if _os.path.basename(v) not in LAUNCHER_NAMES:
            return ""
    except OSError:
        return ""
    return v


def self_entry_path() -> str:
    """「要再開一次自己」時應該執行哪一支檔。★單一真相來源★

    `runpy.run_path` 會把 `sys.argv[0]` 換成被執行的那支源碼;版本化之後
    那是 `versions/<V1>/src/xxx.py`。任何拿它去重新啟動的地方(restart、
    UAC 提權、托盤另開設定)都會【鎖在舊版本、而且不再讀 current.txt】。
    ★所以自我重啟一律走固定的 `.pyw`★;沒釘住(過渡期、直接跑 src)才照舊。
    """
    launcher = pinned_launcher()
    return launcher or os.path.abspath(sys.argv[0])


def build_restart_command(extra_args=None) -> list:
    """組出「重新啟動自己」的命令列。★抽成純函式是為了測得到★

    `restart_self()` 結尾會 `os._exit()` —— 在測試裡呼叫它會【殺掉整個 pytest
    行程】（我第一版就是這樣寫的，輸出直接被截斷）。真正要釘住的性質只有
    「組出來的是哪一條命令」，所以把它抽出來。

    ★[批次L L1 外審 P1] 重啟要走【固定的啟動器】，不是目前這支源碼★
    `runpy.run_path` 會把 `sys.argv[0]` 換成被執行的那支檔（CPython 的
    `_ModifiedArgv0`）。版本化之後那是 `versions/<V1>/src/xxx.py` —— 直接拿它
    重啟，新行程仍然跑 V1，而且【完全不會再讀一次 `current.txt`】:
    更新之後的重啟會永遠停在舊版（而且可能一直重試）。
    走 `.pyw` 啟動器才會重新解析指標，也才會重跑開機復原。
    ★只信真的存在的檔★ 沒釘住（過渡期、直接跑 src）就照舊行為。
    """
    args = list(extra_args) if extra_args else []
    if is_frozen():
        return [sys.executable] + args
    return [sys.executable, self_entry_path()] + args


def restart_self(extra_args=None, hard_exit_code=None,
                 on_confirmed=None, on_preready=None, on_recover=None) -> None:
    """雙軌重啟。

    [第九輪 §4] 兩階段交握(見 wait_for_handover):
      on_preready  — 子行程回報 PRE-READY(即將搶 mutex)或 0.6s 仍活著時呼叫:做「快而
                     關鍵」的拆解(拔熱鍵、放 mutex)。
      on_confirmed — 子行程 READY(或舊版子行程寬限到期)時呼叫:慢的拆解,之後退出。
      on_recover   — 子行程在 PRE-READY 之後、READY 之前死掉時呼叫:重取 mutex、重掛熱鍵,
                     本行程繼續服務(回 SPAWN_CHILD_DIED_AFTER_HANDOVER)。
    只傳 on_confirmed 的舊呼叫端維持今天的時序(0.6s 存活 → on_confirmed → 退出)。

    hard_exit_code：None（預設）→ 成功 spawn 後以 sys.exit(0) 結束（給 main thread
    用，能跑 atexit/finally）。給整數 → 改用 os._exit(code)。供「非 main thread」
    呼叫者使用（例如 health 監看 daemon）：sys.exit 在子 thread 只會結束該 thread、
    process 不會退 → 會變成新舊兩個 instance；os._exit 才能強制整個 process 結束。

    on_confirmed：可選的收尾 callback。**只有在確認新行程存活、即將退出本行程之前**
    才會被呼叫（新行程早夭而保留舊行程時不呼叫）。用途：把「停排程/停 tray/釋放 mutex/
    收 driver」等破壞性拆解延後到確定接手成功之後，避免 spawn 失敗時舊行程已被拆光而
    整個消失（見 autoclock.restart_program）。callback 內例外只記 log，不影響退出。
    傳了 on_confirmed 時，Popen 失敗【不會】退回 os.execv —— execv 無法確認接手且成功時
    永不返回，會讓 callback 裡的拆解整個跳過（見下方 except）。
    ★callback 內的順序要自己顧時間預算★：本函式已先等掉 _SPAWN_ALIVE_POLLS ×
    _SPAWN_ALIVE_INTERVAL_SEC（約 0.6s），而新行程搶 mutex 只重試 retry_sec（1.5s），
    所以「釋放 mutex」要排在 callback 的最前面，慢動作（排空佇列、收 driver）排後面。

    .pyw 模式：subprocess.Popen(pythonw, sys.argv[0], ...) + sys.exit
    .exe 模式：subprocess.Popen(sys.executable, ...) + sys.exit

    [2026-05-22 v29] 從 os.execv 改 subprocess.Popen + sys.exit。
    原因：Windows os.execv 是 spawn-and-exit 而非真正 exec — 並且實測在
    pythonw / 管理員提權 / 中文路徑 情境下偶發新 process 起不來。
    subprocess.Popen 顯式啟動新進程 → 確認 spawn 成功 → 我們才 exit。
    DETACHED_PROCESS + CREATE_NEW_PROCESS_GROUP：讓新 process 完全獨立，
    舊 process 退出時不會帶走新的。
    """
    import subprocess
    import logging

    cmd = build_restart_command(extra_args)

    # Windows: DETACHED_PROCESS=0x08, CREATE_NEW_PROCESS_GROUP=0x200
    # 讓新進程完全脫離父 console / process group，舊 process 退出不影響。
    creationflags = 0
    if sys.platform == "win32":
        creationflags = 0x00000008 | 0x00000200

    # [2026-08-02] 接住子行程的 stderr。pythonw + DETACHED_PROCESS 沒有 console,
    # 子行程若因 ImportError/壞更新而秒退,traceback 會【完全消失】——實機只看得到
    # 「新版本無法啟動」這句話,沒有任何線索可查(使用者 2026-08-02 回報)。
    # 存活確認通過就不再需要它(正常運作時 stderr 是空的);早夭時把尾巴記進 log。
    import tempfile

    _tmpdir = tempfile.gettempdir()
    try:
        sweep_old_restart_err_files(_tmpdir)
    except Exception:
        logging.debug("[restart_self] 清理舊 stderr 暫存檔失敗(忽略)", exc_info=True)
    _err_path = os.path.join(
        _tmpdir,
        f"cmuh_restart_{os.path.basename(str(sys.argv[0])) or 'app'}_{os.getpid()}.err")
    _errf = None
    try:
        _errf = open(_err_path, "wb")
    except OSError:
        _err_path = ""

    # [第九輪 §4] 交握檔:子行程用它回報 PRE-READY / READY;父行程每 0.1s 讀。
    _hs_path = os.path.join(
        _tmpdir,
        f"cmuh_restart_{os.path.basename(str(sys.argv[0])) or 'app'}_{os.getpid()}.hs")
    try:
        os.remove(_hs_path)                 # 上一次殘留(同 pid 重啟過)
    except OSError:
        pass
    _child_env = dict(os.environ)
    _child_env[RESTART_HANDSHAKE_ENV] = _hs_path

    def _child_stderr_tail() -> str:
        """讀子行程留下的 stderr 尾巴(讀不到就誠實說讀不到,不假裝沒事)。"""
        if not _err_path:
            return "(未能建立 stderr 暫存檔)"
        try:
            if _errf is not None:
                _errf.flush()
            with open(_err_path, "rb") as f:
                data = f.read()[-2000:]
            text = data.decode("utf-8", "replace").strip()
            return text or "(子行程沒有留下任何 stderr)"
        except OSError:
            return "(讀不到 stderr 暫存檔)"

    try:
        proc = subprocess.Popen(cmd, creationflags=creationflags, close_fds=True,
                                cwd=get_app_dir(), env=_child_env,
                                stdout=_errf, stderr=subprocess.STDOUT)
        logging.info("[restart_self] spawned new process pid=%s: %s", proc.pid, cmd)
        # [stability] 確認新行程沒有「起來就馬上死」再退出舊行程。主程式沒有外層
        # watchdog 接手，若新行程秒退（crash / 撞單例 mutex）又把舊的關掉 → 整個
        # 程式消失、要人工重開。短暫輪詢確認存活；早夭就保留舊行程不退出，至少
        # 還有一個能用。（單例 mutex 重啟競態已由 ensure_single_instance 重試處理，
        # 故正常情況新行程會穩定存活。）
        # [第九輪 §4] 兩階段交握(邏輯全在 wait_for_handover,純函式可測):
        #   早夭(PRE-READY 之前)→ 分辨「崩潰」與「照設計自行結束」(2026-08-02:exit=0 且無
        #   stderr = 子行程自己決定結束,例如本機沒有該程式的設定檔;不是「新版本無法啟動」),
        #   保留舊行程不退出;PRE-READY → on_preready(拔熱鍵、放 mutex);READY → on_confirmed
        #   (慢的拆解)後退出;交棒後才死 → on_recover(重取 mutex、重掛熱鍵)保留舊行程。
        # 舊呼叫端(只傳 on_confirmed)維持今天的時序。
        outcome = wait_for_handover(
            proc, _hs_path, on_preready=on_preready, on_confirmed=on_confirmed,
            on_recover=on_recover, stderr_tail=_child_stderr_tail)
        if outcome != HANDOVER_CONFIRMED:
            if outcome == SPAWN_CHILD_EXITED_ORDERLY:
                logging.info(
                    "[restart_self] 新行程自行正常結束 (exit=0、無 stderr)"
                    " → 多半是本機未設定該程式或單例已在執行;保留舊行程不退出")
            elif outcome == SPAWN_CHILD_CRASHED:
                logging.error(
                    "[restart_self] 新行程啟動後立即結束，保留舊行程不退出。"
                    "\n--- 新行程 stderr ---\n%s\n--- stderr 結束 ---", _child_stderr_tail())
            # 子行程已結束 → 沒人持有這兩個檔,直接清掉。
            try:
                if _errf is not None:
                    _errf.close()
                if _err_path:
                    os.remove(_err_path)
            except OSError:
                pass
            try:
                os.remove(_hs_path)
            except OSError:
                pass
            return outcome
        try:
            if _errf is not None:
                _errf.close()       # 父行程放掉自己的 handle;子行程仍持有
        except OSError:
            pass
        # 註:stderr 暫存檔【不能】在這裡刪 —— 子行程還開著它。改由下次 spawn 時的
        #     _sweep_old_restart_err_files 清掉(超過一天且已無人開啟者)。交握檔子行程
        #     已寫完、不再開著 → 可以刪。
        try:
            os.remove(_hs_path)
        except OSError:
            pass
    except Exception as e:
        # Popen 失敗 → 子行程根本沒起來,沒人持有這兩個檔 → 這裡就能直接刪掉。
        try:
            if _errf is not None:
                _errf.close()
            if _err_path:
                os.remove(_err_path)
        except OSError:
            pass
        try:
            os.remove(_hs_path)
        except OSError:
            pass
        # [2026-07-26 外審] os.execv 是「取代本行程」——【無法確認新行程真的活著】,
        # 而且成功時永不返回 → on_confirmed 一定不會被呼叫。呼叫端傳 on_confirmed 就是
        # 明確要求「確認接手後才拆解」;此時走這條 fallback 會讓稽核排空/mutex 釋放/
        # Chrome 收尾全部跳過(稽核憑空消失、chromedriver 變孤兒)。
        # 故有 on_confirmed 時【不走 fallback】,保留完好的舊行程不動 —— 這次不重啟
        # (自動更新下次還會再試),遠好過賭一把換到一個拆了一半的狀態。
        if on_confirmed is not None:
            logging.error(
                "[restart_self] subprocess.Popen 失敗: %s — 呼叫端要求確認接手後才拆解,"
                "不走無法確認的 os.execv fallback,保留舊行程繼續運作(本次不重啟)", e)
            return SPAWN_FAILED
        logging.error("[restart_self] subprocess.Popen 失敗: %s — fallback os.execv", e)
        try:
            os.execv(cmd[0], cmd)
        except Exception:
            logging.error("[restart_self] os.execv fallback 也失敗", exc_info=True)
            return SPAWN_FAILED
    # spawn 成功且新行程存活 → 退出本 process
    if hard_exit_code is not None:
        os._exit(hard_exit_code)
    sys.exit(0)
