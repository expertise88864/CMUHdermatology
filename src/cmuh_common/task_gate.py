# -*- coding: utf-8 -*-
"""Small helpers to prevent duplicate background tasks from piling up.

★[2026-07-30 第二輪外審 P2-01] 這把閘門【不會】終止舊工作★

`stale_after_sec` 到了以後，`acquire_lease()` 會把同一個 key 再發一張 lease 給
新的 tick —— 但**舊的 worker 沒有被終止、沒有被通知、也不知道自己被接管了**。
它會繼續跑：繼續開 Chrome、繼續對 HIS 寫入、繼續寄信。舊版連一行 log 都沒有，
所以「一個任務卡了 45 分鐘」這件事在正式環境是完全隱形的。

實務上兩個呼叫端下游都還有一把序列化鎖（autoclock 的 `clock_lock`、consult 的
`_flow_lock`），所以多半不會真的同時動作 —— 但代價是**新 worker 永久阻塞在那把
鎖上**，而整個打卡窗／會診輪詢就這樣靜默過去。`consult_query` 的模組註解甚至
明文寫著「job 互斥 → 同時只有一個 `_do_full_job` 在跑,無並發競爭」；那個假設在
逾時接管之後就不成立了。

本輪處理（誠實範圍）：

  1. **逾時接管一定要出聲**：`logging.warning` + 可選的 `on_supersede` 回呼
     （呼叫端接上寄信告警）。一個跑了 45／90 分鐘的任務是生產事故，不是 info。
  2. **舊 worker 查得到自己被接管**：`lease.superseded` / `gate.holds(lease)`。
     呼叫端在【動作前】check，被接管就放棄，而不是繼續寫下去。
  3. `release()` 早就有 token 比對，所以舊 worker 收尾時不會誤刪新 lease。

★本輪【沒有】做的（需要另開工單）★
  * 真正終止跑掉的工作。Python 沒有安全的 thread kill；要能終止就得把 worker 拆成
    可 terminate 的子行程（外審 P2-01 的長線建議），那是架構改動。
  * 卡在 Win32／Selenium 呼叫裡的 worker 到不了任何 checkpoint，`superseded` 幫不上
    忙 —— 這一條只有子行程化解得掉。
  * 沒有改名為 `AdmissionLeaseGate`。外審的理由（名字暗示了它做不到的保證）成立，
    但改名本身不修任何行為，而這個 repo 現在正在 review 循環中；先把保證講清楚並
    做出來，改名列為後續整理。
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Hashable
from dataclasses import dataclass, field
from itertools import count
from typing import Callable


@dataclass(frozen=True)
class TaskLease:
    key: Hashable
    token: int
    # 反向指標，供 `superseded` 查詢。compare=False：lease 的身分只由 key+token 決定，
    # 既有的 `release(key, lease)` token 比對行為不可因為多了這個欄位而改變。
    gate: "ActiveTaskGate | None" = field(default=None, compare=False,
                                          repr=False)

    @property
    def superseded(self) -> bool:
        """這張 lease 是否已被【別人接管】—— 即目前 key 在另一張更新的 lease 手上。

        ★動作前要 check 這個★ 被接管代表「系統已經認定我死了，並且另外派了一個
        worker 去做同一件事」—— 這時還繼續對 HIS 寫入／寄信就是重複動作。

        ★[2026-07-30 外審第 1 輪] 「沒有人持有」【不】算被接管。★
        我第一版寫 `not gate.holds(self)`，於是「注錯人拿了 lease 但马上放棄」也會
        讓舊 worker 認為自己被接管。consult 就是這種形狀：接管者進來發現
        `_flow_lock` 被舊 worker 持著（non-blocking）→ 立即 return 並释放 lease
        → 舊 worker 跑到檢查點看到「沒人持有」也放棄 → **兩邊都不寄信**，
        比修之前還糟。「没人在做我的工作」的正確結論是「我要繼續把它做完」。
        沒有 gate 反向指標時也回 False（保守：不因為缺資訊就中止臨床流程）。
        """
        gate = self.gate
        if gate is None:
            return False
        return gate.taken_over_by_other(self)


class ActiveTaskGate:
    """Track active task keys across background workers.

    The gate is intentionally tiny: callers acquire before starting a worker and
    release in the worker's finally block. If the worker hangs, later ticks skip
    instead of creating an unbounded queue of blocked threads.

    逾時接管的語意與限制見模組 docstring —— 它**不終止**舊工作。

    on_supersede: (key, age_sec) -> None，逾時接管時呼叫（呼叫端用來寄告警）。
                  絕不可讓它的例外影響取得 lease，故一律吞例外。
    """

    def __init__(
        self,
        stale_after_sec: float | None = None,
        clock: Callable[[], float] | None = None,
        *,
        label: str = "",
        on_supersede: "Callable[[Hashable, float], None] | None" = None,
    ) -> None:
        self._active: dict[Hashable, tuple[float, int]] = {}
        self._lock = threading.Lock()
        self._stale_after_sec = stale_after_sec
        self._clock = clock or time.monotonic
        self._token_counter = count(1)
        self._label = label
        self._on_supersede = on_supersede
        self._superseded_count = 0

    @property
    def superseded_count(self) -> int:
        """本行程存活期間發生過幾次逾時接管（給健康檢查／log 用）。"""
        with self._lock:
            return self._superseded_count

    def holds(self, lease: TaskLease) -> bool:
        """這張 lease 是不是該 key 目前的持有者。"""
        with self._lock:
            entry = self._active.get(lease.key)
            return entry is not None and entry[1] == lease.token

    def taken_over_by_other(self, lease: TaskLease) -> bool:
        """該 key 目前是不是在【別人】手上。

        與 `not holds()` 的差別：**key 沒人持有時回 False**。
        理由見 `TaskLease.superseded` 的註解 —— 「沒人在做我的工作」不是
        「我該放棄」，而是「我要繼續把它做完」。
        """
        with self._lock:
            entry = self._active.get(lease.key)
            if entry is None:
                return False
            return entry[1] != lease.token

    def _is_stale(self, started_at: float, now: float) -> bool:
        return (
            self._stale_after_sec is not None
            and self._stale_after_sec > 0
            and now - started_at >= self._stale_after_sec
        )

    def acquire(self, key: Hashable) -> bool:
        return self.acquire_lease(key) is not None

    def acquire_lease(self, key: Hashable) -> TaskLease | None:
        now = self._clock()
        with self._lock:
            entry = self._active.get(key)
            started_at = entry[0] if entry else None
            if started_at is not None and not self._is_stale(started_at, now):
                return None
            took_over_age = None
            if started_at is not None:
                # 逾時接管：舊 worker 還在跑（我們終止不了它），至少要出聲。
                took_over_age = max(0.0, now - started_at)
                self._superseded_count += 1
            token = next(self._token_counter)
            self._active[key] = (now, token)
        if took_over_age is not None:
            self._report_supersede(key, took_over_age)
        return TaskLease(key=key, token=token, gate=self)

    def _report_supersede(self, key: Hashable, age_sec: float) -> None:
        """★不可在持鎖時呼叫★（回呼可能寄信／取別的鎖 → 死鎖風險）。"""
        tag = f"[{self._label}] " if self._label else ""
        logging.warning(
            "%s工作 %r 已執行 %.0f 分鐘（超過 %s 分鐘上限）→ 已把 lease 發給新的一輪，"
            "但【舊的那個仍在執行且無法終止】。若它還會對 HIS 寫入／寄信，"
            "請以人工方式確認有沒有重複動作。",
            tag, key, age_sec / 60.0,
            "?" if self._stale_after_sec is None
            else f"{self._stale_after_sec / 60.0:.0f}")
        cb = self._on_supersede
        if cb is None:
            return
        try:
            cb(key, age_sec)
        except Exception:
            logging.debug("%son_supersede 回呼失敗（略過）", tag, exc_info=True)

    def release(self, key: Hashable, lease: TaskLease | None = None) -> None:
        with self._lock:
            if lease is not None:
                if lease.key != key:
                    return
                entry = self._active.get(key)
                if entry is None or entry[1] != lease.token:
                    return
            self._active.pop(key, None)

    # ★查詢不可順手清掉逾時紀錄★
    # 舊版 `is_active`/`active_age_sec` 遇到逾時就 `pop`。那樣一來，只要有人先查了
    # 一次（健康檢查、log），下一次 `acquire_lease` 就看不到那筆紀錄，於是【逾時接管
    # 不會被記錄、不會告警】—— 正好把 P2-01 要修的可見性又弄丟。查詢就只是查詢；
    # 逾時紀錄由 `acquire_lease` 接管時取代（那時才會出聲）。
    def is_active(self, key: Hashable) -> bool:
        now = self._clock()
        with self._lock:
            entry = self._active.get(key)
            if entry is None:
                return False
            return not self._is_stale(entry[0], now)

    def active_age_sec(self, key: Hashable) -> float | None:
        now = self._clock()
        with self._lock:
            entry = self._active.get(key)
            if entry is None:
                return None
            started_at, _token = entry
            if self._is_stale(started_at, now):
                return None
            return max(0.0, now - started_at)


# ─── worker 端的「我還是不是現役」查詢 ──────────────────────────────────────
# thread-local 而非模組層變數:逾時接管之後【兩個 worker 同時存在】,若用一個模組層
# 變數記「目前的 lease」,新 worker 一設定就把舊 worker 的身分蓋掉 → 舊 worker 反而
# 查到新 lease、判定自己沒被接管,保護整個失效。每條 worker 緒各自帶自己的 lease。
_worker_lease_tls = threading.local()


class worker_lease_scope:  # noqa: N801 - 當 context manager 用,沿用小寫慣例
    """把 lease 綁在【本 worker 緒】上，讓呼叫樹深處也查得到自己是否已被接管。"""

    def __init__(self, lease: "TaskLease | None") -> None:
        self._lease = lease
        self._prev = None

    def __enter__(self) -> "TaskLease | None":
        self._prev = getattr(_worker_lease_tls, "lease", None)
        _worker_lease_tls.lease = self._lease
        return self._lease

    def __exit__(self, *_exc) -> None:
        _worker_lease_tls.lease = self._prev


def current_worker_superseded() -> bool:
    """本緒的工作是否已被逾時接管（沒有 lease 綁定時回 False）。

    ★用途★ 在【動作之前】check：被接管代表系統已經另派一個 worker 做同一件事，
    這時還繼續對 HIS 寫入／寄信就是重複動作。查不到 lease 一律回 False ——
    保守：不因為缺資訊就中止臨床流程。
    """
    lease = getattr(_worker_lease_tls, "lease", None)
    if lease is None:
        return False
    try:
        return bool(lease.superseded)
    except Exception:
        logging.debug("[task_gate] 查詢 superseded 失敗（視為未接管）",
                      exc_info=True)
        return False
