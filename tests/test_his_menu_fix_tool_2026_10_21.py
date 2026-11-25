# -*- coding: utf-8 -*-
"""[2026-10-21] 程式資料夾的「HIS熱鍵ID修正工具.py」(取代 scripts/test_yiling_menu_id.py)。

給不會寫程式的人用的急救精靈:選功能 → 送出測試 → 確認 → 套用本機快速修正。
這裡釘的是不碰 HIS 視窗的邏輯:寫出去的檔一定能被主程式接受、過期檔不會被
復活、測試範圍不含「完成」選單,以及雙擊入口/線上更新清單真的指到它。
"""
import importlib
import importlib.util
import os
import re
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(ROOT, "src"))

import cmuh_common.his_contract as hc  # noqa: E402

TOOL_NAME = "HIS熱鍵ID修正工具.py"
GUIDE_NAME = "HIS熱鍵ID修正工具_使用說明.txt"


@pytest.fixture(scope="module")
def tool():
    spec = importlib.util.spec_from_file_location("_his_fix_tool", os.path.join(ROOT, TOOL_NAME))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def clean_hc():
    """每個測試結束把 his_contract reload 回純字面值(conftest 已把 settings 導到 tmp)。"""
    yield hc
    try:
        os.remove(hc.override_path())
    except OSError:
        pass
    importlib.reload(hc)


class TestImportHasNoSideEffects:
    def test_importing_the_tool_does_not_touch_the_app_dir_env(self, tool):
        """模組層不可以設 CMUH_APP_DIR —— 只有真的執行(載入主程式設定)時才設。"""
        src = open(os.path.join(ROOT, TOOL_NAME), encoding="utf-8").read()
        head = src.split("def load_running_contract", 1)[0]
        assert "CMUH_APP_DIR" not in head.split('"""', 2)[2]


class TestHisVersion:
    @pytest.mark.parametrize("title, want", [
        ("西醫門診醫師作業 V.1151020.01", ("1151020", "1151020.01")),
        ("西醫門診醫師作業 V1150629", ("1150629", "1150629")),
        ("西醫門診醫師作業", (None, None)),
        ("", (None, None)),
    ])
    def test_parse(self, tool, title, want):
        assert tool.parse_his_version(title) == want


class TestSafeRanges:
    def test_steps_stop_at_the_edges(self, tool):
        assert tool.clamp_step(233, +1, (213, 234)) == 234
        assert tool.clamp_step(234, +1, (213, 234)) == 234
        assert tool.clamp_step(213, -1, (213, 234)) == 213
        assert tool.clamp_step(999, 0, (213, 234)) == 234

    def test_the_ranges_never_reach_the_finish_menu(self, tool):
        """★「完成」選單不在任何測試範圍★:那可能真的把門診完成。"""
        finish = hc.MENU_ID_FINISH_NO_PRINT
        for f in tool.FUNCTIONS:
            lo, hi = f["range"]
            assert not (lo - 5 <= finish <= hi + 5), (f["name"], f["range"])

    def test_the_current_ids_are_inside_their_ranges(self, tool):
        """預設從目前的編號開始測:它本身要在範圍內,前後也要留空間。"""
        for f in tool.FUNCTIONS:
            lo, hi = f["range"]
            cur = getattr(hc, f["key"])
            assert lo + 2 <= cur <= hi - 2, (f["key"], cur, f["range"])

    def test_every_function_key_is_accepted_by_the_override_whitelist(self, tool):
        for f in tool.FUNCTIONS:
            upd, err = hc.parse_override({"for_calibration": hc.override_marker(),
                                          f["key"]: 500}, hc.override_marker())
            assert err == "" and upd == {f["key"]: 500}


class TestBuildOverride:
    def test_the_written_file_is_accepted_by_the_main_program(self, tool, clean_hc):
        data, err = tool.build_override(hc, None, {"MENU_ID_代碼輸入": 221})
        assert err == ""
        assert data["for_calibration"] == hc.override_marker()
        tool.write_override(hc, data)
        m = importlib.reload(hc)
        assert m.MENU_ID_代碼輸入 == 221
        assert m.OVERRIDE_ERROR == "" and "MENU_ID_代碼輸入" in m.OVERRIDE_NOTE

    def test_a_current_file_is_merged(self, tool):
        old = {"for_calibration": hc.override_marker(), "MENU_ID_同意書": 673}
        data, err = tool.build_override(hc, old, {"MENU_ID_代碼輸入": 221})
        assert err == "" and data["MENU_ID_同意書"] == 673 and data["MENU_ID_代碼輸入"] == 221

    def test_a_stale_file_is_not_revived(self, tool):
        """★過期檔的其他鍵是上一個校正時代的值★:合併後蓋上新戳記等於把舊值復活。"""
        stale = {"for_calibration": "1150629#3", "MENU_ID_同意書": 670}
        data, err = tool.build_override(hc, stale, {"MENU_ID_代碼輸入": 221})
        assert err == "" and "MENU_ID_同意書" not in data

    def test_the_tool_never_writes_the_calibrated_version(self, tool):
        """[外審 PR #10 第 1 輪] 按過「重新校正」的機器以基線檔為準,override 的
        CALIBRATED_VERSION 蓋不過它 → 工具不提供「記為校正版本」(會謊報成功),
        改版通知一律走主程式的「重新校正」。"""
        data, err = tool.build_override(hc, None, {"MENU_ID_同意書": 673})
        assert err == "" and "CALIBRATED_VERSION" not in data
        src = open(os.path.join(ROOT, TOOL_NAME), encoding="utf-8").read()
        assert '"CALIBRATED_VERSION"]' not in src and "重新校正" in src

    def test_nothing_to_write_is_rejected(self, tool):
        data, err = tool.build_override(hc, None, {})
        assert data == {} and err

    def test_a_bad_value_is_rejected_before_writing(self, tool):
        data, err = tool.build_override(hc, None, {"MENU_ID_同意書": 0})
        assert data == {} and "1..65535" in err


class TestStatusLine:
    def test_none(self, tool):
        assert "目前沒有" in tool.override_status(hc, None)["text"]

    def test_valid(self, tool):
        st = tool.override_status(hc, {"for_calibration": hc.override_marker(),
                                       "MENU_ID_同意書": 673})
        assert "生效中" in st["text"] and "同意書=673" in st["text"]

    def test_stale_or_broken_is_shown_as_invalid(self, tool):
        assert "無效" in tool.override_status(hc, {"for_calibration": "x#1",
                                                  "MENU_ID_同意書": 673})["text"]
        assert "無效" in tool.override_status(hc, {"_讀取失敗": "JSONDecodeError"})["text"]

    def test_a_broken_file_reads_as_invalid_not_as_missing(self, tool, clean_hc):
        path = hc.override_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("{壞掉")
        assert "無效" in tool.override_status(hc, tool.read_override(hc))["text"]


def test_report_text(tool):
    t = tool.report_text("1151020.01", {"MENU_ID_代碼輸入": 220, "MENU_ID_同意書": 672})
    assert "V.1151020.01" in t and "代碼輸入=220" in t and "同意書=672" in t


class TestEntryPoints:
    def _read(self, rel):
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            return f.read()

    def test_the_old_probe_is_gone_and_the_tool_is_in_the_app_folder(self):
        assert not os.path.exists(os.path.join(ROOT, "scripts", "test_yiling_menu_id.py"))
        assert os.path.isfile(os.path.join(ROOT, TOOL_NAME))
        assert os.path.isfile(os.path.join(ROOT, GUIDE_NAME))

    def test_the_double_click_entry_finds_exactly_this_tool(self):
        """`.cmd` 用 ASCII 萬用字元 HIS*ID*.py 找工具(批次檔原始碼不放中文指令)
        → 程式資料夾裡必須剛好只有它一支符合。"""
        cmd = self._read("修正HIS熱鍵ID.cmd")
        assert 'for %%F in ("%~dp0HIS*ID*.py") do set "SCRIPT=%%~fF"' in cmd
        assert "test_yiling_menu_id" not in cmd
        hits = [n for n in os.listdir(ROOT) if re.fullmatch(r"HIS.*ID.*\.py", n)]
        assert hits == [TOOL_NAME]
        for ln in cmd.splitlines():
            if "SCRIPT" in ln and not ln.lstrip().upper().startswith("REM"):
                assert ln.isascii(), ln

    def test_the_dev_entry_points_at_it_too(self):
        cmd = self._read(os.path.join("tools", "測試醫令選單ID.cmd"))
        assert "HIS*ID*.py" in cmd and "test_yiling_menu_id" not in cmd

    def test_both_files_are_shipped_by_the_online_updater(self):
        sm = self._read(os.path.join("scripts", "sync_manifest.py"))
        assert f'"{TOOL_NAME}"' in sm and f'"{GUIDE_NAME}"' in sm
        assert '"scripts/test_yiling_menu_id.py"' not in sm

    def test_the_guide_covers_the_steps(self):
        g = self._read(GUIDE_NAME)
        for must in ("修正HIS熱鍵ID.cmd", "送出測試", "套用修正", "重新開啟",
                     "清除快速修正", "F11", "重新校正"):
            assert must in g, must


def test_the_window_text_stays_inside_the_bmp():
    """舊版 Tk(< 8.6.10)遇到 BMP 以外的字元(多數 emoji)會直接丟例外 → 急救工具開不起來。"""
    src = open(os.path.join(ROOT, TOOL_NAME), encoding="utf-8").read()
    assert not [c for c in src if ord(c) > 0xFFFF]
