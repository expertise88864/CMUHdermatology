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


def _called_names(func: ast.FunctionDef) -> set[str]:
    """函式內以裸名稱呼叫的函式集合(供「有/沒有呼叫某函式」斷言)。"""
    return {
        node.func.id
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }


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


def test_main_and_scheduler_helper_launches_use_shared_launcher():
    for rel_path in ("src/main.py",):
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


def test_f1_pure_excimer_no_identity_no_51019_sets_療程1():
    """[2026-06-18 user 拍板] F1 純自費 Excimer:不動身份、不 key 51019、只設療程1。"""
    source_path = ROOT / "src" / "main.py"
    f1 = _function_node(source_path, "script_F1_adaptive")
    pure = _function_node(source_path, "_f1_pure_excimer")
    # F1 主函式把純 excimer 委派給專屬 helper
    assert "_f1_pure_excimer" in _called_names(f1)
    # 不動身份:F1 主函式與 helper 都不呼叫 _set_身份_自費
    assert "_set_身份_自費" not in _called_names(f1)
    assert "_set_身份_自費" not in _called_names(pure)
    # 不 key 51019:helper 沒有 "51019" 字面常數
    assert "51019" not in _constant_strings(pure)
    # 有設療程1:走 _set_療程_only,或(明天填代碼後)帶 set_療程 的代碼輸入
    assert ("_set_療程_only" in _called_names(pure)
            or "_script_code_input_adaptive" in _called_names(pure))


def test_f2_f3_pure_excimer_still_set_identity_01():
    """回歸:F2/F3 的純 excimer 維持「設身份 01」(本次只改 F1,不可波及 F2/F3)。"""
    source_path = ROOT / "src" / "main.py"
    for name in ("script_F2_adaptive", "script_F3_adaptive"):
        func = _function_node(source_path, name)
        assert "_set_身份_自費" in _called_names(func)
        assert '_set_身份_自費("01"' in _function_source(source_path, name)


def test_f1_pure_excimer_code_filled_1850159():
    """[2026-06-19 user] F1 純自費 Excimer 的醫令代碼已填 1850159(自費 Excimer 專用,
    非健保 51019)。若被清回空字串會讓 F1 純 Excimer 不 key 醫令 → 漏帳,故鎖住此值。"""
    source = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
    assert 'F1_PURE_EXCIMER_CODE = "1850159"' in source
    # 同時確認沒退回空字串
    assert 'F1_PURE_EXCIMER_CODE = ""' not in source


def test_set_療程_only_is_shared_single_source():
    """療程設定抽成 _set_療程_only 單一實作;_script_code_input_adaptive 也改用它。"""
    source_path = ROOT / "src" / "main.py"
    assert "_set_療程_only" in _called_names(
        _function_node(source_path, "_script_code_input_adaptive"))


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


def test_main_and_scheduler_schedule_cache_cleanup_for_standalone_launches():
    for rel_path in ("src/main.py",):
        src = _function_source(ROOT / rel_path, "start_background_tasks")

        assert "schedule_cleanup_in_background(self.bg_executor, delay_seconds=30)" in src


def test_main_uses_weak_http_session_registry_and_bounded_memory_caches():
    src = (ROOT / "src" / "main.py").read_text(encoding="utf-8")

    assert "_all_reg_sessions: WeakSet = WeakSet()" in src
    assert "trim_oldest_entries(_ttl_cache_store, _TTL_CACHE_MAX_ENTRIES)" in src
    assert "trim_oldest_entries(_parse_cache_store, _PARSE_CACHE_MAX_ENTRIES)" in src
    assert "trim_oldest_entries(_source_backoff_state, _SOURCE_STATE_MAX_ENTRIES)" in src


def test_main_and_scheduler_log_queues_are_bounded():
    for rel_path in ("src/main.py",):
        src = (ROOT / rel_path).read_text(encoding="utf-8")

        assert "self.log_queue = Queue(maxsize=5000)" in src
        assert "self.log_queue = Queue()" not in src


def test_main_and_scheduler_ui_queues_are_bounded():
    for rel_path in ("src/main.py",):
        src = (ROOT / rel_path).read_text(encoding="utf-8")

        assert "self.ui_queue = Queue(maxsize=10000)" in src
        assert "self.ui_queue = Queue()" not in src


def test_main_and_scheduler_background_executors_are_bounded():
    for rel_path in ("src/main.py",):
        src = (ROOT / rel_path).read_text(encoding="utf-8")

        assert "BoundedThreadPoolExecutor(" in src
        assert "self.bg_executor = ThreadPoolExecutor(" not in src
        assert "max_pending=60" in src


def test_refresh_batches_use_local_executor_instead_of_bg_queue():
    for rel_path in ("src/main.py",):
        src = _function_source(ROOT / rel_path, "_trigger_refresh")

        assert 'thread_name_prefix="RefreshBatch"' in src
        assert "refresh_pool.submit(check_appointment_count" in src
        assert "self.bg_executor.submit(check_appointment_count" not in src


def test_duty_info_uses_local_executor_instead_of_bg_queue():
    for rel_path in ("src/main.py",):
        src = _function_source(ROOT / rel_path, "_fetch_all_duty_info")

        assert 'thread_name_prefix="DutyInfo"' in src
        assert "duty_pool.submit(self._run_single_duty_query" in src
        assert "self.bg_executor.submit(self._run_single_duty_query" not in src


def test_duty_info_is_single_flight_and_only_caches_complete_success():
    for rel_path in ("src/main.py",):
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
    for rel_path in ("src/main.py",):
        for name in ("fetch_duty_doctor", "fetch_saturday_duty_doctor", "fetch_duty_vs"):
            src = _function_source(ROOT / rel_path, name)

            assert 'return doctor_name not in {"查詢失敗", "網路錯誤", "查詢錯誤"}' in src


def test_refresh_submit_rejection_restores_ui_state():
    for rel_path in ("src/main.py",):
        src = _function_source(ROOT / rel_path, "_trigger_refresh")

        assert "RejectedExecutionError" in src
        assert "refresh_future = self.bg_executor.submit(run_parallel_checks)" in src
        assert "refresh_future.add_done_callback(_handle_refresh_submit_rejected)" in src
        assert "self._active_refresh_signature = None" in src
        assert 'self.status_text.set("狀態: 背景佇列忙碌，刷新稍後重試")' in src
        assert 'self.refresh_button.config(state="normal")' in src


def test_clinic_worker_submit_rejection_clears_running_flag():
    for rel_path in ("src/main.py",):
        src = _function_source(ROOT / rel_path, "_update_clinic_lights_loop")

        assert "RejectedExecutionError" in src
        assert "clinic_future = self.bg_executor.submit(guarded_run_update, rooms_to_check)" in src
        assert "clinic_future.add_done_callback(_handle_clinic_submit_rejected)" in src
        assert "self._clinic_lights_worker_running = False" in src


def test_clinic_stat_submit_is_deduplicated_and_retries_rejected_closing_save():
    for rel_path in ("src/main.py",):
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
    for rel_path in ("src/main.py",):
        src = _function_source(ROOT / rel_path, "_run_on_ui_thread")

        assert 'getattr(self, "_shutting_down", False)' in src
        assert "stop_event_main.is_set()" in src
        assert "except (tk.TclError, RuntimeError):" in src
        assert src.count("return False") >= 2
        assert src.count("return True") >= 2


def test_refresh_entrypoint_reroutes_to_tk_thread():
    for rel_path in ("src/main.py",):
        src = _function_source(ROOT / rel_path, "_trigger_refresh")

        assert "threading.current_thread() is not threading.main_thread()" in src
        assert "queued_doctors = list(specific_doctors) if specific_doctors is not None else None" in src
        assert "self.root.after(0, lambda: self._trigger_refresh(" in src


def test_clinic_polling_snapshots_tk_modes_before_background_work():
    for rel_path in ("src/main.py",):
        src = _function_source(ROOT / rel_path, "_update_clinic_lights_loop")
        mode_read = "self.clinic_display_mode_vars[i].get()"

        assert src.count(mode_read) == 1
        assert src.index(mode_read) < src.index("def run_update(rooms):")
        assert "for i, (room_code, configured_mode) in enumerate(rooms):" in src
        assert "mode = configured_mode" in src


def test_clinic_room_defaults_use_shared_101_102_constant():
    main_src = (ROOT / "src/main.py").read_text(encoding="utf-8")

    assert "DEFAULT_CLINIC_ROOMS" in main_src
    assert '["181", "182"]' not in main_src


def test_clinic_room_settings_migrate_legacy_defaults_on_load():
    for rel_path in ("src/main.py",):
        src = _function_source(ROOT / rel_path, "load_clinic_settings")

        assert "normalize_clinic_rooms(settings.get(\"rooms\"))" in src
        assert "_atomic_write_json(file_path, settings)" in src
        assert "門診動態診間設定已遷移為" in src


def test_scheduled_background_submits_detect_rejected_futures():
    for rel_path in ("src/main.py",):
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
    for rel_path in ("src/main.py",):
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
    for rel_path in ("src/main.py",):
        loader_src = _function_source(ROOT / rel_path, "_start_hotkey_module_loading")
        deferred_src = _function_source(ROOT / rel_path, "deferred_initialization")

        assert "hotkey_future = self.bg_executor.submit(self._prepare_hotkeys_background)" in loader_src
        assert "hotkey_future.add_done_callback(_handle_hotkey_loader_rejected)" in loader_src
        assert "RejectedExecutionError" in loader_src
        assert "self._heavy_modules_loading = False" in loader_src
        assert "self.root.after(5000, self._start_hotkey_module_loading)" in loader_src
        assert "self._start_hotkey_module_loading()" in deferred_src


def test_url_shortener_recovers_from_rejected_submit():
    for rel_path in ("src/main.py",):
        src = _function_source(ROOT / rel_path, "_start_shorten_url")

        assert "shorten_future = self.bg_executor.submit(self._run_url_shortener, long_url)" in src
        assert "shorten_future.add_done_callback(_handle_shorten_submit_rejected)" in src
        assert "RejectedExecutionError" in src
        assert 'self.shorten_btn.config(state="normal")' in src
        assert "self._run_on_ui_thread(_reset_shorten_ui)" in src


def test_settings_promo_recovers_from_rejected_submit():
    for rel_path in ("src/main.py",):
        src = _function_source(ROOT / rel_path, "ensure_settings_promo_loaded")

        assert "promo_future = self.bg_executor.submit(self._load_settings_promo_image)" in src
        assert "promo_future.add_done_callback(_handle_promo_submit_rejected)" in src
        assert "RejectedExecutionError" in src
        assert "self._settings_promo_loading = False" in src
        assert "self.root.after(5000, self.ensure_settings_promo_loaded)" in src


def test_clock_status_query_is_single_flight_and_recovers_from_rejection():
    for rel_path in ("src/main.py",):
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
    for rel_path in ("src/main.py",):
        src = _function_source(ROOT / rel_path, "start_background_tasks")

        assert "def _submit_startup_background(task_name, fn, *args, attempt=1):" in src
        assert "isinstance(fut.exception(), RejectedExecutionError)" in src
        assert "if attempt >= 3:" in src
        assert "attempt=attempt + 1" in src
        assert "future.add_done_callback(_retry_if_rejected)" in src
        assert '"master-schedule"' in src
        assert '"duty-info"' in src


def test_update_check_is_single_flight_and_manual_submit_reports_rejection():
    for rel_path in ("src/main.py",):
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
    for rel_path in ("src/main.py",):
        src = _function_source(ROOT / rel_path, "_startup_priority_refresh")

        assert "self._trigger_refresh(False" in src
        assert "self.bg_executor.submit(self._trigger_refresh" not in src


def test_chained_startup_refresh_avoids_unnecessary_executor_hop():
    for rel_path in ("src/main.py",):
        src = _function_source(ROOT / rel_path, "_trigger_refresh")

        assert "self._trigger_refresh(False)" in src
        assert "self.bg_executor.submit(self._trigger_refresh, False)" not in src


def test_permanent_background_loops_do_not_consume_executor_workers():
    for rel_path in ("src/main.py",):
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
    for rel_path in ("src/main.py",):
        src = _function_source(ROOT / rel_path, "start_background_tasks")

        # [2026-06-18] 自動重開機(含 now.second<10 的分鐘窗判斷)已移除;此迴圈現只剩
        # schedule.run_pending() + 低頻 5 秒等待。
        assert "stop_event_main.wait(5.0)" in src
        assert "stop_event_main.wait(1.0)" not in src
        assert "auto_reboot" not in src   # 防回歸:自動重開機不應再出現


def test_main_and_scheduler_background_tasks_start_once_and_clear_schedule():
    for rel_path in ("src/main.py",):
        full_src = (ROOT / rel_path).read_text(encoding="utf-8")
        func_src = _function_source(ROOT / rel_path, "start_background_tasks")

        assert "self._background_tasks_started = False" in full_src
        assert "if self._background_tasks_started:" in func_src
        assert "self._background_tasks_started = True" in func_src
        assert "schedule.clear()" in func_src
        assert func_src.index("schedule.clear()") < func_src.index("schedule.every")


def test_main_and_scheduler_schedule_update_checks_from_shared_policy():
    for rel_path in ("src/main.py",):
        full_src = (ROOT / rel_path).read_text(encoding="utf-8")
        start_src = _function_source(ROOT / rel_path, "start_background_tasks")

        assert "from cmuh_common.update_policy import AUTO_UPDATE_CHECK_TIMES" in full_src
        assert "for update_time in AUTO_UPDATE_CHECK_TIMES:" in start_src
        assert 'f"check-update-{scheduled_at.replace(\':\', \'\')}"' in start_src
        assert '.tag("update-check", "daily-3x")' in start_src


def test_main_and_scheduler_ui_queue_polling_is_bounded():
    for rel_path in ("src/main.py",):
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
    no_print_src = _function_source(source_path, "_f11_send_finish_no_print")
    all_finish_src = _function_source(source_path, "_f11_click_finish_all")
    main_src = source_path.read_text(encoding="utf-8")

    assert "MENU_ID_FINISH_NO_PRINT = 276" in main_src
    assert 'if course_value in ("2", "3"):' in f11_main_src
    assert "_f11_send_finish_no_print" in f11_main_src
    assert "_f11_click_finish_all" in f11_main_src
    assert (
        no_print_src.index('_find_menu_command_id_by_text(main_hwnd, "完成不印")')
        < no_print_src.index("_send_yiling_menu_command(main_hwnd, cmd_id)")
    )
    assert "candidate_ids" in no_print_src
    assert "MENU_ID_FINISH_NO_PRINT not in candidate_ids" in no_print_src
    assert "照光病人不改按 全部完成" in no_print_src
    assert '"全部完成"' in all_finish_src
    assert "fallback 全部完成" not in no_print_src

    # [2026-06-05] 卡號偵測 / 自動補 IC / 卡號把關功能已移除：
    # route B 一律直接按「全部完成」，不再讀卡號。確認相關符號徹底消失。
    assert "_f11_ensure_ic_card" not in main_src
    assert "_f11_card_allows_finish_all" not in main_src
    assert "_f11_normalize_card_value" not in main_src
    assert "_find_卡號_edit_hwnd" not in main_src
    assert "_f11_ensure_ic_card" not in all_finish_src
    assert "card_value" not in all_finish_src


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
    # 療程設定(現抽成 _set_療程_only)仍在「代碼輸入完成檢查」之後才執行
    assert (
        code_input_src.index("if code and not workflow_ok:")
        < code_input_src.index("_set_療程_only")
    )
    assert code_input_src.count("_mark_hotkey_action_time()") >= 3

    send_src = _function_source(source_path, "_send_yiling_menu_command")
    assert "-> bool" in send_src
    assert "PostMessageW" in send_src
    assert "return False" in send_src


# === [stability r4] 對抗式審查確認發現的回歸保護 ===

def test_uvb_messagebox_marks_awaiting_user_for_hotkey_watchdog():
    """F2/F3 等醫師回應的 MessageBoxW 期間標記 awaiting-user；硬上限看門狗在此狀態
    不得強制解鎖(否則醫師回應前第二支熱鍵重入、與卡在對話框的第一流程並行操作 HIS)。"""
    source_path = ROOT / "src" / "main.py"
    full = source_path.read_text(encoding="utf-8")
    assert "def _hotkey_awaiting_user_scope(" in full

    core_src = _function_source(source_path, "_update_uvb_dose_core")
    # 兩處 MessageBoxW(劑量確認 + uncertain 行)都要被 awaiting-user scope 包住
    assert core_src.count("with _hotkey_awaiting_user_scope():") >= 2
    assert "MessageBoxW" in core_src

    watch_src = _function_source(source_path, "_hotkey_hard_timeout_watch")
    assert "_hotkey_awaiting_user" in watch_src
    assert "awaiting" in watch_src  # awaiting 狀態下不強制解鎖、再等一個週期
    assert "等待醫師回應中，按 F12 可取消等待" in watch_src

    interrupt_src = _function_source(source_path, "interrupt_automation")
    assert "if _hotkey_awaiting_user:" in interrupt_src
    assert "_hotkey_cancelled_threads.add(thread_ident)" in interrupt_src
    assert "self._subsystem_running = False" in interrupt_src
    assert "self._subsystem_token += 1" in interrupt_src

    check_stop_src = _function_source(source_path, "check_stop")
    assert "threading.get_ident() in _hotkey_cancelled_threads" in check_stop_src

    assert core_src.count("check_stop()") >= 2


def test_refresh_single_flight_flag_set_on_main_thread():
    """單飛旗標必須在 main thread 同步設(submit 前、同一鎖區塊內)，不能只在 worker 內
    非同步才設，否則 submit↔worker 啟動空窗會讓下一個 trigger 重複 submit 同一刷新。"""
    source_path = ROOT / "src" / "main.py"
    full = source_path.read_text(encoding="utf-8")
    idx_sig = full.find("self._active_refresh_signature = req_signature")
    assert idx_sig != -1
    # 緊接著(同鎖區塊內)就同步設旗標
    snippet = full[idx_sig:idx_sig + 500]
    assert "self._refresh_worker_running = True" in snippet


def test_reg64_calendar_cache_guarded_by_lock():
    """reg64 月曆快取：背景燈號 worker 寫、main thread 月曆讀，需同一把鎖保護。"""
    source_path = ROOT / "src" / "main.py"
    full = source_path.read_text(encoding="utf-8")
    assert "self._reg64_cache_lock = threading.Lock()" in full
    write_src = _function_source(source_path, "_update_reg64_public_cache")
    assert "with self._reg64_cache_lock:" in write_src
    read_src = _function_source(source_path, "_reg64_total_for_calendar_cell")
    assert "with self._reg64_cache_lock:" in read_src


# === [r5 穩定性 + 效能優化回歸保護] ===

def test_reset_clinic_stats_resets_under_tracker_lock():
    """[r5] reset 的記憶體 tracker 重置必須在 _tracker_lock 內(與背景 worker 序列化)，
    避免兩緒同時改同一 dict/set 觸發 KeyError/dict-changed 或統計錯亂。"""
    source_path = ROOT / "src" / "main.py"
    src = _function_source(source_path, "reset_clinic_stats")
    assert "with self._tracker_lock:" in src
    lock_idx = src.index("with self._tracker_lock:")
    # 重置欄位必須落在鎖之後
    assert src.index("tracker['durations'] = []") > lock_idx
    assert src.index("tracker['patient_checkin_times'] = {}") > lock_idx


def test_dynamic_state_read_guarded_by_lock():
    """[r5] _get_clinic_dynamic_state 讀取取同一把鎖(persist 整個 rebind / clear pop 都持鎖)。"""
    source_path = ROOT / "src" / "main.py"
    src = _function_source(source_path, "_get_clinic_dynamic_state")
    assert "with self._clinic_dynamic_state_lock:" in src


def test_f11_unknown_popup_scan_throttled():
    """[r5] F11 watcher 的全域 EnumWindows 診斷掃描必須節流(非每輪)，省無謂 CPU。"""
    source_path = ROOT / "src" / "main.py"
    src = _function_source(source_path, "_f11_popup_watcher")
    assert "UNKNOWN_SCAN_INTERVAL" in src
    assert "last_unknown_scan" in src


def test_menu_tree_dump_gated_once_per_session():
    """[r5] _dump_menu_tree(owner-draw 選單每次 F11 route A 都 dump ~200 行，log 暴漲
    主因)改為每 session 只 dump 一次。"""
    source_path = ROOT / "src" / "main.py"
    full = source_path.read_text(encoding="utf-8")
    assert "_menu_tree_dumped_once" in full
    src = _function_source(source_path, "_dump_menu_tree")
    assert "if _menu_tree_dumped_once:" in src
    assert "return" in src


def test_clinic_light_history_written_compact():
    """[r5] 大型純機器快取 clinic_light_history.json 用 compact(indent=None)序列化。"""
    source_path = ROOT / "src" / "main.py"
    full = source_path.read_text(encoding="utf-8")
    assert 'indent=None, separators=(",", ":")' in full


def test_startup_vacuums_old_clinic_count_rows():
    """[r5] 啟動時呼叫 vacuum_old_entries 清過老 row(原本死碼從未被呼叫)。"""
    source_path = ROOT / "src" / "main.py"
    src = _function_source(source_path, "load_cached_data")
    assert "vacuum_old_entries" in src


def test_abbrev_taskkill_runs_off_ui_thread():
    """[fix A] 偵測到可自動關閉的展開程式時，taskkill 必須先丟背景執行緒，
    關完才回 UI thread 掛 hook(否則 UI 凍最壞 3s/個)。"""
    source_path = ROOT / "src" / "main.py"
    src = _function_source(source_path, "_install_abbrev_listeners")
    assert "is_auto_closable" in src
    assert "bg_executor.submit" in src
    assert "_abbrev_bg_close_running" in src
    # 收尾函式存在且同步監看狀態
    finish_src = _function_source(source_path, "_finish_install_abbrev")
    assert "self._abbrev_last_external = getattr(eng, '_external_expander', None)" \
        in finish_src


def test_abbrev_monitor_syncs_state_after_install():
    """[fix B] 監看的 last 狀態不可在 install 前直接記 ext(自動關閉成功後對方重啟
    會 ext==last 永不再處理 → 雙重展開並存)；改由 _finish_install_abbrev 記
    install 後實際狀態。"""
    source_path = ROOT / "src" / "main.py"
    src = _function_source(source_path, "_abbrev_monitor_external")
    # 監看迴圈本身不再直接寫 last=ext
    assert "self._abbrev_last_external = ext" not in src
    assert "_install_abbrev_listeners()" in src


def test_hotkey_guardian_covers_abbrev_only_mode():
    """[fix C] 院外模式/解析度不符(無 F 鍵 profile)但縮寫啟用中 → guardian 仍須監看
    hook 健康(縮寫與 F 鍵共用同一個 keyboard 底層 hook，死了要能自動重啟恢復)。"""
    source_path = ROOT / "src" / "main.py"
    src = _function_source(source_path, "_hotkey_health_tick")
    assert "abbrev_active" in src
    assert "if not has_profile and not abbrev_active:" in src


def test_abbrev_export_import_buttons_exist():
    """[新功能 2026-06-11] 縮寫設定匯出/匯入：方法存在、匯入前驗證 items + 確認對話框、
    匯入只取代清單(enabled/自動關閉等機台偏好不被匯入檔覆寫)。"""
    source_path = ROOT / "src" / "main.py"
    full = source_path.read_text(encoding="utf-8")
    assert '"匯出設定"' in full and '"匯入設定"' in full
    imp_src = _function_source(source_path, "_abbrev_import_settings")
    # 先驗證檔案真的含 items，避免亂選檔被預設值機制靜默變成「匯入內建預設」
    assert 'raw.get("items")' in imp_src
    assert "askyesno" in imp_src  # 取代前確認
    assert "cur.items = new_cfg.items" in imp_src  # 只取代清單
    exp_src = _function_source(source_path, "_abbrev_export_settings")
    assert "asksaveasfilename" in exp_src


def test_heavy_network_imports_are_lazy_after_splash():
    """[r5] requests/urllib3/bs4(~500ms)延後到 __init__(splash 後)才 import，
    模組頂層只佔位 None；加快感知啟動。"""
    source_path = ROOT / "src" / "main.py"
    full = source_path.read_text(encoding="utf-8")
    # PEP 563 讓註解變字串，才能延後 import
    assert "from __future__ import annotations" in full
    # lazy bootstrap 機制存在
    assert "def _ensure_network_imports(" in full
    # __init__ 內(8 空白縮排)有 bootstrap 呼叫(splash 之後)
    assert "\n        _ensure_network_imports()" in full
    # TYPE_CHECKING 慣用法：型別檢查期 import 真模組、執行期只佔位 None(else 分支，縮排)
    assert "if TYPE_CHECKING:" in full
    assert "\n    requests = None" in full


def test_abbrev_bg_close_submit_failure_resets_flag():
    """[review C 2026-06-12] 背景關閉外部展開軟體的 submit 可能被 BoundedExecutor
    拒絕；失敗時必須重置 _abbrev_bg_close_running 並 fall through 同步路徑，否則
    旗標永久卡 True → 整個 session 的背景關閉路徑停用。"""
    src = _function_source(ROOT / "src/main.py", "_install_abbrev_listeners")

    assert re.search(
        r"try:\s*\n\s*self\.bg_executor\.submit\(_bg_close\)\s*\n\s*return\s*\n"
        r"\s*except Exception:\s*\n\s*self\._abbrev_bg_close_running = False",
        src,
    ), "submit(_bg_close) 必須包 try/except 且失敗時重置旗標"


def test_f11_checks_card_before_phototherapy_complete():
    """[2026-06-19 user] F11 快速完成前要先檢查:療程 2/3(照光)且卡號空白 → 中止 +
    提示。鎖住 script_F11_adaptive 有呼叫 _f11_precheck_card_for_phototherapy。"""
    source_path = ROOT / "src" / "main.py"
    f11 = _function_node(source_path, "script_F11_adaptive")
    assert "_f11_precheck_card_for_phototherapy" in _called_names(f11)
    pre = _function_node(source_path, "_f11_precheck_card_for_phototherapy")
    # 前置檢查要讀療程欄與卡號欄
    calls = _called_names(pre)
    assert "_find_療程_edit_hwnd" in calls
    assert "_find_療程卡號_edit_hwnd" in calls
    # 提示文字含「目前卡號未輸入」
    assert "目前卡號未輸入" in _function_source(source_path, "_f11_precheck_card_for_phototherapy")


def test_re_room_matches_letter_prefixed_room():
    """[2026-06-19 user] 止掛信診間:_RE_ROOM 要能配 G06診(字母前綴)+ 101診(純數字),
    否則 G06診(張廖年峰)會解析不到 → 信裡顯示「診間未提供」。"""
    source = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
    m = re.search(r"_RE_ROOM\s*=\s*re\.compile\(r'([^']+)'\)", source)
    assert m, "找不到 _RE_ROOM 定義"
    pat = re.compile(m.group(1))
    assert pat.search("林某 (G06診) 已掛號").group(1) == "G06診"
    assert pat.search("王某 (101診) 已掛號").group(1) == "101診"
    assert pat.search("(自費門診)") is None
    assert pat.search("(已關診)") is None


def test_overview_primary_rooms_a101_a103_and_no_legacy_181_182():
    """[2026-06-19 user] 總覽醫師門診表:本科主診間改為 A101→A102→A103診。
    這三間自動隱藏診間號(自家診間免標)、排序排最前;其餘診間才顯示「(診間)」。
    舊的 181診/182診 硬編碼(顯示隱藏 + 排序桶)必須移除,改用 _OVERVIEW_PRIMARY_ROOMS 常數。"""
    source = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
    # 常數存在且正是 A101/A102/A103診(依序 → 決定排序桶)
    m = re.search(r"_OVERVIEW_PRIMARY_ROOMS\s*=\s*\(([^)]*)\)", source)
    assert m, "找不到 _OVERVIEW_PRIMARY_ROOMS 定義"
    rooms = re.findall(r'"([^"]+)"', m.group(1))
    assert rooms == ["A101診", "A102診", "A103診"], rooms
    # 舊的門診表硬編碼診間字串已不存在(避免回退到 181/182診)
    assert '"181診"' not in source
    assert '"182診"' not in source
    # 顯示隱藏與排序都改用常數(不再硬編碼診間字串)
    assert "room not in _OVERVIEW_PRIMARY_ROOMS" in source
    assert "room in _OVERVIEW_PRIMARY_ROOMS" in source


def test_clinic_widget_mode_floating_only_no_appbar():
    """[2026-06-19 user] 門診動態小工具:邊緣常駐條(AppBar)已移除(與強制近全螢幕的醫囑系統衝突),
    只保留浮動視窗。守門:
      - 單一真實來源 clinic_widget_mode(只 off/floating),無舊 floating_clinic_enabled。
      - appbar 相關程式碼/生命週期方法已完全移除。
      - 舊設定 "appbar" 會被遷移為 "floating"(_normalize_widget_mode)。"""
    source = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
    assert "floating_clinic_enabled" not in source
    assert "self.clinic_widget_mode" in source
    # appbar 生命週期/視窗已不存在(只剩遷移用的字串字面量可出現)
    for gone in ("def _open_clinic_appbar(", "def _close_clinic_appbar(",
                 "def _clinic_appbar_tick(", "clinic_appbar_win", "ClinicAppBar"):
        assert gone not in source, gone
    # appbar 模組與測試已刪除
    assert not (ROOT / "src" / "cmuh_common" / "clinic_appbar.py").exists()
    # "appbar" 遷移為 "floating"
    norm_src = _function_source(ROOT / "src" / "main.py", "_normalize_widget_mode")
    assert "floating" in norm_src and "appbar" in norm_src


def test_overrun_polling_and_closed_hidden():
    """[2026-06-19 user] 早診拖班:輪詢時段「關診才前進」;浮動視窗直接顯示看診中時段、關診隱藏。"""
    source = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
    # 輪詢有套用拖班(overrun)判定
    assert "_overrun_effective_tc(room_code, tc_effective)" in source
    assert "def _overrun_effective_tc(" in source
    # 浮動收集不再強制「目前電腦時間」(room_status_for_current_slot 已移除)
    assert "room_status_for_current_slot" not in source
    fc = (ROOT / "src" / "cmuh_common" / "floating_clinic.py").read_text(encoding="utf-8")
    assert "room_status_for_current_slot" not in fc
    # should_show_room:closed 一律隱藏(早診拖班看完即消失)
    show_src = _function_source(
        ROOT / "src" / "cmuh_common" / "floating_clinic.py", "should_show_room")
    assert "if s.closed:" in show_src


def test_morning_polling_and_residual_closed_guard_wired():
    """[2026-06-22 user] 早上每分鐘輪詢 + 早晨殘留盤面防呆 已接進輪詢迴圈。
    run_update 邏輯難以純單元測,以原始碼守門避免被改掉。"""
    src = (ROOT / "src" / "main.py").read_text(encoding="utf-8")
    # Problem 1:早上起跑窗 45-75 秒隨機輪詢(窗外 60-90)
    assert "clinic_tight_poll_window(now)" in src
    assert "random.randint(45, 75)" in src
    # Problem 2a:跨日重置(記憶體 tracker 清掉昨天的關診/活動殘留)
    assert "is_new_day" in src and "tracker['date'] = today_str" in src
    # Problem 2b:殘留盤面防呆(純函式判定),且必須在 tracker 統計被本輪 data 污染【之前】就蓋 pending
    assert "is_residual_stale_closed(" in src
    run_src = _function_source(ROOT / "src" / "main.py", "_update_clinic_lights_loop")
    # 殘留判定要早於「current_completed_set 取自 data」(否則昨天的看診號會先被寫進今天 tracker)
    assert (run_src.index("is_residual_stale_closed(")
            < run_src.index("current_completed_set = data.get(")), "殘留防呆必須在 tracker 污染前"
