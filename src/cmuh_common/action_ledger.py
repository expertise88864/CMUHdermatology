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
* 【不存病人明文識別】:只記非 PII 的動作與值。呼叫端【絕不可】把採樣到的 HIS 欄位原文
  (可能是誤抓到的姓名/病歷號/卡號)放進 value/detail —— 只放固定原因字串與長度等安全中繼資料。
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
import os
import re
import threading
from datetime import datetime

from cmuh_common.atomic_io import atomic_write_json

SCHEMA_VERSION = 1
LEDGER_FILENAME = "action_ledger.jsonl"
ANCHOR_SUFFIX = ".anchor.json"
DEFAULT_MAX_BYTES = 5 * 1024 * 1024      # 5MB 後輪替
DEFAULT_KEEP = 3                          # 保留 .1 .2 .3
GENESIS = "genesis"

# 面向(surface)
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
    """穩定序列化(排序鍵、無多餘空白)——hash chain 與寫檔共用,確保可重算。"""
    return json.dumps(d, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ── PII 縱深防禦(GPT-5.6 第三輪 P2-07)────────────────────────────────────────
# 文件說「呼叫端絕不可傳病人明文」,但只靠註解不是 enforcement:未來任何新呼叫點誤傳
# detail=採樣原文,病歷內容就永久進帳本。故落地前統一消毒。樣式刻意保守,只遮「幾乎
# 不可能是合法稽核值」的樣式 —— 醫令代碼最長 7 位數(1850159)、HIS 版本 1150713(.02)
# 都是 ≤7 位段,不受影響;病歷號(8 位數)、身分證(1 字母+9 數字)、手機(09 開頭 10 位)
# 會被遮。誤遮的代價只是稽核少一點細節,遠小於 PII 外洩。
_PII_PATTERNS = (
    re.compile(r"[A-Za-z][12]\d{8}"),      # 台灣身分證:1 字母 + 1/2 + 8 數字(先於長數字)
    re.compile(r"\d{8,}"),                 # 8 位以上連續數字:病歷號/卡號/電話
)


def sanitize_text(s) -> str:
    """把可能是病人識別資料的樣式換成 [REDACTED]。純函式;非字串先 str()。不拋。"""
    try:
        out = str(s or "")
        for pat in _PII_PATTERNS:
            out = pat.sub("[REDACTED]", out)
        return out
    except Exception:
        return "[REDACTED]"


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
    """回該檔最後一筆的 (hash, seq);檔不存在/無有效行回 None。不拋。"""
    try:
        if not os.path.exists(path):
            return None
        last = None
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    last = line
        if not last:
            return None
        rec = json.loads(last)
        return (str(rec.get("hash") or GENESIS), int(rec.get("seq") or 0))
    except Exception:
        logging.debug("[ledger] 讀取 %s 末筆失敗", path, exc_info=True)
        return None


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
        self._oldest_seq = 1

    def _load_last_state(self) -> None:
        """[codex] 決定續寫起點。base 不存在/空(例如輪替把 base 改名成 .1 之後、
        新 base 還沒寫就當機)時,要接上 .1 的末筆,否則會從 genesis 另起一條斷鏈。"""
        for cand in (self.path, f"{self.path}.1"):
            st = _last_state_of(cand)
            if st is not None:
                self._last_hash, self._last_seq = st
                break
        else:
            self._last_hash, self._last_seq = GENESIS, 0
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

    def record(self, surface: str, action: str, ts: str = "", **fields) -> bool:
        """記一筆外部動作。回是否寫成功(呼叫端【不應】依此改變臨床行為)。

        ts:動作【發生】的時間(ISO 字串)。非同步寫入時務必由呼叫端在動作當下帶入,
        否則會記成背景緒實際落檔的時間。省略則用現在。

        fields 可帶:target/value/his_version/canary/outcome/detail/correlation_id/
        app_version。切記 value/detail 不得放病人明文識別資料或採樣到的 HIS 欄位原文。"""
        try:
            with self._lock:
                if self._last_hash is None:
                    self._load_last_state()
                self._rotate_if_needed()
                if self._over_hard_cap():
                    logging.warning("[ledger] 檔案超過硬上限且輪替失敗 → 丟棄本筆稽核紀錄")
                    return False
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
                    # [P2-07] value/detail 是自由文字,落地前強制消毒(縱深防禦,
                    # 不只靠呼叫端自律);其餘欄位是受控值,原樣。
                    raw = fields.get(k, "") or ""
                    payload[k] = (sanitize_text(raw) if k in ("value", "detail")
                                  else str(raw))
                if not payload["outcome"]:
                    # [GPT-5.6 第三輪] 預設 unknown 而非 ok:呼叫端忘了傳 outcome 不可
                    # 自動變成「成功」紀錄(不安全預設會讓帳本失真)。
                    payload["outcome"] = OUTCOME_UNKNOWN
                rec = dict(payload)
                rec["hash"] = chain_hash(self._last_hash, payload)
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(_canonical(rec) + "\n")
                self._last_hash = rec["hash"]
                self._last_seq = seq
                self._write_anchor(seq, rec["hash"])
                return True
        except Exception:
            logging.debug("[ledger] 記錄失敗(不影響操作)", exc_info=True)
            return False

    # ── anchor 側檔:讓「截尾」變得可偵測(鏈自己證不了後面還有沒有)────────────
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
            return (False, i, f"第 {start_index + i + 1} 筆 seq 跳號({prev_seq}→{seq},疑遭刪除)")
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
    if first.get("prev") == GENESIS and first_seq != 1:
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
    if first_seq > a_oldest:
        return (False, 0,
                f"最舊留存筆 seq={first_seq} 大於 anchor 記錄的保留邊界 {a_oldest}"
                f"(疑遭截頭刪除)")
    return (True, len(all_recs), "chain 完整(含 anchor 截頭/截尾比對)")


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
        if dropped or write_failures:
            return {"ok": False, "level": "warn", "verified": n,
                    "summary": (f"帳本完整({n} 筆),但本次執行有紀錄遺失:"
                                f"佇列丟棄 {dropped} 筆、落地失敗 {write_failures} 筆")}
        return {"ok": True, "level": "ok", "verified": n,
                "summary": f"帳本完整({n} 筆),無遺失"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "level": "error", "verified": 0,
                "summary": f"健康檢查本身失敗:{e}"}
