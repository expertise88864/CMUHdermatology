# -*- coding: utf-8 -*-
"""Shared launcher for desktop helper scripts."""
from __future__ import annotations

import os
import subprocess
import sys

from cmuh_common.paths import get_app_dir


def launch_python_script(
    script_path: str,
    *,
    args: tuple[str, ...] | list[str] = (),
    executable: str | None = None,
    cwd: str | None = None,
    detached: bool = False,
) -> subprocess.Popen:
    """Start an existing Python script with stable Windows process flags."""
    resolved_script = os.path.realpath(os.path.abspath(script_path))
    if not os.path.isfile(resolved_script):
        raise FileNotFoundError(resolved_script)

    working_dir = os.path.realpath(os.path.abspath(cwd or os.path.dirname(resolved_script)))
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if detached and os.name == "nt":
        creationflags |= getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)

    return subprocess.Popen(
        [executable or sys.executable, resolved_script, *args],
        cwd=working_dir,
        creationflags=creationflags,
        close_fds=True,
    )


def resolve_app_script(script_name: str, *, app_dir: str | None = None) -> str:
    """Resolve a helper script while keeping the target inside the app directory."""
    if not isinstance(script_name, str) or not script_name.strip():
        raise ValueError("script_name must not be empty")
    if os.path.isabs(script_name):
        raise ValueError(f"script_name must be relative: {script_name}")

    app_root = os.path.realpath(os.path.abspath(app_dir or get_app_dir()))
    script_path = os.path.realpath(os.path.abspath(os.path.join(app_root, script_name)))
    try:
        common_path = os.path.commonpath([app_root, script_path])
    except ValueError as e:
        raise ValueError(f"invalid script path: {script_name}") from e
    if os.path.normcase(common_path) != os.path.normcase(app_root):
        raise ValueError(f"script path escapes app directory: {script_name}")
    if os.path.normcase(script_path) == os.path.normcase(app_root):
        raise ValueError(f"script path points to app directory: {script_name}")
    if not os.path.isfile(script_path):
        raise FileNotFoundError(script_path)
    return script_path


def launch_app_script(
    script_name: str,
    *,
    executable: str | None = None,
    app_dir: str | None = None,
) -> subprocess.Popen:
    """Start a root launcher using a stable absolute path and working directory."""
    app_root = os.path.realpath(os.path.abspath(app_dir or get_app_dir()))
    script_path = resolve_app_script(script_name, app_dir=app_root)
    return launch_python_script(
        script_path,
        executable=executable,
        cwd=app_root,
    )
