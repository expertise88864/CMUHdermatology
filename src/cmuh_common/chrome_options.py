# -*- coding: utf-8 -*-
"""共用 Selenium Chrome Options builder — 給 main.py / autoclock 用。

[2026-05-25 v15] 從 clock/webdriver_setup.py 抽出來放共用層，加上更多 RAM
優化 flag。原本主程式 status_driver 跟 autoclock clock_driver 各自一份
options list，flag 不一致（autoclock 已較完整），且都還有 ~10 個常見
省 RAM flag 沒設。

flag 分四類：
  1. 必要 (headless, no-sandbox, gpu)：能跑就靠這幾個
  2. 流量/啟動省 (dns-prefetch, disable-extensions, disable-images)
  3. 背景活動省 (disable-background-networking, disable-sync, mute-audio)
  4. 記憶體省 (renderer-process-limit, js-flags max-old-space, disable-features)

預期效果：headless Chrome RSS 從 ~250MB 降到 ~150MB (依站台複雜度浮動)。
"""
from __future__ import annotations


# [v15] 用 disable-features 一次關掉一票背景功能：
#   Translate          — 翻譯 thread
#   MediaRouter        — Cast 探測
#   OptimizationHints  — Google optimization guide 背景通訊
#   DialMediaRouteProvider — DIAL/UPnP 探測
#   AcceptCHFrame      — Client Hints frame
#   InterestCohort     — FLoC
#   AutofillServerCommunication — autofill 上傳統計
_DISABLED_FEATURES = ",".join((
    "Translate",
    "MediaRouter",
    "OptimizationHints",
    "DialMediaRouteProvider",
    "AcceptCHFrame",
    "InterestCohort",
    "AutofillServerCommunication",
))


def build_chrome_options(headless: bool = True):
    """回傳 selenium Options — 集中所有效能/隱私/RAM 旗標。

    headless=True (預設) 走 --headless=new；False 走有頭瀏覽器 (打卡 GUI 模式)。
    """
    from selenium.webdriver.chrome.options import Options  # type: ignore[import-not-found]

    opts = Options()
    if headless:
        opts.add_argument("--headless=new")

    args = [
        # ─── 必要 ────────────────────────────────────────
        "--disable-gpu",
        "--window-size=1280,800",
        "--no-sandbox",
        "--disable-dev-shm-usage",
        # ─── 流量/啟動省 ───────────────────────────────────
        "--disable-extensions",
        "--dns-prefetch-disable",
        "--log-level=3",
        "--disable-images",
        "--blink-settings=imagesEnabled=false",
        "--disable-notifications",
        "--disable-popup-blocking",
        "--disable-infobars",
        # ─── 背景活動省 ─────────────────────────────────────
        "--disable-background-networking",
        "--disable-sync",
        "--mute-audio",
        "--disable-translate",
        "--disable-default-apps",
        # [v15] 停 crash reporter / 釣魚偵測 / domain reliability telemetry
        "--disable-breakpad",
        "--disable-client-side-phishing-detection",
        "--disable-domain-reliability",
        # [v15] 停 component extension 的背景 page
        "--disable-component-extensions-with-background-pages",
        # ─── 記憶體省 ───────────────────────────────────────
        # [v15] 限 renderer process 數量 (對只跑單頁的 status check 來說 1 就夠)
        "--renderer-process-limit=1",
        # [v15] V8 old space heap 上限 128MB (預設 1.4GB)。對輕量
        # 頁面 (打卡 HR 系統 / 掛號狀態頁) 完全夠用，避免長期累積佔 RAM。
        "--js-flags=--max-old-space-size=128",
        # [v15] 用 disable-features 一次關一票背景功能 (見 _DISABLED_FEATURES)
        f"--disable-features={_DISABLED_FEATURES}",
    ]
    for a in args:
        opts.add_argument(a)

    opts.add_experimental_option("excludeSwitches", ["enable-logging"])
    opts.page_load_strategy = "eager"
    return opts
