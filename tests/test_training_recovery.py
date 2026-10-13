"""Malformed synced teaching rules stay editable without relaxing write guards."""
import json
from types import SimpleNamespace

import pytest

from cmuh_common.roster.service import RosterService
from cmuh_common.roster.storage import RosterStorage, StaleRosterDataError
from cmuh_common.roster.training_params import TrainingParams, TrainingParamsError
from cmuh_common.roster.ui.day_tab import DayScheduleTab
from cmuh_common.roster.ui.settings import SettingsTab


def broken_service(tmp_path, raw, **extra):
    storage = RosterStorage(str(tmp_path))
    cfg = {"training_params": raw, "pgy_members": [{"id": "P1"}],
           "room_capacity": 3, "future_config": {"keep": True}, **extra}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg), encoding="utf-8")
    return RosterService(storage), path


@pytest.mark.parametrize("raw", [None, [], {"pgy_high": 11},
                                {"pgy_low": 3}, {"pgy_high": "2"}])
def test_invalid_rules_open_settings_and_day_then_recover(tk_root, tmp_path, raw):
    svc, path = broken_service(tmp_path, raw)
    before = path.read_bytes()
    settings = SettingsTab(tk_root, svc)
    day = DayScheduleTab(tk_root, svc, SimpleNamespace(ym="2026-10"))
    try:
        editor = settings._training
        assert editor._invalid
        assert "無效" in editor._status.get()
        if isinstance(raw, dict):
            for k, v in raw.items():
                assert editor.variables[k].get() == str(v)
        else:
            assert all(v.get() == "" for v in editor.variables.values())
        assert not day._tree.get_children()
        assert "教學排班參數無效" in day._warns.get(0)
        assert path.read_bytes() == before
        with pytest.raises(TrainingParamsError):
            svc.build_day_input("2026-10")
        with pytest.raises(ValueError):
            svc.run_day_solve("2026-10")
        editor.defaults()
        assert path.read_bytes() == before
        editor.save()
        assert not editor._invalid and editor._status.get() == "已儲存"
        assert svc.build_day_input("2026-10").training == TrainingParams()
        day.refresh()
        assert "教學排班參數無效" not in day._warns.get(0)
        cfg = svc.storage.load_config()
        assert cfg["pgy_members"] == [{"id": "P1"}]
        assert cfg["room_capacity"] == 3 and cfg["future_config"] == {"keep": True}
    finally:
        day.destroy()
        settings.destroy()


def test_repair_preserves_unknown_rules_and_finalized_month(tmp_path):
    raw = {"pgy_high": 11, "future_rule": {"value": 8}}
    svc, _ = broken_service(tmp_path, raw)
    month = svc.storage.load_month("2026-10")
    month.update(finalized=True, day_slots={"2026-10-05": {"上午": {"101": ["P1"]}}})
    svc.storage.save_month("2026-10", month)
    before = svc.storage.month_revision("2026-10")
    svc.repair_training_params(TrainingParams().to_dict(), baseline=raw)
    assert svc.storage.load_config()["training_params"]["future_rule"] == {"value": 8}
    assert svc.storage.month_revision("2026-10") == before


@pytest.mark.parametrize("extra", [{"pgy_members": [None]}, {"schema_version": "broken"}])
def test_repair_cannot_bypass_other_invalid_config(tmp_path, extra):
    svc, path = broken_service(tmp_path, None, **extra)
    before = path.read_bytes()
    with pytest.raises(ValueError):
        svc.repair_training_params(TrainingParams().to_dict(), baseline=None)
    assert path.read_bytes() == before


def test_repair_rejects_newer_rules_and_cas_race(tmp_path, monkeypatch):
    svc, path = broken_service(tmp_path, {"pgy_high": 11})
    desired = TrainingParams().to_dict()
    before = path.read_bytes()
    with pytest.raises(StaleRosterDataError):
        svc.repair_training_params(desired, baseline=None)
    assert path.read_bytes() == before
    original = svc.storage.save_config
    def race(cfg, **kwargs):
        current = json.loads(path.read_text(encoding="utf-8"))
        current["room_capacity"] = 4
        path.write_text(json.dumps(current), encoding="utf-8")
        original(cfg, **kwargs)
    monkeypatch.setattr(svc.storage, "save_config", race)
    with pytest.raises(StaleRosterDataError):
        svc.repair_training_params(desired, baseline={"pgy_high": 11})
    current = json.loads(path.read_text(encoding="utf-8"))
    assert current["room_capacity"] == 4
    assert current["training_params"] == {"pgy_high": 11}


@pytest.mark.parametrize("contents", ["", "{bad json", "[]"])
def test_repair_rejects_corrupted_file(tmp_path, contents):
    svc, path = broken_service(tmp_path, None)
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError):
        svc.repair_training_params(TrainingParams().to_dict(), baseline=None)
    assert path.read_text(encoding="utf-8") == contents


def test_invalid_refresh_clears_old_calendar_and_recovers(tk_root, tmp_path):
    svc, path = broken_service(tmp_path, {})
    tab = DayScheduleTab(tk_root, svc, SimpleNamespace(ym="2026-10"))
    try:
        tab._tree.insert("", "end", iid="stale")
        cfg = svc.storage.load_config()
        cfg["training_params"] = None
        path.write_text(json.dumps(cfg), encoding="utf-8")
        tab.refresh()
        assert not tab._tree.get_children()
        assert tab._calendar_render_key is None
        assert len(tab._cal_body.winfo_children()) == 1
        svc.repair_training_params(TrainingParams().to_dict(), baseline=None)
        tab.refresh()
        assert tab._calendar_render_key is not None
    finally:
        tab.destroy()
