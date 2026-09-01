# -*- coding: utf-8 -*-
"""[外審 P1] `check_appointment_count` 也不可以把本機的錯記在遠端頭上。

批次 V 第一版只改了 `reg52_fetch` 的四個分院抓取，**漏了 `main.py`**：
休診表兩處、主院判定一處，都還是「`not outcome.ok` 就記退避」。
於是 `LOCAL_ERROR` 這個新狀態在那三處等同沒有 —— 契約說「不算在遠端頭上」，
實作照樣算。★宣稱與實作不符，而且是我自己的修正造出來的★。

還有更前面的一刀：`soup_main = BeautifulSoup(html_main, 'lxml')` 原本是
**無條件、而且排在分類之前**。lxml 裝壞時它先丟例外，`_classify_main_html`
的 LOCAL_ERROR 判定與「這是本機問題」的 log 一個都跑不到，例外往上被當成
一般抓取失敗 —— 那條路是會記遠端退避的。

★這支測試真的執行 `check_appointment_count`★
外審點名既有測試只驗 `reg52_fetch` 與用 AST 看回傳值，從來沒有跑過這條主路徑。
"""
import importlib
import logging

import pytest

main = importlib.import_module("main")
contract = importlib.import_module("cmuh_common.reg52_contract")

# ★要真的通過 `_has_reg52_skeleton`★（`table.schedule`，與解析器同一個判準）——
#   隨手寫一段「看起來像」的 HTML 會被判成維護頁，於是每個測試都走進壞頁那條路，
#   而斷言仍然可能剛好成立。測試要餵得進生產的判準才算數。
_MAIN_HTML = ("<html><body><table class='schedule'>"
              "<tr><td class='timeSlot'>上午</td><td class='schBox'>1</td></tr>"
              "</table></body></html>" + "。" * 600)


class _Resp:
    def __init__(self, text):
        self.text = text
        self.encoding = "big5"
        self.status_code = 200

    def raise_for_status(self):
        pass


class _Sess:
    def get(self, url, **kw):
        return _Resp(_MAIN_HTML)

    def post(self, url, **kw):
        return _Resp(_MAIN_HTML)


@pytest.fixture
def harness(monkeypatch):
    """把外部世界全部換掉，只留下「失敗歸因」這件事可觀測。"""
    seen = {"backoff": [], "ok_backoff": [], "circuit": [], "cache_set": []}
    # ★`requests` 在測試環境是 None★（`_ensure_network_imports` 沒跑過），
    #   而 `check_appointment_count` 的 except 子句會取 `requests.exceptions`
    #   —— 不補上去的話每個測試都死在 AttributeError，量到的不是被測的東西。
    monkeypatch.setattr(main, "requests", importlib.import_module("requests"))
    # 同理:`main.BeautifulSoup` 在測試環境是 None（生產是 `_ensure_network_imports`
    # 補上的）。基準狀態要是「解析器正常」，壞掉才由 `_break_bs4` 明確造成 ——
    # 否則每個測試都在「解析器本來就壞」的狀態下跑，反方向那幾條就沒有意義。
    import bs4
    monkeypatch.setattr(main, "BeautifulSoup", bs4.BeautifulSoup)
    monkeypatch.setattr(main, "_get_thread_local_reg52_session", _Sess)
    monkeypatch.setattr(main, "_cache_get", lambda *a, **k: None)
    monkeypatch.setattr(main, "_cache_set",
                        lambda *a, **k: seen["cache_set"].append(a[:1]))
    monkeypatch.setattr(main, "_parse_cache_get", lambda *a, **k: None)
    # ★「健康」的基準要真的走到發布點★
    #   `_MAIN_HTML` 是給契約層判形狀用的最小骨架，解析出來是空的 —— 空的會走進
    #   「無任何可用門診資料」的重試/失敗路徑，根本到不了 `is_live_final` 那一行。
    #   我第一版就是這樣：`test_a_healthy_run_still_succeeds` 只驗到 `ok_backoff`
    #   （發生在更前面），看起來綠，其實從沒跑到被測的那段。
    monkeypatch.setattr(main, "_parse_main_hospital_schedule",
                        lambda soup: {"2026-08-09": [{"time": "上午",
                                                      "count": 3}]})
    monkeypatch.setattr(main, "_parse_cache_set", lambda *a, **k: None)
    monkeypatch.setattr(main, "_source_backoff_allow", lambda k: (True, 0.0))
    monkeypatch.setattr(main, "_source_throttle_allow",
                        lambda k, i=0: (True, 0.0))
    monkeypatch.setattr(main, "_source_backoff_fail",
                        lambda k, *a: seen["backoff"].append(k) or (1.0, 1))
    monkeypatch.setattr(main, "_source_backoff_success",
                        lambda k: seen["ok_backoff"].append(k))
    seen["ui"] = []
    # ★★測試絕對不可以碰網路★★（2026-08-09：CI 紅、本機綠的第一嫌疑）
    #   `check_appointment_count` 會依這三個述詞決定要不要抓東區／惠和／惠盛，
    #   亞大則看醫師在不在 `AUH_DOCTOR_DOCNO_MAP` 裡。抓取走的是【另一條】
    #   thread-local session（`_get_thread_local_reg52_external_session`），
    #   不是上面換掉的那條 —— 也就是說，只換主院那條 session 的話，這支測試
    #   在 CI 上會真的對 61.66.117.10 與 appointment.cmuh.org.tw 發請求：
    #   逾時長短、DNS、代理都會變成測試結果的一部分。
    #   這裡把四個入口一律關掉：本批要測的是「失敗歸因」，不是分院抓取。
    for pred in ("_should_fetch_east_district_reg52", "_should_fetch_huihe_reg52",
                 "_should_fetch_huisheng_reg52", "_should_fetch_tcmc_reg52"):
        monkeypatch.setattr(main, pred, lambda *a, **k: False)
    monkeypatch.setattr(main, "AUH_DOCTOR_DOCNO_MAP", {})
    monkeypatch.setattr(main, "put_ui_message",
                        lambda q, m, *a, **k: seen["ui"].append(m))
    monkeypatch.setattr(main, "_reg52_stale_fallback", lambda *a, **k: "")
    for attr in ("_circuit_is_tripped",):
        if hasattr(main, attr):
            monkeypatch.setattr(main, attr, lambda *a, **k: False)
    for attr in ("_circuit_record_fail", "_circuit_record_success"):
        if hasattr(main, attr):
            monkeypatch.setattr(main, attr,
                                lambda s, _b=seen: _b["circuit"].append(s)
                                or False)
    return seen


def _break_bs4(monkeypatch):
    """機器上的 bs4/lxml 壞掉。

    ★用 seam 注入，不去改 import 機制★
    `classify_*_html` 是在函式裡 `from bs4 import ...`，用 `builtins.__import__`
    去攔會受 import cache 與呼叫順序影響 —— 我第一版就是這樣寫的，結果契約層
    照樣 import 成功、回的是 `missing_schedule_table`（維護頁），測試量到的是
    「壞頁」而不是「本機解析器壞掉」。改成直接讓兩個判定點回 LOCAL_ERROR，
    並讓 `main.BeautifulSoup` 丟例外（那是生產真正會發生的形狀：
    `_ensure_network_imports` 失敗時它就是 None／不可呼叫）。
    """
    local = contract.FetchOutcome(contract.LOCAL_ERROR,
                                  reason="parser_unavailable", length=999)
    monkeypatch.setattr(main, "_classify_dayoff_html", lambda t: local)
    monkeypatch.setattr(main, "_classify_main_html", lambda t: local)

    def _boom(*a, **k):
        raise ImportError("lxml/bs4 裝壞了")

    monkeypatch.setattr(main, "BeautifulSoup", _boom)


def _run(cfg=None):
    import queue
    cfg = cfg or {"name": "甲醫師", "doc_no": "0001"}
    main.check_appointment_count(queue.Queue(), cfg)


def test_a_broken_parser_does_not_record_remote_backoff(harness, monkeypatch,
                                                        caplog):
    """★核心★ 本機 bs4/lxml 壞掉 → 一次遠端退避都不可以記。"""
    _break_bs4(monkeypatch)
    with caplog.at_level(logging.ERROR):
        _run()
    assert harness["backoff"] == [], (
        f"★本機解析器壞掉，卻把遠端記進退避★：{harness['backoff']}")


def test_a_broken_parser_says_it_is_a_local_problem(harness, monkeypatch,
                                                    caplog):
    """★不記退避 ≠ 安靜跳過★ 沒有 log 的話，沒人知道機器壞了。"""
    _break_bs4(monkeypatch)
    with caplog.at_level(logging.ERROR):
        _run()
    assert any("本機" in r.getMessage() for r in caplog.records), (
        "解析器壞掉卻沒有任何一句話說是本機的問題 —— 查的人會往遠端查")


def test_a_broken_parser_does_not_cache_the_unverified_page(harness,
                                                            monkeypatch):
    """沒能判定就不可以寫快取（否則下一輪拿同一份沒驗過的東西重解析）。"""
    _break_bs4(monkeypatch)
    _run()
    assert harness["cache_set"] == [], (
        f"★沒能判定卻把頁面寫進快取★：{harness['cache_set']}")


def test_a_broken_parser_is_not_counted_as_a_parse_cache_hit(harness,
                                                             monkeypatch):
    """★遙測不可以說謊★ 沒命中快取、也沒解析，不是 cache hit。"""
    import ast
    import inspect
    import textwrap
    src = textwrap.dedent(inspect.getsource(main.check_appointment_count))
    tree = ast.parse(src)
    # 找 `elif soup_main is not None:` 之後那個 else —— 它不該再碰 cache_hit_parse
    hits = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        for sub in node.orelse:
            for n in ast.walk(sub):
                if (isinstance(n, ast.Subscript)
                        and isinstance(n.slice, ast.Constant)
                        and n.slice.value == "cache_hit_parse"):
                    hits.append(getattr(n.ctx, "__class__", None).__name__)
    # 只允許在「真的命中」那一支出現一次（AugAssign 的 Store/Load 各算一次）
    assert len(hits) <= 2, (
        f"cache_hit_parse 在超過一個分支被動到：{hits}")


def test_a_healthy_run_still_succeeds(harness, monkeypatch):
    """★反方向★ 解析器正常時，這條路要照舊跑完並記 success。"""
    _run()
    assert harness["ok_backoff"], "正常情況下沒有記任何一次成功 —— 測試沒跑到路上"
    assert harness["backoff"] == [], f"正常頁卻記了退避：{harness['backoff']}"


def test_a_maintenance_page_still_records_backoff(harness, monkeypatch):
    """★反方向★ 真的是壞頁就要記 —— 不記的話每一輪都再打一次同一個壞頁。"""
    bad = "<html><body>系統維護中</body></html>" + "。" * 600

    class _BadSess:
        def get(self, url, **kw):
            return _Resp(bad)

        def post(self, url, **kw):
            return _Resp(bad)

    monkeypatch.setattr(main, "_get_thread_local_reg52_session", _BadSess)
    _run()
    assert harness["backoff"], "維護頁沒有被記進退避（P1-03 修過的洞復活）"


def test_a_cached_dayoff_page_does_not_explode_when_the_parser_breaks(
        harness, monkeypatch, caplog):
    """★這條才打得到那一行★（第一版的突變驗證漏了它）

    休診表【走快取進來】的輪次不會再分類一次 —— 它當初就是通過判定才存進去的。
    所以 LOCAL_ERROR 那條路擋不到 `_parse_doctor_info_dayoff(BeautifulSoup(...))`：
    lxml 壞掉時 `BeautifulSoup` 是 None，TypeError 往上竄，被外層當成一般抓取
    失敗處理（而那條路會記遠端退避）。

    我第一版的突變驗證把這行的 `try` 拿掉仍然全綠 —— 因為那幾條測試裡
    `html_dayoff` 都是空字串（stale fallback 回 ""），那一行連跑都沒跑到。
    ★沒跑到的程式碼，突變當然不會轉紅★。
    """
    def _cache(key, *a, **k):
        # 主表沒快取（要新抓）、休診表有快取（於是不會再被分類）
        return _MAIN_HTML if key and key[0] == "dayoff_html" else None

    monkeypatch.setattr(main, "_cache_get", _cache)
    _break_bs4(monkeypatch)
    with caplog.at_level(logging.ERROR):
        _run()                      # 不可以往上拋
    assert harness["backoff"] == [], (
        f"★快取的休診表 + 壞掉的解析器 → 記了遠端退避★：{harness['backoff']}")
    assert any("休診表解析器不可用" in r.getMessage() for r in caplog.records), (
        "解析器壞掉卻沒說出來 —— 休診覆蓋靜默消失＝停診被顯示成正常門診")


def _clinic_msgs(seen):
    return [m for m in seen["ui"]
            if type(m).__name__ == "UiClinicDataMessage"]


def _break_dayoff_parse_only(monkeypatch):
    """★只讓休診表解析失敗，主表照常★

    `_break_bs4` 把整個 `BeautifulSoup` 弄壞，於是主表也解析不出東西 →
    「無任何可用門診資料」→ 三次重試後失敗，**根本走不到發布點**。
    我第一版就是這樣寫的：兩條測試看起來綠，其實 `finals == []` 是因為一則
    UiClinicDataMessage 都沒發出來，跟 `is_live_final` 的判準無關 ——
    突變（`live_final = True`）也就抓不到。

    這裡改成只讓 `_parse_doctor_info_dayoff` 炸掉（畸形休診表就是這個形狀），
    主表資料仍然完整 → 真的走到發布點，勝負只由「有沒有休診覆蓋」決定。
    """
    def _boom(*a, **k):
        raise ValueError("休診表解析失敗")

    monkeypatch.setattr(main, "_parse_doctor_info_dayoff", _boom)


def _dayoff_from_cache(monkeypatch):
    """休診表走快取進來 → 這一輪不會再分類，LOCAL_ERROR 那條路擋不到它。"""
    def _cache(key, *a, **k):
        return _MAIN_HTML if key and key[0] == "dayoff_html" else None

    monkeypatch.setattr(main, "_cache_get", _cache)


def test_a_broken_dayoff_parser_never_publishes_a_live_final_result(
        harness, monkeypatch):
    """★核心（第 2 回）★ 解析不了休診表 ≠ 這位醫師沒有休診。

    上一版只 log 一句就往下走，`dayoff_data` 留在 `{}` —— 於是結果照樣以
    `is_live_final=True` 發布：★停診的診次被顯示成正常門診★，而且取得止掛
    寄信資格。**把漏報換成了假平安，比原本的誤罰遠端更糟。**
    """
    _dayoff_from_cache(monkeypatch)
    _break_dayoff_parse_only(monkeypatch)
    _run()
    msgs = _clinic_msgs(harness)
    assert msgs, "沒有走到發布點 —— 這條測試量不到任何東西"
    finals = [m for m in msgs if m.is_live_final]
    assert finals == [], (
        "★休診覆蓋沒套上去，卻宣稱是完整的即時資料★ —— 停診會被顯示成正常"
        "門診，而且會拿去寄止掛提醒")


def test_a_broken_dayoff_parser_never_publishes_the_schedule_at_all(
        harness, monkeypatch):
    """★核心（第 3 回）★ 只擋寄信資格還不夠 —— 畫面上也不可以出現半套班表。

    上一版沒有完整快取時仍然往下走，把【沒有休診覆蓋】的班表發到畫面上：
    止掛提醒是不寄了，但★停診的診次照樣被顯示成正常門診★，醫師看到的是錯的。
    這一輪的班表不完整而且無法補全，所以只能送錯誤結果。
    """
    _dayoff_from_cache(monkeypatch)
    _break_dayoff_parse_only(monkeypatch)
    _run()                                   # 沒有 _cached_appointments
    msgs = _clinic_msgs(harness)
    assert msgs, "什麼都沒發布（畫面會停在更舊的東西上而且不知道為什麼）"
    payloads = [m.data for m in msgs]
    assert all(isinstance(d, dict) and "error" in d for d in payloads), (
        f"★發布了沒有休診覆蓋的班表★ —— 停診會被顯示成正常門診：{payloads}")


def test_the_error_payload_is_not_counted_as_appointments():
    """錯誤結果不可以被當成「有資料」（否則它會變成下一輪的完整快取）。"""
    from cmuh_common.appt_utils import appointment_data_count
    assert appointment_data_count(
        {"error": "休診表解析失敗（本機），暫無完整班表"}) == 0


def test_a_broken_dayoff_parser_prefers_the_last_complete_snapshot(
        harness, monkeypatch, caplog):
    """有上一份【併過休診覆蓋】的完整快取，就用它，不要拿半套的覆蓋掉畫面。"""
    # ★`appointment_data_count` 只數 list★ —— 值寫成 dict 的話它回 0，
    #   `_emit_cached_appointments` 直接回 False，這條測試就測不到快取那條路。
    snapshot = {"2026-08-09": [{"time": "上午", "count": 7, "kept": True}]}
    _dayoff_from_cache(monkeypatch)
    _break_dayoff_parse_only(monkeypatch)
    with caplog.at_level(logging.WARNING):
        _run({"name": "甲醫師", "doc_no": "0001",
              "_cached_appointments": snapshot})
    msgs = _clinic_msgs(harness)
    assert msgs, "什麼都沒發布"
    assert msgs[-1].data == snapshot, (
        f"沒有用上一份完整快取：{msgs[-1].data}")
    assert not msgs[-1].is_live_final
    # ★斷言原因★ 不然「三次重試都失敗後才退回快取」也會讓上面那兩條成立 ——
    #   那是完全不同的一條路，量到的不是這個修正。
    assert any("休診表解析器不可用" in r.getMessage() for r in caplog.records), (
        "退回快取的原因不是休診表解析失敗 —— 測試走到別條路上了")


def test_a_healthy_run_is_live_final(harness, monkeypatch):
    """★反方向★ 一切正常時必須是 live_final，否則止掛提醒永遠不會寄。"""
    _run()
    finals = [m for m in _clinic_msgs(harness) if m.is_live_final]
    assert finals, (
        "★把 fail-open 修成 fail-closed：止掛提醒永遠拿不到寄信資格★")


def test_every_local_fault_path_marks_the_source_degraded():
    """★逐一盤點★ 五個本機失效點（休診 3、主院 2）都要標降級。

    少標一個 = 那條路仍然會發出「完整的即時資料」宣稱。
    """
    import ast
    import inspect
    import textwrap
    src = textwrap.dedent(inspect.getsource(main.check_appointment_count))
    marks = 0
    for n in ast.walk(ast.parse(src)):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "add"
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "degraded_sources"):
            marks += 1
    assert marks >= 6, (
        f"標記降級的地方只有 {marks} 處（既有 1 處 + 本批 5 處）")


def test_every_classifier_verdict_site_in_main_checks_blames_remote():
    """★逐一盤點，不是「有一個對就算過」★

    `main.py` 有三處拿 `classify_*_html` 的結果做判定（休診表兩處、主院一處）。
    只改一處就等於另外兩處仍然會誤罰遠端 —— 而那正是外審抓到的事。
    用 AST 找每一個 `_classify_dayoff_html` / `_classify_main_html` 呼叫，
    要求同一個函式裡也出現 `blames_remote`。
    """
    import ast
    import inspect
    import textwrap
    src = textwrap.dedent(inspect.getsource(main.check_appointment_count))
    tree = ast.parse(src)
    verdicts = [n for n in ast.walk(tree)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id in ("_classify_dayoff_html",
                                  "_classify_main_html")]
    assert len(verdicts) == 3, (
        f"判定點的數量變了（原本 3 處），請重新盤點：{len(verdicts)}")
    blames = [n for n in ast.walk(tree)
              if isinstance(n, ast.Attribute) and n.attr == "blames_remote"]
    assert len(blames) >= 3, (
        f"只有 {len(blames)} 處檢查 blames_remote，但有 3 個判定點")


def test_the_main_soup_is_built_defensively():
    """`BeautifulSoup(html_main, 'lxml')` 必須被 try 包住。

    不包的話，lxml 壞掉時它會在分類【之前】丟例外，LOCAL_ERROR 那條路
    連走都走不到 —— 修了契約層卻繞過它，等於沒修。
    """
    import ast
    import inspect
    import textwrap
    src = textwrap.dedent(inspect.getsource(main.check_appointment_count))
    protected = False
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, ast.Try):
            continue
        for n in ast.walk(node):
            if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == "BeautifulSoup"
                    and any(isinstance(a, ast.Name) and a.id == "html_main"
                            for a in n.args)):
                protected = True
    assert protected, "主院的 BeautifulSoup 沒有被 try 包住"


def test_the_contract_really_can_return_local_error(monkeypatch):
    """★上面的 seam 注入必須對應一個真的會發生的狀態★

    否則就是自己編一個生產不存在的輸入，然後證明程式處理得了它。
    這裡讓契約層真的 import 不到 bs4，確認它確實回 LOCAL_ERROR。
    """
    import builtins
    real = builtins.__import__

    def _imp(name, *a, **k):
        if name == "bs4":
            raise ImportError("lxml/bs4 裝壞了")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", _imp)
    out = contract.classify_main_html(_MAIN_HTML)
    assert out.status == contract.LOCAL_ERROR, (
        f"契約層在 bs4 壞掉時沒有回 LOCAL_ERROR：{out.status}/{out.reason}")
    assert out.blames_remote is False


def test_the_harness_leaves_no_way_out_to_the_network(harness, monkeypatch):
    """★守衛自己也要被守★

    這支測試檔真的執行 `check_appointment_count` —— 那條路上有四個對外抓取的
    入口。哪天有人加第五個、或改了述詞的名字，上面的 fixture 就會安靜地
    失效，測試變成「有時候會連外網」：在本機通常失敗得很快看不出來，在 CI 上
    卻可能變成逾時、間歇紅燈，或更糟 —— 對醫院主機發出真實請求。
    """
    import ast
    import inspect
    import textwrap
    src = textwrap.dedent(inspect.getsource(main.check_appointment_count))
    gates = set()
    for n in ast.walk(ast.parse(src)):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                and n.func.id.startswith("_should_fetch_")):
            gates.add(n.func.id)
    assert gates == {"_should_fetch_east_district_reg52",
                     "_should_fetch_huihe_reg52",
                     "_should_fetch_huisheng_reg52",
                     # [2026-09-01] 老人醫院(張廖年峰醫師的門診移過去了)
                     "_should_fetch_tcmc_reg52"}, (
        f"對外抓取的入口變了，fixture 要同步關掉：{sorted(gates)}")
    # 亞大沒有述詞，是看醫師在不在名單裡 —— fixture 把名單清空
    assert main.AUH_DOCTOR_DOCNO_MAP == {}, "亞大那條路沒有被關掉"
    for g in gates:
        assert getattr(main, g)("x", "y") is False, f"{g} 沒有被 fixture 關掉"


def test_stubbing_main_does_not_reach_the_real_backoff_state(harness):
    """★為什麼「關掉入口」比「換掉 main 的 stub」重要★

    分院／亞大的抓取函式住在 `cmuh_common.reg52_fetch`，它用的是**自己 import
    的** `_source_backoff_fail` / `_circuit_record_fail` —— 不是上面 fixture 換
    掉的 `main.*`。所以只要那些抓取真的跑起來，就會：
      ① 對 61.66.117.10 與 appointment.cmuh.org.tw 發出真實請求；
      ② 把真實的 `fetch_resilience._source_backoff_state` 寫進 `east:0001` 之類
         的鍵 —— **污染同一輪其他測試看到的全域狀態**。
    Windows 上連線瞬間失敗、Linux runner 上時序完全不同，於是「本機綠、CI 紅」。

    這條測試釘住那個結構事實：換掉 `main.X` 動不到 `reg52_fetch` 裡的同名東西。
    """
    import cmuh_common.fetch_resilience as fr
    import cmuh_common.reg52_fetch as rf
    assert rf._source_backoff_fail is fr._source_backoff_fail, (
        "reg52_fetch 改成別的來源了 —— 上面的說明要重寫")
    assert main._source_backoff_fail is not rf._source_backoff_fail, (
        "fixture 換掉 main 的 stub 竟然也換到了 reg52_fetch —— "
        "那表示這條測試量錯了東西")
    before = dict(fr._source_backoff_state)
    _run()
    assert dict(fr._source_backoff_state) == before, (
        "★這一輪測試動到了真實的退避狀態★ —— 代表有對外抓取真的跑起來了，"
        "它會污染同一輪的其他測試，而且會對醫院主機發出真實請求")
