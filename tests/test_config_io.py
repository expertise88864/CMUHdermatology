# -*- coding: utf-8 -*-
"""config_io helpers."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.config_io import (  # noqa: E402
    load_json_dict,
    load_json_list,
    normalize_doctor_rows,
)


def test_load_json_dict_merges_defaults():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "cfg.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"b": 3}, f)

        assert load_json_dict(p, {"a": 1, "b": 2}) == {"a": 1, "b": 3}


def test_load_json_dict_bad_type_returns_default_copy():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "cfg.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(["wrong"], f)

        default = {"nested": {"x": 1}}
        result = load_json_dict(p, default)
        result["nested"]["x"] = 2
        assert default["nested"]["x"] == 1


def test_load_json_list_backs_up_corrupt_file():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "items.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{broken")

        assert load_json_list(p, [{"fallback": True}]) == [{"fallback": True}]
        backups = [n for n in os.listdir(tmp)
                   if n.startswith("items.json.corrupt-")]
        assert len(backups) == 1


def test_normalize_doctor_rows_repairs_swapped_fields():
    rows, changed = normalize_doctor_rows([
        {"name": "D12345", "doc_no": "王小明", "notifications": True},
    ])

    assert changed is True
    assert rows == [{"name": "王小明", "doc_no": "D12345", "notifications": True}]


def test_normalize_doctor_rows_drops_bad_rows_and_uses_default_when_empty():
    default = [{"name": "預設", "doc_no": "D1"}]
    rows, changed = normalize_doctor_rows(["bad"], default)

    assert changed is True
    assert rows == default
    rows[0]["name"] = "改掉"
    assert default[0]["name"] == "預設"
