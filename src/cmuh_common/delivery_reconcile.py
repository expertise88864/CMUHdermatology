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

    def __init__(self, ledger_getter, tag: str = ""):
        self._get_ledger = ledger_getter
        self._tag = tag or "delivery"
        #: 本 process 的節流時間戳。★只是跨 process 宣告失效時的後備★
        self.last_ts = 0.0

    # ── 一輪 ──────────────────────────────────────────────────────────────
    def run_once(self, now=None, finder=None) -> int:
        """跑一輪回查。→ 收斂了幾筆。任何一步壞掉都只 log,不往上丟。"""
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
        if not ripe:
            return 0
        if not self._claim(led, now):
            return 0
        if finder is None:
            # 與其他 IMAP 用法同一套：在函式裡 import
            # （避免啟動時就把 ssl/imaplib 拉進來）
            from cmuh_common.imap_reader import (  # noqa: PLC0415
                find_message_in_sent,
            )
            finder = find_message_in_sent
        settled = 0
        for rec in ripe[:MAX_PER_PASS]:
            settled += self._settle_one(led, rec, finder)
        return settled

    def _give_up_on_unverifiable(self, led, pending, now: float) -> int:
        """沒有 Message-ID 又掛很久的 → 明確結案 + 大聲講。→ 結掉幾筆。

        ★為什麼一定要有這個出口(外審 2026-08-09 P2-03)★
        回查完全靠 Message-ID。沒有它的紀錄(舊版寫的、`make_msgid()` 當下
        失敗的)**永遠不會被挑進 `ripe`** —— 於是永遠停在 LIVE_STATES,
        一接上寄送閘門就永久擋住那個 business key,而且沒有任何地方會說。
        那是「沒有出口的 fail-closed」,正是 2026-08-05 事故的形狀。

        ★方向要選會被人發現的那一邊★
        結成「沒送到」的代價是【可能重複寄一封】;放著不管的代價是
        【該寄的永遠不寄,而且沒人知道】。寧可重複。
        """
        done = 0
        for rec in pending:
            if str(rec.get("message_id") or ""):
                continue
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

    def _settle_one(self, led, rec, finder) -> int:
        did = str(rec.get("delivery_id") or "")
        msgid = str(rec.get("message_id") or "")
        if not did:
            return 0
        try:
            found = finder(msgid)
        except Exception:
            logging.warning("[%s] 回查 %s 失敗 → 維持原狀", self._tag, did,
                            exc_info=True)
            return 0
        if found is None:
            logging.info("[%s] %s 回查不出結果 → 維持原狀,下輪再試",
                         self._tag, did)
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
                try:
                    led.confirm_recipients(
                        parent, list(rec.get("recipients") or {}))
                except Exception:
                    logging.warning("[%s] 回寫原信 %s 失敗(可能誤報漏收)",
                                    self._tag, parent, exc_info=True)
        else:
            logging.error("[%s] ★%s 在寄件備份【查無】→ 這封信沒有寄出去★"
                          "(主旨:%s)", self._tag, did, rec.get("subject", ""))
            # ★[外審 2026-08-12 P1-05] 知道沒寄到之後,拿落地的文字補寄★
            #   (收斂已經成功落地才走到這裡 —— 補寄失敗也不會重複,
            #    原信誠實地停在 FAILED + 上面那行告警。)
            self._resend_from_body_text(led, rec)
        return 1

    def _resend_from_body_text(self, led, rec) -> str:
        """Sent 查無 → 用帳上落地的文字內容自動補寄一封。→ 新 delivery_id 或 ""。

        ★只有文字,沒有附件★(使用者定案 2026-08-13):會診通知的附件是
        PHI 截圖,依既有隱私定案【不落地】—— 信裡註明請至 HIS 查看。

        有界性(不可能變成補寄迴圈):
        * 補寄紀錄自己(有 parent_id)★永不★再被自動補寄;
        * 原信已有子紀錄(任何狀態)就不再補 → 每筆原信最多一次,
          跨重啟也算得出來(子紀錄在資料庫裡);
        * `body_text` 空(舊版寫的紀錄)→ 沒得補,維持告警即止的舊行為。
        """
        did = str(rec.get("delivery_id") or "")
        if str(rec.get("parent_id") or ""):
            return ""                    # 補寄的補寄 → 不做(有界)
        body = str(rec.get("body_text") or "")
        if not body:
            return ""                    # 舊紀錄沒有落地文字 → 沒得補
        # 只補給【還沒確認送達】的人 —— 已送達的再寄一次就是重複的臨床通知
        addrs = sorted(a for a, st in (rec.get("recipients") or {}).items()
                       if st != "confirmed")
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
            return ""                    # 已補過一次(可能是另一支程式補的)
        try:
            refused = send_mail(
                recipients=list(addrs), subject=subject, body=body + note,
                category=("system" if rec.get("category") == "system"
                          else "clinical"),
                message_id=new_msgid)
        except DeliveryOutcomeUnknown:
            self._settle_quietly(led, new_did, unknown=True)
            logging.warning("[%s] 補寄 %s 結果不明 → 交給下一輪回查",
                            self._tag, new_did)
            return new_did
        except Exception:
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
