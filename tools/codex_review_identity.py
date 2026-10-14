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
import shutil
import subprocess
import sys

UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
# User decision 2026-10-14: every diff review runs gpt-6.1-sol at xhigh effort.
# There is no fallback model or effort; a session that cannot prove both fails.
MODEL = "gpt-6.1-sol"
EFFORT = "xhigh"
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


def codex_candidates() -> list[str]:
    """Codex CLIs on this machine: the desktop app's bundled builds, then PATH."""
    found = []
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        found.extend(sorted(glob.glob(os.path.join(local, "OpenAI", "Codex", "bin", "*", "codex.exe"))))
    for name in (("codex.cmd", "codex.exe", "codex") if os.name == "nt" else ("codex",)):
        hit = shutil.which(name)
        if hit:
            found.append(hit)
    seen, unique = set(), []
    for path in found:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen and os.path.isfile(path):
            seen.add(key)
            unique.append(path)
    return unique


def find_codex() -> tuple[str, str]:
    """Highest-version runnable CLI. The npm build (0.155.1) rejects gpt-6.1-sol
    for ChatGPT accounts while the desktop app's bundled alpha accepts it, so a
    PATH-only lookup would pick a CLI that cannot run the required model. The
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


def verify(raw: Path, repo: Path, sessions: Path, started_at: float,
           expected_session: str | None = None) -> dict:
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
    return {"sessionId": sid, "tokensUsed": sum(numbers), "actualModel": MODEL,
            "actualEffort": EFFORT, "readOnly": True,
            "turnId": context.get("turn_id"), "contextTimestamp": latest["timestamp"],
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
    parser.add_argument("--proof", type=Path, required=True)
    parser.add_argument("--session-directory", type=Path,
                        default=Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")) / "sessions")
    args = parser.parse_args()
    try:
        proof = verify(args.raw_log, args.repo, args.session_directory,
                       args.started_at, args.expected_session)
        args.proof.write_text(json.dumps(proof, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, TypeError, KeyError) as exc:
        print(f"[codex-review] actual session verification failed: {exc}", file=sys.stderr)
        return 4
    print(f"{proof['sessionId']}\t{proof['tokensUsed']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
