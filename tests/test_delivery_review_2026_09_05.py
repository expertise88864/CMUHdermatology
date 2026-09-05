"""Release tooling must work in Git worktrees and ship dependency manifests."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from cmuh_common.deps_manifest import _REQUIREMENTS_FILES


ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("returncode", [0, 1])
def test_commit_message_supports_gitfile_and_is_cleaned(tmp_path, monkeypatch, returncode):
    helper = load_script("push_helper")
    monkeypatch.setattr(helper, "REPO_ROOT", tmp_path)
    # Linked worktrees have a .git FILE, not a directory.
    gitfile = "gitdir: ../repo/.git/worktrees/review\n"
    (tmp_path / ".git").write_text(gitfile, encoding="utf-8")
    seen = []
    message = "修正 🧪\n\nClaude-Opus-5-Review: pending\nClaude-Opus-5-Review-Effort: high"

    def run(cmd, **kwargs):
        assert cmd[:3] == ["git", "commit", "-F"]
        path = Path(cmd[3])
        assert path.read_text(encoding="utf-8") == message
        seen.append(path)
        return SimpleNamespace(returncode=returncode)

    monkeypatch.setattr(helper, "run", run)
    if returncode:
        with pytest.raises(SystemExit):
            helper.step5_commit(message, "2026.09.05.1")
    else:
        helper.step5_commit(message, "2026.09.05.1")
    assert seen and not seen[0].exists()
    assert (tmp_path / ".git").read_text(encoding="utf-8") == gitfile


def test_all_runtime_dependency_manifests_are_shipped():
    generator = load_script("sync_manifest")
    generated = {entry["remote_path"] for entry in generator.collect_entries("0")}
    published = {entry["remote_path"] for entry in
                 json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))["files"]}
    for filename in _REQUIREMENTS_FILES:
        assert filename in generated, f"generator omits runtime manifest {filename}"
        assert filename in published, f"published manifest omits {filename}"
