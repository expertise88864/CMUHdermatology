# -*- coding: utf-8 -*-
"""Tk 小工具：**只在真的變了才重畫**。
（P2-06 分層第五刀(a) 第二批 2026-08-02，從 `AutomationApp` 搬出）

【為什麼是一組】
兩支都在做同一件事：先比對現況、相同就整個跳過。診間的行事曆與門診進度是
每秒重繪的，`widget.config()` 會觸發 Tk 的重新排版；不比對就重繪會讓整個
視窗持續閃爍、也吃 CPU。

【★不 import tkinter★】
兩支都只呼叫 widget 的 `cget` / `config`，不需要 Tk 的任何型別。不 import 的
好處是它們可以用假的 widget 物件測 —— 而這正是它們留在 `AutomationApp` 裡
最缺的東西（原本只有開得起 Tk 的整支 app 才驗得到）。
"""
from __future__ import annotations


def config_if_changed(widget, **kwargs) -> bool:
    """只有在任何一個選項與現值不同時才 `config()`。回傳有沒有真的重設。

    ★`cget` 失敗一律當成「有變」★ 讀不到現值就無從比較，這時跳過重繪會讓畫面
    停在舊資料上 —— 那比多一次重繪嚴重得多。（實際會拋的情況：該 widget 不支援
    這個選項、或它已經被 destroy。後者接著 `config()` 也會拋，由呼叫端處理。）
    """
    changed = False
    for key, value in kwargs.items():
        try:
            if widget.cget(key) != value:
                changed = True
                break
        except Exception:      # noqa: BLE001  見 docstring：讀不到＝當成有變
            changed = True
            break
    if changed:
        widget.config(**kwargs)
    return changed


def apply_calendar_slot_state(slot, name_text, status_text, bg_color,
                              fg_color, font_style) -> bool:
    """把一格行事曆的完整外觀套上去（相同就跳過）。回傳有沒有真的更新。

    ★比對的是整組狀態，不是逐項★ 一格由三個 widget 組成（卡片、姓名、狀態），
    它們必須一起換：只換其中一個會出現「姓名已經是新醫師、底色還是舊班別」的
    中間狀態。所以用一個 tuple 當這一格的指紋，存在 `slot["_state"]`。
    """
    new_state = (name_text, status_text, bg_color, fg_color, font_style)
    if slot.get("_state") == new_state:
        return False
    slot["_state"] = new_state
    slot["card"].config(bg=bg_color)
    slot["name_lbl"].config(text=name_text, bg=bg_color, fg=fg_color)
    slot["status_lbl"].config(text=status_text, bg=bg_color, fg=fg_color,
                              font=font_style)
    return True
