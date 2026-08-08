# -*- coding: utf-8 -*-
"""院外 reg52 抓取（亞大／東區／惠和／惠盛）。
（P2-06 分層第四刀(b) 2026-08-01，從 main.py 搬入）

【這一層在做什麼】
拿 thread-local 的 HTTP session 去抓四個院外掛號頁的 HTML，過程中接上
`fetch_resilience` 的熔斷器與來源退避 —— 也就是「別把別人的主機打爆、
也別讓一個掛掉的來源拖垮整批」。**解析**在 `reg52_parse`，這裡只負責拿到字串。

【測試接縫】
`_external_session()` 取得器：測試換掉它就能餵假的 session 進來，
把「HTTP 500 會不會讓熔斷器跳閘」「逾時會不會記退避」這類**韌性接線**測出來 ——
那才是這一層真正的邏輯（HTTP 本身 requests 已經測過了）。

【★第三方套件一律延遲載入★】
`requests` / `bs4` 不可以寫成 module 層的 import。`main.py` 在【第 190 行】就
import 本模組，而修復環境的 `_ensure_deps_runtime(REQUIRED_LIBS)` 要到【第 285 行】
才跑 —— 套件缺了或壞了的話，這裡會先 `ModuleNotFoundError`，程式根本進不去，
**那個專門用來自動修好環境的安裝器也就永遠沒機會執行**，變成要有人到現場。
（這是 2026-08-01 外審抓到的：搬進本模組時把 main.py 原本刻意的
 `TYPE_CHECKING` + 執行期 None 佔位 + splash 後才載入的設計弄丟了，
 順帶也把 400-500ms 的 pre-splash 載入成本加了回去。）
所以下面每支函式都在【函式內】才 import。
"""
from __future__ import annotations

import logging
import threading

from cmuh_common.fetch_resilience import (
    _CIRCUIT_BREAKER_RESET_SEC,
    _cache_get,
    _cache_set,
    _circuit_is_tripped,
    _circuit_record_fail,
    _circuit_record_success,
    _source_backoff_allow,
    _source_backoff_fail,
    _source_backoff_success,
)
from cmuh_common.reg52_contract import (
    classify_auh_html as _classify_auh_html,
    classify_branch_html as _classify_branch_html,
)
from cmuh_common.http_client import is_internal as _is_internal
from cmuh_common.appt_utils import reg52_docno_for_dayoff_table as _reg52_docno_for_dayoff_table
from cmuh_common.http_session_registry import register_session as _register_reg_session

_CIRCUIT_BREAKER_THRESHOLD = 3   # 與 fetch_resilience 對齊（只用於 log 措辭）
# ★[2026-08-01 外審 P3] log 要說得跟實際行為一樣★
#   原本三支都寫「本 session 不再嘗試（重啟程式才會重試）」，但共用熔斷器有
#   `_CIRCUIT_BREAKER_RESET_SEC` 冷卻，逾時會自動半開放行一次重試 —— 那句話會讓
#   查問題的人以為「不重開就永遠不會再連」，而事實上它自己會回來。
_CIRCUIT_BREAKER_RESET_MIN = int(_CIRCUIT_BREAKER_RESET_SEC // 60)


_reg52_external_tls = threading.local()

# ★★★ 明文 HTTP 來源：傳輸過程不可信 ★★★
# （2026-08-02 外部 code review P2-03）
#
# 東區與惠盛走 `http://61.66.117.10`：**沒有 TLS，也沒有主機名可驗證**。
# 在院內網路上任何位於路徑上的裝置（交換器、Wi-Fi AP、被入侵的同網段機器、
# 被投毒的 ARP/DNS）都可以【無聲地改寫】回應內容，而我們完全看不出來。
# 惠和與亞大走 https，不在此列。
#
# ★威脅模型（誠實地界定，不誇大）★
#   * 這些頁面【不會】被執行、不會進 webview、不會餵給 shell/SQL/檔案路徑。
#     它們只被 BeautifulSoup 解析成「診次、掛號數、診間號」再顯示/寄信。
#   * 所以可達成的傷害是**顯示與通知內容被操縱**：例如偽造「已額滿」讓人以為
#     滿診、或塞一個荒謬的掛號數觸發假的止掛提醒。不是遠端執行。
#   * 對應處置在 `reg52_parse`：所有從這些頁面來的數值都要**先過範圍檢查**
#     （見 `_MAX_PLAUSIBLE_APPT_COUNT` / `_MAX_ROOM_LEN`），且頁面文字
#     **一律不進 log／稽核帳本**（維護頁可能夾帶任何東西）。
#
# ★為什麼不直接改成 https★ 沒有辦法在這裡驗證院方那台主機有沒有開 443、
# 憑證是否對得上（它用的是 IP，憑證幾乎不可能匹配）。沒實測過就改，等於把
# 「資料可能被竄改」換成「整個來源直接不通」。要改需要在院內實測後再動。
PLAINTEXT_REG52_SOURCES = ("east", "huisheng")

# 給通知/顯示層用的一句話。★內容要說得跟實際知道的一樣★：我們不是「懷疑這筆
# 被改過」，而是「這條線路上我們無從分辨有沒有被改過」。
UNVERIFIED_TRANSPORT_NOTE = (
    "※ 此筆來自未加密連線（明文 HTTP）的分院掛號頁，內容在院內網路上"
    "無法驗證是否被改動過；止掛前請以 HIS 或分院端再確認一次。")


def is_plaintext_source(ext_branch) -> bool:
    """這個分院代碼的資料是不是走明文 HTTP 來的。

    ★[2026-08-02 外審第 2 輪 P2] 這一支存在的理由★
    上一版只宣告了 `PLAINTEXT_REG52_SOURCES` 這個常數，**沒有任何生產程式碼
    讀它** —— 等於寫了一句安全性宣稱卻沒有對應行為。範圍檢查擋得住畸形值與
    資源耗盡，擋不住「刻意改成一個看起來合理的數字」：把東區某診改成 130 人
    就能讓系統寄出一封與真的完全一樣的止掛提醒。

    ★選擇「標註」而不是「抑制」★ 抑制等於讓分院的止掛提醒安靜消失，那是對
    臨床通知的 fail-closed 變更；使用者對金絲雀已經定案過同一件事的方向
    （不擋、寄信通知）。所以照樣寄，但信裡說清楚這個數字的來源無法驗證。
    """
    return str(ext_branch or "") in PLAINTEXT_REG52_SOURCES

# 東區分院掛號（與主院 appointment.cmuh.org.tw 不同主機）
# ★明文 HTTP★ 見上面 PLAINTEXT_REG52_SOURCES
EAST_DISTRICT_REG52_URL = "http://61.66.117.10/cgi-bin/fh1/reg52.cgi"

# 惠和醫院掛號（與主院同網域，路徑為 wh1/reg52.cgi）
HUIHE_REG52_URL = "https://appointment.cmuh.org.tw/cgi-bin/wh1/reg52.cgi"

# 惠盛醫院掛號（與東區同主機 61.66.117.10，路徑為 hs1/reg52.cgi）
# ★明文 HTTP★ 見上面 PLAINTEXT_REG52_SOURCES
HUISHENG_REG52_URL = "http://61.66.117.10/cgi-bin/hs1/reg52.cgi"

AUH_REG52_BASE_URL = "https://appointment.auh.org.tw/cgi-bin/as/reg52.cgi"

AUH_DOCTOR_DOCNO_MAP = {
    "方心禹": "D52646",
    "謝佳陵": "101823",
    "沈冠宇": "D28592",
}

REG52_AUH_TTL_SECONDS = 600

# [O2] 院外連線 timeout 從 (4,8) 縮為 (2,5)：AUH/惠盛/東區若不通，2 秒就失敗，避免拖慢首批
REG52_BRANCH_TIMEOUT = (2, 5)

REG52_AUH_TIMEOUT = (2, 5)

REG52_STALE_CACHE_SECONDS = 15 * 60

# [O2] 院外失敗 backoff 從 60s 拉長到 300s（5 分鐘）；上限 15 分鐘 → 30 分鐘
# 院外（AUH/東區/惠盛）若不通通常 5 分鐘內也不會恢復，過短重試只是浪費時間
REG52_EXTERNAL_BACKOFF_BASE_SECONDS = 300

REG52_EXTERNAL_BACKOFF_MAX_SECONDS = 30 * 60


def _get_thread_local_reg52_external_session():
    # ★延遲載入★ 見模組 docstring：module 層 import 會讓相依壞掉時
    #   連自動安裝器都跑不到（main.py 第 190 行 import 本模組，
    #   而 _ensure_deps_runtime 在第 285 行）。
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    s = getattr(_reg52_external_tls, "session", None)
    if s is None:
        s = requests.Session()
        rtry = Retry(total=0, connect=0, read=0, redirect=0, status=0)
        s.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=rtry))
        s.mount("http://", HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=rtry))
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Connection": "keep-alive",
        })
        _register_reg_session(s)  # [v18] atexit cleanup
        _reg52_external_tls.session = s
    return s


def _fetch_east_district_reg52_html(session, doc_no: str, doctor_name: str):
    """東區 fh1/reg52.cgi；Docname 先試 Big5 再試 UTF-8（不同醫師連結慣例不同）。"""
    # ★延遲載入★ 見模組 docstring：module 層 import 會讓相依壞掉時
    #   連自動安裝器都跑不到（main.py 第 190 行 import 本模組，
    #   而 _ensure_deps_runtime 在第 285 行）。
    import requests
    from urllib.parse import quote, quote_from_bytes

    dparam = _reg52_docno_for_dayoff_table(doc_no)
    variants = []
    try:
        variants.append(quote_from_bytes(doctor_name.encode("big5")))
    except UnicodeEncodeError:
        pass
    variants.append(quote(doctor_name, safe=""))
    seen_urls = set()
    source_key = f"east:{doc_no}"
    # [O36] Circuit breaker：本 session 連續失敗已達閾值 → 完全跳過
    if _circuit_is_tripped("east"):
        return None
    ok, remain = _source_backoff_allow(source_key)
    if not ok:
        logging.info(f"[BACKOFF] skip east fetch {doctor_name} {doc_no}, remaining={remain:.1f}s")
        return None
    # ★[2026-08-01 外審 P2] 只有沒帶 session 時才取 thread-local★
    #   原本是無條件覆蓋參數 —— 呼叫端傳進來的 proxy／認證／憑證政策全部失效，
    #   而測試又以為自己注入得進來（實際上測到的是 thread-local 那條）。
    #   「一定會被丟掉的參數」比沒有參數更糟：它讓 API 契約騙人。
    session = session or _get_thread_local_reg52_external_session()
    last_error = None
    last_semantic = None
    local_fault = None
    for docname_q in variants:
        url = f"{EAST_DISTRICT_REG52_URL}?DocNo={dparam}&Docname={docname_q}"
        if url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            r = session.get(url, timeout=REG52_BRANCH_TIMEOUT, verify=True)
            r.raise_for_status()
            r.encoding = "big5"
            # ★[2026-08-02 外審 P1] HTTP 200 ≠ 這是掛號表★
            #   維護頁／登入頁／改版後的空殼頁都是 200。原本這裡只是 `continue`，
            #   於是語意失敗完全不記 backoff／熔斷 —— 每一輪都再打一次同一個壞頁。
            outcome = _classify_branch_html(r.text)
            if outcome.ok:
                logging.info(f"已自東區主機取得掛號表: {doctor_name} ({dparam})")
                _source_backoff_success(source_key)
                _circuit_record_success("east")
                return outcome.usable_html
            # ★[2026-08-08 外審 P2-01] 只有該算在遠端頭上的才進 last_semantic★
            #   LOCAL_ERROR(我們自己的 bs4/lxml 壞掉)不記 backoff、不記熔斷:
            #   否則本機環境一壞,健康的遠端主機會被擋掉數十分鐘,而 log 上
            #   寫的是「主機連續失敗」—— 查的人會往遠端查。
            if outcome.blames_remote:
                last_semantic = outcome
            else:
                local_fault = outcome
            logging.info("東區主機回應不是掛號表: %s %s → %s",
                         doctor_name, dparam, outcome.describe())
        except requests.exceptions.RequestException as e:
            logging.debug(f"東區 reg52 請求失敗 ({url[:64]}…): {e}")
            last_error = e
            continue
    # ★語意失敗也是失敗★ 只認 RequestException 的話，維護頁會讓
    #   backoff 與熔斷器永遠不動，每一輪都再打一次同一個壞頁。
    if last_error or last_semantic:
        delay, cnt = _source_backoff_fail(
            source_key,
            REG52_EXTERNAL_BACKOFF_BASE_SECONDS,
            REG52_EXTERNAL_BACKOFF_MAX_SECONDS,
        )
        logging.warning(f"[BACKOFF] east fetch fail {doctor_name} {doc_no}, fail={cnt}, delay={delay:.1f}s")
        # [O36] 紀錄 session 級失敗
        if _circuit_record_fail("east"):
            logging.warning("[O36] 東區主機連續失敗 %d 次 → 暫停嘗試 %d 分鐘，之後自動半開重試",
                            _CIRCUIT_BREAKER_THRESHOLD, _CIRCUIT_BREAKER_RESET_MIN)
    if local_fault is not None and last_semantic is None and last_error is None:
        logging.error("[本機] 東區掛號表無法判定:%s —— ★這是本機環境的問題★,"
                      "不記遠端 backoff/熔斷", local_fault.describe())
    logging.warning(f"無法自東區主機取得掛號表: {doctor_name} ({dparam})")
    return None


def _fetch_huihe_reg52_html(session, doc_no: str, doctor_name: str):
    """惠和 wh1/reg52.cgi（與主院同網域）；Docname 先試 Big5 再試 UTF-8。"""
    # ★延遲載入★ 見模組 docstring：module 層 import 會讓相依壞掉時
    #   連自動安裝器都跑不到（main.py 第 190 行 import 本模組，
    #   而 _ensure_deps_runtime 在第 285 行）。
    import requests
    from urllib.parse import quote, quote_from_bytes

    dparam = _reg52_docno_for_dayoff_table(doc_no)
    variants = []
    try:
        variants.append(quote_from_bytes(doctor_name.encode("big5")))
    except UnicodeEncodeError:
        pass
    variants.append(quote(doctor_name, safe=""))
    seen_urls = set()
    source_key = f"huihe:{doc_no}"
    ok, remain = _source_backoff_allow(source_key)
    if not ok:
        logging.info(f"[BACKOFF] skip huihe fetch {doctor_name} {doc_no}, remaining={remain:.1f}s")
        return None
    # ★[2026-08-01 外審 P2] 只有沒帶 session 時才取 thread-local★
    #   原本是無條件覆蓋參數 —— 呼叫端傳進來的 proxy／認證／憑證政策全部失效，
    #   而測試又以為自己注入得進來（實際上測到的是 thread-local 那條）。
    #   「一定會被丟掉的參數」比沒有參數更糟：它讓 API 契約騙人。
    session = session or _get_thread_local_reg52_external_session()
    last_error = None
    last_semantic = None
    local_fault = None
    for docname_q in variants:
        url = f"{HUIHE_REG52_URL}?DocNo={dparam}&Docname={docname_q}"
        if url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            r = session.get(url, timeout=REG52_BRANCH_TIMEOUT, verify=not _is_internal(url))
            r.raise_for_status()
            r.encoding = "big5"
            # ★[2026-08-02 外審 P1] HTTP 200 ≠ 這是掛號表★
            #   維護頁／登入頁／改版後的空殼頁都是 200。原本這裡只是 `continue`，
            #   於是語意失敗完全不記 backoff／熔斷 —— 每一輪都再打一次同一個壞頁。
            outcome = _classify_branch_html(r.text)
            if outcome.ok:
                logging.info(f"已自惠和 wh1 取得掛號表: {doctor_name} ({dparam})")
                _source_backoff_success(source_key)
                return outcome.usable_html
            # ★[2026-08-08 外審 P2-01] 只有該算在遠端頭上的才進 last_semantic★
            #   LOCAL_ERROR(我們自己的 bs4/lxml 壞掉)不記 backoff、不記熔斷:
            #   否則本機環境一壞,健康的遠端主機會被擋掉數十分鐘,而 log 上
            #   寫的是「主機連續失敗」—— 查的人會往遠端查。
            if outcome.blames_remote:
                last_semantic = outcome
            else:
                local_fault = outcome
            logging.info("惠和回應不是掛號表: %s %s → %s",
                         doctor_name, dparam, outcome.describe())
        except requests.exceptions.RequestException as e:
            logging.debug(f"惠和 reg52 請求失敗 ({url[:64]}…): {e}")
            last_error = e
            continue
    # ★語意失敗也是失敗★ 只認 RequestException 的話，維護頁會讓
    #   backoff 與熔斷器永遠不動，每一輪都再打一次同一個壞頁。
    if last_error or last_semantic:
        delay, cnt = _source_backoff_fail(
            source_key,
            REG52_EXTERNAL_BACKOFF_BASE_SECONDS,
            REG52_EXTERNAL_BACKOFF_MAX_SECONDS,
        )
        logging.warning(f"[BACKOFF] huihe fetch fail {doctor_name} {doc_no}, fail={cnt}, delay={delay:.1f}s")
    if local_fault is not None and last_semantic is None and last_error is None:
        logging.error("[本機] 惠和掛號表無法判定:%s —— ★這是本機環境的問題★,"
                      "不記遠端 backoff/熔斷", local_fault.describe())
    logging.warning(f"無法自惠和取得掛號表: {doctor_name} ({dparam})")
    return None


def _fetch_huisheng_reg52_html(session, doc_no: str, doctor_name: str):
    """惠盛 hs1/reg52.cgi（與東區同主機）；Docname 先試 Big5 再試 UTF-8。"""
    # ★延遲載入★ 見模組 docstring：module 層 import 會讓相依壞掉時
    #   連自動安裝器都跑不到（main.py 第 190 行 import 本模組，
    #   而 _ensure_deps_runtime 在第 285 行）。
    import requests
    from urllib.parse import quote, quote_from_bytes

    dparam = _reg52_docno_for_dayoff_table(doc_no)
    variants = []
    try:
        variants.append(quote_from_bytes(doctor_name.encode("big5")))
    except UnicodeEncodeError:
        pass
    variants.append(quote(doctor_name, safe=""))
    seen_urls = set()
    source_key = f"huisheng:{doc_no}"
    if _circuit_is_tripped("huisheng"):  # [O36]
        return None
    ok, remain = _source_backoff_allow(source_key)
    if not ok:
        logging.info(f"[BACKOFF] skip huisheng fetch {doctor_name} {doc_no}, remaining={remain:.1f}s")
        return None
    # ★[2026-08-01 外審 P2] 只有沒帶 session 時才取 thread-local★
    #   原本是無條件覆蓋參數 —— 呼叫端傳進來的 proxy／認證／憑證政策全部失效，
    #   而測試又以為自己注入得進來（實際上測到的是 thread-local 那條）。
    #   「一定會被丟掉的參數」比沒有參數更糟：它讓 API 契約騙人。
    session = session or _get_thread_local_reg52_external_session()
    last_error = None
    last_semantic = None
    local_fault = None
    for docname_q in variants:
        url = f"{HUISHENG_REG52_URL}?DocNo={dparam}&Docname={docname_q}"
        if url in seen_urls:
            continue
        seen_urls.add(url)
        try:
            r = session.get(url, timeout=REG52_BRANCH_TIMEOUT, verify=True)
            r.raise_for_status()
            r.encoding = "big5"
            # ★[2026-08-02 外審 P1] HTTP 200 ≠ 這是掛號表★
            #   維護頁／登入頁／改版後的空殼頁都是 200。原本這裡只是 `continue`，
            #   於是語意失敗完全不記 backoff／熔斷 —— 每一輪都再打一次同一個壞頁。
            outcome = _classify_branch_html(r.text)
            if outcome.ok:
                logging.info(f"已自惠盛 hs1 取得掛號表: {doctor_name} ({dparam})")
                _source_backoff_success(source_key)
                _circuit_record_success("huisheng")
                return outcome.usable_html
            # ★[2026-08-08 外審 P2-01] 只有該算在遠端頭上的才進 last_semantic★
            #   LOCAL_ERROR(我們自己的 bs4/lxml 壞掉)不記 backoff、不記熔斷:
            #   否則本機環境一壞,健康的遠端主機會被擋掉數十分鐘,而 log 上
            #   寫的是「主機連續失敗」—— 查的人會往遠端查。
            if outcome.blames_remote:
                last_semantic = outcome
            else:
                local_fault = outcome
            logging.info("惠盛回應不是掛號表: %s %s → %s",
                         doctor_name, dparam, outcome.describe())
        except requests.exceptions.RequestException as e:
            logging.debug(f"惠盛 reg52 請求失敗 ({url[:64]}…): {e}")
            last_error = e
            continue
    # ★語意失敗也是失敗★ 只認 RequestException 的話，維護頁會讓
    #   backoff 與熔斷器永遠不動，每一輪都再打一次同一個壞頁。
    if last_error or last_semantic:
        delay, cnt = _source_backoff_fail(
            source_key,
            REG52_EXTERNAL_BACKOFF_BASE_SECONDS,
            REG52_EXTERNAL_BACKOFF_MAX_SECONDS,
        )
        logging.warning(f"[BACKOFF] huisheng fetch fail {doctor_name} {doc_no}, fail={cnt}, delay={delay:.1f}s")
        if _circuit_record_fail("huisheng"):  # [O36]
            logging.warning("[O36] 惠盛主機連續失敗 %d 次 → 暫停嘗試 %d 分鐘，之後自動半開重試",
                            _CIRCUIT_BREAKER_THRESHOLD, _CIRCUIT_BREAKER_RESET_MIN)
    if local_fault is not None and last_semantic is None and last_error is None:
        logging.error("[本機] 惠盛掛號表無法判定:%s —— ★這是本機環境的問題★,"
                      "不記遠端 backoff/熔斷", local_fault.describe())
    logging.warning(f"無法自惠盛取得掛號表: {doctor_name} ({dparam})")
    return None


def _fetch_auh_reg52_html(session, doctor_name):
    # ★延遲載入★ 見模組 docstring：module 層 import 會讓相依壞掉時
    #   連自動安裝器都跑不到（main.py 第 190 行 import 本模組，
    #   而 _ensure_deps_runtime 在第 285 行）。
    import requests
    from urllib.parse import quote
    doc_no = AUH_DOCTOR_DOCNO_MAP.get(doctor_name)
    if not doc_no:
        return ""
    url = f"{AUH_REG52_BASE_URL}?DocNo={doc_no}&Docname={quote(doctor_name, safe='')}"
    cache_key = ("auh_html", doctor_name, doc_no)
    hit = _cache_get(cache_key, REG52_AUH_TTL_SECONDS, evict_expired=False)
    if hit is not None:
        return hit
    source_key = f"auh:{doc_no}"
    if _circuit_is_tripped("auh"):  # [O36]
        # ★[2026-08-02 外審第 2 輪 P2] stale 交給呼叫端★
        #   `_run_external_job` 是「拿到非空就 `_cache_set`」——這裡自己回
        #   stale 的話，舊資料會被蓋上新的時間戳，又壓住整整一個 TTL 不再探測，
        #   而且會被當成剛抓回來的新鮮資料。呼叫端已經備好 `stale_html` 分支，
        #   那條路只用不寫。
        return ""
    ok, remain = _source_backoff_allow(source_key)
    if not ok:
        logging.info(f"[BACKOFF] skip auh fetch {doctor_name} {doc_no}, remaining={remain:.1f}s")
        return ""
    try:
        session = session or _get_thread_local_reg52_external_session()
        r = session.get(url, timeout=REG52_AUH_TIMEOUT, verify=True)
        r.raise_for_status()
        r.encoding = "big5"
        # ★[2026-08-02 外審 P1] 這裡原本是最嚴重的一處★
        #   缺掛號欄位只記一行 warning，然後【照樣】_cache_set ＋
        #   _source_backoff_success ＋ _circuit_record_success ——
        #   維護頁被當成健康的成功頁，還把熔斷器重置了；壞頁進了快取還會在
        #   TTL 內一直被拿來用，把上一份好資料蓋掉。
        outcome = _classify_auh_html(r.text)
        if not outcome.ok and not outcome.blames_remote:
            # ★[2026-08-08 外審 P2-01] 錯在本機,不記遠端★
            #   `parser_unavailable`(bs4/lxml 壞掉)以前走下面那條 → 指數退避
            #   加熔斷把一台健康的主機擋掉數十分鐘,而且 log 指向遠端。
            logging.error("[本機] 亞大掛號表無法判定: %s (%s) → %s"
                          " —— ★這是本機環境的問題★,不記遠端 backoff/熔斷",
                          doctor_name, doc_no, outcome.describe())
            return ""
        if not outcome.ok:
            logging.warning("亞大附醫回應不是掛號表: %s (%s) → %s",
                            doctor_name, doc_no, outcome.describe())
            delay, cnt = _source_backoff_fail(
                source_key,
                REG52_EXTERNAL_BACKOFF_BASE_SECONDS,
                REG52_EXTERNAL_BACKOFF_MAX_SECONDS,
            )
            logging.warning("[BACKOFF] auh semantic-invalid %s %s, fail=%s, "
                            "delay=%.1fs", doctor_name, doc_no, cnt, delay)
            if _circuit_record_fail("auh"):
                logging.warning("[O36] AUH 連續失敗 %d 次 → 暫停嘗試 %d 分鐘，"
                                "之後自動半開重試",
                                _CIRCUIT_BREAKER_THRESHOLD,
                                _CIRCUIT_BREAKER_RESET_MIN)
            # ★[2026-08-02 外審第 2 輪 P2] stale 交給呼叫端★
            #   `_run_external_job` 是「拿到非空就 `_cache_set`」——這裡自己回
            #   stale 的話，那份舊資料會被蓋上【新的時間戳】，於是又壓住整整
            #   一個 TTL 不再探測，而且會被當成剛抓回來的新鮮資料併進
            #   is_live_final=True 的結果裡。呼叫端已經備好 `stale_html`
            #   分支，那條路只用不寫。
            return ""
        logging.info(f"已自亞大附醫取得掛號表: {doctor_name} ({doc_no})")
        _cache_set(cache_key, outcome.usable_html)
        _source_backoff_success(source_key)
        _circuit_record_success("auh")
        return outcome.usable_html
    except requests.exceptions.RequestException as e:
        logging.warning(f"亞大附醫資料抓取失敗 ({doctor_name} {doc_no}): {e}")
        delay, cnt = _source_backoff_fail(
            source_key,
            REG52_EXTERNAL_BACKOFF_BASE_SECONDS,
            REG52_EXTERNAL_BACKOFF_MAX_SECONDS,
        )
        logging.warning(f"[BACKOFF] auh fetch fail {doctor_name} {doc_no}, fail={cnt}, delay={delay:.1f}s")
        if _circuit_record_fail("auh"):  # [O36]
            logging.warning("[O36] AUH 連續失敗 %d 次 → 暫停嘗試 %d 分鐘，之後自動半開重試",
                            _CIRCUIT_BREAKER_THRESHOLD, _CIRCUIT_BREAKER_RESET_MIN)
        # ★[2026-08-02 外審第 2 輪 P2] stale 交給呼叫端★
        #   `_run_external_job` 是「拿到非空就 `_cache_set`」——這裡自己回
        #   stale 的話，舊資料會被蓋上新的時間戳，又壓住整整一個 TTL 不再探測，
        #   而且會被當成剛抓回來的新鮮資料。呼叫端已經備好 `stale_html` 分支，
        #   那條路只用不寫。
        return ""


# ── 主院 reg52 的 thread-local session（第四刀(c) 2026-08-01 搬入）──
# 與上面 external 版本對稱：院內主機不必套 IPv4-only／較長 timeout 那一組設定。
_reg52_tls = threading.local()


def _get_thread_local_reg52_session():
    """ThreadPool 每個工作執行緒獨立 Session：掛號 reg52 可並行，且不再與 forward01 值班查詢搶同一連線鎖。"""
    # ★延遲載入★ 見模組 docstring：module 層 import 會讓相依壞掉時
    #   連自動安裝器都跑不到（main.py 第 190 行 import 本模組，
    #   而 _ensure_deps_runtime 在第 285 行）。
    import requests
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    s = getattr(_reg52_tls, "session", None)
    if s is None:
        s = requests.Session()
        # 外層 check_appointment_count 已有醫師層級 retry；這裡不要再對 read timeout
        # 做 urllib3 retry，避免一次院方卡頓放大成 30+ 秒阻塞。
        rtry = Retry(
            total=1,
            connect=1,
            read=0,
            status=1,
            backoff_factor=0.3,
            status_forcelist=[500, 502, 503, 504],
        )
        s.mount("https://", HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=rtry))
        s.mount("http://", HTTPAdapter(pool_connections=4, pool_maxsize=4, max_retries=rtry))
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
            "Connection": "keep-alive",
        })
        _register_reg_session(s)  # [v18] atexit cleanup
        _reg52_tls.session = s
    return s
