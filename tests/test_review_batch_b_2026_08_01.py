# -*- coding: utf-8 -*-
"""[2026-08-01 外部 code review 批次B / P1-03] F11 療程欄讀值。

★原本的問題★
`_f11_read_course_value()` 回一個字串，而且對三種完全不同的情況都回 `""`
（找不到欄位／讀取例外／真的空白）—— 呼叫端分不出「HIS 說沒有」與「我們沒讀到」。
更糟的是它把**讀到的原始內容**寫進 `automation_ui.log`（`讀到療程=%r`），
`_f11_click_finish_all` 的 timeline log 也印一次。定位一漂到姓名欄，
病人姓名就進了一個 5MB×3 輪替、沒有保存期限、常整包交給開發者除錯的檔案。

★使用者定案（2026-08-01）：臨床行為不變★
讀到無法辨識的內容仍**照舊按「全部完成」**，不擋不跳窗（與金絲雀同一套原則）。
改的是可觀測性：記 typed violation ＋ 寄通知，而 log 只留長度。

所以本檔分成兩半：
  1. 分類正確（含外審點名的那一整組輸入）；
  2. ★原值在型別上就流不出去★ —— 這是比「記得不要印」更強的保證。
"""
import ast
import io
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import course_value as cv  # noqa: E402

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


# ─── 分類（外審點名的整組輸入）────────────────────────────────────────────
@pytest.mark.parametrize("raw,status,value", [
    ("", cv.OK_EMPTY, ""),
    ("   ", cv.OK_EMPTY, ""),
    (None, cv.OK_EMPTY, ""),
    ("1", cv.OK_VALUE, "1"),
    ("2", cv.OK_VALUE, "2"),
    ("3", cv.OK_VALUE, "3"),
    ("０２", cv.INVALID, ""),      # 全形兩位 → 轉半形後是 "02"，兩位數不是療程值
    ("２", cv.OK_VALUE, "2"),      # 全形單位數 → 轉成 "2"
    ("A12", cv.INVALID, ""),
    ("王小明", cv.INVALID, ""),
    ("12345678", cv.INVALID, ""),  # 病歷號
    ("A123456789", cv.INVALID, ""),  # 身分證
    ("UNKNOWN", cv.INVALID, ""),
])
def test_course_values_are_classified(raw, status, value):
    got = cv.classify_course_value(raw)
    assert got.status == status
    assert got.value == value


@pytest.mark.parametrize("phi", ["王小明", "12345678", "A123456789",
                                 "呂冠愷(24994923)女 42歲"])
def test_an_unreadable_value_never_survives_in_the_result(phi):
    """★核心★ 分類器是唯一碰得到原值的地方，回傳值不可以把它帶出來。

    這比「記得不要印」強：呼叫端就算想印也印不出來。
    """
    got = cv.classify_course_value(phi)
    assert got.status == cv.INVALID
    assert got.value == ""
    blob = f"{got!r} {got.describe()}"
    for frag in (phi, phi[:2]):
        assert frag not in blob, f"原值外流：{frag}"
    assert got.observed_length == len(phi), "只能帶長度"


def test_describe_never_contains_a_raw_value():
    """`describe()` 會被寫進 log 與信件 —— 它只能講長度。"""
    got = cv.classify_course_value("王小明")
    assert got.describe() == "療程讀值 invalid，長度=3"


def test_not_found_and_read_failed_are_distinguishable():
    """★三種「空」要分得開★ 原本全部回 ""，事後查問題時完全沒有線索。"""
    assert cv.CourseReadResult(cv.NOT_FOUND).describe() == "找不到療程欄"
    assert cv.CourseReadResult(cv.READ_FAILED).describe() == "讀療程欄失敗"
    assert cv.CourseReadResult(cv.OK_EMPTY).describe() == "療程=(空白)"


# ─── 路由（使用者定案：行為不變）──────────────────────────────────────────
@pytest.mark.parametrize("raw,expect_no_print", [
    ("2", True), ("3", True), ("２", True), ("３", True),
    ("1", False), ("", False), ("王小明", False), ("12345678", False),
])
def test_only_a_real_course_2_or_3_takes_the_no_print_route(raw,
                                                            expect_no_print):
    """★「完成不印」只走真的療程 2/3★

    讀不到、讀到怪東西都走「全部完成」—— 那是使用者定案的既有行為，
    這一刀【不改它】，只是把判準從字串比對換成 typed 屬性。
    """
    assert cv.classify_course_value(raw).is_phototherapy_2_or_3 is expect_no_print


@pytest.mark.parametrize("status,expect", [
    (cv.OK_VALUE, False), (cv.OK_EMPTY, False),
    (cv.INVALID, True), (cv.NOT_FOUND, True), (cv.READ_FAILED, True),
])
def test_only_unreadable_states_need_attention(status, expect):
    """空白是正常的（這一診沒有療程）→ 不可以每次都寄信。"""
    assert cv.CourseReadResult(status, value="2" if status == cv.OK_VALUE
                               else "").needs_attention is expect


def test_a_status_that_needs_attention_never_carries_a_value():
    """需要通知的狀態一定沒有 value —— 否則信件/帳本就可能帶到原值。"""
    for status in (cv.INVALID, cv.NOT_FOUND, cv.READ_FAILED):
        assert cv.CourseReadResult(status).value == ""


@pytest.mark.parametrize("status", [cv.INVALID, cv.NOT_FOUND, cv.READ_FAILED])
def test_a_non_ok_status_can_never_take_the_no_print_route(status):
    """★縱深防禦：就算有人把狀態建錯，也不可以走「完成不印」★

    `is_phototherapy_2_or_3` 同時檢查 status 與 value。目前 value 只有在
    OK_VALUE 時才有內容，所以那個 status 檢查看起來是多餘的 ——
    突變驗證也確實顯示拿掉它不會有任何測試轉紅。
    但它擋的是【未來】：哪天有人在 INVALID 上填了 value（例如想「順便留個線索」），
    那一刻照光病人的完成路徑就會被一個讀錯的值決定。
    這支直接建出那個不一致的狀態，把防禦本身釘住。
    """
    forged = cv.CourseReadResult(status, value="2", observed_length=1)
    assert forged.is_phototherapy_2_or_3 is False, \
        "非 OK_VALUE 的狀態不可以決定臨床路徑"


# ─── 接線：main.py 不可以再印原值 ────────────────────────────────────────
def _main_tree():
    return ast.parse(io.open(os.path.join(REPO_ROOT, "src", "main.py"),
                             encoding="utf-8").read())


def _func(name):
    return next(n for n in ast.walk(_main_tree())
                if isinstance(n, ast.FunctionDef) and n.name == name)


def _code_of(name) -> str:
    """函式的原始碼，★剝掉 docstring★。

    這一輪已經被自我命中騙過三次：docstring 本來就會引用「不可以用 X」來解釋
    為什麼，不剝掉就會比對到自己的說明文字。
    """
    fn = _func(name) if isinstance(name, str) else name
    stripped = ast.parse(ast.unparse(fn)).body[0]
    body = getattr(stripped, "body", [])
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        stripped.body = body[1:] or [ast.Pass()]
    return ast.unparse(stripped)


@pytest.mark.parametrize("func_name", ["_f11_read_course_value",
                                       "_f11_click_finish_all"])
def test_the_f11_path_logs_only_the_typed_description(func_name):
    """★兩個洩漏點都要堵★

    原本 `_f11_read_course_value` 印 `讀到療程=%r`、`_f11_click_finish_all` 的
    timeline 也印一次 `療程=%s`。現在兩支都只能傳 `describe()` 進 logging。
    用 AST 檢查傳給 logging 的參數，不比對原始碼文字（註解會自我命中）。
    """
    fn = _func(func_name)
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logging"):
            continue
        for arg in node.args[1:]:            # 第 0 個是格式字串
            src = ast.unparse(arg)
            assert "course_value" not in src and src != "result.value", (
                f"{func_name} 第 {node.lineno} 行把原值傳進 logging：{src}")


def test_the_unreadable_case_is_recorded_and_notified():
    """讀不到要留下紀錄（呼叫 `_f11_report_unreadable_course`），
    而且那支只記長度、不記原值。"""
    fn = _func("_f11_read_course_value")
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "_f11_report_unreadable_course" in called

    reporter = _code_of("_f11_report_unreadable_course")
    assert "observed_length" in reporter, "只能帶長度"
    assert "result.value" not in reporter, "★不可以把原值帶進帳本或信件★"


def test_course_unreadable_is_a_declared_reason():
    """帳本的 Reason 是封閉集合 —— 沒宣告的話會被記成 violation 而失去語意。"""
    from cmuh_common.audit_events import REASONS, Reason
    assert "course_unreadable" in REASONS
    payload = Reason("course_unreadable", length=8).to_payload()
    assert payload["t"] == "reason" and payload["length"] == 8


def test_the_ledger_value_does_not_claim_the_field_was_blank():
    """★措辭鐵律★ 讀不到時記成「療程=(空白)」是在宣稱 HIS 說沒有療程 ——
    那與「我們沒讀到」是完全不同的線索。"""
    import main  # noqa: PLC0415  這支需要真的呼叫 helper
    blank = main._f11_course_ledger_value(cv.CourseReadResult(cv.OK_EMPTY))
    assert blank.to_payload() == {"t": "code", "kind": "療程", "v": ""}
    for status in (cv.INVALID, cv.NOT_FOUND, cv.READ_FAILED):
        got = main._f11_course_ledger_value(
            cv.CourseReadResult(status, observed_length=8)).to_payload()
        assert got["t"] == "reason" and got["code"] == "course_unreadable", \
            f"{status} 被記成了 {got}"


# ─── ★[2026-08-01 外審第 2 輪] 兩個 CONFIRMED★ ───────────────────────────
def test_f12_is_rechecked_before_the_completion_action():
    """★P1：F12 被採樣器吃掉，完成動作照樣送出去★

    `_sample_patient_locator()` 【刻意】把 F12 的 SubsystemInterrupted 吞掉並正常
    返回（2026-08-02 定案：F12 不可以讓整筆稽核紀錄消失）。那個吞掉在當時是安全的，
    因為 mismatch 只發生在動作【之後】。

    我這一刀新增了一條動作【之前】就會記帳本的路（療程讀值異常），於是同一個吞掉
    變成：醫師按 F12 → 被採樣器吃掉 → 照樣按下「全部完成」→ 之後的
    interruptible sleep 才發現 F12 → UI 顯示「已由 F12 手動終止」，
    而完成動作其實已經送出去了。

    所以送出之前必須再有一道明確的 `check_stop()`。
    """
    fn = _func("_f11_快速完成_main")
    lines = _code_of(fn).splitlines()
    i_read = next(i for i, ln in enumerate(lines)
                  if "_f11_read_course_value(" in ln)
    i_send = next(i for i, ln in enumerate(lines)
                  if "_f11_send_finish_no_print(" in ln
                  or "_f11_click_finish_all(" in ln)
    between = [ln for ln in lines[i_read:i_send] if "check_stop()" in ln]
    assert between, (
        "讀療程值與送出完成動作之間沒有 check_stop() —— "
        "F12 會被定位採樣器吃掉，然後照樣按下完成")


def test_the_unreadable_course_does_not_reuse_the_mismatch_notification():
    """★P2：借用 mismatch 那封信，每一句都是假的★

    mismatch 信的固定模板寫著「自動化寫入後回讀驗證不一致（該次寫入已依既有流程
    中止/警告醫師）」。用在療程讀值異常這條路上：
      * 沒有任何寫入；
      * 沒有回讀比對；
      * 沒有警告醫師；
      * F11 是【刻意】繼續按「全部完成」的。
    收到那封信的人會去查一個根本不存在的寫入錯誤。
    """
    reporter = _code_of("_f11_report_unreadable_course")
    assert "_LEDGER_MISMATCH" not in reporter, \
        "★不可以用 mismatch★ 它會被路由到內容完全不符事實的那封信"
    assert "_LEDGER_SKIPPED" in reporter
    assert "_notify_course_unreadable" in reporter


def test_the_dedicated_notification_states_what_actually_happened():
    """專屬通知要講真話：沒寫入、沒回讀、F11 照常完成、醫師沒看到警告。"""
    body = _code_of("_notify_course_unreadable")
    assert "全部完成" in body, "要講明這一次仍照常按了全部完成"
    assert "讀取" in body, "要講明問題在讀取階段"
    assert "沒有任何值被寫入" in body
    # 而且不可以出現 mismatch 那套說法
    assert "回讀驗證不一致" not in body
    assert "警告醫師" not in body or "沒有看到任何警告" in body


def test_the_dedicated_notification_is_deduped_and_carries_no_raw_value():
    """同一天只寄一次（不然定位漂掉時每個病人都寄一封），且只帶長度。"""
    src = _code_of("_notify_course_unreadable")
    assert "_COURSE_ALERTS.claim(" in src and "_COURSE_ALERTS.release(" in src
    assert "result.describe()" in src, "只能帶 describe()（保證不含原值）"
    assert "result.value" not in src
