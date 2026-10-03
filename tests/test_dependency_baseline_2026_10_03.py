"""Baseline closure, fail-closed validation and version-only output."""
from importlib import metadata
from types import SimpleNamespace

import pytest

from scripts import dependency_baseline as baseline


def _installed(mapping):
    def distribution(name):
        if name not in mapping:
            raise metadata.PackageNotFoundError(name)
        version, requires = mapping[name]
        return SimpleNamespace(version=version, requires=requires)
    return distribution


def test_only_active_runtime_dependency_closure_is_recorded():
    installed = _installed({
        "root": ("1.2", ["child>=2", "unix; sys_platform == 'linux'", "test-only; extra == 'test'"]),
        "child": ("2.1", []),
        "unrelated-installed": ("999", []),
    })
    result = baseline.dependency_closure(["root>=1,<2"], distribution=installed,
                                         environment={"sys_platform": "win32"})
    assert result == {"child": "2.1", "root": "1.2"}
    assert baseline.constraints_text(result).splitlines()[1:] == ["child==2.1", "root==1.2"]


def test_extras_are_propagated_even_when_a_package_was_expanded_before():
    installed = _installed({
        "root": ("1", ["child[export]", "child"]),
        "child": ("2", ["leaf; extra == 'export'"]),
        "leaf": ("3", ["root"]),  # A cycle must terminate.
    })
    assert baseline.dependency_closure(["root"], distribution=installed) == {
        "child": "2", "leaf": "3", "root": "1"}


@pytest.mark.parametrize("requirements,mapping", [
    (["missing"], {}),
    (["root>=2"], {"root": ("1", [])}),
    (["root"], {"root": ("1", ["missing"])}),
    (["root"], {"root": ("1", ["child>=3", "child<3"]), "child": ("2", [])}),
])
def test_incomplete_or_incompatible_environment_is_not_a_baseline(requirements, mapping):
    with pytest.raises(ValueError):
        baseline.dependency_closure(requirements, distribution=_installed(mapping))


@pytest.mark.parametrize("private", ["private @ https://user:secret@example.invalid/private.whl", "!private-secret"])
def test_private_origins_and_invalid_input_are_not_echoed(private):
    with pytest.raises(ValueError) as error:
        baseline.dependency_closure(["root"], distribution=_installed({"root": ("1", [private])}))
    assert "secret" not in str(error.value)
    assert "example.invalid" not in str(error.value)


def test_root_markers_and_manifest_fingerprints_are_retained(tmp_path):
    (tmp_path / "requirements.txt").write_text("root>=1\nother; python_version < '2'\n", encoding="ascii")
    (tmp_path / "requirements-lazy.txt").write_text("export==2 # lazy\n", encoding="ascii")
    declarations, hashes = baseline.read_manifests(tmp_path)
    assert declarations == ["root>=1", "export==2"]
    assert set(hashes) == set(baseline.MANIFESTS)
    assert all(len(value) == 64 for value in hashes.values())


def test_broken_tcl_fails_without_echoing_private_search_paths(tmp_path, monkeypatch, capsys):
    def broken(root):
        raise baseline.tkinter.TclError("private-user/private-Tcl-search-path")

    monkeypatch.setattr(baseline, "snapshot", broken)
    output = tmp_path / "record.json"
    constraints = tmp_path / "constraints.txt"
    assert baseline.main(["--output", str(output), "--constraints", str(constraints)]) == 1
    captured = capsys.readouterr()
    assert "baseline failed" in captured.err
    assert "private" not in captured.err
    assert not output.exists() and not constraints.exists()
