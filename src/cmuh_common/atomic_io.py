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


def _flush_and_fsync(f) -> None:
    """Flush file content to disk before os.replace."""
    f.flush()
    os.fsync(f.fileno())


def _make_temp_path(target_path: str) -> tuple[int, str]:
    target_dir = os.path.dirname(os.path.abspath(target_path)) or "."
    os.makedirs(target_dir, exist_ok=True)
    base = os.path.basename(target_path)
    return tempfile.mkstemp(prefix=f".{base}.", suffix=".tmp", dir=target_dir)


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
        os.replace(tmp_path, file_path)
    except Exception:
        if fd >= 0:
            try:
                os.close(fd)
            except Exception:
                pass
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                logging.debug("atomic_write_json: 移除 tmp 失敗", exc_info=True)
        raise


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
    方便事後 forensic / 手動還原。
    """
    if not os.path.exists(file_path):
        return default
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        logging.warning("[safe_load_json] %s 內容損壞 (%s): %s",
                          file_path, type(e).__name__, e)
        if backup_on_corrupt:
            try:
                ts = time.strftime("%Y%m%d_%H%M%S")
                bak = f"{file_path}.corrupt-{ts}"
                os.replace(file_path, bak)
                logging.warning("[safe_load_json] 已 backup 壞檔到 %s", bak)
            except Exception:
                logging.debug("[safe_load_json] backup 壞檔失敗", exc_info=True)
        return default
    except (PermissionError, OSError) as e:
        logging.warning("[safe_load_json] %s 讀取失敗 (%s)", file_path, e)
        return default
    except Exception:
        logging.exception("[safe_load_json] %s 未預期例外", file_path)
        return default


def atomic_write_text(file_path: str, content: str, encoding: str = 'utf-8') -> bool:
    """文字檔原子寫入（含 .bak 備份）。
    搬自原主程式 _safe_write (line 8650-8670)，用於線上更新覆寫程式碼檔。
    """
    import shutil

    backup = file_path + '.bak'
    fd = -1
    tmp = ""
    try:
        target_dir = os.path.dirname(file_path) or '.'
        os.makedirs(target_dir, exist_ok=True)
        if os.path.exists(file_path):
            shutil.copy2(file_path, backup)
        fd, tmp = _make_temp_path(file_path)
        with os.fdopen(fd, 'w', encoding=encoding) as f:
            fd = -1
            f.write(content)
            _flush_and_fsync(f)
        os.replace(tmp, file_path)
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
                os.remove(tmp)
            except OSError:
                logging.debug("移除 tmp 失敗", exc_info=True)
        return False
