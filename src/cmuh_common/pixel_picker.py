# -*- coding: utf-8 -*-
"""[O29] 視覺取座標：開全螢幕透明 overlay，使用者點擊位置 → 抓 X/Y/RGB。

用法：
    from cmuh_common.pixel_picker import pick_pixel
    pick_pixel(parent, on_done=lambda x, y, r, g, b: ...)

如果使用者按 ESC → on_done 不會被呼叫（取消）。
"""
from __future__ import annotations

import logging
import tkinter as tk
from typing import Callable, Optional

from cmuh_common.platform_win import get_primary_monitor_size, get_virtual_screen_rect


def _span_overlay_on_all_screens(overlay: tk.Toplevel) -> tuple:
    """讓 overlay 覆蓋整個虛擬桌面(所有螢幕)，而非只蓋主螢幕。

    回傳 (vx, vy, prim_w, prim_h)：
      vx/vy = 虛擬桌面原點(副螢幕在左/上時為負)，供把內容換算到 overlay 內座標；
      prim_w/prim_h = 主螢幕大小，供提示文字置於「主螢幕中央」(避免落在螢幕接縫)。
    """
    vx, vy, vw, vh = get_virtual_screen_rect()
    try:
        overlay.overrideredirect(True)
    except tk.TclError:
        pass
    try:
        overlay.geometry(f"{vw}x{vh}+{vx}+{vy}")
    except tk.TclError:
        try:
            overlay.attributes('-fullscreen', True)
        except tk.TclError:
            pass
    prim_w, prim_h = get_primary_monitor_size()
    if prim_w <= 0 or prim_h <= 0:
        prim_w, prim_h = vw, vh
    return vx, vy, prim_w, prim_h


def _place_on_primary(widget: tk.Widget, vx: int, vy: int,
                      prim_w: int, prim_h: int, rely: float) -> None:
    """把 widget 放在「主螢幕」的水平中央、垂直 rely 比例處(換算成 overlay 內座標)。"""
    widget.place(x=(prim_w // 2) - vx,
                 y=int(prim_h * rely) - vy,
                 anchor="center")


def pick_pixel(parent: tk.Misc, on_done: Callable[[int, int, int, int, int], None]) -> None:
    """進入像素拾取模式。

    parent: 觸發此操作的視窗（會暫時 withdraw 避免擋到，操作完恢復）。
    on_done: callback(x, y, r, g, b)
    """
    # 取得目前可見的 ancestor 鏈（splash, parent, parent.master ... root）
    windows_to_hide = []
    cur = parent
    seen = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        try:
            if cur.winfo_viewable():
                windows_to_hide.append(cur)
        except Exception:
            pass
        try:
            cur = cur.master
        except Exception:
            cur = None
    # 也要把 toplevel 全找一輪
    try:
        root = parent.winfo_toplevel()
        for child in root.tk.eval("winfo children .").split():
            pass  # 簡化：只 hide ancestors
    except Exception:
        pass

    for w in windows_to_hide:
        try:
            w.withdraw()
        except Exception:
            pass

    overlay = tk.Toplevel()
    # [雙螢幕] overlay 覆蓋整個虛擬桌面，使用者才能在「任一螢幕」上點選座標
    vx, vy, prim_w, prim_h = _span_overlay_on_all_screens(overlay)
    overlay.attributes('-topmost', True)
    overlay.attributes('-alpha', 0.30)  # 30% 不透明（讓使用者看得到背景）
    overlay.configure(bg="#000000")
    overlay.config(cursor='cross')
    overlay.focus_force()

    # 中央提示文字（置於主螢幕中央，避免落在兩螢幕接縫）
    info_label = tk.Label(
        overlay,
        text="🎯 移動滑鼠到目標位置後左鍵點擊\nESC 或右鍵 = 取消",
        font=("Microsoft JhengHei UI", 28, "bold"),
        fg="#FFFFFF",
        bg="#000000",
    )
    _place_on_primary(info_label, vx, vy, prim_w, prim_h, 0.45)

    # 即時座標顯示
    coord_label = tk.Label(
        overlay,
        text="(?, ?)  rgb=?",
        font=("Consolas", 16, "bold"),
        fg="#00E676",
        bg="#000000",
    )
    _place_on_primary(coord_label, vx, vy, prim_w, prim_h, 0.55)

    state = {"done": False, "last_pos": (-1, -1), "last_rgb": (0, 0, 0)}

    def restore_parents(after_pick: Optional[tuple] = None):
        try:
            overlay.destroy()
        except Exception:
            pass
        for w in windows_to_hide:
            try:
                w.deiconify()
            except Exception:
                pass
        if after_pick is not None:
            try:
                on_done(*after_pick)
            except Exception:
                logging.error("[pixel_picker] on_done 失敗", exc_info=True)

    def update_coord_display():
        if state["done"]:
            return
        try:
            x = overlay.winfo_pointerx()
            y = overlay.winfo_pointery()
            if (x, y) != state["last_pos"]:
                state["last_pos"] = (x, y)
                # 抓該點的 RGB（透過 overlay 30% alpha，但 ImageGrab 抓的是螢幕 framebuffer）
                try:
                    from PIL import ImageGrab
                    # all_screens=True：才能讀到副螢幕(座標可能為負)的像素
                    img = ImageGrab.grab(bbox=(x, y, x + 1, y + 1), all_screens=True)
                    px = img.getpixel((0, 0))
                    if isinstance(px, int):
                        r = g = b = px
                    else:
                        r, g, b = px[:3]
                    state["last_rgb"] = (r, g, b)
                except Exception:
                    r, g, b = state["last_rgb"]
                hex_color = f"#{r:02X}{g:02X}{b:02X}"
                coord_label.config(text=f"({x}, {y})  RGB=({r},{g},{b})  {hex_color}")
        except Exception:
            pass
        overlay.after(40, update_coord_display)

    def on_click(event):
        if state["done"]:
            return
        state["done"] = True
        x = event.x_root
        y = event.y_root
        # 抓 RGB
        try:
            # 注意：在 destroy overlay 後再抓，否則會抓到 overlay 的暗色
            r, g, b = state["last_rgb"]
        except Exception:
            r, g, b = 0, 0, 0
        # 先 destroy overlay 再回傳，避免半透明影響
        # 但如果先 destroy 再 grab，畫面已變→ 用上面 update 抓的 last_rgb
        restore_parents((x, y, int(r), int(g), int(b)))

    def on_cancel(_event=None):
        if state["done"]:
            return
        state["done"] = True
        restore_parents(None)

    overlay.bind("<Button-1>", on_click)
    overlay.bind("<Escape>", on_cancel)
    overlay.bind("<Button-3>", on_cancel)
    overlay.protocol("WM_DELETE_WINDOW", on_cancel)

    update_coord_display()


def pick_pixel_with_accurate_color(parent: tk.Misc,
                                   on_done: Callable[[int, int, int, int, int], None]) -> None:
    """精準色版：點擊後 overlay 立即消失再用 PIL ImageGrab 抓真實顏色（不受 30% 暗影響）。

    流程：使用者按 F8（或左鍵）→ overlay destroy → 短延遲後 grab 螢幕該點 → 回傳。
    """
    # 取得 ancestor 視窗鏈
    windows_to_hide = []
    cur = parent
    seen = set()
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        try:
            if cur.winfo_viewable():
                windows_to_hide.append(cur)
        except Exception:
            pass
        try:
            cur = cur.master
        except Exception:
            cur = None
    for w in windows_to_hide:
        try:
            w.withdraw()
        except Exception:
            pass

    # 先讓父視窗 hide → 等系統 100ms 重繪（讓 overlay 之外的內容露出來）
    parent.after(100, lambda: _show_picker_overlay(parent, windows_to_hide, on_done))


def _show_picker_overlay(parent, windows_to_hide, on_done):
    overlay = tk.Toplevel()
    # [雙螢幕] overlay 覆蓋整個虛擬桌面，使用者才能在「任一螢幕」上點選座標
    vx, vy, prim_w, prim_h = _span_overlay_on_all_screens(overlay)
    overlay.attributes('-topmost', True)
    overlay.attributes('-alpha', 0.10)  # 接近透明（10%）
    overlay.configure(bg="#000000")
    overlay.config(cursor='cross')
    overlay.focus_force()

    info_label = tk.Label(
        overlay,
        text="🎯 點擊目標位置｜ESC 取消",
        font=("Microsoft JhengHei UI", 22, "bold"),
        fg="#FFEB3B",
        bg="#000000",
    )
    _place_on_primary(info_label, vx, vy, prim_w, prim_h, 0.05)

    coord_label = tk.Label(
        overlay,
        text="移動滑鼠中...",
        font=("Consolas", 14, "bold"),
        fg="#00E676",
        bg="#000000",
    )
    _place_on_primary(coord_label, vx, vy, prim_w, prim_h, 0.10)

    state = {"done": False, "x": 0, "y": 0}

    def restore(after_pick=None):
        try:
            overlay.destroy()
        except Exception:
            pass
        # 等 100ms 讓 overlay 真正消失再抓 RGB
        if after_pick is not None:
            x, y = after_pick
            def grab_after():
                try:
                    from PIL import ImageGrab
                    # all_screens=True：才能讀到副螢幕(座標可能為負)的像素
                    img = ImageGrab.grab(bbox=(x, y, x + 1, y + 1), all_screens=True)
                    px = img.getpixel((0, 0))
                    if isinstance(px, int):
                        r = g = b = px
                    else:
                        r, g, b = px[:3]
                except Exception:
                    r, g, b = 0, 0, 0
                # 還原視窗
                for w in windows_to_hide:
                    try:
                        w.deiconify()
                    except Exception:
                        pass
                try:
                    on_done(x, y, int(r), int(g), int(b))
                except Exception:
                    logging.error("[pixel_picker] on_done 失敗", exc_info=True)
            parent.after(150, grab_after)
        else:
            for w in windows_to_hide:
                try:
                    w.deiconify()
                except Exception:
                    pass

    def update_pos():
        if state["done"]:
            return
        try:
            x = overlay.winfo_pointerx()
            y = overlay.winfo_pointery()
            state["x"], state["y"] = x, y
            coord_label.config(text=f"({x}, {y})  ← 點擊以選取此位置")
        except Exception:
            pass
        overlay.after(40, update_pos)

    def on_click(event):
        if state["done"]:
            return
        state["done"] = True
        restore((event.x_root, event.y_root))

    def on_cancel(_=None):
        if state["done"]:
            return
        state["done"] = True
        restore(None)

    overlay.bind("<Button-1>", on_click)
    overlay.bind("<Escape>", on_cancel)
    overlay.bind("<Button-3>", on_cancel)
    overlay.protocol("WM_DELETE_WINDOW", on_cancel)
    update_pos()
