"""Anonymous HIS memo races and read-back failures for F1–F3."""

from datetime import date, timedelta

import pytest

import main


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
    monkeypatch.setattr(port, "write_memo", lambda *_args: True)
    result = main._f23_pure_excimer_update(
        10, 20, text, label="F2", memo_port=port)
    # Existing billing policy still allows the caller to set identity 01.
    assert result == main._F23_PURE_EXCIMER_UNVERIFIED
    assert any("無法驗證" in title + msg or "驗證失敗" in title + msg
               for title, msg in fake_his)


def test_cancellation_during_uvb_write_stops_before_followup(
        monkeypatch, fake_his):
    port = FakeMemoPort()
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
    assert any("醫令" in title + msg or "後續" in title + msg
               for title, msg in fake_his)


def test_false_write_result_is_unknown_and_never_retried(fake_his):
    class AppliedButTimedOut(FakeMemoPort):
        def write_memo(self, memo_hwnd, text):
            super().write_memo(memo_hwnd, text)
            return False

    port = AppliedButTimedOut()
    assert main._update_uvb_dose_core("F2", strict=True, memo_port=port) is False
    assert len(port.writes) == 1
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
    monkeypatch.setattr(main, "_f1_phototherapy_route", lambda **_k: "normal")
    monkeypatch.setattr(main, "_script_code_input_adaptive", lambda *a, **k: True)
    monkeypatch.setattr(main, "_f1_update_uvb_dose_if_present",
                        lambda *_a, **_k: False)
    assert main.script_F1_adaptive() is False


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


def test_excimer_readback_empty_keeps_billing_policy_but_warns(fake_his):
    class EmptyAfterWrite(FakeMemoPort):
        def read_memo(self, memo_hwnd):
            if self.writes:
                return ""
            return super().read_memo(memo_hwnd)

    port = EmptyAfterWrite(_memo(kind="Excimer"))
    port.kind = "pure_excimer"
    result = main._f23_pure_excimer_update(
        10, 20, port.text, label="F2", memo_port=port)
    assert result == main._F23_PURE_EXCIMER_UNVERIFIED
    assert len(port.writes) == 1
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
    assert any("取消" in title + msg or "中止" in title + msg
               for title, msg in fake_his)
