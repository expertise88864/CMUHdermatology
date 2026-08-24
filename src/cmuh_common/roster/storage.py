# -*- coding: utf-8 -*-
"""排班資料唯一檔案 IO 層（設計文件 §4）。

原則：
- 其餘程式碼**不得**直接開檔——全部經由 RosterStorage。未來跨機同步
  （private git / 共享資料夾）只需替換/包裝本層（§15）。
- 寫入走 cmuh_common.atomic_io.atomic_write_json；月份檔覆蓋前自動留
  時間戳快照（.bak-YYYYmmddHHMMSS，保留最近 KEEP_SNAPSHOTS 份）。
- 所有檔案含 schema_version；讀到新版本檔（>SCHEMA_VERSION）→ 拒寫防降級毀損。
- 已定案（finalized=True）月份：save_month 需 force=True 才允許覆寫。
"""
from __future__ import annotations

import contextlib
import copy
import glob
import hashlib
import json
import logging
import os
import shutil
import threading
import time
from datetime import date, datetime
from typing import Optional

from cmuh_common.atomic_io import atomic_write_json
from cmuh_common.roster.model import SCHEMA_VERSION, month_dates

KEEP_SNAPSHOTS = 20

#: `expected_revision` 的「沒有帶」哨兵 —— `None` 不能用:它是【檔案還不存在】
#: 這個有意義的期望值(首次建檔要能 CAS 成 "")。
_UNSET = object()

# ★[2026-08-02 第二輪外審 P2-04] 寫入的備份政策★
# 「快照失敗只記 warning、照樣覆寫」對【每改一格就存一次】的月檔是合理的
# （不能因為備份不成就讓人排不了班），但對【失去備份就再也回不來】的那幾份不是
# 同一件事。同一份程式對兩者用同一套風險政策，本身就是不一致。
BEST_EFFORT_BACKUP = "best_effort"   # 備份不成 → 記 warning 後照常寫
REQUIRE_BACKUP = "require"           # 備份不成 → 拒寫（見各 save_* 的選擇理由）


class FinalizedMonthError(RuntimeError):
    """月份已定案，未 force 不可覆寫。"""


class NewerSchemaError(RuntimeError):
    """檔案 schema_version 比程式新（另一台較新版本寫的）→ 拒絕寫入。"""


class StaleRosterDataError(RuntimeError):
    """要寫回的內容是【從舊版本讀出來的】——盤上這一份已經被別人改過。

    ★這是跨機同步的正常結果,不是錯誤狀態★(外審排班第 1 輪 P1-01):
    GitSync 的背景 pull 會在使用者「讀出來 → 改 → 存回去」的中間把月檔換成
    他機的新版本;整份寫回就會把對方剛同步成功的欄位靜默退回舊值(Git 看到的
    是一個 pull 之後產生的合法新變更,幫不上忙)。
    呼叫端要嘛重讀最新版再把【同一個窄改動】套上去(`RosterService.update_month`),
    要嘛明確拒絕(帶著預先算好的結果落地的路徑:套用排班、定案)。
    """


#: 「這一刻讀不到」的版本識別。★不可以摺進 ""(=還沒有這一份)★:
#: 檔案被防毒/同步軟體暫時鎖住時,把它當成「不存在」會讓 CAS 拿 "" 比 "" 而
#: 通過 —— 於是一份【從讀不到的檔推導出來的空月檔】被整份寫回去。這正是
#: `_guard_overwrite` 早就分開處理過的兩件事(見其 docstring)。
#: 這個值永遠不會等於任何 sha256 十六進位字串。
_UNREADABLE_REV = "<unreadable>"


def _revision_of(raw) -> str:
    """這一份內容的版本識別。b""→ ""(還沒有這一份);None → 讀不到。"""
    if raw is None:
        return _UNREADABLE_REV
    if not raw:
        return ""
    return hashlib.sha256(raw).hexdigest()


def _read_bytes(path: str) -> "bytes | None":
    """讀原始位元組。缺檔 → b"";★暫時讀不到 → None★(與缺檔是兩件事)。"""
    try:
        with open(path, "rb") as f:
            return f.read()
    except FileNotFoundError:
        return b""
    except OSError:
        logging.warning("[roster.storage] 讀取失敗(暫時讀不到): %s", path,
                        exc_info=True)
        return None


def _file_revision(path: str) -> str:
    """盤上這一份檔案【現在】的版本識別。"""
    return _revision_of(_read_bytes(path))


def _parse_json_bytes(raw: "bytes | None", path: str) -> dict:
    """把已讀進來的位元組解析成 dict。★解析規則只有這一份★

    `_load_json` 也走這裡 —— 讀檔與「讀檔並記下版本」若各自解析,兩邊對
    BOM/壞檔的判定就會漂移,而那正是「守門說沒事、讀取卻回空」的那道縫
    (見 `_load_json` 的說明)。
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        logging.warning("[roster.storage] 解析失敗(視為空): %s", path,
                        exc_info=True)
        return {}


def _now_stamp() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _load_json(path: str) -> dict:
    """壞檔/缺檔回 {}（呼叫端補預設），絕不拋例外中斷 UI。

    ★[2026-08-02 補審] 一律用 utf-8-sig 讀★ 本模組的三個讀取點（本函式、
    `_guard_overwrite`、`assert_readable`）必須用【同一套解析規則】，否則會出現
    「守門說沒事、讀取卻回空」的縫 —— 而那正是把好資料寫成空白的那條路徑：
    帶 BOM 的檔在 utf-8 下 `json.load` 直接 JSONDecodeError（Python 的訊息本身
    就寫著 "Unexpected UTF-8 BOM (decode using utf-8-sig)"）→ 被下面的
    `except Exception` 吞成 {}；而 `_guard_overwrite` 用 utf-8-sig 讀得動 →
    放行覆寫、也不留 `.corrupt-` 備份。週色/年度假日表/門診模板/Clerk 梯次/
    切片格網都沒有 `_snapshot`，一次存檔就永久消失。
    這個教訓 `cmuh_common/atomic_io.py` 的 [IF-02] 已經學過（「容忍記事本另存
    UTF-8 時加的 BOM；對無 BOM 的純 utf-8 行為完全一致，向後相容、無副作用」），
    當時修了 atomic_io 與 `_guard_overwrite`，卻漏掉每一次讀取都會經過的這裡。
    BOM 的來源：多機 git 衝突是設計內流程（使用者會手動修 JSON），而 PowerShell
    的 `>` / `Out-File` 預設就寫出 UTF-8 with BOM。
    """
    return _parse_json_bytes(_read_bytes(path), path)


def prev_ym(ym: str) -> str:
    """上一個月的 YYYY-MM(跨年)。★這個推導只有一份★ —— 求解、切片跨月週五、
    公平計數回放、跨月銜接各自寫一次的話,遲早有一處在 1 月時算成同一年。"""
    y, m = int(ym[:4]), int(ym[5:7])
    py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
    return f"{py:04d}-{pm:02d}"


def last_weekend_of(month_data: dict, scope: str, ym: str) -> Optional[tuple]:
    """這個月「最後一個週末」是誰值的 -> (saturday_date, member_id) 或 None。

    ★由 canonical duty 推導,不看 `month["last_weekend"]` 那份快取★
    (外審 RS-20 P1-01):那份快取是 Auto Accept 當下寫的,而之後的手動換班
    【不會】更新它 —— 它卻是下個月跨月連休鏈的【硬約束】來源:
      10/31(週六)自動排給 A → 套用 → 使用者手動改成 B
      → 11 月自動排班仍把跨月的 11/1(週日)固定給 A
    這不是 soft fairness、也不是報告問題,而是 solver 的硬輸入錯了,而且完全
    正常的操作就重現得出來。衍生資料只有兩種安全的活法:使用前重新推導,
    或帶新鮮度識別 —— 這裡選前者(推導很便宜,而且不可能忘記更新)。

    ★判準用「這個月的最後一個週六」,不是「有人的最後一個週六」★:
    solver 取的是最後一個【週末區塊】的週六(`weekend_blocks[-1].saturday`)。
    改成「往前找到有人的那一個」會把跨月連休鏈接到兩週前的那個週末,
    在下個月固定給一個根本不相鄰的人。沒有人 -> None(＝沒有銜接資料),
    `apply_boundary_from_prev` 與色塊連週規則本來就對 None 有正確處理。

    ★解讀規則只有一份★:寬鬆讀取(`RosterStorage.prev_month_last_weekend`)與
    嚴格快照(`StrictSources`)都走這裡。
    """
    y, m = int(ym[:4]), int(ym[5:7])
    sats = [d for d in month_dates(y, m) if d.weekday() == 5]
    if not sats:
        return None
    last = sats[-1]
    cell = ((month_data.get(f"{scope}_duty") or {}).get(last.isoformat())) or {}
    person = str(cell.get("person") or "")
    return (last, person) if person else None


#: 合法的週色(`calendar_colors` 的 PINK/GREEN;UI 也只產生這兩種)。
_WEEK_COLORS = ("pink", "green")


def _ym_or_none(v):
    """`YYYY-MM` → (年, 月);不是就回 None。"""
    try:
        y, m = str(v).split("-")
        if len(y) == 4 and len(m) == 2 and 1 <= int(m) <= 12:
            return int(y), int(m)
    except (ValueError, AttributeError):
        pass
    return None


#: 意圖的種類(模組層級,讓嚴格讀取也用得到同一份定義)。
PENDING_KINDS = ("ledger", "biopsy", "all")


def _validate_pending_record(rec: dict, name: str, i: int) -> None:
    """一筆意圖記錄自己也要看得懂(外審 RS-23 P2-02)。

    ★「認不得的義務」不可以等於「沒有義務」★:`{}` 或 scope/ym 是空字串的
    記錄以前照樣通過「是 dict」這一關,而收斂端看到缺欄位就【把它清掉】——
    那是這整套 fail-closed 設計裡最不該有的一個出口。壞掉的記錄要留在檔案裡
    並且明講,由人決定。
    """
    def bad(why: str):
        raise ValueError(
            f"{name} 第 {i + 1} 筆看不懂({why})—— 認不得的義務不可以被當成"
            f"不存在。請人工確認該檔之後再繼續。")

    if name == "pending_rename.json":
        for fld in ("scope", "old_id", "new_id"):
            if not str(rec.get(fld) or "").strip():
                bad(f"沒有 {fld}")
        if rec.get("scope") not in ("r", "vs"):
            bad(f"scope={rec.get('scope')!r}")
        if rec.get("old_id") == rec.get("new_id"):
            bad("old_id 與 new_id 相同")
    elif name == "pending_settle.json":
        if rec.get("scope") not in ("r", "vs"):
            bad(f"scope={rec.get('scope')!r}")
        if _ym_or_none(rec.get("ym")) is None:
            bad(f"ym={rec.get('ym')!r}")
        # 舊版沒有 kind → 一律當成 "all"(見 `pending_kind`),合法。
        if "kind" in rec and rec.get("kind") not in PENDING_KINDS:
            bad(f"kind={rec.get('kind')!r}")
    else:
        if not str(rec.get("batch_id") or "").strip():
            bad("沒有 batch_id")
        for fld in ("old_start", "new_start"):
            if _iso_or_none(rec.get(fld)) is None:
                bad(f"{fld}={rec.get(fld)!r}")
        if "pre_digest" in rec and not isinstance(rec["pre_digest"], str):
            bad(f"pre_digest 型別是 {type(rec['pre_digest']).__name__}")


def _strict_pending(data: dict, name: str, what: str) -> list:
    """意圖檔的嚴格讀取(兩份共用一套規則)。

    ★「沒有這個鍵」與「這個鍵是 null / 壞掉的元素」是兩件事★
    (外審 RS-22 R1-2):前者是「這份檔還沒有任何待辦」,後者是【讀不懂】——
    而兩者都被正規化成空清單的話,收斂不會跑、閘門看不到,接下來的一次寫入
    還會把那些認不得的義務整份覆寫掉。這正是這兩個嚴格讀取要消滅的行為,
    只是換到更裡面一層。
    """
    if "pending" not in data:
        return []                              # 還沒有任何待辦(合法)
    raw = data["pending"]
    if not isinstance(raw, list):
        raise ValueError(
            f"{name} 的 pending 不是清單（{type(raw).__name__}）"
            f"—— 無法確認還有沒有未完成的{what}")
    for i, x in enumerate(raw):
        if not isinstance(x, dict):
            raise ValueError(
                f"{name} 第 {i + 1} 筆不是物件（{type(x).__name__}）"
                f"—— 認不得的義務不可以被當成不存在")
        _validate_pending_record(x, name, i)
    return list(raw)


def _iso_or_none(k):
    try:
        return date.fromisoformat(str(k))
    except (ValueError, TypeError):
        return None


def validate_authoritative_shape(name: str, raw: dict) -> None:
    """權威計算之前的【內容】檢查 —— 壞在裡面的東西不可以被靜靜濾掉。

    (外審 RS-20 P2-01)`_strict_snapshot` 擋的是「讀不到 / 0 位元組 / 不是
    JSON / 頂層不是物件」;但 typed loader 本身是【給顯示用】的寬鬆設計:
      * `load_holiday_duty` 對壞掉的日期鍵只記 warning 然後跳過
        → 整年國定假日可以只剩幾天,而 solver 完全看不出來;
      * `load_clerk_batches` 直接濾掉非 dict 的項目 → 一整梯 Clerk 消失;
      * `load_config` 不看成員項目的形狀 → 少一個人也算「成功載入」。
    正常 UI 不會產生這些形狀,要人工合併/外部編輯/舊檔損壞才會 —— 但那正是
    這一批要處理的那種情境:★它是合法 JSON,所以每一道既有守衛都會放行★。

    顯示路徑不走這裡(讀不到就顯示空,不該讓視窗打不開);
    ★會寫回去的路徑一律先過這一關★(見 `StrictSources`)。
    """
    def bad(why: str):
        raise ValueError(f"{name} 的內容不適合用來排班/結算：{why}。"
                         f"請修正該檔之後再試（顯示不受影響）。")

    if name == "holiday_duty.json":
        for scope in ("r", "vs"):
            # ★先看原值的型別,不要先用 `or` 正規化★(外審 RS-20 R1-5):
            #   `{"r": []}` 這種【錯型別但 falsey】的值會被 `or {}` 變成合法的
            #   空表 —— 整年國定假日就這樣消失,而每一道守衛都放行。
            if raw.get(scope) is not None and not isinstance(raw[scope], dict):
                bad(f"{scope} 不是物件（{type(raw[scope]).__name__}）")
            for k in (raw.get(scope) or {}):
                if _iso_or_none(k) is None:
                    bad(f"{scope} 有不是日期的鍵 {k!r}（整批國定假日會少掉它）")
    elif name == "clerk_batches.json":
        _seen_ids: set = set()
        items = raw.get("batches")
        if items is not None and not isinstance(items, list):
            bad("batches 不是清單")
        for i, b in enumerate(items or []):
            if not isinstance(b, dict):
                bad(f"第 {i + 1} 筆梯次不是物件（{type(b).__name__}）")
            # ★不可以要求 id★(外審 RS-21 P1-02):`ClerkBatch.from_dict`
            #   與 `clerk_batch_key()` 都明文支援「舊資料沒有 id → 退回
            #   start_monday」——把它判成非法,升級之後 PGY/Clerk 自動排班與
            #   正式匯出會對一份【舊版程式自己寫出來的合法檔】整批失敗。
            #   (開程式時的 `migrate_legacy_clerk_batch_ids()` 會補上穩定 id,
            #   但驗證不可以依賴那次遷移已經跑過 —— 它是另一台機器的事。)
            #   要擋的是【同一個 id 指到兩梯】:那會讓切片格網互相覆蓋。
            _bid = str(b.get("id") or "").strip()
            if _bid and _bid in _seen_ids:
                bad(f"梯次 id {_bid!r} 重複(切片格網會互相覆蓋)")
            if _bid:
                _seen_ids.add(_bid)
            if _iso_or_none(b.get("start_monday")) is None:
                bad(f"梯次 {b.get('id')} 的 start_monday 不是日期"
                    f"（{b.get('start_monday')!r}）")
            if b.get("members") is not None and not isinstance(
                    b.get("members"), list):                     # 見上一段
                bad(f"梯次 {b.get('id')} 的 members 不是清單"
                    f"（{type(b.get('members')).__name__}）")
    elif name == "config.json":
        for key in ("r_members", "vs_members", "pgy_members"):
            lst = raw.get(key)
            if lst is not None and not isinstance(lst, list):
                bad(f"{key} 不是清單")
            seen_ids: set = set()
            for i, mm in enumerate(lst or []):
                if not isinstance(mm, dict):
                    bad(f"{key} 第 {i + 1} 位不是物件（{type(mm).__name__}）")
                if not str(mm.get("id") or "").strip():
                    bad(f"{key} 第 {i + 1} 位沒有代號(id)")
                # ★同一份名單裡代號必須唯一★(外審 RS-23 P2-03):CP-SAT 是用
                #   `{(日期, m.id): BoolVar}` 建變數的 —— 重複的代號只會產生
                #   【一顆】變數,而 `AddExactlyOne` 又把它枚舉兩次,約束就變成
                #   `2*A + B == 1`:A 從此不可能值班,只有重複那一位時甚至無解。
                #   ★不可以靜默去重★:兩筆同代號可能有不同 level/固定星期/姓名,
                #   程式沒有資格猜哪一筆才是真的。
                mid = str(mm.get("id") or "").strip()
                if mid in seen_ids:
                    bad(f"{key} 有重複的代號 {mid!r}"
                        f"（會讓求解模型的語意改變,請先確認要保留哪一筆）")
                seen_ids.add(mid)
    elif name == "clinic_template.json":
        tpl = raw.get("template")
        if tpl is not None and not isinstance(tpl, dict):
            bad("template 不是物件")
        for wd, sess in (tpl or {}).items():
            if not isinstance(sess, dict):
                bad(f"週{wd} 的內容不是物件")
            for session, entries in sess.items():
                if not isinstance(entries, list):
                    bad(f"週{wd} {session} 的門診清單不是陣列")
                for e in entries:
                    if not isinstance(e, dict):
                        bad(f"週{wd} {session} 有一筆門診不是物件")
    elif name == "week_colors.json":
        weeks = raw.get("weeks")
        if weeks is not None and not isinstance(weeks, dict):
            bad(f"weeks 不是物件（{type(weeks).__name__}）—— 色塊連週是 CP-SAT "
                f"的硬限制,整組週色靜靜消失會排出違反規則的班")
        for k, v in (weeks or {}).items():
            if not isinstance(v, str):
                bad(f"{k} 的顏色不是字串（{type(v).__name__}）")
            # ★色塊連週是比對兩週的字串相不相等★:拼錯的 "gren" 會被當成
            #   「不同色」,於是本該被禁止的連值兩個週末就這樣放行了。
            elif v not in _WEEK_COLORS:
                bad(f"{k} 的顏色 {v!r} 不是 {'／'.join(sorted(_WEEK_COLORS))}")
    elif name == "ledger.json":
        for scope in ("r", "vs"):
            book = raw.get(scope)
            if book is not None and not isinstance(book, dict):
                bad(f"{scope} 不是物件（{type(book).__name__}）")
            for mid, val in (book or {}).items():
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    bad(f"{scope}/{mid} 的餘額不是數字（{val!r}）")
        hist = raw.get("history")
        if hist is not None and not isinstance(hist, list):
            bad(f"history 不是清單（{type(hist).__name__}）")
        seen_months: set = set()
        for i, e in enumerate(hist or []):
            if not isinstance(e, dict):
                bad(f"history 第 {i + 1} 筆不是物件")
            # ★同一個 (scope, 月份) 只能有一筆結算★(外審 RS-23 P2-03):
            #   `settle_month` 是「先回滾同月舊分錄再重記」,兩筆的話回滾只
            #   拿掉一筆,另一筆的差額就永遠留在餘額裡(而餘額是下個月公平
            #   目標的基準)。人工合併很容易造出這種形狀。
            key = (str(e.get("scope") or ""), str(e.get("month") or ""))
            if key in seen_months:
                bad(f"{key[0]}/{key[1]} 有兩筆結算分錄"
                    f"（回滾只會拿掉一筆,另一筆的差額會永遠留在餘額裡）")
            seen_months.add(key)
            deltas = e.get("deltas")
            if deltas is not None and not isinstance(deltas, dict):
                bad(f"history 第 {i + 1} 筆的 deltas 不是物件")
            for mid, val in (deltas or {}).items():
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    bad(f"history 第 {i + 1} 筆 {mid} 的分錄不是數字（{val!r}）")
    elif name == "biopsy.json":
        counts = raw.get("counts")
        if counts is not None and not isinstance(counts, dict):
            bad(f"counts 不是物件（{type(counts).__name__}）")
        for mid, val in (counts or {}).items():
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                bad(f"{mid} 的切片次數不是數字（{val!r}）")
        hist = raw.get("history")
        if hist is not None and not isinstance(hist, list):
            bad(f"history 不是清單（{type(hist).__name__}）")
    elif name == "biopsy_grid.json":
        grid = raw.get("grid")
        if grid is not None and not isinstance(grid, dict):
            bad("grid 不是物件")
        for bid, days in (grid or {}).items():
            if not isinstance(days, dict):
                bad(f"梯次 {bid} 的格網不是物件")
            for k, sess in days.items():
                if _iso_or_none(k) is None:
                    bad(f"梯次 {bid} 有不是日期的鍵 {k!r}")
                if sess is not None and not isinstance(sess, dict):
                    bad(f"梯次 {bid} 的 {k} 不是物件"
                        f"（{type(sess).__name__}）")
                for session, on in (sess or {}).items():
                    # ★時段值是 bool★:`[]` 之類的 falsey 錯型別會被靜靜當成
                    #   「切片室沒開」—— 整梯的切片班就這樣不見了。
                    if not isinstance(on, bool):
                        bad(f"梯次 {bid} 的 {k}/{session} 不是布林值"
                            f"（{type(on).__name__}）")


def validate_authoritative_month(ym: str, raw: dict) -> None:
    """月檔的【內容】檢查 —— 會被靜靜濾掉的那些形狀(外審 RS-21 P2-03)。

    月檔的日期鍵幾乎全部走「壞的就 warning + 跳過」:
      * `leaves` 少一天 → ★請假的人被排上班★;
      * `must_duty` 少一天 → 指定沒生效;
      * `grid_overrides` 少一天 → 已經停診的診間又被排人;
      * `{scope}_duty` 少一格 → 點數/結算/跨月銜接全部跟著錯。
    這些都是合法 JSON,所以嚴格快照(讀得到、解析得動)照樣放行。
    顯示路徑不走這裡(讀得到多少就顯示多少);★會寫回去/會拿來算的路徑一律
    先過這一關★(見 `StrictSources`)。
    """
    def bad(why: str):
        raise ValueError(f"{ym}.json 的內容不適合用來排班/結算：{why}。"
                         f"請修正該月檔之後再試（顯示不受影響）。")

    for scope in ("r", "vs"):
        duty = raw.get(f"{scope}_duty")
        if duty is not None and not isinstance(duty, dict):
            bad(f"{scope}_duty 不是物件（{type(duty).__name__}）")
        for k, cell in (duty or {}).items():
            if _iso_or_none(k) is None:
                bad(f"{scope}_duty 有不是日期的鍵 {k!r}")
            if cell is not None and not isinstance(cell, dict):
                bad(f"{scope}_duty {k} 不是物件（{type(cell).__name__}）")
            # ★鎖定是用 truthiness 判的★(外審 RS-22 P2-02):`"locked": []`
            #   會被當成「沒鎖」,那一格於是可以被自動排班覆蓋掉 —— 而鎖定的
            #   意思正是「不要動它」。person/source 同理(空字串≠沒排人)。
            for fld, typ in (("person", str), ("locked", bool),
                             ("source", str)):
                if isinstance(cell, dict) and fld in cell \
                        and not isinstance(cell[fld], typ):
                    bad(f"{scope}_duty {k} 的 {fld} 型別不對"
                        f"（{type(cell[fld]).__name__}）")
    if "finalized" in raw and not isinstance(raw["finalized"], bool):
        # 定案＝唯讀。錯型別的 falsey 值會讓那份月檔又可以被整份覆寫。
        bad(f"finalized 不是布林值（{type(raw['finalized']).__name__}）")
    for mapkey in ("leaves", "must_duty"):
        block = raw.get(mapkey)
        if block is not None and not isinstance(block, dict):
            bad(f"{mapkey} 不是物件（{type(block).__name__}）")
        for scope, per_member in (block or {}).items():
            if per_member is not None and not isinstance(per_member, dict):
                bad(f"{mapkey}/{scope} 不是物件")
            for mid, days in (per_member or {}).items():
                if days is not None and not isinstance(days, list):
                    bad(f"{mapkey}/{scope}/{mid} 不是清單")
                for k in (days or []):
                    if _iso_or_none(k) is None:
                        bad(f"{mapkey}/{scope}/{mid} 有不是日期的項目 {k!r}")
    # ★巢狀的形狀也要驗★(外審 RS-21 R1-4):日期鍵對、值卻是 `[]` 的話,
    #   下游一律 `or {}` —— 那一天的日排班/切片人選就這樣從正式文件裡消失,
    #   而它是合法 JSON、日期也沒問題,每一道守衛都放行。
    for key in ("saturday_biopsy", "biopsy_override", "grid_overrides",
                "day_slots", "day_locks"):
        block = raw.get(key)
        if block is not None and not isinstance(block, dict):
            bad(f"{key} 不是物件（{type(block).__name__}）")
        for k, v in (block or {}).items():
            if _iso_or_none(k) is None:
                bad(f"{key} 有不是日期的鍵 {k!r}")
            if v is None:
                continue
            if key == "biopsy_override":
                if not isinstance(v, str):
                    bad(f"{key} {k} 不是代號字串（{type(v).__name__}）")
                continue
            if not isinstance(v, dict):
                bad(f"{key} {k} 不是物件（{type(v).__name__}）")
            if key == "saturday_biopsy":
                # ★葉節點也要驗★(外審 RS-21 R2-2):`"person": []` 是 falsey,
                #   下游 `cell.get("person")` 取到之後直接當「沒排人」跳過 ——
                #   正式留底文件就少了那一格。
                for fld in ("person", "reason"):
                    if fld in v and not isinstance(v[fld], str):
                        bad(f"{key} {k} 的 {fld} 不是字串"
                            f"（{type(v[fld]).__name__}）")
                continue
            for session, inner in v.items():
                if inner is None:
                    continue
                if key == "day_locks":
                    # 內層是 bool。★錯型別的 falsey 值會被靜靜當成「沒鎖」★,
                    #   而鎖定的意思正是「不要動它」—— 那些格子會被自動排班
                    #   覆蓋掉(見 `_overlay_locked_sessions`)。
                    if not isinstance(inner, bool):
                        bad(f"{key} {k}/{session} 不是布林值"
                            f"（{type(inner).__name__}）")
                    continue
                if not isinstance(inner, dict):
                    bad(f"{key} {k}/{session} 不是物件"
                        f"（{type(inner).__name__}）")
                for slot, people in inner.items():
                    if people is None:
                        continue
                    if key == "grid_overrides":
                        if not isinstance(people, list):
                            bad(f"{key} {k}/{session}/{slot} 不是清單"
                                f"（{type(people).__name__}）")
                        continue
                    if not isinstance(people, list):
                        bad(f"{key} {k}/{session}/{slot} 不是清單"
                            f"（{type(people).__name__}）")


class RosterStorage:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.months_dir = os.path.join(base_dir, "months")
        os.makedirs(self.months_dir, exist_ok=True)
        # ★CAS 不是原子的話就不是 CAS★:兩條執行緒可以同時讀到同一個
        #   revision、同時通過比對,然後後寫的那個把先寫的整份蓋掉 ——
        #   正是這一批要消滅的失效模式,只是換成同一個 process 內。
        #   (跨機由 revision 本身擋;同機雙開由單例互斥擋。)
        self._write_lock = threading.RLock()

    #: 走 CAS 的正典檔(檔名 → 給人看的名稱)。★新增正典檔要來這裡加一行★
    #:  —— 漏加就沒有跨機保護(有守衛測試會抓)。
    CANONICAL_FILES = {
        "config.json": "成員名單/參數",
        "ledger.json": "點數帳本",
        "biopsy.json": "切片計數帳本",
        "week_colors.json": "週色",
        "holiday_duty.json": "年度假日指定",
        "clinic_template.json": "門診週模板",
        "clerk_batches.json": "Clerk 梯次",
        "biopsy_grid.json": "切片室開放格網",
    }

    def canonical_revision(self, name: str) -> str:
        """這個正典檔【現在】盤上的版本識別(給 CAS 用)。

        ★月檔以外的正典檔一樣會被整份覆寫★(外審排班第 2 輪 P1-01):
        設定頁的每一次編輯都是「讀整份 config → 改 → 寫整份回去」,而背景
        pull 會在中間把它換成他機的新版本 —— 於是他機剛新增的成員被靜默
        移除,接著 `_sync_ledger` 還會把那個人的點數/歷史當成「已離職」作廢。
        Git 看到的同樣是「pull 之後產生的合法新變更」,擋不住。
        """
        if name not in self.CANONICAL_FILES:
            raise KeyError(f"{name} 不是正典檔(要走 CAS 請先登記到 "
                           f"CANONICAL_FILES)")
        return _file_revision(self._path(name))

    def _parsed_or_read(self, name: str, parsed: "dict | None") -> dict:
        """載入器的內容來源:已解析好的就用它,否則自己讀檔(既有行為)。

        ★正規化只能有一份★:`canonical_snapshot` 若自己再寫一套預設值/排序,
        兩邊遲早會漂移 —— 而漂移的那一刻,窄改動就是在一個與程式其他地方
        不一樣的基底上做的,誰也看不出來。
        """
        return _load_json(self._path(name)) if parsed is None else parsed

    def _canonical_loader(self, name: str):
        """正典檔【窄改動時】用的形狀 —— 與 CANONICAL_FILES 一一對應。

        週色故意用 raw:攤平版會遺失年份/來源,改一格再寫回就把它們弄丟。
        """
        loaders = {
            "config.json": self.load_config,
            "ledger.json": self.load_ledger,
            "biopsy.json": self.load_biopsy,
            "week_colors.json": self.load_week_colors_raw,
            "holiday_duty.json": self.load_holiday_duty,
            "clinic_template.json": self.load_clinic_template,
            "clerk_batches.json": self.load_clerk_batches,
            "biopsy_grid.json": self.load_biopsy_grid,
        }
        if set(loaders) != set(self.CANONICAL_FILES):
            raise AssertionError(
                "正典檔登記表與載入表不一致（新增正典檔要兩邊都補）")
        return loaders[name]

    def canonical_snapshot(self, name: str, *, validate: bool = False):
        """→ (窄改動用的形狀, revision)。★位元組只讀一次★

        (外審排班 RS-5 第 2 輪 P2)「先嚴格檢查、再算 revision、再 load()」是
        三次各自獨立的讀取。通過檢查之後、算 revision 之前背景同步換入損壞
        JSON 的話,revision 與寬鬆 `load_*` 都取自【那份壞的】內容 —— CAS 兩邊
        對得上而放行,一次編輯就把整份設定改成只剩這一筆。每輪重做檢查、
        讀完再比一次 revision,都封不住這個窗口:它們各自又是一次新的讀取。
        要的是【讀一次位元組,嚴格解析它、也用它算 revision】,
        與 `load_month_with_revision` 同一套道理。
        壞檔/暫時讀不到 → 拋 ValueError,中止這次編輯,磁碟原封不動。
        """
        if name not in self.CANONICAL_FILES:
            raise KeyError(f"{name} 不是正典檔(要走 CAS 請先登記到 "
                           f"CANONICAL_FILES)")
        data, rev = self._strict_snapshot(self._path(name))
        if validate:
            # ★會寫回去的路徑要連【內容】都檢查★(外審 RS-20 P2-01):
            #   typed loader 是給顯示用的寬鬆設計,壞掉的日期鍵/梯次項目會被
            #   靜靜濾掉 —— 合法 JSON,所以既有守衛全部放行。
            validate_authoritative_shape(name, data)
        return self._canonical_loader(name)(_parsed=data), rev

    def _strict_snapshot(self, path: str) -> "tuple[dict, str]":
        """★這條規則只有這一份實作★:讀一次位元組 → 用它算 revision、也嚴格
        解析它。壞檔/暫時讀不到直接拋(不是靜默回空);缺檔 → ({}, "")。

        月檔與正典檔共用 —— 兩邊各寫一次的話,遲早只有一邊被修好。
        """
        raw = _read_bytes(path)
        if raw is None:                       # 被鎖住 != 不存在(見 _read_bytes)
            raise ValueError(
                f"{os.path.basename(path)} 暫時無法讀取（被鎖住？），"
                f"為避免用空白覆蓋已中止這次編輯")
        rev = _revision_of(raw)
        if not raw:
            # ★「存在但是空的」不是「還沒有這一份」★(外審排班 RS-6 第 1 輪 P2):
            #   零位元組的月檔/設定檔是壞掉的(寫入被中斷、同步軟體的中間態),
            #   而它的 revision 剛好也是 "" —— 當成「首次建檔」的話 CAS 兩邊
            #   都對得上,窄改動就這樣寫成一份【只有這次改動】的檔案,而且
            #   使用者永遠不會知道那個月/那份名單原本有東西。
            if os.path.exists(path):
                raise ValueError(
                    f"{os.path.basename(path)} 是空檔（0 位元組，通常是寫入或"
                    f"同步被中斷）—— 為避免用空白覆蓋已中止這次編輯，"
                    f"請先確認該檔內容。")
            return {}, rev                    # 還沒有這一份(首次建檔也能 CAS)
        try:
            data = json.loads(raw.decode("utf-8-sig"))
        except ValueError as e:               # 含 UnicodeDecodeError
            raise ValueError(
                f"{os.path.basename(path)} 損壞或無法讀取（{e}）") from e
        if not isinstance(data, dict):        # [] 也能 parse,但寬鬆載入回 {}
            raise ValueError(
                f"{os.path.basename(path)} 頂層不是 JSON 物件"
                f"（{type(data).__name__}）")
        return data, rev

    def strict_sources(self, names=(), months=()) -> "StrictSources":
        """權威計算的輸入,★一次嚴格讀完★ -> `StrictSources`(見該類說明)。

        ★呼叫端必須持有 `write_barrier`★:這裡讀的是好幾個檔,臨界區外的話
        它們彼此就不是同一個時間點的內容(而「整批一致」正是這個包裝的用意)。
        任何一個壞掉/暫時讀不到就在這裡拋 —— 權威計算寧可整批不做,也不要拿
        一份被靜默正規化成合法空值的輸入去算(見 `StrictSources`)。
        """
        return StrictSources(self, names, months)

    def quiesce_local(self) -> None:
        """關閉前收斂本機狀態。基底層沒有背景同步 → 無事可做(見 GitSync)。"""

    @contextlib.contextmanager
    def write_barrier(self):
        """在這個區塊內,★盤上的資料不會被背景同步換掉★。

        (外審排班 RS-2 第 1 輪 P1)月檔的 CAS 只看得到月檔;而「求解結果還配不
        配得上現況」還要看月檔【之外】的檔案(config 名單、門診模板、Clerk 梯次、
        年度假日、上月檔)。驗證與寫入之間若讓背景 pull 插進來合併那些檔,
        指紋比對用的是合併前的資料、月檔 revision 又沒變 —— 兩道關卡都通過,
        舊解照樣落地(請假的人被排上、剛停診的診間又有人)。
        所以「重建輸入 → 比指紋 → CAS 寫入」要整段在同一個臨界區內。

        基底層沒有背景同步,持寫入鎖即可;GitSync 覆寫它,另外持工作樹鎖,
        並把 git commit 延到★離開臨界區之後★(維持既有鎖序,見該處說明)。
        """
        with self._write_lock:
            yield

    # ── 內部共用 ─────────────────────────────────────────────────────────
    def _path(self, name: str) -> str:
        return os.path.join(self.base_dir, name)

    def _month_path(self, ym: str) -> str:
        return os.path.join(self.months_dir, f"{ym}.json")

    def _check_schema(self, data: dict, path: str, *,
                      for_write: bool = False) -> dict:
        """版本守門。**讀寬鬆、寫 fail-closed**（兩者不可混為一談）。

        ★[2026-08-02 補審] 版本欄位看不懂時，讀與寫要走不同分支★
        外部工具/人工合併可能留下 "v3" 之類的值，`int()` 會拋一個訊息毫無幫助的
        ValueError，而本函式在【每一個】`load_*` 的路徑上 —— UI 端 Tk callback 的
        例外只會進 log，使用者只會看到分頁沒重畫、連設定頁都打不開。
        但反過來把寫入也一併放行就更糟（我第一版就是這樣，外審抓到）：那等於
        拿掉降級保護——較新版本寫的檔會被靜默改寫成本版 schema、丟掉本程式不認得
        的欄位，而 `_guard_overwrite` 只驗 JSON 結構，擋不住這件事。
        故：讀 → 記一筆 warning 後放行；寫 → 中止，要求先處理該檔。
        """
        raw = data.get("schema_version", SCHEMA_VERSION)
        try:
            ver = int(raw or SCHEMA_VERSION)
        except (TypeError, ValueError):
            if for_write:
                raise ValueError(
                    f"{os.path.basename(path)} 的 schema_version 無法解讀"
                    f"（{raw!r}），無法判斷是否為較新版本寫的檔；為避免降級覆寫"
                    f"已中止存檔，請先手動處理該檔。") from None
            logging.warning("[roster.storage] %s 的 schema_version 無法解讀（%r），"
                            "讀取時視為本版（寫入會被擋下）",
                            os.path.basename(path), raw)
            return data
        if ver > SCHEMA_VERSION:
            raise NewerSchemaError(
                f"{os.path.basename(path)} schema v{ver} 比本程式(v{SCHEMA_VERSION})新，"
                f"請先更新程式再開啟")
        return data

    def _guard_overwrite(self, path: str) -> None:
        """[2026-07-25 審查] 寫入前確認不會用「從讀不到的檔推導出來的空內容」覆蓋好資料。

        病灶：`_load_json` 把壞檔/鎖檔一律靜默當成 {}（見其 docstring），於是
        load→編輯→save 這條再普通不過的路徑會把整份資料寫成空白。實測：config.json
        帶 git conflict marker 時，設定頁名單顯示全空，使用者只要改一個參數（Spinbox
        去抖 800ms 自動存檔）就把 R/VS/PGY 名單永久清掉；月檔同理，還會因為讀到
        finalized=False 而靜默解除定案。

        策略與 cmuh_common.atomic_io 一致，依失敗種類分流：
          - 可正常解析 → 照常覆寫。
          - 內容損壞(JSON/編碼壞掉) → 先備份成 .corrupt-<ts> 再允許覆寫；否則使用者
            會被永久卡住(存不了任何東西)。備份都失敗就拒寫。
          - OSError/PermissionError(被防毒/同步軟體暫時鎖住,**原檔通常完好**)
            → 拒寫並拋 ValueError，由 UI 顯示錯誤，稍後重試即可。
        """
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                loaded = json.load(f)
            # [codex] roster 全部檔案的根都必須是 object。語法正確但根是 list/純量
            # （多機合併殘留、外部工具誤寫）同樣會被 _load_json 轉成 {} 顯示為空 →
            # 屬於「結構性壞檔」,比照壞檔備份後才允許覆寫（assert_readable 也是這個
            # 契約:非 dict 根一律視為壞檔）。否則週色/年度假日表這類無快照的檔會直接
            # 無備份消失。
            if isinstance(loaded, dict):
                return
            raise json.JSONDecodeError("root is not a JSON object", "", 0)
        except (json.JSONDecodeError, UnicodeDecodeError):
            stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
            bak = f"{path}.corrupt-{stamp}"
            try:
                shutil.copy2(path, bak)
            except OSError as e:
                raise ValueError(
                    f"{os.path.basename(path)} 內容損壞且無法備份，為避免遺失既有"
                    f"資料已中止存檔。請先手動處理該檔：{path}") from e
            logging.warning("[roster.storage] 壞檔已備份到 %s（允許覆寫）", bak)
            return
        except OSError as e:
            raise ValueError(
                f"{os.path.basename(path)} 暫時無法讀取（可能被防毒/同步軟體鎖住），"
                f"為保護既有資料已中止存檔，請稍後再試。") from e

    def _save(self, path: str, data: dict, *,
              backup: str = BEST_EFFORT_BACKUP,
              expected_revision=_UNSET) -> None:
        """唯一的寫入出口：守門 → 留快照 → 原子寫入。

        ★[2026-08-02 補審] 快照掛在這裡，不是各個 `save_*` 各自呼叫★
        原本是各 `save_*` 自己叫 `_snapshot`，結果只有 config / ledger / biopsy /
        月檔有，週色 / 年度假日表 / 門診模板 / Clerk 梯次 / 切片格網一個都沒有——
        寫壞就沒有回頭路。年度假日表最要緊：它的鍵集合【就是】國定假日清單，
        被清空等於整年的點數與週末連休區塊全部算錯。
        （2026-07-25 的審查註解就寫過「config.json 是唯一沒有快照保護的存檔路徑…
        誤刪最痛」，當時補了 config 卻沒有回頭看還有誰漏——所以這次改成掛在唯一
        出口上：不是「補上五個呼叫」，而是【以後不可能再漏】。）
        順序：守門在前，被拒寫時不留快照——什麼都沒改卻佔掉保留額度，會把真正
        有用的歷史擠掉。`.bak-*` 已在 GitSync 的 .gitignore 內，不會同步出去。
        """
        # ★全程持鎖:比對與寫入之間不可以有縫★(見 `_write_lock` 的說明)
        with self._write_lock:
            self._save_body(path, data, backup=backup,
                            expected_revision=expected_revision)

    def _save_body(self, path: str, data: dict, *,
                   backup: str = BEST_EFFORT_BACKUP,
                   expected_revision=_UNSET) -> None:
        """`_save` 的本體。★呼叫端(只有 `_save`)必須持有 `_write_lock`★"""
        # ★CAS:要覆蓋的必須還是我讀到的那一份★(外審排班第 1 輪 P1-01)
        #   比對放在【寫入之前、同一個臨界區內】—— GitSync 覆寫的 `_save` 會
        #   持有工作樹鎖,所以「比對 → 寫入」中間不會被背景 merge 插進來。
        #   `expected_revision` 沒帶 ＝ 呼叫端沒有「先讀後寫」的語意(例如首次
        #   建檔、整批重建),維持原行為。
        if expected_revision is not _UNSET:
            current = _file_revision(path)
            # ★兩個原因的處置不同,訊息就不可以共用★:「被別人搶先」重讀一次
            #   就好;「這一刻讀不到」是防毒/同步軟體鎖住,重讀也沒用,而且
            #   在讀不到的情況下【無法判斷】會不會蓋掉別人的修改 → fail-closed。
            if current == _UNREADABLE_REV or expected_revision == _UNREADABLE_REV:
                raise ValueError(
                    f"{os.path.basename(path)} 暫時無法讀取（可能被防毒/同步"
                    f"軟體鎖住），無法確認這次存檔會不會蓋掉其他電腦的修改，"
                    f"已中止存檔，請稍後再試。")
            if current != expected_revision:
                raise StaleRosterDataError(
                    f"{os.path.basename(path)} 已被其他電腦(或另一個視窗)更新，"
                    f"為避免蓋掉對方剛同步進來的內容，這次存檔已中止。"
                    f"請重新載入最新資料後再改一次。")
        self._guard_overwrite(path)
        if not self._snapshot(path) and backup == REQUIRE_BACKUP:
            raise ValueError(
                f"{os.path.basename(path)} 的備份（.bak-）建立失敗，"
                f"為避免這份【失去備份就無法復原】的資料被覆蓋，已中止存檔。"
                f"請確認檔案沒有被防毒/備份軟體鎖住、磁碟仍有空間後重試。")
        data = dict(data)
        data["schema_version"] = SCHEMA_VERSION
        # atomic_write_json 回傳 None、失敗時拋例外（cmuh_common.atomic_io 介面）
        atomic_write_json(path, data)

    def _snapshot(self, path: str) -> bool:
        """複製一份 .bak-<時間戳>。回傳「是否有可用的備份」——檔案本來就不存在
        （首次建檔，沒有東西要保護）也算 True。"""
        if not os.path.exists(path):
            return True
        # [codex P2] 含微秒避免同秒內連續存檔互相覆蓋快照;仍碰撞則加序號
        stamp = datetime.now().strftime("%Y%m%d%H%M%S%f")
        bak = f"{path}.bak-{stamp}"
        n = 1
        while os.path.exists(bak):
            bak = f"{path}.bak-{stamp}-{n}"
            n += 1
        try:
            shutil.copy2(path, bak)
        except OSError:
            logging.warning("[roster.storage] 快照失敗: %s", path, exc_info=True)
            return False
        # 清舊快照
        snaps = sorted(glob.glob(f"{path}.bak-*"))
        for old in snaps[:-KEEP_SNAPSHOTS]:
            try:
                os.remove(old)
            except OSError:
                pass
        return True

    # ── config / ledger / 週色 / 年度假日表 ─────────────────────────────
    def load_config(self, *, _parsed: "dict | None" = None) -> dict:
        return self._check_schema(
            self._parsed_or_read("config.json", _parsed), "config.json")

    def save_config(self, cfg: dict, *, expected_revision=_UNSET) -> None:
        # [2026-07-25 審查] config.json 存的是全部成員名單，誤刪最痛（快照見 _save）。
        self._check_schema(_load_json(self._path("config.json")), "config.json",
                           for_write=True)
        # 全體 R/VS 成員名單 —— 失去備份就回不來。
        self._save(self._path("config.json"), cfg, backup=REQUIRE_BACKUP,
                   expected_revision=expected_revision)

    def load_ledger(self, *, _parsed: "dict | None" = None) -> dict:
        d = self._check_schema(
            self._parsed_or_read("ledger.json", _parsed), "ledger.json")
        d.setdefault("r", {})
        d.setdefault("vs", {})
        d.setdefault("history", [])
        return d

    def save_ledger(self, ledger: dict, *, expected_revision=_UNSET) -> None:
        # [codex P2] 寫前檢查既有檔 schema：防舊版程式把新版檔靜默降級毀損
        self._check_schema(_load_json(self._path("ledger.json")), "ledger.json",
                           for_write=True)
        # [RP3-10a] ledger.json 記結算/欠點,遭誤寫時可回溯 —— 快照由 _save 統一留。
        self._save(self._path("ledger.json"), ledger,
                   expected_revision=expected_revision)

    def load_biopsy(self, *, _parsed: "dict | None" = None) -> dict:
        """週六切片計數帳本 {"counts":{mid:int}, "history":[{month, assign}]}。"""
        d = self._check_schema(
            self._parsed_or_read("biopsy.json", _parsed), "biopsy.json")
        d.setdefault("counts", {})
        d.setdefault("history", [])
        return d

    # ── 未完成的結算意圖(跨檔交易的補救紀錄;外審排班 P2-01)──────────────
    def load_pending_settles(self) -> list:
        """還沒確認完成的「月檔+帳本」寫入意圖。→ [{"scope","ym","ts"}]。

        ★為什麼只記意圖、不記內容★:帳本是【可以從月檔重算出來的衍生物】
        (`resettle_from_duty` 就是做這件事)。所以中斷後不需要重放整份 payload,
        只要知道「哪一個 (scope, 月份) 的結算沒有確認完成」,就能用月檔這個
        真相來源把帳本重建到一致。記內容反而會有「重放的內容自己就是舊的」
        這種新問題。
        """
        raw = _load_json(self._path("pending_settle.json")).get("pending")
        return [x for x in (raw or []) if isinstance(x, dict)]

    def load_pending_settles_strict(self) -> list:
        """同上,★但壞檔/暫時讀不到就拋★(外審 RS-21 P2-02)。

        這份檔已經不只是 log:開程式的收斂、求解前的帳本閘門、定案閘門都靠
        它判斷「還有沒有未完成的結算」。寬鬆載入把「讀不到」正規化成「沒有
        任何未完成的事」—— 於是收斂不跑、閘門看不到、定案照過,而那正是
        RS-19 已經修過一次的同一個形狀(只是換一個檔)。
        顯示路徑仍用寬鬆的 `load_pending_settles`。
        """
        data, _rev = self._strict_snapshot(self._path("pending_settle.json"))
        return _strict_pending(data, "pending_settle.json", "結算")

    #: 意圖的種類＝★哪一個衍生物還沒被重建★(外審次輪 P2-02)。
    #:   "ledger" 點數帳本 / "biopsy" 切片計數帳本 / "all" 兩者都要
    #: 舊版紀錄沒有這個欄位 → 一律視為 "all"(兩個都重建成功才算收斂;
    #: 把它當成某一種的話,另一種的義務會被靜默丟掉)。
    PENDING_KINDS = PENDING_KINDS

    @classmethod
    def pending_kind(cls, item: dict) -> str:
        """一筆意圖紀錄的種類(不認得的值一律回 "all",寧可多重建一次)。"""
        k = str((item or {}).get("kind") or "all")
        return k if k in cls.PENDING_KINDS else "all"

    def _pending_for_write(self) -> list:
        """改這份檔之前一定要先【嚴格】讀它(外審 RS-21 R1-3)。

        寬鬆讀取把「壞檔/暫時讀不到」變成空清單 —— 接著這一次寫入就把那份
        壞檔換成「只有我這一筆」的新檔;等這次操作成功、意圖被清掉之後,
        ★所有先前未完成的義務就永久消失了★,而之後的閘門會看到一份健康的
        空檔案並放行。它已經是閘門的權威輸入(見 `load_pending_settles_strict`),
        就不可以再用「讀不到＝沒有」的方式去改它。
        """
        return self.load_pending_settles_strict()

    def mark_pending_settle(self, scope: str, ym: str,
                            kind: str = "all") -> bool:
        """落地之前記下意圖(冪等:同 scope+月份+種類不重複記)。

        → ★這一筆是不是【這一次】記下的★(外審排班 RS-10 第 1 輪 P1):
        冪等表示「已經有了就不再記」,而那筆既有的意圖屬於【另一個還沒完成的
        操作】。呼叫端如果不分青紅皂白把它清掉,就等於替別人宣稱「已經一致
        了」—— 那個未完成的結算從此不會再被收斂。

        ★種類★(外審次輪 P2-02):意圖代表「這個衍生物還沒重建成功」,
        而切片帳本與點數帳本是兩個獨立的義務 —— 手動改格那條路只重建切片,
        把整筆意圖(含帳本)清掉等於替帳本宣稱一致。已存在的 "all" 涵蓋
        任何一種,此時回 False(義務是別人的,我不得清它)。
        """
        kind = kind if kind in self.PENDING_KINDS else "all"
        # ★讀 → 改 → 寫必須是一個交易★(外審 RS-23 P1-01):嚴格讀取只保證
        #   「讀到的那一份是完整的」,擋不住【讀完之後、寫回之前】背景 Git
        #   merge 把他機的義務合併進來 —— 這一次寫入就用手上那份舊清單整份
        #   覆蓋,對方的 recovery obligation 合法地消失(不是衝突、不是壞檔、
        #   CAS 也看不到,從 git 看來只是一個正常的 post-merge commit)。
        #   ★`write_barrier` 可重入★:已經在臨界區裡的呼叫端不受影響。
        with self.write_barrier():
            cur = self._pending_for_write()   # ★改它之前一定要嚴格讀★
            for x in cur:
                if x.get("scope") != scope or x.get("ym") != ym:
                    continue
                k = self.pending_kind(x)
                if k == kind or k == "all":      # 已有一筆涵蓋我的義務
                    return False
            cur.append({"scope": str(scope), "ym": str(ym), "kind": kind,
                        "ts": _now_stamp()})
            self._save(self._path("pending_settle.json"), {"pending": cur})
            return True

    def clear_pending_settle(self, scope: str, ym: str,
                             kind: str = "all") -> None:
        """該種類的衍生物確實重建成功之後,清掉【那一筆】意圖。

        ★只清同種類的那一筆★:清掉 "all" 等於連同別人未完成的義務一起
        宣稱一致(RS-10 的教訓,種類化之後同樣成立)。
        """
        kind = kind if kind in self.PENDING_KINDS else "all"
        with self.write_barrier():        # ★讀改寫是一個交易★(見 mark)
            cur = self._pending_for_write()   # ★改它之前一定要嚴格讀★
            left = [x for x in cur
                    if not (x.get("scope") == scope and x.get("ym") == ym
                            and self.pending_kind(x) == kind)]
            if len(left) == len(cur):
                return
            self._save(self._path("pending_settle.json"), {"pending": left})

    def retype_pending_settle(self, scope: str, ym: str,
                              old_kind: str, new_kind: str) -> bool:
        """把一筆意圖★原子地★改成另一個種類。→ 是否真的改到。

        (外審 RS-16 R1-1)「先 mark(新種類) 再 clear(舊種類)」在這裡是行不通
        的:`mark_pending_settle` 的涵蓋規則會判定「已有一筆 all 涵蓋你了」而
        回 False —— 於是新的那筆根本沒記上,接著 clear 又把 all 拿掉,義務整個
        消失。降級必須是【一次寫入】:accept/重算時帳本確實重建好了、只剩切片
        沒有,紀錄就該原地變成 biopsy。
        """
        old_kind = old_kind if old_kind in self.PENDING_KINDS else "all"
        new_kind = new_kind if new_kind in self.PENDING_KINDS else "all"
        with self.write_barrier():        # ★讀改寫是一個交易★(見 mark)
            cur = self._pending_for_write()   # ★改它之前一定要嚴格讀★
            hit = False
            out = []
            for x in cur:
                if (not hit and x.get("scope") == scope and x.get("ym") == ym
                        and self.pending_kind(x) == old_kind):
                    y = dict(x)
                    y["kind"] = new_kind
                    y["ts"] = _now_stamp()
                    out.append(y)
                    hit = True
                    continue
                out.append(x)
            if not hit:
                return False
            self._save(self._path("pending_settle.json"), {"pending": out})
            return True

    # ── 改名的交易意圖(外審 RS-23 P2-04)──────────────────────────────
    def load_pending_renames(self) -> list:
        """還沒確認完成的改名(顯示用,寬鬆)。"""
        raw = _load_json(self._path("pending_rename.json")).get("pending")
        return [x for x in (raw or []) if isinstance(x, dict)]

    def load_pending_renames_strict(self) -> list:
        """★壞檔/讀不到就拋★(與另外兩份意圖檔同一套規則)。

        改名是跨 config/帳本/假日/切片/所有月檔的多檔交易,而回滾只存在
        記憶體裡 —— 斷電/被砍時盤上會留下【一半舊、一半新】,而下次開程式
        沒有任何線索知道那次改名做到哪裡。這份檔就是那個線索。
        """
        data, _rev = self._strict_snapshot(self._path("pending_rename.json"))
        return _strict_pending(data, "pending_rename.json", "改名")

    def mark_pending_rename(self, scope: str, old_id: str, new_id: str, *,
                            config_rev: str = "",
                            config_digest: str = "") -> bool:
        """改名【落地之前】記下意圖(冪等)。→ 這一筆是不是這次記的。

        `config_rev`＝★交易開始【之前】config.json 的 revision★
        (外審 Codex RS-23 P1-02):config 是第一個被寫的檔,所以
        「它還是這個 revision」就★證明★這次交易一個檔都還沒動 ——
        收斂端可以據此把盤上判定為完整的舊狀態(而不是靠猜)。

        `config_digest`＝★我這次要把 config 寫成什麼樣子★的語意識別
        (外審 Codex RS-23 第 3 輪 P2):證明「盤上這份 config 是我寫的」。
        ★兩個識別都必須在寫第一個檔【之前】就落地★——原本是等 config 寫完
        再讀回它的 revision 補記,那麼「config 已寫、證據還沒落地」之間被斷電
        就永遠證明不了,而那正是這批要涵蓋的任意中斷窗口。
        """
        with self.write_barrier():        # ★讀改寫是一個交易★(見 mark_settle)
            cur = self.load_pending_renames_strict()
            for x in cur:
                if (x.get("scope") == scope and x.get("old_id") == old_id
                        and x.get("new_id") == new_id):
                    return False
            cur.append({"scope": str(scope), "old_id": str(old_id),
                        "new_id": str(new_id), "ts": _now_stamp(),
                        "config_rev": str(config_rev or ""),
                        "config_digest_after": str(config_digest or "")})
            self._save(self._path("pending_rename.json"), {"pending": cur})
            return True

    def clear_pending_rename(self, scope: str, old_id: str,
                             new_id: str) -> None:
        with self.write_barrier():
            cur = self.load_pending_renames_strict()
            left = [x for x in cur
                    if not (x.get("scope") == scope
                            and x.get("old_id") == old_id
                            and x.get("new_id") == new_id)]
            if len(left) == len(cur):
                return
            self._save(self._path("pending_rename.json"), {"pending": left})

    # ── 梯次起始日 → 切片格網平移的意圖(外審次輪 P2-05)──────────────────
    def load_pending_grid_shifts_strict(self) -> list:
        """同 `load_pending_grid_shifts`,★但壞檔/讀不到就拋★
        (外審 RS-22 P2-03;與 `load_pending_settles_strict` 同一個道理)。

        梯次起始日已經落地、切片格網還沒跟著平移時,這份檔是【唯一】的線索;
        寬鬆載入把「讀不到」變成「沒有待辦」,收斂就不會跑,而下一次有人移動
        另一梯時還會把它整份覆寫掉 —— 那筆義務就永遠消失了。
        """
        data, _rev = self._strict_snapshot(
            self._path("pending_grid_shift.json"))
        return _strict_pending(data, "pending_grid_shift.json", "平移")

    def _pending_grid_for_write(self) -> list:
        """改這份檔之前一定要先嚴格讀(見 `_pending_for_write`)。"""
        return self.load_pending_grid_shifts_strict()

    def load_pending_grid_shifts(self) -> list:
        """未確認完成的「梯次移動 → 切片格網平移」。

        `clerk_batches.json` 與 `biopsy_grid.json` 是兩個檔:同一個
        `write_barrier` 擋得住背景同步與其他執行緒,★擋不住行程被砍/停電/
        第二次寫入的 I/O 失敗★ —— 那會留下「梯次已經是新日期、格網還在舊
        日期」,而格網日期落在梯次涵蓋範圍外時 `build_day_input` 直接忽略它
        (切片室整梯看起來沒開),沒有任何紀錄。
        意圖記 (batch_id, 舊起始日, 新起始日) 三者:★平移不是冪等的★,
        收斂時必須先看格網現在對齊哪一邊才能決定要不要搬(見
        `RosterService.reconcile_pending_grid_shifts`)。
        """
        raw = _load_json(self._path("pending_grid_shift.json")).get("pending")
        return [x for x in (raw or []) if isinstance(x, dict)]

    def mark_pending_grid_shift(self, batch_id: str, old_start: str,
                                new_start: str, pre_digest: str = "") -> bool:
        """平移之前記下意圖。→ 是不是【這一次】記下的。

        同一梯次已有未收斂的平移意圖時回 False:呼叫端必須★拒絕★這次變更
        (連續兩次平移疊起來之後,收斂端再也分不出格網停在哪一段)。
        """
        with self.write_barrier():        # ★讀改寫是一個交易★(見
            #   `mark_pending_settle`:背景 merge 會在讀與寫之間插進來)
            return self._mark_grid_shift_locked(batch_id, old_start,
                                                new_start, pre_digest)

    def _mark_grid_shift_locked(self, batch_id, old_start, new_start,
                                pre_digest) -> bool:
        """`mark_pending_grid_shift` 的本體。★呼叫端必須持有 write_barrier★"""
        cur = self._pending_grid_for_write()   # ★改它之前一定要嚴格讀★
        if any(str(x.get("batch_id")) == str(batch_id) for x in cur):
            return False
        cur.append({"batch_id": str(batch_id), "old_start": str(old_start),
                    "new_start": str(new_start),
                    # ★平移前那份格網的身分★(外審 RS-16 R1-2):位移 < 14 天時
                    #   新舊視窗會重疊,只看「日期落在哪個窗」分不出搬過沒有
                    #   (8/3→8/10 的格網若只剩 8/10 這一格,兩個窗都符合)。
                    #   記下鍵集的雜湊,收斂時比對就能明確判定。
                    "pre_digest": str(pre_digest or ""),
                    "ts": _now_stamp()})
        self._save(self._path("pending_grid_shift.json"), {"pending": cur})
        return True

    def clear_pending_grid_shift(self, batch_id: str) -> None:
        """格網確實平移完成(或確認無需平移)之後清掉那一筆意圖。"""
        with self.write_barrier():        # ★讀改寫是一個交易★(見 mark)
            cur = self._pending_grid_for_write()  # ★改之前一定要嚴格讀★
            left = [x for x in cur
                    if str(x.get("batch_id")) != str(batch_id)]
            if len(left) == len(cur):
                return
            self._save(self._path("pending_grid_shift.json"),
                       {"pending": left})

    def save_biopsy(self, book: dict, *, expected_revision=_UNSET) -> None:
        # 比照 save_ledger：寫前 schema 檢查（.bak 快照由 _save 統一留）
        self._check_schema(_load_json(self._path("biopsy.json")), "biopsy.json",
                           for_write=True)
        self._save(self._path("biopsy.json"), book,
                   expected_revision=expected_revision)

    def load_week_colors_raw(self, *, _parsed: "dict | None" = None) -> dict:
        """週色檔的原始結構 {"year","weeks","source"}(給窄改動用;
        `load_week_colors` 只回攤平後的 weeks,改一格再寫回會遺失年份/來源)。"""
        d = self._check_schema(
            self._parsed_or_read("week_colors.json", _parsed),
            "week_colors.json")
        d.setdefault("weeks", {})
        return d

    def load_week_colors(self) -> dict:
        """{"2026-W31": "pink", ...}（攤平所有年度檔內容）。"""
        d = self._check_schema(_load_json(self._path("week_colors.json")),
                               "week_colors.json")
        return dict(d.get("weeks") or {})

    def save_week_colors(self, year: int, weeks: dict, source: str = "manual",
                         replace: bool = False, *,
                         expected_revision=_UNSET) -> None:
        """weeks: {week_key: "pink"/"green"}。

        replace=False（預設）：併入既有（只增/改，無法刪）。
        replace=True：以 weeks 整組取代（UI 手動清除某週色時用，需傳完整集合）。
        """
        cur = _load_json(self._path("week_colors.json"))
        self._check_schema(cur, "week_colors.json", for_write=True)
        merged = dict(weeks) if replace else {**(cur.get("weeks") or {}), **weeks}
        self._save(self._path("week_colors.json"),
                   {"year": year, "weeks": merged, "source": source},
                   expected_revision=expected_revision)

    def load_holiday_duty(self, *, _parsed: "dict | None" = None) -> dict:
        """{"r": {date: member_id}, "vs": {...}}；鍵集合即國定假日清單（§16.1）。"""
        raw = self._check_schema(
            self._parsed_or_read("holiday_duty.json", _parsed),
            "holiday_duty.json")
        out = {"r": {}, "vs": {}}
        for scope in ("r", "vs"):
            for k, v in (raw.get(scope) or {}).items():
                try:
                    out[scope][date.fromisoformat(k)] = str(v)
                except ValueError:
                    logging.warning("[roster.storage] holiday_duty 壞日期略過: %r", k)
        return out

    def save_holiday_duty(self, table: dict, *,
                          expected_revision=_UNSET) -> None:
        self._check_schema(_load_json(self._path("holiday_duty.json")),
                           "holiday_duty.json", for_write=True)       # [codex P2] 防降級毀損
        raw = {"r": {}, "vs": {}}
        for scope in ("r", "vs"):
            for d, mid in (table.get(scope) or {}).items():
                key = d.isoformat() if isinstance(d, date) else str(d)
                raw[scope][key] = str(mid)
        # 這張表的鍵集合【就是】整年的國定假日清單，錯了之後點數與週末連休
        # 區塊全部跟著算錯 —— 失去備份就回不來。
        self._save(self._path("holiday_duty.json"), raw,
                   backup=REQUIRE_BACKUP,
                   expected_revision=expected_revision)

    def holidays_set(self) -> set:
        """國定假日集合 = 年度指定表 r/vs 鍵聯集（設計文件 §16.1 定案）。"""
        t = self.load_holiday_duty()
        return set(t["r"]) | set(t["vs"])

    # ── 門診週模板 / Clerk 梯次 / 切片室開放格網（Phase 3）─────────────────
    def load_clinic_template(self, *, _parsed: "dict | None" = None) -> dict:
        """{"template": {weekday: {session: [{room,doctor,is_self_paid}]}}}。"""
        d = self._check_schema(
            self._parsed_or_read("clinic_template.json", _parsed),
            "clinic_template.json")
        d.setdefault("template", {})
        return d

    def save_clinic_template(self, data: dict, *,
                             expected_revision=_UNSET) -> None:
        self._check_schema(_load_json(self._path("clinic_template.json")),
                           "clinic_template.json", for_write=True)
        self._save(self._path("clinic_template.json"), data,
                   expected_revision=expected_revision)

    def load_clerk_batches(self, *, _parsed: "dict | None" = None) -> list:
        """[{"id","start_monday","members":[...]}]（依起始日升冪）。"""
        d = self._check_schema(
            self._parsed_or_read("clerk_batches.json", _parsed),
            "clerk_batches.json")
        # [codex] 非 dict 項（多機合併後可能出現 null/字串）在這裡就會讓 b.get 拋
        # AttributeError —— 比 from_dict 更早,連讓它回 None 的機會都沒有 → 先濾掉。
        items = [b for b in (d.get("batches") or []) if isinstance(b, dict)]
        return sorted(items, key=lambda b: str(b.get("start_monday", "")))

    def save_clerk_batches(self, batches: list, *,
                           expected_revision=_UNSET) -> None:
        self._check_schema(_load_json(self._path("clerk_batches.json")),
                           "clerk_batches.json", for_write=True)
        self._save(self._path("clerk_batches.json"),
                   {"batches": list(batches)},
                   expected_revision=expected_revision)

    def load_biopsy_grid(self, *, _parsed: "dict | None" = None) -> dict:
        """{batch_id: {iso_date: {"上午":bool,"下午":bool}}}。"""
        d = self._check_schema(
            self._parsed_or_read("biopsy_grid.json", _parsed),
            "biopsy_grid.json")
        return dict(d.get("grid") or {})

    def save_biopsy_grid(self, grid: dict, *,
                         expected_revision=_UNSET) -> None:
        self._check_schema(_load_json(self._path("biopsy_grid.json")),
                           "biopsy_grid.json", for_write=True)
        self._save(self._path("biopsy_grid.json"), {"grid": grid},
                   expected_revision=expected_revision)

    # ── 月份檔 ───────────────────────────────────────────────────────────
    def month_exists(self, ym: str) -> bool:
        """該月月檔是否真的存在。

        [2026-08-02] `load_month` 對不存在的月份會回一份預設 dict(刻意如此),
        所以「有沒有這個月」不能用它判斷 —— 跨月連動需要先確認下個月真的排過,
        否則會憑空產生一份月檔。
        """
        return os.path.exists(self._month_path(ym))

    def month_revision(self, ym: str) -> str:
        """月檔【現在】盤上的版本識別 —— 給「來源到底有沒有被改到」的量測用。

        (外審 2026-08-22 P2)意圖要在來源寫入【之前】記下(否則中途斷電就沒有
        線索),於是來源自己失敗時會留下一筆其實不存在的債。要分辨這兩件事
        只能★量★:進場時記下這一份的識別,失敗時再量一次 —— 內容沒變就代表
        什麼都沒落地。讀不到時回 `_UNREADABLE_REV`(見 `revision_is_readable`),
        ★「量不到」不可以被當成「沒事發生」★。
        """
        return _file_revision(self._month_path(ym))

    @staticmethod
    def revision_is_readable(rev: str) -> bool:
        """這個版本識別是不是真的量到了(而不是「此刻讀不到」)。"""
        return rev != _UNREADABLE_REV

    def load_month(self, ym: str) -> dict:
        return self.load_month_with_revision(ym)[0]

    def load_month_with_revision(self, ym: str) -> "tuple[dict, str]":
        """→ (月檔, revision)。revision 是【這一份內容】的識別,回寫時交給
        `save_month(expected_revision=…)` 做 CAS。

        ★位元組只讀一次★:先算 revision 再解析【同一份位元組】—— 分兩次讀
        的話,兩次之間背景 pull 換掉檔案就會得到一個「配不上手中內容」的
        revision,CAS 於是會放行那份其實已經過期的快照(正是要防的事)。
        檔案不存在 → revision 為 ""(首次建檔也能 CAS)。
        """
        path = self._month_path(ym)
        raw = _read_bytes(path)
        return (self._normalize_month(_parse_json_bytes(raw, path), ym),
                _revision_of(raw))

    def _normalize_month(self, d: dict, ym: str) -> dict:
        """月檔的預設值/schema 檢查(寬鬆與嚴格兩條讀取路徑共用一份)。"""
        d = self._check_schema(d, f"{ym}.json")
        d.setdefault("month", ym)
        d.setdefault("finalized", False)
        for k in ("r_duty", "vs_duty", "leaves", "must_duty",
                  "day_slots", "grid_overrides"):
            d.setdefault(k, {})
        d.setdefault("audit", [])
        return d

    def load_month_snapshot(self, ym: str, *,
                            validate: bool = False) -> "tuple[dict, str]":
        """→ (月檔, revision),★嚴格★:壞檔/暫時讀不到直接拋 ValueError。

        ★要編輯,就必須先證明基底是可信的★(2026-07-25 的教訓,外審排班 RS-6):
        `load_month_with_revision` 對壞檔一律靜默回一份預設空月檔,而
        `update_month` 拿它當基底把窄改動寫回去,整月的值班/請假/報告就這樣
        被清成只剩這一次的改動 —— `_guard_overwrite` 只會替壞檔留一份
        `.corrupt-` 備份然後【放行】覆寫,擋不住這件事。
        顯示路徑仍用寬鬆的 `load_month`(讀不到就顯示空,不該讓 UI 開不起來);
        ★會寫回去的路徑一律用這個★。
        """
        d, rev = self._strict_snapshot(self._month_path(ym))
        if validate:
            validate_authoritative_month(ym, d)
        return self._normalize_month(d, ym), rev

    def save_month(self, ym: str, data: dict, force: bool = False, *,
                   backup: "str | None" = None,
                   expected_revision=_UNSET) -> None:
        """存月檔。backup=None ＝ 用預設政策（force 覆寫已定案月 → REQUIRE_BACKUP，
        一般存檔 → BEST_EFFORT）。

        ★呼叫端只有在【自己剛做過 preflight_required_backup】時才可以覆寫成
        BEST_EFFORT★（外審第 10 輪）：否則預檢做了第一次快照、這裡又要第二次，
        兩次之間檔案被鎖住就仍會留下半套。見 finalize()。
        """
        path = self._month_path(ym)
        existing = _load_json(path)
        self._check_schema(existing, f"{ym}.json", for_write=True)
        if existing.get("finalized") and not force:
            raise FinalizedMonthError(f"{ym} 已定案（唯讀）；解除定案後才能修改")
        data = dict(data)
        data["month"] = ym
        data["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        # force=True ＝ 覆寫【已定案】月份，那是留底快照本身；一般存檔維持
        # BEST_EFFORT（每改一格就存一次，不可因備份不成而停擺）。
        if backup is None:
            backup = REQUIRE_BACKUP if force else BEST_EFFORT_BACKUP
        self._save(path, data, backup=backup,
                   expected_revision=expected_revision)

    def iter_month_yms(self) -> list:
        """回傳 months/ 內所有月份檔的 'YYYY-MM'（升冪）。供跨全部月份的維護作業
        （例如成員代號連動改名）列舉；glob '*.json' 不會掃到 '.bak-<ts>' 快照。"""
        out = []
        for p in glob.glob(os.path.join(self.months_dir, "*.json")):
            stem = os.path.basename(p)[:-5]            # 去掉 '.json'
            if len(stem) == 7 and stem[4] == "-" and stem[:4].isdigit() \
                    and stem[5:].isdigit():
                out.append(stem)
        return sorted(out)

    def preflight_required_backup(self, path: str) -> None:
        """在【還沒動任何資料之前】確認這個檔待會兒真的寫得下去。

        ★[2026-08-02 第二輪外審] 多步驟落地要讓失敗發生在第一步★
        `finalize()` 是先重算帳本(寫 ledger.json)、再 force 覆寫月檔。自從
        force 覆寫改成 REQUIRE_BACKUP,月檔那一步可能拒寫 —— 於是 UI 報「定案失敗」
        並把勾選還原,而帳本【已經被重新結算過了】。
        `accept_solution` 早就用同一招處理過(「先把兩個目標都預檢過,讓失敗發生在
        任何寫入之前」),這裡照做:守門 + 實際留一份快照,不成就在動帳本之前拋。
        代價是定案時會多留一份 .bak-（定案很少見，可接受）。
        """
        self._guard_overwrite(path)
        if not self._snapshot(path):
            raise ValueError(
                f"{os.path.basename(path)} 的備份（.bak-）建立失敗，"
                f"為避免只完成一半就中止，本次操作沒有做任何變更。"
                f"請確認檔案沒有被防毒/備份軟體鎖住、磁碟仍有空間後重試。")

    def assert_readable(self, name: str) -> None:
        """嚴格檢查某檔可正確解析；存在但壞掉/讀不到 → 拋 ValueError。

        name：相對 base_dir 的檔名（如 'ledger.json'）或絕對路徑（月檔用 _month_path 傳入）。
        用途：跨檔維護作業（如 rename_member 連動改代號）寫入【前】的預檢——一般 load_* 用的
        _load_json 會把壞檔/鎖檔靜默當空 dict，交易式改名若照寫會用空白覆蓋壞檔而【靜默清空】
        帳本/月檔。改名前逐檔跑這個，壞檔就中止整個改名、要求先修復。"""
        path = name if os.path.isabs(name) else self._path(name)
        if not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8-sig") as f:   # 與 _load_json 同規則
                data = json.load(f)
        except (OSError, ValueError) as e:
            raise ValueError(
                f"{os.path.basename(path)} 損壞或無法讀取（{e}）") from e
        # [codex P1] 非物件頂層（如 []）雖能 parse，但 _load_json 會把它當空 dict → 仍會被空白覆蓋。
        if not isinstance(data, dict):
            raise ValueError(
                f"{os.path.basename(path)} 頂層不是 JSON 物件（{type(data).__name__}）")

    # ── 跨月銜接輔助 ─────────────────────────────────────────────────────
    def prev_month_last_weekend(self, ym: str, scope: str) -> Optional[tuple]:
        """上月「最後一個週末」是誰值的 → (saturday_date, member_id) 或 None。

        ★由上月的 canonical duty 推導★(見 `last_weekend_of`):月檔裡的
        `last_weekend` 欄位是 Auto Accept 當下的快照,手動換班不會更新它,
        而這個回傳值是下個月跨月連休的硬約束。缺 → None（precheck 會警告）。
        """
        pym = prev_ym(ym)
        return last_weekend_of(_load_json(self._month_path(pym)), scope, pym)


class StrictSources:
    """權威計算(求解/套用/結算/定案/匯出)的輸入 —— ★一次讀完,而且嚴格解析★。

    (外審 2026-08-22 P1-01/P1-02)寬鬆載入器對【暫時讀不到/損壞】的 JSON 回
    預設空值。那對「顯示」是好的 UX(讀不到就顯示空,不該讓 UI 開不起來),
    對【會寫回去的計算】卻是 fail-open,而且錯得很安靜:
      * `holiday_duty.json` 讀不到 -> 假日表變空 -> 整年國定假日與年度指定
        消失 -> solver 當普通平日排,預覽看起來完全正常。
      * `config.json` 讀不到 -> members=[] -> `resettle`/`finalize` 算出一份
        空的點數表,而 `settle_month` 會先回滾本月舊分錄再記上它 ——
        ★正式帳本就這樣被改寫★,畫面還回報成功。
      * `clinic_template.json` / `clerk_batches.json` 讀不到 -> 診間或 Clerk
        整批消失 -> 日排班仍然算得完,匯出的正式班表少人少班。

    ★指紋擋不住這一種★:套用時重建 context 若讀到【同一個】壞狀態,兩次
    指紋都是「同樣錯的空語意」,比對相等就放行 —— 全輸入指紋(RS-7)抓得到
    stale input,抓不到被靜默正規化成合法空值的 invalid input。

    介面刻意與 `RosterStorage` 的載入器同名:builder 只要
    `st = src or self.storage` 就換得過去,不必在每個讀取點分岔 ——
    ★分岔一定會漏,而漏掉的那一個讀取就是整個包裝的破口★。
    ★沒有宣告的來源一律拋★:宣告不足會當場失敗,不會靜默退回寬鬆讀取
    (所以「這條路徑到底吃哪些檔」是被測試釘住的,不是註解裡的宣稱)。
    ★每次存取回深拷貝★:呼叫端會就地改月檔/帳本(`settle_biopsy` 就是這樣),
    共用同一個物件的話,「這一次讀到的輸入」會被上一次的計算改掉。
    """

    def __init__(self, storage: "RosterStorage", names=(), months=()):
        self._shapes: dict = {}
        self._revs: dict = {}
        for n in sorted(set(names)):
            self._shapes[n], self._revs[n] = storage.canonical_snapshot(
                n, validate=True)
        self._months: dict = {}
        for ym in sorted(set(months)):
            data, rev = storage.load_month_snapshot(ym, validate=True)
            # ★「月檔存不存在」要在同一次臨界區裡定案★:`load_month_snapshot`
            #   對不存在的月份回一份預設月檔(刻意如此),所以存在與否要另外記,
            #   否則跨月連動會憑空生出一份月檔(見 `RosterStorage.month_exists`)。
            self._months[ym] = (data, rev, storage.month_exists(ym))

    # ── 正典檔 ───────────────────────────────────────────────────────────
    def _shape(self, name: str):
        if name not in self._shapes:
            raise KeyError(
                f"{name} 不在這條路徑宣告的權威輸入裡。權威計算不得臨時讀檔"
                f"（那會退回寬鬆載入，壞檔就變成合法的空值）——"
                f"請把它加進這條路徑的來源宣告。")
        return copy.deepcopy(self._shapes[name])

    def revision(self, name: str) -> str:
        """這一份內容的版本識別(要 CAS 寫回去的呼叫端用)。"""
        self._shape(name)                    # 未宣告 -> 同一句錯誤訊息
        return self._revs[name]

    def snapshot(self, name: str):
        """-> (形狀, revision)。★版本與內容同源★:兩者都出自建構時的那一次讀取,
        呼叫端不必(也不可以)再讀一次去取 revision。"""
        return self._shape(name), self._revs[name]

    def load_config(self) -> dict:
        return self._shape("config.json")

    def load_ledger(self) -> dict:
        return self._shape("ledger.json")

    def load_biopsy(self) -> dict:
        return self._shape("biopsy.json")

    def load_week_colors_raw(self) -> dict:
        return self._shape("week_colors.json")

    def load_week_colors(self) -> dict:
        return dict(self._shape("week_colors.json").get("weeks") or {})

    def load_holiday_duty(self) -> dict:
        return self._shape("holiday_duty.json")

    def holidays_set(self) -> set:
        t = self.load_holiday_duty()
        return set(t["r"]) | set(t["vs"])

    def load_clinic_template(self) -> dict:
        return self._shape("clinic_template.json")

    def load_clerk_batches(self) -> list:
        return self._shape("clerk_batches.json")

    def load_biopsy_grid(self) -> dict:
        return self._shape("biopsy_grid.json")

    # ── 月檔 ─────────────────────────────────────────────────────────────
    def month_snapshot(self, ym: str):
        """-> (月檔, revision),與 `RosterStorage.load_month_snapshot` 同義。"""
        if ym not in self._months:
            raise KeyError(
                f"{ym} 的月檔不在這條路徑宣告的權威輸入裡（同上，"
                f"請把它加進來源宣告）。")
        data, rev, _exists = self._months[ym]
        return copy.deepcopy(data), rev

    def load_month(self, ym: str) -> dict:
        return self.month_snapshot(ym)[0]

    def month_exists(self, ym: str) -> bool:
        self.month_snapshot(ym)              # 未宣告 -> 同一句錯誤訊息
        return self._months[ym][2]

    def prev_month_last_weekend(self, ym: str, scope: str) -> Optional[tuple]:
        pym = prev_ym(ym)
        return last_weekend_of(self.load_month(pym), scope, pym)
