"""Anonymous HIS memo races and read-back failures for F1–F3."""

import ctypes
import threading
from contextlib import nullcontext
from datetime import date, timedelta
from queue import Queue
from types import SimpleNamespace
import sys

import pytest

import main
from main import _record_his_action as record_his_action_real


def _memo(*, kind="UVB"):
    prior = date.today() - timedelta(days=3)
    return (f"{kind}: 800 mj/cm2 (35) on ({prior:%Y/%m/%d}) "
            "add 50 mj/cm2, MAX: 1500 mj/cm2\n"
            "Pathology report: synthetic note\n"
            "Remove stitches")


class FakeMemoPort:
    def __init__(self, text=None):
        self.text = _memo() if text is None else text
        self.main_hwnd = 10
        self.memo_hwnd = 20
        self.kind = "uvb"
        self.writes = []
        self.reads = 0

    def find_main_window(self):
        return self.main_hwnd

    def locate_phototherapy(self, _main_hwnd):
        return self.memo_hwnd, self.kind

    def read_memo(self, _memo_hwnd):
        self.reads += 1
        return self.text

    def write_memo(self, _memo_hwnd, text):
        self.writes.append(text)
        self.text = text
        return True


@pytest.fixture
def fake_his(monkeypatch):
    warnings = []
    monkeypatch.setattr(main, "check_stop", lambda: None)
    monkeypatch.setattr(main, "_show_uvb_warning",
                        lambda _hwnd, title, msg: warnings.append((title, msg)))
    monkeypatch.setattr(main, "_record_his_action", lambda *a, **k: None)
    monkeypatch.setattr(main, "_record_uvb_write", lambda *a, **k: None)
    return warnings


def test_confirmation_does_not_overwrite_memo_changed_while_waiting(
        monkeypatch, fake_his):
    prior = date.today() - timedelta(days=40)
    port = FakeMemoPort(_memo().replace(
        (date.today() - timedelta(days=3)).strftime("%Y/%m/%d"),
        prior.strftime("%Y/%m/%d")))

    def confirm(*_args, **_kwargs):
        port.text += "\nNew HIS edit while confirmation was open"
        return True

    monkeypatch.setattr(main, "_photo_confirm_yesno", confirm)
    assert main._update_uvb_dose_core("F2", strict=True, memo_port=port) is False
    assert port.writes == []
    assert "New HIS edit" in port.text
    assert any("變動" in title + msg for title, msg in fake_his)


@pytest.mark.parametrize("changed_target", ["main", "memo"])
def test_changed_his_window_or_field_is_not_written(
        monkeypatch, fake_his, changed_target):
    port = FakeMemoPort()
    if changed_target == "main":
        original = port.find_main_window
        calls = 0

        def find_main():
            nonlocal calls
            calls += 1
            return original() if calls == 1 else 11

        monkeypatch.setattr(port, "find_main_window", find_main)
    else:
        original = port.locate_phototherapy
        calls = 0

        def locate(main_hwnd):
            nonlocal calls
            calls += 1
            return original(main_hwnd) if calls == 1 else (21, "uvb")

        monkeypatch.setattr(port, "locate_phototherapy", locate)
    assert main._update_uvb_dose_core("F2", strict=True, memo_port=port) is False
    assert port.writes == []


def test_readback_must_preserve_unrelated_history_lines(fake_his):
    class ChangedByHis(FakeMemoPort):
        def write_memo(self, memo_hwnd, text):
            super().write_memo(memo_hwnd, text)
            self.text = self.text.replace("\nRemove stitches", "")
            return True

    port = ChangedByHis()
    assert main._update_uvb_dose_core("F2", strict=True, memo_port=port) is False
    assert len(port.writes) == 1
    assert any("驗證失敗" in title or "無法驗證" in title
               for title, _ in fake_his)


def test_excimer_write_is_not_reported_verified_when_readback_is_stale(
        monkeypatch, fake_his):
    text = _memo(kind="Excimer")
    port = FakeMemoPort(text)
    port.kind = "pure_excimer"
    audit = []
    monkeypatch.setattr(main, "_record_his_action",
                        lambda *_a, **fields: audit.append(fields))
    monkeypatch.setattr(port, "write_memo", lambda *_args: True)
    result = main._f23_pure_excimer_update(
        10, 20, text, label="F2", memo_port=port)
    # Existing billing policy still allows the caller to set identity 01.
    assert result == main._F23_PURE_EXCIMER_UNVERIFIED
    assert len(audit) == 1
    assert audit[0]["outcome"] == main._LEDGER_MISMATCH
    assert audit[0]["detail"].to_payload() == {
        "t": "reason", "code": "readback_mismatch"}
    assert any("無法驗證" in title + msg or "驗證失敗" in title + msg
               for title, msg in fake_his)


def test_excimer_verified_memo_write_has_nonidentifying_audit(
        monkeypatch, fake_his):
    port = FakeMemoPort(_memo(kind="Excimer"))
    port.kind = "pure_excimer"
    audit = []
    monkeypatch.setattr(main, "_record_his_action",
                        lambda *_a, **fields: audit.append(fields))
    result = main._f23_pure_excimer_update(
        10, 20, port.text, label="F2", memo_port=port)
    assert result == main._F23_PURE_EXCIMER
    assert len(port.writes) == 1
    assert len(audit) == 1
    assert audit[0]["outcome"] == main._LEDGER_OK
    assert audit[0]["value"].to_payload() == {
        "t": "measure", "dose": 850, "count": 36}


def test_excimer_mismatch_locator_stays_outside_audit_fields(
        monkeypatch, fake_his):
    port = FakeMemoPort(_memo(kind="Excimer"))
    port.kind = "pure_excimer"
    original_write = port.write_memo

    def altered_readback(memo_hwnd, text):
        original_write(memo_hwnd, text)
        port.text += "\nSynthetic HIS edit"
        return True

    monkeypatch.setattr(port, "write_memo", altered_readback)
    queue = Queue()
    locator = object()
    sampled = []
    monkeypatch.setattr(main, "_record_his_action", record_his_action_real)
    monkeypatch.setattr(main, "_ledger_shutting_down", False)
    monkeypatch.setattr(main, "_ledger_queue", queue)
    monkeypatch.setattr(main, "_ensure_ledger_writer", lambda: None)
    monkeypatch.setattr(main, "_sample_patient_locator",
                        lambda hwnd: sampled.append(hwnd) or locator)
    result = main._f23_pure_excimer_update(
        10, 20, port.text, label="F2", memo_port=port)
    assert result == main._F23_PURE_EXCIMER_UNVERIFIED
    assert sampled == [10]
    _, _, fields, _, separate_locator = queue.get_nowait()
    assert fields["outcome"] == main._LEDGER_MISMATCH
    assert separate_locator is locator
    assert "locator" not in fields
    assert "Synthetic HIS edit" not in str(fields)


def test_f1_reclassified_excimer_warning_never_promises_identity_01(
        monkeypatch, fake_his):
    port = FakeMemoPort(_memo(kind="Excimer"))
    port.kind = "pure_excimer"
    monkeypatch.setattr(port, "write_memo", lambda *_a: False)
    result = SimpleNamespace(
        new_text=port.text, new_dose=850, new_count=36, decrease_note="")
    assert main._write_excimer_memo_checked(
        10, 20, port.text, result, "F1", memo_port=port) is False
    assert any("F1 不自動設定身份 01" in msg for _, msg in fake_his)
    assert not any("身份仍依原規則設為自費" in msg for _, msg in fake_his)


def test_f1_excimer_parse_error_warning_never_promises_identity_01(
        monkeypatch, fake_his):
    from cmuh_common import uvb_dose

    monkeypatch.setattr(uvb_dose, "update_uvb_in_text",
                        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("synthetic")))
    assert main._f23_pure_excimer_update(
        10, 20, _memo(kind="Excimer"), label="F1") == (
            main._F23_PURE_EXCIMER_UNVERIFIED)
    assert any("F1 不自動設定身份 01" in msg for _, msg in fake_his)
    assert not any("身份仍依原規則設為自費" in msg for _, msg in fake_his)


def test_f1_excimer_sanity_warning_never_promises_identity_01(fake_his):
    from cmuh_common.uvb_dose import UvbAction

    main._warn_excimer_not_updated(
        10, "F1", SimpleNamespace(
            action=UvbAction.SANITY_FAIL, sanity_reason="synthetic format"))
    assert any("F1 不自動設定身份 01" in msg for _, msg in fake_his)
    assert not any("身份仍會設為自費" in msg for _, msg in fake_his)


def test_f1_reclassified_excimer_f12_warns_about_already_placed_51019(
        monkeypatch, fake_his):
    port = FakeMemoPort(_memo(kind="Excimer"))
    port.kind = "pure_excimer"
    cancelled = False
    original_write = port.write_memo

    def write(memo_hwnd, text):
        nonlocal cancelled
        wrote = original_write(memo_hwnd, text)
        cancelled = True
        return wrote

    def check_stop():
        if cancelled:
            raise main.SubsystemInterrupted("F12")

    monkeypatch.setattr(port, "write_memo", write)
    monkeypatch.setattr(main, "check_stop", check_stop)
    with pytest.raises(main.SubsystemInterrupted):
        main._update_uvb_dose_core(
            "F1", strict=False, codes_already_placed="51019 醫令與療程 1",
            memo_port=port)
    assert len(port.writes) == 1
    assert any("51019" in msg and "刪除" in msg for _, msg in fake_his)
    assert not any("刪除 51019/療程 1" in msg for _, msg in fake_his)
    assert any("確認" in msg and "療程 1" in msg and "Excimer" in msg
               for _, msg in fake_his)


def test_f1_reclassified_excimer_regular_warning_preserves_course_one(
        monkeypatch, fake_his):
    monkeypatch.setattr(main, "_update_uvb_dose_core",
                        lambda *_a, **_k: main._F23_PURE_EXCIMER_ABORTED)
    monkeypatch.setattr(main, "_find_hospital_main_window", lambda: 10)
    assert main._f1_update_uvb_dose_if_present(label="F1") is False
    assert not any("刪除 51019/療程 1" in msg for _, msg in fake_his)
    assert any("確認" in msg and "療程 1" in msg and "Excimer" in msg
               for _, msg in fake_his)


def test_cancellation_during_uvb_write_stops_before_followup(
        monkeypatch, fake_his):
    port = FakeMemoPort()
    audit = []
    monkeypatch.setattr(main, "_record_his_action",
                        lambda *_a, **fields: audit.append(fields))
    cancelled = False
    original_write = port.write_memo

    def write(memo_hwnd, text):
        nonlocal cancelled
        result = original_write(memo_hwnd, text)
        cancelled = True
        return result

    def check_stop():
        if cancelled:
            raise main.SubsystemInterrupted("F12")

    monkeypatch.setattr(port, "write_memo", write)
    monkeypatch.setattr(main, "check_stop", check_stop)
    with pytest.raises(main.SubsystemInterrupted):
        main._update_uvb_dose_core("F2", strict=True, memo_port=port)
    assert len(port.writes) == 1
    assert len(audit) == 1
    assert audit[0]["outcome"] == main._LEDGER_SUBMITTED
    assert audit[0]["detail"].code == "f12_after_write"
    assert audit[0]["detail"].to_payload()["t"] == "reason"
    assert any("醫令" in title + msg or "後續" in title + msg
               for title, msg in fake_his)


@pytest.mark.parametrize(
    "stage,reason",
    [("write", "f12_during_write"),
     ("readback", "f12_during_readback"),
     ("after_readback", "f12_after_readback")],
)
def test_f12_after_memo_may_have_changed_keeps_unverified_audit(
        monkeypatch, fake_his, stage, reason):
    port = FakeMemoPort()
    audit = []
    monkeypatch.setattr(main, "_record_his_action",
                        lambda *_a, **fields: audit.append(fields))
    if stage == "write":
        original_write = port.write_memo

        def interrupted_write(memo_hwnd, text):
            original_write(memo_hwnd, text)
            raise main.SubsystemInterrupted("F12")

        monkeypatch.setattr(port, "write_memo", interrupted_write)
    elif stage == "readback":
        original_read = port.read_memo

        def interrupted_read(memo_hwnd):
            if port.writes:
                raise main.SubsystemInterrupted("F12")
            return original_read(memo_hwnd)

        monkeypatch.setattr(port, "read_memo", interrupted_read)
    else:
        original_read = port.read_memo
        readback_finished = False

        def read_then_stop(memo_hwnd):
            nonlocal readback_finished
            text = original_read(memo_hwnd)
            if port.writes:
                readback_finished = True
            return text

        def check_stop():
            if readback_finished:
                raise main.SubsystemInterrupted("F12")

        monkeypatch.setattr(port, "read_memo", read_then_stop)
        monkeypatch.setattr(main, "check_stop", check_stop)

    with pytest.raises(main.SubsystemInterrupted):
        main._update_uvb_dose_core("F2", strict=True, memo_port=port)
    assert len(port.writes) == 1
    assert len(audit) == 1
    assert audit[0]["outcome"] == main._LEDGER_SUBMITTED
    assert audit[0]["detail"].to_payload() == {"t": "reason", "code": reason}


def test_false_write_result_is_unknown_and_never_retried(monkeypatch, fake_his):
    class AppliedButTimedOut(FakeMemoPort):
        def write_memo(self, memo_hwnd, text):
            super().write_memo(memo_hwnd, text)
            return False

    port = AppliedButTimedOut()
    audit = []
    monkeypatch.setattr(main, "_record_his_action",
                        lambda *_a, **fields: audit.append(fields))
    assert main._update_uvb_dose_core("F2", strict=True, memo_port=port) is False
    assert len(port.writes) == 1
    assert audit[0]["detail"].to_payload() == {
        "t": "reason", "code": "settext_unconfirmed"}
    assert any("不明" in title + msg or "無法確認" in title + msg
               for title, msg in fake_his)
    assert not any("處置欄未更新" in title + msg for title, msg in fake_his)


@pytest.mark.parametrize("fail_stage", ["write", "readback"])
def test_uvb_io_exception_after_write_is_reported_unknown_without_retry(
        fake_his, fail_stage):
    class TimedOut(FakeMemoPort):
        def write_memo(self, memo_hwnd, text):
            super().write_memo(memo_hwnd, text)
            if fail_stage == "write":
                raise TimeoutError("synthetic HIS timeout")
            return True

        def read_memo(self, memo_hwnd):
            if fail_stage == "readback" and self.writes:
                raise TimeoutError("synthetic HIS timeout")
            return super().read_memo(memo_hwnd)

    port = TimedOut()
    assert main._update_uvb_dose_core("F2", strict=True, memo_port=port) is False
    assert len(port.writes) == 1
    assert any("無法驗證" in title + msg or "結果不明" in title + msg
               for title, msg in fake_his)


def test_repeating_same_day_does_not_write_second_dose(fake_his):
    port = FakeMemoPort()
    assert main._update_uvb_dose_core("F2", strict=True, memo_port=port) is True
    assert main._update_uvb_dose_core("F2", strict=True, memo_port=port) is False
    assert len(port.writes) == 1


def test_f12_after_confirmation_prevents_uvb_write(monkeypatch, fake_his):
    prior = date.today() - timedelta(days=40)
    port = FakeMemoPort(_memo().replace(
        (date.today() - timedelta(days=3)).strftime("%Y/%m/%d"),
        prior.strftime("%Y/%m/%d")))
    cancelled = False

    def confirm(*_args, **_kwargs):
        nonlocal cancelled
        cancelled = True
        return True

    def check_stop():
        if cancelled:
            raise main.SubsystemInterrupted("F12")

    monkeypatch.setattr(main, "_photo_confirm_yesno", confirm)
    monkeypatch.setattr(main, "check_stop", check_stop)
    with pytest.raises(main.SubsystemInterrupted):
        main._update_uvb_dose_core("F2", strict=True, memo_port=port)
    assert port.writes == []


def test_f1_reports_incomplete_after_code_when_uvb_outcome_unknown(
        monkeypatch, fake_his):
    port = FakeMemoPort()
    original_write = port.write_memo

    def unconfirmed_write(memo_hwnd, text):
        original_write(memo_hwnd, text)
        return False

    original_core = main._update_uvb_dose_core
    monkeypatch.setattr(port, "write_memo", unconfirmed_write)
    monkeypatch.setattr(main, "_f1_phototherapy_route", lambda **_k: "normal")
    monkeypatch.setattr(main, "_script_code_input_adaptive", lambda *a, **k: True)
    monkeypatch.setattr(
        main, "_update_uvb_dose_core",
        lambda label, *, strict, codes_already_placed="": original_core(
            label, strict=strict, codes_already_placed=codes_already_placed,
            memo_port=port))
    assert main.script_F1_adaptive() is False
    assert len(port.writes) == 1


def test_f1_without_uvb_remains_completed(monkeypatch, fake_his):
    monkeypatch.setattr(main, "_f1_phototherapy_route", lambda **_k: "normal")
    monkeypatch.setattr(main, "_script_code_input_adaptive", lambda *a, **k: True)
    monkeypatch.setattr(main, "_f1_update_uvb_dose_if_present",
                        lambda *_a, **_k: True)
    assert main.script_F1_adaptive() is True


def test_f1_found_memo_but_read_failed_is_not_silent_success(fake_his):
    port = FakeMemoPort(text="")
    assert main._update_uvb_dose_core("F1", strict=False,
                                      memo_port=port) is False
    assert port.writes == []
    assert any("讀取" in title + msg or "無法確認" in title + msg
               for title, msg in fake_his)


def test_f1_discovery_timeout_is_not_confirmed_absence(monkeypatch, fake_his):
    monkeypatch.setattr(main, "_collect_phototherapy_memos",
                        lambda _hwnd: ([], False))
    assert main._resolve_phototherapy_disposition(10) == (0, "unknown")
    port = FakeMemoPort()
    port.memo_hwnd = 0
    port.kind = "unknown"
    assert main._update_uvb_dose_core("F1", strict=False,
                                      memo_port=port) is False
    assert port.writes == []
    assert any("無法確認" in title + msg or "讀取失敗" in title + msg
               for title, msg in fake_his)


def test_f1_incomplete_discovery_never_places_billing_order(monkeypatch, fake_his):
    class FakeUser32:
        @staticmethod
        def GetClassNameW(_hwnd, buffer, _size):
            buffer.value = "TMemo"
            return 5

        @staticmethod
        def EnumChildWindows(_parent, callback, _data):
            callback(20, 0)
            return 1

    monkeypatch.setattr(main, "_find_hospital_main_window", lambda: 10)
    monkeypatch.setattr(
        main, "ctypes",
        SimpleNamespace(
            WINFUNCTYPE=lambda *_args: lambda callback: callback,
            create_unicode_buffer=ctypes.create_unicode_buffer,
            windll=SimpleNamespace(user32=FakeUser32())))
    monkeypatch.setattr(main, "_wm_gettext_timeout_ex",
                        lambda _hwnd: ("", False))
    monkeypatch.setattr(main, "_script_code_input_adaptive",
                        lambda *_a, **_k: pytest.fail("unknown route cannot place order"))
    assert main._collect_phototherapy_memos(10) == ([], False)
    assert main._f1_phototherapy_route(label="F1") == "unknown"
    assert main.script_F1_adaptive() is False
    assert any("未輸入醫令" in msg for _, msg in fake_his)


@pytest.mark.parametrize("failure", ["class_name", "enumeration"])
def test_f1_win32_discovery_failure_never_places_billing_order(
        monkeypatch, fake_his, failure):
    class FakeUser32:
        @staticmethod
        def GetClassNameW(_hwnd, _buffer, _size):
            return 0

        @staticmethod
        def EnumChildWindows(_parent, callback, _data):
            if failure == "class_name":
                callback(20, 0)
                return 1
            return 0

    monkeypatch.setattr(main, "_find_hospital_main_window", lambda: 10)
    monkeypatch.setattr(
        main, "ctypes",
        SimpleNamespace(
            WINFUNCTYPE=lambda *_args: lambda callback: callback,
            create_unicode_buffer=ctypes.create_unicode_buffer,
            windll=SimpleNamespace(user32=FakeUser32())))
    monkeypatch.setattr(main, "_script_code_input_adaptive",
                        lambda *_a, **_k: pytest.fail("uncertain scan cannot bill"))
    assert main._collect_phototherapy_memos(10) == ([], False)
    assert main._f1_phototherapy_route(label="F1") == "unknown"
    assert main.script_F1_adaptive() is False
    assert any("未輸入醫令" in msg for _, msg in fake_his)


def test_f1_missing_main_window_never_places_billing_order(monkeypatch, fake_his):
    monkeypatch.setattr(main, "_find_hospital_main_window", lambda: 0)
    monkeypatch.setattr(main, "_script_code_input_adaptive",
                        lambda *_a, **_k: pytest.fail("missing HIS cannot place order"))
    assert main._f1_phototherapy_route(label="F1") == "unknown"
    assert main.script_F1_adaptive() is False
    assert any("未輸入醫令" in msg for _, msg in fake_his)


def test_f1_route_preserves_f12_before_any_order(monkeypatch, fake_his):
    def cancelled():
        raise main.SubsystemInterrupted("F12")

    monkeypatch.setattr(main, "_find_hospital_main_window", cancelled)
    monkeypatch.setattr(main, "_script_code_input_adaptive",
                        lambda *_a, **_k: pytest.fail("F12 cannot place order"))
    with pytest.raises(main.SubsystemInterrupted):
        main.script_F1_adaptive()


def test_f1_confirmed_absence_keeps_first_session_order(monkeypatch, fake_his):
    orders = []
    monkeypatch.setattr(main, "_find_hospital_main_window", lambda: 10)
    monkeypatch.setattr(main, "_collect_phototherapy_memos",
                        lambda _hwnd: ([], True))
    monkeypatch.setattr(main, "_script_code_input_adaptive",
                        lambda code, **_k: orders.append(code) or True)
    monkeypatch.setattr(main, "_f1_update_uvb_dose_if_present",
                        lambda **_k: True)
    assert main._f1_phototherapy_route(label="F1") == "normal"
    assert main.script_F1_adaptive() is True
    assert orders == ["51019"]


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_readback_accepts_line_endings_but_protects_unchanged_lines(newline):
    original = "UVB: 800 (35)" + newline + "Pathology report: synthetic"
    proposed = "UVB: 850 (36)" + newline + "Pathology report: synthetic"
    actual = "UVB: 850 (36)\r\nPathology report: synthetic"
    assert main.memo_written_back_intact(original, proposed, actual)
    assert not main.memo_written_back_intact(
        original, proposed, actual.replace("synthetic", "changed"))


def test_edited_line_whitespace_must_not_split_dose_or_count_tokens():
    original = "Excimer: 800 mj/cm2 (35)\nPathology report: synthetic"
    proposed = "Excimer: 850 mj/cm2 (36)\nPathology report: synthetic"
    formatted = "EXCIMER : 850mj / cm2 ( 36 )\nPathology report: synthetic"
    corrupted = "Excimer: 8 50 mj/cm2 (3 6)\nPathology report: synthetic"
    assert main.memo_written_back_intact(original, proposed, formatted)
    assert not main.memo_written_back_intact(original, proposed, corrupted)


def test_edited_line_readback_does_not_split_decimal_tokens():
    original = "UVB: 1.0 mj/cm2\nPathology report: synthetic"
    proposed = "UVB: 1.5 mj/cm2\nPathology report: synthetic"
    actual = "UVB: 1 . 5 mj/cm2\nPathology report: synthetic"
    assert not main.memo_written_back_intact(original, proposed, actual)


@pytest.mark.parametrize("owner_valid,fail_owned_call,expected_owners", [
    (False, False, [0]),
    (True, True, [10, 0]),
    (True, False, [10]),
])
def test_warning_survives_destroyed_owner_or_owner_race(
        monkeypatch, owner_valid, fail_owned_call, expected_owners):
    owners = []

    class FakeUser32:
        def IsWindow(self, hwnd):
            assert hwnd == 10
            return owner_valid

        def FlashWindowEx(self, _info):
            return True

        def MessageBoxW(self, hwnd, _msg, _title, _flags):
            owners.append(hwnd)
            return int(hwnd == 0 or not fail_owned_call)

    fake_ctypes = SimpleNamespace(
        Structure=ctypes.Structure,
        c_uint=ctypes.c_uint,
        sizeof=ctypes.sizeof,
        byref=ctypes.byref,
        windll=SimpleNamespace(user32=FakeUser32()),
    )
    monkeypatch.setattr(main, "ctypes", fake_ctypes)
    monkeypatch.setattr(main, "_hotkey_awaiting_user_scope", nullcontext)
    monkeypatch.setitem(sys.modules, "winsound",
                        SimpleNamespace(MessageBeep=lambda _flags: None))
    main._show_uvb_warning(10, "合成警告", "請人工核對")
    assert owners == expected_owners


def test_excimer_split_digit_readback_is_not_reported_verified(
        monkeypatch, fake_his):
    port = FakeMemoPort(_memo(kind="Excimer"))
    port.kind = "pure_excimer"
    original_write = port.write_memo
    audit = []
    monkeypatch.setattr(main, "_record_his_action",
                        lambda *_a, **fields: audit.append(fields))

    def malformed_readback(memo_hwnd, text):
        original_write(memo_hwnd, text)
        port.text = port.text.replace("850", "8 50").replace("(36)", "(3 6)")
        return True

    monkeypatch.setattr(port, "write_memo", malformed_readback)
    assert main._f23_pure_excimer_update(
        10, 20, port.text, label="F2", memo_port=port) == (
            main._F23_PURE_EXCIMER_UNVERIFIED)
    assert len(audit) == 1
    assert audit[0]["outcome"] == main._LEDGER_MISMATCH


def test_uvb_and_local_uvb_both_update_without_touching_other_history(fake_his):
    prior = date.today() - timedelta(days=3)
    second = (f"局部 UVB: 600 mj/cm2 (7) on ({prior:%Y/%m/%d}) "
              "add 30 mj/cm2, MAX: 1000 mj/cm2")
    port = FakeMemoPort(_memo().replace(
        "Pathology report:", second + "\nPathology report:"))
    assert main._update_uvb_dose_core("F2", strict=True, memo_port=port) is True
    assert len(port.writes) == 1
    assert "UVB: 850 mj/cm2 (36)" in port.text
    assert "局部 UVB: 630 mj/cm2 (8)" in port.text
    assert port.text.endswith(
        "Pathology report: synthetic note\nRemove stitches")


def test_excimer_target_changed_during_confirmation_aborts_without_write(
        monkeypatch, fake_his):
    prior = date.today() - timedelta(days=40)
    port = FakeMemoPort(_memo(kind="Excimer").replace(
        (date.today() - timedelta(days=3)).strftime("%Y/%m/%d"),
        prior.strftime("%Y/%m/%d")))
    port.kind = "pure_excimer"

    def confirm(*_args, **_kwargs):
        port.memo_hwnd = 21
        return True

    monkeypatch.setattr(main, "_photo_confirm_yesno", confirm)
    result = main._f23_pure_excimer_update(
        10, 20, port.text, label="F2", memo_port=port)
    assert result == main._F23_PURE_EXCIMER_ABORTED
    assert port.writes == []
    assert any("身份(自費 01)尚未自動設定" in msg for _, msg in fake_his)


def test_f1_excimer_stale_snapshot_warning_does_not_promise_identity(
        monkeypatch, fake_his):
    port = FakeMemoPort(_memo(kind="Excimer"))
    port.kind = "pure_excimer"
    original = port.text
    port.memo_hwnd = 21
    proposed = SimpleNamespace(
        new_text=original, new_dose=850, new_count=36, decrease_note="")
    assert main._write_excimer_memo_checked(
        10, 20, original, proposed, "F1", memo_port=port) is None
    assert port.writes == []
    assert any("F1 不自動設定身份 01" in msg for _, msg in fake_his)
    assert not any("身份(自費 01)尚未自動設定" in msg for _, msg in fake_his)


@pytest.mark.parametrize("readback_mismatch", [False, True])
def test_excimer_unverified_write_explains_intentionally_skipped_segment(
        monkeypatch, fake_his, readback_mismatch):
    port = FakeMemoPort(_memo(kind="Excimer"))
    port.kind = "pure_excimer"
    original = port.text
    original_write = port.write_memo

    def write(memo_hwnd, text):
        original_write(memo_hwnd, text)
        if readback_mismatch:
            port.text += "\nSynthetic HIS edit"
        return readback_mismatch

    monkeypatch.setattr(port, "write_memo", write)
    proposed = SimpleNamespace(
        new_text=original, new_dose=850, new_count=36,
        decrease_note="decrease 50")
    assert main._write_excimer_memo_checked(
        10, 20, original, proposed, "F2", memo_port=port) is False
    assert len(port.writes) == 1
    assert any("decrease 50" in msg and "寫回結果未確認" in msg
               for _, msg in fake_his)
    assert not any("其他段已更新" in msg for _, msg in fake_his)


def test_excimer_confirmation_then_same_snapshot_writes_once(
        monkeypatch, fake_his):
    prior = date.today() - timedelta(days=40)
    port = FakeMemoPort(_memo(kind="Excimer").replace(
        (date.today() - timedelta(days=3)).strftime("%Y/%m/%d"),
        prior.strftime("%Y/%m/%d")))
    port.kind = "pure_excimer"
    monkeypatch.setattr(main, "_photo_confirm_yesno", lambda *_a, **_k: True)
    result = main._f23_pure_excimer_update(
        10, 20, port.text, label="F2", memo_port=port)
    assert result == main._F23_PURE_EXCIMER
    assert len(port.writes) == 1


def test_noninjected_snapshot_scan_timeout_never_writes(
        monkeypatch, fake_his):
    original = _memo(kind="Excimer")
    proposed = SimpleNamespace(
        new_text=original, new_dose=850, new_count=36, decrease_note="")
    monkeypatch.setattr(main, "_find_hospital_main_window", lambda: 10)
    monkeypatch.setattr(main, "_collect_phototherapy_memos",
                        lambda _hwnd: ([], False))
    monkeypatch.setattr(main, "_write_tmemo_text",
                        lambda *_a: pytest.fail("unknown scan cannot write"))
    assert main._write_excimer_memo_checked(
        10, 20, original, proposed, "F2") is None
    assert any("無法確認" in title for title, _ in fake_his)


@pytest.mark.parametrize("hotkey", ["F2", "F3"])
def test_excimer_stale_target_aborts_before_identity(monkeypatch, fake_his, hotkey):
    monkeypatch.setattr(main, "_f23_update_uvb_dose",
                        lambda **_k: main._F23_PURE_EXCIMER_ABORTED)
    monkeypatch.setattr(main, "_set_身份_自費",
                        lambda *_a, **_k: pytest.fail("stale target cannot set identity"))
    monkeypatch.setattr(main, "_script_code_input_adaptive",
                        lambda *_a, **_k: pytest.fail("stale target cannot place order"))
    assert getattr(main, f"script_{hotkey}_adaptive")() is False


def test_excimer_readback_empty_keeps_billing_policy_but_warns(
        monkeypatch, fake_his):
    class EmptyAfterWrite(FakeMemoPort):
        def read_memo(self, memo_hwnd):
            if self.writes:
                return ""
            return super().read_memo(memo_hwnd)

    port = EmptyAfterWrite(_memo(kind="Excimer"))
    port.kind = "pure_excimer"
    audit = []
    monkeypatch.setattr(main, "_record_his_action",
                        lambda *_a, **fields: audit.append(fields))
    result = main._f23_pure_excimer_update(
        10, 20, port.text, label="F2", memo_port=port)
    assert result == main._F23_PURE_EXCIMER_UNVERIFIED
    assert len(port.writes) == 1
    assert len(audit) == 1
    assert audit[0]["outcome"] == main._LEDGER_SUBMITTED
    assert audit[0]["detail"].code == "readback_empty"
    assert any("無法驗證" in title + msg for title, msg in fake_his)


@pytest.mark.parametrize("hotkey", ["F2", "F3"])
def test_excimer_unverified_status_still_sets_identity_but_not_completed(
        monkeypatch, fake_his, hotkey):
    identity_calls = []
    monkeypatch.setattr(main, "_f23_update_uvb_dose",
                        lambda **_k: main._F23_PURE_EXCIMER_UNVERIFIED)
    monkeypatch.setattr(main, "_set_身份_自費",
                        lambda value, *, label: identity_calls.append((value, label)) or True)
    monkeypatch.setattr(main, "_script_code_input_adaptive",
                        lambda *_a, **_k: pytest.fail("51019 must not be placed"))
    result = getattr(main, f"script_{hotkey}_adaptive")()
    assert result is False
    assert identity_calls == [("01", hotkey)]


@pytest.mark.parametrize("hotkey", ["F2", "F3"])
def test_excimer_unparseable_dose_sets_identity_but_reports_incomplete(
        monkeypatch, fake_his, hotkey):
    port = FakeMemoPort("Excimer: synthetic malformed treatment line")
    port.kind = "pure_excimer"
    identity_calls = []
    monkeypatch.setattr(
        main, "_f23_update_uvb_dose",
        lambda **_k: main._f23_pure_excimer_update(
            10, 20, port.text, label=hotkey, memo_port=port))
    monkeypatch.setattr(
        main, "_set_身份_自費",
        lambda value, *, label: identity_calls.append((value, label)) or True)
    monkeypatch.setattr(
        main, "_script_code_input_adaptive",
        lambda *_a, **_k: pytest.fail("51019 must not be placed"))
    assert getattr(main, f"script_{hotkey}_adaptive")() is False
    assert port.writes == []
    assert identity_calls == [("01", hotkey)]
    assert any("未自動更新" in title + msg for title, msg in fake_his)


@pytest.mark.parametrize("hotkey", ["F2", "F3"])
def test_excimer_verified_dose_and_identity_reports_complete(
        monkeypatch, fake_his, hotkey):
    port = FakeMemoPort(_memo(kind="Excimer"))
    port.kind = "pure_excimer"
    identity_calls = []
    monkeypatch.setattr(
        main, "_f23_update_uvb_dose",
        lambda **_k: main._f23_pure_excimer_update(
            10, 20, port.text, label=hotkey, memo_port=port))
    monkeypatch.setattr(
        main, "_set_身份_自費",
        lambda value, *, label: identity_calls.append((value, label)) or True)
    monkeypatch.setattr(
        main, "_script_code_input_adaptive",
        lambda *_a, **_k: pytest.fail("51019 must not be placed"))
    assert getattr(main, f"script_{hotkey}_adaptive")() is True
    assert len(port.writes) == 1
    assert identity_calls == [("01", hotkey)]


@pytest.mark.parametrize("hotkey", ["F2", "F3"])
def test_excimer_declined_dose_confirmation_reports_incomplete(
        monkeypatch, fake_his, hotkey):
    prior = date.today() - timedelta(days=40)
    port = FakeMemoPort(_memo(kind="Excimer").replace(
        (date.today() - timedelta(days=3)).strftime("%Y/%m/%d"),
        prior.strftime("%Y/%m/%d")))
    port.kind = "pure_excimer"
    identity_calls = []
    monkeypatch.setattr(main, "_photo_confirm_yesno", lambda *_a, **_k: False)
    monkeypatch.setattr(
        main, "_f23_update_uvb_dose",
        lambda **_k: main._f23_pure_excimer_update(
            10, 20, port.text, label=hotkey, memo_port=port))
    monkeypatch.setattr(
        main, "_set_身份_自費",
        lambda value, *, label: identity_calls.append((value, label)) or True)
    assert getattr(main, f"script_{hotkey}_adaptive")() is False
    assert port.writes == []
    assert identity_calls == [("01", hotkey)]


@pytest.mark.parametrize("hotkey", ["F2", "F3"])
def test_repeated_phototherapy_hotkey_while_busy_does_not_start_second_write(
        hotkey):
    app = main.AutomationApp.__new__(main.AutomationApp)
    app._subsystem_lock = threading.RLock()
    app._subsystem_running = True
    app._subsystem_current_hotkey = hotkey
    app._restart_committing = False
    app._last_hotkey_busy_notice_at = 0.0
    app.ui_queue = Queue()
    app._show_notice = lambda *_a, **_k: None
    writes = []
    app.run_subsystem_in_thread(lambda: writes.append("second write"), hotkey,
                                preempt_same=False)
    assert writes == []
    assert "前一個熱鍵流程尚未完成" in app.ui_queue.get_nowait().text


@pytest.mark.parametrize("hotkey", ["F2", "F3"])
def test_excimer_identity_not_verified_is_not_reported_completed(
        monkeypatch, fake_his, hotkey):
    monkeypatch.setattr(main, "_f23_update_uvb_dose",
                        lambda **_k: main._F23_PURE_EXCIMER)
    monkeypatch.setattr(main, "_set_身份_自費", lambda *_a, **_k: False)
    assert getattr(main, f"script_{hotkey}_adaptive")() is False


def test_excimer_f12_during_write_does_not_claim_confirmed_result(
        monkeypatch, fake_his):
    port = FakeMemoPort(_memo(kind="Excimer"))
    port.kind = "pure_excimer"
    audit = []
    monkeypatch.setattr(main, "_record_his_action",
                        lambda *_a, **fields: audit.append(fields))
    cancelled = False
    original_write = port.write_memo

    def write(memo_hwnd, text):
        nonlocal cancelled
        result = original_write(memo_hwnd, text)
        cancelled = True
        return result

    def check_stop():
        if cancelled:
            raise main.SubsystemInterrupted("F12")

    monkeypatch.setattr(port, "write_memo", write)
    monkeypatch.setattr(main, "check_stop", check_stop)
    with pytest.raises(main.SubsystemInterrupted):
        main._f23_pure_excimer_update(
            10, 20, port.text, label="F2", memo_port=port)
    assert len(port.writes) == 1
    assert len(audit) == 1
    assert audit[0]["outcome"] == main._LEDGER_SUBMITTED
    assert audit[0]["detail"].code == "f12_after_write"
    assert any("取消" in title + msg or "中止" in title + msg
               for title, msg in fake_his)


def test_excimer_interrupted_write_keeps_unverified_audit(
        monkeypatch, fake_his):
    port = FakeMemoPort(_memo(kind="Excimer"))
    port.kind = "pure_excimer"
    audit = []
    monkeypatch.setattr(main, "_record_his_action",
                        lambda *_a, **fields: audit.append(fields))
    original_write = port.write_memo

    def interrupted_write(memo_hwnd, text):
        original_write(memo_hwnd, text)
        raise main.SubsystemInterrupted("F12")

    monkeypatch.setattr(port, "write_memo", interrupted_write)
    with pytest.raises(main.SubsystemInterrupted):
        main._f23_pure_excimer_update(
            10, 20, port.text, label="F2", memo_port=port)
    assert len(port.writes) == 1
    assert len(audit) == 1
    assert audit[0]["outcome"] == main._LEDGER_SUBMITTED
    assert audit[0]["detail"].code == "f12_during_write"


@pytest.mark.parametrize(
    "stage,reason",
    [("settext", "settext_unconfirmed"),
     ("readback", "f12_during_readback"),
     ("after_readback", "f12_after_readback")],
)
def test_excimer_unconfirmed_memo_states_have_audit(
        monkeypatch, fake_his, stage, reason):
    port = FakeMemoPort(_memo(kind="Excimer"))
    port.kind = "pure_excimer"
    audit = []
    monkeypatch.setattr(main, "_record_his_action",
                        lambda *_a, **fields: audit.append(fields))
    if stage == "settext":
        original_write = port.write_memo

        def unconfirmed_write(memo_hwnd, text):
            original_write(memo_hwnd, text)
            return False

        monkeypatch.setattr(port, "write_memo", unconfirmed_write)
    elif stage == "readback":
        original_read = port.read_memo

        def interrupted_read(memo_hwnd):
            if port.writes:
                raise main.SubsystemInterrupted("F12")
            return original_read(memo_hwnd)

        monkeypatch.setattr(port, "read_memo", interrupted_read)
    else:
        original_read = port.read_memo
        readback_finished = False

        def read_then_stop(memo_hwnd):
            nonlocal readback_finished
            text = original_read(memo_hwnd)
            if port.writes:
                readback_finished = True
            return text

        def check_stop():
            if readback_finished:
                raise main.SubsystemInterrupted("F12")

        monkeypatch.setattr(port, "read_memo", read_then_stop)
        monkeypatch.setattr(main, "check_stop", check_stop)

    if stage == "settext":
        assert main._f23_pure_excimer_update(
            10, 20, port.text, label="F2", memo_port=port
        ) == main._F23_PURE_EXCIMER_UNVERIFIED
    else:
        with pytest.raises(main.SubsystemInterrupted):
            main._f23_pure_excimer_update(
                10, 20, port.text, label="F2", memo_port=port)
    assert len(port.writes) == 1
    assert len(audit) == 1
    assert audit[0]["outcome"] == main._LEDGER_SUBMITTED
    assert audit[0]["detail"].to_payload() == {"t": "reason", "code": reason}
