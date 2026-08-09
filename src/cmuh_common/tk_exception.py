# -*- coding: utf-8 -*-
"""Tk callback exception handler — 共用模組。

Tk 預設 `report_callback_exception` 把例外 print 到 stderr，pythonw.exe 模式
下完全看不到，等於黑箱。安裝這個 handler 後，所有 `.after()` / 事件 binding
callback 拋的例外都會進 logging 系統，後續可以從 log 看完整 traceback。

主程式 main.py 已用過此 pattern，本模組把它抽出來給 scheduler.py /
consult_query.py / autoclock.py 共用 — 那三支也都有 Tk UI (設定視窗 / tray
互動 / config dialog)，原本各自的 callback 例外都漏進 stderr 黑洞。
"""
from __future__ import annotations

import logging
from typing import Optional


# ★[2026-08-10 批次SA] 單一真相來源★
# 這個模組原本自己 logging.error(無節流)。批次SA 在 tk_stability 做了
# 【每簽名節流】的實作(5 秒自我重排的迴圈一直炸 = 每小時 720 條完整
# traceback,幾小時就把 5MB 輪替灌爆),第一版卻是【另外裝一套】——
# 結果被這裡的舊 handler 蓋掉(外審抓到)。現在:舊 API 保留(四支程式
# 的生產 root 都接在它上面),內部委派給節流實作;不再有第二套。
_THROTTLED = None
_PROGRAM_LABEL = ["Tk"]


def _get_throttled():
    global _THROTTLED
    if _THROTTLED is None:
        from cmuh_common.tk_stability import ThrottledExceptionLog
        _THROTTLED = ThrottledExceptionLog()
    return _THROTTLED


def _report_callback_exception(*args) -> None:
    """Tk override hook — callback 例外 → logging(有節流)。

    [IF-01] 用 *args 相容兩條指派路徑,否則 class-attr 路徑被呼叫必拋 TypeError、原始例外遺失:
      - 設成 instance attr(root.report_callback_exception=...)→ Tk 呼叫傳 (exc, val, tb) 3 個;
      - 設成 class attr(tk.Tk.report_callback_exception=...)→ 變 descriptor,instance 呼叫會綁定
        self → (self, exc, val, tb) 4 個。取最後三個即為 (exc, val, tb),兩路徑都不炸。

    ★這個函式自己絕不可以拋★ 它是最後一道網。
    """
    try:
        exc, val, tb = args[-3:]
        _get_throttled().log(_PROGRAM_LABEL[0], exc, val, tb)
    except Exception:  # noqa: BLE001  最後一道網的失敗只能吞
        try:
            logging.error("Uncaught Tk callback exception(節流器自身失敗)")
        except Exception:  # noqa: BLE001
            pass


def install_tk_exception_handler(root: Optional[object] = None,
                                 program: str = "") -> bool:
    """安裝 Tk callback exception handler。

    root: 已建立的 Tk root instance (主程式 main_root / 設定視窗 self)。
          傳入後既覆蓋該 instance 的 hook，也 patch tk.Tk class itself
          (讓後續 Toplevel 自動繼承)。
          None → 只 patch class，給「import 時就先設好」場景用。

    program: 程式名(進 log 用,例如「主程式」)。多支程式共用 class-attr
             hook,標籤是全域的 —— 最後一個裝的贏;同一個 process 只有
             一支程式,所以這樣就夠了。

    回傳：True 安裝成功，False 例外吞掉 (不阻擋呼叫端流程)。
    """
    if program:
        _PROGRAM_LABEL[0] = str(program)
    try:
        import tkinter as tk  # noqa: F401 — late import 避免無 Tk 環境炸
        if root is not None:
            try:
                root.report_callback_exception = _report_callback_exception
            except Exception:
                logging.debug("Tk root report_callback_exception 設定失敗",
                              exc_info=True)
        # 同時 patch class，讓後續 Toplevel 自動繼承
        try:
            tk.Tk.report_callback_exception = _report_callback_exception
        except Exception:
            logging.debug("Tk.Tk class hook patch 失敗", exc_info=True)
        return True
    except Exception:
        logging.debug("install_tk_exception_handler 例外", exc_info=True)
        return False
