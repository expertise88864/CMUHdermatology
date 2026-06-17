# -*- coding: utf-8 -*-
"""OCR 可行性 + 擷取方式測試:截下「醫師上次」格線 → Windows 內建 OCR。

測四種擷取法,重點是找出「不必把視窗顯示給使用者看」也能截到內容的方法:
  A) PrintWindow(整窗, flag 0/1/2) → 裁切到格線         [隱形,不搶焦點]
  C) 直接對格線送 WM_PRINTCLIENT                          [隱形,不搶焦點]
  B) 把醫師上次叫到最前面 + 螢幕 BitBlt(會閃一下)        [需 --show 才做]

只「讀」,不點任何按鈕、不改任何資料。設計成絕不無聲閃退:任何失敗都會寫進
settings\\_ditto_ocr_probe.txt(.cmd 另存一份 _ditto_ocr_run.log)。

用法:
  1. HIS 裡 DITTO → 醫師上次,清單開著(療程1 / 卡號 那幾列看得到)
  2. 雙擊 probe_ditto_ocr.cmd   (會自動加 --show,連螢幕 BitBlt 也測)
  3. 把 settings\\_ditto_ocr_probe.txt 貼給 Claude,連同 settings\\_ditto_*.png
"""
from __future__ import annotations

import asyncio
import ctypes
import importlib
import subprocess
import sys
import time
import traceback
from pathlib import Path

SETTINGS = Path(__file__).resolve().parent.parent / "settings"
_LINES: list = []


def log(s: str = "") -> None:
    _LINES.append(str(s))
    try:
        print(s)
    except Exception:
        pass


def _save() -> None:
    out = SETTINGS / "_ditto_ocr_probe.txt"
    try:
        SETTINGS.mkdir(parents=True, exist_ok=True)
        out.write_text("\n".join(_LINES), encoding="utf-8")
        print(f"\n>>> 文字結果:{out}")
        print(f">>> 截圖在:{SETTINGS}  (檔名 _ditto_*.png)")
    except Exception as e:  # noqa: BLE001
        print("[寫檔失敗]", e)
        print("\n".join(_LINES))


def _set_dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def _find_grid() -> tuple:
    import win32gui  # type: ignore
    win = win32gui.FindWindow("TFOpdditto1", None)
    if not win:
        return 0, 0
    found: list = []

    def cb(h, _):
        try:
            if win32gui.GetClassName(h) == "TStringAlignGrid":
                found.append(h)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(win, cb, None)
    except Exception:
        pass
    return win, (found[0] if found else 0)


def _bmp_to_image(bmp):
    from PIL import Image
    info = bmp.GetInfo()
    bits = bmp.GetBitmapBits(True)
    return Image.frombuffer(
        "RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1)


def _new_dc_bmp(ref_hwnd: int, w: int, h: int):
    import win32gui  # type: ignore
    import win32ui   # type: ignore
    hwnd_dc = win32gui.GetWindowDC(ref_hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc_dc, w, h)
    save_dc.SelectObject(bmp)
    return hwnd_dc, mfc_dc, save_dc, bmp


def _free_dc_bmp(ref_hwnd, hwnd_dc, mfc_dc, save_dc, bmp):
    import win32gui  # type: ignore
    if bmp is not None:
        try:
            win32gui.DeleteObject(bmp.GetHandle())
        except Exception:
            pass
    for dc in (save_dc, mfc_dc):
        if dc is not None:
            try:
                dc.DeleteDC()
            except Exception:
                pass
    if hwnd_dc is not None:
        try:
            win32gui.ReleaseDC(ref_hwnd, hwnd_dc)
        except Exception:
            pass


def _printwindow(hwnd: int, flag: int):
    """PrintWindow 擷取整個視窗(隱形,不需顯示)。flag: 0=預設 1=只客戶區 2=完整內容。"""
    import win32gui  # type: ignore
    left, top, right, bot = win32gui.GetWindowRect(hwnd)
    w, h = right - left, bot - top
    if w <= 0 or h <= 0:
        raise RuntimeError(f"視窗尺寸異常 {w}x{h}")
    dc = _new_dc_bmp(hwnd, w, h)
    try:
        ctypes.windll.user32.PrintWindow(hwnd, dc[2].GetSafeHdc(), flag)
        return _bmp_to_image(dc[3])
    finally:
        _free_dc_bmp(hwnd, *dc)


def _wm_printclient(hwnd: int):
    """直接對控制項送 WM_PRINTCLIENT(隱形)。有時 PrintWindow 全黑但這招有內容。"""
    import win32gui  # type: ignore
    left, top, right, bot = win32gui.GetWindowRect(hwnd)
    w, h = right - left, bot - top
    if w <= 0 or h <= 0:
        raise RuntimeError(f"控制項尺寸異常 {w}x{h}")
    dc = _new_dc_bmp(hwnd, w, h)
    try:
        WM_PRINTCLIENT = 0x0318
        PRF_CLIENT, PRF_CHILDREN, PRF_ERASEBKGND = 0x4, 0x10, 0x8
        flags = PRF_CLIENT | PRF_CHILDREN | PRF_ERASEBKGND
        win32gui.SendMessage(hwnd, WM_PRINTCLIENT, dc[2].GetSafeHdc(), flags)
        return _bmp_to_image(dc[3])
    finally:
        _free_dc_bmp(hwnd, *dc)


def _screen_rect(left: int, top: int, w: int, h: int):
    """從螢幕 DC BitBlt 一塊矩形(視窗需在螢幕上、未被遮住)。"""
    import win32con  # type: ignore
    import win32gui  # type: ignore
    if w <= 0 or h <= 0:
        raise RuntimeError(f"矩形尺寸異常 {w}x{h}")
    desktop = win32gui.GetDesktopWindow()
    dc = _new_dc_bmp(desktop, w, h)
    try:
        dc[2].BitBlt((0, 0), (w, h), dc[1], (left, top), win32con.SRCCOPY)
        return _bmp_to_image(dc[3])
    finally:
        _free_dc_bmp(desktop, *dc)


def _bring_front(hwnd: int) -> None:
    import win32con      # type: ignore
    import win32gui      # type: ignore
    import win32process  # type: ignore
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    except Exception:
        pass
    try:
        cur = ctypes.windll.kernel32.GetCurrentThreadId()
        fg = win32gui.GetForegroundWindow()
        ftid = win32process.GetWindowThreadProcessId(fg)[0] if fg else 0
        if ftid and ftid != cur:
            ctypes.windll.user32.AttachThreadInput(ftid, cur, True)
        try:
            win32gui.SetWindowPos(hwnd, win32con.HWND_TOP, 0, 0, 0, 0,
                                  win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
        finally:
            if ftid and ftid != cur:
                ctypes.windll.user32.AttachThreadInput(ftid, cur, False)
    except Exception:
        pass


def _is_blank(img) -> bool:
    try:
        ex = img.convert("L").getextrema()
        return ex[0] == ex[1]
    except Exception:
        return False


def _ensure_ocr_pkg():
    for root in ("winsdk", "winrt"):
        try:
            importlib.import_module(f"{root}.windows.media.ocr")
            return root
        except Exception:
            pass
    log("OCR 套件未安裝,嘗試安裝 winsdk …")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "winsdk"], check=False)
    try:
        importlib.import_module("winsdk.windows.media.ocr")
        log("winsdk 安裝完成")
        return "winsdk"
    except Exception:
        pass
    log("winsdk 不可用,改試 winrt 命名空間套件 …")
    pkgs = [
        "winrt-runtime", "winrt-Windows.Foundation", "winrt-Windows.Globalization",
        "winrt-Windows.Storage", "winrt-Windows.Storage.Streams",
        "winrt-Windows.Graphics.Imaging", "winrt-Windows.Media.Ocr",
    ]
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", *pkgs], check=False)
    try:
        importlib.import_module("winrt.windows.media.ocr")
        log("winrt 安裝完成")
        return "winrt"
    except Exception as e:  # noqa: BLE001
        log(f"OCR 套件安裝失敗:{e}")
        return None


async def _ocr(png_path: Path, root: str):
    ocr_mod = importlib.import_module(f"{root}.windows.media.ocr")
    glob_mod = importlib.import_module(f"{root}.windows.globalization")
    img_mod = importlib.import_module(f"{root}.windows.graphics.imaging")
    sto_mod = importlib.import_module(f"{root}.windows.storage")
    OcrEngine = ocr_mod.OcrEngine
    Language = glob_mod.Language
    BitmapDecoder = img_mod.BitmapDecoder
    StorageFile = sto_mod.StorageFile
    FileAccessMode = sto_mod.FileAccessMode

    file = await StorageFile.get_file_from_path_async(str(png_path))
    stream = await file.open_async(FileAccessMode.READ)
    decoder = await BitmapDecoder.create_async(stream)
    bitmap = await decoder.get_software_bitmap_async()

    engine = OcrEngine.try_create_from_user_profile_languages()
    used = "使用者設定語言"
    if engine is None:
        for lang in ("en-US", "zh-Hant-TW", "zh-Hans-CN"):
            try:
                engine = OcrEngine.try_create_from_language(Language(lang))
            except Exception:
                engine = None
            if engine is not None:
                used = lang
                break
    if engine is None:
        return None, "(系統沒有可用的 OCR 語言元件)"
    result = await engine.recognize_async(bitmap)
    return result, used


def _dump_ocr(label: str, png_path: Path, root: str) -> int:
    log("")
    log(f"========== OCR【{label}】 {png_path.name} ==========")
    try:
        result, used = asyncio.run(_ocr(png_path, root))
    except Exception as e:  # noqa: BLE001
        log(f"OCR 執行失敗:{e}")
        return 0
    if result is None:
        log(f"OCR 無法建立引擎:{used}")
        log("→ 可到 Windows『設定→時間與語言→語言→英文/繁中→選項』裝 OCR 元件。")
        return 0
    log(f"OCR 引擎語言:{used}(套件 {root})")
    nlines = 0
    for line in result.lines:
        nlines += 1
        log(f"[行] {line.text}")
        words = []
        for wd in line.words:
            r = wd.bounding_rect
            words.append(
                f"{wd.text}@({int(r.x)},{int(r.y)},{int(r.width)}x{int(r.height)})")
        if words:
            log("     " + "   ".join(words))
    log(f"--- 【{label}】共 {nlines} 行 ---")
    return nlines


def _crop_grid(win_img, wl, wt, gl, gt, gr, gb):
    box = (max(0, gl - wl), max(0, gt - wt),
           min(win_img.width, gr - wl), min(win_img.height, gb - wt))
    return win_img.crop(box), box


def main() -> int:
    do_show = "--show" in sys.argv
    log("===== 醫師上次 OCR + 擷取方式測試 =====")
    log(f"Python: {sys.version.split()[0]}  ({sys.executable})  --show={do_show}")

    try:
        import win32gui  # type: ignore  # noqa: F401
        import win32ui   # type: ignore  # noqa: F401
    except Exception as e:  # noqa: BLE001
        log(f"[缺套件] 這個 python 載入不了 pywin32:{e}")
        log(f'→ 請執行:"{sys.executable}" -m pip install pywin32')
        _save()
        return 1
    try:
        import PIL  # type: ignore  # noqa: F401
    except Exception as e:  # noqa: BLE001
        log(f"[缺套件] 載入不了 Pillow:{e}")
        log(f'→ 請執行:"{sys.executable}" -m pip install Pillow')
        _save()
        return 1

    _set_dpi_aware()
    import win32gui  # type: ignore
    win, grid = _find_grid()
    log(f"醫師上次視窗 hwnd={win} / 格線 hwnd={grid}")
    if not win or not grid:
        log("找不到醫師上次視窗或格線 —— 請先 DITTO→醫師上次 開著清單再跑。")
        _save()
        return 1

    SETTINGS.mkdir(parents=True, exist_ok=True)
    wl, wt, wr, wb = win32gui.GetWindowRect(win)
    gl, gt, gr, gb = win32gui.GetWindowRect(grid)
    log(f"視窗 rect=({wl},{wt})-({wr},{wb})  格線 rect=({gl},{gt})-({gr},{gb})")

    candidates: list = []  # (label, Path)

    # A) PrintWindow 多種 flag（隱形）
    for flag in (2, 0, 1):
        try:
            img = _printwindow(win, flag)
            blank = _is_blank(img)
            log(f"[A flag={flag}] 整窗 size={img.size} 全黑={blank}")
            if not blank:
                crop, box = _crop_grid(img, wl, wt, gl, gt, gr, gb)
                cpng = SETTINGS / f"_ditto_grid_pw{flag}.png"
                crop.save(str(cpng))
                cblank = _is_blank(crop)
                log(f"    裁切格線 box={box} size={crop.size} 全黑={cblank} → {cpng.name}")
                if not cblank:
                    candidates.append((f"A-PrintWindow(flag{flag})[隱形]", cpng))
        except Exception as e:  # noqa: BLE001
            log(f"[A flag={flag}] 失敗:{e}")

    # C) WM_PRINTCLIENT 直接送格線（隱形）
    try:
        img = _wm_printclient(grid)
        blank = _is_blank(img)
        cpng = SETTINGS / "_ditto_grid_printclient.png"
        img.save(str(cpng))
        log(f"[C WM_PRINTCLIENT] size={img.size} 全黑={blank} → {cpng.name}")
        if not blank:
            candidates.append(("C-WM_PRINTCLIENT[隱形]", cpng))
    except Exception as e:  # noqa: BLE001
        log(f"[C WM_PRINTCLIENT] 失敗:{e}")

    # B) 叫到最前 + 螢幕 BitBlt（會閃一下;只有 --show 才做）
    if do_show:
        try:
            _bring_front(win)
            time.sleep(0.45)
            gl2, gt2, gr2, gb2 = win32gui.GetWindowRect(grid)
            scr = _screen_rect(gl2, gt2, gr2 - gl2, gb2 - gt2)
            blank = _is_blank(scr)
            spng = SETTINGS / "_ditto_grid_screen.png"
            scr.save(str(spng))
            log(f"[B 螢幕BitBlt(需顯示)] size={scr.size} 全黑={blank} → {spng.name}")
            if not blank:
                candidates.append(("B-螢幕BitBlt[會閃一下]", spng))
        except Exception as e:  # noqa: BLE001
            log(f"[B 螢幕BitBlt] 失敗:{e}")
    else:
        log("[B] 略過(未加 --show;不搶你的焦點)。")

    if not candidates:
        log("")
        log("所有隱形擷取法都全黑 → 這個視窗無法在不顯示的情況下截到內容。")
        log("(若是雙擊 .cmd 跑的會自動帶 --show,看 [B] 那行有沒有成功)")
        _save()
        return 1

    root = _ensure_ocr_pkg()
    if not root:
        log("沒有 OCR 套件可用(但截圖已存,可把上述 png 給 Claude)。")
        _save()
        return 1

    for label, png_path in candidates:
        _dump_ocr(label, png_path, root)

    log("")
    log("請核對:上面有沒有正確認出『卡號(例如 0028)』與『療程欄(1 / 2)』?")
    log("標『[隱形]』的若成功 = 不必顯示視窗就能做;只有『[會閃一下]』成功 = 得短暫顯示。")
    _save()
    return 0


if __name__ == "__main__":
    try:
        rc = main()
    except Exception:
        log("[未預期的例外]")
        log(traceback.format_exc())
        _save()
        rc = 1
    sys.exit(rc)
