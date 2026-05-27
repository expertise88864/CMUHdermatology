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


def test_scheduler_run_subsystem_uses_guardian_policies():
    source_path = ROOT / "src" / "scheduler.py"
    src = _function_source(source_path, "run_subsystem_in_thread")

    assert "should_show_busy_notice" in src
    assert "result = func()" in src
    assert "if result is False:" in src
    assert "操作未完成" in src
    assert "should_emit_idle_status" in src
    assert "subsystem_token" in src


def test_scheduler_interrupt_automation_is_deduplicated():
    source_path = ROOT / "src" / "scheduler.py"
    src = _function_source(source_path, "interrupt_automation")

    assert "should_emit_interrupt" in src
    assert "stop_already_requested=stop_event_automation.is_set()" in src
    assert "Received F12 but no automation is running; ignored." in src


def test_scheduler_shutdown_is_idempotent_and_stops_automation():
    source_path = ROOT / "src" / "scheduler.py"
    src = _function_source(source_path, "shutdown_app")
    full_src = source_path.read_text(encoding="utf-8")

    assert "self._exit_cleanup_done = False" in full_src
    assert "if getattr(self, '_exit_cleanup_done', False):" in src
    assert "self._exit_cleanup_done = True" in src
    assert "stop_event_automation.set()" in src
    assert src.index("stop_event_automation.set()") < src.index("self.bg_executor.shutdown")


def test_main_and_scheduler_log_queues_are_bounded():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        src = (ROOT / rel_path).read_text(encoding="utf-8")

        assert "self.log_queue = Queue(maxsize=5000)" in src
        assert "self.log_queue = Queue()" not in src


def test_main_and_scheduler_ui_queues_are_bounded():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        src = (ROOT / rel_path).read_text(encoding="utf-8")

        assert "self.ui_queue = Queue(maxsize=10000)" in src
        assert "self.ui_queue = Queue()" not in src


def test_main_and_scheduler_background_executors_are_bounded():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        src = (ROOT / rel_path).read_text(encoding="utf-8")

        assert "BoundedThreadPoolExecutor(" in src
        assert "self.bg_executor = ThreadPoolExecutor(" not in src
        assert "max_pending=60" in src


def test_refresh_batches_use_local_executor_instead_of_bg_queue():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        src = _function_source(ROOT / rel_path, "_trigger_refresh")

        assert 'thread_name_prefix="RefreshBatch"' in src
        assert "refresh_pool.submit(check_appointment_count" in src
        assert "self.bg_executor.submit(check_appointment_count" not in src


def test_duty_info_uses_local_executor_instead_of_bg_queue():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        src = _function_source(ROOT / rel_path, "_fetch_all_duty_info")

        assert 'thread_name_prefix="DutyInfo"' in src
        assert "duty_pool.submit(self._run_single_duty_query" in src
        assert "self.bg_executor.submit(self._run_single_duty_query" not in src


def test_refresh_submit_rejection_restores_ui_state():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        src = _function_source(ROOT / rel_path, "_trigger_refresh")

        assert "RejectedExecutionError" in src
        assert "refresh_future = self.bg_executor.submit(run_parallel_checks)" in src
        assert "refresh_future.add_done_callback(_handle_refresh_submit_rejected)" in src
        assert "self._active_refresh_signature = None" in src
        assert 'self.status_text.set("狀態: 背景佇列忙碌，刷新稍後重試")' in src
        assert 'self.refresh_button.config(state="normal")' in src


def test_clinic_worker_submit_rejection_clears_running_flag():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        src = _function_source(ROOT / rel_path, "_update_clinic_lights_loop")

        assert "RejectedExecutionError" in src
        assert "clinic_future = self.bg_executor.submit(guarded_run_update, rooms_to_check)" in src
        assert "clinic_future.add_done_callback(_handle_clinic_submit_rejected)" in src
        assert "self._clinic_lights_worker_running = False" in src


def test_scheduled_background_submits_detect_rejected_futures():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        src = _function_source(ROOT / rel_path, "start_background_tasks")

        assert "def _future_was_rejected(future):" in src
        assert "isinstance(future.exception(), RejectedExecutionError)" in src
        assert "result = fn()" in src
        assert "_future_was_rejected(result)" in src
        assert "background queue full" in src
        assert "future = self.bg_executor.submit(self._trigger_refresh, False, [doc])" in src
        assert "_future_was_rejected(future)" in src
        assert src.index("_future_was_rejected(future)") < src.index(
            "self._priority_refresh_last_check_time[doc_name] = now_ts"
        )


def test_hotkey_guardian_uses_safe_rehook_policy():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        src = _function_source(ROOT / rel_path, "run_hotkey_guardian")

        assert "should_rehook_hotkeys(" in src
        assert "subsystem_running=getattr(self, '_subsystem_running', False)" in src
        assert "modules_ready=getattr(self, '_heavy_modules_ready', False)" in src
        assert "Hotkey guardian skipped re-hook while automation is running." in src


def test_background_schedule_loop_uses_low_frequency_wait():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        src = _function_source(ROOT / rel_path, "start_background_tasks")

        assert "now.second < 10" in src
        assert "stop_event_main.wait(5.0)" in src
        assert "stop_event_main.wait(1.0)" not in src


def test_main_and_scheduler_background_tasks_start_once_and_clear_schedule():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        full_src = (ROOT / rel_path).read_text(encoding="utf-8")
        func_src = _function_source(ROOT / rel_path, "start_background_tasks")

        assert "self._background_tasks_started = False" in full_src
        assert "if self._background_tasks_started:" in func_src
        assert "self._background_tasks_started = True" in func_src
        assert "schedule.clear()" in func_src
        assert func_src.index("schedule.clear()") < func_src.index("schedule.every")


def test_main_and_scheduler_ui_queue_polling_is_bounded():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        src = _function_source(ROOT / rel_path, "process_ui_queue")

        assert "for _ in range(250):" in src
        assert "while True:" not in src
        assert "next_delay = 80 if had_work else 320" in src


def test_inner_watchdog_heartbeat_uses_monotonic_clock():
    src = (ROOT / "src" / "main.py").read_text(encoding="utf-8")

    assert "now_monotonic = time.monotonic()" in src
    assert "time.time() - last_heartbeat" not in src


def test_f9_f10_consent_menu_post_is_checked():
    source_path = ROOT / "src" / "main.py"
    src = _function_source(source_path, "script_F9_F10_consent_form_adaptive")

    assert "_send_yiling_menu_command(main_hwnd, MENU_ID_同意書)" in src
    assert src.count("_send_yiling_menu_command(main_hwnd, MENU_ID_同意書)") >= 2
    assert "PostMessageW(main_hwnd, WM_COMMAND" not in src


def test_hotkey_waits_are_interruptible():
    source_path = ROOT / "src" / "main.py"

    sleep_src = _function_source(source_path, "_sleep_interruptible")
    assert "check_stop()" in sleep_src
    assert "time.sleep(min(slice_s, left))" in sleep_src

    wait_focus_src = _function_source(source_path, "_wait_for_code_input_focus")
    assert "_sleep_interruptible(poll)" in wait_focus_src
    assert "time.sleep(poll)" not in wait_focus_src

    wait_window_src = _function_source(source_path, "_wait_for_window")
    assert "_sleep_interruptible(" in wait_window_src
    assert "time.sleep(poll_sec)" not in wait_window_src

    f11_watcher_src = _function_source(source_path, "_f11_popup_watcher")
    assert "_sleep_interruptible(0.3)" in f11_watcher_src
    assert "_sleep_interruptible(0.4)" in f11_watcher_src

    f11_main_src = _function_source(source_path, "_f11_快速完成_main")
    assert "_sleep_interruptible(0.5)" in f11_main_src
    assert "time.sleep(0.5)" not in f11_main_src


def test_f9_f10_fixed_waits_are_interruptible():
    source_path = ROOT / "src" / "main.py"
    src = _function_source(source_path, "script_F9_F10_consent_form_adaptive")

    for expected in (
        "_sleep_interruptible(0.3)",
        "_sleep_interruptible(0.5)",
        "_sleep_interruptible(0.2)",
        "_sleep_interruptible(0.1)",
    ):
        assert expected in src
    assert "time.sleep(0.5)" not in src
    assert "time.sleep(0.3)" not in src


def test_post_click_reports_postmessage_failure():
    source_path = ROOT / "src" / "main.py"
    src = _function_source(source_path, "_post_click_to_control")

    assert "down_ok = bool(" in src
    assert "up_ok = bool(" in src
    assert "if not (down_ok and up_ok):" in src
    assert "return False" in src


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
