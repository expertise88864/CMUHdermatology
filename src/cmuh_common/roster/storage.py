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
    """壞檔/缺檔回 {}（呼叫端補預設），絕不拋例外中斷 UI。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
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

    def _check_schema(self, data: dict, path: str) -> dict:
        ver = int(data.get("schema_version", SCHEMA_VERSION) or SCHEMA_VERSION)
        if ver > SCHEMA_VERSION:
            raise NewerSchemaError(
                f"{os.path.basename(path)} schema v{ver} 比本程式(v{SCHEMA_VERSION})新，"
                f"請先更新程式再開啟")
        return data

    def _save(self, path: str, data: dict) -> None:
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
        self._check_schema(_load_json(self._path("config.json")), "config.json")
        self._save(self._path("config.json"), cfg)

    def load_ledger(self) -> dict:
        d = self._check_schema(_load_json(self._path("ledger.json")), "ledger.json")
        d.setdefault("r", {})
        d.setdefault("vs", {})
        d.setdefault("history", [])
        return d

    def save_ledger(self, ledger: dict) -> None:
        # [codex P2] 寫前檢查既有檔 schema：防舊版程式把新版檔靜默降級毀損
        self._check_schema(_load_json(self._path("ledger.json")), "ledger.json")
        self._save(self._path("ledger.json"), ledger)

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
        self._check_schema(cur, "week_colors.json")   # [codex P2] 防降級毀損
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
                           "holiday_duty.json")       # [codex P2] 防降級毀損
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
                           "clinic_template.json")
        self._save(self._path("clinic_template.json"), data)

    def load_clerk_batches(self) -> list:
        """[{"id","start_monday","members":[...]}]（依起始日升冪）。"""
        d = self._check_schema(_load_json(self._path("clerk_batches.json")),
                               "clerk_batches.json")
        items = list(d.get("batches") or [])
        return sorted(items, key=lambda b: str(b.get("start_monday", "")))

    def save_clerk_batches(self, batches: list) -> None:
        self._check_schema(_load_json(self._path("clerk_batches.json")),
                           "clerk_batches.json")
        self._save(self._path("clerk_batches.json"), {"batches": list(batches)})

    def load_biopsy_grid(self) -> dict:
        """{batch_id: {iso_date: {"上午":bool,"下午":bool}}}。"""
        d = self._check_schema(_load_json(self._path("biopsy_grid.json")),
                               "biopsy_grid.json")
        return dict(d.get("grid") or {})

    def save_biopsy_grid(self, grid: dict) -> None:
        self._check_schema(_load_json(self._path("biopsy_grid.json")),
                           "biopsy_grid.json")
        self._save(self._path("biopsy_grid.json"), {"grid": grid})

    # ── 月份檔 ───────────────────────────────────────────────────────────
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
        self._check_schema(existing, f"{ym}.json")
        if existing.get("finalized") and not force:
            raise FinalizedMonthError(f"{ym} 已定案（唯讀）；解除定案後才能修改")
        self._snapshot(path)
        data = dict(data)
        data["month"] = ym
        data["saved_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        self._save(path, data)

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
