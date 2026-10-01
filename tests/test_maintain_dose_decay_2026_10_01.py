"""Physician rule: maintain prevents increases, never cancels interval reductions."""
from datetime import date, timedelta
import re

import pytest

from cmuh_common.uvb_dose import UvbAction, update_uvb_in_text


@pytest.mark.parametrize("days,expected,action", [
    (0, None, UvbAction.TOO_CLOSE),
    (1, None, UvbAction.TOO_CLOSE),
    (2, 1000, UvbAction.UPDATED),
    (7, 1000, UvbAction.UPDATED),
    (8, 750, UvbAction.UPDATED),
    (9, 750, UvbAction.UPDATED),
    (14, 750, UvbAction.UPDATED),
    (15, 500, UvbAction.UPDATED),
    (21, 500, UvbAction.UPDATED),
    (22, 250, UvbAction.UPDATED),
])
@pytest.mark.parametrize("layout", ["uvb", "uvb_additional", "excimer", "excimer_segments"])
def test_maintain_cannot_override_interval_decay(days, expected, action, layout):
    today = date(2026, 10, 1)
    last = today - timedelta(days=days)
    uvb = (f"已打折 UVB: 1000 mj/cm2 (406) on ({last:%Y/%m/%d}) "
           "maintain the dose, fixed at 1000")
    excimer = (f"Excimer light: 1000 mj/cm2 (406) on ({last:%Y/%m/%d}) "
               "maintain the dose, fixed at 1000")
    text = {
        "uvb": uvb,
        "uvb_additional": uvb + "\n局部 " + uvb,
        "excimer": excimer,
        "excimer_segments": excimer + ", " + excimer,
    }[layout]
    text += "\nOMP since (2022/8/25); synthetic unrelated medication history"
    result = update_uvb_in_text(text, today=today)
    assert result.action == action
    if expected is None:
        assert result.new_text is None
        return
    assert result.new_dose == expected
    assert result.new_count == 407
    dose_counts = re.findall(r"(?:UVB|Excimer light):\s*(\d+)\s*mj/cm2\s*\((\d+)\)",
                             result.new_text)
    total = 2 if layout in ("uvb_additional", "excimer_segments") else 1
    assert dose_counts == [(str(expected), "407")] * total
    assert result.new_text.count("on (2026/10/01)") == total
    assert "OMP since (2022/8/25); synthetic unrelated medication history" in result.new_text


def test_reported_uvb_prefix_does_not_cancel_nine_day_reduction():
    text = (
        "已打折 UVB: 1000 mj/cm2 (406) on (2026/09/22) maintain the dose, "
        "fixed at 1000, self, take picture on 2022/8/25\n"
        "start topical ruxo on (2026/4/9)\n"
        "synthetic other treatment history"
    )
    result = update_uvb_in_text(text, today=date(2026, 10, 1))
    assert result.action == UvbAction.UPDATED
    assert result.days_diff == 9
    assert result.new_dose == 750
    assert result.new_count == 407
    assert result.new_text == text.replace("UVB: 1000", "UVB: 750").replace(
        "(406)", "(407)").replace("(2026/09/22)", "(2026/10/01)")
