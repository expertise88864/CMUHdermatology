# -*- coding: utf-8 -*-
"""自製「全黑螢幕保護」覆蓋層。

★[2026-07-30 使用者] 為什麼需要這個★
需求是「閒置 15 分鐘後螢幕關掉」。既有做法有兩層,兩層都【不可回讀】:

  1. `powercfg /change monitor-timeout-*` 15 分鐘 —— 被任何程式的 DISPLAY
     power request(wake lock)壓住就永遠不會關。
  2. 閒置滿 15 分鐘廣播 `WM_SYSCOMMAND / SC_MONITORPOWER=2` —— 這個訊息送出後,
     只要系統上仍有 DISPLAY request,Windows 會【立刻把螢幕點回來】;而 Win32
     沒有給一般行程一個簡單的「螢幕現在是開還是關」查詢 API(要 monitor 視窗 +
     `RegisterPowerSettingNotification` 才拿得到 `GUID_CONSOLE_DISPLAY_STATE`)。
     舊版送完就 log「已強制關閉螢幕」—— 那是【講程式不確知的事】,而實機上螢幕
     根本沒關,log 卻一直說關了,於是這個問題查了兩次都查不出來。

使用者定案:「或是 15 分鐘後進入全黑的螢幕保護程式也可以」。
一個【自己畫的全黑視窗】不依賴電源管理、不受 wake lock 影響,而且**可以回讀**
(`winfo_ismapped()`),所以我們能誠實地說出「這次到底黑了沒有」。
原本的 SC_MONITORPOWER 仍然照送(真的關掉更省電),只是不再假設它有效。

★安全設計(這是診間機,覆蓋全螢幕的東西必須永遠能退場)★

* **狀態是【算出來的】,不是記下來的。** 視窗不存在/不可見就一定回 False。
  若拿一個布林旗標記狀態,任何一條例外路徑都可能把旗標留在 True,結果所有
  F1-F12 從此全部失效而沒人知道原因。
* **兩個查詢入口,看你在哪條緒**：`active`（Tk 主緒專用,用 winfo_*）、
  `active_from_any_thread()`（純 Win32 + IsWindowVisible）。熱鍵閘門在 `keyboard` 的
  hook 緒上跑,**一定要用後者** —— 從別的緒呼叫 tkinter 會拋
  `RuntimeError: main thread is not in main loop`,而閘門把例外當成「沒黑幕」
  → 整個閘門在正式環境從不生效（外審第 1 輪抓到；我的測試因為在 Tk 緒上
  呼叫而全綠）。
* **自動化執行中一律不黑屏。** 主程式有「螢幕擷取 + OCR」的路徑(F2/F3 照光卡號),
  一個 topmost 黑視窗會讓它擷到全黑。跨行程的自動化不受影響(會診查詢用
  PrintWindow,被遮住也能擷取;打卡走 Selenium),只有本行程的需要擋。
* **三條退場路徑,任一條都夠**:(a) Tk 事件(按鍵/點擊/滾輪/滑鼠移動);
  (b) 每 250ms 輪詢 `GetLastInputInfo`,一有輸入就收(不管事件有沒有送到我們);
  (c) 自動化開始跑就收。
* **會搶焦點(`focus_force`)**,好讓喚醒的那一下按鍵打在黑幕上而不是打進病歷;
  收起來時把焦點還給原本的前景視窗。喚醒鍵同時也被熱鍵閘門吃掉 —— 否則醫師為了
  喚醒螢幕按的那一下 F1/F9 會在他還看不見畫面時就對 HIS 寫入。
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time

# GetSystemMetrics 索引:整個虛擬桌面（含所有螢幕）。單用 -fullscreen 只蓋一個螢幕,
# 診間有雙螢幕機器 → 另一台仍亮著顯示病歷。
_SM_XVIRTUALSCREEN = 76
_SM_YVIRTUALSCREEN = 77
_SM_CXVIRTUALSCREEN = 78
_SM_CYVIRTUALSCREEN = 79

POLL_MS = 250                 # 失效保險輪詢間隔
INPUT_FRESH_SEC = 2.0         # 閒置低於這個秒數 → 判定「剛剛有人碰了」

# ★[2026-07-30 外審第 2 輪] 喚醒 token 的失效保險期限★
#   熱鍵回呼（`keyboard` hook 緒）與 Tk 的 <Key> 處理是【並行】的：同一下按鍵，
#   Tk 可能先把黑幕拆掉、HWND 清掉，熱鍵回呼才去問閘門 → 看到「沒黑幕」而把
#   F1/F9 打進已經回到前景的 HIS。因此閘門除了看「現在黑著嗎」，還要看
#   「剛剛才黑過嗎」。
#   ★[第 3 輪] 那是一張【一次性 token】，不是時間窗★ —— 時間窗會把 1.5 秒內的
#   每一下 F1 都吃掉，醫師按第二下也沒反應。token 由第一個熱鍵消耗，之後正常放行；
#   這個秒數只剩失效保險：沒人來領就自動過期，不會累積到下一次黑幕。
WAKE_GRACE_SEC = 1.5

# ★[2026-07-30 外審第 2 輪] 滑鼠飄移門檻 ——【兩條路徑用同一個】★
#   第一版只給 Tk <Motion> 用，但失效保險輪詢看 `GetLastInputInfo`，而它對任何
#   位移（含 1px）都更新 → 門檻永遠輪不到。我第二版乾脆把門檻整個拿掉，那是
#   【改掉需求】而不是修矛盾。現在兩條路徑都拿【實際游標位置】比對同一個門檻；
#   鍵盤／按鍵則以「有新輸入但游標根本沒動」辨識。
MOTION_TOLERANCE_PX = 25
#   ★[第 3 輪] 判斷的單位是「不同的輸入事件」，不是「輪數」★
#   `GetLastInputInfo` 在一次輸入之後會【持續兩秒都算 fresh】。我上一版按輪數計，
#   於是一次 1px 飄移在下一輪就被當成「游標沒動 → 鍵盤輸入」而收掉黑幕 —— 門檻
#   只是把收黑幕延後 250ms。
#   ★[第 4 輪] 事件身分拿【原始 last-input tick】，不是推測的★
#   第 3 輪我用「閒置秒數有沒有變小」推測，那不可靠：上一輪量到 0.05、下一個 250ms
#   區間初有人敲鍵盤、這一輪量到 0.20 —— 0.20 不小於 0.05，那下鍵盤就被漏掉了，
#   而 Tk 拿不到焦點正是這條輪詢存在的理由。見 `last_input_tick()`。
#   ★硬上限★：門檻再怎麼巧，也不可出現「黑幕收不掉」——那是這支模組最不能有的
#   失敗模式。連續這麼多個【不同事件】都判不出結果，就不再判斷，一律收起來。
FRESH_INPUT_EVENTS_BEFORE_FORCE_HIDE = 8


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def last_input_tick() -> "int | None":
    """`GetLastInputInfo` 的原始 `dwTime`（毫秒 tick）。查不到回 None。

    ★[2026-07-30 外審第 4 輪] 為何要拿原始 tick★
    上一版用「閒置秒數有沒有變小」推測「是不是新的輸入事件」，那不可靠：
    上一輪量到 idle=0.05，下一個 250ms 區間初有人敲了鍵盤，這一輪量到
    idle=0.20 —— 0.20 不小於 0.05，於是那下鍵盤被漏掉；而 Tk 拿不到焦點正是
    這條輪詢存在的理由 → 黑幕收不掉。
    `dwTime` 是【事件本身的時間】，只要它變了就是新事件，不需要推論。
    """
    try:
        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(lii)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return None
        return int(lii.dwTime)
    except Exception:
        return None


def cursor_pos() -> "tuple | None":
    """目前游標位置（螢幕座標）。查不到回 None。"""
    class _P(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    try:
        p = _P()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(p)):
            return None
        return (int(p.x), int(p.y))
    except Exception:
        return None


def _moved_beyond(a, b, tolerance: int = MOTION_TOLERANCE_PX) -> bool:
    """兩個游標位置是否相距超過門檻。任一個是 None（查不到）→ 視為【有移動】。

    查不到時偏向「有移動」＝偏向收黑幕。反過來（查不到就當沒動）會讓一次
    `GetCursorPos` 失敗變成「黑幕收不掉」。
    """
    if a is None or b is None:
        return True
    return abs(a[0] - b[0]) > tolerance or abs(a[1] - b[1]) > tolerance


def virtual_screen_rect() -> tuple:
    """(x, y, w, h) 涵蓋所有螢幕。取不到時回主螢幕大小（絕不回 0 大小）。"""
    try:
        g = ctypes.windll.user32.GetSystemMetrics
        x, y = g(_SM_XVIRTUALSCREEN), g(_SM_YVIRTUALSCREEN)
        w, h = g(_SM_CXVIRTUALSCREEN), g(_SM_CYVIRTUALSCREEN)
        if w > 0 and h > 0:
            return (x, y, w, h)
        return (0, 0, g(0) or 1920, g(1) or 1080)
    except Exception:
        logging.debug("[黑幕] 取虛擬桌面尺寸失敗，退回 1920x1080", exc_info=True)
        return (0, 0, 1920, 1080)


class ScreenBlackout:
    """全黑覆蓋層。★所有方法都只能在 Tk 主緒呼叫★

    idle_seconds_fn: () -> float | None（None＝查不到閒置時間）
    busy_fn:         () -> bool（True＝本行程正在跑自動化 → 不黑屏／立刻收起）
    rect_fn:         () -> (x, y, w, h)（可注入，測試用）
    """

    def __init__(self, root, *, idle_seconds_fn, busy_fn,
                 rect_fn=virtual_screen_rect,
                 last_input_tick_fn=last_input_tick):
        self._root = root
        self._idle_seconds_fn = idle_seconds_fn
        self._busy_fn = busy_fn
        self._rect_fn = rect_fn
        self._last_input_tick_fn = last_input_tick_fn
        self._win = None
        # 黑幕視窗的 HWND。熱鍵閘門在【非 Tk 緒】上跑，只能靠這個 + Win32
        # 查狀態（見 `active_from_any_thread`）。int 的讀寫在 CPython 是原子的。
        self._hwnd = 0
        self._poll_id = None
        self._prev_foreground = 0
        # 一次性的喚醒 token（monotonic 時間戳，0＝沒有）。熱鍵回呼在別的緒上
        # 消耗它，所以要上鎖（這把鎖與 Tk 無關，不會引入跨緒碰 Tk 的問題）。
        self._wake_lock = threading.Lock()
        self._wake_token = 0.0
        self._cursor_origin = None  # 黑幕開始時的游標位置
        self._last_cursor = None    # 上一輪輪詢看到的游標位置
        self._last_tick = None      # 上一輪看到的 last-input tick（事件身分）
        # 這一次黑幕期間已經吃掉過熱鍵嗎（★外審第 4 輪★）。
        # hook 緒先跑的情形：第一下 F1 看到黑幕還在 → 擋下但沒消耗 token；
        # Tk 接著拆窗又發一張新 token → 第二下 F1 也被吃掉。有這個旗標就不會。
        self._eaten_this_blackout = False
        self._fresh_events = 0      # 連續幾個【不同的】輸入事件都判不出結果

    # ── 狀態 ────────────────────────────────────────────────────────────────
    @property
    def active(self) -> bool:
        """★算出來的,不是記下來的★ 見模組 docstring 的安全設計第一條。

        ★只能在 Tk 主緒問★（會呼叫 winfo_*）。熱鍵回呼那種非 Tk 緒請用
        `active_from_any_thread()`。
        """
        win = self._win
        if win is None:
            return False
        try:
            return bool(win.winfo_exists()) and bool(win.winfo_ismapped())
        except Exception:
            return False

    def active_from_any_thread(self) -> bool:
        """黑幕是不是正蓋著 —— ★可以從任何緒問★（純 Win32，不碰 Tk）。

        ★[2026-07-30 外審第 1 輪] 為什麼一定要有這個★
        熱鍵閘門是在 `keyboard` 的 hook 緒上跑的，不是 Tk 主緒。`active` 會呼叫
        `winfo_exists()`／`winfo_ismapped()`，從別的緒呼叫 tkinter 會直接拋
        `RuntimeError: main thread is not in main loop` —— 而閘門把任何例外都當成
        「沒黑幕」→ **整個閘門在正式環境完全不生效**，而我的測試因為是在 Tk 緒上
        呼叫所以全綠（測試給假信心）。

        用 `_create()` 時記下的 HWND + `IsWindowVisible`：
        * 看 `IsWindowVisible` 而不是只看 `IsWindow` —— 這台機器上「視窗只是被
          Hide」而誤判的坑踩過一次（2026-07-27 診間事故，F9/F10 全面卡住）。
        * HWND 在 `_destroy()` 裡是【destroy 之後】才清掉的：先清會出現「黑幕還蓋著
          但閘門已放行」的空窗，那時的按鍵就會對 HIS 動作。
        ★這是【純查詢】★：不會消耗喚醒 token。熱鍵閘門請用 `consume_wake_gate()`。

        回 False 是刻意的：查不到就放行熱鍵。反過來（查不到就擋）會讓一次
        Win32 失敗把所有 F1-F12 永久鎖死，那比「黑幕期間漏擋一次」嚴重得多。
        """
        hwnd = self._hwnd
        if not hwnd:
            return False
        try:
            u = ctypes.windll.user32
            return bool(u.IsWindow(hwnd)) and bool(u.IsWindowVisible(hwnd))
        except Exception:
            logging.debug("[黑幕] IsWindowVisible 查詢失敗（視為沒有黑幕）",
                          exc_info=True)
            return False

    def consume_wake_gate(self) -> bool:
        """熱鍵閘門專用：這一下熱鍵要不要吃掉？★可以從任何緒呼叫★

        ★[2026-07-30 外審第 2 輪] 為什麼不能只看 HWND★
        熱鍵回呼（`keyboard` hook 緒）與 Tk 的 <Key> 處理是並行的：同一下按鍵，Tk 可能
        先把黑幕拆掉、HWND 清掉，熱鍵回呼才來問 → 只看 HWND 就會放行，那一下 F1/F9 就
        打進【已經回到前景的 HIS】，而醫師還沒看到畫面。

        ★[2026-07-30 外審第 3 輪] 為什麼是【一次性 token】而不是時間窗★
        我上一版只看「1.5 秒內黑過」→ 醫師按 F1 喚醒、馬上再按一次 F1，第二下也被吃掉，
        變成「要等一下才有反應」。喚醒的那一下只有【一下】，所以用一次性 token：
        第一個非 F12 熱鍵把它消耗掉，之後就正常放行。`WAKE_GRACE_SEC` 只剩失效保險
        （token 沒人來領時自動過期，不會累積到下一次黑幕）。
        """
        if self.active_from_any_thread():
            # ★[外審第 4 輪] 記下「這次黑幕已經吃掉一下熱鍵」★
            #   否則 hook 緒先跑的順序會吃掉兩下：這一下擋了但沒消耗 token，
            #   Tk 接著拆窗又發一張新 token → 下一下 F1 又被吃掉。
            # ★[外審第 5 輪] 同時把 token 清掉★
            #   `active_from_any_thread()` 是在鎖【外面】問的：`_destroy()` 可能剛好
            #   卡在這中間跑完 —— 那時 `_eaten_this_blackout` 還沒設起來，所以它照樣
            #   發了一張 token。只設旗標而不清 token，那張孤兒 token 會把下一下熱鍵
            #   也吃掉。這一下已經被擋了，就是「喚醒的那一下」，token 沒有存在意義。
            with self._wake_lock:
                self._eaten_this_blackout = True
                self._wake_token = 0.0
            return True          # 黑幕還蓋著 → 一律擋（這一下就是喚醒的那一下）
        with self._wake_lock:
            token = self._wake_token
            if not token:
                return False
            self._wake_token = 0.0                    # ★一次性★
            try:
                fresh = (time.monotonic() - token) < WAKE_GRACE_SEC
            except Exception:
                fresh = False
            if not fresh:
                logging.debug("[黑幕] 喚醒 token 已過期（沒吃這一下熱鍵）")
            return fresh

    # ── 顯示 ────────────────────────────────────────────────────────────────
    def show(self) -> bool:
        """顯示黑幕。★回傳的是【回讀結果】★（真的 mapped 才回 True）。"""
        if self._busy_fn():
            logging.info("[黑幕] 本行程正在跑自動化 → 本輪不黑屏")
            return False
        if self.active:
            return True
        try:
            self._create()
        except Exception:
            logging.warning("[黑幕] 建立黑幕視窗失敗（本輪放棄）", exc_info=True)
            self._destroy()
            return False
        mapped = self.active
        if not mapped:
            # 沒 mapped 就不能留一個半死的視窗在那裡（也不能讓熱鍵閘門以為黑著）
            logging.warning("[黑幕] 黑幕視窗沒有顯示出來（回讀 ismapped=False）→ 收回")
            self._destroy()
        return mapped

    def _create(self) -> None:
        import tkinter as tk

        try:
            self._prev_foreground = int(
                ctypes.windll.user32.GetForegroundWindow() or 0)
        except Exception:
            self._prev_foreground = 0

        x, y, w, h = self._rect_fn()
        win = tk.Toplevel(self._root)
        self._win = win
        win.overrideredirect(True)          # 無標題列，才能真正蓋滿虛擬桌面
        win.configure(bg="black", cursor="none")
        # ★[2026-07-30 外審第 1 輪] 偏移量必須帶正負號★
        #   副螢幕放在主螢幕左邊/上方時，虛擬桌面原點是負的（x=-1920）。
        #   `f"...+{x}+{y}"` 會組出 `3840x1080+-1920+0` 這種不合法的幾何字串 →
        #   `_create()` 拋例外 → 黑幕在【雙螢幕常見排列】下永遠不會出現。
        win.geometry(f"{w}x{h}{x:+d}{y:+d}")
        win.attributes("-topmost", True)
        # ★不用 grab_set★：抓住輸入的話，黑幕若沒收乾淨會把整台機器鎖死。
        for seq in ("<Key>", "<Button>", "<MouseWheel>"):
            win.bind(seq, self._on_wake)          # 按鍵/點擊/滾輪＝明確的人為動作
        win.bind("<Motion>", self._on_motion)     # 滑鼠要過門檻（見 MOTION_TOLERANCE_PX）
        win.protocol("WM_DELETE_WINDOW", self._on_wake)
        self._cursor_origin = cursor_pos()
        self._last_cursor = self._cursor_origin
        self._last_tick = self._last_input_tick_fn()
        self._fresh_events = 0
        with self._wake_lock:
            self._eaten_this_blackout = False
        win.update_idletasks()
        try:
            self._hwnd = int(win.winfo_id())
        except Exception:
            # 沒有 HWND 就沒有熱鍵閘門（閘門只能從非 Tk 緒用 Win32 查）→ 寧可不黑屏
            logging.warning("[黑幕] 拿不到黑幕的 HWND → 熱鍵閘門會失效，本輪不黑屏",
                            exc_info=True)
            raise
        try:
            win.focus_force()               # 喚醒的那一下按鍵打在黑幕上，不打進病歷
        except Exception:
            logging.debug("[黑幕] focus_force 失敗（仍以輪詢收場）", exc_info=True)
        self._schedule_poll()

    # ── 退場 ────────────────────────────────────────────────────────────────
    def _on_wake(self, _event=None) -> None:
        self.hide(reason="使用者輸入")

    def _on_motion(self, event=None) -> None:
        """滑鼠移動要超過門檻才收 —— 與失效保險輪詢用【同一個】門檻與同一個原點。

        位置優先取事件裡的 `x_root/y_root`（測試可注入），沒有才問 `GetCursorPos`。
        """
        pos = None
        if event is not None:
            xr, yr = getattr(event, "x_root", None), getattr(event, "y_root", None)
            if isinstance(xr, int) and isinstance(yr, int):
                pos = (xr, yr)
        if pos is None:
            pos = cursor_pos()
        if self._cursor_origin is None:
            self._cursor_origin = pos
            self._last_cursor = pos
            return
        if _moved_beyond(self._cursor_origin, pos):
            self.hide(reason="滑鼠移動")

    def hide(self, *, reason: str = "") -> None:
        was = self.active
        self._destroy()
        if was:
            logging.info("[黑幕] 已收起黑幕%s", f"（{reason}）" if reason else "")
        self._restore_foreground()

    def _destroy(self) -> None:
        self._cancel_poll()
        win, self._win = self._win, None
        if win is None:
            self._hwnd = 0
            return
        try:
            win.destroy()
        except Exception:
            logging.debug("[黑幕] destroy 失敗（已放棄引用）", exc_info=True)
        # ★HWND 要在 destroy 【之後】才清★ 先清會出現「黑幕還蓋著、但熱鍵閘門
        # 已放行」的空窗，那一瞬的按鍵就會對 HIS 動作。destroy 失敗時也要清，
        # 不然閘門會卡在「黑著」（IsWindowVisible 還是 True）而熱鍵全死。
        self._hwnd = 0
        # 發一張【一次性】的喚醒 token：HWND 沒了之後，靠它把「喚醒的那一下」熱鍵
        # 吃掉（外審第 2 輪的競態）。只有真的建過視窗才發。
        # 一次性而非時間窗：喚醒只有一下，第二下 F1 必須正常動作（外審第 3 輪）。
        with self._wake_lock:
            if self._eaten_this_blackout:
                # 這次黑幕期間已經有一下熱鍵被吃掉（hook 緒先跑）→ 不再發新
                # token，否則同一次喚醒會吃掉兩下（外審第 4 輪）。
                self._wake_token = 0.0
            else:
                self._wake_token = time.monotonic()

    def _restore_foreground(self) -> None:
        hwnd, self._prev_foreground = self._prev_foreground, 0
        if not hwnd:
            return
        try:
            if ctypes.windll.user32.IsWindow(hwnd):
                ctypes.windll.user32.SetForegroundWindow(hwnd)
        except Exception:
            logging.debug("[黑幕] 還原前景視窗失敗", exc_info=True)

    # ── 失效保險輪詢 ────────────────────────────────────────────────────────
    def _schedule_poll(self) -> None:
        try:
            self._poll_id = self._root.after(POLL_MS, self._poll)
        except Exception:
            # 排不到 after → 黑幕就沒有失效保險了，寧可不黑屏
            logging.warning("[黑幕] 無法排程失效保險輪詢 → 立刻收起黑幕",
                            exc_info=True)
            self._poll_id = None
            self._destroy()

    def _cancel_poll(self) -> None:
        poll_id, self._poll_id = self._poll_id, None
        if poll_id is None:
            return
        try:
            self._root.after_cancel(poll_id)
        except Exception:
            logging.debug("[黑幕] after_cancel 失敗（略過）", exc_info=True)

    def _poll(self) -> None:
        """每 250ms：有人碰了鍵鼠、或自動化開始跑 → 收黑幕。

        這是【不依賴 Tk 事件送不送到我們】的那條退場路徑：overrideredirect 視窗
        在某些情況下拿不到焦點，只靠 <Key> 綁定會讓黑幕收不掉。
        """
        self._poll_id = None
        if not self.active:
            return
        if self._busy_fn():
            self.hide(reason="自動化開始執行")
            return
        idle = self._idle_seconds_fn()
        if idle is None:
            # ★查不到閒置時間就收起來★ 黑幕是「我確定沒人在用」才該蓋著的東西；
            # 一旦失去那個依據，繼續蓋著＝醫師回來卻看不到病歷。收起來的最壞後果只是
            # 「螢幕沒關」——那就是修好之前的既有狀態，遠優於擋住臨床工作。
            self.hide(reason="查不到閒置時間")
            return
        # ★[外審第 3/4 輪] 只在【新的輸入事件】上判斷，而事件身分拿原始 tick★
        #   `GetLastInputInfo` 在一次輸入之後會持續兩秒都算 fresh，所以不能每輪都重新
        #   判一次（不然一次 1px 飄移下一輪就被當成「游標沒動 ⇒ 鍵盤」）；
        #   也不能用「閒置秒數有沒有變小」推測（上一輪 0.05、這一輪 0.20 的新鍵盤
        #   事件會被漏掉，而 Tk 拿不到焦點正是這條輪詢存在的理由）。
        #   `dwTime` 是事件本身的時間，變了就是新事件 —— 不需要推論。
        tick = self._last_input_tick_fn()
        prev_tick, self._last_tick = self._last_tick, tick
        if tick is None or prev_tick is None:
            # 拿不到 tick 就不推測：有新輸入就直接收黑幕（寧可多收，不可收不掉）。
            if idle < INPUT_FRESH_SEC:
                self.hide(reason="偵測到輸入（拿不到 last-input tick）")
                return
            self._schedule_poll()
            return
        new_event = idle < INPUT_FRESH_SEC and tick != prev_tick
        if not new_event:
            self._schedule_poll()
            return

        # 有新輸入 —— 是「人回來了」還是「滑鼠飄了一格」？拿實際游標位置判斷，
        # 用的是跟 <Motion> 完全相同的門檻與同一個原點（兩條路徑一致）。
        self._fresh_events += 1
        now_cursor = cursor_pos()
        moved = _moved_beyond(self._cursor_origin, now_cursor)
        still = (self._last_cursor is not None and now_cursor is not None
                 and self._last_cursor == now_cursor)
        self._last_cursor = now_cursor
        if moved:
            self.hide(reason="滑鼠移動")
            return
        if still:
            # 游標一格都沒動卻有新輸入 → 鍵盤或按鍵（沒有經過我們的 Tk 綁定）
            self.hide(reason="鍵盤/按鍵輸入")
            return
        if self._fresh_events >= FRESH_INPUT_EVENTS_BEFORE_FORCE_HIDE:
            # ★硬上限★ 判不出來就不判了 —— 絕不可出現「黑幕收不掉」。
            self.hide(reason="持續有輸入（不再判斷是否飄移）")
            return
        self._schedule_poll()
