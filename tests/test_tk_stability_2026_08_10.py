# -*- coding: utf-8 -*-
"""[穩定性總體檢 批次SA] Tk 回呼例外要看得見、after 迴圈不可以死、log 視窗要有上限。

★三個潛伏的長駐殺手★
1. pythonw 沒有 stderr：Tk 回呼（button/bind/after）裡的例外預設印到
   stderr = **完全無聲**。四支 Tk 程式沒有任何一支覆寫
   `report_callback_exception` —— 「某功能從某天起不動了」查無可查。
2. `def loop(): 做事; after(n, loop)` 的自我重排形狀：「做事」一拋例外就
   走不到重排 —— 整條迴圈**永久停擺**。`main._update_clinic_lights_loop`
   有 596 行本體、零個頂層 try、重排在最後一行；它一死，reg64 燈號／
   浮動視窗／現場人數整條管線凍結到重啟為止。
3. 會診與打卡的 log 視窗只插入、從不刪除 —— 常駐數週後 Text widget
   抱著幾十萬行。主程式自己截 500 行，同一個功能寫了三份、漂掉兩份。
"""
import ast
import io
import logging
import os
import queue
import sys
import time

import pytest

NL = chr(10)
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

from cmuh_common import tk_stability as ts  # noqa: E402


def _rec(msg="hello", args=(), level=logging.INFO):
    return logging.LogRecord("t", level, "f.py", 1, msg, args, None)


# ══ 節流器 ════════════════════════════════════════════════════════════════
class TestThrottledExceptionLog:
    @staticmethod
    def _boom():
        try:
            raise ValueError("炸")
        except ValueError:
            return sys.exc_info()

    def test_the_first_occurrence_logs_the_full_traceback(self, caplog):
        t = ts.ThrottledExceptionLog()
        with caplog.at_level(logging.ERROR):
            assert t.log("測試", *self._boom()) is True
        assert any(r.exc_info for r in caplog.records), "沒有記完整 traceback"

    def test_a_repeat_within_the_window_is_throttled(self, caplog):
        """★5 秒重排的迴圈 = 每小時 720 條 traceback → 灌爆 5MB 輪替★"""
        t = ts.ThrottledExceptionLog()
        info = self._boom()
        t.log("測試", *info)
        caplog.clear()          # ★第一條(合法的那條)不算★
        with caplog.at_level(logging.ERROR):
            assert t.log("測試", *info) is False
        assert not caplog.records, "節流沒生效"

    def test_the_suppressed_count_is_reported_later(self, caplog, monkeypatch):
        """★節流不可以變成靜音★ 之後那一條要講出「其間被吞了幾次」。"""
        t = ts.ThrottledExceptionLog(throttle_sec=60.0)
        info = self._boom()
        base = time.monotonic()
        t.log("測試", *info)
        t.log("測試", *info)
        t.log("測試", *info)
        monkeypatch.setattr(ts.time, "monotonic", lambda: base + 61.0)
        with caplog.at_level(logging.ERROR):
            assert t.log("測試", *info) is True
        assert any("2 次" in r.getMessage() for r in caplog.records), \
            [r.getMessage() for r in caplog.records]

    def test_different_sites_do_not_share_a_throttle(self, caplog):
        """★簽名是【型別+最深的 frame】★ 同一個 _boom 的兩次呼叫本來就是
        同一個簽名（對，那正是節流的意義）；不同【行】才是不同簽名。"""
        t = ts.ThrottledExceptionLog()

        def _site_a():
            try:
                raise ValueError("a")
            except ValueError:
                return sys.exc_info()

        def _site_b():
            try:
                raise ValueError("b")
            except ValueError:
                return sys.exc_info()

        t.log("測試", *_site_a())
        with caplog.at_level(logging.ERROR):
            assert t.log("測試", *_site_b()) is True, (
                "不同行的例外被同一個節流窗吃掉")

    def test_the_signature_table_is_bounded(self):
        """★節流器自己不可以變成洩漏源★"""
        t = ts.ThrottledExceptionLog()
        for i in range(ts._MAX_SIGNATURES * 2):
            try:
                exec(compile(f"raise ValueError({i})", f"fake{i}.py", "exec"))
            except ValueError:
                t.log("測試", *sys.exc_info())
        assert len(t._seen) <= ts._MAX_SIGNATURES


# ══ 回呼例外記錄器 ════════════════════════════════════════════════════════
class TestCallbackExceptionLogger:
    def test_the_handler_logs_instead_of_stderr(self, caplog):
        class _FakeRoot:
            pass

        root = _FakeRoot()
        handler = ts.install_callback_exception_logger(root, "測試程式")
        assert root.report_callback_exception is handler
        try:
            raise RuntimeError("回呼裡炸了")
        except RuntimeError:
            info = sys.exc_info()
        with caplog.at_level(logging.ERROR):
            handler(*info)
        assert any("測試程式" in r.getMessage() for r in caplog.records)

    def test_the_handler_itself_never_raises(self):
        """★最後一道網不可以自己破洞★ 連節流器壞掉都要吞。"""
        class _FakeRoot:
            pass

        handler = ts.install_callback_exception_logger(_FakeRoot(), "x")
        handler(None, None, None)          # 最畸形的輸入也不可以炸

    def test_a_broken_throttler_is_still_swallowed(self, monkeypatch):
        """★真的讓節流器炸★ handler(None,None,None) 其實走得完 ——
        要踩到 except 那條路，得讓 log() 自己拋（突變驗證抓到的）。"""
        class _FakeRoot:
            pass

        def _boom(self, *a, **k):
            raise RuntimeError("節流器炸了")

        monkeypatch.setattr(ts.ThrottledExceptionLog, "log", _boom)
        handler = ts.install_callback_exception_logger(_FakeRoot(), "x")
        try:
            raise ValueError("回呼例外")
        except ValueError:
            info = sys.exc_info()
        handler(*info)                     # ★不可以往上拋★

    def test_a_real_tk_callback_exception_reaches_the_log(self, tk_root,
                                                          caplog):
        """端到端：真的 Tk、真的 after 回呼、真的例外。"""
        ts.install_callback_exception_logger(tk_root, "端到端")

        def _boom():
            raise ValueError("after 回呼炸")

        with caplog.at_level(logging.ERROR):
            tk_root.after(0, _boom)
            tk_root.update()
        assert any("端到端" in r.getMessage() for r in caplog.records), \
            "★真的 Tk 回呼例外沒有進 log —— 又回到完全無聲★"


# ══ log 幫浦 ══════════════════════════════════════════════════════════════
class TestPumpLogRecords:
    @staticmethod
    def _text(tk_root):
        import tkinter as tk
        w = tk.Text(tk_root)
        return w

    def test_records_land_in_the_widget(self, tk_root):
        q = queue.Queue()
        q.put(_rec("哈囉"))
        w = self._text(tk_root)
        assert ts.pump_log_records(w, q) is True
        assert "哈囉" in w.get("1.0", "end")

    def test_a_malformed_record_does_not_kill_the_pump(self, tk_root):
        """★核心★ `logging.warning("%d", "x")` 是在 getMessage() 才爆的 ——
        也就是在幫浦這裡，不是在寫 log 的那一行。"""
        q = queue.Queue()
        q.put(_rec("%d", args=("不是數字",)))     # getMessage() 會 TypeError
        q.put(_rec("好的那筆"))
        w = self._text(tk_root)
        assert ts.pump_log_records(w, q) is True
        text = w.get("1.0", "end")
        assert "好的那筆" in text, "★一筆壞紀錄殺掉整批★"
        assert "格式化失敗" in text, "壞紀錄要留痕跡，不是安靜消失"

    def test_the_line_count_is_bounded(self, tk_root):
        """★常駐數週的 Text 不可以無上限成長★"""
        q = queue.Queue()
        w = self._text(tk_root)
        for batch in range(8):
            for i in range(100):
                q.put(_rec(f"line {batch}-{i}"))
            ts.pump_log_records(w, q, max_records=100, max_lines=200)
        lines = int(str(w.index("end-1c")).split(".")[0])
        assert lines <= 220, f"行數沒有被截住:{lines}"
        assert "line 7-99" in w.get("end-50l", "end"), "截錯邊(最新的被砍掉了)"

    def test_an_empty_queue_is_a_cheap_noop(self, tk_root):
        assert ts.pump_log_records(self._text(tk_root), queue.Queue()) is False

    def test_a_dead_widget_does_not_raise(self):
        """視窗關閉瞬間的 TclError 不可以往上炸(呼叫端的重排要無條件跑)。"""
        class _DeadWidget:
            def configure(self, **kw):
                raise RuntimeError("widget 已 destroy")

        q = queue.Queue()
        q.put(_rec("x"))
        ts.pump_log_records(_DeadWidget(), q)   # 不拋就對了

    def test_a_raising_custom_formatter_does_not_kill_the_pump(self, tk_root):
        """★pump 層自己的 try★ 預設 formatter 內部有 try，掩護了 pump 層 ——
        主程式傳的是【自己的】formatter（`self.format_log_record`），它拋的話
        只有 pump 層的 try 接得住（突變驗證抓到的）。"""
        q = queue.Queue()
        q.put(_rec("第一筆"))
        q.put(_rec("第二筆"))
        w = self._text(tk_root)

        def _bad_formatter(rec):
            if "第一筆" in str(rec.msg):
                raise RuntimeError("自訂 formatter 炸了")
            return str(rec.msg) + chr(10)

        assert ts.pump_log_records(w, q, formatter=_bad_formatter) is True
        text = w.get("1.0", "end")
        assert "第二筆" in text, "★自訂 formatter 一炸就殺掉整批★"

    def test_format_survives_every_shape(self):
        assert "hello" in ts.format_log_record(_rec("hello"))
        bad = _rec("%d", args=("x",))
        out = ts.format_log_record(bad)
        assert "格式化失敗" in out


# ══ 接線:四支程式 ════════════════════════════════════════════════════════
def _src(rel):
    return io.open(os.path.join(REPO_ROOT, rel), encoding="utf-8").read()


class TestWiring:
    """★單一真相來源★（外審第 1 輪抓到的）

    repo 早就有 `tk_exception.install_tk_exception_handler`，四支程式的
    生產 root 都裝它。第一版【另外裝了一套】新的 —— 而且裝得比舊的早，
    被舊的蓋掉；autoclock 還裝在測試用的暫時 root 上。
    修法：舊 API 保留（呼叫端不動），內部委派給 tk_stability 的節流實作。
    """

    @pytest.mark.parametrize("rel,label", [
        ("src/main.py", "主程式"),
        ("src/consult_query.py", "會診查詢"),
        ("src/autoclock.py", "打卡程式"),
        ("src/coord_detector.py", "點座標偵測"),
        ("src/scheduler.py", ""),
    ])
    def test_every_tk_program_installs_the_shared_handler(self, rel, label):
        """每支程式的生產 root 都要呼叫【同一個】install API。"""
        tree = ast.parse(_src(rel))
        calls = []
        for n in ast.walk(tree):
            if not isinstance(n, ast.Call):
                continue
            f = n.func
            name = getattr(f, "id", None) or getattr(f, "attr", "")
            if name == "install_tk_exception_handler":
                calls.append(ast.dump(n))
        assert calls, f"{rel} 沒有呼叫共用的 Tk 例外 handler"
        if label:
            assert any(label in c for c in calls), (
                f"{rel} 沒帶程式名標籤:{calls}")

    def test_the_new_module_is_not_installed_as_a_second_handler(self):
        """★不可以再長出第二套★ tk_stability 的 installer 只給
        tk_exception 委派用；生產程式一律走舊 API（單一接線點）。"""
        for rel in ("src/main.py", "src/consult_query.py",
                    "src/autoclock.py", "src/coord_detector.py"):
            tree = ast.parse(_src(rel))
            for n in ast.walk(tree):
                if isinstance(n, ast.Call):
                    f = n.func
                    name = getattr(f, "id", None) or getattr(f, "attr", "")
                    assert name != "install_callback_exception_logger", (
                        f"{rel} 又另外裝了一套 → 兩套互相覆蓋，"
                        "誰後裝誰贏（外審第 1 輪那個缺陷）")

    def test_the_legacy_api_delegates_to_the_throttled_impl(self, caplog):
        """★舊 API 內部必須是節流實作★ 否則合併只是名義上的。"""
        import importlib
        te = importlib.import_module("cmuh_common.tk_exception")
        te._THROTTLED = None                      # 重置單例
        te._PROGRAM_LABEL[0] = "Tk"

        class _FakeRoot:
            pass

        root = _FakeRoot()
        assert te.install_tk_exception_handler(root, program="測試程式")

        def _boom():
            try:
                raise ValueError("x")
            except ValueError:
                return sys.exc_info()

        info = _boom()
        with caplog.at_level(logging.ERROR):
            root.report_callback_exception(*info)
        assert any("測試程式" in r.getMessage() for r in caplog.records), (
            "程式名標籤沒有進 log")
        caplog.clear()
        with caplog.at_level(logging.ERROR):
            root.report_callback_exception(*info)   # 同簽名第二次
        assert not caplog.records, "★舊 API 沒有委派到節流實作★"

    def test_the_legacy_handler_never_raises(self, monkeypatch):
        """最後一道網:節流器炸掉也要吞。"""
        import importlib
        te = importlib.import_module("cmuh_common.tk_exception")
        te._THROTTLED = None

        def _boom(self, *a, **k):
            raise RuntimeError("節流器炸了")

        monkeypatch.setattr(ts.ThrottledExceptionLog, "log", _boom)
        te._report_callback_exception(ValueError, ValueError("x"), None)

    def test_a_real_tk_root_gets_the_throttled_handler(self, tk_root, caplog):
        """端到端:裝在真的 Tk root 上,最終的 handler 就是節流版。"""
        import importlib
        te = importlib.import_module("cmuh_common.tk_exception")
        te._THROTTLED = None
        te.install_tk_exception_handler(tk_root, program="端到端合併")

        def _raise():
            raise ValueError("after 回呼炸")

        with caplog.at_level(logging.ERROR):
            tk_root.after(0, _raise)
            tk_root.update()
        assert any("端到端合併" in r.getMessage() for r in caplog.records), (
            "★生產 root 最終掛的不是節流 handler★")

    def test_the_two_log_pumps_use_the_shared_bounded_impl(self):
        for rel, fn in (("src/consult_query.py", "_poll_log"),
                        ("src/autoclock.py", "poll_log_queue")):
            tree = ast.parse(_src(rel))
            target = None
            for n in ast.walk(tree):
                if isinstance(n, ast.FunctionDef) and n.name == fn:
                    target = n
                    break
            assert target is not None, (rel, fn)
            body_src = ast.get_source_segment(_src(rel), target) or ""
            assert "pump_log_records" in body_src, (
                f"{rel}:{fn} 沒有走共用幫浦（無上限成長 + 一筆壞紀錄殺全部）")
            # ★重排必須是最後一個頂層敘述（不在 try 裡 = 無條件執行）★
            last = target.body[-1]
            calls = [c for c in ast.walk(last)
                     if isinstance(c, ast.Call)
                     and isinstance(c.func, ast.Attribute)
                     and c.func.attr == "after"]
            assert calls, f"{rel}:{fn} 的最後一個敘述不是重排 —— 例外會殺掉迴圈"

    @pytest.mark.parametrize("mod_name,fn,delay", [
        ("consult_query", "_poll_log", 150),
        ("autoclock", "poll_log_queue", 100),
    ])
    def test_a_pump_crash_does_not_kill_the_poll_loop(self, mod_name, fn,
                                                      delay, monkeypatch):
        """★行為測試★ AST 看不出「except 裡偷偷 return」——
        幫浦炸掉時，重排必須照樣發生（突變驗證抓到的）。"""
        import importlib
        mod = importlib.import_module(mod_name)

        def _boom(*a, **k):
            raise RuntimeError("幫浦炸了")

        monkeypatch.setattr(ts, "pump_log_records", _boom)
        scheduled = []

        class _Host:
            log_text = object()

            def after(self, d, cb=None):
                scheduled.append(d)
                return "id"

        cls = None
        for obj in vars(mod).values():
            if isinstance(obj, type) and fn in vars(obj):
                cls = obj
                break
        assert cls is not None, f"找不到 {mod_name}.{fn} 的類別"
        # ★要當方法綁上去★ 重排引用 self.<fn>,host 沒這個屬性會 AttributeError
        setattr(_Host, fn, vars(cls)[fn])
        host = _Host()
        getattr(host, fn)()
        assert scheduled == [delay], (
            f"★{mod_name}.{fn} 幫浦炸掉後沒有重排 → log 視窗永久凍結★"
            f":{scheduled}")

    def test_main_log_section_also_uses_the_shared_pump(self):
        text = _src("src/main.py")
        assert text.count("_pump_log_records(") >= 1, "主程式沒有接上共用幫浦"


# ══ 接線:診間燈號迴圈的駕駛座 ════════════════════════════════════════════
class TestClinicLoopDriver:
    @staticmethod
    def _fns():
        text = _src("src/main.py")
        tree = ast.parse(text)
        out = {}
        for n in ast.walk(tree):
            if isinstance(n, ast.FunctionDef) and n.name in (
                    "_update_clinic_lights_loop",
                    "_update_clinic_lights_loop_body"):
                out[n.name] = n
        return text, out

    def test_the_driver_guards_the_body(self):
        """★核心★ 596 行本體、重排在最後一行 → 例外 = 整條 reg64 管線
        永久凍結。駕駛座必須 try 住本體、例外時仍然重排。"""
        _text, fns = self._fns()
        assert "_update_clinic_lights_loop_body" in fns, "本體不見了"
        driver = fns["_update_clinic_lights_loop"]
        tries = [n for n in driver.body if isinstance(n, ast.Try)]
        assert tries, "★駕駛座沒有 try —— 迴圈死了就永遠死了★"
        handler_src = ast.dump(tries[0])
        assert "'after'" in handler_src, "except 裡沒有重排 —— 例外一次就停擺"

    def test_the_body_is_still_the_real_loop(self):
        _text, fns = self._fns()
        body = fns["_update_clinic_lights_loop_body"]
        span = (body.end_lineno or 0) - body.lineno
        assert span > 400, f"本體只剩 {span} 行 —— 是不是抽錯了"

    def test_the_driver_reschedules_with_a_fixed_fallback(self):
        """例外路徑用固定 5 秒重排（別依賴壞掉的狀態去算延遲）。"""
        text, fns = self._fns()
        driver_src = ast.get_source_segment(text, fns["_update_clinic_lights_loop"])
        assert "5_000" in (driver_src or ""), "例外路徑沒有固定的重排延遲"

    def test_the_exception_path_is_throttled(self):
        """5 秒重排 + 每次完整 traceback = 每小時 720 條 → 要節流。"""
        text, fns = self._fns()
        driver_src = ast.get_source_segment(text, fns["_update_clinic_lights_loop"]) or ""
        assert "_CLINIC_LOOP_THROTTLE" in driver_src

    def test_the_driver_actually_catches_and_reschedules(self, monkeypatch):
        """行為測試：本體炸掉 → 駕駛座活著、有重排、沒有把例外往上丟。"""
        import importlib
        m = importlib.import_module("main")

        scheduled = []

        class _FakeRoot:
            def after(self, delay, fn=None):
                scheduled.append(delay)
                return "id"

        class _Host:
            _shutting_down = False
            root = _FakeRoot()

            def _update_clinic_lights_loop_body(self):
                raise RuntimeError("本體炸了")

        # ★駕駛座要當【方法】綁上去★ 它的重排引用
        #   self._update_clinic_lights_loop —— host 沒這個屬性的話,
        #   AttributeError 會被內層 except 吞掉 → 測到的是「重排參數
        #   解析失敗」,不是被測的行為。
        _Host._update_clinic_lights_loop = m.AutomationApp.__dict__[
            "_update_clinic_lights_loop"]
        host = _Host()
        host._update_clinic_lights_loop()  # ★不可以往上拋★
        assert scheduled == [5_000], f"例外後沒有(或錯誤地)重排:{scheduled}"

    def test_shutdown_suppresses_the_reschedule(self):
        import importlib
        m = importlib.import_module("main")
        scheduled = []

        class _FakeRoot:
            def after(self, delay, fn=None):
                scheduled.append(delay)
                return "id"

        class _Host:
            _shutting_down = True
            root = _FakeRoot()

            def _update_clinic_lights_loop_body(self):
                raise RuntimeError("關閉中炸了")

        m.AutomationApp._update_clinic_lights_loop(_Host())
        assert scheduled == [], "關閉中還在重排"


# ══ 通知視窗的關閉鈕（外部第二意見 #5）═══════════════════════════════════
class TestNoticeCloseButton:
    """★`command=notice.destroy` 繞過 cleanup★

    死掉的 Toplevel 永遠留在 `_active_notices`，而新通知的位置是
    `len(_active_notices) * 110` —— 幾次之後全部疊到螢幕外面，
    「熱鍵忙碌中」「更新失敗」這類提示變成永遠看不到。
    """

    def test_the_close_button_goes_through_cleanup(self):
        """關閉鈕的 command 必須是 cleanup，不是裸 destroy。"""
        text = _src("src/main.py")
        i = text.index("def _show_notice")
        j = text.index("def _cancel_pending_refresh_tick_ui", i)
        seg = text[i:j]
        # ★負向斷言要剝掉註解★ 說明「為什麼不可以」的註解裡就有那個字面。
        code_only = NL.join(ln.split("#")[0] for ln in seg.splitlines())
        assert 'command=cleanup' in code_only, "關閉鈕沒有走 cleanup"
        assert 'command=notice.destroy' not in code_only, (
            "★關閉鈕繞過 cleanup → 死視窗永留清單、新通知被推到螢幕外★")

    def test_cleanup_is_defined_before_the_button_uses_it(self):
        """cleanup 必須在按鈕之前定義（closure 晚綁也能跑，但先定義才讀得懂）。"""
        text = _src("src/main.py")
        i = text.index("def _show_notice")
        j = text.index("def _cancel_pending_refresh_tick_ui", i)
        seg = text[i:j]
        assert seg.index("def cleanup") < seg.index('text="關閉"')
