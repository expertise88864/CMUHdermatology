# -*- coding: utf-8 -*-
"""[批次SK] 登入對話框的【截圖】要附在連續失敗告警信裡。

★為什麼文字不夠★ Delphi 訊息框的內文是 TLabel（TGraphicControl，沒有
HWND），`_window_texts` 拿不到 —— 實機 A01-11106-001 的告警只看得到
「[TMessageForm] 住院醫囑系統 / OK」，HIS 到底說了什麼仍要用猜的。
截「那個對話框視窗本身」的圖（PrintWindow，不是全螢幕）就能看到內文。

★隱私邊界★ 與 record_text 完全相同（只在登入階段、主畫面交出來之前，
畫面上不可能有病人資料），另加尺寸上限當第二道防線：對話框不會有整個
螢幕那麼大，太大代表抓錯視窗，寧可不存。

★告警那一側★ 附件與內文那一句由【同一個】判斷決定（說有附就真的有附）；
太舊或 mtime 在未來的截圖一律不附 —— 舊事故的圖會把人帶去查一個已經
不存在的原因。
"""
import ast
import importlib
import io
import os
import re
import sys
import threading
import time

import pytest

NL = chr(10)
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "src"))

cq = importlib.import_module("consult_query")

SRC = io.open(os.path.join(REPO_ROOT, "src", "consult_query.py"),
              encoding="utf-8").read()


def _fn_src(name):
    for n in ast.walk(ast.parse(SRC)):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return ast.get_source_segment(SRC, n) or ""
    raise AssertionError(f"找不到 {name}")


def _strip_comments(text):
    """★負向斷言先剝註解★（說明「為什麼不可以」的那句話裡就有那個字面）。"""
    return NL.join(re.sub(r"#.*$", "", ln) for ln in text.splitlines())


def _img(w=420, h=160):
    from PIL import Image
    return Image.new("RGB", (w, h), (200, 200, 200))


@pytest.fixture(autouse=True)
def _iso(tmp_path, monkeypatch):
    import cmuh_common.paths as paths
    monkeypatch.setattr(paths, "get_settings_dir", lambda: str(tmp_path))
    cq._login_dialog_texts.clear()
    cq._login_dialog_shot_done[0] = False
    yield
    cq._login_dialog_texts.clear()
    cq._login_dialog_shot_done[0] = False


class TestTheDialogItselfGetsCaptured:
    def test_a_dialog_capture_lands_as_a_png(self, monkeypatch):
        monkeypatch.setattr(cq, "_capture_window_image_impl",
                            lambda hwnd: _img())
        cq._capture_login_dialog_shot(0x1234, "TMessageForm")
        path = cq._login_dialog_shot_path()
        assert os.path.isfile(path)
        with open(path, "rb") as f:
            assert f.read(8) == b"\x89PNG\r\n\x1a\n", "存的要是 PNG"

    def test_only_the_first_dialog_of_a_round_is_kept(self, monkeypatch):
        """第一個對話框通常就是拒絕原因;之後的多半是連鎖噪音。"""
        seq = [_img(400, 150), _img(800, 300)]
        monkeypatch.setattr(cq, "_capture_window_image_impl",
                            lambda hwnd: seq.pop(0))
        cq._capture_login_dialog_shot(1, "TMessageForm")
        first = open(cq._login_dialog_shot_path(), "rb").read()
        cq._capture_login_dialog_shot(2, "TFormOther")
        assert open(cq._login_dialog_shot_path(), "rb").read() == first, (
            "★同一輪的第二個對話框不可以蓋掉第一個★")

    def test_a_new_login_round_captures_again(self, monkeypatch):
        seq = [_img(400, 150), _img(500, 180)]
        monkeypatch.setattr(cq, "_capture_window_image_impl",
                            lambda hwnd: seq.pop(0))
        cq._capture_login_dialog_shot(1, "TMessageForm")
        first = open(cq._login_dialog_shot_path(), "rb").read()
        cq._reset_login_dialog_texts()          # 新的一輪
        cq._capture_login_dialog_shot(2, "TMessageForm")
        assert open(cq._login_dialog_shot_path(), "rb").read() != first, (
            "新的一輪要重截 —— 不然 mtime 永遠停在第一次,新鮮度檢查會把它丟掉")

    def test_an_oversized_capture_is_rejected(self, monkeypatch):
        """對話框不會有整個螢幕那麼大 —— 太大代表抓錯視窗,寧可不存。

        這是隱私的第二道防線:第一道是「只有登入階段會走到這裡」的結構保證。
        """
        monkeypatch.setattr(cq, "_capture_window_image_impl",
                            lambda hwnd: _img(1920, 1080))
        cq._capture_login_dialog_shot(1, "TMessageForm")
        assert not os.path.exists(cq._login_dialog_shot_path())
        assert not cq._login_dialog_shot_done[0], (
            "沒存成的不可以佔掉本輪名額 —— 下一個正常大小的對話框還要能截")

    def test_a_capture_timeout_is_fail_open(self, monkeypatch):
        monkeypatch.setattr(cq, "call_with_timeout",
                            lambda *a, **k: None)   # 逾時 → default=None
        cq._capture_login_dialog_shot(1, "TMessageForm")   # 不可拋
        assert not os.path.exists(cq._login_dialog_shot_path())

    def test_a_crash_inside_capture_never_breaks_login(self, monkeypatch):
        def _boom(hwnd):
            raise RuntimeError("GDI 掛了")
        monkeypatch.setattr(cq, "_capture_window_image_impl", _boom)
        cq._capture_login_dialog_shot(1, "TMessageForm")   # 不可拋

    def test_a_failed_save_leaves_no_torn_file(self, monkeypatch):
        """存檔要先寫暫存再原子換名:告警是背景執行緒在讀這個檔。"""
        class _Torn:
            size = (400, 150)

            def save(self, path, fmt):
                with open(path, "wb") as f:
                    f.write(b"\x89PN")      # 寫到一半
                raise OSError("disk full")

        monkeypatch.setattr(cq, "_capture_window_image_impl",
                            lambda hwnd: _Torn())
        cq._capture_login_dialog_shot(1, "TMessageForm")   # 不可拋
        assert not os.path.exists(cq._login_dialog_shot_path()), (
            "★半張圖不可以出現在正式路徑★")


class TestCaptureIsWiredIntoDialogDetection:
    def test_note_login_dialog_captures_before_any_early_return(self):
        body = _strip_comments(_fn_src("_note_login_dialog"))
        i = body.find("_capture_login_dialog_shot(")
        assert i >= 0, "★沒接上就等於沒做★"
        assert i < body.find("return"), (
            "截圖要排在所有 early-return 之前 —— 文字空白/重複的那幾種,"
            "正是最需要截圖的")

    def test_empty_text_still_captures(self, monkeypatch):
        """TLabel 沒有 HWND → `_window_texts` 可能整個是空的 —— 這正是
        最需要截圖的情況(2026-08-10 實機就是這一種的近親)。"""
        monkeypatch.setattr(cq, "_capture_window_image_impl",
                            lambda hwnd: _img())
        monkeypatch.setattr(cq, "_window_texts", lambda hwnd: [])
        cq._note_login_dialog(0x1234, "TMessageForm", [])
        assert os.path.isfile(cq._login_dialog_shot_path())


class TestOnlyFreshEvidenceIsAttached:
    def _make(self, age_sec):
        path = cq._login_dialog_shot_path()
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
        ts = time.time() - age_sec
        os.utime(path, (ts, ts))
        return path

    def test_fresh_evidence_is_attached(self):
        path = self._make(60.0)
        assert cq._login_dialog_shot_for_alert() == path

    def test_stale_evidence_is_not(self):
        self._make(cq._LOGIN_DIALOG_SHOT_MAX_AGE_SEC + 60.0)
        assert cq._login_dialog_shot_for_alert() is None, (
            "★舊事故的截圖比不附更糟★ 它會把人帶去查一個已不存在的原因")

    def test_a_future_mtime_is_not(self):
        self._make(-3600.0)             # 校時之後 mtime 在未來
        assert cq._login_dialog_shot_for_alert() is None

    def test_no_file_means_none(self):
        assert cq._login_dialog_shot_for_alert() is None


class TestTheAlertMailCarriesTheShot:
    @staticmethod
    def _fire(monkeypatch):
        """讓 `_note_job_failure` 真的走到寄信,並接住 send_mail 的參數。"""
        import cmuh_common.smtp_mail as sm
        sent, done = {}, threading.Event()

        def _fake(**kw):
            sent.update(kw)
            done.set()

        monkeypatch.setattr(sm, "send_mail", _fake)
        monkeypatch.setattr(cq, "_JOB_FAIL_ALERT_THRESHOLD", 1)
        monkeypatch.setattr(cq, "_JOB_FAIL_ALERT_COOLDOWN_SEC", 0)
        monkeypatch.setattr(cq, "_save_job_fail_state", lambda: None)
        monkeypatch.setattr(cq, "_forget_future_alert_ts", lambda now: False)
        monkeypatch.setattr(cq, "_job_fail_streak", 0)
        monkeypatch.setattr(cq, "_job_fail_last_alert", 0.0)
        cq._note_job_failure(["dev@x.tw"], "登入沒有完成")
        assert done.wait(5.0), "告警信沒有寄出去"
        return sent

    def test_fresh_shot_is_attached_and_the_body_says_so(self, monkeypatch):
        path = cq._login_dialog_shot_path()
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
        sent = self._fire(monkeypatch)
        # ★附的是快照,不是正式檔★(外審 SK 第 1 輪 P2):正式檔隨時會被
        #   恢復清理刪掉/被下一輪重截換掉。send_mail 要 Path → 比字串值。
        assert str(sent["attachment_path"]) == (
            cq._login_dialog_shot_sending_path())
        assert "已附上登入途中攔到的對話框截圖" in sent["body"]

    def test_no_shot_means_no_attachment_and_no_claim(self, monkeypatch):
        sent = self._fire(monkeypatch)
        assert sent["attachment_path"] is None
        assert "已附上" not in sent["body"], (
            "★說有附就要真的有附★ 宣稱與實作要由同一個判斷決定")

    def test_cleanup_racing_the_send_cannot_break_the_attachment(
            self, monkeypatch):
        """★外審 SK 第 1 輪 P2★ 恢復清理與寄信是兩條併發路徑。

        worker 決定「要附」到 SMTP 真正開檔之間,正式檔被 `_note_job_success`
        刪掉的話:附件檢查先看到「不存在」→ 信寄出去卻沒附(內文還說有);
        或 exists 過了、open 才炸 → ★整封告警弄丟★,而告警 6 小時才一封。
        """
        canonical = cq._login_dialog_shot_path()
        with open(canonical, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n" + b"EVIDENCE")
        import cmuh_common.smtp_mail as sm
        sent, done = {}, threading.Event()

        def _fake(**kw):
            os.remove(canonical)        # 模擬:寄信當下恢復清理刪掉正式檔
            p = kw["attachment_path"]
            assert p is not None and os.path.isfile(str(p)), (
                "★附件必須是不受清理影響的快照★")
            with open(str(p), "rb") as fh:
                kw["_attached_bytes"] = fh.read()
            sent.update(kw)
            done.set()

        monkeypatch.setattr(sm, "send_mail", _fake)
        monkeypatch.setattr(cq, "_JOB_FAIL_ALERT_THRESHOLD", 1)
        monkeypatch.setattr(cq, "_JOB_FAIL_ALERT_COOLDOWN_SEC", 0)
        monkeypatch.setattr(cq, "_save_job_fail_state", lambda: None)
        monkeypatch.setattr(cq, "_forget_future_alert_ts", lambda now: False)
        monkeypatch.setattr(cq, "_job_fail_streak", 0)
        monkeypatch.setattr(cq, "_job_fail_last_alert", 0.0)
        cq._note_job_failure(["dev@x.tw"], "登入沒有完成")
        assert done.wait(5.0)
        assert sent["_attached_bytes"].endswith(b"EVIDENCE"), (
            "快照內容要跟決定要附的那一刻一致")

    def test_a_failed_snapshot_still_sends_the_alert_without_claiming(
            self, monkeypatch):
        """快照失敗 → 不附、也不宣稱 —— 附件問題不可以弄丟整封告警。"""
        path = cq._login_dialog_shot_path()
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
        monkeypatch.setattr(
            cq.shutil, "copyfile",
            lambda *a, **k: (_ for _ in ()).throw(OSError("locked")))
        sent = self._fire(monkeypatch)
        assert sent["attachment_path"] is None
        assert "已附上" not in sent["body"]

    def test_the_snapshot_is_removed_after_the_send(self, monkeypatch):
        path = cq._login_dialog_shot_path()
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
        sent = self._fire(monkeypatch)
        assert sent["attachment_path"] is not None, (
            "前提:這一封真的有附快照(不然這條測試量不到清理)")
        # worker 的 finally 在 send_mail 回來後才跑 —— 等它把快照清掉
        deadline = time.time() + 5.0
        while (os.path.exists(cq._login_dialog_shot_sending_path())
               and time.time() < deadline):
            time.sleep(0.05)
        assert not os.path.exists(cq._login_dialog_shot_sending_path()), (
            "快照是一次性的,寄完要清掉")


class TestRecoveryClearsTheEvidence:
    def test_success_after_failures_removes_the_file(self, monkeypatch):
        path = cq._login_dialog_shot_path()
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
        monkeypatch.setattr(cq, "_job_fail_streak", 3)
        monkeypatch.setattr(cq, "_save_job_fail_state", lambda: None)
        monkeypatch.setattr(cq, "_set_login_cooldown_until",
                            lambda *a, **k: None)
        monkeypatch.setattr(cq, "_clear_reboot_reason", lambda r: None)
        cq._note_job_success()
        assert not os.path.exists(path), (
            "恢復之後那張圖代表的原因已經不存在了")

    def test_success_with_no_streak_changes_nothing(self, monkeypatch):
        """沒有故障波的成功【不會】動到檔案(early return 在刪除之前)。

        這不是效能問題:正常運作時每 3 分鐘成功一次,若每次都嘗試刪除,
        「新鮮度」的意義就變成「最近 3 分鐘」—— 什麼都附不上。
        """
        path = cq._login_dialog_shot_path()
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
        monkeypatch.setattr(cq, "_job_fail_streak", 0)
        monkeypatch.setattr(cq, "_set_login_cooldown_until",
                            lambda *a, **k: None)
        monkeypatch.setattr(cq, "_clear_reboot_reason", lambda r: None)
        cq._note_job_success()
        assert os.path.exists(path)


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
