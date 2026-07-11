# -*- coding: utf-8 -*-
"""Shared auto-update suspension policy for watchdog and updater."""
from __future__ import annotations

import logging
import os
import time

from cmuh_common.atomic_io import atomic_write_text
from cmuh_common.paths import get_settings_dir

AUTO_UPDATE_SUSPEND_FILENAME = ".auto_update_suspended_until"
# 【2026-06-03】每天只在固定 3 個時間點檢查更新（早上 07:00 / 中午 13:00 / 下午 18:00），
# 不再 08:00–17:00 每 30 分鐘檢查一次。醫院多台電腦共用對外 NAT IP，密集檢查容易撞
# GitHub API 限流（60 次/時/IP），少打才不會 403 → 退回 branch 拿到舊版 → 下載失敗。
AUTO_UPDATE_CHECK_TIMES = ("07:00", "13:00", "18:00")


def get_auto_update_suspend_path() -> str:
    return os.path.join(get_settings_dir(), AUTO_UPDATE_SUSPEND_FILENAME)


def suspend_auto_updates(reason: str, *, duration_sec: float = 3600,
                         now: float | None = None) -> str:
    """Suspend file-writing updates until the timestamp stored in settings."""
    current = time.time() if now is None else float(now)
    suspend_until = int(current + max(0.0, float(duration_sec)))
    path = get_auto_update_suspend_path()
    content = f"{suspend_until}\nreason: {reason}\n"
    if not atomic_write_text(path, content, encoding="utf-8"):
        raise OSError(f"failed to write auto-update suspension flag: {path}")
    return path


def get_auto_update_suspend_until(*, now: float | None = None) -> float:
    """Return an active suspension timestamp; be conservative on read errors.

    [IE-03 2026-07-10] 這支被 updater/watchdog 拿來決定「要不要抑制自動更新」，誤判會
    直接放行/擋掉線上更新(cmuh_common 五程式共用)，所以三種失敗要分開處理：

    - 檔不存在(FileNotFoundError) → 0.0（沒有抑制，正常放行）。
    - 讀取時 OSError（AV/OneDrive 暫時鎖檔、權限）→【保守】視同「仍在抑制」回 current+300，
      且【不刪檔】。寧可暫緩 5 分鐘也不要因為一次讀不到就當沒抑制、貿然覆蓋檔案。
    - 內容真損壞（非數字）→ 刪檔 + 0.0（垃圾檔清掉，下次 suspend 會重寫）。
    - 已過期（suspend_until <= current）→ 回 0.0 但【不刪檔】：避免「這裡讀到過期→準備刪」
      與 watchdog「同一刻寫入新的 suspend 旗標」之間的 TOCTOU 把剛寫好的新旗標誤刪。
      過期本來就不生效、留著無害，下次 suspend 直接覆寫即可。
    """
    current = time.time() if now is None else float(now)
    path = get_auto_update_suspend_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
    except FileNotFoundError:
        return 0.0
    except OSError as e:
        # 讀不到（暫時鎖檔/權限）→ 保守視同仍抑制，且不刪檔（可能是有效旗標）
        logging.warning(
            "[update-policy] cannot read suspension flag %s, holding conservatively: %s",
            path, e)
        return current + 300.0

    try:
        suspend_until = float(first_line)
    except (TypeError, ValueError) as e:
        # 內容真損壞才刪
        logging.warning("[update-policy] corrupt suspension flag %s: %s", path, e)
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError:
            logging.debug("[update-policy] failed to remove corrupt flag %s",
                          path, exc_info=True)
        return 0.0

    if suspend_until > current:
        return suspend_until
    # 已過期 → 不刪（TOCTOU 防護），回 0.0
    return 0.0
