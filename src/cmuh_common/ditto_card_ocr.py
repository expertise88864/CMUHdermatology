# -*- coding: utf-8 -*-
"""醫師上次 卡號 OCR — 從「醫師上次」格線(TStringAlignGrid,純畫到螢幕、無法用
Win32/UIA/MSAA 讀)用 Windows 內建 OCR 讀出「最上面 療程=1 那一列」的卡號。

背景(為何用 OCR):這個 Delphi 格線把內容直接畫到實體螢幕,複製/UIA/MSAA/PrintWindow
全部讀不到(實測全黑或空)。唯一可行 = 視窗顯示時螢幕擷取 + OCR。卡號是純數字,Windows
OCR 對乾淨數字辨識很準(實測 0009/0007/0006/0005/0004 全對)。

安全(卡號是計費欄位):
  - 卡號必須是 3~4 位數字才採用;
  - 盡量用「同一張卡相鄰列卡號一致」交叉驗證,有把握(high)才建議自動填,
    沒把握(low/none)就不要填、提示使用者手動,絕不悄悄填錯。

設計:擷取(screen BitBlt)與 OCR(winsdk)是 Windows 專屬;純解析邏輯
(find_*_column_x / card_cells_from_words / pick_card_number)抽出來,可單元測試。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

CARD_RE = re.compile(r"^\d{3,4}$")
_TIAO_DIGITS = frozenset("123456789")


@dataclass
class Word:
    """OCR 認到的一個詞,座標為原圖(未放大)像素。"""
    text: str
    x: int
    y: int
    w: int
    h: int


@dataclass
class CardResult:
    """讀卡號結果。card 為 None 代表沒把握 → 呼叫端不要自動填。"""
    card: Optional[str]
    confidence: str          # "high" | "low" | "none"
    reason: str
    tiao: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.card is not None and self.confidence == "high"


# ───────────────────────── 純解析(可單元測試)─────────────────────────

def _header_band(words: list[Word], band_px: int = 26) -> list[Word]:
    """回傳標題列(最小 y 那一群)的詞。"""
    if not words:
        return []
    top = min(w.y for w in words)
    return [w for w in words if w.y <= top + band_px]


def find_column_x(words: list[Word], chars: tuple[str, ...],
                  band_px: int = 26) -> Optional[float]:
    """在標題列找含指定字(例如 '卡'、'療')的詞,回其 x 中心(取平均)。"""
    band = _header_band(words, band_px)
    xs = []
    for w in band:
        t = (w.text or "").strip()
        if any(c in t for c in chars):
            xs.append(w.x + w.w / 2.0)
    if not xs:
        return None
    return sum(xs) / len(xs)


def find_card_column_x(words: list[Word]) -> Optional[float]:
    # 只認『卡』——『號』在『時段診號』標題也有,會把欄位中心拉偏。
    return find_column_x(words, ("卡",))


def find_tiao_column_x(words: list[Word]) -> Optional[float]:
    return find_column_x(words, ("療", "撩"))


def card_cells_from_words(words: list[Word], card_x: float,
                          x_tol: int = 40) -> list[tuple[int, str]]:
    """從整張 OCR 詞中,挑出卡號欄(x 接近 card_x)的 3~4 位數字,回 [(y, 卡號)]。"""
    out: list[tuple[int, str]] = []
    for w in words:
        t = (w.text or "").strip()
        if not CARD_RE.match(t):
            continue
        cx = w.x + w.w / 2.0
        if abs(cx - card_x) <= x_tol:
            out.append((w.y, t))
    out.sort()
    return out


def tiao_cells_from_words(words: list[Word], tiao_x: float,
                          x_tol: int = 32) -> list[tuple[int, str]]:
    """從(放大後再換算回原座標的)療欄 OCR 詞,挑出單一數字,回 [(y, 療)]。"""
    out: list[tuple[int, str]] = []
    for w in words:
        t = (w.text or "").strip()
        if len(t) != 1 or t not in _TIAO_DIGITS:
            continue
        cx = w.x + w.w / 2.0
        if abs(cx - tiao_x) <= x_tol:
            out.append((w.y, t))
    out.sort()
    return out


def _nearest(cells: list[tuple[int, str]], y: int,
             row_tol: int) -> Optional[str]:
    best = None
    best_d = row_tol + 1
    for cy, t in cells:
        d = abs(cy - y)
        if d < best_d:
            best_d = d
            best = t
    return best


def _dedupe_rows(ys: list[int], tol: int = 10) -> list[int]:
    """把同一列的多個 y(卡號/療欄各認到、差幾 px)併成一列。"""
    out: list[int] = []
    for y in sorted(ys):
        if not out or y - out[-1] > tol:
            out.append(y)
    return out


def _estimate_pitch(ys: list[int]) -> Optional[float]:
    """估每列間距(去重後連續 y 差的中位數)。少於兩列回 None。"""
    rows = _dedupe_rows(ys)
    diffs = sorted(b - a for a, b in zip(rows, rows[1:], strict=False) if b - a > 0)
    if not diffs:
        return None
    return diffs[len(diffs) // 2]


def _top_row_near_header(card_ys: list[int], all_row_ys: list[int],
                         header_y: int, max_rows_gap: float = 1.8) -> bool:
    """最上面被讀到的『卡號』列是否「就是緊貼表頭的第一列」。

    第一列資料離表頭約 1 個列距 (gap≈1.1×pitch);第二列約 2.1×pitch。門檻設 1.8×pitch
    → 只放行『第一列有被讀到』的情況;只要最上卡號落到第二列(代表真正的最上列——通常
    是反白的今日那列——被漏讀),就擋下不填 (Codex round 4)。這樣即使今日是『新卡單列』
    被漏讀,也不會把下面更舊的卡誤當成現在的卡。
    間距用『所有列(卡號+療欄)』估 —— 療欄密集可靠,避免卡號稀疏時把間距高估而誤放行。
    估不到間距(單列)就不擋(單列本來 n<2 不會 high)。"""
    if not card_ys:
        return False
    pitch = _estimate_pitch(all_row_ys)
    if pitch is None:
        return True
    return (min(card_ys) - header_y) <= max_rows_gap * pitch


def pick_card_number(card_cells: list[tuple[int, str]],
                     tiao_cells: list[tuple[int, str]],
                     row_tol: int = 16,
                     header_y: Optional[int] = None) -> CardResult:
    """核心規則:取「最上面 療程=1 那一列」的卡號。

    card_cells: [(y, 卡號字串)]  ;  tiao_cells: [(y, 療字串)] ;
    header_y: 表頭那一列的 y(給定時多一道「頂部沒漏讀」幾何把關)。

    現在這張卡 = 最上面(y 最小)那一列的卡號 (cards[0])。安全把關 (計費欄):
      - high 必須同時滿足:
          a) 「最上面 療程=1 那列」的卡號 == 最上列卡號 (確認讀到的是『現在這張』卡
             的療程=1,而不是因為現在這張卡的療程=1 漏讀、抓到下面更舊的卡);
          b) 卡號是 4 位數字;
          c) 該卡號在卡號欄出現 >=2 次 (同卡多次回診,OCR 交叉一致);
          d) (給 header_y 時)最上列卡號貼近表頭 (= 沒把現在這張卡整組漏讀)。
      - 只要療程=1 那列卡號 != 最上列卡號 → 視為現在卡的療程=1 漏讀 → 不填。
      - 讀不到任何 療程=1 → 不自動填 (只回報,信心不足)。
    """
    cards = sorted((int(y), t.strip()) for y, t in card_cells
                   if CARD_RE.match(t.strip()))
    tiaos = sorted((int(y), t.strip()) for y, t in tiao_cells
                   if t.strip() in _TIAO_DIGITS)
    if not cards:
        return CardResult(None, "none", "OCR 讀不到任何卡號")

    top_card = cards[0][1]                       # 最上面 = 現在這張卡
    n_top = sum(1 for _, t in cards if t == top_card)

    ones = [y for y, t in tiaos if t == "1"]
    if not ones:
        return CardResult(None, "none" if n_top < 2 else "low",
                          f"讀不到療程=1(最上列卡號={top_card}),不自動填")

    y1 = min(ones)
    tiao1_card = _nearest(cards, y1, row_tol)
    if tiao1_card is None:
        return CardResult(None, "low",
                          f"療程=1 那列(y≈{y1})對不到卡號,不填")
    if tiao1_card != top_card:
        # 現在這張卡 (最上列) 的療程=1 沒被讀到,抓到的是更舊的卡 → 絕不可填
        return CardResult(
            None, "low",
            f"療程=1 卡號={tiao1_card} 與最上列={top_card} 不一致(疑現在卡漏讀),不填")
    if header_y is not None and not _top_row_near_header(
            [y for y, _ in cards],
            [y for y, _ in cards] + [y for y, _ in tiaos], header_y):
        return CardResult(None, "low",
                          f"最上列卡號={top_card} 離表頭過遠,疑頂部整組漏讀,保守不填")
    if len(top_card) == 4 and n_top >= 2:
        return CardResult(top_card, "high",
                          f"療程=1 最上列卡號={top_card}(與最近卡一致、出現 {n_top} 次)",
                          tiao=1)
    return CardResult(top_card, "low",
                      f"療程=1 卡號={top_card} 但交叉驗證不足(出現 {n_top} 次)", tiao=1)


# ───────────────────────── Windows 擷取 + OCR ─────────────────────────

def capture_grid_image(grid_hwnd: int, screen_rect: tuple):
    """螢幕 BitBlt 擷取格線矩形(視窗需顯示在最上層)。回 PIL.Image。

    screen_rect = (left, top, right, bottom) 螢幕座標。
    """
    import win32con  # type: ignore
    import win32gui  # type: ignore
    import win32ui   # type: ignore
    from PIL import Image

    left, top, right, bot = screen_rect
    w, h = right - left, bot - top
    if w <= 0 or h <= 0:
        raise RuntimeError(f"格線矩形異常 {w}x{h}")
    desktop = win32gui.GetDesktopWindow()
    hwnd_dc = mfc_dc = save_dc = bmp = prev_obj = None
    try:
        hwnd_dc = win32gui.GetWindowDC(desktop)
        mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
        save_dc = mfc_dc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(mfc_dc, w, h)
        prev_obj = save_dc.SelectObject(bmp)   # 留著等下還原,刪 bmp 才不會失敗洩漏
        save_dc.BitBlt((0, 0), (w, h), mfc_dc, (left, top), win32con.SRCCOPY)
        info = bmp.GetInfo()
        bits = bmp.GetBitmapBits(True)
        return Image.frombuffer(
            "RGB", (info["bmWidth"], info["bmHeight"]), bits, "raw", "BGRX", 0, 1)
    finally:
        if save_dc is not None and prev_obj is not None:
            try:
                save_dc.SelectObject(prev_obj)   # 先把 bmp 從 DC 取消選取
            except Exception:
                pass
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
                win32gui.ReleaseDC(desktop, hwnd_dc)
            except Exception:
                pass


_OCR_ROOT_CACHE: Optional[str] = None
_OCR_INSTALL_TRIED = False


def _bg_install_winsdk() -> None:
    """[M6 2026-07-09] 背景緒補裝 winsdk。【預設不在生產機 runtime pip install】—— 對 PyPI 做
    runtime 安裝有供應鏈風險、裝進共用環境會影響其他功能,pythonw 下 subprocess 還會閃 console。
    改為:預設只記錄提示「請部署時預先打包 winsdk」;只有明確設環境變數
    CMUH_ALLOW_WINSDK_AUTOINSTALL=1(dev/自願)才真的 pip install,且帶 CREATE_NO_WINDOW 不閃視窗。"""
    global _OCR_ROOT_CACHE
    import os
    if os.environ.get("CMUH_ALLOW_WINSDK_AUTOINSTALL", "").strip().lower() not in (
            "1", "true", "yes"):
        logging.warning(
            "[卡號OCR] winsdk 未安裝且未開放 runtime 安裝 → 卡號 OCR 停用(功能退回手動);"
            "請部署時預先打包 winsdk。(設 CMUH_ALLOW_WINSDK_AUTOINSTALL=1 才會自動安裝)")
        return
    try:
        import importlib
        import subprocess
        import sys
        logging.info("[卡號OCR] winsdk 未安裝,背景補裝中…(已由環境變數開放 runtime 安裝)")
        creationflags = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW,不閃 console
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "winsdk"],
                       check=False, timeout=300, creationflags=creationflags)
        importlib.import_module("winsdk.windows.media.ocr")
        _OCR_ROOT_CACHE = "winsdk"
        logging.info("[卡號OCR] winsdk 背景安裝完成,下次可用")
    except Exception:
        logging.warning("[卡號OCR] winsdk 背景安裝失敗,功能退回手動", exc_info=True)


def _resolve_ocr_root() -> Optional[str]:
    """找可用的 OCR 套件根名('winsdk'/'winrt')。缺了就『背景』補裝一次 winsdk。

    刻意不放進 REQUIRED_LIBS(那會 gate 主程式啟動);這裡 best-effort、fail-open。
    缺套件時:不在 hotkey 緒同步 pip install(會卡住 order),改丟背景緒裝,本次先
    回 None → 呼叫端退回手動;裝好後下次 F2/F3 才用。一個 process 只試裝一次。"""
    global _OCR_INSTALL_TRIED
    import importlib
    if _OCR_ROOT_CACHE:
        return _OCR_ROOT_CACHE
    for r in ("winsdk", "winrt"):
        try:
            importlib.import_module(f"{r}.windows.media.ocr")
            _set_ocr_root(r)
            return r
        except Exception:
            continue
    if not _OCR_INSTALL_TRIED:
        _OCR_INSTALL_TRIED = True
        try:
            import threading
            threading.Thread(target=_bg_install_winsdk, daemon=True).start()
        except Exception:
            logging.debug("[卡號OCR] 背景安裝緒啟動失敗", exc_info=True)
    return None


def _set_ocr_root(root: str) -> None:
    global _OCR_ROOT_CACHE
    _OCR_ROOT_CACHE = root


def _ocr_words_of_png(png_path: str, scale: float = 1.0) -> list[Word]:
    """用 Windows 內建 OCR(winsdk/winrt)認一張 PNG,回 Word 清單(座標已除以 scale)。"""
    import asyncio
    import importlib

    root = _resolve_ocr_root()
    if root is None:
        raise RuntimeError("找不到 OCR 套件(winsdk/winrt)")

    async def _run() -> list[Word]:
        ocr_mod = importlib.import_module(f"{root}.windows.media.ocr")
        img_mod = importlib.import_module(f"{root}.windows.graphics.imaging")
        sto_mod = importlib.import_module(f"{root}.windows.storage")
        f = await sto_mod.StorageFile.get_file_from_path_async(png_path)
        st = await f.open_async(sto_mod.FileAccessMode.READ)
        dec = await img_mod.BitmapDecoder.create_async(st)
        bmp = await dec.get_software_bitmap_async()
        eng = ocr_mod.OcrEngine.try_create_from_user_profile_languages()
        if eng is None:
            raise RuntimeError("系統沒有可用的 OCR 語言元件")
        res = await eng.recognize_async(bmp)
        words: list[Word] = []
        for line in res.lines:
            for wd in line.words:
                r = wd.bounding_rect
                words.append(Word(
                    text=wd.text,
                    x=int(r.x / scale), y=int(r.y / scale),
                    w=int(r.width / scale), h=int(r.height / scale)))
        return words

    return asyncio.run(_run())


def read_card_from_image(grid_img, *, tmp_dir,
                         save_debug: bool = False) -> CardResult:
    """給格線 PIL 圖,做兩段 OCR(整張認卡號 + 療欄裁切放大認療程數),回 CardResult。"""
    from PIL import Image  # noqa: F401
    import os

    # [L3 2026-07-09] 這些暫存 PNG 是【病人卡號格線的截圖(含 PHI)】。原本清除是行末 inline,
    # 早退(找不到卡號欄)或 OCR 例外時會【留下 PHI 檔在共用 %TEMP%】。改用 try/finally 保證清除
    # (save_debug 時才刻意保留供除錯)。收集所有寫出的暫存檔,離開時一律嘗試刪除。
    full_png = os.path.join(tmp_dir, "_ditto_card_full.png")
    tmp_pngs: list[str] = []
    try:
        # [codex P2] 先登記待清路徑再寫檔 —— save() 建/截檔後若拋錯(磁碟滿/IO error),path 已在
        # tmp_pngs → finally 仍會刪掉半成品 PHI 檔;反之(先寫後登記)拋錯就會漏一個檔在共用 %TEMP%。
        tmp_pngs.append(full_png)
        grid_img.save(full_png)
        words = _ocr_words_of_png(full_png, scale=1.0)

        card_x = find_card_column_x(words)
        tiao_x = find_tiao_column_x(words)
        if card_x is None:
            return CardResult(None, "none", "OCR 找不到『卡號』欄標題")

        card_cells = card_cells_from_words(words, card_x)

        tiao_cells: list[tuple[int, str]] = []
        if tiao_x is not None:
            # 療欄是單一數字,整張 OCR 常漏 → 裁切該欄 + 放大 5x 再認一次
            scale = 5
            x0 = max(0, int(tiao_x - 38))
            x1 = min(grid_img.width, int(tiao_x + 38))
            strip = grid_img.crop((x0, 0, x1, grid_img.height))
            strip = strip.resize((strip.width * scale, strip.height * scale))
            strip_png = os.path.join(tmp_dir, "_ditto_card_tiao.png")
            tmp_pngs.append(strip_png)   # [codex P2] 同上:先登記再寫,save 拋錯也保證被清
            strip.save(strip_png)
            tiao_words = _ocr_words_of_png(strip_png, scale=scale)
            # 裁切後 x 要加回 x0 才是原圖座標
            for w in tiao_words:
                w.x += x0
            tiao_cells = tiao_cells_from_words(tiao_words, tiao_x)

        header_y = min((w.y for w in _header_band(words)), default=None)
        result = pick_card_number(card_cells, tiao_cells, header_y=header_y)
        logging.info("[卡號OCR] %s (card=%s conf=%s 卡號列=%d 療列=%d header_y=%s)",
                     result.reason, result.card, result.confidence,
                     len(card_cells), len(tiao_cells), header_y)
        return result
    finally:
        # 一律清除 PHI 暫存(save_debug 才保留);任一刪除失敗不影響其他檔與回傳。
        if not save_debug:
            for p in tmp_pngs:
                try:
                    os.remove(p)
                except Exception:
                    pass
