# -*- coding: utf-8 -*-
"""把 _originals/中國醫皮膚科{主,排班}程式.pyw 轉換為模組化 src/{main,scheduler}.py。

處理流程：
  1. 讀原始檔
  2. 用 anchor 規則切出區段並依需求刪除/替換
  3. 在開頭插入新的 import block（cmuh_common）
  4. AutomationApp.check_and_update 與 SchedulerApp.check_and_update 整段方法改寫
  5. UPDATE_MANIFEST 刪除（改用 manifest.json）
  6. 全域 os.execv 改 cmuh_common.paths.restart_self()
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ORIGINALS = REPO_ROOT / "_originals"
SRC = REPO_ROOT / "src"

# === 各區段的 anchor 與結束策略 ===
# 每個區段以 (start_pattern, end_marker) 表達；end_marker 為遇到該行（不含）即結束。

REGIONS_TO_DELETE = [
    # (描述, 起始 regex, 終止 regex, 取代為的字串 — None 表示刪除)
    ("DependencyInstaller class",
     r"^class DependencyInstaller\(tk\.Tk\):",
     r"^# ---?\s*定義需要的套件清單",
     None),
    # REQUIRED_LIBS / _DEPS_CACHE_FINGERPRINT / _DEPS_CACHE_FILE / ensure_dependencies / ensure_dependencies()
    ("REQUIRED_LIBS + ensure_dependencies",
     r"^# ---?\s*定義需要的套件清單",
     r"^# 執行依賴檢查",
     None),
    ("ensure_dependencies() call line",
     r"^# 執行依賴檢查",
     r"^import ctypes",
     None),
    # 所有 UiXxxMessage dataclass 與 put_ui_message
    ("UiXxxMessage dataclasses",
     r"^# ---?\s*UI 執行緒.*主執行緒訊息",
     r"^class DoctorConfig\(TypedDict\):",
     None),
    # INTERNAL_HOSTS / _is_internal
    ("INTERNAL_HOSTS / _is_internal",
     r"^# ---?\s*原 modules\.config 內容",
     r"^def date_key_encoder\(obj\):",
     None),
    # _atomic_write_json
    ("_atomic_write_json",
     r"^def _atomic_write_json\(file_path",
     r"^# 取得目前程式所在位置",
     None),
    # BASE_DIR / SETTINGS_DIR / get_conf_path
    ("BASE_DIR / SETTINGS_DIR / get_conf_path",
     r"^# 取得目前程式所在位置",
     r"^# 變更圖示來源",
     None),
    # _CMUH_ICON_ASSET_VERSION + _CMU_LOGO_PNG_URLS + _ensure_cmuh_app_icon_path
    ("Icon asset version + ensure_cmuh_app_icon_path",
     r"^# 變更圖示來源",
     r"^def _apply_windows_wm_seticon_from_ico\(",
     None),
    # _apply_windows_wm_seticon_from_ico + _apply_tk_window_icon
    ("Window icon helpers",
     r"^def _apply_windows_wm_seticon_from_ico\(",
     r"^# 定義所有需要檢查更新的檔案",
     None),
    # UPDATE_MANIFEST
    ("UPDATE_MANIFEST dict",
     r"^# 定義所有需要檢查更新的檔案",
     r"^# ---------------------------",
     None),
    # QueueHandler class
    ("QueueHandler class",
     r"^class QueueHandler\(logging\.Handler\):",
     r"^# ---?\s*2\.\s*全域設定與日誌",
     None),
    # is_admin / run_as_admin / show_windows_notification
    ("is_admin / run_as_admin / show_windows_notification",
     r"^# ---?\s*1\.\s*權限與路徑管理",
     r"^# ---?\s*3\.\s*門診與醫師設定",
     None),
    # LASTINPUTINFO + get_idle_duration
    ("LASTINPUTINFO + get_idle_duration",
     r"^class LASTINPUTINFO\(ctypes\.Structure\):",
     r"^def get_local_version\(file_path\):",
     None),
    # get_local_version
    ("get_local_version (now in updater)",
     r"^def get_local_version\(file_path\):",
     r"^def check_stop\(\):",
     None),
    # _set_windows_dpi_awareness + _set_windows_app_user_model_id
    ("DPI awareness + AppUserModelID",
     r"^def _set_windows_dpi_awareness\(\):",
     r"^# ---?\s*主程式執行區",
     None),
]

# === 替換規則：(目標 regex, 取代為) ===
SIMPLE_REPLACEMENTS = [
    # 版本宣告改為 import（保留鏡像值以兼容 IDE 的型別檢查）
    (re.compile(r'^CURRENT_VERSION\s*=\s*"[\d.]+"\s*$', re.MULTILINE),
     'from cmuh_common.version import CURRENT_VERSION'),
    # 主程式重啟：os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])
    (re.compile(r'os\.execv\(sys\.executable,\s*\[sys\.executable,\s*os\.path\.abspath\(__file__\)\]\)'),
     'restart_self()'),
    # 打卡程式風格：os.execv(sys.executable, args)（保險也擋）
    (re.compile(r'os\.execv\(sys\.executable,\s*args\)'),
     'restart_self(args[1:])'),
    # gist URL 拼湊（這部分由 check_and_update 整段替換處理；這裡是保險）
]

# === 在頭部插入的 import block ===
HEADER_IMPORTS = '''# -*- coding: utf-8 -*-
# =============================================================================
# 由 scripts/transform_pyw.py 自動生成。
# 重構自 _originals/{ORIGINAL_FILENAME}
# 共用基底已抽出至 cmuh_common/，本檔僅保留業務邏輯（UI、抓網、熱鍵等）。
# =============================================================================
import os
import sys

# 把 src/ 加到 sys.path，讓 cmuh_common / network / hotkey / ui / clock 子套件可用
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

# === cmuh_common 共用基底 ===
from cmuh_common.version import CURRENT_VERSION, parse_version
from cmuh_common.paths import (
    get_app_dir, get_settings_dir, get_conf_path, restart_self, is_frozen,
)
from cmuh_common.atomic_io import atomic_write_json as _atomic_write_json
from cmuh_common.atomic_io import atomic_write_text
from cmuh_common.platform_win import (
    is_admin, run_as_admin, set_dpi_awareness, set_app_user_model_id, get_idle_duration,
)
from cmuh_common.notifications import show_windows_notification
from cmuh_common.icons import ensure_cmuh_app_icon_path as _ensure_cmuh_app_icon_path
from cmuh_common.window_icon import apply_tk_window_icon as _apply_tk_window_icon
from cmuh_common.logging_setup import QueueHandler
from cmuh_common.http_client import INTERNAL_HOSTS, is_internal as _is_internal
from cmuh_common.ui_messages import (
    UiStatusMessage, UiRefreshTickMessage, UiClinicDataMessage, UiMasterScheduleMessage,
    UiDutyDoctorMessage, UiSaturdayDutyDoctorMessage, UiTodayVsMessage, UiSaturdayVsMessage,
    UiClockStatusMessage, UiAlertInfoMessage, UiAlertErrorMessage, UiMessage, put_ui_message,
)
from cmuh_common.deps_runtime import ensure_dependencies as _ensure_deps_runtime

# === 依賴清單（與原檔一致；指紋由 deps_runtime 處理）===
REQUIRED_LIBS = [
    ("requests", "requests"),
    ("beautifulsoup4", "bs4"),
    ("lxml", "lxml"),
    ("selenium", "selenium"),
    ("keyboard", "keyboard"),
    ("pyautogui", "pyautogui"),
    ("schedule", "schedule"),
    ("psutil", "psutil"),
    ("Pillow", "PIL"),
    ("pystray", "pystray"),
    ("pywin32", "win32gui"),
]
_ensure_deps_runtime(REQUIRED_LIBS)

# === BASE_DIR / SETTINGS_DIR 沿用原語意 ===
BASE_DIR = get_app_dir()
SETTINGS_DIR = get_settings_dir()

# === [雙軌相容] _set_windows_dpi_awareness / _set_windows_app_user_model_id 兼容名 ===
_set_windows_dpi_awareness = set_dpi_awareness
_set_windows_app_user_model_id = set_app_user_model_id

# === 線上更新（取代原 UPDATE_MANIFEST + check_and_update）===
from cmuh_common import updater as _updater_mod  # noqa: E402

'''

# === 新版 check_and_update 方法（取代原行為，URL 改 GitHub raw + manifest.json）===
NEW_CHECK_AND_UPDATE = '''    def check_and_update(self, is_manual=False):
        """檢查並更新所有相關程式（改寫自原 check_and_update）。

        【保留】平行下載、tuple 版本比較、原子寫入 + .bak 備份、失敗保留本地舊版。
        【改動】URL 從 4 個 Gist 改為單一 manifest.json @ GitHub raw。
        """
        if is_manual:
            put_ui_message(self.ui_queue, UiStatusMessage(text="狀態: 正在檢查所有程式更新..."))
        import logging
        logging.info("=== Starting Multi-File Update Check (Parallel via manifest.json) ===")

        try:
            result = _updater_mod.check_and_update()
            if result.errors:
                if is_manual:
                    put_ui_message(self.ui_queue, UiStatusMessage(text="狀態: 更新檢查失敗"))
                    put_ui_message(self.ui_queue, UiAlertErrorMessage(
                        title="更新錯誤",
                        msg="檢查更新時發生錯誤:\\n" + "\\n".join(result.errors),
                    ))
                return

            if _updater_mod.need_restart_after_update(result):
                msg_lines = [f"{fn} (v{ver})" for fn, ver in result.updated_files]
                if is_manual:
                    msg = "以下程式已更新完成：\\n\\n" + "\\n".join(msg_lines) + "\\n\\n程式將立即重新啟動。"
                    put_ui_message(self.ui_queue, UiAlertInfoMessage(
                        title="更新完成", msg=msg, need_restart=True))
                else:
                    logging.info("Auto-update applied. Requesting restart on UI thread...")
                    put_ui_message(self.ui_queue, UiAlertInfoMessage(
                        title="自動更新完成", msg="已套用自動更新，程式將重新啟動。",
                        need_restart=True))
            elif result.is_frozen and result.has_update:
                # .exe 模式偵測到新版：跳通知請使用者去 GitHub release 下載
                put_ui_message(self.ui_queue, UiAlertInfoMessage(
                    title="有新版可下載",
                    msg=(f"偵測到新版 v{result.manifest_app_version}\\n"
                         f"請至 {result.release_url} 下載新版執行檔。"),
                    need_restart=False,
                ))
            else:
                if is_manual:
                    put_ui_message(self.ui_queue, UiAlertInfoMessage(
                        title="檢查完成", msg="所有程式皆為最新版本。", need_restart=False))
                    put_ui_message(self.ui_queue, UiStatusMessage(text="狀態: 所有程式皆為最新"))
        except Exception as e:
            logging.error(f"Global update process failed: {e}")
            if is_manual:
                put_ui_message(self.ui_queue, UiStatusMessage(text="狀態: 更新檢查失敗"))
                put_ui_message(self.ui_queue, UiAlertErrorMessage(
                    title="更新錯誤", msg=f"檢查更新時發生錯誤: {e}"))
'''


def _delete_region_by_anchors(text: str, start_re: str, end_re: str, label: str) -> str:
    """從 text 中刪除 start_re 行到 end_re 行（不含 end_re 行）。"""
    lines = text.split('\n')
    start_pat = re.compile(start_re)
    end_pat = re.compile(end_re)
    start_idx = None
    end_idx = None
    for i, line in enumerate(lines):
        if start_idx is None and start_pat.match(line):
            start_idx = i
        elif start_idx is not None and end_pat.match(line):
            end_idx = i
            break
    if start_idx is None:
        sys.stderr.write(f"  [警告] 找不到 {label} 起始 anchor: {start_re}\n")
        return text
    if end_idx is None:
        sys.stderr.write(f"  [警告] 找不到 {label} 結束 anchor: {end_re}\n")
        return text
    print(f"  [刪除] {label}: L{start_idx + 1}~L{end_idx} ({end_idx - start_idx} 行)")
    return '\n'.join(lines[:start_idx] + lines[end_idx:])


def _replace_check_and_update(text: str) -> str:
    """整段替換 AutomationApp / SchedulerApp 的 check_and_update method。

    匹配從 `    def check_and_update(self, is_manual=False):` 到下一個方法的 `    def ` 之前。
    """
    pattern = re.compile(
        r'^    def check_and_update\(self, is_manual=False\):.*?(?=^    def |^class |^if __name__)',
        re.MULTILINE | re.DOTALL,
    )
    # 用 lambda 取代 string 避免 \n 被 re.sub 當 escape sequence 處理
    new_text, n = pattern.subn(lambda _m: NEW_CHECK_AND_UPDATE + '\n', text, count=1)
    if n == 0:
        sys.stderr.write("  [警告] 找不到 check_and_update 方法\n")
        return text
    print("  [替換] check_and_update method")
    return new_text


def transform(src_path: Path, original_filename: str) -> str:
    text = src_path.read_text(encoding='utf-8')
    print(f"\n=== 處理 {src_path.name}（{len(text.splitlines())} 行）===")

    # 移除 shebang/coding 與 header comment block（前 ~10 行）
    lines = text.split('\n')
    skip_until = 0
    for i, line in enumerate(lines):
        if line.startswith('CURRENT_VERSION'):
            skip_until = i + 1  # 跳過 CURRENT_VERSION 行（會被 import 取代）
            break
    body = '\n'.join(lines[skip_until:])

    # 刪除一系列基礎設施區塊
    for label, start_re, end_re, _ in REGIONS_TO_DELETE:
        body = _delete_region_by_anchors(body, start_re, end_re, label)

    # 替換 check_and_update method
    body = _replace_check_and_update(body)

    # 簡單替換（os.execv → restart_self 等）
    for pat, repl in SIMPLE_REPLACEMENTS:
        body, n = pat.subn(repl, body)
        if n:
            print(f"  [替換] {pat.pattern[:60]} ({n} 次)")

    # 在 body 開頭加上新 header
    header = HEADER_IMPORTS.replace('{ORIGINAL_FILENAME}', original_filename)
    return header + body


def main() -> int:
    targets = [
        ('中國醫皮膚科主程式.pyw', 'main.py'),
        ('中國醫皮膚科排班程式.pyw', 'scheduler.py'),
    ]
    for orig_name, dst_name in targets:
        src_path = ORIGINALS / orig_name
        dst_path = SRC / dst_name
        if not src_path.exists():
            sys.stderr.write(f"找不到原始檔: {src_path}\n")
            return 1
        out = transform(src_path, orig_name)
        dst_path.write_text(out, encoding='utf-8')
        n = len(out.splitlines())
        print(f"\n[OK] 寫入 {dst_path}（{n} 行）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
