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

import json
import logging
import os
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

CREDENTIALS_FILE = Path(get_settings_dir()) / "smtp_credentials.json"

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
            with open(CREDENTIALS_FILE, "r", encoding="utf-8") as f:
                saved = json.load(f)
            if isinstance(saved, dict):
                cred.update(saved)
        else:
            # 建範本檔，使用者編輯填入 password
            os.makedirs(CREDENTIALS_FILE.parent, exist_ok=True)
            with open(CREDENTIALS_FILE, "w", encoding="utf-8") as f:
                json.dump(DEFAULT_CREDENTIALS, f, ensure_ascii=False, indent=2)
            logging.info("已建立 SMTP 設定範本：%s（請填入 App Password 後再寄信）",
                         CREDENTIALS_FILE)
    except Exception:
        logging.warning("讀取 SMTP 設定失敗，使用內建預設", exc_info=True)
    # 正規化
    cred["host"] = str(cred.get("host") or DEFAULT_CREDENTIALS["host"]).strip()
    try:
        cred["port"] = int(cred.get("port") or DEFAULT_CREDENTIALS["port"])
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


def send_mail(recipients: list, subject: str, body: str,
              attachment_path: Optional[Path] = None,
              timeout: float = 60.0,
              override_credentials: Optional[dict] = None) -> None:
    """同步寄一封信。失敗 raise；成功 log info。

    recipients: list of "x@y.z"
    attachment_path: None 或 Path（會自動判斷 image / generic）
    override_credentials: 測試用，覆蓋 settings/smtp_credentials.json
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

    # 連線 + 認證 + 寄送
    host, port = cred["host"], cred["port"]
    use_tls = cred["use_tls"]
    try:
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
    except smtplib.SMTPAuthenticationError as e:
        raise RuntimeError(
            f"SMTP 認證失敗：{e}。\n"
            f"請確認 settings/smtp_credentials.json 的 password 是 Gmail "
            f"App Password（16 字元），不是您日常登入的密碼。") from e
    except smtplib.SMTPException as e:
        raise RuntimeError(f"SMTP 寄信失敗：{type(e).__name__}: {e}") from e
    except socket.timeout as e:
        raise RuntimeError(
            f"SMTP 連線/送信逾時（{int(timeout)}s）：{e}") from e
    except OSError as e:
        raise RuntimeError(f"SMTP 網路錯誤：{e}") from e

    logging.info("SMTP 已寄出（%s → %s）：%s",
                 cred["from_address"], ", ".join(recipients), subject)
