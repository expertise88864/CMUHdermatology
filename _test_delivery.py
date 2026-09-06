"""Offline regression tests for publication evidence. No real push/network."""
import unittest
import io
import subprocess
import os
from pathlib import Path
import shutil
import tempfile
from unittest.mock import patch
import _delivery as d

SHA = "a" * 40


class DeliveryTests(unittest.TestCase):
    def test_cli_preserves_claude_gate_failure_and_stdin(self):
        cfg = {**d.policy(), "claude_hook": True}
        updates = f"HEAD {SHA} refs/heads/codex/test {'0'*40}\n"
        with patch.object(d, "policy", return_value=cfg), \
             patch.object(d.sys, "argv", ["_delivery.py", "pre-push"]), \
             patch.object(d.sys, "stdin", io.StringIO(updates)), \
             patch.object(d, "pre_push") as gate, \
             patch.object(d.subprocess, "run", return_value=subprocess.CompletedProcess([], 4)) as review:
            self.assertEqual(d.main(), 4)
            gate.assert_called_once_with([updates], "origin", cfg)
            self.assertEqual(review.call_args.args[0][1:4], ["tools/claude_diff_review.py", "push", "--remote-name"])
            self.assertEqual(review.call_args.kwargs["input"], updates)

    def test_failed_delivery_gate_never_reaches_claude_or_success(self):
        with patch.object(d, "policy", return_value={**d.policy(), "claude_hook": True}), \
             patch.object(d.sys, "argv", ["_delivery.py", "pre-push"]), \
             patch.object(d.sys, "stdin", io.StringIO("")), \
             patch.object(d, "pre_push", side_effect=d.Blocked("missing CI")), \
             patch.object(d.subprocess, "run") as review:
            self.assertEqual(d.main(), 1)
            review.assert_not_called()

    def test_sha(self):
        for value in ("a", "0" * 40, "g" * 40, "a" * 40 + "\n"):
            with self.assertRaises(d.Blocked):
                d.check_sha(value)
        self.assertEqual(d.check_sha(SHA), SHA)

    def test_jobs_require_presence(self):
        with self.assertRaises(d.Blocked):
            d.assess_jobs([], ["test"], [])

    def test_required_step_must_be_present_unique_complete_and_successful(self):
        step = {"name": "Full tests", "status": "completed", "conclusion": "success"}
        job = {"name": "test", "status": "completed", "conclusion": "success", "steps": [step]}
        contract = {"test": {"required": ["Full tests"]}}
        d.assess_jobs([job], ["test"], [], contract)
        for steps in (None, [], [step, step], [{**step, "name": "Partial tests"}],
                      [{**step, "status": "in_progress"}],
                      *[[{**step, "conclusion": c}] for c in (None, "skipped", "neutral", "failure")]):
            with self.subTest(steps=steps), self.assertRaises(d.Blocked):
                d.assess_jobs([{**job, "steps": steps}], ["test"], [], contract)

    def test_candidate_non_fast_forward_blocks_before_tests(self):
        with patch.object(d, "git", side_effect=["https://github.com/owner/repo.git", "b"*40]), \
             patch.object(d, "clean"), \
             patch.object(d.subprocess, "run", side_effect=[None, subprocess.CalledProcessError(1, "merge-base")]) as run:
            with self.assertRaises(subprocess.CalledProcessError):
                d.pre_push([f"HEAD {SHA} refs/heads/codex/test {'b'*40}"], "origin", {"repository": "owner/repo"})
            self.assertEqual(run.call_count, 2)
            self.assertIn("--is-ancestor", run.call_args.args[0])

    def test_git_really_invokes_the_tracked_hook_and_blocks_unexpected_destination(self):
        # Entire remote is a new temporary bare repo: never contacts GitHub.
        # Git hooks export GIT_DIR etc. Those MUST NOT reach a foreign repo,
        # or even `git init <temp>` can reconfigure the calling repository.
        local_vars = subprocess.check_output(["git", "rev-parse", "--local-env-vars"],
                                             text=True, encoding="utf-8").splitlines()
        foreign_env = {key: value for key, value in os.environ.items() if key not in local_vars}
        with tempfile.TemporaryDirectory(prefix="delivery-hook-") as directory:
            root = Path(directory)
            work = root / "work"
            remote = root / "remote.git"
            def run(*args):
                return subprocess.run(["git", *args], cwd=work if work.exists() else root,
                                      capture_output=True, text=True, encoding="utf-8", check=True, env=foreign_env)
            run("init", "--bare", str(remote))
            run("init", str(work))
            run("config", "user.name", "Delivery test")
            run("config", "user.email", "delivery-test@example.invalid")
            (work / ".githooks").mkdir()
            for name in ("_delivery.py", "_delivery_policy.json", ".githooks/pre-push"):
                shutil.copyfile(d.ROOT / name, work / name)
            os.chmod(work / ".githooks/pre-push", 0o755)
            run("add", ".")
            run("commit", "-m", "isolated hook test")
            run("remote", "add", "origin", str(remote))
            run("config", "core.hooksPath", ".githooks")
            with self.assertRaises(subprocess.CalledProcessError) as blocked:
                run("push", "origin", "HEAD:refs/heads/codex/test")
            self.assertIn("Unexpected push destination", blocked.exception.stderr)
            self.assertEqual(run("ls-remote", "origin").stdout.strip(), "")

    def test_policy_cannot_omit_all_checks(self):
        for cfg in ({}, {"workflows": []}, {"workflows": [{"path": ".github/workflows/ci.yml", "jobs": []}]}):
            with self.assertRaises(d.Blocked):
                d.validate_policy(cfg)
        d.validate_policy(d.policy())

    def test_dispatch_requires_explicit_project_support(self):
        run = {"id": 1, "path": "ci.yml", "event": "workflow_dispatch", "head_branch": "codex/test",
               "head_sha": SHA, "status": "completed", "conclusion": "success"}
        with self.assertRaises(d.Blocked):
            d.select_run([run], "ci.yml", SHA, "candidate")
        self.assertEqual(d.select_run([run], "ci.yml", SHA, "candidate", True), run)

    def test_every_non_success_blocks(self):
        for state in (None, "failure", "cancelled", "timed_out", "neutral", "skipped"):
            with self.subTest(state=state), self.assertRaises(d.Blocked):
                d.assess_jobs([{"name": "test", "status": "completed", "conclusion": state}], ["test"], [])

    def test_only_declared_inapplicable_job_may_skip(self):
        d.assess_jobs([{"name": "test", "status": "completed", "conclusion": "success"},
                       {"name": "production-only", "status": "completed", "conclusion": "skipped"}],
                      ["test"], ["production-only"])

    def test_hidden_failure_blocks(self):
        with self.assertRaises(d.Blocked):
            d.assess_jobs([{"name": "test", "status": "completed", "conclusion": "success",
                            "steps": [{"name": "masked", "conclusion": "failure"}]}], ["test"], [])

    def test_running_job_blocks(self):
        with self.assertRaises(d.Blocked):
            d.assess_jobs([{"name": "test", "status": "in_progress", "conclusion": None}], ["test"], [])

    def test_select_only_exact_candidate_push(self):
        good = {"id": 1, "path": "ci.yml", "event": "push", "head_branch": "codex/test",
                "head_sha": SHA, "status": "completed", "conclusion": "success"}
        self.assertEqual(d.select_run([good], "ci.yml", SHA, "candidate"), good)
        for key, value in (("head_sha", "b"*40), ("head_branch", "main"),
                           ("path", "fake.yml"), ("event", "pull_request")):
            with self.subTest(key=key), self.assertRaises(d.Blocked):
                d.select_run([{**good, key: value}], "ci.yml", SHA, "candidate")

    def test_new_failed_run_does_not_reuse_old_green(self):
        good = {"id": 1, "path": "ci.yml", "event": "push", "head_branch": "codex/test",
                "head_sha": SHA, "status": "completed", "conclusion": "success"}
        with self.assertRaises(d.Blocked):
            d.select_run([good, {**good, "id": 2, "conclusion": "failure"}], "ci.yml", SHA, "candidate")

    def test_reject_other_ref_and_deletion(self):
        cfg = {"repository": "owner/repo"}
        with patch.object(d, "git", return_value="https://github.com/owner/repo.git"), patch.object(d, "clean"):
            for sha, target in (("0"*40, "refs/heads/main"), (SHA, "refs/tags/v1")):
                with self.assertRaises(d.Blocked):
                    d.pre_push([f"HEAD {sha} {target} {'b'*40}"], "origin", cfg)

    def test_main_advanced_blocks_before_api(self):
        with patch.object(d, "git", side_effect=["https://github.com/owner/repo.git", "c"*40]), \
             patch.object(d, "clean"), patch.object(d.subprocess, "run"), patch.object(d, "API") as api:
            with self.assertRaises(d.Blocked):
                d.pre_push([f"HEAD {SHA} refs/heads/main {'b'*40}"], "origin", {"repository": "owner/repo"})
            api.assert_not_called()

    def test_preview_exact_sha_not_production(self):
        class Fake:
            def pages(self, path):
                if path.startswith("/deployments?"):
                    return [{"id": 1, "sha": SHA, "environment": "Preview", "production_environment": False, "creator": {"login": "vercel[bot]"}}]
                return [{"state": "success", "environment_url": "https://candidate-unique.vercel.app"}]
        self.assertEqual(d.preview_url(Fake(), SHA), "https://candidate-unique.vercel.app")

    def test_preview_missing_blocks(self):
        class Fake:
            def pages(self, path):
                return []
        with self.assertRaises(d.Blocked):
            d.preview_url(Fake(), SHA)

    def test_morning_dry_run_routing(self):
        self.assertFalse(d.needs_morning_preview(["AGENTS.md", "_delivery.py", "tests/test_email.py"]))
        for path in ("morning_report.py", "news_rules.py", "requirements.lock", ".github/workflows/morning-report-b.yml"):
            self.assertTrue(d.needs_morning_preview([path]))


if __name__ == "__main__":
    unittest.main()
