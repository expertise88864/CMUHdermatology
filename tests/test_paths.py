# -*- coding: utf-8 -*-
"""paths.py 測試。"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

from cmuh_common.paths import (  # noqa: E402
    get_app_dir, get_settings_dir, get_conf_path, is_frozen,
)


def test_is_frozen_default_false():
    """測試環境通常不是 frozen。"""
    assert is_frozen() is False


def test_app_dir_exists(real_get_app_dir):
    # repo 級 conftest 為隔離會把 get_app_dir 導向 tmp;這裡要驗『正版』的路徑選擇邏輯,
    # 故用 real_get_app_dir 夾具拿未被導向的函式(直接跑本檔時 fallback 成 import 的版本)。
    d = real_get_app_dir()
    assert os.path.isdir(d), f"app_dir 不存在: {d}"


def test_settings_dir_created():
    d = get_settings_dir()
    assert os.path.isdir(d), f"settings_dir 沒被建立: {d}"


def test_conf_path():
    p = get_conf_path("test.json")
    assert p.endswith("test.json")
    assert "settings" in p


if __name__ == "__main__":
    test_is_frozen_default_false()
    test_app_dir_exists(get_app_dir)  # 直接跑：無 conftest，import 的即正版
    test_settings_dir_created()
    test_conf_path()
    print("[OK] paths tests passed")
