# -*- coding: utf-8 -*-
"""UVB批次2 回歸測試（§3E：UC-02/05/06，2026-07-10）。

  UC-02 日期「存在但解析不出」被當無日期 → silent first-time 繞過 TOO_CLOSE/decay/stale
        全部間隔防線（昨天照過也照樣 +increase）。裸民國 115/7/9、點分隔 2026.7.9、
        7-8 位病歷號/連寫日期都屬此類。
  UC-05 v20.15 segment 擴到行首後，清單編號「(1)」被當次數 → 編號被 +1、真次數不動。
  UC-06 first-time 路徑跳過所有 sanity → add 500 一次爆量、(20260708) 被當次數 +1。
"""
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.uvb_dose import update_uvb_in_text  # noqa: E402

TODAY = date(2026, 7, 10)


def _run(text):
    r = update_uvb_in_text(text, today=TODAY)
    return r, (getattr(r, "new_text", None) or "")


# ── UC-02 日期存在但解析不出 → 不得走 first-time +increase ─────────────────────
def test_uc02_bare_roc_date_not_first_time():
    # 昨天(民國 115/7/9)照過，裸民國日期解析不出 → 絕不可 +30 當初診
    text = "UVB 500 mj/cm2 (10) on 115/7/9, add 30, MAX: 800"
    r, nt = _run(text)
    assert r.action != "updated", f"裸民國日期被當無日期 first-time（UC-02）：{r.action} {nt!r}"
    assert "530" not in nt


def test_uc02_dotted_date_not_first_time():
    text = "UVB 500 mj/cm2 (10) on (2026.7.9), add 30, MAX: 800"
    r, nt = _run(text)
    assert r.action != "updated", f"點分隔日期被當無日期 first-time（UC-02）：{r.action} {nt!r}"
    assert "530" not in nt


def test_uc02_eight_digit_concat_not_first_time():
    # (20260708) 8 位連寫日期，原本被當 count → 寫回 (20260709)
    text = "UVB 500 mj/cm2 (20260708) add 30, MAX: 800"
    r, nt = _run(text)
    assert r.action != "updated", f"8 位連寫被當無日期 first-time（UC-02）：{r.action} {nt!r}"
    assert "20260709" not in nt, f"日期樣數字被 +1（UC-02）：{nt!r}"


def test_uc02_finditer_uses_real_date_over_chart_number():
    # 7 位病歷號 (1234567) 在前、真日期 (2026/7/9) 在後 → finditer 應跳過病歷號用真日期,
    # 走 dated 路徑;不得誤當無日期 first-time +increase。
    text = "UVB 500 mj/cm2 (1234567) chart, (10) on (2026/7/9) add 30, MAX: 800"
    r, nt = _run(text)
    assert "530" not in nt or r.action != "updated", \
        f"病歷號讓真日期被跳過、走 first-time（UC-02）：{r.action} {nt!r}"


# ── UC-05 行首編號不得被當次數 ─────────────────────────────────────────────────
def test_uc05_leading_list_number_not_treated_as_count():
    text = "(1) UVB: 500 mj/cm2 (25) on (2026/7/8) add 30, MAX: 800"
    r, nt = _run(text)
    assert r.action == "updated", f"未更新：{r.action} {nt!r}"
    assert "(26)" in nt, f"真次數 25→26 未更新（UC-05）：{nt!r}"
    assert "(2) UVB" not in nt, f"行首編號 (1) 被當次數 +1 成 (2)（UC-05）：{nt!r}"


# ── UC-06 first-time sanity ───────────────────────────────────────────────────
def test_uc06_first_time_rejects_huge_increase():
    # add 500 > 200/次上限 → first-time 不得靜默 +500 寫回 1000
    text = "UVB 500 mj/cm2 (5) add 500, MAX: 1400"
    r, nt = _run(text)
    assert r.action != "updated", f"first-time +500 爆量（UC-06）：{r.action} {nt!r}"
    assert "1000" not in nt


def test_uc06_first_time_normal_still_updates():
    # 真正無日期、增量正常的 first-time 仍要 UPDATED（確認 UC-06 sanity 沒過度收緊）
    text = "UVB 510 mj/cm2 increase 30, max 800"
    r, nt = _run(text)
    assert r.action == "updated" and "540" in nt, f"正常 first-time 未更新：{r.action} {nt!r}"


# ── codex P1：post-dose 日期壞 + 之前有不相關 since 日期 → 安全 fail，不用 since 日期 ──
def test_uc02_malformed_post_dose_date_does_not_fall_back_to_since():
    # on (2026/2/30) 二月卅日無效;之前的 since (2026/1/1) 是起始日、不是上次照光日。
    # 不可退回用 since 日期算時序 → 安全 parse_fail 交醫師。
    text = "since (2026/1/1) UVB 500 mj/cm2 (10) add 30 on (2026/2/30), MAX 800"
    r, nt = _run(text)
    assert r.action != "updated", f"用了不相關的 since 起始日算時序（codex P1）：{r.action} {nt!r}"


# ── codex P1-2：不支援格式的 post-dose 日期(點分隔) → 仍不可退回 since 日期 ─────────
def test_uc02_unsupported_post_dose_date_still_blocks_since_fallback():
    text = "since (2026/1/1) UVB 500 mj/cm2 (10) add 30 on (2026.7.9), MAX 800"
    r, nt = _run(text)
    assert r.action != "updated", f"點分隔 post-dose 日期讓 since 被誤用（codex P1-2）：{r.action} {nt!r}"


# ── codex P2-2：血壓等量測值(BP 120/80/70)不得被當日期而誤 PARSE_FAIL ─────────────
def test_uc02_bp_measurement_not_treated_as_date():
    text = "UVB 510 mj/cm2 increase 30, max 800, BP 120/80/70"
    r, nt = _run(text)
    assert r.action == "updated" and "540" in nt, \
        f"BP 120/80/70 被當日期、誤擋正常 first-time（codex P2-2）：{r.action} {nt!r}"


def test_uc02_lab_slash_values_not_treated_as_date():
    # 年 500 非合理年(西元 1900-2099 或民國 100-199 之外) → 不當日期,正常 first-time
    text = "UVB 510 mj/cm2 increase 30, max 800, lab 500/10/20"
    r, nt = _run(text)
    assert r.action == "updated" and "540" in nt, \
        f"lab 500/10/20 被當日期、誤擋正常 first-time（codex P2-3）：{r.action} {nt!r}"


# ── codex P2：count 值==行首編號值時，只改真次數不改行首編號 ────────────────────
def test_uc05_marker_equals_count_value_only_updates_real_count():
    text = "(1) UVB: 500 mj/cm2 (1) on (2026/7/8) add 30, MAX: 800"
    r, nt = _run(text)
    assert r.action == "updated", f"未更新：{r.action} {nt!r}"
    assert nt.startswith("(1) UVB"), f"行首清單編號 (1) 被誤改（codex P2）：{nt!r}"
    assert "(2) on" in nt, f"真次數 (1)→(2) 未更新（codex P2）：{nt!r}"


# ── 反向：正常 dated 更新仍正確 ────────────────────────────────────────────────
def test_batch2_normal_dated_still_updates():
    text = "UVB 500 mj/cm2 (10) on (2026/7/8) add 30, MAX: 800"
    r, nt = _run(text)
    assert r.action == "updated" and "530 mj/cm2 (11)" in nt, \
        f"正常 dated 更新失效：{r.action} {nt!r}"
