# -*- coding: utf-8 -*-
"""[2026-07-26 審查] P2 批次:讀檔暫時失敗被當成沒資料、空排程覆蓋、疊字、假成功、缺稽核。"""
import inspect
import os
import re
import sys


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import master_schedule_cache as msc  # noqa: E402
from cmuh_common import watchdog_core as wc  # noqa: E402


def _code_only(src: str) -> str:
    src = re.sub(r'("""|\'\'\')(?:.|\n)*?\1', "", src)
    return "\n".join(line.split("#")[0] for line in src.splitlines())


# ── watchdog 設定檔暫時讀不到 → 不可拿預設值跑一輪 ──────────────────────────────
def test_watchdog_skips_tick_when_config_temporarily_unreadable(monkeypatch, tmp_path):
    """★與打卡/排班同一病灶★ 防毒鎖檔時 safe_load_json 回 default,watchdog 會拿
    【預設設定】跑一輪:使用者關掉的程式被當成該啟動、per-machine 選項全被忽略。"""
    monkeypatch.setattr(wc, "CONFIG_PATH", tmp_path / "watchdog_config.json")
    (tmp_path / "watchdog_config.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(wc, "safe_load_json_ex", lambda *a, **k: (None, "error"))
    cfg = wc.load_config()
    assert wc.config_load_failed() is True
    assert isinstance(cfg, dict)
    msgs = wc.run_one_tick(mode="outer")
    assert any("設定檔暫時讀不到" in m for m in msgs), "必須跳過本輪"


def test_watchdog_normal_load_does_not_set_failed_flag(monkeypatch, tmp_path):
    p = tmp_path / "watchdog_config.json"
    p.write_text('{"master_enabled": false}', encoding="utf-8")
    monkeypatch.setattr(wc, "CONFIG_PATH", p)
    wc.load_config()
    assert wc.config_load_failed() is False


def test_watchdog_never_writes_defaults_back_on_transient_error():
    """暫時讀不到時【絕不】把預設值寫回檔案(那會永久蓋掉使用者設定)。"""
    code = _code_only(inspect.getsource(wc.load_config))
    i_err = code.index('status == "error"')
    i_write = code.index("atomic_write_json", i_err)
    seg = code[i_err:i_write]
    assert "return" in seg, "error 分支必須在任何寫回之前 return"


# ── 主排程:抓到空的不可覆蓋既有快取 ──────────────────────────────────────────
def test_empty_master_schedule_never_overwrites_cache(monkeypatch, tmp_path):
    """★資料損失★ 抓取端在「網頁抓到了、但一個醫師都解析不出來」時不會拋例外,只回 {}。
    舊版把 {} 當成合法新排程送進 UI queue → 整份主排程被覆蓋成空的,而且靜默。"""
    cache = tmp_path / "cache_master_schedule.json"
    cache.write_text('{"王醫師": {"0": [{"session": "上午"}]}}', encoding="utf-8")
    sent = []
    monkeypatch.setattr(msc, "put_ui_message", lambda q, m: sent.append(m))
    status = msc.refresh_master_schedule_if_needed(
        None, lambda: {}, str(cache), force=True)
    assert status == "fetch_failed"
    assert not sent, "不可把空排程送出去覆蓋快取"


def test_nonempty_master_schedule_still_applied(monkeypatch, tmp_path):
    cache = tmp_path / "cache_master_schedule.json"
    sent = []
    monkeypatch.setattr(msc, "put_ui_message", lambda q, m: sent.append(m))
    status = msc.refresh_master_schedule_if_needed(
        None, lambda: {"王醫師": {0: [{"session": "上午"}]}}, str(cache), force=True)
    assert status in ("fetched", "updated")
    assert sent, "正常排程必須照常套用"


# ── 縮寫:backspace 沒送成功就不可貼上 ────────────────────────────────────────
def test_no_paste_when_backspace_failed():
    """★疊字寫進病歷★ 縮寫還留在欄位裡又貼上展開內容 → 'nev nevus, benign appearing'。"""
    from cmuh_common import abbrev_engine as ae
    code = _code_only(inspect.getsource(ae.AbbrevEngine._do_replace))
    i_bs = code.index("bs_ok = _send_atomic_keystrokes(bs_events)")
    seg = code[i_bs:i_bs + 700]
    assert "if not bs_ok:" in seg, "必須先判斷 backspace 是否成功"
    i_guard = seg.index("if not bs_ok:")
    i_paste = seg.index("_send_atomic_keystrokes(paste_events)")
    assert i_guard < i_paste, "判斷要在貼上之前"
    assert "else:" in seg[i_guard:i_paste], "失敗時必須跳過貼上,不可只記旗標"


# ── F11 按鈕點擊:送不出去不可回報成功 ───────────────────────────────────────
def test_click_helper_returns_zero_when_post_failed():
    import main
    # [2026-07-31 P2-06 第二刀] 這個函式已搬到 cmuh_common/his_window.py，
    # 呼叫的也從 `_post_click_to_control` 變成模組內的 `post_click_to_control`。
    # 守的性質沒變：PostMessage 的回傳值不可丟掉。
    code = _code_only(inspect.getsource(main._click_button_normalized_text))
    assert "if not post_click_to_control(out[0]):" in code, \
        "PostMessage 的回傳值不可丟掉"
    i = code.index("if not post_click_to_control(out[0]):")
    assert "return 0" in code[i:i + 300], "送不出去要回 0,讓呼叫端走失敗分支"


# ── F8:對 HIS 欄位注入文字必須留稽核 ────────────────────────────────────────
def test_f8_records_audit_ledger_on_both_outcomes():
    import main
    code = _code_only(inspect.getsource(main.script_F8_quick_text))
    assert code.count("_record_his_action(") == 2, "成功與失敗都要記"
    assert "_LEDGER_OK" in code and "_LEDGER_FAILED" in code
    # [2026-07-31 P2-03] 舊版找 `"len="`(自由文字裡的字面量)。現在是型別 —— 只記長度
    # 這件事由 _EvObserved 保證(它的 payload 只有 len),不再靠呼叫端寫對字串。
    assert "_EvObserved(len(text))" in code, "只記長度,不可把輸入內容寫進帳本"
    assert "text}" not in code


def test_f8_never_writes_quick_text_into_logs_or_ledger():
    """★PII★ F8 的預設值就是身分證字號格式,使用者也可能設成病歷號。
    automation_ui.log 是 RotatingFileHandler 持久保存且會輪替備份的 —— 原文一旦寫進去
    就留在磁碟上。帳本與 log 都只能記長度。"""
    import main
    code = _code_only(inspect.getsource(main.script_F8_quick_text))
    for line in code.splitlines():
        if "logging." in line or "_record_his_action" in line:
            assert "%r\", text" not in line and ", text)" not in line,                 f"這行把 quick text 原文寫出去了:{line.strip()}"
    assert "len(text)" in code


# ── 讀取失敗 vs 空欄位必須可區分 ─────────────────────────────────────────────
def test_gettext_ex_distinguishes_failure_from_empty(monkeypatch):
    """★同一病灶★ `_wm_gettext_timeout` 對【逾時/視窗凍住】與【欄位真的是空的】
    都回 "" —— 呼叫端無法區分,於是「讀不到」被當成「欄位是空的」而放行寫入。"""
    import main
    monkeypatch.setattr(main, "_send_message_timeout_ex",
                        lambda *a, **k: (False, 0))
    assert main._wm_gettext_timeout_ex(1) == ("", False), "逾時要回 ok=False"
    monkeypatch.setattr(main, "_send_message_timeout_ex",
                        lambda *a, **k: (True, 0))
    assert main._wm_gettext_timeout_ex(1) == ("", True), "真的空欄位要回 ok=True"


def test_set_liaocheng_refuses_when_original_value_unreadable():
    """療程欄的原值把關存在的理由就是「不確定就不要寫」,而讀不到正是最不確定的情況。
    舊版讀不到(空字串)反而放行 → HIS 卡住時整道把關被繞過,抓錯欄位也照寫。"""
    import main
    code = _code_only(inspect.getsource(main._set_療程_only))
    assert "_wm_gettext_timeout_ex(" in code, "要用帶狀態的讀取"
    i_read = code.index("_wm_gettext_timeout_ex(")
    i_write = code.index("_wm_settext_timeout(")
    seg = code[i_read:i_write]
    assert "if not _read_ok:" in seg and "return False" in seg, \
        "讀取失敗必須在寫入之前中止"


def test_pain_radio_click_is_read_back():
    """★開迴路★ PostMessage 送出 ≠ radio 真的被選取。原本直接 log「已勾 0 radio」
    是推斷,不是事實。"""
    import main
    code = _code_only(inspect.getsource(main._f11_handle_pain))
    assert "0x00F0" in code, "要用 BM_GETCHECK 回讀"
    i_click = code.index("_post_click_to_control(_radio_hwnd)")
    i_check = code.index("0x00F0")
    assert i_click < i_check, "回讀要在點擊之後"
    assert "未選取" in inspect.getsource(main._f11_handle_pain), \
        "沒選到要照實說,不可讓 log 顯示成功"


def test_refresh_flag_cleared_inside_lock_and_last():
    """★外審★ 清除端若先在鎖外把 running 設 False,那一瞬間 main thread 觸發的新 refresh
    會看到 False 而啟動並寫入自己的 active signature,接著舊 worker 才進鎖把【新的】
    signature 清成 None → 新 refresh 期間的同款請求無法去重(重複打掛號站),
    舊 worker 的完成 callback 還會把仍在刷新中的 UI 顯示成「閒置」。"""
    import main
    code = _code_only(inspect.getsource(main.AutomationApp._trigger_refresh))
    # 每一處清除 running 旗標都必須在鎖區塊內,且排在清 signature 之後
    for m in re.finditer(r"self\._refresh_worker_running = False", code):
        head = code[:m.start()]
        i_lock = head.rindex("with self._refresh_queue_lock:")
        seg = head[i_lock:]
        assert "self._active_refresh_signature = None" in seg, \
            "running 旗標要在同一個臨界區內、且排在清 signature 之後"
        # 鎖區塊之後不可再有把旗標清成 False 的落單語句
        assert "\n                self._refresh_worker_running = False" not in code, \
            "不可在鎖外清 running 旗標"


def test_refresh_completion_callback_validates_ownership():
    """★外審 R2★ worker A 的 UI 收尾是 root.after 排到 main thread 才跑;那時 B 可能
    已經在跑。沒有歸屬驗證的話,A 的 callback 會把 UI 顯示成「閒置」並重新啟用按鈕,
    而實際上 B 還在刷新 —— 又是一次「故障看起來跟正常一樣」。"""
    import main
    code = _code_only(inspect.getsource(main.AutomationApp._trigger_refresh))
    assert "self._refresh_generation += 1" in code, "啟動時要遞增世代"
    i_gen = code.index("self._refresh_generation += 1")
    i_lock = code[:i_gen].rindex("with self._refresh_queue_lock:")
    assert "self._refresh_worker_running = True" in code[i_lock:i_gen], \
        "世代遞增要與搶旗標在同一個臨界區"
    assert "gen=_my_refresh_gen" in code, "callback 要綁定自己的世代"
    i_cb = code.index("def _on_refresh_worker_done")
    seg = code[i_cb:i_cb + 2500]
    assert "_owns_ui = (gen == self._refresh_generation)" in seg
    i_guard = seg.index("_owns_ui = (gen == self._refresh_generation)")
    i_ui = seg.index('self.status_text.set(f"狀態: 閒置')
    assert i_guard < i_ui, "歸屬驗證要在動 UI 之前"
    # UI 變更全部包在 _owns_ui 分支內
    assert "if not _owns_ui:" in seg[i_guard:i_ui]
    # ★外審 R6★ 非 UI 的完成記帳與佇列接力【不可】因為世代不符而被丟掉
    i_snap = seg.index("self._last_full_refresh_snapshot = deepcopy")
    assert i_snap > i_ui, "快照記帳要在 UI 分支之外(不論擁不擁有都要做)"
    assert "self._trigger_refresh(qr[0], qr[1])" in seg[i_snap:],         "佇列接力也要照做"


def test_refresh_check_enqueue_and_claim_share_one_critical_section():
    """★外審 R4★ worker 現在是在鎖內清旗標【並抽乾佇列】的,所以「鎖外檢查」有真實空窗:
    呼叫端讀到 True → 還沒拿到鎖 → worker 清完旗標並抽乾佇列 → 呼叫端才拿到鎖並 append
    → 那筆請求永遠不會有人接手,刷新靜默消失。檢查、入列、搶旗標必須同一個臨界區。"""
    import main
    code = _code_only(inspect.getsource(main.AutomationApp._trigger_refresh))
    i_lock = code.index("with self._refresh_queue_lock:")
    # [2026-08-10 批次SB #3] gate 加了 age takeover,條件變成
    # `if not ... or _stale_takeover:` —— 錨點跟著更新,性質不變。
    i_check = code.index("if not self._refresh_worker_running or _stale_takeover:")
    i_append = code.index("self._queued_refresh_requests.append(")
    i_claim = code.index("self._refresh_worker_running = True")
    assert i_lock < i_check < i_claim, "檢查與搶旗標都要在鎖內"
    assert i_lock < i_append, "入列也要在同一個鎖內"
    # 鎖外不可再有「先檢查旗標再決定要不要入列」的舊路徑
    head = code[:i_lock]
    assert "if self._refresh_worker_running:" not in head, "不可在鎖外先檢查旗標"
