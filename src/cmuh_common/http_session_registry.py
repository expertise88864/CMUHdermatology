# -*- coding: utf-8 -*-
"""所有 thread-local `requests.Session` 的註冊表與退出清理。
（P2-06 分層第四刀(b) 2026-08-01，從 main.py 搬入）

【為什麼要有這個註冊表】
`threading.local` 不會把別的執行緒建的 session 暴露給主緒 —— 所以程式退出時
沒辦法「走訪所有 session 去斷線」。額外維護一個 `WeakSet`：建 session 時登記，
atexit 時把每個 adapter 的 poolmanager 清掉，強制斷連。

★用 WeakSet 而不是 set★：執行緒結束、session 沒人引用時要能被回收，
否則跑一整天會累積一堆死 session。

★atexit 只 clear pool、不 close★：`session.close()` 會等未完成的 request，
退出時可能卡住。與 `_kill_orphan_chromedriver` 同一個 spirit：強制斷、不等、立刻返回。
"""
from __future__ import annotations

import atexit
import contextlib
import threading
from weakref import WeakSet


# [v18 2026-05-25] 追蹤所有 thread-local sessions 給 atexit poolmanager.clear()
# 用。threading.local 本身不暴露跨 thread 的 session 給 main thread，所以額外
# 維護一個 set；建 session 時 add，atexit 時 clear adapter pool 強制斷連線。
# (不 call session.close() 避免等待未完成 request — 跟 _kill_orphan handler 同 pattern)
_all_reg_sessions: WeakSet = WeakSet()

_all_reg_sessions_lock = threading.Lock()


@contextlib.contextmanager
def _session_http_guard(session):
    """requests.Session 非執行緒安全；多執行緒共用時以鎖保護連線池與 cookie。"""
    lock = getattr(session, '_lock', None)
    if lock is not None:
        with lock:
            yield
    else:
        yield


def _register_reg_session(s):
    """新建 thread-local session 時呼叫，給 atexit cleanup 用。"""
    with _all_reg_sessions_lock:
        _all_reg_sessions.add(s)


def _atexit_clear_thread_local_sessions() -> None:
    """[v18] 程式退出時清所有 thread-local session 的 poolmanager，
    避免 dangling connection。跟 _kill_orphan_chromedriver 路徑同 spirit:
    強制斷連、不等未完成 request、立刻返回。
    """
    with _all_reg_sessions_lock:
        sessions = list(_all_reg_sessions)
        _all_reg_sessions.clear()
    for s in sessions:
        try:
            for adapter in s.adapters.values():
                try:
                    adapter.poolmanager.clear()
                except Exception:
                    pass
        except Exception:
            pass


# 公開名稱（新呼叫端用這個；main.py 仍以舊私有名匯入，不改呼叫端）
register_session = _register_reg_session
clear_all_sessions = _atexit_clear_thread_local_sessions
session_http_guard = _session_http_guard

# ★atexit 註冊要跟著搬★ 留在 main.py 的話，只有 import main 的行程會清理；
#   而這個註冊表現在是共用的（consult_query / autoclock 也可能用到）。
atexit.register(_atexit_clear_thread_local_sessions)
