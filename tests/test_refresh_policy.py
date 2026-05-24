# -*- coding: utf-8 -*-
"""refresh_policy helpers."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.refresh_policy import partition_doctors_for_refresh_batches  # noqa: E402


def test_partition_doctors_for_refresh_batches_orders_priority_names():
    doctors = [
        {"name": "C", "doc_no": "3"},
        {"name": "A", "doc_no": "1"},
        {"name": "B", "doc_no": "2"},
        {"name": "D", "doc_no": "4"},
    ]

    batches = partition_doctors_for_refresh_batches(
        doctors,
        first_batch_names=("B", "A"),
        second_batch_names=("D",),
    )

    assert batches == [
        [{"name": "B", "doc_no": "2"}, {"name": "A", "doc_no": "1"}],
        [{"name": "D", "doc_no": "4"}],
        [{"name": "C", "doc_no": "3"}],
    ]


def test_partition_doctors_for_refresh_batches_skips_bad_rows():
    doctors = [
        {"doc_no": "bad"},
        "bad",
        {"name": "A", "doc_no": "1"},
        {"name": "C", "doc_no": "3"},
    ]

    batches = partition_doctors_for_refresh_batches(
        doctors,
        first_batch_names=("A", "B"),
        second_batch_names=("D",),
    )

    assert batches == [[{"name": "A", "doc_no": "1"}], [{"name": "C", "doc_no": "3"}]]
