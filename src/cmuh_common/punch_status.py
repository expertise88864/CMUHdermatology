# -*- coding: utf-8 -*-
"""共用:查詢醫院電子打卡 portal 今日上/下班紀錄(供會診信件等跨行程使用)。

設計:
- 純粹的「時間窗判定 / 排班判定 / 分類」抽成純函式(無 selenium 相依),方便單元測試。
- 實際登入讀表用 selenium,**延後 import**(模組 import 時不需要 selenium),且自建
  headless Chrome → 任何行程都能用、不會閃出視窗。
- 完全 fail-open:driver 建不出/單帳號失敗都不丟例外,回傳帶 error 的結果。

資料來源 = 打卡 portal 的 Gv_attppre 表(今日真實上/下班紀錄,含手動打卡),邏輯改寫
自主程式已驗證的 _get_swipe_status_from_web。
"""
from __future__ import annotations

import logging
import socket
import time as _time
from datetime import date as _date, datetime, time as dt_time
from typing import Optional

PUNCH_HOST = "10.20.8.47"
PUNCH_LOGIN_URL = f"http://{PUNCH_HOST}/peoplesystem/electron_card/login.aspx"


def portal_reachable(host: str = PUNCH_HOST, port: int = 80,
                     timeout: float = 2.5) -> bool:
    """快速 TCP 連線測打卡 portal 是否可達(院外/內網斷線時 2.5s 內就回 False,
    免得後面 Chrome 一個個帳號各等十幾秒才逾時、拖慢寄信)。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False

# datetime.weekday(): 0=Mon .. 6=Sun。打卡設定檔的排班 key 用這些前綴(無 sun=週日不排)。
_DAY_ABBR = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

# 狀態三態
PUNCH_OK = "ok"       # 該打且有打(在時間窗內)
PUNCH_FAIL = "fail"   # 該打卻沒打到(排了班但時間窗內查無紀錄)
PUNCH_OFF = "off"     # 今日無排此班(不算失敗)


def weekday_prefix(when: Optional[datetime] = None) -> str:
    """今日(或指定時間)對應打卡設定檔排班 key 的星期前綴('mon'..'sun')。純函式。"""
    return _DAY_ABBR[(when or datetime.now()).weekday()]


def _hhmm_to_time(s) -> Optional[dt_time]:
    """'0815' / '815' → dt_time(8,15)。不合法回 None。純函式。"""
    try:
        s = str(s).strip().zfill(4)
        return dt_time(int(s[:2]), int(s[2:4]))
    except Exception:
        return None


def swipe_in_window(swipes, target_type: str, start: dt_time, end: dt_time) -> Optional[str]:
    """swipes=[(hhmm_str, type_str)]。回「第一筆 target_type 落在 [start,end] 的時間
    字串 HH:MM」,沒有則 None。純函式。"""
    for t_str, typ in swipes:
        if typ != target_type:
            continue
        st = _hhmm_to_time(t_str)
        if st is not None and start <= st <= end:
            s = str(t_str).strip().zfill(4)
            return f"{s[:2]}:{s[2:4]}"
    return None


def scheduled_today(schedule: dict, keys, when: Optional[datetime] = None) -> bool:
    """今日是否排了 keys 中任一班別(keys 如 ('am_in','midday_in'))。schedule 為打卡設定
    檔內某帳號的 schedule dict(key 形如 'mon_am_in')。純函式。"""
    if not isinstance(schedule, dict):
        return False
    day = weekday_prefix(when)
    for k in keys:
        if bool(schedule.get(f"{day}_{k}")):
            return True
    return False


def classify(scheduled: bool, detected: bool) -> str:
    """排班 + 是否偵測到打卡 → 三態。純函式。
    排班且有打=OK;排班卻沒打=FAIL;沒排班=OFF(不算失敗)。"""
    if not scheduled:
        return PUNCH_OFF
    return PUNCH_OK if detected else PUNCH_FAIL


# ── 班別 → 排班 key / 打卡型別 / 時間窗 的對應(會診信件用) ──────────────────
# 上班:涵蓋「早上上班 am_in」與「中午上班 midday_in」,任一排班即視為今日需上班;
#       portal 的上班打卡型別字串為 "上班"。
# 下班:對應 pm_out(17:00-17:30);型別字串為 "下班"。
ON_DUTY_SCHEDULE_KEYS = ("am_in", "midday_in")
OFF_DUTY_SCHEDULE_KEYS = ("pm_out",)
ON_PUNCH_TYPE = "上班"
OFF_PUNCH_TYPE = "下班"


def evaluate_account(schedule: dict, swipes, am_window, pm_window,
                     when: Optional[datetime] = None) -> dict:
    """依排班 + 今日 swipes 算出某帳號的上/下班三態與時間。純函式(無 selenium)。
    am_window/pm_window = (dt_time, dt_time)。回:
      {'on': 三態, 'on_time': 'HH:MM'|None, 'off': 三態, 'off_time': 'HH:MM'|None}"""
    on_sched = scheduled_today(schedule, ON_DUTY_SCHEDULE_KEYS, when)
    off_sched = scheduled_today(schedule, OFF_DUTY_SCHEDULE_KEYS, when)
    on_time = swipe_in_window(swipes, ON_PUNCH_TYPE, am_window[0], am_window[1])
    off_time = swipe_in_window(swipes, OFF_PUNCH_TYPE, pm_window[0], pm_window[1])
    return {
        "on": classify(on_sched, on_time is not None),
        "on_time": on_time,
        "off": classify(off_sched, off_time is not None),
        "off_time": off_time,
    }


# ── selenium 讀表(延後 import) ─────────────────────────────────────────────
def read_today_swipes(driver, username: str, password: str, *,
                      login_url: str = PUNCH_LOGIN_URL, wait_sec: int = 15):
    """用已建好的 driver 登入並讀今日 Gv_attppre。回 (swipes, error)。
    swipes=[(hhmm_str, type_str)] 只含今日;error 非 None 表失敗(swipes 為空)。
    改寫自主程式 _get_swipe_status_from_web 的登入/解析。"""
    try:
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.common.exceptions import (
            TimeoutException, StaleElementReferenceException,
            UnexpectedAlertPresentException,
        )
        from selenium.webdriver.common.keys import Keys
        import time as _time
    except Exception as e:  # noqa: BLE001
        return [], f"selenium 不可用:{e}"

    wait = WebDriverWait(driver, wait_sec)
    try:
        # [2026-06-22] 多帳號共用同一個 driver 逐一查 → 先清掉上一個帳號殘留的登入 session,
        # 否則 portal 可能仍認得上一個帳號、login.aspx 被導走 → 這個帳號登入逾時(實機:同一批
        # 信件固定那幾個帳號「登入逾時/失敗」)。首個帳號尚未導頁時 delete 會丟例外,忽略即可。
        try:
            driver.delete_all_cookies()
        except Exception:
            logging.debug("[punch] delete_all_cookies 失敗(首帳號/尚未導頁,可忽略)", exc_info=True)
        driver.get(login_url)
        try:
            user_elem = wait.until(EC.element_to_be_clickable((By.ID, "TB_logid")))
            user_elem.clear()
            user_elem.send_keys(username)
            user_elem.send_keys(Keys.TAB)   # 觸發 PostBack 刷新
        except Exception:
            logging.debug("[punch] 輸入帳號失敗", exc_info=True)
        _time.sleep(0.5)
        try:
            pwd_elem = wait.until(EC.visibility_of_element_located((By.ID, "TB_pwd")))
            pwd_elem.clear()
            pwd_elem.send_keys(password)
        except StaleElementReferenceException:
            _time.sleep(1)
            pwd_elem = driver.find_element(By.ID, "TB_pwd")
            pwd_elem.clear()
            pwd_elem.send_keys(password)

        login_ok = False
        for _ in range(2):
            try:
                btn = wait.until(EC.element_to_be_clickable((By.ID, "bt_login")))
                driver.execute_script("arguments[0].click();", btn)
                # [2026-07-06] 登入成功錨點改用 lb_systime,不再等 Gv_attppre。空的 GridView
                # (當日尚無刷卡紀錄)不渲染 → 等 Gv_attppre 逾時 → 誤判「登入逾時/失敗」。
                # lb_systime 在登入後一定存在(空表也在);下方 JS 對不存在的表安全回 []=今日無紀錄。
                WebDriverWait(driver, 10).until(
                    EC.presence_of_element_located((By.ID, "lb_systime")))
                login_ok = True
                break
            except UnexpectedAlertPresentException as e:
                return [], (getattr(e, "alert_text", "") or "登入 Alert")[:30]
            except TimeoutException:
                try:
                    _alert = driver.switch_to.alert
                    _txt = (_alert.text or "").strip()
                    _alert.accept()
                    # [SP-07 2026-07-12] 回實際 alert 文字(而非硬編「帳號/密碼錯誤」)。硬編含「密碼
                    # 錯誤」會讓 _is_retryable_punch_error 判不可重試,暫態問題(逾時彈框)因此失去重試。
                    return [], (_txt[:30] or "登入 Alert")
                except Exception:
                    pass
                try:
                    driver.find_element(By.ID, "bt_login")
                except Exception:
                    break
        if not login_ok:
            return [], "登入逾時/失敗"

        # 系統日期(ROC) → 用來只挑今日的列;失敗退回本機日期
        sys_date = _date.today()
        try:
            txt = driver.find_element(By.ID, "lb_systime").text
            if "年" in txt:
                y = int(txt.split("年")[0])
                m = int(txt.split("年")[1].split("月")[0])
                d = int(txt.split("月")[1].split("日")[0])
                sys_date = _date(y + 1911, m, d)
        except Exception:
            logging.debug("[punch] 解析網站日期失敗,用本機日期", exc_info=True)

        rows = driver.execute_script("""
            var rows = document.querySelectorAll("#Gv_attppre tbody tr");
            var data = [];
            for (var i = 1; i < rows.length; i++) {
                var cols = rows[i].querySelectorAll("td");
                if (cols.length >= 3) {
                    data.push([cols[0].innerText.trim(),
                               cols[1].innerText.trim(),
                               cols[2].innerText.trim()]);
                }
            }
            return data;
        """) or []

        swipes = []
        for row in rows:
            try:
                d_str, t_str, type_str = row
                if len(d_str) == 7:
                    rd = _date(int(d_str[:3]) + 1911, int(d_str[3:5]), int(d_str[5:7]))
                    if rd == sys_date:
                        swipes.append((str(t_str).strip().zfill(4), type_str))
            except Exception:
                continue
        return swipes, None
    except Exception as e:  # noqa: BLE001
        logging.debug("[punch] 讀打卡紀錄失敗", exc_info=True)
        return [], str(e)[:40]


def _is_retryable_punch_error(err) -> bool:
    """單帳號登入失敗,是否值得「清 session 後重試一次」。純函式。
    逾時/連線/一般例外 → 可重試(多半是 portal 當下慢或 session 殘留);
    明確帳密錯誤 / selenium 環境不可用 → 不重試(重試也一樣,只會浪費整批時間預算)。"""
    if not err:
        return False
    e = str(err)
    return not ("密碼錯誤" in e or "selenium 不可用" in e)


def _error_result(username, msg) -> dict:
    return {"username": str(username), "on": None, "on_time": None,
            "off": None, "off_time": None, "error": msg}


def query_accounts_today(accounts, *, am_window, pm_window,
                         when: Optional[datetime] = None,
                         headless: bool = True,
                         time_budget_sec: float = 120.0,
                         page_load_timeout_sec: float = 20.0) -> list:
    """自建一個 headless Chrome,逐帳號登入查今日上/下班狀態。完全 fail-open。
    accounts = [{'username','password','schedule'(可選 dict)}, ...]。
    回每帳號 dict:{'username','on','on_time','off','off_time','error'}。
    am_window/pm_window=(dt_time,dt_time)。任何一帳號失敗只在該筆帶 error,不影響其他。

    韌性:(1) 先 TCP 測 portal 可達(院外/斷線 2.5s 內就全標『連不到』、不啟 Chrome);
    (2) set_page_load_timeout 限制單頁載入;(3) time_budget_sec 為整批上限,超時後剩餘
    帳號標『查詢逾時』並停止 —— 避免 portal 卡住把寄信延後好幾分鐘。"""
    accts = [a for a in (accounts or []) if str(a.get("username", "")).strip()]
    if not accts:
        return []

    if not portal_reachable():
        logging.info("[punch] 打卡 portal 連不到(院外/內網斷線?),跳過打卡查詢")
        return [_error_result(a.get("username", ""), "打卡系統連不到(院外?)")
                for a in accts]

    try:
        from selenium import webdriver
        from cmuh_common.chrome_options import build_chrome_options
    except Exception as e:  # noqa: BLE001
        logging.warning("[punch] selenium/chrome_options 載入失敗,跳過打卡查詢:%s", e)
        return [_error_result(a.get("username", ""), "selenium 不可用") for a in accts]

    results = []
    driver = None
    try:
        try:
            driver = webdriver.Chrome(options=build_chrome_options(headless=headless))
            try:
                driver.set_page_load_timeout(page_load_timeout_sec)
            except Exception:
                logging.debug("[punch] set_page_load_timeout 失敗", exc_info=True)
        except Exception as e:  # noqa: BLE001
            logging.warning("[punch] 建立 Chrome 失敗,跳過打卡查詢:%s", e)
            return [_error_result(a.get("username", ""), "Chrome 啟動失敗") for a in accts]

        deadline = _time.monotonic() + max(10.0, time_budget_sec)
        for a in accts:
            username = str(a.get("username", "")).strip()
            if _time.monotonic() > deadline:
                # 整批已超時 → 剩餘帳號不再查,標逾時(避免無限拖慢寄信)
                results.append(_error_result(username, "查詢逾時(略過)"))
                continue
            password = str(a.get("password", ""))
            schedule = a.get("schedule") if isinstance(a.get("schedule"), dict) else {}
            swipes, err = read_today_swipes(driver, username, password)
            # 登入逾時/連線類失敗 → 清 session(read_today_swipes 開頭會 delete_all_cookies)後重試
            # 一次,但僅在整批時間預算內(避免拖慢寄信);明確帳密錯誤不重試。
            if err and _is_retryable_punch_error(err) and _time.monotonic() < deadline:
                logging.info("[punch] %s 查詢失敗(%s)→ 清 session 重試一次", username, err)
                swipes, err = read_today_swipes(driver, username, password)
            ev = evaluate_account(schedule, swipes, am_window, pm_window, when)
            ev["username"] = username
            ev["error"] = err
            results.append(ev)
    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                logging.debug("[punch] driver.quit 失敗", exc_info=True)
    return results
