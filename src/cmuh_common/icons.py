# -*- coding: utf-8 -*-
"""中國醫校徽圖示產製。搬自原主程式 line 400-506 的 _ensure_cmuh_app_icon_path。

第一次啟動時會自維基共享資源下載校徽 PNG 並合成 ICO。
之後讀本地 cache，除非 _CMUH_ICON_ASSET_VERSION 升版才重新產製。
"""
import logging
import os

from cmuh_common.paths import get_app_dir

# 變更圖示來源／演算法時遞增，會觸發重新下載產製
_CMUH_ICON_ASSET_VERSION = 7

_CMU_LOGO_PNG_URLS = (
    "https://upload.wikimedia.org/wikipedia/zh/thumb/d/dd/China_Medical_University_%28Taiwan%29_logo.svg/2000px-China_Medical_University_%28Taiwan%29_logo.svg.png",
    "https://upload.wikimedia.org/wikipedia/zh/thumb/d/dd/China_Medical_University_%28Taiwan%29_logo.svg/1000px-China_Medical_University_%28Taiwan%29_logo.svg.png",
    "https://upload.wikimedia.org/wikipedia/zh/thumb/d/dd/China_Medical_University_%28Taiwan%29_logo.svg/500px-China_Medical_University_%28Taiwan%29_logo.svg.png",
    "https://upload.wikimedia.org/wikipedia/zh/thumb/d/dd/China_Medical_University_%28Taiwan%29_logo.svg/250px-China_Medical_University_%28Taiwan%29_logo.svg.png",
)
_WIKI_REQUEST_HEADERS = {
    "User-Agent": "CMUH-Dermatology-ClinicalTool/1.0 (internal hospital use; Python requests)",
    "Accept": "image/png,image/*;q=0.8,*/*;q=0.5",
}


def ensure_cmuh_app_icon_path() -> str | None:
    """回傳 assets/cmuh_app.ico；必要時自維基共享資源產製。"""
    assets_dir = os.path.join(get_app_dir(), "assets")
    ico_path = os.path.join(assets_dir, "cmuh_app.ico")
    ver_path = os.path.join(assets_dir, "cmuh_icon_version.txt")

    need_build = True
    if os.path.isfile(ico_path) and os.path.getsize(ico_path) >= 800:
        try:
            if os.path.isfile(ver_path):
                with open(ver_path, "r", encoding="ascii", errors="ignore") as vf:
                    need_build = int(vf.read().strip()) < _CMUH_ICON_ASSET_VERSION
            else:
                need_build = True
        except Exception:
            need_build = True
    if not need_build:
        return ico_path

    try:
        os.makedirs(assets_dir, exist_ok=True)
    except OSError:
        return None

    try:
        from io import BytesIO
        import requests
        from PIL import Image, ImageDraw

        response = None
        last_err: Exception | None = None
        for url in _CMU_LOGO_PNG_URLS:
            try:
                response = requests.get(url, timeout=30, verify=True, headers=_WIKI_REQUEST_HEADERS)
                response.raise_for_status()
                break
            except Exception as e:
                last_err = e
                response = None
        if response is None:
            raise last_err or RuntimeError("no logo URL succeeded")

        src = Image.open(BytesIO(response.content)).convert("RGBA")
        sw, sh = src.size
        canvas_sz = 1024
        canvas = Image.new("RGBA", (canvas_sz, canvas_sz), (0, 0, 0, 0))
        cx = cy = canvas_sz // 2
        r = int(canvas_sz * 0.42)
        draw = ImageDraw.Draw(canvas)
        draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(232, 234, 232, 255))
        inner = max(8, int(r * 0.12))
        max_d = 2 * (r - inner)
        scale = min(max_d / sw, max_d / sh)
        nw = max(1, int(sw * scale))
        nh = max(1, int(sh * scale))
        scaled = src.resize((nw, nh), Image.Resampling.LANCZOS)
        ox = (canvas_sz - nw) // 2
        oy = (canvas_sz - nh) // 2
        canvas.paste(scaled, (ox, oy), scaled)

        # ICO 必須最大圖層寫在最前面，否則 Windows/Tk 常誤用 16×16 整顆糊掉
        def _layer(sz):
            w, h = sz
            try:
                return canvas.resize((w, h), Image.Resampling.LANCZOS, reducing_gap=2.0)
            except TypeError:
                return canvas.resize((w, h), Image.Resampling.LANCZOS)

        sizes = [
            (256, 256), (128, 128), (64, 64), (48, 48), (40, 40),
            (32, 32), (24, 24), (20, 20), (16, 16),
        ]
        imgs = [_layer(s) for s in sizes]
        save_kw = {
            "format": "ICO",
            "sizes": [(s[0], s[1]) for s in sizes],
            "append_images": imgs[1:],
        }
        try:
            imgs[0].save(ico_path, bitmap_format="png", **save_kw)
        except TypeError:
            imgs[0].save(ico_path, **save_kw)
        try:
            with open(ver_path, "w", encoding="ascii") as vf:
                vf.write(str(_CMUH_ICON_ASSET_VERSION))
        except OSError:
            pass
        logging.info("已產製視窗圖示 v%s: %s", _CMUH_ICON_ASSET_VERSION, ico_path)
        return ico_path
    except Exception as e:
        logging.warning("無法下載或產製中國醫藥大學圖示檔: %s", e)
        return None
