"""Development setup never edits the user's global account or model settings."""
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def setup(tmp_path, monkeypatch):
    path = Path(__file__).parents[1] / "tools/dev-env-setup.py"
    spec = importlib.util.spec_from_file_location("dev_setup_under_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "anonymous project"
    root.mkdir()
    (root / ".githooks").mkdir()
    (root / ".githooks/pre-push").write_text("test", encoding="utf-8")
    home = tmp_path / "fake home"
    for name in (".codex/config.toml", ".claude/CLAUDE.md", ".claude.json"):
        dest = home / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text("existing user content", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module.shutil, "which", lambda name: name)
    monkeypatch.setattr(module, "run", lambda args, **kwargs:
                        SimpleNamespace(returncode=0, stdout=".githooks\n", stderr=""))
    return module, root, home


def contents(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def existing_environment(module, root, monkeypatch, missing=()):
    environment = root / ".venv"
    (environment / "Scripts").mkdir(parents=True)
    (environment / "Scripts/python.exe").write_bytes(b"anonymous test stub")
    monkeypatch.setattr(module, "_probe_dependencies", lambda py:
                        {"python": [3, 13], "venv": True, "missing": list(missing)})
    return environment


def test_default_is_readonly_and_reports_missing_environment(setup, monkeypatch, capsys):
    module, root, home = setup
    before = contents(root), contents(home)
    calls = []
    monkeypatch.setattr(module, "run", lambda args, **kwargs:
                        calls.append(args) or SimpleNamespace(returncode=0, stdout=".githooks\n", stderr=""))
    assert module.main([]) == 1
    assert (contents(root), contents(home)) == before
    assert calls == [["git", "config", "--get", "core.hooksPath"]]
    assert "not_checked" in capsys.readouterr().out


@pytest.mark.parametrize("action", ["check", "apply"])
def test_existing_environment_and_global_settings_are_preserved(setup, monkeypatch, action):
    module, root, home = setup
    existing_environment(module, root, monkeypatch)
    before = contents(root), contents(home)
    assert module.main([action]) == 0
    assert (contents(root), contents(home)) == before


def test_existing_incomplete_environment_is_not_modified(setup, monkeypatch, capsys):
    module, root, home = setup
    existing_environment(module, root, monkeypatch, ["pytest-cov"])
    before = contents(root), contents(home)
    assert module.main(["apply"]) == 1
    assert (contents(root), contents(home)) == before
    assert "pytest-cov" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["..", "outside", "settings", ".venv/inside", "../.venv"])
def test_environment_must_be_a_named_local_development_directory(setup, value):
    module, root, home = setup
    before = contents(root), contents(home)
    with pytest.raises(SystemExit) as error:
        module.main(["apply", "--venv", value])
    assert error.value.code == 2
    assert (contents(root), contents(home)) == before


@pytest.mark.parametrize("step", [1, 2, 3])
def test_install_failure_never_reports_completion_or_changes_global_settings(
        setup, monkeypatch, capsys, step):
    module, root, home = setup
    old = root / ".venv"
    old.mkdir()
    (old / "keep.txt").write_text("keep", encoding="utf-8")
    before = contents(home), contents(old)
    commands = []
    def run(args, **kwargs):
        commands.append(args)
        return SimpleNamespace(returncode=1 if len(commands) == step else 0,
                               stdout="PRIVATE secret output", stderr="token=PRIVATE")
    monkeypatch.setattr(module, "run", run)
    assert module.main(["apply", "--venv", ".venv-rebuild"]) == 1
    assert (contents(home), contents(old)) == before
    output = capsys.readouterr().out
    assert "未完成" in output and "檢查通過" not in output and "PRIVATE" not in output
    assert len(commands) == step and not any("npm" in command for command in commands)
    if step >= 2:
        assert commands[1][0] == str(root / ".venv-rebuild/Scripts/python.exe")
        assert "--isolated" in commands[1] and "--no-user" in commands[1]


@pytest.mark.parametrize("body,valid", [(' {"loggedIn":true,"token":"PRIVATE"}', True),
                                       ('{"loggedIn":false}', False), ('{"loggedIn":"true"}', False)])
def test_auth_status_reports_only_boolean_not_provider_contents(setup, monkeypatch, capsys, body, valid):
    module, root, home = setup
    existing_environment(module, root, monkeypatch)
    monkeypatch.setattr(module, "run", lambda args, **kwargs: SimpleNamespace(
        returncode=0, stdout=body if args[0] == "claude" else
        ("Logged in using test account\n" if args[0] == "codex" else ".githooks\n"), stderr=""))
    before = contents(home)
    assert module.main(["check", "--auth"]) == (0 if valid else 1)
    assert contents(home) == before
    assert "PRIVATE" not in capsys.readouterr().out


@pytest.mark.parametrize("output,valid", [("Logged in using ChatGPT", True),
                                         ("Not logged in", False), ("unknown", False)])
def test_codex_login_status_is_not_inferred_from_cli_presence(setup, monkeypatch, output, valid):
    module, _, _ = setup
    monkeypatch.setattr(module, "run", lambda args, **kwargs:
                        SimpleNamespace(returncode=0, stdout="", stderr=output))
    assert module._auth_status("codex") is valid


def test_subprocess_ignores_pip_environment_and_user_configuration(setup, monkeypatch):
    module, _, _ = setup
    monkeypatch.setenv("PIP_TARGET", "outside-project")
    monkeypatch.setenv("PIP_USER", "1")
    seen = []
    monkeypatch.setattr(module.subprocess, "run", lambda args, **kwargs: seen.append(kwargs))
    # Exercise the original function, not the fixture's command double.
    spec = importlib.util.spec_from_file_location("dev_setup_command", Path(__file__).parents[1] / "tools/dev-env-setup.py")
    fresh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fresh)
    fresh.run(["python", "-m", "pip", "--isolated", "check"])
    env = seen[0]["env"]
    assert "PIP_TARGET" not in env and "PIP_USER" not in env
    assert env["PIP_CONFIG_FILE"] == module.os.devnull


def test_cmd_propagates_python_exit_code():
    text = (Path(__file__).parents[1] / "tools/dev-env-setup.cmd").read_text(encoding="utf-8")
    assert 'set "RC=%ERRORLEVEL%"' in text and "exit /b %RC%" in text


def test_concurrent_creator_cannot_overwrite_another_environment(setup, monkeypatch):
    module, root, home = setup
    target = root / ".venv-race"
    original_exists = Path.exists
    injected = False
    commands = []
    def racing_exists(path):
        nonlocal injected
        result = original_exists(path)
        if path == target and not result and not injected:
            injected = True
            target.mkdir()
            (target / "pyvenv.cfg").write_text("owned by another creator", encoding="utf-8")
        return result
    monkeypatch.setattr(Path, "exists", racing_exists)
    monkeypatch.setattr(module, "run", lambda args, **kwargs:
                        commands.append(args) or SimpleNamespace(returncode=0))
    assert module.apply(target) is False
    assert commands == []
    assert (target / "pyvenv.cfg").read_text(encoding="utf-8") == "owned by another creator"


def test_real_metadata_probe_does_not_create_bytecode(setup, monkeypatch):
    module, root, home = setup
    target = root / ".venv-probe"
    subprocess.run([sys.executable, "-I", "-m", "venv", "--without-pip", str(target)],
                   check=True, capture_output=True)
    interpreter = module._python(target)
    site_packages = subprocess.run([str(interpreter), "-I", "-B", "-c",
                                   "import sysconfig; print(sysconfig.get_path('purelib'))"],
                                  check=True, capture_output=True, text=True).stdout.strip()
    package = Path(site_packages) / "packaging"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "requirements.py").write_text("class Requirement: pass\n", encoding="utf-8")
    monkeypatch.setattr(module, "_requirements", lambda: [])
    monkeypatch.setattr(module, "run", lambda args, **kwargs: subprocess.run(
        args, cwd=root, capture_output=True, text=True, check=False,
        env={**module.os.environ, "PYTHONDONTWRITEBYTECODE": "1"}))
    before = contents(target), contents(home)
    assert module._probe_dependencies(interpreter)["venv"] is True
    assert (contents(target), contents(home)) == before
