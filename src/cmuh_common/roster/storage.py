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
from cmuh_common.roster.model import SCHEMA_VERSION

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
    def load_config(self) -> dict:
        return self._check_schema(_load_json(self._path("config.json")),
                                  "config.json")

    def save_config(self, cfg: dict) -> None:
        # [2026-07-25 審查] config.json 存的是全部成員名單，誤刪最痛（快照見 _save）。
        self._check_schema(_load_json(self._path("config.json")), "config.json",
                           for_write=True)
        # 全體 R/VS 成員名單 —— 失去備份就回不來。
        self._save(self._path("config.json"), cfg, backup=REQUIRE_BACKUP)

    def load_ledger(self) -> dict:
        d = self._check_schema(_load_json(self._path("ledger.json")),
                               "ledger.json")
        d.setdefault("r", {})
        d.setdefault("vs", {})
        d.setdefault("history", [])
        return d

    def save_ledger(self, ledger: dict) -> None:
        # [codex P2] 寫前檢查既有檔 schema：防舊版程式把新版檔靜默降級毀損
        self._check_schema(_load_json(self._path("ledger.json")), "ledger.json",
                           for_write=True)
        # [RP3-10a] ledger.json 記結算/欠點,遭誤寫時可回溯 —— 快照由 _save 統一留。
        self._save(self._path("ledger.json"), ledger)

    def load_biopsy(self) -> dict:
        """週六切片計數帳本 {"counts":{mid:int}, "history":[{month, assign}]}。"""
        d = self._check_schema(_load_json(self._path("biopsy.json")),
                               "biopsy.json")
        d.setdefault("counts", {})
        d.setdefault("history", [])
        return d

    def save_biopsy(self, book: dict) -> None:
        # 比照 save_ledger：寫前 schema 檢查（.bak 快照由 _save 統一留）
        self._check_schema(_load_json(self._path("biopsy.json")), "biopsy.json",
                           for_write=True)
        self._save(self._path("biopsy.json"), book)

    def load_week_colors(self) -> dict:
        """{"2026-W31": "pink", ...}（攤平所有年度檔內容）。"""
        d = self._check_schema(_load_json(self._path("week_colors.json")),
                               "week_colors.json")
        return dict(d.get("weeks") or {})

    def save_week_colors(self, year: int, weeks: dict, source: str = "manual",
                         replace: bool = False) -> None:
        """weeks: {week_key: "pink"/"green"}。

        replace=False（預設）：併入既有（只增/改，無法刪）。
        replace=True：以 weeks 整組取代（UI 手動清除某週色時用，需傳完整集合）。
        """
        cur = _load_json(self._path("week_colors.json"))
        self._check_schema(cur, "week_colors.json", for_write=True)
        merged = dict(weeks) if replace else {**(cur.get("weeks") or {}), **weeks}
        self._save(self._path("week_colors.json"),
                   {"year": year, "weeks": merged, "source": source})

    def load_holiday_duty(self) -> dict:
        """{"r": {date: member_id}, "vs": {...}}；鍵集合即國定假日清單（§16.1）。"""
        raw = self._check_schema(_load_json(self._path("holiday_duty.json")),
                                 "holiday_duty.json")
        out = {"r": {}, "vs": {}}
        for scope in ("r", "vs"):
            for k, v in (raw.get(scope) or {}).items():
                try:
                    out[scope][date.fromisoformat(k)] = str(v)
                except ValueError:
                    logging.warning("[roster.storage] holiday_duty 壞日期略過: %r", k)
        return out

    def save_holiday_duty(self, table: dict) -> None:
        self._check_schema(_load_json(self._path("holiday_duty.json")),
                           "holiday_duty.json", for_write=True)       # [codex P2] 防降級毀損
        raw = {"r": {}, "vs": {}}
        for scope in ("r", "vs"):
            for d, mid in (table.get(scope) or {}).items():
                key = d.isoformat() if isinstance(d, date) else str(d)
                raw[scope][key] = str(mid)
        # 這張表的鍵集合【就是】整年的國定假日清單，錯了之後點數與週末連休
        # 區塊全部跟著算錯 —— 失去備份就回不來。
        self._save(self._path("holiday_duty.json"), raw, backup=REQUIRE_BACKUP)

    def holidays_set(self) -> set:
        """國定假日集合 = 年度指定表 r/vs 鍵聯集（設計文件 §16.1 定案）。"""
        t = self.load_holiday_duty()
        return set(t["r"]) | set(t["vs"])

    # ── 門診週模板 / Clerk 梯次 / 切片室開放格網（Phase 3）─────────────────
    def load_clinic_template(self) -> dict:
        """{"template": {weekday: {session: [{room,doctor,is_self_paid}]}}}。"""
        d = self._check_schema(_load_json(self._path("clinic_template.json")),
                               "clinic_template.json")
        d.setdefault("template", {})
        return d

    def save_clinic_template(self, data: dict) -> None:
        self._check_schema(_load_json(self._path("clinic_template.json")),
                           "clinic_template.json", for_write=True)
        self._save(self._path("clinic_template.json"), data)

    def load_clerk_batches(self) -> list:
        """[{"id","start_monday","members":[...]}]（依起始日升冪）。"""
        d = self._check_schema(_load_json(self._path("clerk_batches.json")),
                               "clerk_batches.json")
        # [codex] 非 dict 項（多機合併後可能出現 null/字串）在這裡就會讓 b.get 拋
        # AttributeError —— 比 from_dict 更早,連讓它回 None 的機會都沒有 → 先濾掉。
        items = [b for b in (d.get("batches") or []) if isinstance(b, dict)]
        return sorted(items, key=lambda b: str(b.get("start_monday", "")))

    def save_clerk_batches(self, batches: list) -> None:
        self._check_schema(_load_json(self._path("clerk_batches.json")),
                           "clerk_batches.json", for_write=True)
        self._save(self._path("clerk_batches.json"), {"batches": list(batches)})

    def load_biopsy_grid(self) -> dict:
        """{batch_id: {iso_date: {"上午":bool,"下午":bool}}}。"""
        d = self._check_schema(_load_json(self._path("biopsy_grid.json")),
                               "biopsy_grid.json")
        return dict(d.get("grid") or {})

    def save_biopsy_grid(self, grid: dict) -> None:
        self._check_schema(_load_json(self._path("biopsy_grid.json")),
                           "biopsy_grid.json", for_write=True)
        self._save(self._path("biopsy_grid.json"), {"grid": grid})

    # ── 月份檔 ───────────────────────────────────────────────────────────
    def month_exists(self, ym: str) -> bool:
        """該月月檔是否真的存在。

        [2026-08-02] `load_month` 對不存在的月份會回一份預設 dict(刻意如此),
        所以「有沒有這個月」不能用它判斷 —— 跨月連動需要先確認下個月真的排過,
        否則會憑空產生一份月檔。
        """
        return os.path.exists(self._month_path(ym))

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
        d = self._check_schema(_parse_json_bytes(raw, path), f"{ym}.json")
        d.setdefault("month", ym)
        d.setdefault("finalized", False)
        for k in ("r_duty", "vs_duty", "leaves", "must_duty",
                  "day_slots", "grid_overrides"):
            d.setdefault(k, {})
        d.setdefault("audit", [])
        return d, _revision_of(raw)

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
        """讀上月檔的「最後週末」摘要 → (saturday_date, member_id) 或 None。

        由 save 端在成功排班後寫入 data["last_weekend"][scope] =
        {"saturday": iso, "person": id}；此處只讀。缺 → None（precheck 會警告）。
        """
        y, m = int(ym[:4]), int(ym[5:7])
        py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
        prev = _load_json(self._month_path(f"{py:04d}-{pm:02d}"))
        info = ((prev.get("last_weekend") or {}).get(scope)) or {}
        try:
            return (date.fromisoformat(info["saturday"]), str(info["person"]))
        except (KeyError, ValueError, TypeError):
            return None
