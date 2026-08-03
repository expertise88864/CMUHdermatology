# -*- coding: utf-8 -*-
"""會診查詢「常駐登入」的純政策層（無 win32、無 IO，可完整測試）。

[2026-08-03 使用者定案] 背景：院方 HIS 閒置 5 分鐘會強制登出，而每輪「開程式→
登入→查→關程式」的冷啟動既慢又讓登入次數逼近鎖定門檻（今天 BDE 事故時一天內
送出 20+ 次帳密）。改為【常駐】：登入一次後停在主畫面，每 3 分鐘（±10% 隨機）
按一次會診查詢、擷取完按「回」退回主畫面——查詢本身就是 keepalive。

規格（使用者原話對應）：
  * 每 3 分鐘 ±10% 查一次，查完退回主畫面等待，不關閉程式。
  * 掉線 → 整個 systemftp 殺掉重啟；重啟後能正常登入就繼續常駐。
    登入失敗 → 停下來發信（沿用既有告警機制），並進入登入冷卻期
    （避免 3 分鐘節奏把同一組帳密連續送出而逼近鎖定門檻——舊節奏是 15 分鐘
    一次，冷卻期就取 15 分鐘，登入壓力不高於改版前）。
  * 00:00–06:00 休息：不查詢，並把 systemftp 關掉（反正會被強制登出）。
  * 每 6 小時定期重啟 HIS（清 BDE/資源殘留，正是 $250E 的成因）。
  * 跑出 BDE 錯誤 → 重開電腦修復，但必須【使用者連續 30 分鐘沒有鍵盤滑鼠
    輸入】才能重開；且 24 小時內只自動重開一次（重開後仍 BDE → 這不是
    重開機能修的，改為告警請人工處理，絕不能陷入重開機迴圈）。
"""
from __future__ import annotations

POLL_JITTER_RATIO = 0.10          # ±10%（使用者指定）
POLL_MIN_MINUTES = 2              # 常駐後每輪只是「按查詢再退回」，2 分鐘已很保守
POLL_MAX_MINUTES = 120
# [codex P1 R8/R9] 常駐模式的節奏【上限】:院方 5 分鐘閒置登出。schedule 套件
# 的間隔是從【任務結束】起算,而任務對 HIS 的最後互動(按「回」)之後還有寄信
# 尾段(SMTP 最壞 ~60 秒)——所以預算是「郵件尾段 + 間隔上限 < 300 秒」:
# 3 分鐘 ±10% 最長 198 秒,留 >100 秒給尾段,安全;4 分鐘(264 秒)就只剩 36 秒,
# 一封慢信就超線 → 上限定 3。設定值再大(10/30 分)也要夾下來,否則 session
# 每輪過期=每輪冷啟動登入,常駐等於沒做、登入壓力反而更高。
POLL_KEEPALIVE_CAP_MINUTES = 3
# [codex P1 R18] Outlook 寄信路徑的尾段最壞可達 120 秒(既有 Outlook timeout)
# → 198+120=318 > 300 會超線;Outlook 模式上限另夾 2 分鐘(132+120=252,留 48 秒)。
POLL_KEEPALIVE_CAP_OUTLOOK_MINUTES = 2
SESSION_MAX_AGE_HOURS = 6.0       # 定期重啟 HIS（清 BDE/資源殘留）
LOGIN_COOLDOWN_SECONDS = 15 * 60  # 登入失敗冷卻＝舊輪詢節奏，登入壓力不升
BDE_COOLDOWN_SECONDS = 30 * 60    # BDE 失敗冷卻（等重開機/人工處理，別空轉）
BDE_REBOOT_MIN_IDLE_SECONDS = 30 * 60   # 使用者連續閒置 30 分鐘才可重開機
BDE_REBOOT_MIN_GAP_SECONDS = 24 * 3600  # 24 小時內最多自動重開一次（防迴圈）


def poll_seconds_range(interval_minutes) -> tuple:
    """輪詢間隔（分）→ (下限秒, 上限秒)，±10% 隨機抖動的邊界。

    夾在 [POLL_MIN_MINUTES, POLL_MAX_MINUTES]；壞值回預設 3 分鐘的範圍。
    """
    try:
        minutes = float(interval_minutes)
    except (TypeError, ValueError):
        minutes = 3.0
    if minutes != minutes or minutes <= 0:      # NaN / 非正值
        minutes = 3.0
    minutes = max(POLL_MIN_MINUTES, min(POLL_MAX_MINUTES, minutes))
    base = minutes * 60.0
    lo = int(round(base * (1.0 - POLL_JITTER_RATIO)))
    hi = int(round(base * (1.0 + POLL_JITTER_RATIO)))
    return (max(1, lo), max(2, hi))


def session_needs_restart(started_at: float, now: float,
                          max_age_hours: float = SESSION_MAX_AGE_HOURS) -> bool:
    """常駐 session 是否到了定期重啟時間（≥6 小時）。

    時鐘倒退（NTP 校時）→ 視為到期重啟：重啟成本低，比帶著不明狀態繼續穩。
    """
    if now < started_at:
        return True
    return (now - started_at) >= max_age_hours * 3600.0


def login_cooldown_remaining(cooldown_until: float, now: float) -> float:
    """登入冷卻剩餘秒數（≤0 表示可登入）。異常大的未來值視為壞資料 → 0。"""
    remaining = cooldown_until - now
    if remaining > LOGIN_COOLDOWN_SECONDS * 4:   # 遠超任何合法冷卻 → 壞資料
        return 0.0
    return max(0.0, remaining)


def bde_reboot_decision(idle_seconds: float, last_reboot_ts,
                        now: float) -> tuple:
    """BDE 錯誤後是否可以自動重開機 → (動作, 人話理由)。

    動作："reboot"＝現在重開、"wait"＝繼續等閒置、"give_up"＝停止嘗試改人工。
    規則（使用者定案 2026-08-03）：
      * 連續閒置 < 30 分鐘 → wait（使用者可能在用電腦，絕不能重開）。
      * 24 小時內已自動重開過一次 → give_up（重開沒修好，這不是重開機能修的；
        再重開只會進入迴圈，改為告警請人工處理）。
      * 其餘 → reboot。
    """
    if idle_seconds < BDE_REBOOT_MIN_IDLE_SECONDS:
        return ("wait",
                f"使用者 {idle_seconds / 60:.0f} 分鐘前仍有輸入，"
                f"等連續閒置滿 {BDE_REBOOT_MIN_IDLE_SECONDS // 60} 分鐘")
    if last_reboot_ts is not None:
        try:
            gap = now - float(last_reboot_ts)
        except (TypeError, ValueError):
            gap = None
        # [codex P1 R7] gap 為負(重開後時鐘倒退/NTP 校時)一樣算「冷卻期內」——
        # 這道防護存在的目的就是防重開機迴圈,時鐘異常時要保守擋下,不是放行。
        if gap is not None and gap < BDE_REBOOT_MIN_GAP_SECONDS:
            return ("give_up",
                    f"{gap / 3600:.1f} 小時前已自動重開過一次仍出現 BDE 錯誤"
                    f"——重開機修不了，請人工處理（清 BDE 鎖檔/找資訊室）")
    return ("reboot", "使用者已連續閒置逾 30 分鐘且 24 小時內未自動重開過")
