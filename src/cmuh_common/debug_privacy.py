# -*- coding: utf-8 -*-
"""除錯檔的隱私處理（P1-02）。

★[2026-07-30 第二輪外審 P1-02] 為什麼要有這個模組★
打卡失敗時會落地三種檔案到 `settings/debug_dumps/`，每一種都可能含機敏資料：

  * **檔名**：`f"{task_label}_{username}"` —— 帳號直接寫在檔名上。檔名還會被放進
    Windows 通知（`notify_clock_failure`），以及任何看得到那個資料夾的人眼裡。
  * **page_source HTML**：整頁原始碼。登入頁的帳號欄 value、打卡紀錄表格、
    以及院方頁面上任何東西都在裡面。
  * **screenshot**：登入頁截圖會把【帳號欄的明文】拍進去（密碼欄是圓點，
    但帳號不是）。

TTL 那一半已經在 v2026.07.30.4 做掉（見 `cmuh_common/retention.py`）——
但「三天後會刪」不等於「這三天可以隨便存」。這個模組處理的是【存什麼】。

★設計原則★

1. **預設不存 HTML**。要看整頁原始碼是少數狀況，讓需要的人自己打開；
   預設值站在隱私那邊。開關寫在設定檔，改了不必改程式。
2. **檔名用不可逆的短雜湊**，不是帳號本身。仍然能分辨「這批檔是同一個帳號的」，
   但看檔名的人得不到帳號。加了固定 salt，避免拿常見帳號字典反推。
3. **截圖前把憑證欄位清空，而且要【回讀確認】**。清完再讀一次確認真的空了，
   確認不了就【不存這張截圖】並把原因寫進 meta —— 這是 fail-closed：
   我們無法證明它安全，就不要落地。
4. **目錄權限收斂**（Windows ACL）。同一台電腦的其他使用者不該讀得到。
   失敗只記 log：權限收不緊不該讓打卡整個停擺。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
from datetime import datetime

from cmuh_common.atomic_io import atomic_write_json, safe_load_json_ex
from cmuh_common.paths import get_conf_path

# ★[外審第四輪 R4-P3-02] 這裡原本寫著一句不成立的密碼學宣稱★
#   原文:「固定 salt:讓雜湊不可用常見帳號字典反推」。那是錯的 ——
#   這個 salt 是【公開 repo 裡的固定常數】,任何人都算得出
#   `SHA256(salt + 候選帳號)` 再比前 8 個字元。固定 salt 擋得住的只有
#   ★事先建好的通用彩虹表★,擋不住「已知 salt + 帳號候選空間很小」的字典比對。
#   而院內帳號的候選空間確實很小(員工代號)。
# ★它實際上是什麼★:一個【穩定的假名】—— 讓檔名不出現帳號本身
#   (檔名會出現在 Windows 通知、資料夾清單、任何截圖裡的檔案總管視窗),
#   同時讓除錯的人分得出「這批檔是同一個人的」。這個價值是真的,
#   而且遠比把帳號直接寫進檔名安全;不成立的是「不可反推」那一句。
# ★★使用者定案(2026-09-02):不做硬化,勿再提議★★
#   要做到真的無法離線枚舉,需要【每台機器一個隨機祕密】+ HMAC-SHA256
#   (祕密可用本 repo 既有的 `dpapi_seal` 保存),代號也要拉到 12-16 字元。
#   使用者評估後決定★不做★ —— 這個代號的價值本來就只是「帳號不進檔名」,
#   而那個價值現在就成立;硬化的代價是既有檔名分不到同一群。
#   本條只更正宣稱、不動行為。日後審查再點到這裡,請引用這一行。
_ACCOUNT_SALT = "cmuh-derm-debug-dump-v1"
ACCOUNT_TAG_LEN = 8

PRIVACY_FILENAME = "debug_dump_privacy.json"
# ★預設站在隱私那一邊★ 要看整頁原始碼是少數狀況，需要的人自己打開。
DEFAULT_PRIVACY = {"store_page_source": False}


def _privacy_path(path=None) -> str:
    return str(path) if path else get_conf_path(PRIVACY_FILENAME)


def load_privacy_settings(path=None) -> dict:
    """讀隱私設定。★讀不到一律回「不存 HTML」★

    這裡刻意【不】沿用「暫時讀不到就用上次成功值」的做法：那個模式是為了不要讓
    寄信/告警靜默停擺（少做事＝壞事）。這裡相反 —— 讀不到時多做事（落地整頁原始碼）
    才是壞事，所以退回預設就是最安全的方向。
    """
    cfg = dict(DEFAULT_PRIVACY)
    p = _privacy_path(path)
    if not os.path.exists(p):
        return cfg
    saved, status = safe_load_json_ex(p, default={}, backup_on_corrupt=False)
    if status not in ("ok", "missing"):
        logging.warning("[除錯檔] 隱私設定讀取失敗(%s)→ 退回預設（不存整頁原始碼）：%s",
                        status, p)
        return cfg
    if isinstance(saved, dict):
        if saved.get(SANITIZED_SINCE_KEY):
            cfg[SANITIZED_SINCE_KEY] = str(saved[SANITIZED_SINCE_KEY])
        raw = saved.get("store_page_source", False)
        # ★[外審第 1 輪] 只有字面 `true` 才算開啟★
        #   `bool("false")` 是 True。舊版直接 bool()，於是使用者手改成
        #   `"store_page_source": "false"` 或 `0`/`1` 這種看起來是關的值，
        #   反而把整頁原始碼打開了 —— 正好違反 fail-closed 的初衷。
        if raw is True:
            cfg["store_page_source"] = True
        elif raw not in (False, None):
            logging.warning(
                "[除錯檔] store_page_source 的值 %r 不是布林值 → 視為關閉"
                "（要開啟請寫字面 true）", raw)
    return cfg


def save_privacy_settings(cfg: dict, path=None) -> bool:
    try:
        out = {"store_page_source": cfg.get("store_page_source") is True}
        # ★分界線不可被「存一下開關」洗掉★
        #   洗掉就等於回到「不知道哪段安全」，診斷包會整份 log 不收錄。
        #   在這裡【自己從磁碟補回來】，而不是要求每個呼叫端都記得帶著它 ——
        #   「UI 只想存一個開關」是最自然的寫法，不該因此損失分界線。
        since = cfg.get(SANITIZED_SINCE_KEY)
        if not since:
            existing, status = safe_load_json_ex(_privacy_path(path), default={},
                                                 backup_on_corrupt=False)
            if status in ("ok", "missing") and isinstance(existing, dict):
                since = existing.get(SANITIZED_SINCE_KEY)
        if since:
            out[SANITIZED_SINCE_KEY] = str(since)
        atomic_write_json(_privacy_path(path), out, indent=2)
        return True
    except Exception:
        logging.warning("[除錯檔] 隱私設定寫入失敗", exc_info=True)
        return False


def store_page_source_enabled(path=None) -> bool:
    return bool(load_privacy_settings(path).get("store_page_source"))


def account_tag(username) -> str:
    """帳號的★穩定假名★短代號，給檔名用。

    ★不可把帳號本身放進檔名★ 檔名會出現在 Windows 通知、資料夾清單、
    以及任何截圖裡的檔案總管視窗。

    ★不是「不可反推」★(外審第四輪 R4-P3-02):salt 是公開常數,
    知道 salt 的人可以對候選帳號逐一比對。詳見 `_ACCOUNT_SALT` 上方的說明 ——
    宣稱只能講到實際做得到的程度。
    """
    raw = str(username or "").strip().lower()
    if not raw:
        return "anon"
    digest = hashlib.sha256((_ACCOUNT_SALT + raw).encode("utf-8")).hexdigest()
    return digest[:ACCOUNT_TAG_LEN]


# ─── 憑證欄位清空 + 回讀 ───────────────────────────────────────────────────
# 清空用的 JS：把所有 password 欄與指定 id 的欄位清掉，回傳「清完之後還剩幾個
# 非空的」。回傳 0 才算成功 —— 這就是回讀（不是「我送出去了」）。
_CLEAR_AND_VERIFY_JS = """
var ids = arguments[0] || [];
var els = [];
var pw = document.querySelectorAll('input[type=password]');
for (var i = 0; i < pw.length; i++) { els.push(pw[i]); }
for (var j = 0; j < ids.length; j++) {
  var e = document.getElementById(ids[j]);
  if (e) { els.push(e); }
}
var remaining = 0;
for (var k = 0; k < els.length; k++) {
  try {
    els[k].value = '';
    els[k].setAttribute('value', '');
  } catch (err) { /* 讀不到/唯讀 → 下面回讀會抓到 */ }
}
for (var m = 0; m < els.length; m++) {
  if (els[m].value) { remaining += 1; }
}
return remaining;
"""


def blank_credential_fields(driver, field_ids=()) -> bool:
    """截圖前清空帳號／密碼欄，並【回讀確認真的空了】。

    回 True＝確認全部清空（可以安全截圖）；False＝清不掉或問不到（不要截圖）。

    ★為什麼是清空而不是畫遮罩★
    遮罩是「我在上面蓋了東西」——蓋歪了、沒蓋到、被 z-index 壓過去，都無從得知。
    清空可以【回讀】：再問一次 value 是不是空的。這支 repo 的老病灶就是
    「送出去就當成功」，遮罩正是那種做法。

    ★頁面上根本沒有這些欄位時回 True★（例如打卡結果頁）——沒有東西要清，
    截圖本來就不含憑證。
    """
    if driver is None:
        return False
    try:
        remaining = driver.execute_script(_CLEAR_AND_VERIFY_JS, list(field_ids))
    except Exception:
        logging.warning("[除錯檔] 清空憑證欄位失敗 → 本次不存截圖", exc_info=True)
        return False
    try:
        remaining = int(remaining)
    except (TypeError, ValueError):
        logging.warning("[除錯檔] 憑證欄位回讀結果無法判讀(%r) → 本次不存截圖",
                        remaining)
        return False
    if remaining:
        logging.warning("[除錯檔] 仍有 %d 個憑證欄位清不掉 → 本次不存截圖", remaining)
        return False
    return True


# ─── 目錄權限 ──────────────────────────────────────────────────────────────
def restrict_dir_to_current_user(directory) -> bool:
    """把資料夾權限收斂成「只有目前使用者」（Windows ACL）。回傳是否成功。

    ★失敗只記 log★：權限收不緊不該讓打卡整個停擺（這是縱深防禦的一層，
    不是唯一一層 —— 預設不存 HTML、憑證清空、TTL 才是主線）。
    """
    path = str(directory)
    if not os.path.isdir(path):
        return False
    user = os.environ.get("USERNAME") or ""
    if not user:
        logging.debug("[除錯檔] 取不到 USERNAME，略過 ACL 收斂")
        return False
    try:
        cp = subprocess.run(
            ["icacls", path, "/inheritance:r",
             "/grant:r", f"{user}:(OI)(CI)F"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            capture_output=True, timeout=20, check=False)
    except Exception:
        logging.debug("[除錯檔] icacls 執行失敗（略過）", exc_info=True)
        return False
    if cp.returncode != 0:
        logging.debug("[除錯檔] icacls 回 %s（略過）：%r", cp.returncode,
                      (cp.stderr or cp.stdout or b"")[:200])
        return False
    return True


# ─── 一鍵刪除 ──────────────────────────────────────────────────────────────
def purge_dir(directory) -> tuple:
    """刪掉資料夾內所有檔案。回 (刪掉幾個, 刪不掉幾個)。★絕不拋例外★

    只刪檔、不刪資料夾本身（下次要用時不必再處理權限）。
    """
    path = str(directory)
    gone = failed = 0
    try:
        names = os.listdir(path)
    except OSError:
        return (0, 0)
    for name in names:
        full = os.path.join(path, name)
        try:
            if os.path.isfile(full):
                os.remove(full)
                gone += 1
        except OSError:
            failed += 1
    return (gone, failed)


SANITIZED_SINCE_KEY = "sanitized_logging_since"


class RedactingFormatter(logging.Formatter):
    """把已知帳號在【格式化成檔案的那一行文字時】換成佔位符。

    ★[2026-07-30 外審 P1-02 第 2 輪] 為什麼是 Formatter 而不是 Filter★
    我上一版用 `logging.Filter` 改寫 `record.msg`，並宣稱「只掛在檔案 handler 上，
    UI 不受影響」—— **那是錯的**：`logging` 把【同一個 LogRecord 物件】傳給每一個
    handler，filter 一改就全部都看到 `<redacted>`，UI 的即時記錄窗與 console 也一起
    被遮掉，多帳號失敗時根本分不出是哪個帳號。
    Formatter 只產生「這個 handler 要寫出去的那個字串」，不碰共用的 record —— 這才
    真的做得到「檔案遮、畫面不遮」。
    """

    def __init__(self, fmt=None, datefmt=None) -> None:
        super().__init__(fmt, datefmt)
        self._secrets: set = set()

    def add_secret(self, value) -> None:
        raw = str(value or "").strip()
        if len(raw) >= 4:
            self._secrets.add(raw)

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        if not self._secrets:
            return text
        return redact_secrets(text, self._secrets)


_file_formatter = RedactingFormatter("%(asctime)s - %(levelname)s - %(message)s")


def _is_file_handler(h) -> bool:
    return hasattr(h, "baseFilename")


def install_log_secret_filter(*secrets) -> RedactingFormatter:
    """把帳號加進「不可寫進 log 檔」清單，並確保檔案 handler 用遮蔽 formatter。

    同時記下【從什麼時候開始 log 才是乾淨的】（見 `sanitized_logging_since`）——
    升級前既有的 log 仍含帳號，診斷包必須知道那條分界線。
    """
    for sec in secrets:
        _file_formatter.add_secret(sec)
    installed = False
    for h in logging.getLogger().handlers:
        if _is_file_handler(h) and h.formatter is not _file_formatter:
            h.setFormatter(_file_formatter)
            installed = True
    if installed:
        _mark_sanitized_logging_started()
    return _file_formatter


def _mark_sanitized_logging_started(path=None) -> None:
    """第一次啟用遮蔽時記下時間；已經記過就不動（分界線只能有一條）。"""
    try:
        cfg = load_privacy_settings(path)
        if cfg.get(SANITIZED_SINCE_KEY):
            return
        cfg[SANITIZED_SINCE_KEY] = datetime.now().isoformat(timespec="seconds")
        save_privacy_settings(cfg, path)
        logging.info("[除錯檔] 帳號遮蔽已啟用；此時間點【之前】的 log 仍含帳號，"
                     "診斷包不會收錄那一段")
    except Exception:
        logging.debug("[除錯檔] 記錄遮蔽起始時間失敗（略過）", exc_info=True)


def sanitized_logging_since(path=None):
    """log 從什麼時候開始不含帳號（datetime）。沒有紀錄回 None。"""
    raw = load_privacy_settings(path).get(SANITIZED_SINCE_KEY)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")


def log_lines_after(text: str, since):
    """只留下【分界線之後】寫的 log 行。

    ★[外審第 2 輪] 為什麼需要這個★
    遮蔽 formatter 只保護「之後」寫的紀錄。升級前既有的 `autoclock.log` 仍含帳號，
    使用者一升級就產生診斷包，那份「安全」的 zip 裡就有帳號 —— 而且那個帳號可能
    早就刪掉了，寄出前才遮的做法根本不知道它存在。

    沒有分界線（從沒啟用過遮蔽）→ 回空字串：寧可少給，不可外洩。
    沒有時間戳的行（traceback 續行）沿用上一行的判斷。
    """
    if since is None:
        return ""
    kept, keep = [], False
    for line in text.splitlines(keepends=True):
        m = _TS_RE.match(line)
        if m:
            try:
                keep = datetime.fromisoformat(
                    m.group(1).replace(" ", "T")) >= since
            except ValueError:
                keep = False
        if keep:
            kept.append(line)
    return "".join(kept)


def build_safe_diag_bundle(dest_zip, log_files=(), meta_dir=None,
                           secrets=(), max_log_bytes=2_000_000) -> tuple:
    """打包一份【可以安全寄給開發者】的診斷包。回 (放進去幾個檔, 說明字串)。

    ★為什麼需要這個★
    「預設不存整頁原始碼」把隱私風險拿掉了，代價是出事時能給的東西變少。若沒有一個
    安全的替代品，使用者遇到問題時就會被迫（或被要求）去開那個開關、或直接把含帳號的
    截圖整包寄出來 —— 那等於把預設值的保護繞過去。

    ★包什麼、不包什麼★
      放：log 檔（尾端 `max_log_bytes`，已知機敏字串取代掉）、除錯 meta 的 `.txt`。
      不放：`.png` 截圖、`.html` 整頁原始碼 —— 那正是含帳號與病人資料的兩種。
    """
    import zipfile

    added = 0
    skipped_kinds: set = set()
    skipped_legacy_log = truncated_legacy = False
    sanitized_since = sanitized_logging_since()
    try:
        with zipfile.ZipFile(str(dest_zip), "w", zipfile.ZIP_DEFLATED) as zf:
            for lf in log_files:
                try:
                    if not os.path.isfile(lf):
                        continue
                    size = os.path.getsize(lf)
                    with open(lf, "rb") as fh:
                        if size > max_log_bytes:
                            fh.seek(size - max_log_bytes)
                        raw = fh.read()
                    text = redact_secrets(raw.decode("utf-8", "replace"), secrets)
                    # ★[外審第 2 輪] 只收錄【遮蔽啟用之後】寫的那一段★
                    #   升級前既有的 log 仍含帳號，而那個帳號可能早就刪掉了 ——
                    #   寄出前才遮的做法根本不知道它存在。
                    kept = log_lines_after(text, sanitized_since)
                    if not kept.strip():
                        skipped_legacy_log = True
                        continue
                    if len(kept) != len(text):
                        truncated_legacy = True
                    zf.writestr(f"logs/{os.path.basename(lf)}", kept)
                    added += 1
                except OSError:
                    logging.debug("[診斷包] 讀不到 %s（略過）", lf, exc_info=True)
            if meta_dir and os.path.isdir(meta_dir):
                for name in sorted(os.listdir(meta_dir)):
                    full = os.path.join(meta_dir, name)
                    if not os.path.isfile(full):
                        continue
                    if not name.lower().endswith(".txt"):
                        # ★.png / .html 一律不進診斷包★（含帳號與病人資料）
                        skipped_kinds.add(os.path.splitext(name)[1].lower() or "?")
                        continue
                    try:
                        text = redact_secrets(
                            open(full, encoding="utf-8", errors="replace").read(),
                            secrets)
                        zf.writestr(f"debug_meta/{name}", text)
                        added += 1
                    except OSError:
                        logging.debug("[診斷包] 讀不到 %s（略過）", full,
                                      exc_info=True)
    except Exception:
        logging.warning("[診斷包] 建立失敗", exc_info=True)
        return (0, "診斷包建立失敗，請看 log")
    note = f"已收錄 {added} 個檔案"
    if skipped_kinds:
        note += ("；未收錄 " + "、".join(sorted(skipped_kinds))
                 + "（截圖與整頁原始碼含帳號/病人資料，刻意不放）")
    if skipped_legacy_log:
        note += ("；【整份 log 未收錄】—— 它是帳號遮蔽啟用之前寫的，"
                 "仍含帳號。重啟打卡程式並讓問題再發生一次就會有安全的紀錄。")
    elif truncated_legacy:
        note += "；log 只收錄【帳號遮蔽啟用之後】的那一段"
    return (added, note)


def redact_secrets(text, secrets=()) -> str:
    """把已知的機敏字串（帳號、密碼）從文字裡換成佔位符。

    ★這是【allowlist 的反面，但用的是「我確知的值」而不是猜測的樣式】★
    外審 P2-03 批評的是「用 denylist regex 猜什麼像個資」；這裡不猜 —— 呼叫端手上
    就有那個帳號/密碼的實際值，直接比對取代。短字串（<4）不取代：那種長度會誤傷
    一般文字（例如帳號是 "abc" 就會把錯誤訊息裡的每個 abc 都換掉，反而讓訊息難讀）。

    只處理【落地成檔案】的文字（除錯 meta）。log 另有既有機制。
    """
    out = str(text or "")
    for secret in secrets:
        raw = str(secret or "")
        if len(raw) < 4:
            continue
        out = out.replace(raw, "<redacted>")
        # 帳號大小寫可能不一致（使用者輸入 vs 系統回覆）
        if raw.lower() != raw:
            out = out.replace(raw.lower(), "<redacted>")
        if raw.upper() != raw:
            out = out.replace(raw.upper(), "<redacted>")
    return out
