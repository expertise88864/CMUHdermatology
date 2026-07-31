# -*- coding: utf-8 -*-
"""HIS 視窗／控制項的 Win32 原語（P2-06 分層第一刀）。

【為什麼是這一層先搬】
main.py 17,519 行，量過之後有 53 個頂層函式【完全不引用任何模組級全域】——
它們是可以直接搬走的葉子。其中最內聚的一族就是這 15 個：找視窗、走子孫樹、
送訊息、切 IME。它們對 HIS 的業務一無所知，只知道 Win32。

而且這一層正是本 repo 反覆出事的地方：
  * 2026-07-27 診間事故：`IsWindow` 與 `IsWindowVisible` 混用 —— Delphi 的 modal
    form 關閉時只是 Hide，視窗物件還在，於是「還在不在」判斷永遠為真，F9/F10 全面卡住。
  * 「視窗只靠 class+title 辨識」：別的程式跳出的同 class(#32770)對話框被當成 HIS 的
    警告框去自動按「是」。修法是 `require_pid`。
  * 「上一次流程留下沒關的視窗」：`_wait_for_window` 找到就回傳 → 整段操作打在舊視窗上，
    表面完全正常。修法是 `collect_windows_by_class` 先拍快照再 `exclude_hwnds`。
把它們集中起來，下次要再加固（例如統一要求 pid 或回讀）只有一個地方要改。

【測試接縫】
原本每個函式都直接寫 `ctypes.windll.user32.XXX`，等於不可測 —— 上面那三次事故的
判斷邏輯一行都沒有測試守著。這裡把它收斂成 `_user32()` / `_kernel32()` / `_imm32()`
三個取得器，測試換掉它們就能用假的視窗樹跑完整條判斷路徑。
★這是本次唯一的行為面改動★：呼叫的 Win32 API、順序、回傳值語意全部照舊，
只是多了一層取得函式。

【搬移原則】註解一字不刪。裡面每一段 `[日期 來源]` 都是踩過的坑，
刪掉就等於把「為什麼要這樣寫」丟掉（本 repo 已經有過「加守衛前沒查前人為何刻意不做」
的教訓）。
"""
from __future__ import annotations

import ctypes
import logging
import time
from ctypes import wintypes
from typing import Optional


# ── 測試接縫：所有 Win32 呼叫都經過這三個取得器 ──────────────────────────────
def _user32():
    return ctypes.windll.user32


def _kernel32():
    return ctypes.windll.kernel32


def _imm32():
    return ctypes.windll.imm32


# ── 找視窗 ──────────────────────────────────────────────────────────────────
def find_window_by_class_title(class_name: str, title_kw: str = "",
                               exclude_hwnd: int = 0,
                               require_pid: int = 0,
                               exclude_hwnds: tuple = ()) -> int:
    """全域找 class=X 且 title 含 keyword 的可見視窗。

    [H1 2026-07-09] require_pid 非 0 時,只回傳【屬於該 PID(HIS 行程)】的視窗 —— 避免把
    別的程式跳出的同 class(#32770)標準對話框誤當 HIS 警告框去自動按「是」。exclude_hwnds
    可額外排除多個(如已處理過的第一個對話框),避免重複動作。

    [2026-05-22 v38] 從 EnumWindows + Python callback 改 FindWindowExW
    (純 Win32，不走 Python boundary)。EnumWindows + Python cb 每個 top-level
    window 都跨 C→Python 邊界 (~0.05ms/個) — 一台 PC 通常 100-300 個
    top-level windows = 10-30ms per call。9 popup class × 0.12s polling
    = ~70% CPU 都在 Python callback。改 FindWindowExW 後降到 < 1ms。
    """
    user32 = _user32()
    # FindWindowExW(hWndParent=NULL, hWndChildAfter, class, title)
    # hWndParent=NULL + 走 prev_hwnd 鏈 = 跨所有 top-level windows
    FindWindowExW = user32.FindWindowExW
    FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND,
                              wintypes.LPCWSTR, wintypes.LPCWSTR]
    FindWindowExW.restype = wintypes.HWND

    prev = 0
    while True:
        try:
            hwnd = FindWindowExW(None, prev, class_name, None)
        except Exception:
            return 0
        if not hwnd:
            return 0
        prev = hwnd
        if hwnd == exclude_hwnd or (exclude_hwnds and hwnd in exclude_hwnds):
            continue
        try:
            # ★IsWindowVisible 不是 IsWindow★（2026-07-27 診間事故）：
            #   Delphi 的 modal form 關閉時只是 Hide，視窗物件還在。
            if not user32.IsWindowVisible(hwnd):
                continue
        except Exception:
            continue
        if require_pid:
            try:
                wpid = ctypes.c_ulong(0)
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
                if wpid.value != require_pid:
                    continue
            except Exception:
                continue
        if title_kw:
            try:
                n = user32.GetWindowTextLengthW(hwnd)
                if n <= 0:
                    continue
                t_buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, t_buf, n + 1)
                if title_kw not in t_buf.value:
                    continue
            except Exception:
                continue
        return hwnd
    # [2026-05-25 v15 死碼清除] 移除舊 Python callback 路徑 (~50 行) — 上方
    # while True FindWindowExW loop 一定 return (hwnd or 0)，下面永遠到不了。


def collect_windows_by_class(class_name: str, title_kw: str = "",
                             require_pid: int = 0) -> tuple:
    """[2026-07-26 審查] 列出目前【已存在】的同 class 視窗,給呼叫端在觸發新視窗之前
    先拍快照、之後用 exclude_hwnds 排除。

    需要它的原因:`_wait_for_window` 是「找到就回傳」——上一次流程留下沒關的同意書/
    片語視窗會讓它【立刻回傳舊視窗】,後續整段操作都打在舊視窗上,而且表面上完全正常
    (視窗有、按鈕點得到、log 一路綠)。
    內部重複呼叫 `find_window_by_class_title` 並把找到的逐一排除,直到找不到為止。"""
    found: list = []
    for _ in range(32):        # 上限防禦:正常最多一兩個,不會無限長
        hwnd = find_window_by_class_title(class_name, title_kw,
                                          require_pid=require_pid,
                                          exclude_hwnds=tuple(found))
        if not hwnd:
            break
        found.append(hwnd)
    return tuple(found)


# ── 走子孫樹 ────────────────────────────────────────────────────────────────
def enum_class_in_window(parent_hwnd: int, target_class: str) -> list:
    """EnumChildWindows 抓全部 class=X 的子孫，按 (top, left) 排序。
    回傳 list of (hwnd, rect_top, rect_left)。"""
    out = []

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @EnumWindowsProc
    def cb(child, lparam):
        try:
            cls_buf = ctypes.create_unicode_buffer(64)
            _user32().GetClassNameW(child, cls_buf, 64)
            if cls_buf.value == target_class:
                r = wintypes.RECT()
                if _user32().GetWindowRect(child, ctypes.byref(r)):
                    out.append((child, r.top, r.left))
        except Exception:
            pass
        return True

    _user32().EnumChildWindows(parent_hwnd, cb, 0)
    out.sort(key=lambda x: (x[1], x[2]))
    # 去重複（hwnd 可能在 EnumChildWindows 出現多次）
    seen = set()
    uniq = []
    for h, t, left in out:
        if h not in seen:
            seen.add(h)
            uniq.append((h, t, left))
    return uniq


def enum_direct_children(parent_hwnd: int, target_class: str = "") -> list:
    """列出 parent_hwnd 的直系子視窗（不遞迴）；可選 class 過濾。"""
    GW_CHILD = 5
    GW_HWNDNEXT = 2
    children = []
    h = _user32().GetWindow(parent_hwnd, GW_CHILD)
    while h:
        if not target_class:
            children.append(h)
        else:
            cls_buf = ctypes.create_unicode_buffer(64)
            _user32().GetClassNameW(h, cls_buf, 64)
            if cls_buf.value == target_class:
                children.append(h)
        h = _user32().GetWindow(h, GW_HWNDNEXT)
    return children


def find_descendants_by_exact_text(parent_hwnd: int, target_class: str,
                                   target_text: str) -> list:
    """找所有 class+text 精確匹配的子孫；按 (top, left) 排序去重。

    跟 find_descendant_by_class_text 不同：這個比對【完整 strip 後相等】，
    用來精確區分 '片語' vs '單張片語'（同 class、文字含子字串會混淆）。"""
    out = []

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @EnumWindowsProc
    def cb(child, lparam):
        try:
            cls_buf = ctypes.create_unicode_buffer(64)
            _user32().GetClassNameW(child, cls_buf, 64)
            if cls_buf.value != target_class:
                return True
            n = _user32().GetWindowTextLengthW(child)
            if n > 0:
                t_buf = ctypes.create_unicode_buffer(n + 1)
                _user32().GetWindowTextW(child, t_buf, n + 1)
                if t_buf.value.strip() == target_text:
                    r = wintypes.RECT()
                    if _user32().GetWindowRect(child, ctypes.byref(r)):
                        out.append((child, r.top, r.left))
        except Exception:
            pass
        return True

    _user32().EnumChildWindows(parent_hwnd, cb, 0)
    seen = set()
    uniq = [x for x in out if not (x[0] in seen or seen.add(x[0]))]
    uniq.sort(key=lambda x: (x[1], x[2]))
    return uniq


def find_descendant_by_class_text(parent_hwnd: int, target_class: str,
                                  text_keyword: str) -> int:
    """EnumChildWindows 找 class=X 且 text 含 keyword 的子視窗（遞迴）。"""
    found = [0]

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @EnumWindowsProc
    def cb(child, lparam):
        try:
            cls_buf = ctypes.create_unicode_buffer(64)
            _user32().GetClassNameW(child, cls_buf, 64)
            if cls_buf.value == target_class:
                n = _user32().GetWindowTextLengthW(child)
                if n > 0:
                    t_buf = ctypes.create_unicode_buffer(n + 1)
                    _user32().GetWindowTextW(child, t_buf, n + 1)
                    if text_keyword in t_buf.value:
                        found[0] = child
                        return False
        except Exception:
            pass
        return True

    _user32().EnumChildWindows(parent_hwnd, cb, 0)
    return found[0]


def find_first_descendant_by_class(parent_hwnd: int, target_class: str) -> int:
    """EnumChildWindows 找第一個 class=target_class 的子孫 hwnd。"""
    found = [0]
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @EnumWindowsProc
    def cb(child, lparam):
        try:
            buf = ctypes.create_unicode_buffer(64)
            _user32().GetClassNameW(child, buf, 64)
            if buf.value == target_class:
                found[0] = child
                return False
        except Exception:
            pass
        return True

    _user32().EnumChildWindows(parent_hwnd, cb, 0)
    return found[0]


def window_is_ancestor(ancestor_hwnd: int, hwnd: int) -> bool:
    """hwnd 是否為 ancestor_hwnd 本身、或其子孫(沿 parent 鏈上溯)。"""
    if not hwnd or not ancestor_hwnd:
        return False
    GA_PARENT = 1
    cur = hwnd
    for _ in range(64):   # 防環,最多上溯 64 層
        if cur == ancestor_hwnd:
            return True
        try:
            parent = _user32().GetAncestor(cur, GA_PARENT)
        except Exception:
            return False
        if not parent or parent == cur:
            return False
        cur = parent
    return False


# ── 送訊息 ──────────────────────────────────────────────────────────────────
def post_click_to_control(hwnd: int, client_x: Optional[int] = None,
                          client_y: Optional[int] = None) -> bool:
    """送 WM_LBUTTONDOWN + WM_LBUTTONUP 到目標 control，完全不動實體滑鼠。

    位置用 client 座標（相對於該 control 左上角）；不指定就用該 control 的
    client 中心。比 pyautogui.click 好處：
      1. 不會移動實體滑鼠（不會干擾使用者）
      2. 不會被 SetCursorPos 競賽條件影響
      3. 訊息直接到目標 control，不會被別人攔截

    對 Delphi VCL 大部分控制項都生效（TButton/TBitBtn/TGroupButton/TabCtrl
    等都處理 WM_LBUTTONDOWN 來觸發 click event）。"""
    if not hwnd:
        return False
    try:
        if client_x is None or client_y is None:
            r = wintypes.RECT()
            if not _user32().GetClientRect(hwnd, ctypes.byref(r)):
                return False
            client_x = (r.right - r.left) // 2 if client_x is None else client_x
            client_y = (r.bottom - r.top) // 2 if client_y is None else client_y
        lparam = ((client_y & 0xFFFF) << 16) | (client_x & 0xFFFF)
        MK_LBUTTON = 0x0001
        WM_LBUTTONDOWN = 0x0201
        WM_LBUTTONUP = 0x0202
        down_ok = bool(_user32().PostMessageW(
            hwnd, WM_LBUTTONDOWN, MK_LBUTTON, lparam))
        up_ok = bool(_user32().PostMessageW(
            hwnd, WM_LBUTTONUP, 0, lparam))
        if not (down_ok and up_ok):
            logging.warning("post_click_to_control PostMessage failed: "
                            "hwnd=%s down=%s up=%s",
                            hwnd, down_ok, up_ok)
            return False
        return True
    except Exception:
        logging.error("post_click_to_control 失敗", exc_info=True)
        return False


def send_chars_to_window(hwnd: int, text: str) -> bool:
    """送 WM_CHAR 一字一字到目標 control。完全繞過 IME。

    pyautogui.typewrite 走 OS keyboard input → IME 攔截（中文模式下「5」被當組
    字輸入）。WM_CHAR 直接到 control，IME 沒機會攔截。

    [stability] 改用 PostMessageW（非同步）取代 SendMessageW：後者對跨行程視窗
    是同步阻塞，醫院 app 凍住時會無限期卡住 hotkey 工作緒並永久鎖死全部熱鍵。
    PostMessage 立即返回、訊息照 FIFO 入該 control 佇列由 Delphi 依序處理。"""
    if not hwnd or not text:
        return False
    WM_CHAR = 0x0102
    try:
        user32 = _user32()
        for ch in text:
            # [UD-12 2026-07-12] 逐字前確認視窗仍在:編輯器中途被關(hwnd 失效)即中止回 False,交
            # caller 走警示,避免代碼欄殘留半截醫令(原本不論如何一律回 True)。
            if not user32.IsWindow(hwnd):
                logging.warning("[send_chars] 目標視窗中途消失,中止(已送部分字元)")
                return False
            # [GPT-5.6 第三輪] PostMessageW 回 0 = Windows 根本沒收(佇列滿/hwnd 失效)。
            # 原本完全不檢查 → 送出失敗仍回 True → 稽核記成功、半截醫令沒人知道。
            if not user32.PostMessageW(hwnd, WM_CHAR, ord(ch), 0):
                logging.warning("[send_chars] PostMessageW 回 0(佇列滿/視窗失效),"
                                "中止(已送部分字元)")
                return False
            time.sleep(0.02)  # 給 Delphi 依序處理（非同步下保險）
        return True
    except Exception:
        logging.error("send_chars_to_window 失敗", exc_info=True)
        return False


def send_enter_to_window(hwnd: int) -> bool:
    """送 VK_RETURN keydown+up 到指定 control。

    [stability] 同 send_chars_to_window：改用 PostMessageW 非同步送，避免被
    凍住的醫院 app 同步阻塞 hotkey 工作緒。"""
    if not hwnd:
        return False
    try:
        WM_KEYDOWN = 0x0100
        WM_KEYUP = 0x0101
        VK_RETURN = 0x0D
        # 只送 keydown+keyup（等同真人按「一次」Enter）。Delphi VCL 的訊息迴圈會自行
        # TranslateMessage 把 WM_KEYDOWN(VK_RETURN) 轉成對應的 WM_CHAR(\r)，控制項
        # 因而收到「剛好一次」Enter。
        # [修正 2026-06-01] 原本在 keydown/keyup 之後又額外 PostMessage 一個 WM_CHAR \r，
        # 等於控制項收到兩個 Enter 字元（keydown 被翻譯出的 \r + 這個多餘的 \r）→
        # 醫令代碼被送出兩次 → F1/F2/F3/F4/F5 跳「資料重複確認」。移除多餘的 WM_CHAR。
        # [GPT-5.6 第三輪 + codex P1] 檢查 PostMessageW 回傳,但要分清【提交點】:
        # Delphi 的 TranslateMessage 是對 WM_KEYDOWN 轉出 Enter 的 WM_CHAR —— keydown
        # 一旦被接受,醫令【可能已經提交】。此時若因 keyup 失敗回 False,呼叫端會誤判
        # 「什麼都沒送」→ 跳過療程/記 failed、重試還可能把已提交的醫令再下一次。
        # 故:keydown 失敗(真的沒送)→ False;keydown 成功後 keyup 失敗 → 補送一次,
        # 無論補送成敗都回 True(提交點已過,不可走「沒送出」的重試/中止路徑)。
        user32 = _user32()
        ok_down = user32.PostMessageW(hwnd, WM_KEYDOWN, VK_RETURN, 0x1C0001)
        if not ok_down:
            logging.warning("[send_enter] keydown PostMessageW 回 0,未送出 Enter")
            return False
        ok_up = user32.PostMessageW(hwnd, WM_KEYUP, VK_RETURN, 0xC01C0001)
        if not ok_up:
            ok_up = user32.PostMessageW(hwnd, WM_KEYUP, VK_RETURN,
                                        0xC01C0001)   # 補送一次
            logging.warning("[send_enter] keyup 首送失敗(補送%s);keydown 已被接受,"
                            "Enter 視為已提交", "成功" if ok_up else "仍失敗")
        return True
    except Exception:
        return False


# ── z-order / 焦點 / IME ────────────────────────────────────────────────────
def bring_window_front(hwnd: int) -> None:
    """把任意視窗叫到最前(含 AttachThreadInput)。截圖前要視窗真的顯示才有像素。"""
    try:
        SW_RESTORE = 9
        if _user32().IsIconic(hwnd):
            _user32().ShowWindow(hwnd, SW_RESTORE)
    except Exception:
        pass
    try:
        cur = _kernel32().GetCurrentThreadId()
        fg = _user32().GetForegroundWindow()
        ftid = (_user32().GetWindowThreadProcessId(fg, None)
                if fg else 0)
        attached = False
        if ftid and ftid != cur:
            attached = bool(_user32().AttachThreadInput(ftid, cur, True))
        try:
            HWND_TOP = 0
            SWP_NOMOVE, SWP_NOSIZE = 0x0002, 0x0001
            _user32().SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0,
                                   SWP_NOMOVE | SWP_NOSIZE)
            _user32().BringWindowToTop(hwnd)
            _user32().SetForegroundWindow(hwnd)
        finally:
            if attached:
                _user32().AttachThreadInput(ftid, cur, False)
    except Exception:
        logging.debug("[卡號] bring_window_front 失敗", exc_info=True)


def send_window_to_back(hwnd: int) -> bool:
    """把視窗推到 z-order 最底層（不活化、不搶 focus）。

    ★[2026-07-31 P2-06] 目前【沒有任何呼叫端】★
    搬家時 ruff 抓到 main.py 匯入它卻沒用（全 repo 只有這裡有定義）。原本的
    docstring 寫「用於 F9/F10 流程」—— 那是**不成立的宣稱**，F9/F10 沒有呼叫它。
    保留函式本身（零風險、有測試），但措辭要誠實：這是設計意圖，不是現況。

    原本的設計意圖：醫院系統開新視窗時預設會搶 foreground 打斷使用者，
    用這個推到底層 → 使用者保持當前視窗。我們所有的訊息都用 PostMessage，
    不需要視窗是 foreground 就能跑。
    （要嘛把它接回 F9/F10，要嘛刪掉 —— 但那是另一個改動，需要實機確認搶焦點
    現在到底有沒有在發生。）

    SWP_NOACTIVATE：不要把 hwnd 變 active
    SWP_NOMOVE/SWP_NOSIZE：保持位置 / 大小
    HWND_BOTTOM (=1)：z-order 最底"""
    if not hwnd:
        return False
    try:
        SWP_NOACTIVATE = 0x0010
        SWP_NOMOVE = 0x0002
        SWP_NOSIZE = 0x0001
        HWND_BOTTOM = 1
        _user32().SetWindowPos(
            hwnd, HWND_BOTTOM, 0, 0, 0, 0,
            SWP_NOACTIVATE | SWP_NOMOVE | SWP_NOSIZE)
        return True
    except Exception:
        logging.debug("send_window_to_back 失敗", exc_info=True)
        return False


def get_thread_focus(target_hwnd: int) -> int:
    """取得 target_hwnd 那個 thread 內目前焦點的 control hwnd。

    cross-thread 的 GetFocus 預設回 0；要用 AttachThreadInput 把當前 thread
    跟 target thread 連起來才能讀。用於知道「使用者鍵盤輸入會送到哪個 control」。"""
    if not target_hwnd:
        return 0
    try:
        user32 = _user32()
        kernel32 = _kernel32()
        target_tid = user32.GetWindowThreadProcessId(target_hwnd, None)
        cur_tid = kernel32.GetCurrentThreadId()
        if target_tid == cur_tid:
            return user32.GetFocus()
        user32.AttachThreadInput(cur_tid, target_tid, True)
        try:
            return user32.GetFocus()
        finally:
            user32.AttachThreadInput(cur_tid, target_tid, False)
    except Exception:
        return 0


def force_ime_english(hwnd: int = 0) -> None:
    """把當前前景視窗（或指定 hwnd）的 IME 切到英文模式（關閉 IME 轉換）。

    用 ImmSetOpenStatus(himc, False) 對 IME context 設「不開」=「直接送
    英文字」。對 Delphi VCL 應用通常立刻生效，不會像 Ctrl+Space 那樣依賴
    使用者 IME 設定。
    為什麼必要：使用者中文輸入法（注音/新酷音/微軟拼音）打開時，
    pyautogui.typewrite("51017") 的 "5" 會被 IME 攔截當作組字輸入，
    結果什麼都沒寫進輸入欄。強制切英文徹底避免這個問題。"""
    try:
        imm32 = _imm32()
        target = hwnd or _user32().GetForegroundWindow()
        if not target:
            return
        himc = imm32.ImmGetContext(target)
        if himc:
            try:
                imm32.ImmSetOpenStatus(himc, False)
            finally:
                imm32.ImmReleaseContext(target, himc)
    except Exception:
        logging.debug("force_ime_english 失敗（IME 模組不可用？忽略）", exc_info=True)


# ── 查詢小工具（P2-06 第二刀 2026-07-31）────────────────────────────────────
def get_window_pid(hwnd: int) -> int:
    """回傳視窗所屬行程 PID(失敗回 0)。用於「只對 HIS 行程的對話框動作」的把關。"""
    try:
        pid = ctypes.c_ulong(0)
        _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)
    except Exception:
        return 0


def get_class_name_of(hwnd: int) -> str:
    """便捷 wrapper, 取 hwnd 的 class name。"""
    try:
        buf = ctypes.create_unicode_buffer(64)
        _user32().GetClassNameW(hwnd, buf, 64)
        return buf.value
    except Exception:
        return ""


def get_window_text(hwnd: int) -> str:
    n = _user32().GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32().GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def screen_point_in_window(root_hwnd: int, x: int, y: int) -> bool:
    """螢幕座標 (x,y) 最上層的視窗是否屬於 root_hwnd(本身或子孫)。
    用於確認畫面取樣點沒被別的視窗(如 Chrome)遮住 —— 遮住時 WindowFromPoint 會回別視窗。"""
    try:
        wfp = _user32().WindowFromPoint
        wfp.argtypes = [wintypes.POINT]
        wfp.restype = wintypes.HWND
        top = wfp(wintypes.POINT(int(x), int(y)))
    except Exception:
        return False
    return window_is_ancestor(root_hwnd, top)


def get_ime_focus_hwnd():
    """取得前景應用程式真正有焦點的控制項 handle（需 AttachThreadInput）。"""
    try:
        u = _user32()
        hwnd_fg = u.GetForegroundWindow()
        fore_tid = u.GetWindowThreadProcessId(hwnd_fg, None)
        cur_tid = _kernel32().GetCurrentThreadId()
        u.AttachThreadInput(cur_tid, fore_tid, True)
        hwnd_focus = u.GetFocus()
        u.AttachThreadInput(cur_tid, fore_tid, False)
        return hwnd_focus if hwnd_focus else hwnd_fg
    except Exception:
        return _user32().GetForegroundWindow()


# ── 送訊息（第二刀）──────────────────────────────────────────────────────────
def send_key_to_window(hwnd: int, vk: int, count: int = 1,
                       interval: float = 0.05) -> None:
    """對指定 hwnd 送 N 次 VK 鍵 (WM_KEYDOWN + WM_KEYUP)。用 PostMessage
    非同步，不需要 foreground，也不會被 IME 攔截。"""
    WM_KEYDOWN = 0x0100
    WM_KEYUP = 0x0101
    for _ in range(count):
        _user32().PostMessageW(hwnd, WM_KEYDOWN, vk, 0)
        _user32().PostMessageW(hwnd, WM_KEYUP, vk, 0)
        time.sleep(interval)


def send_message_timeout_ex(hwnd: int, msg: int, wparam: int, lparam: int,
                            timeout_ms: int = 2000):
    """同 _send_message_timeout,但回 (ok, result)。ok=False 代表逾時/失敗 ——
    此時 result 沒有意義(呼叫端不可把它當成對方視窗的真實回覆)。"""
    result = ctypes.c_size_t(0)
    SMTO_ABORTIFHUNG = 0x0002
    SendMessageTimeoutW = _user32().SendMessageTimeoutW
    ret = SendMessageTimeoutW(hwnd, msg, wparam, lparam,
                              SMTO_ABORTIFHUNG, timeout_ms,
                              ctypes.byref(result))
    if ret == 0:
        logging.debug("SendMessageTimeout 失敗或超時 hwnd=%s msg=0x%X", hwnd, msg)
        return False, result.value
    return True, result.value


def send_yiling_menu_command(hwnd: int, menu_id: int) -> bool:
    """對主程式視窗送 WM_COMMAND 觸發 menu 項目。

    用 PostMessage (非同步)：實測 (2026-05-18 12:43 F9) 用 SendMessage 會卡 11+ 秒
    沒回應——當 hospital app 處理 WM_COMMAND 開新 modal 視窗時，handler
    可能 block。Post 不會 hang，主程式有空就會處理。後續用 _wait_for_window
    poll 視窗出現。

    HIWORD(wParam)=0 表示來源是 menu (不是 accelerator/control)。
    F3/F4 觸發代碼輸入 (id=219) 用 Send 跑得通，是因為代碼輸入是輕量 UI
    操作（focus 跳到 grid）；開 modal 同意書視窗 (id=669) 重量級。"""
    if not hwnd:
        return False
    WM_COMMAND = 0x0111
    try:
        ok = _user32().PostMessageW(hwnd, WM_COMMAND, menu_id, 0)
        if not ok:
            logging.warning("PostMessageW WM_COMMAND menu_id=%s 失敗 hwnd=%s",
                            menu_id, hwnd)
            return False
        return True
    except Exception:
        logging.warning("PostMessageW WM_COMMAND menu_id=%s 例外 hwnd=%s",
                        menu_id, hwnd, exc_info=True)
        return False


def close_window(hwnd: int) -> None:
    try:
        WM_CLOSE = 0x0010
        _user32().PostMessageW(hwnd, WM_CLOSE, 0, 0)
    except Exception:
        logging.debug("[卡號] 關視窗失敗 hwnd=%s", hwnd, exc_info=True)


# ── 點按鈕（第二刀）──────────────────────────────────────────────────────────
def click_control_center(hwnd: int) -> bool:
    """【相容介面】等同 post_click_to_control(hwnd) — 不動滑鼠，送訊息點擊
    control 的 client center。原本用 pyautogui.click 會閃動滑鼠，已改成訊息。"""
    return post_click_to_control(hwnd)


def click_button_by_text(parent_hwnd: int, text: str) -> bool:
    """找 TButton text 完全等於 text → 用 PostMessage WM_LBUTTONDOWN/UP 觸發。

    不用 SendMessage BM_CLICK：對開啟 modal popup 的 button，BM_CLICK 是
    synchronous，會卡在 popup 的 modal message loop 直到 user 關閉。
    PostMessage 非同步立刻返回，popup 由 Delphi 後續處理，呼叫端用
    _wait_for_window poll 偵測。
    （實測 2026-05-18：SendMessage BM_CLICK 在「開立電子」卡了 73 秒）"""
    btn = find_descendant_by_class_text(parent_hwnd, "TButton", text)
    if not btn:
        return False
    return post_click_to_control(btn)


def click_button_normalized_text(parent_hwnd: int, target_text: str) -> int:
    """找 TButton：把 text 去除「所有」空白後 == 去除空白的 target → PostMessage 點擊。
    解決 Delphi 按鈕常見「完  成」「確  認」這種額外空格。
    回傳：點到的 hwnd (失敗 0)。"""
    target_norm = "".join(target_text.split())
    out = [0]

    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @EnumWindowsProc
    def cb(child, lparam):
        try:
            cls_buf = ctypes.create_unicode_buffer(64)
            _user32().GetClassNameW(child, cls_buf, 64)
            if cls_buf.value != "TButton":
                return True
            n = _user32().GetWindowTextLengthW(child)
            if n <= 0:
                return True
            t_buf = ctypes.create_unicode_buffer(n + 1)
            _user32().GetWindowTextW(child, t_buf, n + 1)
            if "".join(t_buf.value.split()) == target_norm:
                out[0] = child
                return False
        except Exception:
            pass
        return True

    _user32().EnumChildWindows(parent_hwnd, cb, 0)
    if out[0]:
        # [2026-07-26 審查] post_click_to_control 的回傳值原本被丟掉 —— 找得到按鈕、
        # 但 PostMessage 沒送成功(視窗剛被關掉、佇列滿)時仍回傳 hwnd,呼叫端
        # `if _click_button_normalized_text(...)` 就當成「已點」往下走,實際沒點到。
        # 送不出去就回 0,讓呼叫端走既有的失敗分支(重試/警告),不假裝成功。
        if not post_click_to_control(out[0]):
            logging.warning("[F11] 找到按鈕 %r(hwnd=%s)但點擊訊息送出失敗",
                            target_text, out[0])
            return 0
    return out[0]


# ── 前景 / 掃描（第二刀）────────────────────────────────────────────────────
def ensure_hospital_foreground(hwnd: int) -> None:
    """確保主程式視窗在前景，這樣後續 pyautogui.typewrite 才會打進去。
    SetForegroundWindow 在 admin 行程通常能成功。"""
    try:
        # 若已 minimize 先還原
        SW_RESTORE = 9
        if _user32().IsIconic(hwnd):
            _user32().ShowWindow(hwnd, SW_RESTORE)
        _user32().SetForegroundWindow(hwnd)
    except Exception:
        logging.debug("ensure_hospital_foreground 失敗", exc_info=True)


def scan_unknown_popups(known_classes: set, seen: dict, label: str) -> None:
    """[2026-05-22 v41/v42] F11 watcher 期間掃所有 visible top-level windows，
    若 class 不在已知清單就記下來。

    [v42] 為了不對醫院 app 送任何跨 process 訊息，全程只用 kernel-only API：
      - IsWindowVisible: kernel-only ✓
      - GetClassName: kernel-only ✓ (Windows 維護 class atom table)
      - GetWindowRect: kernel-only ✓
      - GetWindowText: 跨 process WM_GETTEXT ✗ (移除，title 不取)
    這樣 unknown scan 對醫院 app 是 **完全零訊息**。
    User 看到 log 中的 unknown class 後可用 抓取當前視窗結構.cmd 取得詳細資訊。
    """
    EnumWindowsProc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @EnumWindowsProc
    def cb(hwnd, lparam):
        try:
            if not _user32().IsWindowVisible(hwnd):
                return True
            if hwnd in seen:
                return True
            cls_buf = ctypes.create_unicode_buffer(64)
            _user32().GetClassNameW(hwnd, cls_buf, 64)
            cls = cls_buf.value
            if cls in known_classes:
                return True
            r = wintypes.RECT()
            if not _user32().GetWindowRect(hwnd, ctypes.byref(r)):
                return True
            w, h = r.right - r.left, r.bottom - r.top
            if w < 100 or h < 40:
                return True
            # [v42] 不再 GetWindowText — class + rect 已足夠識別 unknown popup
            seen[hwnd] = (cls, "", time.time())
            logging.warning(
                "[%s][unknown-popup] 偵測到未知 visible 視窗: class='%s' "
                "hwnd=%s rect=(%dx%d at %d,%d) — 若這擋住 F11 流程，請開 "
                "抓取當前視窗結構.cmd 拍 snapshot 給開發者",
                label, cls, hwnd, w, h, r.left, r.top)
        except Exception:
            pass
        return True

    try:
        _user32().EnumWindows(cb, 0)
    except Exception:
        pass
