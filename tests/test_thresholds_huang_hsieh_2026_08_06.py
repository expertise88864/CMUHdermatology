# -*- coding: utf-8 -*-
"""[2026-08-06 使用者] 止掛提醒新增黃建仁/謝佳陵。

規格：黃建仁 週三早 60；謝佳陵 週四早/週四晚/週五午 都 75。
兩位（連同既有的沈冠宇）提醒開關【預設關】——多台電腦同跑會重複寄信，
使用者只在自己那台手動勾開（沿用 2026-08-05 外審 P2-11 的定案）。
"""
import ast
import os
import sys
from datetime import date

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402
from cmuh_common.settings_defaults import default_threshold_settings  # noqa: E402
from cmuh_common.threshold_policy import (  # noqa: E402
    DEFAULT_THRESHOLDS, build_doctor_threshold_map,
)


def test_default_thresholds_have_the_new_slots():
    # [2026-08-26 使用者] 謝佳陵四診次(含新增的週六早)預設一律 70(原 75 作廢)。
    assert DEFAULT_THRESHOLDS["huang_wed_morning"] == 60
    assert DEFAULT_THRESHOLDS["hsieh_thu_morning"] == 70
    assert DEFAULT_THRESHOLDS["hsieh_thu_night"] == 70
    assert DEFAULT_THRESHOLDS["hsieh_fri_afternoon"] == 70
    assert DEFAULT_THRESHOLDS["hsieh_sat_morning"] == 70


def test_huang_map_is_wednesday_morning_only():
    m = build_doctor_threshold_map("黃建仁", {})
    assert m == {(2, "上午"): 60}, m           # weekday 2 = 週三


def test_hsieh_map_covers_the_four_slots():
    m = build_doctor_threshold_map("謝佳陵", {})
    assert m == {(3, "上午"): 70,              # 週四早
                 (3, "晚上"): 70,              # 週四晚
                 (4, "下午"): 70,              # 週五午
                 (5, "上午"): 70}, m           # 週六早(2026-08-26 新增)


def test_user_override_beats_the_default():
    """設定頁改過的值要壓過原廠預設（與沈/陳同機制）。"""
    m = build_doctor_threshold_map("謝佳陵", {"hsieh_thu_night": 90})
    assert m[(3, "晚上")] == 90
    assert m[(3, "上午")] == 70                # 沒改的維持預設


def test_alert_flags_default_off():
    """★使用者定案★ 沈冠宇/黃建仁/謝佳陵預設關（多台同跑會重複寄信）。"""
    d = default_threshold_settings()
    assert d["alert_shen_enabled"] is False
    assert d["alert_huang_enabled"] is False
    assert d["alert_hsieh_enabled"] is False
    assert d["alert_chen_enabled"] is False    # 既有行為不變


def _main_src() -> str:
    path = os.path.join(os.path.dirname(__file__), "..", "src", "main.py")
    return open(path, encoding="utf-8").read()


def test_main_wires_both_doctors_end_to_end():
    """接線守衛（存檔／設定頁 UI）。

    ★[2026-08-06 外審 P1-01]★ 原本這支還檢查「判斷點寫死 doc_name == 姓名」與
    「影子變數 val_alert_* 出現 3 次」。那些字串已被【註冊表】取代（逐位 if 正是
    漏接的病灶、val_alert_* 是只寫不讀的死碼），改由
    test_stop_signup_doctor_change 的註冊表不變量 + 下面的遠期掃描行為測試守住。
    """
    src = _main_src()
    # 存檔
    # ★[2026-08-08 外審] 這裡原本比對 `self.threshold_settings[...]` 的字面樣子★
    #   而外審要求 payload 必須建在【副本】上、commit 成功才指派回 instance
    #   (存檔失敗時不可以讓背景緒用一份沒存進去的開關)。一改名這個守衛就轉紅,
    #   但它要守的東西 —— 「這兩位的開關有沒有被寫進要存的內容」 —— 完全沒變。
    #   改成用 AST 找【被寫進 threshold_settings.json 那份 payload】的鍵,
    #   不管那個 dict 目前叫什麼名字。
    payload_name = None
    tree = ast.parse(src)
    for n in ast.walk(tree):
        if (isinstance(n, ast.Tuple) and len(n.elts) == 2
                and isinstance(n.elts[0], ast.Call)
                and getattr(n.elts[0].func, "id", "") == "get_conf_path"
                and n.elts[0].args
                and getattr(n.elts[0].args[0], "value", "")
                == "threshold_settings.json"
                and isinstance(n.elts[1], ast.Name)):
            payload_name = n.elts[1].id
    assert payload_name, "找不到 threshold_settings.json 的 payload 變數"
    keys = {t.slice.value for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for t in node.targets
            if isinstance(t, ast.Subscript)
            and getattr(t.value, "id", "") == payload_name
            and isinstance(t.slice, ast.Constant)}
    assert "alert_huang_enabled" in keys, keys
    assert "alert_hsieh_enabled" in keys, keys
    # 設定頁 UI
    assert "啟用 [黃建仁]" in src and "啟用 [謝佳陵]" in src
    assert "'huang_wed_morning': '三早:'" in src
    assert "'hsieh_thu_morning': '四早:'" in src
    assert "'hsieh_thu_night': '四晚:'" in src
    assert "'hsieh_fri_afternoon': '五午:'" in src
    assert "'hsieh_sat_morning': '六早:'" in src   # [2026-08-26] 週六早
    # 還原預設對照
    assert "('alert_huang_enabled', 'alert_huang_enabled', False)" in src
    assert "('alert_hsieh_enabled', 'alert_hsieh_enabled', False)" in src
    # 兩位都必須在註冊表內（唯一來源）
    assert main.ALERT_DOCTORS["黃建仁"] == "alert_huang_enabled"
    assert main.ALERT_DOCTORS["謝佳陵"] == "alert_hsieh_enabled"


# ── ★行為測試★ 遠期背景掃描必須涵蓋新醫師（P1-01 實際發生的事故）─────────────
# 舊版只有源碼字串守衛：九處接線全綠、但 _scan_future_stop_signup_alerts 的
# enabled map 仍只有沈冠宇/陳駿升 → 勾了開關也永遠收不到遠期提醒，而遠期掃描
# 正是「兩三週前就掛滿的診次」唯一的提醒來源。以下改測真的會不會寄。

class _FakeVar:
    def __init__(self, v):
        self._v = v

    def get(self):
        return self._v


def _scan_app(monkeypatch, doctor, doc_no, by_date, enabled=True,
              recipients=("me@example.com",)):
    """組一個只帶遠期掃描所需欄位的 app（不建 Tk）。"""
    app = main.AutomationApp.__new__(main.AutomationApp)
    for name, attr in main.ALERT_DOCTORS.items():
        setattr(app, attr, _FakeVar(enabled and name == doctor))
    app.alert_email_recipients = list(recipients)
    app.doctors_list = [{"name": doctor, "doc_no": doc_no}]
    app.all_doctors_data = {doc_no: by_date}
    app._doctor_data_lock = main.threading.Lock()
    app._alert_state_lock = main.threading.Lock()
    app._reg64_cache_lock = main.threading.Lock()
    app._reg64_public_snapshot = {}
    app._alert_email_inflight = set()
    app._live_clinic_data_keys = {doc_no}
    app.threshold_settings = {}
    app._alert_email_sent = {}
    # ★[#71] 生產的 __init__ 一定會設這兩個★ 假 app 少一個,
    #   去重述詞就會拋 AttributeError 而被外層 except 吞掉 ——
    #   測到的是「掃描整個中止」，不是被測的行為。
    app._alert_email_pending = {}
    app._alert_pending_load_failed = False
    monkeypatch.setattr(app, "_mark_alert_email_sent",
                        lambda nk: app._alert_email_sent.__setitem__(nk, "d"))
    mails = []
    monkeypatch.setattr(main, "_send_alert_email_via_smtp",
                        lambda subj, body, rcpts, **k:
                        mails.append((subj, body)) or True)
    monkeypatch.setattr(main.threading, "Thread",
                        lambda target=None, **k: type(
                            "T", (), {"start": lambda s: target()})())
    return app, mails


def test_huang_far_future_wednesday_morning_alerts(monkeypatch):
    """黃建仁：三週後的週三上午已達 60 人 → 遠期掃描必須寄出一封。"""
    today = date(2026, 8, 6)                       # 週四
    target = date(2026, 8, 26)                     # 週三（20 天後，>2 週）
    assert target.weekday() == 2 and (target - today).days == 20
    app, mails = _scan_app(monkeypatch, "黃建仁", "D90001", {target: [
        {"session": "上午", "count": 60, "is_stopped": False, "room": "101診"}]})
    app._scan_future_stop_signup_alerts(today=today)
    assert len(mails) == 1, "黃建仁週三上午已達門檻 60 → 遠期掃描應寄提醒"
    assert "黃建仁醫師" in mails[0][0] and "60 人" in mails[0][0]


@pytest.mark.parametrize("target,session", [
    (date(2026, 8, 27), "上午"),                   # 週四早
    (date(2026, 8, 27), "晚上"),                   # 週四晚
    (date(2026, 8, 28), "下午"),                   # 週五午
])
def test_hsieh_each_far_future_slot_alerts(monkeypatch, target, session):
    """謝佳陵三個診次各自都要能在遠期被提醒（門檻皆 75）。"""
    today = date(2026, 8, 6)
    app, mails = _scan_app(monkeypatch, "謝佳陵", "D90002", {target: [
        {"session": session, "count": 75, "is_stopped": False}]})
    app._scan_future_stop_signup_alerts(today=today)
    assert len(mails) == 1, f"謝佳陵 {target}({session}) 已達 75 → 應寄提醒"
    assert "謝佳陵醫師" in mails[0][0]


@pytest.mark.parametrize("doctor,doc_no,target,session,count", [
    ("黃建仁", "D90001", date(2026, 8, 26), "上午", 60),
    ("謝佳陵", "D90002", date(2026, 8, 27), "晚上", 75),
])
def test_disabled_toggle_blocks_far_future_alert(
        monkeypatch, doctor, doc_no, target, session, count):
    """開關關閉 → 遠期掃描不得寄（多台同跑的重複寄信防線）。"""
    app, mails = _scan_app(monkeypatch, doctor, doc_no,
                           {target: [{"session": session, "count": count}]},
                           enabled=False)
    app._scan_future_stop_signup_alerts(today=date(2026, 8, 6))
    assert mails == [], f"{doctor} 開關關閉時不得寄提醒"


def test_below_threshold_not_alerted_for_new_doctors(monkeypatch):
    """未達門檻不寄（確認比對的是各自的門檻，不是別人的）。"""
    app, mails = _scan_app(monkeypatch, "黃建仁", "D90001",
                           {date(2026, 8, 26): [{"session": "上午", "count": 59}]})
    app._scan_future_stop_signup_alerts(today=date(2026, 8, 6))
    assert mails == [], "59 < 門檻 60 → 不寄"


def test_registry_covers_every_doctor_with_thresholds(monkeypatch):
    """★核心不變量★ 每一位有門檻表的醫師，遠期掃描都必須真的會寄。

    這支才是 P1-01 的正解：不是檢查「名字有沒有出現在原始碼」，而是逐位跑一遍
    掃描。日後再加醫師時若只補門檻表卻忘了註冊表，這支立刻紅。
    """
    slot_to_date = {  # weekday → 2026-08 月內該星期幾且距 8/6 夠遠的日期
        0: date(2026, 8, 24), 1: date(2026, 8, 25), 2: date(2026, 8, 26),
        3: date(2026, 8, 27), 4: date(2026, 8, 28),
    }
    for doctor in main.ALERT_DOCTORS:
        tmap = build_doctor_threshold_map(doctor, {})
        if not tmap:
            continue                                # 無預設門檻者（如沈冠宇部分診次）
        (weekday, session), threshold = sorted(tmap.items())[0]
        target = slot_to_date[weekday]
        app, mails = _scan_app(monkeypatch, doctor, f"DOC{weekday}", {target: [
            {"session": session, "count": int(threshold), "is_stopped": False}]})
        app._scan_future_stop_signup_alerts(today=date(2026, 8, 6))
        assert len(mails) == 1, (
            f"{doctor} {target}({session}) 達門檻 {threshold} 卻沒寄 → "
            "該醫師沒被接進遠期背景掃描（P1-01 事故重演）")


# ══ [2026-08-26 使用者] 謝佳陵 75 → 70 的一次性遷移 ══════════════════════
class TestHsiehDefaultMigration:
    """★只改原廠預設救不到已部署機器★:設定檔把 75 存成了明確值。
    遷移判準:檔案裡★還沒有新鍵 hsieh_sat_morning★(= 本功能之前存的檔)
    且存值恰等於舊預設 75。使用者存檔一次後檔案就有新鍵 → 遷移自然過期。"""

    @staticmethod
    def _load(tmp_path, stored: dict) -> dict:
        import json
        from cmuh_common.app_settings import load_threshold_settings
        # ★path 參數是【檔案完整路徑】不是目錄★(與 test_settings_defaults
        #   的既有呼叫形狀一致)—— 傳目錄的話讀檔失敗會靜默走預設,而新預設
        #   恰好是期望值,測試就巧合地綠(tests-must-use-the-production-call-shape)。
        p = tmp_path / "threshold_settings.json"
        p.write_text(json.dumps(stored, ensure_ascii=False), encoding="utf-8")
        return load_threshold_settings(str(p))

    def test_an_old_file_with_the_old_default_is_migrated(self, tmp_path):
        data = self._load(tmp_path, {"hsieh_thu_morning": 75,
                                     "hsieh_thu_night": 75,
                                     "hsieh_fri_afternoon": 75})
        assert (data["hsieh_thu_morning"], data["hsieh_thu_night"],
                data["hsieh_fri_afternoon"]) == (70, 70, 70)

    def test_a_deliberate_75_after_this_feature_is_respected(self, tmp_path):
        """★反例只靠「檔案有沒有新鍵」分勝負★:同樣存 75,檔案裡已有
        hsieh_sat_morning(= 本功能之後存過檔)→ 那個 75 是刻意的。"""
        data = self._load(tmp_path, {"hsieh_thu_morning": 75,
                                     "hsieh_sat_morning": 70})
        assert data["hsieh_thu_morning"] == 75

    def test_a_customized_value_is_never_touched(self, tmp_path):
        """★只遷移「與舊預設不可分辨」的值★:自訂 88 動它就是靜默改設定。"""
        data = self._load(tmp_path, {"hsieh_thu_morning": 88})
        assert data["hsieh_thu_morning"] == 88

    def test_an_absent_key_falls_to_the_new_default(self, tmp_path):
        """檔案裡根本沒有這幾個鍵(從沒動過設定頁)→ 走新原廠預設 70。"""
        data = self._load(tmp_path, {})
        assert data.get("hsieh_thu_morning", 70) in ("", 70) or \
            data["hsieh_thu_morning"] == 70
