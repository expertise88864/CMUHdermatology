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

import imaplib
import logging
import socket
import ssl
from typing import Optional

from cmuh_common.smtp_mail import load_credentials

DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993


def _load_imap_settings() -> dict:
    """從 smtp_credentials.json 取出 IMAP 需要的欄位。"""
    c = load_credentials()
    host = str(c.get("imap_host") or DEFAULT_IMAP_HOST).strip()
    try:
        port = int(c.get("imap_port") or DEFAULT_IMAP_PORT)
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


def check_trigger(keyword: str, mark_read: bool = True,
                   timeout: float = 30.0,
                   sample_count: int = 3) -> dict:
    """掃描 IMAP 收件匣未讀信，主旨含 keyword 的就回報、抓 From 地址、並標為已讀。

    回傳 dict：
      triggered (bool)：有比對到至少一封 → True
      scanned (int)：本次掃了多少封未讀
      matched (int)：主旨含 keyword 的未讀數
      matched_senders (list[str])：比對到的信件 From 地址（去重小寫，可能空）。
                       呼叫端可用來判斷「誰觸發的」並把結果回寄給他。
      samples (list[str])：若 matched=0，回 sample_count 個最近未讀主旨給 debug
      error (str|None)：例外訊息（連線/認證失敗等），有錯時其他欄位無意義

    side effect：matched > 0 時把那些信標為 Read（\Seen flag），避免重複觸發。
    """
    result = {
        "triggered": False,
        "scanned": 0,
        "matched": 0,
        "matched_senders": [],
        "samples": [],
        "error": None,
    }
    if not keyword:
        result["error"] = "keyword 為空"
        return result

    s = _load_imap_settings()
    if not s["password"]:
        result["error"] = ("SMTP/IMAP password 未設定（編輯 "
                            "settings/smtp_credentials.json）")
        return result

    socket.setdefaulttimeout(timeout)
    conn: Optional[imaplib.IMAP4_SSL] = None
    try:
        context = ssl.create_default_context()
        conn = imaplib.IMAP4_SSL(s["host"], s["port"], ssl_context=context,
                                  timeout=timeout)
        conn.login(s["username"], s["password"])
        conn.select("INBOX")

        # 用 IMAP SEARCH 直接過濾「未讀 + 主旨含 keyword」，避免拉全部
        # 注意：IMAP SEARCH 對非 ASCII 主旨要用 LITERAL+CHARSET UTF-8
        # imaplib 支援：search(charset, *criteria)
        try:
            kw_bytes = keyword.encode("utf-8")
            # Gmail/Dovecot 都支援 CHARSET UTF-8
            typ, data = conn.search(None, "UNSEEN", "SUBJECT",
                                     f'"{keyword}"')
            # 如果上面失敗（不支援 inline 中文），改用 utf-8 mode
            if typ != "OK":
                typ, data = conn.search("UTF-8", "UNSEEN", "SUBJECT", kw_bytes)
        except Exception:
            # 後備：撈 UNSEEN 後 client 端比對
            typ, data = conn.search(None, "UNSEEN")
            if typ != "OK":
                raise RuntimeError(f"IMAP SEARCH 失敗：{typ} {data}")

        if typ != "OK" or not data:
            result["error"] = f"IMAP SEARCH 異常回應：{typ}"
            return result

        ids = data[0].split() if data[0] else []
        result["scanned"] = len(ids)

        from email.utils import parseaddr

        matched_ids = []
        senders_seen = set()
        for uid in ids:
            try:
                typ, fetch = conn.fetch(uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM)])")
                if typ != "OK" or not fetch:
                    continue
                # fetch 結構：[(b'1 (BODY...', b'Subject: ...\r\nFrom: ...\r\n'), b')']
                header_raw = b""
                for part in fetch:
                    if isinstance(part, tuple) and len(part) >= 2:
                        header_raw = part[1]
                        break
                subj_str = ""
                from_str = ""
                if header_raw:
                    for line in header_raw.splitlines():
                        low = line.lower()
                        if low.startswith(b"subject:"):
                            subj_str = _decode_subject(
                                line.split(b":", 1)[1].strip())
                        elif low.startswith(b"from:"):
                            from_str = _decode_subject(
                                line.split(b":", 1)[1].strip())
                if keyword in subj_str:
                    matched_ids.append(uid)
                    # parseaddr 解 "Name <foo@bar.com>" → ("Name", "foo@bar.com")
                    _, addr = parseaddr(from_str)
                    addr = (addr or "").strip().lower()
                    if addr and addr not in senders_seen:
                        senders_seen.add(addr)
                        result["matched_senders"].append(addr)
                elif len(result["samples"]) < sample_count:
                    result["samples"].append(subj_str or "(空主旨)")
            except Exception:
                logging.debug("IMAP fetch 單筆失敗（忽略）", exc_info=True)
                continue

        result["matched"] = len(matched_ids)
        result["triggered"] = result["matched"] > 0

        if matched_ids and mark_read:
            # 一次標記多封為已讀
            try:
                id_list = b",".join(matched_ids).decode("ascii")
                conn.store(id_list, "+FLAGS", "(\\Seen)")
            except Exception:
                logging.warning("標已讀失敗（不影響觸發）", exc_info=True)

        try:
            conn.close()
        except Exception:
            pass

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
        try:
            if conn is not None:
                conn.logout()
        except Exception:
            pass
        socket.setdefaulttimeout(None)

    return result
