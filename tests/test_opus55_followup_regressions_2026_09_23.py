import inspect

from cmuh_common.roster.service import RosterService
from cmuh_common.roster.storage import RosterStorage
from cmuh_common.roster.ui import day_tab
from cmuh_common.roster.ui.day_tab import DayScheduleTab


def test_every_day_solve_uses_dependency_installing_background_path(monkeypatch):
    tab = object.__new__(DayScheduleTab)
    tab._finalized = False
    tab._day_solving = False
    calls = []
    monkeypatch.setattr(day_tab, "_verify_day_solver_dependency", lambda: None)
    tab._solve_day_async = lambda: calls.append("async")

    tab._on_auto()

    assert calls == ["async"]
    source = inspect.getsource(DayScheduleTab._on_auto)
    assert "run_day_solve" not in source
    assert "build_day_input" not in source


def test_malformed_previous_vs_duty_cell_is_ignored(tmp_path):
    storage = RosterStorage(str(tmp_path))
    storage.save_config({
        "r_members": [],
        "vs_members": [{"id": "A", "name": "A"}],
    })
    storage.save_month("2026-08", {
        "vs_duty": {
            "2026-08-01": "malformed",
            "2026-08-08": {"person": 7},
        },
    })

    ctx = RosterService(storage).build_context("vs", "2026-09")

    assert ctx.prev_weekend_counts == {"7": 1}
