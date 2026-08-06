# -*- coding: utf-8 -*-
"""寄送「結果不明」的一致處理（2026-08-06 外審 P1-03 / P1-04）。

【P1-03 問題】Outlook 逾時會拋 `DeliveryOutcomeUnknown`（外層視為 fatal、不重試），
但 SMTP 逾時只拋普通 `RuntimeError` —— 而 smtp_mail 自己的註解就寫著「timeout 可能
發生在伺服器【已經收下 DATA】之後，信可能已送達」。語意一樣、例外類別不一樣，
於是外層 `_do_full_job` 把 SMTP 逾時當【可重試】→ 同一封 MIME 再提交一次 →
收件人可能收到兩封。固定 Message-ID 只可能幫某些郵件系統收斂顯示，不是
exactly-once 保證。

【P1-04 問題】就算標成 fatal，收尾仍走「一般失敗」那條：刪截圖、釋放 email 觸發
去重、並回信告訴觸發者「已解除限制，可立即重寄」。但 UNKNOWN 的定義正是「原信
可能稍後仍會送達」→ 使用者照做 → 兩封。

【修法】
  * `DeliveryOutcomeUnknown` 移到 `cmuh_common.smtp_mail`，兩條寄信路徑共用同一類別。
  * SMTP 逾時改拋它。
  * `_do_full_job` 給 UNKNOWN 專屬收尾：保留截圖、不釋放去重、寄「請先不要重發」。
"""
import inspect
import os
import socket
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import consult_query as cq  # noqa: E402
import cmuh_common.smtp_mail as sm  # noqa: E402


# ── P1-03:兩條寄信路徑共用同一個「結果不明」類別 ────────────────────────────
def test_both_paths_share_one_unknown_class():
    """★核心★ consult_query 與 smtp_mail 必須是【同一個】類別。

    若各自定義一份，`isinstance(e, DeliveryOutcomeUnknown)` 在 _do_full_job 裡
    就抓不到 SMTP 那一邊 → SMTP 逾時又變回「可重試」。
    """
    assert cq.DeliveryOutcomeUnknown is sm.DeliveryOutcomeUnknown, (
        "★兩邊各有一份 DeliveryOutcomeUnknown★ isinstance 檢查涵蓋不到 SMTP")
    assert issubclass(sm.DeliveryOutcomeUnknown, RuntimeError)


def test_smtp_timeout_raises_delivery_unknown(monkeypatch):
    """★行為★ SMTP 逾時 → 拋 DeliveryOutcomeUnknown（不是普通 RuntimeError）。

    這正是重複寄送的入口：普通 RuntimeError 會被 `_do_full_job` 當可重試。
    """
    monkeypatch.setattr(sm, "_reserve_rate_limit_slot", lambda *a, **k: object())
    monkeypatch.setattr(sm, "_rollback_rate_limit_slot", lambda *a, **k: None)

    def _boom(cred, msg, timeout):
        raise socket.timeout("timed out waiting for 250")

    monkeypatch.setattr(sm, "_send_once", _boom)

    with pytest.raises(sm.DeliveryOutcomeUnknown) as ei:
        sm.send_mail(recipients=["a@b.c"], subject="s", body="b",
                     attachment_path=None, max_retries=0,
                     override_credentials={
                         "host": "smtp.example.com", "port": 465,
                         "use_ssl": True, "username": "u", "password": "p",
                         "from_address": "u@example.com", "from_name": "T"})
    assert "結果不明" in str(ei.value)
    # 且不可以是「純」RuntimeError —— 必須是可辨識的專屬子類別
    assert type(ei.value) is sm.DeliveryOutcomeUnknown


def test_the_job_treats_unknown_as_fatal():
    """接線:UNKNOWN 必須在 fatal 集合內（重試 = 可能寄第二封）。"""
    src = inspect.getsource(cq._do_full_job)
    i = src.index("fatal = isinstance(")
    seg = src[i:i + 400]
    assert "DeliveryOutcomeUnknown" in seg, "UNKNOWN 不在 fatal 集合 → 會被重試"


# ── P1-04:UNKNOWN 專屬收尾 ─────────────────────────────────────────────────
def _terminal_segment() -> str:
    """終局收尾那一段原始碼（從 UNKNOWN 分支開始到一般失敗的去重釋放）。"""
    src = inspect.getsource(cq._do_full_job)
    i = src.index("isinstance(last_err, DeliveryOutcomeUnknown)")
    return src[i:i + 1600]


def test_unknown_branch_exists_and_bails_before_generic_cleanup():
    """★核心★ UNKNOWN 要在一般收尾【之前】分流，並且直接結束那一輪。"""
    src = inspect.getsource(cq._do_full_job)
    i_unknown = src.index("isinstance(last_err, DeliveryOutcomeUnknown)")
    i_discard = src.index("_discard_undelivered_shot(delivery)")
    i_release = src.index("_release_trigger_dedup(override_recipients)")
    assert i_unknown < i_discard < i_release, "UNKNOWN 分流必須排在一般收尾之前"
    seg = src[i_unknown:i_discard]
    assert "break" in seg, "UNKNOWN 分支要 break，不可掉進一般失敗收尾"


def test_unknown_does_not_delete_the_screenshot():
    seg = _terminal_segment()
    i_break = seg.index("break")
    assert "_discard_undelivered_shot" not in seg[:i_break], (
        "★結果不明卻刪了截圖★ 信可能已送達，這不是『沒寄出』")


def test_unknown_does_not_release_the_trigger_dedup():
    """★最關鍵★ 釋放去重 = 允許使用者立刻重發 = 製造第二封。"""
    seg = _terminal_segment()
    i_break = seg.index("break")
    assert "_release_trigger_dedup" not in seg[:i_break], (
        "★結果不明卻釋放了觸發去重★ 使用者重發 + 原信送達 = 兩封")


def test_unknown_sends_a_do_not_resend_notice_not_the_retry_one():
    seg = _terminal_segment()
    i_break = seg.index("break")
    head = seg[:i_break]
    assert "_send_delivery_unknown_notice_async" in head, \
        "要回信告知觸發者『結果尚未確認』"
    assert "_send_failure_notice_async" not in head, (
        "★寄了『可立即重試』通知★ 那正是重複寄送的來源")


def _code_without_docstring(fn) -> str:
    """只取函式的【程式碼】，剝掉 docstring —— 否則說明「不可以寫成 X」的
    註解本身會讓「X 不得出現」的斷言誤判。"""
    src = inspect.getsource(fn)
    doc = inspect.getdoc(fn)
    if not doc:
        return src
    for line in doc.splitlines():
        src = src.replace(line, "")
    return src


def test_unknown_notice_text_tells_user_to_wait_not_retry():
    """措辭必須與一般失敗【相反】：先檢查信箱、不要馬上重發。"""
    body = _code_without_docstring(cq._send_delivery_unknown_notice_async)
    assert "尚未確認" in body
    assert "不會自動重寄" in body
    assert "已解除重查限制" not in body, "不可沿用一般失敗那句『可立即重寄』"


def test_generic_failure_notice_still_says_retry_is_ok():
    """反方向：真正的失敗（不是 UNKNOWN）仍要告訴使用者可以重試。"""
    body = inspect.getsource(cq._send_failure_notice_async)
    assert "可立即重寄" in body or "已解除重查限制" in body


# ── P1-08:重截後仍是黑圖 → 不可當成正常成功（不更新已通知基準）──────────────
# 【問題】`_capture_nonblank` 重截兩次後若仍整張單色，舊版只寫一行 warning 就原樣
# 回傳，回傳值不帶任何品質資訊。若清單剛好 stable，那一輪就會【更新已通知基準】
# ——等於用一張零資訊的黑圖宣稱「這些病人都通知過了」，下一輪不會再補寄他們。
# 【修法】沿用既有的 `as_unverified()` 通道：信照寄（有圖勝過沒信），但不更新基準。

class _BlankImg:
    def getextrema(self):
        return ((0, 0), (0, 0), (0, 0))          # 三色版都只有單一值 = 全黑


class _RealImg:
    def getextrema(self):
        return ((0, 255), (0, 200), (10, 240))


def _snap(rows=("陳X 12F 3801 0012345",)):
    return cq._RosterSnapshot(list(rows), True, [], [])


def test_blank_capture_is_marked_not_baseline_eligible():
    """★核心★ 黑圖 → 快照必須降級為「不可更新基準」。"""
    img, snap = cq._capture_with_settled_roster(
        123,
        capture=lambda h: _BlankImg(),
        settle=lambda h: _snap(),
        read=lambda h: (None, None, list(_snap().texts)),
        sleep=lambda *_a: None)
    assert cq._image_is_blank(img), "測試前提:這是一張黑圖"
    assert cq._may_update_baseline(snap.texts) is False, (
        "★黑圖卻更新了已通知基準★ 那些病人會被當成已通知，下一輪不再補寄")


def test_blank_capture_still_returns_an_image_to_send():
    """fail-open 不變:仍要有圖可寄（有圖勝過整封信不寄）。"""
    img, _ = cq._capture_with_settled_roster(
        123,
        capture=lambda h: _BlankImg(),
        settle=lambda h: _snap(),
        read=lambda h: (None, None, list(_snap().texts)),
        sleep=lambda *_a: None)
    assert img is not None


def test_good_capture_still_updates_the_baseline():
    """反方向:正常截圖 + 清單穩定且回讀一致 → 照舊可以更新基準。"""
    rows = ["陳X 12F 3801 0012345"]
    _img, snap = cq._capture_with_settled_roster(
        123,
        capture=lambda h: _RealImg(),
        settle=lambda h: cq._RosterSnapshot(list(rows), True, [], []),
        read=lambda h: (None, None, list(rows)),
        sleep=lambda *_a: None)
    assert cq._may_update_baseline(snap.texts) is True, (
        "正常截圖不該被降級(否則永遠不更新基準 → 每輪重複寄同一批病人)")
