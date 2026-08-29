# -*- coding: utf-8 -*-
"""外部動作稽核帳本(ExternalActionGateway 第一片)。

【動機】使用者定案(2026-07-17):偵測到院方改版時【不擋自動寫入】,只寄信通知。預防性控制
既然拿掉,補償控制就必須是【偵測性】的:每一次真的動到 HIS/外部系統的動作都留下結構化紀錄。
院方哪天悄悄改版把醫令寫錯,才查得出「幾點、哪支熱鍵、寫了什麼值、當時 HIS 版本與金絲雀
裁決是什麼、回讀對不對」,而不是靠回憶。這也是 GPT-5.6 P0#6(Audit Ledger)的落地。

【設計】
* append-only JSONL + hash chain + 單調 seq:每筆含前一筆的 hash 與遞增 seq。
* 【絕不拋例外】:任何失敗只吞掉記 debug、回 False —— 稽核不可以弄壞臨床功能。
  註:本類別的 record() 是【同步】的(會鎖、會做檔案 IO)。呼叫端若在熱鍵/UI 緒上,
  必須自己丟到背景緒(見 main.py `_record_his_action` 的非阻塞佇列),否則檔案 IO 卡住
  會連帶卡住臨床流程 —— 「不拋例外」不等於「不阻塞」。
* 【不存病人明文識別】:只記非 PII 的動作與值。★這件事現在由【型別】保證,不再靠註解★
  —— value/detail 只接受 `cmuh_common.audit_events` 的型別(Code/Measure/Observed/
  Transition/Redacted/Reason);其他東西一律記成 violation 且【內容不落地】。
  想描述「從 HIS 讀到什麼」的唯一表達方式是 `Observed(length=…)`,它只存長度。
  (2026-07-31 P2-03 取代舊的 denylist regex `sanitize_text` —— 猜不準、誤遮無聲,
  詳見 audit_events 模組開頭。)
* 大小上限 + 輪替(保留數代);輪替失敗時有【硬上限】兜底(超過就丟紀錄不再長大,寧可少記
  也不要塞爆診間電腦磁碟)。

【截尾/截頭偵測】(codex P1):鏈本身無法自證「後面還有沒有」,故另寫一個 anchor 側檔
(<ledger>.anchor.json)記最後的 seq/hash;verify_generations() 會比對「留存紀錄的末筆」
與 anchor。少了尾巴 → 對不上 → 判定疑遭截尾。截頭則靠:最舊留存段的首筆若 prev=genesis,
其 seq 必須是 1(否則前面被刪了)。

【誠實邊界】anchor 與帳本同在 settings/ 下:有檔案寫入權的人可以把兩者【一起】改掉,
本機日誌本質上擋不住這種等級的竄改(要防需外部/遠端 append-only 儲存或離線錨定,不在本
片範圍)。本設計的實際威脅模型是「意外遺失/截斷(當機、磁碟滿、工具截檔)與非蓄意的改動」,
這些都抓得到。另:輪替已淘汰掉的世代無法回溯(那是預期行為,不算竄改)。
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
import os
import threading
from datetime import datetime

from cmuh_common.atomic_io import atomic_write_json
from cmuh_common.audit_events import Reason, to_field_payload

# 2 = value/detail 由自由文字改為型別化事件(dict);1 = 舊的字串欄位。
# ★舊紀錄仍然驗得過★ chain_hash 與 _canonical 是欄位無關的(照 rec 的實際內容重算),
# 所以輪替中同時存在 v1/v2 的世代不影響 verify_generations。
SCHEMA_VERSION = 2
LEDGER_FILENAME = "action_ledger.jsonl"
ANCHOR_SUFFIX = ".anchor.json"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024      # 5MB 後輪替
DEFAULT_KEEP = 3                          # 保留 .1 .2 .3
GENESIS = "genesis"

# 面向(surface)
# ★[外審 R2-P1-01] 帳本自身的事件★:斷電讓「anchor 已 durable、它指的紀錄
#   沒落盤」時,重開後【不重用】那些 seq,並在下一次寫入前先記一筆這個事件,
#   把缺口寫成帳本裡永久可查的事實(而不是靠一行 log)。verify 認得它,
#   因此缺口既不會被抹掉、也不會變成一道永遠紅、沒有出口的閘門。
SURFACE_LEDGER = "ledger"            # 帳本自身
ACTION_DURABILITY_GAP = "durability_gap"
SURFACE_HIS_MENU = "his_menu"        # 送選單 command(醫令代碼/完成/同意書)
SURFACE_HIS_FIELD = "his_field"      # 寫欄位(療程/身份/卡號/劑量 memo)

# 結果(outcome)
# [GPT-5.6 第三輪] 「PostMessage 被 Windows 接受」不等於「HIS 動作成功」:控制項可能已
# 切換、佇列可能滿、Enter 可能沒被處理、醫令可能被拒。把兩者都記成 ok 會讓帳本產生
# 錯誤安全感(比沒有帳本更糟)。故區分:
#   ok                    = 有【回讀/可觀察證據】確認動作結果(療程/身份/卡號/UVB 的
#                           read-verify、同意書視窗真的開出來)
#   submitted_unverified  = 訊息已成功送出(PostMessage 非 0),但【無法確認】HIS 真的
#                           處理了 —— 無回讀路徑(醫令代碼、F11 完成)最多只能記到這級
#   mismatch              = 回讀與預期不符 —— 最重要的訊號
#   failed                = 送出/寫入本身失敗(PostMessage 回 0、WM_SETTEXT 失敗…)
#   skipped               = 前置條件不成立,沒有真的寫
#   unknown               = 呼叫端沒宣告 —— 【預設】。預設不能是 ok:忘了傳 outcome
#                           就自動產生假成功紀錄,是不安全預設。
OUTCOME_OK = "ok"
OUTCOME_SUBMITTED_UNVERIFIED = "submitted_unverified"
OUTCOME_MISMATCH = "mismatch"
OUTCOME_FAILED = "failed"
OUTCOME_SKIPPED = "skipped"
OUTCOME_UNKNOWN = "unknown"

# 稽核紀錄的字串欄位(hash 計算範圍;順序無關,canonical json 會排序)
_FIELDS = ("target", "value", "his_version", "canary", "outcome", "detail",
           "correlation_id", "app_version")


def _canonical(d: dict) -> str:
    """穩定序列化(排序鍵、無多餘空白)——hash chain 與寫檔共用,確保可重算。

    ★[2026-08-01 外審 P2] `allow_nan=False`★
    預設的 `allow_nan=True` 會吐出 `NaN` / `Infinity` / `-Infinity`，那三個都**不是
    合法 JSON**。帳本是要給別的工具讀的(verifier／日後的分析腳本)，寫進去等於那一
    行從此解析不了 —— 而它還在 hash chain 上，後面每一行的驗證都會跟著卡住。
    上游的 `audit_events._numbers_ok` 已經擋掉非有限浮點數，這裡是第二道：
    ★守衛不可以只有一層，因為帳本也收得到不經過 audit_events 的 dict。★
    真的寫不出來時寧可拋例外讓呼叫端記成失敗，也不要落一行壞資料。
    """
    return json.dumps(d, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


# ── PII 縱深防禦 ─────────────────────────────────────────────────────────────
# [2026-07-31 第二輪外審 P2-03] 這裡原本是 `sanitize_text` + 兩條 denylist regex
# (台灣身分證樣式、8 位以上連續數字),在落地前猜「哪一段像個資」。已刪除,理由:
#   * 猜不準:中文姓名/地址/7 位病歷號/生日都不符樣式,照樣落地 —— 而它宣稱防的
#     「呼叫端誤傳採樣原文」恰好是它最擋不住的(F11 讀療程欄完全沒把關,定位漂到
#     姓名欄時姓名就進帳本,那兩條 regex 一個字都攔不到)。
#   * 誤遮無聲:院方哪天發 8 位醫令代碼,整欄集體變 [REDACTED] 而沒有任何訊號 ——
#     偏偏那正是要查「改版把醫令寫錯」的時候。
# 現在改成【型別化事件】:見 cmuh_common.audit_events。防線從「事後猜」移到
# 「呼叫端必須宣告這個值是什麼」。


def chain_hash(prev_hash: str, payload: dict) -> str:
    """純函式:由前一筆 hash + 本筆內容算 chain hash(好測)。"""
    return hashlib.sha256(
        (str(prev_hash) + _canonical(payload)).encode("utf-8")).hexdigest()


def _machine() -> str:
    try:
        return str(os.environ.get("COMPUTERNAME") or "")
    except Exception:
        return ""


def _user() -> str:
    try:
        return str(os.environ.get("USERNAME") or "")
    except Exception:
        return ""


def _last_state_of(path: str):
    """回該檔最後一筆【完整】紀錄的 (hash, seq);檔不存在/無有效行回 None。不拋。

    ★[外審 R2-P1-01] 末行殘缺不可以毒害整個檔案★:斷電可能只寫了半行
    (`{"schema_version":2,"seq":101,"ts":"2026-...`)。原本直接對末行
    `json.loads`,一失敗就整個檔案回 None → 續寫起點退到 `.1` 甚至 genesis,
    單純的尾端 torn write 被放大成「之後整條鏈接錯地方」。
    改成★由後往前找到最後一筆解析得動的紀錄★ —— torn tail 只影響它自己。
    """
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            lines = [ln for ln in f.read().splitlines() if ln.strip()]
        for ln in reversed(lines):
            try:
                rec = json.loads(ln)
            except (ValueError, TypeError):
                continue                    # 殘缺行:略過它、繼續往前找
            if not isinstance(rec, dict) or not rec.get("hash"):
                continue
            return (str(rec.get("hash") or GENESIS), int(rec.get("seq") or 0))
        return None
    except Exception:
        logging.debug("[ledger] 讀取 %s 末筆失敗", path, exc_info=True)
        return None


def _tail_is_torn(path: str) -> bool:
    """檔案是否以【沒有換行結尾】的殘缺行收尾(斷電的典型形狀)。

    ★這件事必須在 append 之前處理★:直接 append 會把新紀錄接在半行後面、
    兩筆黏成一行 —— 連新的那一筆也跟著讀不出來(寫測試時實際踩到)。
    """
    try:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return False
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            return f.read(1) not in (b"\n", b"\r")
    except Exception:
        logging.debug("[ledger] 檢查 %s 尾端失敗", path, exc_info=True)
        return False


def _torn_meta_of(side: str) -> dict:
    """殘片側檔的內容({"tail_seq": 截斷當下的末筆 seq, "fragment": 原殘片})。

    ★解析不出來的標記不算數★:標記是用 `atomic_write_json` 寫的(temp+fsync+
    replace),所以只有「完整存在」與「不存在」兩種狀態 —— 讀不出內容代表它
    不是本機制寫的東西,拿它當一筆損失會憑空多報。
    """
    try:
        with open(side, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and "fragment" in d else {}
    except Exception:
        logging.debug("[ledger] 殘片側檔讀取失敗:%s", side, exc_info=True)
        return {}


def _torn_is_recorded(base: str, keep: int, side: str) -> bool:
    """這個殘片標記的損失,是不是★已經★有一筆缺口紀錄涵蓋了。

    ★判準只有一份★:復原(要不要再記)與健康檢查(要不要多數一筆)問的是
    同一個問題 —— 兩邊各寫一套的話遲早只有一邊被修好。
    掃所有還留著的世代(缺口可能已被輪替搬進 `.2`/`.3`;外審 deep R4-3)。
    """
    t = _torn_meta_of(side).get("tail_seq")
    if t is None:
        return False
    for p in [base] + [f"{base}.{i}" for i in range(1, int(keep) + 1)]:
        for rec in read_records(p):
            d = rec.get("detail")
            if (str(rec.get("action")) == ACTION_DURABILITY_GAP
                    and isinstance(d, dict) and d.get("torn")
                    and d.get("tail_seq") == t):
                return True
    return False


def _torn_pending_files(base: str) -> list:
    """★還沒結案的殘片側檔★(`.torn-N`;結案後改名為 `.torn-N.resolved`)。

    這是「有一筆稽核沒寫完、尚未記入帳本」的 durable 標記,健康檢查與復原
    都讀它(模組層函式:`health_snapshot` 沒有 ActionLedger 實例)。
    """
    out = []
    try:
        for i in range(1, 1000):
            p = f"{base}.torn-{i}"
            if os.path.exists(p) and _torn_meta_of(p):
                out.append(p)
    except Exception:
        logging.debug("[ledger] 列舉殘片側檔失敗", exc_info=True)
    return out


def _first_seq_of(path: str):
    """回該檔第一筆的 seq;檔不存在/無有效行回 None。不拋。"""
    try:
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    return int(json.loads(line).get("seq") or 0)
    except Exception:
        logging.debug("[ledger] 讀取 %s 首筆失敗", path, exc_info=True)
    return None


def _compute_oldest_seq(base: str, keep: int) -> int:
    """[codex P1] 目前【還留著】的最舊 seq(輪替淘汰掉的不算)。verify 用它抓「截頭」——
    否則輪替後最舊段的首筆 prev 非 genesis,前面被刪幾筆完全看不出來。"""
    for i in range(int(keep), 0, -1):
        s = _first_seq_of(f"{base}.{i}")
        if s:
            return s
    return _first_seq_of(base) or 1


@dataclass(frozen=True)
class RecordResult:
    """一次 record() 的結果。

    entry_written    JSONL 那一行有沒有寫進去(＝這筆稽核有沒有落地)
    anchor_written   anchor 側檔有沒有跟著更新(＝這筆之後能不能證明沒被截尾)
    fully_verifiable 兩者皆成立

    `__bool__` 刻意等同 entry_written:既有呼叫端的 `if not record(...)` 問的是
    「這筆有沒有寫進去」,不可因為 anchor 失敗就把它算成遺失。
    """
    entry_written: bool
    anchor_written: bool

    @property
    def fully_verifiable(self) -> bool:
        return self.entry_written and self.anchor_written

    def __bool__(self) -> bool:
        return self.entry_written


class ActionLedger:
    """append-only、hash-chained、會輪替的動作帳本。所有方法都不拋例外。

    注意:record() 是同步阻塞的(鎖 + 檔案 IO)。熱鍵/UI 緒請勿直接呼叫。"""

    def __init__(self, path, max_bytes: int = DEFAULT_MAX_BYTES,
                 keep: int = DEFAULT_KEEP, hard_max_bytes: int = 0):
        self.path = str(path)
        self.max_bytes = int(max_bytes)
        self.keep = int(keep)
        # [codex] 輪替失敗(檔案被鎖/權限)時的硬上限兜底,避免無限長大塞爆磁碟
        self.hard_max_bytes = int(hard_max_bytes) if hard_max_bytes else \
            max(int(max_bytes) * 2, 1)
        self._lock = threading.Lock()
        self._last_hash = None          # lazy
        self._last_seq = 0
        self._gap = None            # [R2-P1-01] 待記錄的 durability 缺口
        self._pending_torn: list = []   # 尚未結案的殘片側檔
        self._oldest_seq = 1

    def _load_last_state(self) -> None:
        """[codex] 決定續寫起點。base 不存在/空(例如輪替把 base 改名成 .1 之後、
        新 base 還沒寫就當機)時,要接上 .1 的末筆,否則會從 genesis 另起一條斷鏈。

        ★殘片的修復也在這裡★(外審 deep R2-2):它是 recovery 的一部分,必須在
        算出續寫起點【之前】做完 —— 否則「殘片對應的那次動作」不會被算進缺口,
        下一筆就直接重用它的 seq,健康檢查還回報「沒有遺失」。
        """
        self._repair_torn_tail()
        for cand in (self.path, f"{self.path}.1"):
            st = _last_state_of(cand)
            if st is not None:
                self._last_hash, self._last_seq = st
                break
        else:
            self._last_hash, self._last_seq = GENESIS, 0
        # ★[外審 R2-P1-01] anchor 比檔案還新 = 有一筆已經 durable 的動作不見了★
        #   (append 沒 fsync、anchor 有 → 斷電時 anchor 會領先)。原本續寫起點
        #   只看檔案末筆 → 下一筆★重用同一個 seq★,而且會把 anchor 一併覆寫成
        #   自己的 seq/hash —— 「曾經有一筆對應到真實 HIS 寫入」的最後證據就此
        #   消失,之後 verify 看到的是一條完整正常的鏈(偵測窗口只有「當機後、
        #   下一筆寫入前」)。改成:seq ★絕不重用★(跳過遺失的號碼),並記下
        #   缺口,由下一筆 `durability_gap` 紀錄把它寫成帳本裡永久可查的事實。
        self._gap = None
        try:
            a_seq = int(read_anchor(self.path).get("last_seq") or 0)
        except (TypeError, ValueError):
            a_seq = 0
        tail = int(self._last_seq)
        # ★殘片也是一筆「已宣稱、卻沒有完整紀錄」的動作★(外審 deep R2-2):
        #   它在 anchor 更新【之前】就被打斷,所以 anchor 完全看不到它 ——
        #   只看 anchor 的話這一筆會被靜靜重用 seq、health 還回報「沒有遺失」,
        #   而對應的 HIS 動作可能真的發生過。這裡替它保留一個 seq。
        #   ★來源是【側檔】不是這次呼叫的旗標★(外審 deep R3):側檔在截斷之前
        #   就 durable,所以「截斷後、記缺口前」斷電也還在;已經記過的(表頭
        #   tail_seq 對得上帳本裡的 torn 缺口)不重複記。
        self._pending_torn = [s for s in self._torn_pending()
                              if not self._torn_already_recorded(s)]
        for s in self._torn_pending():
            if s not in self._pending_torn:
                self._resolve_torn([s])     # 記過了 → 補上結案標記
        claimed = max(a_seq, tail + len(self._pending_torn))
        if claimed > tail:
            self._gap = {"anchor_last_seq": claimed, "tail_seq": tail}
            if self._pending_torn:
                self._gap["torn"] = 1
            logging.error(
                "[ledger] ★durability gap★:已宣稱到 seq=%d(anchor=%d,殘片=%d 個),"
                "檔案只到 seq=%d —— 有 %d 筆已發生的動作沒有完整紀錄(斷電?)。"
                "seq 不重用,下一筆會把這個缺口記進帳本",
                claimed, a_seq, len(self._pending_torn), tail, claimed - tail)
            self._last_seq = claimed
        # 還留著的最舊 seq:優先沿用 anchor,否則由現有各代推算
        anchor = read_anchor(self.path)
        try:
            self._oldest_seq = int(anchor.get("oldest_seq") or 0) or \
                _compute_oldest_seq(self.path, self.keep)
        except (TypeError, ValueError):
            self._oldest_seq = _compute_oldest_seq(self.path, self.keep)

    def _rotate_if_needed(self) -> None:
        """超過上限就 base→.1、.1→.2 …;最舊的丟掉。失敗只記 debug(由硬上限兜底)。

        [codex P2] 保留代數已滿時,要在【真的刪掉最舊一代之前】就把新的保留邊界寫進 anchor。
        順序很重要:若先刪再寫 anchor,中途當機會留下「檔案裡最舊 seq > anchor.oldest_seq」
        → 被永久誤判成截頭。先寫 anchor 再刪,則中途當機只會是「留存比 anchor 宣稱的還多」,
        那是良性的(verify 只在【少於】宣稱時才判截頭)。"""
        try:
            if self.max_bytes <= 0 or not os.path.exists(self.path):
                return
            if os.path.getsize(self.path) < self.max_bytes:
                return
            oldest = f"{self.path}.{self.keep}"
            if os.path.exists(oldest):
                # 保留代數已滿 → 最舊一代即將被淘汰。刪掉是不可逆的,所以【新邊界算不出來
                # 或寫不進 anchor,一律放棄本次輪替】,絕不先刪再說 —— 否則檔案已少一代、
                # anchor 卻還是舊邊界 → 之後永遠被誤判成截頭,且救不回來。
                src = f"{self.path}.{self.keep - 1}" if self.keep > 1 else self.path
                new_oldest = _first_seq_of(src)
                if not new_oldest:
                    # [codex P2] 下一代讀不到/毀損 → 算不出替代邊界 → 不可動最舊一代
                    logging.warning(
                        "[ledger] 算不出輪替後的新保留邊界(下一代讀不到或毀損)→ 放棄本次"
                        "輪替(不刪最舊一代);檔案大小改由硬上限兜底")
                    return
                prev_oldest = self._oldest_seq
                self._oldest_seq = new_oldest
                if not self._write_anchor(self._last_seq,
                                          self._last_hash or GENESIS):
                    self._oldest_seq = prev_oldest
                    logging.warning(
                        "[ledger] 新保留邊界寫不進 anchor → 放棄本次輪替(不刪最舊一代),"
                        "避免留下永久誤判截頭的狀態;檔案大小改由硬上限兜底")
                    return
                os.remove(oldest)
            for i in range(self.keep - 1, 0, -1):
                src2, dst = f"{self.path}.{i}", f"{self.path}.{i + 1}"
                if os.path.exists(src2):
                    os.replace(src2, dst)
            os.replace(self.path, f"{self.path}.1")
            self._oldest_seq = _compute_oldest_seq(self.path, self.keep)
        except Exception:
            logging.debug("[ledger] 輪替失敗(由硬上限兜底)", exc_info=True)

    def _over_hard_cap(self) -> bool:
        """[codex] 輪替失敗後仍超過硬上限 → 停止續寫(寧可少記,不可塞爆磁碟)。"""
        try:
            return (os.path.exists(self.path)
                    and os.path.getsize(self.path) >= self.hard_max_bytes)
        except Exception:
            return False

    def health_check(self, *, dropped: int = 0, write_failures: int = 0) -> dict:
        """[codex P2] 【持寫入鎖】做健康快照。健康檢查與寫入/輪替並行時,模組級
        health_snapshot 會讀到「新紀錄+舊 anchor」或輪替中途的暫態 → 誤報竄改、寄假警報。
        持同一把鎖讓驗證看到穩定狀態(驗證數 MB 檔很快、一天只跑兩次,writer 最多被擋
        數十 ms)。活體檢查一律走這裡;模組級 health_snapshot 留給離線/測試。不拋。"""
        try:
            with self._lock:
                return health_snapshot(self.path, self.keep, dropped=dropped,
                                       write_failures=write_failures)
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "level": "error", "verified": 0,
                    "summary": f"健康檢查本身失敗:{e}"}

    def record(self, surface: str, action: str, ts: str = "",
               **fields) -> "RecordResult":
        """記一筆外部動作。回 RecordResult(呼叫端【不應】依此改變臨床行為)。

        ★[2026-08-02 第二輪外審 P1-04] entry 與 anchor 必須分開回報★
        原本無條件 `return True`,把 `_write_anchor()` 的成敗丟掉。但 anchor 存在的
        理由正是「讓【截尾】變得可偵測」—— 雜湊鏈自己證不了後面還有沒有紀錄。
        anchor 沒寫成功 ＝ 這一筆【無法被證明完整】,而呼叫端(writer loop 只看布林)
        會以為一切正常:計數不增、關機 flush 視為全部落地,要等下一次 audit health
        check 才可能發現 anchor 對不上。
        `RecordResult.__bool__` 仍是 entry_written,既有的 `if not record(...)`
        語意不變(那個計數的意思是「這筆稽核沒寫進去」,不該被 anchor 失敗灌爆)。

        ts:動作【發生】的時間(ISO 字串)。非同步寫入時務必由呼叫端在動作當下帶入,
        否則會記成背景緒實際落檔的時間。省略則用現在。

        fields 可帶:target/value/his_version/canary/outcome/detail/correlation_id/
        app_version。★value/detail 必須是 `cmuh_common.audit_events` 的型別★
        (Code/Measure/Observed/Transition/Redacted/Reason);傳字串會被記成 violation
        且內容不落地 —— 這正是 P2-03 要的效果:誤傳的 HIS 欄位原文進不了帳本。"""
        try:
            with self._lock:
                if self._last_hash is None:
                    self._load_last_state()
                self._rotate_if_needed()
                if self._over_hard_cap():
                    logging.warning("[ledger] 檔案超過硬上限且輪替失敗 → 丟棄本筆稽核紀錄")
                    return RecordResult(False, False)
                if not self._emit_gap_if_any():
                    # ★缺口沒 durable 就不准寫原動作★(外審 deep R1-1):
                    #   否則這一筆會落在保留後的 seq 上,形成沒有紀錄解釋的跳號。
                    logging.warning(
                        "[ledger] 缺口紀錄尚未落地 → 本筆稽核暫不寫入(下次重試)")
                    return RecordResult(False, False)
                seq = int(self._last_seq) + 1
                payload = {
                    "schema_version": SCHEMA_VERSION,
                    "seq": seq,
                    "ts": str(ts) or datetime.now().isoformat(timespec="seconds"),
                    "surface": str(surface),
                    "action": str(action),
                    "machine": _machine(),
                    "user": _user(),
                    "prev": self._last_hash,
                }
                for k in _FIELDS:
                    # [P2-03] value/detail 只接受 audit_events 的型別,落成結構化
                    # payload;不是型別的東西記 violation 且【內容不落地】。
                    # 其餘欄位是受控值(常數、視窗類名、版本字串),原樣。
                    raw = fields.get(k, "") or ""
                    payload[k] = (to_field_payload(k, raw)
                                  if k in ("value", "detail") else str(raw))
                if not payload["outcome"]:
                    # [GPT-5.6 第三輪] 預設 unknown 而非 ok:呼叫端忘了傳 outcome 不可
                    # 自動變成「成功」紀錄(不安全預設會讓帳本失真)。
                    payload["outcome"] = OUTCOME_UNKNOWN
                rec = dict(payload)
                rec["hash"] = chain_hash(self._last_hash, payload)
                if not self._append_durable(_canonical(rec) + "\n"):
                    return RecordResult(False, False)
                self._last_hash = rec["hash"]
                self._last_seq = seq
                anchored = self._write_anchor(seq, rec["hash"])
                if not anchored:
                    logging.warning(
                        "[ledger] seq=%d 已寫入但 anchor 更新失敗 → 這一筆無法證明"
                        "完整(截尾將偵測不到)", seq)
                return RecordResult(True, bool(anchored))
        except Exception:
            logging.debug("[ledger] 記錄失敗(不影響操作)", exc_info=True)
            return RecordResult(False, False)

    # ── anchor 側檔:讓「截尾」變得可偵測(鏈自己證不了後面還有沒有)────────────
    def _emit_gap_if_any(self) -> bool:
        """★把 durability 缺口寫成帳本裡的一筆事實★(呼叫端須持有 self._lock)。

        `_load_last_state()` 發現 anchor 領先檔案末筆時只設了 `self._gap`;真正
        的紀錄要在★下一筆動作之前★寫下去,理由有二:
        (1) 缺口要能被【資料本身】證明,不能只留在 log(log 會輪替、也不進帳本);
        (2) 它同時解釋了接下來那個 seq 跳號 —— 否則 verify 會永遠判「疑遭刪除」,
            那就是一道★沒有出口★的閘門(帳本從此永久紅、無法自證清白)。
        寫失敗就把 `_gap` 留著,下一次再試(絕不因為記不下來就假裝沒發生)。

        ★回傳「現在可以寫原動作了嗎」★(外審 deep R1-1):我第一版沒有回傳值,
        於是缺口寫失敗時 `record()` 照樣把原動作寫在★保留後的 seq★上 ——
        那是一個沒有 gap 紀錄解釋的跳號,而且 anchor 若成功還會回報成功。
        下一次補寫 gap 也解釋不了前面那個跳號(先重啟則 `_gap` 直接消失)。
        所以:缺口沒有 durable 之前,★這一筆原動作不准寫★。
        """
        gap, self._gap = self._gap, None
        if not gap:
            return True
        try:
            seq = int(self._last_seq) + 1
            payload = {
                "schema_version": SCHEMA_VERSION, "seq": seq,
                "ts": datetime.now().isoformat(timespec="seconds"),
                "surface": SURFACE_LEDGER, "action": ACTION_DURABILITY_GAP,
                "machine": _machine(), "user": _user(), "prev": self._last_hash,
            }
            for k in _FIELDS:
                payload[k] = ""
            payload["outcome"] = OUTCOME_UNKNOWN
            _nums = {
                "anchor_last_seq": int(gap["anchor_last_seq"]),
                "tail_seq": int(gap["tail_seq"]),
                "missing": int(gap["anchor_last_seq"]) - int(gap["tail_seq"]),
            }
            if gap.get("torn"):
                # 缺口的來源要說得出來:殘片(稽核沒寫完)vs anchor 領先(沒落盤)。
                _nums["torn"] = 1
            payload["detail"] = to_field_payload(
                "detail", Reason("durability_gap", **_nums))
            rec = dict(payload)
            rec["hash"] = chain_hash(str(self._last_hash or GENESIS), payload)
            if not self._append_durable(_canonical(rec) + "\n"):
                self._gap = gap             # 記不下來 → 留著下次再記
                return False
            self._last_hash, self._last_seq = rec["hash"], seq
            self._write_anchor(seq, rec["hash"])
            # ★缺口與 anchor 都 durable 之後★才把側檔標成已結案 ——
            #   反過來的話「標記完成、缺口還沒寫」中間斷電就永遠記不到了。
            self._resolve_torn(self._pending_torn)
            self._pending_torn = []
            return True
        except Exception:
            self._gap = gap
            logging.debug("[ledger] 缺口紀錄寫入失敗(下次再試)", exc_info=True)
            return False

    def _torn_pending(self) -> list:
        """★還沒結案的殘片側檔★(見 `_torn_pending_files`)。

        ★遺失的事實要 durable,不能只活在記憶體★(外審 deep R3):
        「寫側檔 → 截斷主檔 → 記缺口」中間斷電的話,重開後主檔已經沒有殘片、
        anchor 也與末筆相符 → 舊版的 `torn_lost` 是這一次呼叫算出來的旗標,
        於是缺口就此消失、下一筆靜靜重用那個 seq、health 回報全綠。
        """
        return _torn_pending_files(self.path)

    def _torn_already_recorded(self, side: str) -> bool:
        """這個側檔的損失是不是★已經★有一筆缺口紀錄涵蓋了(避免重複記)。

        「記缺口 → 標記結案」之間也可能斷電;沒有這一步的話重開會再記一次,
        把一次損失說成兩次(過度回報同樣是失真)。判準用側檔表頭裡的
        `tail_seq` 對上帳本裡 torn 缺口紀錄的 `tail_seq`。
        """
        return _torn_is_recorded(self.path, self.keep, side)

    def _resolve_torn(self, sides: list) -> None:
        """缺口紀錄與 anchor 都 durable 之後,才把側檔標記為已結案。"""
        for s in sides:
            try:
                os.replace(s, s + ".resolved")
            except Exception:
                logging.debug("[ledger] 殘片側檔結案標記失敗:%s", s, exc_info=True)

    def _repair_torn_tail(self) -> bool:
        """修復沒有換行結尾的尾端。回「這一筆稽核是不是因此遺失了」。

        ★兩種形狀要分開處理★(外審 deep R2-1):「最後一個位元組不是換行」只是
        ★便利的判斷式★,不等於「那一筆沒寫完」—— 截斷點正好落在換行前面時,
        那是一筆★完整而且可能已被 anchor 指認★的紀錄。一律隔離會把它刪掉,
        而記憶體/anchor 還指著它 → 下一筆接不上,反而製造出斷鏈。
        所以:先解析殘片,
          * 解析得動且結構完整 → 只補一個 durable 的換行,★保留紀錄★;
          * 真的殘缺 → 移到 `.torn-N` 側檔(留檔備查)再截斷主檔,並回報
            「遺失一筆」,由呼叫端保留 seq + 記缺口(見 `_load_last_state`)。
        殘片依定義是沒寫完的紀錄(內容與換行在同一次 fsync 之前寫入),
        不曾 durable、也沒有 anchor 指向它,移走不會刪掉任何被證明過的東西。
        """
        try:
            if not _tail_is_torn(self.path):
                return False
            with open(self.path, "rb") as f:
                data = f.read()
            cut = data.rfind(b"\n") + 1     # 0 = 整個檔案都是殘片
            frag = data[cut:]
            try:
                rec = json.loads(frag.decode("utf-8"))
                complete = isinstance(rec, dict) and bool(rec.get("hash"))
            except Exception:               # noqa: BLE001 - 殘缺就是預期情況
                complete = False
            if complete:
                # ★只少了換行的完整紀錄★:補上去就好,絕不可以刪掉它。
                with open(self.path, "a", encoding="utf-8", newline="") as f:
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                logging.warning(
                    "[ledger] 尾端少了換行但紀錄本身完整 → 已補上換行(未動內容)")
                return False
            frag_text = frag.decode("utf-8", "replace")
            # ★截斷當下的末筆 seq★ —— 它是這次損失的身分,所以要在比對之前算。
            _cut_tail = 0
            for ln in reversed(data[:cut].decode("utf-8", "replace").splitlines()):
                try:
                    _cut_tail = int(json.loads(ln).get("seq") or 0)
                    break
                except Exception:            # noqa: BLE001 - 往前找到一筆算數的
                    continue
            # ★同一個殘片只能有一個標記★(外審 deep R4-2):「側檔已 durable、
            #   主檔還沒截斷」中間斷電的話,重開時殘片還在 —— 若無條件再開一個
            #   `.torn-2`,同一次損失會被算成兩筆、白白跳掉兩個 seq。
            #   ★但判準不可以只看殘片內容★(外審 deep R5-1):稽核紀錄彼此有很長
            #   的相同前綴,斷在同一個位元組位置就會產生【一模一樣的殘片】——
            #   舊標記因為結案改名失敗而還在時,新的損失會被折進它,而它的缺口
            #   早就記過了 → 新損失連標記帶缺口一起消失、seq 被重用、health 全綠。
            #   身分要用 (tail_seq, fragment) 兩者:不同 tail_seq 是不同的損失。
            _dup = next((s for s in _torn_pending_files(self.path)
                         if _torn_meta_of(s).get("fragment") == frag_text
                         and _torn_meta_of(s).get("tail_seq") == _cut_tail), None)
            if _dup is not None:
                logging.warning(
                    "[ledger] 這個殘片已有未結案的標記 %s → 沿用(不重複計損失)",
                    os.path.basename(_dup))
                side = _dup
            else:
                for i in range(1, 1000):
                    side = f"{self.path}.torn-{i}"
                    if not (os.path.exists(side)
                            or os.path.exists(side + ".resolved")):
                        break
                else:
                    logging.error("[ledger] 殘片側檔太多,放棄隔離")
                    raise OSError("too many torn side files")
            # ★側檔要在截斷【之前】就 durable★:它同時是證據與「尚未記入帳本」
            #   的標記(見 `_torn_pending`)。
            # ★標記本身要原子★(外審 deep R4-2):寫到一半斷電會留下解析不出來的
            #   標記,而「解析不出來」既不能當成沒事(漏報)、也不該當成一筆損失
            #   (誤報)。用 atomic_write_json(temp + fsync + replace)之後,
            #   標記只有「完整存在」與「不存在」兩種狀態。
            if _dup is None:
                atomic_write_json(side, {"tail_seq": _cut_tail,
                                         "fragment": frag_text})
            with open(self.path, "r+b") as f:
                f.truncate(cut)
                f.flush()
                os.fsync(f.fileno())
            logging.error(
                "[ledger] ★偵測到未寫完的殘片★(%d bytes)→ 已移到 %s 並截斷帳本。"
                "那一次稽核沒有寫完(斷電?)—— 對應的外部動作可能已經發生,"
                "會保留一個 seq 並記下缺口", len(frag), os.path.basename(side))
            return True
        except Exception:
            logging.error("[ledger] 殘片處理失敗", exc_info=True)
            raise

    def _append_durable(self, line: str) -> bool:
        """append 一行並★真的落盤★(flush + fsync)。回是否成功。

        ★[外審 R2-P1-01] 順序反了就是那個洞★:原本 append 只到 OS page cache,
        而其後的 anchor 走 `atomic_write_json`(內部 fsync 之後才 replace)——
        斷電時因此可能「anchor 已 durable、它指的那一筆卻不見了」。稽核帳本是
        金絲雀採 notify-only 時的補償控制,「回報成功」必須等於「已落盤」
        (這個 repo 在 updater / multiwrite / delivery ledger 都是同一個約定)。

        ★torn tail 要先補換行★:斷電留下的半行沒有換行結尾,直接 append 會把
        新紀錄接在它後面黏成一行 —— 連新的那一筆也一起讀不出來。
        """
        if _tail_is_torn(self.path):
            # ★殘片一律回到 recovery 路徑處理★:在這裡順手截掉會繞過缺口計算
            #   (那正是外審 deep R2-2 抓到的洞)。作廢記憶體狀態 → 下一次
            #   `record()` 會重跑 `_load_last_state()`,由它修復並保留 seq。
            logging.error("[ledger] 寫入前發現殘片 → 交回 recovery 處理,本筆不寫")
            self._last_hash = None
            return False
        started = False
        try:
            with open(self.path, "a", encoding="utf-8", newline="") as f:
                f.write(line)
                started = True              # ★從這裡開始,內容可能已經看得到★
                f.flush()
                os.fsync(f.fileno())
            return True
        except Exception:
            logging.debug("[ledger] 落盤失敗", exc_info=True)
            if started:
                # ★「不確定」不等於「沒寫成功」★(外審 deep R1-2):write+flush 都
                #   過了而 fsync 拋錯時,那一行很可能已經在檔案裡(甚至會被之後的
                #   fsync 一併落盤)。若沿用記憶體狀態繼續寫,下一筆會★重用同一個
                #   seq 與 prev★ —— 兩行都在、第二行卻接不上第一行,帳本從此永久
                #   驗證失敗。所以把記憶體狀態作廢:下一筆寫入前重新從實體檔案
                #   (與 anchor)推導續寫起點。
                logging.error(
                    "[ledger] 落盤結果不確定(內容可能已寫入)→ 作廢記憶體狀態,"
                    "下一筆重新由檔案推導續寫起點")
                self._last_hash = None
            return False

    @property
    def anchor_path(self) -> str:
        return self.path + ANCHOR_SUFFIX

    def _write_anchor(self, seq: int, last_hash: str) -> bool:
        """原子更新 anchor(記末筆 seq/hash 與還留著的最舊 seq)。回是否成功。
        [codex P2] 必須回報成敗:輪替要靠它決定「邊界沒寫成功就不准刪最舊一代」。
        一般記錄路徑失敗只記 debug —— anchor 壞掉不可影響記錄本身(但 verify 會因此
        判定無法證明完整,那是正確的)。"""
        try:
            atomic_write_json(self.anchor_path,
                              {"schema_version": SCHEMA_VERSION,
                               "oldest_seq": int(self._oldest_seq),
                               "last_seq": int(seq), "last_hash": str(last_hash)})
            return True
        except Exception:
            logging.debug("[ledger] anchor 更新失敗", exc_info=True)
            return False


def read_anchor(path) -> dict:
    """讀 anchor 側檔;無/壞回 {}。不拋。"""
    try:
        p = str(path) + ANCHOR_SUFFIX
        if not os.path.exists(p):
            return {}
        with open(p, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        logging.debug("[ledger] anchor 讀取失敗", exc_info=True)
        return {}


def read_records(path) -> list:
    """讀出所有可解析的紀錄(壞行跳過)。查閱/顯示用;要驗證完整性請用 verify_chain。"""
    out = []
    try:
        if not os.path.exists(str(path)):
            return out
        with open(str(path), "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        logging.debug("[ledger] 讀取失敗", exc_info=True)
    return out


def _parse_strict(path: str):
    """[codex] 驗證專用的【嚴格】解析:壞行不跳過而是判定失敗(壞行本身就是竄改跡象);
    檔案不存在也是失敗(不能把「整個被刪掉」當成「本來就沒紀錄」)。
    回 (recs, err) —— err 為 None 代表全部解析成功。"""
    if not os.path.exists(path):
        return ([], "帳本檔不存在(無法證明紀錄沒有被整個刪除)")
    recs = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    return ([], f"第 {i + 1} 行不是合法 JSON(疑遭竄改或截斷)")
                if not isinstance(rec, dict) or "hash" not in rec:
                    return ([], f"第 {i + 1} 行缺 hash 欄位(疑遭竄改)")
                recs.append(rec)
    except Exception as e:  # noqa: BLE001
        return ([], f"讀取失敗:{e}")
    return (recs, None)


def _gap_explains(rec: dict, prev_seq: int, seq: int) -> bool:
    """這一筆是不是「解釋了 prev_seq→seq 這次跳號」的 durability_gap 紀錄。

    ★判準要用它【自己宣告的數字】★:光看 action 名稱等於任何一筆 gap 紀錄
    都能赦免任意跳號 —— 那就把偵測性控制送掉了。要求 anchor_last_seq 正好是
    被跳過的最後一號、tail_seq 正好是前一筆,缺口大小也要對得起來。
    """
    if (str(rec.get("surface")) != SURFACE_LEDGER
            or str(rec.get("action")) != ACTION_DURABILITY_GAP):
        return False
    d = rec.get("detail")
    if not isinstance(d, dict) or d.get("code") != "durability_gap":
        return False
    try:
        tail = int(d.get("tail_seq") or 0)
        a_last = int(d.get("anchor_last_seq") or 0)
        missing = int(d.get("missing") or 0)
    except (TypeError, ValueError):
        return False
    return (tail == int(prev_seq) and a_last == int(seq) - 1
            and missing == int(seq) - 1 - int(prev_seq))


def _verify_sequence(recs: list, start_index: int = 0) -> tuple:
    """驗證一串紀錄的 hash 與 prev/seq 連續性。回 (ok, 檢查筆數, 說明)。"""
    prev_hash = None
    prev_seq = None
    for i, rec in enumerate(recs):
        payload = {k: v for k, v in rec.items() if k != "hash"}
        if chain_hash(payload.get("prev", GENESIS), payload) != rec["hash"]:
            return (False, i, f"第 {start_index + i + 1} 筆內容與 hash 不符(疑遭竄改)")
        if prev_hash is not None and rec.get("prev") != prev_hash:
            return (False, i, f"第 {start_index + i + 1} 筆 prev 未接上前一筆(疑遭刪除/插入)")
        try:
            seq = int(rec.get("seq") or 0)
        except (TypeError, ValueError):
            return (False, i, f"第 {start_index + i + 1} 筆 seq 非數字")
        if prev_seq is not None and seq != prev_seq + 1:
            # ★[外審 R2-P1-01] 跳號只有一種合法解釋★:這一筆就是那個
            #   `durability_gap` 事件,而且它自己宣告的缺口【剛好等於】這次跳號。
            #   斷電遺失的 seq 絕不重用,所以跳號一定存在;若沒有這個出口,
            #   帳本會從此永久判「疑遭刪除」—— 一道沒有出口的閘門。
            #   ★宣告的數字要對得起來★:隨便一筆 gap 紀錄不能赦免任意跳號。
            if not _gap_explains(rec, prev_seq, seq):
                return (False, i,
                        f"第 {start_index + i + 1} 筆 seq 跳號({prev_seq}→{seq},疑遭刪除)")
        prev_hash, prev_seq = rec["hash"], seq
    return (True, len(recs), "chain 完整")


def _anchor_is_valid(anchor) -> bool:
    """[codex P1] anchor 必須【結構有效】才算數(截尾檢查用:last_seq + last_hash)。
    光是「檔案存在/dict 非空」不夠 —— 否則把 anchor 換成 {} 或殘缺內容就能繞過檢查。"""
    if not isinstance(anchor, dict):
        return False
    try:
        if int(anchor.get("last_seq") or 0) <= 0:
            return False
    except (TypeError, ValueError):
        return False
    return bool(str(anchor.get("last_hash") or ""))


def _anchor_has_boundary(anchor) -> bool:
    """[codex P2] 截頭檢查另外需要 oldest_seq。只留 last_* 而把 oldest_seq 拿掉的 anchor
    不得矇混過關(否則輪替後的前段刪除就驗不出來)。"""
    if not isinstance(anchor, dict):
        return False
    try:
        return int(anchor.get("oldest_seq") or 0) > 0
    except (TypeError, ValueError):
        return False


def _check_empty_against_anchor(base: str) -> tuple:
    """[codex P1] 空帳本不可無條件放行:anchor 說曾經有紀錄,現在卻一筆都不剩
    → 整本被清空/截斷。回 (ok, 說明)。"""
    anchor = read_anchor(base)
    if _anchor_is_valid(anchor):
        return (False, f"帳本無任何紀錄,但 anchor 記錄末筆 seq={anchor.get('last_seq')}"
                       f"(疑遭整本清空/截斷)")
    return (True, "")


def _check_anchor_tail(base: str, recs: list) -> tuple:
    """[codex P1] 用 anchor 比對末筆,抓截尾。非空帳本【必須】有結構有效的 anchor ——
    否則(含把 anchor 一起刪掉/清空來掩飾截尾的情況)一律判定無法證明完整。回 (ok, 說明)。"""
    anchor = read_anchor(base)
    if not _anchor_is_valid(anchor):
        return (False, "缺少或毀損 anchor 側檔,無法證明未被截尾(anchor 遺失本身即為異常)")
    last = recs[-1]
    try:
        a_seq = int(anchor.get("last_seq") or 0)
    except (TypeError, ValueError):
        a_seq = 0
    try:
        last_seq = int(last.get("seq") or 0)
    except (TypeError, ValueError):
        last_seq = 0
    if a_seq and last_seq != a_seq:
        return (False, f"末筆 seq={last_seq} 與 anchor 記錄的 {a_seq} 不符"
                       f"(疑遭截尾,少了 {a_seq - last_seq} 筆)")
    if anchor.get("last_hash") and last.get("hash") != anchor.get("last_hash"):
        return (False, "末筆 hash 與 anchor 不符(疑遭截尾/竄改)")
    return (True, "")


def _verify_segment(path) -> tuple:
    """[codex P1] 內部用:只驗【單一段檔】自身的鏈與 seq 連續性,不碰 anchor。
    給 verify_generations 逐段使用(輪替出去的 .1/.2 本就沒有自己的 anchor)。"""
    recs, err = _parse_strict(str(path))
    if err:
        return (False, 0, err)
    if not recs:
        return (True, 0, "空帳本(檔案存在但無紀錄)")
    return _verify_sequence(recs)


def verify_chain(path) -> tuple:
    """公開 API:驗證 base 帳本的 hash chain、seq 連續性,並【一律】用 anchor 比對末筆
    抓截尾。回 (ok, 檢查筆數, 說明)。

    嚴格解析:檔案不存在、壞行、缺 hash 都判失敗。
    [codex P1] 非空帳本【必須】有結構有效的 anchor —— 否則「把 anchor 一起刪掉再截尾」
    就能矇混過關。要驗跨代與截頭請用 verify_generations;要只驗某一段(不含 anchor)
    請用內部的 _verify_segment。"""
    p = str(path)
    ok, n, msg = _verify_segment(p)
    if not ok:
        return (ok, n, msg)
    if n == 0:
        # [codex P1] 空的也要對照 anchor —— 把有紀錄的帳本清成 0 bytes 不是「空帳本」
        e_ok, e_msg = _check_empty_against_anchor(p)
        return (True, 0, msg) if e_ok else (False, 0, e_msg)
    recs, _ = _parse_strict(p)
    a_ok, a_msg = _check_anchor_tail(p, recs)
    if not a_ok:
        return (False, len(recs), a_msg)
    return (True, len(recs), "chain 完整(含 anchor 末筆比對)")


def verify_generations(path, keep: int = DEFAULT_KEEP) -> tuple:
    """把「還留著的各代」(最舊的 .keep → .1 → base)串成一條鏈驗證,並比對 anchor 抓截尾。
    回 (ok, 檢查筆數, 說明)。跳過不存在的代(已被輪替淘汰是正常的)。

    能抓到:改內容、中間刪/插、行毀損、seq 跳號、輪替交界斷鏈、【截頭】(最舊留存段首筆
    prev=genesis 卻 seq!=1)、【截尾】(末筆對不上 anchor)。"""
    base = str(path)
    segments = [f"{base}.{i}" for i in range(int(keep), 0, -1)] + [base]
    existing = [s for s in segments if os.path.exists(s)]
    if not existing:
        return (False, 0, "所有帳本檔都不存在(無法證明紀錄沒有被整個刪除)")
    all_recs = []
    for seg in existing:
        recs, err = _parse_strict(seg)
        if err:
            return (False, len(all_recs), f"{os.path.basename(seg)}:{err}")
        all_recs.extend(recs)
    if not all_recs:
        # [codex P1] 各代都存在卻一筆紀錄都沒有 → 對照 anchor 判斷是否被整本清空
        e_ok, e_msg = _check_empty_against_anchor(base)
        return (True, 0, "空帳本") if e_ok else (False, 0, e_msg)
    ok, n, msg = _verify_sequence(all_recs)
    if not ok:
        return (ok, n, msg)
    # [codex P1] 截頭:首筆若自稱是鏈的起點(prev=genesis),seq 必須是 1;輪替過的情況
    # 首筆 prev 非 genesis,則比對 anchor 記的「還留著的最舊 seq」(否則前面被刪看不出來)。
    first = all_recs[0]
    try:
        first_seq = int(first.get("seq") or 0)
    except (TypeError, ValueError):
        return (False, 0, "首筆 seq 非數字")
    if (first.get("prev") == GENESIS and first_seq != 1
            # ★第一筆就遺失也要有出口★(外審 deep R1-3):seq=1 沒落盤、anchor 卻
            #   已是 1 時,recovery 產生的首筆就是 prev=genesis 的 durability_gap
            #   (seq=2)。判成截頭的話健康狀態永久 error —— 又是一道沒有出口的閘門。
            #   ★仍要求數字完全吻合★:tail_seq=0(前面本來就沒有紀錄)、
            #   anchor_last_seq=seq-1,所以偽造的 gap 紀錄赦免不了真正的截頭。
            and not _gap_explains(first, 0, first_seq)):
        return (False, 0, f"首筆 prev=genesis 但 seq={first_seq}(疑遭截頭刪除)")
    # [codex P1] 截尾:鏈自己證不了「後面還有沒有」→ 非空帳本一律要求有效 anchor 佐證。
    a_ok, a_msg = _check_anchor_tail(base, all_recs)
    if not a_ok:
        return (False, len(all_recs), a_msg)
    # [codex P2] 截頭:輪替過的情況首筆 prev 非 genesis,只能靠 anchor 的保留邊界判斷。
    # 缺 oldest_seq 的 anchor 不得放行(否則把該欄拿掉就能跳過這個檢查)。
    anchor = read_anchor(base)
    if not _anchor_has_boundary(anchor):
        return (False, len(all_recs),
                "anchor 缺少 oldest_seq 保留邊界,無法驗證是否遭截頭")
    a_oldest = int(anchor.get("oldest_seq"))
    # 只有「留存的比 anchor 宣稱的【少】」才是截頭。反過來(留存的比宣稱的多)是良性的:
    # 輪替會先寫新邊界再刪最舊一代,中途當機就會停在這個狀態。
    # ★同一個出口在這裡也要成立★(外審 deep R1-3 的第二處):第一筆就沒落盤時,
    #   檔案的最舊留存筆會是 recovery 產生的 durability_gap,seq 必然大於 anchor
    #   當初記下的 oldest_seq。缺口紀錄自己的數字對得起來就不是截頭 ——
    #   否則健康狀態一樣永久 error(我第一版只修了 prev=genesis 那一處,
    #   測試才把這一處也翻出來)。
    if first_seq > a_oldest and not _gap_explains(all_recs[0], a_oldest - 1,
                                                  first_seq):
        return (False, 0,
                f"最舊留存筆 seq={first_seq} 大於 anchor 記錄的保留邊界 {a_oldest}"
                f"(疑遭截頭刪除)")
    return (True, len(all_recs), "chain 完整(含 anchor 截頭/截尾比對)")


def _count_gaps(base: str, keep: int) -> int:
    """各代裡「已記錄的 durability 缺口」筆數(健康狀態要說出來)。不拋。"""
    n = 0
    try:
        for p in [base] + [f"{base}.{i}" for i in range(1, int(keep) + 1)]:
            for rec in read_records(p):
                if (str(rec.get("surface")) == SURFACE_LEDGER
                        and str(rec.get("action")) == ACTION_DURABILITY_GAP):
                    n += 1
    except Exception:
        logging.debug("[ledger] 統計缺口失敗", exc_info=True)
    return n


def health_snapshot(path, keep: int = DEFAULT_KEEP, *, dropped: int = 0,
                    write_failures: int = 0, empty_is_ok: bool = True) -> dict:
    """[GPT-5.6 第三輪批次三] 稽核健康快照:把「帳本可信嗎」變成一個可判讀的結果,
    供啟動檢查/每日檢查/設定頁顯示共用。純讀取,不拋。

    回 {"ok": bool, "level": "ok|warn|error", "summary": str, "verified": int}。
    - verify_generations 失敗 → error(帳本無法證明完整 —— 偵測性控制已失效)。
    - dropped/write_failures > 0 → warn(有動作沒被記到;帳本本身仍完整)。
    - 尚無任何帳本檔且 empty_is_ok → ok(還沒發生過任何外部動作,是正常初始狀態)。
    """
    try:
        base = str(path)
        any_file = any(os.path.exists(p) for p in
                       [base] + [f"{base}.{i}" for i in range(1, int(keep) + 1)])
        # [codex P1] 「真正的初始狀態」= 帳本檔【和 anchor】都不存在。anchor 只在寫過
        # 紀錄後才會出現 —— anchor 還在、.jsonl 全被刪掉 ≠ 沒發生過動作,是整本被刪,
        # 必須 error;否則刪光帳本反而被回報健康。
        anchor_exists = os.path.exists(base + ANCHOR_SUFFIX)
        if not any_file and anchor_exists:
            v_ok, n, v_msg = False, 0, \
                "帳本檔全數不存在但 anchor 仍在(曾有紀錄,疑遭整本刪除)"
        elif not any_file and empty_is_ok:
            v_ok, n, v_msg = True, 0, "尚無稽核紀錄(尚未執行過外部動作)"
        else:
            v_ok, n, v_msg = verify_generations(base, keep=keep)
        if not v_ok:
            return {"ok": False, "level": "error", "verified": n,
                    "summary": f"帳本完整性驗證失敗:{v_msg}"}
        # ★[外審 R2-P1-01] 已記錄的斷電缺口要【看得見】★:verify 認得
        #   `durability_gap` 所以鏈是完整的(那是刻意留的出口),但「有動作
        #   真的發生過卻沒被記到」本身就是 warn —— 與下面的丟棄/落地失敗同級。
        #   若只讓 verify 放行而不在健康狀態說一聲,等於換一種方式把它藏起來。
        gaps = _count_gaps(base, keep)
        # ★未結案的殘片標記本身就是「有一筆稽核沒寫完」★(外審 deep R4-1):
        #   缺口紀錄要等到【下一次寫入】才會產生,而啟動時的健康檢查就在那之前 ——
        #   只數缺口紀錄的話,重開後到下一個動作之間,補償控制回報全綠。
        # ★同一件事不可以數兩次★(外審 deep R5-2):「缺口已 durable、標記還沒
        #   改名結案」那個窗口裡,缺口紀錄與待處理標記講的是同一次損失。
        pending = len([s for s in _torn_pending_files(base)
                       if not _torn_is_recorded(base, keep, s)])
        if gaps or pending:
            _parts = []
            if gaps:
                _parts.append(f"{gaps} 處已記錄的斷電缺口")
            if pending:
                _parts.append(f"{pending} 筆沒寫完的稽核(尚未記入帳本)")
            return {"ok": False, "level": "warn", "verified": n,
                    "summary": (f"帳本完整({n} 筆),但有 " + "、".join(_parts)
                                + " —— 那段期間發生過的動作沒有留下紀錄")}
        if dropped or write_failures:
            return {"ok": False, "level": "warn", "verified": n,
                    "summary": (f"帳本完整({n} 筆),但本次執行有紀錄遺失:"
                                f"佇列丟棄 {dropped} 筆、落地失敗 {write_failures} 筆")}
        return {"ok": True, "level": "ok", "verified": n,
                "summary": f"帳本完整({n} 筆),無遺失"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "level": "error", "verified": 0,
                "summary": f"健康檢查本身失敗:{e}"}
