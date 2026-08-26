# -*- coding: utf-8 -*-
"""Doctor alert threshold policy helpers."""
from __future__ import annotations

import logging
import re
from typing import Any

# ★止掛提醒對象以下方 _DOCTOR_THRESHOLD_KEYS 為準★
#   (2026-08-05 張廖年峰退場、2026-08-06 加入黃建仁/謝佳陵 —— 名單會變動,
#    所以【不在註解裡重列一份人名】,以免又寫成過期資訊;要看有誰請直接看下面。)
#   哪些醫師「會不會真的收到提醒」另由 main.ALERT_DOCTORS 註冊表 + 各自的開關決定。
#   沈冠宇的一早/一午/三午【刻意不給預設值】—— 沒有預設就不會有門檻,
#   `build_doctor_threshold_map` 會跳過那些診次(int(None)/int("") 都落到 except),
#   等使用者自己在設定頁填上數字才開始提醒。只有三晚預設 100 人。
DEFAULT_THRESHOLDS = {
    "chen_mon_afternoon": 69,
    "chen_tue_night": 59,
    "chen_thu_morning": 54,
    "chen_thu_afternoon": 69,
    "shen_wed_night": 100,
    # [2026-08-06 使用者] 黃建仁:週三早 60。
    # [2026-08-26 使用者] 謝佳陵:新增週六早,且四個診次預設一律 70
    #   (原 2026-08-06 定的 75 作廢;舊檔存 75 的遷移見 app_settings)。
    # 兩位的提醒開關預設皆關(alert_huang/hsieh_enabled,見 settings_defaults)。
    "huang_wed_morning": 60,
    "hsieh_thu_morning": 70,
    "hsieh_thu_night": 70,
    "hsieh_fri_afternoon": 70,
    "hsieh_sat_morning": 70,
}

# ★止掛提醒對象的沿革(功能契約,寫在程式碼旁邊)★
#   這份名單是【使用者定案】的,不是實作細節 —— 外部審查若拿著舊的需求描述
#   來看,會把「多出來的醫師」當成偏離契約而要求移除。把沿革記在這裡,
#   是為了讓下一個讀這段程式的人(或審查者)有唯一可信的來源。
#
#     2026-08-05  使用者:移除張廖年峰、新增沈冠宇(整套止掛邏輯保留)。
#                 沈冠宇一早/一午/三午【刻意不給預設值】,只有三晚預設 100。
#     2026-08-06  使用者(在另一台機器上,commit 624fb39):新增黃建仁、謝佳陵。
#     2026-08-26  使用者:謝佳陵補【週六早】診次;四診次預設 75 → 70。
#
#   ★目前對象:陳駿升、沈冠宇、黃建仁、謝佳陵 四位。★
#   要增減請改這裡與 `main.py` 的 `ALERT_DOCTORS` 註冊表(唯一來源),
#   並更新上面這段沿革。
_DOCTOR_THRESHOLD_KEYS = {
    "陳駿升": (
        ((0, "下午"), "chen_mon_afternoon"),
        ((1, "晚上"), "chen_tue_night"),
        ((3, "上午"), "chen_thu_morning"),
        ((3, "下午"), "chen_thu_afternoon"),
    ),
    "沈冠宇": (
        ((0, "上午"), "shen_mon_morning"),      # 一早：無預設
        ((0, "下午"), "shen_mon_afternoon"),    # 一午：無預設
        ((2, "下午"), "shen_wed_afternoon"),    # 三午：無預設
        ((2, "晚上"), "shen_wed_night"),        # 三晚：預設 100
    ),
    "黃建仁": (
        ((2, "上午"), "huang_wed_morning"),     # 三早：預設 60
    ),
    "謝佳陵": (
        ((3, "上午"), "hsieh_thu_morning"),     # 四早：預設 70
        ((3, "晚上"), "hsieh_thu_night"),       # 四晚：預設 70
        ((4, "下午"), "hsieh_fri_afternoon"),   # 五午：預設 70
        ((5, "上午"), "hsieh_sat_morning"),     # 六早：預設 70(2026-08-26 新增)
    ),
}

_COUNT_DIGIT_RE = re.compile(r"(\d+)")


# ★門檻的合理範圍★(2026-08-05 外審第 5 輪 P2-09)
#   下限不是 1 而是 20:`is_near_alert_threshold` 的判準是 `count >= 門檻 - margin`,
#   margin 預設 10。門檻若 ≤ 10,第一位病人掛進來就「接近門檻」——「還沒真的滿」
#   與「快滿了」再也分不開,提醒等於恆真。門檻 0/負數更是直接恆真。
#   上限純粹是打字防呆(多按一個 0)。實際門檻歷來落在 54–129。
MIN_THRESHOLD = 20
MAX_THRESHOLD = 400


def validate_threshold_entry(cfg_key: str, raw: Any) -> tuple:
    """設定頁一格門檻 → (要存的值, 錯誤訊息)。錯誤訊息非空 = 呼叫端必須拒絕存檔。

    ★留空 = 這個診次【沒有門檻】,不是 0★
      門檻 0 會讓 `is_near_alert_threshold`(count >= 0 - margin)恆真 ——
      「還沒設定」會變成「每一診都提醒」,方向完全相反。這一格原本的寫法是
      `except: DEFAULT_THRESHOLDS.get(key, 0)`,對沈冠宇那三個【刻意沒有預設】
      的診次就會存下 0。故:空字串一律存 ""(build_doctor_threshold_map 會跳過)。

    ★[2026-08-05 外審第 5 輪 P2-09/P2-10] 不合法就報錯,不要替使用者猜★
      上一版對打錯字的處置是「沿用原廠預設,沒有預設就退成空」——兩種都是
      **靜默改掉使用者的設定**:
        * 原本自訂 88、手滑打成 `8O` → 存檔後悄悄變回原廠的 59
        * 原本自訂 100、打錯 → 悄悄變成「這個診次不提醒」
      使用者以為自己改了一個數字,實際上關掉了一個提醒。錯誤要當場說出來、
      保留原值,由使用者自己決定。
      同理,`0` / `-1` 這種「轉得成 int 但語意上是恆真」的輸入也必須擋下 ——
      只防空字串變成 0、卻放行直接輸入的 0,等於只堵了一半。
    """
    text = str(raw if raw is not None else "").strip()
    if not text:
        return "", ""                      # 留空 = 刻意不設門檻
    try:
        value = int(text)
    except (TypeError, ValueError):
        return None, f"「{text}」不是數字"
    if not MIN_THRESHOLD <= value <= MAX_THRESHOLD:
        return None, (f"{value} 不在合理範圍（{MIN_THRESHOLD}–{MAX_THRESHOLD}）"
                      f"；留空代表這個診次不提醒")
    return value, ""


def build_doctor_threshold_map(doctor_name: str, threshold_settings: dict | None) -> dict:
    """Build (weekday, session) -> alert threshold for one doctor."""
    ts = threshold_settings if isinstance(threshold_settings, dict) else {}
    pairs = _DOCTOR_THRESHOLD_KEYS.get(doctor_name)
    if not pairs:
        return {}

    out = {}
    for session_key, cfg_key in pairs:
        raw = ts.get(cfg_key, DEFAULT_THRESHOLDS.get(cfg_key))
        try:
            value = int(raw)
        except (TypeError, ValueError):
            continue
        # ★[2026-08-05 外審第 6 輪 P2-04] 執行期也要驗範圍★
        #   設定頁的驗證只擋得住「現在存進去的」;舊版曾存下的 0、使用者手改
        #   JSON 的 -1、損壞檔案裡的異常值,都是從這裡直接生效 —— 而門檻 ≤
        #   margin(10) 會讓提醒恆真(count >= 門檻-10)。與 UI 用同一組界線,
        #   不合法的當成「這個診次沒有門檻」並記 log(不靜默改成別的數字)。
        if not MIN_THRESHOLD <= value <= MAX_THRESHOLD:
            logging.warning("[threshold] %s=%r 不在合理範圍(%d–%d) → 本診次視為未設門檻",
                            cfg_key, raw, MIN_THRESHOLD, MAX_THRESHOLD)
            continue
        out[session_key] = value
    return out


def appt_item_session_and_count_text(appt_item: Any) -> tuple[str, str]:
    """Extract session and count/status text from cached appointment item."""
    if isinstance(appt_item, dict):
        session_name = str(appt_item.get("session", ""))
        raw_count = appt_item.get("count", 0)
        status_text = str(raw_count)
        if isinstance(raw_count, int):
            status_text += "人"
        return session_name, status_text

    text = str(appt_item)
    parts = text.split("|", 1)
    status_part = parts[0]
    if ":" not in status_part:
        return "", status_part.strip()
    session_name, status_text = status_part.split(":", 1)
    return session_name, status_text.strip()


def is_near_alert_threshold(
    sessions,
    weekday_idx,
    threshold_map,
    margin: int = 10,
) -> bool:
    """Return true when any session count is within margin of its threshold."""
    if not sessions or not threshold_map:
        return False
    try:
        normalized_weekday = int(weekday_idx)
    except (TypeError, ValueError):
        return False
    try:
        normalized_margin = int(margin)
    except (TypeError, ValueError):
        normalized_margin = 10

    for appt_item in sessions:
        # [CL-04 audit 2026-07-12] 已止掛(is_stopped:不會再增號)不應因既有數接近門檻而誤發「快滿」。
        if isinstance(appt_item, dict) and appt_item.get("is_stopped"):
            continue
        session_name, status_text = appt_item_session_and_count_text(appt_item)
        if "休診" in status_text or "停診" in status_text:
            continue
        match = _COUNT_DIGIT_RE.search(status_text)
        if not match:
            continue
        try:
            count = int(match.group(1))
        except ValueError:
            continue
        threshold = threshold_map.get((normalized_weekday, session_name))
        if isinstance(threshold, int) and count >= threshold - normalized_margin:
            return True
    return False
