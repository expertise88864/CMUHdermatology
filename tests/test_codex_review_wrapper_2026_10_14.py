"""tools/codex_review.sh — gpt-6.1-sol / high review profile (user decision 2026-10-14).

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
rows = [{"type": "session_meta", "payload": {"id": sid, "cwd": str(root)}},
        {"type": "turn_context",
         "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
         "payload": context}]
sessions = pathlib.Path(os.environ["CODEX_HOME"]) / "sessions"
sessions.mkdir(parents=True, exist_ok=True)
(sessions / ("rollout-" + sid + ".jsonl")).write_text("\n".join(map(json.dumps, rows)))
print(json.dumps({"type": "thread.started", "thread_id": sid}))
print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 2}}))
sys.exit(int(os.environ["FIXTURE_RC"]))
'''


class CodexReviewWrapperTests(unittest.TestCase):
    def run_wrapper(self, *, resume=False, recorded=None, count=0, returned=SID,
                    model="gpt-6.1-sol", effort="high", sandbox="read-only", rc=0):
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
            if recorded:
                (state / "last_session_id").write_text(recorded)
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
                       CODEX_REVIEW_CLI="codex", FIXTURE_SID=returned, FIXTURE_MODEL=model,
                       FIXTURE_EFFORT=effort, FIXTURE_SANDBOX=sandbox, FIXTURE_RC=str(rc))
            env.pop("GIT_CONFIG_PARAMETERS", None)
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
        self.assertEqual(args[args.index("--model") + 1], "gpt-6.1-sol")
        self.assertIn("model_reasoning_effort=high", args)
        self.assertIn("--json", args)
        if os.name == "nt":
            self.assertIn("windows.sandbox=unelevated", args)
        # Only this repository is marked safe, only for the review child.
        self.assertEqual(called["git"], f"'safe.directory'='{called['top']}'")

    def test_first_pass_records_verified_session(self):
        rc, called, sid, passes, out = self.run_wrapper()
        self.assertEqual((rc, sid, passes), (0, SID, "1"), out)
        self.assert_profile(called)

    def test_wrong_actual_profile_never_counts(self):
        for kwargs in ({"model": "gpt-5.6-sol"}, {"effort": "medium"}, {"sandbox": "workspace-write"}):
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

    def test_resume_returning_another_session_never_counts(self):
        rc, _, sid, passes, out = self.run_wrapper(resume=True, recorded=SID, count=1, returned=OTHER)
        self.assertEqual((rc, sid, passes), (4, SID, "1"), out)


if __name__ == "__main__":
    unittest.main()
