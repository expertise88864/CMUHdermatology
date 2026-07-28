# -*- coding: utf-8 -*-
"""從 HIS 畫面的「病人資訊橫幅」擷取**定位**欄位,供回讀不符告警查得出是哪個病人。

【動機】2026-07-27 使用者收到「回讀不符:F1 UVB 劑量 / 回讀 dose=800 count=21」告警,
但信裡沒有任何病人線索 ——「這樣我沒辦法查詢是哪個病人有錯誤」。稽核帳本
(action_ledger)在 2026-07-17 明訂【不存病人明文識別】,那個決定不動;改由本模組
把定位資訊送進**告警信**與**獨立定位索引檔**(settings/,已 gitignore)。

【資料來源】HIS 主視窗**標題列只有版本號**(`-- V.1150722.01`),病人資訊在主視窗
上方一條獨立橫幅,實機格式(2026-07-28 使用者提供):

    1150728 早上 103診 113號 -呂冠愷(24994923)女 42歲1月 (0730623) #C0024322

【★白名單擷取,絕不保留識別本人的欄位★】
只取:民國日期、時段、**診間**、**診號**、**病歷號**、就診序號。
**姓名、生日、性別、年齡一律不取、不回傳、不落檔。** 這是「拿得到」與「該不該留」
的差別 —— 橫幅原文絕不可整條寫進任何檔案或信件(`parse_banner` 只回白名單 dict,
呼叫端拿不到原文)。

本模組是純函式 + 檔案 IO,不碰 Win32(視窗列舉在 main.py,那裡才有 hwnd 工具)。
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta

INDEX_FILENAME = "patient_locator_index.jsonl"
INDEX_RETAIN_DAYS = 30
# 橫幅通常在視窗上方,不必掃到底;掃太多控件會拖慢熱鍵緒。
MAX_CONTROLS_TO_SCAN = 400
MAX_TEXT_LEN = 400          # 超過這個長度的控件不可能是橫幅(多半是病歷 memo)

# 民國 7 碼日期(1150728);前後不可再接數字,免得從病歷號中間切出來
_ROC_DATE_RE = re.compile(r"(?<!\d)(\d{7})(?!\d)")
_SESSION_RE = re.compile(r"(早上|上午|下午|晚上|夜診)")
_ROOM_RE = re.compile(r"(\d{1,4})\s*診")
_SEQ_RE = re.compile(r"(\d{1,4})\s*號")
# 病歷號 = 「NNN號」之後、姓名後面第一組括號數字。刻意用 `[^()]{0,20}?` 跨過姓名而
# 【不捕捉】它;後面的 (0730623) 是生日,因為已經先匹配到前一組所以不會被取到。
_CHART_AFTER_SEQ_RE = re.compile(
    r"\d{1,4}\s*號\s*[-－—]?\s*[^()]{0,20}?\((\d{5,12})\)")
_VISIT_RE = re.compile(r"#([A-Za-z0-9]{4,20})")

# 回傳/落檔允許出現的鍵。新增欄位必須先加進這裡 —— 這是防止哪天有人順手把
# 姓名塞進來的機械性防線(有測試釘住)。
ALLOWED_FIELDS = ("date_roc", "session", "room", "seq", "chart_no", "visit_no")


def looks_like_banner(text: str) -> bool:
    """這串文字像不像那條病人資訊橫幅。

    要求同時具備「民國日期 + N診 + N號」——單看其中一項太容易誤中其他控件
    (畫面上到處都是數字)。這是**內容過濾**式的識別,與 `_find_disposition_memo`
    同一套思路:不依賴 class 名稱,改版比較不會整組失效。
    """
    if not text or len(text) > MAX_TEXT_LEN:
        return False
    return bool(_ROC_DATE_RE.search(text)
                and _ROOM_RE.search(text)
                and _SEQ_RE.search(text))


def parse_banner(text: str):
    """橫幅文字 → 白名單定位 dict;不像橫幅則回 None。

    ★只回白名單欄位★ 呼叫端拿不到原文,所以姓名/生日不可能被誤傳下去。
    """
    if not looks_like_banner(text):
        return None
    out = {}
    m = _ROC_DATE_RE.search(text)
    if m:
        out["date_roc"] = m.group(1)
    m = _SESSION_RE.search(text)
    if m:
        out["session"] = m.group(1)
    m = _ROOM_RE.search(text)
    if m:
        out["room"] = m.group(1)
    m = _SEQ_RE.search(text)
    if m:
        out["seq"] = m.group(1)
    m = _CHART_AFTER_SEQ_RE.search(text)
    if m:
        out["chart_no"] = m.group(1)
    m = _VISIT_RE.search(text)
    if m:
        out["visit_no"] = m.group(1)
    return {k: v for k, v in out.items() if k in ALLOWED_FIELDS} or None


def format_for_alert(loc) -> str:
    """定位 dict → 告警信/日誌用的一行人話。無資料時回誠實的說明,不留空白。"""
    if not loc:
        return ("(無法取得病人定位資訊 —— 找不到 HIS 病人資訊橫幅,"
                "可能是院方改版或當下沒有開啟病歷)")
    parts = []
    if loc.get("room"):
        parts.append(f"診間 {loc['room']}")
    if loc.get("seq"):
        parts.append(f"診號 {loc['seq']}")
    if loc.get("chart_no"):
        parts.append(f"病歷號 {loc['chart_no']}")
    if loc.get("visit_no"):
        parts.append(f"就診序號 {loc['visit_no']}")
    tail = " ".join(x for x in (loc.get("date_roc"), loc.get("session")) if x)
    return " / ".join(parts) + (f"({tail})" if tail else "")


# ─── 獨立定位索引檔 ────────────────────────────────────────────────────────
# [使用者定案 2026-07-28] 除了寄信,另寫一份獨立索引。理由:告警信「同一功能同一天
# 只寄一次」,同一天第二個出問題的病人根本不會有信;SMTP 掛掉時也整個遺失。
# 索引與 hash-chain 稽核帳本【分開】—— 帳本「不存病人明文識別」的既有定案不動。
def _prune(rows: list, today: datetime, retain_days: int) -> list:
    cutoff = (today - timedelta(days=retain_days)).isoformat()
    out = []
    for r in rows:
        ts = str(r.get("ts") or "")
        if ts >= cutoff:       # ISO 字串可直接比大小;無 ts 的壞列一律丟掉
            out.append(r)
    return out


def append_index(path: str, *, ts: str, action: str, detail: str, locator,
                 now: datetime | None = None,
                 retain_days: int = INDEX_RETAIN_DAYS) -> bool:
    """把一筆回讀不符的定位資訊寫進索引檔(順便修剪過期列)。絕不拋。

    回傳是否成功寫入。**在告警信的去重之前呼叫** —— 去重擋掉的是信,不是紀錄。
    """
    try:
        row = {"ts": ts, "action": str(action), "detail": str(detail)}
        for k in ALLOWED_FIELDS:
            v = (locator or {}).get(k)
            if v:
                row[k] = str(v)
        rows = []
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue      # 壞列略過,不讓一列壞掉毀掉整份索引
                    if isinstance(obj, dict):
                        rows.append(obj)
        rows = _prune(rows, now or datetime.now(), retain_days)
        rows.append(row)
        tmp = f"{path}.tmp-{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
        return True
    except Exception:
        logging.debug("[locator] 寫入定位索引失敗(不影響臨床流程)", exc_info=True)
        return False
