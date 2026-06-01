# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.memory_cache import trim_oldest_entries  # noqa: E402


def test_trim_oldest_entries_uses_tuple_timestamp_by_default():
    store = {
        "old": (1.0, "a"),
        "new": (3.0, "c"),
        "middle": (2.0, "b"),
    }

    assert trim_oldest_entries(store, 2) == 1
    assert store == {
        "new": (3.0, "c"),
        "middle": (2.0, "b"),
    }


def test_trim_oldest_entries_supports_scalar_timestamp():
    store = {"old": 1.0, "new": 3.0, "middle": 2.0}

    assert trim_oldest_entries(
        store, 1, timestamp_of=lambda stamp: stamp,
    ) == 2
    assert store == {"new": 3.0}


def test_trim_oldest_entries_removes_malformed_rows_first():
    store = {"bad": object(), "good": (2.0, "ok")}

    assert trim_oldest_entries(store, 1) == 1
    assert store == {"good": (2.0, "ok")}
