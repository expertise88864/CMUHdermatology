# -*- coding: utf-8 -*-
"""找出主畫面『卡號』輸入欄到底是哪一個控制項(只讀,不改任何東西)。

做法:倒數幾秒,你在這段時間內【用滑鼠點一下卡號欄】(讓游標在卡號欄裡閃),
時間到時本工具讀「目前有輸入焦點的控制項」,回報它的 hwnd / class / 位置 /
目前文字,以及它在主視窗內的相對位置 → Claude 就能寫出穩當的卡號欄定位。

用法:雙擊 tools\probe_card_field.cmd → 切回 HIS、點一下卡號欄 → 等倒數完。
結果存到 settings\\_card_field_probe.txt。
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.windll.user32
SETTINGS = Path(__file__).resolve().parent.parent / "settings"
_LINES: list = []


def log(s: str = "") -> None:
    _LINES.append(str(s))
    try:
        print(s)
    except Exception:
        pass


def _save() -> None:
    out = SETTINGS / "_card_field_probe.txt"
    try:
        SETTINGS.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(_LINES), encoding="utf-8")
        print(f"\n>>> 結果存到:{out}\n>>> 貼給 Claude。")
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


def main() -> int:
    log("===== 卡號欄 焦點探測 =====")
    log("請在倒數結束前,切回 HIS、用滑鼠點一下『卡號』輸入欄(游標在裡面閃)…")
    for i in (6, 5, 4, 3, 2, 1):
        print(f"  {i} …", end="\r")
        time.sleep(1)
    print(" " * 20, end="\r")

    fg = user32.GetForegroundWindow()
    tid = user32.GetWindowThreadProcessId(fg, None)
    gti = GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(GUITHREADINFO)
    ok = user32.GetGUIThreadInfo(tid, ctypes.byref(gti))
    if not ok:
        log("GetGUIThreadInfo 失敗 — 改試 admin 權限,或確認有點進卡號欄。")
        _save()
        return 1

    focus = gti.hwndFocus or gti.hwndCaret or gti.hwndActive
    log(f"前景視窗 hwnd={fg} class={_class_name(fg)!r} title={_text(fg)!r}")
    if not focus:
        log("讀不到有焦點的控制項(可能沒點進可輸入欄位)。請再試一次。")
        _save()
        return 1

    # 找 top-level 祖先
    GA_ROOT = 2
    root = user32.GetAncestor(focus, GA_ROOT) or fg
    rl, rt, rr, rb = _rect(root)
    fl, ft, fr, fb = _rect(focus)

    log("")
    log("── 有焦點的控制項(很可能就是卡號欄)──")
    log(f"  hwnd={focus}")
    log(f"  class={_class_name(focus)!r}")
    log(f"  目前文字={_text(focus)!r}")
    log(f"  螢幕 rect=({fl},{ft})-({fr},{fb})  w={fr-fl} h={fb-ft}")
    log("")
    log("── 所屬主視窗 ──")
    log(f"  hwnd={root} class={_class_name(root)!r} title={_text(root)!r}")
    log(f"  rect=({rl},{rt})-({rr},{rb})")
    log("")
    log("── 卡號欄在主視窗內的相對位置(解析度無關,給 Claude 定位用)──")
    log(f"  rel_left={fl-rl}  rel_top={ft-rt}  w={fr-fl}  h={fb-ft}")
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
