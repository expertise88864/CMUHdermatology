# -*- coding: utf-8 -*-
"""[2026-08-01 外部 code review 批次A] 八項低風險修正的回歸測試。

這一批的共同形狀是 **「呼叫沒有拋例外，不代表操作完成」** 與
**「型別對，不代表值合法」** —— 這個 repo 反覆出事的兩個地方。

  * `open_url` 忽略 `webbrowser.open()` 的 False（找不到瀏覽器不會拋例外）。
  * `_numbers_ok` 放 NaN／Infinity 過去，而 `json.dumps` 預設會把它們寫成
    **不合法的 JSON**，落在 hash chain 上讓整條之後都驗不了。
  * `get_ime_focus_hwnd` 的 AttachThreadInput 沒有 finally，例外時輸入佇列
    永遠黏在 HIS 的執行緒上。
  * reg52 fetcher 的 `session` 參數一定會被覆蓋 —— 一個「騙人的 API 契約」。
  * `.before-reset-*` 備份沿用原檔 mtime，保留期掃描當天就把它刪了。
"""
import io
import json
import os
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import action_ledger as al            # noqa: E402
from cmuh_common import program_launcher as pl         # noqa: E402
from cmuh_common import reg52_fetch as rf              # noqa: E402
from cmuh_common import settings_defaults as sd        # noqa: E402
from cmuh_common.audit_events import Measure           # noqa: E402


# ─── P2-08 `webbrowser.open()` 回 False 不是成功 ──────────────────────────
def test_a_browser_that_refuses_is_a_failure(monkeypatch):
    """★找不到瀏覽器時 `webbrowser.open()` 不拋例外，只回 False★

    原本無論回什麼都當成功 → 使用者按了院內系統連結、什麼都沒發生、
    也沒有任何訊息可查。
    """
    monkeypatch.setattr(pl.webbrowser, "open", lambda *a, **k: False)
    out = pl.open_url("https://appointment.cmuh.org.tw/")
    assert out.failed is True
    assert out.error_title == "開啟失敗"
    assert "找不到可用的瀏覽器" in out.error_message


def test_a_browser_that_accepts_is_success(monkeypatch):
    monkeypatch.setattr(pl.webbrowser, "open", lambda *a, **k: True)
    assert pl.open_url("https://example.org/").ok is True


# ─── P2-06 NaN／Infinity 不是數字 ────────────────────────────────────────
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_numbers_never_reach_the_ledger(bad):
    """★NaN 會被 json.dumps 寫成不合法 JSON★

    `float("nan")` 是 float 的實例，所以只看型別的檢查會放它過去。
    但寫出來的 `NaN` 不是合法 JSON —— 那一行從此沒有任何工具解析得了，
    而它還在 hash chain 上。劑量回讀不到時本來就該用 `None`。
    """
    assert Measure(dose=bad).to_payload()["t"] == "violation"


def test_finite_floats_and_none_still_work():
    """不可矯枉過正：正常的劑量與「讀不到」仍要記得進去。"""
    assert Measure(dose=700.5).to_payload() == {"t": "measure", "dose": 700.5}
    assert Measure(dose=None).to_payload() == {"t": "measure", "dose": None}


def test_the_ledger_refuses_to_serialise_non_finite_numbers():
    """★第二道守衛★ 帳本也收得到不經過 audit_events 的 dict，
    所以 `_canonical` 自己也要 `allow_nan=False`（守衛不可以只有一層）。"""
    with pytest.raises(ValueError):
        al._canonical({"dose": float("nan")})
    # 正常值不受影響
    assert al._canonical({"dose": 700}) == '{"dose":700}'


def test_a_ledger_line_is_always_parseable_json(tmp_path):
    lg = al.ActionLedger(tmp_path / "l.jsonl")
    lg.record("his_field", "UVB", value=Measure(dose=700, count=11))
    for line in io.open(lg.path, encoding="utf-8"):
        if line.strip():
            json.loads(line)          # 不得拋出


# ─── P2-05 AttachThreadInput 一定要 detach ───────────────────────────────
def test_ime_focus_detaches_even_when_getfocus_raises(monkeypatch):
    """★例外時沒有 detach，輸入佇列會永遠黏在 HIS 的執行緒上★

    症狀是之後本程式的按鍵/焦點行為全部異常，而且只能重開程式才會好。
    同檔的 `get_focused_control_hwnd` 早就用 try/finally —— 這裡是漏掉。
    """
    from cmuh_common import his_window as hw

    calls = []

    class _U:
        @staticmethod
        def GetForegroundWindow():
            return 1234

        @staticmethod
        def GetWindowThreadProcessId(_h, _p):
            return 99

        @staticmethod
        def AttachThreadInput(_a, _b, attach):
            calls.append(bool(attach))
            return 1

        @staticmethod
        def GetFocus():
            raise OSError("模擬 GetFocus 失敗")

    monkeypatch.setattr(hw, "_user32", lambda: _U())
    monkeypatch.setattr(hw, "_kernel32",
                        lambda: type("K", (), {"GetCurrentThreadId":
                                               staticmethod(lambda: 1)})())
    hw.get_ime_focus_hwnd()           # 不得拋出（外層有 except）
    assert calls == [True, False], f"attach/detach 不成對：{calls}"


# ─── P2-02 session 參數不可以是騙人的 ────────────────────────────────────
@pytest.mark.parametrize("fetch,args", [
    ("_fetch_east_district_reg52_html", ("1234", "王醫師")),
    ("_fetch_huihe_reg52_html", ("1234", "王醫師")),
    ("_fetch_huisheng_reg52_html", ("1234", "王醫師")),
])
def test_an_injected_session_is_actually_used(monkeypatch, fetch, args):
    """★「一定會被丟掉的參數」比沒有參數更糟★

    原本每支都無條件 `session = _get_thread_local_...()` 覆蓋掉呼叫端傳進來的
    session —— proxy／認證／憑證政策全部失效，而測試又以為自己注入得進來。
    """
    monkeypatch.setattr(rf, "_circuit_is_tripped", lambda s: False)
    monkeypatch.setattr(rf, "_source_backoff_allow", lambda k: (True, 0.0))
    monkeypatch.setattr(rf, "_source_backoff_fail", lambda *a: (0.0, 1))
    monkeypatch.setattr(rf, "_source_backoff_success", lambda k: None)
    monkeypatch.setattr(rf, "_circuit_record_fail", lambda s: False)
    monkeypatch.setattr(rf, "_circuit_record_success", lambda s: None)
    monkeypatch.setattr(
        rf, "_get_thread_local_reg52_external_session",
        lambda: pytest.fail("有傳 session 進來就不該再取 thread-local"))

    used = []

    class _FakeSession:
        def get(self, url, **k):
            used.append(url)
            raise RuntimeError("到這裡就夠了 —— 證明用的是注入的 session")

    with pytest.raises(RuntimeError):
        getattr(rf, fetch)(_FakeSession(), *args)
    assert used, "注入的 session 完全沒被用到"


def test_no_session_still_falls_back_to_thread_local(monkeypatch):
    """反方向：呼叫端沒帶 session 時仍要自己取（現有呼叫端就是這樣用的）。"""
    monkeypatch.setattr(rf, "_circuit_is_tripped", lambda s: False)
    monkeypatch.setattr(rf, "_source_backoff_allow", lambda k: (True, 0.0))
    monkeypatch.setattr(rf, "_source_backoff_fail", lambda *a: (0.0, 1))
    monkeypatch.setattr(rf, "_circuit_record_fail", lambda s: False)
    taken = []

    class _S:
        def get(self, url, **k):
            raise RuntimeError("stop")

    monkeypatch.setattr(rf, "_get_thread_local_reg52_external_session",
                        lambda: (taken.append(1), _S())[1])
    with pytest.raises(RuntimeError):
        rf._fetch_east_district_reg52_html(None, "1234", "王醫師")
    assert taken, "沒帶 session 時要回退到 thread-local"


# ─── P1-06 備份的年齡要從建立時間算 ──────────────────────────────────────
def test_a_fresh_backup_is_not_immediately_expired(tmp_path):
    """★今天備份、明天就被刪掉★

    `copy2` 連 mtime 一起複製 → 一個 180 天沒動過的設定檔，今天備份出來的
    `.before-reset-*` mtime 仍是 180 天前，而那類備份的保留期是 90 天
    → 下一次掃描直接刪掉。使用者按了還原預設之後其實沒有退路。
    """
    target = tmp_path / "doctors.json"
    target.write_text('{"a": 1}', encoding="utf-8")
    old = (datetime.now() - timedelta(days=180)).timestamp()
    os.utime(target, (old, old))
    assert datetime.fromtimestamp(os.path.getmtime(target)) < \
        datetime.now() - timedelta(days=100), "前提：原檔真的很舊"

    dest = sd._backup_existing(str(target))
    assert dest and os.path.exists(dest)

    age_days = (datetime.now()
                - datetime.fromtimestamp(os.path.getmtime(dest))).days
    assert age_days < 1, (
        f"備份的 mtime 還是 {age_days} 天前 → 保留期掃描會立刻刪掉它")


def test_the_backup_keeps_the_original_content(tmp_path):
    """不可為了改時間而弄壞內容。"""
    target = tmp_path / "doctors.json"
    target.write_text('{"a": 1}', encoding="utf-8")
    dest = sd._backup_existing(str(target))
    assert io.open(dest, encoding="utf-8").read() == '{"a": 1}'


# ─── P3-02 log 要說得跟實際行為一樣 ──────────────────────────────────────
def test_the_breaker_log_does_not_promise_a_restart_is_needed():
    """熔斷器有冷卻會自動半開，log 卻寫「重啟程式才會重試」——
    那會讓查問題的人以為不重開就永遠不會再連。"""
    import ast

    raw = io.open(os.path.join(os.path.dirname(__file__), "..", "src",
                               "cmuh_common", "reg52_fetch.py"),
                  encoding="utf-8").read()
    # ★只看會被執行的程式碼★ 註解裡本來就會引用舊措辭來解釋它為什麼錯，
    #   不剝掉就會比對到自己的說明文字（本 repo 反覆踩到的自我命中）。
    tree = ast.parse(raw)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if (isinstance(body, list) and body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            node.body = body[1:] or [ast.Pass()]
    code = ast.unparse(ast.fix_missing_locations(tree))
    assert "重啟程式才會重試" not in code
    assert "重啟才會重試" not in code
    assert "自動半開重試" in code


def test_the_breaker_reset_minutes_come_from_the_real_constant():
    """措辭裡的分鐘數要跟熔斷器實際的冷卻時間同源，不可各寫各的。"""
    from cmuh_common import fetch_resilience as fr
    assert rf._CIRCUIT_BREAKER_RESET_MIN == int(
        fr._CIRCUIT_BREAKER_RESET_SEC // 60)


# ─── P3-01 review 標記日期 ───────────────────────────────────────────────
def test_review_markers_do_not_claim_a_future_date():
    """★標記日期比 commit 還晚會破壞事件時序★

    2026-08-01 這一天的工作我全寫成了 2026-08-05。這條擋的是「日期標記寫到未來」
    這個形狀本身：本 repo 的 review 標記一律用當天日期。
    （roster／day_course 測試裡的 2026-08-05 是**測試資料**（8/5 是週三），
      所以只掃 src/ 的註解，不掃 tests/。）
    """
    root = os.path.join(os.path.dirname(__file__), "..", "src")
    bad = []
    for dirpath, _dirs, files in os.walk(root):
        if "__pycache__" in dirpath:
            continue
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            for i, line in enumerate(io.open(path, encoding="utf-8"), 1):
                if "2026-08-05" in line:
                    bad.append(f"{name}:{i}")
    assert not bad, f"src/ 仍有未來日期的標記：{bad[:10]}"


# ─── ★[2026-08-01 外審第 2 輪] 併行的 worker 不可以共用同一個 Session★ ────
def test_the_parallel_external_fetchers_must_not_share_a_session():
    """★我上一版的「修好假注入」反而製造了併發 bug★

    `session or _get_thread_local_...()` 本身是對的，但正式呼叫端【本來就有傳
    session 進來】—— 而且傳的是 `main.py:6892` 那個【主院】的 thread-local
    session。那四支 fetcher 會被丟進 `ThreadPoolExecutor(thread_name_prefix=
    "r52ext")` 併行跑，於是多個 worker 共用同一個 `requests.Session`：

      * 本 repo 自己在 `http_session_registry._session_http_guard` 明寫
        「requests.Session 非執行緒安全」（連線池與 cookie 會競態）；
      * 那還是【主院】的 retry 設定，不是院外那組刻意的零重試。

    後果是分院掛號數可能整批抓不到 → 用舊快取 → 止掛提醒漏掉。
    所以正式呼叫端一律傳 `None`，讓每個 worker 各自取自己的 external
    thread-local session；參數保留給單獨呼叫與測試注入。

    ★為什麼上一版的測試沒抓到★ 那些測試把一個 fake session 依序【直接】傳給
    各 fetcher，根本沒有經過 `check_appointment_count` 的多來源 executor ——
    又是「測了函式、沒測生產路徑」。
    """
    import ast
    import os

    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "main.py"),
               encoding="utf-8").read()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef)
              and n.name == "check_appointment_count")

    external = {"_fetch_east_district_reg52_html", "_fetch_huihe_reg52_html",
                "_fetch_huisheng_reg52_html", "_fetch_auh_reg52_html"}
    seen = set()
    for node in ast.walk(fn):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id not in external:
            continue
        seen.add(node.func.id)
        first = node.args[0] if node.args else None
        assert isinstance(first, ast.Constant) and first.value is None, (
            f"{node.func.id} 在正式路徑上傳了 session（第 {node.lineno} 行）—— "
            "併行 worker 會共用同一個非執行緒安全的 Session")
    assert seen == external, f"沒掃到全部四支院外 fetcher，只看到 {seen}"


def test_each_thread_gets_its_own_external_session():
    """釘住「傳 None 時每個執行緒各拿各的」—— 這是上面那條成立的前提。"""
    import threading

    # ★存物件本身，不可以存 id()★ thread-local 在執行緒結束時就被釋放，
    #   物件被回收後 id 會被下一個執行緒重用 —— 那樣測出來的「都一樣」是假的。
    got = []
    barrier = threading.Barrier(3)

    def _grab():
        s = rf._get_thread_local_reg52_external_session()
        got.append(s)
        barrier.wait(timeout=10)      # 三個都拿到之後才准結束，確保同時存活

    threads = [threading.Thread(target=_grab) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert len(got) == 3, "有執行緒沒拿到 session"
    assert len({id(s) for s in got}) == 3, \
        "不同執行緒應該各自拿到不同的 Session 物件（共用會踩連線池/cookie 競態）"
