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

import glob
import json
import logging
import os
import shutil
import time
from datetime import date, datetime
from typing import Optional

from cmuh_common.atomic_io import atomic_write_json
from cmuh_common.roster.model import SCHEMA_VERSION

KEEP_SNAPSHOTS = 20


class FinalizedMonthError(RuntimeError):
    """月份已定案，未 force 不可覆寫。"""


class NewerSchemaError(RuntimeError):
    """檔案 schema_version 比程式新（另一台較新版本寫的）→ 拒絕寫入。"""


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
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        logging.warning("[roster.storage] 讀取失敗(視為空): %s", path, exc_info=True)
        return {}


class RosterStorage:
    def __init__(self, base_dir: str):
        self.base_dir = base_dir
        self.months_dir = os.path.join(base_dir, "months")
        os.makedirs(self.months_dir, exist_ok=True)

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

    def _save(self, path: str, data: dict) -> None:
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
        self._guard_overwrite(path)
        self._snapshot(path)
        data = dict(data)
        data["schema_version"] = SCHEMA_VERSION
        # atomic_write_json 回傳 None、失敗時拋例外（cmuh_common.atomic_io 介面）
        atomic_write_json(path, data)

    def _snapshot(self, path: str) -> None:
        if not os.path.exists(path):
            return
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
            logging.warning("[roster.storage] 快照失敗(續存): %s", path, exc_info=True)
        # 清舊快照
        snaps = sorted(glob.glob(f"{path}.bak-*"))
        for old in snaps[:-KEEP_SNAPSHOTS]:
            try:
                os.remove(old)
            except OSError:
                pass

    # ── config / ledger / 週色 / 年度假日表 ─────────────────────────────
    def load_config(self) -> dict:
        return self._check_schema(_load_json(self._path("config.json")),
                                  "config.json")

    def save_config(self, cfg: dict) -> None:
        # [2026-07-25 審查] config.json 存的是全部成員名單，誤刪最痛（快照見 _save）。
        self._check_schema(_load_json(self._path("config.json")), "config.json",
                           for_write=True)
        self._save(self._path("config.json"), cfg)

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
        self._save(self._path("holiday_duty.json"), raw)

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
        d = self._check_schema(_load_json(self._month_path(ym)), f"{ym}.json")
        d.setdefault("month", ym)
        d.setdefault("finalized", False)
        for k in ("r_duty", "vs_duty", "leaves", "must_duty",
                  "day_slots", "grid_overrides"):
            d.setdefault(k, {})
        d.setdefault("audit", [])
        return d

    def save_month(self, ym: str, data: dict, force: bool = False) -> None:
        path = self._month_path(ym)
        existing = _load_json(path)
        self._check_schema(existing, f"{ym}.json", for_write=True)
        if existing.get("finalized") and not force:
            raise FinalizedMonthError(f"{ym} 已定案（唯讀）；解除定案後才能修改")
        data = dict(data)
        data["month"] = ym
        data["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save(path, data)

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
