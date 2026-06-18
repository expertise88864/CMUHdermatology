# -*- coding: utf-8 -*-
"""找出主畫面左上角『身份/部分負擔』欄(目前顯示 40)是哪一個控制項。只讀,不改任何東西。

兩段資訊一起抓,互相佐證(像當初鎖卡號/療程欄):
  1. 倒數結束時讀「目前有輸入焦點的控制項」—— 你在倒數內【點一下身份欄(顯示 40 那格)】,
     這就是定位身份欄最確定的依據(hwnd / class / 位置 / 目前文字)。
  2. 同時列舉主視窗 (TFopdmain) 底下所有 Edit-like 控制項 + 文字 + 位置,並標出 text 含
     "40" 的候選 —— 即使焦點沒讀到,也能用「左上角、文字=40」交叉確認。

用法:雙擊 身份欄探測.cmd → 掛入一位病人 → 切回 HIS、用滑鼠點一下身份欄(顯示 40 那格)
→ 等倒數完。結果存到 settings\\_identity_field_probe.txt,整個貼給 Claude。
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.windll.user32
SETTINGS = Path(__file__).resolve().parent.parent / "settings"
_LINES: list = []

TARGET_CLASS = "TFopdmain"
TARGET_TITLE_KW = "西醫門診醫師作業"
EDIT_CLASS_PATTERNS = (
    "edit", "tedit", "tdbedit", "tcombo", "tdbcombo", "tdblookup",
    "tmaskedit", "tnumedit",
)

EnumWindowsProc = ctypes.WINFUNCTYPE(
    wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def log(s: str = "") -> None:
    _LINES.append(str(s))
    try:
        print(s)
    except Exception:
        pass


def _save() -> None:
    out = SETTINGS / "_identity_field_probe.txt"
    try:
        SETTINGS.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(_LINES), encoding="utf-8")
        print(f"\n>>> 結果存到:{out}\n>>> 整個檔案貼給 Claude。")
    except Exception as e:  # noqa: BLE001
        print("[寫檔失敗]", e)
        print("\n".join(_LINES))


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _text(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    if n <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value or ""


def _rect(hwnd: int):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _find_main() -> int:
    found = [0]

    @EnumWindowsProc
    def cb(hwnd, lparam):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            if _class_name(hwnd) != TARGET_CLASS:
                return True
            if TARGET_TITLE_KW in _text(hwnd):
                found[0] = hwnd
                return False
        except Exception:
            pass
        return True

    user32.EnumWindows(cb, 0)
    return found[0]


def _enum_edits(root_hwnd: int) -> list:
    items: list = []
    visited: set = set()

    def _recurse(parent):
        if parent in visited:
            return
        visited.add(parent)
        children: list = []

        @EnumWindowsProc
        def cb(child, lp):
            children.append(child)
            return True

        user32.EnumChildWindows(parent, cb, 0)
        for ch in children:
            try:
                cls = _class_name(ch)
                if any(p in (cls or "").lower() for p in EDIT_CLASS_PATTERNS):
                    left, top, right, bottom = _rect(ch)
                    items.append({
                        "hwnd": ch, "class": cls, "text": _text(ch),
                        "left": left, "top": top,
                        "w": right - left, "h": bottom - top,
                    })
                _recurse(ch)
            except Exception:
                pass

    _recurse(root_hwnd)
    items.sort(key=lambda it: (it["top"], it["left"]))
    return items


def main() -> int:
    log("===== 身份/部分負擔欄(顯示 40)探測 =====")
    log("請在倒數結束前,切回 HIS、用滑鼠點一下『身份欄(顯示 40 那格)』(游標在裡面閃)…")
    for i in (6, 5, 4, 3, 2, 1):
        print(f"  {i} …", end="\r")
        time.sleep(1)
    print(" " * 20, end="\r")

    fg = user32.GetForegroundWindow()
    tid = user32.GetWindowThreadProcessId(fg, None)
    gti = GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(GUITHREADINFO)
    focus = 0
    if user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
        focus = gti.hwndFocus or gti.hwndCaret or gti.hwndActive

    log(f"前景視窗 hwnd={fg} class={_class_name(fg)!r} title={_text(fg)!r}")

    GA_ROOT = 2
    root = (user32.GetAncestor(focus, GA_ROOT) if focus else 0) or _find_main() or fg

    log("")
    log("── (1) 有焦點的控制項(你剛點的那格,很可能就是身份欄)──")
    if focus:
        fl, ft, fr, fb = _rect(focus)
        rl, rt, _rr, _rb = _rect(root)
        log(f"  hwnd={focus}")
        log(f"  class={_class_name(focus)!r}")
        log(f"  目前文字={_text(focus)!r}")
        log(f"  螢幕 rect=({fl},{ft})-({fr},{fb})  w={fr-fl} h={fb-ft}")
        log(f"  在主視窗內相對位置:rel_left={fl-rl} rel_top={ft-rt} w={fr-fl} h={fb-ft}")
    else:
        log("  讀不到焦點控制項(可能沒點進可輸入欄、或欄位非標準 edit)。請看下面 (2) 列舉。")

    log("")
    log("── 所屬主視窗 ──")
    log(f"  hwnd={root} class={_class_name(root)!r} title={_text(root)!r}")

    log("")
    log("── (2) 主視窗底下所有 Edit-like 控制項(由上而下、由左而右)──")
    log("  格式:hwnd | class | text | (left,top) wxh | [<<焦點] [<<text含40]")
    edits = _enum_edits(root)
    log(f"  共 {len(edits)} 個:")
    for it in edits:
        marks = ""
        if focus and it["hwnd"] == focus:
            marks += " <<焦點"
        if "40" in (it["text"] or ""):
            marks += " <<text含40"
        log(f"  hwnd={it['hwnd']:>10} | {it['class']:<22} | "
            f"text={(it['text'] or '')[:24]!r:<26} | "
            f"({it['left']:>5},{it['top']:>4}) {it['w']:>4}x{it['h']:>3}{marks}")

    log("")
    log("── (3) text 含 '40' 的候選(身份欄應在最左上)──")
    cands = [it for it in edits if "40" in (it["text"] or "")]
    if not cands:
        log("  沒有任何 edit 的 text 含 '40'(身份欄可能此刻不是 40,或非標準 edit)。")
    for it in cands:
        log(f"  hwnd={it['hwnd']} class={it['class']!r} text={it['text']!r} "
            f"({it['left']},{it['top']}) {it['w']}x{it['h']}")
    _save()
    return 0


if __name__ == "__main__":
    import sys
    try:
        sys.exit(main())
    except Exception:
        import traceback
        log("[例外]")
        log(traceback.format_exc())
        _save()
        sys.exit(1)
