#!/usr/bin/env python3
"""Check the actual Codex review session, rather than trusting requested flags."""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys

UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
# User decision 2026-11-18 (replacing 2026-10-14's gpt-6.1-sol / xhigh): every code
# review -- PR review, diff review, resume rounds and pending backfill -- runs
# gpt-6.2-astra at medium effort. Any other model or effort is not the reviewer.
# There is no fallback model or effort; a session that cannot prove both fails.
MODEL = "gpt-6.2-astra"
EFFORT = "medium"
_VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.]+))?")


def version_key(text: str) -> tuple:
    """`codex-cli 0.162.0-alpha.17.2` -> sortable key; a release sorts after its pre-releases."""
    match = _VERSION.search(text or "")
    if not match:
        return (-1,)
    major, minor, patch, pre = match.groups()
    pre_key = (1, ()) if not pre else (0, tuple(
        (0, int(part), "") if part.isdigit() else (1, 0, part) for part in pre.split(".")))
    return (int(major), int(minor), int(patch), pre_key)


def cli_version(path: str) -> str:
    try:
        proc = subprocess.run([path, "--version"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    lines = (proc.stdout or "").strip().splitlines()
    return lines[0] if proc.returncode == 0 and lines else ""


# Windows script shims (npm's codex.cmd / codex.ps1, any .bat). cmd.exe cuts a
# multi-line argument at its first newline, so a review prompt passed through one
# reaches the reviewer as a single line (PR #25, 2026-11-18: 75 of ~6000 chars).
SCRIPT_SHIMS = (".cmd", ".bat", ".ps1")


def is_script_shim(path: str) -> bool:
    return os.path.splitext(path)[1].lower() in SCRIPT_SHIMS


def native_for_shim(shim: str) -> list[str]:
    """The native codex.exe shipped in the npm package behind a PATH shim.

    Global prefix: the shim sits beside `node_modules` (`<npm>/codex.cmd`).
    Project-local install: the shim sits in `node_modules/.bin`, so the package's
    `node_modules` is the shim directory's parent. The platform package may be
    nested under `@openai/codex` or hoisted next to it (bin/codex.js resolves it
    with require.resolve), so both places are searched."""
    shim_dir = os.path.dirname(shim)
    if os.path.basename(shim_dir).lower() == ".bin":
        modules = os.path.dirname(shim_dir)
    else:
        modules = os.path.join(shim_dir, "node_modules")
    package = os.path.join(modules, "@openai", "codex")
    exe = ("vendor", "*", "bin", "codex.exe")
    patterns = (
        os.path.join(package, "node_modules", "@openai", "codex-win32-*", *exe),
        os.path.join(modules, "@openai", "codex-win32-*", *exe),
        os.path.join(package, *exe),
    )
    return sorted({hit for pattern in patterns for hit in glob.glob(pattern)})


def path_files(name: str) -> list[str]:
    """Every regular file called `name` on PATH, in PATH order. shutil.which filters
    by PATHEXT on Windows (hiding a codex.ps1 shim when .PS1 is not listed) and
    stops at the first match, which would let an older install shadow a newer one."""
    hits = []
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        candidate = os.path.join(directory.strip('"'), name) if directory.strip('"') else ""
        if candidate and os.path.isfile(candidate):
            hits.append(candidate)
    return hits


def codex_candidates() -> list[str]:
    """Native Codex CLIs on this machine: the desktop app's bundled builds, then
    PATH. A PATH script shim is replaced by the native exe of its npm package and
    is never itself a candidate (it would truncate the multi-line prompt)."""
    found = []
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        found.extend(sorted(glob.glob(os.path.join(local, "OpenAI", "Codex", "bin", "*", "codex.exe"))))
    for name in (("codex.cmd", "codex.ps1", "codex.exe", "codex") if os.name == "nt" else ("codex",)):
        # Every PATH directory is searched (see path_files), on POSIX for executables;
        # all installs then compete on version below.
        hits = path_files(name)
        if os.name != "nt":
            hits = [hit for hit in hits if os.access(hit, os.X_OK)]
        for hit in hits:
            if os.name == "nt" and (is_script_shim(hit) or not os.path.splitext(hit)[1]):
                # Extensionless `codex` on Windows is npm's sh shim for the same package.
                found.extend(native_for_shim(hit))
            else:
                found.append(hit)
    found = [path for path in found if not is_script_shim(path)]
    seen, unique = set(), []
    for path in found:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen and os.path.isfile(path):
            seen.add(key)
            unique.append(path)
    return unique


def find_codex() -> tuple[str, str]:
    """Highest-version runnable native CLI. Builds differ in which models a ChatGPT
    account may use (2026-11-18: npm 0.169.1 runs gpt-6.2-astra while the desktop
    app's bundled 0.163 alpha rejects it), so neither a PATH-only nor a
    bundled-only lookup is safe; take the newest runnable build. The
    chosen CLI is not trusted for the model either: verify() still checks the
    actual session record."""
    ranked = []
    for path in codex_candidates():
        version = cli_version(path)
        if version:
            ranked.append((version_key(version), path, version))
    if not ranked:
        raise ValueError("no runnable Codex CLI found")
    ranked.sort(key=lambda item: item[0])
    return ranked[-1][1], ranked[-1][2]


def objects(path: Path, *, allow_diagnostics: bool = False) -> list[dict]:
    result = []
    raw = path.read_bytes()
    # Windows PowerShell 5.1 Tee-Object emits BOM-tagged UTF-16; the CLI
    # and PowerShell 7 emit UTF-8. Decode only explicit BOMs, without guessing
    # or suppressing malformed input before the machine identity checks.
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    for line in raw.decode(encoding).splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            if allow_diagnostics:
                continue
            raise ValueError("invalid session JSON") from None
        if isinstance(value, dict):
            result.append(value)
    return result


def _text(payload: dict) -> str:
    return "".join(str(part.get("text", "")) for part in payload.get("content") or []
                   if isinstance(part, dict))


def _normal(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def verify(raw: Path, repo: Path, sessions: Path, started_at: float,
           expected_session: str | None = None, prompt: Path | None = None) -> dict:
    from datetime import datetime

    if not math.isfinite(started_at) or started_at < 0:
        raise ValueError("invalid invocation timestamp")
    events = objects(raw, allow_diagnostics=True)
    ids = [x.get("thread_id") for x in events if x.get("type") == "thread.started"]
    if len(ids) != 1 or not isinstance(ids[0], str) or not UUID.fullmatch(ids[0]):
        raise ValueError("missing or ambiguous machine-readable session identity")
    sid = ids[0].lower()
    if expected_session and sid != expected_session.lower():
        raise ValueError("returned session differs from recorded session")
    turns = [x for x in events if x.get("type") == "turn.completed"]
    if len(turns) != 1 or any(x.get("type") in {"error", "turn.failed"} for x in events):
        raise ValueError("review turn did not complete normally")
    usage = turns[0].get("usage", {})
    numbers = [usage.get(k) for k in ("input_tokens", "output_tokens")]
    if any(type(n) is not int or n < 0 for n in numbers):
        raise ValueError("missing actual token usage")

    matches = list(sessions.rglob(f"*{sid}.jsonl"))
    if len(matches) != 1:
        raise ValueError("missing or ambiguous actual session log")
    log = matches[0]
    records = objects(log)
    metadata = [x.get("payload", {}) for x in records if x.get("type") == "session_meta"]
    if len(metadata) != 1 or str(metadata[0].get("id", "")).lower() != sid:
        raise ValueError("actual session metadata identity mismatch")
    if not metadata[0].get("cwd") or Path(metadata[0]["cwd"]).resolve() != repo.resolve():
        raise ValueError("actual session belongs to another repository")
    contexts = [x for x in records if x.get("type") == "turn_context"]
    if not contexts:
        raise ValueError("actual session has no review context")
    latest = contexts[-1]
    stamp = datetime.fromisoformat(latest.get("timestamp", "").replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.timestamp() < started_at - 1:
        raise ValueError("actual context is stale for this invocation")
    context = latest.get("payload", {})
    if not context.get("cwd") or Path(context["cwd"]).resolve() != repo.resolve():
        raise ValueError("actual turn belongs to another repository")
    if context.get("model") != MODEL or context.get("effort") != EFFORT:
        raise ValueError(f"actual model/effort differs from {MODEL}/{EFFORT}")
    if context.get("sandbox_policy", {}).get("type") != "read-only":
        raise ValueError("actual review sandbox is not read-only")
    # A resumed session must have been this reviewer from its first turn: a session
    # begun under a retired profile (e.g. gpt-6.1-sol / xhigh) and resumed now would
    # let an incremental resume claim the whole range for the configured reviewer.
    if prompt is not None:
        # The reviewer must have received the whole prompt the wrapper sent: the
        # first user message after this invocation's turn context, verbatim.
        last = max(i for i, x in enumerate(records) if x.get("type") == "turn_context")
        after = records[last + 1:]
        sent = [_text(x.get("payload", {})) for x in after if x.get("type") == "response_item"
                and x.get("payload", {}).get("type") == "message"
                and x.get("payload", {}).get("role") == "user"]
        expected = _normal(prompt.read_text(encoding="utf-8"))
        if not expected or not sent or _normal(sent[0]) != expected:
            got = len(_normal(sent[0])) if sent else 0
            raise ValueError(f"reviewer received a truncated or different prompt "
                             f"({got} of {len(expected)} chars)")
    for earlier in contexts:
        payload = earlier.get("payload", {})
        if payload.get("model") != MODEL or payload.get("effort") != EFFORT \
                or payload.get("sandbox_policy", {}).get("type") != "read-only":
            raise ValueError(f"session has a turn outside {MODEL}/{EFFORT}/read-only; "
                             "run a fresh first round")
    return {"sessionId": sid, "tokensUsed": sum(numbers), "actualModel": MODEL,
            "actualEffort": EFFORT, "readOnly": True,
            "turnId": context.get("turn_id"), "contextTimestamp": latest["timestamp"],
            "promptVerified": prompt is not None,
            "sessionLogSHA256": hashlib.sha256(log.read_bytes()).hexdigest(),
            "eventsSHA256": hashlib.sha256(raw.read_bytes()).hexdigest()}


def main() -> int:
    if sys.argv[1:] == ["which"]:
        try:
            path, version = find_codex()
        except ValueError as exc:
            print(f"[codex-review] {exc}", file=sys.stderr)
            return 4
        print(path)
        print(version)
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw-log", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--started-at", type=float, required=True)
    parser.add_argument("--expected-session")
    parser.add_argument("--prompt-file", type=Path,
                        help="exact prompt sent; the session must show it received in full")
    parser.add_argument("--proof", type=Path, required=True)
    parser.add_argument("--session-directory", type=Path,
                        default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions")
    args = parser.parse_args()
    try:
        proof = verify(args.raw_log, args.repo, args.session_directory,
                       args.started_at, args.expected_session, args.prompt_file)
        args.proof.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"[codex-review] actual session verification failed: {exc}", file=sys.stderr)
        return 4
    print(f"{proof['sessionId']}\t{proof['tokensUsed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
