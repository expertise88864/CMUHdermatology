"""Exercise the real startup title without starting monitors or network work."""
from types import SimpleNamespace

import pytest

import main


@pytest.mark.parametrize("version, display", [
    ("2026.10.12.1", "2026.10.12.01"),
    ("2026.10.12.01", "2026.10.12.01"),
    ("2027.01.02.12", "2027.01.02.12"),
    ("2027.01.02.100", "2027.01.02.100"),
])
def test_main_title_is_program_name_and_version_only(monkeypatch, version, display):
    class TitleSet(Exception):
        pass

    def stop_after_title(_root):
        raise TitleSet

    titles = []
    root = SimpleNamespace(title=titles.append)
    monkeypatch.setattr(main, "CURRENT_VERSION", version)
    monkeypatch.setattr(main, "_ensure_network_imports", lambda: None)
    monkeypatch.setattr(main, "place_tk_window_on_preferred_monitor", stop_after_title)
    with pytest.raises(TitleSet):
        main.AutomationApp(root, {})
    assert titles == [f"中國醫皮膚科主程式 v{display}"]
