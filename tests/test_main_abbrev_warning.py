# -*- coding: utf-8 -*-
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402


def test_abbrev_external_conflict_warning_is_single_shot(monkeypatch):
    calls = []

    class FakeRoot:
        def after(self, delay, callback):
            calls.append(("after", delay))
            callback()

    app = main.AutomationApp.__new__(main.AutomationApp)
    app.root = FakeRoot()

    def fake_warning(title, message, parent=None):
        calls.append(("warning", title, message, parent))

    monkeypatch.setattr(main.messagebox, "showwarning", fake_warning)

    app._maybe_warn_abbrev_external_conflict("phraseexpress.exe")
    app._maybe_warn_abbrev_external_conflict("phraseexpress.exe")

    warnings = [c for c in calls if c[0] == "warning"]
    assert len(warnings) == 1
    assert "phraseexpress.exe" in warnings[0][2]


def test_abbrev_external_conflict_warning_ignores_empty_ext(monkeypatch):
    calls = []
    app = main.AutomationApp.__new__(main.AutomationApp)
    app.root = object()
    monkeypatch.setattr(
        main.messagebox,
        "showwarning",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    app._maybe_warn_abbrev_external_conflict(None)

    assert calls == []
