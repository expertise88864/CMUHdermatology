# -*- coding: utf-8 -*-
"""[2026-11-25 使用者] 各台電腦的 HIS 版本不同(1151020/1151117)→ 偵測版本套用 id。"""
import importlib
import importlib.util
import json
import os
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

import main  # noqa: E402
from cmuh_common import contract_canary as cc  # noqa: E402
from cmuh_common import his_contract as hc  # noqa: E402

TITLE = "西醫門診醫師作業 V.{}.01"
# ★字面值釘死★ 不可改成讀 his_contract
OLD = {"MENU_ID_代碼輸入": 220, "MENU_ID_同意書": 672, "MENU_ID_FINISH_NO_PRINT": 278}
NEW = {"MENU_ID_代碼輸入": 221, "MENU_ID_同意書": 673, "MENU_ID_FINISH_NO_PRINT": 279}


@pytest.fixture()
def override():
    """寫本機快速修正並 reload;結束還原成純字面值。"""
    def write(data):
        path = hc.override_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"for_calibration": hc.override_marker(), **data}, f)
        return importlib.reload(hc)
    yield write
    try:
        os.remove(hc.override_path())
    except OSError:
        pass
    importlib.reload(hc)


@pytest.mark.parametrize("version, want", [
    ("1151020", OLD), ("1151103", OLD), ("1151117", NEW),
    ("1151110", OLD), ("1151201", NEW),        # 沒登記:沿用較舊中最近的
    ("1150101", OLD), (None, OLD), ("", OLD),  # 更舊/讀不到:預設版本
])
def test_ids_for_version(version, want):
    assert hc.menu_ids_for(version) == want


def test_every_version_has_evidence():
    assert set(hc.MENU_IDS_BY_VERSION) <= {c.version for c in hc.CALIBRATION_HISTORY}
    assert hc.CALIBRATED_VERSION in hc.MENU_IDS_BY_VERSION


@pytest.mark.parametrize("version, want", [("1151020", OLD), ("1151117", NEW)])
def test_the_window_title_decides(monkeypatch, version, want):
    monkeypatch.setattr(main, "_his_title_of", lambda _h: TITLE.format(version))
    monkeypatch.setattr(main, "_his_last_sample", ("1150101.01", ""))
    assert {n: main._his_menu_id(n, 1) for n in want} == want


@pytest.mark.parametrize("title", ["", "西醫門診醫師作業"])
def test_unreadable_title_uses_the_last_sample(monkeypatch, title):
    monkeypatch.setattr(main, "_his_title_of", lambda _h: title)
    monkeypatch.setattr(main, "_his_last_sample", ("1151117.01", "ok"))
    assert main._his_menu_id("MENU_ID_FINISH_NO_PRINT", 1) == 279
    monkeypatch.setattr(main, "_his_last_sample", ("", ""))
    assert main._his_menu_id("MENU_ID_FINISH_NO_PRINT", 1) == 278


@pytest.mark.parametrize("version, sent", [("1151020", 278), ("1151117", 279)])
def test_f11_sends_the_id_of_this_his(monkeypatch, version, sent):
    got = []
    monkeypatch.setattr(main, "_his_title_of", lambda _h: TITLE.format(version))
    monkeypatch.setattr(main, "_find_menu_command_id_by_text", lambda *_a: 0)
    monkeypatch.setattr(main, "_send_yiling_menu_command",
                        lambda _h, i: got.append(i) or True)
    monkeypatch.setattr(main, "_record_his_action", lambda *_a, **_k: None)
    assert main._f11_send_finish_no_print(1, "2", "F11", 0.0)
    assert got == [sent]


@pytest.mark.parametrize("version, sent", [("1151020", 220), ("1151117", 221)])
def test_f1_sends_the_id_of_this_his(monkeypatch, version, sent):
    got = []
    monkeypatch.setattr(main, "_find_hospital_main_window", lambda: 1)
    monkeypatch.setattr(main, "_his_title_of", lambda _h: TITLE.format(version))
    for name in ("_ensure_hospital_foreground", "_force_ime_english",
                 "_mark_hotkey_action_time"):
        monkeypatch.setattr(main, name, lambda *_a: None)
    monkeypatch.setattr(main, "_get_thread_focus", lambda _h: 0)
    monkeypatch.setattr(main, "_send_yiling_menu_command",
                        lambda _h, i: bool(got.append(i)))
    assert not main._script_code_input_adaptive("51019", "F1",
                                                warn_on_silent_failure=False)
    assert got == [sent]


@pytest.mark.parametrize("version, sent", [("1151020", 672), ("1151117", 673)])
def test_f9_sends_the_id_of_this_his(monkeypatch, version, sent):
    got = []
    monkeypatch.setattr(main, "_find_hospital_main_window", lambda: 1)
    monkeypatch.setattr(main, "_his_title_of", lambda _h: TITLE.format(version))
    monkeypatch.setattr(main, "_get_window_pid", lambda _h: 5)
    monkeypatch.setattr(main, "_collect_windows_by_class", lambda *_a, **_k: [])
    monkeypatch.setattr(main, "_send_yiling_menu_command",
                        lambda _h, i: bool(got.append(i)))
    assert not main.script_F9_F10_consent_form_adaptive("MO04", label="F9")
    assert got == [sent]


@pytest.mark.parametrize("version, status", [
    ("1151020", cc.STATUS_OK), ("1151117", cc.STATUS_OK),
    ("1151201", cc.STATUS_DRIFT)])
def test_registered_versions_are_not_drift(monkeypatch, version, status):
    for base in ("1151020", "1151117"):
        monkeypatch.setattr(main, "_his_write_baseline_fp",
                            lambda b=base: {"title_version": b,
                                            "title_version_full": f"{b}.01"})
        assert main._his_write_verdict_for(TITLE.format(version)).status == status
    # 同主版本仍保留「重新校正」的尾碼敏感度
    assert main._his_write_verdict_for(
        "西醫門診醫師作業 V.1151117.02").status == cc.STATUS_DRIFT


def test_an_override_bound_to_one_version_leaves_the_others(override):
    m = override({"his_version": "1151117", "MENU_ID_同意書": 680})
    assert m.OVERRIDE_ERROR == "" and "V.1151117" in m.OVERRIDE_NOTE
    assert m.menu_ids_for("1151117")["MENU_ID_同意書"] == 680
    assert m.menu_ids_for("1151020") == OLD and m.MENU_ID_同意書 == 672
    assert m.menu_ids_for("1151201") == NEW and m.menu_ids_for(None) == OLD


def test_an_unreadable_version_uses_the_machine_calibration(override):
    m = override({"his_version": "1151020", "MENU_ID_同意書": 680})
    assert m.menu_ids_for(None)["MENU_ID_同意書"] == 680
    assert m.menu_ids_for("1151103") == OLD, "讀得到的版本仍以實際版本比對綁定"
    m = override({"CALIBRATED_VERSION": "1151117"})
    assert m.menu_ids_for("") == NEW and m.menu_ids_for("1151020") == OLD


def test_an_unbound_override_applies_to_every_version(override):
    m = override({"MENU_ID_同意書": 680})
    assert {m.menu_ids_for(v)["MENU_ID_同意書"] for v in ("1151020", "1151117")} == {680}


@pytest.mark.parametrize("bad", [1151117, "abc", "12", None])
def test_a_bad_his_version_rejects_the_whole_file(override, bad):
    m = override({"his_version": bad, "MENU_ID_同意書": 680})
    assert m.OVERRIDE_ERROR and m.menu_ids_for("1151117") == NEW


def test_the_marker_follows_the_table(monkeypatch):
    before = hc.override_marker()
    monkeypatch.setitem(hc.MENU_IDS_BY_VERSION, "1151117", {**NEW, "MENU_ID_同意書": 674})
    assert hc.override_marker() != before


def test_the_fix_tool_binds_to_the_tested_version():
    spec = importlib.util.spec_from_file_location(
        "_his_fix_tool_v", os.path.join(ROOT, "HIS熱鍵ID修正工具.py"))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    assert tool.program_ids(hc, "1151117") == NEW
    data, err = tool.build_override(hc, None, {"MENU_ID_同意書": 680}, "1151117")
    assert err == "" and data["his_version"] == "1151117"
    data_none, err = tool.build_override(hc, None, {"MENU_ID_同意書": 680}, None)
    assert data_none == {} and "HIS 版本" in err
    # 讀不到版本時測的:可以回報、不能寫入,之後才讀到版本也不補綁
    ok, unknown = {"MENU_ID_代碼輸入": 221}, {"MENU_ID_同意書": 680}
    both, tested = {**ok, **unknown}, {"MENU_ID_代碼輸入": "1151117", "MENU_ID_同意書": None}
    assert tool.writable_ids(hc, both, tested, "1151117") == ok
    assert tool.writable_ids(hc, unknown, {"MENU_ID_同意書": None}, None) == {}
    assert tool.writable_ids(object(), both, tested, None) == both
    assert tool.report_version(both, tested, "1151117", "1151117.01") is None
    assert tool.report_version(ok, tested, "1151117", "1151117.01") == "1151117.01"
    # 另一個版本的舊內容不合併
    data, err = tool.build_override(hc, data, {"MENU_ID_代碼輸入": 222}, "1151020")
    assert err == "" and "MENU_ID_同意書" not in data


@pytest.mark.parametrize("ver", [None, "1151117"])
def test_send_test_probes_and_records_the_version_at_send_time(ver):
    """直接執行 main() 裡的 send_test:沒有版本也要送得出去,並記下當時的版本。"""
    import ast
    import textwrap
    from types import SimpleNamespace as NS
    with open(os.path.join(ROOT, "HIS熱鍵ID修正工具.py"), encoding="utf-8") as f:
        src = f.read()
    node = next(n for n in ast.walk(ast.parse(src))
                if isinstance(n, ast.FunctionDef) and n.name == "send_test")
    code = textwrap.dedent(" " * node.col_offset + ast.get_source_segment(src, node))
    sent, last = [], {"key": None, "id": None, "ver": "x"}
    state = {"major": ver, "now": ver, "hwnd": 1}
    ns = {"state": state, "refresh_status": lambda: None, "last_sent": last,
          "cand_var": NS(get=lambda: 221), "send_menu_command": lambda h, i: sent.append(i),
          "current_func": lambda: {"key": "K", "success": "s"},
          "yes_btn": NS(config=lambda **_k: None), "sent_var": NS(set=lambda _t: None),
          "messagebox": NS(showwarning=lambda *a: pytest.fail(str(a)))}
    exec(code, ns)  # noqa: S102
    ns["send_test"]()
    assert sent == [221] and last == {"key": "K", "id": 221, "ver": ver}
