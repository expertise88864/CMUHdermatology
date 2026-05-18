# -*- coding: utf-8 -*-
"""探測「中國醫藥大學附設醫院西醫門診醫師作業」主視窗的 Win32 結構。

用途：為了把熱鍵腳本改寫成「解析度無關」的版本，需要先抓出：
  1. 主視窗的 class name + 完整 title
  2. 主選單列各項目 (病史徵候 / 診斷 / 醫令 / ...) 的 menu handle + 內部 ID
  3. 醫令子選單裡每個項目（含 separator）的 ID — 特別是「代碼輸入」
  4. 視窗下方的醫令代碼輸入欄位（grid）的 class name + child window 結構

用法（從 cmd / PowerShell）：
  1. 打開「中國醫藥大學附設醫院西醫門診醫師作業」(systemftp 或同類)
  2. 切到有患者掛入、醫令代碼欄位看得到的狀態（如本探測使用的螢幕截圖）
  3. 跑這個 script：
       python scripts/probe_main_app_menu.py
  4. 輸出會印到 console，也會寫到 settings/main_app_menu_probe.txt
  5. 把該 .txt 內容貼給 Claude，他會根據結構寫對應的 SendMessage 程式碼

注意：不需要 admin。但建議在你日常使用主程式的環境跑（同一個使用者
session），才能列舉到主程式視窗。
"""
from __future__ import annotations

import ctypes
import json
import os
import sys
from ctypes import wintypes
from pathlib import Path

# === Win32 ===
user32 = ctypes.windll.user32

GetClassNameW = user32.GetClassNameW
GetWindowTextW = user32.GetWindowTextW
GetWindowTextLengthW = user32.GetWindowTextLengthW
IsWindowVisible = user32.IsWindowVisible
EnumWindows = user32.EnumWindows
GetMenu = user32.GetMenu
GetMenuItemCount = user32.GetMenuItemCount
GetSubMenu = user32.GetSubMenu
GetMenuItemID = user32.GetMenuItemID
GetMenuStringW = user32.GetMenuStringW
GetWindowThreadProcessId = user32.GetWindowThreadProcessId
EnumChildWindows = user32.EnumChildWindows

EnumWindowsProc = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

TARGET_TITLE_KEYWORDS = (
    "中國醫藥大學附設醫院西醫門診醫師作業",
    "中國醫藥大學附設醫院西醫門診",
    "西醫門診醫師作業",
    "醫師作業",
)


def _get_window_title(hwnd: int) -> str:
    n = GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _get_class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    GetClassNameW(hwnd, buf, 256)
    return buf.value


def _get_menu_string(hmenu: int, item_id: int, by_position: bool = True) -> str:
    """讀選單項目文字。by_position=True：item_id 是位置 (0-based)。"""
    MF_BYPOSITION = 0x00000400
    MF_BYCOMMAND = 0x00000000
    flag = MF_BYPOSITION if by_position else MF_BYCOMMAND
    # 先試小 buffer，不夠再放大
    buf = ctypes.create_unicode_buffer(512)
    GetMenuStringW(hmenu, item_id, buf, 512, flag)
    return buf.value


def find_target_windows() -> list:
    """列出所有可見、title 含目標關鍵字的視窗。"""
    found = []

    @EnumWindowsProc
    def cb(hwnd, lparam):
        try:
            if not IsWindowVisible(hwnd):
                return True
            title = _get_window_title(hwnd)
            if not title:
                return True
            for kw in TARGET_TITLE_KEYWORDS:
                if kw in title:
                    found.append({
                        "hwnd": hwnd,
                        "title": title,
                        "class": _get_class_name(hwnd),
                    })
                    break
        except Exception:
            pass
        return True

    EnumWindows(cb, 0)
    return found


def dump_menu(hmenu: int, depth: int = 0, max_depth: int = 4) -> list:
    """遞迴 dump menu 結構。回傳 list of dict。"""
    if not hmenu or depth > max_depth:
        return []
    items = []
    count = GetMenuItemCount(hmenu)
    if count < 0:
        return []
    for i in range(count):
        text = _get_menu_string(hmenu, i, by_position=True)
        cmd_id = GetMenuItemID(hmenu, i)
        # cmd_id == -1 (0xFFFFFFFF) 表示這是子選單（不是命令）；
        # cmd_id == 0 表示這是 separator
        sub = GetSubMenu(hmenu, i)
        item = {
            "pos": i,
            "text": text,
            "id": cmd_id if cmd_id != 0xFFFFFFFF else None,
            "is_separator": (cmd_id == 0 and not text and not sub),
            "is_submenu": bool(sub),
        }
        if sub:
            item["children"] = dump_menu(sub, depth + 1, max_depth)
        items.append(item)
    return items


def dump_children(hwnd: int, depth: int = 0, max_depth: int = 6,
                   max_per_level: int = 30) -> list:
    """列出視窗子控制項樹（用來找輸入欄位）。"""
    children = []
    count = [0]

    @EnumWindowsProc
    def cb(hchild, lparam):
        if count[0] >= max_per_level:
            return False
        count[0] += 1
        try:
            cls = _get_class_name(hchild)
            title = _get_window_title(hchild)
            entry = {
                "hwnd": hchild,
                "class": cls,
                "title": title[:80],
            }
            if depth < max_depth:
                grandchildren = dump_children(hchild, depth + 1, max_depth, max_per_level)
                if grandchildren:
                    entry["children"] = grandchildren
            children.append(entry)
        except Exception:
            pass
        return True

    EnumChildWindows(hwnd, cb, 0)
    return children


def print_menu_tree(items: list, indent: int = 0, parent_path: str = "") -> list:
    """印出選單樹，回傳所有 path → id 對應供搜尋。"""
    paths = []
    for it in items:
        path = f"{parent_path}/{it['pos']}"
        prefix = "  " * indent
        if it["is_separator"]:
            print(f"{prefix}[{it['pos']:>3}] ─────────── (separator)")
            continue
        marker = " ▶" if it["is_submenu"] else ""
        id_str = f"id={it['id']}" if it["id"] is not None else "id=---"
        text = it["text"] or "(空白)"
        print(f"{prefix}[{it['pos']:>3}] {text!r:30s} {id_str}{marker}  path={path}")
        if it["text"]:
            paths.append({
                "path": path,
                "text": it["text"],
                "id": it["id"],
                "is_submenu": it["is_submenu"],
            })
        if "children" in it:
            paths.extend(print_menu_tree(it["children"], indent + 1, path))
    return paths


def main() -> int:
    print("=" * 70)
    print("  Main App Window + Menu Probe")
    print("=" * 70)
    print()

    windows = find_target_windows()
    if not windows:
        print("[錯誤] 找不到目標視窗。確認以下視窗已開啟：")
        for kw in TARGET_TITLE_KEYWORDS:
            print(f"   title 含 {kw!r}")
        return 1

    print(f"找到 {len(windows)} 個符合的視窗：")
    for i, w in enumerate(windows):
        print(f"  [{i}] hwnd={w['hwnd']}  class={w['class']!r}  title={w['title']!r}")
    print()

    # 取第一個（通常只有一個）
    w = windows[0]
    hwnd = w["hwnd"]
    print(f"=== 處理 hwnd={hwnd} ===")
    print()

    hmenu = GetMenu(hwnd)
    if not hmenu:
        print("[警告] 此視窗沒有 GetMenu 結果（可能 menu 在子視窗）")
        print("       仍嘗試列出子視窗結構供分析。")
        menu_items = []
        all_paths = []
    else:
        print(f"主選單 HMENU = {hmenu}")
        print()
        print("【主選單結構】（path = /主選單位置/子選單位置/...）")
        print("-" * 70)
        menu_items = dump_menu(hmenu)
        all_paths = print_menu_tree(menu_items)

    # 找 「代碼輸入」
    print()
    print("=" * 70)
    print("【尋找：代碼輸入】")
    matches = [p for p in all_paths if "代碼輸入" in (p["text"] or "")]
    if matches:
        for m in matches:
            print(f"  ✓ path={m['path']}  id={m['id']}  text={m['text']!r}")
    else:
        print("  ✗ 沒找到「代碼輸入」")
    print()
    print("【尋找：醫令】")
    yiling = [p for p in all_paths if "醫令" == p["text"]]
    if yiling:
        for m in yiling:
            print(f"  ✓ path={m['path']}  is_submenu={m['is_submenu']}")
    else:
        print("  ✗ 沒找到「醫令」頂層選單")

    # Dump 子視窗結構（前 30 個 children）
    print()
    print("=" * 70)
    print("【子視窗結構】（前幾層，找輸入欄位用）")
    children = dump_children(hwnd, max_depth=3, max_per_level=15)
    def _print_ch(items, indent=0):
        for c in items:
            prefix = "  " * indent
            print(f"{prefix}hwnd={c['hwnd']}  class={c['class']!r}  title={c['title']!r}")
            if "children" in c:
                _print_ch(c["children"], indent + 1)
    _print_ch(children)

    # 寫到檔案
    out_dir = Path(__file__).resolve().parent.parent / "settings"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "main_app_menu_probe.txt"

    # Re-print to file（用簡單 redirect 法：暫存 stdout）
    import io
    from contextlib import redirect_stdout
    buf = io.StringIO()
    with redirect_stdout(buf):
        print("Main App Window + Menu Probe")
        print("=" * 70)
        print(json.dumps({
            "windows_found": windows,
            "target_hwnd": hwnd,
            "menu_items": menu_items,
            "matches_代碼輸入": matches,
            "matches_醫令": yiling,
            "children_tree": children,
        }, ensure_ascii=False, indent=2))
    out_path.write_text(buf.getvalue(), encoding="utf-8")

    print()
    print("=" * 70)
    print(f"✓ 完整資料已寫入：{out_path}")
    print("  請把這個檔案的內容貼給 Claude，他會根據結構寫 F3/F4 程式碼。")
    print("=" * 70)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("中斷")
        sys.exit(130)
    except Exception:
        import traceback
        traceback.print_exc()
        input("\n按 Enter 結束...")
        sys.exit(1)
