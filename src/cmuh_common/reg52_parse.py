# -*- coding: utf-8 -*-
"""reg52 / 院方掛號頁的 HTML 解析器（P2-06 分層第四刀 2026-08-01）。

【為什麼是這一族】
量過之後，這 10 個 `_parse_*` **全部是純函式** —— 只依賴幾條 regex 常數與彼此，
沒有任何可變的模組級狀態。它們吃 BeautifulSoup 節點、吐資料結構，是整個 reg52
子系統裡最容易測、也最值得測的一層（掛號數、休診、止掛都是從這裡讀出來的）。

★這一刀順便推翻了施工計畫書自己的建議★
計畫書原本寫「reg52 那一族有 42 個全域，要先收斂成 context 物件再搬」。實際量了
組成之後：23 個是常數、29 個是函式呼叫，**真正的可變狀態只有 2 個**
（`AUH_DOCTOR_DOCNO_MAP` 與一個 semaphore），而且都不在解析器這一層。
那個建議是看數量、沒看組成得出來的 —— 已在計畫書更正。

【搬移原則】原文一字不改（用腳本搬，不手抄）。註解裡每一段 `[日期 來源]`
都是踩過的坑，例如：
  * `parse_main_hospital_schedule` 的「止掛是單一日期的狀態」——
    用整格文字判斷會讓同格其他日期的止掛提醒被靜默吃掉（2026-07-26 審查）。
  * `safe_parse_roc_date` 對壞日期【拋例外】而不是回 None —— 呼叫端逐格 try，
    一格壞掉不可以讓整張表消失。

【呼叫端】main.py 用 `import 新名 as 舊私有名` —— 只搬家、不改呼叫端。
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

# 這三個都是 appt_utils 已經擁有的東西（reg52 解析的同一家）——
# 其中 split_schbox_by_date 正是第三刀從 main.py 搬過去的。
from cmuh_common.appt_utils import (
    _appt_dict_ext_branch as _appt_dict_ext_branch,
    _normalize_dayoff_session as _normalize_dayoff_session,
    split_schbox_by_date as _split_schbox_by_date,
)


# --- 模組級 Regex 常數 (只 compile 一次，避免每次呼叫重複編譯) ---
_RE_COUNT_DIGIT = re.compile(r'(\d+)')          # 用於 _update_grid_data 計算人數

_RE_ROOM        = re.compile(r'\(([A-Za-z0-9]+診)\)')  # 診間號:含字母前綴(如 A101診)+純數字(101診)

                                                 # [2026-06-19] 原本只配 \d+診 → 漏掉含字母前綴的診間(如 A101診)→ 止掛信顯示「診間未提供」
_RE_COUNT_APPT  = re.compile(r'已掛號：(\d+)')   # 用於 check_appointment_count 掛號數

_RE_PERSON      = re.compile(r'(\d+)\s*人')      # 用於 check_appointment_count 人數

# ★[2026-08-02 外部 code review P2-03] 分院週表的數值上限★
#   東區與惠盛走【明文 HTTP】（見 `reg52_fetch.PLAINTEXT_REG52_SOURCES`），
#   回應在院內網路上可被改寫。這兩個上限刻意訂得遠高於任何真實值 ——
#   一診半天不可能超過 2000 人，診間號不會超過 12 個字 —— 目的不是「校正資料」，
#   而是不讓被塞進來的荒謬值變成假的止掛提醒或撐爆版面。
#   ★不在 regex 裡限位數★ `(\d{1,4})` 遇到 12345 會配到 1234 而【看起來成功】，
#   那比不限制更糟：錯誤的值會安靜地通過。先照原樣抓，再檢查範圍。
_MAX_PLAUSIBLE_APPT_COUNT = 2000
_MAX_ROOM_LEN = 12


_RE_ROC_DATE    = re.compile(r'(\d{2,3})/(\d{2})/(\d{2})')

# [O16] reg52 hot-path 預編譯：原本散落在函式內的 inline re.search/findall，集中宣告省 compile 開銷
#
# ★[2026-08-01 P2-06 第四刀] 數量不可以貪婪吃掉下一個日期★
#   `parse_auh_reg52_schedule` 的退路先把整列文字的空白【全部拿掉】才做 findall
#   （為了讓「上 午」這種寫法也認得出來）。於是一列裡相鄰的兩組會黏在一起：
#       "115/08/03 已掛號：8 115/08/10 已掛號：11"
#     → "115/08/03已掛號：8115/08/10已掛號：11"
#   舊的 `(\d+)` 是貪婪的，會把 `8115` 整段當成數量 —— 而且把下一個日期的
#   `115` 吃掉之後，那一天就再也配不到了。實測一列三個日期時：
#       舊：[('115/08/03', '8115'), ('115/08/17', '3')]   ← 數量錯、中間那天消失
#       新：[('115/08/03', '8'), ('115/08/10', '11'), ('115/08/17', '3')]
#   亞大(AUH)的週表一列就是一個診別、欄位是一週各天 —— 也就是說這條路徑上的
#   掛號人數【一直都是錯的】，而且沒有任何跡象（解析成功、有數字、只是不對）。
#   改成非貪婪 + 前瞻：下一段若是「日期」或非數字或字串結尾就停。
#   ★這是本刀唯一的行為改變★（其餘都是原文搬家）。
_RE_REG52_DATE_CNT_PAIRS = re.compile(
    r'(\d{2,3}/\d{2}/\d{2})\s*已掛號[：:]\s*(\d+?)(?=\d{2,3}/|\D|$)')


def _bounded_count(digits: str, where: str):
    """把頁面上抓到的數字字串轉成掛號數。

    → int；★不可信時回 `None`，呼叫端必須略過該格★
    刻意不回 0：0 的意思是「這一診沒有人掛號」，而我們真正知道的是「這一格的
    數字不可信」—— 這兩件事對止掛提醒的意義正好相反。

    先看位數再 `int()`：CPython 對超長數字字串的轉換有上限（>4300 位會丟
    ValueError），先擋位數才不會在這裡爆掉。
    """
    if len(digits) > 4 or int(digits) > _MAX_PLAUSIBLE_APPT_COUNT:
        # ★只記位數，不記值也不記頁面文字★（見 reg52_fetch 的威脅模型說明）
        logging.warning("%s：略過不合理的掛號數（%d 位數）", where, len(digits))
        return None
    return int(digits)


def _bounded_room(raw: str, where: str) -> str:
    """診間號的長度上限。過長的一律當成「沒有診間」。

    ★不截斷★ 截斷會生出一個看起來合理、實際上是編造的診間號，而診間號會被
    印在止掛通知裡 —— 寧可說「未提供」，不要說一個錯的。
    """
    room = raw or ""
    if len(room) > _MAX_ROOM_LEN:
        logging.warning("%s：忽略過長的診間號（%d 字）", where, len(room))
        return ""
    return room


def safe_parse_roc_date(roc_date_str):
    match = _RE_ROC_DATE.search(roc_date_str or "")
    if not match:
        raise ValueError(f"無法解析日期: {roc_date_str}")
    year_part, month_part, day_part = match.groups()
    return datetime(int(year_part) + 1911, int(month_part), int(day_part)).date()


def parse_main_hospital_schedule(soup):
    schedule_table = soup.select_one('table.schedule')
    if not schedule_table:
        # 兼容亞大/其他 reg52：無 table.schedule class，但仍有 timeSlot + schBox 結構
        for tbl in soup.find_all('table'):
            if tbl.select_one('td.timeSlot') and tbl.select_one('td.schBox'):
                schedule_table = tbl
                break
    if not schedule_table:
        return {}

    appointments_by_date = {}
    data_rows = schedule_table.select('tr')[1:]
    for row in data_rows:
        time_slot_cell = row.select_one('td.timeSlot')
        if not time_slot_cell:
            continue

        time_slot_text = ""
        cell_text = time_slot_cell.get_text(strip=True)
        cell_class = time_slot_cell.get('class', [])

        if 'AM' in cell_class or "上午" in cell_text:
            time_slot_text = "上午"
        elif 'PM' in cell_class or "下午" in cell_text:
            time_slot_text = "下午"
        elif 'Night' in cell_class or "晚上" in cell_text or "夜間" in cell_text:
            time_slot_text = "晚上"

        if not time_slot_text:
            continue

        for cell in row.select('td.schBox'):
            cell_content = cell.get_text(strip=True)
            # 東區分院/診間號碼是【整格】的屬性(同一診的所有日期共用),維持整格判斷;
            # 止掛是【單一日期】的狀態,改在下面逐日期算(見 _split_schbox_by_date)。
            is_external = "東區分院" in cell_content
            header_text, date_group_texts = _split_schbox_by_date(cell)

            room_match = _RE_ROOM.search(cell_content)
            room = _bounded_room(room_match.group(1) if room_match else "",
                                 "主院掛號表")

            for date_div in cell.find_all('div', class_='visitDate'):
                own_text = date_group_texts.get(id(date_div))
                stop_scope = (cell_content if own_text is None
                              else header_text + own_text)
                is_stopped = "止掛" in stop_scope
                date_tag = date_div.find('b')
                if not date_tag:
                    continue

                roc_date_str = date_tag.get_text(strip=True)
                count = -1
                count_div = date_div.find_next_sibling('div')

                if count_div:
                    count_text = count_div.get_text()
                    count_match = _RE_COUNT_APPT.search(count_text)
                    if count_match:
                        count = _bounded_count(count_match.group(1), "主院掛號表")
                        if count is None:
                            continue
                    elif "已額滿" in count_text:
                        count = "已額滿"

                if count == -1:
                    content_without_date = cell_content.replace(roc_date_str, "")
                    fallback_match = _RE_PERSON.search(content_without_date)
                    if fallback_match:
                        count = _bounded_count(fallback_match.group(1),
                                               "主院掛號表(人數)")
                        if count is None:
                            continue
                    elif "額滿" in cell_content:
                        count = "已額滿"
                    elif "截止" in cell_content or "過" in cell_content:
                        count = "截止"
                    else:
                        count = 0

                # [review C2 2026-06-12] 與 parse_doctor_info_dayoff 同款防護：
                # 單格日期解析失敗只跳過該格，不可讓整個醫師的班表解析中斷。
                try:
                    date_key = safe_parse_roc_date(roc_date_str)
                except ValueError:
                    logging.debug("班表略過無法解析日期之格: %r", roc_date_str)
                    continue
                appointments_by_date.setdefault(date_key, []).append({
                    'session': time_slot_text,
                    'count': count if count != "截止" else "截止",
                    'is_ext': is_external,
                    'ext_branch': 'east' if is_external else None,
                    'room': room,
                    'is_stopped': is_stopped,
                })
    return appointments_by_date


def parse_doctor_info_dayoff(soup, assume_east_branch=False, assume_huihe_branch=False, assume_huisheng_branch=False, assume_tcmc_branch=False):
    """解析 reg52.cgi（宜使用 DocNo=D…）內之休診表：主院常用 table#dayoff；東區 fh1 常為 width=300 三欄小表。"""
    dayoff_table = soup.select_one("table#dayoff")
    if not dayoff_table:
        for tbl in soup.find_all("table"):
            if str(tbl.get("width") or "").strip() != "300":
                continue
            rows = tbl.find_all("tr")
            if len(rows) < 2:
                continue
            first_data = rows[1].find_all(["td", "th"])
            if len(first_data) != 3:
                continue
            if not _RE_ROC_DATE.search(first_data[0].get_text(" ", strip=True)):
                continue
            dayoff_table = tbl
            break
    if not dayoff_table:
        return {}

    appointments_by_date = {}
    for row in dayoff_table.select('tr')[1:]:
        cells = row.find_all(['td', 'th'])
        if len(cells) < 3:
            continue

        roc_date_str = cells[0].get_text(" ", strip=True)
        session_name = _normalize_dayoff_session(cells[1].get_text(" ", strip=True))
        replacement_text = cells[2].get_text(" ", strip=True) or "休診"
        if not session_name:
            logging.debug(f"停診表略過無法辨識診別之列: {cells[1].get_text(' ', strip=True)!r} / 日期 {roc_date_str!r}")
            continue

        row_joined = " ".join(c.get_text(" ", strip=True) for c in cells)
        if assume_east_branch:
            ext_branch = "east"
        elif assume_huihe_branch:
            ext_branch = "huihe"
        elif assume_huisheng_branch:
            ext_branch = "huisheng"
        elif assume_tcmc_branch:
            ext_branch = "tcmc"
        else:
            ext_branch = "east" if ("東區" in row_joined or "東區分院" in row_joined) else None

        # [stability] 單列日期解析失敗只跳過該列，不要讓整個醫師的休診表解析中斷
        # (某列 cells[0] 可能是子標題/合併格/格式異動 → safe_parse_roc_date raise)。
        try:
            date_key = safe_parse_roc_date(roc_date_str)
        except ValueError:
            logging.debug("停診表略過無法解析日期之列: %r", roc_date_str)
            continue
        appointments_by_date.setdefault(date_key, []).append({
            'session': session_name,
            'count': replacement_text,
            'is_ext': ext_branch is not None,
            'ext_branch': ext_branch,
            'room': "",
            'is_stopped': False,
        })
    return appointments_by_date


def _parse_fh_like_weekly_schedule(soup, ext_branch):
    """東區 fh1 / 惠和 wh1 / 惠盛 hs1 週表：無 table.schedule，診別常見「上 午」空格；以列首 + visitDate 解析。"""
    appointments_by_date = {}
    for row in soup.find_all("tr"):
        cells = row.find_all(["td", "th"])
        if not cells:
            continue
        slot_norm = cells[0].get_text(" ", strip=True).replace(" ", "").replace("\u3000", "")
        if "上午" in slot_norm or "早診" in slot_norm:
            session_name = "上午"
        elif "下午" in slot_norm:
            session_name = "下午"
        elif "晚" in slot_norm or "夜間" in slot_norm:
            session_name = "晚上"
        else:
            continue

        for cell in cells[1:]:
            cell_text = cell.get_text(" ", strip=True)
            room_match = _RE_ROOM.search(cell_text)
            room = _bounded_room(room_match.group(1) if room_match else "",
                                 f"分院週表({ext_branch})")

            for date_div in cell.find_all("div", class_="visitDate"):
                date_tag = date_div.find("b")
                if not date_tag:
                    continue

                roc_date_str = date_tag.get_text(strip=True)
                count_text = date_div.get_text(" ", strip=True)
                if "休診" in count_text or "停診" in count_text:
                    count = "休診"
                elif "已額滿" in count_text:
                    count = "已額滿"
                elif "截止" in count_text or "過" in count_text:
                    count = "截止"
                else:
                    count_match = _RE_COUNT_APPT.search(count_text)
                    count = 0
                    if count_match:
                        count = _bounded_count(count_match.group(1),
                                               f"分院週表({ext_branch})")
                        if count is None:
                            continue

                # [review C2 2026-06-12] 單格日期解析失敗只跳過該格(同 dayoff 解析防護)
                try:
                    date_key = safe_parse_roc_date(roc_date_str)
                except ValueError:
                    logging.debug("分院週表略過無法解析日期之格: %r", roc_date_str)
                    continue
                appointments_by_date.setdefault(date_key, []).append({
                    "session": session_name,
                    "count": count if count != "截止" else "截止",
                    "is_ext": True,
                    "ext_branch": ext_branch,
                    "room": room,
                    "is_stopped": False,
                })
    return appointments_by_date


def parse_east_fh1_schedule(soup):
    """東區 61.66.117.10 fh1/reg52 週表。"""
    return _parse_fh_like_weekly_schedule(soup, "east")


def parse_huihe_schedule(soup):
    """惠和 appointment.cmuh.org.tw wh1/reg52 週表。"""
    return _parse_fh_like_weekly_schedule(soup, "huihe")


def parse_huisheng_schedule(soup):
    """惠盛 61.66.117.10 hs1/reg52 週表。"""
    return _parse_fh_like_weekly_schedule(soup, "huisheng")


def parse_tcmc_schedule(soup):
    """老人醫院 appointment.cmuh.org.tw tcmc/reg52 週表。

    ★版型未經實機驗證★(見 `reg52_fetch._fetch_tcmc_reg52_html` 的說明):
    同主機同一支 CGI 家族,東區/惠和/惠盛三個分院路徑都是這個版型。
    版型不合時這裡回空 dict —— 不會產生錯的人數。
    """
    return _parse_fh_like_weekly_schedule(soup, "tcmc")


def parse_branch_schedule(soup):
    form = soup.find('form', attrs={'name': 'FrontPage_Form1'})
    if not form:
        return {}

    schedule_table = None
    for table in form.find_all('table'):
        first_row = table.find('tr')
        if first_row and "星期一" in table.get_text():
            schedule_table = table
            break
    if not schedule_table:
        return {}

    appointments_by_date = {}
    rows = schedule_table.find_all('tr')
    for row in rows[1:]:
        cells = row.find_all('td')
        if len(cells) < 2:
            continue

        slot_label = cells[0].get_text(" ", strip=True)
        if "上午" in slot_label or "早診" in slot_label:
            session_name = "上午"
        elif "下午" in slot_label:
            session_name = "下午"
        elif "晚" in slot_label or "夜間" in slot_label:
            session_name = "晚上"
        else:
            continue

        for cell in cells[1:]:
            room_match = _RE_ROOM.search(cell.get_text(" ", strip=True))
            room = _bounded_room(room_match.group(1) if room_match else "",
                                 "東區週表")

            for date_div in cell.find_all('div', class_='visitDate'):
                date_tag = date_div.find('b')
                if not date_tag:
                    continue

                roc_date_str = date_tag.get_text(strip=True)
                count_text = date_div.get_text(" ", strip=True)
                if "已額滿" in count_text:
                    count = "已額滿"
                else:
                    count_match = _RE_COUNT_APPT.search(count_text)
                    count = 0
                    if count_match:
                        count = _bounded_count(count_match.group(1), "東區週表")
                        if count is None:
                            continue

                # [review C2 2026-06-12] 單格日期解析失敗只跳過該格(同 dayoff 解析防護)
                try:
                    date_key = safe_parse_roc_date(roc_date_str)
                except ValueError:
                    logging.debug("東區週表略過無法解析日期之格: %r", roc_date_str)
                    continue
                appointments_by_date.setdefault(date_key, []).append({
                    'session': session_name,
                    'count': count,
                    'is_ext': True,
                    'ext_branch': 'east',
                    'room': room,
                    'is_stopped': False,
                })
    return appointments_by_date


def parse_auh_reg52_schedule(soup):
    out = {}
    parsed = parse_main_hospital_schedule(soup)
    for d, rows in parsed.items():
        for row in rows:
            rec = dict(row)
            rec["is_ext"] = True
            rec["ext_branch"] = "auh"
            out.setdefault(d, []).append(rec)
    if out:
        return out

    # 亞大 reg52 常見版型：無 timeSlot/schBox class，改以每列文字做日期+人數擷取
    for tr in soup.find_all("tr"):
        txt = tr.get_text(" ", strip=True)
        if not txt:
            continue
        txt_norm = txt.replace(" ", "").replace("\u3000", "")
        if ("上午" in txt_norm) or ("早診" in txt_norm):
            session_name = "上午"
        elif "下午" in txt_norm:
            session_name = "下午"
        elif ("晚上" in txt_norm) or ("夜間" in txt_norm) or ("晚診" in txt_norm):
            session_name = "晚上"
        else:
            continue

        pairs = _RE_REG52_DATE_CNT_PAIRS.findall(txt_norm)  # [O16] precompiled
        for roc_date_str, count_str in pairs:
            try:
                d = safe_parse_roc_date(roc_date_str)
            except Exception:
                continue
            out.setdefault(d, []).append({
                "session": session_name,
                "count": int(count_str),
                "is_ext": True,
                "ext_branch": "auh",
                "room": "",
                "is_stopped": False,
            })
    return out


# =============================================================================
# 止掛提醒寄信（Outlook COM，在獨立執行緒+逾時，避免卡到主迴圈）
# =============================================================================
def parse_appt_item_for_alert(appt_item):
    """把快取的門診項目正規化成 (session_name, count, is_stopped, ext_branch, room)。

    純函式(好測):供止掛背景掃描用,與 _update_grid_data 內的解析同源(dict 新格式 +
    舊字串格式)。休診/停診/取不到人數 → 回 None(不是「0 人」,不可拿去比門檻)。"""
    if isinstance(appt_item, dict):
        session_name = str(appt_item.get("session", ""))
        raw_count = appt_item.get("count", 0)
        is_stopped = bool(appt_item.get("is_stopped", False))
        ext_branch = _appt_dict_ext_branch(appt_item)
        room = str(appt_item.get("room", "") or "")
        status_text = str(raw_count) + ("人" if isinstance(raw_count, int) else "")
    else:
        text = str(appt_item)
        parts = text.split("|")
        status_part = parts[0]
        ext_branch = None
        is_stopped = False
        room = ""
        for p in parts[1:]:
            if p.startswith("Ext:"):
                val = p.split(":", 1)[1]
                ext_branch = {"1": "east", "east": "east", "auh": "auh",
                              "huihe": "huihe", "huisheng": "huisheng",
                              "tcmc": "tcmc"}.get(val)
            elif p.startswith("Rm:"):
                room = p.split(":", 1)[1]
            elif p.startswith("Stop:"):
                is_stopped = (p.split(":", 1)[1] == "1")
        if ":" not in status_part:
            return None
        session_name, status_text = status_part.split(":", 1)
        status_text = status_text.strip()
    if not session_name:
        return None
    if "休診" in status_text or "停診" in status_text:
        return None
    m = _RE_COUNT_DIGIT.search(status_text)
    if not m:
        return None
    # ★[2026-08-02 P2-03] 這一支是守衛掃出來的第四個入口★
    #   我原本只改了三個 `_RE_COUNT_APPT` 的地方，漏了這裡（用的是
    #   `_RE_COUNT_DIGIT`）—— 而它正是【止掛提醒】讀掛號數的那一支，
    #   也就是被塞荒謬值最有感的地方。呼叫端本來就處理 None，語意相容。
    count = _bounded_count(m.group(1), "止掛提醒")
    if count is None:
        return None
    return (session_name, count, is_stopped, ext_branch, room)
