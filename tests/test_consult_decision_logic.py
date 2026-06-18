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
        self.bodies = []        # 寄出的純文字內文
        self.html_bodies = []   # 寄出的 HTML 內文
        self.extracted_text = extracted_text
        self.flow_runs = 0
        self.kills = 0
        self.sleeps = []
        self.released = []      # _release_trigger_dedup 收到的 senders
        self.failure_notices = []  # (recipients, reason)
        self.punch_text = ""       # stub 的打卡狀態純文字段落
        self.punch_html = ""       # stub 的打卡狀態 HTML 段落
        self._fail_times = fail_times

        monkeypatch.setattr(cq, "load_config", lambda: dict(cfg))
        monkeypatch.setattr(smtp_mail, "is_configured", lambda: True)

        def _flow(label=""):
            self.flow_runs += 1
            if self.flow_runs <= self._fail_times:
                raise RuntimeError(f"simulated failure #{self.flow_runs}")
            # [2026-06-15] run_consult_flow 改回傳 (截圖, 擷取純文字, 擷取HTML)
            return Path("C:/fake/shot.png"), self.extracted_text, ""
        monkeypatch.setattr(cq, "run_consult_flow", _flow)
        monkeypatch.setattr(
            cq, "send_via_smtp",
            lambda shot, subject, body, recipients, html_body="":
                self.sent.append((list(recipients), subject))
                or self.bodies.append(body)
                or self.html_bodies.append(html_body))
        monkeypatch.setattr(
            cq, "send_via_outlook",
            lambda shot, subject, body, recipients, sender_account="",
            html_body="": self.sent.append((list(recipients), subject)))
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
        # [2026-06-15] 打卡狀態查詢會起 Chrome,測決策邏輯時 stub 掉(回空段落)。
        self.punch_calls = 0

        def _stub_punch(cfg):
            self.punch_calls += 1
            return self.punch_text, self.punch_html
        monkeypatch.setattr(cq, "_build_punch_status_sections", _stub_punch)


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


# ─── [2026-06-17] 今日打卡狀態:排程+手動查/附,只有 email 省略 ────────────

def test_scheduled_trigger_includes_punch_status(monkeypatch):
    """排程(HH:MM)觸發 → 查並把今日打卡狀態併入信件(純文字+HTML)。"""
    h = _JobHarness(monkeypatch, _base_cfg())
    h.punch_text = "PUNCH_TEXT_MARK"
    h.punch_html = "<i>PUNCH_HTML_MARK</i>"
    cq._do_full_job("12:40")
    assert h.punch_calls == 1                      # 有查打卡(登入 portal)
    assert "PUNCH_TEXT_MARK" in h.bodies[0]
    assert "PUNCH_HTML_MARK" in h.html_bodies[0]


def test_manual_trigger_includes_punch_status(monkeypatch):
    """[2026-06-17 user 要求] 手動觸發 → 也查並附今日打卡狀態(同排程)。"""
    h = _JobHarness(monkeypatch, _base_cfg())
    h.punch_text = "PUNCH_TEXT_MARK"
    h.punch_html = "<i>PUNCH_HTML_MARK</i>"
    cq._do_full_job("手動")
    assert h.punch_calls == 1
    assert "PUNCH_TEXT_MARK" in h.bodies[0]
    assert "PUNCH_HTML_MARK" in h.html_bodies[0]


def test_email_trigger_skips_punch_status(monkeypatch):
    """email(皮膚科會診觸發) → 唯一省略打卡的觸發:不查(不登入 portal)、信件不附。"""
    h = _JobHarness(monkeypatch, _base_cfg())
    h.punch_text = "PUNCH_TEXT_MARK"
    h.punch_html = "<i>PUNCH_HTML_MARK</i>"
    cq._do_full_job("email", override_recipients=["dr.wang@x.tw"])
    assert h.punch_calls == 0                      # 連打卡查詢都沒呼叫
    assert "PUNCH_TEXT_MARK" not in h.bodies[0]
    assert "PUNCH_HTML_MARK" not in h.html_bodies[0]


def test_is_email_trigger_classification():
    assert cq._is_email_trigger("email") is True
    assert cq._is_email_trigger("12:40") is False
    assert cq._is_email_trigger("17:10") is False
    assert cq._is_email_trigger("手動") is False
    assert cq._is_email_trigger("") is False


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

    def _send_fail_once(shot, subject, body, recipients, html_body=""):
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


def test_format_patient_roster_label_param():
    """[美化] label 依時段帶入;預設維持舊行為(現有 UI/相容)。"""
    out = cq._format_patient_roster(["王X"], label="下午會診清單")
    assert out.startswith("下午會診清單(1 位):")
    assert cq._format_patient_roster(["王X"]).startswith("今日會診病人(1 位):")


# ─── 信件美化:時段標題 / 病人列解析 / HTML 排版 ──────────────────────────

def test_consult_slot_label():
    """12:30→昨晚今早;17:30→下午;email/手動用 now 時鐘;壞 label 退回 now。"""
    from datetime import datetime
    noon = datetime(2026, 6, 15, 12, 30)
    eve = datetime(2026, 6, 15, 17, 30)
    assert cq._consult_slot_label("12:30", noon) == "昨晚今早會診清單"
    assert cq._consult_slot_label("17:30", eve) == "下午會診清單"
    assert cq._consult_slot_label("email", datetime(2026, 6, 15, 9, 0)) == "昨晚今早會診清單"
    assert cq._consult_slot_label("", datetime(2026, 6, 15, 16, 0)) == "下午會診清單"
    assert cq._consult_slot_label("bad", noon) == "昨晚今早會診清單"


def test_parse_roster_row_structured():
    p = cq._parse_roster_row("莊振銘B7(163)0029588049(沈冠宇)06/15(08:20)")
    assert p["name"] == "莊振銘"
    assert p["ward_bed"] == "B7 · 163"
    assert p["chart"] == "0029588049"
    assert p["vs"] == "沈冠宇"
    assert p["date"] == "06/15"
    assert p["time"] == "08:20"


def test_parse_roster_row_alphanumeric_bed():
    """床號含英數(如 18A)也要解析得出 —— 否則整列走 raw fallback 會擠成一團。"""
    p = cq._parse_roster_row("簡志仲I8(18A)0042107068(謝佳陵)06/17(11:23)")
    assert p is not None
    assert p["name"] == "簡志仲"
    assert p["ward_bed"] == "I8 · 18A"
    assert p["chart"] == "0042107068"
    assert p["vs"] == "謝佳陵"
    assert p["date"] == "06/17"
    assert p["time"] == "11:23"


def test_parse_roster_row_letter_only_ward():
    """[2026-06-18] 純字母病房(如燒燙傷病房 BURN)也要解析 —— 原本 ward 正規式要求
    字母後接數字(C16/B7),BURN 沒數字 → 整列 fullmatch 失敗 → 跑版擠成一團、
    且會診內容標題缺病房床位。修法:ward 數字改為可有可無。"""
    p = cq._parse_roster_row("賴義恩BURN(10B)0000416350(蔡李澄)06/18(11:27)")
    assert p is not None
    assert p["name"] == "賴義恩"
    assert p["ward_bed"] == "BURN · 10B"
    assert p["chart"] == "0000416350"
    assert p["vs"] == "蔡李澄"
    assert p["date"] == "06/18"
    assert p["time"] == "11:27"
    # 逐病人標題也要帶回病房/床位
    name, meta = cq._patient_head("賴義恩BURN(10B)0000416350(蔡李澄)06/18(11:27)")
    assert name == "賴義恩"
    assert "BURN · 10B" in meta and "0000416350" in meta and "06/18 11:27" in meta


def test_patient_head_name_plus_meta():
    """逐病人標題:姓名 + 床位/病歷號/日期時間。解析不出結構 → 僅顯示簡名。"""
    name, meta = cq._patient_head("簡志仲I8(18A)0042107068(謝佳陵)06/17(11:23)")
    assert name == "簡志仲"
    assert "I8 · 18A" in meta and "0042107068" in meta and "06/17 11:23" in meta
    # 純姓名(無結構)→ 回 (姓名, "")
    assert cq._patient_head("王小明") == ("王小明", "")


def test_extracted_entries_head_has_bed_chart_time():
    """會診內容標題要帶床位/病歷號/時間(文字版與 HTML 版皆然)。"""
    entries = [[("內容1", "癢")]]
    labels = ["簡志仲I8(18A)0042107068(謝佳陵)06/17(11:23)"]
    txt = cq._format_extracted_entries(entries, labels=labels)
    assert "簡志仲" in txt and "0042107068" in txt and "06/17 11:23" in txt
    html = cq._format_extracted_entries_html(entries, labels=labels)
    assert "簡志仲" in html and "0042107068" in html and "18A" in html


def test_parse_roster_row_fallback():
    """外籍(無中文姓名)或結構太弱 → None,呼叫端改顯示原字串、絕不漏人。"""
    assert cq._parse_roster_row("JOHN SMITH 0012345678") is None
    assert cq._parse_roster_row("王小明") is None          # 無病歷號/床號
    assert cq._parse_roster_row("") is None
    # [codex review] 尾端有未預期文字 → fullmatch 失敗 → None(改顯示原字串,
    # 不靜默丟掉尾端資訊)
    assert cq._parse_roster_row(
        "莊振銘B7(163)0029588049(沈冠宇)06/15(08:20)備註XYZ") is None
    # 該整列原字串會在 HTML 走 raw fallback、完整保留
    out = cq._format_patient_roster_html(
        ["莊振銘B7(163)0029588049(沈冠宇)06/15(08:20)備註XYZ"], "下午會診清單")
    assert "備註XYZ" in out


def test_format_patient_roster_html():
    out = cq._format_patient_roster_html(
        ["莊振銘B7(163)0029588049(沈冠宇)06/15(08:20)", "JOHN 0099"],
        "昨晚今早會診清單")
    assert "昨晚今早會診清單" in out and "2 位" in out
    assert "莊振銘" in out and "0029588049" in out
    assert "JOHN 0099" in out          # 無法解析 → 原字串(colspan)保留
    assert "<table" in out
    assert cq._format_patient_roster_html([], "x") == ""


def test_format_extracted_entries_html():
    entries = [[("內容1", "For biopsy"), ("內容2", "line1\nline2")]]
    out = cq._format_extracted_entries_html(entries, labels=["莊振銘"])
    assert "莊振銘" in out
    assert "會診原因" in out and "For biopsy" in out
    assert "病情摘要" in out and "line1<br>line2" in out   # 換行 → <br>
    assert "會診內容" in out


def test_format_extracted_entries_html_escapes_and_empty():
    out = cq._format_extracted_entries_html([[("內容2", "a<b>&c")]], labels=["X"])
    assert "&lt;b&gt;" in out and "&amp;c" in out and "<b>" not in out
    assert cq._format_extracted_entries_html([]) == ""
    assert cq._format_extracted_entries_html([[("內容1", "")]]) == ""


def test_fmt_mail_datetime():
    assert cq._fmt_mail_datetime("2026/6/15", "1230") == "2026 年 6 月 15 日　12:30"
    # 解析失敗(非預期格式)→ 原樣串接,不丟例外
    assert "weird" in cq._fmt_mail_datetime("weird", "nope")
    # [codex review] None/數字等非預期型別不可拋例外(在送信路徑)
    assert cq._fmt_mail_datetime(None, None) == ""
    assert "17:30" in cq._fmt_mail_datetime(20260615, 1730)   # 1730 → 17:30


def test_build_consult_email_html():
    out = cq._build_consult_email_html("2026/6/15", "1230", "intro <line>",
                                       "<p>x</p>")
    assert "會診通知單" in out and "皮膚科會診系統" in out   # letterhead
    assert "2026 年 6 月 15 日" in out and "12:30" in out   # 日期/時間美化
    assert "intro &lt;line&gt;" in out   # intro 也 escape
    assert "<p>x</p>" in out             # content 是我們產生的安全 HTML,原樣嵌入
    # [2026-06-17] 頁尾「本信由…正式內容以附件截圖為準」已移除(user 要求)
    assert "正式內容以附件" not in out
    assert "本信由中國醫皮膚科系統" not in out
    # 手機可讀性:viewport + media query(響應式)
    assert 'name="viewport"' in out and "width=device-width" in out
    assert "@media only screen and (max-width:600px)" in out


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


# ─── [2026-06-15] 排程時間自動升級 12:30/17:00 → 12:31/17:01 ──────────────

def test_load_config_migrates_both_old_default_times(monkeypatch, tmp_path):
    """沿用任一代舊預設(12:30/17:00 或 12:31/17:01)→ 升級為 12:40/17:10 並寫回。"""
    import json
    for idx, old in enumerate((["12:30", "17:00"], ["12:31", "17:01"])):
        p = tmp_path / f"cfg_{idx}.json"
        p.write_text(json.dumps({"weekday_times": old, "weekend_times": old}),
                     encoding="utf-8")
        monkeypatch.setattr(cq, "CONFIG_FILE", p)
        cfg = cq.load_config()
        assert cfg["weekday_times"] == ["12:40", "17:10"]
        assert cfg["weekend_times"] == ["12:40", "17:10"]
        saved = json.loads(p.read_text(encoding="utf-8"))   # 已寫回升級
        assert saved["weekday_times"] == ["12:40", "17:10"]


def test_load_config_keeps_custom_times(monkeypatch, tmp_path):
    """使用者自訂過的時間(非任一代舊預設)不可被升級覆蓋。"""
    import json
    p = tmp_path / "consult_query_config.json"
    p.write_text(json.dumps({"weekday_times": ["12:00", "16:00"],
                             "weekend_times": ["09:00"]}), encoding="utf-8")
    monkeypatch.setattr(cq, "CONFIG_FILE", p)
    cfg = cq.load_config()
    assert cfg["weekday_times"] == ["12:00", "16:00"]
    assert cfg["weekend_times"] == ["09:00"]


# ─── [2026-06-15] 今日打卡狀態併入信件 ────────────────────────────────────

def test_format_punch_text_states():
    results = [
        {"username": "101358", "on": "ok", "on_time": "08:15",
         "off": "ok", "off_time": "17:05", "error": None},
        {"username": "D34251", "on": "fail", "on_time": None,
         "off": "off", "off_time": None, "error": None},
        {"username": "N24367", "on": None, "on_time": None,
         "off": None, "off_time": None, "error": "登入逾時/失敗"},
    ]
    out = cq._format_punch_text(results)
    assert "今日打卡狀態（3 個帳號" in out
    assert "101358" in out and "✅ 成功（08:15）" in out
    assert "D34251" in out and "❌ 未打卡" in out and "➖ 今日無排班" in out
    assert "N24367" in out and "⚠️ 查詢失敗（登入逾時/失敗）" in out
    assert cq._format_punch_text([]) == ""


def test_format_punch_html_states_and_escaping():
    results = [
        {"username": "101358", "on": "ok", "on_time": "08:15",
         "off": "fail", "off_time": None, "error": None},
        {"username": "N24367", "on": None, "on_time": None,
         "off": None, "off_time": None, "error": "<bad&>"},
    ]
    html = cq._format_punch_html(results)
    assert "今日打卡狀態" in html and "2 個帳號" in html
    assert "101358" in html and "成功" in html and "未打卡" in html
    # 錯誤訊息必須 HTML escape(防注入)
    assert "&lt;bad&amp;&gt;" in html and "<bad&>" not in html
    assert cq._format_punch_html([]) == ""


def test_load_autoclock_accounts_dedups_and_filters(monkeypatch):
    monkeypatch.setattr(cq, "safe_load_json", lambda *a, **k: [
        {"username": "101358", "password": "a"},
        {"username": "101358", "password": "b"},   # 重複 username → 去掉
        {"username": "D34251", "password": "c"},
        {"no_username": True},                       # 無 username → 略過
        "not-a-dict",                                # 非 dict → 略過
    ])
    out = cq._load_autoclock_accounts()
    assert [a["username"] for a in out] == ["101358", "D34251"]


def test_punch_text_cell_labels():
    assert cq._punch_text_cell("ok", "08:15") == "✅ 成功（08:15）"
    assert cq._punch_text_cell("ok", None) == "✅ 成功"
    assert cq._punch_text_cell("fail", None) == "❌ 未打卡"
    assert cq._punch_text_cell("off", None) == "➖ 今日無排班"


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
