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
完整畫面。故集中成一支「不依賴任何事件發生」的清掃器。

★[外審第二輪 R2-P2-02] 誰產生敏感資料,誰就要自己執行保留期★
原本只有主程式在啟動時與每日固定時間各跑一次全域清掃,而★產生★這些檔的是
另外兩支獨立程式:會診查詢(consult_shots,7 天)與打卡(debug_dumps,3 天)。
watchdog 允許「這台只跑會診+打卡、主程式很少開」的合法部署(治療室共用電腦
正是這樣),於是宣告的保留期就不再是保證。
而兩支程式原本的 TTL 清掃★只在產生新資料時★被呼叫(存新截圖/存除錯檔那一刻)
—— 事件一停,清掃就再也不會發生,含 PHI 的檔可以無限期留著。
故:兩支程式各自在★啟動時★與★固定週期★清自己那一份
(`start_background_sweeper`);主程式的全域清掃保留為冗餘。

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

import fnmatch
import logging
import os
import stat as stat_module
import threading
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
    # ★[外審第二輪 R2-P2-03] 連年齡都讀不到的檔★:ACL、防毒、暫時性 IO 錯誤。
    #   原本 `except OSError: continue` —— 不進 failed、不進 oldest、不進摘要,
    #   於是「磁碟上還躺著一個含 PHI 的檔」與「沒有過期檔案」長得一模一樣。
    #   ★無法確認檔案年齡 ≠ 檔案在保留期內★:隱私控制要顯示 degraded,不是 clean。
    stat_failed: dict = field(default_factory=dict)  # {label: 讀不到年齡幾個}
    # ★[外審第四輪 R4-P2-01] 連「這個目錄裡有什麼」都問不到★
    #   上一輪修的是「有檔案、但 stat 不到年齡」。再上游一格還有兩種:
    #     * 目錄本身 stat 不到(ACL/防毒)→ 連它存不存在都不知道;
    #     * 目錄在、但列舉不了 → `glob.glob()` 會把 OSError 吞成空清單。
    #   兩者原本都長得跟「這裡沒有過期檔」一模一樣,摘要照樣說「沒有過期檔案」。
    #   ★enumeration failure ≠ directory empty★ —— 與 R2-P2-03 的
    #   「stat failure ≠ file is young」是同一條不變式。
    directory_failed: dict = field(default_factory=dict)    # {label: 1}
    enumeration_failed: dict = field(default_factory=dict)  # {label: 1}

    @property
    def total_deleted(self) -> int:
        return sum(self.deleted.values())

    @property
    def clean(self) -> bool:
        """這一輪有沒有★完全確認★保留期成立。

        ★四種都不算★:刪不掉、讀不到年齡、目錄問不到、目錄列舉不了。
        任何一種成立時,磁碟上都可能還躺著超過保留期的個資,而我們無從證明。
        """
        return not (self.failed or self.stat_failed
                    or self.directory_failed or self.enumeration_failed)

    def summary(self) -> str:
        parts = [f"{k}×{v}" for k, v in sorted(self.deleted.items()) if v]
        out = ("清掉 " + "、".join(parts)) if parts else "沒有過期檔案"
        if self.failed:
            out += "；刪不掉:" + "、".join(
                f"{k}×{v}" for k, v in sorted(self.failed.items()) if v)
        if self.stat_failed:
            out += "；★年齡讀不到(無法確認保留期)★:" + "、".join(
                f"{k}×{v}" for k, v in sorted(self.stat_failed.items()) if v)
        # ★兩種「問不到」要分開講★:處置一樣(都要人去看),但要查的東西不同 ——
        #   一個是目錄本身的權限/存在性,一個是目錄內容的列舉。
        if self.directory_failed:
            out += "；★目錄狀態問不到(無法確認保留期)★:" + "、".join(
                sorted(self.directory_failed))
        if self.enumeration_failed:
            out += "；★目錄列舉不了(無法確認保留期)★:" + "、".join(
                sorted(self.enumeration_failed))
        if self.oldest:
            out += f"；最舊敏感檔:{self.oldest[0]} {self.oldest[1]:%Y-%m-%d}"
        return out


def _older(cur, cand: float) -> float:
    """取較舊的那個 mtime（None＝還沒有候選）。"""
    return cand if cur is None or cand < cur else cur


def _name_matches(name: str, patterns) -> bool:
    """檔名符合任一樣式嗎。★行為要與原本的 `glob` 逐位元一致★

    * `fnmatch.fnmatch` 在 Windows 會 normcase(不分大小寫)—— glob 也是;
    * glob 的隱藏檔慣例:`*` 不匹配開頭是 `.` 的名字,除非樣式自己以 `.` 開頭。
      漏掉這一條的話,`*.before-reset-*` 會開始吃到 dotfile —— 那是★多刪★,
      比原本的問題更糟(一個修正必須連同它新開的可能性一起判斷)。
    ★樣式一律是「單層檔名」★:含路徑分隔字元的樣式在這裡永遠比不中
    (有測試釘住本模組出貨的規則都是單層),要遞迴請另外設計,
    不要以為寫 `sub/*.png` 會生效 —— 那會是一個安靜的保留期漏洞。
    """
    for pat in patterns:
        if name.startswith(".") and not str(pat).startswith("."):
            continue
        if fnmatch.fnmatch(name, str(pat)):
            return True
    return False


def _list_dir_names(directory: str):
    """列出目錄裡的名字。→ `(names, status)`,status ∈
    ok / absent / dir_unreadable / list_failed。

    ★[外審第四輪 R4-P2-01] 為什麼不能再用 `glob.glob()`★
    它在目錄無法列舉時(ACL、防毒鎖、暫時性 IO、網路碟斷線)★把 OSError
    吞掉並回空清單★ —— 呼叫端於是分不出「裡面沒有過期檔」與「裡面有什麼
    我根本沒看到」。而 `os.path.isdir()` 同樣把 OSError 吞成 False,
    跟「這個目錄不存在」(契約上要靜默跳過的那種)混在一起。
    這一支把三件事分開,讓 `sweep()` 可以誠實回報。
    """
    try:
        st = os.stat(directory)
    except (FileNotFoundError, NotADirectoryError):
        return (), "absent"          # 契約:目錄不存在的規則靜默跳過
    except OSError:
        return (), "dir_unreadable"  # 存不存在都不知道 —— 不可以當成沒有
    if not stat_module.S_ISDIR(st.st_mode):
        return (), "absent"
    try:
        # ★整段列舉包在 try 裡★:Windows 的 FindNextFile 可能在【迭代中途】
        #   失敗,只包 `os.scandir()` 那一行擋不到 —— 而半份清單不能證明
        #   任何事,所以失敗就整批作廢,不採用部分結果。
        with os.scandir(directory) as it:
            return tuple(entry.name for entry in it), "ok"
    except OSError:
        return (), "list_failed"


def sweep(rules, extra_tasks=(), *, now: "float | None" = None) -> SweepResult:
    """跑一輪清掃。

    extra_tasks: [(label, callable)]，callable 回傳「處理掉幾筆」。給那些不是
    「刪整個檔」的清理用（例如定位索引是逐【列】修剪，檔案本身要留著）。
    絕不拋例外。
    """
    res = SweepResult()
    now = time.time() if now is None else now
    for rule in rules:
        names, dstatus = _list_dir_names(rule.directory)
        if dstatus == "absent":
            continue                    # 目錄不存在 —— 契約上的靜默跳過
        if dstatus != "ok":
            # ★問不到就要說★(R4-P2-01):這兩種狀態下,目錄裡可能正躺著
            #   超過保留期的截圖/病歷號,而我們連看都沒看到。
            bucket = (res.directory_failed if dstatus == "dir_unreadable"
                      else res.enumeration_failed)
            bucket[rule.label] = bucket.get(rule.label, 0) + 1
            logging.warning("[retention] %s(%s):%s —— 無法確認保留期",
                            rule.label,
                            "目錄狀態問不到" if dstatus == "dir_unreadable"
                            else "目錄列舉不了", rule.directory)
            continue
        cutoff = now - rule.retain_days * 86400.0
        gone = bad = unknown_age = 0
        newest_kept: "float | None" = None
        for _name in names:
            if not _name_matches(_name, rule.patterns):
                continue
            path = os.path.join(rule.directory, _name)
            # ★整條路徑只做【一次】明確的 stat★(外審 R1-2):是不是普通檔、
            #   幾歲,都由這一次的結果回答 —— 中間任何一個「順手判斷」都可能
            #   把 OSError 吞成「不是檔案」而靜默跳過。
            try:
                st = os.stat(path)
            except OSError:
                # ★讀不到年齡的檔【還在磁碟上】★(外審 R2-P2-03):靜默跳過會讓
                #   摘要說「沒有過期檔案」,而那個檔可能早就超過保留期。
                unknown_age += 1
                logging.warning("[retention] 讀不到檔案狀態(無法確認保留期):%s",
                                path, exc_info=True)
                continue
            if not stat_module.S_ISREG(st.st_mode):
                continue                    # 目錄/其他:本來就不歸保留期管
            mtime = st.st_mtime
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
        if unknown_age:
            res.stat_failed[rule.label] = unknown_age
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


def default_rules(settings_dir: str) -> list:
    """本機要定期清掃的落地檔。目錄不存在的規則會被 sweep 靜默跳過。

    [P2-06 第三刀 2026-07-31] 從 main.py 搬入。原本在 main.py 裡自己去拿
    `get_settings_dir()`，改成【由呼叫端傳入】—— 三個 rule builder 本來就都收路徑，
    這樣才一致，也才測得到（不必去 monkeypatch 設定目錄）。
    """
    return [
        debug_dump_rule(os.path.join(settings_dir, "debug_dumps")),
        consult_shot_rule(os.path.join(settings_dir, "consult_shots")),
        settings_backup_rule(settings_dir),
    ]


# ─── 產生者自己的週期清掃 ─────────────────────────────────────────────────
#: 產生敏感資料的程式自己跑清掃的間隔(秒)。12 小時:診間電腦常常整天開著,
#: 一天跑兩次足以讓「保留期」是保證而不是巧合;成本是一條睡著的 daemon 緒。
SELF_SWEEP_INTERVAL_SEC = 12 * 3600


def start_background_sweeper(rules, *, interval_sec: float =
                             SELF_SWEEP_INTERVAL_SEC,
                             extra_tasks=(), label: str = "self",
                             _sleep=None) -> "threading.Thread | None":
    """★誰產生敏感資料,誰就自己執行保留期★(外審 R2-P2-02)。

    立刻掃一次(啟動時),之後每 `interval_sec` 再掃一次。daemon 緒,絕不拋例外
    —— 清理失敗不可以影響臨床流程。回傳執行緒(測試可 join;失敗回 None)。

    ★不依賴任何事件發生★:兩支常駐程式原本的 TTL 清掃只在「存新截圖/存新除錯檔」
    那一刻被呼叫,事件停了就再也不跑 —— 那正是這個模組要消滅的形狀。
    """
    def _once():
        try:
            res = sweep(rules, extra_tasks)
            if not res.clean:
                logging.error("[retention] ★%s 清掃未能完全確認保留期★ %s",
                              label, res.summary())
            elif res.total_deleted:
                logging.info("[retention] %s %s", label, res.summary())
        except Exception:
            logging.debug("[retention] %s 清掃失敗(不影響任何流程)", label,
                          exc_info=True)

    def _loop():
        while True:
            _once()
            (_sleep or time.sleep)(interval_sec)

    try:
        t = threading.Thread(target=_loop, name=f"retention-{label}",
                             daemon=True)
        t.start()
        return t
    except Exception:
        logging.debug("[retention] %s 清掃緒啟動失敗(不影響任何流程)", label,
                      exc_info=True)
        return None
