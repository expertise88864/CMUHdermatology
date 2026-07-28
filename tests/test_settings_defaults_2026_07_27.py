# -*- coding: utf-8 -*-
"""[2026-07-27 使用者需求] 設定頁「還原預設設定」+ 設定預設值的單一事實來源。

使用者原話:「主程式裡面設定新增一個返回預設設定功能(可以直接載入設定頁面預設的
設定,如原本預設醫師代號/原本預設止掛提醒email等等) 針對程式後續擴充性進行完整
深度優化處理」、「止掛提醒email 預設新增 lai.i.chang.58@gmail.com」。

本檔釘的性質分三類:
  1. 擴充規約 —— 新增設定檔只要加一個 SettingsGroup,UI 自動長出;預設值只有一處。
  2. 破壞性動作的安全性 —— 還原前一定備份,備份失敗就不覆蓋。
  3. ★最容易踩的坑★ 預設收件人不可讓「使用者刻意清空」復活。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common import settings_defaults as sd  # noqa: E402
from cmuh_common.app_settings import (  # noqa: E402
    clear_load_failed, load_threshold_settings, settings_load_failed,
)


def _conf_path(tmp):
    return lambda fn: os.path.join(str(tmp), fn)


def _read(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def _write(p, obj):
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


# ─── 預設值本身 ────────────────────────────────────────────────────────────
# [2026-08-02 使用者定案] 止掛提醒的預設收件人擴充為四人。
WANT_ALERT_RECIPIENTS = [
    "lai.i.chang.58@gmail.com",
    "expertise88864@gmail.com",
    "chilly840724@gmail.com",
    "mbpushowo@gmail.com",
]


def test_default_alert_recipients_are_the_requested_addresses():
    assert sd.DEFAULT_ALERT_EMAIL_RECIPIENTS == WANT_ALERT_RECIPIENTS
    assert (sd.default_threshold_settings()["alert_email_recipients"]
            == WANT_ALERT_RECIPIENTS)


def test_new_install_gets_the_default_recipients(tmp_path):
    """全新機器(檔案不存在)→ 直接帶入預設收件人。"""
    got = load_threshold_settings(str(tmp_path / "threshold.json"))
    assert got["alert_email_recipients"] == WANT_ALERT_RECIPIENTS


def test_developer_alert_email_is_declared_once():
    """★系統故障告警 vs 臨床通知是兩條線★ 開發者信箱只宣告一次,
    main.py 與 consult_query.py 都從這裡取,不各自硬編碼。"""
    assert sd.DEVELOPER_ALERT_EMAIL == "expertise88864@gmail.com"
    assert sd.developer_alert_recipients() == ["expertise88864@gmail.com"]
    a = sd.developer_alert_recipients()
    a.append("污染")
    assert sd.developer_alert_recipients() == ["expertise88864@gmail.com"]


def test_deliberately_emptied_recipients_must_not_be_resurrected(tmp_path):
    """★最容易踩的坑★ 使用者刻意把收件人清空 → 檔案裡是 `[]`(鍵存在)。
    合併預設的語意是 base.update(file),所以檔案有鍵就以檔案為準,不可復活。

    同一個坑在 smtp_mail 的帳密快取踩過一次(「成功讀到空設定要無條件清快取,
    否則使用者刻意清空後會被讀取失敗復活」)。"""
    p = str(tmp_path / "threshold.json")
    _write(p, {"alert_email_recipients": []})
    assert load_threshold_settings(p)["alert_email_recipients"] == []


def test_existing_recipients_win_over_default(tmp_path):
    p = str(tmp_path / "threshold.json")
    _write(p, {"alert_email_recipients": ["someone@example.com"]})
    assert (load_threshold_settings(p)["alert_email_recipients"]
            == ["someone@example.com"])


def test_legacy_dnd_hours_still_derive_times(tmp_path):
    """★回歸★ 把完整預設合進來之後,舊格式推導(`if key not in data`)會全部失效 ——
    舊機器的檔案往往只有 notify_dnd_*_hour,勿擾時段會被悄悄換成預設。
    故推導必須先對【原始檔案內容】做,最後才補預設。"""
    p = str(tmp_path / "threshold.json")
    _write(p, {"notify_dnd_start_hour": "bad", "notify_dnd_end_hour": 25})
    got = load_threshold_settings(p, dnd_start_hour=0, dnd_end_hour=8)
    assert got["notify_dnd_start_time"] == "00:00"
    assert got["notify_dnd_end_time"] == "24:00", "由檔案的 25 夾到 24,不是預設 08:00"


def test_threshold_defaults_cover_everything_save_writes():
    """★半套狀態防線★ 還原預設若漏掉設定頁會寫入的鍵,就會留下「一半預設、
    一半舊值」。這裡逐一列出 save_all_settings 實際寫進 threshold_settings.json
    的鍵,要求預設宣告全部涵蓋。"""
    written_by_save = {
        "alert_chang_enabled", "alert_chen_enabled", "out_of_hospital_mode",
        "ui_font_scale", "quick_text_f8", "alert_email_recipients",
    }
    defaults = sd.default_threshold_settings()
    missing = written_by_save - set(defaults)
    assert not missing, f"預設宣告漏了設定頁會寫入的鍵:{sorted(missing)}"
    from cmuh_common.threshold_policy import DEFAULT_THRESHOLDS
    assert set(DEFAULT_THRESHOLDS) <= set(defaults), "止掛門檻要全部涵蓋"


# ─── 擴充規約 ──────────────────────────────────────────────────────────────
def test_group_registry_is_the_single_source_for_ui():
    """設定頁的還原對話框直接走 SETTINGS_GROUPS —— 新增設定檔不必改 UI 程式碼。"""
    src = open(os.path.join(os.path.dirname(__file__), '..', 'src', 'main.py'),
               encoding='utf-8').read()
    i = src.index("def open_restore_defaults_dialog")
    j = src.index("def _restore_settings_defaults", i)
    body = src[i:j]
    assert "for g in SETTINGS_GROUPS:" in body, "群組要用迴圈長出,不可寫死"
    assert "_describe_settings_default(g.key)" in body, "要先讓使用者看到會變成什麼"


def test_every_group_has_a_usable_default_and_summary():
    for g in sd.SETTINGS_GROUPS:
        value = sd.default_for(g.key)
        assert value not in (None, ""), f"{g.key} 沒有可用預設"
        text = sd.describe(g.key)
        assert text and "無法產生摘要" not in text, f"{g.key} 摘要有問題"
        assert g.filename.endswith(".json")


def test_default_for_returns_a_fresh_object():
    """呼叫端改到回傳值不可污染共用預設(還原兩次要得到一樣的結果)。"""
    a = sd.default_for("doctors")
    a.append({"name": "污染", "doc_no": "X", "notifications": False})
    assert len(sd.default_for("doctors")) == len(sd.default_for("doctors"))
    assert all(d["name"] != "污染" for d in sd.default_for("doctors"))


# ─── 還原動作 ──────────────────────────────────────────────────────────────
def test_restore_writes_defaults_and_backs_up_original(tmp_path):
    p = _conf_path(tmp_path)("doctors.json")
    _write(p, [{"name": "自訂醫師", "doc_no": "X1", "notifications": False}])
    rep = sd.restore_defaults(["doctors"], conf_path=_conf_path(tmp_path))
    assert rep.ok and rep.restored == [("doctors", "doctors.json")]
    assert _read(p) == sd.default_for("doctors")
    assert len(rep.backups) == 1
    backup = rep.backups[0][1]
    assert ".before-reset-" in backup
    assert _read(backup)[0]["name"] == "自訂醫師", "原設定必須救得回來"


def test_restore_on_a_machine_with_no_file_yet(tmp_path):
    """新裝機器沒有設定檔 → 還是要能寫出預設,而且不該產生空備份。"""
    rep = sd.restore_defaults(["r_doctor"], conf_path=_conf_path(tmp_path))
    assert rep.ok and rep.backups == []
    assert os.path.exists(_conf_path(tmp_path)("r_doctor_settings.json"))


def test_backup_failure_must_not_overwrite(tmp_path, monkeypatch):
    """★沒有退路的破壞性寫入不可接受★ 備份失敗就不還原,原檔要原封不動。

    [2026-08-02 補審 P2] 備份已由 os.replace(搬移)改為 shutil.copy2(複製)——
    搬移會讓正式檔在寫入預設值【之前】就消失,寫入失敗時設定就整個不見。
    """
    p = _conf_path(tmp_path)("doctors.json")
    _write(p, [{"name": "自訂醫師", "doc_no": "X1", "notifications": False}])
    monkeypatch.setattr(sd.shutil, "copy2",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("locked")))
    rep = sd.restore_defaults(["doctors"], conf_path=_conf_path(tmp_path))
    assert not rep.ok and rep.restored == []
    assert rep.failures[0][0] == "doctors"
    assert "備份" in rep.failures[0][1]
    assert _read(p)[0]["name"] == "自訂醫師", "原檔不可被動到"


def test_write_failure_is_reported_not_raised(tmp_path, monkeypatch):
    """atomic_write_json 的契約是「成功回 None、失敗丟例外」。
    (我第一版寫成 `if not atomic_write_json(...)`,把每一次成功都當成失敗。)"""
    monkeypatch.setattr(sd, "atomic_write_json",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    rep = sd.restore_defaults(["doctors"], conf_path=_conf_path(tmp_path))
    assert not rep.ok and rep.restored == []
    assert "例外" in rep.failures[0][1]


def test_successful_write_is_reported_as_success(tmp_path):
    """★反向釘死★ atomic_write_json 成功時回 None —— 不可被判成失敗。"""
    rep = sd.restore_defaults(["doctors"], conf_path=_conf_path(tmp_path))
    assert rep.ok and rep.failures == []
    assert rep.restored == [("doctors", "doctors.json")]


def test_unknown_group_is_reported_not_raised(tmp_path):
    rep = sd.restore_defaults(["不存在的群組"], conf_path=_conf_path(tmp_path))
    assert not rep.ok and "未知" in rep.failures[0][1]


def test_partial_selection_only_touches_selected_files(tmp_path):
    other = _conf_path(tmp_path)("threshold_settings.json")
    _write(other, {"chang_mon_night": 999})
    sd.restore_defaults(["doctors"], conf_path=_conf_path(tmp_path))
    assert _read(other) == {"chang_mon_night": 999}, "沒勾的檔案不可被動到"


# ─── 與「拒絕存檔」守衛的互動 ─────────────────────────────────────────────
def test_restore_clears_the_refuse_to_save_guard(tmp_path):
    """讀不到設定檔時會啟動「拒絕存檔」保護。使用者【明確】按了還原預設(而且原檔
    已備份)之後,那道保護必須解除,否則他重置完卻永遠不能按儲存。"""
    from cmuh_common import app_settings as a
    a._LOAD_FAILED_FILES.add("doctors.json")
    assert "doctors.json" in settings_load_failed()
    clear_load_failed("doctors.json")
    assert "doctors.json" not in settings_load_failed()


def test_main_only_clears_guard_for_successfully_restored_groups():
    """★寫入失敗就不可解除保護★ main 的迴圈必須跑 report.restored,
    不是使用者勾選的清單。"""
    src = open(os.path.join(os.path.dirname(__file__), '..', 'src', 'main.py'),
               encoding='utf-8').read()
    i = src.index("def _restore_settings_defaults(self, keys)")
    body = src[i:i + 1200]
    assert "for key, _fn in report.restored:" in body
    assert "_clear_settings_load_failed(" in body
