# -*- coding: utf-8 -*-
"""Tkinter UI 小工具 — main.py / scheduler.py 共用。

【重構 2026-05-21】兩個 byte-identical 純函式抽出來。
"""
from __future__ import annotations


def manage_scrollbar(scrollbar_widget, text_widget) -> None:
    """依據 text widget 內容多寡決定顯示/隱藏 scrollbar。"""
    text_widget.update_idletasks()
    if float(text_widget.index('end-1c').split('.')[0]) <= text_widget.cget('height'):
        scrollbar_widget.pack_forget()
    else:
        scrollbar_widget.pack(side="right", fill="y")


def format_vertical_text(text: str) -> str:
    """把文字逐字換行（直書效果）。"""
    return "\n".join(list(text))
