# -*- coding: utf-8 -*-
"""Tk 長駐程式的兩個穩定性地基：回呼例外要看得見、log 視窗要有上限。

【為什麼需要這個模組（2026-08-10 穩定性總體檢）】

★問題 1：pythonw 下，Tk 回呼裡的例外是【完全無聲】的★
Tk 對回呼（button command / bind / `after`）裡的例外只做一件事：呼叫
`root.report_callback_exception`，預設印到 stderr —— 而 `.pyw` 沒有 stderr。
四支 Tk 程式**沒有任何一支**覆寫它：任何回呼例外不進 log、不彈窗、
什麼痕跡都沒有。診間看到的只是「某個功能從某天起不動了」。

★問題 2：自我重排的 `after` 迴圈，例外 = 永久停擺★
`def loop(): 做事; root.after(n, loop)` 這個形狀，「做事」一拋例外就走不到
重排 —— 整條迴圈**死掉且無聲**（配合問題 1）。實際案例：
`main._update_clinic_lights_loop` 有 596 行本體、零個頂層 try、重排在最後
一行；它一死，reg64 診間燈號 / 浮動視窗 / 現場人數整條管線凍結到重啟為止。

★問題 3：log 視窗的 Text widget 沒有行數上限★
會診查詢與打卡程式的 log 輪詢只插入、從不刪除 —— 常駐數週後 Text 抱著
幾十萬行字串不放。主程式自己有截 500 行，另外兩支沒有（同一個功能寫了
三份，漂掉兩份）。

這裡提供三件事，四支程式共用同一份實作：
* `install_callback_exception_logger(root, program)` —— 把無聲變成 log
* `log_and_throttle()` —— 例外重複發生時不要洗版（5 秒迴圈=每小時 720 條）
* `pump_log_records(widget, queue)` —— 有例外保護、有行數上限的 log 幫浦
"""
from __future__ import annotations

import logging
import time
from queue import Empty

from cmuh_common.memory_cache import trim_oldest_entries

#: 同一個例外簽名，至少隔這麼久才再記一次完整 traceback（其間只數次數）。
THROTTLE_SEC = 60.0
#: 例外簽名表的上限（簽名 = 例外型別 + 最深的那一格 frame）。
_MAX_SIGNATURES = 128

#: log 視窗預設行數上限（與主程式既有的 500 行一致）。
DEFAULT_MAX_LINES = 500
#: 一次輪詢最多吃幾筆（別讓一波 log 洪水卡死 UI 主緒）。
DEFAULT_MAX_RECORDS = 80


class ThrottledExceptionLog:
    """「同一個地方一直炸」時的節流器。

    ★為什麼不能每次都 logging.exception★
    被保護的迴圈通常 5 秒重排一次；一個持續性的壞（widget 被 destroy、
    設定檔壞掉）會以每小時 720 條完整 traceback 的速度灌爆 5MB 的
    RotatingFileHandler —— 真正重要的其他訊息在幾小時內被輪替掉。

    ★但也絕不可以完全靜音★ 第一次一定記完整 traceback；之後每隔
    `THROTTLE_SEC` 記一次「這段期間又發生了 N 次」。
    """

    def __init__(self, throttle_sec: float = THROTTLE_SEC):
        self._throttle = float(throttle_sec)
        self._seen: dict = {}      # sig -> [last_full_log_monotonic, 抑制次數]

    @staticmethod
    def _signature(exc_type, tb) -> tuple:
        deepest = ("?", 0)
        while tb is not None:
            code = tb.tb_frame.f_code
            deepest = (code.co_filename, tb.tb_lineno)
            tb = tb.tb_next
        return (getattr(exc_type, "__name__", str(exc_type)), *deepest)

    def log(self, where: str, exc_type, exc, tb) -> bool:
        """記下這一次。→ True = 有記完整 traceback（False = 被節流，只累計）。"""
        now = time.monotonic()
        sig = self._signature(exc_type, tb)
        row = self._seen.get(sig)
        if row is not None and (now - row[0]) < self._throttle:
            row[1] += 1
            return False
        suppressed = row[1] if row else 0
        self._seen[sig] = [now, 0]
        trim_oldest_entries(self._seen, _MAX_SIGNATURES,
                            timestamp_of=lambda v: v[0])
        extra = f"（前 {self._throttle:.0f}s 內另有 {suppressed} 次相同例外被節流）" \
            if suppressed else ""
        logging.error("[tk] %s 回呼例外%s", where, extra,
                      exc_info=(exc_type, exc, tb))
        return True


def install_callback_exception_logger(root, program: str,
                                      throttle_sec: float = THROTTLE_SEC):
    """把 Tk 回呼例外從「無聲消失」變成「進 log（有節流）」。

    ★pythonw 沒有 stderr★ Tk 預設把回呼例外印到 stderr —— 在 `.pyw` 下
    那等於 /dev/null。裝上這個之後，button/bind/after 裡的任何例外至少
    留得下痕跡，「某功能從某天起不動了」才查得出來是哪一行。

    → 回傳 handler（測試用）。任何一步失敗都不可以影響啟動。
    """
    throttled = ThrottledExceptionLog(throttle_sec)

    def _report(exc_type, exc, tb):
        # ★這個函式自己絕不可以拋★ 它就是最後一道網。
        try:
            throttled.log(program, exc_type, exc, tb)
        except Exception:  # noqa: BLE001  最後一道網的失敗只能吞
            try:
                logging.error("[tk] %s 回呼例外（節流器自身失敗）", program)
            except Exception:  # noqa: BLE001
                pass

    try:
        root.report_callback_exception = _report
    except Exception:  # noqa: BLE001  裝不上也不能擋住啟動
        logging.debug("[tk] 安裝回呼例外記錄器失敗", exc_info=True)
    return _report


def format_log_record(record) -> str:
    """單筆 LogRecord → 一行文字。★getMessage() 可能拋★

    `logging.warning("%d", "x")` 這種格式錯誤是在 **getMessage() 被呼叫時**
    才爆的 —— 也就是在 log 視窗的輪詢緒，不是在寫 log 的那一行。
    一筆壞紀錄不可以殺掉整條 log 幫浦。
    """
    try:
        stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
        return f"{stamp} [{record.levelname}] {record.getMessage()}\n"
    except Exception:  # noqa: BLE001
        return f"?? [{getattr(record, 'levelname', '?')}] <log 格式化失敗:" \
               f" {getattr(record, 'msg', '')!r}>\n"


def pump_log_records(text_widget, log_queue, *,
                     formatter=None,
                     max_records: int = DEFAULT_MAX_RECORDS,
                     max_lines: int = DEFAULT_MAX_LINES) -> bool:
    """把 log queue 裡的紀錄倒進 Text widget。→ 有沒有做到事。

    三支程式各寫了一份這個功能，其中兩份【沒有行數上限】——
    常駐數週後 Text widget 抱著幾十萬行不放。統一成這一份：

    * 每筆格式化各自 try（一筆壞的不殺全部）；
    * 超過 `max_lines` 就從頭刪（保留最新的）；
    * widget 操作整段 try（視窗關閉瞬間的 TclError 不往上炸 ——
      呼叫端的重排必須是無條件的，見各程式的輪詢迴圈）。
    """
    fmt = formatter or format_log_record
    lines = []
    for _ in range(int(max_records)):
        try:
            rec = log_queue.get_nowait()
        except Empty:
            break
        except Exception:  # noqa: BLE001  queue 本身壞掉也不可以殺幫浦
            break
        try:
            lines.append(fmt(rec))
        except Exception:  # noqa: BLE001
            lines.append("?? <log 格式化失敗>\n")
    if not lines:
        return False
    try:
        text_widget.configure(state="normal")
        text_widget.insert("end", "".join(lines))
        line_count = int(str(text_widget.index("end-1c")).split(".")[0])
        if line_count > int(max_lines):
            # 與主程式既有行為一致:砍到剩 max_lines 的八成,避免每輪都在刪。
            keep = max(1, int(max_lines) * 4 // 5)
            text_widget.delete("1.0", f"{line_count - keep}.0")
        text_widget.see("end")
        text_widget.configure(state="disabled")
    except Exception:  # noqa: BLE001  視窗正在關 → 丟掉這一批,幫浦活著就好
        logging.debug("[tk] log 視窗更新失敗(丟棄本批 %d 行)", len(lines),
                      exc_info=True)
    return True
