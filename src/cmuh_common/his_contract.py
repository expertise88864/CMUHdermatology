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
CALIBRATED_VERSION = "1150825"

# ── 選單 command id ────────────────────────────────────────────────────────
# 醫令 子選單(F1~F5 都走「代碼輸入」)
MENU_ID_類別字首 = 216       # 未使用,隨同段一起位移時保留紀錄
MENU_ID_代碼字首 = 218       # 未使用,同上
MENU_ID_代碼輸入 = 219       # F1~F5 共用
MENU_ID_名稱輸入 = 220       # 未使用,同上
# 完成 > 完成不印(F11 照光療程 2/3 用,避免印繳費單)
MENU_ID_FINISH_NO_PRINT = 277
# 手術及治療 > 開立電子同意書(F9/F10)
MENU_ID_同意書 = 671


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
        version="1150825", date="2026-08-26",
        evidence="使用者實測:F9/F10(送 670)開成【診斷書】;probe(test_yiling_menu_id)"
                 "按 id=671 開出同意書。版本讀自本機 TFopdmain 標題 V.1150825.01;"
                 "唯讀選單列舉確認 668-671 連號(前方插入一項→後段 +1),"
                 "醫令段 219 結構未動且使用者未回報 F1~F5 異常",
        changes="MENU_ID_同意書 670→671;其餘維持"),
    Calibration(
        version="1150805", date="2026-08-07",
        evidence="使用者實機回報「現在主程式版本 v.1150805.01」+「我已經驗證熱鍵沒問題」",
        changes="無 —— 選單 id 未位移(熱鍵全部實測正常),只把版本守門基線跟上"),
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
    ★本機快速修正檔可覆蓋 CALIBRATED_VERSION(見下)★:被覆蓋的版本當然沒有
    歷史紀錄,此時回「字面值版本」那一筆 —— override 是急救貼布,不是校正。
    """
    for c in CALIBRATION_HISTORY:
        if c.version == CALIBRATED_VERSION:
            return c
    # ★只有【override 真的改了版本】才回退字面值★ 不能看「版本對不上」就回退,
    # 否則「開發者改了 CALIBRATED_VERSION 卻沒補歷史」這條防線就被吞掉了
    # (test_missing_history_entry_fails_loudly 釘住)。
    if _VERSION_OVERRIDDEN:
        for c in CALIBRATION_HISTORY:
            if c.version == _LITERAL_CALIBRATED_VERSION:
                return c
    raise AssertionError(
        f"CALIBRATED_VERSION={CALIBRATED_VERSION} 在 CALIBRATION_HISTORY 裡沒有紀錄 —— "
        "改校正版本時必須同時補上憑據(見本模組開頭的擴充規約)")


def describe() -> str:
    """一行人話,給設定頁/log/告警信用。"""
    c = current_calibration()
    base = (f"HIS 選單 id 校正版本 {c.version}(校正日 {c.date};{c.changes})")
    if OVERRIDE_NOTE:
        base += f";★{OVERRIDE_NOTE}★"
    if OVERRIDE_ERROR:
        base += f";★{OVERRIDE_ERROR}★"
    return base


# ── 本機快速修正檔(2026-08-26,急救通道) ────────────────────────────────────
# 院方改版位移選單 id 時,正式流程(改本檔→過閘→推版→各機更新)最快也要幾十分鐘;
# 門診當下需要「馬上能用」。settings/ 錨在 app 根目錄、不隨版本切換(見 paths
# 的 pinned_app_dir),所以把修正寫進 settings 的 JSON、重啟程式就生效,與推版
# 完全脫鉤。scripts/test_yiling_menu_id.py 的「寫入本機快速修正」按鈕會產生它。
#
# ★風險與對應★ 選單 id 錯 = 熱鍵打到別的功能 = 誤寫病歷,所以:
#   1. 白名單鍵 + 型別/範圍驗證,【任何】一項不對 → 整檔拒用(寧可沒有急救,
#      不可用到一半對一半的急救)。
#   2. 檔內必須帶 for_calibration = 它要修補的「校正標記」(override_marker():
#      字面值版本 + 校正歷史筆數)。任何正式校正都會插一列歷史 → 標記改變 →
#      override 自動過期失效,不會反過來把更新的正式值蓋回舊值(急救貼布不可以
#      貼過下一次正式治療)。★不能只比版本★:主版本不變、只動尾碼的重校正
#      (歷史上沒發生過,但 codex R1 指出可能)也會插歷史列,標記照樣改變。
#   3. 生效/拒用都寫進 OVERRIDE_NOTE/OVERRIDE_ERROR,describe() 帶出、
#      main 啟動時記 WARNING —— 不允許「安靜地跑著本機特例」。
OVERRIDE_FILENAME = "his_menu_override.json"
_OVERRIDABLE_MENU_IDS = ("MENU_ID_代碼輸入", "MENU_ID_同意書",
                         "MENU_ID_FINISH_NO_PRINT")
_LITERAL_CALIBRATED_VERSION = CALIBRATED_VERSION
OVERRIDE_NOTE = ""      # 非空 = override 生效中(內容=改了什麼)
OVERRIDE_ERROR = ""     # 非空 = 有 override 檔但整檔被拒用(原因)
_VERSION_OVERRIDDEN = False   # override 有沒有改到 CALIBRATED_VERSION(供 current_calibration 回退判斷)


def override_path() -> str:
    """本機快速修正檔的完整路徑(settings 目錄,不隨版本切換)。"""
    from cmuh_common.paths import get_conf_path  # noqa: PLC0415 — 避免載入期硬相依
    return get_conf_path(OVERRIDE_FILENAME)


def override_marker() -> str:
    """override 的過期戳記:字面值版本 + 校正歷史筆數。

    任何正式校正(含只動尾碼/只 rebaseline 的)都會在 CALIBRATION_HISTORY 插一列
    → 筆數必然遞增 → 舊 override 必然過期。probe 寫檔時呼叫本函式取當下標記。
    """
    return f"{_LITERAL_CALIBRATED_VERSION}#{len(CALIBRATION_HISTORY)}"


def parse_override(data, marker: str):
    """驗證 override 內容 → (updates dict, error str)。純函式,錯誤先於更新。

    回傳 updates 只含「真的要改」的鍵值;error 非空時 updates 必為空
    (整檔拒用,不做部分套用)。
    """
    if not isinstance(data, dict):
        return {}, "override 內容不是 JSON 物件"
    allowed = set(_OVERRIDABLE_MENU_IDS) | {
        "for_calibration", "CALIBRATED_VERSION", "note"}
    unknown = sorted(set(map(str, data)) - allowed)
    if unknown:
        return {}, f"override 含未知鍵 {unknown} → 整檔拒用"
    got = data.get("for_calibration")
    if not isinstance(got, str) or got != marker:
        return {}, (f"override 是給校正 {got!r} 用的,目前程式是 {marker} → "
                    "已過期/不符,整檔拒用(正式校正已推上去的話請直接刪掉此檔)")
    note = data.get("note", "")
    if not isinstance(note, str) or len(note) > 200:
        return {}, "override 的 note 必須是 ≤200 字的字串"
    updates = {}
    for key in _OVERRIDABLE_MENU_IDS:
        if key not in data:
            continue
        v = data[key]
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 65535:
            return {}, f"override 的 {key}={v!r} 不是 1..65535 的整數 → 整檔拒用"
        updates[key] = v
    if "CALIBRATED_VERSION" in data:
        v = data["CALIBRATED_VERSION"]
        if not isinstance(v, str) or not v.isdigit() or not 6 <= len(v) <= 8:
            return {}, (f"override 的 CALIBRATED_VERSION={v!r} 不是 6-8 位數字"
                        "字串 → 整檔拒用")
        updates["CALIBRATED_VERSION"] = v
    if not updates:
        return {}, "override 沒有任何要修正的鍵 → 檔案無意義,拒用"
    return updates, ""


def _apply_local_override() -> None:
    """載入期套用本機快速修正(有就套、沒有就安靜)。**絕不丟例外**——
    六支程式共用本模組,一個壞掉的 settings 不可以讓它們一起起不來。"""
    global OVERRIDE_NOTE, OVERRIDE_ERROR, _VERSION_OVERRIDDEN
    import json  # noqa: PLC0415
    try:
        try:
            import io as _io  # noqa: PLC0415
            with _io.open(override_path(), encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:                      # noqa: BLE001 — JSON 壞/讀不到
            OVERRIDE_ERROR = f"本機快速修正檔讀取失敗({type(e).__name__}) → 未套用"
            return
        updates, err = parse_override(data, override_marker())
        if err:
            OVERRIDE_ERROR = f"本機快速修正檔:{err}"
            return
        changed = []
        for key, v in updates.items():
            old = globals()[key]
            if old != v:
                globals()[key] = v
                changed.append(f"{key} {old}→{v}")
                if key == "CALIBRATED_VERSION":
                    _VERSION_OVERRIDDEN = True
        if changed:
            OVERRIDE_NOTE = ("本機快速修正生效:" + "、".join(changed)
                             + f"(settings/{OVERRIDE_FILENAME};正式校正推版後"
                               "會自動失效)")
        else:
            OVERRIDE_NOTE = (f"本機快速修正檔存在但與正式值相同(可刪除 "
                             f"settings/{OVERRIDE_FILENAME})")
    except Exception:                               # noqa: BLE001 — 最後保險
        OVERRIDE_ERROR = "本機快速修正套用時發生未預期錯誤 → 未套用"


_apply_local_override()
