# -*- coding: utf-8 -*-
"""「同一件事只通知一次」的去重器 —— 四條告警路徑共用的那套樣板。

【為什麼要有這支】
`main.py` 目前有四條互相獨立的告警路徑(稽核帳本健康、回讀不符、HIS 改版偵測、
止掛提醒無收件人),每一條都自己重寫同一套東西:`notified` 集合、`inflight` 集合、
一把 lock、以及「寄成功才終局去重、寄失敗下次重試」的順序。光是模組級全域就有 8 個。

這套樣板**很容易寫錯,而且錯了不會有人發現**:
  * 忘了 inflight → 同時兩條緒各堆一條 60 秒逾時的寄信背景緒。
  * 先進 notified 再寄 → 一次 SMTP 故障就把該告警**永久滅音**
    (codex P1 在 `_notify_audit_mismatch` 指出過這個)。
  * 寄失敗也不清 inflight → 該告警從此再也不會重試。
「告警永久失效」的故障看起來跟「一切正常」一模一樣,所以這裡不能靠每次重寫。

【擴充規約】新增一種告警 = `AlertDeduper("名稱")` + 用 `send_once()` 包住寄送動作。
不要再自己開 notified/inflight 集合。

【與持久化的關係】HIS 改版通知另有「跨重啟去重」需求(重啟頻繁,不持久化就會重寄),
那條的持久化邏輯較複雜(寄成功但寫檔失敗要留待下次補寫),**本次刻意不動它** ——
先把兩條結構單純的遷過來,持久化版留待下一輪。`persist_probe` 參數已預留給它。
"""
from __future__ import annotations

import logging
import threading
from typing import Callable, Optional


class AlertDeduper:
    """同一個 key 只成功通知一次。執行緒安全,絕不拋。

    語意(順序就是重點):
      1. 已成功通知過(`_done`)→ 不再送。
      2. 正在送(`_inflight`)→ 不並發重送(避免每次都堆一條寄信背景緒)。
      3. 送出動作回 True(真的送成功)→ 才進 `_done` 終局去重。
      4. 送出動作回 False / 丟例外 → 只清 `_inflight`,**下次還會重試**。
    """

    def __init__(self, name: str,
                 persist_probe: Optional[Callable[[str], bool]] = None) -> None:
        self.name = name
        self._lock = threading.Lock()
        self._done: set = set()
        self._inflight: set = set()
        # 預留給「跨重啟去重」:回 True 表示這個 key 之前已經通知過(從磁碟載入)。
        self._persist_probe = persist_probe

    # ── 查詢(測試與呼叫端診斷用)────────────────────────────────────────
    def already_sent(self, key: str) -> bool:
        with self._lock:
            if key in self._done:
                return True
        if self._persist_probe is None:
            return False
        try:
            return bool(self._persist_probe(key))
        except Exception:
            logging.debug("[alert:%s] persist_probe 例外(視為未通知過)",
                          self.name, exc_info=True)
            return False

    def pending(self) -> set:
        with self._lock:
            return set(self._inflight)

    def reset(self) -> None:
        """測試用;正式流程不該呼叫。"""
        with self._lock:
            self._done.clear()
            self._inflight.clear()

    # ── 兩段式:非同步寄送用 ─────────────────────────────────────────────
    def claim(self, key: str) -> bool:
        """佔用 key。回 True 表示「由你負責送」,送完**必須**呼叫 release()。

        ★為什麼需要兩段式★ 有些告警是丟背景緒寄的(SMTP 逾時不可卡住熱鍵/帳本
        寫入緒)。佔用必須在 **spawn 之前同步完成** —— 若等到背景緒裡才佔,
        兩次呼叫之間就會各自堆一條 60 秒逾時的執行緒,正是 inflight 要防的事。
        """
        with self._lock:
            if key in self._done or key in self._inflight:
                return False
        if self.already_sent(key):      # 跨重啟去重(有 persist_probe 時)
            with self._lock:
                self._done.add(key)
            return False
        with self._lock:
            if key in self._done or key in self._inflight:
                return False            # 兩條緒同時走到這 → 只讓一條進去
            self._inflight.add(key)
            return True

    def release(self, key: str, ok: bool) -> None:
        """歸還 claim。ok=True(真的送成功)才終局去重。

        ★只有真的送成功才進 _done★ 失敗要留給下次重試,否則一次 SMTP 故障
        就把該告警**永久滅音** —— 而「永久滅音」看起來跟「一切正常」一模一樣。
        """
        with self._lock:
            self._inflight.discard(key)
            if ok:
                self._done.add(key)

    # ── 主要入口(同步寄送)──────────────────────────────────────────────
    def send_once(self, key: str, send: Callable[[], bool]) -> bool:
        """對 key 執行一次 send();回傳「這次是否真的送成功」。

        send 必須回 bool:True=真的送出去了。**不可**用「沒丟例外」當成功 ——
        SMTP 部分收件人被拒是正常返回(見 smtp_mail),那種情況要能重試。
        """
        if not self.claim(key):
            return False
        ok = False
        try:
            ok = bool(send())
        except Exception:
            logging.debug("[alert:%s] 送出動作丟例外(視為失敗,下次重試)",
                          self.name, exc_info=True)
            ok = False
        finally:
            self.release(key, ok)
        return ok


class DailyOnce:
    """「同一天只做一次」的節流器(分鐘級掃描迴圈用,避免洗版 log)。

    與 AlertDeduper 的差別:這個**不管成不成功**,只管「今天講過沒有」——
    用在「提醒使用者去設定頁補收件人」這種純提示,重試沒有意義。
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = threading.Lock()
        self._seen: dict = {}      # {key: 日期字串}

    def should_run(self, key: str, today: str) -> bool:
        with self._lock:
            if self._seen.get(key) == today:
                return False
            self._seen[key] = today
            return True

    def reset(self) -> None:
        with self._lock:
            self._seen.clear()


# ── 保留期過濾（P2-06 第三刀 2026-07-31 從 main.py 搬入）────────────────────
def filter_recent_alert_sent(data, cutoff: str) -> dict:
    """保留 value(ISO 日期字串)>= cutoff 的項目;非 dict / 非字串鍵值一律剔除。
    ISO 日期零補位 → 可直接字典序比較。純函式以便測試。"""
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items()
            if isinstance(k, str) and isinstance(v, str) and v >= cutoff}
