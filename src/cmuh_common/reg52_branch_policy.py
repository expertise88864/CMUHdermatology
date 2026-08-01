# -*- coding: utf-8 -*-
"""「這位醫師要不要去分院主機抓」的政策。
（P2-06 分層第四刀(c) 2026-08-01，從 main.py 搬入）

★這是【業務設定】，不是抓取、也不是韌性★
所以它沒有跟著 `reg52_fetch` 走 —— 那一層只管「怎麼抓、抓失敗怎麼退避」，
而這裡回答的是「該不該去抓」。兩個問題分開，改醫師名單時不必碰抓取程式碼。

判斷有兩條路，任一成立就去抓：
  1. 主院 reg52 的回應本身提到了該分院（動態偵測，醫師換診也跟得上）；
  2. 醫師在該分院的名單裡（靜態設定，主院頁面沒寫時的兜底）。
兩條都要留：只靠動態會漏掉主院頁面沒提的情況，只靠名單則每次異動都要改程式。
"""
from __future__ import annotations


# 主院網頁未寫「東區分院」時仍應改抓東區 fh1 的醫師（與院方實際設定有關）
EAST_FH1_DOCTOR_NAMES = frozenset({"吳伯元", "蔡李澄"})

HUIHE_DOCTOR_NAMES = frozenset({"蔡李澄"})

# 目前與惠和同醫師名單；若需不同請改為獨立 frozenset
HUISHENG_DOCTOR_NAMES = HUIHE_DOCTOR_NAMES


def _main_html_has_east_branch_clinic(html_text):
    """主院 reg52 回應若提及東區分院門診，改向東區主機抓取人數／休診。"""
    return bool(html_text) and ("東區分院" in html_text)


def _should_fetch_east_district_reg52(html_main, doctor_name):
    return _main_html_has_east_branch_clinic(html_main) or doctor_name in EAST_FH1_DOCTOR_NAMES


def _should_fetch_huihe_reg52(doctor_name):
    return doctor_name in HUIHE_DOCTOR_NAMES


def _should_fetch_huisheng_reg52(doctor_name):
    return doctor_name in HUISHENG_DOCTOR_NAMES
