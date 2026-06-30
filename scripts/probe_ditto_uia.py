# -*- coding: utf-8 -*-
"""探測「醫師上次」清單(TStringAlignGrid)能不能用 UI Automation(UIA)讀到格子文字。

Win32 讀不到這種 Delphi 畫上去的格子;UIA 是另一套 API,有機會讀得到。本工具只「讀」、
不點任何按鈕、不改任何資料,安全。

用法:
  1. 在 HIS 裡 DITTO → 醫師上次,讓那個清單視窗開著(保持開啟即可,不必在最前景)
  2. 跑這支:雙擊 tools\probe_ditto_uia.cmd,或 `python scripts\\probe_ditto_uia.py`
  3. 結果存到 settings\\_ditto_uia_probe.txt → 把內容貼給 Claude
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
    _LINES.append(s)
    try:
        print(s)
    except Exception:
        pass


def _save() -> None:
    out = Path(__file__).resolve().parent.parent / "settings" / "_ditto_uia_probe.txt"
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(_LINES), encoding="utf-8")
        print(f"\n>>> 結果已存到:{out}\n>>> 把這個檔的內容貼給 Claude。")
    except Exception as e:  # noqa: BLE001
        print(f"[寫檔失敗] {e}\n以下為結果,請直接複製:\n" + "\n".join(_LINES))


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _enum_children(hwnd: int) -> list:
    out: list = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(h, _l):
        out.append((h, _class_name(h)))
        return True

    user32.EnumChildWindows(hwnd, cb, 0)
    return out


def _ensure_comtypes():
    try:
        import comtypes  # noqa: F401
        return True
    except Exception:
        log("comtypes 未安裝,嘗試自動安裝 (pip install comtypes) …")
        try:
            import subprocess
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "comtypes"],
                           check=False)
            import comtypes  # noqa: F401
            log("comtypes 安裝完成")
            return True
        except Exception as e:  # noqa: BLE001
            log(f"comtypes 安裝失敗:{e}")
            return False


def main() -> int:
    log("===== 醫師上次 UIA 探測 =====")
    # 1) 找醫師上次視窗
    win = user32.FindWindowW("TFOpdditto1", None)
    log(f"醫師上次視窗(class=TFOpdditto1) hwnd={win}")
    if not win:
        log("找不到醫師上次視窗 —— 請先在 HIS 裡 DITTO→醫師上次 開著清單,再跑一次。")
        _save()
        return 1

    # 2) 找格線
    grid = 0
    for h, c in _enum_children(win):
        if c == "TStringAlignGrid":
            grid = h
            break
    log(f"格線(TStringAlignGrid) hwnd={grid}")
    if not grid:
        log("視窗內找不到 TStringAlignGrid。")
        _save()
        return 1

    if not _ensure_comtypes():
        _save()
        return 1

    # 3) UIA
    try:
        import comtypes
        import comtypes.client
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA

        iuia = comtypes.client.CreateObject(
            UIA.CUIAutomation, interface=UIA.IUIAutomation)
        root = iuia.ElementFromHandle(grid)
        if root is None:
            log("UIA ElementFromHandle 回 None(這個格線可能完全不支援 UIA)。")
            _save()
            return 1

        try:
            log(f"格線 UIA: name={root.CurrentName!r} controlType={root.CurrentControlType}")
        except Exception:
            pass

        # 支援哪些 pattern(Grid/Table/Text 最關鍵)
        pattern_ids = {
            "Grid(10000)": 10000, "Table(10012)": 10012, "Text(10014)": 10014,
            "Value(10002)": 10002, "Selection(10001)": 10001,
            "LegacyIAccessible(10018)": 10018,
        }
        supported = []
        for nm, pid in pattern_ids.items():
            try:
                if root.GetCurrentPattern(pid):
                    supported.append(nm)
            except Exception:
                pass
        log(f"格線支援的 UIA pattern:{supported or '(無)'}")

        # 走子孫,把有 Name 的元素印出來(這就是格子文字會出現的地方)
        walker = iuia.RawViewWalker
        named = [0]
        total = [0]

        def walk(el, depth=0):
            if el is None or total[0] > 1200 or named[0] > 300:
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
                log(f"{'  ' * min(depth, 8)}[ct={ct}] {str(name)[:60]!r}")
            try:
                child = walker.GetFirstChildElement(el)
            except Exception:
                child = None
            while child is not None and total[0] <= 1200:
                walk(child, depth + 1)
                try:
                    child = walker.GetNextSiblingElement(child)
                except Exception:
                    break

        log("--- 格線子孫中『有文字 Name』的元素(理想上會看到卡號/療程/日期等) ---")
        walk(root)
        log(f"--- 走訪 {total[0]} 個元素,其中 {named[0]} 個有文字 ---")
        log("(註:以上若只看到『垂直/水平/上移一行…』等捲軸字樣 = UIA 讀不到格子內容)")
    except Exception as e:  # noqa: BLE001
        log(f"UIA 探測發生例外:{e}")
        log(traceback.format_exc())

    _probe_msaa(grid)
    _save()
    return 0


def _probe_msaa(grid_hwnd: int) -> None:
    """直接走 MSAA(IAccessible)列舉格線子元素 —— UIA 橋接有時看不到、MSAA 直讀
    卻看得到 Delphi 格子文字。只讀,不改任何東西。"""
    log("")
    log("===== MSAA(IAccessible)直接列舉格子 =====")
    try:
        import comtypes
        import comtypes.automation as AUT
        import comtypes.client
        comtypes.client.GetModule("oleacc.dll")
        from comtypes.gen.Accessibility import IAccessible
    except Exception as e:  # noqa: BLE001
        log(f"載入 oleacc/IAccessible 失敗:{e}")
        return
    import ctypes
    from ctypes import POINTER, byref, c_long
    try:
        ppacc = POINTER(IAccessible)()
        iid = IAccessible._iid_
        OBJID_CLIENT = 0xFFFFFFFC
        ctypes.oledll.oleacc.AccessibleObjectFromWindow(
            grid_hwnd, OBJID_CLIENT, byref(iid), byref(ppacc))
        acc = ppacc
    except Exception as e:  # noqa: BLE001
        log(f"AccessibleObjectFromWindow 失敗:{e}")
        return

    seen = [0]
    budget = [0]

    def _emit(depth, nm, vl, loc):
        seen[0] += 1
        l, t, w, h = loc
        log(f"{'  ' * min(depth, 8)}name={str(nm or '')[:36]!r} "
            f"value={str(vl or '')[:20]!r} @({l},{t}) {w}x{h}")

    def _loc(a, cid):
        try:
            res = a.accLocation(cid)   # comtypes 把 4 個 out 參數轉成回傳 tuple
            if isinstance(res, (tuple, list)) and len(res) >= 4:
                return tuple(int(x) for x in res[:4])
        except Exception:
            pass
        return (0, 0, 0, 0)

    def enum(a, depth=0):
        if a is None or budget[0] > 1500:
            return
        try:
            n = int(a.accChildCount)
        except Exception:
            n = 0
        if n <= 0:
            return
        try:
            arr = (AUT.VARIANT * n)()
            got = c_long()
            ctypes.oledll.oleacc.AccessibleChildren(a, 0, n, arr, byref(got))
            kids = arr[: got.value]
        except Exception as e:  # noqa: BLE001
            log(f"  AccessibleChildren 失敗(depth={depth}):{e}")
            return
        for v in kids:
            budget[0] += 1
            if budget[0] > 1500:
                break
            try:
                if v.vt == 9:  # VT_DISPATCH → 完整子物件
                    ch = v.value.QueryInterface(IAccessible)
                    try:
                        nm = ch.accName(0)
                    except Exception:
                        nm = None
                    try:
                        vl = ch.accValue(0)
                    except Exception:
                        vl = None
                    if (nm and str(nm).strip()) or (vl and str(vl).strip()):
                        _emit(depth, nm, vl, _loc(ch, 0))
                    enum(ch, depth + 1)
                else:  # 簡單 childid
                    cid = int(v.value)
                    try:
                        nm = a.accName(cid)
                    except Exception:
                        nm = None
                    try:
                        vl = a.accValue(cid)
                    except Exception:
                        vl = None
                    if (nm and str(nm).strip()) or (vl and str(vl).strip()):
                        _emit(depth, nm, vl, _loc(a, cid))
            except Exception:
                pass

    try:
        total = int(acc.accChildCount)
    except Exception as e:  # noqa: BLE001
        log(f"accChildCount 失敗:{e}")
        return
    log(f"grid IAccessible accChildCount={total}")
    enum(acc)
    log(f"--- MSAA 有文字的元素:{seen[0]} 個 ---")
    if seen[0] == 0:
        log("結論:MSAA 也讀不到格子內容 → 確定只剩 OCR。")
    else:
        log("結論:MSAA 讀得到!上面若有卡號/療程/日期那些值(且帶座標)→ 可乾淨實作。")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        try:
            input("\n按 Enter 關閉…")
        except Exception:
            pass
        sys.exit(1)
