# -*- coding: utf-8 -*-
"""設定的【單一事實來源】:每個設定檔的原廠預設值,以及「還原預設」的執行邏輯。

【為什麼要有這支】
原本「預設值」散在四個地方:`threshold_policy.DEFAULT_THRESHOLDS`、`app_settings` 的
幾個 DEFAULT_*、`main.py` 的 `F8_QUICK_TEXT_DEFAULT`、以及設定頁 UI 裡零星的
`.get(key, False)`。想知道「這個設定的原廠值是什麼」得翻四個檔;要加一個新設定
得同時改預設常數、載入器、`save_all_settings`、UI 建構四處,而且沒有任何機制保證
它們一致 —— 漏一處就是「存了但下次讀不回來」或「還原預設漏掉這一項」。

【擴充規約(改設定時只要照這兩條做)】
  * 新增一個**設定鍵** → 加進對應的 `DEFAULT_*` dict 即可。
    載入(merge_defaults)、還原預設、預設值摘要三件事自動涵蓋。
  * 新增一個**設定檔** → 在 `SETTINGS_GROUPS` 加一個 `SettingsGroup`。
    設定頁的「還原預設」對話框會自動長出那一項,不必改 UI 程式碼。

【邊界】本模組只描述「原廠預設是什麼」與「怎麼還原」,不碰 UI、不決定何時還原。
不依賴 main.py(避免循環 import);main.py 反過來 import 這裡。
"""
from __future__ import annotations

import copy
import logging
import os
import shutil
import time
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from cmuh_common.atomic_io import atomic_write_json
from cmuh_common.threshold_policy import DEFAULT_THRESHOLDS

# ─── 原廠預設值 ────────────────────────────────────────────────────────────
# F8 快速輸入文字。[2026-07-27] 從 main.py 搬來這裡:它是一個「設定的預設值」,
# 放在 16,000 行的 main.py 中段等於沒人找得到。main.py 仍以同名 import 沿用。
F8_QUICK_TEXT_DEFAULT = "A126585189"

# 勿擾窗(不跳彈窗的時段)。與 main.py 的 NOTIFY_DO_NOT_DISTURB_* 同值。
DEFAULT_NOTIFY_DND_START_HOUR = 0
DEFAULT_NOTIFY_DND_END_HOUR = 8

# [2026-07-27 使用者] 止掛提醒的原廠收件人。
# ★注意★ 這是「檔案裡【沒有】這個鍵時才套用」的預設(load_json_dict_ex 的
#   merge 語意是 base.update(file) → 檔案有鍵就以檔案為準)。使用者若刻意把收件人
#   清空,檔案裡會是 `[]`(鍵存在)→ 不會被這個預設復活。這點很重要:同一個坑
#   在 smtp_mail 的帳密快取上踩過一次(「成功讀到空設定要無條件清快取」)。
DEFAULT_ALERT_EMAIL_RECIPIENTS = [
    "lai.i.chang.58@gmail.com",
    "expertise88864@gmail.com",
    "chilly840724@gmail.com",
    "mbpushowo@gmail.com",
]

# [2026-08-02 使用者定案] 「這種系統提示錯誤的信件直接寄給開發者email」。
# ★這推翻了 2026-07-25 的定案(會診連續失敗告警寄給團隊名單)★ —— 使用者實際收到
# 那封信之後認為系統故障訊息不該騷擾整組臨床人員,改為只寄開發者本人。
# 【單一宣告處】main.py 與 consult_query.py 都從這裡取,不各自硬編碼一份。
#
# 界線:「系統/自動化故障」→ 開發者(本常數)。
#       「臨床事件」(止掛達門檻、會診查詢結果、email 觸發的回信)→ 各自的收件人名單。
DEVELOPER_ALERT_EMAIL = "expertise88864@gmail.com"


def developer_alert_recipients() -> list:
    """系統/自動化故障告警的收件人 = 開發者本人(每次回新 list,呼叫端可安全修改)。"""
    return [DEVELOPER_ALERT_EMAIL]


def default_threshold_settings() -> dict:
    """threshold_settings.json 的完整原廠預設。

    這支檔案是個大雜燴(門檻、寄信收件人、F8 文字、字體縮放、勿擾、各種開關),
    歷史上沒有一份完整的預設宣告 —— 「還原預設」若只還原門檻就會留下半套狀態。
    """
    out = dict(DEFAULT_THRESHOLDS)
    out.update({
        # [2026-08-05] 新加的止掛對象預設【開】——不然使用者要求的提醒要再去勾一次才會動。
        # 只有三晚(預設 100)有門檻,一早/一午/三午沒填數字前不會提醒。
        "alert_shen_enabled": True,
        "alert_chen_enabled": False,
        "out_of_hospital_mode": False,
        "ui_font_scale": 1.0,
        "notify_dnd_start_hour": DEFAULT_NOTIFY_DND_START_HOUR,
        "notify_dnd_end_hour": DEFAULT_NOTIFY_DND_END_HOUR,
        "notify_dnd_start_time": f"{DEFAULT_NOTIFY_DND_START_HOUR:02d}:00",
        "notify_dnd_end_time": f"{DEFAULT_NOTIFY_DND_END_HOUR:02d}:00",
        "quick_text_f8": F8_QUICK_TEXT_DEFAULT,
        "alert_email_recipients": list(DEFAULT_ALERT_EMAIL_RECIPIENTS),
    })
    return out


# ─── 設定群組(還原預設的最小單位)────────────────────────────────────────
@dataclass(frozen=True)
class SettingsGroup:
    """一個設定檔 = 一個可獨立還原的群組。

    key:      穩定識別碼。測試與呼叫端用它,【不要】用 label(顯示文字會改)。
    label:    設定頁對話框顯示的名稱。
    filename: settings/ 底下的檔名。
    factory:  回傳一份全新的原廠預設(每次都要新物件,避免呼叫端改到共用 dict)。
    summarize:把預設值變成一行人話,給確認對話框用(讓使用者按下去之前看得到後果)。
    """
    key: str
    label: str
    filename: str
    factory: Callable[[], Any]
    summarize: Callable[[Any], str]


def _summarize_r_doctor(value: Any) -> str:
    if not isinstance(value, dict):
        return "(格式異常)"
    return "、".join(f"{k}={(v or {}).get('name', '')}"
                     for k, v in sorted(value.items()))


def _summarize_doctors(value: Any) -> str:
    if not isinstance(value, list):
        return "(格式異常)"
    names = [str(d.get("name", "")) for d in value if isinstance(d, dict)]
    head = "、".join(names[:4])
    return f"共 {len(names)} 位({head}{'…' if len(names) > 4 else ''})"


def _summarize_thresholds(value: Any) -> str:
    if not isinstance(value, dict):
        return "(格式異常)"
    rcpt = value.get("alert_email_recipients") or []
    return (f"止掛門檻 {len(DEFAULT_THRESHOLDS)} 項回原廠值、"
            f"止掛提醒收件人={('、'.join(rcpt) if rcpt else '(空)')}、"
            f"F8 文字={value.get('quick_text_f8', '')}、"
            f"字體縮放={value.get('ui_font_scale', 1.0)}")


def _summarize_auto_reboot(value: Any) -> str:
    if not isinstance(value, dict):
        return "(格式異常)"
    return (f"自動重開機={'啟用' if value.get('enabled') else '關閉'}、"
            f"時間={value.get('time', '')}")


def _default_r_doctor_settings() -> dict:
    # 延遲 import:app_settings 會 import 本模組的 F8 預設,直接在頂層互相 import
    # 會形成循環。這個預設本身依「生效日」而變(住院醫師升年),必須現算不可快取。
    from cmuh_common.app_settings import default_r_doctor_settings
    return copy.deepcopy(default_r_doctor_settings(date.today()))


def _default_doctors() -> list:
    from cmuh_common.app_settings import DEFAULT_DOCTOR_SETTINGS
    return copy.deepcopy(DEFAULT_DOCTOR_SETTINGS)


def _default_auto_reboot() -> dict:
    from cmuh_common.app_settings import DEFAULT_AUTO_REBOOT_SETTINGS
    return copy.deepcopy(DEFAULT_AUTO_REBOOT_SETTINGS)


SETTINGS_GROUPS: tuple = (
    SettingsGroup(
        key="doctors",
        label="門診醫師代號設定",
        filename="doctors.json",
        factory=_default_doctors,
        summarize=_summarize_doctors,
    ),
    SettingsGroup(
        key="thresholds",
        label="止掛門檻 / 止掛提醒收件人 / F8 文字 / 介面",
        filename="threshold_settings.json",
        factory=default_threshold_settings,
        summarize=_summarize_thresholds,
    ),
    SettingsGroup(
        key="r_doctor",
        label="R1-R3 值班對照姓名",
        filename="r_doctor_settings.json",
        factory=_default_r_doctor_settings,
        summarize=_summarize_r_doctor,
    ),
    SettingsGroup(
        key="auto_reboot",
        label="自動重開機",
        filename="auto_reboot_settings.json",
        factory=_default_auto_reboot,
        summarize=_summarize_auto_reboot,
    ),
)


def group_keys() -> list:
    return [g.key for g in SETTINGS_GROUPS]


def group(key: str) -> SettingsGroup:
    for g in SETTINGS_GROUPS:
        if g.key == key:
            return g
    raise KeyError(f"未知的設定群組:{key}")


def default_for(key: str) -> Any:
    """該群組的原廠預設(每次都是新物件,呼叫端可安全修改)。"""
    return group(key).factory()


def describe(key: str) -> str:
    """該群組還原後會變成什麼樣(一行人話,給確認對話框)。"""
    g = group(key)
    try:
        return g.summarize(g.factory())
    except Exception:
        logging.debug("[settings] 摘要產生失敗 group=%s", key, exc_info=True)
        return "(無法產生摘要)"


# ─── 還原預設 ──────────────────────────────────────────────────────────────
@dataclass
class RestoreReport:
    """還原結果。呼叫端據此決定要不要重載記憶體/更新畫面。"""
    restored: list          # [(group_key, filename)]
    backups: list           # [(group_key, backup_path)]
    failures: list          # [(group_key, 人話原因)]

    @property
    def ok(self) -> bool:
        return not self.failures


def _backup_existing(target: str) -> str:
    """把現有檔案另存一份再覆蓋。回傳備份路徑;沒有原檔則回空字串。

    ★一定要備份★ 這是使用者手動按下的破壞性動作,而且是【不可逆】的 ——
    醫師代號/門檻/收件人重打一次很痛苦。備份名含 PID,避免同秒兩個 process
    產生同名而互相覆蓋(doctors.json 的 .invalid 備份踩過同一個坑)。
    """
    if not os.path.exists(target):
        return ""
    ts = time.strftime("%Y%m%d_%H%M%S")
    dest = f"{target}.before-reset-{ts}-{os.getpid()}"
    if os.path.exists(dest):          # 同秒同 process 連按兩次 → 不覆蓋既有備份
        return dest
    # ★[2026-08-02 補審 P2] 用【複製】而不是搬移★
    #   原本是 os.replace(target, dest):正式檔在寫入預設值【之前】就消失了。
    #   若接著 atomic_write_json 因磁碟滿/權限/暫時鎖定而失敗,使用者的設定檔
    #   就整個不見 —— 而 main 仍會重載設定,loader 對缺檔一律套預設,
    #   等於「還原失敗」卻造成了比還原更嚴重的後果(下次啟動也繼續用預設)。
    #   複製之後,寫入失敗時正式檔原封不動。
    # ★[2026-08-02 補審第 2 次] copy2 不是原子的★
    #   中途失敗會留下一個【半截】的 dest;同秒同 PID 再按一次時,上面的
    #   `os.path.exists(dest)` 會把那個半截檔當成有效備份而直接返回,接著就用
    #   預設值覆蓋正式檔 —— 使用者的設定實際上沒有任何可用備份。
    #   故:先寫暫存、再原子改名;失敗就把暫存清掉,絕不留下半截。
    # ★[2026-08-01 外審 P1] 備份的「年齡」要從備份【建立時間】算，不是原檔的★
    #   `copy2` 會連同 mtime 一起複製，`os.replace` 也不會改它 —— 於是一個 180 天
    #   沒動過的設定檔，今天備份出來的 `.before-reset-*` mtime 仍是 180 天前。
    #   而 RetentionSweeper 一律用 `os.path.getmtime` 判齡、這類備份的 TTL 是 90 天
    #   → **今天備份、下一次掃描就被刪掉**，使用者按了還原預設之後其實沒有退路。
    #   `os.utime(dest, None)` 把 mtime 設成現在，讓「備份保留 90 天」名副其實。
    tmp = f"{dest}.partial"
    try:
        shutil.copy2(target, tmp)
        os.replace(tmp, dest)
        try:
            os.utime(dest, None)
        except OSError:
            # 設不到時間不致命（備份內容還在），但要說出來：這份備份會被提早清掉
            logging.warning("[還原預設] 備份 %s 的時間戳設定失敗 → 它可能被保留期"
                            "掃描提早清除", os.path.basename(dest), exc_info=True)
    except OSError:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        raise
    return dest


def restore_defaults(keys, *, conf_path: Callable[[str], str],
                     backup: bool = True) -> RestoreReport:
    """把指定群組的設定檔還原成原廠預設。不拋例外。

    conf_path: 檔名 → 完整路徑(注入以便測試;正式呼叫傳 paths.get_conf_path)。

    ★不套用「讀不到就拒絕存檔」那道守衛★
      `save_all_settings` 有一道保護:本次啟動若曾讀不到設定檔(防毒鎖檔),就拒絕
      存檔,以免把使用者的設定覆蓋成預設。那道守衛防的是【無意間】覆蓋。
      本函式正好相反 —— 使用者【明確要求】覆蓋成預設,而且我們先備份了原檔。
      硬套那道守衛只會讓「檔案壞掉想重置」的情況永遠救不回來。
    """
    report = RestoreReport(restored=[], backups=[], failures=[])
    for key in keys:
        try:
            g = group(key)
        except KeyError as e:
            report.failures.append((str(key), f"未知的設定群組({e})"))
            continue
        target = conf_path(g.filename)
        try:
            if backup:
                path = _backup_existing(target)
                if path:
                    report.backups.append((g.key, path))
        except OSError as e:
            # 備份失敗就【不要】覆蓋 —— 沒有退路的破壞性寫入不可接受
            report.failures.append(
                (g.key, f"備份原檔失敗,為安全起見未還原({e})"))
            continue
        try:
            # atomic_write_json 的契約是「成功回 None、失敗丟例外」——
            # 不可寫成 `if not atomic_write_json(...)`,那會把每一次成功都當成失敗。
            atomic_write_json(target, g.factory())
            report.restored.append((g.key, g.filename))
        except Exception as e:
            logging.error("[settings] 還原預設失敗 group=%s", g.key, exc_info=True)
            report.failures.append((g.key, f"還原時發生例外({e})"))
    return report
