# -*- coding: utf-8 -*-
"""HIS 寫入面契約金絲雀。

[2026-07-16] 初版:疑似院方改版時 fail-closed 停止自動寫入 + 跳警告視窗。
[2026-07-17 使用者定案] 改為【偵測 + 寄信通知一次、不擋、不跳窗】:實務上「誤擋 + 每按一次
  跳窗」比偶發改版更難用;改版風險由醫師現場判斷兜底(發現功能異常自行停用)。偵測集中在
  _sample_his_write_contract(找到主視窗時),寫入路徑不再有 gate。通知每個現況版本只寄一次,
  丟背景 daemon 緒寄(SMTP 逾時不可卡熱鍵)。校正基線 _HIS_CALIBRATED_VERSION=1150713
  (2026-07-13 HIS V.1150713.02 改版、使用者實測選單 id 仍正常)。
採樣只用主視窗 title 版本號(選單多 owner-draw、動態文字讀不到 → 版本字串最可靠)。
"""
import inspect
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402
from cmuh_common import contract_canary as cc  # noqa: E402


def _verdict(monkeypatch, title, baseline_fp=None):
    """以指定基線即時裁決(隔離基線檔),回 CanaryVerdict(純函式,不碰全域)。"""
    if baseline_fp is None:
        baseline_fp = {"title_version": main._HIS_CALIBRATED_VERSION}
    monkeypatch.setattr(main, "_his_write_baseline_fp", lambda: baseline_fp)
    return main._his_write_verdict_for(title)


# ── 採樣裁決(純函式 _his_write_verdict_for) ─────────────────────────────────
def test_verdict_ok_on_matching_version(monkeypatch):
    # 現行校正版本 1150713(2026-07-13 改版,實測正常)
    assert _verdict(monkeypatch, "西醫門診醫師作業 V.1150713.02").status == cc.STATUS_OK


def test_verdict_drift_on_version_shift(monkeypatch):
    assert _verdict(monkeypatch, "西醫門診醫師作業 V.1150701.01").status == cc.STATUS_DRIFT


def test_verdict_unknown_when_no_version_string(monkeypatch):
    assert _verdict(monkeypatch, "西醫門診醫師作業").status == cc.STATUS_UNKNOWN


def test_verdict_uses_calibrated_baseline_from_file(monkeypatch):
    # 使用者校正過的基線(新版本)→ 該版本變 OK
    v = _verdict(monkeypatch, "西醫門診醫師作業 V.1150701.01",
                 baseline_fp={"title_version": "1150701"})
    assert v.status == cc.STATUS_OK


def test_no_mutable_verdict_globals():
    # [codex P2] 不再保留可變全域裁決/指紋(自足採樣,免競態)
    assert not hasattr(main, "_his_write_verdict")
    assert not hasattr(main, "_his_current_fp")


def test_his_canary_policy_is_notify_only_and_routed():
    # [P2-04] HIS 寫入面政策 = NOTIFY_ONLY,且【單一可見宣告】;偵測走 policy_action 決策,
    # 不是散在各處直接判 is_drift 再私接行為。
    assert main._HIS_CANARY_POLICY == cc.POLICY_NOTIFY_ONLY
    src = inspect.getsource(main._sample_his_write_contract)
    assert "_canary_policy_action(v.status, _HIS_CANARY_POLICY)" in src, \
        "偵測應由政策 × 裁決映射決定動作(政策與裁決分離)"


# ── 改版偵測 = 通知不擋(_sample_his_write_contract → _notify_his_drift) ──────────
class _SyncThread:
    """把 threading.Thread 換成同步執行,讓寄信在測試內即時發生、可斷言(不等背景緒)。"""
    def __init__(self, target=None, daemon=None, name=None, **kw):
        self._t = target

    def start(self):
        if self._t:
            self._t()


def _notify_env(monkeypatch, baseline_fp=None):
    """隔離通知狀態:重置去重集合/一次性 log 旗標,寄信同步化並改記錄器。回 sent list。"""
    if baseline_fp is None:
        baseline_fp = {"title_version": main._HIS_CALIBRATED_VERSION}
    monkeypatch.setattr(main, "_his_write_baseline_fp", lambda: baseline_fp)
    monkeypatch.setattr(main, "_his_drift_notified_versions", set())
    monkeypatch.setattr(main, "_his_drift_inflight_versions", set())
    monkeypatch.setattr(main, "_his_canary_warned", False)
    monkeypatch.setattr(main.threading, "Thread", _SyncThread)
    monkeypatch.setattr(main, "_load_alert_recipients", lambda: ["dev@example.com"])
    sent = []
    monkeypatch.setattr(main, "_send_alert_email_via_smtp",
                        lambda subject, body, recipients, **k:
                        sent.append((subject, recipients)) or True)
    return sent


def test_drift_notifies_once_per_version_and_does_not_return_block(monkeypatch):
    # 偵測到改版 → 寄一次信;同版本再採樣不重寄(不洗版);不同新版本才再寄一次。
    sent = _notify_env(monkeypatch)
    assert main._sample_his_write_contract("西醫門診醫師作業 V.1150701.01") is None  # 不擋、無回值
    assert len(sent) == 1 and "1150701" in sent[0][0]
    main._sample_his_write_contract("西醫門診醫師作業 V.1150701.01")   # 同版本
    assert len(sent) == 1, "同一版本只寄一次(避免洗版)"
    main._sample_his_write_contract("西醫門診醫師作業 V.1150702.01")   # 另一新版本
    assert len(sent) == 2, "偵測到不同新版本才再寄一次"


def test_ok_and_unknown_do_not_notify(monkeypatch):
    sent = _notify_env(monkeypatch)
    main._sample_his_write_contract("西醫門診醫師作業 V.1150713.02")   # OK
    main._sample_his_write_contract("西醫門診醫師作業")               # 無版本→UNKNOWN
    assert sent == [], "契約一致/採不到版本都不寄信"


def test_notify_skips_when_no_recipients(monkeypatch):
    # 無收件人 → 不寄、不報錯(仍不擋)
    sent = _notify_env(monkeypatch)
    monkeypatch.setattr(main, "_load_alert_recipients", lambda: [])
    main._sample_his_write_contract("西醫門診醫師作業 V.1150701.01")
    assert sent == []


def test_notify_retries_until_send_succeeds(monkeypatch):
    # [codex] 「已通知」只在寄信成功後才記:寄失敗 → 下次找視窗仍會重試,不會因一次暫時性
    # SMTP 失敗就整個 process 再也不通知。
    sent = _notify_env(monkeypatch)
    outcome = {"ok": False}
    monkeypatch.setattr(main, "_send_alert_email_via_smtp",
                        lambda s, b, r, **k: sent.append(s) or outcome["ok"])
    main._sample_his_write_contract("西醫門診醫師作業 V.1150701.01")   # 寄失敗(False)
    assert len(sent) == 1
    assert "1150701" not in main._his_drift_notified_versions, "寄失敗不得標記已通知"
    main._sample_his_write_contract("西醫門診醫師作業 V.1150701.01")   # 重試
    assert len(sent) == 2, "上次寄失敗 → 應重試"
    outcome["ok"] = True
    main._sample_his_write_contract("西醫門診醫師作業 V.1150701.01")   # 這次成功
    assert len(sent) == 3 and "1150701" in main._his_drift_notified_versions
    main._sample_his_write_contract("西醫門診醫師作業 V.1150701.01")   # 成功後不再寄
    assert len(sent) == 3, "寄成功後同版本不再寄"


def test_dedup_uses_full_version_after_recalibration(monkeypatch):
    # [codex] 基線帶尾碼(實機重新校正後)時,同主版本的點版(.01/.02)是【不同】通知,
    # 不可因主版本相同而被去重吃掉。
    sent = _notify_env(monkeypatch,
                       baseline_fp={"title_version": "1150713",
                                    "title_version_full": "1150713.02"})
    main._sample_his_write_contract("西醫門診醫師作業 V.1150714.01")
    main._sample_his_write_contract("西醫門診醫師作業 V.1150714.02")   # 同主版本、不同點版
    assert len(sent) == 2, "同主版本的不同點版應各通知一次"


def test_no_blocking_write_gate_anywhere():
    # [使用者定案] 舊的 fail-closed 寫入閘門已整支移除;寫入路徑不得再引用它。
    assert not hasattr(main, "_his_write_contract_ok"), "阻擋式寫入 gate 應已移除"
    for fn in (main._script_code_input_adaptive, main._update_uvb_dose_core,
               main._f11_快速完成_main, main.script_F9_F10_consent_form_adaptive):
        assert "_his_write_contract_ok" not in inspect.getsource(fn), \
            f"{fn.__name__} 不應再有阻擋式 gate(改為偵測+通知不擋)"


def test_sampling_wired_into_find_window():
    src = inspect.getsource(main._find_hospital_main_window)
    assert "_sample_his_write_contract" in src, "找到主視窗時應採樣 HIS 寫入契約(偵測+通知)"


# ── 版本尾碼(.01/.02):安全 opt-in DRIFT ─────────────────────────────────────
def test_suffix_ignored_when_baseline_has_no_suffix(monkeypatch):
    # 隱性硬編碼基線只有主版本(無尾碼)→ 尾碼【不】比對(不會一開機把功能全判成改版)。
    monkeypatch.setattr(main, "_his_write_baseline_fp",
                        lambda: {"title_version": "1150713"})
    assert main._his_write_verdict_for("西醫門診醫師作業 V.1150713.99").status == cc.STATUS_OK
    assert main._his_write_verdict_for("西醫門診醫師作業 V.1150713").status == cc.STATUS_OK


def test_suffix_drift_after_recalibration(monkeypatch):
    # 使用者實機重新校正後,基線帶尾碼(title_version_full)→ 此後尾碼變動(.01→.02)才判 DRIFT。
    monkeypatch.setattr(main, "_his_write_baseline_fp",
                        lambda: {"title_version": "1150713",
                                 "title_version_full": "1150713.02"})
    assert main._his_write_verdict_for("西醫門診醫師作業 V.1150713.02").status == cc.STATUS_OK
    assert main._his_write_verdict_for("西醫門診醫師作業 V.1150713.03").status == cc.STATUS_DRIFT


def test_sample_fp_includes_full_version():
    # 採樣現況指紋同時含主版本與含尾碼的完整版本(供實機校正後尾碼比對)
    fp = main.sample_his_current_fp("西醫門診醫師作業 V.1150713.02")
    assert fp == {"title_version": "1150713", "title_version_full": "1150713.02"}
    assert main.sample_his_current_fp("西醫門診醫師作業") is None   # 無版本 → None


# ── 重新校正 UI(設定頁)─────────────────────────────────────────────────────
def _recal_env(monkeypatch, tmp_path_or_file, title, hwnd=123):
    monkeypatch.setattr(main, "_contract_baseline_singleton", None)
    monkeypatch.setattr(main, "_find_hospital_main_window", lambda: hwnd)
    monkeypatch.setattr(main, "_his_title_of", lambda h: title)


def test_recalibrate_self_sufficient_no_global():
    # [codex P1] 重新校正自足即時採樣,不得讀已移除的全域 _his_current_fp。
    recal_src = inspect.getsource(main.AutomationApp._recalibrate_his_canary)
    assert "sample_his_current_fp(_his_title_of(" in recal_src, "重新校正應自足採樣"
    assert "fp = _his_current_fp" not in recal_src, "重新校正不得讀全域 _his_current_fp"


def test_recalibrate_writes_current_version_as_baseline(monkeypatch, tmp_path):
    # 現況版本 1150701 → 校正後基線=1150701(含尾碼 full)
    monkeypatch.setattr(main, "get_conf_path", lambda name: str(tmp_path / name))
    _recal_env(monkeypatch, tmp_path, "西醫門診醫師作業 V.1150701.01")
    monkeypatch.setattr(main.messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(main.messagebox, "showinfo", lambda *a, **k: None)

    app = main.AutomationApp.__new__(main.AutomationApp)
    main.AutomationApp._recalibrate_his_canary(app)

    # 基線檔已記錄現況版本(含尾碼 full;實機重新校正後尾碼才納入 DRIFT 判定)
    assert main._contract_baseline().get("his_menu") == {
        "title_version": "1150701", "title_version_full": "1150701.01"}
    # 校正後同版本 → 不再判改版(OK)
    assert main._his_write_verdict_for(
        "西醫門診醫師作業 V.1150701.01").status == cc.STATUS_OK


def test_recalibrate_aborts_when_no_version(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "get_conf_path", lambda name: str(tmp_path / name))
    _recal_env(monkeypatch, tmp_path, "西醫門診醫師作業")     # 找到視窗但無版本號
    warned = {"w": False}
    monkeypatch.setattr(main.messagebox, "showwarning",
                        lambda *a, **k: warned.update(w=True))
    yes = {"asked": False}
    monkeypatch.setattr(main.messagebox, "askyesno",
                        lambda *a, **k: yes.update(asked=True) or True)
    app = main.AutomationApp.__new__(main.AutomationApp)
    main.AutomationApp._recalibrate_his_canary(app)
    assert warned["w"] is True and yes["asked"] is False   # 無版本 → 警告、不寫基線


def test_recalibrate_shows_error_not_success_when_refused(monkeypatch, tmp_path):
    # [codex] 基線檔為較新版本 schema → set 被拒 → 顯示錯誤、不可誤報「已校正」
    import json
    p = tmp_path / "contract_baseline.json"
    p.write_text(json.dumps({"schema_version": 999, "surfaces": {}}),
                 encoding="utf-8")
    monkeypatch.setattr(main, "get_conf_path", lambda name: str(p))
    _recal_env(monkeypatch, p, "西醫門診醫師作業 V.1150701.01")
    monkeypatch.setattr(main.messagebox, "askyesno", lambda *a, **k: True)
    calls = {"error": False, "info": False}
    monkeypatch.setattr(main.messagebox, "showerror",
                        lambda *a, **k: calls.update(error=True))
    monkeypatch.setattr(main.messagebox, "showinfo",
                        lambda *a, **k: calls.update(info=True))
    app = main.AutomationApp.__new__(main.AutomationApp)
    main.AutomationApp._recalibrate_his_canary(app)
    assert calls["error"] is True and calls["info"] is False, \
        "被拒時應顯示錯誤、不得顯示成功"
    assert json.loads(p.read_text(encoding="utf-8"))["schema_version"] == 999


def test_canary_settings_wired_into_settings_tab():
    src = inspect.getsource(main.AutomationApp._create_settings_tab)
    assert "_build_canary_settings(left_column)" in src


def test_canary_settings_copy_says_notify_not_block():
    # [使用者定案 2026-07-17] 文案須與行為一致:改版=寄信通知、【不擋不影響操作】;
    # 不得再宣稱會「停止自動寫入」或「未納入契約閘門」,也不得把 F11 誤標成「轉診」。
    src = inspect.getsource(main.AutomationApp._build_canary_settings)
    assert "寄信通知" in src, "應說明改版時寄信通知"
    assert "不會擋住自動寫入" in src, "應明說不擋自動寫入"
    assert "停止自動寫入" not in src, "已不再擋,不可宣稱停止自動寫入"
    assert "未納入契約閘門" not in src
    assert "F11 轉診" not in src, "F11 是快速完成,非轉診"


def test_canary_status_text_now_reflects_live_sample(monkeypatch):
    app = main.AutomationApp.__new__(main.AutomationApp)
    # 找不到視窗 → 尚未偵測(誠實,不 stale)
    monkeypatch.setattr(main, "_find_hospital_main_window", lambda: 0)
    assert "尚未偵測" in main.AutomationApp._canary_status_text_now(app)
    # 找到視窗 + DRIFT title → 顯示疑似改版
    monkeypatch.setattr(main, "_find_hospital_main_window", lambda: 123)
    monkeypatch.setattr(main, "_his_title_of", lambda h: "西醫門診醫師作業 V.1150701.01")
    monkeypatch.setattr(main, "_his_write_baseline_fp",
                        lambda: {"title_version": "1150713"})
    assert "改版" in main.AutomationApp._canary_status_text_now(app)
