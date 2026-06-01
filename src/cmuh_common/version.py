# -*- coding: utf-8 -*-
"""版本資訊單一真實來源（Single Source of Truth）。

所有入口檔案統一 from cmuh_common.version import CURRENT_VERSION。
push.bat 內的 scripts/bump_version.py 會自動 bump 此處版本。
"""

CURRENT_VERSION = "2026.06.01.10"  # 格式 YYYY.MM.DD.serial


def parse_version(s: str) -> tuple:
    """將 '2026.05.04.1' 轉為 tuple，避免字串排序 bug。

    例：parse_version("2026.1.1") > parse_version("2025.12.16") 為 True。
    搬自原主程式 line 8612-8617 的 _parse_version。
    """
    try:
        return tuple(int(x) for x in str(s).split('.'))
    except (ValueError, AttributeError):
        return (0,)
