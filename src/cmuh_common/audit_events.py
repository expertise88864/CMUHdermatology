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

# 代碼類值的字元白名單:數字、ASCII 字母與少數分隔符。中文、空白、標點一律不合格。
_CODE_RE = re.compile(r"\A[0-9A-Za-z._+,/\-]{0,32}\Z")
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
}

_VIOLATION = "violation"


def _numbers_ok(d: dict) -> bool:
    for k, v in d.items():
        if not isinstance(k, str) or not k.isidentifier():
            return False
        if v is None or isinstance(v, (int, float)) and not isinstance(v, bool):
            continue
        if isinstance(v, bool):
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

    字元集白名單(數字/ASCII 字母/`._+,/-`)、長度 ≤32。空字串合法(代表「該欄是空的」,
    那是有意義的稽核事實)。不合格 → violation,內容不落地。
    """
    kind: str
    code: str

    def to_payload(self) -> dict:
        kind = str(self.kind or "")
        if not _KIND_RE.match(kind):
            return _violation("bad_kind")
        raw = "" if self.code is None else str(self.code)
        if not _CODE_RE.match(raw):
            # ★這個 violation 本身是訊號★ 讀到的值不像代碼 ＝ 疑似定位漂移
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
            return _violation("unknown_reason", code=self.code)
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
