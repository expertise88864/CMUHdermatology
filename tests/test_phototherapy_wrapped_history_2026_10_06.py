"""Dated Excimer history wrapped by HIS must not conflict with current UVB."""
from datetime import date
import subprocess
import sys

import pytest

from cmuh_common import uvb_dose as u

TODAY = date(2026, 10, 6)
UVB = "全身 UVB:750 mj/cm2(35) on (2026/10/04) add 40 each time, fixed at 800 first,"
EXCIMER = "excimer light 890 mj/cm2 for upper lip (24)"
OLD_TAIL = "(2024/12/09), add 50 each time, fixed at 950 -> hold"


def test_repeated_unknown_metadata_does_not_stall_classification():
    # A separate owned process bounds a regression in regex backtracking;
    # this exercises the pure parser, never the actual HIS application.
    code = (
        "import sys; from pathlib import Path; "
        f"sys.path.insert(0, str(Path({u.__file__!r}).parents[1])); "
        "from datetime import date; from cmuh_common import uvb_dose as u; "
        "text='excimer light 890 mj/cm2 (24) on\\n(2024/12/09)'"
        "+ (' MAX:950 ' * 20) + 'unrecognized'; "
        "assert u.detect_phototherapy_kind(text, date(2026,10,6)) == 'pure_excimer'"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=5)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
@pytest.mark.parametrize("placement", ["on_before_wrap", "on_after_wrap"])
def test_old_excimer_date_continuation_does_not_conflict_with_current_uvb(newline, placement):
    text = (EXCIMER + " on" + newline + OLD_TAIL if placement == "on_before_wrap"
            else EXCIMER + newline + "on " + OLD_TAIL)
    assert u.detect_phototherapy_kind(text, TODAY) == "none"
    assert u.combine_phototherapy_kinds([
        u.detect_phototherapy_kind(UVB, TODAY), u.detect_phototherapy_kind(text, TODAY),
    ]) == "uvb"


@pytest.mark.parametrize("tail", [
    "on (2026/10/04), add 50 each time, MAX:950",
    "on (2026/08/06), add 50 each time, MAX:950",  # two calendar months is retained
    "on (2026/10/07), add 50 each time, MAX:950",  # future is not stale
    "on (2024/13/40), add 50 each time, MAX:950",
    "on (0050/12/09), add 50 each time, MAX:950",
    "add 50 each time, MAX:950",  # genuinely undated
    "OMP on (2024/12/09)",  # another treatment's date is not borrowed
])
def test_recent_or_unconfirmed_continuation_remains_excimer(tail):
    assert u.detect_phototherapy_kind(EXCIMER + "\n" + tail, TODAY) == "pure_excimer"


@pytest.mark.parametrize("extra", [
    "on (2026/10/04)",
    "restart on (2026/10/04)",
    "restart on (2026/13/40)",
    "-> restart",
    "continue 890",
    "800 mj/cm2, add 30 each time, MAX:950",
    "excimer 800 mj/cm2, add 30 each time, MAX:950",
    "510 mj/cm2",
    "x2 week",
    "re-commence",
    "increase 30",
    "max 800",
    "BIW",
    "TIW",
    "unrecognized subsequent order",
    "OMP 1#; continue 510 mj/cm2",
    "nail clipping; resume 510 mj/cm2",
    "OMP W12 on (2024/12/09), unrecognized subsequent order",
])
def test_later_restart_or_undated_order_is_not_discarded_with_wrapped_history(extra):
    text = EXCIMER + " on\n" + OLD_TAIL + "\n" + extra
    assert u.detect_phototherapy_kind(text, TODAY) == "pure_excimer"


@pytest.mark.parametrize("restart", [" -> restart", " -> restart 500 mj/cm2", ", resume", " -> re-commence"])
def test_restart_on_the_date_continuation_is_not_discarded(restart):
    text = EXCIMER + " on\n" + OLD_TAIL + restart
    assert u.detect_phototherapy_kind(text, TODAY) == "pure_excimer"
    assert u.combine_phototherapy_kinds([
        u.detect_phototherapy_kind(UVB, TODAY), u.detect_phototherapy_kind(text, TODAY),
    ]) == "ambiguous"


@pytest.mark.parametrize("order", ["; continue 510 mj/cm2", "; BIW", "; unrecognized subsequent order"])
def test_unknown_suffix_on_old_date_continuation_keeps_excimer(order):
    text = EXCIMER + " on\n(2024/12/09), add 50 each time, fixed at 950" + order
    assert u.detect_phototherapy_kind(text, TODAY) == "pure_excimer"


def test_blank_line_does_not_connect_an_undated_excimer_to_a_date():
    assert u.detect_phototherapy_kind(EXCIMER + " on\n\non " + OLD_TAIL, TODAY) == "pure_excimer"


def test_another_phototherapy_line_does_not_supply_excimer_date():
    text = EXCIMER + " on\non (2024/12/09); UVB 500 mj/cm2"
    assert u.detect_phototherapy_kind(text, TODAY) == "pure_excimer"


def test_other_medication_on_old_date_line_is_not_borrowed():
    text = EXCIMER + " on\n(2024/12/09) OMP W12 on (2024/12/09)"
    assert u.detect_phototherapy_kind(text, TODAY) == "pure_excimer"


@pytest.mark.parametrize("separator", ["\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"])
@pytest.mark.parametrize("physical_lines", [False, True])
def test_non_his_line_separator_cannot_hide_an_unknown_restart(separator, physical_lines):
    gap = "\n" + separator * 2 + "\n" if physical_lines else separator * 2
    text = EXCIMER + " on\n(2024/12/09), add 50 each time" + gap + "restart 500 mj/cm2 BIW"
    assert u.detect_phototherapy_kind(text, TODAY) == "pure_excimer"


def test_invalid_date_in_excimer_header_is_not_replaced_by_an_old_continuation():
    text = EXCIMER + " on (2026/13/40) on\n" + OLD_TAIL
    assert u.detect_phototherapy_kind(text, TODAY) == "pure_excimer"


def _history(date_layout):
    old = EXCIMER + (" on\r\n" if date_layout == "wrapped" else " on ") + OLD_TAIL
    return (old + "\r\neducate sun-burn reaction and sun-screen usage,\r\n"
            "OMP W12 on (2023/11/3), education side effect -> re-OMP M3 on (2024/11/14)"
            " -> hold -> re-OMP W12 on (2026/7/30)\r\n"
            "nail clipping from left great toenail for nail pathy (r/o tinea unguium),"
            " OMP since , education side effects")


@pytest.mark.parametrize("date_layout", ["inline", "wrapped"])
def test_new_uvb_dose_update_preserves_old_history_and_other_notes(date_layout):
    suffix = "\r\n" + _history(date_layout)
    result = u.update_uvb_in_text(UVB + suffix, TODAY)
    assert result.action == u.UvbAction.UPDATED
    assert result.new_dose == 790
    assert result.new_count == 36
    assert result.new_text.endswith(suffix)


@pytest.mark.parametrize("label", ["F1", "F2", "F3"])
@pytest.mark.parametrize("date_layout", ["inline", "wrapped"])
@pytest.mark.parametrize("fields", ["same", "separate"])
@pytest.mark.parametrize("omp_date", ["2026/7/30", "2026/9/15"])
def test_main_resolver_and_write_core_select_only_current_uvb(monkeypatch, label, date_layout, fields, omp_date):
    import main

    class FixedDate(date):
        @classmethod
        def today(cls):
            return TODAY

    old = _history(date_layout).replace("2026/7/30", omp_date)
    memos = {20: UVB + "\r\n" + old} if fields == "same" else {20: UVB, 30: old}
    writes = []
    monkeypatch.setattr(main, "date", FixedDate)
    monkeypatch.setattr(u, "date", FixedDate)
    monkeypatch.setattr(main, "_collect_phototherapy_memos", lambda _hwnd: (list(memos.items()), True))
    monkeypatch.setattr(main, "check_stop", lambda: None)
    monkeypatch.setattr(main, "_record_his_action", lambda *a, **k: None)
    monkeypatch.setattr(main, "_show_uvb_warning", lambda *a, **k: pytest.fail("valid UVB route must not warn"))
    monkeypatch.setattr(main, "_find_hospital_main_window", lambda: 10)

    class MemoPort:
        def find_main_window(self):
            return 10

        def locate_phototherapy(self, hwnd):
            return main._resolve_phototherapy_disposition(hwnd)

        def read_memo(self, hwnd):
            return memos[hwnd]

        def write_memo(self, hwnd, text):
            writes.append((hwnd, text))
            memos[hwnd] = text
            return True

    assert main._resolve_phototherapy_disposition(10) == (20, "uvb")
    if label == "F1":
        assert main._f1_phototherapy_route(label="F1") == "normal"
    assert main._update_uvb_dose_core(label, strict=label != "F1", memo_port=MemoPort()) is True
    assert len(writes) == 1 and writes[0][0] == 20
    assert "790 mj/cm2(36)" in memos[20]
    if fields == "same":
        assert memos[20].endswith("\r\n" + old)
    else:
        assert memos[30] == old
