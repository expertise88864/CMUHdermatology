# -*- coding: utf-8 -*-
"""稽核帳本的【型別化事件值】(2026-07-31 第二輪外審 P2-03)。

【被批評的舊做法】`action_ledger.sanitize_text` 用一組 denylist regex,在落地前
猜「哪一段像個資」:

    _PII_PATTERNS = (台灣身分證樣式, r"\\d{8,}")

三個問題:

 1. ★猜不準★ 中文姓名、地址、7 位病歷號、生日 `1990-01-01` 都不符樣式,照樣落地。
    而它宣稱防的正是「呼叫端誤把採樣到的 HIS 欄位原文塞進來」—— 那恰好是它最擋
    不住的東西(HIS 欄位裡可以是任何字)。**本次改版查到一條真的路徑**:F11 的
    `_f11_read_course_value()` 讀療程欄【完全沒有把關】(只 strip + 全形轉半形),
    值直接進 `value=f"療程={course_value}"`。定位漂到姓名欄時,姓名就進帳本,
    而那兩條 regex 一個字都攔不到。
 2. ★誤遮無聲★ 未來某個合法的 8 位數稽核值會被換成 `[REDACTED]`,而且沒有任何
    訊號。醫令代碼今天最長 7 位;院方哪天發 8 位的,整欄集體變 `[REDACTED]` ——
    偏偏那正是要查「改版把醫令寫錯」的時候,帳本在最需要證據的時刻失去證據。
 3. ★位置錯★ 消毒是在「已經拿到一段自由文字」之後才做。真正該問的是
    **為什麼帳本會拿到自由文字**。

【改法】呼叫端必須【宣告這個值是什麼】,而不是丟一段字串讓帳本猜。帳本只接受
本模組的型別;不是型別的東西記成 violation 且【內容不落地】。

★誠實邊界★(不要把這裡寫成比實際更強的保證)

 * 型別擋的是【意外】—— 順手把採樣到的欄位原文塞進 value。它擋不住【蓄意】:
   有人真要寫 `Code("chart", 病歷號)` 還是寫得進去。差別在於那是一行看得見的、
   要特別寫出來的程式碼,不是「忘了想」就會發生的事。
 * `Observed` 是唯一能描述「從 HIS 讀到的東西」的型別,而它【只存長度】。
   這是型別層面的「不記內容」,不靠註解提醒。
 * `Code` 的把關是【字元集 + 長度白名單】,不是樣式黑名單:通過的一定是短的
   代碼樣字串,中文姓名/長文字一律過不了。過不了時記 violation ——
   而那個 violation 本身就是有用的訊號(讀到的值不像代碼 ＝ 疑似定位漂移)。
 * ★建構子絕不拋例外★ 它們在臨床路徑上被求值(在 `_record_his_action` 的
   try 之外),拋出去會弄壞熱鍵流程。無效的值要到 `to_payload()` 才變成
   violation —— 那時已經在帳本自己的 try 裡面了。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

# ★[2026-08-01 外審 P1] 每一種 kind 都要宣告【自己的值域】★
#
# 原本只有一條共用的字元白名單(`[0-9A-Za-z._+,/-]{0,32}`)。那擋得住中文姓名，
# 但**擋不住病歷號與身分證** —— `12345678`、`A123456789` 全都是合法字元、長度也夠短。
# 而舊的 denylist regex（`\d{8,}`、身分證樣式）**擋得住那兩個**。也就是說在這條路徑上
# 型別化之後比原本更糟，而我在文件裡宣稱的是相反的事。
#
# 為什麼特別嚴重：`kind` 相同不代表來源可信。同一個 `Code("療程", …)`
#   * `_set_療程_only` 傳的是【我們要寫進去的值】(來自設定) → 可信；
#   * F11 的 `_f11_read_course_value()` 傳的是【從 HIS 讀回來的值】，
#     而那支只做 strip + 全形轉半形，**完全沒有把關** —— 定位漂到病歷號欄位時，
#     病歷號就是這樣進帳本的。
#
# 所以值域要綁在 kind 上，而不是綁在「呼叫端可不可信」上：療程就是一位數，
# 讀到八位數不管來源是誰都是異常 → violation（而那個 violation 本身就是
# 「疑似定位漂移」的訊號，正是我們要的）。
#
# ★未宣告的 kind 一律 violation（fail-closed）★ —— 新增一種 kind 就必須順手宣告
# 它的值域，不能靠一條寬鬆的共用規則矇混過去。
_CODE_DOMAINS = {
    "療程": re.compile(r"\A\d?\Z"),              # 單一位數(F11 路由用 2/3)，或空白
    "身份": re.compile(r"\A\d{0,3}\Z"),          # 1-3 位數代碼(40/01/10…)，或空白
    "醫令代碼": re.compile(r"\A\d{0,8}\Z"),      # 純數字，目前最長 7 位(1850159)
    "同意書": re.compile(r"\A[A-Za-z0-9]{0,8}\Z"),   # MO04 / MU02
}
_KIND_RE = re.compile(r"\A[^\r\n]{1,24}\Z")     # 標籤是我們自己寫的字面量,只擋失控長度

# `detail` 的封閉理由集。★自由文字在這裡就被擋掉了★ ——
# 新增理由要改這個 dict(一個看得見的動作),不能在呼叫點隨手寫一句話。
REASONS = {
    "no_readback": "已送出,但沒有回讀路徑可確認 HIS 真的處理了",
    "send_failed": "送出本身失敗",
    "no_focus": "等不到可信的輸入焦點,未送出",
    "no_candidate_worked": "所有候選 id 都送出失敗,未完成",
    "control_not_found": "找不到目標控制項,未送出",
    "window_not_opened": "等不到目標視窗,動作未完成",
    "field_value_unexpected": "欄位原值不像這個欄位,疑似定位錯欄,未寫",
    "settext_failed": "WM_SETTEXT 寫入失敗",
    "readback_empty": "回讀到空字串,無法驗證寫回",
    "readback_mismatch": "回讀與預期不符",
    "write_exception": "寫入過程發生例外",
    # [2026-08-01 外部 review P1-03] F11 療程欄讀不到／讀到不像療程值的東西。
    # ★這是「疑似定位漂移」最早的訊號★ —— 臨床行為不變（照舊按全部完成），
    # 但要留下紀錄與通知。原值一律不記，只帶 length。
    "course_unreadable": "療程欄讀不到或內容不像療程值,疑似定位漂移",
    # [外審 R2-P1-01] 帳本自身的 durability 缺口:anchor 記到的 seq 比檔案末筆新
    # → 有已發生的動作沒有落盤(斷電)。numbers 帶 anchor_last_seq/tail_seq/missing。
    "durability_gap": "帳本有已 durable 的 anchor 指向沒落盤的紀錄(疑斷電遺失)",
}

_VIOLATION = "violation"


def _numbers_ok(d: dict) -> bool:
    """★[2026-08-01 外審 P2] NaN / Infinity 不算數字★

    `float("nan")` 是 `float` 的實例，所以原本的型別檢查放它過去。但
    `json.dumps` 預設 `allow_nan=True`，會吐出 `NaN` / `Infinity` —— **那不是合法
    JSON**。帳本是要給別的工具（verifier／SIEM／日後的分析腳本）讀的，
    寫進去等於那一行從此解析不了，而且是整條 hash chain 裡的一行。
    劑量回讀不到時本來就該用 `None`（見 `Measure` 的說明），不是 NaN。
    """
    import math

    for k, v in d.items():
        if not isinstance(k, str) or not k.isidentifier():
            return False
        if isinstance(v, bool) or v is None:
            continue
        if isinstance(v, int):
            continue
        if isinstance(v, float):
            if not math.isfinite(v):
                return False
            continue
        return False
    return True


def _violation(reason: str, **extra) -> dict:
    """★內容不落地★ —— 違規時只記「發生了什麼違規」,絕不把原值一起記進去
    (那等於把本模組要防的事情用另一個欄位做了一次)。"""
    out = {"t": _VIOLATION, "reason": str(reason)}
    out.update({k: str(v) for k, v in extra.items()})
    return out


@dataclass(frozen=True)
class AuditValue:
    """所有型別化稽核值的基底。子類必須實作 to_payload()。"""

    def to_payload(self) -> dict:            # pragma: no cover - 抽象
        raise NotImplementedError

    def __str__(self) -> str:
        """人看的字串(告警信、log)。帳本落地的是 to_payload()。"""
        return render(self.to_payload())


@dataclass(frozen=True)
class Code(AuditValue):
    """一個【程式自己選定或已把關過】的短代碼:醫令代碼、療程、身份別、同意書別。

    ★把關落在 kind 上,不是落在呼叫端★ 每一種 kind 在 `_CODE_DOMAINS` 宣告自己的
    值域(療程＝一位數、身份＝1-3 位、醫令代碼＝≤8 位數字、同意書＝≤8 位英數),
    未宣告的 kind 一律 violation(fail-closed)。空字串合法 —— 代表「該欄是空的」,
    那是有意義的稽核事實。不合格 → violation,內容不落地。

    ★為什麼不是一條共用的寬鬆白名單★ 療程值是【從 HIS 讀回來的】
    (`_f11_read_course_value()` 只做 strip + 全形轉半形,沒有把關)。一條「數字＋
    ASCII 字母、長度 ≤32」的規則會讓病歷號(8 位數)、身分證號原封不動寫進
    append-only 帳本 —— 那比它取代掉的 denylist regex 還糟。逐 kind 收窄之後,
    讀到不該出現的東西會變成 violation,而那個 violation 本身就是定位漂移的訊號。
    """
    kind: str
    code: str

    def to_payload(self) -> dict:
        kind = str(self.kind or "")
        if not _KIND_RE.match(kind):
            return _violation("bad_kind")
        pattern = _CODE_DOMAINS.get(kind)
        if pattern is None:
            # ★未宣告值域的 kind 一律拒絕（fail-closed）★
            #   新增 kind 時必須順手宣告它的值域 —— 否則一條寬鬆的共用規則會讓
            #   病歷號/身分證這種「合法字元、長度也短」的東西矇混過去（外審 P1）。
            logging.error(
                "[ledger] Code 的 kind=%r 沒有宣告值域 → 記為違規，內容不落地。"
                "請在 cmuh_common.audit_events._CODE_DOMAINS 補上它的樣式。", kind)
            return _violation("undeclared_kind", kind=kind)
        raw = "" if self.code is None else str(self.code)
        if not pattern.match(raw):
            # ★這個 violation 本身是訊號★ 讀到的值不符該欄位的值域 ＝ 疑似定位漂移
            #   （只記長度，不記內容 —— 那個內容正是可能的病歷號/姓名）
            return _violation("not_a_code", kind=kind, length=len(raw))
        return {"t": "code", "kind": kind, "v": raw}


@dataclass(frozen=True)
class Measure(AuditValue):
    """純數值:劑量、次數、長度。只收 int/float/bool/None。"""
    numbers: dict = field(default_factory=dict)

    def __init__(self, **numbers):
        object.__setattr__(self, "numbers", dict(numbers))

    def to_payload(self) -> dict:
        if not _numbers_ok(self.numbers):
            return _violation("not_numeric")
        out = {"t": "measure"}
        out.update(self.numbers)
        return out


@dataclass(frozen=True)
class Observed(AuditValue):
    """★從 HIS 讀到的東西★ —— 只記長度,【永遠不記內容】。

    這是型別層面的「不存病人明文」:想描述讀到什麼,唯一的表達方式就是它的長度。
    """
    length: int

    def to_payload(self) -> dict:
        try:
            n = int(self.length)
        except (TypeError, ValueError):
            return _violation("bad_length")
        if n < 0:
            return _violation("bad_length")
        return {"t": "observed", "len": n}


@dataclass(frozen=True)
class Transition(AuditValue):
    """before → after。兩邊都必須是本模組的型別。"""
    before: AuditValue
    after: AuditValue

    def to_payload(self) -> dict:
        if not isinstance(self.before, AuditValue) or \
           not isinstance(self.after, AuditValue):
            return _violation("untyped_transition")
        return {"t": "transition",
                "before": self.before.to_payload(),
                "after": self.after.to_payload()}


@dataclass(frozen=True)
class Redacted(AuditValue):
    """明說「這個值刻意不記」(卡號)。比記一句「(已遮罩)」自由文字精確。"""
    what: str

    def to_payload(self) -> dict:
        what = str(self.what or "")
        if not _KIND_RE.match(what):
            return _violation("bad_kind")
        return {"t": "redacted", "what": what}


@dataclass(frozen=True)
class Reason(AuditValue):
    """`detail` 專用:封閉集合的理由碼 + 選用的數值脈絡。

    ★自由文字在這裡被擋掉★ 舊版 detail 是 f-string,想寫什麼寫什麼。
    """
    code: str
    numbers: dict = field(default_factory=dict)

    def __init__(self, code: str, **numbers):
        object.__setattr__(self, "code", str(code))
        object.__setattr__(self, "numbers", dict(numbers))

    def to_payload(self) -> dict:
        if self.code not in REASONS:
            # ★[2026-08-01 外審 P2] 不可以把被拒絕的字串抄進 violation★
            #   原本寫 `_violation("unknown_reason", code=self.code)` —— 於是
            #   `Reason(採樣到的原文)` 會把那段原文完整留在帳本裡，正好違反本模組
            #   「violation 絕不攜帶原值」的契約（也就是用另一個欄位做了同一件壞事）。
            #   只留長度：足以判斷「有人傳了自由文字進來」，又不洩漏內容。
            logging.error("[ledger] Reason 收到未宣告的理由碼(長度=%d)→ 記為違規，"
                          "內容不落地。請在 REASONS 補上它。", len(str(self.code)))
            return _violation("unknown_reason", length=len(str(self.code)))
        if not _numbers_ok(self.numbers):
            return _violation("not_numeric", code=self.code)
        out = {"t": "reason", "code": self.code}
        out.update(self.numbers)
        return out


def to_field_payload(name: str, raw) -> dict:
    """帳本落地前的邊界:把 value/detail 轉成結構化 payload。

    ★不是本模組的型別就【不落地內容】★ 舊版的 sanitize_text 會「盡量消毒後照樣寫
    進去」,於是一個誤傳的 HIS 欄位原文只要不符那兩條 regex 就完整留下。現在改成:
    未宣告型別 → 記 violation + `logging.error`(大聲,不是 debug),內容丟掉。

    代價要說清楚:呼叫端若漏改,那一筆稽核就少了 value/detail(其餘欄位仍在)。
    這個代價是刻意選的 —— 稽核少一個欄位,比病歷內容永久進帳本輕。
    現存呼叫點由 tests/test_audit_events_2026_07_31.py 的 AST 掃描釘住。
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, AuditValue):
        return raw.to_payload()
    logging.error(
        "[ledger] %s 收到未宣告型別的值(型別=%s)→ 記為違規,內容不落地。"
        "呼叫端請改用 cmuh_common.audit_events 的型別(Code/Measure/Observed/"
        "Transition/Redacted/Reason)。", name, type(raw).__name__)
    return _violation("untyped_value", py_type=type(raw).__name__)


def render(payload) -> str:
    """把 payload 轉成人看的字串(告警信、log、日後的查閱介面)。不拋。"""
    try:
        if not payload:
            return ""
        if not isinstance(payload, dict):
            return str(payload)
        t = payload.get("t")
        if t == "code":
            v = payload.get("v", "")
            return f"{payload.get('kind', '?')}={v if v != '' else '(空白)'}"
        if t == "measure":
            nums = " ".join(f"{k}={payload[k]}"
                            for k in sorted(payload) if k != "t")
            return nums or "(無數值)"
        if t == "observed":
            return f"(內容不記錄,長度={payload.get('len')})"
        if t == "transition":
            return (f"{render(payload.get('before'))}"
                    f" → {render(payload.get('after'))}")
        if t == "redacted":
            return f"{payload.get('what', '?')}(刻意不記錄)"
        if t == "reason":
            code = str(payload.get("code") or "")
            text = REASONS.get(code, code)
            nums = " ".join(f"{k}={payload[k]}"
                            for k in sorted(payload) if k not in ("t", "code"))
            return f"{text}({nums})" if nums else text
        if t == _VIOLATION:
            return (f"★未宣告型別/不合格的值,內容未落地"
                    f"(reason={payload.get('reason')})★")
        return str(payload)
    except Exception:                        # pragma: no cover - 顯示不可弄壞流程
        return "(無法顯示)"
