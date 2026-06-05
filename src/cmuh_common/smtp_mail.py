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
import smtplib
import socket
import ssl
import time
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid
from pathlib import Path
from typing import Optional

from cmuh_common.paths import get_settings_dir
from cmuh_common.atomic_io import atomic_write_json, safe_load_json

CREDENTIALS_FILE = Path(get_settings_dir()) / "smtp_credentials.json"

# [C] Rate limit：保護機制防 bug 觸發無窮迴圈狂寄信
# 用 deque 追蹤過去 60 分鐘內每封信的時間戳；超過 RATE_LIMIT_MAX 就拒絕
import collections as _collections
import threading as _threading
RATE_LIMIT_WINDOW_SEC = 3600   # 統計區間 1 小時
RATE_LIMIT_MAX = 30            # 1 小時內最多 30 封
DEFAULT_MAX_RETRIES = 2
MAX_RETRIES = 5
_rate_limit_lock = _threading.Lock()
_recent_send_reservations: "_collections.deque" = _collections.deque(
    maxlen=RATE_LIMIT_MAX * 4)


class SmtpRateLimitExceeded(RuntimeError):
    """寄信頻率超過 RATE_LIMIT_MAX/小時的保護性錯誤。"""


def _normalize_max_retries(value) -> int:
    """Clamp retry counts so bad config cannot skip sending or retry forever."""
    if isinstance(value, bool):
        return DEFAULT_MAX_RETRIES
    try:
        retries = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_RETRIES
    return max(0, min(MAX_RETRIES, retries))


def _reserve_rate_limit_slot() -> tuple[float, object]:
    """Reserve one logical send slot. Roll it back if delivery fails."""
    now = time.time()
    cutoff = now - RATE_LIMIT_WINDOW_SEC
    with _rate_limit_lock:
        # 清掉視窗外的舊紀錄
        while (_recent_send_reservations
               and _recent_send_reservations[0][0] < cutoff):
            _recent_send_reservations.popleft()
        if len(_recent_send_reservations) >= RATE_LIMIT_MAX:
            oldest_ago = now - _recent_send_reservations[0][0]
            raise SmtpRateLimitExceeded(
                f"SMTP rate limit：過去 {RATE_LIMIT_WINDOW_SEC // 60} 分鐘已寄 "
                f"{len(_recent_send_reservations)} 封 (上限 {RATE_LIMIT_MAX})，"
                f"請 {int((RATE_LIMIT_WINDOW_SEC - oldest_ago) // 60)} 分鐘後再試"
            )
        reservation = (now, object())
        _recent_send_reservations.append(reservation)
        return reservation


def _rollback_rate_limit_slot(reservation: tuple[float, object]) -> None:
    with _rate_limit_lock:
        try:
            _recent_send_reservations.remove(reservation)
        except ValueError:
            pass

DEFAULT_CREDENTIALS = {
    "host": "smtp.gmail.com",
    "port": 587,
    "username": "cmuhdermatology@gmail.com",
    "password": "",  # 必須由使用者填入 App Password（16 字元）
    "use_tls": True,
    "from_address": "cmuhdermatology@gmail.com",
    "from_name": "中國醫皮膚科系統",
}


class SmtpNotConfiguredError(RuntimeError):
    """SMTP 設定不完整（通常是 password 為空）。"""


def load_credentials() -> dict:
    """讀取 SMTP 設定，缺欄位以 default 補。檔案不存在則建立預設範本。"""
    cred = dict(DEFAULT_CREDENTIALS)
    try:
        if CREDENTIALS_FILE.exists():
            saved = safe_load_json(str(CREDENTIALS_FILE), default={})
            if isinstance(saved, dict):
                cred.update(saved)
        else:
            # 建範本檔，使用者編輯填入 password
            atomic_write_json(str(CREDENTIALS_FILE), DEFAULT_CREDENTIALS, indent=2)
            logging.info("已建立 SMTP 設定範本：%s（請填入 App Password 後再寄信）",
                         CREDENTIALS_FILE)
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
                    attachment_path: Optional[Path] = None) -> MIMEMultipart:
    """組合 MIME 訊息。圖片附件用 MIMEImage（信箱有預覽），其他用 MIMEApplication。"""
    msg = MIMEMultipart()
    from_header = (f"{sender_name} <{sender_address}>"
                   if sender_name else sender_address)
    msg["From"] = from_header
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=sender_address.split("@")[-1])
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


def _send_once(cred: dict, msg, timeout: float) -> None:
    """單次 SMTP 寄送嘗試 — 失敗會 raise 給 caller 判斷是否重試。"""
    host, port = cred["host"], cred["port"]
    use_tls = cred["use_tls"]
    if port == 465:
        # 純 SSL（少數人用）
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(host, port, timeout=timeout,
                               context=context) as server:
            server.login(cred["username"], cred["password"])
            server.send_message(msg)
    else:
        # 587 STARTTLS（Gmail 推薦）或 25 明文（不建議）
        with smtplib.SMTP(host, port, timeout=timeout) as server:
            server.ehlo()
            if use_tls:
                server.starttls(context=ssl.create_default_context())
                server.ehlo()
            server.login(cred["username"], cred["password"])
            server.send_message(msg)


def send_mail(recipients: list, subject: str, body: str,
              attachment_path: Optional[Path] = None,
              timeout: float = 60.0,
              override_credentials: Optional[dict] = None,
              max_retries: int = DEFAULT_MAX_RETRIES) -> None:
    """同步寄一封信。失敗 raise；成功 log info。

    recipients: list of "x@y.z"
    attachment_path: None 或 Path（會自動判斷 image / generic）
    override_credentials: 測試用，覆蓋 settings/smtp_credentials.json
    max_retries: 暫時性錯誤 (timeout / 網路) 最多重試次數 (預設 2 → 共最多
                  跑 3 次)。認證錯誤這類「不會自己好」的不會重試。

    Retry strategy：exponential backoff 2s → 4s → 8s → 10s (上限)。
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
    )
    max_retries = _normalize_max_retries(max_retries)
    reservation = _reserve_rate_limit_slot()

    import time as _time
    for attempt in range(max_retries + 1):
        try:
            _send_once(cred, msg, timeout)
            if attempt > 0:
                logging.info("SMTP 第 %d 次重試成功", attempt)
            break  # success
        except smtplib.SMTPAuthenticationError as e:
            # 認證錯不會自己好 → 不重試
            _rollback_rate_limit_slot(reservation)
            raise RuntimeError(
                f"SMTP 認證失敗：{e}。\n"
                f"請確認 settings/smtp_credentials.json 的 password 是 Gmail "
                f"App Password（16 字元），不是您日常登入的密碼。") from e
        except (socket.timeout, smtplib.SMTPException, OSError) as e:
            if attempt < max_retries:
                backoff = min(10, 2 * (2 ** attempt))  # 2s, 4s, 8s, 10s (capped)
                logging.warning(
                    "SMTP 第 %d 次嘗試失敗 (%s: %s)，%.0fs 後重試…",
                    attempt + 1, type(e).__name__, e, backoff)
                _time.sleep(backoff)
                continue
            # 用完重試次數
            _rollback_rate_limit_slot(reservation)
            if isinstance(e, socket.timeout):
                raise RuntimeError(
                    f"SMTP 連線/送信逾時 ({int(timeout)}s)，已重試 {max_retries} 次：{e}") from e
            if isinstance(e, OSError):
                raise RuntimeError(
                    f"SMTP 網路錯誤，已重試 {max_retries} 次：{e}") from e
            raise RuntimeError(
                f"SMTP 寄信失敗，已重試 {max_retries} 次：{type(e).__name__}: {e}") from e
        except Exception:
            _rollback_rate_limit_slot(reservation)
            raise

    logging.info("SMTP 已寄出（%s → %s）：%s",
                 cred["from_address"], ", ".join(recipients), subject)
