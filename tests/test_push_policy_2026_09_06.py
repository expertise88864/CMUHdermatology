"""Latest user policy: validate the final commit, never bypass local CI."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def ph():
    spec = importlib.util.spec_from_file_location(
        "push_policy_under_test", Path(__file__).parents[1] / "scripts/push_helper.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("args", [["--emergency", "reason"], ["--emergency=reason"]])
def test_cli_refuses_even_explicit_emergency(ph, args):
    with pytest.raises(SystemExit):
        ph.parse_args(["push", "message", *args])


def test_direct_quality_gate_cannot_bypass(ph, monkeypatch):
    monkeypatch.setattr(ph.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(ph, "_clean_gate_artifacts", lambda: None)
    monkeypatch.setattr(ph.subprocess, "run", lambda *a, **k: pytest.fail("must reject first"))
    with pytest.raises(SystemExit):
        ph.step_quality_gate("reason")


def test_direct_commit_cannot_record_a_bypass(ph, monkeypatch):
    monkeypatch.setattr(ph, "run", lambda *a, **k: pytest.fail("must reject before git"))
    with pytest.raises(SystemExit):
        ph.step5_commit("message", "version", "reason")


@pytest.fixture
def pipeline(ph, monkeypatch, tmp_path):
    version = tmp_path / "src/cmuh_common/version.py"
    version.parent.mkdir(parents=True)
    version.write_text("", encoding="utf-8")
    monkeypatch.setattr(ph, "REPO_ROOT", tmp_path)
    state = {"events": [], "sha": "a" * 40, "status": b""}
    events = state["events"]
    monkeypatch.setattr(ph, "step1_sanity", lambda: None)
    monkeypatch.setattr(ph, "step2_check_changes", lambda: True)
    monkeypatch.setattr(ph, "snapshot_tracked_sources", lambda: {})
    monkeypatch.setattr(ph, "worktree_blob_ids", lambda **kwargs: {})
    monkeypatch.setattr(ph, "verify_unchanged_since_tests", lambda fp: None)
    monkeypatch.setattr(ph, "verify_index_matches", lambda fp: events.append("index"))
    monkeypatch.setattr(ph, "verify_staged_version_consistency", lambda v: None)
    monkeypatch.setattr(ph, "verify_staged_manifest_hashes", lambda: None)
    monkeypatch.setattr(ph, "step3_bump_version", lambda: events.append("bump") or "v")
    monkeypatch.setattr(ph, "step4_sync_manifest", lambda v: events.append("manifest"))
    monkeypatch.setattr(ph, "step5_stage", lambda: events.append("stage"))
    monkeypatch.setattr(ph, "step5_commit", lambda *args: events.append("commit"))
    monkeypatch.setattr(ph, "step_quality_gate", lambda *args: events.append("gate"))
    def push(sha):
        assert sha == "a" * 40
        events.append("push")
    monkeypatch.setattr(ph, "step6_push", push)

    def git_bytes(args, stdin=b""):
        if args[:2] == ["rev-parse", "HEAD"]:
            return state["sha"].encode("ascii") + b"\n"
        if args[0] == "status":
            return state["status"]
        pytest.fail(f"unexpected git request: {args}")

    monkeypatch.setattr(ph, "_git_bytes", git_bytes)
    return state


def test_ci_runs_on_final_commit_before_push(ph, pipeline, capsys):
    assert ph.main(["push", "message"]) == 0
    events = pipeline["events"]
    assert events.index("manifest") < events.index("commit") < events.index("gate")
    assert events.index("gate") < events.index("push")
    assert "尚待 GitHub CI" in capsys.readouterr().out


def test_failed_final_gate_keeps_local_commit_and_never_pushes(ph, pipeline, monkeypatch):
    def fail_gate(*args):
        assert "commit" in pipeline["events"]
        raise SystemExit(1)
    monkeypatch.setattr(ph, "step_quality_gate", fail_gate)
    with pytest.raises(SystemExit):
        ph.main(["push", "message"])
    assert "push" not in pipeline["events"]


@pytest.mark.parametrize("drift", ["head", "tracked", "untracked", "staged"])
def test_revision_drift_after_ci_blocks_push(ph, pipeline, monkeypatch, drift):
    def gate(*args):
        if drift == "head":
            pipeline["sha"] = "b" * 40
        else:
            pipeline["status"] = {
                "tracked": b" M docs/policy.md\n", "untracked": b"?? new-config.json\n",
                "staged": b"M  .github/workflows/ci.yml\n",
            }[drift]
    monkeypatch.setattr(ph, "step_quality_gate", gate)
    with pytest.raises(SystemExit):
        ph.main(["push", "message"])
    assert "push" not in pipeline["events"]


@pytest.mark.parametrize("branch", ["main", "codex/review"])
def test_push_uses_exact_validated_sha_not_a_movable_branch(ph, monkeypatch, branch):
    sha = "a" * 40
    commands = []
    def run(cmd, **kwargs):
        commands.append(cmd)
        return SimpleNamespace(returncode=0, stdout=branch + "\n")
    monkeypatch.setattr(ph, "run", run)
    monkeypatch.setattr(ph, "_git_bytes", lambda args: sha.encode("ascii"))
    ph.step6_push(sha)
    assert commands[-1] == ["git", "push", "origin", f"{sha}:refs/heads/{branch}"]
    assert not any("--force" in cmd or "--no-verify" in cmd for cmd in commands)


def test_last_moment_head_change_is_not_pushed(ph, monkeypatch):
    commands = []
    monkeypatch.setattr(ph, "run", lambda cmd, **kwargs:
                        commands.append(cmd) or SimpleNamespace(returncode=0, stdout="main\n"))
    monkeypatch.setattr(ph, "_git_bytes", lambda args: b"b" * 40)
    with pytest.raises(SystemExit):
        ph.step6_push("a" * 40)
    assert not any(cmd[:2] == ["git", "push"] for cmd in commands)
