# -*- coding: utf-8 -*-
"""[2026-06-16 韌性] 來源級熔斷器:跳閘 + 逾 30 分鐘自動重置。

原本一旦連 3 次失敗就整個 session 熔斷,要重啟程式才恢復(醫院短暫維護就讓某來源
一整個下午沒資料)。改為定時自我恢復,本檔固定該行為。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import main  # noqa: E402


def test_circuit_breaker_trips_after_threshold():
    src = "ut_src_a"
    main._circuit_record_success(src)              # 清乾淨
    assert main._circuit_is_tripped(src) is False
    assert main._circuit_record_fail(src) is False  # 1
    assert main._circuit_record_fail(src) is False  # 2
    assert main._circuit_record_fail(src) is True   # 3 → 剛跳閘
    assert main._circuit_is_tripped(src) is True
    main._circuit_record_success(src)               # 成功 → 解除
    assert main._circuit_is_tripped(src) is False


def test_circuit_breaker_auto_resets_after_window(monkeypatch):
    src = "ut_src_b"
    main._circuit_record_success(src)
    clock = [1000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: clock[0])
    for _ in range(3):
        main._circuit_record_fail(src)
    assert main._circuit_is_tripped(src) is True            # 跳閘中
    clock[0] += main._CIRCUIT_BREAKER_RESET_SEC + 1.0       # 過了重置窗
    assert main._circuit_is_tripped(src) is False           # 自動重置放行
    # 重置後計數歸零:要再連 3 次失敗才會再次跳閘
    assert main._circuit_record_fail(src) is False
    assert main._circuit_is_tripped(src) is False
    main._circuit_record_success(src)


def test_circuit_breaker_stays_tripped_within_window(monkeypatch):
    src = "ut_src_c"
    main._circuit_record_success(src)
    clock = [5000.0]
    monkeypatch.setattr(main.time, "monotonic", lambda: clock[0])
    for _ in range(3):
        main._circuit_record_fail(src)
    clock[0] += main._CIRCUIT_BREAKER_RESET_SEC - 60.0      # 還沒到重置窗
    assert main._circuit_is_tripped(src) is True            # 仍熔斷
    main._circuit_record_success(src)
