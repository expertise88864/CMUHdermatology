"""tools/codex_review.sh — gpt-6.2-astra / medium review profile (user decision 2026-11-18,
replacing the 2026-10-14 gpt-6.1-sol / xhigh profile).

Every run uses an isolated fixture CLI and fixture session log; nothing here
reaches the real Codex installation or the network.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SID = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
OTHER = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"


def _bash() -> str:
    # System32\bash.exe is the WSL launcher, not a POSIX shell for this repo.
    git_bash = Path("C:/Program Files/Git/bin/bash.exe")
    if os.name == "nt" and git_bash.exists():
        return str(git_bash)
    found = shutil.which("bash")
    if not found or "system32" in found.lower():
        raise unittest.SkipTest("Git Bash is required")  # pragma: no cover
    return found


EMITTER = r'''import datetime, json, os, pathlib, sys
root = pathlib.Path.cwd()
args = sys.argv[1:]
(root / "called-args.json").write_text(json.dumps(args))
(root / "called-env.txt").write_text(os.environ.get("GIT_CONFIG_PARAMETERS", ""))
pathlib.Path(args[args.index("-o") + 1]).write_text("NO_ACTIONABLE_FINDINGS\nAPPROVE\n")
sid = os.environ["FIXTURE_SID"]
context = {"cwd": str(root), "model": os.environ["FIXTURE_MODEL"],
           "effort": os.environ["FIXTURE_EFFORT"],
           "sandbox_policy": {"type": os.environ["FIXTURE_SANDBOX"]}, "turn_id": "fixture"}
rows = [{"type": "session_meta", "payload": {"id": sid, "cwd": str(root)}}]
if os.environ.get("FIXTURE_PRIOR_MODEL"):
    # An earlier turn of the same session, run by another reviewer profile.
    rows.append({"type": "turn_context", "timestamp": "2026-10-20T00:00:00+00:00",
                 "payload": dict(context, model=os.environ["FIXTURE_PRIOR_MODEL"],
                                 effort=os.environ["FIXTURE_PRIOR_EFFORT"])})
rows += [{"type": "turn_context",
         "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
         "payload": context}]
# What the reviewer actually received: the last argv entry is the prompt. A cmd.exe
# shim delivers only its first line (FIXTURE_TRUNCATE reproduces that).
received = args[-1]
if os.environ.get("FIXTURE_TRUNCATE"):
    received = received.splitlines()[0]
rows.append({"type": "response_item", "payload": {
    "type": "message", "role": "user",
    "content": [{"type": "input_text", "text": received}]}})
sessions = pathlib.Path(os.environ["CODEX_HOME"]) / "sessions"
sessions.mkdir(parents=True, exist_ok=True)
(sessions / ("rollout-" + sid + ".jsonl")).write_text("\n".join(map(json.dumps, rows)))
print(json.dumps({"type": "thread.started", "thread_id": sid}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 2}}))
sys.exit(int(os.environ["FIXTURE_RC"]))
'''


class CodexReviewWrapperTests(unittest.TestCase):
    def run_wrapper(self, *, resume=False, recorded=None, count=0, returned=SID,
                    model="gpt-6.2-astra", effort="medium", sandbox="read-only", rc=0,
                    prior=None, truncate=False, cli="codex", prompt_ok=True, locked=False,
                    lock_token=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "repo"
            root.mkdir()
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            subprocess.run(["git", "-C", str(root), "-c", "user.name=Fixture", "-c",
                            "user.email=fixture@example.invalid", "commit", "--allow-empty",
                            "-qm", "fixture"], check=True)
            tools = root / "tools"
            tools.mkdir()
            for name in ("codex_review.sh", "codex_review_identity.py"):
                shutil.copyfile(ROOT / "tools" / name, tools / name)
            state = root / ".codex-review"
            state.mkdir()
            (state / "last_pass").write_text(str(count))
            if locked:   # another review (or the driver, with lock_token) holds this checkout
                (state / "review.lock").mkdir()
                if lock_token:
                    (state / "review.lock" / "owner").write_text(lock_token)
            if recorded:
                (state / "last_session_id").write_text(recorded)
                if prompt_ok:   # its first round proved the full prompt arrived
                    (state / "prompt_verified_session").write_text(recorded)
            fixture_bin = Path(directory) / "bin"
            fixture_bin.mkdir()
            emitter = Path(directory) / "emit.py"
            emitter.write_text(EMITTER, encoding="utf-8")
            python = sys.executable.replace("\\", "/")
            (fixture_bin / "codex").write_text(
                f'#!/usr/bin/env bash\nexec "{python}" "{emitter.as_posix()}" "$@"\n',
                encoding="utf-8", newline="\n")
            (fixture_bin / "codex").chmod(0o755)
            env = dict(os.environ,
                       PATH=str(fixture_bin) + os.pathsep + str(Path(sys.executable).parent)
                       + os.pathsep + os.environ.get("PATH", ""),
                       CODEX_HOME=str(Path(directory) / "codex-home"),
                       CODEX_REVIEW_CLI=cli, FIXTURE_TRUNCATE="1" if truncate else "", FIXTURE_SID=returned, FIXTURE_MODEL=model,
                       FIXTURE_EFFORT=effort, FIXTURE_SANDBOX=sandbox, FIXTURE_RC=str(rc),
                       FIXTURE_PRIOR_MODEL=(prior or ("", ""))[0],
                       FIXTURE_PRIOR_EFFORT=(prior or ("", ""))[1])
            env.pop("GIT_CONFIG_PARAMETERS", None)
            env.pop("CODEX_REVIEW_LOCK_TOKEN", None)
            if lock_token:
                env["CODEX_REVIEW_LOCK_TOKEN"] = lock_token
            env.pop("BASH_ENV", None)
            args = ["tools/codex_review.sh"] + (["resume"] if resume else ["deep", "HEAD"])
            result = subprocess.run([_bash(), "--noprofile", "--norc", *args], cwd=root, env=env,
                                    capture_output=True, text=True, encoding="utf-8",
                                    errors="replace", timeout=60)
            called = None
            if (root / "called-args.json").exists():
                called = {
                    "args": json.loads((root / "called-args.json").read_text()),
                    "git": (root / "called-env.txt").read_text(),
                    "top": subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                                          capture_output=True, text=True, check=True).stdout.strip(),
                }
            sid_file = state / "last_session_id"
            return (result.returncode, called,
                    sid_file.read_text().strip() if sid_file.exists() else None,
                    (state / "last_pass").read_text().strip(), result.stdout + result.stderr)

    def assert_profile(self, called):
        args = called["args"]
        self.assertEqual(args[args.index("--model") + 1], "gpt-6.2-astra")
        self.assertIn("model_reasoning_effort=medium", args)
        self.assertFalse([a for a in args if a.startswith("model_reasoning_effort=")
                          and a != "model_reasoning_effort=medium"])
        self.assertIn("--json", args)
        if os.name == "nt":
            self.assertIn("windows.sandbox=unelevated", args)
        # Only this repository is marked safe, only for the review child.
        self.assertEqual(called["git"], f"'safe.directory'='{called['top']}'")

    def test_full_multiline_prompt_reaches_the_reviewer(self):
        # PR #25 (2026-11-18): through npm's codex.cmd the reviewer got only the first
        # prompt line. A native CLI receives every line, and the identity check proves it.
        for resume in (False, True):
            with self.subTest(resume=resume):
                rc, called, sid, passes, out = self.run_wrapper(resume=resume, recorded=SID, count=1)
                self.assertEqual((rc, sid), (0, SID), out)
                prompt = called["args"][-1]
                self.assertGreater(len(prompt.splitlines()), 3)
                if not resume:
                    for section in ("REVIEW RANGE:", "TASK CONTEXT:", "LOCAL VERIFICATION:"):
                        self.assertIn(section, prompt)

    def test_truncated_prompt_never_counts(self):
        for resume in (False, True):
            with self.subTest(resume=resume):
                rc, called, sid, passes, out = self.run_wrapper(resume=resume, recorded=SID,
                                                                count=1, truncate=True)
                self.assertEqual(rc, 4, out)
                self.assertEqual(passes, "1" if resume else "0", out)
                self.assertIn("truncated or different prompt", out)

    def test_concurrent_review_in_the_same_checkout_is_refused(self):
        for resume in (False, True):
            with self.subTest(resume=resume):
                rc, called, sid, passes, out = self.run_wrapper(resume=resume, recorded=SID,
                                                                count=1, locked=True)
                self.assertEqual(rc, 64, out)
                self.assertIsNone(called)

    def test_driver_held_lock_admits_its_own_wrapper_only(self):
        rc, called, sid, passes, out = self.run_wrapper(locked=True, lock_token="t0k")
        self.assertEqual((rc, sid), (0, SID), out)
        rc, called, sid, passes, out = self.run_wrapper(locked=True, lock_token="")
        self.assertEqual(rc, 64, out)
        self.assertIsNone(called)

    def test_batch_shim_cli_is_refused_before_running(self):
        for cli in ("C:/npm/codex.cmd", "C:/npm/CODEX.CMD", "C:/x/codex.bat", "C:/npm/codex.ps1"):
            with self.subTest(cli=cli):
                rc, called, sid, passes, out = self.run_wrapper(cli=cli)
                self.assertEqual(rc, 64, out)
                self.assertIsNone(called)

    def test_first_pass_records_verified_session(self):
        rc, called, sid, passes, out = self.run_wrapper()
        self.assertEqual((rc, sid, passes), (0, SID, "1"), out)
        self.assert_profile(called)

    def test_wrong_actual_profile_never_counts(self):
        # The retired 2026-10-14 reviewer (gpt-6.1-sol / xhigh) is now a mismatch too.
        for kwargs in ({"model": "gpt-5.6-sol"}, {"model": "gpt-6.1-sol"},
                       {"model": "gpt-6.1-sol", "effort": "xhigh"}, {"effort": "xhigh"},
                       {"effort": "high"}, {"effort": "low"}, {"sandbox": "workspace-write"}):
            with self.subTest(**kwargs):
                rc, called, sid, passes, out = self.run_wrapper(**kwargs)
                self.assertEqual((rc, sid, passes), (4, None, "0"), out)
                self.assertIsNotNone(called)

    def test_failed_first_pass_drops_previous_session(self):
        # A new first pass that cannot be verified must not leave the previous
        # session resumable as if it were this review.
        rc, _, sid, passes, out = self.run_wrapper(recorded=OTHER, count=3, model="gpt-5.5")
        self.assertEqual((rc, sid, passes), (4, None, "0"), out)

    def test_resume_same_session_advances(self):
        rc, called, sid, passes, out = self.run_wrapper(resume=True, recorded=SID, count=2)
        self.assertEqual((rc, sid, passes), (0, SID, "3"), out)
        self.assert_profile(called)

    def test_resume_of_a_session_begun_by_a_retired_reviewer_never_counts(self):
        # User decision 2026-11-18: a gpt-6.1-sol / xhigh first round resumed under
        # gpt-6.2-astra / medium must not count; a fresh first round is required.
        for prior in (("gpt-6.1-sol", "xhigh"), ("gpt-6.2-astra", "xhigh"), ("gpt-6.1-sol", "medium")):
            with self.subTest(prior=prior):
                rc, called, sid, passes, out = self.run_wrapper(resume=True, recorded=SID,
                                                                count=2, prior=prior)
                self.assertEqual((rc, passes), (4, "2"), out)
                self.assertIsNotNone(called)

    def test_resume_of_a_session_with_only_this_reviewer_counts(self):
        rc, _, sid, passes, out = self.run_wrapper(resume=True, recorded=SID, count=1,
                                                   prior=("gpt-6.2-astra", "medium"))
        self.assertEqual((rc, sid, passes), (0, SID, "2"), out)

    def test_resume_without_full_prompt_proof_is_refused(self):
        # A session whose first round predates prompt verification (e.g. PR #25's,
        # truncated by codex.cmd) cannot be resumed into a whole-range verdict.
        rc, called, sid, passes, out = self.run_wrapper(resume=True, recorded=SID, count=1,
                                                        prompt_ok=False)
        self.assertEqual((rc, passes), (64, "1"), out)
        self.assertIsNone(called)

    def test_resume_returning_another_session_never_counts(self):
        rc, _, sid, passes, out = self.run_wrapper(resume=True, recorded=SID, count=1, returned=OTHER)
        self.assertEqual((rc, sid, passes), (4, SID, "1"), out)


class NativeCliSelectionTests(unittest.TestCase):
    """find_codex never returns a script shim; it swaps npm's shim for the package's exe."""

    def _identity(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "codex_review_identity_t", ROOT / "tools" / "codex_review_identity.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    NESTED = "node_modules/@openai/codex/node_modules/@openai/codex-win32-x64"
    HOISTED = "node_modules/@openai/codex-win32-x64"

    def _layout(self, root: Path, native: bool, shim_dir: str = "npm",
                package: str = NESTED) -> Path:
        """`shim_dir` holds the shims; `package` (relative to the directory that
        contains node_modules) holds the platform package with the native exe."""
        npm = root / shim_dir
        npm.mkdir(parents=True)
        for name in ("codex.cmd", "codex.ps1", "codex"):
            (npm / name).write_text("@ECHO off\n", encoding="utf-8")
        if native:
            prefix = npm.parent.parent if npm.name == ".bin" else npm
            exe = prefix / package / "vendor/x86_64-pc-windows-msvc/bin/codex.exe"
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"MZ")
        return npm

    def _select(self, identity, npm: Path, name_hit: str):
        from unittest.mock import patch
        hits = {name_hit: str(npm / name_hit)}
        with patch.object(identity.os, "name", "nt"), \
             patch.dict(identity.os.environ, {"LOCALAPPDATA": ""}), \
             patch.object(identity, "path_files", side_effect=lambda n: [hits[n]] if n in hits else []), \
             patch.object(identity, "cli_version", return_value="codex-cli 0.169.1"):
            return identity.find_codex()

    def test_ps1_shim_is_found_without_ps1_in_pathext(self):
        # shutil.which hides codex.ps1 when PATHEXT lacks .PS1; path_files must not.
        from unittest.mock import patch
        identity = self._identity()
        with tempfile.TemporaryDirectory() as tmp:
            npm = self._layout(Path(tmp), True)
            (npm / "codex.cmd").unlink()
            (npm / "codex").unlink()
            with patch.dict(identity.os.environ, {"PATH": str(npm), "PATHEXT": ".COM;.EXE;.BAT;.CMD"}):
                self.assertEqual(identity.path_files("codex.ps1"), [str(npm / "codex.ps1")])
                self.assertEqual(identity.path_files("codex.cmd"), [])
                if os.name == "nt":
                    self.assertIsNone(shutil.which("codex.ps1"))
                    with patch.dict(identity.os.environ, {"LOCALAPPDATA": ""}), \
                         patch.object(identity, "cli_version", return_value="codex-cli 0.169.1"):
                        path, _ = identity.find_codex()
                    self.assertTrue(path.lower().endswith("codex.exe"), path)

    def test_npm_shim_resolves_to_the_native_exe(self):
        identity = self._identity()
        # Global prefix and project-local node_modules/.bin; nested or hoisted
        # platform package; each shim (including codex.ps1) as the only PATH hit.
        layouts = (("npm", self.NESTED), ("npm", self.HOISTED),
                   ("proj/node_modules/.bin", self.NESTED), ("proj/node_modules/.bin", self.HOISTED))
        for shim_dir, package in layouts:
            for shim in ("codex.cmd", "codex.ps1", "codex"):
                with self.subTest(shim_dir=shim_dir, package=package, shim=shim), \
                        tempfile.TemporaryDirectory() as tmp:
                    npm = self._layout(Path(tmp), True, shim_dir, package)
                    path, version = self._select(identity, npm, shim)
                    self.assertTrue(path.lower().endswith("codex.exe"), path)
                    self.assertFalse(identity.is_script_shim(path))
                    self.assertEqual(version, "codex-cli 0.169.1")

    def test_shim_without_native_exe_is_never_selected(self):
        identity = self._identity()
        with tempfile.TemporaryDirectory() as tmp:
            npm = self._layout(Path(tmp), native=False)
            with self.assertRaises(ValueError):
                self._select(identity, npm, "codex.cmd")

    def test_posix_newer_install_later_on_path_is_not_shadowed(self):
        # Runs on the Windows CI runner too: platform and executable bit are mocked.
        from unittest.mock import patch
        identity = self._identity()
        with tempfile.TemporaryDirectory() as tmp:
            dirs = []
            for name in ("old", "new"):
                d = Path(tmp) / name
                d.mkdir()
                (d / "codex").write_text("#!/bin/sh\n", encoding="utf-8")
                (d / "codex").chmod(0o755)
                dirs.append(str(d))
            with patch.object(identity.os, "name", "posix"), \
                 patch.object(identity.os, "access", return_value=True), \
                 patch.dict(identity.os.environ, {"PATH": os.pathsep.join(dirs)}), \
                 patch.object(identity, "cli_version",
                              side_effect=lambda p: "codex-cli 0.169.1" if "new" in p else "codex-cli 0.150.0"):
                path, _ = identity.find_codex()
            self.assertIn("new", path)

    def test_newer_install_later_on_path_is_not_shadowed(self):
        # An older project-local .bin ahead of a newer global prefix must not hide it.
        from unittest.mock import patch
        identity = self._identity()
        with tempfile.TemporaryDirectory() as tmp:
            old = self._layout(Path(tmp) / "a", True, "proj/node_modules/.bin")
            new = self._layout(Path(tmp) / "b", True)
            versions = {"a": "codex-cli 0.150.0", "b": "codex-cli 0.169.1"}

            def version(path):
                return versions["a" if str(Path(tmp) / "a") in path else "b"]
            with patch.object(identity.os, "name", "nt"), \
                 patch.dict(identity.os.environ, {"LOCALAPPDATA": "",
                                                  "PATH": os.pathsep.join([str(old), str(new)])}), \
                 patch.object(identity, "cli_version", side_effect=version):
                self.assertEqual(len(identity.codex_candidates()), 2)
                path, chosen = identity.find_codex()
            self.assertIn(str(Path(tmp) / "b"), path)
            self.assertEqual(chosen, "codex-cli 0.169.1")

    def test_extensionless_npm_shim_override_resolves_to_native(self):
        from unittest.mock import patch
        identity = self._identity()
        with tempfile.TemporaryDirectory() as tmp:
            npm = self._layout(Path(tmp), True, "proj/node_modules/.bin")
            with patch.object(identity.os, "name", "nt"), \
                 patch.object(identity, "cli_version", return_value="codex-cli 0.169.1"):
                path = identity.native_override(str(npm / "codex"))
                self.assertTrue(path.lower().endswith("codex.exe"), path)
                # Not an npm shim: unchanged (the wrapper's own checks still apply).
                self.assertEqual(identity.native_override(str(Path(tmp) / "plain")), str(Path(tmp) / "plain"))
                self.assertEqual(identity.native_override("C:/x/codex.exe"), "C:/x/codex.exe")
            with patch.object(identity.os, "name", "nt"), \
                 patch.object(identity, "cli_version", return_value=""), \
                 self.assertRaises(ValueError):
                identity.native_override(str(npm / "codex"))
        source = (ROOT / "tools" / "codex_review.sh").read_text(encoding="utf-8")
        self.assertIn("codex_review_identity.py\" native-override", source)

    def test_script_shim_detection(self):
        identity = self._identity()
        for path in ("a/codex.cmd", "a/CODEX.BAT", "a/codex.ps1"):
            self.assertTrue(identity.is_script_shim(path))
        for path in ("a/codex.exe", "a/codex"):
            self.assertFalse(identity.is_script_shim(path))


class ResolveCliUnicodePathTests(unittest.TestCase):
    """Automatic CLI selection must return a non-ASCII path intact.

    The wrapper tests pin CODEX_REVIEW_CLI and never run this branch. Here the
    real resolve_cli runs against a stand-in `which` helper that prints a
    Chinese path with Python's default (locale) encoding.
    """
    CLI = "C:/Users/\u9673\u7fca\u5609/AppData/Local/OpenAI/Codex/bin/x/codex.exe"

    def test_resolve_cli_keeps_unicode_path(self):
        source = (ROOT / "tools/codex_review.sh").read_text(encoding="utf-8")
        start = source.index("resolve_cli() {")
        end = source.index("\nVERIFIED_SID=", start)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "codex_review_identity.py").write_text(
                "import sys\nassert sys.argv[1:] == ['which'], sys.argv\n"
                f"print({self.CLI!r})\nprint('codex-cli 0.0.0')\n", encoding="utf-8")
            out = root / "cli.txt"
            script = root / "probe.sh"
            python = sys.executable.replace("\\", "/")
            script.write_text(
                'die() { echo "$*" >&2; exit 64; }\n'
                f'PYTHON="{python}"\nSCRIPT_DIR="{root.as_posix()}"\nREPO_ROOT=/fixture\n'
                + source[start:end]
                + f'\nresolve_cli >/dev/null\nprintf "%s" "$CODEX_CLI" > "{out.as_posix()}"\n',
                encoding="utf-8", newline="\n")
            env = {k: v for k, v in os.environ.items()
                   if k not in ("PYTHONUTF8", "PYTHONIOENCODING", "CODEX_REVIEW_CLI", "BASH_ENV")}
            result = subprocess.run([_bash(), "--noprofile", "--norc", str(script)], env=env,
                                    capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(out.read_bytes().decode("utf-8", "replace"), self.CLI)


if __name__ == "__main__":
    unittest.main()
