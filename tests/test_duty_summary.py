# -*- coding: utf-8 -*-
"""duty_summary helpers."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.duty_summary import (  # noqa: E402
    build_duty_summary_parts,
    split_duty_prefix_name,
    split_duty_vs_label_name,
)


def test_split_duty_prefix_name():
    assert split_duty_prefix_name("今日(05/25 週一) 值班: 王小明") == (
        "今日(05/25 週一) 值班:",
        "王小明",
    )
    assert split_duty_prefix_name("今日值班: ...") == ("今日值班: ...", "")
    assert split_duty_prefix_name(None) == ("", "")


def test_split_duty_vs_label_name():
    assert split_duty_vs_label_name("當日值班VS: 陳醫師") == ("當日值班VS:", "陳醫師")
    assert split_duty_vs_label_name("Custom: Name") == ("Custom:", "Name")
    assert split_duty_vs_label_name("No colon") == ("No colon", "")


def test_build_duty_summary_parts():
    parts = build_duty_summary_parts(
        "今日(05/25 週一) 值班: A",
        "當日值班VS: B",
        "當週(05/30 週六) 值班: C",
        "當週值班VS: D",
    )

    assert parts == {
        "row1_prefix": "今日(05/25 週一) 值班:",
        "row1_name": "A",
        "row1_vs_label": "當日值班VS:",
        "row1_vs_name": "B",
        "row2_prefix": "當週(05/30 週六) 值班:",
        "row2_name": "C",
        "row2_vs_label": "當週值班VS:",
        "row2_vs_name": "D",
    }
