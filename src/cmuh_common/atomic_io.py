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


class MultiWriteError(OSError):
    """多檔寫入失敗。written/pending 讓呼叫端能【精確】告訴使用者存了哪些。

    phase：
      "stage"  —— 還在寫暫存檔就失敗 → 一個目標檔都沒動(written 必為空)。
      "commit" —— 暫存檔都寫好了,replace 到一半失敗 → written 是已生效的檔。
    """

    def __init__(self, message: str, *, phase: str, written: list,
                 pending: list, cause: BaseException | None = None):
        super().__init__(message)
        self.phase = phase
        self.written = list(written)
        self.pending = list(pending)
        self.cause = cause


def atomic_write_json_multi(items, **kwargs) -> None:
    """把多個 JSON 檔【要嘛都生效、要嘛都不生效】地寫下去。

    items: [(file_path, data), ...]，依序 commit。

    【為什麼需要它:2026-08-06 外審 P1-07】
    設定頁的「儲存」要寫 r_doctor_settings / threshold_settings / doctors 三個檔。
    舊做法是連續三次 `atomic_write_json`——單檔各自原子，但【三檔之間不是】：
    第二個檔寫失敗時第一個早就生效了，使用者只看到一個例外，不知道自己的設定
    處於「R 醫師已更新、醫師清單還是舊的」這種半套狀態。

    做法(兩階段)：
      Phase 1 stage  : 全部寫進同目錄的 .tmp + fsync。任一失敗 → 清掉所有 tmp、
                       拋 MultiWriteError(phase="stage")，目標檔【一個都沒動】。
      Phase 2 commit : 依序 os.replace(同磁區的 rename，成功機率極高)。
                       萬一中途失敗 → 拋 MultiWriteError(phase="commit") 並附上
                       「已生效」與「未生效」清單，讓 UI 能講清楚。
    註：Windows 沒有跨檔案的原子 rename，Phase 2 理論上仍可能部分完成；但把所有
    可能失敗的重活(序列化、磁碟寫入、空間不足)都擋在 Phase 1，已經把真實世界的
    半套風險壓到極低，而且失敗時是【可精確描述】的，不再是無聲半套。
    """
    items = [(str(p), d) for p, d in items]
    dump_kwargs = {"ensure_ascii": False, "indent": 4}
    dump_kwargs.update(kwargs)

    staged: list = []          # [(tmp_path, target_path), ...]
    try:
        for target_path, data in items:
            fd, tmp_path = _make_temp_path(target_path)
            try:
                with os.fdopen(fd, 'w', encoding='utf-8') as f:
                    json.dump(data, f, **dump_kwargs)
                    _flush_and_fsync(f)
            except Exception:
                try:
                    os.close(fd)
                except Exception:
                    pass
                if os.path.exists(tmp_path):
                    try:
                        _remove_with_retry(tmp_path)
                    except Exception:
                        logging.debug("[atomic_io] 清除 tmp 失敗", exc_info=True)
                raise
            staged.append((tmp_path, target_path))
    except Exception as e:
        for tmp_path, _ in staged:
            try:
                if os.path.exists(tmp_path):
                    _remove_with_retry(tmp_path)
            except Exception:
                logging.debug("[atomic_io] 清除 tmp 失敗", exc_info=True)
        raise MultiWriteError(
            f"寫入暫存檔失敗，未變更任何設定檔：{e}",
            phase="stage", written=[], pending=[p for p, _ in items],
            cause=e) from e

    written: list = []
    for idx, (tmp_path, target_path) in enumerate(staged):
        try:
            _replace_with_retry(tmp_path, target_path)
        except Exception as e:
            for leftover_tmp, _ in staged[idx:]:
                try:
                    if os.path.exists(leftover_tmp):
                        _remove_with_retry(leftover_tmp)
                except Exception:
                    logging.debug("[atomic_io] 清除 tmp 失敗", exc_info=True)
            raise MultiWriteError(
                f"設定只寫入了一部分（{target_path} 失敗）：{e}",
                phase="commit", written=written,
                pending=[t for _, t in staged[idx:]], cause=e) from e
        written.append(target_path)


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
                # [2026-07-25 審查] backup 失敗仍回 "corrupt" 會**違反本函式自己的契約**
                # （見 docstring:"corrupt" 代表「原檔已被 rename 移走」故可安全覆寫）。
                # 別的行程允許讀取但拒絕 rename/delete 時就會走到這裡:呼叫端據 "corrupt"
                # 判定可覆寫 → 直接把使用者的原檔蓋掉且**毫無備份**。原檔既然還在,
                # 語意上就等同 "error"(暫時性失敗、原檔完好、不可覆寫)。
                logging.warning("[safe_load_json] 壞檔 backup 失敗，原檔仍在 → "
                                "回報 error(不可覆寫): %s", file_path,
                                exc_info=True)
                return default, "error"
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
