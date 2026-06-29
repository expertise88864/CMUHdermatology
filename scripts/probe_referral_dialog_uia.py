# -*- coding: utf-8 -*-
r"""探測「病人轉診提示畫面」(class TFTunMsg)—— 給 F11 自動處理轉診視窗用。

讀兩樣東西:
  (1) 動向選項(TGroupButton)與按鈕(TButton)的文字 → 確認要點哪個 radio / 哪個是「處理/離開」。
  (2) 『本次門診預掛紀錄』表格(TXStringGrid)能不能用 UIA / MSAA 讀到「有幾列預約」。

只【讀】、不點任何按鈕、不改任何資料,安全。

用法:
  1. 在 HIS 裡讓「病人轉診提示畫面」對話框開著(就是按完成會跳出來的那個)。
  2. 以系統管理員跑:`python scripts\probe_referral_dialog_uia.py`
  3. 結果存到 settings\_referral_uia_probe.txt → 把內容整段貼給 Claude。
"""
from __future__ import annotations

import ctypes
import sys
import traceback
from ctypes import wintypes
from pathlib import Path

user32 = ctypes.windll.user32
_LINES: list = []


def log(s: str = "") -> None:
    _LINES.append(str(s))
    try:
        print(s)
    except Exception:
        pass


def _save() -> None:
    out = Path(__file__).resolve().parent.parent / "settings" / "_referral_uia_probe.txt"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(_LINES), encoding="utf-8")
        print(f"\n>>> 結果已存到:{out}\n>>> 把這個檔的內容整段貼給 Claude。")
    except Exception as e:  # noqa: BLE001
        print(f"[寫檔失敗] {e}\n以下為結果,請直接複製:\n" + "\n".join(_LINES))


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _win_text(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


_EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _find_dialog() -> int:
    found = [0]

    @_EnumProc
    def cb(hwnd, _lp):
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            if _class_name(hwnd) == "TFTunMsg" or "轉診" in _win_text(hwnd):
                found[0] = hwnd
                return False
        except Exception:
            pass
        return True

    user32.EnumWindows(cb, 0)
    return found[0]


def _find_children(hwnd: int, want_class=None) -> list:
    out: list = []

    @_EnumProc
    def cb(child, _lp):
        try:
            cls = _class_name(child)
            if want_class is None or cls == want_class:
                out.append((child, cls, _win_text(child)))
        except Exception:
            pass
        return True

    user32.EnumChildWindows(hwnd, cb, 0)
    return out


def main() -> int:
    dlg = _find_dialog()
    if not dlg:
        log("找不到『病人轉診提示畫面』(class TFTunMsg)。請先讓那個對話框開著再跑這支。")
        _save()
        return 1
    log(f"對話框 hwnd={dlg} class={_class_name(dlg)!r} title={_win_text(dlg)!r}")

    log("\n=== 動向選項 (TGroupButton) —— 看哪個是「本科門診進一步追蹤治療」 ===")
    for h, _cls, txt in _find_children(dlg, "TGroupButton"):
        log(f"  hwnd={h} text={txt!r}")

    log("\n=== 按鈕 (TButton) —— 看哪個是「處理/離開」 ===")
    for h, _cls, txt in _find_children(dlg, "TButton"):
        log(f"  hwnd={h} text={txt!r}")

    grids = _find_children(dlg, "TXStringGrid")
    if not grids:
        grids = [(h, c, t) for h, c, t in _find_children(dlg) if "Grid" in c]
    if not grids:
        log("\n找不到表格(TXStringGrid / *Grid)。對話框所有子控件 class 統計:")
        seen: dict = {}
        for _h, c, _t in _find_children(dlg):
            seen[c] = seen.get(c, 0) + 1
        for c, n in sorted(seen.items()):
            log(f"  {c} x{n}")
        _save()
        return 1
    grid = grids[0][0]
    log(f"\n=== 預掛紀錄表格 hwnd={grid} class={grids[0][1]!r} ===")
    _probe_uia(grid)
    _probe_msaa(grid)
    _save()
    return 0


def _probe_uia(grid: int) -> None:
    log("\n===== UIA =====")
    try:
        import comtypes
        import comtypes.client
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA

        iuia = comtypes.client.CreateObject(
            UIA.CUIAutomation, interface=UIA.IUIAutomation)
        root = iuia.ElementFromHandle(grid)
        if root is None:
            log("UIA ElementFromHandle 回 None(表格可能不支援 UIA)。")
            return
        try:
            log(f"表格 UIA: name={root.CurrentName!r} controlType={root.CurrentControlType}")
        except Exception:
            pass
        # Grid pattern → 直接拿 RowCount/ColumnCount(最理想的「有幾列」訊號)
        try:
            pat = root.GetCurrentPattern(10000)  # UIA_GridPatternId
            if pat:
                gp = pat.QueryInterface(UIA.IUIAutomationGridPattern)
                log(f"★ Grid pattern: RowCount={gp.CurrentRowCount} "
                    f"ColumnCount={gp.CurrentColumnCount}")
            else:
                log("(不支援 Grid pattern)")
        except Exception as e:  # noqa: BLE001
            log(f"(取 Grid RowCount 失敗:{e})")

        walker = iuia.RawViewWalker
        named = [0]
        total = [0]

        def walk(el, depth=0):
            if el is None or total[0] > 1500 or named[0] > 400:
                return
            total[0] += 1
            try:
                name = el.CurrentName
            except Exception:
                name = ""
            try:
                ct = el.CurrentControlType
            except Exception:
                ct = 0
            if name and str(name).strip():
                named[0] += 1
                log(f"{'  ' * min(depth, 8)}[ct={ct}] {str(name)[:70]!r}")
            try:
                child = walker.GetFirstChildElement(el)
            except Exception:
                child = None
            while child is not None and total[0] <= 1500:
                walk(child, depth + 1)
                try:
                    child = walker.GetNextSiblingElement(child)
                except Exception:
                    break

        log("--- 表格子孫中『有文字 Name』的元素(理想會看到 1150704 / 皮膚科 / 醫師名 等預約列) ---")
        walk(root)
        log(f"--- 走訪 {total[0]} 個元素,其中 {named[0]} 個有文字 ---")
        log("(若只看到捲軸字樣或 0 個有文字 = UIA 讀不到格子內容,改看下面 MSAA)")
    except Exception as e:  # noqa: BLE001
        log(f"UIA 例外:{e}")
        log(traceback.format_exc())


def _probe_msaa(grid: int) -> None:
    log("\n===== MSAA(IAccessible)=====")
    try:
        import comtypes.client
        comtypes.client.GetModule("oleacc.dll")
        from comtypes.gen.Accessibility import IAccessible
        from ctypes import POINTER, byref

        ppacc = POINTER(IAccessible)()
        iid = IAccessible._iid_
        ctypes.oledll.oleacc.AccessibleObjectFromWindow(
            grid, 0xFFFFFFFC, byref(iid), byref(ppacc))
        acc = ppacc
        try:
            log(f"MSAA accChildCount={acc.accChildCount}(可能=格子數或列數,給 Claude 對照)")
        except Exception as e:  # noqa: BLE001
            log(f"MSAA accChildCount 失敗:{e}")
    except Exception as e:  # noqa: BLE001
        log(f"MSAA 例外:{e}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        input("按 Enter 結束...")
        sys.exit(1)
