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


if __name__ == "__main__":
    unittest.main()
