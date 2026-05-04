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


def test_app_dir_exists():
    d = get_app_dir()
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
    test_app_dir_exists()
    test_settings_dir_created()
    test_conf_path()
    print("[OK] paths tests passed")
