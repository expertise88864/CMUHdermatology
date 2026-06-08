# -*- coding: utf-8 -*-
"""縮寫速寫引擎（PhraseExpress-like text expansion）。

設計重點：
1. 用 `keyboard.on_press` 自製 buffer matcher，支援大小寫不敏感 + longest-match。
2. 原生文字欄位優先直接取代；其他欄位 fallback 為 backspace + 剪貼簿貼上。
3. 防自我觸發：寫入期間設旗標，hook 看到旗標就略過。
4. IME 安全：查焦點子欄位；中文模式或正在組字時跳過，英數模式允許展開。
5. 動態 token：在「展開內文」中可寫 da / da1 / da2 / da-N / da+N，渲染時自動代入。
6. token 邊界以「前後皆非 ASCII 英數」判定，避免 data / Adam 等英文字內被誤觸。

設定檔 schema (settings/abbrev_settings.json)：
    {
        "enabled": false,
        "skip_when_ime_active": true,
        "preserve_trailing_space": true,
        "items": [
            {"abbrev": "da", "expansion": "da"},
            ...
        ]
    }
"""
from __future__ import annotations

import ctypes
import logging
import os
import re
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from cmuh_common.atomic_io import atomic_write_json
from cmuh_common.config_io import load_json_dict


# -----------------------------------------------------------------------------
# 預設 snippets（首次啟動自動寫入；不含 if，避免英文 "if " 誤觸）
# -----------------------------------------------------------------------------
ABBREV_CONFIG_SCHEMA_VERSION = 8  # [v8 2026-06-04] 移除指定醫師代碼預設縮寫（撤回 v7 推送）；bump 觸發現有使用者自動清除
MAX_ABBREV_LENGTH = 63

DEFAULT_ITEMS: list[dict[str, str]] = [
    {"abbrev": "cert", "expansion": "患者因上述皮膚疾病，曾於da_zh至本院皮膚科門診就醫治療，建議持續追蹤。"},
    {"abbrev": "da",   "expansion": "da"},
    {"abbrev": "da1",  "expansion": "da1"},
    {"abbrev": "da2",  "expansion": "da2"},
    {"abbrev": "cbt",  "expansion": "check blood test"},
    {"abbrev": "ec",   "expansion": "epidermoid cyst"},
    {"abbrev": "mf",   "expansion": "medication and follow up"},
    {"abbrev": "nt",   "expansion": "next time:"},
    {"abbrev": "pred", "expansion": "no DM/HBV/HCV"},
    {"abbrev": "rs",   "expansion": "remove stitches and follow up"},
    {"abbrev": "sd",   "expansion": "seborrheic dermatitis"},
    {"abbrev": "se",   "expansion": "subacute eczema"},
    {"abbrev": "sk",   "expansion": "seborrheic keratosis"},
    {"abbrev": "sk1",  "expansion": "r/o seborrheic keratosis, r/o malignancy"},
    {"abbrev": "st",   "expansion": "keep stable"},
    {"abbrev": "nev1", "expansion": "r/o dysplastic nevus, r/o malignancy"},
    {"abbrev": "ef",   "expansion": "excisional biopsy and follow up, inform post-op 3x scar formation"},
    {"abbrev": "uvb",  "expansion": "UVB: 250 mj/cm2 (1) on da, increased 30 mj/cm2 if no erythema, MAX: 800 mj/cm2"},
    {
        "abbrev": "cert1",
        "expansion": (
            "患者因上述皮膚疾病，於da_zh至本院皮膚科門診就醫治療，"
            "後續接受局部麻醉下皮膚腫瘤切除手術及縫合，"
            "術後病理檢查結果合乎上述疾患。"
            "患者於da_zh返回本院皮膚科門診接受術後照護並拆除手術縫線。"
        ),
    },
    {
        "abbrev": "cert2",
        "expansion": (
            "患者因上述皮膚疾病，曾於da_zh-21至本院皮膚科門診就醫，"
            "後續於da_zh-17接受局部麻醉下之皮膚腫瘤切除及縫合手術，"
            "術後病理檢查結果符合上述疾患。"
            "患者於術後之da_zh-14返回本院皮膚科門診接受照護，"
            "並分別於da_zh-7及da_zh分次拆除手術縫線。"
        ),
    },
]


DEFAULT_CONFIG: dict[str, Any] = {
    "schema_version": ABBREV_CONFIG_SCHEMA_VERSION,
    "enabled": False,
    "skip_when_ime_active": True,
    "preserve_trailing_space": True,
    "close_external_expander": False,
    "items": DEFAULT_ITEMS,
}


# 舊版內建預設的逐字版本（用於偵測 user 是否還沿用舊預設，自動升級）。
# 升級規則：若 user 的 cert1/cert2/ef expansion 完全等於下面字串 → 視為「沒改過」
# → 替換為 DEFAULT_ITEMS 內的新版。User 手動編輯過的內容不會被動。
_LEGACY_DEFAULTS_TO_MIGRATE: dict[str, str] = {
    # [v7 2026-05-28] ef 預設改為含 "and follow up"
    "ef": "excisional biopsy, inform post-op 3x scar formation",
    # cert 預設日期由西式 da（(2026/6/1)）改為中文 da_zh（2026年6月1日）
    "cert": "患者因上述皮膚疾病，曾於da至本院皮膚科門診就醫治療，建議持續追蹤。",
    "cert1": (
        "患者因上述皮膚疾病，於2026年5月28日至本院皮膚科門診就醫治療，"
        "後續接受局部麻醉下皮膚腫瘤切除手術及縫合，"
        "術後病理檢查結果合乎上述疾患。"
        "患者於da返回本院皮膚科門診接受術後照護並拆除手術縫線。"
    ),
    "cert2": (
        "患者因上述皮膚疾病，曾於da-21至本院皮膚科門診就醫，"
        "後續於da-17接受局部麻醉下之皮膚腫瘤切除及縫合手術，"
        "術後病理檢查結果符合上述疾患。"
        "患者於術後之da-14返回本院皮膚科門診接受照護，"
        "並分別於da-7及da分次拆除手術縫線。"
    ),
}


# -----------------------------------------------------------------------------
# 設定資料模型
# -----------------------------------------------------------------------------
@dataclass
class AbbrevConfig:
    schema_version: int = ABBREV_CONFIG_SCHEMA_VERSION
    enabled: bool = False
    skip_when_ime_active: bool = True
    preserve_trailing_space: bool = True
    # 偵測到「專用」文字展開程式（PhraseExpress 等，不含 AutoHotkey）執行中時，
    # 是否強制關閉它、改用本程式縮寫。False = 沿用舊行為（暫停本程式禮讓對方）。
    close_external_expander: bool = False
    items: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": int(self.schema_version),
            "enabled": bool(self.enabled),
            "skip_when_ime_active": bool(self.skip_when_ime_active),
            "preserve_trailing_space": bool(self.preserve_trailing_space),
            "close_external_expander": bool(self.close_external_expander),
            "items": [
                {"abbrev": str(it.get("abbrev", "")).strip(),
                 "expansion": str(it.get("expansion", ""))}
                for it in sort_abbrev_items(self.items)
                if str(it.get("abbrev", "")).strip()
            ],
        }


def sort_abbrev_items(items: list[dict[str, str]]) -> list[dict[str, str]]:
    """Return abbreviations in case-insensitive A-to-Z order."""
    return sorted(
        items,
        key=lambda it: str(it.get("abbrev", "")).strip().casefold(),
    )


def _add_missing_default_items(items: list[dict[str, str]]) -> bool:
    """Append newly introduced defaults without overwriting user custom text."""
    known = {
        str(it.get("abbrev", "")).strip().casefold()
        for it in items
    }
    changed = False
    for default in DEFAULT_ITEMS:
        key = str(default["abbrev"]).casefold()
        if key in known:
            continue
        items.append(dict(default))
        known.add(key)
        changed = True
        logging.info("[abbrev] added new default '%s'", default["abbrev"])
    return changed


def _restore_requested_defaults(items: list[dict[str, str]]) -> bool:
    """Restore specific built-in abbreviations that disappeared from v5 configs.

    This is intentionally narrower than _add_missing_default_items: it fixes the
    requested regressions/new defaults without bringing back every default a
    user may have deliberately removed.
    """
    restore_abbrevs = {
        "nt", "se",
    }
    known = {
        str(it.get("abbrev", "")).strip().casefold()
        for it in items
    }
    defaults = {
        str(it["abbrev"]).casefold(): dict(it)
        for it in DEFAULT_ITEMS
    }
    changed = False
    for abbrev in sorted(restore_abbrevs):
        if abbrev in known:
            continue
        default = defaults.get(abbrev)
        if not default:
            continue
        items.append(default)
        known.add(abbrev)
        changed = True
        logging.info("[abbrev] restored requested default '%s'", abbrev)
    return changed


# [v8 2026-06-04] 已退役的醫師代碼預設縮寫（撤回 v7 推送）。schema < 8 升級時主動清除，
# 依 abbrev 比對，不論 user 是否改過該筆 expansion。
_RETIRED_DEFAULT_ABBREVS: set[str] = {
    "101358", "101823",
    "d14355", "d15645", "d15728", "d20191", "d28592",
    "d31352", "d34899", "d35819", "d6175",
}


def _remove_retired_defaults(items: list[dict[str, str]]) -> bool:
    """移除 v7 推送、現已退役的醫師代碼預設縮寫（依 abbrev 比對，忽略 expansion）。"""
    kept: list[dict[str, str]] = []
    removed = False
    for it in items:
        key = str(it.get("abbrev", "")).strip().casefold()
        if key in _RETIRED_DEFAULT_ABBREVS:
            removed = True
            logging.info("[abbrev] removed retired default '%s'", it.get("abbrev"))
            continue
        kept.append(it)
    if removed:
        items[:] = kept
    return removed


def _maybe_migrate_legacy(items: list[dict[str, str]]) -> bool:
    """偵測 user 的 cert1/cert2/ef 是否還是舊版預設字面。
    若是（= 沒手動改過），升級為 DEFAULT_ITEMS 內的新版。User 手動編輯過的
    內容（不等於舊預設）不會被動。回傳 True 表示有修改。
    """
    changed = False
    new_default_by_abbrev = {
        str(d["abbrev"]).lower(): d["expansion"] for d in DEFAULT_ITEMS
    }
    for it in items:
        ab = str(it.get("abbrev", "")).lower()
        legacy = _LEGACY_DEFAULTS_TO_MIGRATE.get(ab)
        if legacy is None:
            continue
        if str(it.get("expansion", "")) == legacy:
            new_exp = new_default_by_abbrev.get(ab)
            if new_exp and new_exp != legacy:
                it["expansion"] = new_exp
                changed = True
                logging.info(
                    "[abbrev] 自動升級舊版預設 '%s' → 新版", ab)
    return changed


def load_config(path: str) -> AbbrevConfig:
    """讀取設定，缺檔/壞檔自動回 defaults。
    若偵測到舊版內建 cert1/cert2 字面預設，會自動升級為動態 da_zh 版本。
    """
    loaded = load_json_dict(path, {}, merge_defaults=False)
    raw = dict(DEFAULT_CONFIG)
    raw.update(loaded)
    try:
        loaded_schema_version = int(loaded.get("schema_version", 1))
    except (TypeError, ValueError):
        loaded_schema_version = 1
    loaded_schema_version = max(1, loaded_schema_version)
    items = raw.get("items")
    needs_save = not isinstance(items, list)
    if not isinstance(items, list):
        items = [dict(it) for it in DEFAULT_ITEMS]
    cleaned: list[dict[str, str]] = []
    seen_abbrevs: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            needs_save = True
            continue
        abbrev = str(it.get("abbrev", "")).strip()
        if not abbrev:
            needs_save = True
            continue
        key = abbrev.lower()
        if key in seen_abbrevs:
            needs_save = True
            continue
        seen_abbrevs.add(key)
        cleaned.append({"abbrev": abbrev, "expansion": str(it.get("expansion", ""))})

    cfg = AbbrevConfig(
        schema_version=max(
            ABBREV_CONFIG_SCHEMA_VERSION, loaded_schema_version),
        enabled=bool(raw.get("enabled", False)),
        skip_when_ime_active=bool(raw.get("skip_when_ime_active", True)),
        preserve_trailing_space=bool(raw.get("preserve_trailing_space", True)),
        close_external_expander=bool(raw.get("close_external_expander", False)),
        items=sort_abbrev_items(cleaned),
    )

    # 偵測 + 自動升級舊版預設；若有改 → 寫回磁碟
    if loaded_schema_version < ABBREV_CONFIG_SCHEMA_VERSION:
        if loaded_schema_version >= 5:
            needs_save = _restore_requested_defaults(cfg.items) or needs_save
        else:
            needs_save = _add_missing_default_items(cfg.items) or needs_save
        # [v8] 撤回 v7 推送的醫師代碼預設縮寫（含 user 改過 expansion 的）
        needs_save = _remove_retired_defaults(cfg.items) or needs_save
        needs_save = True
    needs_save = _maybe_migrate_legacy(cfg.items) or needs_save
    cfg.items = sort_abbrev_items(cfg.items)
    if needs_save:
        try:
            save_config(path, cfg)
        except Exception:
            logging.debug("[abbrev] migrate 後存檔失敗", exc_info=True)

    return cfg


def save_config(path: str, cfg: AbbrevConfig) -> None:
    """原子寫入設定檔。"""
    atomic_write_json(path, cfg.to_dict())


def ensure_config_file(path: str) -> AbbrevConfig:
    """檔不存在時寫入預設；存在則直接讀。"""
    if not os.path.exists(path):
        save_config(path, AbbrevConfig(**{
            "schema_version": ABBREV_CONFIG_SCHEMA_VERSION,
            "enabled": False,
            "skip_when_ime_active": True,
            "preserve_trailing_space": True,
            "close_external_expander": False,
            "items": [dict(it) for it in DEFAULT_ITEMS],
        }))
    return load_config(path)


# -----------------------------------------------------------------------------
# Token 渲染
# -----------------------------------------------------------------------------
# 比對順序很重要：長的（含 _zh / 含 ±N）寫在前，re alternation 從左到右匹配第一個成立的。
# 邊界：前後皆非 [A-Za-z0-9_]（含底線，避免 da_zh 被誤切成 da + _zh）。
_TOKEN_RE = re.compile(
    r'(?<![A-Za-z0-9_])'
    r'(da_zh[+-]\d+|da_zh|da[+-]\d+|da[12]|da)'
    r'(?![A-Za-z0-9_])'
)


def _fmt_date_slash(d: datetime) -> str:
    """2026/5/27（無 zero-pad，斜線）"""
    return f"{d.year}/{d.month}/{d.day}"


def _fmt_date_zh(d: datetime) -> str:
    """2026年5月27日（中文年月日，無 zero-pad）"""
    return f"{d.year}年{d.month}月{d.day}日"


def _fmt_time_hhmm(d: datetime) -> str:
    """23:34"""
    return f"{d.hour:02d}:{d.minute:02d}"


def render_expansion(template: str, now: Optional[datetime] = None) -> str:
    """把 template 內的日期/時間 token 替換為實際字串。

    斜線格式（西式，含括弧）：
      - da     → (2026/5/27)
      - da1    → 23:34
      - da2    → (2026/5/27) 23:34
      - da+N   → (2026/M/D) 今日 + N 天
      - da-N   → (2026/M/D) 今日 - N 天

    中文格式（年月日）：
      - da_zh    → 2026年5月27日
      - da_zh+N  → 2026年M月D日 今日 + N 天
      - da_zh-N  → 2026年M月D日 今日 - N 天
    """
    if now is None:
        now = datetime.now()

    def repl(m: re.Match) -> str:
        tok = m.group(1)
        # da_zh 系列（中文格式）
        if tok == "da_zh":
            return _fmt_date_zh(now)
        m2 = re.match(r"da_zh([+-])(\d+)", tok)
        if m2:
            sign, n = m2.group(1), int(m2.group(2))
            delta = n if sign == "+" else -n
            return _fmt_date_zh(now + timedelta(days=delta))
        # da / da1 / da2 / da±N（斜線格式）
        if tok == "da":
            return f"({_fmt_date_slash(now)})"
        if tok == "da1":
            return _fmt_time_hhmm(now)
        if tok == "da2":
            return f"({_fmt_date_slash(now)}) {_fmt_time_hhmm(now)}"
        m3 = re.match(r"da([+-])(\d+)", tok)
        if m3:
            sign, n = m3.group(1), int(m3.group(2))
            delta = n if sign == "+" else -n
            return f"({_fmt_date_slash(now + timedelta(days=delta))})"
        return tok

    return _TOKEN_RE.sub(repl, template)


# -----------------------------------------------------------------------------
# IME 偵測 — conversion mode (NATIVE flag) 為主，OpenStatus 僅 fallback
# -----------------------------------------------------------------------------
# Win32 conversion mode flags
_IME_CMODE_NATIVE = 0x0001    # 中文/日文/韓文模式（false = 英文模式）
_GCS_COMPSTR = 0x0008         # composition string

_IMM_CONFIGURED = False
_FOCUS_CONFIGURED = False


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("hwndActive", wintypes.HWND),
        ("hwndFocus", wintypes.HWND),
        ("hwndCapture", wintypes.HWND),
        ("hwndMenuOwner", wintypes.HWND),
        ("hwndMoveSize", wintypes.HWND),
        ("hwndCaret", wintypes.HWND),
        ("rcCaret", wintypes.RECT),
    ]


def _ensure_focus_configured() -> None:
    global _FOCUS_CONFIGURED
    if _FOCUS_CONFIGURED:
        return
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetGUIThreadInfo.argtypes = [
            wintypes.DWORD, ctypes.POINTER(_GUITHREADINFO),
        ]
        user32.GetGUIThreadInfo.restype = wintypes.BOOL
        _FOCUS_CONFIGURED = True
    except Exception:
        logging.debug("[abbrev] focus signatures setup failed", exc_info=True)


def _get_focused_window_handle() -> int:
    """Return the focused child HWND of the foreground thread when available."""
    try:
        _ensure_focus_configured()
        user32 = ctypes.windll.user32
        foreground = user32.GetForegroundWindow()
        if not foreground:
            return 0
        thread_id = user32.GetWindowThreadProcessId(foreground, None)
        if thread_id:
            info = _GUITHREADINFO()
            info.cbSize = ctypes.sizeof(info)
            if user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
                if info.hwndFocus:
                    return int(info.hwndFocus)
        return int(foreground)
    except Exception:
        logging.debug("[abbrev] focused HWND lookup failed", exc_info=True)
        return 0


def _ensure_imm_configured() -> None:
    """設定 imm32 函式 argtypes/restype — 64-bit 上 HANDLE 不設會被截斷成
    32-bit int，ImmGetContext 回的 himc 失效 → 所有 IME 檢查失準。"""
    global _IMM_CONFIGURED
    if _IMM_CONFIGURED:
        return
    try:
        imm = ctypes.windll.imm32
        imm.ImmGetContext.argtypes = [wintypes.HWND]
        imm.ImmGetContext.restype = wintypes.HANDLE
        imm.ImmReleaseContext.argtypes = [wintypes.HWND, wintypes.HANDLE]
        imm.ImmReleaseContext.restype = wintypes.BOOL
        imm.ImmGetOpenStatus.argtypes = [wintypes.HANDLE]
        imm.ImmGetOpenStatus.restype = wintypes.BOOL
        imm.ImmGetConversionStatus.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        imm.ImmGetConversionStatus.restype = wintypes.BOOL
        imm.ImmGetCompositionStringW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
        ]
        imm.ImmGetCompositionStringW.restype = wintypes.LONG
        _IMM_CONFIGURED = True
    except Exception:
        logging.debug("[abbrev] IMM signatures 設定失敗", exc_info=True)


def should_skip_for_input_method() -> bool:
    """前景視窗正在「中文輸入」→ 回 True (跳過展開)。Best-effort。

    [v6 2026-05-28] 重點修正「英文模式被誤擋」bug：
      注音/微軟 IME 即使切到英文模式，ImmGetOpenStatus 仍回 True (IME 仍開啟)。
      舊版第一個就 check OpenStatus → return True → 英文模式也被擋 → 縮寫
      永遠無法觸發。改成以 conversion mode 的 NATIVE flag 為準：
        1. 正在組字 (composition string) → 一定跳過 (打到一半)
        2. conversion mode 可讀 → 只在 NATIVE(中文模式) 才跳；英文模式允許展開
        3. conversion mode 不可讀 (舊 IMM IME) → fallback 看 OpenStatus

    特意「不查鍵盤布局語言」：中文布局 + 注音切英文模式時 layout 仍是中文
    台灣，但 user 期望可觸發 — 不能用 layout 一刀切。
    """
    try:
        _ensure_imm_configured()
        imm32 = ctypes.windll.imm32
        hwnd = _get_focused_window_handle()
        if not hwnd:
            return False
        himc = imm32.ImmGetContext(hwnd)
        if not himc:
            # Some legacy controls expose the IME context only on the
            # foreground top-level window. Prefer the focused child, then
            # preserve the old behavior as a compatibility fallback.
            foreground = ctypes.windll.user32.GetForegroundWindow()
            if foreground and int(foreground) != hwnd:
                hwnd = int(foreground)
                himc = imm32.ImmGetContext(hwnd)
        if not himc:
            return False
        try:
            # 1. 正在組字 → 一定跳過 (打到一半的注音/拼音)
            try:
                size = imm32.ImmGetCompositionStringW(
                    himc, _GCS_COMPSTR, None, 0)
                if isinstance(size, int) and size > 0:
                    return True
            except Exception:
                pass
            # 2. conversion mode：用 NATIVE flag 判斷中/英 (authoritative)
            try:
                conversion = wintypes.DWORD(0)
                sentence = wintypes.DWORD(0)
                ok = imm32.ImmGetConversionStatus(
                    himc,
                    ctypes.byref(conversion),
                    ctypes.byref(sentence),
                )
                if ok:
                    # 中文模式 → 跳過；英文模式 (NATIVE off) → 允許展開
                    return bool(conversion.value & _IME_CMODE_NATIVE)
            except Exception:
                pass
            # 3. conversion 不可讀 → fallback OpenStatus (舊 IMM IME)
            try:
                if imm32.ImmGetOpenStatus(himc):
                    return True
            except Exception:
                pass
        finally:
            imm32.ImmReleaseContext(hwnd, himc)
        return False
    except Exception:
        logging.debug("[abbrev] IME 偵測失敗", exc_info=True)
        return False


# -----------------------------------------------------------------------------
# 外部文字展開程式偵測 (PhraseExpress 等) — 避免雙重展開衝突
# -----------------------------------------------------------------------------
# 已知的文字展開 / 巨集程式 exe 名稱 (小寫)。執行中就暫停本程式縮寫。
_KNOWN_EXPANDER_EXES: frozenset = frozenset({
    "phraseexpress.exe",       # PhraseExpress
    "breevy.exe",              # Breevy
    "textexpander.exe",        # TextExpander
    "beeftext.exe",            # Beeftext
    "espanso.exe",             # espanso
    "espansod.exe",            # espanso daemon
    "atext.exe",               # aText
    "fastkeys.exe",            # FastKeys
    "activewords.exe",         # ActiveWords
    "phrase express.exe",      # 舊版 PhraseExpress 帶空格
    "autohotkey.exe",          # AutoHotkey (常被用來做文字展開)
    "autohotkeyu64.exe",
    "autohotkeyu32.exe",
    "autohotkey64.exe",
    "autohotkey32.exe",
})


def _list_process_names() -> set:
    """列出目前執行中所有 process 的 exe 名稱 (小寫)。psutil 優先，
    fallback tasklist (帶 CREATE_NO_WINDOW 不閃黑框)。"""
    # 1. psutil (快、不開子程序)
    try:
        import psutil  # type: ignore
        names = set()
        for p in psutil.process_iter(['name']):
            try:
                nm = (p.info.get('name') or '').lower()
            except Exception:
                nm = ''
            if nm:
                names.add(nm)
        if names:
            return names
    except Exception:
        pass
    # 2. fallback: tasklist CSV
    try:
        import subprocess
        CREATE_NO_WINDOW = 0x08000000
        out = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        names = set()
        for line in (out.stdout or "").splitlines():
            line = line.strip()
            if line.startswith('"'):
                # CSV: "image.exe","pid","session",...
                name = line.split('","', 1)[0].strip('"').lower()
                if name:
                    names.add(name)
        return names
    except Exception:
        logging.debug("[abbrev] tasklist 取得 process 失敗", exc_info=True)
        return set()


def detect_external_expander() -> Optional[str]:
    """偵測是否有已知文字展開程式 (PhraseExpress 等) 執行中。
    回傳第一個命中的 exe 名稱 (小寫)，否則 None。"""
    try:
        names = _list_process_names()
    except Exception:
        return None
    for exe in _KNOWN_EXPANDER_EXES:
        if exe in names:
            return exe
    return None


# 「專用」文字展開程式：偵測到時可在使用者開啟設定下強制關閉、改用本程式。
# 刻意 **不含 AutoHotkey** —— AHK 是通用自動化工具，常被診間拿來做按鍵重對應 /
# 其他巨集，無差別關掉它可能誤殺不相關的功能。AHK 仍會被 detect_external_expander
# 偵測到 → 走「暫停本程式禮讓」的舊行為，但不會被本函式關閉。
_AUTO_CLOSE_EXPANDER_EXES: frozenset = frozenset({
    "phraseexpress.exe",
    "phrase express.exe",
    "breevy.exe",
    "textexpander.exe",
    "beeftext.exe",
    "espanso.exe",
    "espansod.exe",
    "atext.exe",
    "fastkeys.exe",
    "activewords.exe",
})


def _taskkill_image(image_name: str) -> bool:
    """taskkill /F /IM <image_name>（含子行程），回傳是否成功結束。
    不閃黑框（CREATE_NO_WINDOW）。找不到行程(rc=128)視為已不在、回 False。"""
    try:
        import subprocess
        CREATE_NO_WINDOW = 0x08000000
        out = subprocess.run(
            ["taskkill", "/F", "/T", "/IM", image_name],
            capture_output=True, text=True, timeout=10,
            creationflags=CREATE_NO_WINDOW,
        )
        if out.returncode == 0:
            logging.warning("[abbrev] 已強制關閉外部展開程式 '%s'（改用本程式縮寫）",
                            image_name)
            return True
        logging.info("[abbrev] taskkill '%s' rc=%s：%s", image_name,
                     out.returncode, (out.stderr or out.stdout or "").strip())
        return False
    except Exception:
        logging.debug("[abbrev] taskkill '%s' 例外", image_name, exc_info=True)
        return False


def close_auto_closable_expanders() -> list:
    """強制關閉所有「專用」文字展開程式（不含 AutoHotkey）。
    回傳實際成功關閉的 exe 名稱清單；沒有可關的回空 list。"""
    try:
        names = _list_process_names()
    except Exception:
        return []
    closed: list = []
    for exe in _AUTO_CLOSE_EXPANDER_EXES:
        if exe in names and _taskkill_image(exe):
            closed.append(exe)
    return closed


# -----------------------------------------------------------------------------
# Win32 剪貼簿（paste mode 用，避免逐字 keystroke race condition）
# -----------------------------------------------------------------------------
_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002
_CLIPBOARD_OPEN_ATTEMPTS = 3
_CLIPBOARD_RETRY_DELAY_SEC = 0.005


def _open_clipboard_with_retry(user32) -> bool:
    for attempt in range(_CLIPBOARD_OPEN_ATTEMPTS):
        if user32.OpenClipboard(None):
            return True
        if attempt + 1 < _CLIPBOARD_OPEN_ATTEMPTS:
            time.sleep(_CLIPBOARD_RETRY_DELAY_SEC)
    return False


def _configure_win32_signatures() -> None:
    """把要用的 Win32 函式 argtypes/restype 設好。

    若不設，64-bit Windows 上 HANDLE/LPVOID 會被當成 32-bit int 截斷，
    GlobalAlloc/GlobalLock 看似回 0 → 寫剪貼簿全失敗。
    """
    u = ctypes.windll.user32
    k = ctypes.windll.kernel32
    u.OpenClipboard.argtypes = [wintypes.HWND]
    u.OpenClipboard.restype = wintypes.BOOL
    u.CloseClipboard.argtypes = []
    u.CloseClipboard.restype = wintypes.BOOL
    u.EmptyClipboard.argtypes = []
    u.EmptyClipboard.restype = wintypes.BOOL
    u.GetClipboardData.argtypes = [wintypes.UINT]
    u.GetClipboardData.restype = wintypes.HANDLE
    u.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    u.SetClipboardData.restype = wintypes.HANDLE
    k.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k.GlobalAlloc.restype = wintypes.HANDLE
    k.GlobalLock.argtypes = [wintypes.HANDLE]
    k.GlobalLock.restype = wintypes.LPVOID
    k.GlobalUnlock.argtypes = [wintypes.HANDLE]
    k.GlobalUnlock.restype = wintypes.BOOL
    k.GlobalFree.argtypes = [wintypes.HANDLE]
    k.GlobalFree.restype = wintypes.HANDLE


_WIN32_CONFIGURED = False


def _ensure_win32_configured() -> None:
    global _WIN32_CONFIGURED
    if _WIN32_CONFIGURED:
        return
    try:
        _configure_win32_signatures()
        _WIN32_CONFIGURED = True
    except Exception:
        logging.debug("[abbrev] Win32 signatures 設定失敗", exc_info=True)


def _clipboard_get_text() -> Optional[str]:
    """讀剪貼簿 unicode 文字；非文字 / 無資料 / 失敗則回 None。"""
    try:
        _ensure_win32_configured()
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        if not _open_clipboard_with_retry(user32):
            return None
        try:
            h = user32.GetClipboardData(_CF_UNICODETEXT)
            if not h:
                return None
            p = kernel32.GlobalLock(h)
            if not p:
                return None
            try:
                return ctypes.wstring_at(p)
            finally:
                kernel32.GlobalUnlock(h)
        finally:
            user32.CloseClipboard()
    except Exception:
        logging.debug("[abbrev] clipboard read 失敗", exc_info=True)
        return None


def _clipboard_set_text(text: str) -> bool:
    """寫 unicode 文字到剪貼簿；成功 True。"""
    h_mem = None
    try:
        _ensure_win32_configured()
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        # 字串 + null terminator
        data = (text + "\x00").encode("utf-16-le")
        # 先配置並填好記憶體，再碰剪貼簿。配置失敗時保留使用者原內容。
        h_mem = kernel32.GlobalAlloc(_GMEM_MOVEABLE, len(data))
        if not h_mem:
            return False
        p = kernel32.GlobalLock(h_mem)
        if not p:
            return False
        try:
            ctypes.memmove(p, data, len(data))
        finally:
            kernel32.GlobalUnlock(h_mem)

        if not _open_clipboard_with_retry(user32):
            return False
        try:
            if not user32.EmptyClipboard():
                return False
            # 注意：SetClipboardData 接管 h_mem 所有權；成功後勿 GlobalFree
            if not user32.SetClipboardData(_CF_UNICODETEXT, h_mem):
                return False
            h_mem = None
            return True
        finally:
            user32.CloseClipboard()
    except Exception:
        logging.debug("[abbrev] clipboard write 失敗", exc_info=True)
        return False
    finally:
        if h_mem:
            try:
                ctypes.windll.kernel32.GlobalFree(h_mem)
            except Exception:
                logging.debug("[abbrev] clipboard memory free 失敗", exc_info=True)


def _clipboard_has_nontext_data() -> bool:
    """剪貼簿目前是否含「非文字」內容（圖片/檔案/HTML 等）。

    用來避免破壞使用者剛複製的非文字資料：_clipboard_get_text 只讀 CF_UNICODETEXT，
    對圖片/檔案會回 None，若仍走 paste 路徑(會 EmptyClipboard)就會把它清掉且無法
    還原(備份是 None)。有 Unicode 文字 → False(我們能備份還原)；沒文字但剪貼簿
    非空 → True(視為非文字內容，應避免覆寫)。不需開啟剪貼簿，不會卡。"""
    try:
        u = ctypes.windll.user32
        if u.IsClipboardFormatAvailable(_CF_UNICODETEXT):
            return False
        try:
            return int(u.CountClipboardFormats()) > 0
        except Exception:
            return False
    except Exception:
        return False


# -----------------------------------------------------------------------------
# 原子 SendInput（避免 race condition：一次 call 內所有 events 連續 dispatch，
# 中間不會被 user 真實 keystroke 插隊）
# -----------------------------------------------------------------------------
_INPUT_KEYBOARD = 1
_KEYEVENTF_KEYUP = 0x0002

# Virtual-Key codes 需要
_VK_BACK = 0x08
_VK_CONTROL = 0x11
_VK_V = 0x56

# 64-bit safe pointer-sized integer for dwExtraInfo
_ULONG_PTR = ctypes.c_size_t


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [
        ("type", wintypes.DWORD),
        ("i", _INPUT_UNION),
    ]


def _send_atomic_keystrokes(vk_events: list) -> bool:
    """一次 SendInput call 送多個鍵盤事件，OS 保證連續、不被插隊。

    vk_events = [(vk_code, is_keydown_bool), ...]
    """
    n = len(vk_events)
    if n == 0:
        return True
    try:
        arr = (_INPUT * n)()
        for idx, (vk, is_down) in enumerate(vk_events):
            arr[idx].type = _INPUT_KEYBOARD
            arr[idx].i.ki.wVk = vk
            arr[idx].i.ki.wScan = 0
            arr[idx].i.ki.dwFlags = 0 if is_down else _KEYEVENTF_KEYUP
            arr[idx].i.ki.time = 0
            arr[idx].i.ki.dwExtraInfo = 0
        user32 = ctypes.windll.user32
        sent = user32.SendInput(n, ctypes.byref(arr), ctypes.sizeof(_INPUT))
        return sent == n
    except Exception:
        logging.exception("[abbrev] SendInput 失敗")
        return False


def inject_vk_tap(vk: int) -> bool:
    """注入單一 virtual-key 的 down+up（一次原子 SendInput）。

    供主程式的「熱鍵健康探針」使用：注入一個無副作用的鍵（例如 VK_F24），
    若全域鍵盤 hook 還活著就會被攔截到，藉此判斷 hook 是否已被 Windows
    （LowLevelHooks timeout）靜默移除。回傳 True 代表 SendInput 成功送出。
    """
    return _send_atomic_keystrokes([(int(vk), True), (int(vk), False)])


_WM_GETTEXT = 0x000D
_WM_GETTEXTLENGTH = 0x000E
_EM_GETSEL = 0x00B0
_EM_SETSEL = 0x00B1
_EM_REPLACESEL = 0x00C2
_SMTO_ABORTIFHUNG = 0x0002
_NATIVE_EDIT_POLL_INTERVAL_SEC = 0.005
_NATIVE_EDIT_CONFIGURED = False


def _ensure_native_edit_configured() -> None:
    global _NATIVE_EDIT_CONFIGURED
    if _NATIVE_EDIT_CONFIGURED:
        return
    try:
        user32 = ctypes.windll.user32
        user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        user32.GetClassNameW.argtypes = [
            wintypes.HWND, wintypes.LPWSTR, ctypes.c_int,
        ]
        user32.GetClassNameW.restype = ctypes.c_int
        _NATIVE_EDIT_CONFIGURED = True
    except Exception:
        logging.debug("[abbrev] native edit signatures setup failed", exc_info=True)


def _message_param(value) -> int:
    if isinstance(value, int):
        return value
    return int(ctypes.cast(value, ctypes.c_void_p).value or 0)


def _send_message_timeout(
    hwnd: int,
    message: int,
    wparam=0,
    lparam=0,
    timeout_ms: int = 80,
) -> tuple[bool, int]:
    try:
        _ensure_native_edit_configured()
        result = ctypes.c_size_t(0)
        ok = ctypes.windll.user32.SendMessageTimeoutW(
            hwnd,
            message,
            _message_param(wparam),
            _message_param(lparam),
            _SMTO_ABORTIFHUNG,
            timeout_ms,
            ctypes.byref(result),
        )
        return bool(ok), int(result.value)
    except Exception:
        logging.debug("[abbrev] SendMessageTimeout failed", exc_info=True)
        return False, 0


def _get_window_class_name(hwnd: int) -> str:
    try:
        _ensure_native_edit_configured()
        buffer = ctypes.create_unicode_buffer(128)
        if ctypes.windll.user32.GetClassNameW(hwnd, buffer, len(buffer)):
            return buffer.value
    except Exception:
        logging.debug("[abbrev] window class lookup failed", exc_info=True)
    return ""


def _is_native_edit_control(hwnd: int) -> bool:
    class_name = _get_window_class_name(hwnd).casefold()
    return bool(
        class_name
        and any(token in class_name for token in ("edit", "memo", "rich"))
    )


def _read_window_text(hwnd: int) -> Optional[str]:
    ok, length = _send_message_timeout(hwnd, _WM_GETTEXTLENGTH)
    if not ok:
        return None
    buffer = ctypes.create_unicode_buffer(length + 1)
    ok, _ = _send_message_timeout(hwnd, _WM_GETTEXT, length + 1, buffer)
    return buffer.value if ok else None


def _get_edit_selection(hwnd: int) -> Optional[tuple[int, int]]:
    start = wintypes.DWORD(0)
    end = wintypes.DWORD(0)
    ok, _ = _send_message_timeout(hwnd, _EM_GETSEL, ctypes.byref(start), ctypes.byref(end))
    if not ok:
        return None
    return int(start.value), int(end.value)


def _replace_edit_selection(hwnd: int, start: int, end: int, text: str) -> bool:
    ok, _ = _send_message_timeout(hwnd, _EM_SETSEL, start, end)
    if not ok:
        return False
    ok, _ = _send_message_timeout(hwnd, _EM_REPLACESEL, 1, ctypes.c_wchar_p(text))
    return ok


def _replace_native_edit_suffix(
    expected_suffix: str,
    replacement: str,
    timeout_sec: float,
) -> bool:
    """Replace a verified suffix directly in native Windows text controls."""
    hwnd = _get_focused_window_handle()
    if not hwnd or not _is_native_edit_control(hwnd):
        return False

    deadline = time.monotonic() + max(0.0, timeout_sec)
    while True:
        selection = _get_edit_selection(hwnd)
        if selection and selection[0] == selection[1]:
            caret = selection[1]
            text = _read_window_text(hwnd)
            if text is not None and caret >= len(expected_suffix):
                suffix = text[caret - len(expected_suffix) : caret]
                if suffix.casefold() == expected_suffix.casefold():
                    if _get_focused_window_handle() != hwnd:
                        return False
                    return _replace_edit_selection(
                        hwnd,
                        caret - len(expected_suffix),
                        caret,
                        replacement,
                    )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_NATIVE_EDIT_POLL_INTERVAL_SEC, remaining))


# -----------------------------------------------------------------------------
# 引擎主體
# -----------------------------------------------------------------------------
# `keyboard` event.name 對 printable 鍵會是單字元（'a'、'1'、','...）
# 對特殊鍵會是 'space' / 'enter' / 'tab' / 'backspace' / 'shift' ... 等
_TRIGGER_KEY_NAMES = {"space"}  # 對應 user spec 的「空白鍵」
_RESET_KEY_NAMES = {
    "enter", "tab", "esc", "escape",
    "up", "down", "left", "right",
    "home", "end", "page up", "page down",
    "delete",
}
# Backspace 不在重置集合：改為「刪掉 buffer 最後一個字元」(見 _handle_event)，
# 鏡像使用者實際刪字，讓「打錯字→backspace 修正→重打」仍能觸發展開
# （例：cery→⌫→t→空白 = cert）。
_BACKSPACE_KEY_NAMES = {"backspace"}


class AbbrevEngine:
    """縮寫展開引擎。Thread-safe（hook callback 來自 keyboard 模組獨立 thread）。"""

    # 觸發後一次連送的 backspace 上限，純防呆。
    MAX_BACKSPACE = MAX_ABBREV_LENGTH + 1

    # [v7 2026-05-28] 為了「寧慢求對」全面拉長各延遲，確保刪除/展開正確：
    # 展開後的冷卻時間（s）— 期間 buffer 暫停累積，避免 user 連打第二組
    # 縮寫時後續 keystroke 跟我們的 paste 競態。需 >= 整個替換流程時間。
    COOLDOWN_SEC = 0.9
    # [速度] 原生欄位走「同步直接取代、完全不碰剪貼簿」的快路徑，沒有 paste 競態，
    # 不需要上面為剪貼簿路徑設的長 cool-down。命中原生路徑時改用此縮短值，
    # 讓連續展開（醫師快速打多組縮寫）更即時。只縮短、不延長。
    NATIVE_EDIT_COOLDOWN_SEC = 0.25
    # 送 backspace 前的延遲（s）— 確保「縮寫 + 觸發空白」已先抵達目標視窗。
    # keyboard 模組 hook callback 跑在 hook thread，若不夠延遲就送 backspace，
    # 觸發空白還沒被 dispatch → backspace 跑到空白前面 → 刪錯/沒刪到。
    # 0.045 → 0.12（系統忙時 0.045 不夠 → 偶發刪錯）
    PRE_BACKSPACE_DELAY_SEC = 0.12
    # [v7] backspace 送完到送 Ctrl+V 之間的延遲（s）— 讓目標 app 先處理完
    # 刪除，再貼上，避免「刪除還沒生效就貼上」導致殘留縮寫。
    POST_BACKSPACE_DELAY_SEC = 0.05
    # [v7] Ctrl+V 送完到「還原剪貼簿」之間的延遲（s）— 目標 app 是非同步
    # 讀剪貼簿，太早還原會貼到舊內容（展開錯字）。0.12 → 0.30。
    POST_PASTE_DELAY_SEC = 0.30
    # [v9] _suppressing 自癒餘裕（s）：若旗標卡住超過 cooldown + 此餘裕仍未清，
    # 視為 worker thread 異常未 reset → 強制重置，避免縮寫永久失效要重啟。
    SUPPRESS_SELFHEAL_MARGIN_SEC = 2.0

    def __init__(self, kb_module: Any) -> None:
        """kb_module = `keyboard` PyPI 套件物件（已 import 完成）。"""
        self._kb = kb_module
        self._lock = threading.Lock()
        self._cfg: AbbrevConfig = AbbrevConfig()
        # abbrev (lower) → expansion 原文
        self._lookup: dict[str, str] = {}
        self._max_abbrev_len: int = 0
        self._buffer: str = ""
        self._press_hook: Any = None
        self._suppressing = False
        # [2026-06-05] 上次打字時的鍵盤焦點控制項 HWND。焦點(欄位/視窗)改變就清空
        # buffer，避免在 A 欄打"ne"、點到 B 欄打"v1 "被拼成假縮寫 nev1 而誤觸發。
        self._last_focus_hwnd: int = 0
        # 展開後的冷卻截止時間（monotonic）
        self._cooldown_until: float = 0.0
        # [v6] 偵測到的外部文字展開程式名稱 (None=沒有)；有的話暫停本程式縮寫。
        # 注意：外部程式的「持續監看/重評估」由 main.py 的 _abbrev_monitor_external
        # （UI thread, root.after）驅動，本引擎不另起 timer，避免重複輪詢。
        self._external_expander: Optional[str] = None

    # ------------------------------------------------------------------ 公開 API
    def install(self, cfg: AbbrevConfig) -> None:
        """套用設定並掛上 keyboard hook。重複呼叫會先 uninstall 再裝。

        外部展開程式（PhraseExpress 等）的偵測：install 時評估一次，依結果
        決定掛 hook 或暫停。出現/消失的持續監看由 main.py 的
        _abbrev_monitor_external（UI thread）週期重 install 來驅動。

        cfg.close_external_expander=True 時，偵測到「專用」展開程式會先強制關閉它
        （不含 AutoHotkey），再改用本程式；關不掉 / 剩 AutoHotkey 時退回暫停。
        """
        # [鎖外處理] taskkill 最壞會等到 10s timeout；放在 self._lock 內會讓
        # 期間的打字 hook callback（也要搶同一把鎖）卡住。故先在鎖外把「強制關閉
        # 專用展開程式」做完，再進鎖做後續掛載判斷（鎖內會再 detect 一次確認）。
        want_hook = bool(cfg.enabled) and any(
            str(it.get("abbrev", "")).strip() for it in cfg.items
        )
        if want_hook and cfg.close_external_expander:
            try:
                if detect_external_expander():
                    if close_auto_closable_expanders():
                        # taskkill 後給 OS 一點時間把行程移出清單，再讓鎖內重新偵測。
                        time.sleep(0.3)
            except Exception:
                logging.debug("[abbrev] 嘗試關閉外部展開程式時例外", exc_info=True)

        with self._lock:
            self._cfg = cfg
            self._rebuild_lookup_locked()
            self._buffer = ""
            self._uninstall_locked()
            if not cfg.enabled or not self._lookup:
                logging.info("[abbrev] hook 未掛載（enabled=%s, items=%d）",
                             cfg.enabled, len(self._lookup))
                return
            # 偵測外部文字展開程式 → 仍在的話暫停，避免雙重展開。
            # （close_external_expander 開啟時，鎖外已嘗試關閉專用展開程式；
            #   這裡若仍偵測到，多半是 AutoHotkey 或關閉失敗 → 禮讓暫停。）
            ext = detect_external_expander()
            self._external_expander = ext
            if ext:
                logging.warning(
                    "[abbrev] 偵測到外部文字展開程式 '%s' 執行中 → 暫停本程式縮寫"
                    "避免衝突（關閉該程式後會自動恢復）", ext)
                return
            try:
                self._press_hook = self._kb.on_press(self._on_press)
                logging.info("[abbrev] hook 已掛載，%d 筆縮寫（最長 %d 字）",
                             len(self._lookup), self._max_abbrev_len)
            except Exception:
                logging.exception("[abbrev] keyboard hook 掛載失敗")
                self._press_hook = None

    def uninstall(self) -> None:
        with self._lock:
            self._uninstall_locked()

    def is_installed(self) -> bool:
        with self._lock:
            return self._press_hook is not None

    # ----------------------------------------------------------------- 內部工具
    def _uninstall_locked(self) -> None:
        h = self._press_hook
        if h is not None:
            try:
                self._kb.unhook(h)
            except Exception:
                logging.debug("[abbrev] unhook 失敗", exc_info=True)
            self._press_hook = None

    def _rebuild_lookup_locked(self) -> None:
        self._lookup = {}
        max_len = 0
        for it in self._cfg.items:
            abbrev = str(it.get("abbrev", "")).strip()
            if not abbrev:
                continue
            key = abbrev.lower()
            if len(key) > MAX_ABBREV_LENGTH:
                logging.warning(
                    "[abbrev] skip overlong abbreviation '%s' (%d > %d)",
                    key, len(key), MAX_ABBREV_LENGTH)
                continue
            self._lookup[key] = str(it.get("expansion", ""))
            if len(key) > max_len:
                max_len = len(key)
        self._max_abbrev_len = max_len

    # ------------------------------------------------------------------ 事件處理
    def _on_press(self, event: Any) -> None:
        """keyboard 模組 on_press callback。"""
        try:
            self._handle_event(event)
        except Exception:
            logging.exception("[abbrev] _on_press 處理失敗")

    def _reset_buffer_if_focus_changed(self) -> None:
        """[2026-06-05] 鍵盤焦點(欄位/視窗)改變 → 清空 buffer。

        防「跨欄位殘留拼成假縮寫」：在 A 欄打"ne"、滑鼠點到 B 欄再打"v1 "，原本
        buffer 會是"nev1"而誤觸發。每次打字前比對焦點控制項 HWND，變了就清空。
        best-effort：查焦點失敗(回 0)就不動作，避免誤清；只有 buffer 非空且焦點
        確實改變才清。注意:瀏覽器同頁多個輸入框常共用同一 render HWND → 偵測不到
        (限制)；醫院 Delphi 主程式每欄獨立 HWND → 可正確偵測(主要使用情境)。"""
        try:
            cur = _get_focused_window_handle()
        except Exception:
            return
        if not cur:
            return  # 查不到焦點 → 不動作(避免把正常打字的 buffer 誤清)
        with self._lock:
            if (self._buffer and self._last_focus_hwnd
                    and cur != self._last_focus_hwnd):
                self._buffer = ""
            self._last_focus_hwnd = cur

    def _handle_event(self, event: Any) -> None:
        # 自己 send/write 期間，所有按鍵忽略。
        # [v9] 自癒：若 _suppressing 卡在 True 但已遠超過 cooldown 期限
        # （worker thread 異常未 reset / start 失敗），強制重置，避免縮寫
        # 永久失效需重啟程式。
        if self._suppressing:
            if time.monotonic() > self._cooldown_until + self.SUPPRESS_SELFHEAL_MARGIN_SEC:
                logging.warning("[abbrev] _suppressing 逾時未清除，自癒強制重置")
                self._suppressing = False
                with self._lock:
                    self._buffer = ""
            else:
                return

        # cool-down 期間（展開剛完成）— 不更新 buffer、不觸發
        if time.monotonic() < self._cooldown_until:
            return

        name = getattr(event, "name", None)
        if not name:
            return

        # trigger 鍵（空白）：嘗試展開
        if name in _TRIGGER_KEY_NAMES:
            # 焦點換了 → 先清空,避免在新欄位展開舊欄位殘留的縮寫
            self._reset_buffer_if_focus_changed()
            with self._lock:
                buffer_snapshot = self._buffer
                self._buffer = ""
            expanded = self._try_expand(buffer_snapshot, " ")
            if not expanded:
                # [優化] 沒展開 → 把「候選 + 觸發空白」留回 buffer。讓使用者發現打錯、
                # 用 backspace 刪掉空白再改字(例:「nev 」→⌫→「nev1」)時 buffer 仍能
                # 重建成「nev1」而非只剩改的那幾字,下個空白才正確觸發。原本無條件清空
                # 會把前面打過的字丟失。成功展開時不保留(已替換、重新開始)。
                keep = self._max_abbrev_len + 1 if self._max_abbrev_len else 0
                with self._lock:
                    if keep and not self._buffer:  # 期間無新輸入才覆寫(防 race)
                        self._buffer = (buffer_snapshot + " ")[-keep:]
            return

        # backspace：刪掉 buffer 最後一個字元（鏡像使用者刪字），而非整段清空。
        # 讓「打錯字→backspace 修正→重打」仍能觸發展開（例：cery→⌫→t→空白 = cert）。
        if name in _BACKSPACE_KEY_NAMES:
            with self._lock:
                self._buffer = self._buffer[:-1]
            return

        # 重置 buffer 的鍵
        if name in _RESET_KEY_NAMES:
            with self._lock:
                self._buffer = ""
            return

        # printable 單字元（'a' 'B' '1' '/' '-' '_' 等）
        if len(name) == 1:
            # 焦點(欄位/視窗)換了 → 先清空舊欄位殘留,再開始累積本欄位的字
            self._reset_buffer_if_focus_changed()
            ch = name.lower()
            with self._lock:
                # 多保留 1 個字元(max_abbrev_len + 1)：供 _try_expand 判斷縮寫前是否為
                # 字邊界(要看得到「縮寫的前一個字元」，才能擋掉字尾誤觸，如 persist→st)。
                keep = self._max_abbrev_len + 1 if self._max_abbrev_len else 0
                self._buffer = (self._buffer + ch)[-keep:] if keep else ""
            return

        # 其他特殊鍵（shift / ctrl / alt / caps lock 等）—不影響 buffer
        return

    def _try_expand(self, buffer_snapshot: str, trigger_char: str) -> bool:
        """回傳 True=有啟動展開；False=沒展開(無匹配/非完整字/IME/render 失敗/
        worker 啟動失敗)。空白觸發端用此決定沒展開時是否保留 buffer 供後續編輯。"""
        if not buffer_snapshot or not self._lookup:
            return False

        # longest match from the right end of buffer
        matched_key: Optional[str] = None
        for length in range(min(self._max_abbrev_len, len(buffer_snapshot)), 0, -1):
            candidate = buffer_snapshot[-length:]
            if candidate in self._lookup:
                matched_key = candidate
                break
        if matched_key is None:
            return False

        # [修正] 只在縮寫是「完整的字」時才展開。縮寫的「前一個字元」必須是邊界
        # ——空白 / 標點 / 符號，或位於字首——才算完整字。若前一字元是「字母或數字」
        # 就代表縮寫只是某個更長 token 的字尾、黏在別的字裡，不該展開，例如：
        #   persist 的 st、clida 的 da（英文字母在前）、病灶da 的 da（中文字在前）。
        # str.isalnum() 對中文等 CJK 字也回 True，所以單一條件即可同時擋掉
        # 「英文字母在前」與「中文在前」；空白 / 全形或半形標點則回 False → 視為邊界 → 展開。
        prefix = buffer_snapshot[:len(buffer_snapshot) - len(matched_key)]
        if prefix:
            prev_ch = prefix[-1]
            if prev_ch.isalnum():
                logging.debug(
                    "[abbrev] '%s' 前一字元=%r 為字母/數字/中文(非邊界)，"
                    "非完整字，略過展開", matched_key, prev_ch)
                return False

        # IME 中文模式或組字中 → 跳過（best-effort；新 TSF IME 上 IMM API 可能無效，
        # 偵測不到時就照常展開 — 寧可展開也不要整個功能卡死）
        if self._cfg.skip_when_ime_active and should_skip_for_input_method():
            logging.debug("[abbrev] IME 中文模式或組字中，跳過 '%s'", matched_key)
            return False

        raw_expansion = self._lookup[matched_key]
        try:
            rendered = render_expansion(raw_expansion, datetime.now())
        except Exception:
            logging.exception("[abbrev] render_expansion 失敗 abbrev=%s", matched_key)
            return False

        if self._cfg.preserve_trailing_space:
            rendered = rendered + " "

        # 刪掉「縮寫 + 觸發字元」共 len(matched_key)+1 個字元
        delete_count = min(len(matched_key) + len(trigger_char), self.MAX_BACKSPACE)

        # 立即進入 suppress + cool-down（同步，在 hook thread 內），這樣
        # 後續按鍵會被忽略，避免重複觸發。
        self._suppressing = True
        self._cooldown_until = time.monotonic() + self.COOLDOWN_SEC

        # 實際送鍵延後到獨立 thread：讓本 hook callback 先 return → keyboard
        # 模組把「縮寫 + 觸發空白」完整 dispatch 到目標視窗後，我們再送 backspace。
        # （hook callback 同步跑在 hook thread；若在這裡直接送 backspace，
        #  觸發空白還沒到目標視窗 → 順序錯亂 → 沒刪到 / 刪錯字。）
        # [v9] start() 失敗時必須 reset _suppressing，否則旗標永久卡 True →
        # 縮寫全失效（自癒機制是第二道防線，這裡是第一道）。
        try:
            worker = threading.Thread(
                target=self._do_replace,
                args=(delete_count, rendered, matched_key, matched_key + trigger_char),
                daemon=True,
            )
            worker.start()
            return True
        except Exception:
            logging.exception("[abbrev] 啟動展開 worker thread 失敗，重置 suppress")
            self._suppressing = False
            self._cooldown_until = 0.0
            return False

    def _do_replace(
        self,
        backspace_count: int,
        text: str,
        abbrev_key: str,
        typed_suffix: Optional[str] = None,
    ) -> None:
        """在獨立 thread 執行：原生欄位先嘗試直接取代，其他欄位再分兩段
        SendInput：(1) backspace × N → 等目標處理完刪除 → (2) Ctrl+V 貼上。
        期間 BlockInput 凍結 user 真實輸入避免 race（需 admin；非 admin 退而
        靠 cool-down）。[v7] 為「寧慢求對」，刪除與貼上拆開且各加足夠延遲，
        並延後還原剪貼簿至 app 確實讀完。

        _suppressing 與 cool-down 已在呼叫端 (_try_expand) 同步設好。
        """
        kb = self._kb
        if kb is None:
            self._suppressing = False
            return

        replace_started = time.monotonic()
        old_clip: Optional[str] = None
        clip_ok = False
        used_native_edit = False
        used_paste = False
        used_keystroke = False
        try:
            # 原生 Windows 文字欄位可直接核對並取代 suffix，不需剪貼簿或 backspace。
            used_native_edit = _replace_native_edit_suffix(
                typed_suffix or (abbrev_key + " "),
                text,
                self.PRE_BACKSPACE_DELAY_SEC,
            )
            if used_native_edit:
                # 原生欄位同步取代完成、不碰剪貼簿 → 無 paste 競態，cool-down 可大幅
                # 縮短（只縮短不延長），讓連續展開更即時。
                self._cooldown_until = min(
                    self._cooldown_until,
                    time.monotonic() + self.NATIVE_EDIT_COOLDOWN_SEC,
                )
                return

            # 非原生欄位仍等觸發空白抵達目標視窗，再走相容性較廣的 fallback。
            remaining_delay = self.PRE_BACKSPACE_DELAY_SEC - (
                time.monotonic() - replace_started
            )
            if remaining_delay > 0:
                time.sleep(remaining_delay)

            # 1. 備份 + 設剪貼簿（paste mode 首選）
            old_clip = _clipboard_get_text()
            # [safety] 剪貼簿存著非文字內容(圖片/檔案/HTML)時 old_clip 為 None；走
            # paste 路徑會 EmptyClipboard 清掉它且無法還原 → 改走 keystroke fallback
            # 完全不碰剪貼簿，保住使用者剛複製的資料。
            force_keystroke = (old_clip is None and _clipboard_has_nontext_data())
            clip_ok = (not force_keystroke) and _clipboard_set_text(text)

            if clip_ok:
                # [v7] 拆成「先刪除、再貼上」兩段原子 SendInput，中間留時間
                # 給目標 app 處理刪除，避免「刪除還沒生效就貼上」殘留縮寫。
                bs_events: list = []
                for _ in range(backspace_count):
                    bs_events.append((_VK_BACK, True))
                    bs_events.append((_VK_BACK, False))
                paste_events: list = [
                    (_VK_CONTROL, True),
                    (_VK_V, True),
                    (_VK_V, False),
                    (_VK_CONTROL, False),
                ]

                # BlockInput 凍結 user 輸入 → 兩段 SendInput → 解凍
                user32 = ctypes.windll.user32
                blocked = False
                try:
                    blocked = bool(user32.BlockInput(True))
                except Exception:
                    logging.debug("[abbrev] BlockInput 不可用", exc_info=True)
                try:
                    # 2a-1. 先送 backspace 刪除「縮寫 + 觸發空白」
                    bs_ok = _send_atomic_keystrokes(bs_events)
                    # 2a-2. 等目標 app 確實處理完刪除，再貼上
                    time.sleep(self.POST_BACKSPACE_DELAY_SEC)
                    # 2a-3. 送 Ctrl+V 貼上展開內容
                    paste_ok = _send_atomic_keystrokes(paste_events)
                    used_paste = bool(bs_ok and paste_ok)
                finally:
                    if blocked:
                        try:
                            user32.BlockInput(False)
                        except Exception:
                            pass
                # 4a. 等 target app 非同步讀剪貼簿 + 處理 paste 完成，
                #     才在 finally 還原剪貼簿（太早還原會貼到舊內容）。
                time.sleep(self.POST_PASTE_DELAY_SEC)
            else:
                # 2b. fallback: 剪貼簿寫入失敗 / 剪貼簿含非文字內容 → keystroke 老路
                if force_keystroke:
                    logging.info("[abbrev] 剪貼簿含非文字內容(圖片/檔案)，改用 "
                                 "keystroke 展開以免破壞使用者剪貼簿")
                else:
                    logging.warning("[abbrev] 剪貼簿寫入失敗，fallback 用 keystroke")
                for _ in range(backspace_count):
                    try:
                        kb.send("backspace")
                    except Exception:
                        break
                try:
                    kb.write(text)
                    used_keystroke = True
                except Exception:
                    logging.exception("[abbrev] keyboard.write fallback 失敗")
        except Exception:
            logging.exception("[abbrev] _do_replace 失敗 abbrev=%s", abbrev_key)
        finally:
            # 還原剪貼簿。[safety] 僅在「確實寫過剪貼簿(clip_ok)且剪貼簿現在仍是我們
            # 貼上的展開內容」時才動作：BlockInput 在 POST_PASTE 等待前就解凍，那
            # 0.3s 內使用者可能已 Ctrl+C 複製新東西，無條件還原會蓋掉它。
            # [stability r4] 條件由「old_clip is not None and clip_ok」改為「clip_ok」：
            # 原本剪貼簿為空時 old_clip 為 None → 舊條件跳過還原 → 我們寫入的整段展開
            # 內文(可能上百字病歷)會永久殘留在系統剪貼簿，使用者下次 Ctrl+V 貼到展開內容。
            # 現在 old_clip 為 None 時改清空剪貼簿(寫入空字串)，把我們的展開內文清掉。
            if clip_ok:
                try:
                    if _clipboard_get_text() == text:
                        _clipboard_set_text(old_clip if old_clip is not None else "")
                    else:
                        logging.debug("[abbrev] 剪貼簿已被使用者更新，保留不還原")
                except Exception:
                    logging.debug("[abbrev] 還原剪貼簿失敗", exc_info=True)

            mode = (
                "native-edit"
                if used_native_edit
                else ("atomic-paste" if used_paste else ("keystroke" if used_keystroke else "FAIL"))
            )
            logging.info("[abbrev] 展開 '%s' → %d 字 (%s)",
                         abbrev_key, len(text), mode)

            # cool-down 期滿後才清 suppress 旗標 + buffer
            def _clear():
                self._suppressing = False
                with self._lock:
                    self._buffer = ""
            remaining = max(0.0, self._cooldown_until - time.monotonic())
            if remaining:
                t = threading.Timer(remaining, _clear)
                t.daemon = True
                try:
                    t.start()
                except Exception:
                    logging.exception(
                        "[abbrev] cooldown timer start failed; clear now")
                    _clear()
            else:
                _clear()
