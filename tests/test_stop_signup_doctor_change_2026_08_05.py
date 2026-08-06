# -*- coding: utf-8 -*-
"""止掛提醒對象變更(2026-08-05 使用者定案):移除張廖年峰、加入沈冠宇。

【使用者要的】
  * 刪除張廖年峰的止掛提醒(設定頁那一排也要拿掉),但**整套止掛邏輯保留**。
  * 陳駿升維持原樣(門檻、開關、行為都不動)。
  * 新增沈冠宇:一早/一午/三午【先不預設止掛人數】,三晚預設 100 人。

【本檔為什麼存在】
這種「換一個對象」的變更,最容易壞在**接線**而不是邏輯:門檻表改好了,但
`main.py` 裡讀開關、建 threshold map、比門檻的那幾處還指著舊醫師 —— 邏輯測試
全綠,實機不提醒。故本檔把接線本身當成受測對象(AST/來源檢查那幾支),
把單一函式的行為留給 test_threshold_policy.py。

★「先不預設」的陷阱★ 沒有預設 ≠ 門檻 0。門檻 0 會讓「接近門檻」恆真 →
「還沒設定」變成「每一診都提醒」。存檔那一格原本會把空框寫成 0,見
`test_empty_box_is_saved_as_no_threshold_not_zero`。
"""
import ast
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402
from cmuh_common import settings_defaults as sd  # noqa: E402
from cmuh_common.threshold_policy import (  # noqa: E402
    DEFAULT_THRESHOLDS,
    build_doctor_threshold_map,
    is_near_alert_threshold,
    validate_threshold_entry,
)

_MAIN_SRC = open(main.__file__, encoding="utf-8").read()
_MAIN_TREE = ast.parse(_MAIN_SRC)


# ── 存檔:留空不可變成 0 ────────────────────────────────────────────────────
def test_empty_box_is_saved_as_no_threshold_not_zero():
    """空框存成 "" → build_doctor_threshold_map 跳過該診次 → 不提醒。

    存成 0 的話:0 - margin(10) = -10,任何人數都 >= -10 → 每一診都提醒。
    """
    for key in ("shen_mon_morning", "shen_mon_afternoon", "shen_wed_afternoon"):
        for blank in ("", "   ", None):
            assert validate_threshold_entry(key, blank) == ("", "")
        # 沒有預設的鍵,打錯字也不可生出數字
        # ★[2026-08-05 外審第 5 輪 P2-10]★ 而且不再靜默退成空字串 ——
        #   靜默退空 = 靜默把這個診次的提醒關掉。現在是【拒絕存檔】。
        value, err = validate_threshold_entry(key, "abc")
        assert value is None and err

    # 存出來的東西真的不會產生門檻
    saved = {k: validate_threshold_entry(k, "")[0] for k in
             ("shen_mon_morning", "shen_mon_afternoon", "shen_wed_afternoon")}
    assert build_doctor_threshold_map("沈冠宇", saved) == {(2, "晚上"): 100}


def test_clearing_a_defaulted_box_disables_that_session():
    """清空一格 = 停用該診次的提醒,不可被原廠預設復活。

    ★這條在有預設值的鍵上才看得出來★ 沈冠宇那三格本來就沒預設,「留空→""」
    與「留空→預設」在它們身上結果相同;要用陳駿升的鍵才能把兩者分開。
    語意選一致的那個:框裡是空的,就代表沒有門檻 —— 否則使用者會遇到
    「我把它清掉了,它還是在提醒」而且看不出原因。
    """
    assert DEFAULT_THRESHOLDS["chen_tue_night"] == 59
    assert validate_threshold_entry("chen_tue_night", "") == ("", ""), \
        "清空有預設值的格子也要真的清掉(否則那格永遠關不掉)"
    assert build_doctor_threshold_map("陳駿升", {"chen_tue_night": ""}) == {
        (0, "下午"): 69, (3, "上午"): 54, (3, "下午"): 69,
    }, "被清空的診次要從門檻表消失,其餘不受影響"


def test_normal_input_is_accepted():
    assert validate_threshold_entry("chen_tue_night", "88") == (88, "")
    assert validate_threshold_entry("chen_tue_night", " 88 ") == (88, "")


def test_save_site_uses_the_validator():
    """★接線★ 存檔那一格必須走 validate_threshold_entry。

    直接呼叫上面那支函式的測試,在 `save_all_settings` 改回 `int(...)` 之後
    照樣全綠 —— 這一支才是會轉紅的那一支。
    """
    fn = next(n for n in ast.walk(_MAIN_TREE)
              if isinstance(n, ast.FunctionDef) and n.name == "save_all_settings")
    calls = [n for n in ast.walk(fn)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id == "validate_threshold_entry"]
    assert len(calls) == 1, "門檻存檔必須經過 validate_threshold_entry"
    # 且不可再有「寫死 0 當後備」的舊寫法
    for n in ast.walk(fn):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "get" and len(n.args) == 2
                and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "DEFAULT_THRESHOLDS"):
            assert not (isinstance(n.args[1], ast.Constant) and n.args[1].value == 0), \
                "門檻不可以 0 當後備(0 會讓提醒恆真)"


# ── 接線:main.py 已經完全改用沈冠宇 ────────────────────────────────────────
def test_no_chang_threshold_wiring_left_in_main():
    """張廖年峰的止掛接線要整組消失(開關、門檻鍵、設定頁那一排)。

    ★不是掃「張廖年峰」這四個字★ —— 他仍出現在醫師名單/查詢批次/註解裡,
    那些是別的功能,刻意保留。這裡只掃止掛專用的識別字。
    """
    for token in ("alert_chang_enabled", "val_alert_chang",
                  "chang_mon_night", "chang_thu_morning",
                  "chang_thu_night", "chang_fri_afternoon"):
        assert token not in _MAIN_SRC, f"止掛殘留:{token}"
    assert not [k for k in DEFAULT_THRESHOLDS if k.startswith("chang_")]
    assert "alert_chang_enabled" not in sd.default_threshold_settings()


def test_every_alert_gate_reads_the_shen_toggle():
    """三條止掛路徑(行事曆比門檻、遠期背景掃描、設定頁同步影子變數)都要接上新開關。

    以 AST 找 `self.alert_shen_enabled` 的讀取點,數量要與陳駿升那支一致 ——
    兩位醫師走的是同一組程式碼路徑,少一處就是少一條路徑沒接上。
    """
    def _reads(attr):
        return [n for n in ast.walk(_MAIN_TREE)
                if isinstance(n, ast.Attribute) and n.attr == attr
                and isinstance(n.value, ast.Name) and n.value.id == "self"]

    shen = _reads("alert_shen_enabled")
    chen = _reads("alert_chen_enabled")
    assert len(shen) == len(chen), \
        f"沈冠宇的開關接線數({len(shen)})與陳駿升({len(chen)})不一致 → 有路徑沒接上"
    assert len(shen) >= 5, "至少:宣告、影子變數×2、設定頁 checkbox、比門檻、遠期掃描"


def test_threshold_lookups_name_exactly_the_two_doctors():
    """★接線★ 「拿誰的門檻表」與「拿來比對的醫師名」必須都是沈冠宇+陳駿升。

    上一支只數開關的讀取次數 —— 把 `doc_name == "沈冠宇"` 改回舊名字,次數不變、
    照樣全綠,但實機再也不會提醒。這裡改成看實際用到的醫師名字串本身。
    """
    names = set()
    for n in ast.walk(_MAIN_TREE):
        # self._get_doctor_threshold_map("<名字>")
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "_get_doctor_threshold_map"
                and n.args and isinstance(n.args[0], ast.Constant)
                and isinstance(n.args[0].value, str)):
            names.add(n.args[0].value)
        # doctor_threshold_maps["<名字>"]
        if (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name)
                and n.value.id == "doctor_threshold_maps"
                and isinstance(n.slice, ast.Constant)
                and isinstance(n.slice.value, str)):
            names.add(n.slice.value)
    # [2026-08-06 使用者] 新增黃建仁(三早60)/謝佳陵(四早/四晚/五午75) → 四位
    assert names == {"沈冠宇", "陳駿升", "黃建仁", "謝佳陵"},         f"止掛門檻查詢用到的醫師名:{sorted(names)}"

    # 比門檻的分支條件裡拿來跟 doc_name 比的名字,也必須是同兩位
    compared = {c.value for n in ast.walk(_MAIN_TREE)
                if isinstance(n, ast.Compare)
                and isinstance(n.left, ast.Name) and n.left.id == "doc_name"
                for c in n.comparators
                if isinstance(c, ast.Constant) and isinstance(c.value, str)}
    assert compared == {"沈冠宇", "陳駿升", "黃建仁", "謝佳陵"},         f"與 doc_name 比對的名字:{sorted(compared)}"


def test_settings_page_shows_shen_row_with_four_sessions():
    """設定頁必須長出沈冠宇那一排,四個診次的框都在(一早/一午/三午留空)。"""
    i = _MAIN_SRC.index("shen_labels = {")
    row = _MAIN_SRC[i:_MAIN_SRC.index("}", i)]
    for key, label in (("shen_mon_morning", "一早"), ("shen_mon_afternoon", "一午"),
                       ("shen_wed_afternoon", "三午"), ("shen_wed_night", "三晚")):
        assert key in row and label in row, f"設定頁缺 {key}({label})"
    assert "啟用 [沈冠宇]" in _MAIN_SRC
    assert "啟用 [張廖年峰]" not in _MAIN_SRC, "設定頁不可再有張廖年峰那一排"


# ── 行為:三晚 100、其餘三診次沉默 ──────────────────────────────────────────
def test_only_wed_night_alerts_out_of_the_box():
    """出廠狀態下,只有三晚會提醒;一早/一午/三午再多人都不提醒。"""
    tmap = build_doctor_threshold_map("沈冠宇", sd.default_threshold_settings())
    assert tmap == {(2, "晚上"): 100}

    # 三晚:90 人(門檻-10)開始提醒
    assert is_near_alert_threshold(["晚上: 90人"], 2, tmap, margin=10)
    assert not is_near_alert_threshold(["晚上: 89人"], 2, tmap, margin=10)
    # 一早/一午/三午:沒有門檻 → 掛爆也不提醒
    assert not is_near_alert_threshold(["上午: 999人"], 0, tmap, margin=10)
    assert not is_near_alert_threshold(["下午: 999人"], 0, tmap, margin=10)
    assert not is_near_alert_threshold(["下午: 999人"], 2, tmap, margin=10)


def test_shen_alert_default_is_off_with_inheritance():
    """★[2026-08-05 外審第 5 輪 P2-11 推翻我當天的決定]★

    我原本設成 True,理由是「使用者要的是新增提醒,不是新增一個要自己去勾的
    選項」。那是錯的 —— main.py 同一段既有註解已經定案「多台電腦同時跑會重複
    寄信 → 預設關」,而全院每一台診間機都會載到這個新鍵。
    改成:原廠預設關,但舊設定檔裡【原本就在做止掛提醒】的那台會繼承成開
    (見 test_threshold_settings_safety_2026_08_05.py)。
    """
    assert sd.default_threshold_settings()["alert_shen_enabled"] is False


def test_chen_thresholds_untouched():
    """陳駿升整組不動(使用者明確要求保留原本的陳駿升醫師)。"""
    assert build_doctor_threshold_map("陳駿升", {}) == {
        (0, "下午"): 69, (1, "晚上"): 59, (3, "上午"): 54, (3, "下午"): 69,
    }
