# -*- coding: utf-8 -*-
"""[2026-07-31 第二輪外審 P2-03] 型別化稽核事件取代 denylist regex 消毒器。

舊做法是 `action_ledger.sanitize_text`：兩條 regex（台灣身分證樣式、8 位以上連續
數字）在落地前猜「哪一段像個資」。這一檔要證明的是**換掉之後真的不一樣**，
而不是把同一件事換個寫法：

  ★`test_a_patient_name_used_to_sail_through_the_old_regex`★
      舊 regex 對中文姓名一個字都攔不到，而 F11 的 `_f11_read_course_value()`
      讀療程欄【完全沒有把關】—— 定位漂到姓名欄時姓名就會進帳本。現在同一個值
      進不去。這條是整個 P2-03 的理由。

  ★`test_a_legitimate_eight_digit_code_is_no_longer_redacted`★
      反方向：舊 regex 會把 8 位數的合法醫令代碼整個吃掉，而且沒有任何訊號 ——
      偏偏那正是要查「改版把醫令寫錯」的時候。

  ★`test_a_violation_never_carries_the_original_value`★
      違規紀錄本身不可以把原值抄一份進去（那等於用另一個欄位做了同一件壞事）。
"""
import ast
import io
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from cmuh_common import action_ledger as al          # noqa: E402
from cmuh_common.audit_events import (                # noqa: E402
    REASONS, Code, Measure, Observed, Redacted, Reason, Transition,
    render, to_field_payload,
)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# 舊版真正用過的兩條樣式（從被刪掉的 sanitize_text 抄來，用於對照）
_OLD_PATTERNS = (re.compile(r"[A-Za-z][12]\d{8}"), re.compile(r"\d{8,}"))


def _old_sanitize(s: str) -> str:
    out = str(s or "")
    for pat in _OLD_PATTERNS:
        out = pat.sub("[REDACTED]", out)
    return out


# ─── ★這次改版的理由★ ────────────────────────────────────────────────────
def test_a_patient_name_used_to_sail_through_the_old_regex():
    """★核心★ 舊消毒器擋不住它宣稱要擋的東西。

    F11 讀療程欄只做 strip + 全形轉半形，沒有任何把關（見
    `_f11_normalize_course_value`）。定位漂到姓名欄時，`value=f"療程={course_value}"`
    就把姓名寫進 hash-chain 帳本 —— 而帳本是 append-only 的，寫進去就永久留著。
    """
    name = "王小明"
    assert _old_sanitize(f"療程={name}") == f"療程={name}", \
        "前提：舊 regex 對中文姓名完全沒作用（這正是它的問題）"

    payload = Code("療程", name).to_payload()
    assert payload["t"] == "violation"
    assert name not in json.dumps(payload, ensure_ascii=False), \
        "姓名不可以用任何形式出現在落地內容裡"


def test_a_legitimate_eight_digit_code_is_no_longer_redacted():
    """★反方向的傷害★ 舊 regex 把合法值也吃掉，而且是無聲的。

    醫令代碼今天最長 7 位；院方哪天發 8 位的，整欄集體變 [REDACTED] ——
    帳本正好在最需要證據的時候失去證據。
    """
    code = "12345678"
    assert "[REDACTED]" in _old_sanitize(code), "前提：舊 regex 會遮掉它"
    assert Code("醫令代碼", code).to_payload() == {
        "t": "code", "kind": "醫令代碼", "v": code}


def test_a_violation_never_carries_the_original_value():
    """違規紀錄不可以把原值抄一份進去 —— 那等於換個欄位做同一件壞事。"""
    secret = "A123456789 王小明 0912345678"
    for payload in (Code("身份", secret).to_payload(),
                    to_field_payload("value", secret),
                    Reason("不存在的理由碼", n=1).to_payload()):
        blob = json.dumps(payload, ensure_ascii=False)
        assert "王小明" not in blob
        assert "A123456789" not in blob
        assert "0912345678" not in blob


# ─── 邊界：帳本只收型別 ───────────────────────────────────────────────────
def test_the_ledger_records_a_violation_for_a_plain_string(tmp_path, caplog):
    """自由文字進不了帳本，而且是【大聲】的（error 不是 debug）。"""
    lg = al.ActionLedger(tmp_path / "l.jsonl")
    with caplog.at_level("ERROR"):
        lg.record("his_field", "測試", value="採樣到的原文 王小明", detail="隨手寫的")
    rec = al.read_records(lg.path)[0]
    assert rec["value"]["t"] == "violation"
    assert rec["detail"]["t"] == "violation"
    assert "王小明" not in json.dumps(rec, ensure_ascii=False)
    assert any("未宣告型別" in r.getMessage() for r in caplog.records), \
        "要留下 error log，否則呼叫端漏改沒人知道"


def test_an_empty_value_stays_empty(tmp_path):
    """沒帶 value 的紀錄不該被記成違規。"""
    lg = al.ActionLedger(tmp_path / "l.jsonl")
    lg.record("his_menu", "測試")
    rec = al.read_records(lg.path)[0]
    assert rec["value"] == {} and rec["detail"] == {}


def test_typed_values_land_as_structured_payloads(tmp_path):
    lg = al.ActionLedger(tmp_path / "l.jsonl")
    lg.record("his_field", "UVB", value=Measure(dose=700, count=11),
              detail=Reason("readback_mismatch", readback_len=3),
              outcome=al.OUTCOME_MISMATCH)
    rec = al.read_records(lg.path)[0]
    assert rec["value"] == {"t": "measure", "dose": 700, "count": 11}
    assert rec["detail"] == {"t": "reason", "code": "readback_mismatch",
                             "readback_len": 3}
    ok, n, _msg = al.verify_chain(lg.path)
    assert ok and n == 1, "結構化 payload 不可弄壞 hash chain"


# ─── 各型別 ───────────────────────────────────────────────────────────────
def test_observed_only_ever_stores_a_length():
    """★型別層面的「不記內容」★ 想描述從 HIS 讀到什麼，唯一的表達方式是長度。"""
    p = Observed(len("王小明的病歷內容")).to_payload()
    assert p == {"t": "observed", "len": 8}
    assert "王" not in json.dumps(p, ensure_ascii=False)


def test_code_accepts_the_real_values_this_repo_writes():
    """把實際會寫進去的值列出來 —— 白名單不可以嚴到把正常稽核擋掉。"""
    for kind, value in (("醫令代碼", "1850159"), ("醫令代碼", "51017"),
                        ("療程", "2"), ("療程", ""), ("身份", "40"),
                        ("身份", "001"), ("同意書", "MO04"), ("同意書", "MU02")):
        p = Code(kind, value).to_payload()
        assert p["t"] == "code", f"{kind}={value!r} 不該被擋"
        assert p["v"] == value


@pytest.mark.parametrize("bad", [
    "王小明", "台中市北區學士路2號", "abc def", "x" * 33, "line\nbreak",
])
def test_code_rejects_anything_that_is_not_a_short_code(bad):
    assert Code("療程", bad).to_payload()["t"] == "violation"


def test_reason_is_a_closed_set():
    """★自由文字在這裡被擋掉★ 新增理由要改 REASONS（一個看得見的動作）。"""
    assert Reason("readback_mismatch").to_payload()["code"] == "readback_mismatch"
    assert Reason("回讀怪怪的").to_payload()["t"] == "violation"


@pytest.mark.parametrize("bad", [{"x": "字串"}, {"x": []}, {"2bad": 1}])
def test_measure_only_takes_numbers(bad):
    assert Measure(**bad).to_payload()["t"] == "violation"


def test_measure_takes_none_for_an_unknown_number():
    """回讀不到時是 None，不是字串 "?" —— 那正是舊版 f-string 的寫法。"""
    assert Measure(dose=None, count=3).to_payload() == {
        "t": "measure", "dose": None, "count": 3}


def test_transition_requires_typed_sides():
    assert Transition(Code("身份", "01"), Code("身份", "40")).to_payload()["t"] \
        == "transition"
    assert Transition("01", "40").to_payload()["t"] == "violation"  # type: ignore[arg-type]


def test_redacted_says_what_was_deliberately_not_recorded():
    assert Redacted("卡號").to_payload() == {"t": "redacted", "what": "卡號"}


# ─── 建構子不可拋（它們在臨床路徑上被求值）────────────────────────────────
@pytest.mark.parametrize("make", [
    lambda: Code(None, None), lambda: Code("x" * 999, "y" * 999),
    lambda: Observed("不是數字"), lambda: Observed(-1),
    lambda: Reason(None), lambda: Measure(x=object()),
    lambda: Transition(None, None), lambda: Redacted(None),
])
def test_constructors_never_raise(make):
    """★這些在 `_record_his_action` 的 try 【之外】被求值★

    `_record_his_action(..., value=Code(...))` 的 `Code(...)` 是在呼叫端的框架裡
    先算出來的。建構子若拋例外，就直接打斷熱鍵流程 —— 稽核弄壞臨床功能，
    正好是這個模組最不該做的事。無效值要到 to_payload() 才變成 violation。
    """
    obj = make()
    assert obj.to_payload()["t"] == "violation"
    assert isinstance(str(obj), str)


# ─── 人看得懂的呈現（告警信要用）──────────────────────────────────────────
def test_render_covers_every_type():
    assert render(Code("療程", "2").to_payload()) == "療程=2"
    assert "空白" in render(Code("療程", "").to_payload())
    assert render(Measure(dose=700).to_payload()) == "dose=700"
    assert "長度=5" in render(Observed(5).to_payload())
    assert "→" in render(Transition(Code("身份", "01"),
                                    Code("身份", "40")).to_payload())
    assert "卡號" in render(Redacted("卡號").to_payload())
    assert REASONS["no_focus"] in render(Reason("no_focus").to_payload())
    assert render({}) == "" and render(None) == ""


def test_str_of_a_typed_value_is_the_rendered_text():
    """`_notify_audit_mismatch` 對 value/detail 做 `str(...)` 送進告警信與定位索引。
    型別化之後那裡沒有改，靠的就是 __str__ —— 釘住它，否則信裡會出現物件 repr。"""
    assert str(Code("療程", "2")) == "療程=2"
    assert "回讀與預期不符" in str(Reason("readback_mismatch"))


# ─── 舊 schema 相容 ───────────────────────────────────────────────────────
def test_v1_and_v2_records_verify_in_the_same_chain(tmp_path):
    """★診間電腦上已經有 v1 紀錄★ 換 schema 不可以讓既有帳本驗不過。

    chain_hash/_canonical 是欄位無關的（照紀錄實際內容重算），所以字串 value 的
    舊紀錄與 dict value 的新紀錄可以接在同一條鏈上。
    """
    path = tmp_path / "l.jsonl"
    payload = {"schema_version": 1, "seq": 1, "ts": "2026-07-01T08:00:00",
               "surface": "his_field", "action": "舊紀錄", "machine": "", "user": "",
               "prev": al.GENESIS, "target": "field:療程", "value": "療程=2",
               "his_version": "1150713", "canary": "OK", "outcome": "ok",
               "detail": "", "correlation_id": "", "app_version": "2026.07.01.1"}
    rec = dict(payload)
    rec["hash"] = al.chain_hash(al.GENESIS, payload)
    io.open(path, "w", encoding="utf-8").write(
        json.dumps(rec, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")) + "\n")

    lg = al.ActionLedger(path)
    assert lg.record("his_field", "新紀錄", value=Code("療程", "3"),
                     outcome=al.OUTCOME_OK)
    ok, n, msg = al.verify_chain(path)
    assert ok and n == 2, msg
    recs = al.read_records(path)
    assert recs[0]["schema_version"] == 1 and recs[0]["value"] == "療程=2"
    assert recs[1]["schema_version"] == 2 and recs[1]["value"]["t"] == "code"


def test_the_schema_version_was_bumped():
    assert al.SCHEMA_VERSION == 2, "欄位形狀變了就要換版號，否則日後判讀不了"


# ─── 呼叫端全數遷移（AST，不是字串比對）──────────────────────────────────
_TYPES = {"_EvCode", "_EvMeasure", "_EvObserved", "_EvReason", "_EvRedacted",
          "_EvTransition"}


def _typed(node) -> bool:
    if isinstance(node, ast.IfExp):
        return _typed(node.body) and _typed(node.orelse)
    if isinstance(node, ast.Constant) and node.value in ("", None):
        return True
    return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
            and node.func.id in _TYPES)


def _record_call_sites():
    tree = ast.parse(io.open(os.path.join(REPO_ROOT, "src", "main.py"),
                             encoding="utf-8").read())
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_record_his_action"):
            yield node


def test_every_record_call_site_passes_typed_values():
    """★用 AST 而不是比對原始碼文字★

    這一輪已經被「註解/docstring 自己命中比對字串」騙過好幾次。AST 看的是
    實際傳進去的東西，註解怎麼寫都不影響。
    """
    sites = list(_record_call_sites())
    assert len(sites) >= 20, f"呼叫點只找到 {len(sites)} 個，掃描器可能壞了"
    bad = [(n.lineno, kw.arg) for n in sites for kw in n.keywords
           if kw.arg in ("value", "detail") and not _typed(kw.value)]
    assert not bad, f"這些呼叫點還在傳自由文字：{bad}"


def test_the_denylist_sanitizer_is_gone():
    """留著它就等於留著那條「猜」的路 —— 而它猜不準、誤遮又無聲。"""
    assert not hasattr(al, "sanitize_text")
    assert not hasattr(al, "_PII_PATTERNS")


# ══════════════════════════════════════════════════════════════════════════
# [2026-08-01 外審第 2 輪] 三個 CONFIRMED finding 的回歸測試
# ══════════════════════════════════════════════════════════════════════════
_PHI_LIKE = ["12345678", "A123456789", "0912345678", "87654321"]


@pytest.mark.parametrize("phi", _PHI_LIKE)
def test_a_chart_number_shaped_value_no_longer_passes_as_a_course(phi):
    """★外審 P1：型別化之後在這條路徑上比舊 regex 更糟★

    舊的 denylist（`\\d{8,}`、身分證樣式）擋得住病歷號與身分證；而第一版的
    `Code` 只有一條共用字元白名單 —— `12345678` 全是合法字元、長度也短，
    **照樣寫進 append-only 帳本**。

    而療程值正是【從 HIS 讀回來的】：`_f11_read_course_value()` 只做 strip +
    全形轉半形，完全沒有把關。定位一漂到病歷號欄位，病歷號就這樣進帳本。

    現在每個 kind 有自己的值域（療程＝一位數），讀到八位數就是 violation ——
    而那個 violation 本身就是「疑似定位漂移」的訊號。
    """
    payload = Code("療程", phi).to_payload()
    assert payload["t"] == "violation"
    assert phi not in json.dumps(payload, ensure_ascii=False)


@pytest.mark.parametrize("phi", _PHI_LIKE)
def test_the_old_denylist_would_have_caught_these(phi):
    """★釘住「舊做法在這一點上是對的」★

    這幾個樣式正是舊 regex 唯一守得住、而第一版型別化守不住的東西。
    留著這支是為了讓「新做法在每一個面向都不比舊的差」變成可驗證的宣稱，
    而不是我在文件裡的說法。
    """
    assert "[REDACTED]" in _old_sanitize(phi)


def test_each_kind_has_its_own_domain():
    """療程一位數、身份三位數、醫令代碼八位數 —— 值域不可以共用一條寬鬆規則。"""
    assert Code("療程", "2").to_payload()["t"] == "code"
    assert Code("療程", "12").to_payload()["t"] == "violation"
    assert Code("身份", "001").to_payload()["t"] == "code"
    assert Code("身份", "0011").to_payload()["t"] == "violation"
    assert Code("醫令代碼", "1850159").to_payload()["t"] == "code"
    assert Code("同意書", "MO04").to_payload()["t"] == "code"
    assert Code("同意書", "王小明").to_payload()["t"] == "violation"


def test_an_undeclared_kind_is_rejected(caplog):
    """★fail-closed★ 新增一種 kind 就必須宣告它的值域 ——
    不可以靠一條寬鬆的共用規則矇混過去（那正是 P1 的成因）。"""
    with caplog.at_level("ERROR"):
        payload = Code("我沒宣告過的欄位", "12345678").to_payload()
    assert payload["t"] == "violation" and payload["reason"] == "undeclared_kind"
    assert "12345678" not in json.dumps(payload, ensure_ascii=False)
    assert any("沒有宣告值域" in r.getMessage() for r in caplog.records)


def test_an_unknown_reason_does_not_carry_its_own_text():
    """★外審 P2：violation 自己在洩漏★

    第一版寫 `_violation("unknown_reason", code=self.code)` —— 於是
    `Reason(採樣到的原文)` 會把那段原文完整留在帳本裡，正好違反本模組
    「violation 絕不攜帶原值」的契約（用另一個欄位做了同一件壞事）。
    """
    secret = "A123456789 王小明"
    payload = Reason(secret).to_payload()
    assert payload["t"] == "violation" and payload["reason"] == "unknown_reason"
    blob = json.dumps(payload, ensure_ascii=False)
    assert secret not in blob and "王小明" not in blob and "A123456789" not in blob
    assert payload["length"] == str(len(secret)), "只留長度"
