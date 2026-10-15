"""tools/pr_review.py: whole-PR review bookkeeping. Offline; no GitHub or Codex calls."""
import importlib.util
import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("pr_review", ROOT / "tools" / "pr_review.py")
pr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pr)

SHA = "a" * 40


class FakeAPI:
    repo = "owner/repo"

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def call(self, method, path, body=None):
        self.calls.append(path)
        return self.routes(path)


class QuotaClassificationTests(unittest.TestCase):
    def test_reviewed_content_mentioning_quota_is_not_a_quota_error(self):
        raw = "\n".join([
            json.dumps({"type": "item.completed", "item": {"type": "command_execution",
                        "aggregated_output": "+QUOTA = re.compile('usage limit|rate limit')"}}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message",
                        "text": "checking quota-pending handling"}}),
            "[codex-review] ERROR: rate limit text in a wrapper line is not provider evidence",
        ])
        self.assertFalse(pr.provider_quota_error(raw))

    def test_provider_error_events_are_quota(self):
        for raw in (json.dumps({"type": "error", "message": "You've hit your usage limit. Try again at 5pm"}),
                    json.dumps({"type": "turn.failed", "error": {"message": "rate limit exceeded"}}),
                    "ERROR: You've hit your usage limit."):
            with self.subTest(raw=raw):
                self.assertTrue(pr.provider_quota_error(raw))

    def test_other_provider_errors_are_not_quota(self):
        self.assertFalse(pr.provider_quota_error(json.dumps({"type": "error", "message": "stream disconnected"})))


class StaleRoundTests(unittest.TestCase):
    def test_wrapper_refusing_early_cannot_reuse_previous_quota_log(self):
        import subprocess
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / ".codex-review"
            state.mkdir()
            (state / "last_raw.log").write_text(json.dumps({"type": "error", "message": "usage limit"}),
                                                encoding="utf-8")
            (state / "last_message.txt").write_text("", encoding="utf-8")
            with patch.object(pr, "ROOT", Path(tmp)), patch.object(pr, "bash", return_value="bash"), \
                 patch.object(pr, "wrapper", return_value="tools/codex_review.sh"), \
                 patch.object(pr.subprocess, "run", return_value=subprocess.CompletedProcess([], 64)):
                verdict, _, _ = pr.run_review("targeted", "b" * 40, SHA, None, None)
            self.assertEqual(verdict, "ERROR")
            self.assertFalse((state / "last_raw.log").exists())


class PendingAndBackfillTests(unittest.TestCase):
    def test_pending_reads_every_page(self):
        pages = {1: [{"n": i} for i in range(100)], 2: [{"n": i} for i in range(100)], 3: [{"n": 0}]}

        def routes(path):
            page = int(path.rsplit("page=", 1)[1])
            return pages[page]
        api = FakeAPI(routes)
        self.assertEqual(len(pr.all_prs(api)), 201)
        self.assertEqual(len(api.calls), 3)

    def test_backfill_reuses_newest_owner_reviewed_base(self):
        statuses = [
            {"id": 1, "context": pr.CONTEXT, "creator": {"login": "owner"},
             "description": f"REQUEST_CHANGES base={'b' * 40}"},
            {"id": 3, "context": pr.CONTEXT, "creator": {"login": "intruder"},
             "description": f"APPROVE base={'d' * 40}"},
            {"id": 2, "context": pr.CONTEXT, "creator": {"login": "Owner"},
             "description": f"quota-pending base={'c' * 40}"},
        ]
        self.assertEqual(pr.reviewed_base(FakeAPI(lambda path: statuses), SHA), "c" * 40)
        self.assertEqual(pr.reviewed_base(FakeAPI(lambda path: []), SHA), "")


class TerminalQuotaTests(unittest.TestCase):
    """PR #17 review round 1 (P1): only the terminal provider error decides quota-pending."""

    def test_recovered_rate_limit_then_other_failure_is_not_quota(self):
        for raw in ("\n".join([json.dumps({"type": "error", "message": "rate limit exceeded, retrying"}),
                               json.dumps({"type": "turn.failed", "error": {"message": "401 Unauthorized"}})]),
                    "ERROR: rate limit exceeded; retrying\nERROR: 401 Unauthorized"):
            with self.subTest(raw=raw):
                self.assertFalse(pr.provider_quota_error(raw))

    def test_recovered_rate_limit_then_crash_or_silence_is_not_quota(self):
        """PR #17 review round 2 (P1): quota-pending needs the run to END on a quota error."""
        recovered = "ERROR: rate limit exceeded; retrying"
        for tail in ("exec git diff --stat\nsucceeded in 120ms", "panic: failed to write final output",
                     json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}})):
            with self.subTest(tail=tail):
                self.assertFalse(pr.provider_quota_error(f"{recovered}\n{tail}\n[codex-review] ERROR: x"))

    def test_terminal_quota_after_other_error_is_quota(self):
        self.assertTrue(pr.provider_quota_error("ERROR: stream disconnected\nERROR: You've hit your usage limit."))


class ResumeScopeTests(unittest.TestCase):
    """PR #17 review round 1 (P2): a resume reviews every commit since the last verdict."""

    def test_resume_base_requires_a_newer_descendant_head(self):
        import subprocess
        from unittest.mock import patch
        prev, head = "b" * 40, SHA
        for saved in ({}, {"head": "short"}, {"head": head}):
            with self.subTest(saved=saved), self.assertRaises(pr.Blocked):
                pr.resume_base(saved, head)
        with patch.object(pr.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)), \
             self.assertRaises(pr.Blocked):
            pr.resume_base({"head": prev}, head)
        with patch.object(pr.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run:
            self.assertEqual(pr.resume_base({"head": prev}, head), prev)
        self.assertEqual(run.call_args.args[0][-2:], [prev, head])

    def test_previous_head_reaches_the_wrapper_only_on_resume(self):
        import os
        import subprocess
        import tempfile
        from unittest.mock import patch
        envs = []

        def fake_run(args, **kwargs):
            envs.append(kwargs["env"])
            return subprocess.CompletedProcess(args, 64)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"CODEX_REVIEW_PREV_TIP": "c" * 40}), \
             patch.object(pr, "ROOT", Path(tmp)), patch.object(pr, "bash", return_value="bash"), \
             patch.object(pr, "wrapper", return_value="tools/codex_review.sh"), \
             patch.object(pr.subprocess, "run", side_effect=fake_run):
            pr.run_review("targeted", "b" * 40, SHA, None, None, "d" * 40)
            pr.run_review("targeted", "b" * 40, SHA, None, "0" * 8 + "-0000-0000-0000-" + "0" * 12, "d" * 40)
        self.assertNotIn("CODEX_REVIEW_PREV_TIP", envs[0])
        self.assertEqual(envs[1]["CODEX_REVIEW_PREV_TIP"], "d" * 40)

    def test_wrapper_widens_the_resume_scope_to_every_new_commit(self):
        text = (ROOT / "tools" / "codex_review.sh").read_text(encoding="utf-8")
        assert 'PREV_TIP="${CODEX_REVIEW_PREV_TIP:-}"' in text
        assert 'merge-base --is-ancestor "$PREV_TIP" "$TIP"' in text
        assert "Review EVERY change in ${PREV_TIP}..${TIP}" in text
        main_src = (ROOT / "tools" / "pr_review.py").read_text(encoding="utf-8")
        assert '"head": head,' in main_src and "previous_head = resume_base(saved, head, bool(rebuttal))" in main_src


if __name__ == "__main__":
    unittest.main()


class RetryingQuotaTests(unittest.TestCase):
    """PR #1 round 4 (P1): an interrupted retry is not terminal quota exhaustion."""

    def test_retrying_or_transient_rate_limit_last_is_not_quota(self):
        for raw in (json.dumps({"type": "error", "message": "rate limit exceeded, retrying"}),
                    json.dumps({"type": "error", "message": "rate limit exceeded"}),
                    json.dumps({"type": "turn.failed", "error": {"message": "usage limit; retrying in 5s"}}),
                    "ERROR: rate limit exceeded"):
            with self.subTest(raw=raw):
                self.assertFalse(pr.provider_quota_error(raw))


class RecordRetryTests(unittest.TestCase):
    """PR #1 round 4 (P2): a failed GitHub recording keeps the verdict for --retry-record."""

    def test_failed_recording_keeps_verdict_and_old_binding_then_retry_records(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            binding = Path(tmp) / "pr_review.json"
            unrecorded = Path(tmp) / "pr_review_unrecorded.json"
            binding.write_text(json.dumps({"head": "b" * 40}), encoding="utf-8")
            entry = {"repo": "owner/repo", "pr": 7, "verdict": "APPROVE", "base": "c" * 40, "head": SHA,
                     "session": "s", "bound_session": "s", "message": "APPROVE", "resume": True}
            unrecorded.write_text(json.dumps(entry), encoding="utf-8")
            api, pull = FakeAPI(lambda path: None), {"number": 7}
            with patch.object(pr, "record", side_effect=pr.Blocked("HTTP 502")), \
                 self.assertRaises(pr.Blocked):
                pr.finish(api, pull, entry, binding, unrecorded)
            self.assertEqual(json.loads(binding.read_text(encoding="utf-8"))["head"], "b" * 40)
            self.assertTrue(unrecorded.is_file())
            with patch.object(pr, "record") as record:
                self.assertEqual(pr.retry_record(api, pull, SHA, binding, unrecorded), 0)
            record.assert_called_once()
            self.assertEqual(json.loads(binding.read_text(encoding="utf-8"))["head"], SHA)
            self.assertFalse(unrecorded.exists())

    def test_retry_refuses_another_head_or_pr(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            unrecorded = Path(tmp) / "u.json"
            unrecorded.write_text(json.dumps({"repo": "owner/repo", "pr": 7, "head": SHA}), encoding="utf-8")
            for pull, head in (({"number": 8}, SHA), ({"number": 7}, "b" * 40)):
                with self.subTest(pull=pull, head=head), self.assertRaises(pr.Blocked):
                    pr.retry_record(FakeAPI(lambda path: None), pull, head, Path(tmp) / "b.json", unrecorded)


class RebuttalTests(unittest.TestCase):
    """User decision 2026-10-15: one evidence-based rebuttal per unchanged head."""

    def test_same_head_needs_rebuttal_of_request_changes_once(self):
        saved = {"head": SHA, "verdict": "REQUEST_CHANGES"}
        with self.assertRaises(pr.Blocked):
            pr.resume_base(saved, SHA)
        self.assertIsNone(pr.resume_base(saved, SHA, rebuttal=True))
        for bad in ({**saved, "verdict": "APPROVE"}, {**saved, "rebutted_head": SHA}, {"head": SHA}):
            with self.subTest(saved=bad), self.assertRaises(pr.Blocked):
                pr.resume_base(bad, SHA, rebuttal=True)

    def test_changed_head_cannot_be_rebutted_before_its_own_verdict(self):
        """PR #2 round 1 (P2): a rebuttal on a new head would skip eligibility and the once-only limit."""
        for saved in ({"head": "b" * 40, "verdict": "APPROVE"},
                      {"head": "b" * 40, "verdict": "REQUEST_CHANGES"}):
            with self.subTest(saved=saved), self.assertRaises(pr.Blocked):
                pr.resume_base(saved, SHA, rebuttal=True)

    def test_rebuttal_reaches_the_wrapper_only_on_resume(self):
        import os
        import subprocess
        import tempfile
        from unittest.mock import patch
        envs = []

        def fake_run(args, **kwargs):
            envs.append(kwargs["env"])
            return subprocess.CompletedProcess(args, 64)
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"CODEX_REVIEW_REBUTTAL_FILE": "stale"}), \
             patch.object(pr, "ROOT", Path(tmp)), patch.object(pr, "bash", return_value="bash"), \
             patch.object(pr, "wrapper", return_value="tools/codex_review.sh"), \
             patch.object(pr.subprocess, "run", side_effect=fake_run):
            pr.run_review("targeted", "b" * 40, SHA, None, None, None, "r.txt")
            pr.run_review("targeted", "b" * 40, SHA, None, "0" * 8 + "-0000-0000-0000-" + "0" * 12, None, "r.txt")
        self.assertNotIn("CODEX_REVIEW_REBUTTAL_FILE", envs[0])
        self.assertEqual(envs[1]["CODEX_REVIEW_REBUTTAL_FILE"], "r.txt")
        self.assertNotIn("CODEX_REVIEW_PREV_TIP", envs[1])

    def test_finish_marks_a_same_head_rebuttal_so_it_cannot_repeat(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            binding, unrecorded = Path(tmp) / "b.json", Path(tmp) / "u.json"
            entry = {"repo": "owner/repo", "pr": 7, "verdict": "REQUEST_CHANGES", "base": "c" * 40, "head": SHA,
                     "session": "s", "bound_session": "s", "message": "", "resume": True,
                     "rebuttal": True, "same_head_rebuttal": True}
            with patch.object(pr, "record") as record:
                pr.finish(FakeAPI(lambda path: None), {"number": 7}, entry, binding, unrecorded)
            self.assertTrue(record.call_args.args[-1])
            saved = json.loads(binding.read_text(encoding="utf-8"))
            self.assertEqual((saved["verdict"], saved["rebutted_head"]), ("REQUEST_CHANGES", SHA))
            with self.assertRaises(pr.Blocked):
                pr.resume_base(saved, SHA, rebuttal=True)

    def test_wrapper_passes_the_rebuttal_and_rejects_diffs(self):
        text = (ROOT / "tools" / "codex_review.sh").read_text(encoding="utf-8")
        assert 'REBUTTAL_FILE="${CODEX_REVIEW_REBUTTAL_FILE:-}"' in text
        assert "Never withdraw a finding on assertion alone" in text
        assert "rebuttal 檔看起來含 diff" in text
