# -*- coding: utf-8 -*-
"""寄送帳本的回查收斂 —— **主程式與會診程式共用同一份實作**。

★為什麼要獨立成模組(外審 2026-08-09 P1-04)★
回查原本只寫在 `consult_query.py` 裡。但寫進這本帳的有兩支程式:

* 會診查詢:會診通知信
* 主程式:**止掛提醒信**(`_send_alert_email_via_smtp`,同一個
  `DeliveryLedger`、同一個檔)

診間電腦不一定兩支都裝。**只跑主程式的那幾台,止掛信的 UNKNOWN 與卡住的
SUBMITTING 永遠不會有人去收斂** —— 那正是回查機制要解決的問題本身,
而它在那些機器上根本沒有執行者。一旦把帳本接成寄送閘門
(`has_live_delivery()`),那些永遠停在 LIVE 的紀錄會**永久擋住**
同一個 business key 的重寄。

所以:實作放這裡,兩邊都驅動它。

★節流必須跨 process(不只是跨執行緒)★
兩支程式各拿一個記憶體時間戳的話,同一台機器上兩邊會【同時】對同一批
紀錄開 IMAP 回查、同時呼叫 `resolve_unknown()`。而 `delivery_ledger.
_save_once_locked()` 的合併規則明文寫著「delivery_id 是全域唯一的,
所以兩個 process 不可能改到同一筆」—— 兩邊都回查會直接推翻那個前提,
退化成「用我手上的舊副本蓋掉對方的新版本」。
所以宣告(claim)走帳本的跨 process 鎖,見 `DeliveryLedger.claim_reconcile_pass`。
"""
from __future__ import annotations

import logging
import math
import time

#: 寄件備份要一點時間才會出現;太早查到「沒有」會誤判成沒寄出去。
MIN_AGE_SEC = 600.0
#: 兩次回查之間至少隔這麼久(它要開 IMAP 連線)。★這是跨 process 的★
EVERY_SEC = 600.0
#: 一次最多查幾筆(慢查詢不要拖住呼叫端的輪次)。
MAX_PER_PASS = 5
#: 卡在 SUBMITTING 超過這麼久 → 多半是送到一半就被砍,與 UNKNOWN 一樣要回查。
#: 比 `MIN_AGE_SEC` 寬鬆一點:正常的 SUBMITTING 只會存在幾秒鐘。
STUCK_SUBMITTING_AFTER_SEC = 900.0
#: 沒有 Message-ID 的紀錄掛超過這麼久 → 放棄查證、明確結案 + 告警(見下)。
NO_MESSAGE_ID_GIVE_UP_SEC = 86400.0
#: ★有 Message-ID 但【持續查不出來】的也要有出口★(外審 2026-08-18 第七輪 P1)
#:   回查的三態裡,`None` = 查不出來,而它有一大堆完全合法的來源:
#:   ★IMAP 根本沒設定★(那台機器的每一次回查【永遠】回 None)、找不到
#:   寄件備份信箱、SEARCH 沒回 OK、連線/認證/逾時。這些情況下
#:   UNKNOWN / 卡住的 SUBMITTING 永遠不會收斂 —— 在 AE-6 之前那只是帳面
#:   髒;接上事件所有權之後,它會【永久】擋掉同一個 business_key 的臨床
#:   通知(醫師再寄一次 email 觸發也一樣被擋),而且沒有任何地方會說。
#:   與上面同一個 24 小時政策:寧可重複寄一封,不可以永遠不寄。
UNVERIFIABLE_GIVE_UP_SEC = 86400.0
#: 欠補寄的親紀錄要「沉」這麼久才由回查接手 —— 讓呼叫端的記憶體退避佇列
#: (2/10/30 分)先跑完它的快路徑,兩邊不會對同一批收件人同時補寄。
#: 重啟後佇列消失,這個門檻就是 durable 補寄的接手時間。
RESEND_OWED_MIN_AGE_SEC = 3600.0
#: 一輪最多驅動幾筆欠補寄(每筆都可能開一次 SMTP)。
MAX_OWED_PER_PASS = 3
#: ★跨 process 的「這筆還有沒落地的逐位拒收」寄存處★(外審 AE-3 第 4 輪 P1)
#: 帳本停機時 `record_refusals()` 寫不進去,那個 4xx 只在【會診程式的】
#: 記憶體佇列裡。但主程式跑的是同一本帳的回查 —— 它不知道這件事,
#: 帳本一恢復就可能先把那位 UNKNOWN 收件人判成已送達(整封粒度),
#: 逐位證據永久消失。所以落地失敗時把它寫進這個 sidecar(不需要
#: SQLite,帳本掛了照樣寫得進去),★兩支程式的回查都會先來這裡收斂★:
#: 補記成功就移除,仍失敗就把那筆列入本輪不准收斂。
#: 內容只有 delivery_id / 收件人信箱 / SMTP 碼 —— ★沒有病人資料★。
#: ★每筆一個檔,不是一份共用 JSON★(外審 AE-3 第 5 輪 P1):共用檔的
#: 「讀-改-寫」跨 process 沒有序列化 —— 主程式讀到 A、會診同時寫入
#: A+B、主程式再寫回它的殘餘就把 B 刪掉了(`atomic_write_json` 只保證
#: 替換是原子的,不提供跨 process 的互斥)。各寫各的檔就沒有這個問題:
#: 寫入者只碰自己那個檔,drain 只刪它真的補記成功的那幾個。
REFUSAL_STASH_DIRNAME = "pending_refusals"
_STASH_MAX = 200                    # 有界:壞掉的機器不可以把它撐爆
_STASH_TTL_SEC = 7 * 86400.0        # 出口:七天還補不上就不再擋(有告警)
#: 讀不懂的寄存檔(寫到一半斷電/被改壞):不知道它指哪一筆 → 只能全部
#: 擋住;但那樣的封鎖半徑很大,所以給它一天的出口(比 TTL 短),
#: 到期就刪掉並大聲講(不是安靜丟掉)。
_STASH_UNREADABLE_TTL_SEC = 86400.0


def _created_at(rec) -> float:
    """紀錄的建立時間。看不懂 → 當成【很舊】(0.0),不是當成很新。

    ★[外審 2026-08-09 P2-01] 一筆壞資料不可以讓整輪回查中止★
    原本直接 `float(rec["created_at"])` 寫在 list comprehension 裡,而那一段
    在 try 之外 —— 帳本裡只要有【一筆】時間戳壞掉(手動編輯、寫到一半斷電、
    舊格式),`ValueError` 就會把【整輪】打掉,而且是每一輪都打掉。
    其他所有紀錄從此永遠不會被收斂,一個字都不會說。

    ★看不懂要當成「很舊」★ 當成「很新」的話,壞掉的那一筆就會被年齡門檻
    永遠濾掉 —— 又是一個把看得見的壞變成安靜的壞。
    """
    raw = rec.get("created_at")
    try:
        f = float(raw or 0)
    except (TypeError, ValueError):
        logging.warning("[delivery] 紀錄 %s 的 created_at 看不懂(%r)→ 當成很舊",
                        rec.get("delivery_id"), raw)
        return 0.0
    # ★[外審第 2 輪 #5] NaN / Infinity 不會拋例外,但比較永遠是 False★
    #   `json.load()` 接受這兩個非標準 token(同 repo 的 `pidfile.py` 特地擋過),
    #   而 `float("nan")` 完全合法。NaN 的紀錄:年齡門檻永遠不成立 → 永遠
    #   進不了回查清單;而在 `_give_up_on_unverifiable()` 裡 `age < 門檻`
    #   也永遠不成立 → 反而【立刻】被結案,跳過 24 小時的保護期。
    #   同一個壞值在兩條路上往【相反】方向出錯 —— 必須在源頭就擋掉。
    if not math.isfinite(f):
        logging.warning("[delivery] 紀錄 %s 的 created_at 不是有限數(%r)→ 當成很舊",
                        rec.get("delivery_id"), raw)
        return 0.0
    return f


def _stash_dir() -> str:
    import os  # noqa: PLC0415
    from cmuh_common.paths import get_settings_dir  # noqa: PLC0415
    return os.path.join(get_settings_dir(), REFUSAL_STASH_DIRNAME)


def _stash_file_for(delivery_id: str) -> str:
    """delivery_id → 它專屬的寄存檔路徑。

    ★檔名用雜湊,不用 id 原文★:id 多半是 uuid4 的 hex(安全),但舊
    JSON 匯入的紀錄可以是任何字串 —— 直接當檔名就可能出現路徑分隔字元。
    真正的 id 存在檔案內容裡。
    """
    import hashlib  # noqa: PLC0415
    import os  # noqa: PLC0415
    name = hashlib.sha256(str(delivery_id).encode("utf-8")).hexdigest()[:32]
    return os.path.join(_stash_dir(), name + ".json")


def _load_stash_entries() -> tuple:
    """讀寄存處 → ([(path, rec 或 None), …], 列舉得到嗎)。

    ★列舉不到要回 False★:當成空的話,這一輪就會放行那些本該被擋住的
    收斂。個別檔讀不懂 → rec=None(呼叫端另外處理,見 drain)。
    """
    import os  # noqa: PLC0415
    from cmuh_common.atomic_io import safe_load_json_ex  # noqa: PLC0415
    d = _stash_dir()
    try:
        if not os.path.isdir(d):
            return [], True                 # 從來沒存過東西
        names = sorted(os.listdir(d))
    except OSError:
        logging.error("[delivery] 列舉逐位拒收寄存處失敗(設定目錄有問題?)",
                      exc_info=True)
        return [], False
    out = []
    for n in names:
        if not n.endswith(".json"):
            continue
        p = os.path.join(d, n)
        data, status = safe_load_json_ex(p, {}, backup_on_corrupt=False)
        if status == "missing":
            continue                        # 別人剛補記完刪掉了
        if status != "ok" or not isinstance(data, dict) \
                or not str(data.get("delivery_id") or ""):
            out.append((p, None))           # 讀不懂/沒有 id
            continue
        out.append((p, data))
    return out, True


def _prune_stash_locked(now: float) -> None:
    """有界:超過上限就砍最舊的(只砍自己看得懂的那些)。"""
    import os  # noqa: PLC0415
    entries, ok = _load_stash_entries()
    if not ok or len(entries) <= _STASH_MAX:
        return
    aged = sorted(((_as_float((r or {}).get("at")), p) for p, r in entries),
                  reverse=True)
    for _at, p in aged[_STASH_MAX:]:
        try:
            os.remove(p)
            logging.error("[delivery] ★寄存處超過 %d 筆,砍掉最舊的一筆★"
                          "(%s)—— 那筆拒收之後可能被誤判成已送達",
                          _STASH_MAX, p)
        except OSError:
            logging.debug("[delivery] 砍寄存檔失敗 %s", p, exc_info=True)


def stash_refusal(delivery_id: str, refused: dict, now=None) -> bool:
    """★落地失敗時的跨 process 備援★(外審 AE-3 第 4 輪 P1)。→ 寫進去了嗎。

    帳本停機時這裡照樣寫得進去(純檔案);兩支程式的回查都會先來這裡
    補記,補不上的那一輪誰都不准收斂那一筆。
    ★每筆寫自己那個檔★(第 5 輪 P1):共用檔的讀-改-寫跨 process 會
    丟更新(A 的 drain 把同時寫入的 B 蓋掉)—— 各寫各的就沒有交集。
    """
    did = str(delivery_id or "")
    if not did or not refused:
        return True
    now = float(now if now is not None else time.time())
    try:
        import os  # noqa: PLC0415
        from cmuh_common.atomic_io import atomic_write_json  # noqa: PLC0415
        os.makedirs(_stash_dir(), exist_ok=True)
        atomic_write_json(_stash_file_for(did), {
            "delivery_id": did, "at": now,
            "refused": {str(a): (r[0] if isinstance(r, (tuple, list)) and r
                                 else r)
                        for a, r in dict(refused).items()}})
        _prune_stash_locked(now)
        return True
    except Exception:
        logging.error("[delivery] ★逐位拒收既寫不進帳本、也存不進寄存處★ —— "
                      "回查可能把它覆蓋成已送達(%s)", did, exc_info=True)
        return False


def _as_float(v) -> float:
    try:
        f = float(v or 0)
    except (TypeError, ValueError):
        return 0.0
    return f if math.isfinite(f) else 0.0


class Reconciler:
    """把帳本上的 UNKNOWN 與卡住的 SUBMITTING 用 Message-ID 回查寄件備份。

    ★這是 `delivery_ledger` 一直宣稱、但一度沒有人呼叫的那條路★
    模組 docstring 寫「UNKNOWN 就誠實地記成 UNKNOWN,之後用 Message-ID 回查
    寄件備份把它收斂成 CONFIRMED 或 FAILED」,而 `resolve_unknown()` 曾經在
    生產程式碼裡【一個呼叫端都沒有】—— 於是每一筆 UNKNOWN 都永遠停在 UNKNOWN:

    * `has_live_delivery()` 把它算成「還沒被否證」→ 同一批【永遠不會再寄】
    * 也永遠不會有人知道那封信到底送到了沒有

    ★三態必須原封不動地傳下去★ `find_message_in_sent` 回 None = 查不出來。
    查不出來就【什麼都不做】,下一輪再試 —— 把它摺成「沒寄到」會重寄一封
    已經送達的信,摺成「有寄到」會把真正的漏寄吞掉。
    """

    def __init__(self, ledger_getter, tag: str = "", missed_alert=None):
        self._get_ledger = ledger_getter
        self._tag = tag or "delivery"
        #: 自動補寄【明確放棄】時的告警管道(who, subject, why)。
        #: 使用者不翻 log —— 「有人始終沒收到」只寫 log 等於沒說。
        #: 沒接的(主程式)至少有 error log。
        self._missed_alert = missed_alert
        #: 本 process 的節流時間戳。★只是跨 process 宣告失效時的後備★
        self.last_ts = 0.0

    # ── 一輪 ──────────────────────────────────────────────────────────────
    def run_once(self, now=None, finder=None, skip_ids=()) -> int:
        """跑一輪回查。→ 收斂了幾筆。任何一步壞掉都只 log,不往上丟。

        `skip_ids`:這一輪【不准收斂】的 delivery_id(外審 AE-3 第 3 輪
        P1)—— 呼叫端手上還有沒落地的逐位拒收時,整封粒度的回查會把
        那位收件人一起判成已送達,把證據永久蓋掉。帳本上的紀錄不完整
        就先不要下結論,下一輪再說。
        """
        led = self._get_ledger()
        if led is None:
            return 0
        now = float(now if now is not None else time.time())
        # ★逾期內文的掃除掛在這裡★(外審 AD-3 第 2 輪 P1):回查在兩支程式
        #   都是排程驅動、與寄信量無關 —— 常駐好幾週、不再寄信的行程,
        #   靠啟動掃除與 begin 交易永遠掃不到,內文就超過宣稱的保留期。
        try:
            led.scrub_stale_bodies()
        except Exception:
            logging.debug("[%s] 清逾期內文失敗(下輪再試)", self._tag,
                          exc_info=True)
        try:
            # ★卡住的 SUBMITTING 也要回查★
            #   `begin()` 已經把 SUBMITTING 落地;SMTP 送出之後、settle 之前
            #   crash,那一筆就【永久】停在 SUBMITTING。而 SUBMITTING 屬於
            #   `LIVE_STATES` —— 一旦把帳本接成寄送閘門,它會【永久擋住】
            #   同一個 business key。兩者都靠同一個 Message-ID 回查收斂,
            #   本來就該走同一個 worker。
            pending = list(led.unresolved()) + list(
                led.stuck_submitting(older_than_sec=STUCK_SUBMITTING_AFTER_SEC))
        except Exception:
            logging.debug("[%s] 讀取待回查清單失敗", self._tag, exc_info=True)
            return 0
        skip = {str(s) for s in (skip_ids or ()) if str(s)}
        skip |= self._drain_refusal_stash(led, now)
        if "*" in skip:
            # 寄存處讀不到 = 不知道有哪幾筆的帳面不完整 → 這一輪全部不碰
            logging.warning("[%s] 這一輪跳過所有收斂(見上一行)", self._tag)
            return 0
        if skip:
            # ★整封粒度的結論會蓋掉逐位的證據★ —— 帳本上還不完整的那幾筆
            #   本輪一律不碰(收斂、逾期結案、補寄都不碰)。
            pending = [r for r in pending
                       if str(r.get("delivery_id") or "") not in skip]
        # ★沒有 Message-ID 的先處理掉★(外審 2026-08-09 P2-03)
        #   它們【永遠】查不出結果 —— 回查靠的就是那個 ID。放著不管的話:
        #   `has_live_delivery()` 會把它們算成「還沒被否證」,一接上寄送閘門
        #   就【永久】擋住同一個 business key,而且沒有任何地方會說出來。
        #   給一個出口:掛超過一天就明確結案 + 大聲講。
        self._give_up_on_unverifiable(led, pending, now)
        # ★先看有沒有事做,再看節流★ 沒事做的時候不該把節流時間戳往前推。
        ripe = [r for r in pending
                if now - _created_at(r) >= MIN_AGE_SEC
                and str(r.get("message_id") or "")]
        # ★兩個來源要【合併後依年齡排序】再切上限★
        #   直接切 [:5] 的話,只要有 5 筆以上的 UNKNOWN,SUBMITTING 就
        #   【永遠輪不到】—— 而 UNKNOWN 產生的速度可能超過每 10 分鐘 5 筆的
        #   消化率。餓死的那一筆一旦接上寄送閘門,會永久擋住它的 business key。
        #   兩個 ledger 方法各自只排序自己那一份,合起來沒有全域順序。
        ripe.sort(key=_created_at)
        # ★「還欠補寄」的清單由資料庫回答★(外審 2026-08-13 P1-01/02)
        #   舊版只在「這一輪剛好查無」的 call-stack 上補寄 —— resolve 落地後、
        #   補寄建立前 crash,那筆 FAILED 就永遠沒有人再看它一眼。
        try:
            owed = list(led.resends_owed(min_age_sec=RESEND_OWED_MIN_AGE_SEC))
        except Exception:
            owed = []
            logging.debug("[%s] 讀取欠補寄清單失敗(下輪再試)", self._tag,
                          exc_info=True)
        if skip:
            owed = [r for r in owed
                    if str(r.get("delivery_id") or "") not in skip]
        if not ripe and not owed:
            return 0
        if not self._claim(led, now):
            return 0
        if finder is None and ripe:
            # 與其他 IMAP 用法同一套：在函式裡 import
            # （避免啟動時就把 ssl/imaplib 拉進來）
            from cmuh_common.imap_reader import (  # noqa: PLC0415
                find_message_in_sent,
            )
            finder = find_message_in_sent
        settled = 0
        # ★放棄之前一定要【剛剛才查過一次、而且查不出結果】★
        #   (外審第七輪 P1 的第一版自測 + 第 1 輪 P2)
        #   * 純看年齡:IMAP 壞了一天、剛剛才修好的那一刻,下一輪會在
        #     「連查都沒查」的情況下把它判成未送達 —— 已收到的人會再收一封;
        #   * 只看「這一輪查過」也不夠:查到了 True/False 卻因為 SQLite 忙碌
        #     而寫不進帳本的那一筆,狀態仍是 UNKNOWN —— 第二次寫入若剛好
        #     恢復,就會把【已確認送達】改寫成未送達。
        #   所以只收 finder 真的沒有結論的那幾筆。
        unverifiable: list = []
        for rec in ripe[:MAX_PER_PASS]:
            settled += self._settle_one(led, rec, finder, unverifiable)
        self._release_unverifiable(led, unverifiable, now)
        for rec in owed[:MAX_OWED_PER_PASS]:
            try:
                self._resend_owed_one(led, rec)
            except Exception:
                logging.warning("[%s] 驅動欠補寄 %s 失敗(下輪再試)",
                                self._tag, rec.get("delivery_id"),
                                exc_info=True)
        return settled

    def _drain_refusal_stash(self, led, now: float) -> set:
        """把寄存處裡的逐位拒收補記進帳本。→ 補不上、本輪不准收斂的 id。

        ★兩支程式的回查都會跑這裡★(外審 AE-3 第 4 輪 P1):寫下這筆
        拒收的是【會診程式】的記憶體佇列,但主程式跑的是同一本帳的回查 ——
        排除清單若只活在會診那個 process,主程式照樣會用整封粒度的結論
        把逐位證據蓋掉。寄存處是檔案,兩邊都看得到。
        ★讀不到寄存處 → 這一輪誰都不收斂★(空的當成「沒有待補記」會直接
        放行本該被擋的那幾筆);補記成功就移除;七天還補不上就放它過去
        (抑制要有出口),但大聲講。
        """
        import os  # noqa: PLC0415
        entries, ok = _load_stash_entries()
        if not ok:
            logging.warning("[%s] 讀不到逐位拒收寄存處 → 這一輪不收斂任何"
                            "紀錄(帳面可能不完整)", self._tag)
            return {"*"}            # 見下:'*' = 全部跳過的哨符
        blocked = set()
        for path, rec in entries:
            if rec is None:
                # 讀不懂:不知道它指哪一筆 → 只能全部擋住;一天後的出口。
                try:
                    age = now - os.path.getmtime(path)
                except OSError:
                    age = 0.0
                if age > _STASH_UNREADABLE_TTL_SEC:
                    self._drop_stash_file(path, "讀不懂且已過一天")
                    continue
                logging.error("[%s] ★寄存處有讀不懂的檔(%s)→ 這一輪不收斂"
                              "任何紀錄★(不知道它指哪一筆)", self._tag, path)
                return {"*"}
            did = str(rec.get("delivery_id") or "")
            refused = rec.get("refused") or {}
            try:
                led.record_refusals(did, refused)
                # ★只刪自己這一個檔★(第 5 輪 P1):共用檔的寫回會把
                #   別的 process 同時寫入的新項目一起蓋掉。
                self._drop_stash_file(path, "已補記進帳本")
                continue
            except Exception:
                pass
            if now - _as_float(rec.get("at")) > _STASH_TTL_SEC:
                # ★出口★:七天補不上就不再擋(否則一筆補不上的拒收會讓
                #   整台機器的收斂永遠停擺)。
                logging.error("[%s] ★%s 的逐位拒收七天補不進帳本 → 放棄"
                              "阻擋收斂(帳面不完整,可能誤報已送達)★",
                              self._tag, did)
                self._drop_stash_file(path, "逾期放棄")
                continue
            blocked.add(did)
        return blocked

    def _drop_stash_file(self, path: str, why: str) -> None:
        import os  # noqa: PLC0415
        try:
            os.remove(path)
        except OSError:
            logging.debug("[%s] 移除寄存檔失敗(%s)%s —— 下輪會再補記一次"
                          "(record_refusals 是冪等的)", self._tag, why, path,
                          exc_info=True)

    def _give_up_on_unverifiable(self, led, pending, now: float) -> int:
        """查不出結果又掛很久的 → 明確結案 + 大聲講。→ 結掉幾筆。

        ★為什麼一定要有這個出口(外審 2026-08-09 P2-03)★
        回查完全靠 Message-ID。沒有它的紀錄(舊版寫的、`make_msgid()` 當下
        失敗的)**永遠不會被挑進 `ripe`** —— 於是永遠停在 LIVE_STATES,
        一接上寄送閘門就永久擋住那個 business key,而且沒有任何地方會說。
        那是「沒有出口的 fail-closed」,正是 2026-08-05 事故的形狀。

        ★★有 Message-ID 的也一樣要有出口★★(外審 2026-08-18 第七輪 P1)
        上一版把「有 Message-ID」當成「查得出來」而直接跳過 —— 但
        `find_message_in_sent()` 的 `None`(查不出來)有一大堆合法來源,
        其中★IMAP 根本沒設定的機器,每一次回查都回 None★:那一筆會永遠
        停在 UNKNOWN / SUBMITTING。AE-6 之後那不只是帳面髒 —— 事件所有權
        會讓同一個 business_key 的臨床通知【永久寄不出去】(醫師重寄
        email 觸發也照樣被擋,而且觸發 journal 還會被結案)。
        所以兩種「查不出來」都給同一個 24 小時出口,只是訊息要分得開。

        ★方向要選會被人發現的那一邊★
        結成「沒送到」的代價是【可能重複寄一封】;放著不管的代價是
        【該寄的永遠不寄,而且沒人知道】。寧可重複。
        """
        done = 0
        for rec in pending:
            if str(rec.get("message_id") or ""):
                continue          # 有 ID 的走 `_release_unverifiable`(見下)
            age = now - _created_at(rec)
            if age < NO_MESSAGE_ID_GIVE_UP_SEC:
                continue
            did = str(rec.get("delivery_id") or "")
            if not did:
                continue
            try:
                led.resolve_unknown(did, delivered=False,
                                    note="沒有 Message-ID,無法查證,逾期結案")
            except Exception:
                logging.warning("[%s] 結案 %s 失敗", self._tag, did,
                                exc_info=True)
                continue
            done += 1
            logging.error("[%s] ★%s 沒有 Message-ID、已掛 %.0f 小時 → 無法查證,"
                          "結案為【未送達】並解除封鎖(主旨:%s)★"
                          "若對方其實已經收到,會收到第二封 —— 那比永遠不寄好",
                          self._tag, did, age / 3600.0, rec.get("subject", ""))
        return done

    def _release_unverifiable(self, led, attempted, now: float) -> int:
        """★這一輪真的查過、仍然查不出來、而且已經掛太久 → 結案 + 解除封鎖★

        (外審 2026-08-18 第七輪 P1)`find_message_in_sent()` 的 `None` 有
        一大堆合法來源,其中【IMAP 根本沒設定的機器每一次都回 None】——
        那一筆就永遠停在 UNKNOWN/SUBMITTING。AE-6 之後,那等於同一個
        business_key 的臨床通知【永久寄不出去】,連醫師重寄 email 觸發都
        被擋掉,而且沒有任何地方會說。

        ★兩個條件缺一不可★
        * ★這一輪剛剛查過、而且【查證本身沒有結論】★:純看年齡的話,
          「IMAP 壞了一天、剛修好」的下一輪會在【連查都沒查】的情況下判成
          未送達 —— 已收到的人會再收一封。而「查到了、只是寫不進帳本」
          (SQLite 忙碌 → `LedgerUnavailable`)也不算沒有結論:那一筆的
          答案已經有了,只是這一刻落不了地,下一輪重寫即可
          —— 混進來的話會把【已確認送達】覆寫成未送達(外審第 1 輪 P2)。
          呼叫端只把 finder 回 `None`/拋錯的那幾筆傳進來。
        * 年齡超過 `UNVERIFIABLE_GIVE_UP_SEC`:偶爾一次查不出來很正常
          (網路抖一下),要的是「持續」查不出來。

        結成「未送達」的代價是【可能重複寄一封】(而且原信會由 durable
        補寄鏈真的送出去);放著不管的代價是【該寄的永遠不寄,沒人知道】。
        """
        from cmuh_common.delivery_ledger import SUBMITTING, UNKNOWN  # noqa: PLC0415
        done = 0
        for rec in attempted:
            did = str(rec.get("delivery_id") or "")
            if not did or not str(rec.get("message_id") or ""):
                continue
            age = now - _created_at(rec)
            if age < UNVERIFIABLE_GIVE_UP_SEC:
                continue
            try:
                if led.state_of(did) not in (UNKNOWN, SUBMITTING):
                    continue          # 這一輪已經查出結果了 → 不必釋放
            except Exception:
                logging.warning("[%s] 讀不出 %s 的狀態 → 本輪不釋放",
                                self._tag, did, exc_info=True)
                continue
            try:
                led.resolve_unknown(
                    did, delivered=False,
                    note="有 Message-ID 但回查持續查不出來,逾期結案")
            except Exception:
                logging.warning("[%s] 結案 %s 失敗", self._tag, did,
                                exc_info=True)
                continue
            done += 1
            logging.error("[%s] ★%s 有 Message-ID,但已掛 %.0f 小時、剛剛"
                          "又查不出結果 → 回查這條路可能根本沒通"
                          "(IMAP 未設定/連不上/找不到寄件備份)。結案為"
                          "【未送達】並解除事件封鎖(主旨:%s)★ 對方若其實"
                          "已收到會收到第二封 —— 那比【這個事件從此再也寄不"
                          "出去】好;請檢查 IMAP 設定",
                          self._tag, did, age / 3600.0, rec.get("subject", ""))
        return done

    def _settle_one(self, led, rec, finder, unverifiable_out=None) -> int:
        """回查一筆並寫回結論。→ 收斂了幾筆(0/1)。

        `unverifiable_out`:★只有【查證本身沒有結論】的紀錄會被放進來★
        (finder 回 None、或 finder 自己拋錯)。查到了 True/False 卻寫不進
        帳本的【不算】—— 那一筆已經有答案,只是這一刻落不了地,下一輪
        重寫即可。混進去的話,逾期釋放會拿 `delivered=False` 覆蓋掉一個
        【已經確認送達】的結論,補寄鏈就對收到的人再寄一封
        (外審 AE-7 第 1 輪 P2)。
        """
        did = str(rec.get("delivery_id") or "")
        msgid = str(rec.get("message_id") or "")
        if not did:
            return 0
        try:
            found = finder(msgid)
        except Exception:
            logging.warning("[%s] 回查 %s 失敗 → 維持原狀", self._tag, did,
                            exc_info=True)
            if unverifiable_out is not None:
                unverifiable_out.append(rec)
            return 0
        if found is None:
            logging.info("[%s] %s 回查不出結果 → 維持原狀,下輪再試",
                         self._tag, did)
            if unverifiable_out is not None:
                unverifiable_out.append(rec)
            return 0
        try:
            state = led.resolve_unknown(did, delivered=bool(found))
        except Exception:
            logging.warning("[%s] 收斂 %s 失敗", self._tag, did, exc_info=True)
            return 0
        if found:
            logging.info("[%s] %s 在寄件備份查到 → 收斂為 %s",
                         self._tag, did, state)
            # ★補寄子紀錄查到=原信的那幾位其實收到了★(外審 AD-3 第 1 輪
            #   P1-1 下半):補寄當下結果不明的,現在有答案了 —— 不回寫的話
            #   原信仍掛著暫時被拒,照樣誤報漏收。
            parent = str(rec.get("parent_id") or "")
            if parent:
                # ★用【收斂後】的子紀錄狀態,不是當初嘗試的名單★
                #   (外審 2026-08-17 P1-01 下半):嘗試名單裡可能有被
                #   SMTP 明確 4xx/5xx 拒收的人 —— 整封回查只證明「這封信
                #   進了寄件備份」,證不了他收到了。拿舊名單回寫等於把
                #   明確的拒收洗成已送達,那位從此沒有補寄義務。
                from cmuh_common.delivery_ledger import (  # noqa: PLC0415
                    R_CONFIRMED as _RC,
                    R_PERMANENT as _RP,
                )
                fresh = led.get(did) or {}
                got = sorted(a for a, st in
                             (fresh.get("recipients") or {}).items()
                             if st == _RC)
                gone = sorted(a for a, st in
                              (fresh.get("recipients") or {}).items()
                              if st == _RP)
                try:
                    if got:
                        led.confirm_recipients(parent, got)
                    if gone:
                        # ★永久被拒單調往上傳★(P2-02):不然那個不存在的
                        #   信箱會一路吃完退避與補寄額度。
                        led.mark_permanently_refused(parent, gone)
                except Exception:
                    logging.warning("[%s] 回寫原信 %s 失敗(可能誤報漏收)",
                                    self._tag, parent, exc_info=True)
        else:
            logging.error("[%s] ★%s 在寄件備份【查無】→ 這封信沒有寄出去★"
                          "(主旨:%s)", self._tag, did, rec.get("subject", ""))
            # ★[外審 2026-08-13 P1-01] 用資料庫的最新狀態決定下一步★
            #   resolve 已 COMMIT(body 依新規則保留)—— 這之後 crash 的話,
            #   `resends_owed` 掃描會接手;立即補寄只是縮短等待,
            #   兩條路走的是【同一個】決策函式。任何失敗都不可打斷回查輪。
            try:
                fresh = led.get(did) or {}
                if str(fresh.get("parent_id") or ""):
                    # ★子紀錄被否證 → 欠的在【親】那一筆★(P1-02):親的
                    #   payload 還在(鏈未關)。不在這裡立刻補 —— 親可能是
                    #   PARTIAL、記憶體退避佇列還在跑它的快路徑,立即出手
                    #   會跟佇列對同一位收件人同時補寄;欠補寄掃描會照
                    #   in-flight/上限/較新同 key 的規則接手。
                    logging.warning("[%s] 補寄 %s 查無 → 親紀錄 %s 仍欠補寄,"
                                    "由欠補寄掃描接手", self._tag, did,
                                    fresh.get("parent_id"))
                else:
                    # 親紀錄查無:整封結果不明才會走到這裡,退避佇列從未
                    #   接手過這封 → 立即嘗試,不必等年齡門檻。
                    self._resend_owed_one(led, fresh)
            except Exception:
                logging.warning("[%s] 查無後的補寄驅動失敗(交給欠補寄掃描)",
                                self._tag, exc_info=True)
        return 1

    def _resend_owed_one(self, led, rec) -> str:
        """一筆欠補寄的【親】紀錄 → 補寄/等待/結鏈/放棄。→ 新 id 或 ""。

        ★這是欠補寄的唯一決策點★(外審 2026-08-13 P1-01/02/03):
        「查無後立即補」與「crash 後掃描接手」都走這裡 —— 兩邊各寫一套
        判準就會有一邊靜默失效。全部以資料庫的當下狀態為準,不吃快照。
        """
        from cmuh_common.delivery_ledger import (  # noqa: PLC0415
            CONFIRMED, FAILED, KIND_AUTO_RESEND, PARTIAL, PREPARED,
            R_CONFIRMED, R_PERMANENT, RESEND_MAX_AUTO, SUBMITTING, UNKNOWN,
            recipients_needing_retry,
        )
        did = str(rec.get("delivery_id") or "")
        if not did or str(rec.get("parent_id") or ""):
            return ""
        try:
            children = list(led.resend_children(did))
        except Exception:
            logging.warning("[%s] 讀不出 %s 的補寄子紀錄 → 本輪不補"
                            "(寧可不補,不可重複寄)", self._tag, did,
                            exc_info=True)
            return ""
        # 1) 自癒:已收斂子紀錄的送達先回寫親紀錄(冪等)。
        #    「子紀錄已確認、回寫親紀錄之前 crash」的窗口靠這裡收 ——
        #    不回寫的話親紀錄還掛著暫時被拒,會再補一次=重複的臨床通知。
        for c in children:
            if str(c.get("state")) not in (CONFIRMED, PARTIAL, FAILED):
                continue
            got = sorted(a for a, st in (c.get("recipients") or {}).items()
                         if st == R_CONFIRMED)
            # ★永久被拒也要往上傳★(外審 2026-08-17 P2-02):FAILED 的子紀錄
            #   也可能帶著 550(查無此人)—— 那是比暫時被拒更強的結論。
            gone = sorted(a for a, st in (c.get("recipients") or {}).items()
                          if st == R_PERMANENT)
            if not got and not gone:
                continue
            try:
                if got:
                    led.confirm_recipients(did, got)
                if gone:
                    led.mark_permanently_refused(did, gone)
            except Exception:
                logging.warning("[%s] 回寫 %s 的收件人結論失敗",
                                self._tag, did, exc_info=True)
        rec = led.get(did) or {}
        # ★[外審第二輪 R2-P2-05] 落地內文解不開★(settings/ 被離機複製、
        #   DPAPI 金鑰換了、密文毀損)。「讀不出來」≠「鏈已關」:直接當
        #   空字串會把一封欠著的臨床通知★靜默★結案。但也★不可以在這裡
        #   就放棄★(deep R1 P1):較新同 key 紀錄可能已把信送到、或有
        #   子紀錄正在 in-flight —— 先放棄+告警會誘導人工重寄=重複的
        #   臨床通知。旗標帶著走,穿過下面既有的接手/互斥守衛,最後併入
        #   額度用盡共用的「明確放棄」漏斗(判準與出口只留一份)。
        unreadable = bool(rec.get("body_unreadable"))
        body = str(rec.get("body_text") or "")
        if (not body and not unreadable)                 or str(rec.get("state") or "") == CONFIRMED:
            return ""                   # 鏈已關(全數送達或 payload 已清)
        targets = recipients_needing_retry(rec.get("recipients") or {})
        if not targets:
            # 剩下的是永久被拒/已送達 → 沒有可自動補的人,結鏈。
            try:
                led.clear_body(did, note="無暫時性待補收件人,補寄鏈結案")
            except Exception:
                logging.warning("[%s] 結鏈 %s 失敗(下輪再試)", self._tag,
                                did, exc_info=True)
            return ""
        # 2) ★同一把 key 只留最新一條鏈,但接手要有本錢★(AE-1 R1 P1-1 +
        #    R3 P1-03):較新者已送達或自己有 body 才能接走義務;混版部署
        #    匯入的 bodyless 較新紀錄若光憑「較新」就讓這裡 clear_body,
        #    唯一的 durable payload 就被刪掉 = 永久漏寄。
        bk = str(rec.get("business_key") or "")
        if bk:
            try:
                verdict, sib_delivered, newer_id = led.newer_sibling_takeover(
                    bk, than_created_at=_created_at(rec))
            except Exception:
                # ★查不出 ≠ 可接手★(外審 AE-1 第 1 輪 P1-2):結鏈會刪掉
                #   唯一的 payload,不可逆 —— 讀取失敗只能【跳過本輪】,
                #   body 原封不動,下輪再試。
                logging.warning("[%s] 查不出 %s 的較新同 key 紀錄 → 本輪"
                                "不補也不結鏈(結鏈不可逆)", self._tag, did,
                                exc_info=True)
                return ""
            if verdict == "takeover":
                # ★接手要記成顯式狀態★(外審 2026-08-17 P2-01):只清 body
                #   的話,結案路徑會看到「還有暫時被拒的人 + 沒有 payload」
                #   而寄出「始終沒收到,請人工轉寄」—— 但那封信剛剛已經由
                #   較新的紀錄送到了,告警反而誘導人工重寄。
                try:
                    if led.supersede(did, by=newer_id,
                                     note="同 business_key 已有較新的紀錄"
                                          "接手,本鏈結案"):
                        logging.info("[%s] %s 的同 key 已有較新紀錄接手 →"
                                     " 本鏈結案", self._tag, did)
                    else:
                        # ★有 in-flight 子紀錄 → 這一輪還不能交棒★
                        #   (外審 AE-5 第 1 輪 P1):那封可能正在寄,
                        #   交棒會讓兩條鏈同時對同一個人出手。
                        logging.info("[%s] %s 還有結果未定的補寄 → 這一輪"
                                     "先不交棒(下輪再試)", self._tag, did)
                except Exception:
                    logging.warning("[%s] 結鏈 %s 失敗(下輪再試)",
                                    self._tag, did, exc_info=True)
                return ""
            if verdict == "wait":
                # 較新的 bodyless 紀錄結果未定 —— 它可能已送達;現在補是
                #   潛在重複、現在結鏈是潛在漏寄。等回查把它收斂。
                logging.info("[%s] %s 的較新同 key 紀錄結果未定 → 本輪等待",
                             self._tag, did)
                return ""
            if sib_delivered:
                # ★沒本錢接手,但它送到的人要先回寫★(外審 AE-3 第 1 輪
                #   F2):bodyless PARTIAL sibling 送達了 A、暫時被拒 B ——
                #   舊鏈接回義務時若不回寫,A 會再收一封臨床通知。
                #   (claim 交易內也擋得住;這裡讓帳面誠實,不靠縱深。)
                try:
                    done = led.confirm_recipients(did, list(sib_delivered))
                    if done:
                        logging.info("[%s] 較新同 key 紀錄已送達 %s → 回寫"
                                     "%s(這幾位不再補寄)", self._tag,
                                     ", ".join(done), did)
                    rec = led.get(did) or rec
                except Exception:
                    logging.warning("[%s] 回寫較新紀錄的已送達收件人失敗"
                                    "(claim 交易仍會擋)", self._tag,
                                    exc_info=True)
                targets = recipients_needing_retry(
                    rec.get("recipients") or {})
                if not targets:
                    try:
                        led.supersede(did, by=newer_id,
                                      note="較新紀錄已送達所有待補收件人,"
                                           "補寄鏈結案")
                    except Exception:
                        logging.warning("[%s] 結鏈 %s 失敗(下輪再試)",
                                        self._tag, did, exc_info=True)
                    return ""
        # 3) in-flight 互斥(claim 交易內會再驗;這裡只是省一次 send 準備)。
        if any(str(c.get("state")) in (SUBMITTING, PREPARED, UNKNOWN)
               for c in children):
            return ""
        # 4) ★抑制的出口★ 上限一到就明確放棄+告警,不無聲、不無限追打。
        #    ★額度只數真正進入 SMTP 的嘗試(attempts>0)★(外審 R3
        #    P1-01):claim 後、send 前 crash 的子紀錄不扣額度 —— 不然
        #    連續兩次這種 crash 就能在【零次 SMTP】的情況下 abandon 一封
        #    臨床通知。claim 總數另有硬背擋(反覆 claim=那台機器壞了)。
        from cmuh_common.delivery_ledger import (  # noqa: PLC0415
            RESEND_MAX_CLAIMS, _as_attempts,
        )
        autos = [c for c in children
                 if str(c.get("kind") or "") == KIND_AUTO_RESEND]
        started = sum(1 for c in autos
                      if _as_attempts(c.get("attempts")) > 0)
        why = ""
        if started >= RESEND_MAX_AUTO:
            why = "自動補寄已達 %d 次(實際進入 SMTP)上限仍未送達" \
                  % RESEND_MAX_AUTO
        elif len(autos) >= RESEND_MAX_CLAIMS:
            why = ("自動補寄 claim 已達 %d 次硬背擋 —— 反覆在 claim 與"
                   " send 之間中斷,這台機器需要人工檢查" % RESEND_MAX_CLAIMS)
        elif unreadable:
            # 走到這裡=沒有較新紀錄可接手、也沒有子紀錄在飛 —— 這條鏈
            # 只剩解不開的密文,沒有內容可補。唯一誠實的出口。
            why = "落地內文無法解密(DPAPI),自動補寄沒有內容可用"
        if why:
            subject = str(rec.get("subject") or "")
            try:
                gone = led.abandon_recipient_retry(
                    did, note=why + " → 明確放棄")
            except Exception:
                logging.warning("[%s] 放棄 %s 的補寄失敗(下輪再試)",
                                self._tag, did, exc_info=True)
                return ""
            if gone:
                logging.error("[%s] ★%s,明確放棄★:%s 始終沒收到"
                              "(主旨:%s)—— 請人工確認/轉寄",
                              self._tag, why, ", ".join(gone), subject)
                if self._missed_alert is not None:
                    try:
                        self._missed_alert(gone, subject, why)
                    except Exception:
                        logging.warning("[%s] 漏收告警管道失敗", self._tag,
                                        exc_info=True)
            return ""
        return self._resend_from_body_text(led, rec)

    def _resend_from_body_text(self, led, rec) -> str:
        """用帳上落地的文字內容自動補寄一封。→ 新 delivery_id 或 ""。

        ★只有文字,沒有附件★(使用者定案 2026-08-13):會診通知的附件是
        PHI 截圖,依既有隱私定案【不落地】—— 信裡註明請至 HIS 查看。

        有界性(不可能變成補寄迴圈;外審 2026-08-13 P1-02 改版):
        * 補寄紀錄自己(有 parent_id)★永不★再被自動補寄 —— 欠的永遠
          記在【親】紀錄上;
        * 同時最多一封 in-flight、自動補寄最多 RESEND_MAX_AUTO 次:
          由 `claim_resend_child` 在交易內仲裁(跨重啟、跨 process 都
          算得出來 —— 子紀錄與 kind 都在資料庫裡);
        * `body_text` 空(舊版寫的紀錄/鏈已關)→ 沒得補,告警即止。
        """
        did = str(rec.get("delivery_id") or "")
        if not did:
            return ""
        # ★重新讀資料庫,不用呼叫端手上的快照★(外審 2026-08-13 P1-01):
        #   快照跨越了 resolve 的 COMMIT —— crash 之後重來時只剩資料庫;
        #   平時與 crash 後要走同一條路,才是同一個被測行為。
        rec = led.get(did) or {}
        if str(rec.get("parent_id") or ""):
            return ""                    # 補寄的補寄 → 不做(欠的在親紀錄)
        body = str(rec.get("body_text") or "")
        if not body:
            return ""                    # 沒有落地文字 → 沒得補(或鏈已關)
        # ★只補給【暫時性被拒】的人★ 已送達的再寄一次是重複的臨床通知;
        #   永久被拒(5xx)重寄也不會變好(要人工改設定,告警已在);
        #   UNKNOWN 的要先回查,不可盲寄。
        from cmuh_common.delivery_ledger import (  # noqa: PLC0415
            recipients_needing_retry,
        )
        addrs = recipients_needing_retry(rec.get("recipients") or {})
        if not addrs:
            return ""
        from email.utils import make_msgid  # noqa: PLC0415
        from cmuh_common.smtp_mail import (  # noqa: PLC0415
            DeliveryOutcomeUnknown, send_mail,
        )
        subject = str(rec.get("subject") or "")
        try:
            new_msgid = make_msgid()
        except Exception:
            new_msgid = ""
        note = ("\n\n——\n本封為【自動補寄】:原信經寄件備份查證未送達。\n"
                "原信附件依隱私政策未保留 —— 如需會診單畫面,請逕至 HIS 查看。")
        try:
            # ★「查子紀錄+登記」是同一筆交易★(外審 AD-3 第 1 輪 P1-2):
            #   兩支程式的回查同時走到這裡時,拆兩步就是 TOCTOU ——
            #   兩邊都查到「沒補過」,各寄一封重複的臨床通知。
            new_did = led.claim_resend_child(
                did, business_key=str(rec.get("business_key") or ""),
                category=str(rec.get("category") or ""),
                recipients=list(addrs), subject=subject,
                message_id=new_msgid)
        except Exception:
            # 登記不了就不寄:沒有帳的補寄,查無時會再補一次 → 破壞有界性。
            logging.warning("[%s] 補寄 %s 登記失敗 → 本輪不補(下輪也不會重複,"
                            "原信已收斂為 FAILED)", self._tag, did,
                            exc_info=True)
            return ""
        if not new_did:
            return ""            # in-flight/已送達/額度已盡(交易內仲裁)
        # ★寄給交易核定的名單★(外審 R3 P1-02):claim 可能把名單縮小
        #   (這一瞬間有人被別的 sender 送達了)—— 手上的 addrs 是舊的。
        #   ★讀不到就不寄★(外審 AE-3 第 1 輪 F3):`get()` 讀不到回 {},
        #   沿用舊名單會寄給剛被別人送達的人,而且那位不在子紀錄的帳上。
        addrs = sorted((led.get(new_did) or {}).get("recipients") or {})
        if not addrs:
            logging.warning("[%s] 補寄 %s claim 後讀不回名單 → 本輪不寄"
                            "(子紀錄交給卡住收斂,不扣額度)",
                            self._tag, new_did)
            return ""
        try:
            refused = send_mail(
                recipients=list(addrs), subject=subject, body=body + note,
                category=("system" if rec.get("category") == "system"
                          else "clinical"),
                message_id=new_msgid,
                # ★只留一層重試★(外審 AE-4 第 2 輪 P2):內層預設會再試
                #   兩次 —— 全部 421 時一個子紀錄就做了 3 次 RCPT,兩個
                #   auto 子紀錄=6 次,`RESEND_MAX_AUTO=2` 形同虛設。
                #   補寄的重試機制在【外層】(claim + 額度 + 退避)。
                max_retries=0,
                # ★逐位結果在 DATA 之前落地,同時劃下嘗試邊界★
                #   (外審 2026-08-17 P1-01/P2-03):跨過 RCPT 才算一次
                #   真正的 SMTP 嘗試(額度數這個);而被拒的那幾位在信送出
                #   之前就成為 durable 事實 —— 之後就算立刻斷電,整封回查
                #   也不會把他們一起判成已送達。
                on_rcpt_result=self._rcpt_recorder(led, new_did, did),
                require_durable_rcpt=True)
        except DeliveryOutcomeUnknown:
            self._settle_quietly(led, new_did, unknown=True)
            logging.warning("[%s] 補寄 %s 結果不明 → 交給下一輪回查",
                            self._tag, new_did)
            return new_did
        except Exception as e:
            # ★縱深:逐位的碼要保留★(外審 AE-4 第 1 輪 P2)——「全部收件人
            #   被拒」是 SMTPRecipientsRefused,例外身上就帶著 550/421;
            #   摺成 generic failed 會把「查無此人」記成暫時被拒,那個信箱
            #   會一路吃完額度,最後的告警還說它是暫時性的。
            from cmuh_common.smtp_mail import (  # noqa: PLC0415
                recipients_refused_map,
            )
            _bad = recipients_refused_map(e)
            # ★只有涵蓋【全部】收件人時才用逐位結果★:`settle(refused=…)`
            #   會把不在 map 裡的人標成【已送達】—— 這裡明明整封都沒送出去。
            #   SMTPRecipientsRefused 依定義涵蓋全部,不涵蓋就是拿到殘缺的
            #   資訊,那寧可用 generic failed(它永不推翻已有的 PERMANENT)。
            if _bad and set(_bad) == {str(a).strip().lower() for a in addrs}:
                self._settle_quietly(led, new_did, refused=_bad)
            else:
                self._settle_quietly(led, new_did, failed=True)
            logging.error("[%s] ★補寄 %s 也失敗★(原信 %s,主旨:%s)",
                          self._tag, new_did, did, subject, exc_info=True)
            return new_did
        self._settle_quietly(led, new_did, refused=refused or {})
        # ★把補寄成功的人回寫到【原信】★(外審 AD-3 第 1 輪 P1-1)
        #   不回寫的話,原信的收件人停在暫時被拒 → 一小時後被
        #   「始終沒收到」的結案路徑誤報漏收 → 人工照著轉寄 = 重複通知。
        delivered = [a for a in addrs if a not in (refused or {})]
        if delivered:
            try:
                led.confirm_recipients(did, delivered)
            except Exception:
                logging.warning("[%s] 補寄成功但回寫原信 %s 失敗"
                                "(可能誤報漏收)", self._tag, did,
                                exc_info=True)
        logging.warning("[%s] ★已自動補寄★ %s(原信 %s 查無,主旨:%s,"
                        "收件人 %d 位,只有文字、無附件)",
                        self._tag, new_did, did, subject, len(addrs))
        return new_did

    @staticmethod
    def _rcpt_recorder(led, child_id: str, parent_id: str = ""):
        """做出「RCPT 全部回答完、DATA 之前」要落地的那筆事實。→ callback。

        ★[外審 2026-08-17 P1-01/P2-03]★ 落地順序有意義:
        (1) 先把逐位拒收寫進子紀錄與親紀錄 —— 這是 crash 之後唯一能證明
            「他沒收到」的東西(整封 Message-ID 回查證不了逐位);
        (2) 都成功了才 `mark_submitting`(attempts+1)= ★真正跨過 SMTP
            protocol boundary 的嘗試★,額度數的是它。順序反過來的話,
            拒收沒寫成功卻先扣掉額度,等於用額度換沉默。
        任何一步失敗回 False → 呼叫端(補寄路徑)在 DATA 之前中止,
        信一個 byte 都沒送出,子紀錄 attempts 仍是 0(不扣額度)。
        冪等:同一次寄送重試時會被再呼叫一次。
        """
        def _record(accepted, refused):
            # ★三件事同一筆交易★(外審 AE-5 第 1 輪 P2):子紀錄拒收 +
            #   親紀錄分類升級(550 單調升、421 只補沒有結論的)+ 嘗試
            #   邊界。拆開寫的話會出現「子已 permanent、親還是 transient」
            #   的中間狀態 —— claim 看子紀錄拒絕補寄、佇列看親紀錄繼續
            #   退避,最後用「暫時性拒收用盡」的語氣告警一個確定不存在的
            #   信箱。落不了地 → 整筆回捲 → 呼叫端在 DATA 之前中止。
            return bool(led.record_rcpt_outcome(child_id, parent_id,
                                                dict(refused or {})))
        return _record

    def _settle_quietly(self, led, did, **kw) -> None:
        """補寄的結果照常入帳;入不了帳也不可以打斷回查輪。"""
        try:
            led.settle(did, **kw)
        except Exception:
            logging.warning("[%s] 補寄 %s 的結果入帳失敗(停在 SUBMITTING,"
                            "由回查收斂)", self._tag, did, exc_info=True)

    # ── 節流／宣告 ────────────────────────────────────────────────────────
    def _claim(self, led, now: float) -> bool:
        """這一輪歸不歸我跑。搶到才回 True。

        ★宣告失敗【不可以】變成永久沉默★
        帳本沒有 `claim_reconcile_pass`(舊版)、或那支自己壞掉時,退回
        本 process 的記憶體節流並照跑 —— 那正是這個修正之前的行為,
        不會比現況更差;而「搶不到就永遠不回查」會把一個看得見的
        競態換成一個安靜的漏收斂(2026-08-05 事故的形狀)。
        """
        claim = getattr(led, "claim_reconcile_pass", None)
        if claim is not None:
            try:
                if not claim(now=now, every_sec=EVERY_SEC):
                    return False
                self.last_ts = now
                return True
            except Exception:
                logging.warning("[%s] 跨 process 宣告回查失敗 → 退回本行程節流",
                                self._tag, exc_info=True)
        if now - self.last_ts < EVERY_SEC:
            return False
        self.last_ts = now
        return True
