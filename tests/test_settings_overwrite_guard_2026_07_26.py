# -*- coding: utf-8 -*-
"""[2026-07-26 main.py 未審區段 review] 設定檔「暫時讀不到」被當成「沒有設定」→ 覆蓋。

今天已經在打卡設定、排班 config、watchdog config、SMTP 帳密、updater 版本檔上修過同一個
病灶,這次是主程式自己的設定(門檻、止掛提醒收件人、醫師清單、F8 快速輸入文字)。
路徑:`config_io.load_json_dict` 用不帶狀態的 `safe_load_json` → 讀不到就回【預設值】
→ 載進記憶體 → 使用者在設定頁按一次「儲存」→ `_atomic_write_json` 把預設值寫回磁碟。
真正的設定永久消失,而且整個過程沒有任何徵兆。
"""
import inspect
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import app_settings as aps  # noqa: E402
from cmuh_common import config_io as cio  # noqa: E402


def _code_only(src: str) -> str:
    src = re.sub(r'("""|\'\'\')(?:.|\n)*?\1', "", src)
    return "\n".join(line.split("#")[0] for line in src.splitlines())


def test_load_json_dict_ex_reports_transient_error(monkeypatch, tmp_path):
    """「讀不到」與「檔案不存在」必須分得出來。"""
    monkeypatch.setattr(cio, "safe_load_json_ex", lambda *a, **k: (None, "error"))
    data, status = cio.load_json_dict_ex(str(tmp_path / "x.json"), {"a": 1})
    assert status == "error"
    assert data == {"a": 1}, "仍回預設值供顯示,但狀態要能讓呼叫端拒絕存檔"


def test_load_json_dict_keeps_old_behaviour(monkeypatch, tmp_path):
    """薄包裝不可改變既有呼叫端的行為。"""
    monkeypatch.setattr(cio, "safe_load_json_ex", lambda *a, **k: ({"b": 2}, "ok"))
    assert cio.load_json_dict(str(tmp_path / "x.json"), {"a": 1}) == {"a": 1, "b": 2}


def test_threshold_loader_records_failure(monkeypatch, tmp_path):
    aps._LOAD_FAILED_FILES.clear()
    monkeypatch.setattr(aps, "load_json_dict_ex", lambda *a, **k: ({}, "error"))
    aps.load_threshold_settings(str(tmp_path / "threshold_settings.json"))
    assert "threshold_settings.json" in aps.settings_load_failed()
    # 重新讀到就要清掉(不可一次失敗就永久卡住不能存檔)
    monkeypatch.setattr(aps, "load_json_dict_ex", lambda *a, **k: ({}, "ok"))
    aps.load_threshold_settings(str(tmp_path / "threshold_settings.json"))
    assert "threshold_settings.json" not in aps.settings_load_failed()
    aps._LOAD_FAILED_FILES.clear()


def test_missing_and_corrupt_are_not_treated_as_failure(monkeypatch, tmp_path):
    """missing/corrupt 代表磁碟上本來就沒有可用內容 → 用預設值存檔是合理修復,
    不可因為這次收緊而讓新裝機器永遠不能存設定。"""
    for st in ("missing", "corrupt"):
        aps._LOAD_FAILED_FILES.clear()
        monkeypatch.setattr(aps, "load_json_dict_ex", lambda *a, **k: ({}, st))
        aps.load_threshold_settings(str(tmp_path / "threshold_settings.json"))
        assert not aps.settings_load_failed(), st
    aps._LOAD_FAILED_FILES.clear()


def test_doctors_repair_write_skipped_on_transient_error(monkeypatch, tmp_path):
    """★不需使用者做任何事就會發生★ doctors 載入路徑有一條「正規化後順手修檔」的寫入。
    暫時讀不到時它拿到的是預設清單,寫下去等於光是啟動就把醫師清單清成預設。"""
    written = []
    aps._LOAD_FAILED_FILES.clear()
    monkeypatch.setattr(aps, "load_json_list_ex", lambda *a, **k: ([], "error"))
    monkeypatch.setattr(aps, "normalize_doctor_rows",
                        lambda data, defaults: (defaults, True))  # fixed=True
    monkeypatch.setattr(aps, "atomic_write_json",
                        lambda *a, **k: written.append(a))
    aps.load_doctors_settings(str(tmp_path / "doctors.json"))
    assert written == [], "讀不到就不可寫回"
    aps._LOAD_FAILED_FILES.clear()


def test_save_all_settings_refuses_when_load_failed():
    """★寫入咽喉★ 只要本次執行曾讀不到,存檔一律拒絕並告訴使用者為什麼。"""
    import main
    code = _code_only(inspect.getsource(main.AutomationApp.save_all_settings))
    i_guard = code.index("_settings_load_failed()")
    i_write = code.index("_atomic_write_json(")
    assert i_guard < i_write, "檢查要在任何寫檔之前"
    seg = code[i_guard:i_write]
    assert "return" in seg, "有失敗就要直接 return,不可繼續寫"
    assert "showwarning" in seg, "要告訴使用者為什麼沒存(否則他會以為存好了)"
