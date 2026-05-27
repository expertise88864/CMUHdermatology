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
            "患者因上述皮膚疾病，於2026年5月28日至本院皮膚科門診就醫治療，"
            "後續接受局部麻醉下皮膚腫瘤切除手術及縫合，"
            "術後病理檢查結果合乎上述疾患。"
            "患者於da返回本院皮膚科門診接受術後照護並拆除手術縫線。"
        ),
    },
    {
        "abbrev": "cert2",
        "expansion": (
            "患者因上述皮膚疾病，曾於da-21至本院皮膚科門診就醫，"
            "後續於da-17接受局部麻醉下之皮膚腫瘤切除及縫合手術，"
            "術後病理檢查結果符合上述疾患。"
            "患者於術後之da-14返回本院皮膚科門診接受照護，"
            "並分別於da-7及da分次拆除手術縫線。"
        ),
    },
]


DEFAULT_CONFIG: dict[str, Any] = {
    "enabled": False,
    "skip_when_ime_active": True,
    "preserve_trailing_space": True,
    "items": DEFAULT_ITEMS,
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


def load_config(path: str) -> AbbrevConfig:
    """讀取設定，缺檔/壞檔自動回 defaults。"""
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
    return AbbrevConfig(
        enabled=bool(raw.get("enabled", False)),
        skip_when_ime_active=bool(raw.get("skip_when_ime_active", True)),
        preserve_trailing_space=bool(raw.get("preserve_trailing_space", True)),
        items=cleaned,
    )


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
# 比對：da+N、da-N、da1、da2、da。前後皆非 ASCII alnum。
# 注意：較長的 alternative 寫在前面，re 才會 greedy 抓到 da+N / da[12] 而不是只抓 da。
_TOKEN_RE = re.compile(r'(?<![A-Za-z0-9])(da[+-]\d+|da[12]|da)(?![A-Za-z0-9])')


def _fmt_date_slash(d: datetime) -> str:
    """2026/5/27（無 zero-pad）"""
    return f"{d.year}/{d.month}/{d.day}"


def _fmt_time_hhmm(d: datetime) -> str:
    """23:34"""
    return f"{d.hour:02d}:{d.minute:02d}"


def render_expansion(template: str, now: Optional[datetime] = None) -> str:
    """把 template 內的 da / da1 / da2 / da±N tokens 替換為實際日期/時間字串。

    - da    → (2026/5/27)
    - da1   → 23:34
    - da2   → (2026/5/27) 23:34
    - da+N  → (2026/M/D) 今日+N 天
    - da-N  → (2026/M/D) 今日-N 天
    """
    if now is None:
        now = datetime.now()
    today = now.date()

    def repl(m: re.Match) -> str:
        tok = m.group(1)
        if tok == "da":
            return f"({_fmt_date_slash(now)})"
        if tok == "da1":
            return _fmt_time_hhmm(now)
        if tok == "da2":
            return f"({_fmt_date_slash(now)}) {_fmt_time_hhmm(now)}"
        m2 = re.match(r"da([+-])(\d+)", tok)
        if m2:
            sign, n = m2.group(1), int(m2.group(2))
            delta = n if sign == "+" else -n
            target = now + timedelta(days=delta)
            return f"({_fmt_date_slash(target)})"
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

    用三重檢查（任一條件成立就視為中文輸入中）：
      1. ImmGetOpenStatus：IME 開啟旗標
      2. ImmGetCompositionString：是否有 composition string in progress
      3. ImmGetConversionStatus：conversion mode 是否含 IME_CMODE_NATIVE

    舊 IMM IME（傳統注音）走 1；新 TSF IME（新注音、Google 注音）
    對 1 可能不更新，但 2/3 通常還能反映。失敗則回 False（fail-open）。
    """
    try:
        user32 = ctypes.windll.user32
        imm32 = ctypes.windll.imm32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        himc = imm32.ImmGetContext(hwnd)
        if not himc:
            return False
        try:
            # 1. IME 開啟旗標
            try:
                if imm32.ImmGetOpenStatus(himc):
                    return True
            except Exception:
                pass
            # 2. 有 composition string 在組字
            try:
                size = imm32.ImmGetCompositionStringW(himc, _GCS_COMPSTR, None, 0)
                if isinstance(size, int) and size > 0:
                    return True
            except Exception:
                pass
            # 3. conversion mode 含 NATIVE (中文模式)
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
    # 縮寫時，後續 keystroke 跟我們的 paste 競態，造成串接型亂碼。
    COOLDOWN_SEC = 0.40

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
        """送 N 個 backspace + 用剪貼簿 paste（Ctrl+V）寫出展開內容。

        為何用 paste：原本逐字 `keyboard.write(text)` 對長字串會送出幾十個
        OS keystroke，user 在這段期間若繼續打下個縮寫（連打 nev1 nev1 ），
        OS event queue 會把 user 的 keystroke 跟我們的 backspace/write
        交錯，造成輸出字串混亂。改 paste 後只送 ~3 個 OS event
        （Ctrl 下、V 下、Ctrl V 放開），race window 從 100-200ms 縮到 ~20ms。
        """
        kb = self._kb
        if kb is None:
            return
        self._suppressing = True
        # 進入「冷卻期」— 這段時間 buffer 暫停累積，避免 user 連打污染。
        self._cooldown_until = time.monotonic() + self.COOLDOWN_SEC
        try:
            # 1. 刪掉「縮寫 + trigger char」
            for _ in range(backspace_count):
                try:
                    kb.send("backspace")
                except Exception:
                    logging.debug("[abbrev] send backspace 失敗", exc_info=True)
                    break

            # 2. paste 寫出展開內容（fallback 到 keyboard.write）
            paste_ok = self._paste_via_clipboard(text)
            if not paste_ok:
                logging.warning("[abbrev] paste 失敗，fallback 用 keystroke")
                try:
                    kb.write(text)
                except Exception:
                    logging.exception("[abbrev] keyboard.write fallback 也失敗")

            logging.info("[abbrev] 展開 '%s' → %d 字 (%s)",
                         abbrev_key, len(text),
                         "paste" if paste_ok else "keystroke")
        finally:
            # cool-down 期滿後才清 suppress 旗標 + buffer
            def _clear():
                self._suppressing = False
                with self._lock:
                    self._buffer = ""
            t = threading.Timer(self.COOLDOWN_SEC, _clear)
            t.daemon = True
            t.start()

    def _paste_via_clipboard(self, text: str) -> bool:
        """備份 → 設 clip → Ctrl+V → 等待 → 還原 clip。任何步驟失敗回 False。"""
        kb = self._kb
        if kb is None:
            return False
        old_clip: Optional[str] = None
        try:
            old_clip = _clipboard_get_text()
            if not _clipboard_set_text(text):
                return False
            # 等剪貼簿落地（避免 Ctrl+V 拿到舊內容）
            time.sleep(0.04)
            try:
                kb.send("ctrl+v")
            except Exception:
                logging.debug("[abbrev] send ctrl+v 失敗", exc_info=True)
                return False
            # 等 OS paste 完成（時間取決於 focused window）
            time.sleep(0.10)
            return True
        except Exception:
            logging.exception("[abbrev] paste 流程例外")
            return False
        finally:
            # 不論成功失敗都試圖還原剪貼簿
            if old_clip is not None:
                try:
                    _clipboard_set_text(old_clip)
                except Exception:
                    logging.debug("[abbrev] 還原剪貼簿失敗", exc_info=True)
