#!/usr/bin/env python3
"""One review per pull request (user decision 2026-10-15).

Reviews the whole PR range merge-base(main, head)..head with the repository's
read-only Codex wrapper (gpt-6.1-sol / xhigh, identity verified by the wrapper),
then records the outcome on GitHub instead of in commits:

* a PR comment with the verdict, exact base/head SHAs, session and findings;
* a commit status ``codex-review/gpt-6.1-sol`` on the exact head SHA, which
  ``_delivery.py`` checks before promoting that SHA to main.

Usage (clean checkout of the exact, already pushed codex/* head):
    python tools/pr_review.py                       # first round; opens the PR if missing
    python tools/pr_review.py --resume              # after fixing CONFIRMED findings
    python tools/pr_review.py --mode deep --ctx task.txt
    python tools/pr_review.py --pr 12               # backfill a PR (HEAD must equal its head)
    python tools/pr_review.py --pending             # list PRs whose head is not approved

Exit codes: 0 APPROVE, 2 REQUEST_CHANGES, 3 quota pending, 1 blocked/error.
Quota pending is not approval; rerun when quota returns (same PR, same head).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = Path(__file__).resolve().parent.parent
CONTEXT = "codex-review/gpt-6.1-sol"
MODEL = "gpt-6.1-sol"
EFFORT = "xhigh"
MARKER = "<!-- codex-pr-review -->"
SHA = re.compile(r"^[0-9a-f]{40}$")
QUOTA = re.compile(r"usage limit|rate limit|try again at|quota", re.I)
BASE_IN_STATUS = re.compile(r"base=([0-9a-f]{40})")
MAX_COMMENT = 60000


class Blocked(RuntimeError):
    pass


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True, encoding="utf-8").strip()


def repository() -> str:
    repo = json.loads((ROOT / "_delivery_policy.json").read_text(encoding="utf-8"))["repository"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise Blocked("Invalid repository in _delivery_policy.json")
    return repo


def token() -> str:
    value = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if value:
        return value
    result = subprocess.run(
        ["git", "credential", "fill"], cwd=ROOT, input="protocol=https\nhost=github.com\n\n",
        text=True, capture_output=True, timeout=30,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"},
    )
    found = dict(l.split("=", 1) for l in result.stdout.splitlines() if "=" in l).get("password", "")
    if result.returncode != 0 or not found:
        raise Blocked("No GitHub credential available (git credential helper or GH_TOKEN)")
    return found


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Blocked("GitHub API redirects are not accepted")


class API:
    def __init__(self, repo: str):
        self.repo = repo
        self.base = "https://api.github.com/repos/" + repo
        self.auth = token()

    def call(self, method: str, path: str, body: dict | None = None):
        if not path.startswith("/") or "://" in path:
            raise Blocked("Only relative repository API paths are accepted")
        request = urllib.request.Request(
            self.base + path, method=method,
            data=None if body is None else json.dumps(body).encode("utf-8"),
            headers={"Authorization": "Bearer " + self.auth, "User-Agent": "pr-review",
                     "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=60) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise Blocked(f"GitHub API {method} {path}: HTTP {exc.code} {exc.read()[:300]!r}") from exc
        return json.loads(raw) if raw else None


def bash() -> str:
    """Git for Windows' bash; System32\\bash.exe is WSL and cannot run the wrapper."""
    if os.name == "nt":
        exec_path = Path(git("--exec-path"))
        for parent in exec_path.parents:
            candidate = parent / "bin" / "bash.exe"
            if candidate.is_file():
                return str(candidate)
    found = shutil.which("bash")
    if not found:
        raise Blocked("bash not found")
    return found


def wrapper() -> str:
    for rel in ("tools/codex_review.sh", "scripts/codex_review.sh"):
        if (ROOT / rel).is_file():
            return rel
    raise Blocked("No Codex review wrapper (tools/ or scripts/codex_review.sh) in this repository")


def find_pr(api: API, branch: str, number: int | None) -> dict | None:
    if number:
        return api.call("GET", f"/pulls/{number}")
    owner = api.repo.split("/")[0]
    rows = api.call("GET", f"/pulls?state=open&base=main&head={owner}:{branch}")
    return rows[0] if rows else None


def open_pr(api: API, branch: str, base: str, head: str) -> dict:
    subjects = git("log", "--reverse", "--format=%s", f"{base}..{head}").splitlines()
    title = subjects[0] if len(subjects) == 1 else (subjects[0] if subjects else branch)
    body = ("Opened by tools/pr_review.py for one whole-PR review "
            f"(`{MODEL}` / `{EFFORT}`, read-only). The verdict is posted below and as the "
            f"`{CONTEXT}` commit status on the exact head SHA.\n\nCommits:\n"
            + "\n".join(f"- {s}" for s in subjects)
            + "\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)")
    return api.call("POST", "/pulls", {"title": title[:200], "head": branch, "base": "main", "body": body})


def provider_quota_error(raw: str) -> bool:
    """Quota only from the CLI's own error events, never from reviewed content.

    Agent messages and command output (for example a diff mentioning "quota")
    arrive as other JSON events and must not turn a failure into quota-pending."""
    for line in raw.splitlines():
        text = line.strip()
        if not text or text.startswith("[codex-review]"):
            continue
        try:
            event = json.loads(text)
        except ValueError:
            event = None
        if isinstance(event, dict):
            if event.get("type") not in ("error", "turn.failed"):
                continue
            text = json.dumps(event.get("message") or event.get("error") or "", ensure_ascii=False)
        elif not text.upper().startswith("ERROR"):
            continue
        if QUOTA.search(text):
            return True
    return False


def run_review(mode: str, base: str, head: str, ctx: str | None,
               resume_session: str | None) -> tuple[str, str, str]:
    """Return (verdict, session_id, last_message)."""
    env = {**os.environ, "CODEX_REVIEW_TIP": head, "CODEX_REVIEW_BASE": base}
    args = [bash(), wrapper()]
    args += ["resume", resume_session] if resume_session else [mode, base] + ([ctx] if ctx else [])
    rc = subprocess.run(args, cwd=ROOT, env=env, stdin=subprocess.DEVNULL).returncode
    state = ROOT / ".codex-review"
    message = (state / "last_message.txt").read_text(encoding="utf-8", errors="replace") \
        if (state / "last_message.txt").is_file() else ""
    raw = (state / "last_raw.log").read_text(encoding="utf-8", errors="replace") \
        if (state / "last_raw.log").is_file() else ""
    session = (state / "last_session_id").read_text(encoding="utf-8").strip() \
        if (state / "last_session_id").is_file() else "unavailable"
    if rc == 0:
        return "APPROVE", session, message
    if rc == 2:
        return "REQUEST_CHANGES", session, message
    if not message.strip() and provider_quota_error(raw):
        return "QUOTA_PENDING", session, raw[-2000:]
    return "ERROR", session, message or raw[-2000:]


def record(api: API, pr: dict, verdict: str, base: str, head: str, session: str,
           message: str, resume: bool) -> None:
    state = {"APPROVE": "success", "REQUEST_CHANGES": "failure",
             "QUOTA_PENDING": "pending", "ERROR": "error"}[verdict]
    prefix = "quota-pending" if verdict == "QUOTA_PENDING" else verdict
    api.call("POST", f"/statuses/{head}", {
        "state": state, "context": CONTEXT,
        "description": f"{prefix} base={base}"[:140],
        "target_url": pr["html_url"]})
    findings = message.strip() or "(no review message)"
    if len(findings) > MAX_COMMENT:
        findings = findings[:MAX_COMMENT] + "\n\n…(truncated)"
    note = {"QUOTA_PENDING": "Codex quota exhausted. **Pending is not approval**; rerun "
                             "`python tools/pr_review.py` on this head when quota returns.",
            "ERROR": "Review did not produce a verified verdict; this round does not count."}.get(verdict, "")
    body = (f"{MARKER}\n### Codex PR review: **{verdict}**\n\n"
            f"| | |\n|---|---|\n| model / effort | `{MODEL}` / `{EFFORT}` (read-only, identity verified) |\n"
            f"| range | `{base}..{head}` |\n| round | {'resume' if resume else 'first'} |\n"
            f"| session | `{session}` |\n\n"
            + (note + "\n\n" if note else "")
            + "Findings are validated by Claude Code before any fix (CONFIRMED / REJECTED / UNCERTAIN).\n\n"
            + "<details><summary>Review output</summary>\n\n```text\n"
            + findings.replace("```", "ˋˋˋ") + "\n```\n</details>")
    api.call("POST", f"/issues/{pr['number']}/comments", {"body": body})


def reviewed_base(api: API, sha: str) -> str:
    """Base of the newest owner review status on this head (any verdict), or ''."""
    owner = api.repo.split("/")[0].lower()
    rows = [s for s in api.call("GET", f"/commits/{sha}/statuses?per_page=100") or []
            if s.get("context") == CONTEXT
            and str(s.get("creator", {}).get("login", "")).lower() == owner]
    for status in sorted(rows, key=lambda s: s["id"], reverse=True):
        match = BASE_IN_STATUS.search(str(status.get("description", "")))
        if match:
            return match.group(1)
    return ""


def latest_status(api: API, sha: str) -> dict | None:
    owner = api.repo.split("/")[0].lower()
    rows = api.call("GET", f"/commits/{sha}/statuses?per_page=100") or []
    mine = [s for s in rows if s.get("context") == CONTEXT
            and str(s.get("creator", {}).get("login", "")).lower() == owner]
    return max(mine, key=lambda s: s["id"]) if mine else None


def all_prs(api: API) -> list:
    rows = []
    for page in range(1, 51):
        batch = api.call("GET", f"/pulls?state=all&base=main&per_page=100&page={page}") or []
        rows.extend(batch)
        if len(batch) < 100:
            return rows
    raise Blocked("More than 5000 PRs; pending list would be incomplete")


def list_pending(api: API) -> int:
    rows = all_prs(api)
    count = 0
    for pr in rows:
        head = pr["head"]["sha"]
        status = latest_status(api, head)
        if status and status["state"] == "success":
            continue
        label = status["description"] if status else "never reviewed"
        merged = "merged" if pr.get("merged_at") else pr["state"]
        print(f"#{pr['number']} [{merged}] {head[:12]} {label} — {pr['title']}")
        count += 1
    print(f"{count} PR(s) without an APPROVE status on their head")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", choices=("diff", "targeted", "deep"), default="targeted")
    parser.add_argument("--ctx", help="task-context file (summary, acceptance, tests; never a diff)")
    parser.add_argument("--resume", action="store_true", help="continue the recorded session")
    parser.add_argument("--pr", type=int, help="PR number (default: open PR of the current branch)")
    parser.add_argument("--pending", action="store_true", help="list PRs whose head lacks APPROVE")
    args = parser.parse_args()
    try:
        api = API(repository())
        if args.pending:
            return list_pending(api)
        head = git("rev-parse", "HEAD")
        if git("status", "--porcelain", "--untracked-files=all"):
            raise Blocked("Working tree must be clean: the review covers committed history only")
        subprocess.run(["git", "fetch", "--no-tags", "-q", "origin", "main"], cwd=ROOT, check=True)
        if args.pr:
            pr = find_pr(api, "", args.pr)
            if pr["head"]["sha"] != head:
                raise Blocked(f"Check out the PR head first: git switch --detach {pr['head']['sha']}")
        else:
            branch = git("branch", "--show-current")
            if not branch.startswith("codex/"):
                raise Blocked("Run on a codex/* branch (or pass --pr for a backfill)")
            remote = subprocess.run(["git", "ls-remote", "origin", f"refs/heads/{branch}"], cwd=ROOT,
                                    capture_output=True, text=True, check=True).stdout.split()
            if not remote or remote[0] != head:
                raise Blocked("Push this exact head to origin first; the PR must show what was reviewed")
            pr = find_pr(api, branch, None)
        base = git("merge-base", git("rev-parse", "FETCH_HEAD"), head)
        if base == head and args.pr:
            # Backfill of a head already published to main: reuse the base the PR's
            # recorded review covered, else the PR's own base.
            base = reviewed_base(api, head) or git("merge-base", pr["base"]["sha"], head)
        if not SHA.fullmatch(base) or base == head:
            raise Blocked("Nothing to review: head has no commits beyond main")
        if pr is None:
            pr = open_pr(api, branch, base, head)
            print(f"[pr-review] opened PR #{pr['number']}: {pr['html_url']}")
        print(f"[pr-review] PR #{pr['number']} range {base[:12]}..{head[:12]} model={MODEL}/{EFFORT}")
        # The wrapper resumes the session recorded in this checkout; make sure that
        # session belongs to this PR, never to another PR reviewed here earlier.
        binding = ROOT / ".codex-review" / "pr_review.json"
        resume_session = None
        if args.resume:
            saved = json.loads(binding.read_text(encoding="utf-8")) if binding.is_file() else {}
            recorded = ROOT / ".codex-review" / "last_session_id"
            current = recorded.read_text(encoding="utf-8").strip().lower() if recorded.is_file() else ""
            resume_session = str(saved.get("session", "")).lower()
            if saved.get("pr") != pr["number"] or saved.get("repo") != api.repo                     or not resume_session or resume_session != current:
                raise Blocked("The recorded review session is not this PR's; run a first round")
        else:
            binding.unlink(missing_ok=True)
        verdict, session, message = run_review(args.mode, base, head, args.ctx, resume_session)
        if not args.resume and verdict in ("APPROVE", "REQUEST_CHANGES"):
            binding.write_text(json.dumps({"repo": api.repo, "pr": pr["number"], "session": session}),
                               encoding="utf-8")
        record(api, pr, verdict, base, head, session, message, args.resume)
        print(f"[pr-review] {verdict} recorded on PR #{pr['number']} and status {CONTEXT}@{head[:12]}")
        return {"APPROVE": 0, "REQUEST_CHANGES": 2, "QUOTA_PENDING": 3}.get(verdict, 1)
    except (Blocked, OSError, ValueError, KeyError, subprocess.SubprocessError) as exc:
        print(f"[pr-review] BLOCKED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
