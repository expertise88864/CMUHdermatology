# -*- coding: utf-8 -*-
"""中國醫皮膚科守護程式（watchdog daemon）— thin wrapper over cmuh_common.watchdog_core.

兩種模式：
  - 預設（無參數）：daemon loop，每 30s 呼叫 watchdog_core.run_one_tick("outer")
  - --once：給 schtasks 觸發；跑一輪 outer tick 即 exit

【重構 2026-05-21】所有 schema / config 載入 / 程式判定邏輯全在
cmuh_common.watchdog_core，這裡只負責 daemon loop。早期版本 runner 自帶舊 schema
（v1）的 DEFAULT_CONFIG 和 _load_config 會覆蓋掉 core 的 v5 schema migration，
互相打架。砍 ~260 行重複碼後問題消失。

啟動方式：
  雙擊「中國醫皮膚科守護程式.pyw」啟動；或加進「安裝開機自動啟動」勾選 → 登入時自動跑。
  [EH-02 2026-07-12] 註：本程式與 .pyw 皆【不會自動提權】。非 admin 執行時,對 admin 權限
  行程的 kill/重啟會 AccessDenied（救不了半死的 admin 行程）。要完整守護請用「安裝開機自動
  啟動」的 schtasks /RL HIGHEST 路徑,或以系統管理員身分啟動。
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
import time
from pathlib import Path

# ─── 路徑 + sys.path（讓 import cmuh_common 起作用） ──────────────────────
_HERE = Path(__file__).resolve().parent
_ROOT = _HERE.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# 依賴自動安裝（核心需要 psutil）
try:
    from cmuh_common.deps_runtime import ensure_dependencies
    ensure_dependencies([("psutil", "psutil")])
except Exception:
    pass

try:
    from cmuh_common.version import CURRENT_VERSION  # noqa: E402
except Exception:
    CURRENT_VERSION = "?.?.?.?"

from cmuh_common.single_instance import (  # noqa: E402
    ensure_single_instance,
    release_single_instance,
)

# ─── Logging ─────────────────────────────────────────────────────────────
SETTINGS_DIR = _ROOT / "settings"
LOG_PATH = SETTINGS_DIR / "watchdog.log"
WATCHDOG_DAEMON_MUTEX_NAME = "Local\\CMUH_Skin_Watchdog_Daemon_v1"


def _setup_logging(log_path: Path = LOG_PATH) -> None:
    # [EH-09 2026-07-12] handler 建構失敗(權限/防毒鎖檔)不可讓守護整個死掉 → 退化 basicConfig
    # 續跑 tick(唯讀 fail-open);否則 schtasks 每 2 分重複同結局、外層守護靜默失效。
    try:
        SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s"
        ))
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        for h in list(root.handlers):
            root.removeHandler(h)
        root.addHandler(handler)
    except Exception:
        try:
            logging.basicConfig(level=logging.INFO)
        except Exception:
            pass
        return
    # console handler（從工作管理員開時看得到；pythonw 模式無 console 也無影響）
    try:
        ch = logging.StreamHandler()
        ch.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(message)s"))
        root.addHandler(ch)
    except Exception:
        pass


# ─── Entry points ────────────────────────────────────────────────────────
def _run_once_via_core() -> int:
    """schtasks 每 2 分鐘呼叫 `python watchdog_runner.py --once`。
    委派給 watchdog_core 跑一輪 outer tick 即 exit。
    RAM 佔用 ≈ 0（只在執行那 1-3 秒）。"""
    # [EH-05 2026-07-12] --once 用獨立 log 檔,避免與 daemon 的 watchdog.log 跨行程 RotatingFileHandler
    # 輪替競態(掉 log/超 1MB)。
    _setup_logging(LOG_PATH.with_name("watchdog_once.log"))
    logging.info("=== watchdog --once v%s ===", CURRENT_VERSION)
    try:
        from cmuh_common import watchdog_core
        actions = watchdog_core.run_one_tick(mode="outer")
        logging.info("[outer once] %s",
                     " | ".join(actions) if actions else "-")
        return 0
    except Exception:
        logging.exception("[outer once] 例外")
        return 1


def main() -> int:
    """daemon loop — 每 30s 呼叫 watchdog_core.run_one_tick('outer')."""
    if "--once" in sys.argv:
        return _run_once_via_core()

    _setup_logging()
    # [EH-02 2026-07-12] 非 admin daemon 對 admin 權限行程 kill 會 AccessDenied → 只警告(不自動
    # 提權,避免無人在場反覆彈 UAC);schtasks /RL HIGHEST 路徑不受影響。
    try:
        import ctypes
        if not ctypes.windll.shell32.IsUserAnAdmin():
            logging.warning(
                "守護程式非以系統管理員啟動 → 對 admin 權限行程的重啟/kill 可能失敗;"
                "完整守護請用開機自動啟動(schtasks /RL HIGHEST)或以管理員身分啟動")
    except Exception:
        pass
    if not ensure_single_instance(WATCHDOG_DAEMON_MUTEX_NAME):
        logging.info("watchdog daemon already running; exit this duplicate")
        return 0

    logging.info("=" * 60)
    logging.info("=== 守護程式啟動 v%s (daemon mode) ===", CURRENT_VERSION)

    try:
        from cmuh_common import watchdog_core
    except Exception:
        logging.exception("載入 watchdog_core 失敗，無法啟動 daemon")
        return 1

    try:
        last_heartbeat = 0.0
        while True:
            try:
                cfg = watchdog_core.load_config()
                actions = watchdog_core.run_one_tick(mode="outer")
                heartbeat, interval = watchdog_core.get_loop_timing(cfg)
                now_monotonic = time.monotonic()
                if now_monotonic - last_heartbeat >= heartbeat:
                    logging.info("[daemon heartbeat] %s",
                                 " | ".join(actions) if actions else "-")
                    last_heartbeat = now_monotonic
            except Exception:
                logging.exception("[daemon] tick 例外")
                interval = 30
            time.sleep(interval)
    finally:
        release_single_instance()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logging.info("收到 Ctrl-C，離開")
        sys.exit(0)
