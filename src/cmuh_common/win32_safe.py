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

★[2026-08-10 批次SB #4] 「偶發洩一條」要有上限★
HIS 凍結持續期間,使用者每按一次熱鍵就再洩一條(3 秒 timeout 很快回來,
使用者以為沒按到就一直按)。無上限的話:native-blocked threads、ctypes
callback、Event/closure 逐條堆積,最後 thread 建不出來、熱鍵全面失效 ——
為了「不卡死」而放生的東西,累積起來又把程式弄死。
同名(name)的放生 thread 有上限;滿了就直接回 default,不再疊加
(與 consult 的 IMAP single-flight 同一個立場:上一條還卡著就不開新的)。
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

# 視窗列舉/尋找的預設逾時:HIS 正常時 <50ms;逾時代表 GUI 執行緒凍結。
WIN_ENUM_TIMEOUT_SEC = 3.0

#: 同名放生 thread 的上限。到頂 = HIS 已持續凍結一陣子,再開也只是多洩一條。
MAX_STRANDED_PER_NAME = 4

#: name -> 仍卡著的 thread 清單(收斂時自動剔除;只在鎖內動)。
_stranded: dict = {}
_stranded_lock = threading.Lock()


def _occupies_slot(t: threading.Thread) -> bool:
    """這條 thread 還占不占額度。

    ★不可以只看 is_alive()★ 佔位發生在 start() 之前(檢查與佔位要在同一個
    臨界區),而【還沒 start 的 thread】is_alive() 也是 False —— 併發窗內
    別的呼叫會把它當成死的剔掉,上限又被繞過(同一個競態換個位置)。
    `ident is None` = 還沒 start = 仍占額度;started 且不 alive 才是真的結束。
    """
    return t.ident is None or t.is_alive()


def _stranded_count(name: str) -> int:
    """同名還卡著幾條。★順便把已經結束的剔掉★(HIS 恢復後它們會自己收尾)。"""
    with _stranded_lock:
        alive = [t for t in _stranded.get(name, ()) if _occupies_slot(t)]
        if alive:
            _stranded[name] = alive
        else:
            _stranded.pop(name, None)
        return len(alive)


def call_with_timeout(fn: Callable[[], Any], timeout_sec: float = WIN_ENUM_TIMEOUT_SEC,
                      default: Optional[Any] = None,
                      name: str = "win32-call") -> Any:
    """在 daemon thread 執行 fn(),最多等 timeout_sec 秒。

    - 正常完成:回 fn() 的結果。
    - fn() 內拋例外:吞掉、回 default(fail-open)。
    - 逾時(通常代表 HIS GUI 凍結):回 default,並讓卡住的 thread 自生自滅。
    - ★同名已有 MAX_STRANDED_PER_NAME 條卡著:不再開新的,直接回 default★
      (HIS 凍結期間使用者反覆按熱鍵,無上限會把 thread 堆到建不出來)。

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
    # ★[外審 SB 第 1 輪] 檢查與佔位必須在同一個臨界區★
    #   第一版是「鎖內數、鎖外開」:HIS 凍結時多個熱鍵回呼同時進來,
    #   每一條都數到 <上限 → 全部開 → 上限形同虛設。
    #   改成:建好 thread 先【登記佔位】(同一把鎖內數+入列)才 start;
    #   正常完成就把自己移掉(釋放額度),逾時的留著,等它真的結束後
    #   由 is_alive 剔除。代價:執行中(尚未逾時)的呼叫也占額度 ——
    #   同名同時 4 條在跑本身就是病態,擋掉第 5 條是對的。
    with _stranded_lock:
        alive = [x for x in _stranded.get(name, ()) if _occupies_slot(x)]
        if len(alive) >= MAX_STRANDED_PER_NAME:
            if alive:
                _stranded[name] = alive
            else:
                _stranded.pop(name, None)
            logging.warning(
                "[win32_safe] %s 已有 %d 條 thread 執行中/卡著 → 不再疊加,"
                "直接回 default(HIS 凍結中;它們恢復後會自行收斂)", name,
                MAX_STRANDED_PER_NAME)
            return default
        alive.append(t)
        _stranded[name] = alive
    try:
        t.start()
    except Exception:
        # ★[外審 SB 第 2 輪 #4] start 失敗必須釋放佔位★
        #   「can't start new thread」是暫時性的;佔位的 thread 永遠不會有
        #   ident,`_occupies_slot` 會把它當成永久占用 —— 四次失敗之後
        #   這個呼叫點就【永久停用】,資源恢復了也救不回來。
        with _stranded_lock:
            lst = _stranded.get(name)
            if lst is not None and t in lst:
                lst.remove(t)
                if not lst:
                    _stranded.pop(name, None)
        logging.warning("[win32_safe] %s 開不出 thread → 回 default(佔位已釋放)",
                        name, exc_info=True)
        return default
    if not done.wait(timeout_sec):
        logging.warning(
            "[win32_safe] %s 逾時 %.1fs → fail-open 回 default(HIS GUI 可能凍結)",
            name, timeout_sec)
        return default          # ★逾時 → 佔位留著,等 thread 真結束才釋放★
    with _stranded_lock:
        lst = _stranded.get(name)
        if lst is not None and t in lst:
            lst.remove(t)
            if not lst:
                _stranded.pop(name, None)
    return result[0]
