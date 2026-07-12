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
import threading
import time
from typing import Optional

from cmuh_common.smtp_mail import load_credentials

DEFAULT_IMAP_HOST = "imap.gmail.com"
DEFAULT_IMAP_PORT = 993

# ─── Watchdog 支援：暴露當前活動的 IMAP 連線給外部 force-close ────────────
# 用途：如果 check_trigger 在 socket 上卡住 > N 秒，呼叫端可從另一個 thread
# 呼叫 force_close_active() 強制砍 socket，讓卡住的 thread 立刻 unblock。
_active_conn_lock = threading.Lock()
_active_conns: set[imaplib.IMAP4_SSL] = set()


def _set_active(conn: Optional[imaplib.IMAP4_SSL]) -> None:
    if conn is None:
        return
    with _active_conn_lock:
        _active_conns.add(conn)


def _clear_active(conn: Optional[imaplib.IMAP4_SSL]) -> None:
    if conn is None:
        return
    with _active_conn_lock:
        _active_conns.discard(conn)


def force_close_active(clear: bool = False) -> bool:
    """從另一個 thread 緊急砍掉目前活動的 IMAP socket，讓 hang 的 recv 立即拋例外。
    回傳 True 表示有試著關（不保證 socket 確實已斷）；False 表示沒有 active 連線。

    [opt B2] clear=True：關閉後一併把這些 conn 從 _active_conns 移除。供「worker thread
    被放生、永遠走不到 finally 的 _clear_active」的逾時路徑使用，避免已死連線物件永久留在
    set 內(socket/fd 已由上面 _force_close_conn 釋放，但 Python 物件仍被 set 強引用無法 GC)。
    預設 False 維持原語意(force_close 不負責 discard)。
    注意：目前為單連線設計(consult_query single-flight)，clear=True 等同清掉當下唯一那條；
    若未來改成多連線並發，必須改成只清「逾時的那一條」而非全部，否則會誤清仍在用的健康連線。"""
    with _active_conn_lock:
        conns = list(_active_conns)
    if not conns:
        return False
    for conn in conns:
        _force_close_conn(conn)
    if clear:
        with _active_conn_lock:
            for conn in conns:
                _active_conns.discard(conn)
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
        typ, fetch = conn.fetch(uid, "(INTERNALDATE)")
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


def check_trigger(keyword: str, mark_read: bool = True,
                   timeout: float = 30.0,
                   sample_count: int = 3,
                   max_age_sec: Optional[float] = None) -> dict:
    """掃描 IMAP 收件匣未讀信，主旨含 keyword 的就回報、抓 From 地址、並標為已讀。

    回傳 dict：
      triggered (bool)：有比對到至少一封 → True
      scanned (int)：本次掃了多少封未讀
      matched (int)：主旨含 keyword 的未讀數
      matched_senders (list[str])：比對到的信件 From 地址（去重小寫，可能空）。
                       呼叫端可用來判斷「誰觸發的」並把結果回寄給他。
      samples (list[str])：若 matched=0，回 sample_count 個最近未讀主旨給 debug
      error (str|None)：例外訊息（連線/認證失敗等），有錯時其他欄位無意義

    side effect：matched > 0 時把那些信標為 Read（\\Seen flag），避免重複觸發。
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

    # 【穩定性 2026.05.20】不用 socket.setdefaulttimeout — 那是 process-global，
    # 會污染同 process 的 SMTP / selenium / requests。IMAP4_SSL(timeout=...) 已夠。
    conn: Optional[imaplib.IMAP4_SSL] = None
    try:
        context = ssl.create_default_context()
        conn = imaplib.IMAP4_SSL(s["host"], s["port"], ssl_context=context,
                                  timeout=timeout)
        _set_active(conn)
        conn.login(s["username"], s["password"])
        conn.select("INBOX")

        # 用 IMAP SEARCH 直接過濾「未讀 + 主旨含 keyword」，避免拉全部
        # 注意：IMAP SEARCH 對非 ASCII 主旨要用 LITERAL+CHARSET UTF-8
        # imaplib 支援：search(charset, *criteria)
        try:
            # ASCII 主旨 → server-side SEARCH(高效);中文主旨會在 imaplib ASCII 編碼階段先拋
            # UnicodeEncodeError → 落 except 後備「全 UNSEEN client 端比對」。
            # [IF-05 2026-07-12] 移除原「typ!=OK 改 UTF-8 mode」死碼:中文走的是【例外】路徑而非
            # typ!=OK,該 UTF-8 retry 永不執行(kw_bytes 一併移除)。
            typ, data = conn.search(None, "UNSEEN", "SUBJECT",
                                     f'"{keyword}"')
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
        stale_ids = []  # [會診2] 主旨命中但太舊的觸發信(只清掉、不觸發)
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

        # [會診2] 陳舊命中信一併標已讀(清掉，避免之後每輪 poll 重複命中+重複 log)
        ids_to_mark = list(matched_ids) if mark_read else []
        if mark_read:
            ids_to_mark += stale_ids
        if ids_to_mark:
            # 一次標記多封為已讀
            try:
                id_list = b",".join(ids_to_mark).decode("ascii")
                conn.store(id_list, "+FLAGS", "(\\Seen)")
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
