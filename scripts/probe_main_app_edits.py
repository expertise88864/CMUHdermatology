# -*- coding: utf-8 -*-
"""擴充 probe：dump 主程式 (TFopdmain) 所有 Edit/TEdit/Combo 子視窗的
hwnd + class + text + 螢幕位置。

用途：找出「療程」輸入欄位的 hwnd，讓 F4 可以 SendMessage 設定它（不用
寫死座標）。

用法（雙擊「探測療程欄位.cmd」，會自動提權跑此 .py）：
  1. 開啟主程式並掛入患者（畫面顯示像截圖：療程欄目前值 = "3"）
  2. 跑 probe
  3. 把 settings/main_app_edits_probe.txt 內容貼給 Claude
  4. Claude 從輸出找出「療程」是哪個 hwnd（會是 text="3" 且 y 在頂部
     header 區域、x 在中段、寬度短的 Edit）

注意：本 script 不會修改任何欄位，只是讀取。
"""
from __future__ import annotations

import ctypes
import json
import sys
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.windll.user32

TARGET_CLASS = "TFopdmain"
TARGET_TITLE_KW = "西醫門診醫師作業"

# 視為「可能是輸入欄」的 class name 前綴／子字串
EDIT_CLASS_PATTERNS = (
    "edit", "tedit", "tdbedit", "tcombo", "tdbcombo", "tdblookup",
    "tmaskedit", "tnumedit", "tlabel", "tdblabel",
)

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


def _find_target() -> int:
    found = [0]

    @EnumWindowsProc
    def cb(hwnd, lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            if _get_class(hwnd) != TARGET_CLASS:
                return True
            if TARGET_TITLE_KW in _get_text(hwnd):
                found[0] = hwnd
                return False
        except Exception:
            pass
        return True

    user32.EnumWindows(cb, 0)
    return found[0]


def _enum_all_descendants(root_hwnd: int) -> list:
    """遞迴列出 root_hwnd 的所有子孫視窗，回傳 [(hwnd, class, text, rect)]。"""
    all_items = []
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
                all_items.append({
                    "hwnd": ch,
                    "class": _get_class(ch),
                    "text": _get_text(ch),
                    "rect": _get_rect(ch),
                })
                _recurse(ch)
            except Exception:
                pass

    _recurse(root_hwnd)
    return all_items


def main() -> int:
    target = _find_target()
    if not target:
        print(f"[錯誤] 找不到 class={TARGET_CLASS} title 含 {TARGET_TITLE_KW!r} 的視窗")
        return 1

    target_rect = _get_rect(target)
    print(f"主視窗 hwnd={target}  rect={target_rect}")
    print()

    print("正在列舉所有子孫視窗...")
    all_items = _enum_all_descendants(target)
    print(f"總共 {len(all_items)} 個子孫視窗")
    print()

    # 過濾出 Edit-like 控制項
    edits = []
    for it in all_items:
        cls_lower = (it["class"] or "").lower()
        if any(p in cls_lower for p in EDIT_CLASS_PATTERNS):
            edits.append(it)

    print(f"=== Edit-like 控制項 ({len(edits)} 個) ===")
    print("格式：hwnd | class | text | (left, top, w x h)")
    print("-" * 90)

    # 依 y, x 排序（由上至下、由左至右）
    def _sort_key(it):
        r = it["rect"]
        return (r.get("top", 999999), r.get("left", 999999))

    edits_sorted = sorted(edits, key=_sort_key)
    for it in edits_sorted:
        r = it["rect"]
        txt = (it["text"] or "")[:30]
        print(f"  hwnd={it['hwnd']:>10} | {it['class']:<25} | text={txt!r:<32} | "
              f"({r.get('left'):>5},{r.get('top'):>4}) {r.get('w'):>4}x{r.get('h'):>3}")

    # 特別標出 text=="3" 的（療程欄目前值）
    print()
    print("=== text == '3' 的（療程目前值）===")
    threes = [it for it in edits if (it["text"] or "").strip() == "3"]
    if not threes:
        print("  找不到 text='3' 的欄位（療程可能不是 3 了？或不在 Edit-class 集合裡）")
    for it in threes:
        r = it["rect"]
        print(f"  hwnd={it['hwnd']} class={it['class']!r} text='3' "
              f"({r.get('left')},{r.get('top')}) {r.get('w')}x{r.get('h')}")

    # 全部寫進檔案
    out = Path(__file__).resolve().parent.parent / "settings" / "main_app_edits_probe.txt"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({
        "target_hwnd": target,
        "target_rect": target_rect,
        "edit_controls": edits_sorted,
        "text_eq_3_candidates": threes,
        "total_descendants": len(all_items),
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print()
    print(f"✓ 已寫入：{out}")
    print("  把這個檔案內容貼給 Claude，他會告訴你「療程」是哪個 hwnd。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        input("\n按 Enter 結束...")
        sys.exit(1)
