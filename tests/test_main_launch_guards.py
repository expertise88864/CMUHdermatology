# -*- coding: utf-8 -*-
"""Regression checks for guarded launches in desktop app entry points."""
import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _function_node(source_path: Path, name: str) -> ast.FunctionDef:
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"function not found in {source_path.name}: {name}")


def _first_call_line(func: ast.FunctionDef, dotted_name: str) -> int:
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name) and target.id == dotted_name:
            return node.lineno
        if isinstance(target, ast.Attribute):
            base = target.value
            if isinstance(base, ast.Name) and f"{base.id}.{target.attr}" == dotted_name:
                return node.lineno
    raise AssertionError(f"call not found in {func.name}: {dotted_name}")


def _constant_strings(func: ast.FunctionDef) -> set[str]:
    return {
        node.value
        for node in ast.walk(func)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _assert_autoclock_launch_guard(source_path: Path) -> None:
    func = _function_node(source_path, "_launch_autoclock_program")

    assert (
        _first_call_line(func, "is_instance_running")
        < _first_call_line(func, "subprocess.Popen")
    )
    assert "Local\\CMUH_Skin_AutoClock_SingleInstance_v1" in _constant_strings(func)


def _assert_consult_launch_guard(source_path: Path) -> None:
    func = _function_node(source_path, "_launch_consult_query_program")

    assert (
        _first_call_line(func, "is_instance_running")
        < _first_call_line(func, "subprocess.Popen")
    )
    assert "Local\\CMUH_Skin_ConsultQuery_SingleInstance_v1" in _constant_strings(func)


def test_main_background_launches_check_mutex_before_spawn():
    source_path = ROOT / "src" / "main.py"

    _assert_autoclock_launch_guard(source_path)
    _assert_consult_launch_guard(source_path)


def test_scheduler_background_launches_check_mutex_before_spawn():
    source_path = ROOT / "src" / "scheduler.py"

    _assert_autoclock_launch_guard(source_path)
    _assert_consult_launch_guard(source_path)
