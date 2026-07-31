# -*- coding: utf-8 -*-
"""skip 守衛（P2-07）—— 靜默 skip 是這個 repo 踩過兩次的坑。

★為什麼需要這道關卡★
被 skip 的測試【不會讓套件變紅】，所以「全套綠燈」跟「那批測試根本沒跑」長得一模
一樣。實際發生過兩次：

  1. 匯出重依賴（openpyxl / python-docx / reportlab）在 CI 沒裝 → `test_roster_export`
     的 10 項全部 skip。匯出是每個月真的在用的功能，等於完全沒測。
     （已由 ci.yml 的 `import` 防呆擋住 —— 那是針對「這一個已知原因」。）
  2. 多加一個 Tk 測試檔，就讓 `test_roster_ui_smoke` 的 42 支全部變 skip
     （這台開發機的 Tcl 一個行程只容得下一個活著的 root，先建的能用、後建的整檔
     被 skip）。**全套仍然是綠燈**，而且順序一換受害者就換人。

─── ★[2026-07-31] 從「數量上限」改成「宣告式期待」★ ────────────────────────
舊版是一個數字（`CMUH_MAX_SKIPPED`，CI 上設 0）。它連紅了四輪，而且原因很難堪：
**那個 0 是我推理出來的，不是量出來的。** 當時只想到 `test_push_helper_antirevert`
有兩支在「工作區有未提交變更」時會 skip、而 CI 的 checkout 永遠乾淨，卻漏看了
同一個檔案裡還有一支是【平台條件】skip（.gitattributes 強制 `eol=lf`，CI 的全新
checkout 一定是 LF）。於是 CI 從加上這道關卡的那一次起就沒綠過，連帶讓它後面的
覆蓋率門檻與型別債棘輪【從來沒有在 CI 上執行過】。

裸數字還有一個更根本的問題：它分不出「1 個合理的環境 skip」和「42 支被靜默關掉」
之間的差別 —— 只要有人把數字調大一次，兩者就都放行了。而「把數字調大」正是這道
關卡紅燈時最順手的反應。

現在改成：**每一個 skip 都必須事先被宣告**（`skip_expectations.json` 記理由樣式、
數量上限與為什麼合理）。沒宣告過的 skip 理由一律紅，不管數量多少；宣告過的超出
上限也紅。要放行就得寫下「為什麼這個 skip 是環境造成的、不是測試被關掉」。

★這道關卡自己不可以無聲失效★：期待檔讀不到／解析不了 → 判失敗（不是「沒有期待
就全部放行」，也不是「沒有期待就全部擋掉」）；JUnit 報告讀不到 → 判失敗。
"""
from __future__ import annotations

import json
import os
import sys
import xml.etree.ElementTree as ET

# ★路徑錨在 __file__，不靠 CWD★ 從別的目錄呼叫時不可以安靜地找不到期待檔而放行。
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXPECTATIONS_PATH = os.path.join(_REPO_ROOT, "skip_expectations.json")


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


def annotate(title: str, body: str) -> None:
    """在 GitHub Actions 上輸出 annotation。

    ★為什麼要特地做這件事★ job log 要 repo admin 權限才下載得到，但 annotation
    走 check-runs API 是【公開可讀】的。這道關卡紅了四輪都沒人看得出是哪幾支
    測試被 skip —— 關卡說不清楚自己為什麼紅，跟沒有關卡差不了多少。
    """
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    esc = (str(body).replace("%", "%25")
           .replace("\r", "%0D").replace("\n", "%0A"))
    print(f"::error title={title}::{esc}")


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


def load_expectations(path: str = "") -> list:
    """讀宣告檔。★讀不到就拋★ —— 由 main() 判成失敗。

    「檔案不見了」與「沒有任何預期 skip」在磁碟上長得一樣，但意義完全相反：
    前者代表這道關卡失去了判準，不可以當成後者放行。
    """
    p = path or EXPECTATIONS_PATH
    with open(p, "r", encoding="utf-8") as f:
        data = json.load(f)
    items = data.get("expected")
    if not isinstance(items, list):
        raise ValueError("expected 欄位必須是陣列")
    out = []
    for i, it in enumerate(items):
        if not isinstance(it, dict):
            raise ValueError(f"expected[{i}] 不是物件")
        reason = str(it.get("reason_contains") or "")
        if not reason:
            raise ValueError(f"expected[{i}] 缺 reason_contains")
        if not str(it.get("why") or "").strip():
            # 沒有理由的白名單條目＝小型的「把數字調大」，一律不接受
            raise ValueError(f"expected[{i}] 缺 why（為什麼這個 skip 是合理的）")
        try:
            cap = int(it.get("max"))
        except (TypeError, ValueError):
            raise ValueError(f"expected[{i}] 的 max 必須是整數") from None
        out.append({"id": str(it.get("id") or f"#{i}"),
                    "reason_contains": reason,
                    "nodeid_contains": str(it.get("nodeid_contains") or ""),
                    "max": cap, "why": str(it["why"])})
    return out


def classify(skipped: list, expectations: list) -> "tuple[dict, list]":
    """把每個 skip 歸到宣告過的條目，或歸為「未宣告」。

    → ({條目 id: [測試…]}, [未宣告的 (測試, 原因)…])。純函式，好測。
    """
    matched = {e["id"]: [] for e in expectations}
    unexpected = []
    for name, why in skipped:
        for e in expectations:
            if e["reason_contains"] not in (why or ""):
                continue
            if e["nodeid_contains"] and e["nodeid_contains"] not in (name or ""):
                continue
            matched[e["id"]].append((name, why))
            break
        else:
            unexpected.append((name, why))
    return matched, unexpected


def main(argv: list) -> int:
    if not argv:
        print("用法：python scripts/check_skips.py <pytest 的 --junitxml 檔>")
        return 2
    path = argv[0]
    try:
        count, skipped = parse_junit(path)
    except (OSError, ET.ParseError) as e:
        msg = (f"讀不到/解析不了 {path}：{e}\n"
               "★讀不到不等於沒問題★ —— 這道關卡沒跑成，視為失敗。")
        print(f"[skips] {msg}")
        annotate("skip 守衛：報告讀不到", msg)
        return 1

    try:
        expectations = load_expectations()
    except (OSError, ValueError, json.JSONDecodeError) as e:
        msg = (f"讀不到/解析不了 {EXPECTATIONS_PATH}：{e}\n"
               "★沒有宣告檔就沒有判準★ —— 不可當成「沒有預期 skip」放行。")
        print(f"[skips] {msg}")
        annotate("skip 守衛：宣告檔壞了", msg)
        return 1

    matched, unexpected = classify(skipped, expectations)
    print(f"[skips] 被 skip 的測試：{count}")
    for e in expectations:
        hit = matched[e["id"]]
        flag = "!!" if len(hit) > e["max"] else "ok"
        print(f"  [{flag}] {e['id']}：{len(hit)}/{e['max']}  —— {e['why']}")
        for name, _why in hit:
            print(f"         {name}")
    for name, why in unexpected:
        print(f"  [??] 未宣告：{name}  —— {why}")

    problems = []
    for e in expectations:
        hit = len(matched[e["id"]])
        if hit > e["max"]:
            problems.append(f"已宣告的 skip「{e['id']}」變多了：{hit} > {e['max']}"
                            f"（原本的理由是：{e['why']}）")
    for name, why in unexpected:
        problems.append(f"未宣告的 skip：{name} —— {why}")

    if problems:
        body = "\n".join(problems) + (
            "\n\n被 skip 的測試不會讓套件變紅 ——「全套綠燈」跟「那批根本沒跑」"
            "長得一模一樣。\n"
            "若確認是環境造成（而不是一批測試被靜默關掉），請在 "
            "skip_expectations.json 新增條目並寫清楚 why；"
            "★不要只把 max 調大★。")
        print("\n[skips] ★skip 守衛不通過★")
        for line in body.splitlines():
            print(f"  {line}")
        annotate("skip 守衛不通過", body)
        return 1
    print("\n[skips] 所有 skip 都在宣告範圍內。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
