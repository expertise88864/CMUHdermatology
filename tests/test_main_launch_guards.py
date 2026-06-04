# -*- coding: utf-8 -*-
"""Regression checks for guarded launches in desktop app entry points."""
import ast
import re
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
        < _first_call_line(func, "launch_app_script")
    )
    assert "Local\\CMUH_Skin_AutoClock_SingleInstance_v1" in _constant_strings(func)


def _assert_consult_launch_guard(source_path: Path) -> None:
    func = _function_node(source_path, "_launch_consult_query_program")

    assert (
        _first_call_line(func, "is_instance_running")
        < _first_call_line(func, "launch_app_script")
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


def test_main_and_scheduler_helper_launches_use_shared_launcher():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        source_path = ROOT / rel_path
        for func_name in (
            "_launch_scheduler_program",
            "_launch_autoclock_program",
            "_launch_coordinate_detector_program",
            "_launch_consult_query_program",
        ):
            src = _function_source(source_path, func_name)
            assert "launch_app_script(" in src
            assert "subprocess.Popen(" not in src


def test_scheduler_window_has_distinct_title():
    source = (ROOT / "src" / "scheduler.py").read_text(encoding="utf-8")

    assert 'self.root.title("中國醫皮膚科排班程式")' in source


def test_main_places_window_on_preferred_monitor_before_and_after_deiconify():
    source = (ROOT / "src" / "main.py").read_text(encoding="utf-8")

    assert "place_tk_window_on_preferred_monitor(self.root)" in source
    # 手動啟動路徑：deiconify 後立即定位到偏好螢幕（縮排無關，可包在 else 分支內）
    assert re.search(
        r"main_root\.deiconify\(\)\s*\n\s*place_tk_window_on_preferred_monitor\(main_root\)",
        source,
    )


def test_main_background_restart_starts_minimized_silently():
    """背景重啟（--background）必須靜默：不開 splash、視窗最小化進工作列、
    不搶焦點；第一次還原時才定位/最大化。手動啟動則維持正常顯示。"""
    source = (ROOT / "src" / "main.py").read_text(encoding="utf-8")

    assert '_start_background = ("--background" in sys.argv)' in source
    # splash 只在非背景啟動時顯示
    assert "if not _start_background:" in source
    # 背景啟動以最小化進工作列、第一次 <Map> 還原時才最大化
    assert "main_root.iconify()" in source
    assert 'main_root.bind(\n                "<Map>"' in source or 'main_root.bind("<Map>"' in source
    # app 端重啟匯流點帶 --background
    assert 'restart_self(["--background"])' in source


def test_scheduler_background_restart_starts_minimized_silently():
    source = (ROOT / "src" / "scheduler.py").read_text(encoding="utf-8")

    assert '_start_background = ("--background" in sys.argv)' in source
    assert "main_root.iconify()" in source
    # __init__ 的 zoom 對 withdrawn 視窗要跳過（避免背景啟動被 re-map 閃一下）
    assert "if self.root.state() != 'withdrawn':" in source
    # 自動重啟帶 --background
    assert 'restart_self(["--background"])' in source


def test_startup_splash_uses_same_preferred_monitor_as_main_window():
    source = (ROOT / "src" / "cmuh_common" / "splash.py").read_text(encoding="utf-8")

    assert "monitor = get_preferred_monitor_rect()" in source
    assert "move_tk_window_to_monitor(top, MonitorRect(x, y, w, h))" in source


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


def test_scheduler_shutdown_clears_http_pools_without_blocking_close():
    src = _function_source(ROOT / "src/scheduler.py", "shutdown_app")

    assert "adapter.poolmanager.clear()" in src
    assert "session.close()" not in src


def test_main_and_scheduler_schedule_cache_cleanup_for_standalone_launches():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        src = _function_source(ROOT / rel_path, "start_background_tasks")

        assert "schedule_cleanup_in_background(self.bg_executor, delay_seconds=30)" in src


def test_main_uses_weak_http_session_registry_and_bounded_memory_caches():
    src = (ROOT / "src" / "main.py").read_text(encoding="utf-8")

    assert "_all_reg_sessions: WeakSet = WeakSet()" in src
    assert "trim_oldest_entries(_ttl_cache_store, _TTL_CACHE_MAX_ENTRIES)" in src
    assert "trim_oldest_entries(_parse_cache_store, _PARSE_CACHE_MAX_ENTRIES)" in src
    assert "trim_oldest_entries(_source_backoff_state, _SOURCE_STATE_MAX_ENTRIES)" in src


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


def test_duty_info_is_single_flight_and_only_caches_complete_success():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        full_src = (ROOT / rel_path).read_text(encoding="utf-8")
        fetch_src = _function_source(ROOT / rel_path, "_fetch_all_duty_info")
        single_src = _function_source(ROOT / rel_path, "_run_single_duty_query")

        assert "self._duty_fetch_worker_running = False" in full_src
        assert "self._duty_fetch_lock = threading.Lock()" in full_src
        assert "with self._duty_fetch_lock:" in fetch_src
        assert "if self._duty_fetch_worker_running:" in fetch_src
        assert "self._duty_fetch_worker_running = True" in fetch_src
        assert "all_succeeded = True" in fetch_src
        assert "if all_succeeded:" in fetch_src
        assert "self._duty_last_fetch_date = today_str" in fetch_src
        assert "finally:" in fetch_src
        assert "self._duty_fetch_worker_running = False" in fetch_src
        assert "return bool(fn(self.ui_queue, s, third_arg))" in single_src


def test_duty_queries_report_success_for_daily_cache_decision():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        for name in ("fetch_duty_doctor", "fetch_saturday_duty_doctor", "fetch_duty_vs"):
            src = _function_source(ROOT / rel_path, name)

            assert 'return doctor_name not in {"查詢失敗", "網路錯誤", "查詢錯誤"}' in src


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


def test_clinic_stat_submit_is_deduplicated_and_retries_rejected_closing_save():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        full_src = (ROOT / rel_path).read_text(encoding="utf-8")
        loop_src = _function_source(ROOT / rel_path, "_update_clinic_lights_loop")
        submit_src = _function_source(ROOT / rel_path, "_submit_clinic_session_stat")

        assert "self._clinic_stat_pending_keys = set()" in full_src
        assert "self._clinic_stat_pending_lock = threading.Lock()" in full_src
        assert "if pending_key in self._clinic_stat_pending_keys:" in submit_src
        assert "self._clinic_stat_pending_keys.add(pending_key)" in submit_src
        assert "self._clinic_stat_pending_keys.discard(pending_key)" in submit_src
        assert "future = self.bg_executor.submit(" in submit_src
        assert "except RuntimeError:" in submit_src
        assert "RejectedExecutionError" in submit_src
        assert "future.add_done_callback(_release_pending_key)" in submit_src
        assert "stat_submitted = self._submit_clinic_session_stat(" in loop_src
        assert "if is_ended and stat_submitted:" in loop_src


def test_ui_thread_dispatch_skips_callbacks_during_shutdown():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        src = _function_source(ROOT / rel_path, "_run_on_ui_thread")

        assert 'getattr(self, "_shutting_down", False)' in src
        assert "stop_event_main.is_set()" in src
        assert "except (tk.TclError, RuntimeError):" in src
        assert src.count("return False") >= 2
        assert src.count("return True") >= 2


def test_refresh_entrypoint_reroutes_to_tk_thread():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        src = _function_source(ROOT / rel_path, "_trigger_refresh")

        assert "threading.current_thread() is not threading.main_thread()" in src
        assert "queued_doctors = list(specific_doctors) if specific_doctors is not None else None" in src
        assert "self.root.after(0, lambda: self._trigger_refresh(" in src


def test_clinic_polling_snapshots_tk_modes_before_background_work():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        src = _function_source(ROOT / rel_path, "_update_clinic_lights_loop")
        mode_read = "self.clinic_display_mode_vars[i].get()"

        assert src.count(mode_read) == 1
        assert src.index(mode_read) < src.index("def run_update(rooms):")
        assert "for i, (room_code, configured_mode) in enumerate(rooms):" in src
        assert "mode = configured_mode" in src


def test_clinic_room_defaults_use_shared_101_102_constant():
    main_src = (ROOT / "src/main.py").read_text(encoding="utf-8")
    scheduler_src = (ROOT / "src/scheduler.py").read_text(encoding="utf-8")

    assert "DEFAULT_CLINIC_ROOMS" in main_src
    assert "DEFAULT_CLINIC_ROOMS" in scheduler_src
    assert '["181", "182"]' not in main_src
    assert '["181", "182"]' not in scheduler_src


def test_clinic_room_settings_migrate_legacy_defaults_on_load():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        src = _function_source(ROOT / rel_path, "load_clinic_settings")

        assert "normalize_clinic_rooms(settings.get(\"rooms\"))" in src
        assert "_atomic_write_json(file_path, settings)" in src
        assert "門診動態診間設定已遷移為" in src


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
    """守護程式偵測到全域 hook 失效時，只能在安全狀態下自動重啟：非自動化執行中、
    模組就緒、且使用者閒置（system idle 達門檻），避免打斷醫令流程。健康判定邏輯
    已從 run_hotkey_guardian 移到 _hotkey_health_tick。"""
    for rel_path in ("src/main.py", "src/scheduler.py"):
        guardian = _function_source(ROOT / rel_path, "run_hotkey_guardian")
        assert "GUARDIAN_INTERVAL_SEC" in guardian
        assert "self._hotkey_health_tick()" in guardian
        # 守護程式不再「無腦每 N 秒重掛」（對 LowLevelHooks timeout 無效）
        assert "should_rehook_hotkeys(" not in guardian

        tick = _function_source(ROOT / rel_path, "_hotkey_health_tick")
        assert "should_auto_restart_for_dead_hook(" in tick
        assert "subsystem_running=getattr(self, '_subsystem_running', False)" in tick
        assert "modules_ready=getattr(self, '_heavy_modules_ready', False)" in tick
        assert "system_idle_sec=idle" in tick
        # 確認失效需連續多次探針未回應，且重啟前先主動探針
        assert "is_hook_probe_failure_confirmed(" in tick
        assert "self._probe_hotkey_hook_alive()" in tick


def test_hotkey_module_loader_recovers_from_rejected_submit():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        loader_src = _function_source(ROOT / rel_path, "_start_hotkey_module_loading")
        deferred_src = _function_source(ROOT / rel_path, "deferred_initialization")

        assert "hotkey_future = self.bg_executor.submit(self._prepare_hotkeys_background)" in loader_src
        assert "hotkey_future.add_done_callback(_handle_hotkey_loader_rejected)" in loader_src
        assert "RejectedExecutionError" in loader_src
        assert "self._heavy_modules_loading = False" in loader_src
        assert "self.root.after(5000, self._start_hotkey_module_loading)" in loader_src
        assert "self._start_hotkey_module_loading()" in deferred_src


def test_url_shortener_recovers_from_rejected_submit():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        src = _function_source(ROOT / rel_path, "_start_shorten_url")

        assert "shorten_future = self.bg_executor.submit(self._run_url_shortener, long_url)" in src
        assert "shorten_future.add_done_callback(_handle_shorten_submit_rejected)" in src
        assert "RejectedExecutionError" in src
        assert 'self.shorten_btn.config(state="normal")' in src
        assert "self._run_on_ui_thread(_reset_shorten_ui)" in src


def test_settings_promo_recovers_from_rejected_submit():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        src = _function_source(ROOT / rel_path, "ensure_settings_promo_loaded")

        assert "promo_future = self.bg_executor.submit(self._load_settings_promo_image)" in src
        assert "promo_future.add_done_callback(_handle_promo_submit_rejected)" in src
        assert "RejectedExecutionError" in src
        assert "self._settings_promo_loading = False" in src
        assert "self.root.after(5000, self.ensure_settings_promo_loaded)" in src


def test_clock_status_query_is_single_flight_and_recovers_from_rejection():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        full_src = (ROOT / rel_path).read_text(encoding="utf-8")
        src = _function_source(ROOT / rel_path, "update_clock_status_from_web")

        assert "self._clock_status_worker_running = False" in full_src
        assert "if self._clock_status_worker_running:" in src
        assert "self._clock_status_worker_running = True" in src
        assert "clock_future = self.bg_executor.submit(run_check)" in src
        assert "clock_future.add_done_callback(_handle_clock_submit_rejected)" in src
        assert "RejectedExecutionError" in src
        assert src.count("self._clock_status_worker_running = False") >= 2
        assert "UiClockStatusMessage(status_data={'error': '背景忙碌'})" in src


def test_startup_background_submits_retry_when_queue_is_full():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        src = _function_source(ROOT / rel_path, "start_background_tasks")

        assert "def _submit_startup_background(task_name, fn, *args, attempt=1):" in src
        assert "isinstance(fut.exception(), RejectedExecutionError)" in src
        assert "if attempt >= 3:" in src
        assert "attempt=attempt + 1" in src
        assert "future.add_done_callback(_retry_if_rejected)" in src
        assert '"master-schedule"' in src
        assert '"duty-info"' in src


def test_update_check_is_single_flight_and_manual_submit_reports_rejection():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        full_src = (ROOT / rel_path).read_text(encoding="utf-8")
        submit_src = _function_source(ROOT / rel_path, "_submit_update_check")
        check_src = _function_source(ROOT / rel_path, "check_and_update")
        start_src = _function_source(ROOT / rel_path, "start_background_tasks")

        assert "self._update_check_running = False" in full_src
        assert "self._update_check_lock = threading.Lock()" in full_src
        assert "future = self.bg_executor.submit(self.check_and_update, is_manual)" in submit_src
        assert "future.add_done_callback(_handle_update_submit_rejected)" in submit_src
        assert "RejectedExecutionError" in submit_src
        assert "with self._update_check_lock:" in check_src
        assert "if self._update_check_running:" in check_src
        assert "self._update_check_running = True" in check_src
        assert "self._update_check_running = False" in check_src
        assert "self._submit_update_check(False)" in start_src
        assert "command=lambda: self._submit_update_check(True)" in full_src


def test_startup_refresh_avoids_unnecessary_executor_hop():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        src = _function_source(ROOT / rel_path, "_startup_priority_refresh")

        assert "self._trigger_refresh(False" in src
        assert "self.bg_executor.submit(self._trigger_refresh" not in src


def test_chained_startup_refresh_avoids_unnecessary_executor_hop():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        src = _function_source(ROOT / rel_path, "_trigger_refresh")

        assert "self._trigger_refresh(False)" in src
        assert "self.bg_executor.submit(self._trigger_refresh, False)" not in src


def test_permanent_background_loops_do_not_consume_executor_workers():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        start_src = _function_source(ROOT / rel_path, "start_background_tasks")
        guardian_src = _function_source(ROOT / rel_path, "run_hotkey_guardian")

        assert "target=run_schedule" in start_src
        assert 'name="ScheduleLoop"' in start_src
        assert "self.bg_executor.submit(run_schedule)" not in start_src
        assert "target=guardian_loop" in guardian_src
        assert 'name="HotkeyGuardian"' in guardian_src
        assert "self.bg_executor.submit(guardian_loop)" not in guardian_src
        assert "duplicate start ignored" in guardian_src


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


def test_main_and_scheduler_schedule_update_checks_from_shared_policy():
    for rel_path in ("src/main.py", "src/scheduler.py"):
        full_src = (ROOT / rel_path).read_text(encoding="utf-8")
        start_src = _function_source(ROOT / rel_path, "start_background_tasks")

        assert "from cmuh_common.update_policy import AUTO_UPDATE_CHECK_TIMES" in full_src
        assert "for update_time in AUTO_UPDATE_CHECK_TIMES:" in start_src
        assert 'f"check-update-{scheduled_at.replace(\':\', \'\')}"' in start_src
        assert '.tag("update-check", "daily-3x")' in start_src


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


def test_f11_phototherapy_uses_finish_no_print_without_print_fallback():
    source_path = ROOT / "src" / "main.py"

    f11_main_src = _function_source(source_path, "_f11_快速完成_main")
    main_src = source_path.read_text(encoding="utf-8")

    assert "MENU_ID_FINISH_NO_PRINT = 276" in main_src
    assert "cmd_id = MENU_ID_FINISH_NO_PRINT" in f11_main_src
    assert "照光病人不改按 全部完成" in f11_main_src
    assert "fallback 全部完成" not in f11_main_src


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
