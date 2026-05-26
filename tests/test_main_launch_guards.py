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


def _function_source(source_path: Path, name: str) -> str:
    source = source_path.read_text(encoding="utf-8")
    node = _function_node(source_path, name)
    return ast.get_source_segment(source, node) or ""


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


def _call_lines(func: ast.FunctionDef, dotted_name: str) -> list[int]:
    out = []
    for node in ast.walk(func):
        if not isinstance(node, ast.Call):
            continue
        target = node.func
        if isinstance(target, ast.Name) and target.id == dotted_name:
            out.append(node.lineno)
        elif isinstance(target, ast.Attribute):
            base = target.value
            if isinstance(base, ast.Name) and f"{base.id}.{target.attr}" == dotted_name:
                out.append(node.lineno)
    if not out:
        raise AssertionError(f"call not found in {func.name}: {dotted_name}")
    return sorted(out)


def _returns_inside_not_ok_guard(func: ast.FunctionDef) -> bool:
    for node in ast.walk(func):
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
            if isinstance(test.operand, ast.Name) and test.operand.id == "ok":
                return any(isinstance(child, ast.Return) for child in ast.walk(node))
    return False


def _has_return(func: ast.FunctionDef) -> bool:
    return any(isinstance(node, ast.Return) for node in ast.walk(func))


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


def test_f1_does_not_update_uvb_when_code_input_fails():
    source_path = ROOT / "src" / "main.py"
    func = _function_node(source_path, "script_F1_adaptive")

    assert (
        _first_call_line(func, "_script_code_input_adaptive")
        < _first_call_line(func, "_f1_update_uvb_dose_if_present")
    )
    assert _returns_inside_not_ok_guard(func)
    assert (
        _first_call_line(func, "_show_light_code_incomplete_warning")
        < _first_call_line(func, "_f1_update_uvb_dose_if_present")
    )


def test_f2_f3_warn_when_code_input_fails_after_uvb_update():
    source_path = ROOT / "src" / "main.py"

    for name in ("script_F2_adaptive", "script_F3_adaptive"):
        func = _function_node(source_path, name)
        assert (
            _first_call_line(func, "_f23_update_uvb_dose")
            < _first_call_line(func, "_script_code_input_adaptive")
        )
        assert (
            _first_call_line(func, "_script_code_input_adaptive")
            < _first_call_line(func, "_show_light_code_incomplete_warning")
        )
        assert len(_call_lines(func, "_show_light_code_incomplete_warning")) == 1


def test_hotkey_scripts_return_completion_status():
    source_path = ROOT / "src" / "main.py"

    for name in (
        "script_F1_adaptive",
        "script_F2_adaptive",
        "script_F3_adaptive",
        "script_F4_adaptive",
        "script_F5_adaptive",
        "script_F9_adaptive",
        "script_F10_adaptive",
        "script_F11_adaptive",
    ):
        assert _has_return(_function_node(source_path, name)), name


def test_run_subsystem_reports_incomplete_return_status():
    source_path = ROOT / "src" / "main.py"
    src = _function_source(source_path, "run_subsystem_in_thread")

    assert "result = func()" in src
    assert "if result is False:" in src
    assert "操作未完成" in src
    assert src.index("if result is False:") < src.index("操作完成")


def test_f9_f10_consent_menu_post_is_checked():
    source_path = ROOT / "src" / "main.py"
    src = _function_source(source_path, "script_F9_F10_consent_form_adaptive")

    assert "_send_yiling_menu_command(main_hwnd, MENU_ID_同意書)" in src
    assert src.count("_send_yiling_menu_command(main_hwnd, MENU_ID_同意書)") >= 2
    assert "PostMessageW(main_hwnd, WM_COMMAND" not in src


def test_code_input_waits_for_focus_after_menu_command():
    source_path = ROOT / "src" / "main.py"
    func = _function_node(source_path, "_script_code_input_adaptive")

    assert (
        _first_call_line(func, "_send_yiling_menu_command")
        < _first_call_line(func, "_wait_for_code_input_focus")
        < _first_call_line(func, "_send_chars_to_window")
    )

    wait_func = _function_node(source_path, "_wait_for_code_input_focus")
    constants = {
        node.value
        for node in ast.walk(wait_func)
        if isinstance(node, ast.Constant)
    }
    assert 0.6 in constants

    wait_src = _function_source(source_path, "_wait_for_code_input_focus")
    assert "is_input_like and (focus != previous_focus or not previous_focus)" in wait_src
    assert "return 0" in wait_src

    code_input_src = _function_source(source_path, "_script_code_input_adaptive")
    assert "if not _send_yiling_menu_command" in code_input_src
    menu_fail_idx = code_input_src.index("if not _send_yiling_menu_command")
    menu_fail_block = code_input_src[menu_fail_idx:code_input_src.index(
        "# 等焦點移到醫令代碼欄")]
    assert "_mark_hotkey_action_time()" in menu_fail_block
    assert "hotkey_modules.pyautogui.typewrite(code" not in code_input_src
    assert "workflow_ok = False" in code_input_src
    assert "if code and not workflow_ok:" in code_input_src
    assert (
        code_input_src.index("if code and not workflow_ok:")
        < code_input_src.index("_find_療程_edit_hwnd")
    )
    assert code_input_src.count("_mark_hotkey_action_time()") >= 3

    send_src = _function_source(source_path, "_send_yiling_menu_command")
    assert "-> bool" in send_src
    assert "PostMessageW" in send_src
    assert "return False" in send_src
