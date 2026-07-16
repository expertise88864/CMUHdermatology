# -*- coding: utf-8 -*-
"""契約金絲雀框架（contract_canary）純邏輯 + 基線檔 IO。"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common.contract_canary import (  # noqa: E402
    STATUS_DRIFT, STATUS_OK, STATUS_UNCALIBRATED, STATUS_UNKNOWN,
    ContractBaseline, compare_fingerprint,
)


# ── compare_fingerprint 裁決 ─────────────────────────────────────────────────
def test_ok_when_identical():
    v = compare_fingerprint("his_menu", {"代碼輸入": 219}, {"代碼輸入": 219})
    assert v.status == STATUS_OK and not v.is_drift
    assert v.should_block_write is False


def test_drift_when_value_changed():
    v = compare_fingerprint("his_menu", {"代碼輸入": 219}, {"代碼輸入": 220})
    assert v.status == STATUS_DRIFT and v.is_drift
    assert v.should_block_write is True
    assert v.changes == [("代碼輸入", 219, 220)]
    assert "219" in v.detail and "220" in v.detail


def test_uncalibrated_when_no_baseline():
    v = compare_fingerprint("punch", None, {"lb_systime": True})
    assert v.status == STATUS_UNCALIBRATED
    assert v.should_block_write is False    # 沒基線不擋(避免假警報停熱鍵)


def test_unknown_when_sampling_failed():
    v = compare_fingerprint("punch", {"lb_systime": True}, None)
    assert v.status == STATUS_UNKNOWN
    assert v.should_block_write is False    # 採不到現況不擋


def test_keys_and_ignore():
    base = {"a": 1, "b": 2, "vol": 99}
    cur = {"a": 1, "b": 5, "vol": 0}
    # 只比 a/b、忽略 vol
    v = compare_fingerprint("s", base, cur, keys=("a", "b"))
    assert v.status == STATUS_DRIFT and v.changes == [("b", 2, 5)]
    v2 = compare_fingerprint("s", base, cur, ignore=("vol",))
    assert {k for k, _b, _c in v2.changes} == {"b"}


def test_missing_key_in_current_is_drift():
    v = compare_fingerprint("s", {"x": 1}, {})     # 現況缺 x
    assert v.status == STATUS_DRIFT and v.changes == [("x", 1, None)]


def test_human_readable():
    assert "疑似院方改版" in compare_fingerprint("his", {"x": 1}, {"x": 2}).human()
    assert "契約一致" in compare_fingerprint("his", {"x": 1}, {"x": 1}).human()
    assert "尚未校正" in compare_fingerprint("his", None, {"x": 1}).human()


# ── ContractBaseline 檔案 IO ─────────────────────────────────────────────────
def test_baseline_roundtrip(tmp_path):
    p = str(tmp_path / "contract_baseline.json")
    b = ContractBaseline(p)
    assert b.get("his_menu") is None              # 未校正
    b.set("his_menu", {"代碼輸入": 219, "同意書": 669}, note="2026-06-29 校正")
    assert b.get("his_menu") == {"代碼輸入": 219, "同意書": 669}
    info = b.info("his_menu")
    assert info["note"] == "2026-06-29 校正" and info["calibrated_at"]
    # 另一面向獨立
    assert b.get("punch") is None
    b.set("punch", {"lb_systime": True})
    assert b.get("his_menu") == {"代碼輸入": 219, "同意書": 669}   # 不受影響


def test_baseline_end_to_end_verdict(tmp_path):
    p = str(tmp_path / "contract_baseline.json")
    b = ContractBaseline(p)
    b.set("his_menu", {"代碼輸入": 219})
    # 現況一致 → OK
    assert compare_fingerprint("his_menu", b.get("his_menu"),
                               {"代碼輸入": 219}).status == STATUS_OK
    # 院方改版後動態 id 位移 → DRIFT → 擋寫
    v = compare_fingerprint("his_menu", b.get("his_menu"), {"代碼輸入": 220})
    assert v.should_block_write is True


def test_baseline_clear(tmp_path):
    p = str(tmp_path / "contract_baseline.json")
    b = ContractBaseline(p)
    b.set("s", {"x": 1})
    assert b.clear("s") is True and b.get("s") is None
    assert b.clear("s") is False                  # 已無


def test_baseline_rejects_newer_schema(tmp_path):
    p = tmp_path / "contract_baseline.json"
    p.write_text(json.dumps({
        "schema_version": 999,
        "surfaces": {"s": {"fingerprint": {"x": 1}}}}), encoding="utf-8")
    b = ContractBaseline(str(p))
    assert b.get("s") is None                     # 拒絕降版 → 視為無基線


def test_baseline_set_clear_no_op_on_newer_schema(tmp_path):
    # [codex] set/clear 對「更新版 schema」檔必須 no-op、原檔 bytes 不變(不降版毀損)
    p = tmp_path / "contract_baseline.json"
    original = json.dumps({
        "schema_version": 999,
        "surfaces": {"s": {"fingerprint": {"x": 1}}},
        "future_field": "keep me"}, ensure_ascii=False)
    p.write_text(original, encoding="utf-8")
    raw_bytes = p.read_bytes()
    b = ContractBaseline(str(p))
    assert b.set("his_menu", {"title_version": "1150701"}) is False  # 回 False=被拒
    assert p.read_bytes() == raw_bytes, "set 不得覆寫更新版 schema 檔"
    assert b.clear("s") is False                      # 應被拒
    assert p.read_bytes() == raw_bytes, "clear 不得覆寫更新版 schema 檔"


def test_baseline_set_returns_true_on_success(tmp_path):
    b = ContractBaseline(str(tmp_path / "contract_baseline.json"))
    assert b.set("s", {"x": 1}) is True               # 正常寫入回 True


def test_baseline_corrupt_file_is_empty(tmp_path):
    p = tmp_path / "contract_baseline.json"
    p.write_text("{ not json", encoding="utf-8")
    b = ContractBaseline(str(p))
    assert b.get("s") is None                     # 壞檔不拋、視為無基線
    b.set("s", {"x": 1})                          # 仍可覆寫成新基線
    assert b.get("s") == {"x": 1}
