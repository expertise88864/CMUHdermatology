# -*- coding: utf-8 -*-
"""UVB 劑量規則外部化(settings/uvb_rules.json)載入器測試。

確保:沒檔→用程式內預設並寫出模板;合法 override→生效並反映在 compute_new_dose;壞值/壞檔→逐欄或整體
安全退回預設(醫療安全:壞設定絕不算錯劑量)。每個測試後還原常數,避免 override 外洩影響其他測試。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import cmuh_common.uvb_dose as uvb_dose  # noqa: E402
from cmuh_common.uvb_dose import (  # noqa: E402
    compute_new_dose,
    load_and_apply_uvb_rules,
    write_uvb_rules_template,
)


@pytest.fixture(autouse=True)
def _restore_rules():
    """每個測試前後都把劑量常數還原成凍結預設,避免外部測試/本機 settings 污染、或 override 外洩。"""
    uvb_dose._apply_uvb_rules(dict(uvb_dose._UVB_RULE_DEFAULTS))
    yield
    uvb_dose._apply_uvb_rules(dict(uvb_dose._UVB_RULE_DEFAULTS))


def _write(tmp_path, data) -> str:
    p = tmp_path / "uvb_rules.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_no_file_uses_defaults_and_writes_template(tmp_path):
    p = str(tmp_path / "uvb_rules.json")          # 不存在
    eff = load_and_apply_uvb_rules(p)
    assert eff == uvb_dose._UVB_RULE_DEFAULTS       # 本次用預設
    assert os.path.exists(p)                         # 已 materialize 模板供編輯
    data = json.loads(open(p, encoding="utf-8").read())
    assert data["schema_version"] == uvb_dose.UVB_RULES_SCHEMA_VERSION
    assert data["too_close_days"] == 2 and data["decay_75_factor"] == 0.75


def test_valid_override_applied_and_affects_dose(tmp_path):
    p = _write(tmp_path, {"same_dose_days": 10, "decay_75_factor": 0.8})
    eff = load_and_apply_uvb_rules(p)
    assert eff["same_dose_days"] == 10 and eff["decay_75_factor"] == 0.8
    assert uvb_dose.SAME_DOSE_DAYS == 10
    # 10 天剛好「保持」(門檻被改成 10);12 天落在 ×0.8 衰減 → floor10(500*0.8)=400
    assert compute_new_dose(dose=500, increase=30, max_dose=800, days_diff=10) == 500
    assert compute_new_dose(dose=500, increase=30, max_dose=800, days_diff=12) == 400


def test_out_of_range_and_bad_type_fall_back_per_field(tmp_path):
    p = _write(tmp_path, {"too_close_days": 999, "max_dose": "abc", "same_dose_days": 8})
    eff = load_and_apply_uvb_rules(p)
    # 超出上限 → 退回預設
    assert eff["too_close_days"] == uvb_dose._UVB_RULE_DEFAULTS["too_close_days"]
    # 型別錯 → 退回預設
    assert eff["max_dose"] == uvb_dose._UVB_RULE_DEFAULTS["max_dose"]
    # 合法欄位照常生效
    assert eff["same_dose_days"] == 8


def test_corrupt_json_falls_back_to_defaults(tmp_path):
    p = tmp_path / "uvb_rules.json"
    p.write_text("{ this is not valid json", encoding="utf-8")
    eff = load_and_apply_uvb_rules(str(p))
    assert eff == uvb_dose._UVB_RULE_DEFAULTS
    assert uvb_dose.TOO_CLOSE_DAYS == 2             # 常數未被壞檔影響


def test_non_dict_json_falls_back(tmp_path):
    p = tmp_path / "uvb_rules.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")     # 合法 JSON 但非物件
    eff = load_and_apply_uvb_rules(str(p))
    assert eff == uvb_dose._UVB_RULE_DEFAULTS


def test_write_template_roundtrip(tmp_path):
    p = str(tmp_path / "uvb_rules.json")
    assert write_uvb_rules_template(p) is True
    data = json.loads(open(p, encoding="utf-8").read())
    for key, _const, *_ in uvb_dose._UVB_RULE_FIELDS:
        assert data[key] == uvb_dose._UVB_RULE_DEFAULTS[key]


@pytest.mark.parametrize("bad", [True, False, "5", 7.9])
def test_wrong_type_does_not_coerce_into_dose(tmp_path, bad):
    """[Codex] true→1 / "5"→5 / 7.9→7 等錯型別不可被轉成合法值而改到劑量 → 必須退回該欄預設。"""
    p = _write(tmp_path, {"too_close_days": bad})
    eff = load_and_apply_uvb_rules(p)
    assert eff["too_close_days"] == uvb_dose._UVB_RULE_DEFAULTS["too_close_days"]
    assert uvb_dose.TOO_CLOSE_DAYS == 2
    # 確認 compute_new_dose 的「太近」邊界沒被污染:1 天仍回 None(警告終止),不會變成遞增
    assert compute_new_dose(dose=500, increase=30, max_dose=800, days_diff=1) is None


def test_incoherent_bucket_order_falls_back_whole(tmp_path):
    """[Codex] 每欄都在範圍內,但 same_dose_days=30 > 預設 decay_75_upper=14 → day-bucket 順序錯亂 →
    整份 override 退回預設(不可只靠逐欄上下限)。"""
    p = _write(tmp_path, {"same_dose_days": 30})
    eff = load_and_apply_uvb_rules(p)
    assert eff == uvb_dose._UVB_RULE_DEFAULTS
    assert uvb_dose.SAME_DOSE_DAYS == 7
    # 12 天仍走預設 ×0.75 衰減(370),不會因壞組合變成遞增 530
    assert compute_new_dose(dose=500, increase=30, max_dose=800, days_diff=12) == 370


def test_inverted_decay_factor_falls_back_whole(tmp_path):
    """衰減倍率反轉(×0.5 比 ×0.75 還大)= 衰減反而增量 → 整份退回預設。"""
    p = _write(tmp_path, {"decay_75_factor": 0.4, "decay_50_factor": 0.6})
    eff = load_and_apply_uvb_rules(p)
    assert eff == uvb_dose._UVB_RULE_DEFAULTS


def test_coherent_multifield_override_applied(tmp_path):
    """一致地整組往上調(同時改 same / decay 上界)→ 通過一致性檢查、正常生效。"""
    p = _write(tmp_path, {"same_dose_days": 9, "decay_75_upper": 12, "decay_50_upper": 18})
    eff = load_and_apply_uvb_rules(p)
    assert eff["same_dose_days"] == 9
    assert eff["decay_75_upper"] == 12 and eff["decay_50_upper"] == 18
    assert compute_new_dose(dose=500, increase=30, max_dose=800, days_diff=9) == 500  # 9 天=保持
