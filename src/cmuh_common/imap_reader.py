# -*- coding: utf-8 -*-
"""IMAP 收信工具（共用模組）— 用於 email 遠端觸發功能。

對稱於 smtp_mail.py：
  - smtp_mail.py 用 smtp.gmail.com:587 「往外寄信」
  - imap_reader.py 用 imap.gmail.com:993 「往內收信」
  兩個用同一個 Gmail App Password（settings/smtp_credentials.json 的 password）。

為何不用 Outlook：admin 行程的 Outlook COM 拉起的 admin Outlook 沒設定任何
郵件帳號（用 administrator 的 MAPI profile），完全收不到信。改 IMAP 直接連
Gmail，任何權限都能讀。

設定來源：
  username + password 從 settings/smtp_credentials.json 讀（與 SMTP 共用）
  imap_host / imap_port 預設 imap.gmail.com:993，可由 smtp_credentials.json
  的同名欄位 override（如未來要改非 Gmail 信箱）

使用方式（在 scheduler 每 60 秒輪詢一次）：
  result = check_trigger(keyword="皮膚科會診觸發")
  if result["triggered"]:
      # 已將比對到的信標為已讀，呼叫端可立刻觸發任務
      ...
"""
from __future__ import annotations

import hashlib
import imaplib
import logging
import re
import socket
import ssl
import threading
import time
from typing import Optional

from cmuh_common.smtp_mail import load_credentials

DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993
# [2026-07-25 審查] 後備掃描（中文關鍵字必走）單輪最多檢查幾封未讀信。
# 每封需一次 FETCH round-trip，不設上限時未讀累積數百封就會撐爆單輪時限。
_MAX_SCAN_IDS = 50

# ─── Watchdog 支援：暴露當前活動的 IMAP 連線給外部 force-close ────────────
# 用途：如果 check_trigger 在 socket 上卡住 > N 秒，呼叫端可從另一個 thread
# 呼叫 force_close_active() 強制砍 socket，讓卡住的 thread 立刻 unblock。
_active_conn_lock = threading.Lock()
#: conn → 用途標籤("trigger"/"commands"/"ack"/"sent")。
#: ★[外審 2026-08-12 P2-05] 不再是單連線集合★:回查的 Sent 查詢可能與
#: 掃描重疊 —— 全域斬殺會把健康的那一條一起砍掉。
_active_conns: dict = {}


def _set_active(conn: Optional[imaplib.IMAP4_SSL], tag: str = "") -> None:
    if conn is None:
        return
    with _active_conn_lock:
        _active_conns[conn] = str(tag or "")


def _clear_active(conn: Optional[imaplib.IMAP4_SSL]) -> None:
    if conn is None:
        return
    with _active_conn_lock:
        _active_conns.pop(conn, None)


def force_close_active(clear: bool = False, tag: "Optional[str]" = None) -> bool:
    """從另一個 thread 緊急砍掉活動中的 IMAP socket，讓 hang 的 recv 立即拋例外。
    回傳 True 表示有試著關（不保證 socket 確實已斷）；False 表示沒有目標連線。

    ★[外審 2026-08-12 P2-05] `tag` 只砍那一種用途的連線★
    以前是「單連線設計」的全域斬殺;現在回查的 Sent 查詢("sent")可能與
    指令掃描("commands")/觸發掃描("trigger")在不同執行緒上重疊 ——
    指令掃描逾時卻把健康的 Sent 查詢一起砍掉,收斂就白跑一輪。
    呼叫端一律帶自己那個 worker 的 tag;`tag=None` 保留全砍
    (self-watchdog 的「整個排程器卡死」情境本來就該全砍)。

    [opt B2] clear=True:關閉後一併移出 registry。供「worker thread 被放生、
    永遠走不到 finally 的 _clear_active」的逾時路徑使用,避免已死連線物件
    被 registry 強引用無法 GC。
    """
    with _active_conn_lock:
        conns = [c for c, t in _active_conns.items()
                 if tag is None or t == tag]
    if not conns:
        return False
    for conn in conns:
        _force_close_conn(conn)
    if clear:
        with _active_conn_lock:
            for conn in conns:
                _active_conns.pop(conn, None)
    return True


def _force_close_conn(conn: Optional[imaplib.IMAP4_SSL]) -> None:
    """正常 cleanup：不送 LOGOUT/CLOSE（它們本身也可能卡 socket），直接砍底層 socket。"""
    if conn is None:
        return
    sock = getattr(conn, "sock", None)
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except Exception:
        pass
    try:
        sock.close()
    except Exception:
        pass


def _load_imap_settings() -> dict:
    """從 smtp_credentials.json 取出 IMAP 需要的欄位。"""
    c = load_credentials()
    host = str(c.get("imap_host") or DEFAULT_IMAP_HOST).strip()
    try:
        raw_port = c.get("imap_port") or DEFAULT_IMAP_PORT
        if isinstance(raw_port, bool):
            raise ValueError
        port = int(raw_port)
        if not 1 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        port = DEFAULT_IMAP_PORT
    return {
        "host": host,
        "port": port,
        "username": c.get("username", ""),
        "password": c.get("password", ""),
    }


def is_configured() -> bool:
    """IMAP 設定是否齊全可以收信。"""
    s = _load_imap_settings()
    return bool(s["host"] and s["port"] and s["username"] and s["password"])


def _decode_subject(raw_subject: bytes) -> str:
    """解 RFC2047 編碼的主旨（中文常見 =?UTF-8?B?xxx?= 或 =?big5?Q?xxx?=）。"""
    if raw_subject is None:
        return ""
    if isinstance(raw_subject, bytes):
        try:
            raw_subject = raw_subject.decode("utf-8", errors="replace")
        except Exception:
            raw_subject = str(raw_subject)
    try:
        from email.header import decode_header, make_header
        return str(make_header(decode_header(raw_subject)))
    except Exception:
        return raw_subject


def _message_age_seconds(conn, uid) -> Optional[float]:
    """[會診2 2026-06-11] 取該信 INTERNALDATE(伺服器收信時刻)距現在的秒數。
    任何失敗(fetch 失敗/格式解析不出)一律回 None → 呼叫端 fail-open 視為新信照常觸發
    (寧可多觸發、不可漏掉會診請求)。Internaldate2tuple 回本地時間 struct，配 mktime。"""
    try:
        # ★[2026-08-06 外審] 必須用 UID FETCH★
        #   check_trigger 已改用 `conn.uid("search", ...)`,傳進來的是【UID】;
        #   但這裡先前仍用 `conn.fetch()` —— 那是【序號】API。Gmail 的 UID 通常
        #   遠大於 mailbox 內的信件數 → 幾乎必然查無此序號 → 回 None → 呼叫端
        #   fail-open 把每一封陳舊觸發信都當成新信。結果:程式停機數日後恢復,
        #   幾天前的觸發信會被當成現在的請求全部重跑。(UID 遷移時漏改的一處。)
        typ, fetch = conn.uid("fetch", uid, "(INTERNALDATE)")
        if typ != "OK" or not fetch:
            return None
        raw = b""
        for part in fetch:
            if isinstance(part, bytes) and b"INTERNALDATE" in part:
                raw = part
                break
            if (isinstance(part, tuple) and part
                    and isinstance(part[0], bytes)
                    and b"INTERNALDATE" in part[0]):
                raw = part[0]
                break
        if not raw:
            return None
        tt = imaplib.Internaldate2tuple(raw)
        if tt is None:
            return None
        return max(0.0, time.time() - time.mktime(tt))
    except Exception:
        logging.debug("INTERNALDATE 解析失敗(fail-open 視為新信)", exc_info=True)
        return None


def _subject_fingerprint(subject: str) -> str:
    """未命中信件的主旨 → 可比對但不可還原的指紋。純函式。

    ★[2026-08-04 外審 P2-05，實機證實] 不可以把主旨原文寫進 log★
    這行 debug 在診間 log 裡一天出現 3850 次。那個信箱收到的【任何】信件主旨
    都會被寫進 `consult_query.log` —— 而其他醫療或個人信件的主旨可能含病人姓名、
    床號。log 檔沒有跟 Email 一樣的保存政策。

    ★診斷價值幾乎沒有損失★：這行的用途是「確認我的觸發信有沒有進收件匣」。
    真正回答那件事的是 `matched` 的數字；而使用者本來就打得開那個信箱，log 不
    需要複製一份他自己讀得到的信件內容。保留長度與雜湊是為了跨輪比對——
    「還是原來那幾封沒動」與「有新信進來了」看得出差別。
    """
    text = (subject or "").strip()
    if not text:
        return "len=0 sha=(空主旨)"
    digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:8]
    return f"len={len(text)} sha={digest}"


# 可信的 authserv-id（Authentication-Results 開頭那個「是誰做的驗證」）。
# 只有【我方收件伺服器】加的那一段才算數:同一封信可以有很多個 Authentication-Results,
# 上游轉寄站會加,攻擊者也能自己在信裡塞一段假的。信箱是 Gmail → 只信 Google 那組。
# 換信箱服務商時要改這裡（否則所有觸發信都會被判定「未驗證」）。
TRUSTED_AUTHSERV_IDS = ("mx.google.com", "google.com")


def _authserv_is_trusted(auth_results: str) -> bool:
    """這一段 Authentication-Results 是不是【我方收件伺服器】加的。

    格式:`authserv-id; method=result ...`,authserv-id 在第一個分號之前。
    """
    head = str(auth_results or "").split(";", 1)[0].strip().lower()
    # authserv-id 後面可能跟 version（RFC 8601: `mx.google.com 1;`）
    head = head.split()[0] if head.split() else ""
    head = head.rstrip(".")
    # ★只接受完全相同的 authserv-id★(外審 2026-08-08)
    #   `endswith("." + t)` 會讓 `anything.mx.google.com` 也算可信 ——
    #   而這整個字串就是攻擊者自己寫進 header 的文字,不是我們驗證過的。
    return head in TRUSTED_AUTHSERV_IDS


def _parse_trigger_headers(header_raw: bytes) -> tuple:
    """(subject, from, authentication-results) —— 用標準 parser 解，支援折行 header。

    ★[2026-08-06 外審 P2-04]★ 舊版逐行 `line.startswith(b"subject:")`。RFC 5322
    允許 header 折行(長 display name、多段 RFC 2047 encoded-word 幾乎必折)，逐行
    比對只會拿到第一行 → 主旨關鍵字漏判、From 變空 → 白名單寄件人被誤拒。
    解析失敗一律回三個空字串(呼叫端的既有行為:不命中)。
    """
    if not header_raw:
        return "", "", ""
    try:
        from email import policy
        from email.parser import BytesParser
        msg = BytesParser(policy=policy.default).parsebytes(header_raw)
        def _get(name):
            try:
                v = msg.get(name)
                return str(v) if v is not None else ""
            except Exception:
                # 極少數畸形 encoded-word 會讓 policy.default 在取值時拋 →
                # 該欄視為空(呼叫端的既有行為:主旨不命中/From 解不出)。
                logging.debug("[IMAP] header %s 取值失敗", name, exc_info=True)
                return ""
        # ★[2026-08-07 外審] Authentication-Results 只能採信【我方收件伺服器】那一段★
        #   這個 header 可以有很多個:上游轉寄站會加、而攻擊者也可以直接把一段
        #   假的塞進自己寄的信裡(那就變成信件內容的一部分)。只取 msg.get() 拿到
        #   的第一個 → 攻擊者只要把假的排在前面就通過了。
        #   規則:收件伺服器加的那一段一定在【最上方】(每經一跳就 prepend),而且
        #   authserv-id 是我們自己的信箱服務商。用 get_all() 取全部,只採信
        #   authserv-id 在信任清單內的那些。
        try:
            all_ar = [str(v) for v in (msg.get_all("Authentication-Results") or [])]
        except Exception:
            logging.debug("[IMAP] Authentication-Results 取值失敗", exc_info=True)
            all_ar = []
        # ★[2026-08-08 外審] 只採信【最上方】那一段,而且不能有第二段冒用★
        #   上一版把「authserv-id 看起來可信」的【每一段】都留下來再串接。
        #   但 authserv-id 也只是文字:攻擊者可以在自己的信裡直接寫
        #   `Authentication-Results: mx.google.com; dmarc=pass header.from=…`,
        #   它就混進 trusted、被下游的關鍵字搜尋找到 —— 真正 Gmail 那一段
        #   寫著 fail 也沒用,因為兩段是串在一起搜的。
        #   收件伺服器加的那一段【一定在最上方】(每經一跳就 prepend),
        #   所以只看 all_ar[0];而且若下方還有別段自稱同樣可信的 authserv-id,
        #   那是偽造的明確跡象 → 整封信一律不採信(fail-closed)。
        trusted = []
        if all_ar and _authserv_is_trusted(all_ar[0]):
            impostors = [ar for ar in all_ar[1:] if _authserv_is_trusted(ar)]
            if impostors:
                logging.warning(
                    "[IMAP] 信中有第二段自稱可信收件伺服器的 Authentication-"
                    "Results(偽造跡象)→ 整封不採信:%r", impostors[:1])
            else:
                trusted = [all_ar[0]]
        if all_ar and not trusted:
            logging.warning(
                "[IMAP] 這封信最上方的 Authentication-Results 不是可信收件"
                "伺服器加的(可能是轉寄或偽造),一律不採信:%r", all_ar[:1])
        return _get("Subject"), _get("From"), "".join(trusted)
    except Exception:
        logging.debug("[IMAP] header 解析失敗", exc_info=True)
        return "", "", ""


def _from_is_authenticated(auth_results: str, from_addr: str) -> bool:
    """Authentication-Results 是否證明這封信的 From 通過驗證。

    ★[2026-08-06 外審 P1-05]★ `From:` 是寄件者自填的純文字,可以偽造;只比對它
    等於沒有授權驗證。Gmail 收信時會把 SPF/DKIM/DMARC 判定寫進這個 header。

    判準(保守):dmarc / dkim / spf 任一 =pass,【且】該機制標示的網域與實際 From 的
    網域完整相等或為其子網域,才算通過。只有 =pass 而網域對不上不算(spf 只證明信封
    寄件者;dmarc/dkim 的網域也可能根本是別人的)。
    沒有這個 header(例如自己寄給自己、或郵件服務不加)→ False(無證據 ≠ 通過)。

    ★[2026-08-06 外審] 網域比對不可以用 substring★
    上一版寫 `if domain in seg` —— `"gmail.com" in "attacker-gmail.com"` 是 True,
    於是攻擊者只要用自己完全控制、能通過 DKIM 的 `attacker-gmail.com`,再把 From
    偽造成 `doctor@gmail.com` 就會被判定為「已對齊」。改成解析出實際網域再比對。
    """
    text = (auth_results or "").lower()
    if not text:
        return False
    domain = (from_addr or "").rsplit("@", 1)[-1].strip().lower().rstrip(".")
    if not domain:
        return False

    def _aligned(value: str) -> bool:
        """value 是 header.d= / smtp.mailfrom= 取出的網域(可能帶 email)。"""
        v = value.strip().strip('"<>').rstrip(".").lower()
        if "@" in v:
            v = v.rsplit("@", 1)[-1]
        # 完全相等,或 value 是 from 網域的子網域(mail.example.com vs example.com)
        return bool(v) and (v == domain or v.endswith("." + domain))

    # ★[2026-08-07 外審] dmarc=pass 也必須核對 header.from★
    #   上一版寫 `if "dmarc=pass" in text: return True` —— 無條件通過。理由當時
    #   寫成「DMARC 本身即要求對齊」,但那只保證【該封信自己的 From】與它自己的
    #   驗證網域對齊;這個 header 是文字,可能來自轉寄上游、也可能整段是攻擊者
    #   塞進信裡的。攻擊者只要用自己完全控制、能通過 DMARC 的網域寄信,得到
    #   `dmarc=pass header.from=attacker.example`,再把 From 偽造成白名單醫師,
    #   舊版就直接放行。現在 dmarc 與 dkim/spf 走同一套 header.from 對齊檢查。
    for mech, keys in (("dmarc", ("header.from=",)),
                       ("dkim", ("header.d=", "header.i=")),
                       ("spf", ("smtp.mailfrom=", "envelope-from="))):
        idx = text.find(f"{mech}=pass")
        if idx < 0:
            continue
        seg = text[idx:idx + 300]
        for key in keys:
            k = seg.find(key)
            if k < 0:
                continue
            # 取該 key 之後、到下一個分隔字元為止的值
            raw = seg[k + len(key):]
            value = re.split(r"[;\s,()]", raw, maxsplit=1)[0]
            if _aligned(value):
                return True
    return False


def mailbox_identity() -> str:
    """帳號+信箱的指紋 —— journal/收據的鍵要用它當命名空間。

    ★[外審 2026-08-12 P1-06/P2-07]★ UID 只在【同一個帳號、同一個 mailbox、
    同一個 UIDVALIDITY 世代】裡才有意義:換 IMAP 帳號後,相同的
    UIDVALIDITY+UID 可能剛好撞號 —— 舊收據會把新指令/新觸發認成處理過的。
    取不到帳號 → 空字串(呼叫端一律 fail-closed:不執行、不落地)。
    指紋用雜湊:鍵會進磁碟上的 journal,不要把帳號明文散出去。
    """
    return _identity_from_settings(_load_imap_settings())


def _identity_from_settings(s) -> str:
    """★身分要跟連線用同一份設定算★(外審 AD-4 第 1 輪 P1-3)

    掃描之後另外重載憑證的話,兩次載入之間換了帳號,A 連線掃到的 UID 會
    被掛在 B 的身分下 —— 身分與 UID 世代必須描述【同一個】掃描情境。
    掃描函式用自己那份 `s` 呼叫這裡,並把結果放進回傳值。
    """
    import hashlib
    user = str((s or {}).get("username") or "").strip().lower()
    if not user:
        return ""
    return hashlib.sha256(user.encode("utf-8")).hexdigest()[:12] + ":INBOX"


def read_uidvalidity(conn) -> str:
    """SELECT 之後把 UIDVALIDITY 讀出來。讀不到 → 空字串(呼叫端 fail-closed)。"""
    try:
        uv = conn.response("UIDVALIDITY")[1]
        if uv and uv[0]:
            return (uv[0].decode() if isinstance(uv[0], bytes) else str(uv[0]))
    except Exception:
        logging.debug("[IMAP] 取 UIDVALIDITY 失敗(用空字串)", exc_info=True)
    return ""


def mark_uids_seen(uids, expect_uidvalidity: str = "",
                   expect_identity: str = "") -> bool:
    """把這幾封信標成已讀(獨立連線)。回傳是否成功。

    ★[2026-08-08 外審]★ 與 `check_trigger` 分開,是為了讓呼叫端可以
    「先把工作持久化、再標已讀」。順序反過來的話,兩者之間中止就等於
    那封觸發信永遠消失。
    """
    ids = [str(u).strip() for u in (uids or []) if str(u).strip()]
    if not ids:
        return True
    s = _load_imap_settings()
    if not s["password"]:
        logging.warning("[IMAP] 未設定密碼,無法標已讀")
        return False
    # ★帳號也要對得上★(外審 AD-4 第 1 輪 P1-3):這裡又重載了一次設定 ——
    #   掃描之後憑證被換掉的話,同一個 uid(甚至同一個 UIDVALIDITY)在
    #   【另一個帳號】可能指向不相干的信。不符就不連線、不 STORE。
    if expect_identity and _identity_from_settings(s) != expect_identity:
        logging.error("[IMAP] ★帳號已改變 → 不標已讀★(掃描時=%s)",
                      expect_identity)
        return False
    # ★[2026-08-08 外審第 2 回] 這條連線也要納入既有的 active/watchdog 機制★
    #   上一版直接在 scheduler thread 開一條新連線,而且用了本檔註解
    #   明確禁止的 blocking `logout()` —— socket 半死時它會等在那裡,
    #   而 `force_close_active()` 找不到這條連線,救不了它。
    #   結果就是排程器整個停住(它是這支程式的心跳)。
    conn = None
    try:
        context = ssl.create_default_context()
        conn = imaplib.IMAP4_SSL(s["host"], s["port"], ssl_context=context,
                                 timeout=12.0)
        _set_active(conn, "ack")
        conn.login(s["username"], s["password"])
        conn.select("INBOX")
        # ★世代要對得上★(外審 2026-08-12 P1-06):這是一條【新的】連線,
        #   掃描到現在之間信箱可能被重建過 —— UIDVALIDITY 變了,同一個 uid
        #   已經指向【另一封信】,STORE 會把不相干的信標成已讀。
        #   不符就不標(信留在未讀,下一輪重掃;去重靠 journal 的世代化鍵)。
        if expect_uidvalidity:
            cur_uv = read_uidvalidity(conn)
            if cur_uv != expect_uidvalidity:
                logging.error("[IMAP] ★UIDVALIDITY 已改變(%s→%s)→ 不標已讀★"
                              "同一個 uid 可能已指向別封信", expect_uidvalidity,
                              cur_uv)
                return False
        # ★[外審 SI 第 1 輪 P1-2] 要看 `typ`★ IMAP 可以【正常返回】
        #   NO/BAD(配額、權限、mailbox 唯讀…)而不拋例外。本函式的契約是
        #   「回傳是否成功」,而呼叫端拿它做 fail-closed 判斷:無條件回 True
        #   等於「標不掉卻說標好了」→ 指令每一輪重跑一次(無限重啟迴圈)。
        typ, data = conn.uid("store", ",".join(ids), "+FLAGS", "(\\Seen)")
        if typ != "OK":
            logging.error("[IMAP] 標已讀被拒(typ=%s data=%s uids=%s)",
                          typ, data, ids[:5])
            return False
        return True
    except Exception:
        logging.warning("[IMAP] 標已讀失敗(uids=%s)", ids[:5], exc_info=True)
        return False
    finally:
        # 與 `check_trigger` 收尾同一套:不呼叫 logout(它會等回應、
        # socket 死了就 hang 住整個 finally),直接砍底層 socket。
        _clear_active(conn)
        _force_close_conn(conn)


# 寄件備份可能叫什麼名字（Gmail 中英文介面、以及一般 IMAP 伺服器）。
# ★選不到就回 None，不可以回 False★ ——「找不到信箱」不等於「信沒寄出去」。
_SENT_MAILBOXES = (
    '"[Gmail]/Sent Mail"', '"[Gmail]/&Xn9uRZR-"',   # 英文 / 中文「寄件備份」
    '"[Google Mail]/Sent Mail"', '"Sent"', '"Sent Items"', '"INBOX.Sent"',
)


def _message_id_is_safe(message_id: str) -> bool:
    """能不能安全地放進 IMAP SEARCH 的加引號字串。

    ★這是注入防線★ Message-ID 進了 IMAP 指令列。雙引號/反斜線會提前結束
    字串，CR/LF 會直接多送一整條 IMAP 指令。合法的 Message-ID 本來就不含
    這些字元 —— 含了就是不對勁，寧可回報「查不出來」。
    """
    if not message_id or len(message_id) > 300:
        return False
    return not any(ch in message_id for ch in ('"', chr(92), chr(13), chr(10)))


def find_message_in_sent(message_id: str, timeout: float = 12.0):
    """這封信在寄件備份裡嗎？→ True 有／False 確定沒有／**None 查不出來**。

    ★三態，而且 None 絕對不可以被當成 False★
    這是 `delivery_ledger` 那套 UNKNOWN 收斂的最後一塊：SMTP 逾時的時候
    「伺服器可能已經收下了」，只有回查寄件備份才問得出答案。
    把「查不出來」摺成「沒寄出去」就會重寄一封已經送到的信；摺成「有寄到」
    則會把真正的漏寄無聲吞掉。兩個方向都錯，所以它必須是三態。

    只有【選得到寄件備份、SEARCH 也成功回 OK】才給得出 True/False。
    """
    if not _message_id_is_safe(message_id):
        logging.warning("[IMAP] Message-ID 不適合放進 SEARCH → 回報查不出來")
        return None
    s = _load_imap_settings()
    if not (s["host"] and s["username"] and s["password"]):
        return None
    conn = None
    try:
        context = ssl.create_default_context()
        conn = imaplib.IMAP4_SSL(s["host"], s["port"], ssl_context=context,
                                 timeout=timeout)
        _set_active(conn, "sent")
        conn.login(s["username"], s["password"])
        selected = ""
        for box in _SENT_MAILBOXES:
            try:
                typ, _d = conn.select(box, readonly=True)
            except Exception:
                continue
            if typ == "OK":
                selected = box
                break
        if not selected:
            logging.warning("[IMAP] 找不到寄件備份信箱 → 回報查不出來(不是查無)")
            return None
        # ★不傳 charset 的 None★ imaplib 的 `_command` 會跳過 None,所以它
        #   在執行期可用,但那是靠實作細節;`UID SEARCH HEADER ...` 本來
        #   就不需要 charset,直接不傳最乾淨(也不會多一筆型別債)。
        typ, data = conn.uid("search", "HEADER", "Message-ID",
                             '"%s"' % message_id)
        if typ != "OK":
            logging.warning("[IMAP] 寄件備份 SEARCH 未回 OK(%s) → 查不出來", typ)
            return None
        hits = (data[0] or b"").split() if data else []
        return bool(hits)
    except Exception:
        logging.warning("[IMAP] 回查寄件備份失敗 → 回報查不出來", exc_info=True)
        return None
    finally:
        # 與本檔其他連線同一套收尾：不呼叫 logout（socket 半死會 hang 住）。
        _clear_active(conn)
        _force_close_conn(conn)


def command_is_expired(age_sec, max_age_sec) -> bool:
    """指令信算不算過期。純函式（抽出來才測得到）。

    ★時間不明（`age_sec is None`）一律算過期★ —— 指令 fail-closed。
    查詢觸發是【相反】的 fail-open（`_message_age_seconds` 讀不到就照常
    觸發），因為多跑一次查詢無害、漏掉一次會診請求有害；指令反過來:
    多重啟一次不是無害的。

    ★[外審 SI 第 1 輪之後補]★ 這段本來寫在 `check_commands` 裡面，
    而測試又把整個 `check_commands` 假掉、直接餵 `expired` 旗標 ——
    突變驗證量出來：把它改成永遠 False，測試【全綠】。
    測試要用生產的那段程式，不是自己另外算一次。
    """
    if not max_age_sec or max_age_sec <= 0:
        return False        # 沒設時效上限 → 不做這個判斷
    return age_sec is None or age_sec > max_age_sec


_REPLY_PREFIX_RE = re.compile(r"^\s*(re|fw|fwd|回覆|轉寄)\s*:\s*",
                              re.IGNORECASE)


def normalize_subject(subject) -> str:
    """主旨正規化（全形空白→半形、剝掉 `Re:`/`Fwd:` 之類的前綴）。純函式。

    ★[外審 SJ 第 1 輪 P2-4] 掃描端與解析端必須用【同一個】正規化★
    掃描端原本是 `subj.startswith(短語)`，而解析端是在任意位置 `find`。
    於是 `Re: 皮膚科會診重開` 在掃描端就被濾掉，根本到不了解析端 ——
    契約明明說允許 `Re:`。兩邊各寫一套判準，就會有一邊靜默失效。
    """
    text = str(subject or "").replace("　", " ").strip()
    for _ in range(4):          # 有些客戶端會疊好幾層 `Re: Re:`
        stripped = _REPLY_PREFIX_RE.sub("", text).strip()
        if stripped == text:
            break
        text = stripped
    return text


def check_commands(prefixes, timeout: float = 12.0,
                   max_age_sec: Optional[float] = None) -> dict:
    """掃未讀信裡【主旨以其中一個短語開頭】的遠端指令信。

    → `{"items": [...], "error": ..., "uidvalidity": "..."}`

    每個 item：`{"uid", "sender", "authenticated", "subject", "age_sec"}`。

    ★為什麼另開一個函式，而不是擴充 `check_trigger`★
    `check_trigger` 刻意【不把主旨交出去】（見 `_subject_fingerprint`：那個信箱
    收到的任何信件主旨都可能含病人姓名、床號，而 log 沒有跟 Email 一樣的保存
    政策）。遠端指令卻必須讀主旨才知道要做什麼、對哪一台做。
    ★邊界靠「只回傳主旨以我們自己的固定前綴開頭的信」★ —— 那種主旨是我們
    自己的約定格式，不可能是別人寄來的臨床郵件。其他信件連主旨都不會被讀出來。

    ★本函式【不改任何 flag】★：要不要標已讀由呼叫端決定（指令的取捨與查詢
    觸發相反，見 consult_query 的說明）。
    """
    out: dict = {"items": [], "error": None, "uidvalidity": "",
                 "mailbox_identity": ""}
    heads = [str(p) for p in (prefixes or []) if str(p)]
    if not heads:
        out["error"] = "沒有給任何指令短語"
        return out
    s = _load_imap_settings()
    if not s["password"]:
        out["error"] = "SMTP/IMAP password 未設定"
        return out
    conn: Optional[imaplib.IMAP4_SSL] = None
    try:
        context = ssl.create_default_context()
        conn = imaplib.IMAP4_SSL(s["host"], s["port"], ssl_context=context,
                                  timeout=timeout)
        _set_active(conn, "commands")
        conn.login(s["username"], s["password"])
        conn.select("INBOX")
        out["mailbox_identity"] = _identity_from_settings(s)
        # ★UIDVALIDITY★ 呼叫端拿它跟 uid 一起當【本機收據】的鍵:
        #   UID 只在 UIDVALIDITY 不變時才穩定。信箱被重建過的話,
        #   舊收據可能壓住一封剛好撞到同一個 uid 的新指令。
        try:
            uv = conn.response("UIDVALIDITY")[1]
            if uv and uv[0]:
                out["uidvalidity"] = (
                    uv[0].decode() if isinstance(uv[0], bytes)
                    else str(uv[0]))
        except Exception:
            logging.debug("[IMAP] 取 UIDVALIDITY 失敗(用空字串)",
                          exc_info=True)
        # 中文前綴在 imaplib 的 ASCII 編碼階段就會拋 → 一律走「撈 UNSEEN 後
        # client 端比對」，與 `check_trigger` 的後備路徑同一個做法。
        typ, data = conn.uid("search", "UNSEEN")
        if typ != "OK" or not data:
            out["error"] = f"IMAP SEARCH 異常回應：{typ}"
            return out
        ids = data[0].split() if data[0] else []
        if len(ids) > _MAX_SCAN_IDS:
            ids = ids[-_MAX_SCAN_IDS:]
        from email.utils import parseaddr
        for uid in ids:
            try:
                typ, fetch = conn.uid(
                    "fetch", uid,
                    "(BODY.PEEK[HEADER.FIELDS "
                    "(SUBJECT FROM AUTHENTICATION-RESULTS)])")
                if typ != "OK" or not fetch:
                    continue
                header_raw = b""
                for part in fetch:
                    if isinstance(part, tuple) and len(part) >= 2:
                        header_raw = part[1]
                        break
                subj, from_str, auth_str = _parse_trigger_headers(header_raw)
                if not any(normalize_subject(subj).startswith(h)
                           for h in heads):
                    continue        # ★不是我們的指令信 → 主旨連交出去都不交★
                addr = (parseaddr(from_str)[1] or "").strip().lower()
                out["items"].append({
                    "uid": uid.decode() if isinstance(uid, bytes) else str(uid),
                    "sender": addr,
                    "authenticated": _from_is_authenticated(auth_str, addr),
                    "subject": subj.strip(),
                    "age_sec": _message_age_seconds(conn, uid),
                })
            except Exception:
                logging.debug("[IMAP] 指令信解析失敗(略過這一封)", exc_info=True)
                continue
        # 時效過濾放在這裡而不是呼叫端:讀不到 INTERNALDATE 時 `age_sec` 是 None,
        # ★指令要 fail-closed★(與查詢觸發相反:多跑一次查詢無害,多重啟一次不是)。
        # ★[外審 SI 第 1 輪 P2-5] 過期的【不可以直接丟掉】★
        #   丟掉的話呼叫端連 uid 都拿不到,那封信就永遠停在 UNSEEN ——
        #   每一輪(20 秒)都要為它 FETCH header + FETCH INTERNALDATE 並寫
        #   一行 warning。50 封就是每 20 秒約 100 次 round-trip,而且每一台
        #   共用信箱的機器各做一份。那是一個【不需要通過驗證】就能發動的
        #   資源與 log DoS。改成標記 `expired`,由呼叫端做終局處置。
        for it in out["items"]:
            it["expired"] = command_is_expired(it.get("age_sec"),
                                             max_age_sec)
        return out
    except Exception as e:      # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
        return out
    finally:
        # ★[外審 SI 第 1 輪 P1-3]★ 本檔第 782 行的註解【明文禁止】
        #   `logout()`(它 send LOGOUT + 等回應,socket 半死就 hang 住整個
        #   finally),而我還是寫了 —— 更糟的是先 `_clear_active`,連
        #   self-watchdog 的 `force_close_active()` 都找不到這條連線來救它。
        #   排程器是這支程式的心跳,卡在這裡等於整支停住。
        _clear_active(conn)
        _force_close_conn(conn)


def check_trigger(keyword: str, mark_read: bool = True,
                   timeout: float = 12.0,
                   sample_count: int = 3,
                   max_age_sec: Optional[float] = None,
                   defer_mark_matched: bool = False) -> dict:
    """掃描 IMAP 收件匣未讀信，主旨含 keyword 的就回報、抓 From 地址、並標為已讀。

    回傳 dict：
      triggered (bool)：有比對到至少一封 → True
      scanned (int)：本次掃了多少封未讀
      matched (int)：主旨含 keyword 的未讀數
      matched_senders (list[str])：比對到的信件 From 地址（去重小寫，可能空）。
                       呼叫端可用來判斷「誰觸發的」並把結果回寄給他。
      samples (list[str])：若 matched=0，回 sample_count 個最近未讀信件的
                       ★指紋★（長度＋雜湊，不是主旨原文）給 debug
      error (str|None)：例外訊息（連線/認證失敗等），有錯時其他欄位無意義

    side effect：matched > 0 時把那些信標為 Read（\\Seen flag），避免重複觸發。
    """
    result = {
        "triggered": False,
        "scanned": 0,
        "matched": 0,
        "matched_senders": [],
        # [2026-08-08 外審] (uid, 寄件人, 是否通過驗證)。呼叫端要先把「這個 uid
        # 對應的工作」持久化,才可以呼叫 mark_uids_seen() —— 順序反過來
        # 的話,兩者之間中止就等於那封觸發信永遠消失。
        "matched_uids": [],
        # [2026-08-06 外審 P1-05] matched_senders 的子集:From 有通過
        # SPF/DKIM/DMARC 驗證者。呼叫端要做「可信授權」時看這個,不要只看
        # matched_senders(那裡的 From 是可偽造的純文字)。
        "authenticated_senders": [],
        "samples": [],
        "error": None,
        # ★呼叫端拿它與 uid 一起組【世代化】的 journal 鍵★(外審 2026-08-12
        #   P1-06);也是 mark_uids_seen 的世代驗證依據。空=取不到,fail-closed。
        "uidvalidity": "",
        # ★與這條連線同一份設定算出來的帳號身分★(外審 AD-4 第 1 輪 P1-3)
        "mailbox_identity": "",
    }
    if not keyword:
        result["error"] = "keyword 為空"
        return result

    s = _load_imap_settings()
    if not s["password"]:
        result["error"] = ("SMTP/IMAP password 未設定（編輯 "
                            "settings/smtp_credentials.json）")
        return result

    # 【穩定性 2026.05.20】不用 socket.setdefaulttimeout — 那是 process-global，
    # 會污染同 process 的 SMTP / selenium / requests。IMAP4_SSL(timeout=...) 已夠。
    # [2026-08-06 深度穩定] 單操作逾時 30→12 秒:一次檢查有多個 socket 操作
    # (login/select/search/逐封 fetch),各 30 秒的話兩三個停滯就撞上外層 watchdog
    # 的 60 秒強制砍 socket(3 天 12 次,還伴隨 daemon thread 收不回來)。Gmail 正常
    # 每操作 <2 秒;12 秒已是十倍餘裕,單點卡住改為快速失敗、走正常重試路徑。
    conn: Optional[imaplib.IMAP4_SSL] = None
    try:
        context = ssl.create_default_context()
        conn = imaplib.IMAP4_SSL(s["host"], s["port"], ssl_context=context,
                                  timeout=timeout)
        _set_active(conn, "trigger")
        conn.login(s["username"], s["password"])
        conn.select("INBOX")
        result["uidvalidity"] = read_uidvalidity(conn)
        result["mailbox_identity"] = _identity_from_settings(s)

        # 用 IMAP SEARCH 直接過濾「未讀 + 主旨含 keyword」，避免拉全部
        # 注意：IMAP SEARCH 對非 ASCII 主旨要用 LITERAL+CHARSET UTF-8
        # imaplib 支援：search(charset, *criteria)
        # ★[2026-08-06 外審 P2-02] 一律用 UID API,不用 message sequence number★
        #   舊版用 conn.search/fetch/store —— 那是【序號】API。序號會因為其他
        #   client(手機 Gmail App、網頁版)在我們 SEARCH 與 FETCH/STORE 之間
        #   EXPUNGE 而整批位移 → 可能讀到另一封信、甚至把【另一封】標成已讀。
        #   UID 在同一個 mailbox 內穩定不變(UIDVALIDITY 不變時),既正確也才能
        #   當持久化的去重鍵。
        try:
            # ASCII 主旨 → server-side SEARCH(高效);中文主旨會在 imaplib ASCII 編碼階段先拋
            # UnicodeEncodeError → 落 except 後備「全 UNSEEN client 端比對」。
            # [IF-05 2026-07-12] 移除原「typ!=OK 改 UTF-8 mode」死碼:中文走的是【例外】路徑而非
            # typ!=OK,該 UTF-8 retry 永不執行(kw_bytes 一併移除)。
            # 註:`IMAP4.search(charset, ...)` 的第一個參數是 charset;UID SEARCH
            # 這裡是直接傳命令參數,不需要(也不該)傳 charset 的 None 佔位。
            typ, data = conn.uid("search", "UNSEEN", "SUBJECT",
                                 f'"{keyword}"')
        except Exception:
            # 後備：撈 UNSEEN 後 client 端比對
            typ, data = conn.uid("search", "UNSEEN")
            if typ != "OK":
                # 後備搜尋本身回了非 OK → 這是新的失敗條件(訊息已自足),與觸發後備的
                # 原例外無因果關係 → from None 明示不接續原鏈。
                raise RuntimeError(f"IMAP SEARCH 失敗：{typ} {data}") from None

        if typ != "OK" or not data:
            result["error"] = f"IMAP SEARCH 異常回應：{typ}"
            return result

        ids = data[0].split() if data[0] else []
        # [2026-07-25 審查] 只掃「最新的 N 封」。中文關鍵字必然走這條後備路徑
        # (imaplib 對非 ASCII 關鍵字會在編碼階段拋例外) → 每封未讀信都要一次 FETCH
        # round-trip。信箱累積數百封未讀時,單輪就會超過 IMAP_HARD_TIMEOUT(60s) →
        # 看門狗每輪強制關 socket → 3 次錯誤 → 5 分鐘冷卻 → 循環,email 觸發功能實質
        # 永久失效(只留 warning)。觸發信本來就是「剛剛寄的」,掃最新幾十封即足夠;
        # SEARCH 回傳的序號為遞增,故取尾端＝最新。
        if len(ids) > _MAX_SCAN_IDS:
            logging.warning(
                "[IMAP] 未讀 %d 封超過單輪掃描上限 %d → 只檢查最新 %d 封"
                "（信箱未讀過多，建議清理；觸發信為即時寄出故不受影響）",
                len(ids), _MAX_SCAN_IDS, _MAX_SCAN_IDS)
            ids = ids[-_MAX_SCAN_IDS:]
        result["scanned"] = len(ids)

        from email.utils import parseaddr

        matched_ids = []
        stale_ids = []  # [會診2] 主旨命中但太舊的觸發信(只清掉、不觸發)
        senders_seen = set()
        authed_seen = set()
        for uid in ids:
            try:
                typ, fetch = conn.uid(
                    "fetch", uid,
                    "(BODY.PEEK[HEADER.FIELDS "
                    "(SUBJECT FROM AUTHENTICATION-RESULTS)])")
                if typ != "OK" or not fetch:
                    continue
                # fetch 結構：[(b'1 (BODY...', b'Subject: ...\r\nFrom: ...\r\n'), b')']
                header_raw = b""
                for part in fetch:
                    if isinstance(part, tuple) and len(part) >= 2:
                        header_raw = part[1]
                        break
                # ★[2026-08-06 外審 P2-04] 用 email 標準 parser,不要逐行 startswith★
                #   RFC 5322 的 header 可以【折行】(長 display name、多段 RFC 2047
                #   encoded-word 都會折)。舊版逐行比對只會拿到第一行 → 主旨關鍵字
                #   漏判、From 變空字串 → 白名單寄件人被誤拒、觸發靜默失效。
                subj_str, from_str, auth_str = _parse_trigger_headers(header_raw)
                if keyword in subj_str:
                    # [會診2 2026-06-11] 觸發信時效過濾：程式停機數天(或長期標已讀
                    # 失敗)累積的舊未讀觸發信，恢復後第一輪 poll 會全部命中 → 把幾天
                    # 前的請求當現在處理、回寄與當下不符的截圖。超過時效的命中信改
                    # 「標已讀清掉但不觸發」。INTERNALDATE 解析失敗 → fail-open 照常
                    # 觸發(寧可多觸發、不可漏會診請求)。
                    if max_age_sec and max_age_sec > 0:
                        age = _message_age_seconds(conn, uid)
                        if age is not None and age > max_age_sec:
                            stale_ids.append(uid)
                            logging.warning(
                                "[IMAP] 忽略陳舊觸發信(已 %.1f 小時 > 上限 %.1f "
                                "小時)：主旨=%r 寄件人=%r — 標已讀不觸發",
                                age / 3600, max_age_sec / 3600,
                                subj_str[:60], from_str[:60])
                            continue
                    matched_ids.append(uid)
                    # ★[2026-08-08 外審] 把 uid 與寄件人配起來回報★
                    #   呼叫端要先把「這個 uid 對應的工作」持久化,才可以標
                    #   \Seen —— 沒有 uid 就做不到(見 `defer_mark_matched`)。
                    # parseaddr 解 "Name <foo@bar.com>" → ("Name", "foo@bar.com")
                    _, addr = parseaddr(from_str)
                    addr = (addr or "").strip().lower()
                    # ★[2026-08-06 外審 P1-05] 記錄寄件人驗證結果★
                    #   From: 是寄件者自己填的字串,可以偽造。Gmail 會在收下時把
                    #   SPF/DKIM/DMARC 的判定寫進 Authentication-Results。這裡把
                    #   「這封信的 From 有沒有通過驗證」一併回報,呼叫端才有可信的
                    #   授權依據(而不是只比對一個可偽造的字串)。
                    auth_ok = _from_is_authenticated(auth_str, addr)
                    if not auth_ok:
                        logging.warning(
                            "[IMAP] 觸發信寄件人未通過 SPF/DKIM/DMARC 驗證"
                            "(From 可偽造):%r;Authentication-Results=%r",
                            addr, (auth_str or "")[:200])
                    # ★[2026-08-08 外審] 驗證結果要綁在【每一封信】上★
                    #   `senders_seen` 以地址去重:同一輪先掃到一封偽造的未驗證
                    #   信,後面那封【合法且已驗證】的就不會被加進
                    #   `authenticated_senders` —— 攻擊者只要持續寄較早的偽造信,
                    #   就能讓那位醫師的授權觸發長期失效。
                    #   逐封記 auth_ok;寄件人清單只用來顯示與合併收件人。
                    try:
                        result["matched_uids"].append(
                            (uid.decode("ascii", "replace"), addr,
                             bool(auth_ok)))
                    except Exception:
                        logging.debug("[IMAP] 記錄 uid 失敗", exc_info=True)
                    if addr and addr not in senders_seen:
                        senders_seen.add(addr)
                        result["matched_senders"].append(addr)
                    if addr and auth_ok and addr not in authed_seen:
                        authed_seen.add(addr)
                        result["authenticated_senders"].append(addr)
                elif len(result["samples"]) < sample_count:
                    result["samples"].append(_subject_fingerprint(subj_str))
            except Exception:
                logging.debug("IMAP fetch 單筆失敗（忽略）", exc_info=True)
                continue

        result["matched"] = len(matched_ids)
        result["triggered"] = result["matched"] > 0

        # [會診2] 陳舊命中信一併標已讀(清掉，避免之後每輪 poll 重複命中+重複 log)
        # ★[2026-08-08 外審] 命中信可以【延後】標記★
        #   舊寫法在 `check_trigger` 回傳之前就標了 \Seen。之後才回到排程器、
        #   才起 worker —— 這中間程式若結束/重啟,那封信已經不是 UNSEEN,
        #   永遠不會再被掃到,醫師乾等一個不會來的結果。
        #   `defer_mark_matched=True` 時由呼叫端在【工作已持久化之後】自己
        #   呼叫 `mark_uids_seen()`。陳舊命中信不受影響(它們不會被觸發,
        #   標掉只是清乾淨)。
        ids_to_mark = ([] if defer_mark_matched
                       else (list(matched_ids) if mark_read else []))
        if mark_read:
            ids_to_mark += stale_ids
        if ids_to_mark:
            # 一次標記多封為已讀
            try:
                id_list = b",".join(ids_to_mark).decode("ascii")
                # [外審 P2-02] UID STORE —— 與上面的 UID SEARCH/FETCH 一致,
                # 否則會用序號去標記,可能標到另一封信。
                # ★[外審 2026-08-12 P2-03] 要看 typ★ NO/BAD 是正常返回不拋 ——
                #   當成功的話,陳舊觸發信一直 UNSEEN,每輪重掃、重發告警。
                typ, _d = conn.uid("store", id_list, "+FLAGS", "(\\Seen)")
                if typ != "OK":
                    logging.warning("標已讀被拒(typ=%s)—— 這些信下輪會再掃到",
                                    typ)
            except Exception:
                logging.warning("標已讀失敗（不影響觸發）", exc_info=True)

        # 不用 conn.close() (要 SELECT 後 EXPUNGE，可能 hang)，
        # 直接砍 socket 由 finally 處理。

    except imaplib.IMAP4.error as e:
        msg = str(e)
        if "AUTHENTICATIONFAILED" in msg.upper() or "Invalid credentials" in msg:
            result["error"] = (f"IMAP 認證失敗：{e}。請確認 password 是 Gmail "
                                "App Password（16 字元）。")
        else:
            result["error"] = f"IMAP 錯誤：{e}"
    except (socket.timeout, TimeoutError) as e:
        result["error"] = f"IMAP 連線/讀取逾時（{int(timeout)}s）：{e}"
    except OSError as e:
        result["error"] = f"IMAP 網路錯誤：{e}"
    except Exception as e:  # noqa: BLE001
        result["error"] = f"IMAP 未知錯誤：{type(e).__name__}: {e}"
    finally:
        # 重要：不呼叫 conn.logout()，它內部 send LOGOUT + 等回應，
        # socket 死了會 hang 整個 finally。直接砍底層 socket 就好。
        _clear_active(conn)
        _force_close_conn(conn)

    return result
