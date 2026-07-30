# -*- coding: utf-8 -*-
"""寄信配額（跨行程 + 行程內雙層）。

★[2026-07-30 第二輪外審 P2-02] 為什麼要有這個模組★
在此之前 rate limit 是 `smtp_mail` 模組裡的一個 `deque` + `threading.Lock`。
那把鎖只鎖得住【同一個 process 裡的 thread】，但這個 repo 同時跑五支獨立程式：

    main（主程式／止掛提醒／稽核告警）
    autoclock（打卡）
    consult_query（會診查詢）
    watchdog（看門狗）
    scheduler（排程）

它們共用【同一個 Gmail 帳號】寄信，卻各自以為自己每小時可以寄 30 封 —— 也就是
真正的上限其實是 30×5。這個 rate limit 存在的唯一理由是「程式出 bug 陷入迴圈時
不要把 Gmail 帳號寄爆／被 Google 停用」，而 bug 迴圈最可能同時發生在多支程式
（例如共用的 HIS 契約偵測改版、共用的網路斷線），正是最需要它的時候它最沒用。

★設計取捨★

1. **跨行程用 SQLite，不是 lock-protected JSON。**
   需要的是「清舊紀錄 → 數目前幾封 → 寫入一筆」三步不可被別的 process 插隊。
   SQLite 的 `BEGIN IMMEDIATE` + `busy_timeout` 直接提供跨行程序列化與原子
   commit，而且 process 被強制終止時不會留下一把要人手動清的鎖檔（`updater.py`
   的 msvcrt 鎖就有這個代價，那裡是短暫持有所以可接受）。

2. **兩層都保留，而且共用同一份配額判斷（`quota_refusal`）。**
   SQLite 拿不到（磁碟權限、防毒、DB 損壞）時【降級】成原本的行程內 deque，
   不是放行也不是全擋：
     * 放行＝在最需要這個保護的時刻把保護關掉（＝P1-06 更新鎖犯過的錯）。
     * 全擋＝連臨床告警都寄不出去，而 email 正是這套系統唯一的告警管道。
   降級後仍有「單一 process 每小時 30 封」這層 —— 那就是修好之前的既有行為，
   嚴格優於沒有。配額判斷寫成純函式讓兩層共用，否則降級時類別配額會跟著消失。

3. **配額鑰匙＝SMTP 帳號 + 類別，刻意【不】含收件人。**
   外審建議 key 含收件人，但要保護的資源是「這個 Gmail 帳號的寄送額度」，
   而一封信可以有多個收件人 —— 若按收件人計費，一封 4 人的信到底算 1 還是 4？
   若其中一人超額是要拆信、還是整封拒？兩個答案都會讓「臨床告警有沒有寄出」
   變得難以推理。「同一件事不要重複轟炸同一個人」已經由 `alert_dedupe.py`
   以事件為單位處理，那是它的職責，不該在額度層再做一次半套的。

4. **臨床與系統兩種配額，臨床永遠有保留名額。**
   `system`（故障告警、健康檢查、改版偵測、重複觸發提醒、測試信）最多用
   `SYSTEM_MAX`；`clinical`（止掛提醒、會診結果、回讀不符）可用到總額
   `TOTAL_MAX`。因此系統類再怎麼迴圈狂寄，臨床類永遠還有
   `TOTAL_MAX - SYSTEM_MAX` 個名額。反向不設限是刻意的：臨床信的數量受真實
   事件（診次、會診數）約束，不會因為 bug 而暴增。

5. **釋放失敗寧可少寄，不可多寄。**
   `release()` 吞掉所有錯誤：釋放不掉的名額最多佔用一小時就自然過期，
   而「釋放失敗就當成沒佔用」會讓失敗重試變成無上限寄信。

6. **降級期間寄出的信，store 一恢復就補寫進去（`_flush_backfill`）。**
   ★[外審第 1 輪 finding]★ 只做 (2) 的降級而不補寫，等於留了一個洞：store 壞掉
   那段時間本行程照樣寄了（最多）30 封而 DB 一筆都沒有，修好之後其他 process 看到
   一個【偏少】的數字，於是又各自寄滿 30 封 —— 那一小時的實際總量回到修好之前的
   量級，而 `snapshot()` 還會把那個不完整的數字當成全機器總量報出去。
   補寫用原本的 ts；補寫可能讓封數直接超過上限，那是【正確的】結果。

★已知限制（刻意，不是漏做）★
  * 補寫是在本行程【下一次 reserve】時觸發。若某支程式在降級期間寄了幾封之後就
    整個視窗都不再寄信，那幾封就不會被補寫 —— 但這個機制要防的是「bug 迴圈狂寄」，
    而會迴圈的程式必然一直在 reserve。
  * 只涵蓋【同一台機器】的行程（store 在各機器自己的 settings 目錄）。同一個 Gmail
    帳號若在多台機器上跑，仍然各機器各算各的；外審指出的實際量級是一台機器五支程式。
  * `snapshot()` 另外回報 `pending_backfill` 與 `ever_degraded`，讓呼叫端看得出
    「這個數字可能還有缺口」，而不是把它當成鐵板一塊的全機器總量。
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from cmuh_common.paths import get_settings_dir

# ─── 配額:★單一權威★ ──────────────────────────────────────────────────────
WINDOW_SEC = 3600       # 統計區間 1 小時
TOTAL_MAX = 30          # 同一個 SMTP 帳號 1 小時內最多 30 封（所有 process 合計）
SYSTEM_MAX = 18         # 其中系統／除錯類最多 18 封 → 臨床類永遠保留 12 個名額

CATEGORY_CLINICAL = "clinical"   # 止掛提醒、會診結果、回讀不符 —— 關於病人的事
CATEGORY_SYSTEM = "system"       # 故障告警、健康檢查、改版偵測、測試信 —— 關於程式的事

DB_NAME = "mail_quota.sqlite3"
_BUSY_TIMEOUT_MS = 8000

_SCHEMA = (
    # AUTOINCREMENT 是刻意的:沒有它,`DELETE` 之後 rowid 會被回收再利用 →
    # 一個慢半拍的 release() 可能刪掉別人剛拿到的名額。release() 另外再比對
    # ts 與 account,兩者相符才刪。
    "CREATE TABLE IF NOT EXISTS mail_sends ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
    " ts REAL NOT NULL,"
    " account TEXT NOT NULL,"
    " category TEXT NOT NULL)",
    "CREATE INDEX IF NOT EXISTS ix_mail_sends_ts ON mail_sends (ts)",
)


class MailQuotaExceeded(RuntimeError):
    """寄信封數超過配額的保護性錯誤。"""


@dataclass(frozen=True)
class Reservation:
    """一個已佔用的寄信名額。寄失敗要 `release()` 還回去。

    `row_id` / `cross_process` 刻意 `compare=False`：行程內那層是先佔的（那時還
    不知道 row_id），拿到 row_id 之後產生的新物件必須仍然等於 deque 裡那一筆，
    `release()` 才收得回來。身分靠 `token`（identity）保證唯一。
    """
    account: str
    category: str
    ts: float
    token: object                                       # 行程內那層的身分
    row_id: "int | None" = field(default=None, compare=False)
    cross_process: bool = field(default=False, compare=False)


# ─── 配額判斷:純函式,兩層共用 ────────────────────────────────────────────
def _minutes(sec: float) -> int:
    return max(1, int(sec // 60))


def quota_refusal(counts: dict, category: str, *,
                  oldest_ago: "float | None" = None) -> "str | None":
    """看目前各類別已寄幾封，決定是否拒絕。回拒絕理由，或 None＝放行。

    counts: {category: 該類別在視窗內的封數}
    oldest_ago: 視窗內最舊那封距今幾秒（有值時訊息會告訴使用者幾分鐘後可再試）
    """
    total = sum(int(v) for v in counts.values())
    when = ""
    if oldest_ago is not None:
        when = f"，請 {_minutes(WINDOW_SEC - oldest_ago)} 分鐘後再試"
    if total >= TOTAL_MAX:
        return (f"寄信配額：過去 {_minutes(WINDOW_SEC)} 分鐘已寄 {total} 封"
                f"（帳號上限 {TOTAL_MAX}）{when}")
    if category == CATEGORY_SYSTEM:
        used = int(counts.get(CATEGORY_SYSTEM, 0))
        if used >= SYSTEM_MAX:
            return (f"寄信配額：系統／除錯類過去 {_minutes(WINDOW_SEC)} 分鐘已寄 "
                    f"{used} 封（上限 {SYSTEM_MAX}，其餘名額保留給臨床告警）{when}")
    return None


# ─── 跨行程層:SQLite ──────────────────────────────────────────────────────
def db_path() -> str:
    """★一定要延遲取得★ 模組載入時就算路徑，測試的 settings 目錄隔離會失效。"""
    return os.path.join(get_settings_dir(), DB_NAME)


def _connect(path: str) -> sqlite3.Connection:
    """每次操作開一條新連線。

    寄信是從好幾個背景 thread 發出的，而 sqlite3 連線預設不可跨 thread 共用；
    這裡的交易極短（三個小 SQL），開連線的成本遠低於維護一個 thread-local 池
    以及池子在降級／DB 被刪除後要如何重建的複雜度。
    """
    con = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_MS / 1000.0,
                          isolation_level=None)   # 交易由我們自己下 BEGIN
    con.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    # WAL：讓「數封數」的讀不會被另一支 process 的寫擋住。
    try:
        con.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass        # 有些檔案系統不支援 WAL；退回 delete journal 仍然正確
    for stmt in _SCHEMA:
        con.execute(stmt)
    return con


def _flush_backfill(con: sqlite3.Connection, pending: list) -> dict:
    """把降級期間【已經寄出但沒寫進 DB】的名額補寫進去，回傳 {token: row_id}。

    ★[2026-07-30 外審 P2-02 第 1 輪]★ 沒有這一步的話：跨行程 store 壞掉的那段時間，
    本行程照樣寄了(最多)30 封而 DB 裡一筆都沒有；store 修好之後其他 process 看到的
    是一個【偏少】的數字，於是又各自寄滿 30 封 —— 那一小時的實際總量回到修好之前的
    量級，而 `snapshot()` 還會把那個不完整的數字當成全機器總量報出去。

    補寫用原本的 ts（不是現在），才不會把一小時前的信算成剛剛寄的。補寫可能讓封數
    直接超過上限：那是【正確的】結果，代表那一小時真的寄超了，後續 reserve 會被擋。
    """
    if not pending:
        return {}
    filled: dict = {}
    con.execute("BEGIN IMMEDIATE")
    try:
        for r in pending:
            filled[r.token] = int(con.execute(
                "INSERT INTO mail_sends (ts, account, category)"
                " VALUES (?, ?, ?)", (r.ts, r.account, r.category)).lastrowid)
        con.execute("COMMIT")
    except BaseException:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    logging.warning(
        "[mail_quota] 跨行程配額已恢復，補寫降級期間的 %d 筆寄信紀錄", len(filled))
    return filled


def _reserve_row(con: sqlite3.Connection, *, account: str, category: str,
                 now: float) -> int:
    cutoff = now - WINDOW_SEC
    row_id: "int | None" = None
    why: "str | None" = None
    con.execute("BEGIN IMMEDIATE")
    try:
        # 順手清掉視窗外的舊紀錄，以及【時鐘倒退造成的未來紀錄】—— 後者若不清，
        # 一次系統時間跳動就會讓那些紀錄永遠留在「視窗內」，把配額永久佔掉。
        con.execute("DELETE FROM mail_sends WHERE ts < ? OR ts > ?",
                    (cutoff, now + WINDOW_SEC))
        counts = {str(c): int(n) for c, n in con.execute(
            "SELECT category, COUNT(*) FROM mail_sends WHERE account = ?"
            " GROUP BY category", (account,)).fetchall()}
        oldest = con.execute(
            "SELECT MIN(ts) FROM mail_sends WHERE account = ?",
            (account,)).fetchone()[0]
        why = quota_refusal(
            counts, category,
            oldest_ago=(now - float(oldest)) if oldest is not None else None)
        if why is None:
            row_id = int(con.execute(
                "INSERT INTO mail_sends (ts, account, category)"
                " VALUES (?, ?, ?)", (now, account, category)).lastrowid)
        con.execute("COMMIT")       # 就算被拒，清理成果也要留住
    except BaseException:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    if why is not None:
        raise MailQuotaExceeded(why)
    assert row_id is not None
    return row_id


# ─── 行程內層 + 降級狀態 ──────────────────────────────────────────────────
_lock = threading.Lock()            # 只保護 _recent 的短臨界區
_reserve_lock = threading.Lock()    # 序列化整個 reserve()，見該函式 docstring
_recent: "deque" = deque(maxlen=TOTAL_MAX * 4)
_degraded_reason: "str | None" = None
_degraded_logged: set = set()


def _degrade(reason: str) -> None:
    """記下「跨行程那層目前不管用」。同一個原因只 warning 一次，避免 log 洗版。"""
    global _degraded_reason
    _degraded_reason = reason
    if reason not in _degraded_logged:
        _degraded_logged.add(reason)
        logging.warning(
            "[mail_quota] 跨行程寄信配額無法使用（%s）→ 本行程降級為「單一 process "
            "每小時 %d 封」。多支程式同時寄信時實際總量可能超過 %d 封；"
            "請檢查 settings 目錄的寫入權限或防毒設定。",
            reason, TOTAL_MAX, TOTAL_MAX)


def _healthy() -> None:
    global _degraded_reason
    _degraded_reason = None


def _prune_locked(now: float) -> None:
    cutoff = now - WINDOW_SEC
    horizon = now + WINDOW_SEC
    while _recent and _recent[0].ts < cutoff:
        _recent.popleft()
    # 時鐘倒退造成的「未來」紀錄永遠不會從左邊過期 —— 必須整批挑掉，否則一次系統
    # 時間跳動就把本行程的額度永久佔掉。跨行程那層的 DELETE 同理（見 _reserve_row）。
    if any(r.ts > horizon for r in _recent):
        kept = [r for r in _recent if r.ts <= horizon]
        _recent.clear()
        _recent.extend(kept)


def _in_process_counts_locked(account: str) -> "tuple[dict, float | None]":
    counts: dict = {}
    oldest: "float | None" = None
    for r in _recent:
        if r.account != account:
            continue
        counts[r.category] = counts.get(r.category, 0) + 1
        if oldest is None or r.ts < oldest:
            oldest = r.ts
    return counts, oldest


def _refusal_detail(why: str, account: str, now: float, *,
                    cross_process: bool) -> str:
    """在拒絕理由後面補上「這個數字算的是誰」，並把現況記進 log。

    ★措辭鐵律★ 降級時那個封數只算得出【本行程】的量；訊息與 log 都不可講成
    全機器的總量，否則之後有人拿這行 log 去推論「帳號今天只寄了 5 封」就錯了。
    """
    if not cross_process:
        why += ("（此封數只算本行程；跨行程配額目前不可用："
                f"{_degraded_reason}）" if _degraded_reason
                else "（此封數只算本行程）")
    snap = snapshot(account=account, now=now)
    logging.warning(
        "[mail_quota] 拒寄一封信：%s｜目前封數 %s（%s）", why, snap["counts"],
        "全機器合計" if snap["cross_process"] else "僅本行程")
    return why


def reserve(*, account: str, category: str = CATEGORY_CLINICAL,
            now: "float | None" = None) -> Reservation:
    """佔用一個寄信名額。超額則 raise `MailQuotaExceeded`。

    先過行程內那層（純記憶體、不會失敗），再過跨行程那層。跨行程拒絕時要把
    行程內那筆收回來，否則本行程的額度會被一筆從未寄出的信白白吃掉。

    ★整段以 `_reserve_lock` 序列化★（`_lock` 只保護 deque 的短臨界區）。
    理由是補寫（`_flush_backfill`）要靠「deque 裡沒有 row_id」認出欠帳，而一筆
    【正在跑跨行程那段、還沒換成帶 row_id 的版本】的名額長得一模一樣 —— 兩個寄信
    thread 同時進來就會把對方那筆重複補寫一次，那一小時的封數於是被多算。
    寄信本來就不是熱路徑（上限 30 封／小時），序列化的代價遠低於算錯配額。
    """
    with _reserve_lock:
        return _reserve_locked(account=account, category=category, now=now)


def _reserve_locked(*, account: str, category: str,
                    now: "float | None") -> Reservation:
    now = time.time() if now is None else now
    account = (account or "?").strip().lower()
    reservation = Reservation(account=account, category=category, ts=now,
                             token=object())
    with _lock:
        _prune_locked(now)
        counts, oldest = _in_process_counts_locked(account)
        why = quota_refusal(
            counts, category,
            oldest_ago=(now - oldest) if oldest is not None else None)
        if why is not None:
            refused = why
        else:
            refused = None
            _recent.append(reservation)
    if refused is not None:
        raise MailQuotaExceeded(_refusal_detail(refused, account, now,
                                                cross_process=False))

    try:
        con = _connect(db_path())
    except (sqlite3.Error, OSError) as e:
        _degrade(f"開不了 {DB_NAME}：{e}")
        return reservation
    try:
        # store 一旦可用，先把降級期間欠的紀錄補寫進去（含本次以外的舊筆），
        # 再數封數 —— 否則剛恢復的那一刻其他 process 會看到偏少的數字。
        with _lock:
            pending = _pending_backfill_locked(reservation)
        if pending:
            filled = _flush_backfill(con, pending)
            with _lock:
                for r in pending:
                    rid = filled.get(r.token)
                    if rid is not None:
                        _swap_locked(r, r.__class__(
                            account=r.account, category=r.category, ts=r.ts,
                            token=r.token, row_id=rid, cross_process=True))
        row_id = _reserve_row(con, account=account, category=category, now=now)
    except MailQuotaExceeded as e:
        with _lock:
            _discard_locked(reservation)
        _healthy()
        raise MailQuotaExceeded(_refusal_detail(str(e), account, now,
                                                cross_process=True)) from None
    except (sqlite3.Error, OSError) as e:
        _degrade(f"{DB_NAME} 交易失敗：{e}")
        return reservation
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass
    _healthy()
    full = Reservation(account=account, category=category, ts=now,
                       token=reservation.token, row_id=row_id,
                       cross_process=True)
    with _lock:
        # deque 裡那筆要換成帶 row_id 的版本，否則 `_pending_backfill_locked`
        # 會把已經有紀錄的名額當成「欠著的」，恢復時重複補寫。
        _swap_locked(reservation, full)
    return full


def _pending_backfill_locked(exclude: Reservation) -> list:
    """本行程「已佔用但 DB 裡沒有紀錄」且仍在視窗內的名額（本次那筆除外）。"""
    return [r for r in _recent
            if r.row_id is None and r.token is not exclude.token]


def _swap_locked(old: Reservation, new: Reservation) -> None:
    try:
        idx = list(_recent).index(old)
    except ValueError:
        return          # 已經被 release 掉了：不要把它加回去
    _recent[idx] = new


def _discard_locked(reservation: Reservation) -> "int | None":
    """從 deque 移除，並回傳【deque 裡那一筆】的 row_id。

    呼叫端手上的 Reservation 可能是降級時發出的 row-less 版本，而 deque 裡那筆
    已經在恢復時被補寫、換成帶 row_id 的版本 —— 若只看呼叫端手上的值，那筆補寫
    出來的 DB 紀錄就沒人刪得掉（一整小時的額度白白被一封寄失敗的信佔住）。
    """
    try:
        idx = list(_recent).index(reservation)
    except ValueError:
        return None
    row_id = _recent[idx].row_id
    del _recent[idx]
    return row_id


def release(reservation: "Reservation | None") -> None:
    """寄失敗時把名額還回去。★絕不拋例外★

    跨行程那筆刪不掉就算了：它最多佔用一小時後自然過期（少寄），而把刪不掉
    當成「已經還回去」會讓失敗重試迴圈變成無上限寄信（多寄）。

    ★[2026-07-30 外審 P2-02 第 2 輪]★ 整段也要拿 `_reserve_lock`。
    `_flush_backfill()` 是「先在 `_lock` 內算出 pending → 放掉 `_lock` → 才 INSERT」；
    若這中間有一封降級期間的信寄失敗而 release 進得來，它會把那筆 row-less 紀錄從
    deque 移走然後（因為還沒有 row_id）什麼都不刪就返回，而補寫照樣把它 INSERT 進去
    —— DB 於是留下一筆【從來沒寄出的信】的配額紀錄，沒有任何人刪得掉，整整佔用一小時。
    這種孤兒紀錄累積起來會把臨床告警擋掉，所以不能當成「保守方向」放過。
    """
    if reservation is None:
        return
    with _reserve_lock:
        _release_locked(reservation)


def _release_locked(reservation: Reservation) -> None:
    with _lock:
        row_id = _discard_locked(reservation)
    if row_id is None:
        row_id = reservation.row_id
    if row_id is None:
        return
    try:
        con = _connect(db_path())
    except (sqlite3.Error, OSError):
        logging.debug("[mail_quota] 釋放名額時開不了 DB（略過）", exc_info=True)
        return
    try:
        con.execute("BEGIN IMMEDIATE")
        try:
            # 三個欄位都要對得上才刪 —— 見 _SCHEMA 對 rowid 重用的說明。
            con.execute(
                "DELETE FROM mail_sends WHERE id = ? AND account = ? AND ts = ?",
                (row_id, reservation.account, reservation.ts))
            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    except (sqlite3.Error, OSError):
        logging.debug("[mail_quota] 釋放名額失敗（略過，一小時後自然過期）",
                      exc_info=True)
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass


def snapshot(*, account: "str | None" = None,
             now: "float | None" = None) -> dict:
    """給健康檢查／log 看的現況。★絕不拋例外★

    cross_process=False 代表「這個數字只算本行程」—— 呼叫端顯示時務必照實講，
    不可把它當成全機器的總量（措辭鐵律：只講程式確知的事）。

    pending_backfill > 0 代表【本行程】還有降級期間寄出、尚未補寫進 DB 的封數，
    此時 counts 即使 cross_process=True 也仍然偏少（其他 process 也可能有它們自己
    的欠帳，那是本行程無從得知的 —— 這一點寫在模組 docstring 的已知限制裡）。
    """
    now = time.time() if now is None else now
    out: dict = {"cross_process": False, "degraded": _degraded_reason,
                 "ever_degraded": bool(_degraded_logged),
                 "pending_backfill": 0,
                 "counts": {}, "limits": {"total": TOTAL_MAX,
                                          "system": SYSTEM_MAX,
                                          "window_min": _minutes(WINDOW_SEC)}}
    with _lock:
        _prune_locked(now)
        out["pending_backfill"] = sum(1 for r in _recent if r.row_id is None)
    try:
        con = _connect(db_path())
    except (sqlite3.Error, OSError) as e:
        out["degraded"] = out["degraded"] or f"開不了 {DB_NAME}：{e}"
    else:
        try:
            cutoff = now - WINDOW_SEC
            sql = ("SELECT category, COUNT(*) FROM mail_sends"
                   " WHERE ts >= ? AND ts <= ?")
            params: list = [cutoff, now + WINDOW_SEC]
            if account:
                sql += " AND account = ?"
                params.append(account.strip().lower())
            out["counts"] = {str(c): int(n) for c, n in
                             con.execute(sql + " GROUP BY category",
                                         params).fetchall()}
            out["cross_process"] = True
        except (sqlite3.Error, OSError) as e:
            out["degraded"] = out["degraded"] or f"讀不到 {DB_NAME}：{e}"
        finally:
            try:
                con.close()
            except sqlite3.Error:
                pass
    if not out["cross_process"]:
        want = account.strip().lower() if account else None
        counts: dict = {}
        with _lock:
            _prune_locked(now)
            for r in _recent:
                if want is not None and r.account != want:
                    continue
                counts[r.category] = counts.get(r.category, 0) + 1
        out["counts"] = counts
    return out
