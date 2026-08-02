# -*- coding: utf-8 -*-
"""批次 I（外部 review P1-06）：壞列與異常時間戳不可以永久繞過保留期。

【這一條為什麼是個資問題而不只是工程問題】
定位索引存的是病歷號，它存在的唯一理由是「有 30 天保留期的可追查紀錄」。
原本三個缺陷疊在一起，會讓一列紀錄【永久】留在磁碟上，而保留期報告說一切正常：

  1. `_read_rows` 對壞列直接 `continue` —— 不計數、也不會在重寫時被剔除。
  2. `if not rows: return NOTHING_TO_DO` —— 整份都是壞列時完全不重寫檔案。
  3. `_prune` 用 ISO 字串比大小 —— `"zzz" >= cutoff` 恆為真。
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timedelta

import pytest

from cmuh_common import patient_locator as pl


def _write(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _row(ts, chart_no="24994923"):
    return json.dumps({"ts": ts, "action": "測", "chart_no": chart_no},
                      ensure_ascii=False)


NOW = datetime(2026, 8, 2, 12, 0, 0)


def _iso(days_ago):
    return (NOW - timedelta(days=days_ago)).isoformat(timespec="seconds")


class TestCorruptRowsCannotOutliveRetention:

    def test_a_file_of_only_malformed_rows_is_rewritten_not_ignored(self,
                                                                    tmp_path):
        """★這是最嚴重的那一個★

        整份都是壞列 → 原本 `rows` 是空的 → 回「沒有過期的列」→ 檔案原封不動。
        壞列裡的病歷號就此永久留在磁碟上。
        """
        p = tmp_path / "idx.jsonl"
        _write(p, ["{這不是 JSON, chart_no: 24994923}",
                   "另一列壞掉的 24994923"])

        result = pl.prune_index(str(p), now=NOW)

        assert result.status == pl.PRUNE_OK
        assert result.invalid_removed == 2
        assert result.corruption_detected is True
        assert "24994923" not in p.read_text(encoding="utf-8"), (
            "★壞列裡的病歷號還在磁碟上★")

    def test_a_line_that_is_valid_json_but_not_an_object_also_counts(self,
                                                                     tmp_path):
        """★突變驗證抓到的漏洞★

        `json.loads` 成功但結果不是 dict（例如整列是一個陣列或字串）——
        原本只是 `isinstance` 檢查沒過就被丟掉，不計數。整份都是這種列時，
        檔案一樣不會被重寫，裡面的病歷號一樣永久留著。
        """
        p = tmp_path / "idx.jsonl"
        _write(p, ['["24994923"]', '"24994923"', "12345"])

        result = pl.prune_index(str(p), now=NOW)

        assert result.invalid_removed == 3
        assert result.corruption_detected is True
        assert "24994923" not in p.read_text(encoding="utf-8")

    def test_a_garbage_timestamp_does_not_survive_forever(self, tmp_path):
        """`"zzz" >= cutoff` 在字串比較下恆為真 → 那一列永遠不過期。"""
        p = tmp_path / "idx.jsonl"
        _write(p, [_row("zzz"), _row(_iso(1), chart_no="11111111")])

        result = pl.prune_index(str(p), now=NOW)

        assert result.corrupt_removed == 1
        assert "24994923" not in p.read_text(encoding="utf-8")
        assert "11111111" in p.read_text(encoding="utf-8"), "正常列不該被牽連"

    def test_a_far_future_timestamp_is_treated_as_corrupt(self, tmp_path):
        """時鐘跑掉/打錯/被竄改造成的未來時間戳同樣永遠不過期。"""
        p = tmp_path / "idx.jsonl"
        far_future = (NOW + timedelta(days=400)).isoformat(timespec="seconds")
        _write(p, [_row(far_future)])

        result = pl.prune_index(str(p), now=NOW)

        assert result.corrupt_removed == 1
        assert "24994923" not in p.read_text(encoding="utf-8")

    def test_a_slightly_future_timestamp_is_tolerated(self, tmp_path):
        """★空集合不算通過★ 一天以內的偏差是時區/NTP 誤差，不是壞資料。"""
        p = tmp_path / "idx.jsonl"
        soon = (NOW + timedelta(hours=6)).isoformat(timespec="seconds")
        _write(p, [_row(soon)])

        result = pl.prune_index(str(p), now=NOW)

        assert result.corrupt_removed == 0
        assert result.status == pl.PRUNE_NOTHING_TO_DO
        assert "24994923" in p.read_text(encoding="utf-8")

    def test_mixed_valid_and_broken_rows(self, tmp_path):
        p = tmp_path / "idx.jsonl"
        _write(p, [
            _row(_iso(1), chart_no="11111111"),      # 留
            _row(_iso(99), chart_no="22222222"),     # 過期
            _row("zzz", chart_no="33333333"),        # 時間戳壞
            "{壞掉的 JSON 44444444}",                 # 格式壞
        ])

        result = pl.prune_index(str(p), now=NOW)

        text = p.read_text(encoding="utf-8")
        assert "11111111" in text
        for gone in ("22222222", "33333333", "44444444"):
            assert gone not in text, f"{gone} 應該被清掉"
        assert (result.removed, result.kept) == (3, 1)
        assert (result.corrupt_removed, result.invalid_removed) == (1, 1)

    def test_a_healthy_file_is_still_left_alone(self, tmp_path):
        """★空集合不算通過★ 沒事就不要重寫（重寫本身也有風險）。"""
        p = tmp_path / "idx.jsonl"
        _write(p, [_row(_iso(1)), _row(_iso(2))])
        before = p.read_text(encoding="utf-8")

        result = pl.prune_index(str(p), now=NOW)

        assert result.status == pl.PRUNE_NOTHING_TO_DO
        assert result.corruption_detected is False
        assert p.read_text(encoding="utf-8") == before


class TestTheHealthReportSaysSoOutLoud:

    def test_corruption_appears_in_the_summary(self, tmp_path):
        p = tmp_path / "idx.jsonl"
        _write(p, [_row("zzz")])
        said = pl.prune_index(str(p), now=NOW).describe()
        assert "時間戳異常" in said or "格式損毀" in said, (
            "保留期報告必須說出有壞資料，不能只說刪了幾列")

    def test_a_clean_prune_does_not_cry_wolf(self, tmp_path):
        p = tmp_path / "idx.jsonl"
        _write(p, [_row(_iso(99))])
        said = pl.prune_index(str(p), now=NOW).describe()
        assert "時間戳異常" not in said and "格式損毀" not in said


class TestReadFailureIsNotEmptyData:

    def test_an_unreadable_index_is_a_failure_not_nothing_to_do(self, tmp_path,
                                                               monkeypatch):
        p = tmp_path / "idx.jsonl"
        _write(p, [_row(_iso(1))])

        def _boom(*a, **k):
            raise OSError("被鎖住")

        monkeypatch.setattr(pl, "open", _boom, raising=False)
        result = pl.prune_index(str(p), now=NOW)

        assert result.status == pl.PRUNE_FAILED
        assert result.ok is False

    def test_appending_refuses_to_overwrite_an_unreadable_index(self, tmp_path,
                                                               monkeypatch):
        """★同型病灶★ 讀不到卻照樣寫回，會把整份索引取代成只有這一筆。"""
        p = tmp_path / "idx.jsonl"
        _write(p, [_row(_iso(1), chart_no="11111111")])
        before = p.read_text(encoding="utf-8")

        real_open = open

        def _boom(path, *a, **k):
            if str(path) == str(p):
                raise OSError("被鎖住")
            return real_open(path, *a, **k)

        monkeypatch.setattr(pl, "open", _boom, raising=False)
        ok = pl.append_index(str(p), ts=_iso(0), action="新", detail="",
                             locator={"room": "103"}, now=NOW)

        assert ok is False
        monkeypatch.undo()
        assert p.read_text(encoding="utf-8") == before, (
            "★讀不到卻把索引覆蓋成只剩新的那一筆★")


class TestTheTimestampClassifier:

    @pytest.mark.parametrize("raw,expected", [
        (None, "corrupt"),
        ("", "corrupt"),
        ("zzz", "corrupt"),
        ("2026-13-45T99:99:99", "corrupt"),
        (12345, "corrupt"),
        ((NOW + timedelta(days=400)).isoformat(), "corrupt"),
        ((NOW + timedelta(hours=1)).isoformat(), "keep"),
        ((NOW - timedelta(days=1)).isoformat(), "keep"),
        ((NOW - timedelta(days=99)).isoformat(), "expired"),
    ])
    def test_each_shape_of_timestamp(self, raw, expected):
        assert pl._classify_ts(raw, NOW, 30) == expected

    def test_a_timezone_aware_timestamp_does_not_explode(self):
        """存的一律是 naive，但別人手改過的檔可能帶時區 —— 不可以丟例外。"""
        from datetime import timezone
        aware = (NOW - timedelta(days=1)).replace(
            tzinfo=timezone.utc).isoformat()
        assert pl._classify_ts(aware, NOW, 30) in ("keep", "expired")


class TestByteLevelCorruption:
    """[2026-08-02 外審第 2 輪 P1] 非 UTF-8 位元組是另一種壞列。"""

    def test_a_line_with_invalid_utf8_does_not_wedge_the_whole_prune(
            self, tmp_path):
        """★這是我這批自己漏掉的同型 bug★

        text mode 的 `readlines()` 只要碰到一列含非 UTF-8 位元組就丟
        `UnicodeDecodeError` —— 那不是 OSError，會一路穿到最外層而永遠回
        PRUNE_FAILED。那一列裡的病歷號於是永久留在磁碟上。
        """
        p = tmp_path / "idx.jsonl"
        good = _row(_iso(1), chart_no="11111111").encode("utf-8")
        broken = b'{"ts": "x", "chart_no": "24994923\xff\xfe"}'
        p.write_bytes(good + b"\n" + broken + b"\n")

        result = pl.prune_index(str(p), now=NOW)

        assert result.status == pl.PRUNE_OK, (
            f"整份修剪被一列壞位元組卡死了：{result.describe()}")
        assert result.invalid_removed == 1
        raw = p.read_bytes()
        assert b"24994923" not in raw, "★壞位元組那列的病歷號還在磁碟上★"
        assert b"11111111" in raw, "正常列不該被牽連"

    def test_a_file_that_is_entirely_binary_garbage(self, tmp_path):
        p = tmp_path / "idx.jsonl"
        p.write_bytes(b"\x00\x01\xff\xfe 24994923\n\xff\xff\n")

        result = pl.prune_index(str(p), now=NOW)

        assert result.ok is True
        assert result.corruption_detected is True
        assert b"24994923" not in p.read_bytes()


class TestAppendAlsoReportsCorruption:
    """[2026-08-02 外審第 2 輪 P2] append 也會順手清掉壞列 —— 要講出來。"""

    def test_append_logs_the_corruption_it_silently_removed(self, tmp_path,
                                                            caplog):
        """清掃之前先發生一次 mismatch，壞列就被安靜清掉；下一次清掃看到的是
        乾淨檔案 → 損毀事件永久不可觀測。而「刪除但記錄數量」正是不做
        quarantine 的前提。"""
        p = tmp_path / "idx.jsonl"
        _write(p, [_row("zzz", chart_no="24994923"), "{壞掉的}"])

        with caplog.at_level("WARNING"):
            ok = pl.append_index(str(p), ts=_iso(0), action="新", detail="",
                                 locator={"room": "103"}, now=NOW)

        assert ok is True
        assert "24994923" not in p.read_text(encoding="utf-8")
        text = caplog.text
        assert "時間戳異常" in text and "格式損毀" in text, (
            f"append 清掉壞列卻沒有留下任何紀錄：{text}")
        assert "24994923" not in text, "★log 不可以含病歷號★"

    def test_a_clean_append_does_not_warn(self, tmp_path, caplog):
        """★空集合不算通過★"""
        p = tmp_path / "idx.jsonl"
        _write(p, [_row(_iso(1))])
        with caplog.at_level("WARNING"):
            pl.append_index(str(p), ts=_iso(0), action="新", detail="",
                            locator={"room": "103"}, now=NOW)
        assert "格式損毀" not in caplog.text


def test_the_retention_adapter_surfaces_corruption(tmp_path, caplog):
    """★真的跑 main 的 adapter + sweep，不是只跑 prune_index★

    [2026-08-02 外審第 2 輪 P2] 原本這支叫 `..._sweeper_still_reports_it`
    卻只呼叫 `prune_index()` —— 名字說的事它根本沒做。
    `sweep` 的 extra_task 契約只收整數，摘要只會印「定位索引×N」，
    所以損毀分類必須由 adapter 自己講出來。
    """
    import main
    from cmuh_common.paths import get_conf_path
    from cmuh_common.retention import sweep

    idx = get_conf_path(main._LOCATOR_INDEX_FILENAME)
    with io.open(idx, "w", encoding="utf-8", newline="") as f:
        f.write(_row("zzz", chart_no="24994923") + "\n")

    with caplog.at_level("WARNING"):
        res = sweep([], extra_tasks=[("定位索引", main._prune_locator_index)])

    assert res.deleted.get("定位索引") == 1
    assert not res.failed

    # ★[突變驗證抓到] 不可以只斷言「caplog 裡有這個詞」★
    #   `prune_index` 自己也會記一行含「時間戳異常」的 warning，所以把 adapter
    #   那一行拿掉，測試照樣綠 —— 它驗的是別人的證據。要指名 adapter 那一行。
    from_adapter = [r.getMessage() for r in caplog.records
                    if r.getMessage().startswith("[retention]")]
    assert from_adapter, "清掃層完全沒有把損毀講出來（只有 locator 自己記）"
    assert any("時間戳異常" in m for m in from_adapter), (
        f"清掃摘要看不出有損毀：{from_adapter}")
    assert "24994923" not in caplog.text
    with io.open(idx, encoding="utf-8") as f:
        assert "24994923" not in f.read()
