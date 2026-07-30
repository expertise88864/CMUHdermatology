# -*- coding: utf-8 -*-
"""統一的保留期清掃（RetentionSweeper）。

★[2026-07-30 第二輪外審 P1-03] 為什麼要有這個模組★
在此之前每一種落地檔各自清自己的，而且都有同一個結構性問題：
**清理只發生在「產生那種檔案的事情再度發生」的時候，而且大多只看數量、沒有時效。**

  * `patient_locator.append_index()` 只在【下一次有回讀不符】時才修剪 →
    宣告 `INDEX_RETAIN_DAYS = 30`，實際上某個病人的病歷號可以留一整年
    （只要這一年內沒有再發生 mismatch）。
  * `autoclock.prune_debug_dumps()` 只看「最多 40 個檔」→ 只要總數沒破 40，
    含帳號的完整 screenshot 與 page_source HTML 可以永久留在電腦上。
  * `consult_query._prune_old_shots()` 同上（最多 60 張會診截圖）。
  * `settings_defaults.restore_defaults()` 產生的 `.before-reset-*` 完全沒人清。
  * `paths.sweep_old_restart_err_files()` 有 TTL，但要有人叫它。

宣告了保留期卻不主動執行，等於沒有保留期 —— 而這些檔案裡有病歷號、帳號、
完整畫面。故集中成一支「不依賴任何事件發生」的清掃器，由主程式在
**啟動時**與**每日固定時間**各跑一次。

設計取捨：
- **一律以 mtime 判齡**，不解析檔名。各處的時間戳格式不一致，解析失敗就會靜默
  跳過該檔 —— 那正是「宣告了卻沒生效」的老毛病。
- **純 TTL，不保底留幾份**。曾想過「就算過期也留最新 N 份好除錯」，但這幾類檔的
  保留期是【隱私要求】而不是容量管理；留一份過期的含病歷號截圖，違反的正是要求
  本身。要除錯就在期限內去看。
- **絕不拋例外**。清理失敗不可影響臨床流程（與 action_ledger / patient_locator
  同一原則）；逐檔吞例外並回報實際刪掉幾個，讓呼叫端可以記 log / 顯示健康狀態。
"""
from __future__ import annotations

import glob
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime


# ─── 保留期天數:★單一權威★ ────────────────────────────────────────────────
# 產生這些檔的模組(autoclock / consult_query)與跑清掃的模組(main)都從這裡取。
# 兩邊各寫一個數字遲早會不一致 —— 外審第 1 輪就抓到 `.corrupt-*` 同時被宣告成
# 30 天(cache_cleanup)與 90 天(我新加的規則),於是「有 90 天搶救窗」是謊話。
#
# 天數的取法:含【完整畫面】(帳號、病人清單)的最短;定位索引只有診間/診號/病歷號、
# 又是事後查「哪個病人寫錯」的唯一依據 → 沿用既有宣告的 30 天;設定備份不含個資。
DEBUG_DUMP_RETAIN_DAYS = 3          # 打卡除錯檔:截圖 + page_source HTML
CONSULT_SHOT_RETAIN_DAYS = 7        # 會診清單截圖
SETTINGS_BACKUP_RETAIN_DAYS = 90    # .before-reset-*（不含個資）
# `.corrupt-*` 刻意不在此:cmuh_common/cache_cleanup.py 已經以 30 天清它,
# 同一種檔只能有一個權威 TTL。


@dataclass(frozen=True)
class RetentionRule:
    """一條「這個資料夾裡符合這些樣式的檔案只留幾天」規則。

    label       健康狀態/log 用的人話名稱
    directory   絕對路徑（不存在 → 靜默跳過）
    patterns    glob 樣式（相對 directory）
    retain_days 幾天前的檔案要刪（以 mtime 計）
    """
    label: str
    directory: str
    patterns: tuple
    retain_days: float
    sensitive: bool = True      # 是否含個資/帳號（供「最舊敏感檔」統計）


@dataclass
class SweepResult:
    deleted: dict = field(default_factory=dict)     # {label: 刪掉幾個}
    failed: dict = field(default_factory=dict)      # {label: 刪不掉幾個}
    oldest: "tuple | None" = None                   # (label, mtime datetime)

    @property
    def total_deleted(self) -> int:
        return sum(self.deleted.values())

    def summary(self) -> str:
        parts = [f"{k}×{v}" for k, v in sorted(self.deleted.items()) if v]
        out = ("清掉 " + "、".join(parts)) if parts else "沒有過期檔案"
        if self.failed:
            out += "；刪不掉:" + "、".join(
                f"{k}×{v}" for k, v in sorted(self.failed.items()) if v)
        if self.oldest:
            out += f"；最舊敏感檔:{self.oldest[0]} {self.oldest[1]:%Y-%m-%d}"
        return out


def _older(cur, cand: float) -> float:
    """取較舊的那個 mtime（None＝還沒有候選）。"""
    return cand if cur is None or cand < cur else cur


def _iter_matches(rule: RetentionRule):
    for pat in rule.patterns:
        for p in glob.glob(os.path.join(rule.directory, pat)):
            if os.path.isfile(p):
                yield p


def sweep(rules, extra_tasks=(), *, now: "float | None" = None) -> SweepResult:
    """跑一輪清掃。

    extra_tasks: [(label, callable)]，callable 回傳「處理掉幾筆」。給那些不是
    「刪整個檔」的清理用（例如定位索引是逐【列】修剪，檔案本身要留著）。
    絕不拋例外。
    """
    res = SweepResult()
    now = time.time() if now is None else now
    for rule in rules:
        if not os.path.isdir(rule.directory):
            continue
        cutoff = now - rule.retain_days * 86400.0
        gone = bad = 0
        newest_kept: "float | None" = None
        for path in _iter_matches(rule):
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime >= cutoff:
                if rule.sensitive:      # 掃完之後【還在磁碟上】的之中最舊的那個
                    newest_kept = _older(newest_kept, mtime)
                continue
            try:
                os.remove(path)
                gone += 1
            except OSError:
                bad += 1
                # ★[外審第1輪] 刪不掉的【也還在磁碟上】★
                #   原本只把「未過期而留下」的算進 oldest → 一個被鎖住的過期截圖
                #   會被排除在統計外,摘要於是報一個比實情【新】的「最舊敏感檔」,
                #   把真正的保留期違規藏起來。統計要看的是「掃完之後還在的東西」,
                #   不是「我打算留的東西」。
                if rule.sensitive:
                    newest_kept = _older(newest_kept, mtime)
                logging.debug("[retention] 刪不掉(略過):%s", path, exc_info=True)
        if gone:
            res.deleted[rule.label] = gone
        if bad:
            res.failed[rule.label] = bad
        if newest_kept is not None:
            cand = (rule.label, datetime.fromtimestamp(newest_kept))
            if res.oldest is None or cand[1] < res.oldest[1]:
                res.oldest = cand
    for label, fn in extra_tasks:
        try:
            n = int(fn() or 0)
        except Exception:
            logging.debug("[retention] %s 清理失敗(略過)", label, exc_info=True)
            res.failed[label] = res.failed.get(label, 0) + 1
            continue
        if n:
            res.deleted[label] = res.deleted.get(label, 0) + n
    return res


# ─── 規則工廠:規則定義本身也只有一份 ──────────────────────────────────────
def debug_dump_rule(directory: str) -> RetentionRule:
    return RetentionRule("打卡除錯檔", directory,
                         ("*.png", "*.html", "*.txt"), DEBUG_DUMP_RETAIN_DAYS)


def consult_shot_rule(directory: str) -> RetentionRule:
    return RetentionRule("會診截圖", directory, ("consult_*.png",),
                         CONSULT_SHOT_RETAIN_DAYS)


def settings_backup_rule(directory: str) -> RetentionRule:
    return RetentionRule("設定備份", directory, ("*.before-reset-*",),
                         SETTINGS_BACKUP_RETAIN_DAYS, sensitive=False)
