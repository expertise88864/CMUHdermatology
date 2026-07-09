# -*- coding: utf-8 -*-
"""atomic_io 測試（基本 round-trip）。"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common import atomic_io as aio  # noqa: E402
from cmuh_common.atomic_io import (  # noqa: E402
    atomic_write_json,
    atomic_write_text,
    safe_load_json,
)


def test_atomic_write_json_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "data.json")
        atomic_write_json(p, {"a": 1, "中文": "OK"})
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        assert data == {"a": 1, "中文": "OK"}


def test_atomic_write_json_creates_parent_dir_and_cleans_tmp():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "nested", "data.json")
        atomic_write_json(p, {"ok": True}, indent=2)
        with open(p, encoding="utf-8") as f:
            assert json.load(f) == {"ok": True}
        leftovers = [n for n in os.listdir(os.path.dirname(p))
                     if n.endswith(".tmp")]
        assert leftovers == []


def test_atomic_write_json_retries_transient_replace_failure(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "data.json")
        real_replace = aio.os.replace
        calls = []
        sleeps = []

        def flaky_replace(src, dst):
            calls.append((src, dst))
            if len(calls) < 3:
                raise PermissionError(5, "Access is denied", dst)
            return real_replace(src, dst)

        monkeypatch.setattr(aio.os, "replace", flaky_replace)
        monkeypatch.setattr(aio.time, "sleep", sleeps.append)

        atomic_write_json(p, {"ok": True})

        with open(p, encoding="utf-8") as f:
            assert json.load(f) == {"ok": True}
        assert len(calls) == 3
        assert sleeps == list(aio._FILE_OP_RETRY_DELAYS_SEC[:2])


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


def test_atomic_write_text_creates_parent_dir_and_cleans_tmp():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "nested", "code.py")
        assert atomic_write_text(p, "print('ok')\n") is True
        with open(p, encoding="utf-8") as f:
            assert f.read() == "print('ok')\n"
        leftovers = [n for n in os.listdir(os.path.dirname(p))
                     if n.endswith(".tmp")]
        assert leftovers == []


def test_safe_load_json_backs_up_corrupt_file():
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "broken.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{not valid json")

        assert safe_load_json(p, default={"fallback": True}) == {"fallback": True}
        assert not os.path.exists(p)
        backups = [n for n in os.listdir(tmp)
                   if n.startswith("broken.json.corrupt-")]
        assert len(backups) == 1


def test_safe_load_json_keeps_existing_same_second_corrupt_backup(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "broken.json")
        first_backup = p + ".corrupt-20260601_010203"
        with open(first_backup, "w", encoding="utf-8") as f:
            f.write("older evidence")
        with open(p, "w", encoding="utf-8") as f:
            f.write("{new broken json")

        monkeypatch.setattr("cmuh_common.atomic_io.time.strftime",
                            lambda _fmt: "20260601_010203")

        assert safe_load_json(p, default={}) == {}
        with open(first_backup, encoding="utf-8") as f:
            assert f.read() == "older evidence"
        with open(first_backup + "-1", encoding="utf-8") as f:
            assert f.read() == "{new broken json"


if __name__ == "__main__":
    test_atomic_write_json_roundtrip()
    test_atomic_write_json_creates_parent_dir_and_cleans_tmp()
    test_atomic_write_text_creates_bak()
    test_atomic_write_text_creates_parent_dir_and_cleans_tmp()
    test_safe_load_json_backs_up_corrupt_file()
    print("[OK] atomic_io tests passed")


# ═══ AB-04：safe_load_json_ex 區分暫時鎖住 vs 損壞 ═══════════════════════════
def test_safe_load_json_ex_status_missing_ok_corrupt():
    import glob
    from cmuh_common.atomic_io import safe_load_json_ex
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "c.json")
        # missing
        assert safe_load_json_ex(p, {"d": 1}) == ({"d": 1}, "missing")
        # ok
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"a": 1}')
        assert safe_load_json_ex(p, {}) == ({"a": 1}, "ok")
        # corrupt → backup 壞檔並回 default
        with open(p, "w", encoding="utf-8") as f:
            f.write("{bad json")
        val, status = safe_load_json_ex(p, {"d": 2})
        assert val == {"d": 2} and status == "corrupt"
        assert not os.path.exists(p)                 # 壞檔已 rename
        assert glob.glob(p + ".corrupt-*")


def test_safe_load_json_ex_status_error_keeps_file(monkeypatch):
    from cmuh_common import atomic_io as _aio
    from cmuh_common.atomic_io import safe_load_json_ex
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "locked.json")
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"a": 1}')
        before = open(p, "rb").read()

        def boom(*_a, **_k):
            raise PermissionError("locked by AV")
        monkeypatch.setattr(_aio, "open", boom, raising=False)
        val, status = safe_load_json_ex(p, {"fallback": True})
        assert status == "error" and val == {"fallback": True}
        # 原檔完好、未被 backup/刪除
        assert open(p, "rb").read() == before


def test_safe_load_json_wrapper_unchanged():
    """safe_load_json 契約不變（只丟掉 status）。"""
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "x.json")
        assert safe_load_json(p, default={"d": 1}) == {"d": 1}
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"a": 2}')
        assert safe_load_json(p, default={}) == {"a": 2}
