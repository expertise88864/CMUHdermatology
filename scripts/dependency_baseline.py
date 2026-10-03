"""Record runtime dependency closure for developer-only environment rebuilding.

Never reads pip configuration, direct_url metadata, accounts or environment values.
The generated constraints supplement the existing manifests; installers are unchanged.
"""
from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import platform
import re
import subprocess
import sys
import tkinter

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version


ROOT = Path(__file__).resolve().parents[1]
MANIFESTS = ("requirements.txt", "requirements-lazy.txt")


def _requirement(text: str) -> Requirement:
    try:
        requirement = Requirement(text)
    except InvalidRequirement:
        raise ValueError("unsupported dependency declaration") from None
    if requirement.url:
        raise ValueError("direct URL dependencies are outside the version-only baseline")
    return requirement


def dependency_closure(requirements, *, distribution=metadata.distribution, environment=None):
    """Resolve active installed metadata, including propagated optional extras."""
    env = default_environment() if environment is None else dict(environment)
    pending = [_requirement(text) for text in requirements]
    packages = {}
    expanded = {}
    while pending:
        requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        try:
            installed = distribution(name)
        except metadata.PackageNotFoundError:
            raise ValueError(f"required distribution missing: {name}") from None
        try:
            version = str(Version(installed.version))
        except InvalidVersion:
            raise ValueError(f"invalid installed version: {name}") from None
        if requirement.specifier and not requirement.specifier.contains(version, prereleases=True):
            raise ValueError(f"installed version violates a declaration: {name}")
        packages[name] = version
        extras = set(requirement.extras) | expanded.get(name, set())
        if name in expanded and extras == expanded[name]:
            continue
        expanded[name] = extras
        for declaration in installed.requires or ():
            dependency = _requirement(declaration)
            if dependency.marker is None or any(
                dependency.marker.evaluate({**env, "extra": extra}) for extra in {"", *extras}
            ):
                pending.append(dependency)
    return dict(sorted(packages.items()))


def read_manifests(root: Path):
    declarations = []
    hashes = {}
    env = {**default_environment(), "extra": ""}
    for name in MANIFESTS:
        content = (root / name).read_bytes()
        hashes[name] = hashlib.sha256(content).hexdigest()
        for line in content.decode("ascii").splitlines():
            declaration = line.split("#", 1)[0].strip()
            if declaration:
                requirement = _requirement(declaration)
                if requirement.marker is None or requirement.marker.evaluate(env):
                    declarations.append(declaration)
    return declarations, hashes


def constraints_text(packages):
    return "# Developer rebuild baseline; does not replace runtime manifests.\n" + "".join(
        f"{name}=={version}\n" for name, version in sorted(packages.items()))


def snapshot(root: Path):
    declarations, hashes = read_manifests(root)
    packages = dependency_closure(declarations)
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True,
                            text=True, timeout=10, check=True).stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}", commit) is None:
        raise ValueError("source revision is not a full Git SHA")
    dirty = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True,
                           timeout=10, check=True).stdout.strip()
    return {
        "schema": 1, "source_commit": commit, "source_tree_dirty": bool(dirty),
        "manifest_sha256": hashes,
        "runtime": {"python": platform.python_version(), "implementation": sys.implementation.name,
                    "os": {"system": platform.system(), "release": platform.release(),
                           "version": platform.version(), "machine": platform.machine()},
                    "tcl_patchlevel": str(tkinter.Tcl().call("info", "patchlevel")),
                    "tk_abi": str(tkinter.TkVersion)},
        "packages": packages,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--constraints", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        record = snapshot(ROOT)
        for destination in (args.output, args.constraints):
            destination.parent.mkdir(parents=True, exist_ok=True)
        args.constraints.write_text(constraints_text(record["packages"]), encoding="ascii")
        args.output.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    except (ValueError, OSError, subprocess.SubprocessError, tkinter.TclError):
        # File paths, pip origins and malformed metadata may contain private values.
        print("Dependency baseline failed; no verified environment was recorded.", file=sys.stderr)
        return 1
    print(f"Recorded {len(record['packages'])} runtime distributions; clean_source={not record['source_tree_dirty']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
