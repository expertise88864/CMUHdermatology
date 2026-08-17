# -*- coding: utf-8 -*-
"""SMTP 寄信工具（共用模組）。

為什麼不用 Outlook COM：
  consult_query / main 程式以 admin 執行 → 透過 win32com.DispatchEx 啟動
  Outlook 時會拉起一個 admin-level 的 Outlook 實例，這個實例的 MAPI profile
  跟使用者日常 user-level Outlook 不同（用 administrator 的 profile，預設沒
  設定任何郵件帳號），導致 mail.Send() 成功但信永遠卡在隱形 Outbox 寄不出。
  改用 SMTP 直接連 smtp.gmail.com，完全跳過 Windows UAC + Outlook profile
  地獄，admin / user 任何權限都能寄。

設定檔（settings/smtp_credentials.json）：
  {
    "host": "smtp.gmail.com",
    "port": 587,
    "username": "cmuhdermatology@gmail.com",
    "password": "<16 字元 app password>",
    "use_tls": true,
    "from_address": "cmuhdermatology@gmail.com",
    "from_name": "中國醫皮膚科系統"
  }

App Password 取得（一次性）：
  1. 用 cmuhdermatology@gmail.com 登入 https://myaccount.google.com/
  2. 安全性 → 啟用「兩步驟驗證」（必要前提）
  3. 安全性 → 應用程式密碼 (https://myaccount.google.com/apppasswords)
  4. 自訂名稱「皮膚科自動寄信」→ 建立 → 複製 16 字元密碼
  5. 貼到 settings/smtp_credentials.json 的 password 欄位
"""
from __future__ import annotations

import logging
import re
import smtplib
import socket
import ssl
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Optional

from cmuh_common.paths import get_settings_dir
from cmuh_common.atomic_io import atomic_write_json, safe_load_json_ex

CREDENTIALS_FILE = Path(get_settings_dir()) / "smtp_credentials.json"

# [C] Rate limit：保護機制防 bug 觸發無窮迴圈狂寄信
#
# ★[2026-07-30 第二輪外審 P2-02]★ 判斷與計數已整批搬到 cmuh_common/mail_quota.py。
# 原本是這個模組裡的一個 deque + threading.Lock —— 只鎖得住同一個 process 的
# thread，但 main / autoclock / consult_query / watchdog / scheduler 是五支獨立
# 程式共用同一個 Gmail 帳號，各自以為自己有 30 封／小時。詳細取捨見該模組 docstring。
from cmuh_common import mail_quota as _quota

RATE_LIMIT_WINDOW_SEC = _quota.WINDOW_SEC   # 統計區間 1 小時
RATE_LIMIT_MAX = _quota.TOTAL_MAX           # 1 小時內最多 30 封（跨行程合計）
DEFAULT_MAX_RETRIES = 2
MAX_RETRIES = 5

# 對外沿用舊名（外部 except 這個名字的地方不必改）；實體是同一個類別。
SmtpRateLimitExceeded = _quota.MailQuotaExceeded

CATEGORY_CLINICAL = _quota.CATEGORY_CLINICAL
CATEGORY_SYSTEM = _quota.CATEGORY_SYSTEM

# 行程內那層的 deque 本體（降級時唯一生效的一層）。指向 mail_quota 的同一個物件，
# 測試沿用 `smtp_mail._recent_send_reservations` 觀察行程內狀態仍然有效。
_recent_send_reservations = _quota._recent


def _normalize_max_retries(value) -> int:
    """Clamp retry counts so bad config cannot skip sending or retry forever."""
    if isinstance(value, bool):
        return DEFAULT_MAX_RETRIES
    try:
        retries = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_RETRIES
    return max(0, min(MAX_RETRIES, retries))


def _smtp_account(cred: dict) -> str:
    """配額鑰匙用的帳號。

    用 `username`（＝真正向 Gmail 認證、真正有寄送額度的那個帳號）而不是
    `from_address`：兩者可以不同（別名寄件），而額度綁在認證帳號上。
    """
    return str(cred.get("username") or cred.get("from_address") or "?")


def _reserve_rate_limit_slot(cred: "Optional[dict]" = None,
                             category: str = CATEGORY_CLINICAL):
    """Reserve one logical send slot. Roll it back if delivery fails."""
    return _quota.reserve(account=_smtp_account(cred or {}), category=category)


def _rollback_rate_limit_slot(reservation) -> None:
    _quota.release(reservation)

DEFAULT_CREDENTIALS = {
    "host": "smtp.gmail.com",
    "port": 587,
    "username": "cmuhdermatology@gmail.com",
    "password": "",  # 必須由使用者填入 App Password（16 字元）
    "use_tls": True,
    "from_address": "cmuhdermatology@gmail.com",
    "from_name": "中國醫皮膚科系統",
}


# 例外身上的階段標記:True = 郵件內容【已經提交】給伺服器(在等最終回應)。
# 沒有這個標記 = 還在連線/STARTTLS/登入階段,伺服器確定沒收到任何內容。
# 逾時要不要重試完全取決於它(見 send_mail 的兩條 socket.timeout 分支)。
SUBMITTED_ATTR = "_cmuh_submitted"


def _dot_stuff(payload: bytes) -> bytes:
    """RFC 5321 dot-stuffing + 結尾 ".<CRLF>"。純函式。

    smtplib.data() 內部的等價物(它用的 `_quote_periods` 是模組私有,
    不依賴):行首的 "." 要疊成 ".." —— 不疊的話,內文裡一行單獨的 "."
    會【提早結束 DATA】,後面的內容變成 SMTP 指令(截斷+協定錯亂)。
    """
    q = re.sub(br"(?m)^\.", b"..", payload)
    if q[-2:] != b"\r\n":
        q += b"\r\n"
    return q + b".\r\n"


class DeliveryOutcomeUnknown(RuntimeError):
    """寄信結果不明(逾時,但伺服器可能【已經收下】) → 不得自動重試,以免重複寄出。

    ★[2026-08-06 外審 P1-03]★ 這個類別原本只存在於 consult_query,而且只有
    Outlook 逾時會用；SMTP 逾時走的是普通 `RuntimeError`,於是外層 `_do_full_job`
    把它當成【可重試】→ 同一封 MIME 可能再提交一次 → 收件人收到兩封。
    但兩者的語意完全一樣:timeout 可能發生在伺服器收下 DATA 之後(smtp_mail 自己的
    註解就寫著「信可能已送達,配額不退回」)。現在統一由這裡定義,兩條寄信路徑共用,
    consult_query 直接 import —— isinstance 檢查才會同時涵蓋兩者。

    固定 Message-ID 只可能幫助部分郵件系統收斂顯示,不是 exactly-once 保證,
    所以【不重試】才是唯一安全的選擇。
    """


class SmtpNotConfiguredError(RuntimeError):
    """SMTP 設定不完整（通常是 password 為空）。"""


# [2026-07-26 審查] 本次執行中最後一次【成功讀到】的設定檔內容。
# 用途:設定檔被防毒/備份短暫鎖住時,不要讓「讀不到」被當成「沒設定」而靜默停用所有寄信
# (見 load_credentials 的 "error" 分支)。只放記憶體、不落盤。
_LAST_GOOD_CREDENTIALS: dict = {}


def ensure_credentials_template() -> None:
    """[opt B1] 若 SMTP 設定檔不存在，建立預設範本供使用者填入 App Password。
    只在啟動 / 設定視窗開啟時呼叫一次 —— 與『讀取』分離，避免讀路徑(每 20s 的 IMAP
    poll 也會走 load_credentials)帶寫檔副作用。"""
    try:
        if not CREDENTIALS_FILE.exists():
            atomic_write_json(str(CREDENTIALS_FILE), DEFAULT_CREDENTIALS, indent=2)
            logging.info("已建立 SMTP 設定範本：%s（請填入 App Password 後再寄信）",
                         CREDENTIALS_FILE)
    except Exception:
        logging.warning("建立 SMTP 設定範本失敗（忽略）", exc_info=True)


def load_credentials() -> dict:
    """讀取 SMTP 設定，缺欄位以 default 補。

    [opt B1] 純讀取、無副作用：檔案不存在直接回 default(password 空 → is_configured()
    為 False，會診流程自然靜默跳過)。建立範本改由 ensure_credentials_template() 在啟動時
    呼叫，避免這個被熱路徑(IMAP poll 每 20s)呼叫的函式帶 fsync 寫檔副作用。"""
    cred = dict(DEFAULT_CREDENTIALS)
    try:
        if CREDENTIALS_FILE.exists():
            # [IF-02] credentials 檔【不可】用預設的 backup_on_corrupt=True:官方流程是使用者用記事本
            # 貼 App Password,存成 UTF-8 BOM(BOM 已由 utf-8-sig 容忍)或 ANSI/cp950(from_name 中文)時
            # 會 UnicodeDecodeError → 若照預設把「唯一一份帳密」rename 成 .corrupt 搬走,SMTP 寄信+IMAP
            # 收信會【一次全滅】且診間無人看 log。改 backup_on_corrupt=False:壞檔【原地保留】可救,並
            # 明確 log 告警;讀不到就回 default(password 空 → is_configured() False,流程自然靜默跳過)。
            saved, _status = safe_load_json_ex(
                str(CREDENTIALS_FILE), default={}, backup_on_corrupt=False)
            if _status == "corrupt":
                logging.error(
                    "SMTP 設定檔 %s 內容無法解析(可能存成 ANSI/cp950 或非 JSON);已保留原檔未搬移,"
                    "請用『UTF-8』重新存檔。在修好前寄信/收信會停用。", CREDENTIALS_FILE)
            elif _status == "error":
                # [2026-07-26 審查 ★所有告警一起消失★] 檔案還在、只是【暫時】讀不到
                # (防毒/備份掃描時鎖住)。舊版把 default({}) 當成合法內容 → password 空
                # → is_configured() 回 False → 呼叫端「自然靜默跳過」,止掛提醒/會診通知/
                # 改版通知【全部不寄】而且【一行 log 都沒有】。
                # 這裡沿用上一次成功讀到的帳密:檔案沒變、值一定還是對的,寄信照常;
                # 沒有快取(開機後第一次就讀不到)才退回停用,並且一定要留下 log。
                if _LAST_GOOD_CREDENTIALS:
                    logging.warning(
                        "SMTP 設定檔暫時讀不到(檔案仍在,可能被防毒/備份鎖住)→ "
                        "沿用本次執行中上一次成功讀到的設定,寄信不中斷:%s", CREDENTIALS_FILE)
                    cred.update(_LAST_GOOD_CREDENTIALS)
                else:
                    logging.error(
                        "SMTP 設定檔暫時讀不到且本次執行尚未成功讀過(檔案仍在,可能被防毒/"
                        "備份鎖住)→ 這段期間【所有通知信都不會寄出】:%s", CREDENTIALS_FILE)
            elif isinstance(saved, dict):
                cred.update(saved)
                # [外審] 成功讀到就【無條件】換掉快取,空 dict 也算 —— 舊寫法用 `if saved`
                # 守著,於是使用者刻意把設定清空({})之後,下一次暫時讀取失敗會把
                # 【舊帳密復活】,把已經關掉的寄信又打開。成功讀到 = 這才是現況。
                _LAST_GOOD_CREDENTIALS.clear()
                _LAST_GOOD_CREDENTIALS.update(saved)
    except Exception:
        logging.warning("讀取 SMTP 設定失敗，使用內建預設", exc_info=True)
    # 正規化
    cred["host"] = str(cred.get("host") or DEFAULT_CREDENTIALS["host"]).strip()
    try:
        raw_port = cred.get("port") or DEFAULT_CREDENTIALS["port"]
        if isinstance(raw_port, bool):
            raise ValueError
        cred["port"] = int(raw_port)
        if not 1 <= cred["port"] <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        cred["port"] = DEFAULT_CREDENTIALS["port"]
    cred["username"] = str(cred.get("username") or "").strip()
    cred["password"] = str(cred.get("password") or "")
    cred["use_tls"] = bool(cred.get("use_tls", True))
    cred["from_address"] = (str(cred.get("from_address") or cred["username"]).strip()
                            or cred["username"])
    cred["from_name"] = str(cred.get("from_name") or "").strip()
    return cred


def is_configured() -> bool:
    """SMTP 設定是否齊全可以寄信。"""
    c = load_credentials()
    return bool(c["host"] and c["port"] and c["username"] and c["password"])


def _build_message(sender_address: str, sender_name: str,
                    recipients: list, subject: str, body: str,
                    attachment_path: Optional[Path] = None,
                    html_body: Optional[str] = None,
                    message_id: Optional[str] = None) -> MIMEMultipart:
    """組合 MIME 訊息。圖片附件用 MIMEImage（信箱有預覽），其他用 MIMEApplication。

    html_body 有值時內文走 multipart/alternative：同時帶純文字(fallback)與 HTML，
    不支援 HTML 的客戶端、螢幕閱讀器仍可讀純文字版。截圖附件不受影響照常夾帶
    (外層 multipart/mixed)。"""
    msg = MIMEMultipart()  # 預設 mixed：內文(alt 或 plain) + 截圖附件
    from_header = (f"{sender_name} <{sender_address}>"
                   if sender_name else sender_address)
    msg["From"] = from_header
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    # ★[2026-08-05 外審第 5 輪 P1-04] 呼叫端可以指定 Message-ID★
    #   會診信在寄送失敗時會由呼叫端重試【同一份 payload】。若每次都換一個
    #   Message-ID,SMTP「已收下但回應逾時」的情況會讓收件人收到兩封各自獨立
    #   的信;沿用同一個則多數郵件客戶端會視為同一封而收斂。
    msg["Message-ID"] = message_id or make_msgid(
        domain=sender_address.split("@")[-1])
    if html_body:
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body, "plain", "utf-8"))      # fallback 在前
        alt.attach(MIMEText(html_body, "html", "utf-8"))  # 客戶端優先顯示後者
        msg.attach(alt)
    else:
        msg.attach(MIMEText(body, "plain", "utf-8"))

    if attachment_path and Path(attachment_path).exists():
        p = Path(attachment_path).resolve()
        with open(p, "rb") as f:
            data = f.read()
        ext = p.suffix.lower().lstrip(".")
        if ext in ("png", "jpg", "jpeg", "gif", "bmp"):
            part = MIMEImage(data, _subtype=ext if ext != "jpg" else "jpeg")
        else:
            part = MIMEApplication(data)
        part.add_header("Content-Disposition", "attachment", filename=p.name)
        msg.attach(part)
    return msg


class RcptResultNotDurable(RuntimeError):
    """逐位 RCPT 結果落不了地,而呼叫端要求「落不了地就不要送」。

    ★[外審 2026-08-17 P1-01]★ RCPT 階段伺服器已經逐位回答「A 收、B 拒」,
    但那個事實直到 send 回來、呼叫端寫帳本才變成 durable。中間 crash 的話:
    帳上兩位都還是 UNKNOWN,而信【確實進了寄件備份】(A 收到了)——
    重啟後 Message-ID 回查查到這封信,就把所有 UNKNOWN 判成已送達,
    B 那筆明確的 421 從此消失,不重試也不告警。
    所以逐位結果要在 ★DATA 之前★ 落地;補寄路徑落不了地就不送(拋這個),
    此時內容一個 byte 都還沒送出去 —— 與 MAIL/RCPT 階段同級,可安全重試。
    """


def recipients_refused_map(exc) -> dict:
    """SMTP 例外裡的【逐位拒收碼】。沒有就回空 dict。

    ★[外審 2026-08-17 P2-02 / AE-4 第 1 輪 P2]★ smtplib 只有在【全部】
    收件人被拒時才拋 `SMTPRecipientsRefused`,而那個例外身上就帶著逐位的
    碼(550 查無此人 / 421 暫時忙碌)。把它摺成 generic「失敗」會讓所有
    拒收都被記成暫時性 —— 一個確定不存在的信箱會一路吃完退避與補寄額度,
    最後的告警還用「暫時性拒收」的語氣描述。包在 RuntimeError 裡也找得到
    (`send_mail` 對永久性錯誤會轉包一層)。
    """
    e = exc
    for _ in range(3):
        if isinstance(e, smtplib.SMTPRecipientsRefused):
            return dict(getattr(e, "recipients", {}) or {})
        e = getattr(e, "__cause__", None) or getattr(e, "__context__", None)
        if e is None:
            break
    return {}


def _send_once(cred: dict, msg, timeout: float, on_rcpt_result=None,
               require_durable_rcpt: bool = False) -> dict:
    """單次 SMTP 寄送嘗試 — 失敗會 raise 給 caller 判斷是否重試。

    [2026-07-26 審查 ★假成功★] 回傳 `send_message` 的【被拒收件人 dict】。
    smtplib 只有在【全部】收件人都被拒時才拋 SMTPRecipientsRefused;只有一部分被拒
    (信箱打錯、對方信箱滿)時是【正常返回】並把被拒者放在回傳值裡。舊版把它丟掉,
    於是那些人永遠收不到止掛提醒/會診通知,而 log 寫著「SMTP 已寄出(→ 全部人)」——
    故障與正常完全長得一樣。
    """
    host, port = cred["host"], cred["port"]
    use_tls = cred["use_tls"]

    def _submit(server):
        """把信交出去,而且【知道自己走到哪一步】。

        ★[2026-08-08 外審第 10 輪 P1-03] 上一版把整段都當成「已提交」★
        `server.send_message(msg)` 內部依序做 MAIL FROM → RCPT TO → DATA,
        但它是一個黑盒:例外拋出來時無從得知走到哪一步。上一版對它的【任何】
        例外都蓋上「已提交」的章,於是 MAIL/RCPT 階段的逾時 —— 伺服器【確定】
        還沒收到任何郵件內容 —— 也變成「結果不明」。止掛提醒收到 UNKNOWN 的
        處理是「視為已寄、不重寄」並永久去重,於是那則提醒這輩子都不會寄出。
        (main.py 當時的註解甚至寫著「UNKNOWN 只在 DATA 已提交時才成立」——
         那句話從來就不成立,因為這裡包住的是整個 send_message。)

        改用 smtplib 的低階流程,ambiguous 的邊界就明確落在 `data()` 上,
        而且逐位 RCPT 的拒收資訊也保留得下來(部分拒收補寄要靠它)。

        ★[外審 2026-08-12 P1-04] DATA 也拆開,不再當黑箱★
        `server.data()` 內部是「送 DATA 指令 → 等 354 → 送內容 → 等 250」。
        等 354 時逾時,伺服器其實【一個 byte 的內容都還沒收到】,但它拋的是
        socket.timeout(不是 SMTPDataError)—— 舊版把它蓋上「已提交」的章,
        一封確定可以安全重試的信就被冤成 UNKNOWN(不重試、等回查)。
        現在自己拆:354 之前的任何失敗=可重試;內容開始送出之後才是
        「已提交」的範圍。
        """
        from email import utils as _eutils  # noqa: PLC0415
        from email.generator import BytesGenerator  # noqa: PLC0415
        import io as _io  # noqa: PLC0415

        from_addr = _eutils.parseaddr(msg["From"] or "")[1]
        to_addrs = [a for _n, a in
                    _eutils.getaddresses(msg.get_all("To", [])) if a]
        if not from_addr or not to_addrs:
            raise RuntimeError("信件缺少寄件者或收件人,拒絕送出")
        buf = _io.BytesIO()
        BytesGenerator(buf).flatten(msg, linesep="\r\n")
        payload = buf.getvalue()

        # ── 階段一:MAIL / RCPT ── 伺服器確定還沒收到郵件內容,失敗可安全重試
        server.ehlo_or_helo_if_needed()
        code, resp = server.mail(from_addr, [])
        if code != 250:
            try:
                server.rset()
            except Exception:
                pass
            raise smtplib.SMTPSenderRefused(code, resp, from_addr)
        refused = {}
        for addr in to_addrs:
            code, resp = server.rcpt(addr, [])
            if code not in (250, 251):
                refused[addr] = (code, resp)
        # ── ★逐位結果先落地,再進 DATA★(外審 2026-08-17 P1-01)──
        #   到這一行為止,伺服器已經逐位回答完畢,而信【還沒送出去】。
        #   呼叫端在這裡把「誰收了、誰被拒」寫成 durable 事實(COMMIT+fsync);
        #   之後就算 DATA 成功後立刻斷電,重啟時 Message-ID 回查也不會把
        #   被明確拒收的那位一起判成已送達。
        #   落不了地時:補寄路徑(require_durable_rcpt=True)★不送★ ——
        #   內容還沒送出,可安全重試;初次臨床通知則照送(availability-first
        #   的既有政策),呼叫端會另外把拒收存進跨 process 寄存處。
        if on_rcpt_result is not None:
            accepted = [a for a in to_addrs if a not in refused]
            try:
                durable = bool(on_rcpt_result(accepted=list(accepted),
                                              refused=dict(refused)))
            except Exception:
                logging.error("[mail] 逐位 RCPT 結果落地時拋例外", exc_info=True)
                durable = False
            if not durable and require_durable_rcpt:
                try:
                    server.rset()
                except Exception:
                    pass
                raise RcptResultNotDurable(
                    "逐位 RCPT 結果落不了地 → 這一次不送(內容尚未送出,"
                    "可安全重試)")

        # ★全部被拒的判斷要在 callback【之後】★(外審 AE-4 第 1 輪 P2):
        #   放在前面的話,「唯一收件人回 550」這條最重要的路完全不會呼叫
        #   callback —— 逐位的碼丟掉(被上層記成暫時失敗、繼續追打一個
        #   不存在的信箱),而且這次確實跨過了 RCPT 卻沒有劃下嘗試邊界
        #   (attempts=0 → 額度繞過)。RCPT 全部回答完就是同一個事實,
        #   不管接受幾個。
        if len(refused) == len(to_addrs):
            try:
                server.rset()
            except Exception:
                pass
            raise smtplib.SMTPRecipientsRefused(refused)

        # ── 階段二:DATA,拆成三個網路階段(外審 2026-08-12 P1-04)──
        #   ②a 送 "DATA" 指令、等 354 —— 這一段的任何失敗(含逾時),
        #      內容【一個 byte 都還沒送】,與 MAIL/RCPT 同級,可安全重試。
        #      ★舊版在這裡逾時會被蓋上「已提交」★:socket.timeout 不是
        #      SMTPDataError,黑箱分不出它發生在 354 之前還是之後。
        #   ②b 送內容、等最終回應 —— 從內容開始送出的那一刻起,
        #      任何失敗都是真的結果不明(SUBMITTED_ATTR)。
        #   ②c 最終回應不是 250 → 伺服器明確拒絕(確定失敗,不是 UNKNOWN)。
        code, resp = server.docmd("data")
        if code != 354:
            # 與 smtplib.data() 的 ① 同形狀:內容還沒送出 → 可重試。
            raise smtplib.SMTPDataError(code, resp)
        try:
            server.send(_dot_stuff(payload))
            code, resp = server.getreply()
        except BaseException as e:
            setattr(e, SUBMITTED_ATTR, True)   # ②b 只有這一段是結果不明
            raise
        if code != 250:
            # ②c 明確被拒。與 smtplib.sendmail 一致:421 就關掉,其餘 rset。
            #   ★上上一版把這個回傳值丟掉★ —— 伺服器明確拒絕(554/451),
            #   我們卻回報成功、還把已通知基準往前推。
            try:
                if code == 421:
                    server.close()
                else:
                    server.rset()
            except Exception:
                pass
            raise smtplib.SMTPDataError(code, resp)
        return refused

    if port == 465:
        # 純 SSL（少數人用）
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, timeout=timeout,
                               context=context) as server:
            server.login(cred["username"], cred["password"])
            return _submit(server)
    else:
        # 587 STARTTLS（Gmail 推薦）或 25 明文（不建議）
        with smtplib.SMTP(host, port, timeout=timeout) as server:
            server.ehlo()
            if use_tls:
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            # [IF-08 2026-07-12] 無 TLS 且非 loopback → 拒絕明文送 App Password(帳密明文過網)。
            # 此分支 port≠465;正常設定為 587+STARTTLS(use_tls=True)不受影響。loopback 判斷涵蓋
            # localhost/LOCALHOST/localhost./127.0.0.0-8/::1(本機 relay 明文可接受)。
            if not use_tls and not _is_loopback_host(host):
                raise RuntimeError(
                    "拒絕在無 TLS 下對非本機 SMTP 傳送帳密(use_tls=False);"
                    "請改用 587+STARTTLS 或 465 SSL。")
            server.login(cred["username"], cred["password"])
            return _submit(server)


def _is_loopback_host(host) -> bool:
    """host 是否為本機 loopback(無 TLS 明文送帳密僅在此可接受)。涵蓋 localhost/大小寫/
    結尾點/127.0.0.0-8/::1。"""
    h = str(host or "").strip().rstrip(".").lower()
    if h == "localhost":
        return True
    try:
        import ipaddress
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def _smtp_error_is_permanent(e) -> bool:
    """SMTP 例外是否為永久性(全部相關 response code 皆 5xx)。含任何 4xx、或取不到碼、或非
    SMTP 錯誤(逾時/OSError)一律回 False(視為可重試),以免把 greylisting/暫時性失敗的信丟掉。"""
    codes = []
    if isinstance(e, smtplib.SMTPRecipientsRefused):
        codes = [c for (c, _m) in (getattr(e, "recipients", {}) or {}).values()]
    elif isinstance(e, (smtplib.SMTPSenderRefused, smtplib.SMTPDataError,
                        smtplib.SMTPResponseException)):
        code = getattr(e, "smtp_code", None)
        if code is not None:
            codes = [code]
    if not codes:
        return False
    try:
        return all(500 <= int(c) < 600 for c in codes)
    except (TypeError, ValueError):
        return False


def send_mail(recipients: list, subject: str, body: str,
              attachment_path: Optional[Path] = None,
              timeout: float = 60.0,
              override_credentials: Optional[dict] = None,
              max_retries: int = DEFAULT_MAX_RETRIES,
              html_body: Optional[str] = None,
              category: str = CATEGORY_CLINICAL,
              message_id: Optional[str] = None,
              on_rcpt_result=None,
              require_durable_rcpt: bool = False) -> dict:
    """同步寄一封信。失敗 raise；成功 log info。

    on_rcpt_result: `f(accepted: list, refused: dict) -> bool`,在【RCPT 全部
      回答完、DATA 之前】被呼叫(外審 2026-08-17 P1-01)。呼叫端在這裡把
      逐位結果寫成 durable 事實;回 False(或拋例外)代表沒落地。
      ★每一次 SMTP 嘗試都會呼叫一次★(重試時會再叫),所以它必須冪等。
    require_durable_rcpt: 落不了地就【不要送】(拋 `RcptResultNotDurable`;
      內容尚未送出,可安全重試)。補寄路徑用 True(正確性優先);
      初次臨床通知用 False(availability-first,既有政策)。

    回傳【被拒收件人】的 dict(空 dict = 全部送達)。[2026-07-26 審查]
    smtplib 只在【全部】收件人被拒時才拋例外,部分被拒是正常返回 —— 呼叫端若要
    嚴格處理(例如標記某位收件人長期收不到),看這個回傳值。

    recipients: list of "x@y.z"
    attachment_path: None 或 Path（會自動判斷 image / generic）
    override_credentials: 測試用，覆蓋 settings/smtp_credentials.json
    max_retries: 暫時性錯誤 (timeout / 網路) 最多重試次數 (預設 2 → 共最多
                  跑 3 次)。認證錯誤這類「不會自己好」的不會重試。

    Retry strategy：exponential backoff 2s → 4s → 8s → 10s (上限)。

    category: 寄信配額類別（見 cmuh_common/mail_quota.py）。
      `CATEGORY_CLINICAL`（預設）＝關於病人的信（止掛提醒、會診結果、回讀不符），
      可用到帳號總額；`CATEGORY_SYSTEM` ＝關於程式的信（故障告警、健康檢查、
      改版偵測、重複觸發提醒、測試信），額度較小，因此系統類就算陷入迴圈狂寄，
      臨床告警仍有保留名額。
      ★預設值刻意是 clinical★：漏標一個系統類呼叫端，後果是「系統信吃到臨床的
      額度」（跟修好之前一樣）；反之若預設 system，漏標一個臨床呼叫端就會讓
      【臨床告警在保留名額還空著的情況下被拒寄】—— 那是更糟的失敗方向。
    """
    if not recipients:
        raise RuntimeError("沒有設定收件人")
    cred = override_credentials or load_credentials()
    if not cred["password"]:
        raise SmtpNotConfiguredError(
            f"SMTP password 未設定。請編輯 {CREDENTIALS_FILE} 填入 Gmail App "
            "Password（16 字元）。取得方式：登入 cmuhdermatology@gmail.com → "
            "https://myaccount.google.com/apppasswords")
    if not cred["host"] or not cred["username"]:
        raise SmtpNotConfiguredError(
            f"SMTP host/username 未設定。請編輯 {CREDENTIALS_FILE}")

    msg = _build_message(
        sender_address=cred["from_address"],
        sender_name=cred["from_name"],
        recipients=recipients,
        subject=subject, body=body,
        attachment_path=attachment_path,
        html_body=html_body,
        message_id=message_id,
    )
    max_retries = _normalize_max_retries(max_retries)
    reservation = _reserve_rate_limit_slot(cred, category)

    import time as _time
    refused: dict = {}
    for attempt in range(max_retries + 1):
        try:
            refused = _send_once(cred, msg, timeout,
                                 on_rcpt_result=on_rcpt_result,
                                 require_durable_rcpt=require_durable_rcpt
                                 ) or {}
            if attempt > 0:
                logging.info("SMTP 第 %d 次重試成功", attempt)
            break  # success
        except RcptResultNotDurable:
            # ★落不了地就不送★ 內容一個 byte 都還沒送出去(確定沒寄出)。
            #   立刻重試也不會讓帳本變得可用 → 交回呼叫端排到下一輪。
            _rollback_rate_limit_slot(reservation)
            raise
        except smtplib.SMTPAuthenticationError as e:
            # 認證錯不會自己好 → 不重試
            _rollback_rate_limit_slot(reservation)
            raise RuntimeError(
                f"SMTP 認證失敗：{e}。\n"
                f"請確認 settings/smtp_credentials.json 的 password 是 Gmail "
                f"App Password（16 字元），不是您日常登入的密碼。") from e
        except (socket.timeout, smtplib.SMTPException, OSError) as e:
            # [IF-07 2026-07-12] 永久性 SMTP 錯誤(全部相關 response code 皆 5xx:收件人/寄件人被
            # 拒、DATA 5xx)不會自己好 → 不重試,免徒勞 backoff。但同類例外也可能帶 4xx(暫時,如
            # greylisting/信箱暫時滿)→ 那些仍走下面重試,以免把可恢復的信丟掉(codex)。
            if _smtp_error_is_permanent(e):
                _rollback_rate_limit_slot(reservation)
                raise RuntimeError(
                    f"SMTP 永久性錯誤(5xx),不重試：{type(e).__name__}: {e}") from e
            # ★[2026-08-06 外審] 逾時要在【重試判斷之前】就分流★
            #   上一版把 DeliveryOutcomeUnknown 放在「用完重試次數」之後,於是
            #   max_retries=2 時是:逾時→重送→逾時→重送→逾時→才說結果不明。
            #   若逾時發生在伺服器已收下 DATA 之後,那就是【送出三封】才承認不明。
            #   會診端有傳 max_retries=0 所以沒事,但止掛提醒走預設值 2 —— 正是
            #   會重複寄的那條路。逾時＝結果不明,第一次發生就不可以再送。
            # ★[2026-08-07 外審 P1-02] 但只有【已提交之後】的斷線才算不明★
            #   連線/STARTTLS/登入階段的逾時,伺服器確定還沒收到任何郵件內容,
            #   那是最常見的暫時性故障,必須照常重試(上一版一律當 UNKNOWN,
            #   把「連不上」也變成不重試)。階段由 _send_once 標在例外身上。
            if isinstance(e, socket.timeout) and not getattr(
                    e, SUBMITTED_ATTR, False):
                if attempt < max_retries:
                    backoff = min(10, 2 * (2 ** attempt))
                    logging.warning(
                        "SMTP 連線階段逾時(尚未送出任何內容,可安全重試) "
                        "第 %d 次,%.0fs 後重試…", attempt + 1, backoff)
                    _time.sleep(backoff)
                    continue
                _rollback_rate_limit_slot(reservation)
                raise RuntimeError(
                    f"SMTP 連線逾時 ({int(timeout)}s)，已重試 {max_retries} 次"
                    f"(尚未送出郵件內容,確定沒有寄出)：{e}") from e
            if isinstance(e, socket.timeout):
                raise DeliveryOutcomeUnknown(
                    f"SMTP 連線/送信逾時 ({int(timeout)}s) —— 結果不明,伺服器"
                    f"可能已收下;不重試以免重複寄出(配額不退回)：{e}") from e
            # ★[2026-08-08 外審第 10 輪 P1-02] 不只逾時會落在 DATA 之後★
            #   上一版只在 `isinstance(e, socket.timeout)` 那條分支裡看這個標記。
            #   於是 DATA 送出後的 `SMTPServerDisconnected` / `ConnectionResetError`
            #   (伺服器已收下、只是連線斷了)掉進下面的一般重試 —— 醫師會收到
            #   兩封同樣的臨床通知。標記代表的是「走到哪一步」,與例外的型別無關。
            if getattr(e, SUBMITTED_ATTR, False):
                raise DeliveryOutcomeUnknown(
                    f"SMTP 已送出郵件內容,但沒有收到最終回應就中斷 —— 結果不明,"
                    f"伺服器可能已收下;不重試以免重複寄出(配額不退回):"
                    f"{type(e).__name__}: {e}") from e
            if attempt < max_retries:
                backoff = min(10, 2 * (2 ** attempt))  # 2s, 4s, 8s, 10s (capped)
                logging.warning(
                    "SMTP 第 %d 次嘗試失敗 (%s: %s)，%.0fs 後重試…",
                    attempt + 1, type(e).__name__, e, backoff)
                _time.sleep(backoff)
                continue
            # 用完重試次數（逾時已在上面提前分流，不會走到這裡）
            # ★[2026-08-05 外審第 6 輪 P2-03] 逾時不退配額★ —— 見上方分流處。
            _rollback_rate_limit_slot(reservation)
            if isinstance(e, OSError):
                raise RuntimeError(
                    f"SMTP 網路錯誤，已重試 {max_retries} 次：{e}") from e
            raise RuntimeError(
                f"SMTP 寄信失敗，已重試 {max_retries} 次：{type(e).__name__}: {e}") from e
        except Exception:
            _rollback_rate_limit_slot(reservation)
            raise

    # [2026-07-26 審查 ★假成功★] 部分收件人被拒時 smtplib 是【正常返回】的,
    # 舊版直接印「已寄出 → 全部人」。實際上那些人一封都沒收到,而這些信正是
    # 止掛提醒/會診通知/改版通知 —— 收不到就等於那個人的告警永久失效,且無跡可循。
    if refused:
        _bad = sorted(str(r) for r in refused)
        _ok = [r for r in recipients if r not in refused]
        logging.error(
            "SMTP 部分收件人被拒(這些人【沒有】收到信):%s;實際送達:%s。主旨:%s。"
            "被拒原因:%s",
            ", ".join(_bad), ", ".join(_ok) or "(無)", subject, refused)
        logging.info("SMTP 已寄出（%s → %s）：%s",
                     cred["from_address"], ", ".join(_ok) or "(無)", subject)
    else:
        logging.info("SMTP 已寄出（%s → %s）：%s",
                     cred["from_address"], ", ".join(recipients), subject)
    # 回傳被拒清單(空 dict = 全部送達)。刻意【不 raise】:已送達的人不該因為重試而收到
    # 重複的信;呼叫端要更嚴格處理可以看這個回傳值。
    return refused
