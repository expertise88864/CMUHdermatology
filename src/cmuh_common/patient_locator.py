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
from dataclasses import dataclass
import os
import re
import tempfile
import threading
from datetime import datetime, timedelta

INDEX_FILENAME = "patient_locator_index.jsonl"
# ★[2026-07-30 外審] append 與 prune 必須互斥★
#   兩者都是 read-modify-replace,而且原本都用 `{path}.tmp-{os.getpid()}` ——
#   同一個行程 ⇒ 同一個檔名。背景清掃讀完舊內容之後,若剛好有一筆回讀不符寫進來,
#   清掃再把它的舊快照 replace 回去,那筆【新病人就此消失】——正是這個功能存在的
#   唯一目的。tmp 檔名也會互相踩。
#   清掃很短(只有 30 天內的 mismatch),鎖的代價可以忽略。
_INDEX_LOCK = threading.Lock()
INDEX_RETAIN_DAYS = 30
# 橫幅通常在視窗上方,不必掃到底;掃太多控件會拖慢熱鍵緒。
MAX_CONTROLS_TO_SCAN = 400
# ★[2026-08-02 補審 P1] 這段跑在【熱鍵緒】上,而且只在 mismatch 時觸發 ——
#   mismatch 的典型成因正是「HIS 沒有回應」。若每個控件都用預設 2.5 秒逾時,
#   400 × 2.5s ≈ 1,000 秒,期間所有熱鍵全被鎖住(F12 也不會被檢查)。
#   故:單控件逾時壓到很短,並加一道整體 deadline。抓不到就誠實回報抓不到。
SCAN_PER_CONTROL_TIMEOUT_MS = 120
SCAN_TOTAL_BUDGET_SEC = 2.0
MAX_TEXT_LEN = 400          # 超過這個長度的控件不可能是橫幅(多半是病歷 memo)

# 民國 7 碼日期(1150728)。★[2026-08-02 補審 P1] 必須錨在【開頭】並驗證是合法日期★
#   原本用「橫幅中任何一個獨立七位數」——而實機橫幅本來就含七位的【生日】
#   (0730623)。只要院方把開頭日期改成西元八碼/斜線格式、或那一格暫時是空的,
#   生日就會滿足 banner 識別條件並被輸出成 date_roc,進而進入告警信與索引檔,
#   直接違反本模組「生日一律不取」的核心承諾。
_ROC_DATE_RE = re.compile(r"^\s*(\d{7})(?!\d)")


def _valid_roc_date(text: str) -> bool:
    """民國日期合理性:年 100-199、月 01-12、日 01-31。

    不做真正的日曆驗證(閏年/大小月)——這只是用來把「七位數的生日」與
    「七位數的民國日期」分開,過度嚴格反而會在院方改格式時整組失效。
    """
    if not text or len(text) != 7 or not text.isdigit():
        return False
    year, month, day = int(text[:3]), int(text[3:5]), int(text[5:7])
    return 100 <= year <= 199 and 1 <= month <= 12 and 1 <= day <= 31


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
    m = _ROC_DATE_RE.search(text)
    return bool(m and _valid_roc_date(m.group(1))
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
    if m and _valid_roc_date(m.group(1)):
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


def format_for_log(loc) -> str:
    """給【一般 log】用的定位字串 —— ★不含病歷號★。

    [2026-08-02 補審 P2] `automation_ui.log` 是容量輪替、沒有保存期限,而且常被
    整包交給開發者除錯。病歷號只留在有明確 30 天期限的定位索引與告警信裡;
    一般 log 有「診間/診號」就足以對應到索引裡那一筆。
    """
    if not loc:
        return "(無法取得)"
    parts = [f"診間 {loc[k]}" if k == "room" else f"診號 {loc[k]}"
             for k in ("room", "seq") if loc.get(k)]
    return "/".join(parts) if parts else "(無診間診號)"


# ─── 獨立定位索引檔 ────────────────────────────────────────────────────────
# [使用者定案 2026-07-28] 除了寄信,另寫一份獨立索引。理由:告警信「同一功能同一天
# 只寄一次」,同一天第二個出問題的病人根本不會有信;SMTP 掛掉時也整個遺失。
# 索引與 hash-chain 稽核帳本【分開】—— 帳本「不存病人明文識別」的既有定案不動。
def _atomic_write_rows(path: str, rows: list) -> None:
    """把整份索引原子寫回。tmp 檔名用 mkstemp —— 不可用 pid 當唯一性來源
    (append 與 prune 在【同一個行程】,pid 相同就會互相踩)。"""
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".tmp-",
                              dir=os.path.dirname(path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_rows(path: str) -> list:
    rows: list = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue          # 壞列略過,不讓一列壞掉毀掉整份索引
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _prune(rows: list, today: datetime, retain_days: int) -> list:
    cutoff = (today - timedelta(days=retain_days)).isoformat()
    out = []
    for r in rows:
        ts = str(r.get("ts") or "")
        if ts >= cutoff:       # ISO 字串可直接比大小;無 ts 的壞列一律丟掉
            out.append(r)
    return out


PRUNE_OK = "ok"                    # 真的刪掉了幾列
PRUNE_NOTHING_TO_DO = "nothing"    # 檔案不存在／沒有過期列
PRUNE_FAILED = "failed"            # 讀不到、寫不回去 —— ★保留期沒有執行★


@dataclass(frozen=True)
class PruneResult:
    """★[2026-08-02 外部 code review P2-04] 不要用一個 int★

    原本回傳「刪掉幾列」，而失敗時回 `0` —— 跟「沒有過期的列」長得一模一樣。
    這是一道**保留期控制**：它默默失敗的意思是病人的病歷號留在磁碟上超過宣告的
    30 天，而唯一的呼叫端（RetentionSweeper）看到 0 只會當成「今天沒事」。
    宣告了保留期卻不知道它有沒有執行，等於沒有保留期。

    ★`reason` 不含任何病人資料★ 這個字串會進 log 與清掃摘要。
    """
    status: str
    removed: int = 0
    kept: int = 0
    reason: str = ""

    @property
    def ok(self) -> bool:
        """有沒有【確定完成】這一輪修剪（沒事可做也算完成）。"""
        return self.status in (PRUNE_OK, PRUNE_NOTHING_TO_DO)

    def describe(self) -> str:
        if self.status == PRUNE_OK:
            return f"刪掉 {self.removed} 列(留 {self.kept} 列)"
        if self.status == PRUNE_NOTHING_TO_DO:
            return "沒有過期的列"
        return f"★修剪失敗★{self.reason}"


def prune_index(path: str, *, now: datetime | None = None,
                retain_days: int = INDEX_RETAIN_DAYS) -> PruneResult:
    """★[2026-07-30 外審 P1-03] 不依賴「下一次 mismatch」的獨立修剪★

    原本修剪只寫在 `append_index()` 裡:宣告 `INDEX_RETAIN_DAYS = 30`,但只要這
    30 天內沒有再發生回讀不符,某個病人的病歷號就會一直留著(實務上可以留一整年)。
    宣告了保留期卻不主動執行,等於沒有保留期。由 RetentionSweeper 定期呼叫本函式。

    ★持 _INDEX_LOCK★:與 append_index 互斥 —— 背景清掃讀完舊內容之後,若剛好有
    一筆回讀不符寫進來,清掃再把舊快照 replace 回去,那筆新病人就此消失(外審抓到)。

    → `PruneResult`。絕不拋（呼叫端靠 `.ok` 分辨失敗，不是靠回傳 0）。
    """
    try:
        with _INDEX_LOCK:
            rows = _read_rows(path)
            if not rows:
                return PruneResult(PRUNE_NOTHING_TO_DO)
            kept = _prune(rows, now or datetime.now(), retain_days)
            removed = len(rows) - len(kept)
            if removed <= 0:
                return PruneResult(PRUNE_NOTHING_TO_DO, kept=len(kept))
            _atomic_write_rows(path, kept)
            return PruneResult(PRUNE_OK, removed=removed, kept=len(kept))
    except Exception as e:
        # ★不要降級成 debug★ 這是保留期控制失敗，不是無關痛癢的小事。
        logging.error("[locator] 修剪定位索引失敗 —— ★保留期本輪沒有執行★",
                      exc_info=True)
        # ★只放例外類別名，不放訊息★ 訊息可能含檔案路徑；路徑含使用者名稱。
        return PruneResult(PRUNE_FAILED, reason=type(e).__name__)


def append_index(path: str, *, ts: str, action: str, detail: str, locator,
                 now: datetime | None = None,
                 retain_days: int = INDEX_RETAIN_DAYS) -> bool:
    """把一筆回讀不符的定位資訊寫進索引檔(順便修剪過期列)。絕不拋。

    回傳是否成功寫入。**在告警信的去重之前呼叫** —— 去重擋掉的是信,不是紀錄。
    ★持 _INDEX_LOCK★:與 prune_index 互斥(見那裡的說明)。
    """
    try:
        row = {"ts": ts, "action": str(action), "detail": str(detail)}
        for k in ALLOWED_FIELDS:
            v = (locator or {}).get(k)
            if v:
                row[k] = str(v)
        with _INDEX_LOCK:
            rows = _prune(_read_rows(path), now or datetime.now(), retain_days)
            rows.append(row)
            _atomic_write_rows(path, rows)
        return True
    except Exception:
        logging.debug("[locator] 寫入定位索引失敗(不影響臨床流程)", exc_info=True)
        return False
