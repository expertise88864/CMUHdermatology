# -*- coding: utf-8 -*-
"""Duty summary text helpers shared by Tk entry points."""
from __future__ import annotations


def split_duty_prefix_name(full, sep: str = " 值班:") -> tuple[str, str]:
    """Split labels like '今日 值班: Dr A' into prefix and name."""
    text = "" if full is None else str(full)
    if sep in text:
        idx = text.index(sep)
        return text[: idx + len(sep)], text[idx + len(sep):].strip()
    return text, ""


def split_duty_vs_label_name(full) -> tuple[str, str]:
    """Split VS labels while keeping the display label colon."""
    text = "" if full is None else str(full)
    for label in ("當日值班VS:", "當週值班VS:"):
        if text.startswith(label):
            return label, text[len(label):].strip()
    if ":" in text:
        label, name = text.split(":", 1)
        return label.strip() + ":", name.strip()
    return text, ""


def build_duty_summary_parts(
    duty_doctor_text,
    duty_vs_text,
    saturday_duty_text,
    saturday_vs_text,
) -> dict[str, str]:
    """Return the eight text pieces consumed by the summary widgets."""
    row1_prefix, row1_name = split_duty_prefix_name(duty_doctor_text)
    row1_vs_label, row1_vs_name = split_duty_vs_label_name(duty_vs_text)
    row2_prefix, row2_name = split_duty_prefix_name(saturday_duty_text)
    row2_vs_label, row2_vs_name = split_duty_vs_label_name(saturday_vs_text)
    return {
        "row1_prefix": row1_prefix,
        "row1_name": row1_name,
        "row1_vs_label": row1_vs_label,
        "row1_vs_name": row1_vs_name,
        "row2_prefix": row2_prefix,
        "row2_name": row2_name,
        "row2_vs_label": row2_vs_label,
        "row2_vs_name": row2_vs_name,
    }
