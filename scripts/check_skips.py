# -*- coding: utf-8 -*-
"""skip 數量守衛（P2-07）—— 靜默 skip 是這個 repo 踩過兩次的坑。

★為什麼需要這道關卡★
被 skip 的測試【不會讓套件變紅】，所以「全套綠燈」跟「那批測試根本沒跑」長得一模
一樣。實際發生過兩次：

  1. 匯出重依賴（openpyxl / python-docx / reportlab）在 CI 沒裝 → `test_roster_export`
     的 10 項全部 skip。匯出是每個月真的在用的功能，等於完全沒測。
     （已由 ci.yml 的 `import` 防呆擋住 —— 那是針對「這一個已知原因」。）
  2. 多加一個 Tk 測試檔，就讓 `test_roster_ui_smoke` 的 42 支全部變 skip
     （這台開發機的 Tcl 一個行程只容得下一個活著的 root，先建的能用、後建的整檔
     被 skip）。**全套仍然是綠燈**，而且順序一換受害者就換人。

第 2 種沒有「已知原因」可以事先防，只有數量守衛擋得住：skip 變多就紅，逼人去看
為什麼變多。要嘛是真的多了合理的 skip（更新基線並說明），要嘛就是又有一批測試
被無聲關掉了。
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET

# ★基線：目前唯一合理的 skip★
#   tests/test_push_helper_antirevert.py 有兩支在「工作區有未提交變更」時 skip
#   —— 它們比對 index 與工作區內容，未提交時本來就不會相同。
#   CI 的工作區永遠是乾淨的，所以【CI 上預期是 0】；本機跑則可能是 2。
def _make_stdout_robust() -> None:
    """★關卡不可死在「印不出自己的輸出」★

    這些關卡會把外部工具的訊息（pyright 診斷、pytest 的 skip 理由、
    pip-audit 的套件名）原樣印出來，而那些可能含任何 Unicode（例如 pyright
    用的 U+2022 項目符號）。控制台是 cp936/cp1252 時，`print` 會拋
    `UnicodeEncodeError` —— 關卡於是因為【印不出來】而失敗或中斷，
    而不是因為它要擋的事情。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


_make_stdout_robust()


MAX_SKIPPED = int(os.environ.get("CMUH_MAX_SKIPPED", "2"))


def parse_junit(path: str) -> "tuple[int, list]":
    """→ (skip 數, [(測試名稱, 原因)])。

    ★用 JUnit XML 而不是抓 pytest 的文字輸出★
    文字輸出要靠 shell 管線存檔（Windows 上 `tee` 的行為與編碼隨 PowerShell
    版本而異），而且格式一改就抓不到 —— 抓不到時這道關卡會安靜地失效，
    而它本來就是一道防「安靜失效」的關卡。XML 是結構化的，也不怕編碼。
    """
    tree = ET.parse(path)
    skipped = []
    for case in tree.iter("testcase"):
        for sk in case.findall("skipped"):
            name = f"{case.get('classname', '')}::{case.get('name', '')}"
            skipped.append((name, sk.get("message", "")))
    return len(skipped), skipped


def main(argv: list) -> int:
    if not argv:
        print("用法：python scripts/check_skips.py <pytest 的 --junitxml 檔>")
        return 2
    path = argv[0]
    try:
        count, skipped = parse_junit(path)
    except (OSError, ET.ParseError) as e:
        print(f"[skips] 讀不到/解析不了 {path}：{e}")
        print("  ★讀不到不等於沒問題★ —— 這道關卡沒跑成，視為失敗。")
        return 1

    print(f"[skips] 被 skip 的測試：{count}（上限 {MAX_SKIPPED}）")
    for name, why in skipped:
        print(f"    {name}  —— {why}")
    if count > MAX_SKIPPED:
        print(f"\n[skips] ★skip 數量超過上限★（{count} > {MAX_SKIPPED}）")
        print("  被 skip 的測試不會讓套件變紅 —— 「全套綠燈」跟「那批根本沒跑」")
        print("  長得一模一樣。請確認是哪些測試被關掉、為什麼；若確有正當理由，")
        print("  調整 scripts/check_skips.py 的 MAX_SKIPPED 並在 PR 說明。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
