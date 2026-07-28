# -*- coding: utf-8 -*-
"""[2026-08-02 補審第 3 輪] Codex 對 00121b0..HEAD 的 4 條 finding。

兩條是我引進的嚴重問題:
  P1 定位掃描可能把熱鍵鎖住十幾分鐘(而它只在「HIS 沒回應」時才觸發)。
  P1 民國日期沒錨定 → 院方改格式時【生日】會被當成日期外送,違反核心隱私承諾。
另兩條:
  P2 病歷號被寫進沒有保存期限的一般 log。
  P2 還原預設寫入失敗會先把正式設定檔刪掉。
"""
import os
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common import patient_locator as pl  # noqa: E402
from cmuh_common import settings_defaults as sd  # noqa: E402

REAL = "1150728 早上 103診 113號 -呂冠愷(24994923)女 42歲1月 (0730623) #C0024322"


def _code_only(src: str) -> str:
    src = re.sub(r'("""|\'\'\')(?:.|\n)*?\1', "", src)
    return "\n".join(line.split("#")[0] for line in src.splitlines())


def _main_src():
    return open(os.path.join(os.path.dirname(__file__), '..', 'src', 'main.py'),
                encoding='utf-8').read()


# ─── P1:生日不可被當成民國日期 ───────────────────────────────────────────
def test_birthday_cannot_become_date_roc_when_header_drifts():
    """★核心隱私防線★ 橫幅本來就含七位的【生日】(0730623)。原本用「任何獨立
    七位數」當日期,只要院方把開頭日期改成西元八碼/斜線/暫時空白,生日就會滿足
    banner 條件並被輸出成 date_roc → 進告警信與索引檔。"""
    for drifted in (
            "20260728 早上 103診 113號 -某人(24994923)女 42歲1月 (0730623)",
            "115/07/28 早上 103診 113號 -某人(24994923)女 42歲1月 (0730623)",
            " 早上 103診 113號 -某人(24994923)女 42歲1月 (0730623)"):
        loc = pl.parse_banner(drifted)
        blob = "" if loc is None else str(loc)
        assert "0730623" not in blob, f"生日外洩:{drifted[:24]}"


def test_valid_roc_date_rejects_a_birthday_shaped_number():
    assert pl._valid_roc_date("1150728") is True
    assert pl._valid_roc_date("0730623") is False, "民國年 073 → 不是本世紀的日期"
    assert pl._valid_roc_date("1151328") is False, "月份 13"
    assert pl._valid_roc_date("1150732") is False, "日 32"
    assert pl._valid_roc_date("abcdefg") is False


def test_normal_banner_still_parses():
    """不可矯枉過正:正常格式仍要完整解析。"""
    assert pl.parse_banner(REAL) == {
        "date_roc": "1150728", "session": "早上", "room": "103",
        "seq": "113", "chart_no": "24994923", "visit_no": "C0024322"}


# ─── P1:掃描不可鎖住熱鍵 ─────────────────────────────────────────────────
def test_scan_budget_is_tiny_compared_to_the_default_timeout():
    """400 控件 × 預設 2.5s ≈ 1,000 秒。整體預算必須遠小於這個數量級。"""
    assert pl.SCAN_TOTAL_BUDGET_SEC <= 3.0
    assert pl.SCAN_PER_CONTROL_TIMEOUT_MS <= 300
    worst = pl.MAX_CONTROLS_TO_SCAN * pl.SCAN_PER_CONTROL_TIMEOUT_MS / 1000
    assert pl.SCAN_TOTAL_BUDGET_SEC < worst, \
        "整體 deadline 必須真的比「逐控件逾時 × 上限」更早生效"


def test_sampler_has_deadline_and_cancel_check():
    code = _code_only(_main_src())
    i = code.index("def _sample_patient_locator(")
    body = code[i:i + 2500]
    assert "_deadline = time.monotonic()" in body
    assert "if time.monotonic() > _deadline:" in body
    assert "check_stop()" in body, "F12 取消要能立刻脫身"
    assert "timeout_ms=_LOCATOR_CONTROL_TIMEOUT_MS" in body, "不可用預設 2.5 秒"


# ─── P2:一般 log 不可含病歷號 ────────────────────────────────────────────
def test_general_log_gets_no_chart_number():
    loc = pl.parse_banner(REAL)
    assert "24994923" not in pl.format_for_log(loc)
    assert "24994923" in pl.format_for_alert(loc), "告警信仍要有(那是使用者要的)"
    assert "103" in pl.format_for_log(loc) and "113" in pl.format_for_log(loc)


def test_main_logs_via_the_safe_formatter():
    code = _code_only(_main_src())
    i = code.index("def _notify_audit_mismatch(")
    body = code[i:i + 2000]
    i_log = body.index("logging.warning")
    seg = body[i_log:i_log + 400]
    assert "_format_patient_locator_safe(locator)" in seg, \
        "一般 log 要走不含病歷號的版本"
    assert "_loc_text" not in seg, "★不可把含病歷號的字串寫進一般 log★"


# ─── P2:還原預設失敗不可弄丟正式設定檔 ──────────────────────────────────
def test_restore_failure_leaves_the_original_intact(tmp_path, monkeypatch):
    """★備份要用複製不是搬移★ 原本 os.replace 先把正式檔搬走,寫入失敗時
    設定檔就整個不見 —— 而 main 仍會重載設定,loader 對缺檔一律套預設,
    等於「還原失敗」卻造成比還原更嚴重的後果。"""
    import json
    target = tmp_path / "doctors.json"
    target.write_text(json.dumps([{"name": "自訂醫師", "doc_no": "X1"}]),
                      encoding="utf-8")
    monkeypatch.setattr(sd, "atomic_write_json",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    rep = sd.restore_defaults(["doctors"],
                              conf_path=lambda fn: str(tmp_path / fn))
    assert not rep.ok and rep.restored == []
    assert target.exists(), "★正式設定檔必須還在★"
    assert json.loads(target.read_text(encoding="utf-8"))[0]["name"] == "自訂醫師"


def test_backup_is_a_copy_not_a_move(tmp_path):
    import json
    target = tmp_path / "doctors.json"
    target.write_text(json.dumps([{"name": "自訂醫師", "doc_no": "X1"}]),
                      encoding="utf-8")
    rep = sd.restore_defaults(["doctors"],
                              conf_path=lambda fn: str(tmp_path / fn))
    assert rep.ok
    assert target.exists(), "還原後正式檔存在(已是預設值)"
    backup = rep.backups[0][1]
    assert json.loads(open(backup, encoding="utf-8").read())[0]["name"] == "自訂醫師"


# ─── 補審第 2 次的兩條 ────────────────────────────────────────────────────
def test_backup_never_leaves_a_partial_file(tmp_path, monkeypatch):
    """★copy2 不是原子的★ 中途失敗留下半截 dest,同秒同 PID 再按一次時
    `os.path.exists(dest)` 會把它當成有效備份 → 接著用預設覆蓋正式檔,
    使用者實際上沒有任何可用備份。"""
    import json
    target = tmp_path / "doctors.json"
    target.write_text(json.dumps([{"name": "自訂醫師"}]), encoding="utf-8")

    def _half_written(src, dst, *a, **k):
        open(dst, "wb").write(b"HALF")       # 模擬寫到一半
        raise OSError("disk full")

    monkeypatch.setattr(sd.shutil, "copy2", _half_written)
    rep = sd.restore_defaults(["doctors"], conf_path=lambda fn: str(tmp_path / fn))
    assert not rep.ok
    leftovers = [f for f in os.listdir(tmp_path) if ".before-reset-" in f]
    assert leftovers == [], f"不可留下半截備份:{leftovers}"
    assert json.loads(target.read_text(encoding="utf-8"))[0]["name"] == "自訂醫師"


def test_f12_cancels_sampling_only_not_the_audit_record():
    """★F12 只中止採樣★ check_stop() 丟 SubsystemInterrupted,若往上拋會被
    _record_his_action 的廣義 except 吞掉 → 整筆稽核紀錄、定位索引與告警信
    全部消失。醫師按取消,系統不該把「剛剛寫錯了」這件事一起忘掉。"""
    code = _code_only(_main_src())
    i = code.index("def _sample_patient_locator(")
    body = code[i:i + 2500]
    i_chk = body.index("check_stop()")
    seg = body[i_chk:i_chk + 300]
    assert "except SubsystemInterrupted:" in seg
    assert "return None" in seg
    assert "raise" not in seg, "★不可往上拋★"
