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


class ReviewerProfileTests(unittest.TestCase):
    """User decision 2026-11-18: every review runs gpt-6.2-astra / medium."""

    def test_pr_review_matches_the_wrapper_and_the_delivery_gate(self):
        spec = importlib.util.spec_from_file_location(
            "codex_review_identity", ROOT / "tools" / "codex_review_identity.py")
        identity = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(identity)
        spec = importlib.util.spec_from_file_location("_delivery_gate", ROOT / "_delivery.py")
        delivery = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(delivery)
        self.assertEqual((pr.MODEL, pr.EFFORT), ("gpt-6.2-astra", "medium"))
        self.assertEqual((identity.MODEL, identity.EFFORT), (pr.MODEL, pr.EFFORT))
        self.assertEqual(pr.CONTEXT, "codex-review/gpt-6.2-astra")
        self.assertEqual(delivery.REVIEW_CONTEXT, pr.CONTEXT)
        wrapper = (ROOT / "tools" / "codex_review.sh").read_text(encoding="utf-8")
        self.assertIn('MODEL="gpt-6.2-astra"', wrapper)
        self.assertNotIn('EFFORT="xhigh"', wrapper)


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

    def test_backfill_recovers_the_base_of_a_retired_reviewer_status(self):
        # User decision 2026-11-18: a pre-switch quota-pending status lives only under
        # codex-review/gpt-6.1-sol; its base still names the range to backfill.
        import subprocess
        from unittest.mock import patch
        legacy = [{"id": 5, "context": "codex-review/gpt-6.1-sol", "creator": {"login": "owner"},
                   "description": f"quota-pending base={'e' * 40}"},
                  {"id": 6, "context": "codex-review/gpt-6.1-sol", "creator": {"login": "intruder"},
                   "description": f"quota-pending base={'f' * 40}"}]
        with patch.object(pr.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)):
            self.assertEqual(pr.reviewed_base(FakeAPI(lambda path: legacy), SHA), "e" * 40)
            current = legacy + [{"id": 1, "context": pr.CONTEXT, "creator": {"login": "owner"},
                                 "description": f"REQUEST_CHANGES base={'b' * 40}"}]
            self.assertEqual(pr.reviewed_base(FakeAPI(lambda path: current), SHA), "b" * 40)
        # A legacy base that is not a strict ancestor of the head is ignored.
        with patch.object(pr.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)):
            self.assertEqual(pr.reviewed_base(FakeAPI(lambda path: legacy), SHA), "")
        self.assertEqual(pr.reviewed_base(FakeAPI(lambda path: [dict(legacy[0], description=
                         f"quota-pending base={SHA}")]), SHA), "")


class HistoricalHeadAndBaseTests(unittest.TestCase):
    """Backfill with current tooling; --base widens only (user decision 2026-11-18)."""

    def _repo(self, tmp):
        import subprocess
        root = Path(tmp)
        run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True,  # noqa: E731
                                        capture_output=True, text=True).stdout.strip()
        run("init", "-q")
        shas = []
        for i in range(4):
            run("-c", "user.name=F", "-c", "user.email=f@example.invalid",
                "commit", "--allow-empty", "-qm", f"c{i}")
            shas.append(run("rev-parse", "HEAD"))
        return root, shas

    def test_historical_head_is_reviewed_from_a_newer_checkout(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root, shas = self._repo(tmp)
            with patch.object(pr, "ROOT", root):
                self.assertEqual(pr.historical_head({"number": 25, "head": {"sha": shas[2]}}, shas[3]),
                                 shas[2])
                with patch.object(pr.subprocess, "run", wraps=pr.subprocess.run) as run, \
                        self.assertRaises((pr.Blocked, pr.subprocess.CalledProcessError)):
                    pr.historical_head({"number": 25, "head": {"sha": "9" * 40}}, shas[3])
                self.assertTrue(any("pull/25/head" in c.args[0] for c in run.call_args_list))
                with self.assertRaises(pr.Blocked):
                    pr.historical_head({"number": 25, "head": {"sha": "nope"}}, shas[3])

    def test_base_only_widens(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root, shas = self._repo(tmp)
            with patch.object(pr, "ROOT", root):
                self.assertEqual(pr.widened_base(shas[0], shas[2], shas[3]), shas[0])
                self.assertEqual(pr.widened_base(shas[2], shas[2], shas[3]), shas[2])
                for requested in (shas[3], "9" * 40):
                    with self.subTest(requested=requested), self.assertRaises((pr.Blocked, pr.subprocess.CalledProcessError)):
                        pr.widened_base(requested, shas[2], shas[3])
                with self.assertRaises(pr.Blocked):   # newer than the merge-base: narrows
                    pr.widened_base(shas[2], shas[1], shas[3])


class ResumePlanTests(unittest.TestCase):
    """--resume keeps the first round's base and needs its full-prompt proof (2026-11-18)."""

    def _repo(self, tmp):
        import subprocess
        root = Path(tmp)
        run = lambda *a: subprocess.run(["git", "-C", str(root), *a], check=True,  # noqa: E731
                                        capture_output=True, text=True).stdout.strip()
        run("init", "-q")
        shas = []
        for i in range(4):
            run("-c", "user.name=F", "-c", "user.email=f@example.invalid",
                "commit", "--allow-empty", "-qm", f"c{i}")
            shas.append(run("rev-parse", "HEAD"))
        return root, shas

    def test_resume_plan(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root, (a, b, h1, h2) = self._repo(tmp)
            saved = {"repo": "o/r", "pr": 26, "session": "s", "head": h1, "base": b,
                     "verdict": "REQUEST_CHANGES", "prompt_verified": True}
            with patch.object(pr, "ROOT", root):
                self.assertEqual(pr.resume_plan(saved, 26, "o/r", "s", h2, False, None), ("s", h1, b))
                self.assertEqual(pr.resume_plan(saved, 26, "o/r", "s", h2, False, b), ("s", h1, b))
                bad = (({**saved, "prompt_verified": False}, None),   # first round unproven
                       ({k: v for k, v in saved.items() if k != "prompt_verified"}, None),
                       ({k: v for k, v in saved.items() if k != "base"}, None),
                       (saved, a),                                     # widening on resume
                       ({**saved, "pr": 25}, None))
                for plan_saved, requested in bad:
                    with self.subTest(saved=plan_saved, requested=requested), self.assertRaises(pr.Blocked):
                        pr.resume_plan(plan_saved, 26, "o/r", "s", h2, False, requested)


class ReviewLockTests(unittest.TestCase):
    def test_second_review_in_the_same_checkout_is_blocked(self):
        import tempfile
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp, patch.object(pr, "ROOT", Path(tmp)):
            lock = pr.acquire_lock()
            with self.assertRaises(pr.Blocked):
                pr.acquire_lock()
            pr.release_lock(lock)
            pr.release_lock(pr.acquire_lock())
            self.assertFalse(lock.exists())

    def test_main_is_pinned_before_fetching_a_pr_head(self):
        source = (ROOT / "tools" / "pr_review.py").read_text(encoding="utf-8")
        pinned = source.index('main_sha = git("rev-parse", "FETCH_HEAD")')
        self.assertLess(pinned, source.index("head = historical_head(pr, head)"))
        self.assertIn('base = git("merge-base", main_sha, head)', source)
        self.assertNotIn('git("merge-base", git("rev-parse", "FETCH_HEAD"), head)', source)


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
        assert '"head": head,' in main_src and "return session, resume_base(saved, head, rebuttal), base" in main_src


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
                     "model": pr.MODEL, "effort": pr.EFFORT, "prompt_verified": True,
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


    def test_retry_refuses_a_verdict_from_another_reviewer_profile(self):
        # User decision 2026-11-18: a verdict saved by gpt-6.1-sol / xhigh (or with no
        # profile at all) must not be posted as a codex-review/gpt-6.2-astra status.
        import tempfile
        from unittest.mock import patch
        base = {"repo": "owner/repo", "pr": 7, "verdict": "APPROVE", "base": "c" * 40, "head": SHA,
                "session": "s", "bound_session": "s", "message": "APPROVE", "resume": False}
        for profile in ({}, {"model": "gpt-6.1-sol", "effort": "xhigh", "prompt_verified": True},
                        {"model": pr.MODEL, "effort": "xhigh", "prompt_verified": True},
                        {"model": "gpt-6.1-sol", "effort": pr.EFFORT, "prompt_verified": True},
                        # Saved by e1edffe (PR #25) before prompt proof existed: may be truncated.
                        {"model": pr.MODEL, "effort": pr.EFFORT},
                        {"model": pr.MODEL, "effort": pr.EFFORT, "prompt_verified": False}):
            with self.subTest(profile=profile), tempfile.TemporaryDirectory() as tmp:
                unrecorded = Path(tmp) / "u.json"
                unrecorded.write_text(json.dumps({**base, **profile}), encoding="utf-8")
                with patch.object(pr, "record") as record, self.assertRaises(pr.Blocked):
                    pr.retry_record(FakeAPI(lambda path: None), {"number": 7}, SHA,
                                    Path(tmp) / "b.json", unrecorded)
                record.assert_not_called()
                self.assertFalse(pr.current_profile({**base, **profile}))
        self.assertTrue(pr.current_profile({"model": pr.MODEL, "effort": pr.EFFORT,
                                            "verdict": "APPROVE", "prompt_verified": True}))
        # Quota/error records have no review turn to prove; profile alone decides.
        self.assertTrue(pr.current_profile({"model": pr.MODEL, "effort": pr.EFFORT,
                                            "verdict": "QUOTA_PENDING"}))


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


DIFF_SAMPLES = ("diff --git a/x.py b/x.py", "    diff --git a/x.py b/x.py", "\t@@ -1,2 +1,2 @@",
                "  index 1234abc..5678def 100644", "    --- a/x.py", "    +++ b/x.py")
EVIDENCE_SAMPLES = ("Finding 1 is REJECTED: _delivery.py:40 already checks the owner.",
                    "---", "See the delivery suite, test_y (line 12) -- it covers this path.")


class RebuttalDiffMarkerTests(unittest.TestCase):
    """Backport of the morning-report PR #18 fix (P2): indented diffs must not pass as rebuttal evidence."""

    def test_python_check_rejects_indented_diffs_and_keeps_evidence(self):
        for line in DIFF_SAMPLES:
            with self.subTest(line=line):
                self.assertTrue(pr.DIFF_MARKER.search(f"evidence\n{line}\nmore"))
        for line in EVIDENCE_SAMPLES:
            with self.subTest(line=line):
                self.assertIsNone(pr.DIFF_MARKER.search(line))

    def test_wrapper_check_rejects_indented_diffs_and_keeps_evidence(self):
        import re
        import shutil
        import subprocess
        import tempfile
        grep = shutil.which("grep")
        if not grep:
            self.skipTest("grep not available")
        text = (ROOT / "tools" / "codex_review.sh").read_text(encoding="utf-8")
        block = text.split('REBUTTAL_FILE="${CODEX_REVIEW_REBUTTAL_FILE:-}"', 1)[1]
        pattern = re.search(r"grep -qE '([^']+)' \"\$REBUTTAL_FILE\"", block).group(1)
        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "rebuttal.txt"
            for line, expected in [(x, 0) for x in DIFF_SAMPLES] + [(x, 1) for x in EVIDENCE_SAMPLES]:
                sample.write_text(f"evidence\n{line}\n", encoding="utf-8")
                with self.subTest(line=line):
                    rc = subprocess.run([grep, "-qE", pattern, str(sample)]).returncode
                    self.assertEqual(rc, expected)
