# -*- coding: utf-8 -*-
"""定案 PDF 留底（reportlab，重依賴 → 呼叫端負責 lazy 安裝）。

把整月排班決策報告（R / VS / PGY-Clerk）render 成 PDF 存檔留底。繁體中文以
reportlab 內建 CID 字型 MSung-Light（不需外掛 TTF）呈現。
"""
from __future__ import annotations

_FONT = "MSung-Light"        # reportlab 內建繁中 CID 字型（CNS-CS）


def _wrap(s: str, measure, max_w: float) -> list:
    """把過寬的行逐字斷行，避免超出頁面右緣被裁掉。

    measure(str)->寬度（pt）。空字串回 ['']（保留空行間距）。
    """
    if not s or measure(s) <= max_w:
        return [s]
    out: list = []
    cur = ""
    for ch in s:
        if cur and measure(cur + ch) > max_w:
            out.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out


def export(path: str, sections: list) -> None:
    """sections: [(標題, 內文多行字串), ...]，每段各自換頁。"""
    try:
        from reportlab.lib.pagesizes import A4  # noqa: PLC0415
        from reportlab.pdfbase import pdfmetrics  # noqa: PLC0415
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont  # noqa: PLC0415
        from reportlab.pdfgen import canvas  # noqa: PLC0415
    except ImportError as e:
        raise RuntimeError("未安裝 reportlab（PDF 留底）；請按提示安裝後重試。") from e

    try:
        pdfmetrics.registerFont(UnicodeCIDFont(_FONT))
    except Exception:                       # 已註冊或環境缺字型 → 忽略（drawString 仍可）
        pass

    c = canvas.Canvas(path, pagesize=A4)
    width, height = A4
    left, top, bottom = 40, height - 48, 40
    line_h = 12
    max_w = width - left - 40                # 內文可印寬度（左右各留 40pt）

    def _measure(t: str) -> float:
        return pdfmetrics.stringWidth(t, _FONT, 9)

    for title, text in sections:
        y = top
        c.setFont(_FONT, 14)
        c.drawString(left, y, str(title))
        y -= 22
        c.setFont(_FONT, 9)
        for line in str(text).split("\n"):
            for seg in _wrap(line, _measure, max_w):   # 過長行斷行，避免右緣被裁
                if y < bottom:
                    c.showPage()
                    c.setFont(_FONT, 9)
                    y = top
                c.drawString(left, y, seg)
                y -= line_h
        c.showPage()
    if not sections:                        # 空內容也產一頁避免壞檔
        c.setFont(_FONT, 12)
        c.drawString(left, top, "（本月無可留底之排班報告）")
        c.showPage()
    c.save()
