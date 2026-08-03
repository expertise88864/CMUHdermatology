# -*- coding: utf-8 -*-
"""app_settings helpers."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from datetime import date  # noqa: E402

from cmuh_common.app_settings import (
    stamp_r_doctor_revision,  # noqa: E402
    DEFAULT_DOCTOR_SETTINGS,
    default_r_doctor_settings,
    load_auto_reboot_settings,
    load_doctors_settings,
    load_r_doctor_settings,
    load_threshold_settings,
)


def _write_json(path, data):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)


def test_load_r_doctor_settings_trims_names_and_uses_defaults():
    """存檔【帶著目前的名單版號】時才以檔案為準,並去掉前後空白。

    [使用者定案 2026-08-03] 加了名單版號之後,沒有版號的舊存檔會被預設值複寫
    (見 test_an_old_saved_roster_is_superseded)。所以這支測試要明確蓋上版號,
    它驗的是「使用者自己改過的名字要留住」。
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "r.json")
        _write_json(path, stamp_r_doctor_revision({"R1": {"name": " Alice "}}))

        # 指定 today 讓預設確定(8/1 起 R2=林于喬)
        settings = load_r_doctor_settings(path, today=date(2026, 8, 1))

        assert settings["R1"] == {"name": "Alice"}
        assert settings["R2"]["name"] == "林于喬"


def test_an_old_saved_roster_is_superseded_by_the_defaults():
    """★[使用者定案 2026-08-03] 直接複寫每台電腦上的舊存檔★

    `r_doctor_settings.json` 一存在就蓋過預設值,所以光改預設值救不了已經存過檔
    的機器 —— 它們會繼續顯示漏掉 R4 的舊名單(蔡明洋在院方值班表上比對不到)。
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "r.json")
        _write_json(path, {"R1": {"name": "林于喬"},      # 升年前的舊名單
                           "R2": {"name": "陳翊嘉"},
                           "R3": {"name": "蔡明洋"}})

        settings = load_r_doctor_settings(path, today=date(2026, 8, 3))

        assert settings["R1"]["name"] == "賴奕彰"
        assert settings["R4"]["name"] == "蔡明洋", "★R4 沒有補回來★"


def test_a_saved_roster_with_the_current_revision_is_respected():
    """★空集合不算通過★ 複寫只發生一次:使用者改過並儲存之後要留住。"""
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "r.json")
        _write_json(path, stamp_r_doctor_revision(
            {"R1": {"name": "自己改的"}, "R2": {"name": "林于喬"},
             "R3": {"name": "陳翊嘉"}, "R4": {"name": "蔡明洋"}}))

        settings = load_r_doctor_settings(path, today=date(2026, 8, 3))

        assert settings["R1"]["name"] == "自己改的"


def test_default_r_doctor_names_transition_2026_08_01():
    # [codex] 生效日閘門:8/1 前仍舊組(避免無存檔機器提早換階級);8/1 起換新組
    assert default_r_doctor_settings(date(2026, 7, 31)) == {
        "R1": {"name": "林于喬"},
        "R2": {"name": "陳翊嘉"},
        "R3": {"name": "蔡明洋"},
    }
    # [使用者定案 2026-08-03] 升年是每人往上一階,蔡明洋 R3→R4。
    # 原本這組只寫到 R3 —— 他就此從值班姓名對照裡消失了。
    assert default_r_doctor_settings(date(2026, 8, 1)) == {
        "R1": {"name": "賴奕彰"},
        "R2": {"name": "林于喬"},
        "R3": {"name": "陳翊嘉"},
        "R4": {"name": "蔡明洋"},
    }


def test_default_doctors_include_new_clinic_count_codes():
    # [使用者定案 2026-07-20] 門診人數查詢新增 D34257 蔡明洋、101358 陳翊嘉;D35819 李威儒已在
    by_code = {d["doc_no"]: d["name"] for d in DEFAULT_DOCTOR_SETTINGS}
    assert by_code.get("D34257") == "蔡明洋"
    assert by_code.get("101358") == "陳翊嘉"
    assert by_code.get("D35819") == "李威儒"


def test_load_threshold_settings_fills_legacy_dnd_times_safely():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "threshold.json")
        _write_json(path, {"notify_dnd_start_hour": "bad", "notify_dnd_end_hour": 25})

        settings = load_threshold_settings(
            path,
            {"chang_mon_night": 129},
            dnd_start_hour=0,
            dnd_end_hour=8,
        )

        assert settings["chang_mon_night"] == 129
        assert settings["ui_font_scale"] == 1.0
        assert settings["notify_dnd_start_time"] == "00:00"
        assert settings["notify_dnd_end_time"] == "24:00"


def test_load_doctors_settings_repairs_and_persists_swapped_rows():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "doctors.json")
        _write_json(path, [{"name": "D12345", "doc_no": "王小明", "notifications": True}])

        rows = load_doctors_settings(path)

        assert rows == [{"name": "王小明", "doc_no": "D12345", "notifications": True}]
        with open(path, encoding="utf-8") as handle:
            persisted = json.load(handle)
        assert persisted == rows


def test_load_doctors_settings_returns_independent_default_copy():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "missing.json")

        rows = load_doctors_settings(path)
        rows[0]["name"] = "changed"

        assert DEFAULT_DOCTOR_SETTINGS[0]["name"] == "張廖年峰"


def test_load_auto_reboot_settings_merges_defaults():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "auto.json")
        _write_json(path, {"enabled": True})

        assert load_auto_reboot_settings(path) == {"enabled": True, "time": "07:01"}
