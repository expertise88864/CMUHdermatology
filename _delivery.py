#!/usr/bin/env python3
"""Remote-first delivery gate. CI evidence is never inferred from local tests."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parent
SHA = re.compile(r"^[0-9a-f]{40}$")
ZERO = "0" * 40


class Blocked(RuntimeError):
    """Missing or unsuccessful evidence; do not publish."""


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", *args], cwd=ROOT, text=True, encoding="utf-8"
    ).strip()


def policy() -> dict:
    cfg = json.loads((ROOT / "_delivery_policy.json").read_text(encoding="utf-8"))
    validate_policy(cfg)
    return cfg


def validate_policy(cfg: dict) -> None:
    entries = cfg.get("workflows")
    if not isinstance(entries, list) or not entries:
        raise Blocked("A nonempty workflow contract is required")
    paths = set()
    for entry in entries:
        path = entry.get("path", "")
        if not re.fullmatch(r"\.github/workflows/[A-Za-z0-9_-]+\.yml", path) or path in paths:
            raise Blocked("Invalid or duplicate required workflow")
        paths.add(path)
        jobs = entry.get("jobs")
        if not isinstance(jobs, list) or not jobs or any(not isinstance(j, str) or not j for j in jobs):
            raise Blocked("Each workflow needs named required jobs")
        if any(not entry.get("steps", {}).get(job, {}).get("required") for job in jobs):
            raise Blocked("Required jobs need explicit validation step contracts")
    if cfg.get("allow_dispatch", False) not in (True, False):
        raise Blocked("Invalid dispatch policy")


def check_sha(sha: str) -> str:
    if not SHA.fullmatch(sha) or sha == ZERO:
        raise Blocked("A full, nonzero commit SHA is required")
    return sha


def clean(sha: str) -> None:
    check_sha(sha)
    if git("rev-parse", "HEAD") != sha or git("status", "--porcelain", "--untracked-files=all"):
        raise Blocked("Use the clean, exact candidate revision; preserve other work separately")


def token() -> str:
    value = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if value:
        return value
    # Use the already configured Git credential helper. Never print/store credentials.
    result = subprocess.run(
        ["git", "credential", "fill"], cwd=ROOT,
        input="protocol=https\nhost=github.com\n\n", text=True,
        capture_output=True, timeout=15,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"},
    )
    if result.returncode == 0:
        return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line).get("password", "")
    return ""


class API:
    def __init__(self, repo: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise Blocked("Invalid GitHub repository")
        self.base = "https://api.github.com/repos/" + repo
        self.auth = token()

    def get(self, path: str):
        if not path.startswith("/") or "://" in path:
            raise Blocked("Only relative repository API paths are accepted")
        headers = {"User-Agent": "remote-ci-delivery", "Accept": "application/vnd.github+json"}
        if self.auth:
            headers["Authorization"] = "Bearer " + self.auth
        request = urllib.request.Request(self.base + path, headers=headers)
        # A redirected API URL must never carry the user's credential elsewhere.
        opener = urllib.request.build_opener(NoRedirect)
        with opener.open(request, timeout=30) as response:
            return json.load(response)

    def pages(self, path: str, key: str | None = None) -> list:
        rows = []
        for page in range(1, 21):
            data = self.get(path + ("&" if "?" in path else "?") + f"per_page=100&page={page}")
            items = data[key] if key else data
            if not isinstance(items, list) or any(not isinstance(x, dict) for x in items):
                raise Blocked("Invalid GitHub API response")
            rows.extend(items)
            if len(items) < 100:
                return rows
        raise Blocked("API pagination exceeded bound; evidence is incomplete")


def assess_jobs(jobs: list, required: list, allowed_skips: list,
                contracts: dict | None = None, phase: str = "candidate") -> None:
    if not jobs or not set(required).issubset({j.get("name") for j in jobs}):
        raise Blocked("Required jobs are absent")
    for job in jobs:
        name = job.get("name")
        conclusion = job.get("conclusion")
        if job.get("status") != "completed":
            raise Blocked(f"Job not complete: {name}")
        if conclusion == "skipped" and name in allowed_skips:
            continue
        if conclusion != "success":
            raise Blocked(f"Job not successful: {name}: {conclusion}")
        if contracts is not None:
            contract = contracts.get(name)
            if not contract or not contract.get("required"):
                raise Blocked(f"Missing step contract: {name}")
            needed = contract["required"] + contract.get(phase + "_required", [])
            steps = job.get("steps")
            if not isinstance(steps, list) or not steps:
                raise Blocked(f"Missing step evidence: {name}")
            for step_name in needed:
                matches = [s for s in steps if s.get("name") == step_name]
                if (len(matches) != 1 or matches[0].get("status") != "completed"
                        or matches[0].get("conclusion") != "success"):
                    raise Blocked(f"Required step absent/incomplete/not successful: {name} / {step_name}")
        # continue-on-error must not hide a failed executed step.
        for step in job.get("steps", []):
            if step.get("conclusion") in ("failure", "cancelled", "timed_out", "action_required"):
                raise Blocked(f"Failed step: {name} / {step.get('name')}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Blocked("GitHub API redirects are not accepted")


def select_run(runs: list, path: str, sha: str, phase: str, allow_dispatch: bool = False) -> dict:
    matches = [
        r for r in runs
        if r.get("head_sha") == sha and r.get("path") == path
        and (r.get("event") == "push" or (allow_dispatch and r.get("event") == "workflow_dispatch"))
        and ((r.get("head_branch") == "main") if phase == "main"
             else str(r.get("head_branch", "")).startswith("codex/"))
    ]
    if not matches:
        raise Blocked(f"No exact-SHA {phase} push run for {path}")
    run = max(matches, key=lambda r: (r["id"], r.get("run_attempt", 1)))
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise Blocked(f"{path}: {run.get('status')}/{run.get('conclusion')} ({run.get('html_url')})")
    return run


def verify(sha: str, phase: str, cfg: dict, api: API) -> list:
    check_sha(sha)
    validate_policy(cfg)
    runs = api.pages(f"/actions/runs?head_sha={sha}", "workflow_runs")
    evidence = []
    for entry in cfg["workflows"]:
        run = select_run(runs, entry["path"], sha, phase, cfg.get("allow_dispatch", False))
        jobs = api.pages(f"/actions/runs/{run['id']}/attempts/{run.get('run_attempt', 1)}/jobs", "jobs")
        assess_jobs(jobs, entry["jobs"], entry.get(phase + "_skips", []), entry["steps"], phase)
        evidence.append({"sha": sha, "run_id": run["id"], "url": run["html_url"],
                         "jobs": [{"name": j["name"], "id": j["id"],
                                   "conclusion": j["conclusion"]} for j in jobs]})
    if phase == "candidate" and cfg.get("require_pr"):
        prs = api.pages(f"/commits/{sha}/pulls")
        if not any(p.get("state") == "open" and p.get("head", {}).get("sha") == sha
                   and p.get("base", {}).get("ref") == "main"
                   and p.get("head", {}).get("repo", {}).get("full_name") == cfg["repository"] for p in prs):
            raise Blocked("An open, same-repository PR for this exact candidate is required")
    if phase == "candidate" and cfg.get("morning_dry_run"):
        base = check_sha(api.get("/git/ref/heads/main")["object"]["sha"])
        changed = git("diff", "--name-only", base, sha).splitlines()
        if needs_morning_preview(changed):
            candidates = [r for r in runs if r.get("head_sha") == sha
                          and r.get("path") == ".github/workflows/ci.yml"
                          and r.get("event") == "workflow_dispatch"
                          and str(r.get("head_branch", "")).startswith("codex/")]
            if not candidates:
                raise Blocked("Production pipeline changed: dispatch CI dry-run-preview on this candidate")
            run = max(candidates, key=lambda r: r["id"])
            if run.get("status") != "completed" or run.get("conclusion") != "success":
                raise Blocked("Required morning dry-run-preview has not succeeded")
            jobs = api.pages(f"/actions/runs/{run['id']}/attempts/{run.get('run_attempt', 1)}/jobs", "jobs")
            ci = next(e for e in cfg["workflows"] if e["path"] == ".github/workflows/ci.yml")
            assess_jobs(jobs, ["test", "dry-run-preview"], [], ci["steps"])
            evidence.append({"sha": sha, "dry_run_id": run["id"], "url": run["html_url"]})
    return evidence


def needs_morning_preview(paths: list[str]) -> bool:
    """Conservative production-change routing; docs/tests/gate-only need no paid LLM run."""
    for path in paths:
        if path.startswith(("tests/", "docs/", ".githooks/")) or path in ("_delivery.py", "_test_delivery.py"):
            continue
        if path.startswith("tools/claude_"):
            continue
        if path.endswith(".py") or path.startswith("requirements"):
            return True
        if path.startswith(".github/workflows/") and path != ".github/workflows/delivery.yml":
            return True
    return False


def deployment_url(api: API, sha: str, environment: str = "preview") -> str:
    check_sha(sha)
    deployments = api.pages(f"/deployments?sha={sha}")
    for deployment in sorted(deployments, key=lambda d: d["id"], reverse=True):
        if (deployment.get("sha") != sha
                or str(deployment.get("environment", "")).lower() != environment
                or (environment == "preview" and deployment.get("production_environment") is not False)):
            continue
        if str(deployment.get("creator", {}).get("login", "")).lower() not in ("vercel[bot]", "vercel"):
            continue
        statuses = api.pages(f"/deployments/{deployment['id']}/statuses")
        if not statuses or statuses[0].get("state") != "success":
            continue
        url = statuses[0].get("environment_url", "")
        parsed = urllib.parse.urlparse(url)
        if (parsed.scheme == "https" and parsed.hostname
                and parsed.hostname.endswith(".vercel.app")
                and not parsed.username and not parsed.password and parsed.port is None
                and parsed.path in ("", "/") and not parsed.query and not parsed.fragment):
            return url.rstrip("/")
    raise Blocked(f"No successful Vercel {environment} deployment bound to this SHA")


def preview_url(api: API, sha: str) -> str:
    return deployment_url(api, sha, "preview")


def pre_push(lines: list[str], remote: str, cfg: dict) -> None:
    expected = "https://github.com/" + cfg["repository"] + ".git"
    if remote != "origin" or git("remote", "get-url", "--push", remote) != expected:
        raise Blocked("Unexpected push destination")
    for line in lines:
        fields = line.split()
        if len(fields) != 4:
            raise Blocked("Malformed pre-push ref update")
        _, sha, target, previous = fields
        check_sha(sha)
        clean(sha)
        if target == "refs/heads/main":
            if not SHA.fullmatch(previous) or previous == ZERO:
                raise Blocked("Refuse creating/replacing production main")
            # The advertised remote SHA must be present AND an ancestor.
            subprocess.run(["git", "fetch", "--no-tags", remote, "main"], cwd=ROOT, check=True)
            if git("rev-parse", "FETCH_HEAD") != previous:
                raise Blocked("Main advanced while preparing publication; fetch/integrate/retest")
            subprocess.run(["git", "merge-base", "--is-ancestor", previous, sha], cwd=ROOT, check=True)
            evidence = verify(sha, "candidate", cfg, API(cfg["repository"]))
            clean(sha)
            print(json.dumps(evidence, ensure_ascii=True))
        elif target.startswith("refs/heads/codex/"):
            if previous != ZERO:
                check_sha(previous)
                subprocess.run(["git", "fetch", "--no-tags", remote, target], cwd=ROOT, check=True)
                if git("rev-parse", "FETCH_HEAD") != previous:
                    raise Blocked("Candidate advanced; integrate and revalidate")
                subprocess.run(["git", "merge-base", "--is-ancestor", previous, sha], cwd=ROOT, check=True)
            # Exact committed candidate only; no full local CI requirement.
            subprocess.run([sys.executable, "-m", "unittest", "_test_delivery"], cwd=ROOT, check=True)
            subprocess.run(["git", "diff", "--check", sha + "^", sha], cwd=ROOT, check=True)
            clean(sha)
        else:
            raise Blocked("Only codex/* candidates and verified main promotions are supported")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("pre-push", "verify", "preview", "production"))
    parser.add_argument("sha", nargs="?")
    parser.add_argument("--phase", choices=("candidate", "main"), default="candidate")
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--wait", type=int, default=0, help="Bounded seconds; emits only changed state")
    args = parser.parse_args()
    try:
        cfg = policy()
        if args.command == "pre-push":
            lines = list(sys.stdin)
            pre_push(lines, args.remote, cfg)
            if cfg.get("claude_hook"):
                return subprocess.run(
                    [sys.executable, "tools/claude_diff_review.py", "push", "--remote-name", args.remote],
                    cwd=ROOT, input="".join(lines), text=True, encoding="utf-8",
                ).returncode
            return 0
        sha = check_sha(args.sha or git("rev-parse", "HEAD"))
        api = API(cfg["repository"])
        deadline = time.monotonic() + min(max(args.wait, 0), 3600)
        previous = ""
        while True:
            try:
                if args.command in ("preview", "production"):
                    print(deployment_url(api, sha, args.command))
                else:
                    print(json.dumps(verify(sha, args.phase, cfg, api), ensure_ascii=True))
                return 0
            except Blocked as exc:
                message = str(exc)
                if message != previous:
                    print(message, file=sys.stderr)
                    previous = message
                if time.monotonic() >= deadline:
                    return 1
                time.sleep(min(30, max(0, deadline - time.monotonic())))
    except (Blocked, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        print(f"DELIVERY BLOCKED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
