# -*- coding: utf-8 -*-
"""Win32 安全呼叫小工具(W2 2026-07-03,共用 Win32 層的種子)。

問題:醫院 HIS(Delphi)GUI 執行緒凍結時,某些 Win32 呼叫(尤其 callback 內的
raw GetWindowTextW = 送 WM_GETTEXT 給凍結視窗)會【無限期阻塞】呼叫執行緒。若這發生
在熱鍵工作緒或視窗尋找,整個熱鍵子系統會卡死(finally 不執行、之後所有熱鍵報「前一個
尚未完成」)。

對策:把可能阻塞的同步呼叫丟到 daemon thread 執行 + join(timeout);逾時就 fail-open
回 default(呼叫端當作「沒找到」),讓呼叫執行緒解脫。卡住的 daemon thread 自生自滅
(HIS 恢復回應後會結束;Python 無法安全 kill 卡在 Win32 的 thread,這是可接受的取捨:
偶發洩一條 thread << 永久卡死整個熱鍵)。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

# 視窗列舉/尋找的預設逾時:HIS 正常時 <50ms;逾時代表 GUI 執行緒凍結。
WIN_ENUM_TIMEOUT_SEC = 3.0


def call_with_timeout(fn: Callable[[], Any], timeout_sec: float = WIN_ENUM_TIMEOUT_SEC,
                      default: Optional[Any] = None,
                      name: str = "win32-call") -> Any:
    """在 daemon thread 執行 fn(),最多等 timeout_sec 秒。

    - 正常完成:回 fn() 的結果。
    - fn() 內拋例外:吞掉、回 default(fail-open)。
    - 逾時(通常代表 HIS GUI 凍結):回 default,並讓卡住的 thread 自生自滅。

    注意:fn 應為「自足、無副作用依賴呼叫緒」的函式(Win32 列舉/尋找符合)。逾時後
    卡住的 thread 仍可能在稍後才寫 result,但呼叫端已拿到 default,不會用到它。
    """
    result: list = [default]
    done = threading.Event()

    def _run() -> None:
        try:
            result[0] = fn()
        except Exception:
            logging.debug("[win32_safe] %s 執行例外(回 default)", name, exc_info=True)
        finally:
            done.set()

    t = threading.Thread(target=_run, name=name, daemon=True)
    t.start()
    if not done.wait(timeout_sec):
        logging.warning(
            "[win32_safe] %s 逾時 %.1fs → fail-open 回 default(HIS GUI 可能凍結)",
            name, timeout_sec)
        return default
    return result[0]
