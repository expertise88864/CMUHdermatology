# -*- coding: utf-8 -*-
"""[2026-08-26] 本機快速修正檔(settings/his_menu_override.json)。

院方改版位移選單 id 時的急救通道:寫 JSON + 重啟就生效,不用等推版。
選單 id 錯 = 熱鍵打到別的功能 = 誤寫病歷,所以這裡釘的全是「不能套」的情況:
整檔拒用、過期失效、部分錯誤不做部分套用。conftest 已把 settings 目錄逐測試
導到 tmp,寫檔互不污染;每個測試結束都把模組 reload 回乾淨字面值。
"""
import importlib
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import cmuh_common.his_contract as hc  # noqa: E402


def _write(data) -> None:
    path = hc.override_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        if isinstance(data, str):
            f.write(data)
        else:
            json.dump(data, f, ensure_ascii=False)


def _reload():
    return importlib.reload(hc)


def _clean_reload():
    """刪掉 override 檔再 reload → 模組回到純字面值(供 finally 用)。"""
    try:
        os.remove(hc.override_path())
    except OSError:
        pass
    return importlib.reload(hc)


MARK = hc.override_marker()


def test_no_override_file_keeps_the_literals():
    try:
        m = _reload()
        assert m.MENU_ID_同意書 == 671 and m.MENU_ID_代碼輸入 == 219
        assert m.OVERRIDE_NOTE == "" and m.OVERRIDE_ERROR == ""
    finally:
        _clean_reload()


def test_valid_override_applies_and_says_so_loudly():
    """急救主路徑:合法檔 → 值改掉、OVERRIDE_NOTE 說了改什麼、describe 帶出。"""
    try:
        _write({"for_calibration": MARK, "MENU_ID_同意書": 672})
        m = _reload()
        assert m.MENU_ID_同意書 == 672
        assert m.MENU_ID_代碼輸入 == 219, "沒列的鍵不可以動"
        assert "MENU_ID_同意書 671→672" in m.OVERRIDE_NOTE
        assert "本機快速修正" in m.describe(), "設定頁/log 必須看得到 override 生效"
    finally:
        _clean_reload()


def test_stale_override_expires_after_a_real_calibration():
    """★核心安全性★ 檔內戳記 ≠ 程式字面值(=正式校正已推上去)→ 整檔失效,
    急救貼布不可以把【更新的正式值】蓋回舊值。"""
    try:
        _write({"for_calibration": "1150805#5", "MENU_ID_同意書": 999})
        m = _reload()
        assert m.MENU_ID_同意書 == 671, "過期 override 蓋掉了正式校正值"
        assert "過期" in m.OVERRIDE_ERROR or "不符" in m.OVERRIDE_ERROR
    finally:
        _clean_reload()


def test_missing_version_marker_rejects_the_whole_file():
    try:
        _write({"MENU_ID_同意書": 672})
        m = _reload()
        assert m.MENU_ID_同意書 == 671 and m.OVERRIDE_ERROR
    finally:
        _clean_reload()


def test_unknown_key_rejects_the_whole_file():
    """打錯鍵名(例如 MENU_ID_同意 少個字)不可以「其他鍵照套」——
    使用者以為修好了,實際上要修的那個鍵根本沒進去。"""
    try:
        _write({"for_calibration": MARK, "MENU_ID_同意書": 672,
                "MENU_ID_打錯的鍵": 5})
        m = _reload()
        assert m.MENU_ID_同意書 == 671, "含未知鍵仍部分套用了"
        assert "未知鍵" in m.OVERRIDE_ERROR
    finally:
        _clean_reload()


def test_bad_id_values_reject_the_whole_file():
    for bad in (True, "671", 0, 65536, 671.0):
        try:
            _write({"for_calibration": MARK, "MENU_ID_同意書": bad})
            m = _reload()
            assert m.MENU_ID_同意書 == 671, f"{bad!r} 被當成合法 id 套用了"
            assert m.OVERRIDE_ERROR
        finally:
            _clean_reload()


def test_broken_json_is_rejected_not_fatal():
    """壞 JSON:六支程式共用本模組,絕不可因此 import 失敗;要拒用+留痕。"""
    try:
        _write("{ 這不是 JSON")
        m = _reload()
        assert m.MENU_ID_同意書 == 671
        assert "讀取失敗" in m.OVERRIDE_ERROR
    finally:
        _clean_reload()


def test_overridden_version_still_describes_without_crashing():
    """CALIBRATED_VERSION 被覆蓋成沒有歷史紀錄的版本 → current_calibration
    回字面值那筆,describe() 不可以炸(它掛在設定頁/告警信路徑上)。"""
    try:
        _write({"for_calibration": MARK, "CALIBRATED_VERSION": "1150901"})
        m = _reload()
        assert m.CALIBRATED_VERSION == "1150901"
        assert (m.current_calibration().version == m._LITERAL_CALIBRATED_VERSION)
        assert "本機快速修正" in m.describe()
    finally:
        _clean_reload()


def test_parse_override_is_pure_and_rejects_partial():
    updates, err = hc.parse_override(
        {"for_calibration": MARK, "MENU_ID_同意書": 672,
         "MENU_ID_代碼輸入": 0}, MARK)
    assert err and not updates, "有一鍵不合法就不可以回任何 updates"
