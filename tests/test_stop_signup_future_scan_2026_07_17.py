# -*- coding: utf-8 -*-
"""止掛提醒:遠期診次漏提醒的修正(2026-07-17 使用者實例)。

【原狀況】止掛提醒寄生在主行事曆 refresh(_update_grid_data 且 is_future=False),而主行事曆
錨在【本週一 + 2 週】→ 可提前偵測的天數隨週內遞減(週一 13 天、週五 9 天、週日 7 天);更遠
的診次只出現在「未來週次」分頁,那裡傳 is_future=True 直接關掉寄信、資料還只在分頁被點開時
才更新。結果:兩三週前就掛滿的熱門診次【永遠不會提醒】——使用者實例:2026-07-17(週五)時
7/30(週四)晚上張廖年峰已 >129 人(門檻 chang_thu_night=129)卻沒收到信。

【修法】改用不依賴 UI 的背景掃描 _scan_future_stop_signup_alerts:固定「今天起
STOP_SIGNUP_SCAN_DAYS(28)天」,純讀既有 all_doctors_data 快取(reg52 早就抓進來、未來分頁
就是讀它 → 不增加院方請求),只寄 email 不跳彈窗,與行事曆共用 notify_key 持久化去重。
"""
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import main  # noqa: E402


# ── 純函式:門診項目解析 ─────────────────────────────────────────────────────
def test_parse_appt_item_dict_and_legacy_string():
    got = main._parse_appt_item_for_alert(
        {"session": "晚上", "count": 130, "is_stopped": False, "room": "101診"})
    assert got == ("晚上", 130, False, None, "101診")
    got2 = main._parse_appt_item_for_alert("晚上:130人|Rm:101診|Stop:1")
    assert got2[0] == "晚上" and got2[1] == 130 and got2[2] is True


def test_parse_appt_item_rejects_non_numeric_sessions():
    # 休診/停診【不是 0 人】,不可拿去比門檻
    assert main._parse_appt_item_for_alert({"session": "晚上", "count": "休診"}) is None
    assert main._parse_appt_item_for_alert("晚上:停診") is None
    assert main._parse_appt_item_for_alert({"session": "", "count": 5}) is None


# ── 掃描:使用者回報的 7/30 情境 ─────────────────────────────────────────────
class _FakeVar:
    def __init__(self, v):
        self._v = v

    def get(self):
        return self._v


def _app(monkeypatch, by_date, chang_on=True, recipients=("me@example.com",)):
    """組一個只帶掃描所需欄位的 app(不建 Tk)。"""
    app = main.AutomationApp.__new__(main.AutomationApp)
    app.alert_chang_enabled = _FakeVar(chang_on)
    app.alert_chen_enabled = _FakeVar(False)
    app.alert_email_recipients = list(recipients)
    app.doctors_list = [{"name": "張廖年峰", "doc_no": "D12345"}]
    app.all_doctors_data = {"D12345": by_date}
    app._doctor_data_lock = main.threading.Lock()
    app._alert_state_lock = main.threading.Lock()
    app._reg64_cache_lock = main.threading.Lock()
    app._reg64_public_snapshot = {}
    app._alert_email_inflight = set()
    # 本次執行已收到即時 reg52 資料(否則掃描會略過:開機舊快取不可拿來寄)
    app._live_clinic_data_keys = {"D12345"}
    app.threshold_settings = {}
    sent = []
    app._alert_email_sent = {}
    monkeypatch.setattr(app, "_mark_alert_email_sent",
                        lambda nk: (sent.append(nk),
                                    app._alert_email_sent.__setitem__(nk, "d")))
    return app, sent


def _dispatch_sync(monkeypatch, app):
    """把寄信改成同步 + 記錄,免測背景緒時序。"""
    mails = []
    monkeypatch.setattr(main, "_send_alert_email_via_smtp",
                        lambda subj, body, rcpts, **k:
                        mails.append((subj, body, tuple(rcpts))) or True)
    monkeypatch.setattr(main.threading, "Thread",
                        lambda target=None, **k: type(
                            "T", (), {"start": lambda s: target()})())
    return mails


def test_user_case_thursday_clinic_13_days_out_now_alerts(monkeypatch):
    """使用者實例:今天週五,13 天後的週四晚上已 130 人(門檻 129)→ 必須寄信。
    修正前這一天落在主行事曆(本週一+2 週)之外 → 完全不會寄。"""
    today = date(2026, 7, 17)                      # 週五
    target = date(2026, 7, 30)                     # 週四(13 天後)
    assert target.weekday() == 3 and (target - today).days == 13
    app, sent = _app(monkeypatch, {target: [
        {"session": "晚上", "count": 130, "is_stopped": False, "room": "101診"}]})
    mails = _dispatch_sync(monkeypatch, app)
    app._scan_future_stop_signup_alerts(today=today)
    assert len(mails) == 1, "7/30 晚上已超過門檻 → 應寄出止掛提醒"
    subj, body, rcpts = mails[0]
    assert "止掛提醒" in subj and "130 人" in subj
    assert "2026/7/30(週四)" in subj and "張廖年峰醫師" in subj
    # [使用者定案 2026-07-20] 內文不再有「提前提醒/距此診次還有 N 天」與結尾附註
    assert "提前提醒" not in body and "還有" not in body
    assert "此診次只會通知這一封" not in body
    assert "已達/超過止掛門檻 89 人" not in body   # 門檻是 129,顯示的是實際門檻
    assert "已達/超過止掛門檻 129 人" in body and "目前掛號 130 人" in body
    assert sent == [f"{target}_晚上_張廖年峰_main"], "須以 notify_key 記錄已寄(跨重啟去重)"


def test_scan_covers_whole_28_day_horizon_regardless_of_weekday(monkeypatch):
    # 視窗必須固定「今天起 28 天」,不再隨日曆週縮短(舊 bug 的根因)
    today = date(2026, 7, 19)                      # 週日 → 舊做法只剩 7 天
    far = today + timedelta(days=27)
    while far.weekday() != 3:                      # 挑一個在門檻表內的週四
        far -= timedelta(days=1)
    app, _ = _app(monkeypatch, {far: [
        {"session": "晚上", "count": 200, "is_stopped": False}]})
    mails = _dispatch_sync(monkeypatch, app)
    app._scan_future_stop_signup_alerts(today=today)
    assert len(mails) == 1, f"{far} 在 28 天內 → 應提醒(舊做法週日只看 7 天)"


def test_scan_ignores_beyond_horizon_and_past(monkeypatch):
    today = date(2026, 7, 17)
    beyond = today + timedelta(days=main.STOP_SIGNUP_SCAN_DAYS + 7)
    past = today - timedelta(days=7)
    app, _ = _app(monkeypatch, {
        beyond: [{"session": "晚上", "count": 200}],
        past: [{"session": "晚上", "count": 200}],
    })
    mails = _dispatch_sync(monkeypatch, app)
    app._scan_future_stop_signup_alerts(today=today)
    assert mails == [], "超出前瞻視窗與已過去的診次都不提醒"


def test_already_stopped_clinic_is_not_alerted(monkeypatch):
    # 已止掛 → 不會再增號,提醒無意義(與 threshold_policy 既有的 is_stopped 邏輯一致)
    today = date(2026, 7, 17)
    target = date(2026, 7, 30)
    app, _ = _app(monkeypatch, {target: [
        {"session": "晚上", "count": 140, "is_stopped": True}]})
    mails = _dispatch_sync(monkeypatch, app)
    app._scan_future_stop_signup_alerts(today=today)
    assert mails == []


def test_below_threshold_not_alerted(monkeypatch):
    today = date(2026, 7, 17)
    target = date(2026, 7, 30)
    app, _ = _app(monkeypatch, {target: [
        {"session": "晚上", "count": 128, "is_stopped": False}]})   # 門檻 129
    mails = _dispatch_sync(monkeypatch, app)
    app._scan_future_stop_signup_alerts(today=today)
    assert mails == [], "未達門檻不寄(129 才是門檻)"


def test_disabled_doctor_toggle_blocks_alert(monkeypatch):
    today = date(2026, 7, 17)
    target = date(2026, 7, 30)
    app, _ = _app(monkeypatch, {target: [
        {"session": "晚上", "count": 200}]}, chang_on=False)
    mails = _dispatch_sync(monkeypatch, app)
    app._scan_future_stop_signup_alerts(today=today)
    assert mails == [], "該醫師提醒開關關閉時不寄"


def test_no_duplicate_email_for_same_clinic(monkeypatch):
    # 每輪 refresh 都會掃 → 同一診次只能寄一次(靠 notify_key 持久化去重)
    today = date(2026, 7, 17)
    target = date(2026, 7, 30)
    app, sent = _app(monkeypatch, {target: [
        {"session": "晚上", "count": 130, "is_stopped": False}]})
    mails = _dispatch_sync(monkeypatch, app)
    for _ in range(5):
        app._scan_future_stop_signup_alerts(today=today)
    assert len(mails) == 1, "同一診次不得重複寄信"


def test_no_recipients_sends_nothing(monkeypatch):
    today = date(2026, 7, 17)
    app, _ = _app(monkeypatch, {date(2026, 7, 30): [
        {"session": "晚上", "count": 200}]}, recipients=())
    mails = _dispatch_sync(monkeypatch, app)
    app._scan_future_stop_signup_alerts(today=today)
    assert mails == []


def test_stale_startup_cache_is_not_alerted(monkeypatch):
    """[codex P2] 開機時會先用磁碟舊快取渲染行事曆 → 那時還沒收到即時 reg52 資料。
    用舊資料寄提醒會寄錯,而且會把該診次【永久】標記已寄 → 之後真的爆掉反而不提醒。
    故:沒收到該醫師的即時資料前不掃。"""
    today = date(2026, 7, 17)
    target = date(2026, 7, 30)
    app, _ = _app(monkeypatch, {target: [{"session": "晚上", "count": 200}]})
    app._live_clinic_data_keys = set()          # 尚未收到任何即時資料(開機當下)
    mails = _dispatch_sync(monkeypatch, app)
    app._scan_future_stop_signup_alerts(today=today)
    assert mails == [], "只有磁碟舊快取時不得寄提醒"
    # 收到即時資料後才會寄
    app._live_clinic_data_keys = {"D12345"}
    app._scan_future_stop_signup_alerts(today=today)
    assert len(mails) == 1


def test_only_final_live_payload_unlocks_scanning():
    """[codex P2] UiClinicDataMessage 有多種來源:磁碟舊快取 fallback、漸進式部分結果、
    快照重播、錯誤 payload,以及最後那筆完整成功的即時資料。只有最後那種可以解鎖遠期止掛
    掃描 —— 否則開機用舊快取就會寄出過期提醒並永久去重。預設必須是「不解鎖」。"""
    from cmuh_common.ui_messages import UiClinicDataMessage
    assert UiClinicDataMessage(doctor_name="D1", data={}).is_live_final is False, \
        "預設必須不解鎖(舊快取/部分結果/重播都走這個預設)"
    assert UiClinicDataMessage(doctor_name="D1", data={},
                               is_live_final=True).is_live_final is True


def test_only_final_payload_emit_site_sets_live_flag():
    """實際 emit 點:只有『完整成功的即時資料』那一處帶 is_live_final=True。
    (磁碟快取 fallback / 漸進式部分結果 / 快照重播 / 錯誤 payload 都不得帶。)"""
    src = open(main.__file__, encoding="utf-8").read()
    assert src.count("is_live_final=True") == 1, \
        "只能有一個 emit 點宣告自己是完整成功的即時資料"
    # 該處必須是 return 前的最終成功 payload(其鄰近有成功 log)
    idx = src.index("is_live_final=True")
    assert "successful" in src[max(0, idx - 800):idx], \
        "is_live_final=True 應只出現在查詢成功的最終 payload"


def test_handler_marks_live_on_final_and_clears_on_non_final():
    """[codex P2] 掃描資格必須綁在【目前存著的那筆資料】:每一筆 payload 都會覆蓋
    all_doctors_data,所以最終即時資料→解鎖;之後若被漸進式部分結果/磁碟快取 fallback
    覆蓋 → 必須立刻取消資格(否則會拿非最終資料去寄信),等下一筆最終資料再解鎖。"""
    src = open(main.__file__, encoding="utf-8").read()
    i = src.index("_live_clinic_data_keys.add")
    window = src[max(0, i - 500):i + 300]
    assert "if is_live_final:" in window, "只有最終即時 payload 可解鎖掃描"
    assert "_live_clinic_data_keys.discard" in window, \
        "非最終 payload 覆蓋資料時必須取消資格(資格不可黏著)"


def test_eligibility_follows_currently_stored_payload(monkeypatch):
    """行為:final 解鎖 → 掃描會寄;之後 partial/fallback 覆蓋 → 資格取消 → 不再寄。"""
    today = date(2026, 7, 17)
    target = date(2026, 7, 30)
    app, _ = _app(monkeypatch, {target: [
        {"session": "晚上", "count": 130, "is_stopped": False}]})
    mails = _dispatch_sync(monkeypatch, app)

    # 模擬處理端:最終即時 payload → 解鎖
    with app._alert_state_lock:
        app._live_clinic_data_keys.add("D12345")
    app._scan_future_stop_signup_alerts(today=today)
    assert len(mails) == 1

    # 模擬處理端:之後來一筆非最終 payload(部分結果/舊快取)覆蓋 → 取消資格
    with app._alert_state_lock:
        app._live_clinic_data_keys.discard("D12345")
    app._alert_email_sent.clear()          # 假設是另一個尚未寄過的診次
    app._scan_future_stop_signup_alerts(today=today)
    assert len(mails) == 1, "資格已取消 → 不得再用非最終資料寄信"


def test_calendar_and_scan_cannot_both_send_same_clinic(monkeypatch):
    """[codex P2] 兩條寄信路徑(行事曆 notify、遠期背景掃描)不可同一診次各寄一封。
    持久化的「已寄」記號是【寄成功後】才寫的,所以要靠共用的原子 claim 擋住。
    這裡模擬:行事曆那邊已取得寄送權、SMTP 還在飛,此時掃描跑起來 → 必須不寄。"""
    today = date(2026, 7, 17)
    target = date(2026, 7, 30)
    app, _ = _app(monkeypatch, {target: [
        {"session": "晚上", "count": 130, "is_stopped": False}]})
    mails = _dispatch_sync(monkeypatch, app)
    nk = f"{target}_晚上_張廖年峰_main"
    assert app._claim_alert_email(nk) is True    # 行事曆先搶到寄送權(信還在寄)
    app._scan_future_stop_signup_alerts(today=today)
    assert mails == [], "另一條路徑正在寄 → 掃描不得再寄一封"
    # 對方寄完並記號後,掃描仍不得重寄
    app._mark_alert_email_sent(nk)
    app._release_alert_email_claim(nk)
    app._scan_future_stop_signup_alerts(today=today)
    assert mails == [], "已寄過 → 不得重寄"


def test_claim_is_atomic_and_released_on_failure(monkeypatch):
    app, _ = _app(monkeypatch, {})
    nk = "2026-07-30_晚上_張廖年峰_main"
    assert app._claim_alert_email(nk) is True
    assert app._claim_alert_email(nk) is False, "同一 key 不得重複取得寄送權"
    app._release_alert_email_claim(nk)
    assert app._claim_alert_email(nk) is True, "寄失敗釋放後應可重試(不會永久卡死)"
    app._release_alert_email_claim(nk)
    app._mark_alert_email_sent(nk)
    assert app._claim_alert_email(nk) is False, "已寄過就不再給寄送權"


def test_claim_released_when_dispatch_body_raises(monkeypatch):
    """[GPT-5.6 P1-01 fault-injection] 組主旨/讀 snapshot 階段(Thread 啟動前)拋例外
    → 外層掃描的 catch 會吞掉,但寄送權必須釋放,否則該診次永久卡在 in-flight、
    本次執行再也不寄。"""
    today = date(2026, 7, 17)
    target = date(2026, 7, 30)
    app, _ = _app(monkeypatch, {target: [
        {"session": "晚上", "count": 130, "is_stopped": False}]})
    _dispatch_sync(monkeypatch, app)
    # 讓組裝階段炸掉(reg64 snapshot 被非預期資料覆蓋的模擬)
    monkeypatch.setattr(app, "_dispatch_future_stop_alert_inner",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("subject assembly boom")))
    app._scan_future_stop_signup_alerts(today=today)
    assert app._alert_email_inflight == set(), \
        "組裝失敗必須釋放寄送權(不得永久卡 in-flight)"
    # 釋放後下一輪(組裝恢復正常)仍能寄出
    monkeypatch.undo()
    app2, _ = _app(monkeypatch, {target: [
        {"session": "晚上", "count": 130, "is_stopped": False}]})
    mails = _dispatch_sync(monkeypatch, app2)
    app2._scan_future_stop_signup_alerts(today=today)
    assert len(mails) == 1, "上輪組裝失敗 → 本輪應可重試寄出"


def test_claim_released_when_thread_start_raises(monkeypatch):
    # Thread 啟動失敗(執行緒耗盡)也必須釋放寄送權
    today = date(2026, 7, 17)
    target = date(2026, 7, 30)
    app, _ = _app(monkeypatch, {target: [
        {"session": "晚上", "count": 130, "is_stopped": False}]})
    monkeypatch.setattr(main, "_send_alert_email_via_smtp",
                        lambda *a, **k: True)
    monkeypatch.setattr(main.threading, "Thread",
                        lambda *a, **k: (_ for _ in ()).throw(
                            RuntimeError("no more threads")))
    app._scan_future_stop_signup_alerts(today=today)
    assert app._alert_email_inflight == set(), "Thread 啟動失敗必須釋放寄送權"


def test_failed_send_can_retry_next_scan(monkeypatch):
    # 寄失敗(SMTP 暫時故障)→ 沒有永久記號 → 下一輪掃描應重試
    today = date(2026, 7, 17)
    target = date(2026, 7, 30)
    app, sent = _app(monkeypatch, {target: [
        {"session": "晚上", "count": 130, "is_stopped": False}]})
    attempts = []
    ok = {"v": False}
    monkeypatch.setattr(main, "_send_alert_email_via_smtp",
                        lambda s, b, r, **k: attempts.append(s) or ok["v"])
    monkeypatch.setattr(main.threading, "Thread",
                        lambda target=None, **k: type(
                            "T", (), {"start": lambda s: target()})())
    app._scan_future_stop_signup_alerts(today=today)
    assert len(attempts) == 1 and sent == [], "寄失敗不得留下已寄記號"
    ok["v"] = True
    app._scan_future_stop_signup_alerts(today=today)
    assert len(attempts) == 2 and len(sent) == 1, "上次失敗 → 下一輪應重試並成功"


def test_malformed_date_value_skipped_not_aborting_scan(monkeypatch):
    # [codex P2] 某日的值不是 list(None/int/壞物件)→ 只跳過該日,不炸掉整輪;同醫師其他
    # 日期仍正常寄。原本 list(v) 會拋例外被外層 catch → 全部醫師都不寄。
    today = date(2026, 7, 17)
    good = date(2026, 7, 30)
    bad = date(2026, 7, 28)
    app, _ = _app(monkeypatch, {
        bad: None,                                   # 畸形:非 list
        good: [{"session": "晚上", "count": 130, "is_stopped": False}],
    })
    mails = _dispatch_sync(monkeypatch, app)
    app._scan_future_stop_signup_alerts(today=today)
    assert len(mails) == 1, "壞日期跳過,好日期仍應寄"


def test_one_bad_doctor_does_not_block_other_doctor(monkeypatch):
    # [codex P2] 一位醫師的快取整塊畸形 → 只跳過該位,另一位仍正常寄
    today = date(2026, 7, 17)                     # 週五
    tue = date(2026, 7, 21)                       # 週二(陳駿升晚上門檻表內,門檻 59)
    assert tue.weekday() == 1
    app, _ = _app(monkeypatch, {})
    app.alert_chen_enabled = _FakeVar(True)       # 兩位都啟用
    app.doctors_list = [
        {"name": "張廖年峰", "doc_no": "D12345"},
        {"name": "陳駿升", "doc_no": "D67890"},
    ]
    app._live_clinic_data_keys = {"D12345", "D67890"}
    app.all_doctors_data = {
        "D12345": "整塊不是 dict",                 # 張廖:畸形 → 該位跳過
        "D67890": {tue: [{"session": "晚上", "count": 100, "is_stopped": False}]},
    }
    mails = _dispatch_sync(monkeypatch, app)
    app._scan_future_stop_signup_alerts(today=today)
    assert len(mails) == 1 and "陳駿升" in mails[0][0], \
        "張廖快取畸形不得害陳駿升的提醒不寄"


def test_scan_swallows_errors(monkeypatch):
    # 掃描壞掉不可影響行事曆/其他功能
    app, _ = _app(monkeypatch, {})
    monkeypatch.setattr(app, "_get_doctor_threshold_map",
                        lambda n: (_ for _ in ()).throw(RuntimeError("boom")))
    app._scan_future_stop_signup_alerts()          # 不得拋


# ── 設定耦合:保留期必須大於前瞻視窗 ─────────────────────────────────────────
def test_sent_record_retention_exceeds_scan_horizon():
    """保留期若短於前瞻天數,遠期診次的『已寄』記錄會在該診次到來前被剪掉 → 重寄。
    (28 天前瞻 + 舊的 21 天保留 = 第 21 天重寄一次。)"""
    assert main.ALERT_EMAIL_SENT_RETAIN_DAYS > main.STOP_SIGNUP_SCAN_DAYS


def test_scan_is_wired_into_refresh():
    import inspect
    src = inspect.getsource(main.AutomationApp.refresh_all_calendars)
    assert "_scan_future_stop_signup_alerts()" in src, "每次行事曆刷新都要順便掃遠期止掛"
