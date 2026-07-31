# -*- coding: utf-8 -*-
"""型別債棘輪（type-debt ratchet）—— 舊債可以不還，但【不准再欠】。

★[2026-07-30 第二輪外審 P2-07] 為什麼不是「打開 strict」★
外審建議「pyright strict」。實測過了：把 strict 開在最新、最乾淨的六個模組上就有
**481 個錯誤**（絕大多數是 Tk 與注入式 callable 的 `partially unknown`）；把
`pyrightconfig.json` 目前關掉的 11 條規則全開共 **273 個錯誤**。全開只會得到一個
永遠紅燈、因此永遠被忽略的關卡 —— 那比沒有還糟。

所以這裡做的是【棘輪】：記下每條被關掉的規則【現在】有哪些診斷，新的診斷就紅。

★[外審第 1 輪] 比對的是【診斷本身】，不是總數★
我第一版只比每條規則的錯誤【總數】。那樣「修掉一個、又新增一個」總數不變 →
CI 報告「沒有新增型別債」，跟這支腳本宣稱的事情正好相反。
現在的指紋是 `(規則, 檔案, 所在函式, 訊息, 那一行的原始碼)` 的計數：
  * 不含行號 —— 上下增刪幾行不該讓整批診斷看起來像新的。
  * 用計數而不是集合 —— 同一個位置出現兩次也分得出來。

★已知殘留的洞（認了）★：同一個函式裡兩行【字面完全相同】的程式碼產生同一則訊息時，
互換仍然抵銷。再細分就只剩行號，而行號會讓每次編輯都變紅（→ 被忽略）。這個取捨是
刻意的：那種情形下兩個診斷實質上是同一個缺陷樣式，混淆的代價極小。

★[2026-07-31] 基線是【環境相依】的，而且沒辦法完全消除★
這道棘輪第一次在 CI 上跑就紅了，而本機是綠的。原因兩次都不是程式碼：
  * 本機 ortools 停在 9.14.6206，而 `requirements-lazy.txt` 自己釘的是 9.15.6755
    （CI 裝的）。9.15 的型別介面不再對外顯示 `CpModel.NewBoolVar/Minimize/
    AddExactlyOne`（**執行期仍然可用**，roster 求解測試在兩個版本上都綠）→ 多 3 筆。
  * 本機 beautifulsoup4 停在 4.13.5，CI 裝 4.15.0 → main.py 少 15 筆
    `NavigableString`/`PageElement` 診斷。

`requirements.txt` 是**刻意**用範圍的（上界只擋下一個主版本，好讓診間電腦拿得到
安全更新），所以 CI 與開發機的相依版本本來就不會一致、而且會各自漂移。
結論：**指紋比對無法在兩個環境之間穩定**。因此
  * 「新增診斷」仍然是紅燈 —— 那是這道棘輪的主要目的。
  * 「診斷消失」降為警告（見 main() 裡的說明與代價）。
本機跑出來跟 CI 不一致時，先看輸出開頭的「環境：」那一行對照版本，
再去懷疑程式碼。

還債流程：修好之後跑 `python scripts/type_debt.py --update`；某條規則歸零時直接把它
從 `pyrightconfig.json` 的關閉清單移到啟用（並從基線刪掉，`tests/test_ci_gates_*`
會檢查兩邊一致）。
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import subprocess
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


def annotate(title: str, body: str, level: str = "error") -> None:
    """在 GitHub Actions 上輸出 annotation（理由見 scripts/check_skips.py 的同名函式：
    job log 要 repo admin 才下載得到，annotation 走 check-runs API 是公開可讀的）。"""
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    esc = (str(body).replace("%", "%25")
           .replace("\r", "%0D").replace("\n", "%0A"))
    print(f"::{level} title={title}::{esc}")


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASELINE_FILE = os.path.join(REPO_ROOT, "type_debt_baseline.json")

# 這些套件會帶 type stubs → 版本不同，pyright 的診斷就可能不同。
# 基線是環境相依的（見本檔開頭），所以每次都把環境印出來/annotate 出來，
# 讓「本機綠、CI 紅」第一時間就能對照，而不是靠猜。
_ENV_PACKAGES = ("pyright", "ortools", "beautifulsoup4", "selenium", "requests",
                 "lxml", "Pillow", "psutil", "pywin32", "openpyxl", "reportlab",
                 "python-docx", "protobuf")


def _environment_note() -> str:
    import importlib.metadata as _md
    parts = [f"python={sys.version.split()[0]}"]
    for name in _ENV_PACKAGES:
        try:
            parts.append(f"{name}={_md.version(name)}")
        except Exception:                       # noqa: BLE001 - 沒裝就標明
            parts.append(f"{name}=(未安裝)")
    return "環境：" + " ".join(parts)
TEMP_CONFIG = os.path.join(REPO_ROOT, ".pyright_type_debt.json")
MAIN_CONFIG = os.path.join(REPO_ROOT, "pyrightconfig.json")


def fingerprint(diag: dict) -> str:
    """`檔案相對路徑|訊息` —— ★刻意不含行號★

    行號會因為上下增刪幾行而整批改變，那會讓棘輪每次都紅（然後被忽略）。
    檔案 + 訊息已經足以定位，而且對「換個地方犯同一個錯」仍然敏感。
    """
    path = str(diag.get("file", "")).replace("\\", "/")
    # ★路徑必須與「repo 被 clone 到哪裡」無關★
    #   pyright 回報的絕對路徑，大小寫（`c:` vs `C:`）與 CI 的工作目錄都跟本機不同；
    #   直接 relpath 會得到 `../../程式/CMUHdermatology-main/src/main.py` 這種東西，
    #   於是 CI 上每一個指紋都算「新的」→ 棘輪紅燈上線 → 被忽略。
    #   一律截到 `src/` 開頭：那是這個 repo 裡唯一被檢查的樹。
    marker = "/src/"
    idx = path.rfind(marker)
    if idx >= 0:
        path = path[idx + 1:]
    else:
        try:
            path = os.path.relpath(path, REPO_ROOT).replace("\\", "/")
        except ValueError:
            pass
    # `str.split()` 會吃掉包含 U+00A0 在內的所有 Unicode 空白（搭配上面的
    # UTF-8 解碼，pyright 的縮排不會變成指紋的一部分）。
    message = " ".join(str(diag.get("message", "")).split())
    # ★[2026-07-30 外審第 2 輪] 加上【出錯那一行的原始碼】★
    #   只用 `檔案|訊息` 的話，「修掉一個、在同一個檔案別的地方又寫出一個
    #   完全相同的錯」會讓計數不變 → 棘輪看不到。加上那一行的程式碼就分得出來，
    #   而且【不含行號】，所以單純上下增刪行不會讓整批指紋看起來像新的。
    #   代價：那一行的字面改了（例如改變數名）指紋就算新的 —— 要跑 --update。
    #   那個摩擦是刻意的：診斷真的移位了就該重新確認一次。
    source = _source_line(diag)
    # ★[2026-07-30 外審第 3 輪] 再加上【所在函式/類別】★
    #   同一個檔案裡出現兩行【字面完全相同】的程式碼（實際存在：基線裡有幾個
    #   計數是 2–5 的 `cells = row.find_all(...)`）時，`path|message|source` 還是
    #   一樣 → 「修一個、別處又寫一個一模一樣的」計數不變，棘輪看不到。
    scope = _enclosing_scope(diag)
    return f"{path}|{scope}|{message}|{source}"


_SCOPE_CACHE: dict = {}


def _scope_map(path: str) -> dict:
    """{行號(0-based): 所在的 類別.函式 完整名稱}。解析不了回空 dict。

    用 `ast` 而不是往回掃縮排：縮排推測在多行字串、裝飾子、嵌套函式
    上都會錯，而認錯範圍會讓指紋無端變動（→ 棘輪誤報 → 被忽略）。
    """
    if path in _SCOPE_CACHE:
        return _SCOPE_CACHE[path]
    mapping: dict = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            tree = ast.parse(fh.read())
    except (OSError, SyntaxError, ValueError):
        _SCOPE_CACHE[path] = mapping
        return mapping

    def walk(node, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = getattr(child, "name", None)
            if name and isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                           ast.ClassDef)):
                qual = f"{prefix}.{name}" if prefix else name
                start = getattr(child, "lineno", 1) - 1
                end = getattr(child, "end_lineno", start + 1) - 1
                for line in range(start, end + 1):
                    mapping[line] = qual        # 內層最後寫，自然覆蓋外層
                walk(child, qual)
            else:
                walk(child, prefix)

    walk(tree, "")
    _SCOPE_CACHE[path] = mapping
    return mapping


def _enclosing_scope(diag: dict) -> str:
    """診斷所在的 類別.函式 名稱；在模組層或查不到時回空字串。"""
    try:
        line_no = int(diag["range"]["start"]["line"])
    except (KeyError, TypeError, ValueError):
        return ""
    return _scope_map(str(diag.get("file", ""))).get(line_no, "")


def _source_line(diag: dict) -> str:
    """出錯那一行的程式碼（去頭尾空白）。讀不到回空字串。"""
    try:
        line_no = int(diag["range"]["start"]["line"])
    except (KeyError, TypeError, ValueError):
        return ""
    path = str(diag.get("file", ""))
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for i, text in enumerate(fh):
                if i == line_no:
                    return " ".join(text.split())
    except OSError:
        return ""
    return ""


def _rule_diagnostics(rule: str) -> dict:
    """把單一規則打開，回傳 {指紋: 出現次數}。

    ★設定檔一定要寫在 repo 根目錄★：pyright 的 `include` 是相對設定檔位置解析的，
    寫到 %TEMP% 會變成找不到任何檔案而回報 0 個錯誤 —— 一個看起來全綠的假結果。
    """
    with open(MAIN_CONFIG, encoding="utf-8") as fh:
        cfg = json.load(fh)
    cfg[rule] = "error"
    with open(TEMP_CONFIG, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh)
    try:
        # ★[2026-07-30 外審第 2 輪] 必須明寫 encoding="utf-8"★
        #   `text=True` 是拿【系統 locale】解碼。pyright 吐的是 UTF-8，而它的診斷
        #   訊息用 U+00A0（不斷行空白）做縮排 —— 在 cp936 的機器上那個位元組會
        #   被解成「聽」。基線裡 273 個指紋裡有 83 個中鐡，而 CI runner 的代碼頁不同
        #   → 每一個指紋都算「新的」→ 棘輪紅燈上線 → 被忽略。
        cp = subprocess.run(
            [sys.executable, "-m", "pyright", "-p", TEMP_CONFIG, "--outputjson"],
            cwd=REPO_ROOT, capture_output=True,
            encoding="utf-8", errors="replace", check=False)
        try:
            report = json.loads(cp.stdout)
        except ValueError as e:
            raise SystemExit(
                f"[type-debt] 無法解析 pyright 輸出（規則 {rule}）：{e}\n"
                f"  ★解析不了不等於沒有型別債★ —— 這道關卡沒跑成，視為失敗。\n"
                f"  stdout 前 500 字：{cp.stdout[:500]}\n"
                f"  stderr 前 500 字：{cp.stderr[:500]}") from e
        counts: dict = {}
        for diag in report.get("generalDiagnostics", []):
            if diag.get("severity") != "error" or diag.get("rule") != rule:
                continue
            fp = fingerprint(diag)
            counts[fp] = counts.get(fp, 0) + 1
        return counts
    finally:
        try:
            os.remove(TEMP_CONFIG)
        except OSError:
            pass


def diff_counts(baseline: dict, current: dict) -> tuple:
    """→ (新增的 {指紋: 多幾個}, 消失的 {指紋: 少幾個})。"""
    added, gone = {}, {}
    for fp in set(baseline) | set(current):
        delta = current.get(fp, 0) - baseline.get(fp, 0)
        if delta > 0:
            added[fp] = delta
        elif delta < 0:
            gone[fp] = -delta
    return added, gone


def _load_baseline() -> dict:
    with open(BASELINE_FILE, encoding="utf-8") as fh:
        return {k: v for k, v in json.load(fh).items() if not k.startswith("//")}


def main(argv: list) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true",
                    help="把目前的診斷寫回基線（還完債之後用）")
    args = ap.parse_args(argv)

    baseline = _load_baseline()
    current = {rule: _rule_diagnostics(rule) for rule in sorted(baseline)}

    print(_environment_note())
    any_added = any_gone = False
    added_detail = []
    gone_detail = []
    for rule in sorted(current):
        added, gone = diff_counts(baseline[rule], current[rule])
        n_now = sum(current[rule].values())
        n_was = sum(baseline[rule].values())
        mark = "↑ " if added else ("↓ " if gone else "  ")
        print(f"{mark}{rule:32} {n_now:4}  (基線 {n_was})")
        for fp, k in sorted(added.items()):
            print(f"      + {k}x {fp}")
            added_detail.append((fp, k))
            any_added = True
        for fp, k in sorted(gone.items()):
            print(f"      - {k}x {fp}")
            gone_detail.append((fp, k))
            any_gone = True

    if args.update:
        with open(BASELINE_FILE, encoding="utf-8") as fh:
            raw = json.load(fh)
        raw.update(current)
        with open(BASELINE_FILE, "w", encoding="utf-8") as fh:
            json.dump(raw, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.write("\n")
        print(f"\n已更新基線：{BASELINE_FILE}")
        return 0

    if any_added:
        print("\n[type-debt] ★新增了型別債★（上面 `+` 的那幾筆）")
        print("  修掉它們，或（若確有正當理由）跑 "
              "`python scripts/type_debt.py --update` 並在 PR 說明原因。")
        print("  ※ 若本機是綠的、只有 CI 紅：先確認本機裝的相依版本與 "
              "requirements*.txt 的 pin 一致（基線是環境相依的，見本檔開頭）。")
        annotate("型別債棘輪：新增了型別債",
                 _environment_note() + "\n"
                 + "\n".join(f"+{k}x {fp}" for fp, k in added_detail))
        return 1
    if any_gone:
        # ★[2026-07-31] 「診斷消失」從紅燈降為警告★
        #
        # 原本這裡是 `return 1`，理由是「不鎖住的話，還完的債可以被無聲地欠回去」。
        # 那個顧慮成立，但它的誤報來源壓過了它的價值：
        #   指紋是 pyright 的診斷，而 pyright 的診斷取決於【每一個已安裝套件的
        #   type stubs】。requirements.txt 是【刻意】用範圍的（上界只擋下一個主版本，
        #   好讓診間電腦拿得到安全更新，例如 P2-07 把 Pillow 下限拉到 12.3.0）——
        #   也就是說 CI 與開發機的相依版本【本來就不會一致】，而且會各自隨時間漂移。
        #   實測：beautifulsoup4 4.13.5→4.15.0 讓 main.py 少掉 15 筆
        #   NavigableString/PageElement 診斷；ortools 9.14→9.15 則多出 3 筆。
        #   兩者都與被推送的改動完全無關。
        #
        # 而 CI 從加上這道關卡起連紅五輪、後面的步驟因此從沒跑過 —— 這正是本檔開頭
        # 說要避免的「永遠紅燈因而被忽略的關卡」。
        #
        # ★誠實記下代價★：這確實削弱了「還完的債被無聲欠回去」的防護
        #   （要同時發生：債被修好、沒人跑 --update、日後又寫回一模一樣的診斷）。
        #   `新增` 診斷仍然是紅燈 —— 那才是這道棘輪的主要目的。
        print("\n[type-debt] 有型別債不見了（不是紅燈，但請確認）")
        print("  修好了就跑 `python scripts/type_debt.py --update` 把門檻鎖住；")
        print("  若只是相依套件的 stubs 變了（見上面的環境行），一樣跑 --update。")
        annotate("型別債棘輪：基線裡的診斷消失了（未擋，請確認）",
                 _environment_note() + "\n"
                 + "\n".join(f"-{k}x {fp}" for fp, k in gone_detail),
                 level="warning")
        return 0
    print("\n[type-debt] 沒有新增型別債。")
    return 0


def _main_guarded(argv: list) -> int:
    """★關卡自己爆掉時也要說得出話★

    崩潰路徑（基線讀不到、pyright 跑不起來）原本只留一個 traceback 在 job log 裡，
    而 job log 要 repo admin 才下載得到 —— 對只看得到 annotation 的人來說，
    「棘輪爆炸」跟「棘輪判定失敗」長得一模一樣。
    """
    try:
        return main(argv)
    except Exception as e:                       # noqa: BLE001
        import traceback
        detail = traceback.format_exc()
        print(detail)
        annotate("型別債棘輪：關卡本身失敗",
                 f"{type(e).__name__}: {e}\n{_environment_note()}\n"
                 f"{detail[-1200:]}")
        return 1


if __name__ == "__main__":
    raise SystemExit(_main_guarded(sys.argv[1:]))
