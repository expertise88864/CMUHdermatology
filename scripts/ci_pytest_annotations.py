# -*- coding: utf-8 -*-
"""把 junit.xml 裡的失敗轉成 GitHub Actions annotation。

★為什麼需要這支★（2026-08-09 實測）
CI 的 `test` job 紅了，而我在本機把**完整的 CI 步驟**逐條重現（pytest 4200
passed、skip 守衛 0、覆蓋率三層都過、逐條型別債棘輪 0）全部是綠的 ——
失敗只發生在 CI 的環境上。要修就得知道是哪一支測試、哪一行。

而拿不到：
* `check-runs/<id>/annotations` 只回「Process completed with exit code 1」；
* `actions/jobs/<id>/logs` 匿名讀是 **403**（要 repo admin token）。

於是「哪一支測試壞了」變成只有 repo 擁有者點進網頁才看得到的資訊。
**一道看不到原因的紅燈，最後就是被忽略的紅燈。**

annotation 是匿名讀得到的，所以這裡把 junit.xml 的失敗**逐筆**印成
`::error file=…,line=…::…`（不設數量上限 —— 全紅時正是最需要看清楚的時候），
並在最前面印一則列出所有失敗測試名稱的總表，免得平台自己截斷顯示。

★這支自己不可以變成假綠燈★
它是在 pytest 已經失敗之後才跑的報告器：解析不了 junit.xml 就照實說，
而且**永遠不改變 job 的成敗**（workflow 用 `if: failure()` 呼叫它，
pytest 自己的 exit code 才是判準）。
"""
from __future__ import annotations

import os
import sys
import xml.etree.ElementTree as ET


def _make_stdout_robust() -> None:
    """★關卡不可死在「印不出自己的輸出」★（與其他關卡腳本同一套）

    這支要把 pytest 的失敗訊息原樣印出來,那裡面可能有任何 Unicode。
    控制台是 cp936/cp1252 時 `print` 會拋 `UnicodeEncodeError` ——
    於是「讓紅燈看得到原因」的東西自己因為印不出來而失效。
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


_make_stdout_robust()

# ★[2026-08-09 外審 P3] 不設「最多印幾筆」★
#   第一版寫 `_MAX = 20`,而 docstring 說的是「逐筆」—— 超過 20 筆時後面那些
#   只剩一個總數,而那正是「全紅」時最需要看清楚的情況。宣稱與實作不符。
#   現在每一筆都印。GitHub 自己在 UI 上可能只顯示前幾則,所以【先】印一則
#   把所有失敗測試名字列出來的總表:就算平台截斷顯示,總表仍帶得出完整清單。
_MSG_CHARS = 900   # 每筆訊息截斷長度（annotation 有長度上限）
_SUMMARY_CHARS = 4000   # 總表的長度上限（超過就照實說被截了幾筆）


def _one_line(text: str) -> str:
    """annotation 的訊息不能有裸換行 —— 用 `%0A` 表示。"""
    s = " ".join(str(text or "").split("\n"))
    return s.replace("\r", " ")


def _iter_failures(root):
    for case in root.iter("testcase"):
        for kind in ("failure", "error"):
            node = case.find(kind)
            if node is None:
                continue
            yield (case.get("file") or case.get("classname") or "tests",
                   case.get("line"), case.get("name") or "?", kind,
                   (node.get("message") or "") + " " + (node.text or ""))


def main(argv: list) -> int:
    path = argv[0] if argv else "junit.xml"
    if not os.path.exists(path):
        # ★不可以安靜跳過★ 沒有報告本身就是要知道的事（pytest 可能連
        #   collection 都沒過，那時 junit.xml 不會產生）。
        print(f"::error::找不到 {path} —— pytest 可能在 collection 階段就失敗了，"
              f"請看 job log")
        return 0
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as e:
        print(f"::error::junit.xml 解析失敗（{e}）—— 失敗細節無法轉成 annotation")
        return 0
    failures = list(_iter_failures(root))
    if not failures:
        print("::error::junit.xml 裡沒有任何 failure/error —— "
              "紅燈可能來自 collection 錯誤或 pytest 自己的退出碼")
        return 0
    # ★總表先印★ GitHub 的 UI 只顯示前幾則 annotation;總表放第一則,
    #   這樣「哪些測試壞了」永遠看得到,細節再看後面那幾則。
    names = [f"{f}::{n}" for f, _l, n, _k, _m in failures]
    joined = " | ".join(names)
    if len(joined) > _SUMMARY_CHARS:
        keep = joined[:_SUMMARY_CHARS].rsplit(" | ", 1)[0]
        cut = len(names) - keep.count(" | ") - 1
        joined = f"{keep} …（另有 {cut} 筆，名稱過長被截斷）"
    print(f"::error title=pytest 失敗 {len(failures)} 筆::{joined}")
    for file_, line, name, kind, msg in failures:
        loc = f"file={file_}"
        if line and str(line).isdigit():
            loc += f",line={int(line) + 1}"   # junit 是 0-based
        print(f"::error {loc},title=pytest {kind}: {name}::"
              f"{_one_line(msg)[:_MSG_CHARS]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
