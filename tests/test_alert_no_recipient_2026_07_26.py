# -*- coding: utf-8 -*-
"""[2026-07-26 main.py 未審區段] 止掛提醒觸發但收件人清單是空的 → 靜默不寄。

使用者看到功能開著、門檻也設了,信卻永遠不來,而且查 log 也查不到原因 ——
兩條寄送路徑原本都只是 `if not recipients: return False` / `if rcpts:`,沒有任何 log。
"""
import inspect
import logging
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402


def _code_only(src: str) -> str:
    src = re.sub(r'("""|\'\'\')(?:.|\n)*?\1', "", src)
    return "\n".join(line.split("#")[0] for line in src.splitlines())


def test_empty_recipients_is_warned(caplog):
    main._ALERT_NO_RECIPIENT_WARNED_DAY[0] = ""
    with caplog.at_level(logging.WARNING):
        ok = main._send_alert_email_via_smtp("主旨", "內文", [])
    assert ok is False
    msgs = [r.getMessage() for r in caplog.records]
    assert any("收件人清單是空的" in m for m in msgs), "不可靜默"
    assert any("設定頁" in m for m in msgs), "要告訴使用者怎麼修"


def test_warning_is_throttled_per_day(caplog):
    """掃描迴圈是分鐘級的,不可每輪都洗一次 log(洗版會蓋掉真正重要的訊息)。"""
    main._ALERT_NO_RECIPIENT_WARNED_DAY[0] = ""
    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            main._send_alert_email_via_smtp("主旨", "內文", [])
    hits = [r for r in caplog.records if "收件人清單是空的" in r.getMessage()]
    assert len(hits) == 1, f"同一天只該講一次,實際 {len(hits)} 次"


def test_calendar_path_also_warns():
    """兩條寄送路徑都要提示(行事曆通知 + 遠期背景掃描)。"""
    src = open(main.__file__, encoding="utf-8").read()
    assert src.count("_warn_alert_has_no_recipients(") == 4, \
        "定義 1 次 + 三條路徑各呼叫 1 次"
    assert '_warn_alert_has_no_recipients("行事曆止掛通知")' in src
    assert '_warn_alert_has_no_recipients("SMTP 寄信")' in src
    # ★外審★ 遠期掃描在入口就 return,那條路徑原本完全碰不到 SMTP helper
    assert ('_warn_alert_has_no_recipients("遠期止掛掃描", threshold_reached=False)'
            in src)


def test_warning_does_not_change_send_behaviour():
    """只加提示,不可改變「沒有收件人就不寄」的既有行為。"""
    code = _code_only(inspect.getsource(main._send_alert_email_via_smtp))
    i_guard = code.index("if not recipients:")
    seg = code[i_guard:i_guard + 200]
    assert "return False" in seg


def test_scan_entry_message_does_not_claim_threshold_reached(caplog):
    """★訊息只能陳述程式確知的事★ 遠期掃描是在【還沒檢查任何診次之前】就返回,
    那時說「已達門檻」是程式不知道的事。"""
    main._ALERT_NO_RECIPIENT_WARNED_DAY[0] = ""
    with caplog.at_level(logging.WARNING):
        main._warn_alert_has_no_recipients("遠期止掛掃描", threshold_reached=False)
    msg = [r.getMessage() for r in caplog.records][-1]
    assert "已達止掛門檻" not in msg, "還沒檢查診次就不可宣稱已達門檻"
    assert "就算有診次達門檻也不會通知" in msg
    assert "設定頁" in msg


def test_send_path_message_still_states_threshold_reached(caplog):
    """真正在寄信那一刻,「已達門檻」是確知的事實,措辭不可退化。"""
    main._ALERT_NO_RECIPIENT_WARNED_DAY[0] = ""
    with caplog.at_level(logging.WARNING):
        main._send_alert_email_via_smtp("主旨", "內文", [])
    msg = [r.getMessage() for r in caplog.records][-1]
    assert "已達止掛門檻" in msg
