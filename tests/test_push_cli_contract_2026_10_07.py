"""Safe release entry points; all mutations are confined to anonymous repositories."""
import importlib.util
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def ph():
    path = Path(__file__).parents[1] / "scripts/push_helper.py"
    spec = importlib.util.spec_from_file_location("safe_push_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("args", [
    ["--sanity-only"], ["--chek"], ["check", "--publsh"],
    ["publish"], ["a legacy commit message"],
])
def test_invalid_arguments_stop_before_repository_access(ph, monkeypatch, args):
    monkeypatch.setattr(ph, "_git_bytes", lambda *a, **k: pytest.fail("accessed repository"))
    monkeypatch.setattr(ph, "run", lambda *a, **k: pytest.fail("launched command"))
    with pytest.raises(SystemExit) as error:
        ph.main(["push", *args])
    assert error.value.code != 0


@pytest.mark.parametrize("args", [[], ["--help"], ["check", "--help"], ["publish", "--help"]])
def test_help_does_not_access_repository(ph, monkeypatch, args, capsys):
    monkeypatch.setattr(ph, "_git_bytes", lambda *a, **k: pytest.fail("accessed repository"))
    monkeypatch.setattr(ph, "run", lambda *a, **k: pytest.fail("launched command"))
    try:
        result = ph.main(["push", *args])
    except SystemExit as error:
        result = error.code
    assert result == 0
    assert "usage:" in capsys.readouterr().out


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"}).stdout


def snapshot(root):
    files = {p.relative_to(root).as_posix(): p.read_bytes()
             for p in root.rglob("*") if p.is_file() and ".git" not in p.relative_to(root).parts}
    return files, (root / ".git/index").read_bytes(), git(root, "rev-parse", "HEAD"), git(
        root, "for-each-ref", "--format=%(refname) %(objectname)")


@pytest.fixture
def repo(ph, tmp_path, monkeypatch):
    root = tmp_path / "匿名 repository space"
    root.mkdir()
    git(root, "init", "-b", "codex/test-safe-release")
    git(root, "config", "user.name", "Anonymous Test")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "core.hooksPath", ".githooks")
    git(root, "config", "core.autocrlf", "false")
    for path, content in {
        ".gitignore": "settings/\n_originals/\n*.log\n.deps_cache\npython_embed/\n__pycache__/\n",
        ".gitattributes": "* text=auto eol=lf\n",
        ".githooks/pre-push": "#!/bin/sh\nexit 0\n",
        "src/cmuh_common/version.py": 'CURRENT_VERSION = "2020.01.01.1"\n',
        "src/選定 file.py": "value = 1\n",
        "docs/other.md": "original\n",
    }.items():
        dest = root / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(content, encoding="utf-8", newline="\n")
    (root / "scripts").mkdir()
    shutil.copyfile(Path(__file__).parents[1] / "scripts/sync_manifest.py",
                    root / "scripts/sync_manifest.py")
    entries = [{"remote_path": path, "sha256": hashlib.sha256((root / path).read_bytes()).hexdigest()}
               for path in ("src/cmuh_common/version.py", "src/選定 file.py")]
    (root / "manifest.json").write_text(json.dumps({"app_version": "2020.01.01.1", "files": entries}),
                                        encoding="utf-8")
    git(root, "add", "-A")
    git(root, "commit", "-m", "Anonymous baseline")
    git(root, "remote", "add", "origin", str(tmp_path / "unused-local-remote"))
    monkeypatch.setattr(ph, "REPO_ROOT", root)
    return root


@pytest.mark.parametrize("args", [[], ["--help"], ["--sanity-only"], ["check"], ["check", "--typo"]])
def test_readonly_and_invalid_modes_leave_actual_git_state_unchanged(ph, repo, args):
    (repo / "docs/other.md").write_text("staged\n", encoding="utf-8")
    git(repo, "add", "docs/other.md")
    (repo / "docs/other.md").write_text("unstaged\n", encoding="utf-8")
    (repo / "unrelated.txt").write_text("untracked\n", encoding="utf-8")
    before = snapshot(repo)
    try:
        ph.main(["push", *args])
    except SystemExit as error:
        assert error.code in (0, 2)
    assert snapshot(repo) == before


def test_check_accepts_runtime_staged_changes_without_generating_metadata(ph, repo, capsys):
    dirty_source(repo)
    before = snapshot(repo)
    assert ph.main(["push", "check"]) == 0
    assert snapshot(repo) == before
    output = capsys.readouterr().out
    assert "HEAD" in output and "src/選定 file.py" in output


@pytest.mark.parametrize("damage", ["version", "hash"])
def test_check_still_rejects_corrupt_committed_metadata(ph, repo, damage):
    manifest = json.loads((repo / "manifest.json").read_text(encoding="utf-8"))
    if damage == "version":
        manifest["app_version"] = "wrong-version"
    else:
        manifest["files"][1]["sha256"] = "0" * 64
    (repo / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    git(repo, "add", "manifest.json")
    git(repo, "commit", "-m", "Anonymous corrupt baseline")
    before = snapshot(repo)
    with pytest.raises(SystemExit):
        ph.main(["push", "check"])
    assert snapshot(repo) == before


def dirty_source(repo):
    selected = repo / "src/選定 file.py"
    selected.write_text("value = 2\n", encoding="utf-8")
    git(repo, "add", "src/選定 file.py")
    selected.write_text("value = 3\n", encoding="utf-8")
    (repo / "docs/other.md").write_text("unrelated staged\n", encoding="utf-8")
    git(repo, "add", "docs/other.md")
    (repo / "docs/other.md").write_text("unrelated unstaged\n", encoding="utf-8")
    (repo / "unrelated.txt").write_text("untracked\n", encoding="utf-8")


def publish_args(tmp_path, mode="index"):
    selection = (["--committed"] if mode == "committed" else
                 ["--path", "src/選定 file.py", "--source", mode])
    message = tmp_path / "中文 message.txt"
    message.write_text("修正 🧪\n\nClaude-Opus-5-Review: pending\nClaude-Opus-5-Review-Effort: high\n",
                       encoding="utf-8", newline="\n")
    return ["push", "publish", *selection, "--message-file", str(message),
            "--output", str(tmp_path / "candidate output")]


@pytest.mark.parametrize("policy", ["fix", "strip", "error"])
@pytest.mark.parametrize("mode", ["index", "worktree"])
def test_candidate_preserves_selected_whitespace_under_global_git_policy(
        ph, repo, tmp_path, monkeypatch, policy, mode):
    config = tmp_path / "anonymous global git config"
    config.write_text(f"[apply]\n\twhitespace = {policy}\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    content = 'value = """\nkeep two spaces  \n"""\n'
    (repo / "src/選定 file.py").write_text(content, encoding="utf-8", newline="\n")
    git(repo, "add", "src/選定 file.py")
    before = snapshot(repo)
    patch = ph._selection_patch(["src/選定 file.py"], mode)
    options = ph.parse_args(publish_args(tmp_path, mode))
    with ph._candidate_copy(options, "codex/test-safe-release") as (_, _, verify_source):
        assert git(ph.REPO_ROOT, "show", ":src/選定 file.py") == content.encode("utf-8")
        assert git(ph.REPO_ROOT, "diff", "--no-ext-diff", "--no-textconv", "--cached",
                   "--binary", "--full-index", "--no-renames", "HEAD") == patch
        verify_source()
    assert snapshot(repo) == before
    assert config.read_text(encoding="utf-8") == f"[apply]\n\twhitespace = {policy}\n"


@pytest.mark.parametrize("count", [1, 2])
def test_candidate_preserves_effective_push_urls(ph, repo, tmp_path, count):
    urls = [str(tmp_path / f"explicitly-disabled-push-{i}") for i in range(count)]
    for url in urls:
        git(repo, "remote", "set-url", "--add", "--push", "origin", url)
    dirty_source(repo)
    before = snapshot(repo)
    expected = git(repo, "remote", "get-url", "--push", "--all", "origin")
    fetch_url = git(repo, "remote", "get-url", "origin")
    with ph._candidate_copy(ph.parse_args(publish_args(tmp_path)), "codex/test-safe-release"):
        assert git(ph.REPO_ROOT, "remote", "get-url", "--push", "--all", "origin") == expected
        assert git(ph.REPO_ROOT, "remote", "get-url", "origin") == fetch_url
    assert snapshot(repo) == before


@pytest.mark.parametrize("mode", ["index", "worktree"])
@pytest.mark.parametrize("settings", [
    {"diff.mnemonicPrefix": "true"},
    {"diff.srcPrefix": "old/", "diff.dstPrefix": "new/"},
    {"core.quotePath": "false"},
    {"diff.context": "0", "diff.algorithm": "histogram"},
    {"diff.noprefix": "true", "diff.suppressBlankEmpty": "true"},
])
def test_candidate_exact_patch_is_independent_of_local_diff_format(
        ph, repo, tmp_path, mode, settings):
    path = repo / "src/選定 file.py"
    baseline = "before = 1\n\nvalue = 1\n\nafter = 1\n"
    path.write_text(baseline, encoding="utf-8", newline="\n")
    git(repo, "add", "src/選定 file.py")
    git(repo, "commit", "-m", "Anonymous multiline baseline")
    for key, value in settings.items():
        git(repo, "config", key, value)
    selected = baseline.replace("value = 1", "value = 2")
    path.write_text(selected, encoding="utf-8", newline="\n")
    git(repo, "add", "src/選定 file.py")
    path.write_text(selected.replace("value = 2", "value = 3"), encoding="utf-8", newline="\n")
    before = snapshot(repo)
    config = git(repo, "config", "--local", "--list")
    expected = selected if mode == "index" else selected.replace("value = 2", "value = 3")
    with ph._candidate_copy(ph.parse_args(publish_args(tmp_path, mode)), "codex/test-safe-release"):
        assert git(ph.REPO_ROOT, "show", ":src/選定 file.py") == expected.encode("utf-8")
    assert snapshot(repo) == before
    assert git(repo, "config", "--local", "--list") == config


@pytest.mark.parametrize("rewrite", ["fetch", "push"])
def test_candidate_rejects_effective_remote_url_rewrite_before_safety_gate(
        ph, repo, tmp_path, monkeypatch, capsys, rewrite):
    config = tmp_path / "anonymous global git config"
    content = ('[url "https://mirror-one.invalid/"]\n'
               '\tinsteadOf = https://origin.invalid/\n'
               '[url "https://mirror-two.invalid/"]\n'
               '\tinsteadOf = https://mirror-one.invalid/\n')
    config.write_text(content, encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    if rewrite == "fetch":
        git(repo, "remote", "set-url", "origin", "https://origin.invalid/PRIVATE_TEST")
    else:
        git(repo, "remote", "set-url", "--add", "--push", "origin", str(tmp_path / "disabled-push"))
        git(repo, "remote", "set-url", "--add", "--push", "origin", "https://origin.invalid/PRIVATE_TEST")
    dirty_source(repo)
    before = snapshot(repo)
    expected = b"https://mirror-one.invalid/PRIVATE_TEST\n"
    if rewrite == "fetch":
        assert git(repo, "remote", "get-url", "origin") == expected
        assert git(repo, "remote", "get-url", "--push", "--all", "origin") == expected
    else:
        assert git(repo, "remote", "get-url", "--push", "--all", "origin").splitlines()[-1] == expected.strip()
    monkeypatch.setattr(ph, "step1_sanity", lambda: pytest.fail("accepted rewritten candidate remote"))
    with pytest.raises(SystemExit):
        with ph._candidate_copy(ph.parse_args(publish_args(tmp_path)), "codex/test-safe-release"):
            pytest.fail("accepted rewritten candidate remote")
    assert snapshot(repo) == before
    assert config.read_text(encoding="utf-8") == content
    receipt = json.loads((tmp_path / "candidate output/release.json").read_text(encoding="utf-8"))
    assert receipt["phase"] == "incomplete" and receipt["failed_at"] == "prepare"
    assert "PRIVATE_TEST" not in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["index", "worktree"])
@pytest.mark.parametrize("driver", ["python", "custom"])
def test_candidate_exact_index_matches_despite_local_diff_driver(
        ph, repo, tmp_path, mode, driver):
    attributes = repo / ".gitattributes"
    attributes.write_text(f"* text=auto eol=lf\n*.py diff={driver}\n", encoding="utf-8", newline="\n")
    path = repo / "src/選定 file.py"
    baseline = "# Section special\ndef original():\n" + "    value = 1\n" * 35
    path.write_text(baseline, encoding="utf-8", newline="\n")
    git(repo, "add", ".gitattributes", "src/選定 file.py")
    git(repo, "commit", "-m", "Anonymous Python diff driver baseline")
    if driver == "python":
        git(repo, "config", "diff.python.xfuncname", "^[[:space:]]*# Section.*")
    else:
        git(repo, "config", "diff.custom.binary", "true")
    lines = baseline.splitlines(keepends=True)
    lines[30] = "    value = 2\n"
    selected = "".join(lines)
    path.write_text(selected, encoding="utf-8", newline="\n")
    git(repo, "add", "src/選定 file.py")
    lines[30] = "    value = 3\n"
    path.write_text("".join(lines), encoding="utf-8", newline="\n")
    before = snapshot(repo)
    config = git(repo, "config", "--local", "--list")
    expected = selected if mode == "index" else "".join(lines)
    with ph._candidate_copy(ph.parse_args(publish_args(tmp_path, mode)), "codex/test-safe-release"):
        assert git(ph.REPO_ROOT, "show", ":src/選定 file.py") == expected.encode("utf-8")
    assert snapshot(repo) == before
    assert git(repo, "config", "--local", "--list") == config


@pytest.mark.parametrize("drift", ["content", "extra_path", "mode"])
def test_candidate_rejects_applied_content_drift_before_safety_gate(ph, repo, tmp_path, monkeypatch, drift):
    dirty_source(repo)
    before = snapshot(repo)
    original_run = subprocess.run
    def drift_after_apply(command, **kwargs):
        result = original_run(command, **kwargs)
        if command[:2] == ["git", "apply"]:
            candidate = Path(kwargs["cwd"])
            if drift == "mode":
                change = ["git", "update-index", "--chmod=+x", "src/選定 file.py"]
            else:
                rel = "src/選定 file.py" if drift == "content" else "src/extra.dat"
                (candidate / rel).write_text("value = 999\n", encoding="utf-8")
                change = ["git", "add", rel]
            original_run(change, cwd=candidate, check=True, capture_output=True)
        return result
    monkeypatch.setattr(ph.subprocess, "run", drift_after_apply)
    monkeypatch.setattr(ph, "step1_sanity", lambda: pytest.fail("accepted a changed candidate patch"))
    with pytest.raises(SystemExit):
        with ph._candidate_copy(ph.parse_args(publish_args(tmp_path)), "codex/test-safe-release"):
            pytest.fail("accepted a changed candidate patch")
    assert snapshot(repo) == before
    receipt = json.loads((tmp_path / "candidate output/release.json").read_text(encoding="utf-8"))
    assert receipt["phase"] == "incomplete" and receipt["failed_at"] == "prepare"


@pytest.mark.parametrize("mode", ["committed", "index"])
def test_candidate_rejects_extra_gitlink_when_global_diff_hides_submodules(
        ph, repo, tmp_path, monkeypatch, mode):
    config = tmp_path / "anonymous global git config"
    content = "[diff]\n\tignoreSubmodules = all\n"
    config.write_text(content, encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    dirty_source(repo)
    before = snapshot(repo)
    original_run = subprocess.run
    def inject_gitlink(command, **kwargs):
        result = original_run(command, **kwargs)
        if (mode == "committed" and command[:2] == ["git", "checkout"]
                or mode == "index" and command[:2] == ["git", "apply"]):
            candidate = Path(kwargs["cwd"])
            head = original_run(["git", "rev-parse", "HEAD"], cwd=candidate,
                                check=True, capture_output=True).stdout.decode().strip()
            original_run(["git", "update-index", "--add", "--cacheinfo",
                          f"160000,{head},src/extra.link"], cwd=candidate, check=True, capture_output=True)
        return result
    monkeypatch.setattr(ph.subprocess, "run", inject_gitlink)
    monkeypatch.setattr(ph, "step1_sanity", lambda: pytest.fail("accepted unexpected gitlink before generation"))
    with pytest.raises(SystemExit):
        with ph._candidate_copy(ph.parse_args(publish_args(tmp_path, mode)), "codex/test-safe-release"):
            pytest.fail("accepted unexpected gitlink before generation")
    assert snapshot(repo) == before
    assert config.read_text(encoding="utf-8") == content
    receipt = json.loads((tmp_path / "candidate output/release.json").read_text(encoding="utf-8"))
    assert receipt["phase"] == "incomplete" and receipt["failed_at"] == "prepare"


@pytest.mark.parametrize("kind", ["missing", "unstaged", "intent_to_add", "unchanged", "case_variant"])
def test_index_selection_rejects_each_path_without_an_exact_staged_delta(
        ph, repo, tmp_path, monkeypatch, kind):
    extra = "docs/missing.md"
    if kind in ("unstaged", "intent_to_add"):
        extra = "docs/pending.md"
        (repo / extra).write_text("not staged for publication\n", encoding="utf-8")
        if kind == "intent_to_add":
            git(repo, "add", "-N", extra)
    elif kind == "unchanged":
        extra = "scripts/sync_manifest.py"
    elif kind == "case_variant":
        (repo / "src/Case.py").write_text("baseline\n", encoding="utf-8")
        git(repo, "add", "src/Case.py")
        git(repo, "commit", "-m", "Anonymous case-sensitive baseline")
        (repo / "src/Case.py").write_text("staged\n", encoding="utf-8")
        git(repo, "add", "src/Case.py")
        extra = "src/case.py"
    dirty_source(repo)
    before = snapshot(repo)
    monkeypatch.setattr(ph, "step3_bump_version", lambda: pytest.fail("generated with a missing selected path"))
    args = publish_args(tmp_path) + ["--path", extra]
    with pytest.raises(SystemExit) as error:
        ph.main(args)
    assert error.value.code != 0
    assert snapshot(repo) == before
    assert not (tmp_path / "candidate output").exists()


def test_worktree_missing_selection_fails_cleanly_before_creating_receipt(
        ph, repo, tmp_path, monkeypatch):
    dirty_source(repo)
    before = snapshot(repo)
    monkeypatch.setattr(ph, "step3_bump_version", lambda: pytest.fail("generated with a missing selected path"))
    with pytest.raises(SystemExit) as error:
        ph.main(publish_args(tmp_path, "worktree") + ["--path", "docs/missing.md"])
    assert error.value.code != 0
    assert snapshot(repo) == before
    assert not (tmp_path / "candidate output").exists()


@pytest.mark.parametrize("mode", ["index", "committed"])
def test_generation_cannot_renormalize_an_unselected_head_blob(
        ph, repo, tmp_path, monkeypatch, mode):
    # A real legacy HEAD blob conflicts with text/eol normalization. Only its
    # stat information changes after generation; the user selected no doc edit.
    (repo / ".gitattributes").write_text("* text eol=lf\n", encoding="utf-8", newline="\n")
    git(repo, "add", ".gitattributes")
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], cwd=repo,
                          input=b"original\r\n", check=True, capture_output=True).stdout.decode().strip()
    git(repo, "update-index", "--cacheinfo", f"100644,{blob},docs/other.md")
    git(repo, "commit", "-m", "Anonymous legacy non-normalized blob")
    assert git(repo, "show", "HEAD:docs/other.md") == b"original\r\n"
    dirty_source(repo)
    before = snapshot(repo)
    sync = ph.step4_sync_manifest
    def sync_then_invalidate_stat(version):
        sync(version)
        os.utime(ph.REPO_ROOT / "docs/other.md", ns=(0, 0))
    monkeypatch.setattr(ph, "step4_sync_manifest", sync_then_invalidate_stat)
    monkeypatch.setattr(ph, "step_candidate_gate", lambda *args: None)
    pushed = []
    monkeypatch.setattr(ph, "step6_push", lambda sha: pushed.append(sha))
    with pytest.raises(SystemExit) as error:
        ph.main(publish_args(tmp_path, mode))
    assert error.value.code != 0
    assert not pushed and snapshot(repo) == before
    receipt = json.loads((tmp_path / "candidate output/release.json").read_text(encoding="utf-8"))
    assert receipt["phase"] == "incomplete" and receipt["failed_at"] == "verify_candidate_index"


@pytest.mark.parametrize("mode,expected", [("index", "value = 2\n"),
                                         ("worktree", "value = 3\n"),
                                         ("committed", "value = 1\n")])
@pytest.mark.parametrize("stdio_encoding", [None, "cp1252:strict"])
def test_publish_uses_exact_scope_and_preserves_all_source_changes(
        ph, repo, tmp_path, monkeypatch, mode, expected, stdio_encoding):
    # A Windows redirected child defaults to its ANSI encoding. Reproduce the
    # hosted English runner without changing this machine's system locale.
    if stdio_encoding:
        monkeypatch.setenv("PYTHONIOENCODING", stdio_encoding)
    else:
        monkeypatch.delenv("PYTHONIOENCODING", raising=False)
    dirty_source(repo)
    before = snapshot(repo)
    pushed = []
    monkeypatch.setattr(ph, "step_candidate_gate", lambda *args: None)
    def push(sha):
        assert git(ph.REPO_ROOT, "rev-parse", "HEAD").strip().decode() == sha
        assert (ph.REPO_ROOT / "src/選定 file.py").read_text(encoding="utf-8") == expected
        assert (ph.REPO_ROOT / "docs/other.md").read_text(encoding="utf-8") == "original\n"
        assert not (ph.REPO_ROOT / "unrelated.txt").exists()
        assert git(ph.REPO_ROOT, "config", "--get", "core.hooksPath").strip() == b".githooks"
        ph.verify_staged_manifest_hashes()
        pushed.append(sha)
    monkeypatch.setattr(ph, "step6_push", push)
    assert ph.main(publish_args(tmp_path, mode)) == 0
    assert pushed and snapshot(repo) == before
    receipt = json.loads((tmp_path / "candidate output/release.json").read_text(encoding="utf-8"))
    assert receipt["sha"] == pushed[0]
    assert receipt["phase"] == "candidate_pushed_awaiting_remote_ci"
    candidate = tmp_path / "candidate output/candidate"
    message = git(candidate, "log", "-1", "--format=%B").decode("utf-8")
    assert "修正 🧪" in message and "Claude-Opus-5-Review: pending" in message
    assert os.environ.get("PYTHONIOENCODING") == stdio_encoding


@pytest.mark.parametrize("stage,expected_phase", [
    ("step3_bump_version", "generate"), ("step4_sync_manifest", "generate"),
    ("step5_stage", "stage_candidate"), ("verify_index_matches", "verify_candidate_index"),
    ("step5_commit", "commit"), ("step_candidate_gate", "local_candidate_checks"),
    ("step6_push", "push_candidate"),
])
def test_failures_keep_original_state_and_recoverable_candidate(
        ph, repo, tmp_path, monkeypatch, stage, expected_phase):
    dirty_source(repo)
    before = snapshot(repo)
    monkeypatch.setattr(ph, "step_candidate_gate", lambda *args: None)
    monkeypatch.setattr(ph, "step6_push", lambda *args: None)
    def stop(*args):
        raise SystemExit(1)
    monkeypatch.setattr(ph, stage, stop)
    with pytest.raises(SystemExit):
        ph.main(publish_args(tmp_path))
    assert snapshot(repo) == before
    receipt = json.loads((tmp_path / "candidate output/release.json").read_text(encoding="utf-8"))
    assert receipt["phase"] == "incomplete" and receipt["failed_at"] == expected_phase
    assert (tmp_path / "candidate output/candidate/.git").is_dir()


def test_selected_source_drift_during_checks_prevents_push(ph, repo, tmp_path, monkeypatch):
    dirty_source(repo)
    def gate(*args):
        (repo / "src/選定 file.py").write_text("value = 4\n", encoding="utf-8")
        git(repo, "add", "src/選定 file.py")
    monkeypatch.setattr(ph, "step_candidate_gate", gate)
    monkeypatch.setattr(ph, "step6_push", lambda *args: pytest.fail("pushed drifted source"))
    with pytest.raises(SystemExit):
        ph.main(publish_args(tmp_path))
    assert (repo / "src/選定 file.py").read_text(encoding="utf-8") == "value = 4\n"


def test_keyboard_interrupt_keeps_source_and_records_recovery_phase(ph, repo, tmp_path, monkeypatch):
    dirty_source(repo)
    before = snapshot(repo)
    def interrupt(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(ph, "step4_sync_manifest", interrupt)
    with pytest.raises(KeyboardInterrupt):
        ph.main(publish_args(tmp_path))
    assert snapshot(repo) == before
    receipt = json.loads((tmp_path / "candidate output/release.json").read_text(encoding="utf-8"))
    assert receipt["phase"] == "incomplete" and receipt["failed_at"] == "generate"


def test_explicit_single_digit_commit_message_is_preserved(ph, repo, monkeypatch):
    messages = []
    def commit(command, **kwargs):
        messages.append(Path(command[-1]).read_text(encoding="utf-8"))
        return subprocess.CompletedProcess(command, 0)
    monkeypatch.setattr(ph, "run", commit)
    ph.step5_commit("1", "anonymous-version")
    assert messages == ["1"]


@pytest.mark.parametrize("path", ["../escape.py", "src", "src/*.py", ".git/config",
                                 "settings/password.json", "manifest.json"])
def test_unsafe_selection_rejected_without_source_mutation(ph, repo, tmp_path, path):
    before = snapshot(repo)
    with pytest.raises(SystemExit):
        ph.main(["push", "publish", "--path", path, "--message", "test",
                 "--output", str(tmp_path / "candidate output")])
    assert snapshot(repo) == before
    assert not (tmp_path / "candidate output").exists()


def test_candidate_cannot_reintroduce_settings_hidden_by_source_index(ph, repo, tmp_path, monkeypatch):
    private = repo / "settings/fake-secret.txt"
    private.parent.mkdir()
    private.write_text("anonymous fake credential", encoding="utf-8")
    git(repo, "add", "-f", "settings/fake-secret.txt")
    git(repo, "commit", "-m", "Anonymous unsafe historical HEAD")
    git(repo, "rm", "--cached", "settings/fake-secret.txt")
    before = snapshot(repo)
    monkeypatch.setattr(ph, "step_candidate_gate", lambda *args: None)
    monkeypatch.setattr(ph, "step6_push", lambda *args: pytest.fail("candidate tracked settings"))
    with pytest.raises(SystemExit):
        ph.main(publish_args(tmp_path, "committed"))
    assert snapshot(repo) == before


def test_candidate_checks_selected_gitignore_not_unstaged_source_version(ph, repo, tmp_path, monkeypatch):
    original = (repo / ".gitignore").read_text(encoding="utf-8")
    (repo / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    git(repo, "add", ".gitignore")
    (repo / ".gitignore").write_text(original, encoding="utf-8")
    before = snapshot(repo)
    monkeypatch.setattr(ph, "step_candidate_gate", lambda *args: None)
    monkeypatch.setattr(ph, "step6_push", lambda *args: pytest.fail("unsafe candidate gitignore"))
    with pytest.raises(SystemExit):
        ph.main(["push", "publish", "--path", ".gitignore", "--message", "anonymous",
                 "--output", str(tmp_path / "candidate output")])
    assert snapshot(repo) == before


@pytest.mark.parametrize("options", [[], ["--add", "--push"]])
def test_remote_credentials_are_not_echoed_in_command_output(ph, repo, monkeypatch, capsys, options):
    monkeypatch.setattr(ph.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 0))
    ph.run(["git", "remote", "set-url", *options, "origin", "https://anonymous:PRIVATE_TEST@example.invalid/repo"])
    assert "PRIVATE_TEST" not in capsys.readouterr().out


@pytest.mark.parametrize("options", [[], ["--add", "--push"]])
def test_remote_failure_exception_also_hides_credentials(ph, repo, monkeypatch, capsys, options):
    monkeypatch.setattr(ph.subprocess, "run", lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 1, "PRIVATE_TEST", "PRIVATE_TEST"))
    with pytest.raises(subprocess.CalledProcessError) as error:
        ph.run(["git", "remote", "set-url", *options, "origin", "https://anonymous:PRIVATE_TEST@example.invalid/repo"])
    assert "PRIVATE_TEST" not in str(error.value) + capsys.readouterr().out
