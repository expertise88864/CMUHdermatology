# -*- coding: utf-8 -*-
"""止掛門檻的設定安全性（外審第 5 輪 P2-09 / P2-10 / P2-11）。

三件事都屬於同一類：**程式替使用者做了他沒有要求、也看不見的決定**。

  * P2-09 只防「空字串變成 0」，卻放行使用者【直接輸入】的 0／負數 ——
    門檻 0 時 `count >= 0 - margin` 恆真，每一診都提醒。只堵了一半。
  * P2-10 打錯字時靜默改成原廠值或空字串 —— 使用者以為自己改了一個數字，
    實際上把提醒關掉了，或把自訂的 88 悄悄換回 59。
  * P2-11 新的 `alert_shen_enabled` 預設開 —— 全院診間機升級後每一台都寄。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from cmuh_common import settings_defaults as sd  # noqa: E402
from cmuh_common.app_settings import load_threshold_settings  # noqa: E402
from cmuh_common.threshold_policy import (  # noqa: E402
    MAX_THRESHOLD,
    MIN_THRESHOLD,
    build_doctor_threshold_map,
    is_near_alert_threshold,
    validate_threshold_entry,
)


class TestZeroAndNegativeAreRejected:
    """★P2-09★ 只防空字串不夠 —— 直接打 0 一樣會讓提醒恆真。"""

    def test_zero_is_rejected(self):
        value, err = validate_threshold_entry("chen_tue_night", "0")
        assert value is None and err, "門檻 0 被接受了 → 每一診都會提醒"

    def test_negative_is_rejected(self):
        for raw in ("-1", "-999"):
            value, err = validate_threshold_entry("chen_tue_night", raw)
            assert value is None and err, f"{raw} 被接受了"

    def test_why_it_matters(self):
        """釘住理由:門檻 0 時連 0 人都算「接近門檻」。"""
        assert is_near_alert_threshold(["晚上: 0人"], 1, {(1, "晚上"): 0}, margin=10)

    def test_a_threshold_at_or_below_the_margin_is_rejected(self):
        """門檻 ≤ margin(10) 時第一位病人就「接近門檻」→ 也要擋。"""
        value, err = validate_threshold_entry("chen_tue_night", "10")
        assert value is None and err
        assert MIN_THRESHOLD > 10, "下限必須大於 margin，否則提醒還是幾乎恆真"

    def test_absurdly_large_is_rejected(self):
        value, err = validate_threshold_entry("chen_tue_night",
                                              str(MAX_THRESHOLD + 1))
        assert value is None and err

    def test_normal_values_still_pass(self):
        """★反方向:合理的數字不可以被擋★"""
        for raw in (str(MIN_THRESHOLD), "59", "100", "129", str(MAX_THRESHOLD)):
            value, err = validate_threshold_entry("chen_tue_night", raw)
            assert err == "" and value == int(raw), f"{raw} 被誤擋:{err}"


class TestTyposAreNotSilentlyRewritten:
    """★P2-10★ 打錯字要當場說，不可以替使用者猜一個值存下去。"""

    def test_a_typo_is_an_error_not_a_fallback(self):
        value, err = validate_threshold_entry("chen_tue_night", "8O")
        assert value is None, (
            "★打錯字被靜默改掉了★ 使用者的 88 會變成原廠的 59，而他不會知道")
        assert "不是數字" in err

    def test_a_typo_on_a_key_without_a_default_is_also_an_error(self):
        """沒有原廠值的鍵更危險:靜默退成空 = 靜默把這個診次的提醒關掉。"""
        value, err = validate_threshold_entry("shen_mon_morning", "abc")
        assert value is None and err

    def test_blank_is_still_a_deliberate_no_threshold(self):
        """★反方向:留空不是錯誤★ 它是「這個診次不設門檻」的正常表達方式。"""
        for raw in ("", "   ", None):
            value, err = validate_threshold_entry("shen_mon_morning", raw)
            assert value == "" and err == ""


def test_save_rejects_before_writing_anything():
    """★接線★ 驗證必須排在【寫任何檔案之前】。

    第一版把驗證放在 r_doctor_settings.json 寫入之後 —— 「拒絕存檔」實際上
    會變成「存了一半」。這裡確認 `validate_threshold_entry` 出現在
    `save_all_settings` 裡第一個 `_atomic_write_json` 之前。
    """
    import ast
    import inspect
    import textwrap

    import main

    tree = ast.parse(textwrap.dedent(inspect.getsource(main.AutomationApp.save_all_settings)))
    first_validate = first_write = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id == "validate_threshold_entry" and first_validate is None:
            first_validate = node.lineno
        # [2026-08-06 外審 P1-07] 寫檔改成 _atomic_write_json_multi(三檔一起 commit)
        # → 這裡認任何 _atomic_write_json* 的呼叫,不綁死單一函式名。
        if node.func.id.startswith("_atomic_write_json") and first_write is None:
            first_write = node.lineno
    assert first_validate is not None, "存檔沒有經過門檻驗證"
    assert first_write is not None, "找不到寫檔點（測試失效了）"
    assert first_validate < first_write, (
        "★驗證排在寫檔之後★「拒絕存檔」會變成「存了一半」")


def test_an_invalid_entry_actually_aborts_the_save():
    """★接線★ 驗出錯誤之後必須【真的中止】存檔。

    突變驗證抓到的洞：把 `if bad_thresholds:` 改成 `if False:` —— 驗證照跑、
    錯誤照收集，然後什麼都沒發生（那一格被靜默跳過、保留舊值）。上面那支
    「驗證排在寫檔之前」照樣全綠。所以要確認那個分支會 return，而且
    分支條件真的是「有錯誤」。
    """
    import ast
    import inspect
    import textwrap

    import main

    tree = ast.parse(textwrap.dedent(
        inspect.getsource(main.AutomationApp.save_all_settings)))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If):
            continue
        names = {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}
        if "bad_thresholds" not in names:
            continue
        assert any(isinstance(n, ast.Return) for n in ast.walk(node)), (
            "★驗出不合法卻沒有中止存檔★ 那一格會被靜默跳過")
        # 而且要告訴使用者（不可以只寫 log 就默默不存）
        called = {n.func.attr for n in ast.walk(node)
                  if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        assert "showwarning" in called or "showerror" in called, (
            "拒絕存檔沒有告訴使用者 → 他會以為存好了")
        return
    raise AssertionError("找不到『門檻不合法就中止』的分支")


class TestTheNewAlertKeyInheritsWhoWasAlerting:
    """★P2-11★ 換醫師時，新鍵要繼承「這台是不是負責寄信的機器」。

    2026-08-05 把止掛對象從張廖年峰換成沈冠宇，於是**所有**舊機器的設定檔都
    沒有 `alert_shen_enabled` → 一律走原廠預設。兩種預設都不對：

      * 預設開 → 全院每一台診間機都寄一封（既有定案：多台同時跑會重複寄信）
      * 預設關 → 原本負責寄信的那台也靜悄悄不寄了，功能等於沒上線

    真正的資訊在舊鍵裡：這台原本有沒有在做止掛提醒。
    """

    def _load(self, tmp_path, payload):
        import json
        p = tmp_path / "threshold_settings.json"
        p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return load_threshold_settings(str(p))

    def test_a_machine_that_was_alerting_keeps_alerting(self, tmp_path):
        got = self._load(tmp_path, {"alert_chang_enabled": True})
        assert got["alert_shen_enabled"] is True, (
            "★原本負責寄信的那台升級後不寄了★ 使用者要求的提醒等於沒上線")

    def test_chen_only_machine_does_not_get_shen_turned_on(self, tmp_path):
        """★[2026-08-08 外審] 這個測試原本把錯誤語意釘成通過條件★

        沈冠宇接的是【張廖年峰】的位置,遷移就該是一對一。
        從「這台有沒有在提醒陳駿升」推導出「要不要提醒沈冠宇」,
        等於替使用者做了一個他從來沒做過的逐醫師選擇 ——
        一台設定成「只提醒陳駿升」的機器會被自動打開沈冠宇提醒。
        (代價是這台不會自動有沈冠宇提醒;程式會在 log 講清楚要去勾選,
         而不是替他決定。)
        """
        got = self._load(tmp_path, {"alert_chen_enabled": True})
        assert got.get("alert_shen_enabled") is not True, (
            "★從別位醫師的開關推導出沈冠宇提醒★ 那不是使用者做過的選擇")

    def test_the_retired_doctors_switch_is_migrated_one_to_one(self, tmp_path):
        """★正方向★ 原本啟用張廖年峰的機器,要由沈冠宇接手。"""
        got = self._load(tmp_path, {"alert_chang_enabled": True})
        assert got["alert_shen_enabled"] is True

    def test_a_machine_that_was_not_alerting_stays_quiet(self, tmp_path):
        """★核心★ 沒在寄的機器升級後不可以開始寄（多台會重複寄信）。"""
        got = self._load(tmp_path, {"alert_chang_enabled": False,
                                    "alert_chen_enabled": False})
        assert got["alert_shen_enabled"] is False

    def test_a_brand_new_install_is_quiet(self, tmp_path):
        got = self._load(tmp_path, {})
        assert got["alert_shen_enabled"] is False

    def test_an_explicit_choice_is_never_overridden(self, tmp_path):
        """使用者自己勾過(檔案裡有這個鍵)→ 永遠以他的選擇為準。"""
        got = self._load(tmp_path, {"alert_shen_enabled": False,
                                    "alert_chang_enabled": True})
        assert got["alert_shen_enabled"] is False, "使用者關掉的又被推導打開了"

    def test_the_factory_default_is_off(self):
        """原廠預設要與『多台同時跑會重複寄信 → 預設關』這條既有定案一致。"""
        assert sd.default_threshold_settings()["alert_shen_enabled"] is False


def test_shen_thresholds_unchanged_by_this_batch():
    """★反方向:這一批不可以動到上一批定好的門檻★"""
    assert build_doctor_threshold_map("沈冠宇", {}) == {(2, "晚上"): 100}
    assert build_doctor_threshold_map("陳駿升", {}) == {
        (0, "下午"): 69, (1, "晚上"): 59, (3, "上午"): 54, (3, "下午"): 69,
    }
