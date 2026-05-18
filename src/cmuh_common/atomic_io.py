# -*- coding: utf-8 -*-
"""原子寫入工具。搬自原主程式 _atomic_write_json (line 364-381)。

先寫入暫存檔 (.tmp) 再用 os.replace 覆蓋原檔，確保斷電/強制結束時原檔不會變空。
"""
import json
import logging
import os


def atomic_write_json(file_path: str, data, **kwargs) -> None:
    """JSON 原子寫入。kwargs 會傳給 json.dump（如 default=...）。"""
    tmp_path = file_path + ".tmp"
    try:
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=4, **kwargs)
        os.replace(tmp_path, file_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                logging.debug("atomic_write_json: 移除 tmp 失敗", exc_info=True)
        raise


def atomic_write_text(file_path: str, content: str, encoding: str = 'utf-8') -> bool:
    """文字檔原子寫入（含 .bak 備份）。
    搬自原主程式 _safe_write (line 8650-8670)，用於線上更新覆寫程式碼檔。
    """
    import shutil

    backup = file_path + '.bak'
    tmp = file_path + '.tmp'
    try:
        if os.path.exists(file_path):
            shutil.copy2(file_path, backup)
        target_dir = os.path.dirname(file_path) or '.'
        os.makedirs(target_dir, exist_ok=True)
        with open(tmp, 'w', encoding=encoding) as f:
            f.write(content)
        os.replace(tmp, file_path)
        return True
    except Exception as e:
        logging.error("atomic_write_text 失敗 [%s]: %s", file_path, e)
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                logging.debug("移除 tmp 失敗", exc_info=True)
        return False
