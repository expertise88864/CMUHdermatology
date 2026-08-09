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


def pinned_launcher() -> str:
    """啟動器釘住的固定 `.pyw`;沒釘住／檔案不在／不在 app 根目錄第一層 → 空字串。

    ★[外審 P2-02] 不可以只驗『檔案存在』★
    環境變數是會被繼承的。只驗存在的話,一個指向別處的值就能讓我們去重啟
    另一支程式。所以要求它【就在釘住的 app 根目錄底下、而且是第一層】——
    那正是六支 launcher 真正的位置。
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
                 on_confirmed=None) -> None:
    """雙軌重啟。

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
                                cwd=get_app_dir(),
                                stdout=_errf, stderr=subprocess.STDOUT)
        logging.info("[restart_self] spawned new process pid=%s: %s", proc.pid, cmd)
        # [stability] 確認新行程沒有「起來就馬上死」再退出舊行程。主程式沒有外層
        # watchdog 接手，若新行程秒退（crash / 撞單例 mutex）又把舊的關掉 → 整個
        # 程式消失、要人工重開。短暫輪詢確認存活；早夭就保留舊行程不退出，至少
        # 還有一個能用。（單例 mutex 重啟競態已由 ensure_single_instance 重試處理，
        # 故正常情況新行程會穩定存活。）
        import time as _time
        for _ in range(_SPAWN_ALIVE_POLLS):        # 最多約 0.6 秒
            _time.sleep(_SPAWN_ALIVE_INTERVAL_SEC)
            rc = proc.poll()
            if rc is not None:
                tail = _child_stderr_tail()
                # ★[2026-08-02] 分辨「崩潰」與「照設計自行結束」★
                #   exit=0 且沒有任何 stderr → 新行程是【自己決定】結束的,例如
                #   這台機器沒有該程式的設定檔、或單例已被別人持有。那不是
                #   「新版本無法啟動」—— 對使用者宣稱後者,就是在陳述程式並不
                #   確知的事(使用者回報:沒在跑打卡的電腦一直跳這個通知)。
                outcome = classify_child_exit(rc, tail)
                orderly = outcome == SPAWN_CHILD_EXITED_ORDERLY
                if orderly:
                    logging.info(
                        "[restart_self] 新行程自行正常結束 (exit=0、無 stderr)"
                        " → 多半是本機未設定該程式或單例已在執行;保留舊行程不退出")
                else:
                    logging.error(
                        "[restart_self] 新行程啟動後立即結束 (exit=%s)，保留舊行程不退出。"
                        "\n--- 新行程 stderr ---\n%s\n--- stderr 結束 ---",
                        rc, tail)
                try:
                    if _errf is not None:
                        _errf.close()
                    if _err_path:
                        os.remove(_err_path)
                except OSError:
                    pass
                return outcome
        try:
            if _errf is not None:
                _errf.close()       # 父行程放掉自己的 handle;子行程仍持有
        except OSError:
            pass
        # 註:這個檔【不能】在這裡刪 —— 子行程還開著它。改由下次 spawn 時的
        #     _sweep_old_restart_err_files 清掉(超過一天且已無人開啟者)。
        # [2026-07-25 審查/codex] 確認新行程存活【之後】才做破壞性拆解。
        # 背景：呼叫端(如 autoclock.restart_program)原本必須在 spawn 前就 running.clear()
        # + 停 tray + 釋放 mutex,於是上面「保留舊行程」的保護 return 回去時,舊行程其實
        # 已經被拆光 → 排程/看門狗迴圈與 tray 都停了、main() 隨即返回 → 打卡程式整個消失,
        # 正是這道保護想避免的事。改由呼叫端把拆解包成 on_confirmed 交進來,只有確定
        # 新行程活著才執行;子行程搶 mutex 失敗會自行重試(~1.5s > 這裡的 0.6s),故仍安全。
        if on_confirmed is not None:
            try:
                on_confirmed()
            except Exception:
                logging.exception("[restart_self] on_confirmed 收尾失敗（仍照常退出）")
    except Exception as e:
        # Popen 失敗 → 子行程根本沒起來,沒人持有這個檔 → 這裡就能直接刪掉。
        try:
            if _errf is not None:
                _errf.close()
            if _err_path:
                os.remove(_err_path)
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
