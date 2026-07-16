# -*- coding: utf-8 -*-
"""契約金絲雀（Contract Canary）——偵測「院方系統改版導致自動化前提失效」。

【動機】F1–F12 靠硬編碼的選單 command id / 視窗 class / 欄位結構操作 HIS;掛號/打卡
靠元素 id 解析網頁。院方悄悄改版（例：2026-06-29 選單 id 整批 +1）時，舊假設會讓自動化
【寫錯病歷】或【顯示錯資料】，且往往沒有明顯報錯。金絲雀＝每次危險動作前先確認「契約」
仍成立，不成立就依面向裁決：
  - 寫入面（HIS 醫令/劑量）＝ fail-closed：DRIFT 就停止自動寫入 + 疑似改版警告，交醫師手動。
  - 讀取面（打卡/掛號）＝ 標記存疑：DRIFT 就不顯示可能錯的值 + 通知，不靜默顯示錯資料。

【設計】每個【面向 surface】有一份可序列化的「結構指紋 fingerprint」(dict)。本模組只有
純邏輯 + 基線檔 IO，【不依賴 Win32/Selenium】——採樣（取得現況指紋）由各面向的呼叫端做
（注入 dict）。這讓核心裁決邏輯可完整單元測試。基線檔 settings/contract_baseline.json 仿
roster storage：schema_version + 拒絕降版 + 原子寫。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from cmuh_common.atomic_io import atomic_write_json

SCHEMA_VERSION = 1
BASELINE_FILENAME = "contract_baseline.json"

# 裁決狀態
STATUS_OK = "ok"                    # 現況與基線一致
STATUS_DRIFT = "drift"              # 現況與基線不符（疑似改版）
STATUS_UNCALIBRATED = "uncalibrated"  # 尚未建立基線（需校正）
STATUS_UNKNOWN = "unknown"          # 採樣本身失敗（取不到現況）→ 不判定、不擋（避免假警報）


@dataclass
class CanaryVerdict:
    """金絲雀裁決結果。changes: [(key, baseline_value, current_value), ...]。"""
    status: str
    surface: str
    detail: str = ""
    changes: list = field(default_factory=list)

    @property
    def is_drift(self) -> bool:
        return self.status == STATUS_DRIFT

    @property
    def should_block_write(self) -> bool:
        """寫入面是否應 fail-closed（擋自動寫入）。

        【刻意】只有明確 DRIFT 才擋——uncalibrated（沒基線）/unknown（採不到現況）都不擋:
        若因假警報或採樣失敗就停掉整組 F 鍵，比原本的改版風險更糟（醫師整天不能用熱鍵）。
        沒基線時「以硬編碼常數為隱性基線」的比對仍能抓到 DRIFT（見 his_menu 採樣）。"""
        return self.status == STATUS_DRIFT

    def human(self) -> str:
        base = {
            STATUS_OK: "契約一致",
            STATUS_DRIFT: "疑似院方改版（契約不符）",
            STATUS_UNCALIBRATED: "尚未校正基線",
            STATUS_UNKNOWN: "無法採樣現況",
        }.get(self.status, self.status)
        return f"[{self.surface}] {base}" + (f"：{self.detail}" if self.detail else "")


def compare_fingerprint(surface: str, baseline: Optional[dict],
                        current: Optional[dict], *,
                        keys=None, ignore=None) -> CanaryVerdict:
    """純函式比對現況指紋 vs 基線指紋。

    baseline is None → UNCALIBRATED（尚未校正）。
    current is None  → UNKNOWN（採樣失敗，取不到現況）。
    keys 指定要比對的鍵集合（None＝以 baseline 的鍵為準）；ignore 排除鍵。
    任一鍵值不同 → DRIFT（detail 列出差異）；全同 → OK。
    """
    if current is None:
        return CanaryVerdict(STATUS_UNKNOWN, surface, "無法採樣現況（取不到）")
    if baseline is None:
        return CanaryVerdict(STATUS_UNCALIBRATED, surface, "尚未建立基線（需校正）")
    ks = set(keys) if keys is not None else set(baseline.keys())
    if ignore:
        ks -= set(ignore)
    changes = []
    for k in sorted(ks, key=str):
        b = baseline.get(k)
        c = current.get(k)
        if b != c:
            changes.append((k, b, c))
    if changes:
        detail = "；".join(f"{k}: 基線={b!r} 現況={c!r}" for k, b, c in changes)
        return CanaryVerdict(STATUS_DRIFT, surface, detail, changes)
    return CanaryVerdict(STATUS_OK, surface)


class ContractBaseline:
    """契約基線檔讀寫（schema_version + 拒絕降版 + 原子寫，仿 roster storage）。

    檔案結構：{"schema_version": 1, "surfaces": {surface: {"fingerprint": {...},
    "calibrated_at": iso, "note": str}}}。壞檔/缺檔一律視為「無基線」（呼叫端得到
    UNCALIBRATED），絕不拋例外中斷熱鍵。
    """

    def __init__(self, path: str):
        self.path = path

    def _load_raw(self) -> dict:
        import json
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception:
            logging.warning("[canary] 基線檔讀取失敗（視為空）: %s", self.path,
                            exc_info=True)
            return {}

    @staticmethod
    def _schema_of(d: dict) -> int:
        try:
            return int(d.get("schema_version", SCHEMA_VERSION) or SCHEMA_VERSION)
        except (TypeError, ValueError):
            return SCHEMA_VERSION

    def _is_newer_schema(self) -> bool:
        """既有檔 schema 是否比本程式新（→ 讀忽略、寫拒絕，皆不降版毀損）。"""
        return self._schema_of(self._load_raw()) > SCHEMA_VERSION

    def _surfaces(self) -> dict:
        d = self._load_raw()
        if self._schema_of(d) > SCHEMA_VERSION:
            logging.warning("[canary] 基線檔 schema 比程式新 → 忽略（不降版毀損）")
            return {}
        s = d.get("surfaces")
        return s if isinstance(s, dict) else {}

    def get(self, surface: str) -> Optional[dict]:
        """回該面向的基線指紋 dict；無則 None（→ 呼叫端得 UNCALIBRATED）。"""
        entry = self._surfaces().get(surface)
        if not isinstance(entry, dict):
            return None
        fp = entry.get("fingerprint")
        return fp if isinstance(fp, dict) else None

    def info(self, surface: str) -> Optional[dict]:
        """回該面向的完整基線紀錄（fingerprint/calibrated_at/note）；無則 None。"""
        entry = self._surfaces().get(surface)
        return dict(entry) if isinstance(entry, dict) else None

    def set(self, surface: str, fingerprint: dict, *, note: str = "") -> bool:
        """（重新）校正：記錄該面向現況指紋為新基線。原子寫、帶時間戳。
        回 True＝已寫入;False＝被拒（既有檔 schema 較新，防降版 no-op）。

        [codex] 寫入前檢查既有檔 schema:比本程式新 → 拒絕(no-op),不用舊 schema 覆寫
        毀損較新版本寫的檔（同 roster storage 每次 save 前的防降版）。呼叫端須依回值判斷
        是否真的落地（勿無條件顯示成功）。"""
        if self._is_newer_schema():
            logging.warning("[canary] 基線檔 schema 比程式新 → 拒絕寫入 set(%s)（防降版毀損）",
                            surface)
            return False
        raw = self._load_raw()
        surfaces = raw.get("surfaces")
        if not isinstance(surfaces, dict):
            surfaces = {}
        surfaces[surface] = {
            "fingerprint": dict(fingerprint),
            "calibrated_at": datetime.now().isoformat(timespec="seconds"),
            "note": str(note),
        }
        atomic_write_json(self.path, {
            "schema_version": SCHEMA_VERSION,
            "surfaces": surfaces,
        })
        logging.info("[canary] 已校正基線 surface=%s（%d 欄）", surface, len(fingerprint))
        return True

    def clear(self, surface: str) -> bool:
        """移除某面向基線（回到 UNCALIBRATED）。回是否有移除。

        [codex] 同 set:既有檔 schema 較新 → 拒絕(no-op),不降版覆寫。"""
        if self._is_newer_schema():
            logging.warning("[canary] 基線檔 schema 比程式新 → 拒絕 clear(%s)（防降版毀損）",
                            surface)
            return False
        raw = self._load_raw()
        surfaces = raw.get("surfaces")
        if not isinstance(surfaces, dict) or surface not in surfaces:
            return False
        surfaces.pop(surface, None)
        atomic_write_json(self.path, {
            "schema_version": SCHEMA_VERSION,
            "surfaces": surfaces,
        })
        return True
