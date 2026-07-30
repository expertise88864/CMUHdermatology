# -*- coding: utf-8 -*-
"""[2026-07-30 第二輪外審 P2-02] 寄信 rate limit 每個 process 各算各的。

原本的 rate limit 是 `smtp_mail` 裡的一個 `deque` + `threading.Lock`。那把鎖只
鎖得住同一個 process 裡的 thread，但這個 repo 同時跑五支獨立程式（main /
autoclock / consult_query / watchdog / scheduler），共用同一個 Gmail 帳號 ——
每支都以為自己有 30 封／小時，真正的上限其實是 30×5。

這個保護存在的唯一理由是「程式出 bug 迴圈時不要把 Gmail 帳號寄爆」，而 bug 迴圈
最可能【同時】發生在多支程式（共用的 HIS 契約偵測、共用的網路斷線）—— 正是最需要
它的時候它最沒用。

★這一檔的核心是那兩支「真的開子行程」的測試★
單行程的 mock 測不到「跨行程」這件事；一個只在同一個 process 裡數封數的測試，在
修好之前也會全綠。
"""
import os
import sqlite3
import subprocess
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common import mail_quota, smtp_mail  # noqa: E402

CLINICAL = mail_quota.CATEGORY_CLINICAL
SYSTEM = mail_quota.CATEGORY_SYSTEM
ACC = "sender@example.com"


@pytest.fixture(autouse=True)
def _fresh_quota():
    """每支測試從乾淨的行程內狀態開始（DB 本身已由 conftest 的 tmp settings 隔離）。"""
    mail_quota._recent.clear()
    mail_quota._degraded_reason = None
    mail_quota._degraded_logged.clear()
    yield
    mail_quota._recent.clear()


def _rows() -> list:
    con = sqlite3.connect(mail_quota.db_path())
    try:
        return con.execute(
            "SELECT account, category FROM mail_sends ORDER BY id").fetchall()
    finally:
        con.close()


def _reserve_n(n: int, category: str = CLINICAL, account: str = ACC) -> int:
    """盡量佔 n 個名額，回傳實際成功幾個。"""
    got = 0
    for _ in range(n):
        try:
            mail_quota.reserve(account=account, category=category)
        except mail_quota.MailQuotaExceeded:
            break
        got += 1
    return got


# ─── ★核心★ 真的開子行程 ──────────────────────────────────────────────────
_CHILD = r'''
import sys
sys.path.insert(0, sys.argv[1])
from cmuh_common import paths
paths.get_app_dir = lambda: sys.argv[2]          # 指到父行程的 tmp app 目錄
from cmuh_common import mail_quota

want, category = int(sys.argv[3]), sys.argv[4]
account = sys.argv[5]
got = 0
for _ in range(want):
    try:
        mail_quota.reserve(account=account, category=category)
    except mail_quota.MailQuotaExceeded:
        break
    got += 1
print(got)
'''


def _child(app_dir, want, category=CLINICAL, account=ACC, script=None,
           wait=True):
    proc = subprocess.Popen(
        [sys.executable, str(script), os.path.abspath(
            os.path.join(os.path.dirname(__file__), "..", "src")),
         str(app_dir), str(want), category, account],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if not wait:
        return proc
    out, err = proc.communicate(timeout=120)
    assert proc.returncode == 0, f"子行程失敗：{err[:500]}"
    return int(out.strip())


@pytest.fixture
def child_script(tmp_path):
    p = tmp_path / "quota_child.py"
    p.write_text(_CHILD, encoding="utf-8")
    return p


def _app_dir() -> str:
    from cmuh_common.paths import get_app_dir
    return get_app_dir()


def test_a_second_real_process_shares_the_same_budget(child_script):
    """★這一支就是 P2-02★

    子行程先寄滿 25 封，本行程（記憶體裡一封都沒有）最多只能再寄 5 封。
    修好之前本行程會拿到完整的 30 封 —— 帳號實際被寄 55 封。
    """
    used = _child(_app_dir(), 25, script=child_script)
    assert used == 25, "測試前提不成立：子行程沒有寄滿 25 封"
    assert not mail_quota._recent, "本行程記憶體裡本來就沒有紀錄"

    mine = _reserve_n(mail_quota.TOTAL_MAX, CLINICAL)
    assert mine == mail_quota.TOTAL_MAX - 25, (
        f"★另一支【真的行程】已用掉 25 個名額，本行程只該拿到 5 個，"
        f"實際拿到 {mine} 個★")


def test_three_real_processes_racing_cannot_over_allocate(child_script):
    """並行搶額度不可超發 —— BEGIN IMMEDIATE 真的序列化了「數封數→寫入」。

    三支子行程同時各要 20 個名額（總需求 60 > 上限 30）。若「清舊紀錄 → 數目前
    幾封 → 寫入」不是原子的，兩支會同時讀到同一個封數而各自寫入 → 超發。
    """
    app = _app_dir()
    procs = [_child(app, 20, script=child_script, wait=False) for _ in range(3)]
    granted = 0
    for p in procs:
        out, err = p.communicate(timeout=180)
        assert p.returncode == 0, f"子行程失敗：{err[:500]}"
        granted += int(out.strip())
    assert granted == mail_quota.TOTAL_MAX, (
        f"三支行程共拿到 {granted} 個名額，上限是 {mail_quota.TOTAL_MAX}")
    assert len(_rows()) == mail_quota.TOTAL_MAX


# ─── 類別配額:系統類不可餓死臨床告警 ──────────────────────────────────────
def test_system_mail_cannot_starve_clinical_alerts():
    assert _reserve_n(mail_quota.TOTAL_MAX, SYSTEM) == mail_quota.SYSTEM_MAX, (
        "系統／除錯類必須在 SYSTEM_MAX 就被擋下")
    reserved = mail_quota.TOTAL_MAX - mail_quota.SYSTEM_MAX
    assert _reserve_n(mail_quota.TOTAL_MAX, CLINICAL) == reserved, (
        f"★系統類寄爆之後臨床告警仍必須有 {reserved} 個保留名額★")


def test_clinical_mail_may_use_the_whole_account_budget():
    """反向【刻意】不設限：臨床信的數量受真實事件約束，不會因 bug 暴增。"""
    assert _reserve_n(mail_quota.TOTAL_MAX + 5, CLINICAL) == mail_quota.TOTAL_MAX


def test_clinical_usage_still_counts_towards_the_system_total():
    """臨床先用滿，系統類就沒有名額 —— 保護的是【帳號】的總額度。"""
    assert _reserve_n(mail_quota.TOTAL_MAX, CLINICAL) == mail_quota.TOTAL_MAX
    with pytest.raises(mail_quota.MailQuotaExceeded):
        mail_quota.reserve(account=ACC, category=SYSTEM)


def test_different_accounts_have_independent_budgets():
    assert _reserve_n(mail_quota.TOTAL_MAX, CLINICAL, "a@x.com") == \
        mail_quota.TOTAL_MAX
    assert _reserve_n(3, CLINICAL, "b@x.com") == 3


def test_the_account_key_is_case_insensitive():
    _reserve_n(mail_quota.TOTAL_MAX, CLINICAL, "Sender@Example.COM")
    with pytest.raises(mail_quota.MailQuotaExceeded):
        mail_quota.reserve(account="sender@example.com", category=CLINICAL)


# ─── 過期與釋放 ────────────────────────────────────────────────────────────
def test_reservations_expire_after_the_window():
    old = time.time() - mail_quota.WINDOW_SEC - 60
    for _ in range(mail_quota.TOTAL_MAX):
        mail_quota.reserve(account=ACC, category=CLINICAL, now=old)
    assert _reserve_n(1, CLINICAL) == 1, "一小時前的紀錄不該再佔用額度"


def test_a_clock_jump_backwards_does_not_permanently_consume_quota():
    """★時鐘倒退★ 未來的紀錄永遠不會「從左邊過期」，必須主動清掉。"""
    future = time.time() + 5 * mail_quota.WINDOW_SEC
    for _ in range(mail_quota.TOTAL_MAX):
        mail_quota.reserve(account=ACC, category=CLINICAL, now=future)
    assert _reserve_n(1, CLINICAL) == 1, (
        "時鐘跳到未來再回來，額度不可被永久佔住")


def test_release_gives_the_slot_back_in_both_layers():
    r = mail_quota.reserve(account=ACC, category=CLINICAL)
    assert r.cross_process is True and r.row_id is not None
    assert len(_rows()) == 1
    mail_quota.release(r)
    assert _rows() == [], "跨行程紀錄要被刪掉"
    assert not mail_quota._recent, "行程內紀錄也要被刪掉"


def test_releasing_twice_cannot_delete_someone_elses_slot():
    """★rowid 重用★ 若 DELETE 之後 id 會被回收，一個慢半拍的 release 會刪掉
    別人剛拿到的名額 —— 那個 process 就多寄了一封而沒人知道。"""
    first = mail_quota.reserve(account=ACC, category=CLINICAL)
    mail_quota.release(first)
    second = mail_quota.reserve(account=ACC, category=CLINICAL)
    assert second.row_id != first.row_id, "id 不可回收再利用（AUTOINCREMENT）"
    mail_quota.release(first)            # 重複釋放（例如 retry 路徑上的 bug）
    assert len(_rows()) == 1, "重複釋放不可刪掉別人的名額"


def test_release_of_none_is_harmless():
    mail_quota.release(None)


# ─── 降級:store 壞掉時「少寄」而不是「不擋」也不是「全擋」 ────────────────
def _break_store(monkeypatch, tmp_path):
    """把 DB 路徑指到一個【檔案】底下 → sqlite 開不起來（模擬權限／防毒／損壞）。

    回傳一個 `heal()`。★不可用 `monkeypatch.undo()` 修回來★：那會把 conftest 的
    autouse `_isolate_settings_dir` 一起還原，DB 於是掉到【全 session 共用】的
    暫存目錄 —— 好幾支測試互相看到對方的封數，斷言就變成看運氣（實測第一版就這樣
    紅在「已寄 39 封」）。
    """
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("x", encoding="utf-8")
    original = mail_quota.db_path
    monkeypatch.setattr(mail_quota, "db_path",
                        lambda: str(blocker / "mail_quota.sqlite3"))
    return lambda: monkeypatch.setattr(mail_quota, "db_path", original)


def test_a_broken_store_degrades_to_the_in_process_limit_not_to_no_limit(
        monkeypatch, tmp_path):
    """★P1-06 那個錯不可以再犯一次★

    跨行程那層壞掉時退回「完全不擋」＝在最需要保護的時刻把保護關掉。
    降級之後仍必須有「本行程每小時 30 封」這層 —— 那就是修好之前的既有行為。
    """
    _break_store(monkeypatch, tmp_path)
    assert _reserve_n(mail_quota.TOTAL_MAX + 10, CLINICAL) == \
        mail_quota.TOTAL_MAX
    assert mail_quota._degraded_reason, "降級必須留下痕跡"


def test_a_broken_store_does_not_block_clinical_mail_entirely(
        monkeypatch, tmp_path):
    """★不可矯枉過正★ email 是這套系統唯一的告警管道；全擋比多寄還糟。"""
    _break_store(monkeypatch, tmp_path)
    r = mail_quota.reserve(account=ACC, category=CLINICAL)
    assert r.cross_process is False, "這一筆沒有跨行程紀錄，要照實標記"
    assert r.row_id is None


def test_degradation_is_logged_once_per_reason(monkeypatch, tmp_path, caplog):
    _break_store(monkeypatch, tmp_path)
    with caplog.at_level("WARNING"):
        _reserve_n(5, CLINICAL)
    warnings = [r for r in caplog.records
                if r.levelname == "WARNING" and "mail_quota" in r.getMessage()]
    assert len(warnings) == 1, "同一個原因不可每封信都洗一次版"


def test_category_quota_survives_degradation(monkeypatch, tmp_path):
    """降級之後【類別配額不可跟著消失】—— 兩層共用同一個判斷函式的理由。"""
    _break_store(monkeypatch, tmp_path)
    assert _reserve_n(mail_quota.TOTAL_MAX, SYSTEM) == mail_quota.SYSTEM_MAX
    assert _reserve_n(mail_quota.TOTAL_MAX, CLINICAL) == \
        mail_quota.TOTAL_MAX - mail_quota.SYSTEM_MAX


def test_recovery_clears_the_degraded_flag(monkeypatch, tmp_path):
    heal = _break_store(monkeypatch, tmp_path)
    mail_quota.reserve(account=ACC, category=CLINICAL)
    assert mail_quota._degraded_reason
    heal()
    mail_quota.reserve(account=ACC, category=CLINICAL)
    assert mail_quota._degraded_reason is None, "store 修好之後要恢復"


# ─── ★外審第 1 輪 finding★ 降級期間寄出的信,恢復後必須補寫進共用 DB ─────────
def test_degraded_sends_are_backfilled_once_the_store_recovers(
        monkeypatch, tmp_path):
    """★不補寫的話,那一小時的實際總量會回到修好之前的量級★

    store 壞掉的那段時間本行程照樣寄了 10 封而 DB 一筆都沒有；修好之後其他 process
    看到偏少的數字 → 又各自寄滿 30 封。而且 `snapshot()` 會把那個不完整的數字當成
    全機器總量報出去（＝「講程式不確知的事」）。
    """
    heal = _break_store(monkeypatch, tmp_path)
    assert _reserve_n(10, CLINICAL) == 10
    heal()
    assert mail_quota.snapshot(account=ACC)["pending_backfill"] == 10

    mail_quota.reserve(account=ACC, category=CLINICAL)      # 恢復後的第一封
    assert len(_rows()) == 11, "降級期間那 10 封必須被補寫進 DB"
    assert mail_quota.snapshot(account=ACC)["pending_backfill"] == 0


def test_backfilled_sends_count_against_other_processes(
        monkeypatch, tmp_path, child_script):
    """補寫的目的就是這個:別的 process 看得到那些信。"""
    app = _app_dir()
    heal = _break_store(monkeypatch, tmp_path)
    assert _reserve_n(25, CLINICAL) == 25
    heal()
    mail_quota.reserve(account=ACC, category=CLINICAL)      # 觸發補寫（第 26 封）

    got = _child(app, mail_quota.TOTAL_MAX, script=child_script)
    assert got == mail_quota.TOTAL_MAX - 26, (
        f"★另一支行程只該拿到 4 個名額，實際拿到 {got} 個★")


def test_backfill_keeps_the_original_timestamps():
    """補寫要用原本的 ts —— 用「現在」會把一小時前的信算成剛剛寄的，額度晚一小時才放。"""
    old = time.time() - mail_quota.WINDOW_SEC + 120     # 再 2 分鐘就過期
    mail_quota._recent.append(mail_quota.Reservation(
        account=ACC, category=CLINICAL, ts=old, token=object()))
    mail_quota.reserve(account=ACC, category=CLINICAL)
    con = sqlite3.connect(mail_quota.db_path())
    try:
        rows = [r[0] for r in con.execute("SELECT ts FROM mail_sends ORDER BY id")]
    finally:
        con.close()
    assert any(abs(ts - old) < 1 for ts in rows), "補寫的 ts 被改成現在了"


def test_backfill_does_not_double_count_healthy_reservations():
    """已經有 DB 紀錄的名額不可被當成欠帳再補寫一次（那會把封數多算）。"""
    _reserve_n(5, CLINICAL)
    assert len(_rows()) == 5
    _reserve_n(1, CLINICAL)
    assert len(_rows()) == 6, "第 6 次 reserve 不可把前 5 筆重複補寫"


def test_a_failed_send_during_recovery_still_releases_the_backfilled_row(
        monkeypatch, tmp_path):
    """呼叫端手上是降級時發的 row-less 版本，但 DB 裡那筆是恢復時補寫的 ——
    release 必須看 deque 裡的 row_id，否則那個名額整小時沒人收得回來。"""
    heal = _break_store(monkeypatch, tmp_path)
    degraded = mail_quota.reserve(account=ACC, category=CLINICAL)
    assert degraded.row_id is None
    heal()
    mail_quota.reserve(account=ACC, category=CLINICAL)      # 觸發補寫
    assert len(_rows()) == 2

    mail_quota.release(degraded)
    assert len(_rows()) == 1, "★補寫出來的那筆紀錄要刪得掉★"


def test_a_release_racing_with_the_recovery_backfill_leaves_no_orphan_row(
        monkeypatch, tmp_path):
    """★外審第 2 輪 finding:真的把兩個 thread 交錯起來,不是循序跑一遍★

    `_flush_backfill` 是「在 `_lock` 內算出 pending → 放掉 `_lock` → 才 INSERT」。
    若這中間有一封降級期間的信【寄失敗】而 release 插得進來,它會把那筆 row-less
    紀錄從 deque 移走、然後(還沒有 row_id)什麼都不刪就返回,而補寫照樣把它 INSERT
    進去 —— DB 留下一筆「從來沒寄出的信」的配額紀錄,沒人刪得掉,整整佔用一小時。
    """
    heal = _break_store(monkeypatch, tmp_path)
    degraded = mail_quota.reserve(account=ACC, category=CLINICAL)
    heal()

    real_flush = mail_quota._flush_backfill
    probe: dict = {}

    def _racing_flush(con, pending):
        # 已經算出 pending、還沒 INSERT 的那一瞬間,讓「寄失敗 → release」插進來。
        t = threading.Thread(target=mail_quota.release, args=(degraded,),
                             name="RacingRelease")
        t.start()
        t.join(timeout=0.4)
        # 有修 → release 卡在 _reserve_lock 上,這裡還活著;沒修 → 早就跑完了。
        probe["release_still_blocked"] = t.is_alive()
        probe["thread"] = t
        return real_flush(con, pending)

    monkeypatch.setattr(mail_quota, "_flush_backfill", _racing_flush)
    mail_quota.reserve(account=ACC, category=CLINICAL)   # 觸發補寫（第 2 封）
    probe["thread"].join(timeout=10)
    assert not probe["thread"].is_alive(), "release thread 沒有結束（可能死鎖）"

    # 先斷言「可觀察到的後果」，再斷言機制 —— 前者才是這個 finding 的真正傷害。
    assert len(_rows()) == 1, (
        f"★沒寄出的那封信留下了孤兒配額紀錄：{_rows()}★")
    assert probe["release_still_blocked"] is True, (
        "★release 必須被補寫序列化★（沒被擋住就代表兩者可以交錯）")


def test_snapshot_admits_it_has_been_degraded(monkeypatch, tmp_path):
    """恢復之後 DB 讀得到了，但【別的 process】可能也還欠著帳 —— 呼叫端要看得出
    這個數字曾經有過缺口，不可當成鐵板一塊的全機器總量。"""
    assert mail_quota.snapshot(account=ACC)["ever_degraded"] is False
    heal = _break_store(monkeypatch, tmp_path)
    mail_quota.reserve(account=ACC, category=CLINICAL)
    heal()
    snap = mail_quota.snapshot(account=ACC)
    assert snap["cross_process"] is True
    assert snap["ever_degraded"] is True


def test_snapshot_reports_degradation_honestly(monkeypatch, tmp_path):
    snap = mail_quota.snapshot(account=ACC)
    assert snap["cross_process"] is True and snap["degraded"] is None
    _break_store(monkeypatch, tmp_path)
    snap = mail_quota.snapshot(account=ACC)
    assert snap["cross_process"] is False, (
        "讀不到 DB 時不可把「本行程的封數」講成全機器的總量")
    assert snap["degraded"]


def test_a_refusal_says_the_count_is_process_only_when_degraded(
        monkeypatch, tmp_path):
    """★措辭鐵律★ 降級時那個封數只算得出本行程，訊息不可講成全機器的總量。"""
    _break_store(monkeypatch, tmp_path)
    _reserve_n(mail_quota.TOTAL_MAX, CLINICAL)
    with pytest.raises(mail_quota.MailQuotaExceeded) as ei:
        mail_quota.reserve(account=ACC, category=CLINICAL)
    assert "只算本行程" in str(ei.value)
    assert "跨行程配額目前不可用" in str(ei.value)


def test_a_refusal_from_the_in_process_layer_is_labelled_process_only():
    """本行程自己就寄滿 30 封時，是行程內那層先擋下的 —— 那個 30 確實只算本行程，
    所以照實標記（就算跨行程 store 好好的）。全機器的實際封數另外記在 log。"""
    _reserve_n(mail_quota.TOTAL_MAX, CLINICAL)
    with pytest.raises(mail_quota.MailQuotaExceeded) as ei:
        mail_quota.reserve(account=ACC, category=CLINICAL)
    assert "只算本行程" in str(ei.value)
    assert "跨行程配額目前不可用" not in str(ei.value), "store 沒壞就不該說它壞了"


def _foreign_rows(n: int, category: str = CLINICAL, account: str = ACC):
    """模擬【別的 process】已經寄掉 n 封（直接寫共用 DB，本行程記憶體裡沒有紀錄）。"""
    con = sqlite3.connect(mail_quota.db_path())
    try:
        for stmt in mail_quota._SCHEMA:
            con.execute(stmt)
        now = time.time()
        con.executemany(
            "INSERT INTO mail_sends (ts, account, category) VALUES (?,?,?)",
            [(now, account, category)] * n)
        con.commit()
    finally:
        con.close()


def test_a_refusal_caused_by_another_process_is_not_labelled_process_only():
    """額度是【別的 process】用掉的 → 訊息不可說「只算本行程」（那會誤導成本程式
    自己寄了 30 封，於是有人跑去查錯的 log）。"""
    _foreign_rows(mail_quota.TOTAL_MAX)
    with pytest.raises(mail_quota.MailQuotaExceeded) as ei:
        mail_quota.reserve(account=ACC, category=CLINICAL)
    assert "只算本行程" not in str(ei.value)


def test_snapshot_counts_by_category():
    _reserve_n(2, SYSTEM)
    _reserve_n(3, CLINICAL)
    snap = mail_quota.snapshot(account=ACC)
    assert snap["counts"] == {SYSTEM: 2, CLINICAL: 3}


# ─── 走生產呼叫形狀（send_mail），不是只測 mail_quota 本身 ────────────────
def _credentials() -> dict:
    return {"host": "smtp.example.com", "port": 587,
            "username": ACC, "password": "secret", "use_tls": True,
            "from_address": "alias@example.com", "from_name": "Sender"}


def _send(monkeypatch, **kw):
    monkeypatch.setattr(smtp_mail, "_send_once", lambda *_a, **_k: None)
    smtp_mail.send_mail(["r@example.com"], "s", "b",
                        override_credentials=_credentials(), max_retries=0, **kw)


def test_send_mail_records_the_clinical_category_by_default(monkeypatch):
    """★預設刻意是 clinical★ 漏標一個系統呼叫端只是「跟修好之前一樣」；
    反之預設 system 會讓漏標的臨床告警在保留名額還空著時被拒寄。"""
    _send(monkeypatch)
    assert _rows() == [(ACC, CLINICAL)]


def test_send_mail_passes_the_system_category_through(monkeypatch):
    _send(monkeypatch, category=SYSTEM)
    assert _rows() == [(ACC, SYSTEM)]


def test_the_quota_key_is_the_authenticated_account_not_the_from_address(
        monkeypatch):
    """額度綁在向 Gmail 認證的帳號上；別名寄件（from_address 不同）不會多一份額度。"""
    _send(monkeypatch)
    assert _rows() == [(ACC, CLINICAL)], "不可用 from_address 當鑰匙"


def test_a_failed_send_releases_the_cross_process_slot(monkeypatch):
    monkeypatch.setattr(smtp_mail, "_send_once",
                        lambda *_a, **_k: (_ for _ in ()).throw(OSError("offline")))
    with pytest.raises(RuntimeError, match="offline"):
        smtp_mail.send_mail(["r@example.com"], "s", "b",
                            override_credentials=_credentials(), max_retries=0)
    assert _rows() == [], "★寄失敗必須把跨行程名額還回去★"
    assert not mail_quota._recent


def test_send_mail_raises_the_old_exception_name_when_over_quota(monkeypatch):
    """`SmtpRateLimitExceeded` 是舊名（現在等於 MailQuotaExceeded）；
    既有的 `except SmtpRateLimitExceeded` 呼叫端不可因為改名而漏接。"""
    _reserve_n(mail_quota.TOTAL_MAX, CLINICAL)
    monkeypatch.setattr(smtp_mail, "_send_once", lambda *_a, **_k: None)
    with pytest.raises(smtp_mail.SmtpRateLimitExceeded):
        smtp_mail.send_mail(["r@example.com"], "s", "b",
                            override_credentials=_credentials(), max_retries=0)


# ─── 呼叫端有沒有標類別（機械檢查）─────────────────────────────────────────
def test_the_known_system_mail_call_sites_are_tagged():
    """系統／除錯類的呼叫端若沒標 category，保留名額機制形同不存在。

    這是【原始碼層】的檢查，不是行為測試 —— 它守的是「以後新增系統類寄信時別忘了
    標」。真正的行為由上面 send_mail 那兩支測。
    """
    root = os.path.join(os.path.dirname(__file__), "..", "src")
    # 定位點都選在【呼叫之前】的唯一字串，往後 900 字元內必須看到 category="system"。
    checks = [
        ("consult_query.py", "會診查詢：剛已處理（重複觸發已略過）"),
        ("consult_query.py", "⚠ 會診查詢自動化連續失敗"),
        ("consult_query.py", "會診查詢失敗通知"),
        ("consult_query.py", "測試信 (SMTP)"),
        ("main.py", "[皮膚科自動化] 稽核"),
        # _notify_his_drift（改版偵測通知）——主旨是在別處組的，直接以呼叫本身定位
        ("main.py", "_send_alert_email_via_smtp(subject, body, list(recipients),"),
    ]
    missing = []
    for fname, anchor in checks:
        src = open(os.path.join(root, fname), encoding="utf-8").read()
        i = src.find(anchor)
        assert i > 0, f"{fname} 找不到定位點「{anchor}」（信件內容改過？）"
        if 'category="system"' not in src[i:i + 900]:
            missing.append(f"{fname}:{anchor}")
    assert not missing, f"這些系統類寄信沒有標 category=\"system\"：{missing}"
