# -*- coding: utf-8 -*-
"""atomic_io 測試（基本 round-trip）。"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.atomic_io import atomic_write_json, atomic_write_text  # noqa: E402


def test_atomic_write_json_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "data.json")
        atomic_write_json(p, {"a": 1, "中文": "OK"})
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        assert data == {"a": 1, "中文": "OK"}


def test_atomic_write_text_creates_bak():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "code.py")
        # 第一次寫入：無 .bak
        atomic_write_text(p, "v1")
        assert not os.path.exists(p + ".bak")
        # 第二次寫入：應產生 .bak
        atomic_write_text(p, "v2")
        with open(p, encoding="utf-8") as f:
            assert f.read() == "v2"
        with open(p + ".bak", encoding="utf-8") as f:
            assert f.read() == "v1"


if __name__ == "__main__":
    test_atomic_write_json_roundtrip()
    test_atomic_write_text_creates_bak()
    print("[OK] atomic_io tests passed")
