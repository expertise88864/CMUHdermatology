# -*- coding: utf-8 -*-
"""通用視窗快照工具：抓「當前前景視窗」的完整結構 dump。

用法（雙擊 tools\抓取當前視窗結構.cmd，會自動提權）：
  1. 切到要 dump 的視窗（讓它變成前景，例如 同意書視窗、片語 popup ...）
  2. 跑 snapshot 工具 — 它會倒數 3 秒，給你切回去；接著抓最後一個前景視窗
  3. 輸出寫入 settings/snapshot_<timestamp>.txt
  4. 把該檔貼給 Claude

支援多視窗 workflow：在每個視窗階段各跑一次（檔名會帶時間戳避免覆蓋）。
"""
from __future__ import annotations

import ctypes
import json
import sys
import time
from ctypes import wintypes
from datetime import datetime
from pathlib import Path

user32 = ctypes.windll.user32

EnumWindowsProc = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _get_class(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _get_text(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _get_rect(hwnd: int) -> dict:
    r = wintypes.RECT()
    if user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return {"left": r.left, "top": r.top,
                "right": r.right, "bottom": r.bottom,
                "w": r.right - r.left, "h": r.bottom - r.top}
    return {}


def _enum_descendants(root_hwnd: int) -> list:
    """遞迴列出所有子孫。回傳 list of dicts。"""
    items = []
    visited = set()

    def _recurse(parent):
        if parent in visited:
            return
        visited.add(parent)
        children = []

        @EnumWindowsProc
        def cb(child, lp):
            children.append(child)
            return True

        user32.EnumChildWindows(parent, cb, 0)
        for ch in children:
            try:
                items.append({
                    "hwnd": ch,
                    "class": _get_class(ch),
                    "text": _get_text(ch),
                    "rect": _get_rect(ch),
                })
                _recurse(ch)
            except Exception:
                pass

    _recurse(root_hwnd)
    return items


def _dump_menu(hmenu: int, max_depth: int = 3, depth: int = 0) -> list:
    if not hmenu or depth > max_depth:
        return []
    GetMenuItemCount = user32.GetMenuItemCount
    GetMenuItemID = user32.GetMenuItemID
    GetSubMenu = user32.GetSubMenu
    GetMenuStringW = user32.GetMenuStringW
    items = []
    count = GetMenuItemCount(hmenu)
    if count < 0:
        return []
    for i in range(count):
        buf = ctypes.create_unicode_buffer(512)
        GetMenuStringW(hmenu, i, buf, 512, 0x400)  # MF_BYPOSITION
        cmd_id = GetMenuItemID(hmenu, i)
        sub = GetSubMenu(hmenu, i)
        it = {
            "pos": i, "text": buf.value,
            "id": cmd_id if cmd_id != 0xFFFFFFFF else None,
            "is_separator": (cmd_id == 0 and not buf.value and not sub),
            "is_submenu": bool(sub),
        }
        if sub:
            it["children"] = _dump_menu(sub, max_depth, depth + 1)
        items.append(it)
    return items


def main() -> int:
    print("=" * 60)
    print("  Foreground Window Snapshot Tool")
    print("=" * 60)
    print()
    print("This tool will snapshot whatever window is in the FOREGROUND")
    print("after a 3-second countdown.")
    print()
    print("USAGE:")
    print("  1. Switch to the target window (click on it to focus)")
    print("  2. Wait for the countdown to finish")
    print("  3. Output saves to: settings/snapshot_<timestamp>.txt")
    print()

    for i in range(5, 0, -1):
        sys.stdout.write(f"\r  Snapshotting in {i} seconds...  ")
        sys.stdout.flush()
        time.sleep(1)
    print()
    print()

    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        print("[ERROR] no foreground window")
        return 1

    target_class = _get_class(hwnd)
    target_title = _get_text(hwnd)
    target_rect = _get_rect(hwnd)
    print(f"Target: hwnd={hwnd}")
    print(f"  class = {target_class!r}")
    print(f"  title = {target_title!r}")
    print(f"  rect  = {target_rect}")
    print()

    hmenu = user32.GetMenu(hwnd)
    menu_items = _dump_menu(hmenu) if hmenu else []

    print("Enumerating descendants...")
    descendants = _enum_descendants(hwnd)
    print(f"  Found {len(descendants)} descendants")
    print()

    # Group by class for quick overview
    by_class = {}
    for d in descendants:
        by_class.setdefault(d["class"], []).append(d)
    print("=== Class distribution ===")
    for cls, items in sorted(by_class.items(), key=lambda x: -len(x[1])):
        print(f"  {cls:<30} x{len(items)}")
    print()

    # Show items with text (often the most useful)
    print("=== Descendants WITH text (likely labels/buttons) ===")
    with_text = [d for d in descendants if (d.get("text") or "").strip()]
    for d in with_text[:80]:
        r = d["rect"]
        txt = d["text"][:40]
        print(f"  hwnd={d['hwnd']:>10} {d['class']:<22} text={txt!r:<45} "
              f"({r.get('left'):>5},{r.get('top'):>4}) {r.get('w'):>4}x{r.get('h'):>3}")

    # Write JSON
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = Path(__file__).resolve().parent.parent / "settings"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / f"snapshot_{ts}.txt"

    out_path.write_text(json.dumps({
        "snapshot_time": ts,
        "target_hwnd": hwnd,
        "target_class": target_class,
        "target_title": target_title,
        "target_rect": target_rect,
        "menu_items": menu_items,
        "descendants_count": len(descendants),
        "descendants": descendants,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"✓ Snapshot saved to: {out_path}")
    print("  Send this file content to Claude.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        input("\nPress Enter to close...")
        sys.exit(1)
