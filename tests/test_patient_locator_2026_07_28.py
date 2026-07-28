# -*- coding: utf-8 -*-
"""[2026-07-28 使用者需求] 回讀不符告警要查得出是哪個病人。

使用者原話:「但是沒有紀錄該病人診間/診號/或是病歷號 這樣我沒辦法查詢是哪個病人有錯誤」。

實機資料來源(使用者提供截圖):HIS 主視窗**標題列只有版本號**
(`中國醫藥大學附設醫院…作業(海青灣)-- V.1150722.01`),病人資訊在上方一條獨立橫幅:

    1150728 早上 103診 113號 -呂冠愷(24994923)女 42歲1月 (0730623) #C0024322

本檔釘三件事:
  1. ★只擷取定位欄位★ 姓名/生日/性別年齡絕不可外流到任何回傳值、信件或檔案。
  2. 索引檔要在告警信【去重之前】寫 —— 否則同一天第二個病人完全查不到。
  3. 定位資訊【不可】進 hash-chain 稽核帳本(2026-07-17 明訂不存病人明文識別)。
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common import patient_locator as pl  # noqa: E402

REAL = "1150728 早上 103診 113號 -呂冠愷(24994923)女 42歲1月 (0730623) #C0024322"


def _code_only(src: str) -> str:
    src = re.sub(r'("""|\'\'\')(?:.|\n)*?\1', "", src)
    return "\n".join(line.split("#")[0] for line in src.splitlines())


def _main_src():
    return open(os.path.join(os.path.dirname(__file__), '..', 'src', 'main.py'),
                encoding='utf-8').read()


# ─── 解析(以實機字串為準)──────────────────────────────────────────────────
def test_parses_the_real_banner():
    loc = pl.parse_banner(REAL)
    assert loc == {"date_roc": "1150728", "session": "早上", "room": "103",
                   "seq": "113", "chart_no": "24994923", "visit_no": "C0024322"}


def test_never_leaks_name_or_birthday():
    """★核心隱私防線★ 拿得到 ≠ 該留。姓名、生日、性別年齡一律不得出現在
    回傳值或告警文字裡。"""
    loc = pl.parse_banner(REAL)
    blob = json.dumps(loc, ensure_ascii=False) + pl.format_for_alert(loc)
    for leak in ("呂冠愷", "呂", "冠愷", "0730623", "女", "42歲"):
        assert leak not in blob, f"外流了:{leak}"


def test_birthday_is_not_mistaken_for_chart_no():
    """橫幅裡有兩組括號數字:(24994923)=病歷號、(0730623)=生日。
    必須取【診號之後緊接的第一組】,取錯就等於把生日當成病歷號寄出去。"""
    assert pl.parse_banner(REAL)["chart_no"] == "24994923"


def test_allowed_fields_is_the_mechanical_guard():
    """新增欄位一定要先進 ALLOWED_FIELDS —— 這是防止哪天有人順手把姓名塞進來
    的機械性防線,不是靠記性。"""
    assert set(pl.parse_banner(REAL)) <= set(pl.ALLOWED_FIELDS)
    assert "name" not in pl.ALLOWED_FIELDS
    assert "birthday" not in pl.ALLOWED_FIELDS


def test_rejects_non_banner_controls():
    """畫面上到處都是數字,識別條件必須同時要求 民國日期 + N診 + N號。"""
    for text in ("", "病人資料", "1150728 早上", "總共 113號", "V.1150722.01",
                 "103診", "1150728 103診", "x" * 500):
        assert pl.parse_banner(text) is None, f"不該把 {text[:20]!r} 當成橫幅"


def test_tolerates_missing_optional_parts():
    """沒有就診序號/病歷號時仍要回傳診間診號,不可整個放棄。"""
    loc = pl.parse_banner("1150728 下午 105診 7號 -某人")
    assert loc["room"] == "105" and loc["seq"] == "7"
    assert "chart_no" not in loc


def test_format_is_honest_when_unavailable():
    """★訊息只能陳述程式確知的事★ 抓不到時要說抓不到,不可留空白讓人以為沒事。"""
    text = pl.format_for_alert(None)
    assert "無法取得" in text and "橫幅" in text


# ─── 索引檔 ────────────────────────────────────────────────────────────────
def test_index_appends_and_keeps_only_whitelist(tmp_path):
    p = str(tmp_path / "idx.jsonl")
    assert pl.append_index(p, ts="2026-07-28T09:15:00", action="F1 UVB 劑量",
                           detail="回讀 dose=800 count=21",
                           locator=pl.parse_banner(REAL))
    rows = [json.loads(x) for x in open(p, encoding="utf-8") if x.strip()]
    assert len(rows) == 1
    assert rows[0]["room"] == "103" and rows[0]["chart_no"] == "24994923"
    assert "呂冠愷" not in open(p, encoding="utf-8").read()


def test_index_records_every_mismatch_not_just_the_first(tmp_path):
    """★這是使用者痛點的核心★ 告警信同功能同日只寄一次,所以第二個病人
    只能靠索引查得到。索引不可有任何去重。"""
    p = str(tmp_path / "idx.jsonl")
    for seq in ("113", "114", "115"):
        pl.append_index(p, ts="2026-07-28T09:15:00", action="F1 UVB 劑量",
                        detail="d", locator={"room": "103", "seq": seq})
    rows = [json.loads(x) for x in open(p, encoding="utf-8") if x.strip()]
    assert [r["seq"] for r in rows] == ["113", "114", "115"]


def test_index_prunes_old_rows(tmp_path):
    p = str(tmp_path / "idx.jsonl")
    now = datetime(2026, 7, 28, 9, 0, 0)
    old = (now - timedelta(days=40)).isoformat(timespec="seconds")
    keep = (now - timedelta(days=5)).isoformat(timespec="seconds")
    pl.append_index(p, ts=old, action="a", detail="", locator=None, now=now)
    pl.append_index(p, ts=keep, action="b", detail="", locator=None, now=now)
    pl.append_index(p, ts=now.isoformat(timespec="seconds"), action="c",
                    detail="", locator=None, now=now)
    rows = [json.loads(x) for x in open(p, encoding="utf-8") if x.strip()]
    assert [r["action"] for r in rows] == ["b", "c"], "40 天前那筆要被修剪掉"


def test_index_survives_a_corrupt_line(tmp_path):
    """一列壞掉不可毀掉整份索引(它是事後查病人的唯一依據)。"""
    p = str(tmp_path / "idx.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"ts":"2026-07-28T08:00:00","action":"good"}\n')
        f.write("這不是 JSON\n")
    assert pl.append_index(p, ts="2026-07-28T09:00:00", action="new",
                           detail="", locator=None,
                           now=datetime(2026, 7, 28))
    rows = [json.loads(x) for x in open(p, encoding="utf-8") if x.strip()]
    assert [r["action"] for r in rows] == ["good", "new"]


def test_index_never_raises(tmp_path):
    """寫索引失敗不可弄壞臨床流程(與稽核帳本同一原則)。"""
    assert pl.append_index(str(tmp_path / "no" / "such" / "dir" / "x.jsonl"),
                           ts="t", action="a", detail="", locator=None) is False


# ─── 與主程式的接線 ────────────────────────────────────────────────────────
def test_locator_is_sampled_at_action_time_not_in_the_background():
    """★時機★ 背景緒稍後才採樣可能已經換病人了 —— 必須在動作當下(入列時)採。"""
    code = _code_only(_main_src())
    i = code.index("def _record_his_action(")
    body = code[i:i + 2500]
    assert "_sample_patient_locator(main_hwnd)" in body
    assert 'if str(fields.get("outcome", "")) == _LEDGER_MISMATCH:' in body


def test_locator_must_not_enter_the_ledger_fields():
    """★2026-07-17 定案不動★ fields 會落進 hash-chain 稽核帳本,定位資訊只能走
    佇列的獨立欄位。"""
    code = _code_only(_main_src())
    i = code.index("def _record_his_action(")
    body = code[i:i + 2500]
    assert 'fields["locator"]' not in body
    assert 'fields.setdefault("locator"' not in body
    assert "dict(fields), ts, locator)" in body, "要走獨立欄位,不可塞進 fields"


def test_index_is_written_before_the_dedup_gate():
    """★去重擋掉的是信,不是紀錄★ 索引寫入必須在去重之前。
    (2026-07-28 去重改用共用的 AlertDeduper.claim,順序要求不變。)"""
    code = _code_only(_main_src())
    i = code.index("def _notify_audit_mismatch(")
    body = code[i:i + 2500]
    i_idx = body.index("_append_locator_index(")
    i_dedup = body.index("_MISMATCH_ALERTS.claim(")
    assert i_idx < i_dedup, "索引寫在去重之後 → 同一天第二個病人查不到"


def test_alert_email_includes_the_locator():
    code = _code_only(_main_src())
    i = code.index("def _notify_audit_mismatch(")
    body = code[i:i + 3500]
    assert "病人定位:{_loc_text}" in body
    assert "_LOCATOR_INDEX_FILENAME" in body, "信裡要告訴使用者去哪查後續病人"


def test_writer_loop_tolerates_the_old_four_field_item():
    """關機排空時佇列裡可能還有改版前入列的 4 元素項目 —— 不可 unpack 炸掉。"""
    code = _code_only(_main_src())
    i = code.index("def _ledger_writer_loop(")
    body = code[i:i + 1500]
    assert "if len(item) >= 5:" in body
    assert "locator = None" in body


def test_alert_includes_what_it_meant_to_write():
    """[2026-07-28] 原本只印回讀值,看不出「本來要寫什麼」——
    少了預期值就分不出是「寫錯」還是「讀錯/根本沒寫進去」。
    (實機案例:信上只有『回讀 dose=800 count=21』,無從判斷。)"""
    code = _code_only(_main_src())
    i = code.index("def _notify_audit_mismatch(")
    body = code[i:i + 3500]
    assert "預期寫入:{expected" in body
    assert "實際回讀:{detail}" in body


def test_expected_value_comes_from_the_ledger_value_field():
    """value 依 _record_his_action 的契約必為非 PII(醫令代碼/劑量/療程數),
    拿來當「預期寫入」是安全的;detail 同理。不可改抓別的欄位。"""
    code = _code_only(_main_src())
    i = code.index("def _ledger_writer_loop(")
    body = code[i:i + 2000]
    assert 'expected=str(fields.get("value", ""))' in body


def test_his_calibrated_version_matches_user_confirmed_build():
    """[2026-07-28 使用者實測] V.1150722.01 全部熱鍵功能正常 → 校正版本必須跟上,
    否則每台沒有自訂基線的機器都會一直收到「疑似改版」通知。"""
    from cmuh_common import his_contract as hc
    assert hc.CALIBRATED_VERSION == "1150722"
    # main 的別名要真的指向單一宣告處(不可又有一份自己的字面值)
    import main
    assert main._HIS_CALIBRATED_VERSION == hc.CALIBRATED_VERSION
