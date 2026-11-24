# -*- coding: utf-8 -*-
"""[2026-11-24 使用者] 門診醫師代號:既有電腦補上蔡明洋、李威儒(只補一次)。"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import app_settings as aps  # noqa: E402

ROW = {"name": "測試甲", "doc_no": "D00001", "notifications": True}


def _load(tmp_path, rows=None):
    p = tmp_path / "doctors.json"
    if rows is not None:
        p.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return aps.load_doctors_settings(str(p)), p


def _codes(rows):
    return [r["doc_no"] for r in rows]


def test_existing_file_gets_both_once_and_keeps_its_rows(tmp_path):
    rows, p = _load(tmp_path, [ROW])
    assert rows[0] == ROW and sorted(_codes(rows)[1:]) == ["D34257", "D35819"]
    assert json.loads(p.read_text(encoding="utf-8")) == rows


def test_a_later_deletion_is_respected(tmp_path):
    _load(tmp_path, [ROW])
    rows, _p = _load(tmp_path, [ROW])
    assert _codes(rows) == ["D00001"]


def test_only_the_missing_one_is_added(tmp_path):
    rows, _p = _load(tmp_path, [{"name": "李威儒", "doc_no": "D35819", "notifications": True}])
    assert _codes(rows) == ["D35819", "D34257"] and rows[0]["notifications"] is True


def test_same_name_with_another_code_is_not_duplicated(tmp_path):
    rows, _p = _load(tmp_path, [{"name": "蔡明洋", "doc_no": "D99999", "notifications": False}])
    assert [r["name"] for r in rows].count("蔡明洋") == 1


def test_fresh_install_already_has_both(tmp_path):
    rows, _p = _load(tmp_path)
    assert {"D34257", "D35819"} <= set(_codes(rows))


def test_unreadable_file_is_neither_written_nor_marked(tmp_path, monkeypatch):
    p = tmp_path / "doctors.json"
    p.write_text(json.dumps([ROW]), encoding="utf-8")
    monkeypatch.setattr(aps, "load_json_list_ex",
                        lambda *a: (list(aps.DEFAULT_DOCTOR_SETTINGS), "error"))
    aps.load_doctors_settings(str(p))
    assert json.loads(p.read_text(encoding="utf-8")) == [ROW]
    assert not (tmp_path / aps._DOCTORS_ADD_ONCE_MARK).exists()


def test_a_pre_transaction_backup_is_left_alone(tmp_path):
    p = tmp_path / "doctors.json.rollback.bak"
    p.write_text(json.dumps([ROW]), encoding="utf-8")
    assert _codes(aps.load_doctors_settings(str(p))) == ["D00001"]
