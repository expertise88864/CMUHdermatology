# -*- coding: utf-8 -*-
"""啟動外部程式／開檔／開網頁，以及清掉孤兒 chromedriver。
（P2-06 分層第五刀(a) 2026-08-05，從 `AutomationApp` 搬出）

【為什麼這些原本是 method 卻不碰 self】
它們是「被誤放進類別的模組函式」—— 沒有任何實例狀態，只是剛好被寫在類別裡。

【★這一層不碰 UI★】
原本每支都直接 `messagebox.showerror(...)`。那讓「要啟動哪個腳本、單一實例怎麼判、
哪種錯誤算哪種」這些真正的邏輯**只能靠開 Tk 視窗才測得到**，實務上等於沒測。
所以這裡改成回傳 `LaunchOutcome`，由呼叫端決定怎麼呈現（main.py 仍然是彈
messagebox，行為不變）。共用層不該相依 tkinter，這也順便把那條相依擋掉。

【使用者看得到的字串是契約】
錯誤標題與內文一字不改地保留（診間使用者看慣了）。要改措辭是另一件事，
不可以夾帶在搬家裡 —— 搬家的驗收標準就是「使用者看到的東西完全一樣」。
"""
from __future__ import annotations

import logging
import os
import subprocess
import webbrowser
from dataclasses import dataclass

from cmuh_common.process_launch import launch_app_script
from cmuh_common.single_instance import is_instance_running


@dataclass(frozen=True)
class LaunchOutcome:
    """啟動結果。★不含任何 UI★ —— 呼叫端自己決定要不要彈窗。

    `already_running` 與 `ok=False` 是不同的事：前者是「刻意不啟動」（正常狀況，
    靜默即可），後者才是失敗。原本這兩者都只是 `return`，分不出來。
    """
    ok: bool
    already_running: bool = False
    error_title: str = ""
    error_message: str = ""

    @property
    def failed(self) -> bool:
        return not self.ok and not self.already_running


_OK = LaunchOutcome(True)


def launch_helper_script(script_name: str, *, what: str,
                         args: tuple = (), single_instance_mutex: str = "",
                         peer: str = "該程式") -> LaunchOutcome:
    """啟動同資料夾的輔助腳本（排班／打卡／座標偵測／會診查詢）。

    ★`single_instance_mutex` 必須在啟動【之前】檢查★
    否則使用者連按兩下就會有兩個實例在跑（打卡程式會重複打卡、會診查詢會重複寄信）。
    `test_main_background_launches_check_mutex_before_spawn` 釘的就是這個順序。

    `what`：使用者看到的程式名稱（「排班程式」）。
    `peer`：「請確認主程式與○○在同一個資料夾中」裡的○○ —— 原本四支的措辭
            不完全一樣（兩支寫程式名、兩支寫「該程式」），照原樣保留。
    """
    if single_instance_mutex and is_instance_running(single_instance_mutex):
        logging.info("%s is already running; skip launch", what)
        return LaunchOutcome(False, already_running=True)
    try:
        logging.info("Launching %s: %s%s", what, script_name,
                     f" ({' '.join(args)})" if args else "")
        launch_app_script(script_name, args=args)
    except FileNotFoundError:
        logging.error("%s script not found: %s", what, script_name)
        return LaunchOutcome(
            False, error_title="啟動失敗",
            error_message=(f"找不到{what}檔案: {script_name}\n\n"
                           f"請確認主程式與{peer}在同一個資料夾中。"))
    except Exception as e:
        logging.error("Failed to launch %s: %s", what, e)
        return LaunchOutcome(False, error_title="啟動失敗",
                             error_message=f"無法啟動{what}:\n{e}")
    return _OK


def open_local_program(path: str) -> LaunchOutcome:
    """用系統關聯開啟本機程式／檔案（院內系統捷徑那一排按鈕）。"""
    try:
        logging.info("Attempting to launch program at: %s", path)
        os.startfile(path)                       # noqa: S606 (Windows 專用)
    except FileNotFoundError:
        logging.error("Program not found at: %s", path)
        return LaunchOutcome(
            False, error_title="啟動失敗",
            error_message=f"找不到指定的程式！\n\n請確認路徑是否正確:\n{path}")
    except Exception as e:
        logging.error("Failed to launch program: %s", e)
        return LaunchOutcome(False, error_title="啟動失敗",
                             error_message=f"無法啟動程式:\n{e}")
    return _OK


def open_url(url: str) -> LaunchOutcome:
    try:
        logging.info("Attempting to open URL: %s", url)
        webbrowser.open(url, new=2)
    except Exception as e:
        logging.error("Failed to open URL: %s", e)
        return LaunchOutcome(False, error_title="開啟失敗",
                             error_message=f"無法開啟網頁:\n{e}")
    return _OK


def kill_orphan_chromedriver() -> None:
    """[O21] 結束孤兒 chromedriver.exe + 其子 chrome.exe，防止背景殘留的鬼視窗。

    原理：找父行程為本程式的 chromedriver → 連鎖對話 kill 其子行程（chrome
          .exe / chrome_native_messaging_host 等）。比 driver.quit() 快 10x
          (taskkill 100ms vs Chrome graceful shutdown 1-2s)。

    [MG-04] kill 條件限「父行程是本程式」或「父行程已不存在」（chromedriver 孤兒＝
    前次崩潰遺留）；父行程存在且非本程式者不動，避免誤殺使用者自己的 Chrome。

    ★psutil 用函式內 import★ 它是選用相依；缺了只代表「清不掉孤兒」，
    不可以讓整個模組 import 不起來（見 reg52_fetch 那次外審）。
    """
    try:
        import psutil
    except ImportError:
        return
    try:
        my_pid = os.getpid()
        to_kill = []
        for p in psutil.process_iter(['pid', 'name', 'ppid']):
            try:
                n = (p.info.get('name') or '').lower()
                if 'chromedriver' not in n:
                    continue
                ppid = p.info.get('ppid', 0)
                # [MG-04 2026-07-12] 除本程式直屬的 chromedriver 外，也收「父行程已不存在」的孤兒
                # （前次崩潰遺留的殘留行程）；實驗證實 chromedriver 孤兒本身極少自動回收，長期會
                # 造成記憶體洩漏（~150MB）。用 pid_exists 明確判斷（PID 重用極罕見，可接受）。
                if ppid == my_pid or (ppid and not psutil.pid_exists(ppid)):
                    to_kill.append(p)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        # 對每個 chromedriver，連鎖對話 kill 子孫 (chrome.exe / 渲染行程)
        for cd in to_kill:
            try:
                for child in cd.children(recursive=True):
                    try:
                        child.kill()  # kill 比 terminate 快 (immediate)
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue
                cd.kill()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        logging.debug("[O21] iter chromedriver 失敗", exc_info=True)


def open_file_at_line(path: str, line_no: int) -> LaunchOutcome:
    """用外部編輯器開啟指定檔案並跳到某一行（cursor → code → notepad → xdg-open）。

    ★`path` 一定要由呼叫端傳進來★
    原本這支寫在 main.py 裡、用 `__file__` 取路徑。搬到本模組之後 `__file__` 會變成
    **本檔**，開出來就是錯的檔案 —— 而且不會有任何錯誤訊息，只會安靜地開錯。
    所以路徑改成參數，由知道自己是誰的那一方提供。
    """
    for args in (
        ["cursor", "-g", f"{path}:{line_no}"],
        ["code", "-g", f"{path}:{line_no}"],
    ):
        import shutil
        exe = shutil.which(args[0])
        if not exe:
            continue
        try:
            subprocess.Popen(args, close_fds=os.name != "nt")
            return _OK
        except Exception as e:
            logging.debug("用 %s 開啟失敗: %s", args[0], e)
    if os.name == "nt":
        ntp = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "notepad.exe")
        if os.path.isfile(ntp):
            try:
                subprocess.Popen([ntp, path], close_fds=False)
                return _OK
            except Exception as e:
                logging.debug("notepad 開啟失敗: %s", e)
    try:
        subprocess.Popen(["xdg-open", path], close_fds=True)
    except Exception as e:
        return LaunchOutcome(
            False, error_title="無法開啟",
            error_message=f"無法開啟程式檔案給編輯器\n{e}\n\n{path}")
    return _OK
