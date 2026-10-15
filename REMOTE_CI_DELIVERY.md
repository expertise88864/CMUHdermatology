# Remote-first delivery — 2026-09-06

The user explicitly replaced mandatory full local CI before candidate pushes.
Production remains fail-closed. This is a delivery workflow, not permission to
change medical content, visual baselines, credentials, recipients or scoring.

## Candidate
1. Fetch main. Preserve other work. Use an isolated codex/* branch based on current main.
2. Make task-owned changes; run relevant local regression tests, syntax/lint and
   required generators. Commit the exact source and generated artifacts together.
3. Commit freely; there is no per-commit review and no review trailer (user decision
   2026-10-15). Claude Opus 5.5/high applies only to pre-switch pending backfill.
4. Install the tracked pre-push hook with the documented existing hook mechanism.
5. Push the exact SHA to codex/* normally. The hook runs the offline delivery tests.
   Full tests, types, coverage, browsers and generation drift run on GitHub.
6. Websites: create a same-repository PR to main and wait for exact-SHA Preview.
   Do not use production screenshots as proof of candidate correctness.
7. Run: python _delivery.py verify FULL_SHA --phase candidate --wait 1800
8. Review the whole PR once: python tools/pr_review.py (opens the PR if needed).
   gpt-6.1-sol/xhigh read-only reviews merge-base(main, head)..head; the verdict goes to a
   PR comment and to the owner's `codex-review/gpt-6.1-sol` status on the exact head.
   Validate findings, fix only CONFIRMED ones, push, then python tools/pr_review.py --resume
   until APPROVE. A new head needs a new verdict. A finding REJECTED with evidence may be
   rebutted once per head: python tools/pr_review.py --resume --rebuttal FILE (evidence,
   never a diff); the reviewer re-checks it and keeps or withdraws it. Confirmed quota exhaustion records
   quota-pending (publishable, not approval); backfill later via --pending.

## Promotion
Fetch main again. It must be an ancestor of the exact successful candidate;
otherwise integrate without overwriting CMS/user changes and revalidate.
With the clean candidate checked out:
    git push origin FULL_SHA:refs/heads/main
The pre-push hook independently retrieves GitHub evidence (candidate CI plus the PR
review status covering every promoted commit); no cached approval flag,
environment override, skip token or --no-verify is accepted.
Then run:
    python _delivery.py verify FULL_SHA --phase main --wait 1800
Record all applicable CI and actual deployment/smoke evidence separately.

## Project boundaries
- CMUH: full Windows CI, existing Security workflow, version/manifest consistency.
  The updater still reads main; no release source/medical/punch behavior changes.
- Morning report: ordinary candidate tests use fakes and do not send mail. Relevant
  production pipeline changes also require the existing manual dry-run-preview on
  the candidate (DRY_RUN=1, isolated runner, read-only repository permission).
  Read-only previews must not persist state or modify production concurrency.
  Operational state-writing schedules are not automatically redesigned by this tool.
- DermNotes/HsiaoEye: PR and Preview browser must pass before main. Production
  deployment's ignored-build gate allows only exact candidate-CI-green commits.
  Direct CMS saves remain saved in Git but are not automatically published without
  candidate validation. This does not change medical approval requirements.
- HsiaoEye: visual regression uses candidate Preview. Baseline changes require human
  confirmation and Ubuntu-generated artifacts; never auto-update to get a green run.

## Evidence and limits
_delivery_policy.json names required workflows/jobs and successful validation steps.
Missing, duplicated, incomplete or skipped required steps block publication even
when the containing job says success. Conditional phase-only steps are documented.
Only documented conditional
jobs may skip (for example production-only checks on a candidate). A required job
that is missing, unsuccessful or has a failed executed step blocks publication.
New workflows must be classified and added as appropriate, not silently ignored.
All candidate core workflows use push events to bind tests to the exact head SHA;
PR test-merge checks are additional evidence, not substituted SHAs.
A local Git hook protects this checkout, not other machines or server-side writes.
The website Vercel gate protects Git-triggered production builds independently of
local hooks. It does not prevent an administrator from manually disabling the
ignored-build check or promoting a prebuilt deployment; those bypasses are forbidden.
No branch protection settings, CMS saving API or production schedules are silently
changed. Review results live on the PR; no audit commits are created.
GitHub/Vercel outages are blockers, not code bugs to paper over. Keep results compact;
ordinary bounded polling does not need model reasoning on every interval.
