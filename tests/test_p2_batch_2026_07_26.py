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
    code = _code_only(inspect.getsource(main._click_button_normalized_text))
    assert "if not _post_click_to_control(out[0]):" in code, \
        "PostMessage 的回傳值不可丟掉"
    i = code.index("if not _post_click_to_control(out[0]):")
    assert "return 0" in code[i:i + 300], "送不出去要回 0,讓呼叫端走失敗分支"


# ── F8:對 HIS 欄位注入文字必須留稽核 ────────────────────────────────────────
def test_f8_records_audit_ledger_on_both_outcomes():
    import main
    code = _code_only(inspect.getsource(main.script_F8_quick_text))
    assert code.count("_record_his_action(") == 2, "成功與失敗都要記"
    assert "_LEDGER_OK" in code and "_LEDGER_FAILED" in code
    assert "len=" in code and "text}" not in code, "只記長度,不可把輸入內容寫進帳本"


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
