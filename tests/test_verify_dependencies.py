# -*- coding: utf-8 -*-
"""Bootstrap dependency verifier tests."""
import importlib.util
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_dependencies.py"
SPEC = importlib.util.spec_from_file_location("verify_dependencies", SCRIPT)
assert SPEC and SPEC.loader
verify_dependencies = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify_dependencies)


def test_find_import_failures_reports_module_and_error(monkeypatch):
    def fake_import(name):
        if name == "broken":
            raise ImportError("not installed")
        return object()

    monkeypatch.setattr(verify_dependencies.importlib, "import_module", fake_import)

    assert verify_dependencies.find_import_failures(("ok", "broken")) == [
        ("broken", "ImportError: not installed"),
    ]
