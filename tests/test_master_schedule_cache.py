# -*- coding: utf-8 -*-
"""master_schedule_cache helpers."""
import json
import os
import sys
import tempfile
import time
from queue import Queue

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.master_schedule_cache import (  # noqa: E402
    load_master_schedule_cache,
    normalize_master_schedule,
    refresh_master_schedule_if_needed,
)
from cmuh_common.ui_messages import UiMasterScheduleMessage  # noqa: E402


def test_normalize_master_schedule_skips_bad_weekday_keys():
    raw = {
        "Dr A": {"1": [{"session": "上午"}], "bad": [{"session": "下午"}]},
        "Bad": ["wrong"],
    }

    assert normalize_master_schedule(raw) == {"Dr A": {1: [{"session": "上午"}]}}


def test_load_master_schedule_cache_uses_safe_loader():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "master.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{broken")

        assert load_master_schedule_cache(path) == {}
        assert any(name.startswith("master.json.corrupt-") for name in os.listdir(tmp))


def test_refresh_master_schedule_skips_fresh_cache():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "master.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"Dr A": {"1": []}}, f)
        q = Queue()

        status = refresh_master_schedule_if_needed(
            q,
            lambda: (_ for _ in ()).throw(AssertionError("should not fetch")),
            path,
            fresh_seconds=60,
            ttl_seconds=3600,
        )

        assert status == "fresh"
        assert q.empty()


def test_refresh_master_schedule_emits_only_when_changed():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "master.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"Dr A": {"1": []}}, f)
        old_time = time.time() - 120
        os.utime(path, (old_time, old_time))
        q = Queue()

        unchanged = refresh_master_schedule_if_needed(
            q,
            lambda: {"Dr A": {1: []}},
            path,
            fresh_seconds=1,
            ttl_seconds=3600,
        )
        os.utime(path, (old_time, old_time))
        changed = refresh_master_schedule_if_needed(
            q,
            lambda: {"Dr A": {1: [{"session": "上午"}]}},
            path,
            fresh_seconds=1,
            ttl_seconds=3600,
        )

        assert unchanged == "unchanged"
        assert changed == "updated"
        msg = q.get_nowait()
        assert isinstance(msg, UiMasterScheduleMessage)
        assert msg.schedule == {"Dr A": {1: [{"session": "上午"}]}}
