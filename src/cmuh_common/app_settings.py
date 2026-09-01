# -*- coding: utf-8 -*-
"""Application settings loaders shared by the main app and scheduler."""
from __future__ import annotations

import logging
import os
import time
from datetime import date
from typing import TYPE_CHECKING

from cmuh_common.atomic_io import atomic_write_json
from cmuh_common.config_io import (
    clone_default,
    load_json_dict,
    load_json_dict_ex,
    load_json_list_ex,
    normalize_doctor_rows,
)
from cmuh_common.paths import get_conf_path

# [2026-07-26 審查 ★設定被覆蓋★] 本次執行中「暫時讀不到」(檔案仍在,只是被防毒/備份鎖住)
# 的設定檔名。載入失敗時這些 loader 會回【預設值】—— 若呼叫端拿那份預設值去存檔,
# 使用者的門檻/止掛提醒收件人/醫師清單就永久消失,而且過程完全沒有徵兆。
# 寫入端(main.save_all_settings)必須先查這裡,有紀錄就拒絕存檔。
_LOAD_FAILED_FILES: set = set()


def settings_load_failed() -> set:
    """本次執行中曾經「暫時讀不到」的設定檔名(空 set = 都正常)。"""
    return set(_LOAD_FAILED_FILES)


def clear_load_failed(filename: str) -> None:
    """把某個設定檔標記為「已無讀取失敗疑慮」。

    [2026-07-27] 只有「還原預設」該呼叫:那條路徑是使用者【明確要求】把該檔覆蓋成
    預設,而且已先備份原檔 —— 拒絕存檔的守衛(防的是無意間覆蓋)在此已無意義,
    再留著只會讓使用者重置完卻永遠不能按儲存。**成功寫入之後**才可呼叫。
    """
    if filename in _LOAD_FAILED_FILES:
        _LOAD_FAILED_FILES.discard(filename)
        logging.info("設定檔 %s 已還原為預設 → 解除本次執行的「拒絕存檔」保護", filename)


def _note_load_status(filename: str, status: str) -> None:
    """只記錄 "error"(原檔完好、暫時讀不到)。missing/corrupt 不記 ——
    那兩種情況磁碟上本來就沒有可用內容,用預設值存檔是合理的修復。"""
    if status == "error":
        if filename not in _LOAD_FAILED_FILES:
            logging.error(
                "設定檔 %s 暫時讀不到(檔案仍在,可能被防毒/備份鎖住)→ 本次執行改用預設值;"
                "在重新讀到之前【不會】允許存檔,以免把您的設定覆蓋成預設", filename)
        _LOAD_FAILED_FILES.add(filename)
    else:
        _LOAD_FAILED_FILES.discard(filename)

# [使用者定案] R1-R3 值班對照姓名(僅供依姓名比對院方值班表 fetch_duty_doctor;name-only,
# 無 doc_no/公務機 欄位)。住院醫師升年:2026-08-01 起更替。
# [codex] 設【生效日閘門】—— 舊組保留到 7/31,8/1(含)起才換新組。否則無存檔的機器(新裝/
# 刪檔)在 7 月就會把現任 R 顯示成下一年的階級(值班對照靠姓名比對,直接影響顯示)。
R_DOCTOR_TRANSITION_DATE = date(2026, 8, 1)
_R_DOCTOR_SETTINGS_BEFORE = {
    "R1": {"name": "林于喬"},
    "R2": {"name": "陳翊嘉"},
    "R3": {"name": "蔡明洋"},
}
_R_DOCTOR_SETTINGS_FROM_2026_08_01 = {
    "R1": {"name": "賴奕彰"},
    "R2": {"name": "林于喬"},
    "R3": {"name": "陳翊嘉"},
    # ★[使用者定案 2026-08-03] 補上漏掉的 R4★
    #   升年是每個人往上一階：林于喬 R1→R2、陳翊嘉 R2→R3、蔡明洋 R3→【R4】，
    #   賴奕彰是新的 R1。原本這組只寫到 R3 —— 蔡明洋就此從值班姓名對照裡
    #   整個消失，8/1 起他的值班在院方值班表上比對不到。
    "R4": {"name": "蔡明洋"},
}

# ★名單修訂版號★（2026-08-03 使用者定案：直接複寫每台電腦上的舊存檔）
#   `r_doctor_settings.json` 一旦存在就會蓋過預設值，所以光改預設值救不了
#   已經存過檔的機器 —— 它們會繼續顯示漏掉 R4 的舊名單。
#   存檔裡的版號小於這個數字（或根本沒有）就以【預設名單】為準；
#   使用者之後在設定頁改過並儲存，存檔就會帶上新版號而重新被尊重。
R_DOCTOR_ROSTER_REVISION = 2
_ROSTER_REVISION_KEY = "_roster_revision"


def default_r_doctor_settings(today: date | None = None) -> dict:
    """依生效日回傳 R1-R3 值班對照預設姓名:2026-08-01(含)起用新組,之前用舊組。"""
    today = today or date.today()
    return (_R_DOCTOR_SETTINGS_FROM_2026_08_01
            if today >= R_DOCTOR_TRANSITION_DATE else _R_DOCTOR_SETTINGS_BEFORE)


# 向後相容常數(import 當下凍結)。呼叫端要【當下】正確值請用 default_r_doctor_settings()。
DEFAULT_R_DOCTOR_SETTINGS = default_r_doctor_settings()

DEFAULT_DOCTOR_SETTINGS = [
    {"name": "張廖年峰", "doc_no": "D15728", "notifications": True},
    {"name": "吳伯元", "doc_no": "D15645", "notifications": False},
    {"name": "陳駿升", "doc_no": "D34899", "notifications": False},
    {"name": "沈冠宇", "doc_no": "D28592", "notifications": False},
    {"name": "許致榮", "doc_no": "D20191", "notifications": False},
    {"name": "謝佳陵", "doc_no": "101823", "notifications": False},
    {"name": "方心禹", "doc_no": "D14355", "notifications": False},
    {"name": "黃建仁", "doc_no": "D6175", "notifications": False},
    {"name": "邵湘德", "doc_no": "D30915", "notifications": False},
    {"name": "李威儒", "doc_no": "D35819", "notifications": False},
    {"name": "蔡李澄", "doc_no": "D31352", "notifications": False},
    # [使用者定案 2026-07-20] 新增門診人數查詢預設醫師
    {"name": "蔡明洋", "doc_no": "D34257", "notifications": False},
    {"name": "陳翊嘉", "doc_no": "101358", "notifications": False},
]

DEFAULT_AUTO_REBOOT_SETTINGS = {"enabled": False, "time": "07:01"}
DEFAULT_NOTIFY_DND_START_HOUR = 0
DEFAULT_NOTIFY_DND_END_HOUR = 8


def _path(path: str | None, filename: str) -> str:
    if path is not None:
        return path
    return _consistent_snapshot_path(get_conf_path(filename))


def _consistent_snapshot_path(target: str) -> str:
    """未撤銷的多檔交易還在時,改讀★交易前的一致快照★(`.rollback.bak`)。

    ★[外審 deep R2] 只擋寫入是不夠的★:磁碟上那三個檔可能是半舊半新
    (A 已還原、B 還是新的),而它們讀得到、也讀得懂 —— 載入端沒有任何訊號,
    整個執行期就用著一份不一致的組合做臨床判斷(門檻/醫師清單/R 醫師)。
    ★而「最後一致的快照」其實還在★:未撤銷的交易會把 `.rollback.bak` 留著
    (復原是複製、備份留到整筆成功才清),那正是交易前的內容。
    所以這裡不是「猜一個安全值」,是讀那份確實存在、確實一致的舊資料。
    復原一旦完成,備份被清掉、這個轉向自動消失(出口)。

    ★我第一版改的是別的東西,而且沒有作用★:原本把 stuck 的檔丟進
    `_LOAD_FAILED_FILES`,但那個集合的語意是「這次讀不到」——
    下一次成功讀取就會 `discard` 掉它(見 `_note_load_status`),
    標記自我消滅。那是把宣稱寫在一個會被沖掉的地方。
    """
    try:
        if settings_recovery_incomplete() is None:
            return target
        bak = str(target) + ".rollback.bak"
        if os.path.exists(bak):
            logging.warning(
                "[設定] 上次的設定交易尚未撤銷乾淨 → %s 改讀交易前的備份"
                "(避免用到半舊半新的組合);還原完成後自動恢復",
                os.path.basename(target))
            return bak
        # ★沒有備份分兩種,不可以一律退回 live 檔★(外審 deep R3):
        #   契約上「交易前不存在」的目標本來就沒有備份(全新安裝第一次存檔),
        #   那時★交易前的一致狀態就是「這個檔不存在」★ —— 退回 live 檔等於
        #   載入一份【沒有 commit 成功】的新值,而同批其他檔用預設值,
        #   還是一組混合快照。回一個不存在的路徑,讓載入端照既有規則用預設值。
        _tx = _pending_tx_info()
        if _tx and str(target) in _tx["targets"]:
            if str(target) not in _tx["existed"]:
                logging.warning(
                    "[設定] %s 是上次未完成交易【新建】的檔 → 視為不存在"
                    "(交易前的一致狀態就是沒有這個檔);還原完成後自動恢復",
                    os.path.basename(target))
                return str(target) + ".pre-transaction-absent"
            # 交易前存在、備份卻不見了 → ★無法證明 live 檔是哪一版★。
            #   仍然讀它(把設定整個換成預設會直接毀掉使用者的門檻/收件人),
            #   但要明講:這一份無法證明,而寫入端的閘門仍然關著。
            logging.error(
                "[設定] %s 的交易前備份不見了 → 無法證明目前這一份是交易前還是"
                "未提交的新值;暫時照讀,但存檔仍被擋住(請重啟讓復原再試一次)",
                os.path.basename(target))
    except Exception:
        logging.debug("[設定] 一致快照判定失敗(改用原路徑)", exc_info=True)
    return target


def unprovable_settings() -> tuple:
    """★無法證明版本★的設定檔名(basename)。空 tuple = 沒有這種檔。

    ★[R2-P2-01 使用者定案 2026-08-31:B 案]★
    「交易前存在、備份卻遺失」的檔,live 內容無法證明是交易前的舊值還是
    未提交的新值(防毒隔離/人工清掉備份才會發生,極罕見)。
    使用者定案:這個狀態下★停用止掛提醒★(其餘功能照常)——
    錯的通知會被當成對的,比暫時沒有通知更危險;金絲雀的 notify-only 是
    針對「擋 HIS 自動寫入」的可用性代價,這裡的代價小得多。
    消費端在 main.py 的 `_stop_alerts_suspended_reason()`(單一可見宣告)。
    ★出口★:復原完成(manifest 清掉)或備份回來 → 自動回空 → 提醒自動恢復。
    """
    try:
        _recover_interrupted_settings_write()
        tx = _pending_tx_info()
        if not tx:
            return ()
        out = []
        for t in tx["targets"]:
            if (t in tx["existed"]
                    and not os.path.exists(str(t) + ".rollback.bak")
                    and os.path.exists(t)):
                out.append(os.path.basename(str(t)))
        return tuple(sorted(out))
    except Exception:
        logging.debug("[設定] 無法證明清單查詢失敗", exc_info=True)
        # ★查不出來不可以說成「沒有」★ —— 但也不可能列出名字;
        #   回一個明確的哨兵讓消費端當作「有」處理。
        return ("<查詢失敗>",)


def _pending_tx_info() -> "dict | None":
    """未撤銷交易的 manifest 內容({"targets": [...], "existed": {...}})。

    ★`existed` 是判斷「交易前這個檔在不在」的唯一事實★:不可以從有沒有
    `.rollback.bak` 去推 —— 備份失敗時也沒有 bak,而那兩種情況的正解相反。
    """
    # ★[外審 R5-2] 讀不出來要【拋】,不可回 None★:None 的語意是「確定沒有
    #   pending 交易」;把「manifest 壞掉/暫時讀不到」也壓成 None 的話,
    #   `unprovable_settings()` 會回空、B 案閘門照樣放行 —— 而復原端明明把
    #   同一種狀態判成未完成。呼叫端各自接:`unprovable_settings` 的 except
    #   轉成 fail-closed 哨兵;`_consistent_snapshot_path` 退回原路徑。
    from cmuh_common.atomic_io import _MANIFEST_NAME  # noqa: PLC0415
    from cmuh_common.paths import get_settings_dir  # noqa: PLC0415
    mf = os.path.join(get_settings_dir(), _MANIFEST_NAME)
    if not os.path.exists(mf):
        return None
    import json  # noqa: PLC0415
    with open(mf, encoding="utf-8") as f:
        m = json.load(f) or {}
    if m.get("committed") or m.get("rolled_back"):
        # ★[外審 R5-3] commit 成功的殘留 manifest 不是 pending 交易★:
        #   成功路徑先刪備份再刪 manifest,刪 manifest 被擋(但讀得到)時,
        #   備份不在是【合法】的 —— 不分辨 committed 的話,B 案會把每一個
        #   目標都判成無法證明,止掛提醒被無限期停掉。與復原端的
        #   `_has_anything_to_undo`(committed 具權威)同一個分類。
        return None
    if "existed" not in m:
        return None            # 舊格式:不知道交易前存不存在 → 不做推斷
    return {"targets": [str(t) for t in (m.get("targets") or ())],
            "existed": {str(t) for t in (m.get("existed") or ())}}


def _legacy_hour_to_hhmm(value: object, fallback_hour: int) -> str:
    try:
        hour = int(value)
    except (TypeError, ValueError):
        hour = fallback_hour
    hour = max(0, min(24, hour))
    return f"{hour:02d}:00"


def load_r_doctor_settings(path: str | None = None,
                           today: date | None = None) -> dict:
    """Load R1-R3 doctor name mappings with trimmed names.
    預設值依生效日決定(見 default_r_doctor_settings);已存檔者以檔案為準。"""
    defaults = default_r_doctor_settings(today)
    data, _st = load_json_dict_ex(_path(path, "r_doctor_settings.json"), defaults)
    _note_load_status("r_doctor_settings.json", _st)
    out = clone_default(defaults)
    try:
        saved_revision = int(data.get(_ROSTER_REVISION_KEY, 0))
    except (TypeError, ValueError):
        saved_revision = 0
    if saved_revision < R_DOCTOR_ROSTER_REVISION:
        # ★存檔版本比程式舊 → 以預設名單為準（使用者定案：直接複寫）★
        #   不在這裡寫檔：載入不該有副作用，而且「拒絕存檔」保護正是為了
        #   避免讀到一半的狀態被寫回去。使用者按一次儲存就會帶上新版號。
        logging.info("R 醫師名單存檔版本較舊(%s < %s) → 本次以程式內建名單為準",
                     saved_revision, R_DOCTOR_ROSTER_REVISION)
        return out
    for key in out:
        if isinstance(data.get(key), dict):
            out[key] = {"name": str(data[key].get("name", "")).strip()}
    return out


def stamp_r_doctor_revision(mapping: dict) -> dict:
    """存檔前蓋上名單版號 —— 之後這份存檔才會被尊重。"""
    out = dict(mapping)
    out[_ROSTER_REVISION_KEY] = R_DOCTOR_ROSTER_REVISION
    return out



if TYPE_CHECKING:                       # 只給型別檢查,執行期不匯入
    from cmuh_common.atomic_io import RecoveryResult


def _recover_interrupted_settings_write() -> None:
    """把上次中途被中止的多檔設定交易還原。

    ★[外審第二輪 R2-P2-01] 兩個問題★
    1. `_settings_recovery_done = True` 設在★嘗試之前★ —— 這個行程從此不再重試,
       就算什麼都沒還原成功。
    2. 呼叫端拿到的只是「還原了幾個」,分不出「全部還原」與「兩個還原、一個
       被防毒鎖住」。後者代表設定★半舊半新★,而程式會就這樣繼續載入下去 ——
       正是 `atomic_write_json_multi` 花那麼多程式碼要避免的狀態。
    現在:只有★完整完成★才記為已處理(沒完成的話下次載入會再試一次,
    那就是出口);未完成時把狀態記下來,讓依賴這組設定的寫入路徑可以拒絕。
    """
    global _settings_recovery_done, _settings_recovery_state
    # ★[外審 deep R1-2] 不可以永久快取「沒有交易」★
    #   乾淨啟動時記成已處理之後,★同一個行程裡★後來的多檔寫入若 commit 失敗
    #   而 rollback 不完整,會留下新的 manifest 與備份 —— 舊快取讓載入與閘門
    #   直接沿用「沒事」的結論,只能靠重開程式處理,與「每次載入都重試」的
    #   契約不符。改成:每次都先看一眼 manifest 在不在(一次 stat,成本可忽略),
    #   有新的交易紀錄就重跑一次復原。
    try:
        from cmuh_common.atomic_io import (  # noqa: PLC0415
            _MANIFEST_NAME as _MF,
        )
        from cmuh_common.paths import get_settings_dir as _gsd  # noqa: PLC0415
        _pending_now = os.path.exists(os.path.join(_gsd(), _MF))
    except Exception:
        _pending_now = True          # 看不出來 → 當作要再試(不可假設沒事)
    if _settings_recovery_done and not _pending_now:
        return
    try:
        from cmuh_common.atomic_io import (  # noqa: PLC0415
            recover_interrupted_multiwrite,
        )
        from cmuh_common.paths import get_settings_dir  # noqa: PLC0415
        res = recover_interrupted_multiwrite(get_settings_dir())
        _settings_recovery_state = res
        _settings_recovery_done = bool(res)
        if not res:
            logging.error(
                "[設定] ★上次的設定存檔沒有完整撤銷★:%s。"
                "設定可能是半舊半新;在還原成功之前不會接受新的設定存檔"
                "(下次啟動會自動再試一次)。", res.describe())
            # ★不在這裡標 `_LOAD_FAILED_FILES`★(外審 deep R2 抓到):
            #   那個集合的語意是「這次讀不到」,下一次成功讀取就會 discard 掉
            #   —— 標記自我消滅,等於沒做。真正的處置在 `_consistent_snapshot_path`
            #   (載入改讀交易前的備份)與存檔閘門(`settings_recovery_incomplete`)。
    except Exception:
        # ★連跑都跑不起來也不可以當作沒事★(測試抓到:原本只記 log,於是
        #   `_settings_recovery_state` 停在先前那次「乾淨」的結果,閘門照樣放行
        #   —— 又是「不知道」被當成「沒問題」)。明確記成★未完成★,
        #   並解除已處理旗標;★出口★:下一次呼叫會再試,成功就自動清掉。
        logging.warning("[設定] 還原未完成的設定交易失敗 → 下次再試",
                        exc_info=True)
        try:
            from cmuh_common.atomic_io import RecoveryResult  # noqa: PLC0415
            _settings_recovery_state = RecoveryResult(False, pending=True)
        except Exception:
            logging.debug("[設定] 無法標記復原狀態", exc_info=True)
        _settings_recovery_done = False


_settings_recovery_done = False
_settings_recovery_state: "RecoveryResult | None" = None


def settings_recovery_incomplete() -> "RecoveryResult | None":
    """上次的設定交易有沒有★還沒撤銷乾淨★;沒問題回 None。

    ★給寫入路徑當閘門用★:在半舊半新的狀態上再寫一次新設定,會把下次重試
    需要的備份與交易紀錄永久覆蓋掉 —— 那是把「還救得回來」變成「救不回來」。
    ★出口★:這不是永久狀態 —— 每次載入設定都會再試一次還原,
    檔案不再被鎖住就自動恢復;訊息也說得出可以怎麼自救。
    """
    _recover_interrupted_settings_write()
    st = _settings_recovery_state
    return None if st is None or bool(st) else st


def load_threshold_settings(
    path: str | None = None,
    default_thresholds: dict | None = None,
    *,
    dnd_start_hour: int = DEFAULT_NOTIFY_DND_START_HOUR,
    dnd_end_hour: int = DEFAULT_NOTIFY_DND_END_HOUR,
) -> dict:
    """Load threshold settings and fill legacy notification defaults."""
    # ★開機先把上次沒做完的多檔設定交易還原★(外審第 11 輪第 2 回 F7)
    #   行程被砍/斷電時 Python 的 rollback 不會執行,磁碟會停在半新半舊。
    #   接在設定載入這裡:那是開機一定會走到的地方,
    #   而不是一個沒人呼叫的 API(這一輪外審已經點名過兩次)。
    _recover_interrupted_settings_write()
    # [2026-07-27] 預設值改由 settings_defaults 統一宣告(門檻 + 收件人 + F8 + 介面),
    # 這樣「新增一個設定鍵」只要動那一份 dict,載入/還原預設/摘要三件事自動涵蓋。
    # 呼叫端仍可用 default_thresholds 覆寫(測試用)。
    from cmuh_common.settings_defaults import default_threshold_settings
    defaults = dict(default_threshold_settings())
    if default_thresholds:
        defaults.update(default_thresholds)
    # ★順序很重要★ 下面那幾條「舊格式推導」的條件都是 `if key not in data`。
    # 若先把預設合進來,每個鍵都會存在 → 推導全部失效,而舊機器的檔案往往【只有】
    # notify_dnd_*_hour(沒有 *_time),它們的勿擾時段就會被悄悄換成預設值。
    # 故:先拿【原始檔案內容】做推導,最後才用預設補齊缺的鍵。
    data, _st = load_json_dict_ex(_path(path, "threshold_settings.json"), None)
    _note_load_status("threshold_settings.json", _st)
    # ★[2026-08-05 外審第 5 輪 P2-11] 新的止掛對象要繼承「這台是不是負責寄信的機器」★
    #   2026-08-05 把止掛提醒對象從張廖年峰換成沈冠宇,於是所有舊機器的設定檔都
    #   【沒有】alert_shen_enabled 這個鍵 → 一律走原廠預設。兩種預設都不對:
    #     * 預設開 → 全院每一台診間機都寄一封(既有定案:多台同時跑會重複寄信)
    #     * 預設關 → 原本負責寄信的那台也靜悄悄不寄了,使用者要求的功能等於沒上線
    #   真正的資訊是「這台原本有沒有在做止掛提醒」——它就記在舊鍵裡。
    #   ★只在【檔案裡沒有新鍵】時推導★(與下面幾條同樣的 `not in data` 語意):
    #   使用者一旦自己勾過,檔案就有這個鍵,之後永遠以他的選擇為準。
    # ★[2026-08-08 外審] 只遷移【被取代的那一位】,不從別人的開關推測★
    #   上一版是 `chang or chen`:一台原本設定成「只提醒陳駿升」的機器,
    #   會被自動打開沈冠宇提醒 —— 那是使用者從來沒有做過的選擇。
    #   每位醫師一個獨立開關,遷移就該是一對一:沈冠宇接的是張廖年峰的位置。
    #   (代價是「原本沒開過張廖年峰」的機器不會自動有沈冠宇提醒。所以
    #    下面補一行 log 把這件事講清楚,而不是替使用者決定。)
    if "alert_shen_enabled" not in data:
        if data.get("alert_chang_enabled"):
            data["alert_shen_enabled"] = True
            logging.info("[設定] 原本啟用的張廖年峰提醒 → 由沈冠宇接手"
                         "(可在設定頁關閉)")
        elif data.get("alert_chen_enabled"):
            logging.info("[設定] 這台有在做止掛提醒(陳駿升),但沒有啟用過"
                         "張廖年峰 → 沈冠宇提醒【預設不開】;"
                         "需要的話請到設定頁勾選")
    # ★[2026-08-26 使用者] 謝佳陵預設 75 → 70,並新增週六早★
    #   已部署機器的設定檔把 75 存成了明確值 —— 只改原廠預設的話,那幾台
    #   永遠停在 75。遷移判準(與上面沈冠宇那條同一個哲學):
    #   ★只在檔案裡還沒有新鍵 hsieh_sat_morning 時★(= 本功能之前存的檔)
    #   把「存值恰等於舊預設 75」的診次改成 70 —— 那個 75 與原廠預設
    #   不可分辨,而定 75 與定 70 的是同一位使用者。使用者存檔一次之後
    #   檔案就有新鍵(設定頁會把每一格都存),這條遷移自然過期;
    #   之後刻意設回 75 會被尊重。
    if "hsieh_sat_morning" not in data:
        for _k in ("hsieh_thu_morning", "hsieh_thu_night",
                   "hsieh_fri_afternoon"):
            if data.get(_k) == 75:
                data[_k] = 70
                logging.info("[設定] 謝佳陵 %s 沿用舊原廠預設 75 → 依 "
                             "2026-08-26 定案改 70(設定頁可自行調整)", _k)
    if "ui_font_scale" not in data:
        data["ui_font_scale"] = 1.0
    if "notify_dnd_start_hour" not in data:
        data["notify_dnd_start_hour"] = dnd_start_hour
    if "notify_dnd_end_hour" not in data:
        data["notify_dnd_end_hour"] = dnd_end_hour
    if "notify_dnd_start_time" not in data:
        data["notify_dnd_start_time"] = _legacy_hour_to_hhmm(
            data.get("notify_dnd_start_hour", dnd_start_hour),
            dnd_start_hour,
        )
    if "notify_dnd_end_time" not in data:
        data["notify_dnd_end_time"] = _legacy_hour_to_hhmm(
            data.get("notify_dnd_end_hour", dnd_end_hour),
            dnd_end_hour,
        )
    out = defaults
    out.update(data)
    return out


def load_doctors_settings(path: str | None = None) -> list:
    """Load doctor rows and repair historical swapped name/doc_no values."""
    target = _path(path, "doctors.json")
    defaults = DEFAULT_DOCTOR_SETTINGS
    data, _st = load_json_list_ex(target, defaults)
    _note_load_status("doctors.json", _st)
    normalized, fixed = normalize_doctor_rows(data, defaults)
    # [2026-07-26 審查] 讀不到就【絕不】寫回:這條「正規化後順手修檔」的路徑在
    # 暫時讀取失敗時拿到的是預設清單,寫下去等於把使用者的醫師清單清成預設 ——
    # 而且不需要使用者做任何事,光是啟動就會發生。
    if fixed and _st == "error":
        logging.error("doctors.json 暫時讀不到 → 跳過正規化寫回(避免把醫師清單覆蓋成預設)")
        fixed = False
    if fixed:
        # [IE-11 2026-07-12] 若正規化結果退回預設(原檔形狀全錯被整個丟棄)且原檔確有異於預設的
        # 內容 → 覆寫前先備份成 .invalid-<ts>,免 OneDrive 還原的舊格式檔被靜默清空無法救回。
        if normalized == defaults and data != defaults:
            try:
                # [codex 2026-07-12] 備份名含 PID,避免同秒兩 process/session 產生同名 .invalid-<ts>
                # 而第二個覆寫掉第一個的原檔備份;且不覆寫既有備份。
                ts = time.strftime("%Y%m%d_%H%M%S")
                dest = f"{target}.invalid-{ts}-{os.getpid()}"
                if os.path.exists(target) and not os.path.exists(dest):
                    os.replace(target, dest)
            except OSError:
                logging.debug("[doctors] 備份 .invalid 失敗", exc_info=True)
        atomic_write_json(target, normalized)
    return normalized


def load_auto_reboot_settings(path: str | None = None) -> dict:
    """Load auto reboot settings."""
    return load_json_dict(
        _path(path, "auto_reboot_settings.json"),
        DEFAULT_AUTO_REBOOT_SETTINGS,
    )
