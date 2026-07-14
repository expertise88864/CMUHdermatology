# -*- coding: utf-8 -*-
"""主程式 2026-07-14 兩項修正：

1. 打卡狀態查詢：常駐 Chrome 放著跨過閒置逾時後,打卡網站殘留的 JS alert
   (「閒置時間過長，將被導向登入畫面」) 會讓下一次 driver.get 拋
   UnexpectedAlertPresentException、整個查詢失敗 → 查詢開頭先清掉殘留 alert。
2. 縮寫速寫頁面移除「啟用後自動：中文組字…」說明文字（三項行為固定自動開啟、不再說明）。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402


# ── 假 driver：模擬「頁面上有殘留 alert」 ─────────────────────────────────────
class _FakeAlert:
    def __init__(self, text):
        self.text = text
        self.accepted = False

    def accept(self):
        self.accepted = True


class _FakeSwitchTo:
    def __init__(self, alerts):
        self._alerts = list(alerts)

    @property
    def alert(self):
        if self._alerts:
            return self._alerts.pop(0)
        raise RuntimeError("no alert present")   # 仿 NoAlertPresentException


class _FakeDriver:
    def __init__(self, alerts):
        self.switch_to = _FakeSwitchTo(alerts)


def test_dismiss_clears_pending_idle_alert():
    a = _FakeAlert("閒置時間過長，將被導向登入畫面！")
    d = _FakeDriver([a])
    assert main._dismiss_status_driver_alert(d) is True
    assert a.accepted is True


def test_dismiss_returns_false_when_no_alert():
    d = _FakeDriver([])
    assert main._dismiss_status_driver_alert(d) is False


def test_dismiss_clears_multiple_then_stops():
    alerts = [_FakeAlert("閒置時間過長，將被導向登入畫面！"),
              _FakeAlert("閒置時間過長，將被導向登入畫面！")]
    d = _FakeDriver(alerts)
    assert main._dismiss_status_driver_alert(d) is True
    assert all(a.accepted for a in alerts)


def test_dismiss_survives_broken_driver():
    class _Boom:
        @property
        def switch_to(self):
            raise RuntimeError("driver dead")
    assert main._dismiss_status_driver_alert(_Boom()) is False   # 不拋、回 False


# ── 原始碼守門：查詢開頭必須先清 alert、且在 driver.get 之前 ───────────────────
def test_swipe_check_dismisses_alert_before_get():
    src = inspect.getsource(main._get_swipe_status_from_web)
    assert "_dismiss_status_driver_alert(driver)" in src, "查詢應先清殘留 alert"
    assert (src.index("_dismiss_status_driver_alert(driver)")
            < src.index("driver.get(LOGIN_URL)")), "清 alert 須在 driver.get 之前"
    # get 撞殘留 alert 時要能清掉重試
    assert "except UnexpectedAlertPresentException:" in src, "get 撞 alert 應清掉重試"


# ── 縮寫速寫頁面說明文字已移除 ───────────────────────────────────────────────
def test_abbrev_page_annotation_removed():
    main_src = os.path.join(os.path.dirname(__file__), "..", "src", "main.py")
    with open(main_src, encoding="utf-8") as f:
        text = f.read()
    assert "啟用後自動：中文組字" not in text, "縮寫速寫頁面說明文字應已移除"
