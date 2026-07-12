# -*- coding: utf-8 -*-
"""共用 requests.Session（連線池）。

打卡程式 / 主程式 / 排班程式都可從這裡拿一個帶 Adapter 的 Session 重用 TCP 連線，
省掉每次任務 ~200ms 的握手時間。

內網 host 列表搬自原主程式 line 306-311 的 INTERNAL_HOSTS（_is_internal()）。
"""
import logging
import threading
from urllib.parse import urlparse

INTERNAL_HOSTS: set[str] = {
    '10.20.8.47',
    'forward01.cmuh.org.tw',
    'appointment.cmuh.org.tw',
    'administration.cmuh.org.tw',
}


def is_internal(url: str) -> bool:
    """判斷 URL 是否為已知院內主機（用於決定 SSL verify 策略）。"""
    try:
        # [IF-06 2026-07-12] 用 hostname(自動剝 userinfo/port、無 scheme 較穩)取代
        # netloc.split(':')[0] —— 後者會被 userinfo 欺騙(如 https://10.20.8.47:x@evil.com
        # 的 netloc 為 "10.20.8.47:x@evil.com",split(':')[0]="10.20.8.47" 誤判內網→停 SSL verify)。
        host = (urlparse(url).hostname or "").lower()
        return host in INTERNAL_HOSTS
    except Exception:
        return False


_session_singleton = None
_session_lock = threading.Lock()


def get_shared_session():
    """取得進程級共用 Session（thread-safe）。"""
    global _session_singleton
    if _session_singleton is not None:
        return _session_singleton
    with _session_lock:
        if _session_singleton is not None:
            return _session_singleton
        _session_singleton = _build_session()
        return _session_singleton


def _build_session():
    import requests
    from requests.adapters import HTTPAdapter
    try:
        from urllib3.util.retry import Retry
    except ImportError:
        Retry = None  # type: ignore[assignment]

    session = requests.Session()
    if Retry is not None:
        retry = Retry(
            total=2, connect=2, read=2,
            backoff_factor=0.3,
            status_forcelist=(500, 502, 503, 504),
            allowed_methods=frozenset(['GET', 'HEAD']),
        )
    else:
        retry = None  # type: ignore[assignment]

    adapter = HTTPAdapter(
        pool_connections=4,
        pool_maxsize=8,
        max_retries=retry,
    ) if retry else HTTPAdapter(pool_connections=4, pool_maxsize=8)

    session.mount("http://", adapter)
    session.mount("https://", adapter)
    # session 自帶鎖，便於多執行緒場景 with _session_http_guard 配合使用
    session._lock = threading.RLock()  # type: ignore[attr-defined]
    return session


def disable_insecure_warnings() -> None:
    """關閉內網自簽憑證的 InsecureRequestWarning。"""
    try:
        import requests
        from urllib3.exceptions import InsecureRequestWarning
        requests.packages.urllib3.disable_warnings(category=InsecureRequestWarning)  # type: ignore[attr-defined]
    except Exception:
        logging.debug("disable_insecure_warnings 失敗", exc_info=True)
