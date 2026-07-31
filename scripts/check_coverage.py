# -*- coding: utf-8 -*-
"""分層覆蓋率門檻（P2-07）。

★為什麼不是單一個全域門檻★
全域是 49%，但那個數字幾乎完全由 `main.py`（10,322 行、16%）主導。用全域門檻的話，
**共用層掉 10 個百分點也照樣綠燈** —— 因為分母太大。所以按「這一層是什麼」分開設：

  * `cmuh_common/`（含 roster）＝共用邏輯層，測得動也該測，門檻最高。
  * 進入點（main / consult_query / autoclock / scheduler…）＝大量 Tk 與 Win32
    自動化，單元測試本來就搆不到；門檻只用來擋「整批掉下去」。

★門檻是【防退步】不是【追目標】★
設在目前值下面一點點（留給正常波動），任何一次明顯退步都會紅。覆蓋率上升之後
記得把門檻跟著提上來（`--update`），否則漲上去的部分可以被無聲地吃回去。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

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
    """在 GitHub Actions 上輸出 annotation（理由見 scripts/check_skips.py 的同名函式：
    job log 要 repo admin 才下載得到，annotation 走 check-runs API 是公開可讀的）。"""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    esc = (str(body).replace("%", "%25")
           .replace("\r", "%0D").replace("\n", "%0A"))
    print(f"::error title={title}::{esc}")


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FLOORS_FILE = os.path.join(REPO_ROOT, "coverage_floors.json")


def classify(path: str) -> str:
    norm = path.replace("\\", "/")
    if "/roster/" in norm or "cmuh_common/" in norm:
        return "shared"
    return "entrypoints"


def layer_percentages(cov_json: dict) -> dict:
    """→ {layer: (covered, statements, percent)}；沒有任何檔案的層不出現。"""
    acc: dict = {}
    for path, data in cov_json.get("files", {}).items():
        s = data["summary"]
        layer = classify(path)
        cov, tot = acc.get(layer, (0, 0))
        acc[layer] = (cov + s["covered_lines"], tot + s["num_statements"])
    out = {}
    for layer, (cov, tot) in acc.items():
        out[layer] = (cov, tot, (100.0 * cov / tot) if tot else 0.0)
    return out


def main(argv: list) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("coverage_json")
    ap.add_argument("--update", action="store_true",
                    help="把目前的百分比寫回門檻（覆蓋率提升之後用）")
    args = ap.parse_args(argv)

    # ★[2026-07-31] 這一步現在帶 `if: always()`★ 前面的 pytest 若紅了，cov.json
    #   可能根本不存在。讀不到要說清楚是「沒跑成」，不是覆蓋率退步 —— 但一樣算失敗
    #   （讀不到不等於沒問題）。
    try:
        with open(args.coverage_json, encoding="utf-8") as fh:
            cov = json.load(fh)
    except (OSError, json.JSONDecodeError) as e:
        msg = (f"讀不到/解析不了覆蓋率報告 {args.coverage_json}：{e}\n"
               "（pytest 沒跑完就不會有這個檔）★讀不到不等於沒問題★，視為失敗。")
        print(f"[coverage] {msg}")
        annotate("覆蓋率門檻：報告讀不到", msg)
        return 1
    with open(FLOORS_FILE, encoding="utf-8") as fh:
        raw = json.load(fh)
    floors = {k: v for k, v in raw.items() if not k.startswith("//")}

    layers = layer_percentages(cov)
    total = float(cov["totals"]["percent_covered"])
    layers["total"] = (cov["totals"]["covered_lines"],
                       cov["totals"]["num_statements"], total)

    failed = []
    for name in sorted(floors):
        if name not in layers:
            # ★分層消失＝分類規則跟目錄結構脫節，不可當成通過★
            failed.append(f"{name}: 這一層在覆蓋率報告裡找不到任何檔案"
                          f"（classify() 的規則是不是跟不上目錄搬動了？）")
            continue
        _cov, tot, pct = layers[name]
        floor = float(floors[name])
        mark = "FAIL" if pct + 1e-9 < floor else "ok  "
        print(f"  [{mark}] {name:12} {pct:5.1f}%  (門檻 {floor:.1f}%, {tot} 行)")
        if pct + 1e-9 < floor:
            failed.append(f"{name}: {pct:.1f}% < 門檻 {floor:.1f}%")

    if args.update:
        for name in floors:
            if name in layers:
                # 留 1 個百分點的正常波動空間
                raw[name] = round(max(0.0, layers[name][2] - 1.0), 1)
        with open(FLOORS_FILE, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print(f"\n已更新門檻：{FLOORS_FILE}")
        return 0

    if failed:
        print("\n[coverage] ★覆蓋率退步★")
        for line in failed:
            print(f"  {line}")
        print("  補上對應的測試，或（若確有正當理由）跑 "
              "`python scripts/check_coverage.py <json> --update` 並在 PR 說明原因。")
        annotate("覆蓋率門檻不通過", "\n".join(failed))
        return 1
    print("\n[coverage] 各層都在門檻之上。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
