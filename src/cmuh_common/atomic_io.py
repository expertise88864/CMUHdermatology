# -*- coding: utf-8 -*-
"""原子寫入工具 + corruption-safe JSON 載入。

- atomic_write_json: 先寫 .tmp 再 os.replace，斷電時原檔不變空。
- atomic_write_text: 含 .bak 備份的文字寫入。
- safe_load_json: corrupt JSON → 自動 backup 壞檔到 .corrupt-<ts> + fallback default。
"""
import json
import logging
import os
import tempfile
import time


_FILE_OP_RETRY_DELAYS_SEC = (0.05, 0.15, 0.35)


def _file_op_with_retry(label: str, func, *args):
    """Retry transient Windows file locks for small atomic file operations."""
    last_exc = None
    total_attempts = len(_FILE_OP_RETRY_DELAYS_SEC) + 1
    for attempt in range(total_attempts):
        try:
            return func(*args)
        except OSError as e:
            last_exc = e
            if attempt >= len(_FILE_OP_RETRY_DELAYS_SEC):
                break
            delay = _FILE_OP_RETRY_DELAYS_SEC[attempt]
            logging.debug(
                "[atomic_io] %s failed (%s), retry %d/%d in %.2fs",
                label, e, attempt + 2, total_attempts, delay,
            )
            time.sleep(delay)
    raise last_exc


def _replace_with_retry(src: str, dst: str) -> None:
    _file_op_with_retry(f"replace {src} -> {dst}", os.replace, src, dst)


def _copy2_with_retry(src: str, dst: str) -> None:
    import shutil
    _file_op_with_retry(f"copy {src} -> {dst}", shutil.copy2, src, dst)


def _remove_with_retry(path: str) -> None:
    _file_op_with_retry(f"remove {path}", os.remove, path)


def _flush_and_fsync(f) -> None:
    """Flush file content to disk before os.replace."""
    f.flush()
    os.fsync(f.fileno())


def _make_temp_path(target_path: str) -> tuple[int, str]:
    target_dir = os.path.dirname(os.path.abspath(target_path)) or "."
    os.makedirs(target_dir, exist_ok=True)
    base = os.path.basename(target_path)
    return tempfile.mkstemp(prefix=f".{base}.", suffix=".tmp", dir=target_dir)


def _next_corrupt_backup_path(file_path: str, timestamp: str) -> str:
    """Return a non-conflicting corrupt backup path."""
    candidate = f"{file_path}.corrupt-{timestamp}"
    suffix = 1
    while os.path.exists(candidate):
        candidate = f"{file_path}.corrupt-{timestamp}-{suffix}"
        suffix += 1
    return candidate


def atomic_write_json(file_path: str, data, **kwargs) -> None:
    """JSON 原子寫入。kwargs 會傳給 json.dump（如 default=...）。"""
    fd = -1
    tmp_path = ""
    try:
        fd, tmp_path = _make_temp_path(file_path)
        dump_kwargs = {"ensure_ascii": False, "indent": 4}
        dump_kwargs.update(kwargs)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            fd = -1
            json.dump(data, f, **dump_kwargs)
            _flush_and_fsync(f)
        _replace_with_retry(tmp_path, file_path)
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except Exception:
                pass
        if tmp_path and os.path.exists(tmp_path):
            try:
                _remove_with_retry(tmp_path)
            except Exception:
                logging.debug("atomic_write_json: 移除 tmp 失敗", exc_info=True)
        raise


def safe_load_json_ex(file_path: str, default=None, *,
                      backup_on_corrupt: bool = True):
    """同 safe_load_json，但額外回傳「載入狀態」以便呼叫端決策。回 (value, status)：

      "ok"      正常載入
      "missing" 檔案不存在（回 default）
      "corrupt" JSON/編碼損壞——已 backup 壞檔並回 default（原檔已被 rename 移走）
      "error"   OSError/PermissionError 等暫時性失敗（回 default；**原檔通常仍完好**）

    用途（AB-04）：呼叫端可據 status 決定「是否可用預設值覆寫原檔」——missing/corrupt
    可（原檔已不存在/已移走），但 "error" **不可**（只是暫時被防毒/備份軟體鎖住，覆寫
    會把使用者的好檔毀成預設）。
    """
    if not os.path.exists(file_path):
        return default, "missing"
    try:
        # [IF-02] 用 utf-8-sig 讀:容忍記事本另存 UTF-8 時加的 BOM(否則 json.load 直接 JSONDecodeError
        # → 被當 corrupt)。utf-8-sig 對「無 BOM 的純 utf-8」行為與 utf-8 完全一致,向後相容、無副作用。
        with open(file_path, 'r', encoding='utf-8-sig') as f:
            return json.load(f), "ok"
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logging.warning("[safe_load_json] %s 內容損壞 (%s): %s",
                          file_path, type(e).__name__, e)
        if backup_on_corrupt:
            try:
                ts = time.strftime("%Y%m%d_%H%M%S")
                bak = _next_corrupt_backup_path(file_path, ts)
                _replace_with_retry(file_path, bak)
                logging.warning("[safe_load_json] 已 backup 壞檔到 %s", bak)
            except Exception:
                logging.debug("[safe_load_json] backup 壞檔失敗", exc_info=True)
        return default, "corrupt"
    except (PermissionError, OSError) as e:
        logging.warning("[safe_load_json] %s 讀取失敗 (%s)", file_path, e)
        return default, "error"
    except Exception:
        logging.exception("[safe_load_json] %s 未預期例外", file_path)
        return default, "error"


def safe_load_json(file_path: str, default=None, *,
                    backup_on_corrupt: bool = True):
    """讀 JSON，corrupt 自動 backup 壞檔 + log warning + 回 default。

    使用：
        cfg = safe_load_json('settings.json', default={"enabled": True})

    處理的錯誤：
      - FileNotFoundError → 回 default (不視為錯誤)
      - json.JSONDecodeError → backup 壞檔 → log warning → 回 default
      - UnicodeDecodeError → 同上 (檔案不是 UTF-8，可能被改壞)
      - PermissionError / OSError → log warning → 回 default
      - 其他例外 → log error → 回 default

    backup_on_corrupt=True 時，壞檔會 rename 成 `<file_path>.corrupt-<timestamp>`，
    方便事後 forensic / 手動還原。需要區分失敗原因（暫時鎖住 vs 損壞）請改用
    safe_load_json_ex（契約向後相容，本函式只是丟掉 status）。
    """
    value, _status = safe_load_json_ex(
        file_path, default, backup_on_corrupt=backup_on_corrupt)
    return value


def atomic_write_text(file_path: str, content: str, encoding: str = 'utf-8') -> bool:
    """文字檔原子寫入（含 .bak 備份）。
    搬自原主程式 _safe_write (line 8650-8670)，用於線上更新覆寫程式碼檔。
    """
    backup = file_path + '.bak'
    fd = -1
    tmp = ""
    try:
        target_dir = os.path.dirname(file_path) or '.'
        os.makedirs(target_dir, exist_ok=True)
        if os.path.exists(file_path):
            _copy2_with_retry(file_path, backup)
        fd, tmp = _make_temp_path(file_path)
        with os.fdopen(fd, 'w', encoding=encoding) as f:
            fd = -1
            f.write(content)
            _flush_and_fsync(f)
        _replace_with_retry(tmp, file_path)
        return True
    except Exception as e:
        logging.error("atomic_write_text 失敗 [%s]: %s", file_path, e)
        if fd >= 0:
            try:
                os.close(fd)
            except Exception:
                pass
        if tmp and os.path.exists(tmp):
            try:
                _remove_with_retry(tmp)
            except OSError:
                logging.debug("移除 tmp 失敗", exc_info=True)
        return False
