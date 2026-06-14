# -*- coding: utf-8 -*-
"""換電腦時的開發環境搬家腳本(冪等,可重複執行)。

目的:讓新電腦的 Claude Code 在做完後,能呼叫 codex(GPT-5.5, reasoning=high)
做 push 前 diff 審查。這些都是「本機」設定、不會隨 git 跟過去,所以才需要這支。

它會自動處理:
  1. codex CLI(npm i -g @openai/codex)
  2. ~/.codex/config.toml:model=gpt-5.5 + model_reasoning_effort=high(頂層 → 所有專案)
  3. Claude Code 註冊 codex MCP(user scope) → 讓 Claude 能叫 mcp__codex__codex
  4. ~/.claude/CLAUDE.md:push 前先 codex 審查的規則
  5. 開發工具:ruff / pytest / pyright

執行後「還需你手動」:codex 登入、Claude Code 登入、複製舊機器的 settings/ 密碼。

用法:雙擊同資料夾的 dev-env-setup.cmd,或直接 `python dev-env-setup.py`。
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path.home()


def step(n: int, msg: str) -> None:
    print(f"\n[{n}] {msg}")


def ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def warn(msg: str) -> None:
    print(f"  [!] {msg}")


def have(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def run(args: list) -> tuple:
    """跑外部命令,回 (returncode, 合併輸出)。不丟例外。
    [codex review] Windows 上 npm/claude/codex 是 .cmd shim,subprocess(shell=False)
    不會套 PATHEXT → 用 bare 名稱會 WinError 2;先用 shutil.which 解析成完整路徑
    (含 .cmd)再執行。傳完整路徑(如 sys.executable)時 which 原樣回傳。"""
    exe = shutil.which(args[0]) or args[0]
    try:
        p = subprocess.run([exe, *args[1:]], capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as exc:  # noqa: BLE001
        return 1, str(exc)


def ensure_codex_cli() -> None:
    step(1, "確認 codex CLI")
    if have("codex"):
        _, out = run(["codex", "--version"])
        ver = out.strip().splitlines()[0] if out.strip() else ""
        ok(f"codex 已安裝 {ver}".rstrip())
        return
    if have("npm"):
        warn("codex 未安裝,嘗試 npm i -g @openai/codex ...")
        run(["npm", "i", "-g", "@openai/codex"])
        if have("codex"):
            ok("codex 安裝完成")
        else:
            warn("codex 安裝失敗,請手動執行:npm i -g @openai/codex")
    else:
        warn("找不到 npm。請先裝 Node.js(https://nodejs.org),再執行:"
             "npm i -g @openai/codex")


def merge_codex_config(content: str) -> str:
    """把 model=gpt-5.5 + model_reasoning_effort=high 放到頂層,保留其餘內容
    (notify / [windows] / [projects.*] ...)。

    只移除「第一個真正的 [section] 之前、且不在三引號多行字串內」的舊
    model/effort 行,稍後把正確值放最前面。
    [codex review] 追蹤 \"\"\" 與 ''' 多行字串狀態:避免刪到別的鍵的多行字串值
    裡剛好長得像 model= 的行,也避免把多行字串內的 [ 誤判成 section 開頭。
    section 內(如 [projects.*])若也有 model= 一律保留。
    """
    want = ['model = "gpt-5.5"', 'model_reasoning_effort = "high"']
    kept: list = []
    in_ml = False          # 是否在三引號多行字串內
    delim = ""             # 目前多行字串的結束符
    seen_section = False    # 是否已遇到第一個真正的 [section]
    for ln in content.splitlines():
        if in_ml:
            kept.append(ln)
            if delim in ln:
                in_ml = False
                delim = ""
            continue
        stripped = ln.lstrip()
        if stripped.startswith("["):
            seen_section = True
        drop = (not seen_section) and bool(
            re.match(r"\s*(model|model_reasoning_effort)\s*=", ln))
        if not drop:
            kept.append(ln)
        # 這行是否開啟了一段尚未閉合的三引號字串(出現奇數個 delim)
        for d in ('"""', "'''"):
            if ln.count(d) % 2 == 1:
                in_ml = True
                delim = d
                break
    return "\n".join(want + kept).rstrip("\n") + "\n"


def ensure_codex_config() -> None:
    step(2, "設定 codex 全域:gpt-5.5 + reasoning_effort=high(頂層,套用所有專案)")
    cfg = HOME / ".codex" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    if cfg.exists():
        content = cfg.read_text(encoding="utf-8", errors="replace")
        cfg.write_text(merge_codex_config(content), encoding="utf-8")
        ok(f"已更新 {cfg}")
    else:
        cfg.write_text('model = "gpt-5.5"\nmodel_reasoning_effort = "high"\n',
                       encoding="utf-8")
        ok(f"已建立 {cfg}")


def register_codex_mcp() -> None:
    """用官方 `claude mcp add`(不手改 ~/.claude.json,避免破壞 JSON)。
    不帶 -c:model/effort 由 config.toml 提供,避免跨 shell 引號地獄。"""
    step(3, "在 Claude Code 註冊 codex MCP(user scope)")
    manual = "  claude mcp add codex --scope user -- codex mcp-server"
    if not have("claude"):
        warn("找不到 claude CLI。Claude Code 裝好後,執行:")
        warn(manual)
        return
    add_cmd = ["claude", "mcp", "add", "codex", "--scope", "user",
               "--", "codex", "mcp-server"]
    # [codex review] 永不 remove:既有的 codex 註冊本來就能用(它就是啟動
    # `codex mcp-server`,讀 config.toml 拿 high),沒有覆蓋的必要。先 add,失敗
    # 若是「已存在」就保留現狀;其他錯誤才提示手動 —— 任何情況都不會把使用者
    # 原有的註冊弄不見。
    rc, out = run(add_cmd)
    if rc == 0:
        ok("已註冊 codex MCP(model/effort 由 ~/.codex/config.toml 的 high 提供)")
        return
    low = out.lower()
    if any(s in low for s in ("already exists", "already configured",
                              "already registered")):
        ok("codex MCP 已存在,保留現有註冊(現狀即可運作:啟動 codex mcp-server"
           " 讀 config.toml 的 high)")
        return
    warn("自動註冊失敗(未更動任何既有設定)。請手動執行:")
    warn(manual)
    if out.strip():
        warn("(claude 輸出:" + out.strip()[:150] + ")")


_RULE = """
## Codex (GPT-5.5) diff review before pushing — ALL projects
Before `git push` in any project (a push may auto-deploy to production), first run a
Codex GPT-5.5 review of exactly what will be pushed, and only push if it passes:
1. Get the diff (unpushed commits and/or working changes).
2. Review with Codex (model gpt-5.5): prefer the codex MCP tools if loaded, else CLI
   `codex exec -c model="gpt-5.5" --skip-git-repo-check` (or `codex review`).
3. Show findings; fix anything blocking; re-review.
4. Only `git push` once the review returns no blocking issues.
"""


def ensure_claude_md_rule() -> None:
    step(4, "寫入全域 CLAUDE.md 的 codex 審查規則")
    claude_md = HOME / ".claude" / "CLAUDE.md"
    marker = "Codex (GPT-5.5) diff review before pushing"
    if claude_md.exists() and marker in claude_md.read_text(
            encoding="utf-8", errors="replace"):
        ok("規則已存在,略過")
        return
    claude_md.parent.mkdir(parents=True, exist_ok=True)
    with claude_md.open("a", encoding="utf-8") as fh:
        fh.write("\n" + _RULE.strip() + "\n")
    ok(f"已寫入規則到 {claude_md}")


def ensure_dev_tools() -> None:
    step(5, "安裝開發工具(ruff / pytest / pyright)")
    if not (have("python") or have("py")):
        warn("找不到 python,請先安裝 Python 3.10+")
        return
    rc, out = run([sys.executable, "-m", "pip", "install", "-q",
                   "ruff", "pytest", "pyright"])
    if rc == 0:
        ok("ruff / pytest / pyright 已安裝")
    else:
        warn("安裝失敗(可稍後手動 pip install ruff pytest pyright):"
             + out.strip()[:200])


def print_manual_steps() -> None:
    step(6, "還需要你手動完成的(腳本不便代勞)")
    print("  1. codex 登入:執行  codex  依指示用 ChatGPT 登入(或 codex login)")
    print("  2. Claude Code 用同一個帳號登入")
    print("  3. git clone 你的專案,並把舊電腦的 settings\\ 資料夾複製進去")
    print("     (settings/ 含明文帳密、在 .gitignore 內,不會隨 git 過來)")
    print("  4. 重開 Claude Code 讓 codex MCP 生效")
    print("\n完成!之後 Claude Code 做完事就能呼叫 codex(GPT-5.5 high)做 diff 審查。")


def main() -> int:
    print("===== 開發環境搬家:Claude Code + Codex(GPT-5.5 high)diff 審查 =====")
    ensure_codex_cli()
    ensure_codex_config()
    register_codex_mcp()
    ensure_claude_md_rule()
    ensure_dev_tools()
    print_manual_steps()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"\n[錯誤] {exc}")
        sys.exit(1)
