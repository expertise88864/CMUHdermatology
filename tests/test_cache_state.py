# -*- coding: utf-8 -*-
"""cache_state helpers."""
from collections import defaultdict
from datetime import date
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.cache_state import (  # noqa: E402
    build_master_schedule_index,
    convert_keys_to_str,
    decode_date_keys,
    save_json_cache,
)


def test_convert_and_decode_date_keys():
    day = date(2026, 5, 24)
    converted = convert_keys_to_str({day: [{"nested": {1: "ok"}}]})

    assert converted == {"2026-05-24": [{"nested": {"1": "ok"}}]}
    assert decode_date_keys({"2026-05-24": []}) == {day: []}


def test_save_json_cache_normalizes_keys_atomically():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "cache.json")

        save_json_cache(path, {date(2026, 5, 24): {2: "x"}})

        with open(path, "r", encoding="utf-8") as f:
            assert json.load(f) == {"2026-05-24": {"2": "x"}}


def test_build_master_schedule_index_filters_bad_rows():
    by_weekday, self_paid = build_master_schedule_index({
        "Dr A": {
            "1": [
                {"session": "上午", "is_self_paid": True},
                {"session": ""},
                "bad",
            ],
            "bad": [{"session": "下午"}],
        },
        "Bad": ["not dict"],
    })

    assert isinstance(by_weekday, defaultdict)
    assert by_weekday[1] == [("Dr A", "上午", True)]
    assert self_paid == {("Dr A", 1, "上午"): True}
