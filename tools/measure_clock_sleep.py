# -*- coding: utf-8 -*-
"""量測:這台機器睡眠/休眠期間,三個時鐘各自走了多少。

[第九輪 §5-3] watchdog 的喚醒守衛靠「醒著的時間不含睡眠」這個性質
(`QueryUnbiasedInterruptTime`,文件明定如此);退回 `time.monotonic()` 時它含不含睡眠
沒有實測(fetch_resilience 的 docstring 也說了同一件事)。這支腳本在診間機器跑一次
就有答案,★不進 CI★。

用法(在診間電腦上):
    python tools/measure_clock_sleep.py
    → 它每 5 秒印一行;讓電腦睡眠 3~5 分鐘再喚醒,看喚醒後那一行的三個 Δ:
       Δwall      應 ≈ 睡眠時間(牆上時鐘一定走)
       Δunbiased  應 ≈ 5 秒左右(不含睡眠 → 守衛能分辨「睡過」)
       Δmonotonic 若 ≈ Δwall → monotonic 含睡眠(退路無保護力,但不會誤隔離)
                  若 ≈ Δunbiased → monotonic 不含睡眠(退路也有保護力)
    Ctrl+C 結束。
"""
import ctypes
import sys
import time


def unbiased_now() -> float:
    t = ctypes.c_ulonglong(0)
    ok = ctypes.windll.kernel32.QueryUnbiasedInterruptTime(ctypes.byref(t))
    return t.value / 1e7 if ok else float("nan")


def main() -> int:
    if sys.platform != "win32":
        print("這支只在 Windows 上有意義(QueryUnbiasedInterruptTime)。")
        return 1
    print("每 5 秒一行;請讓電腦睡眠幾分鐘再喚醒,看喚醒後那一行。Ctrl+C 結束。")
    print(f"{'時間':19s} {'Δwall':>9s} {'Δunbiased':>10s} {'Δmonotonic':>11s}  判讀")
    pw, pu, pm = time.time(), unbiased_now(), time.monotonic()
    try:
        while True:
            time.sleep(5)
            w, u, m = time.time(), unbiased_now(), time.monotonic()
            dw, du, dm = w - pw, u - pu, m - pm
            note = ""
            if dw > 60:
                note = ("剛睡過 → monotonic "
                        + ("含睡眠(退路無保護力)" if abs(dm - dw) < abs(dm - du)
                           else "不含睡眠(退路有保護力)"))
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {dw:9.1f} {du:10.1f} {dm:11.1f}  {note}")
            pw, pu, pm = w, u, m
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
