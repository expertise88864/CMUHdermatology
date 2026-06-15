# -*- coding: utf-8 -*-
"""consult_query 觸發/寄信「決策邏輯」測試。

[review C2 2026-06-12] 覆蓋率分析顯示 consult_query 僅 20% 被測,且最關鍵的
_do_full_job 收件人路由(寄給誰)與失敗路徑(去重釋放/失敗告知)完全沒測 ——
這正是「寄錯人/狂寄/漏寄」風險的核心。本檔以 monkeypatch 隔離全部重依賴
(不真跑自動化、不真寄信、不真殺 systemftp),只測決策。
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

import consult_query as cq  # noqa: E402
import cmuh_common.smtp_mail as smtp_mail  # noqa: E402


# ─── 共用 harness ────────────────────────────────────────────────────────

def _base_cfg(**over) -> dict:
    cfg = dict(cq.DEFAULT_CONFIG)
    cfg.update({
        "mail_method": "smtp",
        "recipients": ["sched_a@x.tw", "sched_b@x.tw"],
        "email_trigger_recipients": ["fallback@x.tw"],
        "retry_count": 3,
    })
    cfg.update(over)
    return cfg


class _JobHarness:
    """monkeypatch _do_full_job 的全部重依賴,記錄決策結果。"""

    def __init__(self, monkeypatch, cfg, fail_times=0, extracted_text=""):
        self.sent = []          # [(recipients, subject)]
        self.bodies = []        # 寄出的信件內文
        self.extracted_text = extracted_text
        self.flow_runs = 0
        self.kills = 0
        self.sleeps = []
        self.released = []      # _release_trigger_dedup 收到的 senders
        self.failure_notices = []  # (recipients, reason)
        self._fail_times = fail_times

        monkeypatch.setattr(cq, "load_config", lambda: dict(cfg))
        monkeypatch.setattr(smtp_mail, "is_configured", lambda: True)

        def _flow(label=""):
            self.flow_runs += 1
            if self.flow_runs <= self._fail_times:
                raise RuntimeError(f"simulated failure #{self.flow_runs}")
            # [2026-06-13] run_consult_flow 改回傳 (截圖, 擷取文字)
            return Path("C:/fake/shot.png"), self.extracted_text
        monkeypatch.setattr(cq, "run_consult_flow", _flow)
        monkeypatch.setattr(
            cq, "send_via_smtp",
            lambda shot, subject, body, recipients: self.sent.append(
                (list(recipients), subject)) or self.bodies.append(body))
        monkeypatch.setattr(
            cq, "send_via_outlook",
            lambda shot, subject, body, recipients, sender_account="":
                self.sent.append((list(recipients), subject)))
        monkeypatch.setattr(
            cq, "_kill_systemftp",
            lambda: setattr(self, "kills", self.kills + 1))
        monkeypatch.setattr(time, "sleep", lambda s: self.sleeps.append(s))
        monkeypatch.setattr(
            cq, "_release_trigger_dedup",
            lambda senders: self.released.append(list(senders)))
        monkeypatch.setattr(
            cq, "_send_failure_notice_async",
            lambda recipients, reason: self.failure_notices.append(
                (list(recipients), reason)))


# ─── _do_full_job 收件人路由 ─────────────────────────────────────────────

def test_route_email_trigger_sends_to_trigger_sender(monkeypatch):
    """IMAP 觸發(override_recipients=觸發者) → 結果回寄給「觸發者本人」。"""
    h = _JobHarness(monkeypatch, _base_cfg())
    cq._do_full_job("email", override_recipients=["dr.wang@x.tw"])
    assert h.sent == [(["dr.wang@x.tw"], h.sent[0][1])]
    assert h.flow_runs == 1


def test_route_email_without_sender_falls_back_to_trigger_recipients(monkeypatch):
    """email 觸發但解析不出寄件人 → 用 email_trigger_recipients。"""
    h = _JobHarness(monkeypatch, _base_cfg())
    cq._do_full_job("email")
    assert h.sent[0][0] == ["fallback@x.tw"]


def test_route_email_fallback_empty_uses_general_recipients(monkeypatch):
    """email fallback 名單為空 → 退回一般 recipients(不可寄空名單)。"""
    h = _JobHarness(monkeypatch, _base_cfg(email_trigger_recipients=[]))
    cq._do_full_job("email")
    assert h.sent[0][0] == ["sched_a@x.tw", "sched_b@x.tw"]


def test_route_scheduled_uses_general_recipients(monkeypatch):
    """排程觸發(label=HH:MM) → 寄一般四人名單,絕不可寄 email fallback。"""
    h = _JobHarness(monkeypatch, _base_cfg())
    cq._do_full_job("17:00")
    assert h.sent[0][0] == ["sched_a@x.tw", "sched_b@x.tw"]


def test_subject_time_uses_trigger_label_when_clock_format(monkeypatch):
    """trigger_label 含「:」(排程時刻) → 主旨的 {time} 用 label 去冒號(1700),
    讓收件人看得出是哪一班排程的結果。"""
    cfg = _base_cfg(subject_template="會診 {date} {time}")
    h = _JobHarness(monkeypatch, cfg)
    cq._do_full_job("17:00")
    assert h.sent[0][1].endswith("1700")


def test_subject_time_uses_now_for_manual_label(monkeypatch):
    """非時刻型 label(手動/email) → {time} 用當下 HHMM(4 位數字)。"""
    cfg = _base_cfg(subject_template="會診 {date} {time}")
    h = _JobHarness(monkeypatch, cfg)
    cq._do_full_job("手動")
    t = h.sent[0][1].rsplit(" ", 1)[-1]
    assert len(t) == 4 and t.isdigit()


# ─── _do_full_job 靜默跳過(多機部署) ────────────────────────────────────

def test_smtp_not_configured_skips_whole_flow(monkeypatch):
    """SMTP 未設定 → 整個流程靜默跳過:不跑自動化、不寄信(多機只有一台寄)。"""
    h = _JobHarness(monkeypatch, _base_cfg())
    monkeypatch.setattr(smtp_mail, "is_configured", lambda: False)
    cq._do_full_job("17:00")
    assert h.flow_runs == 0
    assert h.sent == []


def test_outlook_mode_without_outlook_skips(monkeypatch):
    """mail_method=outlook 且本機無 Outlook → 靜默跳過。"""
    h = _JobHarness(monkeypatch, _base_cfg(mail_method="outlook"))
    monkeypatch.setattr(cq, "_outlook_available", lambda timeout=5.0: False)
    cq._do_full_job("17:00")
    assert h.flow_runs == 0
    assert h.sent == []


def test_flow_lock_held_skips_without_side_effects(monkeypatch):
    """_flow_lock 已被佔用 → 本次直接略過(不排隊、不寄信)。"""
    h = _JobHarness(monkeypatch, _base_cfg())
    assert cq._flow_lock.acquire(blocking=False)
    try:
        cq._do_full_job("17:00")
    finally:
        cq._flow_lock.release()
    assert h.flow_runs == 0
    assert h.sent == []


# ─── _do_full_job 重試與失敗路徑 ─────────────────────────────────────────

def test_retry_then_success_sends_once_no_failure_notice(monkeypatch):
    """第 1 次失敗、第 2 次成功 → 只寄 1 封、kill 1 次、不發失敗告知、不放去重。"""
    h = _JobHarness(monkeypatch, _base_cfg(), fail_times=1)
    cq._do_full_job("email", override_recipients=["dr.wang@x.tw"])
    assert h.flow_runs == 2
    assert len(h.sent) == 1
    assert h.kills == 1
    assert h.failure_notices == []
    assert h.released == []


def test_retry_backoff_uses_schedule(monkeypatch):
    """重試間隔走 BACKOFF_SCHEDULE(3,30,90),不是固定值。"""
    h = _JobHarness(monkeypatch, _base_cfg(retry_count=3), fail_times=3)
    cq._do_full_job("17:00")
    assert h.sleeps == [3, 30]  # 3 次嘗試 → 2 段間隔
    assert h.flow_runs == 3


def test_all_fail_email_trigger_releases_dedup_and_notifies(monkeypatch):
    """email 觸發全部失敗 → 釋放觸發者去重(可立即重發)+ 回失敗告知信。"""
    h = _JobHarness(monkeypatch, _base_cfg(retry_count=2), fail_times=99)
    cq._do_full_job("email", override_recipients=["dr.wang@x.tw"])
    assert h.sent == []
    assert h.released == [["dr.wang@x.tw"]]
    assert len(h.failure_notices) == 1
    assert h.failure_notices[0][0] == ["dr.wang@x.tw"]
    assert "simulated failure" in h.failure_notices[0][1]


def test_all_fail_scheduled_no_dedup_release_no_notice(monkeypatch):
    """排程全部失敗 → 不放去重、不寄失敗告知(那是 email 觸發專屬行為)。"""
    h = _JobHarness(monkeypatch, _base_cfg(retry_count=2), fail_times=99)
    cq._do_full_job("17:00")
    assert h.released == []
    assert h.failure_notices == []


def test_retry_count_respected(monkeypatch):
    """cfg.retry_count=2 → 最多嘗試 2 次就放棄。"""
    h = _JobHarness(monkeypatch, _base_cfg(retry_count=2), fail_times=99)
    cq._do_full_job("17:00")
    assert h.flow_runs == 2


def test_send_failure_also_retries(monkeypatch):
    """截圖成功但「寄信」失敗 → 同樣觸發重試(寄信失敗不可吞掉)。"""
    h = _JobHarness(monkeypatch, _base_cfg(retry_count=2))
    calls = {"n": 0}

    def _send_fail_once(shot, subject, body, recipients):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("smtp down")
        h.sent.append((list(recipients), subject))
    monkeypatch.setattr(cq, "send_via_smtp", _send_fail_once)
    cq._do_full_job("17:00")
    assert calls["n"] == 2
    assert len(h.sent) == 1


# ─── load_config 正規化(觸發安全相關欄位) ──────────────────────────────

def test_load_config_lowercases_trigger_whitelist(tmp_path, monkeypatch):
    """白名單必須全小寫儲存:寄件人比對大小寫無關,否則大寫白名單永遠擋人。"""
    monkeypatch.setattr(cq, "CONFIG_FILE", tmp_path / "c.json")
    import json
    (tmp_path / "c.json").write_text(json.dumps({
        "allowed_trigger_senders": ["Dr.Wang@X.TW", "  ", None],
    }), encoding="utf-8")
    cfg = cq.load_config()
    assert cfg["allowed_trigger_senders"] == ["dr.wang@x.tw"]


def test_load_config_clamps_poll_seconds(tmp_path, monkeypatch):
    """輪詢週期夾在 5-300 秒:太小會打爆 Gmail、太大失去即時性。"""
    monkeypatch.setattr(cq, "CONFIG_FILE", tmp_path / "c.json")
    import json
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"email_trigger_poll_seconds": 1}), encoding="utf-8")
    assert cq.load_config()["email_trigger_poll_seconds"] == 5.0
    p.write_text(json.dumps({"email_trigger_poll_seconds": 9999}), encoding="utf-8")
    assert cq.load_config()["email_trigger_poll_seconds"] == 300.0
    p.write_text(json.dumps({"email_trigger_poll_seconds": "bad"}), encoding="utf-8")
    assert (cq.load_config()["email_trigger_poll_seconds"]
            == cq.DEFAULT_CONFIG["email_trigger_poll_seconds"])


def test_load_config_filters_empty_recipients(tmp_path, monkeypatch):
    """recipients 過濾 None/空白(寄信名單不可含空字串)。"""
    monkeypatch.setattr(cq, "CONFIG_FILE", tmp_path / "c.json")
    import json
    (tmp_path / "c.json").write_text(json.dumps({
        "recipients": ["a@x.tw", "", "  ", None, "b@x.tw"],
    }), encoding="utf-8")
    assert cq.load_config()["recipients"] == ["a@x.tw", "b@x.tw"]


def test_load_config_times_non_list_falls_back(tmp_path, monkeypatch):
    """weekday_times 型別錯(字串) → 退回預設,不可讓排程整組消失。"""
    monkeypatch.setattr(cq, "CONFIG_FILE", tmp_path / "c.json")
    import json
    (tmp_path / "c.json").write_text(json.dumps({
        "weekday_times": "12:30",
    }), encoding="utf-8")
    assert (cq.load_config()["weekday_times"]
            == list(cq.DEFAULT_CONFIG["weekday_times"]))


# ─── _rebuild_schedule 排程建立 ──────────────────────────────────────────

def test_rebuild_schedule_creates_jobs_for_valid_times(monkeypatch):
    cfg = _base_cfg(enabled=True, weekday_times=["12:30"], weekend_times=["08:00"])
    monkeypatch.setattr(cq, "load_config", lambda: cfg)
    cq._rebuild_schedule()
    try:
        # 平日 5 天 × 1 時刻 + 假日 2 天 × 1 時刻 = 7 jobs
        assert len(cq.schedule.get_jobs()) == 7
    finally:
        cq.schedule.clear()


def test_rebuild_schedule_bad_time_format_no_raise(monkeypatch):
    """壞時間格式(缺冒號)不可炸掉排程器:記 error、該時刻不排。"""
    cfg = _base_cfg(enabled=True, weekday_times=["banana"], weekend_times=[])
    monkeypatch.setattr(cq, "load_config", lambda: cfg)
    cq._rebuild_schedule()  # 不可 raise
    try:
        assert len(cq.schedule.get_jobs()) == 0
    finally:
        cq.schedule.clear()


def test_rebuild_schedule_disabled_clears_jobs(monkeypatch):
    cfg = _base_cfg(enabled=False, weekday_times=["12:30"])
    monkeypatch.setattr(cq, "load_config", lambda: cfg)
    cq._rebuild_schedule()
    try:
        assert len(cq.schedule.get_jobs()) == 0
    finally:
        cq.schedule.clear()


# ─── 截圖檔案輪替 ────────────────────────────────────────────────────────

def test_prune_old_shots_keeps_newest(tmp_path, monkeypatch):
    monkeypatch.setattr(cq, "SHOTS_DIR", tmp_path)
    for i in range(cq.MAX_SHOT_FILES + 3):
        p = tmp_path / f"consult_2026{i:04d}.png"
        p.write_bytes(b"x")
        os.utime(p, (1000000 + i, 1000000 + i))  # 遞增 mtime
    cq._prune_old_shots()
    remain = sorted(tmp_path.glob("consult_*.png"))
    assert len(remain) == cq.MAX_SHOT_FILES
    # 留下的是最新的(mtime 最大的那批)
    assert (tmp_path / f"consult_2026{cq.MAX_SHOT_FILES + 2:04d}.png").exists()
    assert not (tmp_path / "consult_20260000.png").exists()


def test_prune_old_shots_missing_dir_no_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(cq, "SHOTS_DIR", tmp_path / "nope")
    cq._prune_old_shots()  # 不可 raise


# ─── scheduler 迴圈的 result 契約 ────────────────────────────────────────

def test_empty_imap_result_has_all_keys_scheduler_reads():
    """scheduler_loop 直接取 r['scanned']/r['matched']/r['samples'] 等 key;
    timeout/skip 路徑回的 _empty_imap_result 必須含全部 key,否則 KeyError
    殺掉整輪迴圈(雖有外層 except,但會默默跳過該輪觸發)。"""
    r = cq._empty_imap_result("boom")
    for key in ("triggered", "scanned", "matched", "matched_senders",
                "samples", "error"):
        assert key in r, key
    assert r["triggered"] is False
    assert r["matched_senders"] == []


# ─── [2026-06-13] 會診文字擷取 ───────────────────────────────────────────

def test_extracted_text_appended_to_mail_body(monkeypatch):
    """擷取到文字 → 附在信件內文(template 之後);沒擷取到 → 內文維持原樣。"""
    h = _JobHarness(monkeypatch, _base_cfg(body_template="本文"),
                    extracted_text="【病人 1】\n[內容1]\n蜂窩性組織炎")
    cq._do_full_job("17:00")
    assert h.bodies[0].startswith("本文")
    assert "蜂窩性組織炎" in h.bodies[0]

    h2 = _JobHarness(monkeypatch, _base_cfg(body_template="本文"),
                     extracted_text="")
    cq._do_full_job("17:00")
    assert h2.bodies[0] == "本文"


def test_find_text_panes_filters_and_sorts():
    """挑文字面板:只收 Memo/RichEdit/Edit 類、高度>=40,依上→下排序。"""
    kids = [
        (1, "TDBGrid", "", (0, 0, 400, 200)),        # 格線 → 不收
        (2, "TMemo", "", (0, 300, 400, 420)),         # 下面的 memo
        (3, "TEditExt", "", (0, 250, 200, 270)),      # 高度 20 單行 → 不收
        (4, "TRichEdit", "", (0, 210, 400, 290)),     # 上面的 richedit
        (5, "TButton", "確認", (0, 430, 60, 455)),    # 按鈕 → 不收
    ]
    panes = cq._find_text_panes(kids)
    assert [h for h, _c, _r in panes] == [4, 2]  # 上(210)→下(300)


def test_format_extracted_entries():
    out = cq._format_extracted_entries([
        [("內容1", "會診事項 A"), ("內容2", "病情摘要 A")],
        [("內容1", "  "), ("內容2", "")],            # 全空白 → 跳過
        [("內容1", "會診事項 B"), ("內容2", "")],     # 空面板略過、非空保留
    ])
    assert "【病人 1】" in out and "會診事項 A" in out and "病情摘要 A" in out
    assert "【病人 3】" in out and "會診事項 B" in out
    assert "【病人 2】" not in out
    assert cq._format_extracted_entries([]) == ""
    assert cq._format_extracted_entries([[("內容1", "")]]) == ""


# ─── [2026-06-15] 病人清單改用 TRadioButton 解析 ──────────────────────────

def test_find_patient_radios_picks_radiobuttons_only_and_sorts():
    """病人 = class 精確 TRadioButton 且文字帶病人結構(床號/房號/病歷號);
    TGroupButton(篩選選項)、空字串、無結構字串一律排除;依上→下、左→右排序。"""
    kids = [
        (10, "TRadioButton", "莊振銘B7(163)002958", (9, 130, 661, 151)),
        (11, "TGroupButton", "依醫師查詢", (263, 51, 424, 72)),   # 篩選選項 → 排除
        (12, "TRadioButton", "", (9, 110, 661, 131)),             # 空 → 排除
        (13, "TRadioButton", "全部", (9, 90, 661, 111)),          # 無病人結構 → 排除
        (14, "TRadioButton", "王小明A3(101)001234", (9, 170, 661, 191)),
        (15, "TRadioButton", "莊振銘B7(163)002958", (9, 130, 661, 151)),  # 同文字 → 去重
    ]
    radios = cq._find_patient_radios(kids)
    assert [h for h, _t, _r in radios] == [10, 14]  # 去重後;130 在 170 之前
    assert radios[0][1] == "莊振銘B7(163)002958"


def test_find_patient_radios_includes_foreign_names():
    """外籍病人(羅馬拼音、無中文)只要有床號/房號/病歷號結構也算病人,絕不可
    漏掉(漏病人=漏會診通知)。無結構的雜訊 radio 仍排除。"""
    kids = [
        (30, "TRadioButton", "NGUYEN VANB5(210)004222", (0, 0, 600, 21)),
        (31, "TRadioButton", "陳𠮷祥C2(205)003111", (0, 30, 600, 51)),  # Ext-B 姓名
        (32, "TRadioButton", "Select", (0, 60, 600, 81)),  # 無結構 → 排除
    ]
    radios = cq._find_patient_radios(kids)
    assert [t for _h, t, _r in radios] == [
        "NGUYEN VANB5(210)004222", "陳𠮷祥C2(205)003111"]


def test_patient_display_name_extracts_leading_cjk():
    assert cq._patient_display_name("莊振銘B7(163)002958") == "莊振銘"
    assert cq._patient_display_name("  王小明A3(101)  ") == "王小明"
    assert cq._patient_display_name("ABC123") == "ABC123"[:8]
    assert cq._patient_display_name("") == ""


def test_format_patient_roster():
    out = cq._format_patient_roster(
        ["莊振銘B7(163)002958", "  ", "王小明A3(101)001234"])
    assert "今日會診病人(2 位):" in out
    assert "1. 莊振銘B7(163)002958" in out
    assert "2. 王小明A3(101)001234" in out
    assert cq._format_patient_roster([]) == ""
    assert cq._format_patient_roster(["   ", ""]) == ""


def test_format_extracted_entries_with_named_labels():
    """提供 labels 時以姓名標題,且 labels 對齊原始 entries 索引(空項被跳過
    不影響非空項的標題對位)。"""
    out = cq._format_extracted_entries(
        [
            [("內容1", "蜂窩性組織炎")],
            [("內容1", "")],              # 空 → 跳過
            [("內容1", "帶狀疱疹")],
        ],
        labels=["莊振銘", "王小明", "李大華"])
    assert "【莊振銘】" in out and "蜂窩性組織炎" in out
    assert "【李大華】" in out and "帶狀疱疹" in out
    assert "【王小明】" not in out      # 內容空 → 整段跳過
    assert "【病人" not in out          # 有 label 就不用預設編號


def test_cleanup_excludes_user_instance_when_borrowed():
    """[review C2 fix] SW_HIDE 後備借用「使用者自己開的」systemftp 時，收尾
    不可替使用者關掉他的程式 —— 啟動前已存在的 pid 必須排除。"""
    before = {100, 200}
    our_pids = {100, 300}  # 100=借用的使用者實例, 300=本次起的子行程
    assert cq._cleanup_pids_excluding_borrowed(our_pids, before, borrowed=True) \
        == {300}


def test_cleanup_normal_mode_closes_all_our_pids():
    """非借用(正常路徑) → 維持原行為:本次開啟的全部關掉。"""
    before = {100}
    our_pids = {300, 301}
    assert cq._cleanup_pids_excluding_borrowed(our_pids, before, borrowed=False) \
        == {300, 301}


def test_cleanup_borrowed_with_only_user_instance_closes_nothing():
    """整組都是借來的(沒有任何新行程) → 收尾一個都不關(空集合安全)。"""
    before = {100, 200}
    assert cq._cleanup_pids_excluding_borrowed({100}, before, borrowed=True) \
        == set()


def test_normalize_retry_count_bounds():
    assert cq._normalize_retry_count(0) == cq.DEFAULT_CONFIG["retry_count"]  # 0=未設定→預設
    assert cq._normalize_retry_count(-5) == 1                # 負值夾到下限 1
    assert cq._normalize_retry_count(999) == cq.MAX_RETRY_COUNT
    assert cq._normalize_retry_count(None) == cq.DEFAULT_CONFIG["retry_count"]
    assert cq._normalize_retry_count("bad") == cq.DEFAULT_CONFIG["retry_count"]
