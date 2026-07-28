# -*- coding: utf-8 -*-
"""HIS 契約的**單一宣告處**:院方版本、選單 command id、校正沿革。

【為什麼要有這支】
院方每次改版,維護者要做的事是固定的:確認熱鍵還能不能用 → 改校正版本(有時再改
幾個選單 id)。但在此之前,這些常數散在 `main.py` 的 **三個相隔數千行的位置**
(1502 的版本、2244 的醫令選單、5096 的完成不印、5913 的同意書),而且 **4 支測試
各自硬編碼版本號字串**。2026-07-28 校正到 1150722 時,實際動到 5 個檔 —— 那不是
「改一個常數」該有的成本,而且很容易漏掉其中一處。

【擴充規約(下次院方改版就照這兩步)】
  1. 使用者實機確認熱鍵是否正常。
  2. 只改本檔:`CALIBRATED_VERSION` 換成新版本,若某個功能壞掉就同時改對應的
     `MENU_ID_*`,並在 `CALIBRATION_HISTORY` 加一列(**寫下憑據**:是誰、怎麼確認的)。
  `main.py` 以同名 import 沿用,測試一律讀本模組而**不得**再硬編碼版本字串
  (有守門測試釘住)。

【為什麼「憑據」是規約的一部分】
選單 id 猜錯 = 熱鍵打到別的功能 = 寫錯病歷。2026-06-29 那次是「整批 +1」,
2026-07-20 是「同意書 669→670、代碼輸入仍 219」,2026-07-28 是「完全沒動」——
三次形態都不同,沒有規律可循,只能靠實機驗證。把憑據寫進歷史,是為了讓下一個人
(或下一個 session)知道這個數字**憑什麼**是這個數字,而不是照抄。
"""
from __future__ import annotations

from dataclasses import dataclass

# ── 目前校正對應的 HIS 版本 ────────────────────────────────────────────────
# 只取主版本(6-8 位數字),不含尾碼 .01 —— 隱性基線刻意不比對尾碼,免得一開機就把
# F 鍵全判成改版(見 main.sample_his_current_fp 的說明)。
CALIBRATED_VERSION = "1150722"

# ── 選單 command id ────────────────────────────────────────────────────────
# 醫令 子選單(F1~F5 都走「代碼輸入」)
MENU_ID_類別字首 = 216       # 未使用,隨同段一起位移時保留紀錄
MENU_ID_代碼字首 = 218       # 未使用,同上
MENU_ID_代碼輸入 = 219       # F1~F5 共用
MENU_ID_名稱輸入 = 220       # 未使用,同上
# 完成 > 完成不印(F11 照光療程 2/3 用,避免印繳費單)
MENU_ID_FINISH_NO_PRINT = 277
# 手術及治療 > 開立電子同意書(F9/F10)
MENU_ID_同意書 = 670


@dataclass(frozen=True)
class Calibration:
    """一次校正的紀錄。evidence 是規約的重點,不是註解。"""
    version: str          # 該次校正對應的 HIS 主版本
    date: str             # 校正日期(YYYY-MM-DD)
    evidence: str         # 憑什麼相信這組 id 是對的
    changes: str          # 這次動了什麼(「無」= 只是版本跟上)


# 由新到舊。下次改版在最前面插一列。
CALIBRATION_HISTORY: tuple = (
    Calibration(
        version="1150722", date="2026-07-28",
        evidence="使用者實機回報「我已確定目前版本可以使用所有熱鍵功能 V.1150722.01」",
        changes="無 —— 選單 id 未位移,只把版本守門的隱性基線跟上"),
    Calibration(
        version="1150720", date="2026-07-20",
        evidence="使用者實測:同意書開不出來、代碼輸入正常",
        changes="MENU_ID_同意書 669→670;MENU_ID_代碼輸入 維持 219"),
    Calibration(
        version="1150713", date="2026-07-13",
        evidence="使用者實測熱鍵正常",
        changes="無"),
    Calibration(
        version="1150629", date="2026-06-29",
        evidence="使用者確認完成不印壞掉;probe 新「完成」選單 top[4] index 1 = 277,"
                 "與『整批 +1』一致",
        changes="醫令段 215/217/218/219 → 216/218/219/220;完成不印 276→277"),
)


def current_calibration() -> Calibration:
    """CALIBRATED_VERSION 對應的那一筆校正紀錄。

    找不到 → 表示有人改了版本卻沒補歷史,那正是本模組要防的事,所以直接丟例外
    (啟動時就會炸,不會拖到改版當下才發現沒有憑據)。
    """
    for c in CALIBRATION_HISTORY:
        if c.version == CALIBRATED_VERSION:
            return c
    raise AssertionError(
        f"CALIBRATED_VERSION={CALIBRATED_VERSION} 在 CALIBRATION_HISTORY 裡沒有紀錄 —— "
        "改校正版本時必須同時補上憑據(見本模組開頭的擴充規約)")


def describe() -> str:
    """一行人話,給設定頁/log/告警信用。"""
    c = current_calibration()
    return (f"HIS 選單 id 校正版本 {c.version}(校正日 {c.date};{c.changes})")
