# -*- coding: utf-8 -*-
"""reg52 頁面的「語意有效」判定。
（2026-08-02 外部 code review P1-02）

【問題】
抓取層把 **HTTP 200** 當成 **資料有效**。維護頁、登入頁、改版後的空殼頁都是 200，
於是：

  * 亞大：缺掛號欄位只記一行 warning，之後照樣 `_cache_set` ＋
    `_source_backoff_success` ＋ `_circuit_record_success` —— **維護頁被當成健康的
    成功頁，還把熔斷器重置了**。下一輪又去打同一個壞頁。
  * 東區／惠和／惠盛：`last_error` 只在 `RequestException` 時才設定。內容過短、
    缺 `div.visitDate`／`table#dayoff` 這類**語意失敗完全不記 backoff／熔斷**，
    每一輪都再打一次。
  * 主院：`_cache_set` 發生在解析與 `data_count` 驗證【之前】。維護頁進了快取之後，
    三次 retry 都拿同一份壞內容重解析，而且在 TTL 內一直有效。

【三態】
把「連得上」與「內容可信」分開：

    SUCCESS          —— HTTP 正常 ＋ 版面契約齊 ＋（有資料或明確的合法空資料）
    TRANSPORT_ERROR  —— 連不上／逾時／HTTP 錯誤（既有的 RequestException 路徑）
    SEMANTIC_INVALID —— 連得上、但拿回來的東西不是我們要的那一頁

只有 SUCCESS 才可以寫 good cache、清 backoff、重置熔斷器。
SEMANTIC_INVALID 要累加 backoff／熔斷，並且**不可以覆蓋**上一份語意有效的快取 ——
壞頁把好資料蓋掉，比抓不到還糟。

【★這一層不看頁面內容，只回報形狀★】
`reason` 與 `length` 是給 log／contract-canary 用的，一律不含頁面文字：
維護頁可能夾帶任何東西，而 log 是會被整包交給開發者的。
"""
from __future__ import annotations

from dataclasses import dataclass

SUCCESS = "success"
TRANSPORT_ERROR = "transport_error"
SEMANTIC_INVALID = "semantic_invalid"
# ★[2026-08-08 外審 P2-01] 第四態：錯在【我們自己】,不是遠端★
#   `parser_unavailable`(bs4/lxml 載不進來、解析器炸掉)以前回 SEMANTIC_INVALID,
#   而呼叫端據此累加 backoff、記熔斷 —— **我們自己的環境壞了,卻去懲罰一台
#   完全健康的遠端主機**:指數退避把它擋掉數十分鐘、熔斷器跳閘,整個 reg52
#   功能變暗,而 log 上寫的是「東區主機連續失敗」。原本的註解自己就寫著
#   「解析器壞掉不代表頁面壞掉」—— 宣稱與實作不符。
#   LOCAL_ERROR 一樣【不可以用那份 html】(ok 仍是 False、usable_html 仍是空),
#   只是不記在遠端頭上。
LOCAL_ERROR = "local_error"

# 低於這個長度一定不是掛號表（原本各處散落的 `len(text) < 500`）
MIN_PAGE_CHARS = 500

# 分院掛號表的版面契約：這兩個選擇器至少要有一個
BRANCH_REQUIRED_SELECTORS = ("div.visitDate", "table#dayoff")

# 亞大頁面的契約（它不是同一套版型）
AUH_REQUIRED_MARKERS = ("已掛號", "visitDate")


@dataclass(frozen=True)
class FetchOutcome:
    """★不含頁面內容★ 只帶得動狀態、原因代碼與長度。"""
    status: str
    html: str = ""
    reason: str = ""
    length: int = 0

    @property
    def ok(self) -> bool:
        return self.status == SUCCESS

    @property
    def blames_remote(self) -> bool:
        """這次失敗該不該算在遠端頭上（backoff／熔斷器只看這個）。

        ★兩個方向都要守★
          * 該記卻不記 → 維護頁每一輪都再打一次（P1-02 修過的那個洞）。
          * 不該記卻記 → 我們自己的解析器壞掉會把健康的主機擋掉數十分鐘,
            而且 log 指向錯的地方,查的人往遠端查。
        """
        return self.status in (TRANSPORT_ERROR, SEMANTIC_INVALID)

    @property
    def usable_html(self) -> str:
        """只有 SUCCESS 的 html 可以拿去用／進快取。"""
        return self.html if self.status == SUCCESS else ""

    def describe(self) -> str:
        if self.status == SUCCESS:
            return f"語意有效（{self.length} 字）"
        if self.status == TRANSPORT_ERROR:
            return f"連線失敗（{self.reason}）"
        if self.status == LOCAL_ERROR:
            return f"★本機環境問題,不是遠端的錯★（{self.reason}）"
        return f"★內容不是掛號表★（{self.reason}，{self.length} 字）"


def transport_error(reason: str) -> FetchOutcome:
    """連不上／逾時／HTTP 錯誤。`reason` 只放例外類別名，不放訊息內容。"""
    return FetchOutcome(TRANSPORT_ERROR, reason=reason)


def classify_branch_html(text) -> FetchOutcome:
    """東區／惠和／惠盛的掛號表。

    ★為什麼「太短」不是用猜的★ 這三個站在維護時會回一頁極短的公告，
    而正常掛號表一定超過 500 字（原本各處就是這樣判，只是判完 `continue`
    而沒有記成失敗）。
    """
    body = str(text or "")
    if len(body) < MIN_PAGE_CHARS:
        return FetchOutcome(SEMANTIC_INVALID, reason="page_too_short",
                            length=len(body))
    try:
        from bs4 import BeautifulSoup          # ★延遲載入★ 見 reg52_fetch 說明
        probe = BeautifulSoup(body, "lxml")
    except Exception:
        # 解析器壞掉不代表頁面壞掉 —— 但我們也就無法確認它是不是掛號表。
        # ★查不到 ≠ 沒問題★：不採用這一份，寧可用上一份好的。
        # ★但也不可以記在遠端頭上★（LOCAL_ERROR，見模組頂端 P2-01 說明）。
        return FetchOutcome(LOCAL_ERROR, reason="parser_unavailable",
                            length=len(body))
    if not any(probe.select_one(sel) for sel in BRANCH_REQUIRED_SELECTORS):
        return FetchOutcome(SEMANTIC_INVALID, reason="missing_schedule_markup",
                            length=len(body))
    return FetchOutcome(SUCCESS, html=body, length=len(body))


def classify_auh_html(text) -> FetchOutcome:
    """亞大附醫的掛號表（版型與分院不同，用文字標記判）。"""
    body = str(text or "")
    if len(body) < MIN_PAGE_CHARS:
        return FetchOutcome(SEMANTIC_INVALID, reason="page_too_short",
                            length=len(body))
    if not any(m in body for m in AUH_REQUIRED_MARKERS):
        return FetchOutcome(SEMANTIC_INVALID, reason="missing_booking_field",
                            length=len(body))
    return FetchOutcome(SUCCESS, html=body, length=len(body))


def _has_reg52_skeleton(soup) -> bool:
    """這張頁面有沒有 reg52 的掛號表版面。

    判準與 `reg52_parse.parse_main_hospital_schedule` 找表格的方式【完全一致】——
    解析器認得的東西，這裡就認；解析器認不得的，這裡就不認。
    """
    if soup.select_one("table.schedule"):
        return True
    for tbl in soup.find_all("table"):
        if tbl.select_one("td.timeSlot") and tbl.select_one("td.schBox"):
            return True
    return False


def _has_east_style_dayoff_table(soup) -> bool:
    """東區 fh1 的休診表：width=300 的三欄小表，第一欄是民國日期。

    判準抄自 `reg52_parse.parse_doctor_info_dayoff` 的退路 —— 同上，
    以「解析器讀得懂嗎」為準，不自己另立一套。
    """
    import re as _re
    date_pat = _re.compile(r"(\d{2,3})/(\d{2})/(\d{2})")
    for tbl in soup.find_all("table"):
        if str(tbl.get("width") or "").strip() != "300":
            continue
        rows = tbl.find_all("tr")
        if len(rows) < 2:
            continue
        cells = rows[1].find_all(["td", "th"])
        if len(cells) != 3:
            continue
        if date_pat.search(cells[0].get_text(" ", strip=True)):
            return True
    return False


def classify_dayoff_html(text) -> FetchOutcome:
    """休診表頁。★它就是同一支 reg52.cgi，只是換一個 DocNo★
    （2026-08-02 外部 code review P1-03）

    【原本的問題】
    主院掛號表已經做到「分類成功才寫快取」，但休診表這一支還是 HTTP 200 就
    `_cache_set` ＋ `_source_backoff_success`。維護頁／登入頁／改版空殼頁全是
    200 —— 壞頁會覆蓋掉上一份好的休診資料，退避還被清掉。
    後果特別嚴重：★休診覆蓋消失 = 停診的診次被顯示成正常門診★。

    【判準：分兩層，而且「沒有休診」是合法的】
      1. 有 `table#dayoff` → 這就是休診表
      2. 有東區式的 width=300 三欄表 → 同上（解析器的退路）
      3. 兩者都沒有，但有 reg52 掛號表版面 → ★合法的「這位醫師沒有休診」★
      4. 以上皆非 → 語意無效（維護頁／登入頁／未知版面）

    ★這裡刻意【不】要求一定要有休診表★ 多數醫師多數時候就是沒有休診；
    把「沒有休診」判成無效，會讓休診來源一路退避到停更 —— 那與壞頁覆蓋
    造成的傷害一模一樣（停診顯示成正常門診），只是換個方向。
    """
    body = str(text or "")
    if len(body) < MIN_PAGE_CHARS:
        return FetchOutcome(SEMANTIC_INVALID, reason="page_too_short",
                            length=len(body))
    try:
        from bs4 import BeautifulSoup          # ★延遲載入★ 見 reg52_fetch 說明
        soup = BeautifulSoup(body, "lxml")
    except Exception:
        return FetchOutcome(LOCAL_ERROR, reason="parser_unavailable",
                            length=len(body))
    if (soup.select_one("table#dayoff")
            or _has_east_style_dayoff_table(soup)
            or _has_reg52_skeleton(soup)):
        return FetchOutcome(SUCCESS, html=body, length=len(body))
    return FetchOutcome(SEMANTIC_INVALID, reason="missing_reg52_layout",
                        length=len(body))


def classify_main_html(text) -> FetchOutcome:
    """主院掛號表 —— ★判的是【版面在不在】，不是【有沒有診】★

    ★[2026-08-02 外審第 2 輪 P2] 我第一版用「解析出幾個時段」當判準，那是錯的★
    有些醫師的門診【本來就只在分院／亞大】（見 `reg52_branch_policy` 的靜態
    外院來源清單與 `AUH_DOCTOR_DOCNO_MAP`）—— 他們的主院頁是一張**結構完整、
    但沒有時段**的正常頁。把那個判成語意無效的話：
      * 主院頁永遠不進快取；
      * 每一輪都累加 backoff，最長退避到 5 分鐘；
      * 而下一輪會在「主院 backoff 檢查」就被擋掉，**連分院都抓不到** ——
        於是那些醫師的分院掛號數整批停在舊資料。
    「這一批完全沒有資料」的判斷仍然在原處（`check_appointment_count` 合併
    所有來源之後的 `data_count == 0`），這一支不碰它。

    真正能分辨維護頁的是**版面本身**：主院解析器認的是 `table.schedule`，
    或退而求其次一張同時有 `td.timeSlot` 與 `td.schBox` 的表格
    （見 `reg52_parse.parse_main_hospital_schedule`）。那張表在 → 這是掛號表，
    有沒有診是另一回事；那張表不在 → 我們拿到的根本不是掛號頁。
    """
    body = str(text or "")
    if len(body) < MIN_PAGE_CHARS:
        return FetchOutcome(SEMANTIC_INVALID, reason="page_too_short",
                            length=len(body))
    try:
        from bs4 import BeautifulSoup          # ★延遲載入★ 見 reg52_fetch 說明
        soup = BeautifulSoup(body, "lxml")
    except Exception:
        return FetchOutcome(LOCAL_ERROR, reason="parser_unavailable",
                            length=len(body))
    if _has_reg52_skeleton(soup):
        return FetchOutcome(SUCCESS, html=body, length=len(body))
    return FetchOutcome(SEMANTIC_INVALID, reason="missing_schedule_table",
                        length=len(body))
