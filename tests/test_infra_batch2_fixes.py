# -*- coding: utf-8 -*-
"""基建批次2 回歸（§6F：IF-01 / IF-02 / IF-03，2026-07-11）。

  IF-01 tk_exception handler 3-arg 簽章 → 被指派為 class attr 後 instance 呼叫多帶 self=4 引數必炸。
  IF-02 smtp_credentials.json 存成 BOM/ANSI 被 corrupt-rename 搬走 → SMTP+IMAP 一次全滅。
  IF-03 阻塞式 MessageBox 在 health monitor 緒 inline 呼叫 → 卡死不再 tick → RAM 保險絲失效。
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import notifications, smtp_mail, tk_exception  # noqa: E402
from cmuh_common.atomic_io import safe_load_json_ex  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


# ══ IF-01：handler 相容 3 引數(instance-attr)與 4 引數(class-bound)兩條路徑 ═══════════
def test_if01_handler_accepts_3_and_4_args():
    # instance-attr 路徑:Tk 傳 (exc, val, tb)
    tk_exception._report_callback_exception(ValueError, ValueError("x"), None)
    # class-bound 路徑:綁定後多帶 self → (self, exc, val, tb)
    tk_exception._report_callback_exception(object(), ValueError, ValueError("y"), None)


def test_if01_class_attr_binding_does_not_raise():
    # 直接重現 bug:指派為 class attr → descriptor 綁定 → instance 呼叫多帶 self。舊 3-arg 簽章必炸。
    class _FakeTk:
        report_callback_exception = staticmethod(tk_exception._report_callback_exception)

    class _FakeTkBound:
        report_callback_exception = tk_exception._report_callback_exception

    _FakeTk().report_callback_exception(ValueError, ValueError("boom"), None)
    _FakeTkBound().report_callback_exception(ValueError, ValueError("boom"), None)  # 綁定→4 引數


# ══ IF-02：BOM 可讀;credentials corrupt 不搬走(backup_on_corrupt=False)════════════════
def test_if02_bom_json_loads_ok(tmp_path):
    p = tmp_path / "x.json"
    p.write_text("﻿" + json.dumps({"a": 1}), encoding="utf-8")   # UTF-8 BOM + json
    val, status = safe_load_json_ex(str(p), default={})
    assert status == "ok" and val == {"a": 1}


def test_if02_corrupt_kept_when_backup_disabled(tmp_path):
    p = tmp_path / "smtp.json"
    p.write_bytes("『中文名』 not json".encode("cp950"))              # ANSI/cp950 → UnicodeDecodeError
    val, status = safe_load_json_ex(str(p), default={}, backup_on_corrupt=False)
    assert status == "corrupt" and val == {}
    assert p.exists(), "IF-02: backup_on_corrupt=False 壞檔要原地保留"
    assert not list(tmp_path.glob("*.corrupt-*")), "IF-02: 不可 rename 成 .corrupt 搬走唯一帳密"


def test_if02_load_credentials_reads_bom(tmp_path, monkeypatch):
    cf = tmp_path / "smtp_credentials.json"
    data = {"host": "smtp.x", "port": 587, "username": "u@x", "password": "pw", "use_tls": True}
    cf.write_text("﻿" + json.dumps(data), encoding="utf-8")     # 記事本另存 UTF-8(帶 BOM)
    monkeypatch.setattr(smtp_mail, "CREDENTIALS_FILE", cf)
    cred = smtp_mail.load_credentials()
    assert cred["password"] == "pw" and cred["username"] == "u@x", "BOM 檔應能正常讀出帳密"


def test_if02_load_credentials_keeps_corrupt_file(tmp_path, monkeypatch):
    cf = tmp_path / "smtp_credentials.json"
    cf.write_bytes("from_name=『中文』 not-json".encode("cp950"))     # ANSI/cp950
    monkeypatch.setattr(smtp_mail, "CREDENTIALS_FILE", cf)
    cred = smtp_mail.load_credentials()
    assert cred["password"] == "", "讀不到 → 回 default(空密碼,流程靜默跳過)"
    assert cf.exists(), "IF-02: 壞掉的帳密檔要原地保留(不搬走)"
    assert not list(tmp_path.glob("*.corrupt-*")), "IF-02: 不可搬成 .corrupt"


# ══ IF-03：非阻塞通知 —— 呼叫端立即返回,實際通知在背景緒執行 ════════════════════════
def test_if03_async_notification_is_nonblocking(monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def _blocking(title, message):
        started.set()
        release.wait(5)          # 模擬 MessageBox 阻塞到使用者按掉

    monkeypatch.setattr(notifications, "show_windows_notification", _blocking)
    t0 = time.time()
    notifications.show_windows_notification_async("t", "m")
    assert time.time() - t0 < 1.0, "IF-03: async 版呼叫端必須立即返回(不阻塞監看緒)"
    assert started.wait(2), "IF-03: 通知應在背景 daemon 緒真的執行"
    release.set()


def test_if03_ram_warn_uses_async_variant():
    text = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
    i = text.index("def _ram_restart_warn")
    j = text.index("start_health_monitor", i)
    assert "show_windows_notification_async(" in text[i:j], \
        "IF-03: _ram_restart_warn(監看緒 inline 呼叫)必須用非阻塞版"
