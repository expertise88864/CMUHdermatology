# -*- coding: utf-8 -*-
"""Abbreviation default migration, sorting, and length guard tests."""
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import abbrev_engine as ae  # noqa: E402


def _default_map() -> dict[str, str]:
    return {item["abbrev"]: item["expansion"] for item in ae.DEFAULT_ITEMS}


def test_requested_default_abbreviations_are_present():
    defaults = _default_map()

    assert defaults["st"] == "keep stable"
    assert defaults["sd"] == "seborrheic dermatitis"
    assert defaults["mf"] == "medication and follow up"
    assert defaults["cert"] == \
        "患者因上述皮膚疾病，曾於da至本院皮膚科門診就醫治療，建議持續追蹤。"


def test_cert_default_renders_dynamic_visit_date():
    template = _default_map()["cert"]

    assert ae.render_expansion(template, datetime(2026, 5, 31)) == \
        "患者因上述皮膚疾病，曾於(2026/5/31)至本院皮膚科門診就醫治療，建議持續追蹤。"


def test_old_config_adds_new_defaults_once_and_preserves_custom_text(tmp_path):
    path = tmp_path / "abbrev_settings.json"
    path.write_text(json.dumps({
        "enabled": True,
        "items": [
            {"abbrev": "st", "expansion": "my custom stable text"},
            {"abbrev": "zz", "expansion": "custom"},
        ],
    }), encoding="utf-8")

    cfg = ae.load_config(str(path))
    values = {item["abbrev"]: item["expansion"] for item in cfg.items}
    saved = json.loads(path.read_text(encoding="utf-8"))

    assert cfg.schema_version == ae.ABBREV_CONFIG_SCHEMA_VERSION
    assert values["st"] == "my custom stable text"
    assert values["sd"] == "seborrheic dermatitis"
    assert values["mf"] == "medication and follow up"
    assert values["cert"].startswith("患者因上述皮膚疾病")
    assert saved["schema_version"] == ae.ABBREV_CONFIG_SCHEMA_VERSION
    assert [item["abbrev"] for item in saved["items"]] == sorted(
        item["abbrev"] for item in saved["items"])


def test_current_schema_does_not_restore_manually_deleted_default(tmp_path):
    path = tmp_path / "abbrev_settings.json"
    path.write_text(json.dumps({
        "schema_version": ae.ABBREV_CONFIG_SCHEMA_VERSION,
        "enabled": True,
        "items": [{"abbrev": "zz", "expansion": "custom"}],
    }), encoding="utf-8")

    cfg = ae.load_config(str(path))

    assert cfg.items == [{"abbrev": "zz", "expansion": "custom"}]


def test_to_dict_sorts_abbreviations_case_insensitively():
    cfg = ae.AbbrevConfig(items=[
        {"abbrev": "zz", "expansion": "3"},
        {"abbrev": "Aa", "expansion": "1"},
        {"abbrev": "bb", "expansion": "2"},
    ])

    assert [item["abbrev"] for item in cfg.to_dict()["items"]] == [
        "Aa", "bb", "zz",
    ]


def test_engine_skips_overlong_abbreviation(monkeypatch):
    class FakeKeyboard:
        def on_press(self, _callback):
            return object()

        def unhook(self, _hook):
            pass

    monkeypatch.setattr(ae, "detect_external_expander", lambda: None)
    engine = ae.AbbrevEngine(FakeKeyboard())
    overlong = "x" * (ae.MAX_ABBREV_LENGTH + 1)

    engine.install(ae.AbbrevConfig(enabled=True, items=[
        {"abbrev": "ok", "expansion": "kept"},
        {"abbrev": overlong, "expansion": "skipped"},
    ]))

    assert engine._lookup == {"ok": "kept"}
    assert engine._max_abbrev_len == 2


def test_replace_cooldown_timer_waits_only_remaining_time():
    import inspect

    source = inspect.getsource(ae.AbbrevEngine._do_replace)

    assert "self._cooldown_until - time.monotonic()" in source
    assert "threading.Timer(self.COOLDOWN_SEC" not in source


def test_replace_clears_suppress_when_cooldown_timer_cannot_start(monkeypatch):
    class FakeKeyboard:
        def send(self, _key):
            pass

        def write(self, _text):
            pass

    class BrokenTimer:
        def __init__(self, *_args, **_kwargs):
            self.daemon = False

        def start(self):
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr(ae, "_clipboard_get_text", lambda: None)
    monkeypatch.setattr(ae, "_clipboard_set_text", lambda _text: False)
    monkeypatch.setattr(ae.threading, "Timer", BrokenTimer)
    monkeypatch.setattr(ae.AbbrevEngine, "PRE_BACKSPACE_DELAY_SEC", 0)
    engine = ae.AbbrevEngine(FakeKeyboard())
    engine._suppressing = True
    engine._cooldown_until = ae.time.monotonic() + 10

    engine._do_replace(2, "expanded", "ok")

    assert engine._suppressing is False
