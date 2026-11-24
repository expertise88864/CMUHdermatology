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


def test_same_name_with_another_code_is_corrected_not_duplicated(tmp_path):
    rows, _p = _load(tmp_path, [{"name": "蔡明洋", "doc_no": "D99999", "notifications": True}])
    assert rows[0] == {"name": "蔡明洋", "doc_no": "D34257", "notifications": True}
    assert sorted(_codes(rows)) == ["D34257", "D35819"]


def test_fresh_install_already_has_both(tmp_path):
    rows, _p = _load(tmp_path)
    assert {"D34257", "D35819"} <= set(_codes(rows))
    # [PR #30 審查] 第一次存檔就刪掉的,不可以再被加回來
    rows, _p = _load(tmp_path, [r for r in rows if r["doc_no"] != "D34257"])
    assert "D34257" not in _codes(rows)


def test_a_denied_write_still_loads_and_retries_later(tmp_path, monkeypatch):
    """[PR #30 審查] 補醫師寫不進檔不可以讓程式開不起來。"""
    with monkeypatch.context() as m:
        m.setattr(aps, "atomic_write_json",
                  lambda *a, **k: (_ for _ in ()).throw(PermissionError("鎖住")))
        rows, p = _load(tmp_path, [ROW])
    assert len(rows) == 3 and json.loads(p.read_text(encoding="utf-8")) == [ROW]
    assert len(aps.load_doctors_settings(str(p))) == 3
    assert len(json.loads(p.read_text(encoding="utf-8"))) == 3


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
    raw = json.dumps([{"name": "D00001", "doc_no": "測試甲", "notifications": True}])
    p.write_text(raw, encoding="utf-8")                     # 姓名代號寫反的舊檔
    assert _codes(aps.load_doctors_settings(str(p))) == ["D00001"]
    assert p.read_text(encoding="utf-8") == raw             # 備份一個位元組都不動
