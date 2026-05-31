# -*- coding: utf-8 -*-
"""Shared launcher for desktop helper scripts."""
from __future__ import annotations

import os
import subprocess
import sys

from cmuh_common.paths import get_app_dir


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
    return subprocess.Popen(
        [executable or sys.executable, script_path],
        cwd=app_root,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        close_fds=True,
    )
