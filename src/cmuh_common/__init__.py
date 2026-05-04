# -*- coding: utf-8 -*-
"""中國醫皮膚科常用程式共用基底套件。

從原 中國醫皮膚科主程式.pyw / 中國醫皮膚科排班程式.pyw 抽出兩支程式約 70% 的重複碼。
"""
from cmuh_common.version import CURRENT_VERSION, parse_version

__all__ = ["CURRENT_VERSION", "parse_version"]
