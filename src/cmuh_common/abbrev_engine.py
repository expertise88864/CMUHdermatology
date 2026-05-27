# -*- coding: utf-8 -*-
"""縮寫速寫引擎（PhraseExpress-like text expansion）。

設計重點：
1. 用 `keyboard.on_press` 自製 buffer matcher，支援大小寫不敏感 + longest-match。
2. 觸發後送 backspace 刪掉原始字串，再 `keyboard.write` 出展開內容。
3. 防自我觸發：寫入期間設旗標，hook 看到旗標就略過。
4. IME 安全：觸發前查前景視窗 IME，組字中（ImmGetOpenStatus）一律跳過。
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
from typing import Any, Callable, Optional

from cmuh_common.atomic_io import atomic_write_json
from cmuh_common.config_io import load_json_dict


# -----------------------------------------------------------------------------
# 預設 snippets（首次啟動自動寫入；不含 if，避免英文 "if " 誤觸）
# -----------------------------------------------------------------------------
DEFAULT_ITEMS: list[dict[str, str]] = [
    {"abbrev": "da",   "expansion": "da"},
    {"abbrev": "da1",  "expansion": "da1"},
    {"abbrev": "da2",  "expansion": "da2"},
    {"abbrev": "ec",   "expansion": "epidermoid cyst"},
    {"abbrev": "sk",   "expansion": "seborrheic keratosis"},
    {"abbrev": "sk1",  "expansion": "r/o seborrheic keratosis, r/o malignancy"},
    {"abbrev": "nev1", "expansion": "r/o dysplastic nevus, r/o malignancy"},
    {"abbrev": "ef",   "expansion": "excisional biopsy, inform post-op 3x scar formation"},
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
    "enabled": False,
    "skip_when_ime_active": True,
    "preserve_trailing_space": True,
    "items": DEFAULT_ITEMS,
}


# 舊版內建預設的逐字版本（用於偵測 user 是否還沿用舊預設，自動升級）。
# 升級規則：若 user 的 cert1/cert2 expansion 完全等於下面字串 → 視為「沒改過」
# → 替換為 DEFAULT_ITEMS 內的新版（含 da_zh token）。
_LEGACY_DEFAULTS_TO_MIGRATE: dict[str, str] = {
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
    enabled: bool = False
    skip_when_ime_active: bool = True
    preserve_trailing_space: bool = True
    items: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.enabled),
            "skip_when_ime_active": bool(self.skip_when_ime_active),
            "preserve_trailing_space": bool(self.preserve_trailing_space),
            "items": [
                {"abbrev": str(it.get("abbrev", "")).strip(),
                 "expansion": str(it.get("expansion", ""))}
                for it in self.items
                if str(it.get("abbrev", "")).strip()
            ],
        }


def _maybe_migrate_legacy(items: list[dict[str, str]]) -> bool:
    """偵測 user 的 cert1/cert2 是否還是舊版預設（字面 2026/5/28、da-N 斜線）。
    若是，升級為新版（da_zh token）。User 手動編輯過的內容不會被動。
    回傳 True 表示有修改。
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
                    "[abbrev] 自動升級舊版預設 '%s' → 新版含 da_zh token", ab)
    return changed


def load_config(path: str) -> AbbrevConfig:
    """讀取設定，缺檔/壞檔自動回 defaults。
    若偵測到舊版內建 cert1/cert2 字面預設，會自動升級為動態 da_zh 版本。
    """
    raw = load_json_dict(path, DEFAULT_CONFIG, merge_defaults=True)
    items = raw.get("items")
    if not isinstance(items, list):
        items = list(DEFAULT_ITEMS)
    cleaned: list[dict[str, str]] = []
    seen_abbrevs: set[str] = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        abbrev = str(it.get("abbrev", "")).strip()
        if not abbrev:
            continue
        key = abbrev.lower()
        if key in seen_abbrevs:
            continue
        seen_abbrevs.add(key)
        cleaned.append({"abbrev": abbrev, "expansion": str(it.get("expansion", ""))})

    cfg = AbbrevConfig(
        enabled=bool(raw.get("enabled", False)),
        skip_when_ime_active=bool(raw.get("skip_when_ime_active", True)),
        preserve_trailing_space=bool(raw.get("preserve_trailing_space", True)),
        items=cleaned,
    )

    # 偵測 + 自動升級舊版預設；若有改 → 寫回磁碟
    if _maybe_migrate_legacy(cfg.items):
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
            "enabled": False,
            "skip_when_ime_active": True,
            "preserve_trailing_space": True,
            "items": list(DEFAULT_ITEMS),
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
# IME 偵測 — 多重檢查（新版注音/微軟 IME 不一定回 ImmGetOpenStatus）
# -----------------------------------------------------------------------------
# Win32 conversion mode flags
_IME_CMODE_NATIVE = 0x0001    # 中文/日文/韓文模式（false = 英文模式）
_GCS_COMPSTR = 0x0008         # composition string


def should_skip_for_input_method() -> bool:
    """前景視窗目前是「中文/組字輸入狀態」就回 True。

    用四重檢查（任一條件成立就視為中文輸入中）：
      1. 鍵盤布局主語非英文（最強信號；新注音/微軟拼音 IME 都在中文布局上）
      2. ImmGetOpenStatus：IME 開啟旗標（舊 IMM IME 可靠）
      3. ImmGetCompositionString：是否有 composition string in progress
      4. ImmGetConversionStatus：conversion mode 是否含 IME_CMODE_NATIVE

    為何要 layout check：新版 TSF-based IME（微軟新注音、Google 注音 IME）
    對 2/3/4 不一定更新，但只要 user 用中文布局，layout langid 一定是中文。
    Trade-off：若 user 只裝中文布局，便沒有英文觸發環境；要使用縮寫
    請另裝 en-US 布局，或關閉本分頁的「中文輸入法組字中暫停展開」選項。
    """
    try:
        user32 = ctypes.windll.user32
        imm32 = ctypes.windll.imm32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False

        # 1. 鍵盤布局主語檢查（最強信號）
        try:
            pid = ctypes.c_ulong(0)
            tid = user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            hkl = user32.GetKeyboardLayout(tid)
            # hkl low 16 bits = LANGID；primary language 在 low 10 bits
            langid = hkl & 0xFFFF
            primary_lang = langid & 0x3FF
            _LANG_ENGLISH = 0x09
            if primary_lang and primary_lang != _LANG_ENGLISH:
                return True
        except Exception:
            logging.debug("[abbrev] layout 檢查失敗", exc_info=True)

        # 2-4. IMM 系列檢查（傳統 IME 用）
        himc = imm32.ImmGetContext(hwnd)
        if not himc:
            return False
        try:
            try:
                if imm32.ImmGetOpenStatus(himc):
                    return True
            except Exception:
                pass
            try:
                size = imm32.ImmGetCompositionStringW(himc, _GCS_COMPSTR, None, 0)
                if isinstance(size, int) and size > 0:
                    return True
            except Exception:
                pass
            try:
                conversion = ctypes.c_uint(0)
                sentence = ctypes.c_uint(0)
                ok = imm32.ImmGetConversionStatus(
                    himc,
                    ctypes.byref(conversion),
                    ctypes.byref(sentence),
                )
                if ok and (conversion.value & _IME_CMODE_NATIVE):
                    return True
            except Exception:
                pass
        finally:
            imm32.ImmReleaseContext(hwnd, himc)
        return False
    except Exception:
        logging.debug("[abbrev] IME 偵測失敗", exc_info=True)
        return False


# 舊名稱相容（外部不應再用，但保留 import 不爆）
def is_ime_active_for_foreground() -> bool:
    return should_skip_for_input_method()


# -----------------------------------------------------------------------------
# Win32 剪貼簿（paste mode 用，避免逐字 keystroke race condition）
# -----------------------------------------------------------------------------
_CF_UNICODETEXT = 13
_GMEM_MOVEABLE = 0x0002


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
        if not user32.OpenClipboard(None):
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
    try:
        _ensure_win32_configured()
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        # 字串 + null terminator
        data = (text + "\x00").encode("utf-16-le")
        if not user32.OpenClipboard(None):
            return False
        try:
            user32.EmptyClipboard()
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
            # 注意：SetClipboardData 接管 h_mem 所有權；成功後勿 GlobalFree
            if not user32.SetClipboardData(_CF_UNICODETEXT, h_mem):
                return False
            return True
        finally:
            user32.CloseClipboard()
    except Exception:
        logging.debug("[abbrev] clipboard write 失敗", exc_info=True)
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
    "delete", "backspace",
}


class AbbrevEngine:
    """縮寫展開引擎。Thread-safe（hook callback 來自 keyboard 模組獨立 thread）。"""

    # 觸發後一次連送的 backspace 上限，純防呆。
    MAX_BACKSPACE = 64

    # 展開後的冷卻時間（s）— 期間 buffer 暫停累積，避免 user 連打第二組
    # 縮寫時後續 keystroke 跟我們的 paste 競態。配合 BlockInput + 原子
    # SendInput，0.55 秒已足夠涵蓋 paste 完成 + clipboard 還原。
    COOLDOWN_SEC = 0.55

    def __init__(self, kb_module: Any) -> None:
        """kb_module = `keyboard` PyPI 套件物件（已 import 完成）。"""
        self._kb = kb_module
        self._lock = threading.Lock()
        self._cfg: AbbrevConfig = AbbrevConfig()
        # abbrev (lower) → expansion 原文
        self._lookup: dict[str, str] = {}
        self._max_abbrev_len: int = 0
        self._buffer: str = ""
        self._hook_handle: Any = None
        self._suppressing = False
        # 展開後的冷卻截止時間（monotonic）
        self._cooldown_until: float = 0.0

    # ------------------------------------------------------------------ 公開 API
    def install(self, cfg: AbbrevConfig) -> None:
        """套用設定並掛上 keyboard hook。重複呼叫會先 uninstall 再裝。"""
        with self._lock:
            self._cfg = cfg
            self._rebuild_lookup_locked()
            self._buffer = ""
            self._uninstall_locked()
            if not cfg.enabled or not self._lookup:
                logging.info("[abbrev] hook 未掛載（enabled=%s, items=%d）",
                             cfg.enabled, len(self._lookup))
                return
            try:
                self._hook_handle = self._kb.on_press(self._on_press)
                logging.info("[abbrev] hook 已掛載，%d 筆縮寫（最長 %d 字）",
                             len(self._lookup), self._max_abbrev_len)
            except Exception:
                logging.exception("[abbrev] keyboard.on_press 掛載失敗")
                self._hook_handle = None

    def uninstall(self) -> None:
        with self._lock:
            self._uninstall_locked()

    def is_installed(self) -> bool:
        with self._lock:
            return self._hook_handle is not None

    # ----------------------------------------------------------------- 內部工具
    def _uninstall_locked(self) -> None:
        if self._hook_handle is None:
            return
        try:
            self._kb.unhook(self._hook_handle)
        except Exception:
            logging.debug("[abbrev] unhook 失敗", exc_info=True)
        self._hook_handle = None

    def _rebuild_lookup_locked(self) -> None:
        self._lookup = {}
        max_len = 0
        for it in self._cfg.items:
            abbrev = str(it.get("abbrev", "")).strip()
            if not abbrev:
                continue
            key = abbrev.lower()
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

    def _handle_event(self, event: Any) -> None:
        # 自己 send/write 期間，所有按鍵忽略
        if self._suppressing:
            return

        # cool-down 期間（展開剛完成）— 不更新 buffer、不觸發
        # 這能擋掉 paste 完成前 user 連打的後續 keystroke 進入 buffer 造成亂碼。
        if time.monotonic() < self._cooldown_until:
            return

        name = getattr(event, "name", None)
        if not name:
            return

        # trigger 鍵（空白）：嘗試展開
        if name in _TRIGGER_KEY_NAMES:
            trigger_char = " "
            buffer_snapshot = ""
            with self._lock:
                buffer_snapshot = self._buffer
                self._buffer = ""
            self._try_expand(buffer_snapshot, trigger_char)
            return

        # 重置 buffer 的鍵
        if name in _RESET_KEY_NAMES:
            with self._lock:
                self._buffer = ""
            return

        # printable 單字元（'a' 'B' '1' '/' '-' '_' 等）
        if len(name) == 1:
            ch = name.lower()
            with self._lock:
                self._buffer = (self._buffer + ch)[-self._max_abbrev_len:] if self._max_abbrev_len else ""
            return

        # 其他特殊鍵（shift / ctrl / alt / caps lock 等）—不影響 buffer
        return

    def _try_expand(self, buffer_snapshot: str, trigger_char: str) -> None:
        if not buffer_snapshot or not self._lookup:
            return

        # longest match from the right end of buffer
        matched_key: Optional[str] = None
        for length in range(min(self._max_abbrev_len, len(buffer_snapshot)), 0, -1):
            candidate = buffer_snapshot[-length:]
            if candidate in self._lookup:
                matched_key = candidate
                break
        if matched_key is None:
            return

        # IME / 中文輸入狀態 → 跳過（多重檢查：開啟旗標 / composition / conversion mode）
        if self._cfg.skip_when_ime_active and should_skip_for_input_method():
            logging.debug("[abbrev] 中文/IME 輸入中，跳過 '%s' 展開", matched_key)
            return

        raw_expansion = self._lookup[matched_key]
        try:
            rendered = render_expansion(raw_expansion, datetime.now())
        except Exception:
            logging.exception("[abbrev] render_expansion 失敗 abbrev=%s", matched_key)
            return

        if self._cfg.preserve_trailing_space:
            rendered = rendered + " "

        # 刪掉「縮寫 + 觸發字元」共 len(matched_key)+1 個字元
        delete_count = min(len(matched_key) + len(trigger_char), self.MAX_BACKSPACE)
        self._do_replace(delete_count, rendered, matched_key)

    def _do_replace(self, backspace_count: int, text: str, abbrev_key: str) -> None:
        """原子 SendInput：backspace × N + Ctrl+V 一次發送，期間 BlockInput
        凍結 user 真實輸入避免 race。

        為何需要 BlockInput：keyboard 模組 callback 在 worker thread 跑，
        user 連打下個縮寫的字元會在我們 SendInput 之前到達 focused window，
        造成「d00:27 」這種少刪 1-2 個 char 的 race condition。
        BlockInput 需要 admin 權限；非 admin 環境下退而只靠 cool-down。
        """
        kb = self._kb
        if kb is None:
            return

        self._suppressing = True
        # 進入「冷卻期」— 這段時間 buffer 暫停累積，避免 user 連打污染。
        self._cooldown_until = time.monotonic() + self.COOLDOWN_SEC

        old_clip: Optional[str] = None
        used_paste = False
        used_keystroke = False
        try:
            # 1. 備份 + 設剪貼簿（paste mode 首選）
            old_clip = _clipboard_get_text()
            clip_ok = _clipboard_set_text(text)

            if clip_ok:
                # 2a. 組原子事件序列：backspace × N + Ctrl + V + Ctrl up + V up
                events: list = []
                for _ in range(backspace_count):
                    events.append((_VK_BACK, True))
                    events.append((_VK_BACK, False))
                events.append((_VK_CONTROL, True))
                events.append((_VK_V, True))
                events.append((_VK_V, False))
                events.append((_VK_CONTROL, False))

                # 3a. BlockInput 凍結 user 輸入 → 原子 SendInput → 解凍
                user32 = ctypes.windll.user32
                blocked = False
                try:
                    blocked = bool(user32.BlockInput(True))
                except Exception:
                    logging.debug("[abbrev] BlockInput 不可用", exc_info=True)
                try:
                    used_paste = _send_atomic_keystrokes(events)
                finally:
                    if blocked:
                        try:
                            user32.BlockInput(False)
                        except Exception:
                            pass
                # 4a. 等 OS dispatch + target app 處理 paste 完成
                time.sleep(0.12)
            else:
                # 2b. fallback: 剪貼簿寫入失敗 → 用 keyboard.send/write 老路
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
            # 還原剪貼簿（即使失敗也試）
            if old_clip is not None:
                try:
                    _clipboard_set_text(old_clip)
                except Exception:
                    logging.debug("[abbrev] 還原剪貼簿失敗", exc_info=True)

            mode = "atomic-paste" if used_paste else ("keystroke" if used_keystroke else "FAIL")
            logging.info("[abbrev] 展開 '%s' → %d 字 (%s)",
                         abbrev_key, len(text), mode)

            # cool-down 期滿後才清 suppress 旗標 + buffer
            def _clear():
                self._suppressing = False
                with self._lock:
                    self._buffer = ""
            t = threading.Timer(self.COOLDOWN_SEC, _clear)
            t.daemon = True
            t.start()
