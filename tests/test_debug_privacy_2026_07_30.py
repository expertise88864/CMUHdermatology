# -*- coding: utf-8 -*-
"""[2026-07-30 第二輪外審 P1-02] 除錯檔可能永久保存帳密與頁面內容。

TTL 那一半已在 v2026.07.30.4 做掉(`cmuh_common/retention.py`)——但「三天後會刪」
不等於「這三天可以隨便存」。這一檔管的是【存什麼】:

  * **檔名**:舊版 `f"{task_label}_{username}"`,帳號直接寫在檔名上;而檔名還會被
    塞進 Windows 通知、出現在資料夾清單與任何拍到檔案總管的截圖裡。
  * **page_source HTML**:整頁原始碼(登入頁的帳號欄 value、打卡紀錄表格)。
  * **screenshot**:登入頁截圖會把【帳號欄的明文】拍進去。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common import debug_privacy as dp  # noqa: E402


# ─── 檔名不含帳號 ──────────────────────────────────────────────────────────
def test_the_account_tag_does_not_contain_the_account():
    tag = dp.account_tag("A123456789")
    assert "A123456789" not in tag
    assert "a123456789" not in tag.lower()
    assert len(tag) == dp.ACCOUNT_TAG_LEN


def test_the_account_tag_is_stable_and_distinguishes_accounts():
    """除錯時仍要分得出「這批檔是同一個人的」。"""
    assert dp.account_tag("user1") == dp.account_tag("user1")
    assert dp.account_tag("USER1") == dp.account_tag("user1"), "大小寫視為同一人"
    assert dp.account_tag("user1") != dp.account_tag("user2")


def test_a_missing_account_still_produces_a_usable_tag():
    assert dp.account_tag("") == "anon"
    assert dp.account_tag(None) == "anon"


def test_autoclock_filenames_use_the_tag_not_the_username():
    """★真正的接線★ 純函式對了但呼叫端還是塞帳號的話，什麼都沒改善。"""
    import inspect

    import autoclock
    src = inspect.getsource(autoclock._handle_clock_failure)
    assert "account_tag(username)" in src
    assert '{task_label}_{username}' not in src
    assert 'fail_{username}' not in src


# ─── 預設不存整頁原始碼 ────────────────────────────────────────────────────
def test_page_source_is_not_stored_by_default(tmp_path):
    p = tmp_path / "privacy.json"
    assert dp.store_page_source_enabled(p) is False
    assert dp.DEFAULT_PRIVACY["store_page_source"] is False


def test_page_source_can_be_turned_on_explicitly(tmp_path):
    p = tmp_path / "privacy.json"
    assert dp.save_privacy_settings({"store_page_source": True}, p) is True
    assert dp.store_page_source_enabled(p) is True
    assert json.loads(p.read_text(encoding="utf-8"))["store_page_source"] is True


def test_an_unreadable_privacy_file_falls_back_to_not_storing(tmp_path,
                                                             monkeypatch):
    """★讀不到時要往【隱私安全】的方向倒★

    別處的慣例是「暫時讀不到就沿用上次成功值」，那是為了不要讓寄信/告警靜默停擺
    （少做事＝壞事）。這裡相反：讀不到時多做事（落地整頁原始碼）才是壞事。
    """
    p = tmp_path / "privacy.json"
    dp.save_privacy_settings({"store_page_source": True}, p)
    assert dp.store_page_source_enabled(p) is True

    monkeypatch.setattr(dp, "safe_load_json_ex",
                        lambda *_a, **_k: ({}, "error"))
    assert dp.store_page_source_enabled(p) is False


def test_a_corrupt_privacy_file_falls_back_to_not_storing(tmp_path):
    p = tmp_path / "privacy.json"
    p.write_text("{ this is not json", encoding="utf-8")
    assert dp.store_page_source_enabled(p) is False


def test_autoclock_only_writes_html_when_explicitly_enabled():
    import inspect

    import autoclock
    src = inspect.getsource(autoclock.save_debug_artifacts)
    assert "store_page_source_enabled()" in src
    # ★比對 `driver.page_source` 而不是字串 "page_source"★
    #   `store_page_source_enabled` 本身就含 "page_source"，拿它比位置會比到
    #   import 那一行，永遠成立（實測弄紅了這支測試）。
    assert src.index("store_page_source_enabled()") < src.index("driver.page_source")


# ─── 截圖前清空憑證,而且要回讀 ────────────────────────────────────────────
class _Driver:
    """假的 driver：`remaining` 就是 JS 回讀「還剩幾個非空欄位」的結果。"""

    def __init__(self, remaining=0, raises=False):
        self.remaining = remaining
        self.raises = raises
        self.calls = 0

    def execute_script(self, _js, *_a):
        self.calls += 1
        if self.raises:
            raise RuntimeError("JS 炸了")
        return self.remaining


def test_a_verified_blank_allows_the_screenshot():
    assert dp.blank_credential_fields(_Driver(remaining=0), ["u", "p"]) is True


def test_fields_that_cannot_be_cleared_block_the_screenshot():
    """★fail-closed★ 我們無法證明它安全，就不要落地。"""
    assert dp.blank_credential_fields(_Driver(remaining=1), ["u"]) is False


def test_a_failing_readback_blocks_the_screenshot():
    assert dp.blank_credential_fields(_Driver(raises=True), ["u"]) is False


def test_an_unreadable_result_blocks_the_screenshot():
    """回讀結果判讀不了 → 也算不能證明（不可當成 0）。"""
    d = _Driver()
    d.remaining = "??"
    assert dp.blank_credential_fields(d, ["u"]) is False


def test_no_driver_blocks_the_screenshot():
    assert dp.blank_credential_fields(None, ["u"]) is False


def test_the_javascript_reads_back_rather_than_assuming():
    """★這支 repo 的老病灶就是「送出去就當成功」★
    JS 必須在清完之後【再讀一次】value，而不是清完就回 0。"""
    js = dp._CLEAR_AND_VERIFY_JS
    assert js.index("els[k].value = ''") < js.index("if (els[m].value)")
    assert "remaining" in js


def test_autoclock_skips_the_screenshot_when_it_cannot_verify():
    import inspect

    import autoclock
    src = inspect.getsource(autoclock.save_debug_artifacts)
    assert "blank_credential_fields(driver, cred_ids)" in src
    assert src.index("blank_credential_fields") < src.index("save_screenshot")
    assert "screenshot_skipped" in src


def test_autoclock_always_writes_the_meta_even_when_nothing_else_was_saved():
    """截圖與 HTML 都沒存時，現場不該只看到一個空資料夾 ——
    要留下「為什麼沒存」與錯誤訊息。"""
    import inspect

    import autoclock
    src = inspect.getsource(autoclock.save_debug_artifacts)
    tail = src[src.index("meta 一律寫"):]
    assert "meta_path.write_text" in tail
    assert "notes" in tail


# ─── 已知機敏字串取代 ──────────────────────────────────────────────────────
def test_known_secrets_are_replaced_in_meta_text():
    out = dp.redact_secrets("登入失敗 account=A123456789 ok", ["A123456789"])
    assert "A123456789" not in out
    assert "<redacted>" in out


def test_redaction_is_case_insensitive_for_the_known_value():
    out = dp.redact_secrets("user a123456789 failed", ["A123456789"])
    assert "a123456789" not in out


def test_very_short_secrets_are_not_replaced():
    """帳號是 'abc' 就把訊息裡每個 abc 都換掉，反而讓錯誤訊息無法閱讀。"""
    out = dp.redact_secrets("abc: connection refused", ["abc"])
    assert out == "abc: connection refused"


def test_redaction_handles_empty_input():
    assert dp.redact_secrets(None, ["secret"]) == ""
    assert dp.redact_secrets("hello", []) == "hello"
    assert dp.redact_secrets("hello", [None, ""]) == "hello"


def test_autoclock_redacts_every_meta_line_not_just_the_error_hint():
    """★[外審第 1 輪] 遮蔽要在【最後的寫入邊界】★

    舊版只遮 `error_hint`，但 `notes` 裡的 `screenshot_failed={e}` 同樣是院方
    丟回來的例外文字（unexpected alert 會把整段彈窗內容帶進來）。
    遠離「哪一行記得要遮」這種人為判斷。
    """
    import inspect

    import autoclock
    src = inspect.getsource(autoclock.save_debug_artifacts)
    assert "redact_secrets(" in src and "join(lines), redact)" in src, (
        "要把整份 meta（含 notes）一起遮，不是只遮 error_hint")
    assert "redact_secrets(error_hint" not in src, "不可只遮單一一行"
    caller = inspect.getsource(autoclock._handle_clock_failure)
    assert "redact=(username,)" in caller


# ─── ★外審第 1/2 輪★ 帳號根本不要寫進 log 檔 ───────────────────────────────
import logging as _lg                                            # noqa: E402


def _fmt(formatter, msg, *args):
    rec = _lg.LogRecord("t", _lg.INFO, __file__, 1, msg, args, None)
    return formatter.format(rec), rec


def test_the_file_formatter_replaces_known_accounts():
    f = dp.RedactingFormatter("%(message)s")
    f.add_secret("A123456789")
    out, _rec = _fmt(f, "%s 打卡成功", "A123456789")
    assert "A123456789" not in out
    assert "<redacted>" in out


def test_the_formatter_does_not_mutate_the_shared_record():
    """★[外審第 2 輪] 這是我上一版錯的地方★

    我用 `logging.Filter` 改 `record.msg`，並宣稱「只掛在檔案 handler 上」——
    但 `logging` 把【同一個 LogRecord 物件】傳給每一個 handler，filter 一改就
    連 UI 即時記錄窗與 console 也被遮，多帳號失敗時分不出是哪個帳號。
    """
    f = dp.RedactingFormatter("%(message)s")
    f.add_secret("A123456789")
    out, rec = _fmt(f, "%s 打卡成功", "A123456789")
    assert "<redacted>" in out, "檔案那一份要遮"
    assert rec.getMessage() == "A123456789 打卡成功", (
        "★共用的 record 不可被改—— UI 還要看真實帳號★")
    assert rec.args == ("A123456789",)


def test_a_real_handler_pipeline_only_redacts_the_file(tmp_path):
    """拿真的 handler 跑一遍（不是只問孤立的 record）。"""
    logfile = tmp_path / "x.log"
    logger = _lg.getLogger("cmuh_test_pipeline")
    logger.handlers.clear()
    logger.setLevel(_lg.INFO)
    logger.propagate = False

    fh = _lg.FileHandler(str(logfile), encoding="utf-8")
    redacting = dp.RedactingFormatter("%(message)s")
    redacting.add_secret("A123456789")
    fh.setFormatter(redacting)

    seen = []

    class _UI(_lg.Handler):
        def emit(self, record):
            seen.append(record.getMessage())

    ui = _UI()
    ui.setFormatter(_lg.Formatter("%(message)s"))
    logger.addHandler(fh)
    logger.addHandler(ui)
    try:
        logger.info("%s 打卡成功", "A123456789")
    finally:
        fh.close()
        logger.handlers.clear()

    assert "A123456789" not in logfile.read_text(encoding="utf-8")
    assert seen == ["A123456789 打卡成功"], "UI handler 必須仍看到真實帳號"


def test_short_values_are_not_added_as_secrets():
    f = dp.RedactingFormatter("%(message)s")
    f.add_secret("abc")
    out, _rec = _fmt(f, "abc: refused")
    assert out == "abc: refused"


# ─── ★外審第 2 輪★ 升級前的舊 log 仍含帳號 ────────────────────────────────
_LEGACY = "2026-07-01 08:00:00 - INFO - OLD_ACCOUNT_1 打卡失敗\n"
_CLEAN = "2026-07-30 09:00:00 - INFO - <redacted> 打卡成功\n"


def test_lines_before_the_boundary_are_dropped():
    from datetime import datetime as _dt
    kept = dp.log_lines_after(_LEGACY + _CLEAN, _dt(2026, 7, 30, 0, 0, 0))
    assert "OLD_ACCOUNT_1" not in kept
    assert "<redacted>" in kept


def test_continuation_lines_follow_the_previous_decision():
    """traceback 續行沒有時間戳 —— 不可因此把舊 log 的 traceback 漏出去。"""
    from datetime import datetime as _dt
    text = ("2026-07-01 08:00:00 - ERROR - boom\n"
            "  File x, line 1\n    OLD_ACCOUNT_1\n"
            "2026-07-30 09:00:00 - ERROR - boom2\n"
            "  File y, line 2\n    clean\n")
    kept = dp.log_lines_after(text, _dt(2026, 7, 30, 0, 0, 0))
    assert "OLD_ACCOUNT_1" not in kept
    assert "clean" in kept


def test_no_boundary_means_nothing_from_the_log_is_included():
    """★寧可少給，不可外洩★ 從沒啟用過遮蔽 → 整份 log 不收錄。"""
    assert dp.log_lines_after(_LEGACY + _CLEAN, None) == ""


def test_the_bundle_drops_a_legacy_log_and_says_so(tmp_path, monkeypatch):
    """★[外審第 2 輪] 升級後馬上產生診斷包★

    既有 log 是遮蔽啟用前寫的，裡面那個帳號可能早就刪掉了 ——
    寄出前才遮的做法根本不知道它存在。
    """
    from datetime import datetime as _dt
    monkeypatch.setattr(dp, "sanitized_logging_since",
                        lambda *_a, **_k: _dt(2026, 7, 30, 0, 0, 0))
    log = tmp_path / "old.log"
    log.write_text(_LEGACY, encoding="utf-8")
    dest = tmp_path / "d.zip"
    added, note = dp.build_safe_diag_bundle(dest, log_files=[str(log)])
    assert added == 0
    assert "整份 log 未收錄" in note, note
    import zipfile
    with zipfile.ZipFile(dest) as zf:
        assert zf.namelist() == []


def test_the_bundle_keeps_only_the_sanitized_tail(tmp_path, monkeypatch):
    from datetime import datetime as _dt
    monkeypatch.setattr(dp, "sanitized_logging_since",
                        lambda *_a, **_k: _dt(2026, 7, 30, 0, 0, 0))
    log = tmp_path / "mixed.log"
    log.write_text(_LEGACY + _CLEAN, encoding="utf-8")
    dest = tmp_path / "d.zip"
    added, note = dp.build_safe_diag_bundle(dest, log_files=[str(log)])
    assert added == 1
    import zipfile
    with zipfile.ZipFile(dest) as zf:
        body = zf.read("logs/mixed.log")
    assert b"OLD_ACCOUNT_1" not in body
    assert "遮蔽啟用之後" in note, note


def test_the_boundary_survives_a_settings_save(tmp_path):
    """分界線不可被「存一下開關」洗掉 —— 洗掉就等於回到「不知道哪段安全」。"""
    p = tmp_path / "privacy.json"
    dp.save_privacy_settings({dp.SANITIZED_SINCE_KEY: "2026-07-30T00:00:00"}, p)
    dp.save_privacy_settings({"store_page_source": True}, p)   # 只改開關
    assert dp.load_privacy_settings(p)[dp.SANITIZED_SINCE_KEY] == \
        "2026-07-30T00:00:00"


def test_autoclock_installs_the_redacting_formatter():
    import inspect

    import autoclock
    assert "install_log_secret_filter()" in inspect.getsource(
        autoclock._setup_clock_logging)
    assert "install_log_secret_filter(" in inspect.getsource(
        autoclock.load_config)


# ─── ★外審第 1 輪★ 只有字面 true 才算開啟 ───────────────────
def test_a_non_boolean_setting_is_treated_as_disabled(tmp_path):
    """`bool("false")` 是 True —— 舊版直接 bool()，於是一個看起來是關的值
    反而把整頁原始碼打開了，正好違反 fail-closed 的初衷。"""
    for bad in ('"false"', '"no"', '1', '"true"', '[]'):
        p = tmp_path / f"privacy_{abs(hash(bad))}.json"
        p.write_text('{"store_page_source": %s}' % bad, encoding="utf-8")
        assert dp.store_page_source_enabled(p) is False, bad


def test_literal_true_still_enables_it(tmp_path):
    p = tmp_path / "privacy.json"
    p.write_text('{"store_page_source": true}', encoding="utf-8")
    assert dp.store_page_source_enabled(p) is True


# ─── 一鍵刪除 ──────────────────────────────────────────────────────────────
def test_purge_removes_every_file_but_keeps_the_directory(tmp_path):
    # 用子目錄：conftest 的隔離夾具會在 tmp_path 下建 `_cmuh_app`
    dumps = tmp_path / "dumps"
    dumps.mkdir()
    for name in ("a.png", "b.html", "c.txt"):
        (dumps / name).write_text("x", encoding="utf-8")
    (dumps / "sub").mkdir()          # 子目錄不動（只刪檔）
    gone, failed = dp.purge_dir(dumps)
    assert (gone, failed) == (3, 0)
    assert dumps.is_dir(), "資料夾本身要留著（下次不必再處理權限）"
    assert [x.name for x in dumps.iterdir()] == ["sub"]


def test_purge_on_a_missing_directory_is_harmless(tmp_path):
    assert dp.purge_dir(tmp_path / "nope") == (0, 0)


# ─── 安全診斷包 ────────────────────────────────────────────────────────────
def test_the_diag_bundle_excludes_screenshots_and_page_source(tmp_path,
                                                             monkeypatch):
    """★這是「預設不存 HTML」的配套★

    沒有安全的替代品，使用者遲早會被迫去打開那個開關、或把含帳號的截圖整包寄出，
    預設值的保護就被繞過去了。診斷包只放 log 與錯誤摘要。
    """
    import zipfile
    from datetime import datetime as _dt

    monkeypatch.setattr(dp, "sanitized_logging_since",
                        lambda *_a, **_k: _dt(2026, 7, 30, 0, 0, 0))
    meta = tmp_path / "dumps"
    meta.mkdir()
    (meta / "fail_ab12cd34_20260730.txt").write_text(
        "time=x\nTimeoutException: account=A123456789", encoding="utf-8")
    (meta / "fail_ab12cd34_20260730.png").write_bytes(b"\x89PNG fake")
    (meta / "fail_ab12cd34_20260730.html").write_text(
        "<input value='A123456789'>", encoding="utf-8")
    log = tmp_path / "autoclock.log"
    log.write_text("2026-07-30 09:00:00 - INFO - 登入 A123456789 失敗\n",
                   encoding="utf-8")

    dest = tmp_path / "diag.zip"
    added, note = dp.build_safe_diag_bundle(
        dest, log_files=[str(log)], meta_dir=str(meta),
        secrets=["A123456789"])

    assert added == 2, note
    with zipfile.ZipFile(dest) as zf:
        names = zf.namelist()
        blob = b"".join(zf.read(n) for n in names)
    assert not any(n.endswith(".png") for n in names), "★截圖不可進診斷包★"
    assert not any(n.endswith(".html") for n in names), "★整頁原始碼不可進診斷包★"
    assert b"A123456789" not in blob, "★帳號必須被遮蔽★"
    assert b"<redacted>" in blob
    assert "png" in note and "html" in note, f"要說明少了什麼：{note}"


def test_the_diag_bundle_truncates_huge_logs(tmp_path, monkeypatch):
    from datetime import datetime as _dt
    monkeypatch.setattr(dp, "sanitized_logging_since",
                        lambda *_a, **_k: _dt(2026, 7, 30, 0, 0, 0))
    log = tmp_path / "big.log"
    # 每行都要帶分界線之後的時間戳，否則整段會被剔掉
    line = "2026-07-30 09:00:00 - INFO - " + "x" * 60 + "\n"
    log.write_text(line * 200, encoding="utf-8")
    dest = tmp_path / "diag.zip"
    added, _note = dp.build_safe_diag_bundle(
        dest, log_files=[str(log)], max_log_bytes=1000)
    assert added == 1
    import zipfile
    with zipfile.ZipFile(dest) as zf:
        body = zf.read("logs/big.log")
    assert 0 < len(body) <= 1000, "只取尾端，不可把整份包進去"


def test_the_diag_bundle_never_raises_on_a_bad_destination(tmp_path):
    added, note = dp.build_safe_diag_bundle(tmp_path / "no" / "such" / "d.zip")
    assert added == 0 and note


# ─── ACL ───────────────────────────────────────────────────────────────────
def test_acl_hardening_never_raises_on_a_missing_directory(tmp_path):
    assert dp.restrict_dir_to_current_user(tmp_path / "nope") is False


def test_acl_failure_does_not_stop_anything(tmp_path, monkeypatch):
    """★權限收不緊不該讓打卡整個停擺★（這是縱深防禦的一層，不是唯一一層）。"""
    monkeypatch.setattr(dp.subprocess, "run",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("no")))
    assert dp.restrict_dir_to_current_user(tmp_path) is False


def test_autoclock_hardens_the_dump_directory():
    import inspect

    import autoclock
    src = inspect.getsource(autoclock)
    assert "restrict_dir_to_current_user(DEBUG_DUMPS_DIR)" in src
